# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A key's own statistics: centred on its mean, and checked against how it is laid out."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from flux_engine.author.columns import ColumnContext, ColumnPlan, plan_column
from flux_engine.seam.contract import CompiledColumn, parse_logical_type

pytestmark = pytest.mark.unit

TOKENS = ("tables", "t", "columns", "c")


def _metric(definition: str, value: Any = None) -> Dict[str, Any]:
    return {
        "definition": definition,
        "status": "F",
        "value": value,
        "disclosure": "acro-default",
        "sampleSize": {"min": 100, "max": 100},
    }


def _quantile(bins: List[str], quantile_bin: int, below: int, through: int) -> Dict[str, Any]:
    return {
        "definition": "quantile/2",
        "level": "0.25",
        "bins": bins,
        "sampleSize": {"min": 100, "max": 100},
        "disclosure": "acro-default",
        "groups": [
            {
                "group": "",
                "k": 100,
                "quantileBin": quantile_bin,
                "binBelow": below,
                "binThrough": through,
                "status": "F",
            }
        ],
    }


def _plan(entry: Dict[str, Any], rows: int = 100) -> ColumnPlan:
    column = CompiledColumn(
        name="c",
        logical_type=parse_logical_type("INT"),
        required=False,
        declared_type="INT",
        key=True,
    )
    return plan_column(
        ColumnContext(
            column=column,
            expose_index=0,
            schema_index=0,
            product_ref="p",
            profile_tokens=TOKENS,
            entry=entry,
            rows=rows,
            minimum_cell=10,
            key=True,
            rounding_base=5,
        )
    )


def _read(plan: ColumnPlan) -> Dict[tuple, str]:
    found: Dict[tuple, str] = {}
    for tokens, how in plan.reads:
        found.setdefault(tokens, how)
    return found


def test_a_key_with_a_withheld_end_is_centred_on_the_published_mean() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", 100),
            "sum": _metric("sum/1", "45050"),
            "min": {**_metric("min/1"), "status": "A"},
        },
    }
    plan = _plan(entry)
    # The mean is 450.5: a hundred consecutive keys centred on it run 401 to 500.
    assert plan.declaration["bins"] == ["401", "500"]
    assert not plan.key_span
    assert plan.labels[0].basis.statistic is not None
    read = _read(plan)
    assert TOKENS + ("metrics", "sum") in read
    assert read[TOKENS + ("metrics", "count")] == "the mean the keys are centred on"


def test_a_count_equal_to_the_rows_is_one_value_of_the_key_per_row_in_every_path() -> None:
    entry = {
        "domain": "number",
        "metrics": {"count": _metric("count/1", 100), "distinctCount": _metric("d/1", 100)},
    }
    read = _read(_plan(entry))
    assert read[TOKENS + ("metrics", "count")] == "checked: one value of the key per row"
    assert read[TOKENS + ("metrics", "distinctCount")] == "checked: one value of the key per row"


def _ladder() -> List[str]:
    return ["0", "10", "20", "50", "100", "200", "1000"]


def test_a_binned_quantile_the_even_layout_holds_is_checked() -> None:
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_quantile(_ladder(), 2, 19, 49)],
    }
    plan = _plan(entry)
    assert _read(plan)[TOKENS + ("statistics", "0")] == "checked: the even layout holds it"


def test_a_binned_quantile_the_even_layout_does_not_hold_stays_unused_with_the_reason() -> None:
    entry = {
        "domain": "number",
        "metrics": {},
        "statistics": [_quantile(_ladder(), 2, 40, 80)],
    }
    plan = _plan(entry)
    assert TOKENS + ("statistics", "0") not in _read(plan)
    tokens, why = next(item for item in plan.passed if item[0][-2:] == ("statistics", "0"))
    assert "counts 40 of 100 records below 20" in why


def test_a_mean_that_is_not_the_middle_of_the_published_ends_is_unused_with_the_reason() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", 100),
            "sum": _metric("sum/1", "9000"),
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", "100"),
        },
    }
    plan = _plan(entry)
    reasons = [why for tokens, why in plan.passed if tokens[-1] == "sum"]
    assert reasons and "have mean 101/2" not in reasons[0] and "50.5" in reasons[0]
    agreeing = dict(entry)
    agreeing["metrics"] = {**entry["metrics"], "sum": _metric("sum/1", "5050")}
    assert _read(_plan(agreeing))[TOKENS + ("metrics", "sum")] == (
        "checked: the even layout holds it"
    )


def test_a_published_end_off_the_centred_layout_is_unused_with_the_reason() -> None:
    entry = {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", 100),
            "sum": _metric("sum/1", "45050"),
            "min": _metric("min/1", "7"),
            "max": {**_metric("max/1"), "status": "A"},
        },
    }
    plan = _plan(entry)
    why = next(why for tokens, why in plan.passed if tokens[-1] == "min")
    assert "published min is 7" in why
    assert _read(plan).get(TOKENS + ("metrics", "min")) is None


def test_a_binned_quantile_at_the_edge_of_the_profile_s_rounding_is_held_not_past_it() -> None:
    # Keys 1 to 100 laid evenly leave 19 of 100 records below 20; the profile rounds to 5, so
    # a count within 6 of that (the base and one more) agrees, and 7 does not.
    held = _plan(
        {"domain": "number", "metrics": {}, "statistics": [_quantile(_ladder(), 2, 25, 55)]}
    )
    assert _read(held)[TOKENS + ("statistics", "0")] == "checked: the even layout holds it"
    past = _plan(
        {"domain": "number", "metrics": {}, "statistics": [_quantile(_ladder(), 2, 26, 55)]}
    )
    assert TOKENS + ("statistics", "0") not in _read(past)
    assert "counts 26 of 100 records below 20" in next(
        why for tokens, why in past.passed if tokens[-2:] == ("statistics", "0")
    )
    through = _plan(
        {"domain": "number", "metrics": {}, "statistics": [_quantile(_ladder(), 2, 19, 56)]}
    )
    assert any("below 50" in why for _, why in through.passed)


def test_a_mean_is_checked_against_the_span_a_key_ends_with() -> None:

    from flux_engine.author.columns import recheck_key

    entry = {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", 100),
            "sum": _metric("sum/1", "5050"),
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", "50"),  # fewer than a hundred values: the span is assumed
        },
        "statistics": [_quantile(_ladder(), 2, 19, 49)],
    }
    column = CompiledColumn(
        name="c",
        logical_type=parse_logical_type("INT"),
        required=False,
        declared_type="INT",
        key=True,
    )
    context = ColumnContext(
        column=column,
        expose_index=0,
        schema_index=0,
        product_ref="p",
        profile_tokens=TOKENS,
        entry=entry,
        rows=100,
        minimum_cell=10,
        key=True,
        rounding_base=5,
    )
    plan = plan_column(context)
    # An assumed span 1 to 100 holds the mean of 50.5 and the quantile.
    assert plan.key_span and plan.declaration["bins"] == ["1", "100"]
    assert _read(plan)[TOKENS + ("metrics", "sum")] == "checked: the even layout holds it"
    assert _read(plan)[TOKENS + ("statistics", "0")] == "checked: the even layout holds it"
    # A foreign key widens it to 1 to 300: neither holds any longer, and the first results go.
    plan.declaration["bins"] = ["1", "300"]
    recheck_key(context, plan)
    assert TOKENS + ("metrics", "sum") not in _read(plan)
    assert TOKENS + ("statistics", "0") not in _read(plan)
    why = {tokens: why for tokens, why in plan.passed}
    assert "have mean 150.5" in why[TOKENS + ("metrics", "sum")]
    assert "leave" in why[TOKENS + ("statistics", "0")]
    # The count still holds, once.
    assert [t for t, _ in plan.reads].count(TOKENS + ("metrics", "count")) == 1
    # Back to a span that holds them: the results are the first ones again, none twice.
    plan.declaration["bins"] = ["1", "100"]
    recheck_key(context, plan)
    assert _read(plan)[TOKENS + ("statistics", "0")] == "checked: the even layout holds it"
    assert not [t for t, _ in plan.passed if t[-1] in ("sum", "0")]
