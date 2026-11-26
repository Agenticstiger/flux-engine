# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""When a noisy count stands clear of its noise (ADR 0019, amendment of 2026-11-25).

``noise_floor`` is the union bound over a histogram's ``m`` cells at the confidence
``1 - e^-5`` the widening uses: ``sqrt(2 v (5 + ceil(ln m)))`` for Gaussian noise of variance
``v``, ``b (5 + ceil(ln m))`` for Laplace noise of scale ``b``. ``ceil_ln`` is exact, so the floor
is the same number on every platform.
"""

from __future__ import annotations

import math
import random
from fractions import Fraction

import pytest

from flux_stats.noise import CONFIDENCE_EXPONENT, ceil_ln, noise_floor


def test_ceil_ln_is_the_least_power_of_e_at_or_above_the_count() -> None:
    for count in [*range(1, 3000), 10**6, 10**9, 10**12]:
        k = ceil_ln(count)
        assert k == math.ceil(math.log(count)), count
        assert math.e**k >= count and (k == 0 or math.e ** (k - 1) < count), count


@pytest.mark.parametrize("k", range(1, 21))
def test_ceil_ln_steps_at_each_power_of_e(k: int) -> None:
    below, above = math.floor(math.e**k), math.ceil(math.e**k)
    assert ceil_ln(below) == k and ceil_ln(above) == k + 1


def test_ceil_ln_needs_a_count_of_at_least_one() -> None:
    with pytest.raises(ValueError):
        ceil_ln(0)


@pytest.mark.parametrize("mechanism", ["gaussian", "discreteGaussian"])
def test_a_gaussian_floor_is_the_union_bound_of_its_tail(mechanism: str) -> None:
    # m = 12 cells: ceil(ln 12) = 3, so the floor is sqrt(2 v (5 + 3)) = 4 sigma.
    assert noise_floor(mechanism, Fraction(50), 12) == 200
    assert noise_floor(mechanism, Fraction("2.5"), 12) == 10
    # m = 202, a year grid with its outer bins: ceil(ln 202) = 6.
    floor = noise_floor(mechanism, Fraction("1249.444321"), 202)
    assert floor >= Fraction("1249.444321") * Fraction(math.sqrt(2 * 11))
    assert floor - Fraction("1249.444321") * Fraction(math.sqrt(2 * 11)) < Fraction(1, 10**6)


def test_a_laplace_floor_is_its_scale_times_the_exponent() -> None:
    assert noise_floor("laplace", Fraction(3), 12) == 3 * (CONFIDENCE_EXPONENT + 3)
    assert noise_floor("laplace", Fraction(10), 202) == 110


def test_the_floor_grows_with_the_cells_and_the_scale() -> None:
    floors = [noise_floor("gaussian", Fraction(5), cells) for cells in (1, 3, 21, 1100, 60000)]
    assert floors == sorted(floors) and len(set(floors)) == len(floors)
    assert noise_floor("gaussian", Fraction(6), 12) > noise_floor("gaussian", Fraction(5), 12)


def test_empty_cells_rarely_clear_the_floor() -> None:
    # With 200 empty cells of sigma 10, the noise alone clears the floor in at most e^-5 of the
    # histograms (about 0.7 percent); the per-cell bound sqrt(10 v) would let one through often.
    rng = random.Random(20261125)
    floor = float(noise_floor("gaussian", Fraction(10), 200))
    per_cell = math.sqrt(10 * 100)
    cleared = sum(max(rng.gauss(0, 10) for _ in range(200)) > floor for _ in range(2000))
    loose = sum(max(rng.gauss(0, 10) for _ in range(200)) > per_cell for _ in range(2000))
    assert cleared <= 2000 * math.exp(-5)
    assert loose > 10 * max(cleared, 1)


@pytest.mark.parametrize(
    ("scale", "cells"), [(Fraction(-1), 3), (Fraction(1), 0)], ids=["negative scale", "no cells"]
)
def test_a_floor_needs_cells_and_a_scale_of_at_least_zero(scale: Fraction, cells: int) -> None:
    with pytest.raises(ValueError):
        noise_floor("gaussian", scale, cells)


def test_the_floor_steps_where_the_count_of_cells_passes_a_power_of_e() -> None:
    # e^3 is about 20.09: 20 cells have ceil(ln m) = 3, and 21 have 4.
    assert (ceil_ln(20), ceil_ln(21)) == (3, 4)
    assert noise_floor("gaussian", Fraction(1), 20) == 4  # sqrt(2 (5 + 3))
    above = noise_floor("gaussian", Fraction(1), 21)  # sqrt(2 (5 + 4)) = sqrt(18), rounded up
    assert above * above >= 18 and above - Fraction(math.sqrt(18)) < Fraction(1, 10**6)
    assert noise_floor("laplace", Fraction(1), 20) == 8
    assert noise_floor("laplace", Fraction(1), 21) == 9
