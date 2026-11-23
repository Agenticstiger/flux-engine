# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The arithmetic of a lifetime's duration draw (ADR 0024 section 4, ``flux_engine.generator.dates``).

Every case is worked by hand from EXPR-SPEC-2 section 6's integer thresholds: the weights of the
bins and of the ``open`` slot, ``T_i = floor(2^53 * (W_1 + ... + W_i) / W)``, the first slot with
``u < T_i``, and inside a bin of ``n`` values the value at ``(u - T_(i-1)) * n // (T_i - T_(i-1))``.
"""

from __future__ import annotations

from datetime import date

import pytest

from flux_engine.generator.dates import (
    DAY_MS,
    KIND_DATE,
    KIND_TIMESTAMP,
    UNIFORM53,
    Duration,
    date_of_days,
    days_of_date,
    draw_end,
    duration_table,
    unit_of,
    window_in_units,
)
from flux_engine.seam.dates import Histogram

pytestmark = pytest.mark.unit

#: Weights 1, 3 and the open slot 4: total 8, so the thresholds are 2^50, 2^52 and 2^53.
LOWER = Histogram(bins=("0", "10", "30"), bin_weights=("1", "3"), open="4")
UPPER = Histogram(bins=("0", "10", "30"), bin_weights=("1", "3"), closed="upper", open="4")
CLOSED = Histogram(bins=("0", "10", "30"), bin_weights=("1", "3"))
T1, T2 = 1 << 50, 1 << 52
START = 20_000


def test_the_thresholds_are_the_floored_cumulative_shares_and_the_last_is_two_to_the_53rd() -> None:
    table = duration_table(LOWER)
    assert table.cuts == (T1, T2, UNIFORM53)
    assert len(table.cuts) > len(table.days)
    assert len(duration_table(CLOSED).cuts) == len(duration_table(CLOSED).days)
    assert duration_table(CLOSED).cuts == (UNIFORM53 // 4, UNIFORM53)
    # A share that is not a power of two floors: 1/3 of 2^53.
    thirds = duration_table(Histogram(bins=("0", "1", "2", "3"), bin_weights=("1", "1", "1")))
    assert thirds.cuts == (UNIFORM53 // 3, 2 * UNIFORM53 // 3, UNIFORM53)


def test_lower_closed_bins_hold_their_lower_edge_and_the_last_bin_its_upper_edge() -> None:
    table = duration_table(LOWER)
    assert table.days == ((0, 9), (10, 30))
    assert draw_end(table, START, 0, 1) == START
    assert draw_end(table, START, T1 - 1, 1) == START + 9
    assert draw_end(table, START, T1, 1) == START + 10
    assert draw_end(table, START, T2 - 1, 1) == START + 30


def test_upper_closed_bins_hold_their_upper_edge_and_the_first_bin_its_lower_edge() -> None:
    table = duration_table(UPPER)
    assert table.days == ((0, 10), (11, 30))
    assert draw_end(table, START, 0, 1) == START
    assert draw_end(table, START, T1 - 1, 1) == START + 10
    assert draw_end(table, START, T1, 1) == START + 11
    assert draw_end(table, START, T2 - 1, 1) == START + 30


def test_the_index_inside_a_bin_is_the_integer_share_of_the_bins_thresholds() -> None:
    table = duration_table(LOWER)
    # Bin 1 holds 21 days over the thresholds T1..T2: day k starts at the least u with
    # (u - T1) * 21 // (T2 - T1) >= k, which is T1 plus the ceiling of k * (T2 - T1) / 21.
    span = T2 - T1
    for k in (1, 2, 7, 20):
        first = T1 - (-k * span // 21)
        assert draw_end(table, START, first - 1, 1) == START + 10 + k - 1
        assert draw_end(table, START, first, 1) == START + 10 + k


def test_the_open_slot_is_no_end_and_only_a_histogram_that_declares_it_has_one() -> None:
    table = duration_table(LOWER)
    assert draw_end(table, START, T2, 1) is None
    assert draw_end(table, START, UNIFORM53 - 1, 1) is None
    closed = duration_table(CLOSED)
    assert draw_end(closed, START, UNIFORM53 - 1, 1) == START + 30


def test_a_zero_weight_bin_is_never_chosen() -> None:
    table = duration_table(Histogram(bins=("0", "5", "10", "15"), bin_weights=("1", "0", "1")))
    assert table.cuts == (UNIFORM53 // 2, UNIFORM53 // 2, UNIFORM53)
    assert draw_end(table, 0, 0, 1) == 0
    assert draw_end(table, 0, table.cuts[0] - 1, 1) == 4
    # The threshold the empty bin shares goes to the bin after it: days 10 to 15, the last closed.
    assert draw_end(table, 0, table.cuts[0], 1) == 10
    assert draw_end(table, 0, UNIFORM53 - 1, 1) == 15


def test_a_timestamp_end_counts_milliseconds_inside_whole_days() -> None:
    table = duration_table(LOWER)
    start = 1_700_000_000_000
    assert draw_end(table, start, 0, DAY_MS) == start
    # Bin 0 holds the 10 whole days from the start: its last millisecond is 10 days less one.
    assert draw_end(table, start, T1 - 1, DAY_MS) == start + 10 * DAY_MS - 1
    # Bin 1, the last, is closed at its top: it holds the day 30 as well, so 21 days of values.
    assert draw_end(table, start, T1, DAY_MS) == start + 10 * DAY_MS
    assert draw_end(table, start, T2 - 1, DAY_MS) == start + 31 * DAY_MS - 1
    # The whole-day offset of an end is the floor of (end - start) / day.
    for u in (0, 12345, T1, T1 + 99999999999, T2 - 1):
        end = draw_end(table, start, u, DAY_MS)
        assert end is not None
        offset = (end - start) // DAY_MS
        assert 0 <= offset <= 30


def test_a_timestamp_end_under_upper_closed_bins_starts_the_second_bin_after_its_edge() -> None:
    table = duration_table(UPPER)
    start = 1_700_000_000_000
    assert draw_end(table, start, T1 - 1, DAY_MS) == start + 11 * DAY_MS - 1
    assert draw_end(table, start, T1, DAY_MS) == start + 11 * DAY_MS


def _window(kind: str) -> tuple[int, int] | None:
    # 2025-01-01T00:00:00Z to 2025-12-31T23:59:59Z, in milliseconds.
    bounds = (1_735_689_600_000, 1_767_225_599_000)
    return window_in_units(kind, bounds)


def test_the_window_is_in_the_columns_units_and_a_date_window_ends_on_the_day_holding_its_end() -> (
    None
):
    assert window_in_units(KIND_DATE, None) is None
    assert _window(KIND_TIMESTAMP) == (1_735_689_600_000, 1_767_225_599_000)
    low, high = _window(KIND_DATE) or (0, 0)
    assert date_of_days(low) == date(2025, 1, 1)
    assert date_of_days(high) == date(2025, 12, 31)
    assert unit_of(KIND_DATE) == 1 and unit_of(KIND_TIMESTAMP) == DAY_MS
    assert days_of_date(date(1970, 1, 2)) == 1


def _duration(kind: str, past: str, histogram: Histogram = LOWER) -> Duration:
    return Duration(start="s", end="e", kind=kind, histogram=histogram, end_past_window=past)


def test_an_end_past_the_window_is_no_end_when_open_and_the_drawn_date_when_kept() -> None:
    window = _window(KIND_DATE)
    assert window is not None
    last = window[1]
    for past in ("open", "kept"):
        duration = _duration(KIND_DATE, past)
        # u = T1 draws bin 1's first day, 10 days after the start: 5 days past the window here.
        assert duration.end_of(last - 5, T1, window) == (last + 5 if past == "kept" else None)
        # An end on the window's last day, or inside it, is kept either way.
        assert duration.end_of(last - 10, T1, window) == last
        assert duration.end_of(last - 50, T1, window) == last - 40


def test_a_run_with_no_window_has_nothing_past_it() -> None:
    assert _duration(KIND_DATE, "open").end_of(START, T1, None) == START + 10


def test_the_open_slot_is_no_end_whether_the_end_is_kept_or_open() -> None:
    window = _window(KIND_DATE)
    for past in ("open", "kept"):
        assert _duration(KIND_DATE, past).end_of(START, T2, window) is None


def test_a_timestamp_end_past_the_window_follows_the_same_rule_in_milliseconds() -> None:
    window = _window(KIND_TIMESTAMP)
    assert window is not None
    start = window[1] - 5 * DAY_MS
    for past in ("open", "kept"):
        duration = _duration(KIND_TIMESTAMP, past)
        end = duration.end_of(start, T1, window)  # bin 1's first value: 10 days after the start
        assert end == (start + 10 * DAY_MS if past == "kept" else None)
        inside = duration.end_of(window[1] - 11 * DAY_MS, 0, window)
        assert inside == window[1] - 11 * DAY_MS  # bin 0's first value: the start itself


def test_the_document_names_the_step_and_changes_with_any_part_of_it() -> None:
    base = _duration(KIND_DATE, "open")
    document = base.as_document()
    assert document["duration"] == LOWER.as_member()
    assert document["endPastWindow"] == "open" and document["kind"] == KIND_DATE
    assert _duration(KIND_DATE, "kept").as_document() != document
    assert _duration(KIND_TIMESTAMP, "open").as_document() != document
    assert _duration(KIND_DATE, "open", UPPER).as_document() != document
