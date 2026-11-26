# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The share of rows two dates drawn apart would put out of order (ADR 0024 section 10).

Exact under two assumptions: the two columns are drawn independently, and each bin's mass is
spread evenly over the days (a DATE) or the milliseconds (a TIMESTAMP) it covers, a bin holding its
lower edge and every grid point up to one step below its upper edge, except the last declared bin,
which the generator draws closed at the top (``_bin_top`` in ``generator/rows.py``).
"""

from __future__ import annotations

import random
from fractions import Fraction
from typing import Dict, List, Tuple

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


def _drawn(edges: Tuple[int, ...], weights: Tuple[int, ...] = ()) -> Dict[int, Fraction]:
    """The days the generator draws from a declared histogram, with their shares, counted from the edges.

    Each bin holds its lower edge up to one day below its upper edge, and the last bin its upper
    edge too; a day is drawn evenly within the bin it is in.
    """
    weights = weights or (1,) * (len(edges) - 1)
    total = sum(weights)
    found: Dict[int, Fraction] = {}
    for index, (low, high, weight) in enumerate(zip(edges[:-1], edges[1:], weights, strict=True)):
        top = high + 1 if index == len(weights) - 1 else high
        for day in range(low, top):
            found[day] = found.get(day, Fraction(0)) + Fraction(weight, total * (top - low))
    return found


def _broken_by(
    start: Dict[int, Fraction], end: Dict[int, Fraction], strict: bool = False
) -> Fraction:
    """The share of rows with an end before its start (on or before it, if *strict*), drawn apart."""
    return sum(
        (
            s_share * e_share
            for s_day, s_share in start.items()
            for e_day, e_share in end.items()
            if e_day < s_day or (strict and e_day == s_day)
        ),
        Fraction(0),
    )


def test_two_days_drawn_apart_are_out_of_order_one_time_in_three() -> None:
    # A bin of [0, 2) is closed at the top, so it draws days 0, 1 and 2.
    both = _days(0, 2)
    assert independent_break_share(both, both, _order()) == Fraction(3, 9)  # end before start
    assert independent_break_share(both, both, _order(strict=True)) == Fraction(6, 9)  # or the same


def test_an_end_that_cannot_be_before_its_start_breaks_nothing() -> None:
    # The start's last bin holds no mass, so its bin before is half-open: days 0 to 9, and the
    # end is day 10 or later.
    start = _days(0, 10, 20, weights=(1, 0))
    assert independent_break_share(start, _days(10, 20), _order()) == 0
    assert independent_break_share(start, _days(10, 20), _order(strict=True)) == 0
    # Where the start's last bin holds the mass, it is closed and the end can tie it.
    assert independent_break_share(_days(0, 10), _days(10, 20), _order()) == 0
    assert independent_break_share(_days(0, 10), _days(10, 20), _order(strict=True)) == Fraction(
        1, 11 * 11
    )


def test_a_last_bin_closed_at_the_top_can_put_a_start_a_day_past_an_end() -> None:
    # Half-open, a start in [0, 10) is at most 9 and an end in [9, 20) at least 9: never out of
    # order. The generator draws the last bin closed, so a start can be day 10 and an end day 9.
    start, end = _days(0, 10), _days(9, 20)
    share = independent_break_share(start, end, _order())
    assert share == _broken_by(_drawn((0, 10)), _drawn((9, 20))) == Fraction(1, 11 * 12)
    # Touching bins: the tie of days 10 and 10 breaks a strict check, and only that.
    touching = independent_break_share(_days(0, 10), _days(10, 20), _order(strict=True))
    assert touching == Fraction(1, 11 * 11)
    assert independent_break_share(_days(0, 10), _days(10, 20), _order()) == 0


def test_only_the_last_declared_bin_is_closed() -> None:
    spread = spread_of(
        LogicalKind.DATE, [0, 10 * DAY, 20 * DAY], [Fraction(1), Fraction(1)], Fraction(0)
    )
    assert spread is not None
    assert spread.bins == ((0, 10, Fraction(1, 2)), (10, 21, Fraction(1, 2)))
    # A last bin that holds no mass stays out, and leaves the bin before it half-open.
    empty = spread_of(
        LogicalKind.DATE, [0, 10 * DAY, 20 * DAY], [Fraction(1), Fraction(0)], Fraction(0)
    )
    assert empty is not None and empty.bins == ((0, 10, Fraction(1)),)
    # A one-day last bin holds both of its edges.
    one = spread_of(
        LogicalKind.DATE, [0, 10 * DAY, 11 * DAY], [Fraction(1), Fraction(1)], Fraction(0)
    )
    assert one is not None and one.bins[-1] == (10, 12, Fraction(1, 2))


def test_an_end_that_is_always_before_its_start_breaks_everything() -> None:
    assert independent_break_share(_days(11, 20), _days(0, 10), _order()) == 1
    assert independent_break_share(_days(10, 20), _days(0, 10), _order()) == Fraction(120, 121)
    assert independent_break_share(_days(10, 20), _days(0, 10), _order(strict=True)) == 1


def test_a_bin_weighs_by_its_share_of_the_rows() -> None:
    # A quarter on day 0, three quarters on day 10 or 11; the end is day 5 or 6.
    start = _days(0, 1, 10, 11, weights=(1, 0, 3))
    end = _days(5, 6)
    assert independent_break_share(start, end, _order()) == _broken_by(
        _drawn((0, 1, 10, 11), (1, 0, 3)), _drawn((5, 6))
    )
    assert independent_break_share(start, end, _order()) == Fraction(3, 4)


def test_a_bin_of_no_mass_is_left_out() -> None:
    start = _days(0, 1, 5, 6, weights=(1, 0, 1))
    assert [bin_[:2] for bin_ in start.bins] == [(0, 1), (5, 7)]  # the last bin is closed
    assert sum(bin_[2] for bin_ in start.bins) == 1


def test_a_timestamp_is_spread_over_its_milliseconds() -> None:
    start = spread_of(LogicalKind.TIMESTAMP, [0, 4], [Fraction(1)], Fraction(0))
    assert start is not None  # milliseconds 0 to 4, the last one drawn too
    assert independent_break_share(start, start, _order()) == Fraction(10, 25)
    assert independent_break_share(start, start, _order(strict=True)) == Fraction(15, 25)


def test_a_date_grid_is_the_day_of_each_edge() -> None:
    spread = spread_of(LogicalKind.DATE, [3 * DAY, 5 * DAY + 5], [Fraction(1)], Fraction(0))
    assert spread is not None and spread.bins == ((3, 6, Fraction(1)),)


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
        starts, ends = [tuple(sorted(generator.sample(range(-4, 14), 3))) for _ in range(2)]
        weights = [(generator.randint(0, 5), generator.randint(1, 5)) for _ in range(2)]
        start, end = (
            _days(*edges, weights=weight)
            for edges, weight in zip((starts, ends), weights, strict=True)
        )
        for strict in (False, True):
            # Counted from the declared edges, not from the spread: the engine's own draw.
            assert independent_break_share(start, end, _order(strict)) == _broken_by(
                _drawn(starts, weights[0]), _drawn(ends, weights[1]), strict
            )


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


def test_a_share_is_tolerated_only_with_three_standard_errors_to_spare_at_the_row_count() -> None:
    # A share of 4% in a band of 5%: the room is 1 point, and 3 * sqrt(0.04 * 0.96 / n) fits in it
    # from n = 3,456 rows up, since 9 * 0.04 * 0.96 = 0.3456 = n * 0.01 ** 2 there.
    closed = DateOrder("r", "a", "b", False, False, False, Fraction(1, 20), False)
    opened = DateOrder("r", "a", "b", False, False, False, Fraction(1, 20), True)
    share = Fraction(1, 25)
    assert closed.tolerates(share, 3456) and not closed.tolerates(share, 3455)
    assert not opened.tolerates(share, 3456) and opened.tolerates(share, 3457)
    # No row count known: the shares themselves are compared.
    assert closed.tolerates(share) and opened.tolerates(share)
    assert not closed.tolerates(Fraction(1, 19)) and not closed.tolerates(Fraction(1, 19), 10**9)


def test_a_share_of_none_is_tolerated_by_a_band_of_none_at_any_row_count() -> None:
    assert _order().tolerates(Fraction(0), 1) and _order().tolerates(Fraction(0), 10**9)
    assert not DateOrder("r", "a", "b", False, False, False, Fraction(0), True).tolerates(
        Fraction(0), 10**9
    )
