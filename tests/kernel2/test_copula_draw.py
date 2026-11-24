# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Gaussian copula's draw (ADR 0035 decision 6): normals, the latent mix, bins and values."""

from __future__ import annotations

import math
from fractions import Fraction
from statistics import NormalDist
from typing import Callable, List, Sequence, Tuple

import numpy as np
import pytest

from flux_engine.core import copula, kernel2
from flux_engine.core.errors import FluxError

ROWS = 10_000
KEY = kernel2.key_of(b"copula-draw-tests")
INV = NormalDist().inv_cdf


def _words(rows: int, draws: int) -> List["np.ndarray"]:
    index = np.arange(rows, dtype=np.uint64)
    return [kernel2.column_words(KEY, index, d) for d in range(draws)]


def test_uniform_is_symmetric_and_strictly_inside() -> None:
    assert copula.uniform(0) == 2.0**-53
    assert copula.uniform((1 << 64) - 1) == 1.0 - 2.0**-53
    assert copula.uniform(1 << 12) == 3 * 2.0**-53
    for word in (0, 5, 1 << 63, (1 << 64) - 1, 123456789123456789):
        mirrored = copula.uniform((1 << 64) - 1 - word)
        assert copula.uniform(word) + mirrored == 1.0


def test_the_array_uniform_and_normal_are_the_scalar_ones() -> None:
    words = _words(ROWS, 1)[0]
    scalar_u = [copula.uniform(int(w)) for w in words]
    assert [v.hex() for v in copula.uniform_array(words).tolist()] == [v.hex() for v in scalar_u]
    scalar_z = [copula.normal(int(w)) for w in words]
    assert [v.hex() for v in copula.normal_array(words).tolist()] == [v.hex() for v in scalar_z]
    assert copula.normal(1 << 63) == pytest.approx(INV(0.5 + 2.0**-54), abs=1e-12)


def test_latent_row_by_hand() -> None:
    rows = [[1.0], [0.5, 0.75], [0.25, -0.5, 0.125]]
    z = [2.0, -4.0, 8.0]
    assert copula.latent_row(rows, z) == [2.0, 1.0 - 3.0, 0.5 + 2.0 + 1.0]


def test_latent_row_adds_strictly_left_to_right() -> None:
    big = 2.0**53
    # (1 + big) - big = 0 in binary64 but 1 + (big - big) = 1: the order is observable.
    assert copula.latent_row([[1.0], [1.0, 1.0], [1.0, 1.0, 1.0]], [1.0, big, -big])[2] == 0.0


def _factor_of(size: int, r: str) -> List[List[float]]:
    names = [f"c{i}" for i in range(size)]
    pairs = {(names[i], names[j]): r for i in range(size) for j in range(i)}
    return [list(row) for row in copula.plan_copula(names, "kendall", pairs).factor]


@pytest.mark.parametrize("size,r", [(2, "0.6"), (3, "0.4"), (3, "-0.9"), (5, "0.7"), (5, "-0.4")])
def test_the_latent_columns_are_the_rows_bit_for_bit(size: int, r: str) -> None:
    rows = _factor_of(size, r)
    words = _words(ROWS, size)
    z = [copula.normal_array(w) for w in words]
    columns = copula.latent_columns(rows, z)
    for row in range(0, ROWS, 7):
        zs = [copula.normal(int(words[d][row])) for d in range(size)]
        per_row = copula.latent_row(rows, zs)
        assert [columns[i][row].hex() for i in range(size)] == [v.hex() for v in per_row]


def test_the_column_path_checks_subnormals(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> None:
        raise FluxError(kernel2.FLX_5050, "flushed")

    monkeypatch.setattr(kernel2, "require_subnormals", refuse)
    law = copula.ColumnLaw([Fraction(1)], [2])
    with pytest.raises(FluxError):
        copula.latent_columns([[1.0]], [np.zeros(2)])
    with pytest.raises(FluxError):
        law.locate_columns(np.zeros(2))


def test_thresholds_of_exact_shares() -> None:
    assert copula.threshold(0, 1) == -math.inf
    assert copula.threshold(1, 1) == math.inf
    assert copula.threshold(1, 2) == 0.0
    assert copula.threshold(1, 4) == pytest.approx(INV(0.25), abs=1e-12)
    assert copula.threshold(3, 4) == pytest.approx(INV(0.75), abs=1e-12)
    # An upper-tail share keeps its digits: 1 - 2**-60 is not a binary64, its complement is.
    assert copula.threshold((1 << 60) - 1, 1 << 60) == -copula.threshold(1, 1 << 60)
    assert copula.threshold(1, 1 << 60) == pytest.approx(INV(2.0**-60), rel=1e-12)
    # Below 2**-1022 the argument is infinite, at the edge it is not.
    assert copula.threshold(1, 1 << 1023) == -math.inf
    assert copula.threshold(1, 1 << 1022) < -37.0
    assert copula.threshold((1 << 1023) - 1, 1 << 1023) == math.inf


def test_a_column_by_hand_with_a_zero_weight_bin() -> None:
    law = copula.ColumnLaw([Fraction(1, 4), Fraction(0), Fraction(3, 4)], [2, 5, 4])
    cases = [
        (-5.0, (1, 0)),
        (INV(0.125) - 1e-9, (1, 0)),
        (INV(0.125) + 1e-9, (1, 1)),
        (INV(0.25) - 1e-9, (1, 1)),
        (INV(0.25) + 1e-9, (3, 0)),
        (INV(0.4375) - 1e-9, (3, 0)),
        (INV(0.4375) + 1e-9, (3, 1)),
        (INV(0.625) + 1e-9, (3, 2)),
        (INV(0.8125) - 1e-9, (3, 2)),
        (INV(0.8125) + 1e-9, (3, 3)),
        (6.0, (3, 3)),
    ]
    for x, expected in cases:
        assert law.locate(x) == expected, x
    assert law.bin_threshold(1) == law.bin_threshold(2)


def test_infinite_latent_values_never_pick_a_zero_weight_bin() -> None:
    law = copula.ColumnLaw(
        [Fraction(0), Fraction(1, 2), Fraction(1, 2), Fraction(0), Fraction(0)], [3, 4, 6, 2, 9]
    )
    assert law.locate(-math.inf) == (2, 0)
    assert law.locate(math.inf) == (3, 5)
    b, lo = law.locate_columns(np.array([-math.inf, math.inf]))
    assert (b.tolist(), lo.tolist()) == ([2, 3], [0, 5])


def test_a_latent_value_on_a_threshold_takes_the_upper_cell() -> None:
    law = copula.ColumnLaw([Fraction(1, 4), Fraction(1, 4), Fraction(1, 2)], [4, 4, 4])
    t = law.bin_threshold(1)
    below = float(np.nextafter(t, -np.inf))
    assert law.locate(t)[0] == 2
    assert law.locate(below)[0] == 1
    inner = law.grid_threshold(3, 2)
    assert law.locate(inner) == (3, 2)
    assert law.locate(float(np.nextafter(inner, -np.inf))) == (3, 1)


def _laws() -> List["copula.ColumnLaw"]:
    return [
        copula.ColumnLaw([Fraction(1)], [1]),
        copula.ColumnLaw([Fraction(1)], [1000]),
        copula.ColumnLaw([Fraction(1, 3), Fraction(1, 3), Fraction(1, 3)], [5, 1, 7]),
        copula.ColumnLaw([Fraction(0), Fraction(2, 5), Fraction(0), Fraction(3, 5)], [4, 9, 3, 1]),
        copula.ColumnLaw([Fraction(1, 7)] * 7, [1, 2, 3, 4, 5, 6, 12345]),
        # Shares so small that their thresholds are infinite or collapse to one binary64.
        copula.ColumnLaw(
            [
                Fraction(1, 1 << 1100),
                Fraction(1, 1 << 70),
                Fraction(1, 2) - Fraction(1, 1 << 70),
                Fraction(1, 2) - Fraction(2, 1 << 1100),
                Fraction(1, 1 << 1100),
            ],
            [3, 2, 9, 4, 6],
        ),
    ]


def _probes(law: "copula.ColumnLaw") -> "np.ndarray":
    rng = np.random.default_rng(11)
    own: List[float] = []
    for b in range(1, law.bins + 1):
        own.append(law.bin_threshold(b))
        width = law._widths[b - 1]
        for j in sorted({*range(min(width, 40)), width // 2, width - 1}):
            own.append(law.grid_threshold(b, j))
    finite = np.array([v for v in own if math.isfinite(v)])
    around = np.concatenate([finite, np.nextafter(finite, np.inf), np.nextafter(finite, -np.inf)])
    noise = rng.normal(size=4000) * 2.5
    return np.concatenate([around, noise, [-math.inf, math.inf, 0.0, -0.0, 9.0, -9.0]])


@pytest.mark.parametrize("index", range(6))
def test_the_column_bisection_is_the_row_bisection(index: int) -> None:
    law = _laws()[index]
    x = _probes(law)
    b, lo = law.locate_columns(x)
    for position, value in enumerate(x.tolist()):
        assert law.locate(value) == (int(b[position]), int(lo[position])), value


def test_a_row_is_always_inside_a_bin_that_has_weight() -> None:
    law = copula.ColumnLaw(
        [Fraction(0), Fraction(1, 3), Fraction(0), Fraction(2, 3), Fraction(0)], [2, 3, 4, 5, 6]
    )
    for value in _probes(law).tolist():
        assert law.locate(value)[0] in (2, 4)


@pytest.mark.parametrize("index", range(6))
def test_a_larger_latent_value_never_gives_a_smaller_cell(index: int) -> None:
    law = _laws()[index]
    x = np.sort(_probes(law))
    b, lo = law.locate_columns(x)
    cells: List[Tuple[int, int]] = list(zip(b.tolist(), lo.tolist(), strict=True))
    assert cells == sorted(cells)


def _joint(size: int, r: str, laws: Sequence["copula.ColumnLaw"]) -> Callable[[], None]:
    def run() -> None:
        rows = _factor_of(size, r)
        words = _words(ROWS, size)
        z = [copula.normal_array(w) for w in words]
        x = copula.latent_columns(rows, z)
        located = [laws[i].locate_columns(x[i]) for i in range(size)]
        for row in range(0, ROWS, 3):
            zs = [copula.normal(int(words[d][row])) for d in range(size)]
            xs = copula.latent_row(rows, zs)
            for i in range(size):
                cell = laws[i].locate(xs[i])
                assert cell == (int(located[i][0][row]), int(located[i][1][row]))

    return run


@pytest.mark.parametrize("size,r", [(2, "0.6"), (3, "0.4"), (3, "-0.9"), (5, "0.7")])
def test_a_whole_copula_row_path_equals_the_column_path(size: int, r: str) -> None:
    laws = _laws()
    _joint(size, r, [laws[(i + 2) % len(laws)] for i in range(size)])()


def test_the_copula_correlates_the_cells() -> None:
    rows = _factor_of(2, "0.8")
    words = _words(ROWS, 2)
    x = copula.latent_columns(rows, [copula.normal_array(w) for w in words])
    law = copula.ColumnLaw([Fraction(1, 2), Fraction(1, 2)], [1, 1])
    first = law.locate_columns(x[0])[0]
    second = law.locate_columns(x[1])[0]
    assert abs(float(np.mean(first == 1)) - 0.5) < 0.03
    assert float(np.mean(first == second)) > 0.7


def test_bad_columns_are_refused() -> None:
    with pytest.raises(ValueError):
        copula.ColumnLaw([Fraction(1, 2)], [1])
    with pytest.raises(ValueError):
        copula.ColumnLaw([Fraction(1)], [0])
    with pytest.raises(ValueError):
        copula.ColumnLaw([Fraction(1), Fraction(0)], [1])
    with pytest.raises(ValueError):
        copula.ColumnLaw([Fraction(-1, 2), Fraction(3, 2)], [1, 1])


# ---------------------------------------------------------------------------
# Wide bins: the column path equals the row path, and the grid cache is bounded
# ---------------------------------------------------------------------------


def _latents(count: int = 600) -> "np.ndarray":
    spread = np.linspace(-4.0, 4.0, count)
    return np.concatenate([spread, np.array([-math.inf, math.inf, 0.0])])


@pytest.mark.parametrize(
    "width", [(1 << 61) + 1, (1 << 62) + 12345, 3 << 61, 6 * 10**18, (1 << 63) - 1]
)
def test_the_column_path_equals_the_row_path_for_bins_near_and_above_two_to_the_62(
    width: int,
) -> None:
    # (lo + hi) // 2 on int64 wrapped once a bin held more than 2**62 values, and the column
    # path then located a grid index below zero.
    law = copula.ColumnLaw([Fraction(1, 3), Fraction(2, 3)], [width, 7])
    x = _latents()
    b, lo = law.locate_columns(x)
    expected = [law.locate(float(value)) for value in x.tolist()]
    assert list(zip(b.tolist(), lo.tolist(), strict=True)) == expected
    assert all(index >= 0 for index in lo.tolist())


@pytest.mark.parametrize("width", [1 << 63, (1 << 63) + (1 << 62), (1 << 64) - 1, 1 << 64])
def test_a_bin_wider_than_int64_is_built_and_located_in_python_integers(width: int) -> None:
    # np.array(widths, dtype=np.int64) raised a raw OverflowError for a width of 2**63 or more.
    law = copula.ColumnLaw([Fraction(1, 2), Fraction(1, 2)], [width, 5])
    x = _latents(200)
    b, lo = law.locate_columns(x)
    expected = [law.locate(float(value)) for value in x.tolist()]
    assert list(zip(b.tolist(), lo.tolist(), strict=True)) == expected
    assert all(0 <= index < width for index in lo.tolist())
    assert max(lo.tolist()) > (1 << 62)  # indexes the int64 path could not hold


def test_the_grid_cache_stays_bounded_over_many_rows_and_changes_no_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(copula, "GRID_CACHE_ENTRIES", 64)
    width = 10**12
    bounded = copula.ColumnLaw([Fraction(1, 2), Fraction(1, 2)], [width, width])
    fresh = copula.ColumnLaw([Fraction(1, 2), Fraction(1, 2)], [width, width])
    x = np.random.default_rng(7).normal(size=2000)
    b, lo = bounded.locate_columns(x)
    assert len(bounded._grid) <= 64
    # Row by row, each from a cache that holds nothing, the same cells.
    for position, value in enumerate(x.tolist()):
        fresh._grid.clear()
        assert fresh.locate(value) == (int(b[position]), int(lo[position]))
