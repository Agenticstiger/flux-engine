# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The widening a distance tolerance takes from sampling (``flux_stats.noise.sampling_widening``)."""

from __future__ import annotations

from fractions import Fraction
from math import isqrt, sqrt

import pytest

from flux_stats.noise import sampling_widening, widening

pytestmark = pytest.mark.unit


def _ceil_root(value: int) -> Fraction:
    """The root of a whole number rounded up to 12 places, by exact integers."""
    scaled = value * 10**24
    root = isqrt(scaled)
    if root * root < scaled:
        root += 1
    return Fraction(root, 10**12)


def _up(value: Fraction) -> Fraction:
    return Fraction(-((-value.numerator * 10**12) // value.denominator), 10**12)


def test_a_known_value_is_the_formula_with_each_root_rounded_up() -> None:
    # m = 4, n = 100, p = 1/4: v = 25, sqrt(m) = 2, sqrt(m v) = 10, sqrt(10 v) = sqrt(250).
    expected = _up(Fraction(2) * (Fraction(10) + _ceil_root(250)) / 100 + Fraction(4, 200))
    assert sampling_widening(4, 100, Fraction(1, 4)) == expected
    # m = 9, n = 1000, p = 1/10: v = 100, sqrt(m) = 3, sqrt(m v) = 30, sqrt(10 v) = sqrt(1000).
    expected = _up(Fraction(3) * (Fraction(30) + _ceil_root(1000)) / 1000 + Fraction(9, 2000))
    assert sampling_widening(9, 1000, Fraction(1, 10)) == expected


def test_smaller_samples_are_wider() -> None:
    widths = [sampling_widening(5, rows, Fraction(1, 3)) for rows in (10, 100, 1000, 10000)]
    assert widths == sorted(widths, reverse=True)
    assert len(set(widths)) == len(widths)


def test_exact_allocation_keeps_only_the_rounding_term() -> None:
    assert sampling_widening(4, 100, Fraction(1, 4), exact_allocation=True) == Fraction(2, 100)
    third = sampling_widening(1, 3, Fraction(1), exact_allocation=True)
    assert third == Fraction(166666666667, 10**12)
    assert third >= Fraction(1, 6)
    assert sampling_widening(4, 100, Fraction(1, 4)) > Fraction(2, 100)


def test_it_is_the_noise_widening_with_the_sampling_variance() -> None:
    assert sampling_widening(7, 250, Fraction(2, 5)) == widening(
        Fraction(100), 7, Fraction(250), 250
    )


@pytest.mark.parametrize(
    "cells, rows, share",
    [(0, 10, Fraction(1, 2)), (3, 0, Fraction(1, 2)), (3, -1, Fraction(1, 2))]
    + [(3, 10, Fraction(0)), (3, 10, Fraction(-1, 2)), (3, 10, Fraction(3, 2))],
)
def test_it_refuses_what_has_no_bound(cells: int, rows: int, share: Fraction) -> None:
    with pytest.raises(ValueError):
        sampling_widening(cells, rows, share)
    with pytest.raises(ValueError):
        sampling_widening(cells, rows, share, exact_allocation=True)


@pytest.mark.parametrize("cells, rows, share", [(2, 50, 0.5), (10, 1000, 0.2), (3, 7, 1.0)])
@pytest.mark.parametrize("exact", [False, True])
def test_it_is_never_below_the_unrounded_bound(
    cells: int, rows: int, share: float, exact: bool
) -> None:
    v = rows * share
    bound = cells / (2 * rows)
    if not exact:
        bound += sqrt(cells) * (sqrt(cells * v) + sqrt(10 * v)) / rows
    got = sampling_widening(cells, rows, Fraction(share), exact_allocation=exact)
    assert float(got) >= bound - 1e-12
    assert (10**12) % got.denominator == 0
