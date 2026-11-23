# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected emit's derived rows and periods are bounded as a declared emit's rows are."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict

import pytest

from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.core.sizing import MOST_ROWS
from flux_engine.generator import periods as periods_module
from flux_engine.generator.periods import MOST_PERIODS, period_count, period_grid
from flux_engine.generator.projections import refuse_rows
from flux_engine.generator.rows import GeneratorPlan
from flux_engine.seam.projections import PeriodKind

from .test_period_projection import (
    PERIODIC,
    fresh_layouts,  # noqa: F401  (an autouse fixture)
    held,
    periods_plan,
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
    plans: Dict[str, GeneratorPlan],
) -> None:
    kinds = (PeriodKind(count=(1 << 31) - 1),)
    with pytest.raises(FluxError) as caught:
        periods_plan(plans, grain="month", kinds=kinds)
    assert caught.value.code.code == "FLX-3405"
    assert PERIODIC in str(caught.value)
