# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §2, §4 and §5 for ``levelHistogram``, end to end.

A level histogram counts a support column's records per declared level, and
grouped by a second support column it is a two-way count table of two
categorical columns. This module pins its whole path: the declaration the
parser accepts and refuses, the margin and the magnitude bound the compiler
gives it, the counts ``S`` holds and the fail-closed conditions that refuse
them, and the verdict the verifier reaches.

The published vectors file predates this property and its digests do not
move when a property kind is added, so this module is where the kind is
pinned (``tests/concordance/conftest.py`` registers it). Every expected
count here is written out by hand or recounted from the rows with
:class:`collections.Counter`, and every expected verdict comes from total
variation computed in exact rational arithmetic, never from the code under
test.
"""

from __future__ import annotations

import copy
from collections import Counter
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.calibrate.structure import (
    declarations,
    pair_table_from_level_groups,
    suggest_tree,
)
from flux_engine.concordance.boundary.slice import SliceReference, commit_state
from flux_engine.concordance.boundary.statistic import (
    UNGROUPED,
    GroupStatistic,
    compute_property_statistic,
    compute_statistic,
    level_cells,
)
from flux_engine.concordance.compiler import CompiledFidelity, compile_fidelity
from flux_engine.concordance.predicate import (
    PropertyKind,
    ToleranceKind,
    parse_fidelity_block,
)
from flux_engine.concordance.verifier import ClearStatistic, FidelityEvaluation
from flux_engine.core.errors import FluxError
from tests.concordance.conftest import (
    blinding_of,
    build_compiled,
    build_expose,
    vectors_document,
)
from tests.concordance.conftest import total_variation as _tv

pytestmark = pytest.mark.unit

EXPOSE_ID = "plan_moment"
GROUPED = "000:levelHistogram:plan@channel"
UNGROUPED_ID = "001:levelHistogram:channel"
TV = {"type": "distance", "measure": "totalVariation", "max": "0.25"}

#: The committed record set, as (channel, plan) counts: plan depends on channel.
COMMITTED: Dict[str, Dict[str, int]] = {
    "store": {"basic": 6, "plus": 3, "pro": 1},
    "web": {"basic": 1, "plus": 3, "pro": 8},
}


def _document(
    *,
    properties: Optional[List[Dict[str, Any]]] = None,
    columns: Optional[Mapping[str, Mapping[str, Any]]] = None,
    records: Tuple[int, int] = (2, 64),
) -> Dict[str, Any]:
    """A vectors-shaped document with two support columns and one range column."""
    declared: Dict[str, Any] = {
        "channel": {"support": {"values": ["web", "store"]}, "maxCardinality": 4, "null": "fail"},
        "plan": {
            "support": {"values": ["pro", "basic", "plus"]},
            "maxCardinality": 4,
            "null": "exclude",
        },
        "amount": {"range": {"min": "0", "max": "100"}, "null": "exclude"},
    }
    for name, entry in (columns or {}).items():
        declared[name] = copy.deepcopy(dict(entry))
    return {
        "contract": {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "test.gold.plan",
            "exposes": [
                {
                    "exposeId": EXPOSE_ID,
                    "kind": "stream",
                    "contract": {
                        "schema": [
                            {"name": "channel", "type": "STRING", "required": True},
                            {"name": "plan", "type": "STRING"},
                            {"name": "tier", "type": "enum('silver', 'gold', 'bronze')"},
                            {"name": "amount", "type": "DECIMAL(18,4)"},
                        ]
                    },
                }
            ],
        },
        "emit": {"productRef": "test.gold.plan", "exposeId": EXPOSE_ID, "fluidVersion": "^0.7.3"},
        "extension": {
            "fidelity": {
                "encoding": {"fractionalBits": 4, "rounding": "floor"},
                "records": {"min": records[0], "max": records[1]},
                "columns": declared,
                "properties": (
                    properties
                    if properties is not None
                    else [
                        {
                            "property": "levelHistogram",
                            "column": "plan",
                            "groupBy": "channel",
                            "tolerance": dict(TV),
                        },
                        {"property": "levelHistogram", "column": "channel", "tolerance": dict(TV)},
                    ]
                ),
                "budget": {"maxChecksPerState": 20},
                "failureAction": "withholdBundle",
            }
        },
        "expose": {"exposeId": EXPOSE_ID, "columns": ["channel", "plan", "tier", "amount"]},
    }


def _rows(table: Mapping[str, Mapping[str, int]]) -> List[Dict[str, Any]]:
    """Records holding exactly *table*'s (channel, plan) counts."""
    rows: List[Dict[str, Any]] = []
    for channel in table:
        for plan, count in table[channel].items():
            rows.extend(
                {"channel": channel, "plan": plan, "tier": "gold", "amount": "1"}
                for _ in range(count)
            )
    return rows


def _parse(document: Mapping[str, Any]) -> Any:
    return parse_fidelity_block(document["extension"]["fidelity"], expose=build_expose(document))


def _commit(rows: Sequence[Mapping[str, Any]], compiled: CompiledFidelity) -> Any:
    return commit_state(
        rows=rows,
        compiled=compiled,
        ordinal=1,
        committed_at="2026-11-01T00:00:00Z",
        blinding=blinding_of(vectors_document()),
        slice_reference=SliceReference(
            uri="file:///committed.ndjson", first_line=1, last_line=len(rows)
        ),
    )


def _evaluate(
    synthetic: Sequence[Mapping[str, Any]],
    *,
    document: Optional[Mapping[str, Any]] = None,
    committed: Optional[Sequence[Mapping[str, Any]]] = None,
) -> FidelityEvaluation:
    compiled = build_compiled(document or _document())
    state = _commit(committed if committed is not None else _rows(COMMITTED), compiled)
    return ClearStatistic(compiled=compiled, committed=state, synthetic_rows=synthetic).evaluate()


def _outcomes(evaluation: FidelityEvaluation) -> Dict[str, Any]:
    return {outcome.property_id: outcome for outcome in evaluation.outcomes}


def _refusal(error: pytest.ExceptionInfo[FluxError]) -> Tuple[str, str]:
    return error.value.code.code, error.value.detail


# ---------------------------------------------------------------------------
# §2: the declaration
# ---------------------------------------------------------------------------


def test_a_level_histogram_parses_over_a_support_column() -> None:
    block = _parse(_document())
    grouped, ungrouped = block.properties
    assert grouped.kind is PropertyKind.LEVEL_HISTOGRAM
    assert grouped.columns == ("plan",)
    assert grouped.group_by == "channel"
    assert grouped.tolerance.kind is ToleranceKind.DISTANCE
    assert grouped.tolerance.measure == "totalVariation"
    assert grouped.tolerance.magnitude == Fraction(1, 4)
    assert grouped.bins == ()
    assert grouped.property_id == GROUPED
    assert ungrouped.group_by is None
    assert ungrouped.property_id == UNGROUPED_ID
    assert block.columns_covered == ("channel", "plan")


@pytest.mark.parametrize(
    ("change", "code", "says"),
    [
        ({"column": "amount"}, "FLX-5030", "needs a support column"),
        ({"column": "nosuch"}, "FLX-5034", "has no declaration"),
        ({"bins": ["0", "100"]}, "FLX-5030", "carries no bins"),
        ({"level": "0.5"}, "FLX-5030", "only a quantile carries a level"),
        ({"minVariance": "1"}, "FLX-5030", "only a correlation carries a minVariance"),
        ({"tolerance": {"type": "count", "max": 0}}, "FLX-5030", "not a tolerance"),
        ({"tolerance": {"type": "absolute", "value": "0.1"}}, "FLX-5030", "not a tolerance"),
        ({"groupBy": "amount"}, "FLX-5030", "groupBy needs a support column"),
        ({"groupBy": "plan"}, "FLX-5030", "repeats the property's own column"),
        ({"groupBy": "nosuch"}, "FLX-5034", "has no declaration"),
    ],
)
def test_a_level_histogram_declaration_is_refused_offline(
    change: Dict[str, Any], code: str, says: str
) -> None:
    document = _document()
    entry = document["extension"]["fidelity"]["properties"][0]
    entry.update(copy.deepcopy(change))
    with pytest.raises(FluxError) as error:
        _parse(document)
    assert error.value.code.code == code
    assert says in error.value.detail


def test_a_level_histogram_names_one_column() -> None:
    document = _document()
    entry = document["extension"]["fidelity"]["properties"][0]
    del entry["column"]
    entry["columns"] = ["plan", "channel"]
    del entry["groupBy"]
    with pytest.raises(FluxError) as error:
        _parse(document)
    assert _refusal(error)[0] == "FLX-5030"
    assert "names one column" in error.value.detail


def test_the_cells_are_the_declared_levels_sorted_whatever_the_declared_order() -> None:
    block = _parse(_document())
    assert block.column("plan").support_values == ("pro", "basic", "plus")
    assert level_cells(block.column("plan")) == ("basic", "plus", "pro")
    assert level_cells(block.column("channel")) == ("store", "web")


def test_support_declared_gives_the_cells_from_the_contracts_enum() -> None:
    document = _document(
        columns={"tier": {"support": "declared", "maxCardinality": 3, "null": "fail"}},
        properties=[{"property": "levelHistogram", "column": "tier", "tolerance": dict(TV)}],
    )
    block = _parse(document)
    assert level_cells(block.column("tier")) == ("bronze", "gold", "silver")


# ---------------------------------------------------------------------------
# §3: the margin and the magnitude bound
# ---------------------------------------------------------------------------


def test_the_margin_is_the_tolerance_on_the_grid_and_epsilon_is_zero() -> None:
    compiled = build_compiled(_document())
    entry = compiled.property_by_id(GROUPED)
    assert entry.epsilon == 0
    assert entry.margin == Fraction(1, 4)
    assert entry.margin_units is None
    assert entry.max_non_members is None
    # 2 * d * k**2 with d = 4 (the margin's denominator) and k = 64 (records.max).
    assert dict(entry.magnitudes) == {"totalVariationComparison": (2 * 4 * 64 * 64).bit_length()}


def test_an_off_grid_tolerance_tightens_onto_the_grid() -> None:
    document = _document()
    document["extension"]["fidelity"]["properties"][0]["tolerance"]["max"] = "0.3"
    entry = build_compiled(document).property_by_id(GROUPED)
    # fractionalBits 4: the grid step is 1/16, and 0.3 rounds down to 4/16.
    assert entry.margin == Fraction(4, 16)


def test_the_plan_document_carries_the_property() -> None:
    plan = build_compiled(_document()).plan_document()
    assert plan["properties"][0] == {
        "propertyId": GROUPED,
        "property": "levelHistogram",
        "columns": ["plan"],
        "groupBy": "channel",
        "epsilon": "0",
        "margin": "0.25",
        "maxNonMembers": None,
        "magnitudeBits": {"totalVariationComparison": 16},
    }


def test_the_comparison_must_fit_the_field_capacity() -> None:
    document = _document()
    with pytest.raises(FluxError) as error:
        compile_fidelity(
            contract=document["contract"],
            emit=document["emit"],
            extension=document["extension"],
            expose=build_expose(document),
            capacity_bits=15,
        )
    assert error.value.code.code == "FLX-5001"
    assert "totalVariationComparison needs 16 bits" in error.value.detail


# ---------------------------------------------------------------------------
# §4: what S holds
# ---------------------------------------------------------------------------


def test_s_holds_one_count_per_declared_level_per_group() -> None:
    rows = _rows(COMMITTED)
    statistic = compute_statistic(rows, build_compiled(_document()))
    grouped = statistic.properties[GROUPED]
    assert grouped.n == len(rows)
    reference = Counter((row["channel"], row["plan"]) for row in rows)
    for group in grouped.groups:
        assert group.k == sum(count for (c, _), count in reference.items() if c == group.group)
        assert group.level_counts == tuple(
            reference[(group.group, level)] for level in ("basic", "plus", "pro")
        )
    assert [(g.group, g.k, g.level_counts) for g in grouped.groups] == [
        ("store", 10, (6, 3, 1)),
        ("web", 12, (1, 3, 8)),
    ]
    ungrouped = statistic.properties[UNGROUPED_ID]
    assert [(g.group, g.k, g.level_counts) for g in ungrouped.groups] == [(UNGROUPED, 22, (10, 12))]


def test_the_document_is_counts_and_nothing_else() -> None:
    statistic = compute_statistic(_rows(COMMITTED), build_compiled(_document()))
    assert statistic.properties[GROUPED].to_document() == {
        "propertyId": GROUPED,
        "property": "levelHistogram",
        "n": 22,
        "groups": [
            {"group": "store", "k": 10, "levelCounts": [6, 3, 1]},
            {"group": "web", "k": 12, "levelCounts": [1, 3, 8]},
        ],
    }


@pytest.mark.parametrize(
    "kind", [kind for kind in PropertyKind if kind is not PropertyKind.LEVEL_HISTOGRAM]
)
def test_no_other_property_documents_level_counts(kind: PropertyKind) -> None:
    group = GroupStatistic(group=UNGROUPED, k=1, level_counts=(1,), bin_counts=(1,))
    assert "levelCounts" not in group.to_document(kind)


def test_a_declared_level_the_state_never_holds_is_a_zero_cell() -> None:
    table = {"store": {"basic": 6, "plus": 4}, "web": {"basic": 1, "plus": 3, "pro": 8}}
    statistic = compute_statistic(_rows(table), build_compiled(_document()))
    assert statistic.properties[GROUPED].groups[0].level_counts == (6, 4, 0)


def test_the_statistic_does_not_depend_on_row_order() -> None:
    compiled = build_compiled(_document())
    rows = _rows(COMMITTED)
    forward = compute_statistic(rows, compiled).to_document()
    backward = compute_statistic(list(reversed(rows)), compiled).to_document()
    assert forward == backward


def test_a_null_level_under_exclude_drops_the_record() -> None:
    rows = _rows(COMMITTED) + [{"channel": "store", "plan": None, "tier": "gold", "amount": "1"}]
    statistic = compute_statistic(rows, build_compiled(_document()))
    grouped = statistic.properties[GROUPED]
    assert grouped.n == 23
    assert grouped.groups[0].k == 10
    assert grouped.groups[0].level_counts == (6, 3, 1)


def test_a_null_level_under_fail_is_refused() -> None:
    document = _document(
        columns={
            "plan": {
                "support": {"values": ["pro", "basic", "plus"]},
                "maxCardinality": 4,
                "null": "fail",
            }
        }
    )
    rows = _rows(COMMITTED) + [{"channel": "store", "plan": None, "tier": "gold", "amount": "1"}]
    compiled = build_compiled(document)
    with pytest.raises(FluxError) as error:
        compute_property_statistic(rows, compiled.property_by_id(GROUPED), compiled.block)
    assert _refusal(error) == ("FLX-5005", "column 'plan' row 22: null is refused")


def test_a_null_group_under_exclude_drops_the_record() -> None:
    document = _document(
        columns={
            "channel": {
                "support": {"values": ["web", "store"]},
                "maxCardinality": 4,
                "null": "exclude",
            }
        }
    )
    rows = _rows(COMMITTED) + [{"channel": None, "plan": "pro", "tier": "gold", "amount": "1"}]
    statistic = compute_statistic(rows, build_compiled(document))
    assert [g.k for g in statistic.properties[GROUPED].groups] == [10, 12]


@pytest.mark.parametrize(
    ("row", "column"),
    [
        ({"channel": "store", "plan": "gold"}, "plan"),
        ({"channel": "phone", "plan": "pro"}, "channel"),
    ],
)
def test_a_level_outside_the_declared_support_is_refused_without_naming_it(
    row: Dict[str, str], column: str
) -> None:
    rows = _rows(COMMITTED) + [{**row, "tier": "gold", "amount": "1"}]
    compiled = build_compiled(_document())
    with pytest.raises(FluxError) as error:
        compute_property_statistic(rows, compiled.property_by_id(GROUPED), compiled.block)
    code, detail = _refusal(error)
    assert code == "FLX-5005"
    assert detail == f"column {column!r} row 22: value outside the declared support"
    assert row[column] not in detail


def test_an_ungrouped_level_histogram_refuses_an_undeclared_level_too() -> None:
    rows = _rows(COMMITTED) + [{"channel": "phone", "plan": "pro", "tier": "gold", "amount": "1"}]
    compiled = build_compiled(_document())
    with pytest.raises(FluxError) as error:
        compute_property_statistic(rows, compiled.property_by_id(UNGROUPED_ID), compiled.block)
    assert _refusal(error) == (
        "FLX-5005",
        "column 'channel' row 22: value outside the declared support",
    )


def test_other_grouped_properties_still_group_by_whatever_the_rows_hold() -> None:
    """The declared-support rule is the level histogram's; a grouped mean is unchanged."""
    document = _document(
        properties=[
            {
                "property": "mean",
                "column": "amount",
                "groupBy": "channel",
                "tolerance": {"type": "absolute", "value": "1"},
            }
        ]
    )
    rows = _rows(COMMITTED) + [
        {"channel": "phone", "plan": "pro", "tier": "gold", "amount": "1"} for _ in range(2)
    ]
    statistic = compute_statistic(rows, build_compiled(document))
    groups = statistic.properties["000:mean:amount@channel"].groups
    assert [group.group for group in groups] == ["phone", "store", "web"]


def test_a_level_that_is_not_a_string_is_refused() -> None:
    rows = _rows(COMMITTED) + [{"channel": "store", "plan": 3, "tier": "gold", "amount": "1"}]
    compiled = build_compiled(_document())
    with pytest.raises(FluxError) as error:
        compute_property_statistic(rows, compiled.property_by_id(GROUPED), compiled.block)
    assert _refusal(error) == ("FLX-5005", "column 'plan' row 22: value class 'int'")


def test_a_column_absent_from_the_rows_is_refused() -> None:
    rows = [{key: value for key, value in row.items() if key != "plan"} for row in _rows(COMMITTED)]
    compiled = build_compiled(_document())
    with pytest.raises(FluxError) as error:
        compute_property_statistic(rows, compiled.property_by_id(GROUPED), compiled.block)
    assert _refusal(error) == ("FLX-5031", "column 'plan' is absent from row 0")


def test_a_group_below_records_min_is_refused_by_ordinal() -> None:
    compiled = build_compiled(_document(records=(11, 64)))
    with pytest.raises(FluxError) as error:
        compute_property_statistic(
            _rows(COMMITTED), compiled.property_by_id(GROUPED), compiled.block
        )
    code, detail = _refusal(error)
    assert code == "FLX-5005"
    assert f"{GROUPED} group #0" in detail
    assert "store" not in detail


def test_a_committed_s_declares_the_link_it_checks() -> None:
    """S's (group, level) counts give the table, the tree and the declaration, from counts alone."""
    compiled = build_compiled(_document())
    statistic = compute_statistic(_rows(COMMITTED), compiled)
    table = pair_table_from_level_groups(
        "channel",
        "plan",
        compiled.block.column("plan").support_values,
        statistic.properties[GROUPED].groups,
    )
    edges = suggest_tree([table], categorical=["channel", "plan"])
    assert [(edge.parent, edge.child) for edge in edges] == [("channel", "plan")]
    columns = declarations(edges, [table], categorical=["channel", "plan"])
    assert columns["plan"] == {
        "given": "channel",
        "levelWeightsGiven": {
            "store": {"basic": "6", "plus": "3", "pro": "1"},
            "web": {"basic": "1", "plus": "3", "pro": "8"},
        },
    }
    assert set(columns["channel"]["levels"]) == {"store", "web"}


_EMPTY_LEVEL = {"support": {"values": ["", "web"]}, "maxCardinality": 4, "null": "fail"}
_GROUPED_BY_CHANNEL = {
    "levelHistogram": {
        "property": "levelHistogram",
        "column": "plan",
        "groupBy": "channel",
        "tolerance": dict(TV),
    },
    "mean": {
        "property": "mean",
        "column": "amount",
        "groupBy": "channel",
        "tolerance": {"type": "absolute", "value": "1"},
    },
}


@pytest.mark.parametrize("kind", sorted(_GROUPED_BY_CHANNEL))
def test_the_empty_level_group_still_needs_records_min(kind: str) -> None:
    """The ungrouped key is "", and so is the group of an empty-string level.

    Whether the floor applies is read off the declaration, so a one-record
    group of the level "" is refused like any other small group, by ordinal.
    """
    document = _document(columns={"channel": _EMPTY_LEVEL}, properties=[_GROUPED_BY_CHANNEL[kind]])
    compiled = build_compiled(document)
    rows = _rows({"": {"basic": 1}, "web": {"basic": 1, "plus": 1, "pro": 1}})
    with pytest.raises(FluxError) as error:
        compute_property_statistic(rows, compiled.properties[0], compiled.block)
    code, detail = _refusal(error)
    assert code == "FLX-5005"
    assert f"{compiled.properties[0].property_id} group #0" in detail
    at_the_floor = _rows({"": {"basic": 2}, "web": {"basic": 1, "plus": 1, "pro": 1}})
    statistic = compute_property_statistic(at_the_floor, compiled.properties[0], compiled.block)
    assert [(group.group, group.k) for group in statistic.groups] == [("", 2), ("web", 3)]


# ---------------------------------------------------------------------------
# §5: the verdict
# ---------------------------------------------------------------------------


def test_the_committed_record_set_is_within_against_itself() -> None:
    evaluation = _evaluate(_rows(COMMITTED))
    assert evaluation.within and not evaluation.refused
    grouped = _outcomes(evaluation)[GROUPED]
    assert [(g.group, g.committed_count, g.synthetic_count) for g in grouped.groups] == [
        ("store", 10, 10),
        ("web", 12, 12),
    ]
    assert grouped.to_record_row() == {
        "property": "levelHistogram",
        "column": "plan",
        "groupBy": "channel",
        "tolerance": {"max": "0.25", "measure": "totalVariation", "type": "distance"},
        "margin": "0.25",
        "epsilon": "0",
        "outcome": "within",
    }


@pytest.mark.parametrize(
    ("store", "within"),
    [
        # 20 synthetic store rows against 10 committed: the cross-multiplied
        # sum is |20*6 - 10*7| + |20*3 - 10*6| + |20*1 - 10*7| = 100, and
        # 2 * tau' * k * n' = 2 * 1/4 * 10 * 20 = 100: exactly on the margin.
        ({"basic": 7, "plus": 6, "pro": 7}, True),
        ({"basic": 6, "plus": 6, "pro": 8}, False),
    ],
)
def test_the_margin_is_inclusive_and_exact(store: Dict[str, int], within: bool) -> None:
    synthetic = {"store": store, "web": COMMITTED["web"]}
    assert (_tv(COMMITTED["store"], store) <= Fraction(1, 4)) is within
    evaluation = _evaluate(_rows(synthetic))
    grouped = _outcomes(evaluation)[GROUPED]
    assert [g.within for g in grouped.groups] == [within, True]
    assert grouped.within is within
    # 20 store and 12 web rows against 10 and 12: the channel shares move by
    # 15/88 in total variation, inside the same margin.
    assert _tv({"store": 10, "web": 12}, {"store": 20, "web": 12}) == Fraction(15, 88)
    assert _outcomes(evaluation)[UNGROUPED_ID].outcome == "within"


def test_independent_draws_with_the_right_margins_are_outside() -> None:
    """The pooled plan shares for every channel: each column right, the link lost."""
    independent = {
        "store": {"basic": 3, "plus": 3, "pro": 4},
        "web": {"basic": 4, "plus": 3, "pro": 5},
    }
    evaluation = _evaluate(_rows(independent))
    assert not evaluation.refused
    assert _outcomes(evaluation)[GROUPED].outcome == "outside"
    assert _outcomes(evaluation)[UNGROUPED_ID].outcome == "within"
    assert [g.within for g in _outcomes(evaluation)[GROUPED].groups] == [
        _tv(COMMITTED[c], independent[c]) <= Fraction(1, 4) for c in ("store", "web")
    ]


def test_an_undeclared_level_on_the_synthetic_side_refuses_that_property_only() -> None:
    synthetic = _rows(COMMITTED) + [
        {"channel": "web", "plan": "gold", "tier": "gold", "amount": "1"}
    ]
    evaluation = _evaluate(synthetic)
    assert evaluation.refused and not evaluation.within
    assert [(r.code, r.property_id) for r in evaluation.refusals] == [("FLX-5005", GROUPED)]
    assert _outcomes(evaluation)[UNGROUPED_ID].outcome == "within"


def test_a_group_the_synthetic_side_never_holds_is_refused() -> None:
    synthetic = _rows({"web": {"basic": 2, "plus": 6, "pro": 14}})
    evaluation = _evaluate(synthetic)
    reasons = [r.reason for r in evaluation.refusals if r.property_id == GROUPED]
    assert reasons == ["the two sides group into different sets: 2 committed and 1 synthetic"]


_COUNT = st.integers(min_value=0, max_value=9)


@settings(max_examples=80, deadline=None)
@given(
    store=st.tuples(_COUNT, _COUNT, _COUNT).filter(lambda t: sum(t) >= 2),
    web=st.tuples(_COUNT, _COUNT, _COUNT).filter(lambda t: sum(t) >= 2),
)
def test_every_group_verdict_is_total_variation_against_the_margin(
    store: Tuple[int, int, int], web: Tuple[int, int, int]
) -> None:
    levels = ("basic", "plus", "pro")
    synthetic = {
        "store": dict(zip(levels, store, strict=True)),
        "web": dict(zip(levels, web, strict=True)),
    }
    evaluation = _evaluate(_rows(synthetic))
    assert not evaluation.refused
    grouped = _outcomes(evaluation)[GROUPED]
    assert [g.within for g in grouped.groups] == [
        _tv(COMMITTED[c], synthetic[c]) <= Fraction(1, 4) for c in ("store", "web")
    ]
