# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column drawn given a column of a linked row (ADR 0023 section 3, SEAM-FIDELITY-SPEC section 2).

``given: <relationship>.<column>`` draws a column under the weight table of the level the row's link
reaches: the level of the parent's final value. The fixtures make every table exact: each parent level
allows one child level only, so a row either follows its parent or the plan is wrong. What is pinned,
on kernel 0.2.0 (the only kernel that generates it):

* **The child follows its parent, row by row**, whether the parent column is drawn from its own levels,
  is the ``to`` column of a contract dependency, or is a column of a reference expose; a categorical
  and a histogram child both.
* **The refusals** are ``FLX-3402`` and come before a row exists: a relationship that may reach no row,
  a parent column that is not a categorical drawn from its own levels, a rule that sets it, weight
  tables that do not name the parent's levels, a path of two links, a list that holds a path, a
  relationship the contract does not declare, a cycle, and kernel 0.1.0.
* **Two paths, one answer**: the column path leaves such a plan to the per-row path, and a column
  generated alone is the column of the row pass.
* The plan lists the plan it reads, and a plan with no such column keeps its document.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.generator import columns
from flux_engine.generator.reference import ReferenceRows, build_reference_plan
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
)
from flux_engine.generator.rules import plan_rules
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.keys import compile_keys, mark_key_columns
from flux_engine.seam.links import LinkEntry

from .test_relations import _column, _expose, _h, _primary, _rel

pytestmark = pytest.mark.unit

SIM_ID = "sim-given-link"
ACCOUNTS, INVOICES = 40, 500

#: Each region allows one channel, each tier one band of the amount histogram.
CHANNEL = {
    "given": "account.a_region",
    "levelWeightsGiven": {
        "north": {"web": "1", "shop": "0"},
        "south": {"web": "0", "shop": "1"},
        "west": {"web": "1", "shop": "0"},
    },
}
AMOUNT = {
    "range": {"min": "0", "max": "100"},
    "bins": ["0", "50", "100"],
    "given": "account.a_tier",
    "binWeightsGiven": {"gold": ["0", "1"], "silver": ["1", "0"]},
}
ACCOUNT_STATISTICS: Dict[str, Any] = {
    "a_region": {"levels": {"north": "0.5", "south": "0.3", "west": "0.2"}},
    "a_tier": {"levels": {"gold": "0.5", "silver": "0.5"}},
}


def contract(*, account_invariants: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    account = _expose(
        "account",
        [
            _column("a_id", "STRING"),
            _column("a_region", "STRING", False),
            _column("a_tier", "STRING", False),
        ],
        _primary("account", "a_id"),
    )
    invoice = _expose(
        "invoice",
        [
            _column("i_id", "STRING"),
            _column("i_account", "STRING", False),
            _column("i_channel", "STRING", False),
            _column("i_amount", "INT", False),
        ],
        {
            **_primary("invoice", "i_id"),
            "relationships": [_rel("account", "account", ["i_account"], ["a_id"])],
        },
    )
    account["contract"]["invariants"] = (
        [{"id": "tier_of_region", "dependency": {"from": ["a_region"], "to": ["a_tier"]}}]
        if account_invariants is None
        else account_invariants
    )
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.link",
        "exposes": [account, invoice],
    }


def root_for(seed: int = 5) -> SeedTreeRoot:
    return derive_root(
        digest_bytes("sha256:" + "a" * 64), seed, seed_tree_version=kernel2.SEED_TREE_VERSION
    )


def plans_of(
    document: Optional[Dict[str, Any]] = None,
    *,
    account: Optional[Dict[str, Any]] = None,
    invoice: Optional[Dict[str, Any]] = None,
    references: Optional[Mapping[str, ReferenceRows]] = None,
    counter_based: bool = True,
    relate: bool = True,
    links: Sequence[LinkEntry] = (),
) -> Dict[str, GeneratorPlan]:
    document = contract() if document is None else document
    keys = compile_keys(document)
    assert keys is not None
    declared = {
        "account": (ACCOUNT_STATISTICS if account is None else account, ACCOUNTS),
        "invoice": (
            {
                "i_channel": CHANNEL,
                "i_amount": AMOUNT,
                **({} if invoice is None else invoice),
            },
            INVOICES,
        ),
    }
    version = GENERATOR_VERSION_2 if counter_based else GENERATOR_VERSION
    plans: Dict[str, GeneratorPlan] = {}
    for expose in document["exposes"]:
        expose_id = expose["exposeId"]
        expose_keys = keys.expose(expose_id)
        compiled = mark_key_columns(compile_expose(expose), expose_keys)
        if references and expose_id in references:
            plans[expose_id] = build_reference_plan(
                compiled,
                sim_id=SIM_ID,
                seam_digest=_h(expose_id),
                table=references[expose_id],
                generator_version=version,
                where=f"reference expose {expose_id!r}",
            )
            continue
        columns_, count = declared[expose_id]
        related = related_columns(expose_keys)
        rules = plan_rules(
            contract_document=document,
            expose_id=expose_id,
            settings=None,
            related=related,
            mapping=None,
            has_fidelity=True,
            strict=False,
            flux_rules=None,
            keys=expose_keys,
            rows=count,
            null_fractions={
                name: block["nullFraction"]
                for name, block in columns_.items()
                if "nullFraction" in block
            },
            counter_based=counter_based,
            links=links if expose_id == "invoice" else (),
        )
        plans[expose_id] = build_plan(
            compiled,
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=count,
            fidelity={"columns": columns_},
            related=related,
            rules=rules,
            generator_version=version,
        )
    return relate_plans(plans, keys) if relate else plans


def table(plan: GeneratorPlan, rows: Any) -> List[Dict[str, Any]]:
    names = [column_plan.column.name for column_plan in plan.columns]
    return [dict(zip(names, row, strict=True)) for row in rows]


def refused(change: Callable[[], Any], code: str = "FLX-3402") -> str:
    with pytest.raises(FluxError) as caught:
        change()
    assert caught.value.code.code == code
    return str(caught.value)


@pytest.fixture(scope="module")
def world() -> Dict[str, List[Dict[str, Any]]]:
    plans = plans_of()
    root = root_for()
    return {name: table(plan, generate_rows(plan, root=root)) for name, plan in plans.items()}


def test_a_categorical_child_follows_the_level_of_its_parents_row(
    world: Dict[str, List[Dict[str, Any]]],
) -> None:
    accounts = {row["a_id"]: row for row in world["account"]}
    expected = {"north": "web", "south": "shop", "west": "web"}
    seen = set()
    for row in world["invoice"]:
        parent = accounts[row["i_account"]]
        assert row["i_channel"] == expected[parent["a_region"]], row["i_id"]
        seen.add((parent["a_region"], row["i_channel"]))
    assert seen == {("north", "web"), ("south", "shop"), ("west", "web")}


def test_a_histogram_child_follows_the_level_a_dependency_set(
    world: Dict[str, List[Dict[str, Any]]],
) -> None:
    # a_tier is the `to` column of a contract dependency on a_region: the child reads what the
    # parent's own pass wrote, which is the dependency's value and not a draw of the child's own.
    accounts = {row["a_id"]: row for row in world["account"]}
    tiers = {}
    for row in world["account"]:
        tiers.setdefault(row["a_region"], set()).add(row["a_tier"])
    assert all(len(held) == 1 for held in tiers.values())
    seen = set()
    for row in world["invoice"]:
        tier = accounts[row["i_account"]]["a_tier"]
        assert (row["i_amount"] >= 50) == (tier == "gold"), row["i_id"]
        seen.add(tier)
    assert seen


def test_the_other_columns_of_the_row_and_the_draw_index_are_the_ones_it_always_has() -> None:
    # Only the weight table changes: the same plan with the given dropped draws the same uniforms.
    plain = plans_of(
        invoice={
            "i_channel": {"levels": {"web": "0.5", "shop": "0.5"}},
            "i_amount": {
                "range": {"min": "0", "max": "100"},
                "bins": ["0", "50", "100"],
                "binWeights": ["1", "1"],
            },
        }
    )
    linked = plans_of()
    root = root_for()
    a, b = generate_rows(plain["invoice"], root=root), generate_rows(linked["invoice"], root=root)
    for first, second in zip(a, b, strict=True):
        assert first[0] == second[0] and first[1] == second[1]  # key and foreign key


def test_a_column_of_a_reference_expose_is_a_parent_with_its_distinct_published_values() -> None:
    document = contract()
    document["exposes"][0]["contract"]["schema"][1]["name"] = "a_region"
    references = {
        "account": ReferenceRows(
            ("a_id", "a_region", "a_tier"),
            tuple(
                (f"acc{i}", ("north", "south", "west")[i % 3], ("gold", "silver")[i % 2])
                for i in range(6)
            ),
        )
    }
    document["exposes"][0]["contract"]["invariants"] = []
    plans = plans_of(document, references=references)
    root = root_for()
    accounts = {
        r["a_id"]: r for r in table(plans["account"], generate_rows(plans["account"], root=root))
    }
    rows = table(plans["invoice"], generate_rows(plans["invoice"], root=root))
    assert len({r["i_account"] for r in rows}) == 6
    expected = {"north": "web", "south": "shop", "west": "web"}
    for row in rows:
        assert row["i_channel"] == expected[accounts[row["i_account"]]["a_region"]]


def test_a_reference_column_that_publishes_null_has_no_level() -> None:
    references = {"account": ReferenceRows(("a_id", "a_region", "a_tier"), (("x", None, "gold"),))}
    refused(lambda: plans_of(references=references, account={}))


def test_the_plan_lists_the_plan_it_reads_and_a_plan_with_no_such_column_keeps_its_document() -> (
    None
):
    plans = plans_of()
    document = plans["invoice"].as_document()
    assert sorted(document["reads"]) == ["account"]
    assert document["relations"]["i_channel"]["given"] == {
        "relationship": "account",
        "parent": "account",
        "column": "a_region",
        "levels": ["north", "south", "west"],
    }
    assert "reads" not in plans["account"].as_document()
    plain = plans_of(
        invoice={
            "i_channel": {"levels": {"web": "0.5", "shop": "0.5"}},
            "i_amount": {"range": {"min": "0", "max": "100"}},
        }
    )
    assert "reads" not in plain["invoice"].as_document()
    assert {"i_channel", "i_amount"}.isdisjoint(plain["invoice"].as_document()["relations"])


@pytest.mark.parametrize(
    ("label", "build", "needle"),
    [
        (
            "relationship-may-reach-no-row",
            lambda: plans_of(invoice={"i_account": {"nullFraction": "0.2"}}),
            "may reach no row",
        ),
        (
            "relationship-has-a-links-condition",
            lambda: plans_of(links=[LinkEntry("invoice", "account", "i_amount > 100")]),
            "links condition",
        ),
        (
            "parent-draws-nulls",
            lambda: plans_of(
                account={
                    **ACCOUNT_STATISTICS,
                    "a_region": {
                        "levels": {"north": "0.5", "south": "0.3", "west": "0.2"},
                        "nullFraction": "0.1",
                    },
                }
            ),
            "nullFraction",
        ),
        (
            "parent-is-not-a-categorical",
            lambda: plans_of(
                account={**ACCOUNT_STATISTICS, "a_region": {"range": {"min": "1", "max": "3"}}}
            ),
            "categorical",
        ),
        (
            "a-table-misses-a-level",
            lambda: plans_of(
                account={
                    **ACCOUNT_STATISTICS,
                    "a_region": {"levels": {"north": "0.5", "east": "0.5"}},
                }
            ),
            "levelWeightsGiven declares levels",
        ),
        (
            "two-links",
            lambda: plans_of(
                invoice={"i_channel": {**CHANNEL, "given": "account.customer.a_region"}}
            ),
            "one link",
        ),
        (
            "a-list-holds-a-path",
            lambda: plans_of(
                invoice={"i_channel": {**CHANNEL, "given": ["i_amount", "account.a_region"]}}
            ),
            "only given",
        ),
        (
            "no-such-relationship",
            lambda: plans_of(invoice={"i_channel": {**CHANNEL, "given": "customer.a_region"}}),
            "no relationship",
        ),
        (
            "no-such-column",
            lambda: plans_of(invoice={"i_channel": {**CHANNEL, "given": "account.nothing"}}),
            "no column",
        ),
        (
            "a-derive-sets-the-parent",
            lambda: plans_of(
                contract(
                    account_invariants=[
                        {
                            "id": "region_of_tier",
                            "derive": "a_region",
                            "expr": "CASE WHEN a_tier = 'gold' THEN 'north' ELSE 'south' END",
                        }
                    ]
                ),
                account={
                    **ACCOUNT_STATISTICS,
                    "a_region": {"levels": {"north": "0.5", "south": "0.5"}},
                },
                invoice={
                    "i_channel": {
                        **CHANNEL,
                        "levelWeightsGiven": {
                            k: v for k, v in CHANNEL["levelWeightsGiven"].items() if k != "west"
                        },
                    }
                },
            ),
            "a rule sets",
        ),
    ],
)
def test_what_cannot_be_drawn_is_refused_before_a_row_exists(
    label: str, build: Callable[[], Any], needle: str
) -> None:
    assert needle in refused(build), label


def test_kernel_0_1_0_refuses_the_declaration_and_names_the_kernel() -> None:
    message = refused(lambda: plans_of(counter_based=False))
    assert "kernel 0.2.0" in message and "kernel 0.1.0" in message


def test_a_plan_the_contract_relates_nothing_in_refuses_the_link() -> None:
    from flux_engine.generator.relations import check_links_resolved

    plans = plans_of(relate=False)
    assert "no relationship" in refused(lambda: check_links_resolved(plans["invoice"]))


def test_the_column_path_leaves_the_plan_to_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = plans_of()
    root = root_for(9)
    seen: List[Tuple[str, bool]] = []
    column_rows = columns.column_rows

    def spy(*args: Any) -> Any:
        computed = column_rows(*args)
        seen.append((args[0].expose.expose_id, computed is not None))
        return computed

    outcomes: Dict[str, Any] = {}
    for label, patched in (("columns", spy), ("rows", lambda *args: None)):
        with monkeypatch.context() as patch:
            patch.setattr(columns, "column_rows", patched)
            outcomes[label] = (
                generate_rows(plans["invoice"], root=root),
                generate_row_slice(plans["invoice"], 13, 200, root=root),
                generate_column(plans["invoice"], "i_channel", root=root),
                generate_column(plans["invoice"], "i_amount", root=root),
            )
    assert outcomes["columns"] == outcomes["rows"]
    # The invoice plan is the per-row path's; its parent's rows, read through final_row, are not.
    assert [ok for expose, ok in seen if expose == "invoice"] == [False] * seen.count(
        ("invoice", False)
    )
    assert ("invoice", False) in seen and ("invoice", True) not in seen
    assert ("account", True) in seen
    names = [c.column.name for c in plans["invoice"].columns]
    rows = outcomes["rows"][0]
    assert outcomes["rows"][2] == tuple(row[names.index("i_channel")] for row in rows)
    assert outcomes["rows"][3] == tuple(row[names.index("i_amount")] for row in rows)


def test_a_slice_anywhere_is_the_same_rows_as_the_whole_table() -> None:
    plans = plans_of()
    root = root_for(3)
    whole = generate_rows(plans["invoice"], root=root)
    assert generate_row_slice(plans["invoice"], 100, 333, root=root) == whole[100:333]
    assert generate_rows(plans["invoice"], root=root_for(3)) == whole


# ---------------------------------------------------------------------------
# A column whose name holds a dot is that column, not a link (SEAM-FIDELITY-SPEC section 2)
# ---------------------------------------------------------------------------


def _with_a_column_named_like_a_link() -> Dict[str, Any]:
    """The invoice also holds a column called ``account.a_region``, as the link's own column is."""
    document = contract()
    document["exposes"][1]["contract"]["schema"].append(
        _column("account.a_region", "STRING", False)
    )
    return document


def test_a_given_that_is_exactly_a_column_of_the_expose_is_that_column_and_not_the_link() -> None:
    """``given: account.a_region`` reads the invoice's own column of that name, drawn on its own."""
    document = _with_a_column_named_like_a_link()
    own = {"levels": {"north": "0.4", "south": "0.3", "west": "0.3"}}
    plans = plans_of(document, invoice={"account.a_region": own})
    statistics = plans["invoice"].column_plan("i_channel").statistics
    assert statistics.given == "account.a_region" and statistics.given_link is None
    root = root_for()
    rows = table(plans["invoice"], generate_rows(plans["invoice"], root=root))
    accounts = {
        row["a_id"]: row
        for row in table(plans["account"], generate_rows(plans["account"], root=root))
    }
    expected = {"north": "web", "south": "shop", "west": "web"}
    parents_differ = 0
    for row in rows:
        assert row["i_channel"] == expected[row["account.a_region"]], row["i_id"]
        parents_differ += row["account.a_region"] != accounts[row["i_account"]]["a_region"]
    assert parents_differ > 50  # the column is drawn on its own: the link was not read


def test_the_link_form_beside_it_still_reaches_the_parents_row() -> None:
    """A ``given: account.a_tier`` names no column of the invoice, so it is the link."""
    document = _with_a_column_named_like_a_link()
    own = {"levels": {"north": "0.4", "south": "0.3", "west": "0.3"}}
    plans = plans_of(document, invoice={"account.a_region": own})
    statistics = plans["invoice"].column_plan("i_amount").statistics
    assert statistics.given is None and statistics.given_link == ("account", "a_tier")
    root = root_for()
    accounts = {
        row["a_id"]: row
        for row in table(plans["account"], generate_rows(plans["account"], root=root))
    }
    for row in table(plans["invoice"], generate_rows(plans["invoice"], root=root)):
        tier = accounts[row["i_account"]]["a_tier"]
        assert (row["i_amount"] >= 50) == (tier == "gold"), row["i_id"]


def test_a_given_with_a_dot_that_no_column_holds_and_no_relationship_names_is_refused() -> None:
    message = refused(
        lambda: plans_of(invoice={"i_channel": {**CHANNEL, "given": "ghost.a_region"}})
    )
    assert "no relationship 'ghost'" in message
