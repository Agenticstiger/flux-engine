# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A rule that reads its parent's column through a link (EXPR-SPEC-2 section 6, ADR 0022 section 3).

Three exposes, customer to account to invoice, FLUID 0.7.7 relationships and fidelity blocks. What
is pinned, on kernel 0.2.0 (the only kernel that generates it):

* **A copy equals the parent's final value.** A contract ``derive`` over ``account.<column>`` holds
  the value the account row ends with, whether the account drew it, a rule of the account set it,
  a dependency set it or it was drawn given another account column; a NULL key gives NULL.
* **A chain and a difference.** A FLUX-side ``derive`` over two hops equals the grandparent's final
  value, and a difference form in days whose ``x`` reads a link moves the parent's date by ``k``.
* **NULLs first.** A column that declares a ``nullFraction`` keeps its own NULLs; every other row copies.
* **Planning and relating.** Kernel 0.1.0 and every other reach stay ``FLX-6210``, and so do a cycle,
  an expose the Simulation does not generate, a determined foreign key and a band that needs
  violations. The plan lists the plans a plan reads and the paths of its rules, and only then.
* **Two paths, one answer.** The column path's cells under the rules equal the per-row path's rows,
  for the whole table and for any slice of it.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.expr2.errors import Expr2Error
from flux_engine.generator import columns, rules
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.relations import relate_plans, related_columns
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)
from flux_engine.generator.rules import plan_rules
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.coverage import Origin
from flux_engine.seam.keys import compile_keys, mark_key_columns

from .test_relations import _column, _expose, _h, _primary, _rel

pytestmark = pytest.mark.unit

SIM_ID = "sim-links"
SETTINGS = {"due_of_opened": {"generates": "i_due"}}
ACCOUNTS, INVOICES, CUSTOMERS = 60, 400, 25


def _invoice_columns() -> List[Dict[str, Any]]:
    return [
        _column("i_id", "STRING"),
        _column("i_account", "STRING", False),
        _column("i_region", "STRING", False),
        _column("i_band", "STRING", False),
        _column("i_tier", "STRING", False),
        _column("i_channel", "STRING", False),
        _column("i_segment", "STRING", False),
        _column("i_note", "STRING", False),
        _column("i_duration", "INT", False),
        _column("i_due", "DATE", False),
    ]


def chain_contract(
    invoice_invariants: Sequence[Mapping[str, Any]] = (),
    *,
    account_invariants: Optional[Sequence[Mapping[str, Any]]] = None,
    invoice_columns: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Customer, account and invoice: an invoice names its account, an account its customer."""
    if account_invariants is None:
        account_invariants = [
            {
                "id": "band_of_score",
                "derive": "a_band",
                "expr": "CASE WHEN a_score >= 50 THEN 'high' ELSE 'low' END",
            },
            {"id": "tier_of_region", "dependency": {"from": ["a_region"], "to": ["a_tier"]}},
        ]
    customer = _expose(
        "customer",
        [_column("cu_id", "STRING"), _column("cu_segment", "STRING", False)],
        _primary("customer", "cu_id"),
    )
    account = _expose(
        "account",
        [
            _column("a_id", "STRING"),
            _column("a_customer", "STRING", False),
            _column("a_region", "STRING", False),
            _column("a_score", "INT", False),
            _column("a_band", "STRING", False),
            _column("a_tier", "STRING", False),
            _column("a_channel", "STRING", False),
            _column("a_opened", "DATE", False),
        ],
        {
            **_primary("account", "a_id"),
            "relationships": [_rel("customer", "customer", ["a_customer"], ["cu_id"])],
        },
    )
    invoice = _expose(
        "invoice",
        invoice_columns or _invoice_columns(),
        {
            **_primary("invoice", "i_id"),
            "relationships": [_rel("account", "account", ["i_account"], ["a_id"])],
        },
    )
    account["contract"]["invariants"] = [dict(rule) for rule in account_invariants]
    if invoice_invariants:
        invoice["contract"]["invariants"] = [dict(rule) for rule in invoice_invariants]
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.links",
        "exposes": [customer, account, invoice],
    }


COPIES: List[Dict[str, Any]] = [
    {"id": "region_copy", "derive": "i_region", "expr": "account.a_region"},
    {"id": "band_copy", "derive": "i_band", "expr": "account.a_band"},
    {"id": "tier_copy", "derive": "i_tier", "expr": "account.a_tier"},
    {"id": "channel_copy", "derive": "i_channel", "expr": "account.a_channel"},
]
DUE = {"id": "due_of_opened", "check": "i_duration = DAYS_BETWEEN(account.a_opened, i_due)"}
SEGMENT = {
    "id": "segment_copy",
    "language": "flux-expr/2",
    "derive": "i_segment",
    "expr": "account.customer.cu_segment",
}


#: The invoice columns a rule sets: each declares a draw too, which the rule's value replaces.
_DERIVED = ("i_region", "i_band", "i_tier", "i_channel", "i_segment")


def statistics() -> Dict[str, Tuple[Dict[str, Any], int]]:
    return {
        "customer": ({"cu_segment": {"support": {"values": ["retail", "sme", "corp"]}}}, CUSTOMERS),
        "account": (
            {
                "a_region": {"levels": {"north": "0.5", "south": "0.3", "west": "0.2"}},
                "a_score": {"range": {"min": "0", "max": "100"}},
                "a_tier": {"levels": {"gold": "0.3", "silver": "0.7"}},
                "a_channel": {
                    "given": "a_region",
                    "levelWeightsGiven": {
                        "north": {"web": "1", "shop": "0"},
                        "south": {"web": "0", "shop": "1"},
                        "west": {"web": "0.5", "shop": "0.5"},
                    },
                },
                "a_opened": {"range": {"min": "2026-01-01", "max": "2026-06-30"}},
            },
            ACCOUNTS,
        ),
        "invoice": (
            {
                **{name: {"levels": {"x": "0.5", "y": "0.5"}} for name in _DERIVED},
                "i_account": {"nullFraction": "0.1"},
                "i_note": {"levels": {"a": "0.5", "b": "0.5"}, "nullFraction": "0.25"},
                "i_duration": {"range": {"min": "1", "max": "30"}},
                "i_due": {"range": {"min": "2026-01-01", "max": "2027-12-31"}},
            },
            INVOICES,
        ),
    }


def plans_of(
    contract: Mapping[str, Any],
    *,
    settings: Any = "default",
    flux_rules: Any = None,
    counter_based: bool = True,
    declared: Optional[Dict[str, Tuple[Dict[str, Any], int]]] = None,
    only: Optional[Sequence[str]] = None,
    relate: bool = True,
    strict: bool = False,
    flux_on: str = "invoice",
) -> Dict[str, GeneratorPlan]:
    declared = statistics() if declared is None else declared
    if settings == "default":
        invoice_rules = next(e for e in contract["exposes"] if e["exposeId"] == "invoice")
        ids = [r["id"] for r in invoice_rules["contract"].get("invariants", [])]
        settings = SETTINGS if "due_of_opened" in ids else None
    keys = compile_keys(contract)
    assert keys is not None
    version = GENERATOR_VERSION_2 if counter_based else GENERATOR_VERSION
    plans: Dict[str, GeneratorPlan] = {}
    for expose in contract["exposes"]:
        expose_id = expose["exposeId"]
        if only is not None and expose_id not in only:
            continue
        expose_keys = keys.expose(expose_id)
        columns_, count = declared[expose_id]
        related = related_columns(expose_keys)
        is_invoice = expose_id == "invoice"
        plan_of_rules = plan_rules(
            contract_document=contract,
            expose_id=expose_id,
            settings=settings if is_invoice else None,
            related=related,
            mapping=None,
            has_fidelity=True,
            strict=strict,
            flux_rules=flux_rules if expose_id == flux_on else None,
            keys=expose_keys,
            rows=count,
            null_fractions={
                name: block["nullFraction"]
                for name, block in columns_.items()
                if "nullFraction" in block
            },
            counter_based=counter_based,
        )
        plans[expose_id] = build_plan(
            mark_key_columns(compile_expose(expose), expose_keys),
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=count,
            fidelity={"columns": columns_},
            related=related,
            rules=plan_of_rules,
            generator_version=version,
        )
    return relate_plans(plans, keys) if relate else plans


def root_for(seed: int = 5) -> SeedTreeRoot:
    return derive_root(
        digest_bytes("sha256:" + "a" * 64), seed, seed_tree_version=kernel2.SEED_TREE_VERSION
    )


def table(plan: GeneratorPlan, rows: Sequence[Tuple[Any, ...]]) -> List[Dict[str, Any]]:
    names = [column_plan.column.name for column_plan in plan.columns]
    return [dict(zip(names, row, strict=True)) for row in rows]


@pytest.fixture(scope="module")
def world() -> Dict[str, List[Dict[str, Any]]]:
    plans = plans_of(chain_contract([*COPIES, DUE]), flux_rules=[SEGMENT])
    root = root_for()
    return {
        expose_id: table(plan, generate_rows(plan, root=root)) for expose_id, plan in plans.items()
    }


def _parents(world: Mapping[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    return {row["a_id"]: row for row in world["account"]}


# ---------------------------------------------------------------------------
# One hop: a copy of the parent's final value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("copy_column", "parent_column"),
    [
        pytest.param("i_region", "a_region", id="drawn-from-its-own-levels"),
        pytest.param("i_band", "a_band", id="set-by-the-parents-derive"),
        pytest.param("i_tier", "a_tier", id="set-by-a-dependency"),
        pytest.param("i_channel", "a_channel", id="drawn-given-another-column"),
    ],
)
def test_a_copy_equals_the_parents_final_value(
    world: Mapping[str, List[Dict[str, Any]]], copy_column: str, parent_column: str
) -> None:
    accounts = _parents(world)
    copied = nulls = 0
    for row in world["invoice"]:
        if row["i_account"] is None:
            assert row[copy_column] is None, row["i_id"]
            nulls += 1
        else:
            assert row[copy_column] == accounts[row["i_account"]][parent_column], row["i_id"]
            assert row[copy_column] is not None
            copied += 1
    assert nulls > 10 and copied > 300
    assert len({row[copy_column] for row in world["invoice"]}) > 1


def test_the_parents_rules_and_dependencies_are_what_a_child_reads(
    world: Mapping[str, List[Dict[str, Any]]],
) -> None:
    # The band is the parent's derive and the channel follows the region it drew with: a child
    # that read the parent's raw draw would see neither.
    for row in world["account"]:
        assert row["a_band"] == ("high" if row["a_score"] >= 50 else "low")
        if row["a_region"] == "north":
            assert row["a_channel"] == "web"
        if row["a_region"] == "south":
            assert row["a_channel"] == "shop"
    by_region: Dict[str, set] = {}
    for row in world["account"]:
        by_region.setdefault(row["a_region"], set()).add(row["a_tier"])
    assert all(len(tiers) == 1 for tiers in by_region.values())


def test_a_flux_side_chain_over_two_hops_equals_the_grandparents_final_value(
    world: Mapping[str, List[Dict[str, Any]]],
) -> None:
    accounts = _parents(world)
    segments = {row["cu_id"]: row["cu_segment"] for row in world["customer"]}
    seen = set()
    for row in world["invoice"]:
        if row["i_account"] is None:
            assert row["i_segment"] is None
            continue
        expected = segments[accounts[row["i_account"]]["a_customer"]]
        assert row["i_segment"] == expected
        seen.add(expected)
    assert len(seen) == 3


def test_a_difference_form_moves_the_parents_date_by_k_days(
    world: Mapping[str, List[Dict[str, Any]]],
) -> None:
    accounts = _parents(world)
    moved = 0
    for row in world["invoice"]:
        if row["i_account"] is None:
            continue
        opened = accounts[row["i_account"]]["a_opened"]
        assert row["i_due"] == opened + timedelta(days=row["i_duration"]), row["i_id"]
        moved += 1
    assert moved > 300


# ---------------------------------------------------------------------------
# NULLs first
# ---------------------------------------------------------------------------

#: A copy whose column declares a nullFraction keeps those NULLs first, and the check counts each
#: as a violation, so the rule gives a ``rows`` band that admits them (EXPR-SPEC-2 section 6).
NOTE_COPY: Dict[str, Any] = {
    "id": "note_copy",
    "derive": "i_note",
    "expr": "account.a_region",
    "mustBeLessThan": 400,
    "unit": "rows",
}


def test_a_column_that_declares_a_null_fraction_keeps_its_nulls_and_every_other_row_copies() -> (
    None
):
    contract = chain_contract([NOTE_COPY])
    plans = plans_of(contract, settings=None)
    root = root_for()
    invoices = table(plans["invoice"], generate_rows(plans["invoice"], root=root))
    accounts = {
        row["a_id"]: row
        for row in table(plans["account"], generate_rows(plans["account"], root=root))
    }
    own = table(
        plans_of(chain_contract())["invoice"],
        generate_rows(plans_of(chain_contract())["invoice"], root=root),
    )
    nulls = copies = 0
    for row, drawn in zip(invoices, own, strict=True):
        if drawn["i_note"] is None:
            assert row["i_note"] is None
            nulls += 1
        elif row["i_account"] is None:
            assert row["i_note"] is None  # a NULL path gives NULL
        else:
            assert row["i_note"] == accounts[row["i_account"]]["a_region"]
            copies += 1
    assert nulls > 60 and copies > 200


def test_a_column_that_declares_only_a_null_fraction_draws_its_nulls_and_copies_elsewhere() -> None:
    """A copied column with no draw of its own still keeps its declared NULLs (NULLs first)."""
    declared = statistics()
    invoice_columns, count = declared["invoice"]
    declared["invoice"] = ({**invoice_columns, "i_note": {"nullFraction": "0.25"}}, count)
    contract = chain_contract([NOTE_COPY])
    plans = plans_of(contract, settings=None, declared=declared)
    assert plans["invoice"].column_plan("i_note").family is None
    root = root_for()
    invoices = table(plans["invoice"], generate_rows(plans["invoice"], root=root))
    accounts = {
        row["a_id"]: row
        for row in table(plans["account"], generate_rows(plans["account"], root=root))
    }
    nulls = copies = 0
    for row in invoices:
        if row["i_note"] is None:
            nulls += 1
        else:
            assert row["i_account"] is not None
            assert row["i_note"] == accounts[row["i_account"]]["a_region"]
            copies += 1
    # 400 rows: a NULL share of 0.25 alone puts about 100 rows here, and a NULL key a few more.
    assert 60 < nulls < 170 and copies > 200
    # The same plan gives the same rows again: the null draw is a function of the row.
    assert table(plans["invoice"], generate_rows(plans["invoice"], root=root)) == invoices


def _with_note_null_fraction(fraction: str) -> Dict[str, Tuple[Dict[str, Any], int]]:
    declared = statistics()
    invoice_columns, count = declared["invoice"]
    declared["invoice"] = ({**invoice_columns, "i_note": {"nullFraction": fraction}}, count)
    return declared


NOTE_NO_BAND: Dict[str, Any] = {"id": "note_copy", "derive": "i_note", "expr": "account.a_region"}


def test_a_column_whose_nulls_would_violate_its_rule_is_refused_naming_the_emit_column_and_rule() -> (
    None
):
    """EXPR-SPEC-2 section 6: NULLs come first, and a rule with no band allows no violating row."""
    message = refused(
        chain_contract([NOTE_NO_BAND]),
        code="FLX-6204",
        settings=None,
        declared=_with_note_null_fraction("0.25"),
    )
    assert "emit 'invoice'" in message and "'i_note'" in message and "'note_copy'" in message
    assert "NULLs come first" in message and "drop the nullFraction" in message
    assert "`rows` band" in message


@pytest.mark.parametrize(
    "band",
    [
        {"mustBe": 0},
        {"mustBeLessOrEqualTo": 0, "unit": "rows"},
        {"mustBeBetween": [0, 0]},
        {"mustBeLessThan": 1, "unit": "rows"},
    ],
    ids=["mustBe-0", "at-most-0", "between-0-and-0", "less-than-1-row"],
)
def test_a_band_of_zero_violations_is_no_band_that_admits_the_nulls(band: Dict[str, Any]) -> None:
    refused(
        chain_contract([{**NOTE_NO_BAND, **band}]),
        code="FLX-6204",
        settings=None,
        declared=_with_note_null_fraction("0.25"),
    )


def test_a_rows_band_above_zero_keeps_nulls_first_and_a_zero_null_fraction_is_no_conflict() -> None:
    admitted = {**NOTE_NO_BAND, "mustBeLessThan": 400, "unit": "rows"}
    plans_of(chain_contract([admitted]), settings=None, declared=_with_note_null_fraction("0.25"))
    plans_of(chain_contract([NOTE_NO_BAND]), settings=None, declared=_with_note_null_fraction("0"))


def test_a_flux_side_rule_over_a_chain_is_refused_the_same_way() -> None:
    message = refused(
        chain_contract(),
        code="FLX-6204",
        settings=None,
        declared=_with_note_null_fraction("0.25"),
        flux_rules=[
            {
                "id": "shout",
                "language": "flux-expr/3",
                "derive": "i_note",
                "expr": "account.a_region || '!'",
            }
        ],
    )
    assert "'i_note'" in message and "'shout'" in message


# ---------------------------------------------------------------------------
# Kernel 0.1.0 and the reaches that stay refused
# ---------------------------------------------------------------------------


def refused(contract: Mapping[str, Any], code: str = "FLX-6210", **kwargs: Any) -> str:
    with pytest.raises((Expr2Error, FluxError)) as caught:
        plans_of(contract, **kwargs)
    assert caught.value.code.code == code, str(caught.value)
    return str(caught.value)


def test_kernel_0_1_0_refuses_every_form_of_a_rule_over_a_link_and_says_which_kernel_generates_it() -> (
    None
):
    for contract, flux_rules in (
        (chain_contract(COPIES[:1]), None),
        (chain_contract([DUE]), None),
        (chain_contract(), [SEGMENT]),
    ):
        message = refused(contract, flux_rules=flux_rules, counter_based=False)
        assert "another row" in message and "Kernel 0.2.0 generates" in message


@pytest.mark.parametrize(
    "rule",
    [
        pytest.param(
            {
                "id": "r",
                "language": "flux-expr/2",
                "derive": "a_score",
                "expr": "COUNT(CHILDREN.invoice) + 1",
            },
            id="children",
        ),
    ],
)
def test_children_stay_refused_on_kernel_0_2_0(rule: Dict[str, Any]) -> None:
    assert "another row" in refused(chain_contract(), flux_rules=[rule], flux_on="account")


def test_a_rule_that_determines_a_foreign_key_through_a_link_is_refused() -> None:
    rule = {"id": "r", "derive": "i_account", "expr": "account.a_id"}
    assert "key or foreign key" in refused(chain_contract([rule]))


def test_a_percent_band_of_positive_width_on_a_rule_over_a_link_is_refused() -> None:
    rule = {**COPIES[0], "mustBeLessOrEqualTo": 5, "unit": "percent"}
    assert "percent band" in refused(chain_contract([rule]))
    plans_of(chain_contract([{**COPIES[0], "mustBeLessThan": 3}]))  # a count of rows is met


def test_a_bounds_form_over_a_link_is_refused() -> None:
    contract = chain_contract([{"id": "r", "check": "i_due >= account.a_opened"}])
    assert "bounds" in refused(contract, settings={"r": {"generates": "i_due"}})


def test_a_rule_over_an_expose_the_simulation_does_not_generate_is_refused() -> None:
    message = refused(chain_contract(COPIES[:1]), only=("customer", "invoice"))
    assert "'account'" in message and "from statistics" in message


def test_a_cycle_among_the_exposes_that_rules_read_through_is_refused() -> None:
    contract = chain_contract(
        COPIES[:1],
        account_invariants=[{"id": "back", "derive": "a_band", "expr": "customer.cu_segment"}],
    )
    # An invoice reads its account, which reads its customer: no cycle yet.
    plans_of(contract)
    cyclic = chain_contract(COPIES[:1])
    account = next(e for e in cyclic["exposes"] if e["exposeId"] == "account")
    invoice = next(e for e in cyclic["exposes"] if e["exposeId"] == "invoice")
    # The account's invoice is picked by a second relationship, and the account reads it.
    account["contract"]["schema"].append(_column("a_last", "STRING", False))
    account["semantics"]["relationships"].append(_rel("last", "invoice", ["a_last"], ["i_id"]))
    account["contract"]["invariants"] = [
        {"id": "back", "derive": "a_band", "expr": "last.i_region"}
    ]
    assert invoice
    declared = statistics()
    message = refused(cyclic, declared=declared)
    assert "in a cycle" in message and "'invoice'" in message and "'account'" in message


def test_an_expose_that_reads_itself_through_a_link_is_a_cycle() -> None:
    contract = chain_contract(COPIES[:1])
    invoice = next(e for e in contract["exposes"] if e["exposeId"] == "invoice")
    invoice["contract"]["schema"].append(_column("i_prior", "STRING", False))
    invoice["semantics"]["relationships"].append(_rel("prior", "invoice", ["i_prior"], ["i_id"]))
    invoice["contract"]["invariants"].append(
        {"id": "from_prior", "derive": "i_band", "expr": "prior.i_region"}
    )
    assert "in a cycle" in refused(contract)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def test_the_plan_lists_the_paths_the_reads_and_the_columns_a_path_reads() -> None:
    plans = plans_of(chain_contract([*COPIES, DUE]), flux_rules=[SEGMENT])
    invoice = plans["invoice"]
    assert invoice.rules is not None
    by_id = {rule.id: rule for rule in invoice.rules.rules}
    assert by_id["region_copy"].paths == (("account",),)
    assert by_id["segment_copy"].paths == (("account", "customer"),)
    assert by_id["due_of_opened"].paths == (("account",),)
    # A path reads the link's fromColumns as well as its target column.
    assert "i_account" in by_id["region_copy"].reads
    assert "i_account" in by_id["segment_copy"].reads
    assert {"i_account", "i_duration"} <= set(by_id["due_of_opened"].reads)
    document = invoice.as_document()
    assert sorted(document["reads"]) == ["account", "customer"]
    assert document["reads"]["account"] == dict(invoice.reads)["account"].digest().value
    rule_documents = {entry["id"]: entry for entry in document["rules"]["rules"]}
    assert rule_documents["region_copy"]["paths"] == [["account"]]
    assert rule_documents["segment_copy"]["paths"] == [["account", "customer"]]
    # The reached account plan reads nothing, so it carries nothing; its parent plan is not changed.
    assert "reads" not in plans["account"].as_document()
    assert "reads" not in plans["customer"].as_document()
    assert all("paths" not in entry for entry in plans["account"].as_document()["rules"]["rules"])


def test_a_plan_whose_rules_read_no_link_comes_back_as_it_was() -> None:
    contract = chain_contract([{"id": "r", "derive": "i_duration", "expr": "1 + 1"}])
    plans = plans_of(contract, settings=None)
    assert all(plan.reads == () for plan in plans.values())
    assert "reads" not in plans["invoice"].as_document()


def test_a_moved_parent_moves_its_readers() -> None:
    contract = chain_contract(COPIES[:1])
    base = plans_of(contract)
    other = chain_contract(COPIES[:1])
    account = next(e for e in other["exposes"] if e["exposeId"] == "account")
    account["contract"]["invariants"][0][
        "expr"
    ] = "CASE WHEN a_score >= 60 THEN 'high' ELSE 'low' END"
    moved = plans_of(other)
    assert moved["account"].digest() != base["account"].digest()
    assert moved["invoice"].digest() != base["invoice"].digest()


def test_a_copy_of_a_whole_path_is_copied_by_rule_and_any_other_rule_over_a_link_is_derived() -> (
    None
):
    contract = chain_contract(
        [COPIES[0], {"id": "score_up", "derive": "i_duration", "expr": "account.a_score + 1"}]
    )
    plans = plans_of(
        contract,
        flux_rules=[
            {**SEGMENT},
            {
                "id": "shout",
                "language": "flux-expr/3",
                "derive": "i_note",
                "expr": "account.a_region || '!'",
                "mustBeLessThan": 400,
                "unit": "rows",
            },
        ],
    )
    rows = {row.column: row for row in coverage_of_plan(plans["invoice"]).columns}
    assert (rows["i_region"].origin, rows["i_region"].decided_by) == (Origin.COPIED, "rule")
    assert rows["i_region"].source == "contract.invariants.region_copy"
    assert (rows["i_segment"].origin, rows["i_segment"].decided_by) == (Origin.COPIED, "rule")
    assert rows["i_segment"].source == "bundle.rules.segment_copy"
    assert (rows["i_duration"].origin, rows["i_duration"].decided_by) == (Origin.DERIVED, "rule")
    assert (rows["i_note"].origin, rows["i_note"].decided_by) == (Origin.DERIVED, "rule")
    own = {row.column: row for row in coverage_of_plan(plans["account"]).columns}
    assert (own["a_band"].origin, own["a_band"].decided_by) == (Origin.DERIVED, "rule")


# ---------------------------------------------------------------------------
# The column path and the per-row path
# ---------------------------------------------------------------------------


def _by_both_paths(monkeypatch: pytest.MonkeyPatch, make: Any) -> Tuple[Any, Any, List[bool]]:
    taken: List[bool] = []
    column_rows = columns.column_rows

    def spy(*args: Any) -> Any:
        computed = column_rows(*args)
        taken.append(computed is not None)
        return computed

    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", spy)
        by_columns = make()
    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", lambda *args: None)
        by_rows = make()
    return by_columns, by_rows, taken


def test_a_plan_with_a_rule_over_a_link_is_applied_by_the_rows() -> None:
    plans = plans_of(chain_contract([*COPIES, DUE]), flux_rules=[SEGMENT])
    plan = plans["invoice"]
    rows = generate_row_slice(plan, 0, 5, root=root_for())
    assert rules._apply_on_arrays(plan, rows, 0, root_for()) is None
    # The account's rules read only its own row, so its rules keep the array path.
    account = plans["account"]
    cells = generate_row_slice(account, 0, 5, root=root_for())
    assert rules._apply_on_arrays(account, cells, 0, root_for()) is not None


@pytest.mark.parametrize("chunk", [5, 64])
def test_the_column_paths_cells_under_the_rules_are_the_per_row_paths_rows(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plans = plans_of(chain_contract([*COPIES, DUE]), flux_rules=[SEGMENT])
    root = root_for()
    for expose_id, plan in plans.items():
        by_columns, by_rows, taken = _by_both_paths(
            monkeypatch, lambda plan=plan: generate_rows(plan, root=root)
        )
        assert by_columns == by_rows, expose_id
        assert taken and all(taken), expose_id


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (3, 40), (117, 118), (390, 400)])
def test_a_slice_of_a_plan_with_link_rules_is_the_whole_tables_slice(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    plans = plans_of(chain_contract([*COPIES, DUE]), flux_rules=[SEGMENT])
    plan = plans["invoice"]
    root = root_for()
    whole = generate_rows(plan, root=root)
    assert generate_row_slice(plan, start, stop, root=root) == whole[start:stop]
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    assert generate_row_slice(plan, start, stop, root=root) == whole[start:stop]


def test_a_plan_pickles_with_the_plans_it_reads() -> None:
    import pickle

    plans = plans_of(chain_contract([*COPIES, DUE]), flux_rules=[SEGMENT])
    again = pickle.loads(pickle.dumps(plans["invoice"]))
    assert again.digest() == plans["invoice"].digest()
    assert generate_row_slice(again, 0, 20, root=root_for()) == generate_row_slice(
        plans["invoice"], 0, 20, root=root_for()
    )


def test_the_contract_is_not_changed_by_planning() -> None:
    contract = chain_contract([*COPIES, DUE])
    before = copy.deepcopy(contract)
    plans_of(contract, flux_rules=[SEGMENT])
    assert contract == before
