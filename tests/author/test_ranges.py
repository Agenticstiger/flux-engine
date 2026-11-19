# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column never draws past the range its contract's ``validationRules`` declare."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.author.columns import ColumnContext, ColumnPlan, plan_column
from flux_engine.author.ranges import RangeRule, column_range, grid_bounds, parse_range
from flux_engine.seam.contract import CompiledColumn, parse_logical_type
from flux_engine.seam.parameters import Source

pytestmark = pytest.mark.unit


def _rules(*constraints: str) -> Dict[str, Any]:
    return {"validationRules": [{"type": "range", "constraint": text} for text in constraints]}


def test_the_grammar_reads_between_and_comparisons() -> None:
    assert parse_range("between 1 and 100") == RangeRule(Fraction(1), False, Fraction(100), False)
    assert parse_range("BETWEEN -0.5 AND 2.25") == RangeRule(
        Fraction(-1, 2), False, Fraction(9, 4), False
    )
    assert parse_range("> 0 and <= 99.99") == RangeRule(Fraction(0), True, Fraction("99.99"), False)
    assert parse_range(">= 5") == RangeRule(low=Fraction(5))
    assert parse_range("< 7 AND >= 2") == RangeRule(Fraction(2), False, Fraction(7), True)


@pytest.mark.parametrize(
    "text",
    ["", "between 1 and", "between 5 and 1", "> x", "== 3", "> 1 or < 5", ">1 and", "1e3 < 4"],
)
def test_anything_else_declares_nothing(text: str) -> None:
    assert parse_range(text) is None


def test_a_number_of_more_than_a_thousand_characters_declares_nothing() -> None:
    assert parse_range(">= " + "1" * 1000) is not None
    assert parse_range(">= " + "1" * 1001) is None


def test_several_range_rules_keep_the_tightest_bound_on_each_side() -> None:
    column = _rules(">= 1 and < 50", "> 1", "<= 40", "bad rule")
    assert column_range(column) == RangeRule(Fraction(1), True, Fraction(40), False)
    assert column_range({"validationRules": [{"type": "enum", "constraint": ">= 1"}]}) is None


def test_a_strict_bound_is_not_reached() -> None:
    rule = RangeRule(Fraction(0), True, Fraction("99.99"), False)
    assert grid_bounds(rule, Fraction(1, 100)) == (Fraction(1, 100), Fraction("99.99"))
    assert grid_bounds(RangeRule(Fraction(1), True, Fraction(3), True), Fraction(1)) == (2, 2)
    assert grid_bounds(RangeRule(Fraction(1), True, Fraction(2), True), Fraction(1)) is None


def _plan(declared: str, entry: Optional[Dict[str, Any]], constraint: str) -> ColumnPlan:
    column = CompiledColumn(
        name="c",
        logical_type=parse_logical_type(declared),
        required=False,
        declared_type=declared,
        key=False,
    )
    return plan_column(
        ColumnContext(
            column=column,
            expose_index=0,
            schema_index=1,
            product_ref="shop.orders",
            profile_tokens=("tables", "orders", "columns", "c"),
            entry=entry,
            rows=600,
            minimum_cell=10,
            range_rule=parse_range(constraint),
        )
    )


def _histogram(bins: List[str], counts: List[int]) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "bins": bins,
        "sampleSize": {"min": 600, "max": 600},
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": 600, "binCounts": counts, "status": "F"}],
    }


def _metric(definition: str, value: Any) -> Dict[str, Any]:
    return {
        "definition": definition,
        "status": "F",
        "value": value,
        "disclosure": "acro-default",
        "sampleSize": {"min": 600, "max": 600},
    }


def test_an_integer_histogram_over_a_declared_range_never_draws_past_it() -> None:
    bins = ["1", "10", "20", "30", "40", "50", "60", "70", "80", "90", "100", "100.5"]
    counts = [60] * 9 + [30, 30]
    entry = {"domain": "number", "metrics": {}, "statistics": [_histogram(bins, counts)]}
    plan = _plan("INT", entry, "between 1 and 100")
    edges = [Decimal(text) for text in plan.declaration["bins"]]
    # The closed last bin holds the grid values up to its edge: 100 at most.
    assert int(edges[-1]) <= 100 and edges[0] == 1
    # The check compares the profile's histogram over its own edges: the range and the
    # property keep them (a property over other bins finds no statistic, FLX-5037).
    assert plan.declaration["range"] == {"min": "1", "max": "100.5"}
    histogram = next(p for p in plan.properties if p.document["property"] == "marginalHistogram")
    assert histogram.document["bins"] == bins


def test_a_bin_wholly_outside_the_range_is_dropped_and_the_loss_is_an_assumption() -> None:
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_histogram(["0", "10", "20", "30"], [200, 200, 200])],
    }
    plan = _plan("INT", entry, "between 0 and 19")
    assert plan.declaration["bins"] == ["0", "10", "19"]
    assert sum(Decimal(text) for text in plan.declaration["binWeights"]) == 1
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_histogram(["0", "10", "20", "30"], [200, 200, 200])],
    }
    cut = _plan("INT", entry, "between 10 and 19")
    assert len(cut.declaration["bins"]) == 2
    assert Decimal(cut.declaration["binWeights"][0]) == 1
    assert any("leaves no value" in sentence for sentence in cut.assumptions)


def test_a_decimal_with_strict_bounds_never_draws_either_bound() -> None:
    bins = ["0", "25", "50", "75", "100"]
    entry = {"domain": "number", "metrics": {}, "statistics": [_histogram(bins, [150] * 4)]}
    plan = _plan("DECIMAL(10,2)", entry, "> 0 and <= 99.99")
    edges = [Decimal(text) for text in plan.declaration["bins"]]
    assert edges[0] == Decimal("0.01")
    assert edges[-1] == Decimal("99.99")
    assert plan.declaration["range"] == {"min": "0", "max": "100"}  # the profile's edges


def test_a_tail_stops_at_a_declared_maximum() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "0"),
            "sum": _metric("sum/1", "6000"),
            "count": _metric("count/1", 600),
        },
        "statistics": [],
    }
    plan = _plan("INT", entry, "between 0 and 40")
    assert Decimal(plan.declaration["bins"][-1]) <= 40


def test_a_column_with_nothing_published_draws_over_the_declared_range() -> None:
    plan = _plan("INT", {"domain": "number", "metrics": {}, "statistics": []}, "between 5 and 250")
    assert plan.declaration["bins"] == ["5", "250"]
    assert not plan.placeholder
    sources = {label.tokens: label.basis.source for label in plan.labels}
    assert sources[("bins",)] is Source.CONTRACT
    assert not any("placeholder" in sentence for sentence in plan.assumptions)


def test_a_long_run_of_spaces_is_read_in_linear_time() -> None:
    import time

    from flux_engine.author.ranges import parse_range

    started = time.perf_counter()
    assert parse_range(">= 1" + " " * 200_000 + "x") is None
    assert parse_range("between 1" + " " * 200_000 + "x") is None
    assert time.perf_counter() - started < 2
    assert parse_range(">= 1" + " " * 1000 + "and <= 5") == parse_range(">= 1 and <= 5")


# --- a range the profile's values do not meet -------------------------------------------


def _numbers(plan: ColumnPlan) -> List[Decimal]:
    """Every value a plan's draw can take, its edges and its levels."""
    values = list(plan.declaration.get("bins") or ()) + list(plan.declaration.get("levels") or ())
    return [Decimal(str(value)) for value in values]


def test_a_range_that_leaves_no_weight_draws_evenly_within_what_it_leaves() -> None:
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_histogram(["0", "10", "20", "30"], [200, 200, 0])],
    }
    plan = _plan("INT", entry, ">= 21 and <= 29")
    assert plan.declaration["bins"] == ["21", "29"]
    assert plan.declaration["binWeights"] == ["1"]
    assert any("puts no row inside the range" in sentence for sentence in plan.assumptions)
    assert not any("leaves no value in" in sentence for sentence in plan.assumptions)


def test_a_range_that_leaves_one_grid_value_declares_that_value() -> None:
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_histogram(["0", "10", "20", "30"], [200, 200, 200])],
    }
    plan = _plan("INT", entry, "> 28 and < 30")
    assert plan.declaration["levels"] == {"29": "1"}
    assert "bins" not in plan.declaration and "binWeights" not in plan.declaration
    # The last bin's edge is the one value's, never two equal edges.
    single = _plan("INT", entry, ">= 30")
    assert single.declaration["levels"] == {"30": "1"}


def test_a_range_that_leaves_one_value_in_the_last_zero_weight_bin_still_declares_it() -> None:
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_histogram(["0", "10", "20", "30"], [200, 200, 0])],
    }
    plan = _plan("INT", entry, ">= 30")
    assert plan.declaration["levels"] == {"30": "1"}


@pytest.mark.parametrize(
    ("declared", "constraint", "low", "high"),
    [
        ("INT", ">= 100", 100, None),
        ("INT", "< 0", None, -1),
        ("INT", "between 50 and 60", 50, 60),
        ("DECIMAL(10,2)", "> 99.5", Decimal("99.51"), None),
    ],
)
def test_a_range_beyond_every_value_of_the_profile_is_drawn_evenly_over_the_range(
    declared: str, constraint: str, low: Any, high: Any
) -> None:
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_histogram(["0", "10", "20", "30"], [200, 200, 200])],
    }
    plan = _plan(declared, entry, constraint)
    values = _numbers(plan)
    assert values and (low is None or min(values) >= low) and (high is None or max(values) <= high)
    assert "range" not in plan.declaration and plan.published_range is None
    assert not plan.properties
    sources = {label.tokens: label.basis.source for label in plan.labels}
    if low is not None and high is not None:  # a one-sided range gives a placeholder from it
        assert sources[("bins",)] is Source.CONTRACT
    assert any("lie outside the range the contract declares" in s for s in plan.assumptions)
    reasons = {tokens: why for tokens, why in plan.passed}
    cited = ("tables", "orders", "columns", "c", "statistics", "0")
    assert any(
        tokens[: len(cited)] == cited and "none of them shapes the draw" in why
        for tokens, why in plan.passed
    ), reasons
    assert not plan.reads
    assert not any(
        label.basis.statistic is not None
        for label in plan.labels
        if label.tokens[:1] in (("bins",), ("binWeights",), ("levels",), ("range",))
    )


def test_every_statistic_a_draw_outside_the_range_had_read_is_unused() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "10"),
            "max": _metric("max/1", "20"),
            "sum": _metric("sum/1", "9000"),
            "count": _metric("count/1", 600),
        },
        "statistics": [],
    }
    plan = _plan("INT", entry, "<= 5")
    unused = {tokens[-1] for tokens, why in plan.passed if "none of them shapes" in why}
    assert {"min", "max", "sum", "count"} <= unused
    assert max(_numbers(plan)) <= 5
    assert plan.declaration.get("range") is None


def test_a_range_keeps_only_the_levels_inside_it() -> None:
    from flux_engine.author.columns import _clip_levels

    plan = ColumnPlan(name="c", declaration={"levels": {"1": "0.5", "5": "0.25", "9": "0.25"}})
    assert _clip_levels(plan, Fraction(2), Fraction(8)) == 2
    assert plan.declaration["levels"] == {"5": "1"}
    wide = ColumnPlan(name="c", declaration={"levels": {"1": "0.5", "5": "0.25", "9": "0.25"}})
    assert _clip_levels(wide, Fraction(2), None) == 1
    assert wide.declaration["levels"] == {"5": "0.5", "9": "0.5"}
    high = ColumnPlan(name="c", declaration={"levels": {"1": "0.5", "5": "0.25", "9": "0.25"}})
    assert _clip_levels(high, None, Fraction(5)) == 1
    assert high.declaration["levels"] == {"1": "0.666666667", "5": "0.333333333"}
    none = ColumnPlan(name="c", declaration={"levels": {"1": "1"}})
    assert _clip_levels(none, Fraction(2), None) is None
    assert none.declaration["levels"] == {"1": "1"}
    whole = ColumnPlan(name="c", declaration={"levels": {"1": "1"}})
    assert _clip_levels(whole, Fraction(0), Fraction(3)) == 0


def test_levels_a_range_leaves_no_weight_share_the_rest_evenly() -> None:
    from flux_engine.author.columns import _clip_levels

    plan = ColumnPlan(name="c", declaration={"levels": {"1": "1", "5": "0", "6": "0"}})
    assert _clip_levels(plan, Fraction(4), Fraction(8)) == 1
    assert plan.declaration["levels"] == {"5": "0.5", "6": "0.5"}


def test_a_one_value_the_profile_publishes_outside_the_range_is_drawn_over_the_range() -> None:
    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "7"), "max": _metric("max/1", "7")},
        "statistics": [],
    }
    plan = _plan("INT", entry, "between 100 and 120")
    assert plan.declaration["bins"] == ["100", "120"]


# --- the type bounds the range ------------------------------------------------------------


def test_a_range_beyond_what_an_integer_holds_keeps_the_placeholder() -> None:
    nothing = {"domain": "number", "metrics": {}, "statistics": []}
    plan = _plan("INT", nothing, ">= 9007199254740992")
    assert plan.declaration["bins"] == ["0", "99"]
    assert any("lies beyond what its type holds" in sentence for sentence in plan.assumptions)
    assert any("lies beyond what its type holds" in sentence for sentence in plan.choices)
    below = _plan("INT", nothing, "<= -9007199254740992")
    assert below.declaration["bins"] == ["0", "99"]
    decimal = _plan("DECIMAL(10,2)", nothing, "> 99999999.99")
    assert decimal.declaration["bins"] == ["0.00", "0.99"]


def test_a_range_the_type_holds_part_of_is_cut_to_what_it_holds() -> None:
    nothing = {"domain": "number", "metrics": {}, "statistics": []}
    top = _plan("INT", nothing, ">= 9007199254740990")
    assert top.declaration["bins"] == ["9007199254740990", "9007199254740991"]
    both = _plan("INT", nothing, "between -9007199254740999 and 9007199254741000")
    assert both.declaration["bins"] == ["-9007199254740991", "9007199254740991"]
    assert not any("beyond what its type" in sentence for sentence in both.assumptions)


def test_a_range_beyond_the_type_leaves_a_draw_outside_it_as_it_is() -> None:
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_histogram(["0", "10", "20", "30"], [200, 200, 200])],
    }
    plan = _plan("INT", entry, ">= 9007199254740992")
    assert plan.declaration["bins"] == ["0", "10", "20", "30"]
    assert any("lies beyond what its type holds" in sentence for sentence in plan.assumptions)
    assert not plan.passed


# --- a fill, a tail and a ladder bin keep to the range --------------------------------------


def _mean_of(plan: ColumnPlan) -> Fraction:
    from flux_engine.author.fill import drawn_mean

    edges = [Fraction(Decimal(text)) for text in plan.declaration["bins"]]
    weights = [Fraction(Decimal(text)) for text in plan.declaration["binWeights"]]
    return drawn_mean(edges, weights, Fraction(1))


def test_a_fill_from_a_bound_meets_the_mean_inside_a_declared_maximum() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "0"),
            "sum": _metric("sum/1", "6000"),
            "count": _metric("count/1", 600),
        },
        "statistics": [],
    }
    plan = _plan("INT", entry, "between 0 and 30")
    assert Decimal(plan.declaration["bins"][-1]) <= 30
    # The geometric tail is cut where the range ends, and the mean is the published one.
    assert abs(_mean_of(plan) - 10) <= 1


def test_a_fill_from_zero_meets_the_mean_inside_a_declared_maximum() -> None:
    entry = {
        "domain": "number",
        "metrics": {"sum": _metric("sum/1", "6000"), "count": _metric("count/1", 600)},
        "statistics": [],
    }
    plan = _plan("INT", entry, "between 0 and 30")
    assert Decimal(plan.declaration["bins"][-1]) <= 30
    assert abs(_mean_of(plan) - 10) <= 1
    below = {
        "domain": "number",
        "metrics": {"sum": _metric("sum/1", "-6000"), "count": _metric("count/1", 600)},
        "statistics": [],
    }
    negative = _plan("INT", below, "between -30 and 0")
    assert Decimal(negative.declaration["bins"][0]) >= -30
    assert abs(_mean_of(negative) + 10) <= 1


def test_a_tail_beyond_the_highest_quantile_meets_the_mean_inside_a_declared_maximum() -> None:
    from tests.author.test_binned_quantiles import _NUMBER_LADDER, _binned
    from tests.author.test_columns import _metric as withheld

    entry = {
        "domain": "number",
        "metrics": {
            "min": _metric("min/1", "1"),
            "max": withheld("max/1", status="A"),
            "sum": _metric("sum/1", "100000"),
            "count": _metric("count/1", 1000),
        },
        "statistics": [
            _binned(_NUMBER_LADDER, 4, 200, 300),
            _binned(_NUMBER_LADDER, 6, 450, 650),
            _binned(_NUMBER_LADDER, 7, 650, 850),
        ],
    }
    free = _plan("INT", entry, ">= 1")
    plan = _plan("INT", entry, "between 1 and 600")
    assert Decimal(plan.declaration["bins"][-1]) <= 600 < Decimal(free.declaration["bins"][-1])
    assert abs(_mean_of(plan) - 100) <= 1


def test_the_ladder_bin_beyond_the_range_is_not_added() -> None:
    from tests.author.test_binned_quantiles import _NUMBER_LADDER, _binned
    from tests.author.test_columns import _metric as withheld

    entry = {
        "domain": "number",
        "metrics": {"min": _metric("min/1", "1"), "max": withheld("max/1", status="A")},
        "statistics": [_binned(_NUMBER_LADDER, 5, 300, 500)],
    }
    free = _plan("INT", entry, ">= 1")
    assert free.declaration["bins"][-2:] == ["50", "100"]  # the ladder bin out to the next edge
    held = _plan("INT", entry, "between 1 and 80")
    assert held.declaration["bins"][-1] == "50"  # the next edge, 100, lies past the range
    assert not any("next bin of the edge ladder" in sentence for sentence in held.assumptions)


def test_a_range_that_leaves_only_withheld_cells_drops_the_histogram_property() -> None:
    # The check scores a histogram over the cells the profile published and refuses a run that
    # puts no record in them (FLX-5005): the one value 2000 lies in a withheld cell.
    histogram = _histogram(["1", "1000", "2000", "2000.5"], [950, 30, None])
    histogram["groups"][0]["cellStatus"] = ["F", "F", "C"]
    entry = {"domain": "number", "metrics": {}, "statistics": [histogram]}
    plan = _plan("INT", entry, "> 1999.9")
    assert plan.declaration["levels"] == {"2000": "1"}
    assert not [p for p in plan.properties if p.document["property"] == "marginalHistogram"]
    tokens = ("tables", "orders", "columns", "c", "statistics", "0")
    assert (
        tokens,
        "the range the contract declares leaves no value in a cell the profile "
        "published, so the check has no published cell to score the draw by",
    ) in plan.passed
    kept = _plan("INT", entry, ">= 500")
    assert [p for p in kept.properties if p.document["property"] == "marginalHistogram"]
