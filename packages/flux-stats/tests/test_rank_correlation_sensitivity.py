# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The two rank correlations and the sensitivity a noisy release of one is charged (ADR 0035).

``kendallTau/1`` moves by at most ``4 / (f + 1)`` and ``spearmanRho/1`` by at most ``9 / (f + 1)``
when one record is added or removed, for a row floor ``f`` and ``N = max(n, f)``. The bounds are
checked by brute force over every small data set on a small grid of values with heavy ties, in
exact fractions, so a reviewer can run them. The Pearson correlation of mid-ranks has no such bound,
which is why the definition of ``spearmanRho/1`` is not that.
"""

from __future__ import annotations

import itertools
import random
from fractions import Fraction
from math import comb
from typing import List, Sequence, Tuple

import pytest

from flux_stats.rank_correlation import (
    kendall_s,
    kendall_tau,
    rank_sensitivity,
    spearman_a4,
    spearman_rho,
)

pytestmark = pytest.mark.unit

Pairs = Sequence[Tuple[int, int]]

#: Two small grids of record values, so that ties are the rule. A record is a point of one of them.
GRIDS = (
    [(x, y) for x in range(3) for y in range(3)],
    [(x, y) for x in range(4) for y in range(2)],
)
MOST_RECORDS = 5
FLOORS = range(2, 8)


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _naive_s(pairs: Pairs) -> int:
    return sum(_sign(a[0] - b[0]) * _sign(a[1] - b[1]) for a, b in itertools.combinations(pairs, 2))


def _mid_ranks(values: Sequence[int]) -> List[Fraction]:
    """The mid-rank of each value, straight from the definition: ranks from 1, ties averaged."""
    return [
        sum(1 for other in values if other < value)
        + Fraction(sum(1 for other in values if other == value) + 1, 2)
        for value in values
    ]


def _naive_a4(pairs: Pairs) -> int:
    n = len(pairs)
    left = _mid_ranks([x for x, _ in pairs])
    right = _mid_ranks([y for _, y in pairs])
    centre = Fraction(n + 1, 2)
    four_a = 4 * sum((r - centre) * (q - centre) for r, q in zip(left, right, strict=True))
    assert four_a.denominator == 1
    return int(four_a)


def test_the_fast_forms_equal_the_definitions_on_random_tie_heavy_data() -> None:
    generator = random.Random(20261123)
    for _ in range(300):
        n = generator.randrange(0, 40)
        spread = generator.choice((2, 3, 6, 50))
        pairs = [(generator.randrange(spread), generator.randrange(spread)) for _ in range(n)]
        assert kendall_s(pairs) == _naive_s(pairs)
        assert spearman_a4(pairs) == _naive_a4(pairs)


def test_without_ties_the_values_are_the_textbook_coefficients() -> None:
    pairs = [(1, 3), (2, 1), (3, 2), (4, 5), (5, 4)]
    # 7 concordant and 3 discordant pairs of 10: tau = 4 / 10
    assert kendall_s(pairs) == 4 and kendall_tau(4, 5) == Fraction(2, 5)
    # Spearman: 1 - 6 * sum d^2 / (n (n^2 - 1)) with d = (2, 1, 1, 1, 1) is 1 - 6 * 8 / 120
    assert spearman_rho(spearman_a4(pairs), 5) == Fraction(3, 5)
    increasing = [(i, 10 * i) for i in range(6)]
    assert kendall_tau(kendall_s(increasing), 6) == 1
    assert spearman_rho(spearman_a4(increasing), 6) == 1


def test_a_row_floor_shrinks_the_value_towards_zero() -> None:
    pairs = [(i, i) for i in range(4)]
    assert kendall_tau(kendall_s(pairs), 4) == 1
    assert kendall_tau(kendall_s(pairs), 4, 12) == Fraction(6, comb(12, 2))
    assert spearman_rho(spearman_a4(pairs), 4, 4) == 1
    assert spearman_rho(spearman_a4(pairs), 4, 10) == Fraction(3 * 20, 10 * 99)
    with pytest.raises(ValueError):
        kendall_tau(0, 1)
    with pytest.raises(ValueError):
        spearman_rho(0, 0)


def test_the_charge_is_four_and_nine_over_the_floor_plus_one() -> None:
    for floor in FLOORS:
        assert rank_sensitivity("kendallTau/1", floor) == Fraction(4, floor + 1)
        assert rank_sensitivity("spearmanRho/1", floor) == Fraction(9, floor + 1)
    with pytest.raises(ValueError):
        rank_sensitivity("kendallTau/1", 1)
    with pytest.raises(KeyError):
        rank_sensitivity("correlation/1", 5)


@pytest.mark.parametrize("grid", GRIDS, ids=("three-by-three", "four-by-two"))
def test_one_added_or_removed_record_moves_each_value_by_no_more_than_the_bound(
    grid: List[Tuple[int, int]],
) -> None:
    """Every data set of up to five records on the grid, every floor from 2 to 7, every neighbour.

    A neighbour of a data set adds one record or removes one, so each pair of a data set and the
    data set with one record added is checked once, which covers both directions.
    """
    checked = 0
    for floor in FLOORS:
        kendall_bound = rank_sensitivity("kendallTau/1", floor)
        spearman_bound = rank_sensitivity("spearmanRho/1", floor)
        for n in range(0, MOST_RECORDS + 1):
            for records in itertools.combinations_with_replacement(grid, n):
                data = list(records)
                tau = kendall_tau(kendall_s(data), n, floor)
                rho = spearman_rho(spearman_a4(data), n, floor)
                for extra in grid:
                    bigger = [*data, extra]
                    moved_tau = abs(kendall_tau(kendall_s(bigger), n + 1, floor) - tau)
                    moved_rho = abs(spearman_rho(spearman_a4(bigger), n + 1, floor) - rho)
                    assert moved_tau <= kendall_bound, (floor, data, extra)
                    assert moved_rho <= spearman_bound, (floor, data, extra)
                    checked += 1
    assert checked > 10_000


@pytest.mark.parametrize("floor", FLOORS)
def test_the_kendall_bound_is_reached_at_a_row_count_equal_to_the_floor(floor: int) -> None:
    """``f`` records that all disagree, and one more that agrees with every one of them."""
    antichain = [(i, floor - 1 - i) for i in range(floor)]
    assert kendall_s(antichain) == -comb(floor, 2)
    bigger = [*antichain, (floor, floor)]
    assert kendall_s(bigger) == -comb(floor, 2) + floor
    moved = kendall_tau(kendall_s(bigger), floor + 1, floor) - kendall_tau(
        kendall_s(antichain), floor, floor
    )
    assert moved == Fraction(4, floor + 1) == rank_sensitivity("kendallTau/1", floor)


def _pearson_of_mid_ranks(pairs: Pairs) -> Tuple[Fraction, Fraction, Fraction]:
    """``(Sxy, Sxx, Syy)`` of the mid-ranks: the correlation is ``Sxy / sqrt(Sxx * Syy)``."""
    left = _mid_ranks([x for x, _ in pairs])
    right = _mid_ranks([y for _, y in pairs])
    n = len(pairs)
    centre = Fraction(n + 1, 2)
    sxy = sum((r - centre) * (q - centre) for r, q in zip(left, right, strict=True))
    sxx = sum((r - centre) ** 2 for r in left)
    syy = sum((q - centre) ** 2 for q in right)
    return sxy, sxx, syy


@pytest.mark.parametrize("floor", [9, 10, 25, 100])
def test_the_pearson_correlation_of_mid_ranks_has_no_bound_of_this_shape(floor: int) -> None:
    """The reason ``spearmanRho/1`` is not that: one added row moves it by more than ``9 / (f + 1)``.

    ``n - 1`` records tied at one point and one at another have a correlation of exactly 1. One more
    record, high in one column and low in the other, makes it negative: a move of more than 1, where
    the charge of ``spearmanRho/1`` is below 1 from a row floor of 9 up.
    """
    n = floor
    before = [(0, 0)] * (n - 1) + [(1, 1)]
    sxy, sxx, syy = _pearson_of_mid_ranks(before)
    assert sxy > 0 and sxy * sxy == sxx * syy, "a correlation of exactly one"
    after = [*before, (2, -1)]
    sxy2, sxx2, syy2 = _pearson_of_mid_ranks(after)
    assert sxy2 < 0, "negative after one more row"
    # the correlation of the data with the extra row is -1 / n, so the move is 1 + 1 / n
    assert sxy2 * sxy2 * n * n == sxx2 * syy2
    moved = 1 + Fraction(1, n)
    assert moved > 1 > rank_sensitivity("spearmanRho/1", floor)
    # while the same two data sets move the definition this reader uses by no more than its bound
    rho = spearman_rho(spearman_a4(before), n, floor)
    rho_after = spearman_rho(spearman_a4(after), n + 1, floor)
    assert abs(rho_after - rho) <= rank_sensitivity("spearmanRho/1", floor)
