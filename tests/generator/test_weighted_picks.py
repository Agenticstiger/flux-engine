# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Parents picked by a weight column (ADR 0023 section 4, KERNEL-SPEC.md section 8.2).

A ``links`` entry's ``weight`` names a number column of the relationship's target. The parent row of
a child row is the first row ``i`` with ``u < T_i``, ``u`` one ``uniform53`` drawn on the stream of
the relationship's first column after its null draw, ``T_i`` the exact integer thresholds of the
parent's final weights. What is pinned, on kernel 0.2.0 (the only kernel that generates it):

* **The shares follow the weights**: a large child's counts per parent row sit within a chi-square
  bound of the weights, a parent that weighs zero or NULL is never picked, and a parent that weighs
  alone is always picked.
* **The draw index**: one pick worked by hand from the stream, with and without a null draw.
* **The refusals**: a weight column that is no number column of the target, one that does not exist,
  a weight on a ``distinct`` relationship, on a ``declared`` pick and on a bridge side, a negative
  weight, weights that total zero (at plan time for a reference parent, at the first pick for any
  other), and kernel 0.1.0.
* **Two paths, one answer**: the column path leaves such a plan to the per-row path, and a slice
  anywhere is the rows of the whole table.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.rng import bernoulli, uniform53
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, digest_bytes
from flux_engine.generator import columns
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.reference import ReferenceRows, build_reference_plan
from flux_engine.generator.relations import (
    ForeignKey,
    RelationReader,
    relate_plans,
    related_columns,
    weight_thresholds,
)
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_column,
    generate_row_slice,
    generate_rows,
)
from flux_engine.seam.contract import compile_expose
from flux_engine.seam.keys import compile_keys, mark_key_columns

from .test_relations import _column, _expose, _h, _primary, _rel

pytestmark = pytest.mark.unit

SIM_ID = "sim-weighted"
ACCOUNTS, INVOICES = 40, 6000
WEIGHTED = {"invoice": {"account": "a_weight"}}


def contract(*, account_id: str = "STRING", cardinality: str = "many_to_one") -> Dict[str, Any]:
    account = _expose(
        "account",
        [
            _column("a_id", account_id),
            _column("a_weight", "INT", False),
            _column("a_name", "STRING"),
        ],
        _primary("account", "a_id"),
    )
    tag = _expose("tag", [_column("t_id", "STRING")], _primary("tag", "t_id"))
    invoice = _expose(
        "invoice",
        [
            _column("i_id", "STRING"),
            _column("i_account", account_id, False),
            _column("i_tag", "STRING", False),
        ],
        {
            **_primary("invoice", "i_id"),
            "relationships": [
                _rel("account", "account", ["i_account"], ["a_id"], cardinality),
                _rel("tag", "tag", ["i_tag"], ["t_id"]),
            ],
        },
    )
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "test.weighted",
        "exposes": [account, tag, invoice],
    }


def root_for(seed: int = 5) -> SeedTreeRoot:
    return derive_root(
        digest_bytes("sha256:" + "a" * 64), seed, seed_tree_version=kernel2.SEED_TREE_VERSION
    )


#: Weights 0 to 9, so a parent that draws 0 is a parent that never gets picked.
SPREAD = {"a_weight": {"range": {"min": "0", "max": "9"}}}


def plans_of(
    *,
    account: Optional[Dict[str, Any]] = None,
    invoice: Optional[Dict[str, Any]] = None,
    document: Optional[Dict[str, Any]] = None,
    weights: Optional[Mapping[str, Mapping[str, str]]] = None,
    bridges: Optional[Mapping[str, Any]] = None,
    references: Optional[Mapping[str, ReferenceRows]] = None,
    counter_based: bool = True,
    rows: int = INVOICES,
) -> Dict[str, GeneratorPlan]:
    document = contract() if document is None else document
    keys = compile_keys(document)
    assert keys is not None
    declared: Dict[str, Tuple[Dict[str, Any], int]] = {
        "account": (
            {"a_name": {"levels": {"n": "1"}}, **(SPREAD if account is None else account)},
            ACCOUNTS,
        ),
        "tag": ({}, 5),
        "invoice": ({} if invoice is None else invoice, rows),
    }
    version = GENERATOR_VERSION_2 if counter_based else GENERATOR_VERSION
    plans: Dict[str, GeneratorPlan] = {}
    for expose in document["exposes"]:
        expose_id = expose["exposeId"]
        compiled = mark_key_columns(compile_expose(expose), keys.expose(expose_id))
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
        plans[expose_id] = build_plan(
            compiled,
            sim_id=SIM_ID,
            seam_digest=_h(expose_id),
            rows=count,
            fidelity={"columns": columns_},
            related=related_columns(keys.expose(expose_id)),
            rules=None,
            generator_version=version,
        )
    return relate_plans(plans, keys, bridges, WEIGHTED if weights is None else weights)


def table(plan: GeneratorPlan, rows: Any) -> List[Dict[str, Any]]:
    names = [column_plan.column.name for column_plan in plan.columns]
    return [dict(zip(names, row, strict=True)) for row in rows]


def refused(change: Callable[[], Any], code: str = "FLX-3403") -> str:
    with pytest.raises(FluxError) as caught:
        change()
    assert caught.value.code.code == code
    return str(caught.value)


def counts_of(
    plans: Dict[str, GeneratorPlan], seed: int = 5
) -> Tuple[Counter[str], Dict[str, Any]]:
    root = root_for(seed)
    accounts = {
        row["a_id"]: row
        for row in table(plans["account"], generate_rows(plans["account"], root=root))
    }
    invoices = table(plans["invoice"], generate_rows(plans["invoice"], root=root))
    return Counter(row["i_account"] for row in invoices if row["i_account"] is not None), accounts


# -- the shares follow the weights ------------------------------------------------------------


def test_the_pick_shares_follow_the_weights_and_a_parent_that_weighs_zero_is_never_picked() -> None:
    plans = plans_of()
    picked, accounts = counts_of(plans)
    weights = {key: (row["a_weight"] or 0) for key, row in accounts.items()}
    total = sum(weights.values())
    assert 0 in weights.values() and total > 0
    assert all(picked[key] == 0 for key, weight in weights.items() if weight == 0)
    assert sum(picked.values()) == INVOICES
    chi = sum(
        (picked[key] - INVOICES * weight / total) ** 2 / (INVOICES * weight / total)
        for key, weight in weights.items()
        if weight
    )
    # 39 degrees of freedom at most: 80 is far past the 99.99th percentile of the distribution.
    assert chi < 80, chi


def test_a_null_weight_weighs_zero() -> None:
    plans = plans_of(
        account={"a_weight": {"range": {"min": "0", "max": "9"}, "nullFraction": "0.5"}}
    )
    picked, accounts = counts_of(plans)
    nulls = [key for key, row in accounts.items() if row["a_weight"] is None]
    assert nulls and all(picked[key] == 0 for key in nulls)
    assert sum(picked.values()) == INVOICES


def test_a_parent_that_weighs_alone_is_always_picked() -> None:
    references = {
        "account": ReferenceRows(
            ("a_id", "a_weight", "a_name"),
            tuple((f"acc{i}", "7" if i == 3 else "0", "n") for i in range(6)),
        )
    }
    plans = plans_of(references=references)
    picked, _ = counts_of(plans)
    assert picked == Counter({"acc3": INVOICES})


def test_a_reference_parent_is_picked_by_its_published_weights() -> None:
    references = {
        "account": ReferenceRows(
            ("a_id", "a_weight", "a_name"),
            tuple((f"acc{i}", str(i), "n") for i in range(5)),  # 0, 1, 2, 3, 4
        )
    }
    picked, _ = counts_of(plans_of(references=references))
    assert picked["acc0"] == 0
    for i in range(1, 5):
        expected = INVOICES * i / 10
        assert abs(picked[f"acc{i}"] - expected) < 5 * (expected * (1 - i / 10)) ** 0.5, picked


# -- the draw index ---------------------------------------------------------------------------


@pytest.mark.parametrize("null_fraction", [None, "0.25"])
def test_one_pick_is_the_first_row_whose_threshold_the_stream_draw_is_below(
    null_fraction: Optional[str],
) -> None:
    published = [0, 3, 0, 5, 2, 0, 1]
    references = {
        "account": ReferenceRows(
            ("a_id", "a_weight", "a_name"),
            tuple((f"acc{i}", str(w), "n") for i, w in enumerate(published)),
        )
    }
    invoice = {} if null_fraction is None else {"i_account": {"nullFraction": null_fraction}}
    plans = plans_of(references=references, invoice=invoice, rows=300)
    root = root_for(11)
    streams = CellStreams(root, SIM_ID, "invoice")
    reader = RelationReader(root)
    seen = set()
    for row in range(300):
        stream = streams.stream_at(row, "i_account")
        if null_fraction is not None and bernoulli(stream, Decimal(null_fraction)):
            expected: Optional[int] = None
        else:
            u = uniform53(stream)
            total = sum(published)
            thresholds = [
                int(Fraction(2**53 * sum(published[: i + 1]), total)) for i in range(len(published))
            ]
            expected = next(i for i, limit in enumerate(thresholds) if u < limit)
        assert reader.parent_row(plans["invoice"], "account", row) == expected
        seen.add(expected)
    assert seen >= {1, 3, 4, 6}
    assert not seen & {0, 2, 5}


def test_the_thresholds_are_exact_integers_over_the_weights() -> None:
    assert weight_thresholds([1, 1, 2], "w") == (2**51, 2**52, 2**53)
    assert weight_thresholds([None, Decimal("0.5"), 0, Decimal("1.5")], "w") == (
        0,
        2**51,
        2**51,
        2**53,
    )


# -- the plan -----------------------------------------------------------------------------------


def test_the_foreign_key_is_weighted_and_the_plan_names_the_weight_and_reads_the_parent() -> None:
    plans = plans_of()
    plan = plans["invoice"]
    assert plan.relations is not None
    driver = plan.relations.driver("i_account")
    assert isinstance(driver, ForeignKey) and driver.pick == "weighted"
    document = plan.as_document()
    assert document["relations"]["i_account"]["weight"] == "a_weight"
    assert sorted(document["reads"]) == ["account"]
    tag = plan.relations.driver("i_tag")
    assert isinstance(tag, ForeignKey) and tag.pick == "uniform"
    assert "weight" not in document["relations"]["i_tag"]


def test_a_plan_with_no_weight_keeps_its_document() -> None:
    plain = plans_of(weights={})
    assert "reads" not in plain["invoice"].as_document()
    assert plain["invoice"].relations is not None
    driver = plain["invoice"].relations.driver("i_account")
    assert isinstance(driver, ForeignKey) and driver.pick == "uniform"


def test_weights_move_the_plan_digest_and_the_rows() -> None:
    plain, weighted = plans_of(weights={}), plans_of()
    assert plain["invoice"].digest() != weighted["invoice"].digest()
    root = root_for()
    assert generate_rows(plain["invoice"], root=root) != generate_rows(
        weighted["invoice"], root=root
    )


# -- the refusals -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "build", "needle"),
    [
        (
            "not-a-number",
            lambda: plans_of(weights={"invoice": {"account": "a_name"}}),
            "number column",
        ),
        (
            "no-such-column",
            lambda: plans_of(weights={"invoice": {"account": "nothing"}}),
            "no column",
        ),
        (
            "no-such-relationship",
            lambda: plans_of(weights={"invoice": {"customer": "a_weight"}}),
            "no relationship",
        ),
        (
            "a-distinct-relationship",
            lambda: plans_of(document=contract(cardinality="one_to_one"), rows=ACCOUNTS),
            "at most once",
        ),
        (
            "a-declared-pick",
            lambda: plans_of(
                document=contract(account_id="INT"),
                account={**SPREAD, "a_id": {"range": {"min": "1", "max": "40"}}},
                invoice={"i_account": {"range": {"min": "1", "max": "40"}}},
            ),
            "declares a draw",
        ),
        (
            "a-bridge-side",
            lambda: plans_of(bridges={"invoice": [("account", "tag")]}, rows=50),
            "bridge",
        ),
        (
            "a-reference-expose",
            lambda: plans_of(
                references={
                    "invoice": ReferenceRows(("i_id", "i_account", "i_tag"), (("x", None, None),))
                }
            ),
            "reference expose",
        ),
        (
            "a-negative-published-weight",
            lambda: plans_of(
                references={
                    "account": ReferenceRows(
                        ("a_id", "a_weight", "a_name"), (("a", "3", "n"), ("b", "-1", "n"))
                    )
                }
            ),
            "negative weight",
        ),
        (
            "all-published-weights-zero",
            lambda: plans_of(
                references={
                    "account": ReferenceRows(
                        ("a_id", "a_weight", "a_name"), (("a", "0", "n"), ("b", "0", "n"))
                    )
                }
            ),
            "weighs nothing",
        ),
    ],
)
def test_what_cannot_be_picked_by_weight_is_refused_before_a_row_exists(
    label: str, build: Callable[[], Any], needle: str
) -> None:
    assert needle in refused(build), label


def test_a_weight_that_is_negative_or_totals_zero_is_refused_at_the_first_pick() -> None:
    negative = plans_of(account={"a_weight": {"range": {"min": "-5", "max": "5"}}})
    assert "negative weight" in refused(lambda: generate_rows(negative["invoice"], root=root_for()))
    zero = plans_of(account={"a_weight": {"levels": {"0": "1"}}})
    assert "weighs nothing" in refused(lambda: generate_rows(zero["invoice"], root=root_for()))


def test_kernel_0_1_0_refuses_the_weight_and_names_the_kernel() -> None:
    message = refused(lambda: plans_of(counter_based=False), "FLX-3105")
    assert "0.2.0" in message and "0.1.0" in message


# -- two paths, one answer --------------------------------------------------------------------


def test_the_column_path_leaves_the_plan_to_the_per_row_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = plans_of(rows=700)
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
                generate_column(plans["invoice"], "i_account", root=root),
                generate_column(plans["invoice"], "i_tag", root=root),
            )
    assert outcomes["columns"] == outcomes["rows"]
    assert ("invoice", False) in seen and ("invoice", True) not in seen
    names = [c.column.name for c in plans["invoice"].columns]
    rows = outcomes["rows"][0]
    assert outcomes["rows"][2] == tuple(row[names.index("i_account")] for row in rows)


def test_a_slice_anywhere_is_the_same_rows_as_the_whole_table() -> None:
    plans = plans_of(rows=900)
    root = root_for(3)
    whole = generate_rows(plans["invoice"], root=root)
    assert generate_row_slice(plans["invoice"], 100, 333, root=root) == whole[100:333]
    assert generate_rows(plans["invoice"], root=root_for(3)) == whole
