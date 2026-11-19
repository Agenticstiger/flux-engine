# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` never draws a column as one value unless its profile shows one (ADR 0014).

Pinned here, one case per row of the matrix observed on 0.7.0, and then as a
property over every combination of published metrics:

* a lone order statistic is a bound: with a mean, the largest-entropy fill
  from it, whose mean as drawn is the published one;
* a mean with no spread and no bound is filled from 0, on the mean's side;
* a mean with ``distinctCount`` keeps the even span around it;
* nothing published is a placeholder spread, and a foreign key with no spread
  of its own is drawn over its parent's keys;
* a declared level a histogram does not list shares what the published counts
  leave, and the message names it.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict

import pytest

from flux_engine.author.columns import ColumnPlan
from flux_engine.author.fill import drawn_mean, tilted_fill
from flux_engine.seam.parameters import Source

from .test_columns import _levels, _metric, _plan

pytestmark = pytest.mark.unit

GRID_BITS = 16


def _mean_statistic(mean: int, k: int = 600) -> Dict[str, Any]:
    return {
        "definition": "mean/1",
        "sampleSize": {"min": 600, "max": 600},
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": k, "Sx": str(mean * k * 2**GRID_BITS), "status": "F"}],
    }


def _number(**metrics: Any) -> Dict[str, Any]:
    return {
        "domain": "number",
        "metrics": {name: _metric(f"{name}/1", value) for name, value in metrics.items()},
    }


def _drawn(plan: ColumnPlan, step: str = "1") -> Fraction:
    edges = [Fraction(Decimal(edge)) for edge in plan.declaration["bins"]]
    weights = [Fraction(Decimal(weight)) for weight in plan.declaration["binWeights"]]
    return drawn_mean(edges, weights, Fraction(Decimal(step)))


def _one_value(plan: ColumnPlan) -> bool:
    levels = plan.declaration.get("levels")
    if isinstance(levels, dict):
        return len(levels) == 1
    bins = plan.declaration.get("bins")
    return isinstance(bins, list) and len(bins) == 2 and bins[0] == bins[1]


def test_a_lone_min_with_a_mean_is_a_spread_from_it_with_that_mean() -> None:
    entry = _number(count=5000, min="57", sum="500000", distinctCount=40)
    entry["statistics"] = [_mean_statistic(100, 5000)]
    plan = _plan("int", entry, rows=5000)
    assert not _one_value(plan)
    assert plan.declaration["bins"][0] == "57"
    assert abs(_drawn(plan) - 100) < Fraction(1, 10**6)
    low = next(label for label in plan.labels if label.tokens == ("bins", "0"))
    assert low.basis.source is Source.PROFILE  # the bound is the published min
    assert {tokens[-1] for tokens, _ in plan.reads} >= {"sum", "count", "distinctCount", "0"}


def test_a_lone_max_with_a_mean_fills_downward() -> None:
    plan = _plan("int", _number(count=600, max="100", sum="48000"))
    assert plan.declaration["bins"][-1] == "100"
    assert abs(_drawn(plan) - 80) < Fraction(1, 10**6)


def test_a_mean_equal_to_its_bound_is_that_one_value() -> None:
    plan = _plan("int", _number(count=600, min="7", sum="4200"))
    assert plan.declaration["levels"] == {"7": "1"}  # every value is the minimum


def test_a_mean_with_distinct_count_keeps_the_even_span() -> None:
    plan = _plan("int", _number(count=5000, sum="500000", distinctCount=40))
    assert plan.declaration["binWeights"] == ["1"]
    low, high = (Decimal(edge) for edge in plan.declaration["bins"])
    assert low < 100 < high and high - low >= 39


def test_a_mean_alone_is_filled_from_zero_on_its_side() -> None:
    plan = _plan("int", _number(count=5000, sum="500000"), rows=5000)
    assert plan.declaration["bins"][0] == "0"
    assert abs(_drawn(plan) - 100) < Fraction(1, 10**6)
    negative = _plan("decimal(9,2)", _number(count=600, sum="-1260.00"))
    assert negative.declaration["bins"][-1] == "0.00"
    assert abs(_drawn(negative, "0.01") - Fraction(-21, 10)) < Fraction(1, 10**6)


def test_a_count_alone_is_a_placeholder_spread() -> None:
    plan = _plan("int", _number(count=600))
    assert plan.declaration["bins"] == ["0", "99"] and plan.placeholder
    assert any("placeholder" in sentence for sentence in plan.assumptions)
    short = _plan("decimal(3,2)", _number(count=600))
    assert short.declaration["bins"] == ["0.00", "0.99"]


def test_a_foreign_key_with_only_its_sum_is_drawn_over_its_parent() -> None:
    plan = _plan("int", _number(count=5000, sum="2500000"), foreign="customer")
    assert plan.placeholder  # the bundle lays it over the parent's keys
    passed = {tokens[-1]: why for tokens, why in plan.passed}
    assert "drawn evenly over the keys of its parent" in passed["sum"]


def test_a_level_the_histogram_does_not_list_shares_the_remainder() -> None:
    entry = {
        "domain": "category",
        "statistics": [_levels(["A", "B", "C"], [300, 150, 100], ["F", "F", "F"])],
    }
    plan = _plan("enum('A','B','C','D')", entry)
    # k is 600: the published counts leave 50 records, all of them D's.
    assert plan.declaration["levels"] == {
        "A": "0.5",
        "B": "0.25",
        "C": "0.166666667",
        "D": "0.083333333",
    }
    assert any("do not list D" in sentence for sentence in plan.assumptions)
    sources = {label.tokens: label.basis.source for label in plan.labels}
    assert sources[("levels", "A")] is Source.PROFILE
    assert sources[("levels", "D")] is Source.ASSUMPTION
    assert not any(prop.document["property"] == "levelHistogram" for prop in plan.properties)


def test_a_boolean_with_one_level_listed_draws_both() -> None:
    entry = {
        "domain": "category",
        "statistics": [_levels(["true"], [450], ["F"])],
    }
    plan = _plan("boolean", entry)
    assert plan.declaration["levels"] == {"false": "0.25", "true": "0.75"}


@pytest.mark.parametrize(
    "fields",
    [
        dict(zip(("min", "max", "sum", "distinctCount", "quantiles"), combination, strict=True))
        for combination in itertools.product(
            (None, "10"), (None, "90"), (None, "24000"), (None, 30), (None, {"0.5": "35"})
        )
    ],
)
def test_no_number_column_is_one_value_unless_its_profile_shows_one(
    fields: Dict[str, Any],
) -> None:
    metrics: Dict[str, Any] = {"count": 600}
    metrics.update({name: value for name, value in fields.items() if value is not None})
    plan = _plan("int", _number(**metrics))
    shows_one = (
        fields["min"] is not None and fields["max"] is not None and fields["min"] == fields["max"]
    )
    mean = Fraction(int(fields["sum"]), 600) if fields["sum"] is not None else None
    lone = [value for value in (fields["min"], fields["max"]) if value is not None]
    if mean is not None and len(lone) == 1 and fields["quantiles"] is None:
        shows_one = shows_one or Fraction(lone[0]) == mean
    assert _one_value(plan) == shows_one, plan.declaration


def test_the_fill_meets_its_mean_on_a_coarse_and_a_fine_grid() -> None:
    for bound, mean, step in (
        (Fraction(57), Fraction(100), Fraction(1)),
        (Fraction(57), Fraction("57.3"), Fraction(1)),
        (Fraction(0), Fraction(50), Fraction(1, 100)),
        (Fraction(100), Fraction(80), Fraction(1)),
    ):
        filled = tilted_fill(bound, mean, step, 5000)
        assert filled is not None
        edges, weights = filled
        assert abs(drawn_mean(edges, weights, step) - mean) < Fraction(1, 10**12)
        assert bound in (edges[0], edges[-1])
    assert tilted_fill(Fraction(5), Fraction(5), Fraction(1), 100) is None
    assert tilted_fill(Fraction(0), Fraction(10), Fraction(1), 5000, limit=Fraction(1)) is None
