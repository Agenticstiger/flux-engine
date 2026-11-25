# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``quantile/2``, read as two points of a column's distribution (ADR 0014 section 3).

A published group says ``binBelow`` of its ``k`` records lie below one edge of
the statistic's ladder and ``binThrough`` below the next. The column rules lay
their bins over those points, close the ends a profile withholds, and check a
``distinctCount`` against the bins they end up with.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Set, Tuple

import pytest

from flux_engine.author.columns import ColumnPlan, bound_to_window
from flux_engine.author.fill import drawn_mean
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.parameters import Source
from tests.author.test_columns import _metric, _plan, _sources

pytestmark = pytest.mark.unit

_PREFIX = ("tables", "orders", "columns", "c")

#: The 1-2-5 ladder of a number column, between the type's extremes.
_NUMBER_LADDER = [
    "-9223372036854775808",
    "1",
    "2",
    "5",
    "10",
    "20",
    "50",
    "100",
    "200",
    "500",
    "1000",
    "2000",
    "9223372036854775807",
]

#: An edge at each 1 January from 2021 to 2026 (epoch milliseconds), between the extremes.
_DATE_LADDER = [
    "-62135596800000",
    "1609459200000",
    "1640995200000",
    "1672531200000",
    "1704067200000",
    "1735689600000",
    "1767225600000",
    "253402214400000",
]


def _binned(
    ladder: List[str], quantile_bin: int, below: int, through: int, k: int = 1000
) -> Dict[str, Any]:
    return {
        "definition": "quantile/2",
        "level": "0.5",
        "bins": ladder,
        "sampleSize": {"min": k, "max": k},
        "disclosure": "acro-default",
        "groups": [
            {
                "group": "",
                "k": k,
                "quantileBin": quantile_bin,
                "binBelow": below,
                "binThrough": through,
                "status": "F",
            }
        ],
    }


def _reads(plan: ColumnPlan) -> Set[Tuple[str, ...]]:
    return {tokens for tokens, _ in plan.reads}


def _cited(plan: ColumnPlan) -> Set[Tuple[str, ...]]:
    return {label.basis.statistic[0] for label in plan.labels if label.basis.statistic is not None}


def _statistic(index: int) -> Tuple[str, ...]:
    return _PREFIX + ("statistics", str(index))


def test_a_number_with_three_binned_quantiles_and_no_max_gets_a_tail_at_its_mean() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", status="A"),
            "sum": _metric("sum/1", "100000"),
            "count": _metric("count/1", 1000),
        },
        "statistics": [
            _binned(_NUMBER_LADDER, 4, 200, 300),
            _binned(_NUMBER_LADDER, 6, 450, 650),
            _binned(_NUMBER_LADDER, 7, 650, 850),
        ],
    }
    plan = _plan("int", entry)
    bins = [Decimal(edge) for edge in plan.declaration["bins"]]
    weights = [Fraction(Decimal(text)) for text in plan.declaration["binWeights"]]
    assert bins[:6] == [Decimal(edge) for edge in ("1", "10", "20", "50", "100", "200")]
    assert bins[-1] > 200  # the part above the highest quantile has bins of its own
    assert len(weights) == len(bins) - 1 and min(weights) >= 0
    assert abs(sum(weights) - 1) < Fraction(1, 10**9)
    assert weights[:5] == [Fraction(share, 100) for share in (20, 10, 15, 20, 20)]
    assert abs(sum(weights[5:]) - Fraction(15, 100)) < Fraction(1, 10**9)
    drawn = drawn_mean([Fraction(edge) for edge in bins], weights, Fraction(1))
    assert abs(drawn - 100) <= 1  # the published mean, within one grid step
    # Every statistic is read: the three binned quantiles, and the mean the tail is tilted to.
    assert {_statistic(0), _statistic(1), _statistic(2)} <= _reads(plan)
    assert _metric_tokens("sum") in _reads(plan) and _metric_tokens("count") in _reads(plan)
    # A point that became an edge is cited; the tail's edges and weights are assumed.
    sources = _sources(plan)
    assert _statistic(0) + ("bins", "4") in _cited(plan)
    assert sources[("bins", "0")] is Source.PROFILE
    assert sources[("bins", "5")] is Source.PROFILE
    assert sources[("bins", "6")] is Source.ASSUMPTION
    assert sources[("binWeights", "5")] is Source.ASSUMPTION
    assert any("largest entropy" in sentence for sentence in plan.assumptions)
    assert "range" not in plan.declaration  # no published max: the check reads no guessed bound


def _metric_tokens(name: str) -> Tuple[str, ...]:
    return _PREFIX + ("metrics", name)


def test_a_date_with_two_binned_quantiles_and_no_min_gets_the_ladder_bin_below() -> None:
    entry = {
        "domain": "timestamp",
        "metrics": {
            "min": _metric("min/1", status="A"),
            "max": _metric("max/1", "1751241600000"),  # 2025-06-30
        },
        "statistics": [
            _binned(_DATE_LADDER, 2, 250, 500),
            _binned(_DATE_LADDER, 3, 500, 800),
        ],
    }
    plan = _plan("date", entry)
    assert plan.declaration["bins"] == [
        "2021-01-01",  # the ladder's bin below the lowest point, assumed
        "2022-01-01",
        "2023-01-01",
        "2024-01-01",
        "2025-06-30",
    ]
    assert plan.declaration["binWeights"] == ["0.25", "0.25", "0.3", "0.2"]
    assert {_statistic(0), _statistic(1)} <= _reads(plan)
    sources = _sources(plan)
    assert sources[("bins", "0")] is Source.ASSUMPTION
    assert sources[("binWeights", "0")] is Source.ASSUMPTION
    assert sources[("bins", "1")] is Source.PROFILE
    assert _statistic(0) + ("bins", "2") in _cited(plan)
    assert any("next bin of the edge ladder" in sentence for sentence in plan.assumptions)
    assert "range" not in plan.declaration


def test_points_whose_rounded_levels_disagree_still_give_non_negative_weights() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "1"), "max": _metric("max/1", "5000")},
        "statistics": [
            _binned(_NUMBER_LADDER, 4, 200, 400),  # level 0.4 at 20
            _binned(_NUMBER_LADDER, 5, 380, 600),  # level 0.38 at 50, 0.6 at 100
            _binned(_NUMBER_LADDER, 7, 590, 700),  # level 0.59 at 100, 0.7 at 200
        ],
    }
    plan = _plan("int", entry)
    weights = [Fraction(Decimal(text)) for text in plan.declaration["binWeights"]]
    assert min(weights) >= 0
    assert sum(weights) == 1
    assert len(plan.declaration["bins"]) == len(weights) + 1


def test_a_column_with_only_min_max_and_quantiles_is_declared_as_before() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", "100"),
            "quantiles": _metric("quantiles/1", {"0.25": "10", "0.5": "20", "0.75": "50"}),
        },
    }
    plan = _plan("int", entry)
    assert plan.declaration["bins"] == ["1", "10", "20", "50", "100"]
    assert plan.declaration["binWeights"] == ["0.25"] * 4
    assert plan.declaration["range"] == {"min": "1", "max": "100"}
    assert _sources(plan)[("binWeights",)] is Source.PROFILE
    assert plan.reads == []


def test_a_distinct_count_larger_than_the_span_is_passed_with_its_reason() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", "100"),
            "distinctCount": _metric("distinctCount/1", 500),
        },
        "statistics": [_binned(_NUMBER_LADDER, 4, 200, 300)],
    }
    plan = _plan("int", entry)
    tokens = _metric_tokens("distinctCount")
    assert (tokens, "the bins span fewer values than the profile counts") in plan.passed
    assert tokens not in _reads(plan)


def test_a_distinct_count_the_span_holds_is_read_as_checked() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", "100"),
            "distinctCount": _metric("distinctCount/1", 60),
        },
        "statistics": [_binned(_NUMBER_LADDER, 4, 200, 300)],
    }
    plan = _plan("int", entry)
    assert (
        _metric_tokens("distinctCount"),
        "checked: the bins span that many values",
    ) in plan.reads
    assert plan.passed == []


def test_a_key_reads_no_binned_quantile() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "1"), "max": _metric("max/1", "100")},
        "statistics": [_binned(_NUMBER_LADDER, 4, 200, 300)],
    }
    plan = _plan("int", entry, key=True)
    assert _statistic(0) not in _reads(plan)


def test_a_ladder_edge_beyond_the_published_ends_moves_neither_end() -> None:
    # The quantile's bins straddle the data: 2 lies below min 3 and 100 above max 40.
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "3"), "max": _metric("max/1", "40")},
        "statistics": [_binned(_NUMBER_LADDER, 2, 0, 300), _binned(_NUMBER_LADDER, 6, 700, 1000)],
    }
    plan = _plan("int", entry)
    assert plan.declaration["range"] == {"min": "3", "max": "40"}
    assert Decimal(plan.declaration["bins"][0]) == 3
    assert Decimal(plan.declaration["bins"][-1]) == 40


def test_a_decimal_near_the_top_of_its_type_keeps_its_bins() -> None:
    # DECIMAL(5,2) holds at most 999.99; the ladder's edge at 1000 bounds the values all the same.
    ladder = ["-9223372036854775808", "100", "200", "500", "1000", "2000", "9223372036854775807"]
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "510"), "max": _metric("max/1", status="A")},
        "statistics": [_binned(ladder, 3, 0, 1000)],
    }
    plan = _plan("decimal(5,2)", entry)
    assert not plan.placeholder
    assert Decimal(plan.declaration["bins"][0]) == Decimal("510")
    assert Decimal(plan.declaration["bins"][-1]) == Decimal("999.99")


def test_only_valid_groups_give_points() -> None:
    def plan_of(group: Dict[str, Any], quantile_bin: int = 4) -> ColumnPlan:
        statistic = _binned(_NUMBER_LADDER, quantile_bin, 200, 300)
        statistic["groups"][0].update(group)
        entry = {"domain": "number", "metrics": {}, "statistics": [statistic]}
        return _plan("int", entry)

    for group in (
        {"k": True},
        {"binBelow": True},
        {"binBelow": 400},  # more below the bin than through it
        {"binThrough": 2000},  # more than k
        {"k": 0},
        {"status": "C"},
    ):
        assert _statistic(0) not in _reads(plan_of(group)), group
    assert _statistic(0) in _reads(plan_of({}))
    # The ladder's first and last edges say nothing of where the values lie: a group in
    # the first bin gives its upper edge only.
    first = plan_of({"binBelow": 0, "binThrough": 300}, quantile_bin=0)
    assert all(Decimal(edge) > -(2**62) for edge in first.declaration.get("bins", ["0"]))


def test_binned_points_are_never_a_published_end() -> None:
    entry = {
        "domain": "number",
        "metrics": {"sum": _metric("sum/1", "80000"), "count": _metric("count/1", 1000)},
        "statistics": [_binned(_NUMBER_LADDER, 4, 200, 300), _binned(_NUMBER_LADDER, 6, 450, 650)],
    }
    plan = _plan("int", entry)
    # Both ends withheld: each is the ladder bin out, an assumption, and no range is declared.
    assert "range" not in plan.declaration
    bins = [Decimal(edge) for edge in plan.declaration["bins"]]
    assert bins[0] == 5 and bins[-1] == 200  # the ladder's bins below 10 and above 100
    sources = _sources(plan)
    assert sources[("bins", "0")] is Source.ASSUMPTION
    assert sources[("bins", str(len(bins) - 1))] is Source.ASSUMPTION


def test_a_tail_bound_off_the_grid_is_snapped_outward() -> None:
    ladder = ["-9223372036854775808", "0.0625", "0.125", "0.25", "9223372036854775807"]
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "0.01"),
            "sum": _metric("sum/1", "300"),
            "count": _metric("count/1", 1000),
        },
        "statistics": [_binned(ladder, 1, 100, 400)],
    }
    plan = _plan("decimal(10,2)", entry)
    bins = [Decimal(edge) for edge in plan.declaration["bins"]]
    assert all(edge == edge.quantize(Decimal("0.01")) for edge in bins[2:])  # tail on the grid
    assert Decimal("0.13") in bins  # 0.125 snapped up to the next cent


def test_a_foreign_key_reads_its_binned_quantile() -> None:
    entry = {
        "domain": "number",
        "metrics": {"count": _metric("count/1", 600)},
        "statistics": [_binned(_NUMBER_LADDER, 4, 200, 300, k=600)],
    }
    plan = _plan("int", entry, foreign="parent.id")
    assert _statistic(0) in _reads(plan)


def test_a_published_max_that_a_window_cut_keeps_is_read_with_the_cut() -> None:
    """The max closes the ladder's top; the window then cuts the bins, and the max is still read."""
    entry = {
        "domain": "timestamp",
        "metrics": {
            "min": _metric("min/1", status="A"),
            "max": _metric("max/1", "1788220800000"),  # 2026-09-01
        },
        "statistics": [
            {**_binned(_DATE_LADDER, 2, 250, 500), "level": "0.25"},
            {**_binned(_DATE_LADDER, 4, 500, 800), "level": "0.75"},
        ],
    }
    plan = _plan("date", entry)
    assert plan.declaration["bins"][-1] == "2026-09-01"
    assert _metric_tokens("max") in _cited(plan)
    bound_to_window(plan, LogicalKind.DATE, ("2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z"))
    assert plan.declaration["bins"] == ["2026-01-01", "2026-09-01"]
    # The bins that rest on the max now carry the window's label, so the read says so.
    assert _metric_tokens("max") not in _cited(plan)
    assert _metric_tokens("max") in _reads(plan)
    assert _statistic(1) in _reads(plan)
