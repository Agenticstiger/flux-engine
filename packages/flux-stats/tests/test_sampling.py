# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The block-sample estimator and its interval, against the formula worked out by hand.

The t table is checked against the t distribution's closed form for whole
degrees of freedom (Abramowitz and Stegun 26.7.3 and 26.7.4), computed here in
binary floating point, which is fine in a test: every tabulated value covers
at least 95 % and a ten-thousandth less covers less. The square root is
checked as a bound, the estimate against a hand computation, and the interval
against the coverage it claims: over a hundred seeds, a 5 % sample of whole
blocks covers the full mean at least ninety times.
"""

from __future__ import annotations

import math
import random
from fractions import Fraction
from statistics import NormalDist
from typing import List, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_stats.sampling import T_975, ratio_estimate, sqrt_upper, t_critical


def _covered(t: float, degrees: int) -> float:
    """``P(|T| <= t)`` for Student t with *degrees* degrees of freedom."""
    theta = math.atan(t / math.sqrt(degrees))
    cos, sin = math.cos(theta), math.sin(theta)
    if degrees == 1:
        return 2 * theta / math.pi
    if degrees % 2:
        total, coefficient = cos, 1.0
        for power in range(3, degrees, 2):
            coefficient *= (power - 1) / power
            total += coefficient * cos**power
        return 2 / math.pi * (theta + sin * total)
    total, coefficient = 1.0, 1.0
    for power in range(2, degrees, 2):
        coefficient *= (power - 1) / power
        total += coefficient * cos**power
    return sin * total


@pytest.mark.parametrize("degrees", sorted(T_975))
def test_each_t_value_covers_at_least_95_percent_and_is_tight(degrees: int) -> None:
    value = float(T_975[degrees])
    assert _covered(value, degrees) >= 0.95
    assert _covered(value - 0.0001, degrees) < 0.95


def test_a_degree_between_entries_reads_the_larger_t_and_120_is_the_floor() -> None:
    assert t_critical(35) == T_975[30]
    assert t_critical(59) == T_975[40]
    assert t_critical(10**6) == T_975[120]
    assert float(T_975[120]) > NormalDist().inv_cdf(0.975)
    for bad in (0, -1, True):
        with pytest.raises(ValueError):
            t_critical(bad)


@settings(max_examples=300)
@given(value=st.fractions(min_value=0, max_value=10**12, max_denominator=10**9))
def test_the_square_root_is_bounded_above_by_less_than_one_step(value: Fraction) -> None:
    bound = sqrt_upper(value)
    step = Fraction(1, 1 << 64)
    assert bound * bound >= value
    assert bound == 0 or (bound - step) ** 2 < value


def test_the_estimate_and_its_interval_are_the_formula() -> None:
    # Three chosen blocks of ten: totals 12, 15, 9 over 4, 5, 3 records.
    blocks = [(12, 4), (15, 5), (9, 3)]
    found = ratio_estimate(blocks, 10)
    estimate = Fraction(36, 12)
    assert found.estimate == estimate == 3
    # Every block's total is exactly 3 per record: no residual, no width.
    assert (found.low, found.high) == (estimate, estimate)
    uneven = ratio_estimate([(10, 4), (20, 5), (3, 3)], 10)
    ratio = Fraction(33, 12)
    residual = sum(((y - ratio * m) ** 2 for y, m in [(10, 4), (20, 5), (3, 3)]), Fraction(0))
    variance = (1 - Fraction(3, 10)) * Fraction(3, 2) * residual / 12**2
    half = T_975[2] * sqrt_upper(variance)
    assert (uneven.estimate, uneven.low, uneven.high) == (ratio, ratio - half, ratio + half)
    assert (uneven.blocks, uneven.population, uneven.records) == (3, 10, 12)
    # Wider than the formula with the exact t and square root, never narrower.
    exact_half = 4.302652729911275 * math.sqrt(float(variance))
    assert float(half) >= exact_half


def test_a_sample_of_every_block_is_exact() -> None:
    found = ratio_estimate([(10, 4), (20, 5)], 2)
    assert found.exact and found.low == found.high == Fraction(30, 9)


@pytest.mark.parametrize(
    "blocks, population",
    [
        ([(5, 2)], 3),
        ([], 3),
        ([(0, 0), (0, 0)], 3),
        ([(1, 1), (1, 1), (1, 1)], 2),
        ([(1, -1), (1, 2)], 3),
    ],
)
def test_a_sample_that_cannot_give_an_interval_is_refused(
    blocks: List[Tuple[int, int]], population: int
) -> None:
    with pytest.raises(ValueError):
        ratio_estimate(blocks, population)


def test_a_five_percent_block_sample_covers_the_full_mean_at_least_ninety_times_in_a_hundred() -> (
    None
):
    generator = random.Random(2026)
    population: List[Tuple[int, int]] = []
    for _ in range(100):
        records = generator.randint(80, 120)
        centre = generator.gauss(50, 8)
        total = sum(round(generator.gauss(centre, 15)) for _ in range(records))
        population.append((total, records))
    full = Fraction(sum(y for y, _ in population), sum(m for _, m in population))
    covered = 0
    for seed in range(100):
        chosen = random.Random(seed).sample(population, 5)
        found = ratio_estimate(chosen, len(population))
        covered += found.low <= full <= found.high
    assert covered >= 90
