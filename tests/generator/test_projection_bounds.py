# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected emit's derived rows and periods are bounded as a declared emit's rows are."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Dict

import numpy as np
import pytest

from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.core.sizing import MOST_ROWS
from flux_engine.generator import periods as periods_module
from flux_engine.generator.periods import MOST_PERIODS, estimated_pairs, period_count, period_grid
from flux_engine.generator.projections import RunBounds, chain_of, refuse_rows, sources_of
from flux_engine.generator.rows import GeneratorPlan
from flux_engine.seam.history import END_SHARE_OF_ELIGIBLE, END_SHARE_OF_ENTITIES, HistoryEnd
from flux_engine.seam.projections import PeriodKind

from .test_period_projection import (
    LEVELS,
    PERIODIC,
    fresh_layouts,  # noqa: F401  (an autouse fixture)
    held,
    periods_plan,
    root_for,
)
from .test_projection_plan import fresh_sources  # noqa: F401  (an autouse fixture)

pytestmark = pytest.mark.unit

START = "2023-01-01T00:00:00Z"


def _window(days: int) -> Dict[str, str]:
    end = date(2023, 1, 1) + timedelta(days=days)
    return {"start": START, "end": f"{end.isoformat()}T00:00:00Z"}


@pytest.fixture(scope="module")
def plans() -> Dict[str, GeneratorPlan]:
    return held()


@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
def test_the_count_of_periods_is_the_length_of_the_grid(grain: str) -> None:
    for days in (0, 1, 40, 400, 1500):
        bounds = _window(days)
        window = (parse_rfc3339_ms(bounds["start"]), parse_rfc3339_ms(bounds["end"]))
        assert period_count(grain, window) == len(period_grid(grain, window)[0]), days


def test_the_cap_falls_between_a_million_and_a_million_and_one_periods() -> None:
    def count(days: int) -> int:
        bounds = _window(days)
        return period_count(
            "day", (parse_rfc3339_ms(bounds["start"]), parse_rfc3339_ms(bounds["end"]))
        )

    assert count(MOST_PERIODS) == MOST_PERIODS
    assert count(MOST_PERIODS + 1) == MOST_PERIODS + 1


def test_a_window_to_year_9999_at_a_day_grain_is_refused_before_the_periods_are_laid(
    plans: Dict[str, GeneratorPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal comes before the per-period pass, so the pass never runs on such a window."""

    def laid(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the periods were laid before the refusal")

    monkeypatch.setattr(periods_module, "period_grid", laid)
    with pytest.raises(FluxError) as caught:
        periods_plan(plans, grain="day", bounds={"start": START, "end": "9999-12-31T00:00:00Z"})
    assert caught.value.code.code == "FLX-3403"
    assert PERIODIC in str(caught.value) and "periods" in str(caught.value)


def test_a_window_just_over_the_cap_is_refused_naming_the_entry_and_the_count(
    plans: Dict[str, GeneratorPlan],
) -> None:
    with pytest.raises(FluxError) as caught:
        periods_plan(plans, grain="day", bounds=_window(MOST_PERIODS + 1))
    assert caught.value.code.code == "FLX-3403"
    assert PERIODIC in str(caught.value) and str(MOST_PERIODS + 1) in str(caught.value)


def test_a_derived_count_above_the_most_rows_is_refused() -> None:
    refuse_rows("periods (x)", MOST_ROWS)
    with pytest.raises(FluxError) as caught:
        refuse_rows("periods (x)", MOST_ROWS + 1)
    assert caught.value.code.code == "FLX-3405"
    assert str(MOST_ROWS + 1) in str(caught.value)


def test_a_periodic_emit_that_derives_more_than_the_most_rows_is_refused(
    plans: Dict[str, GeneratorPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard after the layout, with a budget no plan can set, so the estimate passes."""
    monkeypatch.setattr(periods_module, "DEFAULT_PERIODIC_ROWS", 1 << 62)
    kinds = (PeriodKind(count=(1 << 31) - 1),)
    with pytest.raises(FluxError) as caught:
        periods_plan(plans, grain="month", kinds=kinds)
    assert caught.value.code.code == "FLX-3405"
    assert PERIODIC in str(caught.value)


@pytest.mark.parametrize("match", ["overlap", "start", "end"])
@pytest.mark.parametrize("grain", ["day", "week", "month", "quarter", "year"])
@pytest.mark.parametrize("days", [1, 45, 400, 1500])
def test_the_estimated_pairs_bound_the_pairs_the_layout_holds(
    plans: Dict[str, GeneratorPlan], grain: str, match: str, days: int
) -> None:
    """At least the real pairs, and close: at most three more a version.

    A version meets at most two periods more than its span holds whole ones, and under ``start``
    or ``end`` it may hold one fewer than it meets.
    """
    bounds = _window(days)
    plan = periods_plan(plans, grain=grain, match=match, bounds=bounds)
    layout = sources_of(plan, root_for())
    real = int(np.maximum(layout.last - layout.first + 1, 0).sum())
    window = (parse_rfc3339_ms(bounds["start"]), parse_rfc3339_ms(bounds["end"]))
    valid = layout.view.valid_ms(window[1])
    estimate = estimated_pairs(layout.entry, window, valid)
    assert real <= estimate <= real + 3 * int(valid[0].shape[0])


def test_an_estimate_above_the_budget_is_refused_before_the_periods_are_laid(
    plans: Dict[str, GeneratorPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    def laid(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the periods were laid before the refusal")

    monkeypatch.setattr(periods_module, "period_grid", laid)
    monkeypatch.setattr(periods_module, "DEFAULT_PERIODIC_ROWS", 10)
    with pytest.raises(FluxError) as caught:
        periods_plan(plans, grain="month")
    assert caught.value.code.code == "FLX-3403"
    assert PERIODIC in str(caught.value) and "above the budget of 10 rows" in str(caught.value)


def test_the_estimate_counts_the_most_rows_a_pair_takes(
    plans: Dict[str, GeneratorPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two kinds, a count of 3 and levels up to 3, take up to 6 rows a pair."""
    pairs = periods_plan(plans, grain="month").rows
    monkeypatch.setattr(periods_module, "DEFAULT_PERIODIC_ROWS", 6 * pairs - 1)
    kinds = (PeriodKind(count=3), PeriodKind(count=LEVELS))
    with pytest.raises(FluxError) as caught:
        periods_plan(plans, grain="month", kinds=kinds)
    assert "rows at up to 6 rows a pair, above the budget" in str(caught.value)
    assert "kinds[1]'s count reaches 3 rows a pair" in str(caught.value)


def test_a_run_of_a_recorded_plan_makes_no_estimate(
    plans: Dict[str, GeneratorPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded plan states its rows: no estimate is made, however small the budget."""
    planned = periods_plan(plans, grain="month")
    monkeypatch.setattr(periods_module, "DEFAULT_PERIODIC_ROWS", 1)
    recorded = periods_plan(plans, grain="month", run_bounds=RunBounds(ranges=True))
    assert recorded.rows == planned.rows
    with pytest.raises(FluxError):
        periods_plan(plans, grain="month")


def _layout_key(layout: Any) -> Any:
    return (
        [day.isoformat() for day in layout.days],
        layout.first.tolist(),
        layout.last.tolist(),
        layout.offsets.tolist(),
    )


def test_two_runs_that_clip_differently_share_no_layout(plans: Dict[str, GeneratorPlan]) -> None:
    """A layout is kept per process, so what clips the periods is part of what it is kept under."""
    periods = periods_module
    root = root_for()
    plan = periods_plan(plans, grain="month")
    assert plan.projection is not None and plan.projection.clip is None
    whole = replace(plan.projection, root=root)
    cut = replace(whole, clip=("2023-04-01", "2025-08-01"))
    chain = chain_of(plan.projection.history, root)

    def alone(projection: Any) -> Any:
        periods.clear_layouts()
        return _layout_key(periods.layout_of(chain, projection))

    expected = {"cut": alone(cut), "whole": alone(whole)}
    assert expected["cut"] != expected["whole"]
    periods.clear_layouts()
    for name, projection in (("cut", cut), ("whole", whole), ("cut", cut)):
        assert _layout_key(periods.layout_of(chain, projection)) == expected[name], name


def test_two_runs_that_share_an_end_differently_share_no_layout() -> None:
    """What an ``end`` share is a share of is set by the run, so the layout is kept under it."""
    root = root_for()
    ends = {
        name: HistoryEnd("closeLast", "0.9", "status", "gone", share_of=name)
        for name in (END_SHARE_OF_ENTITIES, END_SHARE_OF_ELIGIBLE)
    }
    projections = {
        name: replace(
            periods_plan(held(end, rows=40, entities=24), grain="month").projection, root=root
        )
        for name, end in ends.items()
    }
    first, second = projections.values()
    assert first.history.digest() == second.history.digest()

    def alone(name: str) -> Any:
        periods_module.clear_layouts()
        projection = projections[name]
        return _layout_key(periods_module.layout_of(chain_of(projection.history, root), projection))

    expected = {name: alone(name) for name in projections}
    assert expected[END_SHARE_OF_ENTITIES] != expected[END_SHARE_OF_ELIGIBLE]
    periods_module.clear_layouts()
    for name in (*projections, END_SHARE_OF_ENTITIES):
        projection = projections[name]
        layout = periods_module.layout_of(chain_of(projection.history, root), projection)
        assert _layout_key(layout) == expected[name], name


def test_what_an_end_share_is_a_share_of_is_read_through_the_plans_a_plan_reads() -> None:
    """The layouts a relationship keeps hold it too, for the history of a plan it reads."""
    from flux_engine.generator.relations import end_shares_of

    shares = {}
    for name in (END_SHARE_OF_ENTITIES, END_SHARE_OF_ELIGIBLE):
        end = HistoryEnd("closeLast", "0.9", "status", "gone", share_of=name)
        plan = periods_plan(held(end, rows=40, entities=24), grain="month")
        shares[name] = (end_shares_of(plan), end_shares_of(plan.projection.history))
    for name, (through_projection, direct) in shares.items():
        assert through_projection == direct and direct and direct[0][1] == name
    assert shares[END_SHARE_OF_ENTITIES] != shares[END_SHARE_OF_ELIGIBLE]
