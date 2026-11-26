# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The share of rows two dates drawn apart would put out of order (ADR 0024 section 10).

Exact under two assumptions: the two columns are drawn independently, and each bin's mass is
spread evenly over the days (a DATE) or the milliseconds (a TIMESTAMP) it covers.
"""

from __future__ import annotations

import random
from fractions import Fraction
from typing import List, Tuple

import pytest

from flux_engine.author.date_spread import DateSpread, independent_break_share, spread_of
from flux_engine.author.orders import DateOrder, contract_date_orders
from flux_engine.seam.contract import LogicalKind

DAY = 86_400_000


def _order(strict: bool = False, null_end: bool = False, null_start: bool = False) -> DateOrder:
    return DateOrder("r", "a", "b", strict, null_end, null_start)


def _days(*edges: int, weights: Tuple[int, ...] = (), nulls: Fraction = Fraction(0)) -> DateSpread:
    spread = spread_of(
        LogicalKind.DATE,
        [edge * DAY for edge in edges],
        [Fraction(weight) for weight in weights] or [Fraction(1)] * (len(edges) - 1),
        nulls,
    )
    assert spread is not None
    return spread


def test_two_days_drawn_apart_are_out_of_order_one_time_in_four() -> None:
    both = _days(0, 2)
    assert independent_break_share(both, both, _order()) == Fraction(1, 4)  # end before start
    assert independent_break_share(both, both, _order(strict=True)) == Fraction(
        3, 4
    )  # or the same day


def test_an_end_that_cannot_be_before_its_start_breaks_nothing() -> None:
    assert independent_break_share(_days(0, 10), _days(10, 20), _order()) == 0
    assert independent_break_share(_days(0, 10), _days(10, 20), _order(strict=True)) == 0
    assert independent_break_share(_days(0, 11), _days(9, 20), _order()) == Fraction(1, 121)
    assert independent_break_share(_days(0, 11), _days(9, 20), _order(strict=True)) == Fraction(
        3, 121
    )


def test_an_end_that_is_always_before_its_start_breaks_everything() -> None:
    assert independent_break_share(_days(10, 20), _days(0, 10), _order()) == 1
    assert independent_break_share(_days(10, 20), _days(0, 11), _order()) == Fraction(109, 110)
    assert independent_break_share(_days(10, 20), _days(0, 11), _order(strict=True)) == 1


def test_a_bin_weighs_by_its_share_of_the_rows() -> None:
    start = _days(0, 1, 10, 11, weights=(1, 0, 3))  # a quarter on day 0, three quarters on day 10
    end = _days(5, 6)  # always day 5
    assert independent_break_share(start, end, _order()) == Fraction(3, 4)


def test_a_bin_of_no_mass_is_left_out() -> None:
    start = _days(0, 1, 5, 6, weights=(1, 0, 1))
    assert [bin_[:2] for bin_ in start.bins] == [(0, 1), (5, 6)]
    assert sum(bin_[2] for bin_ in start.bins) == 1


def test_a_timestamp_is_spread_over_its_milliseconds() -> None:
    start = spread_of(LogicalKind.TIMESTAMP, [0, 4], [Fraction(1)], Fraction(0))
    assert start is not None
    assert independent_break_share(start, start, _order()) == Fraction(6, 16)
    assert independent_break_share(start, start, _order(strict=True)) == Fraction(10, 16)


def test_a_date_grid_is_the_day_of_each_edge() -> None:
    spread = spread_of(LogicalKind.DATE, [3 * DAY, 5 * DAY + 5], [Fraction(1)], Fraction(0))
    assert spread is not None and spread.bins == ((3, 5, Fraction(1)),)


def test_two_kinds_are_not_on_one_grid() -> None:
    stamp = spread_of(LogicalKind.TIMESTAMP, [0, 4], [Fraction(1)], Fraction(0))
    assert stamp is not None
    assert independent_break_share(stamp, _days(0, 2), _order()) is None


@pytest.mark.parametrize(
    ("edges", "weights", "nulls"),
    [
        ([0, DAY], [Fraction(0)], Fraction(0)),  # no mass
        ([0, DAY], [Fraction(1)], Fraction(1)),  # nothing but NULLs
        (
            [0, 2 * DAY + 5, 2 * DAY + 10],
            [Fraction(1), Fraction(1)],
            Fraction(0),
        ),  # a bin of no day
        ([0], [], Fraction(0)),
    ],
)
def test_a_histogram_with_nothing_to_draw_has_no_spread(
    edges: List[int], weights: List[Fraction], nulls: Fraction
) -> None:
    assert spread_of(LogicalKind.DATE, edges, weights, nulls) is None


def test_the_share_equals_the_count_of_every_pair_of_days() -> None:
    generator = random.Random(7)
    for _ in range(200):
        starts, ends = [sorted(generator.sample(range(-4, 14), 3)) for _ in range(2)]
        weights = [(generator.randint(1, 5), generator.randint(1, 5)) for _ in range(2)]
        start, end = (
            _days(*edges, weights=weight)
            for edges, weight in zip((starts, ends), weights, strict=True)
        )
        for strict in (False, True):
            expected = Fraction(0)
            for s_low, s_high, s_weight in start.bins:
                for e_low, e_high, e_weight in end.bins:
                    for s in range(s_low, s_high):
                        for e in range(e_low, e_high):
                            if e < s or (strict and e == s):
                                expected += (
                                    s_weight * e_weight / ((s_high - s_low) * (e_high - e_low))
                                )
            assert independent_break_share(start, end, _order(strict)) == expected


def test_a_null_date_breaks_the_check_that_does_not_hold_on_it() -> None:
    separate = (_days(0, 10, nulls=Fraction(1, 5)), _days(10, 20, nulls=Fraction(1, 4)))
    start, end = separate
    # The ends are never before the starts, so only the rows with a NULL date can break it.
    assert independent_break_share(start, end, _order()) == Fraction(1, 4) * Fraction(
        4, 5
    ) + Fraction(1, 5) * Fraction(3, 4) + Fraction(1, 20)
    assert independent_break_share(start, end, _order(null_end=True)) == Fraction(1, 5) * Fraction(
        3, 4
    )
    assert independent_break_share(start, end, _order(null_start=True)) == Fraction(
        1, 4
    ) * Fraction(4, 5)
    assert independent_break_share(start, end, _order(null_end=True, null_start=True)) == 0


def test_the_tolerance_is_the_share_of_a_percent_band_with_an_upper_bound() -> None:
    def tolerance(**band: object) -> Tuple[Fraction, bool]:
        invariant = {"id": "r", "check": "b >= a", **band}
        (found,) = contract_date_orders({"contract": {"invariants": [invariant]}})
        return found.tolerance, found.tolerance_open

    assert tolerance(mustBeLessOrEqualTo=5, unit="percent") == (Fraction(1, 20), False)
    assert tolerance(mustBeLessThan="2.5", unit="percent") == (Fraction(1, 40), True)
    assert tolerance(mustBeBetween=[0, 10], unit="percent") == (Fraction(1, 10), False)
    assert tolerance(mustBeBetween=[1, 10], unit="percent") == (0, False)
    assert tolerance(mustBeLessOrEqualTo=5) == (0, False)  # a count of rows
    assert tolerance(mustBeLessOrEqualTo=5, unit="rows") == (0, False)
    assert tolerance(mustBe=0, unit="percent") == (0, False)
    assert tolerance(mustBeLessOrEqualTo="five", unit="percent") == (0, False)
    assert tolerance() == (0, False)


def test_a_share_is_tolerated_up_to_the_bound_and_not_at_an_open_one() -> None:
    closed = DateOrder("r", "a", "b", False, False, False, Fraction(1, 20), False)
    opened = DateOrder("r", "a", "b", False, False, False, Fraction(1, 20), True)
    assert closed.tolerates(Fraction(1, 20)) and not closed.tolerates(Fraction(1, 19))
    assert not opened.tolerates(Fraction(1, 20)) and opened.tolerates(Fraction(1, 21))
    assert _order().tolerates(Fraction(0)) and not _order().tolerates(Fraction(1, 1000))
