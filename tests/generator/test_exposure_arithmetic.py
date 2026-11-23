# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A parent's exposure in days (ADR 0025 section 4): the integral of the child column's intensity over its lifetime.

``exposure_of`` is compared with a sum written the long way round, one day (DATE) or one hour
(TIMESTAMP) at a time, with ``lambda`` from :func:`flux_engine.generator.rates.intensity_of`. What is
pinned:

* **A DATE child** counts the days of the lifetime inside the window, each weighed by ``lambda``.
* **A TIMESTAMP child** counts milliseconds: a partial hour at either edge of the lifetime or of the
  window weighs its share of the hour, and ``E`` is in days.
* **The lifetime** is read as a ``within`` entry reads one: open when its end is NULL or before its
  start (it then runs to the window's end), absent when its start is NULL, and ``0`` when it lies
  wholly outside the window. A DATE end holds up to the end of its day.
* **The weights** of a window are summed once for each window and set of rates.
* **The hook** pickles with its declarations, which is how a worker process holds them.
"""

from __future__ import annotations

import pickle
from datetime import date, datetime, timezone
from fractions import Fraction
from typing import List, Optional, Tuple

import pytest

from flux_engine.generator import exposure
from flux_engine.generator.dates import DAY_MS, KIND_DATE, KIND_TIMESTAMP
from flux_engine.generator.exposure import ExposureHook, exposure_of
from flux_engine.generator.rates import Rates, intensity_of

pytestmark = pytest.mark.unit

HOUR_MS = 3_600_000


def _ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(moment.timestamp()) * 1000


def _day(year: int, month: int, day: int) -> int:
    return (date(year, month, day) - date(1970, 1, 1)).days


#: 2025, as a run's window: the last second of the year is inside it.
WINDOW = (_ms(2025, 1, 1), _ms(2025, 12, 31, 23, 59, 59))
MONTHS = tuple(str(1 + (month % 5)) for month in range(12))
WEEK = ("1", "2", "3", "1", "5", "0.5", "1")
HOURS = tuple(str(1 + (hour * 7) % 11) for hour in range(168))

DATE_RATES = Rates(column="d", kind=KIND_DATE, month=MONTHS, day_of_week=WEEK)
TIMESTAMP_RATES = Rates(
    column="t", kind=KIND_TIMESTAMP, month=MONTHS, day_of_week=WEEK, hour_of_week=HOURS
)


def _by_days(first: int, last: int, rates: Optional[Rates]) -> Fraction:
    """The sum of ``lambda`` over the days ``first`` to ``last``, one at a time."""
    if rates is None:
        return Fraction(last - first + 1)
    return sum((intensity_of(rates, day) for day in range(first, last + 1)), Fraction(0))


def _by_hours(first_ms: int, last_ms: int, rates: Optional[Rates]) -> Fraction:
    """The sum of ``lambda(hour) * overlap_ms / 3_600_000`` over the hours ``[first, last]`` covers, in days."""
    total = Fraction(0)
    for hour in range(first_ms // HOUR_MS, last_ms // HOUR_MS + 1):
        overlap = min(last_ms + 1, (hour + 1) * HOUR_MS) - max(first_ms, hour * HOUR_MS)
        weight = Fraction(1) if rates is None else intensity_of(rates, hour * HOUR_MS)
        total += weight * Fraction(overlap, HOUR_MS)
    return total / 24


# -- a DATE child ---------------------------------------------------------------------------------


def test_a_date_child_without_rates_counts_the_days_of_the_lifetime() -> None:
    found = exposure_of(KIND_DATE, KIND_DATE, _day(2025, 3, 1), _day(2025, 3, 10), WINDOW, None)
    assert found == 10


def test_a_date_child_with_rates_sums_lambda_over_the_days() -> None:
    cases: List[Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = [
        ((2025, 1, 1), (2025, 12, 31)),
        ((2025, 2, 14), (2025, 2, 14)),
        ((2025, 3, 3), (2025, 9, 9)),
        ((2025, 6, 28), (2025, 7, 3)),
    ]
    for start, end in cases:
        found = exposure_of(KIND_DATE, KIND_DATE, _day(*start), _day(*end), WINDOW, DATE_RATES)
        assert found == _by_days(_day(*start), _day(*end), DATE_RATES), (start, end)


def test_a_lifetime_is_cut_at_the_window_and_the_last_day_of_the_window_counts() -> None:
    before = exposure_of(
        KIND_DATE, KIND_DATE, _day(2024, 11, 20), _day(2025, 1, 5), WINDOW, DATE_RATES
    )
    assert before == _by_days(_day(2025, 1, 1), _day(2025, 1, 5), DATE_RATES)
    after = exposure_of(
        KIND_DATE, KIND_DATE, _day(2025, 12, 20), _day(2026, 2, 1), WINDOW, DATE_RATES
    )
    assert after == _by_days(_day(2025, 12, 20), _day(2025, 12, 31), DATE_RATES)


# -- a TIMESTAMP child ----------------------------------------------------------------------------


def test_a_timestamp_child_without_rates_counts_milliseconds_as_days() -> None:
    start, end = _ms(2025, 3, 1, 10, 30), _ms(2025, 3, 3, 4, 15)
    found = exposure_of(KIND_TIMESTAMP, KIND_TIMESTAMP, start, end, WINDOW, None)
    assert found == Fraction(end - start + 1, DAY_MS)
    assert found == _by_hours(start, end, None)


def test_a_timestamp_child_weighs_the_partial_hours_at_the_edges() -> None:
    cases = [
        (_ms(2025, 3, 1, 10, 30), _ms(2025, 3, 3, 4, 15)),
        (_ms(2025, 3, 1, 10, 30), _ms(2025, 3, 1, 10, 45)),  # inside one hour
        (_ms(2025, 3, 1, 10, 0), _ms(2025, 3, 1, 10, 59, 59) + 999),  # exactly one hour
        (_ms(2025, 5, 4, 23, 59, 59), _ms(2025, 5, 5, 0, 0, 1)),  # across midnight
    ]
    for start, end in cases:
        found = exposure_of(KIND_TIMESTAMP, KIND_TIMESTAMP, start, end, WINDOW, TIMESTAMP_RATES)
        assert found == _by_hours(start, end, TIMESTAMP_RATES), (start, end)


def test_a_timestamp_window_that_starts_inside_an_hour_cuts_that_hour() -> None:
    window = (_ms(2025, 1, 1, 0, 20), _ms(2025, 1, 5, 13, 40))
    found = exposure_of(
        KIND_TIMESTAMP, KIND_TIMESTAMP, _ms(2024, 12, 1), None, window, TIMESTAMP_RATES
    )
    assert found == _by_hours(window[0], window[1], TIMESTAMP_RATES)
    inside = exposure_of(
        KIND_TIMESTAMP,
        KIND_TIMESTAMP,
        _ms(2025, 1, 5, 13, 10),
        _ms(2025, 1, 9),
        window,
        TIMESTAMP_RATES,
    )
    assert inside == _by_hours(_ms(2025, 1, 5, 13, 10), window[1], TIMESTAMP_RATES)


def test_a_date_lifetime_holds_up_to_the_end_of_its_last_day_for_a_timestamp_child() -> None:
    found = exposure_of(
        KIND_TIMESTAMP, KIND_DATE, _day(2025, 3, 1), _day(2025, 3, 2), WINDOW, TIMESTAMP_RATES
    )
    assert found == _by_hours(_ms(2025, 3, 1), _ms(2025, 3, 3) - 1, TIMESTAMP_RATES)
    assert exposure_of(KIND_TIMESTAMP, KIND_DATE, _day(2025, 3, 1), _day(2025, 3, 2), WINDOW) == 2


def test_a_timestamp_lifetime_bounds_a_date_child_by_the_days_that_hold_it() -> None:
    found = exposure_of(
        KIND_DATE, KIND_TIMESTAMP, _ms(2025, 3, 1, 23, 59), _ms(2025, 3, 3, 0, 1), WINDOW, None
    )
    assert found == 3


# -- the lifetime ---------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [KIND_DATE, KIND_TIMESTAMP])
def test_an_open_lifetime_runs_to_the_end_of_the_window(kind: str) -> None:
    unit = 1 if kind == KIND_DATE else DAY_MS
    start = _day(2025, 6, 1) * unit
    closed_at_window_end = exposure_of(kind, kind, start, WINDOW[1] // (DAY_MS // unit), WINDOW)
    assert exposure_of(kind, kind, start, None, WINDOW) == closed_at_window_end
    assert exposure_of(kind, kind, start, start - unit, WINDOW) == closed_at_window_end


def test_a_reversed_lifetime_reads_as_open_with_rates() -> None:
    found = exposure_of(
        KIND_DATE, KIND_DATE, _day(2025, 6, 10), _day(2025, 6, 1), WINDOW, DATE_RATES
    )
    assert found == _by_days(_day(2025, 6, 10), _day(2025, 12, 31), DATE_RATES)


def test_a_null_start_has_no_exposure() -> None:
    assert exposure_of(KIND_DATE, KIND_DATE, None, _day(2025, 6, 1), WINDOW) == 0
    assert exposure_of(KIND_TIMESTAMP, KIND_TIMESTAMP, None, None, WINDOW, TIMESTAMP_RATES) == 0


@pytest.mark.parametrize("kind", [KIND_DATE, KIND_TIMESTAMP])
def test_a_lifetime_wholly_outside_the_window_has_no_exposure(kind: str) -> None:
    unit = 1 if kind == KIND_DATE else DAY_MS
    before = (_day(2023, 1, 1) * unit, _day(2024, 12, 31) * unit)
    after = (_day(2026, 1, 1) * unit, _day(2026, 6, 1) * unit)
    for start, end in (before, after, (after[0], None)):
        assert exposure_of(kind, kind, start, end, WINDOW) == 0
    assert exposure_of(kind, kind, before[0], before[1], WINDOW, None) == 0


def test_an_unclosed_lifetime_that_started_before_the_window_has_the_whole_window() -> None:
    found = exposure_of(KIND_DATE, KIND_DATE, _day(2020, 1, 1), None, WINDOW, DATE_RATES)
    assert found == _by_days(_day(2025, 1, 1), _day(2025, 12, 31), DATE_RATES)


# -- the sums are kept, and the hook travels ------------------------------------------------------


def test_the_weights_of_a_window_are_summed_once_for_each_window_and_rates() -> None:
    exposure._table.cache_clear()
    for day in range(1, 20):
        exposure_of(KIND_DATE, KIND_DATE, _day(2025, 1, day), None, WINDOW, DATE_RATES)
    info = exposure._table.cache_info()
    assert info.misses == 1 and info.hits >= 18
    other = Rates(column="d", kind=KIND_DATE, month=MONTHS)
    exposure_of(KIND_DATE, KIND_DATE, _day(2025, 1, 1), None, WINDOW, other)
    assert exposure._table.cache_info().misses == 2


def test_a_window_too_long_to_weigh_is_refused() -> None:
    from flux_engine.core.errors import FluxError

    wide = (_ms(1, 1, 1), _ms(9999, 12, 30))
    with pytest.raises(FluxError) as refused:
        exposure_of(KIND_TIMESTAMP, KIND_TIMESTAMP, wide[0], None, wide, TIMESTAMP_RATES)
    assert refused.value.code.code == "FLX-3403"


def test_the_hook_pickles_with_its_declarations_and_without_what_it_cached() -> None:
    hook = ExposureHook()
    spec = exposure._Spec(
        child="account",
        relationship="customer",
        column="a_open",
        kind=KIND_DATE,
        start="cu_start",
        end="cu_end",
        parent_kind=KIND_DATE,
        rates=DATE_RATES,
    )
    hook._specs[("sim", "customer", "account")] = spec
    hook._found[("sim", "customer", "customer")] = (spec, 0, 1)
    copy = pickle.loads(pickle.dumps(hook))
    assert copy._specs == hook._specs and copy._found == {}
