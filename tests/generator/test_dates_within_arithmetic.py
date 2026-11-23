# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The arithmetic of a date within a linked row's lifetime (ADR 0024 section 5, ``flux_engine.generator.dates``).

Every case is worked by hand from the ADR and EXPR-SPEC-2 section 6: a parent's closed lifetime
in the column's units, the interval ``J`` it is cut to at the window, and the integer thresholds
of an offset histogram cut to ``J``, ``T_i = floor(2^53 * (W_1 + ... + W_i) / W)`` over the part
of each bin's weight that its values inside ``J`` hold.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import pytest

from flux_engine.generator.dates import (
    DAY_MS,
    KIND_DATE,
    KIND_TIMESTAMP,
    UNIFORM53,
    bound_in_units,
    cut_table,
    draw_within,
    intersect,
    interval_of,
    lifetime_in_units,
    offset_table,
    offsets_reach_forward,
    unit_of,
)
from flux_engine.seam.dates import Histogram

pytestmark = pytest.mark.unit

#: Weights 1 and 3 over the days 0 to 9 and 10 to 30 (lower closed): 10 and 21 values per DATE.
OFFSET = Histogram(bins=("0", "10", "30"), bin_weights=("1", "3"))
UPPER = Histogram(bins=("0", "10", "30"), bin_weights=("1", "3"), closed="upper")


class _Stream:
    """A stream that hands back the draws it is given and records which sampler was asked."""

    def __init__(self, u: int = 0, bounded: int = 0) -> None:
        self._u, self._bounded = u, bounded
        self.calls: List[Tuple[str, int]] = []

    def uniform53(self) -> int:
        self.calls.append(("uniform53", 0))
        return self._u

    def bounded(self, n: int) -> int:
        self.calls.append(("bounded", n))
        return self._bounded


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_a_date_start_bounds_a_timestamp_from_midnight_and_a_date_end_to_its_last_millisecond() -> (
    None
):
    assert bound_in_units(10, KIND_DATE, KIND_TIMESTAMP, end=False) == 10 * DAY_MS
    assert bound_in_units(12, KIND_DATE, KIND_TIMESTAMP, end=True) == 13 * DAY_MS - 1
    assert bound_in_units(10, KIND_DATE, KIND_DATE, end=True) == 10
    assert bound_in_units(7, KIND_TIMESTAMP, KIND_TIMESTAMP, end=False) == 7


def test_a_timestamp_bounds_a_date_by_the_day_that_holds_it() -> None:
    assert bound_in_units(10 * DAY_MS + 5, KIND_TIMESTAMP, KIND_DATE, end=False) == 10
    assert bound_in_units(12 * DAY_MS - 1, KIND_TIMESTAMP, KIND_DATE, end=True) == 11
    assert bound_in_units(12 * DAY_MS, KIND_TIMESTAMP, KIND_DATE, end=True) == 12


def test_a_lifetime_in_the_units_of_a_date_or_a_timestamp_column() -> None:
    assert lifetime_in_units(10, 12, KIND_DATE, KIND_DATE) == (10, 12)
    assert lifetime_in_units(10, 12, KIND_DATE, KIND_TIMESTAMP) == (10 * DAY_MS, 13 * DAY_MS - 1)
    start, end = 10 * DAY_MS + 5, 12 * DAY_MS + 7
    assert lifetime_in_units(start, end, KIND_TIMESTAMP, KIND_DATE) == (10, 12)
    assert lifetime_in_units(start, end, KIND_TIMESTAMP, KIND_TIMESTAMP) == (start, end)
    assert lifetime_in_units(5, 5, KIND_DATE, KIND_DATE) == (5, 5)  # its last day is included


def test_a_parent_with_no_end_or_an_end_before_its_start_is_open_and_one_with_no_start_has_none() -> (
    None
):
    assert lifetime_in_units(10, None, KIND_DATE, KIND_DATE) == (10, None)
    assert lifetime_in_units(10, 9, KIND_DATE, KIND_DATE) == (10, None)
    assert lifetime_in_units(10, 9, KIND_DATE, KIND_TIMESTAMP) == (10 * DAY_MS, None)
    assert lifetime_in_units(12 * DAY_MS, 10 * DAY_MS, KIND_TIMESTAMP, KIND_DATE) == (12, None)
    assert lifetime_in_units(None, 9, KIND_DATE, KIND_DATE) is None
    assert unit_of(KIND_DATE) == 1 and unit_of(KIND_TIMESTAMP) == DAY_MS


# ---------------------------------------------------------------------------
# Chains and the window
# ---------------------------------------------------------------------------


def test_the_levels_of_a_chain_meet_at_the_latest_start_and_the_earliest_end() -> None:
    assert intersect([(10, 50), (20, None), (5, 40)]) == (20, 40)
    assert intersect([(10, None), (20, None)]) == (20, None)
    assert intersect([(10, 20)]) == (10, 20)
    # Levels that do not meet, which the plan's ties exclude, leave the nearest link's interval.
    assert intersect([(10, 20), (30, 40)]) == (10, 20)


@pytest.mark.parametrize(
    ("lifetime", "expected"),
    [
        ((120, 150), (120, 150)),  # inside the window
        ((50, 150), (100, 150)),  # starts before it
        ((120, 250), (120, 200)),  # ends after it
        ((50, 250), (100, 200)),  # covers it
        ((150, None), (150, 200)),  # open: cut at the window's end
        ((50, None), (100, 200)),
        ((50, 100), (100, 100)),  # ends on the window's first day
        ((200, 300), (200, 200)),  # starts on its last day
        ((10, 50), (10, 50)),  # ended before the window starts: the whole lifetime
        ((300, 400), (300, 300)),  # starts after the window ends: its start alone
        ((300, None), (300, 300)),
    ],
)
def test_the_lifetime_is_cut_at_the_window(
    lifetime: Tuple[int, Optional[int]], expected: Tuple[int, Optional[int]]
) -> None:
    assert interval_of(lifetime, (100, 200)) == expected


def test_without_a_window_the_interval_is_the_lifetime() -> None:
    assert interval_of((120, None), None) == (120, None)
    assert interval_of((120, 130), None) == (120, 130)


# ---------------------------------------------------------------------------
# The cut histogram
# ---------------------------------------------------------------------------


def test_the_bins_of_an_offset_hold_the_days_a_duration_bins_hold() -> None:
    assert offset_table(OFFSET).days == ((0, 9), (10, 30))
    assert offset_table(UPPER).days == ((0, 10), (11, 30))
    negative = Histogram(bins=("-10", "0", "5"), bin_weights=("1", "1"))
    assert offset_table(negative).days == ((-10, -1), (0, 5))


def test_a_whole_bin_inside_the_interval_keeps_its_whole_weight() -> None:
    cut = cut_table(offset_table(OFFSET), 1000, (1000, 1100), 1)
    assert cut is not None
    assert cut.spans == ((1000, 1009), (1010, 1030))
    assert cut.cuts == (UNIFORM53 // 4, UNIFORM53)
    assert cut.value(0) == 1000
    assert cut.value(cut.cuts[0] - 1) == 1009
    assert cut.value(cut.cuts[0]) == 1010
    assert cut.value(UNIFORM53 - 1) == 1030


def test_a_partial_bin_keeps_the_part_of_its_weight_its_values_inside_hold() -> None:
    # Inside 1005 to 1020: five of the first bin's ten values (weight 1/2) and eleven of the
    # second bin's 21 (weight 3 * 11/21 = 11/7). The total is 29/14, so T_1 = floor(2^53 * 7/29).
    cut = cut_table(offset_table(OFFSET), 1000, (1005, 1020), 1)
    assert cut is not None
    assert cut.spans == ((1005, 1009), (1010, 1020))
    assert cut.cuts == ((UNIFORM53 * 7) // 29, UNIFORM53)
    first = cut.cuts[0]
    assert cut.value(0) == 1005
    assert cut.value(first - 1) == 1009
    assert cut.value(first) == 1010
    assert cut.value(UNIFORM53 - 1) == 1020
    # Inside a bin the index is (u - T_(i-1)) * n // (T_i - T_(i-1)): the middle of the second.
    middle = first + (UNIFORM53 - first) // 2
    assert cut.value(middle) == 1010 + (middle - first) * 11 // (UNIFORM53 - first)


def test_a_bin_with_no_value_inside_and_a_bin_with_no_weight_drop_out() -> None:
    cut = cut_table(offset_table(OFFSET), 1000, (1012, 1100), 1)
    assert cut is not None and cut.spans == ((1012, 1030),) and cut.cuts == (UNIFORM53,)
    zero = Histogram(bins=("0", "10", "30"), bin_weights=("0", "3"))
    cut = cut_table(offset_table(zero), 1000, (1000, 1100), 1)
    assert cut is not None and cut.spans == ((1010, 1030),) and cut.cuts == (UNIFORM53,)
    assert cut_table(offset_table(OFFSET), 1000, (1031, 1100), 1) is None  # past every bin
    assert cut_table(offset_table(OFFSET), 1000, (900, 999), 1) is None  # before every bin


def test_an_interval_with_no_upper_end_keeps_the_whole_bins_it_starts_in() -> None:
    cut = cut_table(offset_table(OFFSET), 1000, (1020, None), 1)
    assert cut is not None and cut.spans == ((1020, 1030),)


def test_a_lower_closed_and_an_upper_closed_bin_hold_their_own_edges() -> None:
    lower = cut_table(offset_table(OFFSET), 1000, (1000, 1100), 1)
    upper = cut_table(offset_table(UPPER), 1000, (1000, 1100), 1)
    assert lower is not None and upper is not None
    assert lower.spans == ((1000, 1009), (1010, 1030))
    assert upper.spans == ((1000, 1010), (1011, 1030))
    # Weights 1 and 3 over 11 and 20 values, each whole inside: the cut is the same.
    assert upper.cuts == (UNIFORM53 // 4, UNIFORM53)
    assert upper.value(upper.cuts[0]) == 1011
    partial = cut_table(offset_table(UPPER), 1000, (1005, 1010), 1)
    assert partial is not None and partial.spans == ((1005, 1010),)


def test_a_timestamp_bin_counts_milliseconds_from_the_parents_start() -> None:
    start = 5 * DAY_MS + 3_600_000  # an hour past midnight: bins count from the start, not midnight
    # Days 0 to 9 are the values from the start up to ten days after it, less a millisecond.
    inside = (start + 2 * DAY_MS, start + 4 * DAY_MS - 1)
    cut = cut_table(offset_table(OFFSET), start, inside, DAY_MS)
    assert cut is not None and cut.spans == (inside,)
    assert cut.cuts == (UNIFORM53,)
    assert cut.value(0) == inside[0]
    assert cut.value(UNIFORM53 - 1) == inside[1]
    # The last day of the first bin and the first day of the second: a span across both.
    across = (start + 9 * DAY_MS, start + 11 * DAY_MS - 1)
    cut = cut_table(offset_table(OFFSET), start, across, DAY_MS)
    assert cut is not None and len(cut.spans) == 2
    # One day of 10 (weight 1/10) and one of 21 (weight 3/21 = 1/7): T_1 = floor(2^53 * 7/17).
    assert cut.cuts == ((UNIFORM53 * 7) // 17, UNIFORM53)
    assert cut.spans == (
        (start + 9 * DAY_MS, start + 10 * DAY_MS - 1),
        (start + 10 * DAY_MS, start + 11 * DAY_MS - 1),
    )


def test_a_date_parent_of_a_timestamp_column_counts_from_its_midnight() -> None:
    start = bound_in_units(7, KIND_DATE, KIND_TIMESTAMP, end=False)
    cut = cut_table(offset_table(OFFSET), start, (start, start + 30 * DAY_MS), DAY_MS)
    assert cut is not None and cut.spans[0] == (start, start + 10 * DAY_MS - 1)


def test_an_offset_reaches_forward_when_a_day_from_zero_up_has_weight() -> None:
    assert offsets_reach_forward(OFFSET)
    assert not offsets_reach_forward(Histogram(bins=("-10", "-1"), bin_weights=("1",)))
    assert offsets_reach_forward(
        Histogram(bins=("-10", "0"), bin_weights=("1",))
    )  # the last bin holds 0
    assert not offsets_reach_forward(Histogram(bins=("-5", "0", "5"), bin_weights=("1", "0")))
    assert offsets_reach_forward(Histogram(bins=("-5", "0", "5"), bin_weights=("0", "1")))


# ---------------------------------------------------------------------------
# The draw and the fallback
# ---------------------------------------------------------------------------


def test_with_a_cut_table_the_value_is_one_uniform53_and_the_row_has_not_fallen_back() -> None:
    interval = (1000, 1100)
    cut = cut_table(offset_table(OFFSET), 1000, interval, 1)
    stream = _Stream(u=UNIFORM53 // 4)
    assert draw_within(interval, cut, True, stream) == (1010, False)
    assert stream.calls == [("uniform53", 0)]


def test_without_an_offset_the_value_is_one_bounded_over_the_values_of_the_interval() -> None:
    stream = _Stream(bounded=7)
    assert draw_within((1000, 1100), None, False, stream) == (1007, False)
    assert stream.calls == [("bounded", 101)]
    stamps = _Stream(bounded=3)
    assert draw_within((5 * DAY_MS, 6 * DAY_MS - 1), None, False, stamps) == (5 * DAY_MS + 3, False)
    assert stamps.calls == [("bounded", DAY_MS)]


def test_a_row_whose_bins_leave_no_value_inside_falls_back_to_uniform_and_says_so() -> None:
    interval = (1100, 1110)
    cut = cut_table(offset_table(OFFSET), 1000, interval, 1)
    assert cut is None
    stream = _Stream(bounded=10)
    assert draw_within(interval, cut, True, stream) == (1110, True)
    assert stream.calls == [("bounded", 11)]


def test_which_sampler_a_row_takes_depends_on_the_interval_and_the_bins_not_on_a_draw() -> None:
    interval = (1000, 1100)
    cut = cut_table(offset_table(OFFSET), 1000, interval, 1)
    for u in (0, 5, UNIFORM53 // 2, UNIFORM53 - 1):
        stream = _Stream(u=u)
        draw_within(interval, cut, True, stream)
        assert [name for name, _ in stream.calls] == ["uniform53"]
    for bounded in (0, 1, 100):
        stream = _Stream(bounded=bounded)
        draw_within(interval, None, False, stream)
        assert [name for name, _ in stream.calls] == ["bounded"]


def test_an_interval_with_no_upper_end_and_no_cut_has_nothing_to_draw_over() -> None:
    with pytest.raises(ValueError):
        draw_within((1000, None), None, False, _Stream())
    # A single-day interval is one value, drawn all the same.
    assert draw_within((1000, 1000), None, False, _Stream(bounded=0)) == (1000, False)
