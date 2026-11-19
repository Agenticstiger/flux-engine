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
