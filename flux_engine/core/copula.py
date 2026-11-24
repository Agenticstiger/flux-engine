# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Gaussian copula's pinned arithmetic (ADR 0035 decisions 5 and 6).

Two halves, both pure functions of their arguments.

The plan (``decimal`` at 50 digits, rounding half to even, in a local context that never
touches the caller's): :func:`latent` turns a published rank correlation into a latent
Gaussian correlation, :func:`passes` and :func:`shrink` make the matrix valid, :func:`factor`
takes its Cholesky factor in binary64, :func:`factor_digest` names it, and
:func:`plan_copula` runs the five steps.

The draw (binary64, one IEEE operation per step): :func:`uniform` and :func:`normal` turn a
block word into a standard normal, :func:`latent_row` and :func:`latent_columns` correlate a
row's normals, and :class:`ColumnLaw` turns a latent value into a bin and a grid value. Every
``*_columns`` function makes the operations of its per-row twin one ufunc at a time, with no
``dot``, ``sum`` or fused operation, so the two give the same bits.
"""

from __future__ import annotations

import math
import struct
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from math import lcm
from typing import List, Literal, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.canonical import sha256_hex
from flux_engine.core.rng import _pi, kernel_context

__all__ = [
    "ColumnLaw",
    "CopulaPlan",
    "factor",
    "factor_digest",
    "latent",
    "latent_columns",
    "latent_row",
    "normal",
    "normal_array",
    "passes",
    "plan_copula",
    "shrink",
    "uniform",
    "uniform_array",
]

Measure = Literal["kendall", "spearman"]
Matrix = List[List[Decimal]]
Number = Union[Decimal, str, int]

#: The digits the plan computes in, and the digits the sine series is summed in.
PLAN_DIGITS = 50
SERIES_DIGITS = 60
_SERIES_BOUND = Decimal(10) ** -55
#: A pivot below this fails the test (decision 5, step 3).
_DELTA = Decimal(10) ** -6
_BISECTIONS = 40
#: A factor entry below this magnitude is zero, so no draw product is subnormal.
_FACTOR_FLOOR = math.ldexp(1.0, -900)
_SMALLEST_NORMAL = math.ldexp(1.0, -1022)
_TWO_POW_MINUS_53 = 1.0 / (1 << 53)
_HALF = Fraction(1, 2)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def _sine(x: Decimal) -> Decimal:
    """sin x for ``|x| <= pi/2``: the Taylor series at 60 digits, rounded to 50.

    The first term is always added; a later term is added only when its magnitude is at
    least ``10**-55``, and the series stops at the first that is not.
    """
    with localcontext(kernel_context(SERIES_DIGITS)):
        term = x
        total = x
        n = 1
        square = x * x
        while True:
            term = -term * square / ((2 * n) * (2 * n + 1))
            n += 1
            if abs(term) < _SERIES_BOUND:
                break
            total += term
    with localcontext(kernel_context(PLAN_DIGITS)):
        return +total


def latent(value: Number, measure: Measure) -> Decimal:
    """The latent Gaussian correlation of a rank correlation *value* in ``[-1, 1]``.

    ``sin(pi * tau / 2)`` for ``"kendall"``, ``2 * sin(pi * rho / 6)`` for ``"spearman"``,
    at 50 digits. *value* is a Decimal, an int or its decimal text.
    """
    v = Decimal(value)
    if not v.is_finite() or not -1 <= v <= 1:
        raise ValueError("a rank correlation lies in [-1, 1]")
    if measure not in ("kendall", "spearman"):
        raise ValueError("a rank correlation's measure is kendall or spearman")
    with localcontext(kernel_context(SERIES_DIGITS)):
        pi = _pi(SERIES_DIGITS)
        angle = pi * v / 2 if measure == "kendall" else pi * v / 6
    sine = _sine(angle)
    if measure == "kendall":
        return sine
    with localcontext(kernel_context(PLAN_DIGITS)):
        return 2 * sine


def _ldl(matrix: Sequence[Sequence[Decimal]]) -> Optional[Tuple[Matrix, List[Decimal]]]:
    """The unit lower factor and the pivots of ``matrix``, or None at the first pivot below delta.

    No pivoting, in the order of the columns, at 50 digits.
    """
    size = len(matrix)
    unit: Matrix = [[Decimal(0)] * size for _ in range(size)]
    pivots: List[Decimal] = []
    with localcontext(kernel_context(PLAN_DIGITS)):
        for j in range(size):
            pivot = matrix[j][j]
            for k in range(j):
                pivot -= unit[j][k] * unit[j][k] * pivots[k]
            if pivot < _DELTA:
                return None
            pivots.append(pivot)
            unit[j][j] = Decimal(1)
            for i in range(j + 1, size):
                entry = matrix[i][j]
                for k in range(j):
                    entry -= unit[i][k] * unit[j][k] * pivots[k]
                unit[i][j] = entry / pivot
    return unit, pivots


def passes(matrix: Sequence[Sequence[Decimal]]) -> bool:
    """Whether every pivot of the LDL^T factorisation of *matrix* is at least ``10**-6``."""
    return _ldl(matrix) is not None


def _shrunk(matrix: Sequence[Sequence[Decimal]], alpha: Decimal) -> Matrix:
    """``(1 - alpha) * matrix + alpha * I`` at 50 digits."""
    size = len(matrix)
    with localcontext(kernel_context(PLAN_DIGITS)):
        keep = 1 - alpha
        return [
            [keep * matrix[i][j] + (alpha if i == j else Decimal(0)) for j in range(size)]
            for i in range(size)
        ]


def shrink(matrix: Sequence[Sequence[Decimal]]) -> Decimal:
    """The shrinkage ``alpha``: 0 when *matrix* passes, else 40 bisection steps on ``[0, 1]``.

    The midpoint becomes ``hi`` when ``R(mid)`` passes and ``lo`` when it does not; the
    result is ``hi``. Every midpoint is a dyadic fraction of at most 40 binary places, so it
    is exact at 50 digits.
    """
    if passes(matrix):
        return Decimal(0)
    lo, hi = Decimal(0), Decimal(1)
    with localcontext(kernel_context(PLAN_DIGITS)):
        for _ in range(_BISECTIONS):
            mid = (lo + hi) / 2
            if passes(_shrunk(matrix, mid)):
                hi = mid
            else:
                lo = mid
    return hi


def _binary64(value: Decimal) -> float:
    """*value* rounded once to binary64 (an exact fraction, then a correctly rounded division)."""
    ratio = Fraction(value)
    return ratio.numerator / ratio.denominator


def factor(matrix: Sequence[Sequence[Decimal]], alpha: Decimal) -> List[List[float]]:
    """The Cholesky factor ``L`` of ``R(alpha)`` as binary64 rows of ``i + 1`` entries.

    Each entry is the unit LDL^T factor's entry times the square root of its column's pivot,
    at 50 digits, rounded once to binary64; an entry whose magnitude is below ``2**-900`` is
    0.0. Raises ValueError when ``R(alpha)`` does not pass.
    """
    shrunk = _shrunk(matrix, alpha)
    decomposed = _ldl(shrunk)
    if decomposed is None:
        raise ValueError("the matrix does not pass at this alpha")
    unit, pivots = decomposed
    with localcontext(kernel_context(PLAN_DIGITS)):
        roots = [pivot.sqrt() for pivot in pivots]
        exact = [[unit[i][j] * roots[j] for j in range(i + 1)] for i in range(len(unit))]
    rows = [[_binary64(entry) for entry in row] for row in exact]
    return [[0.0 if abs(entry) < _FACTOR_FLOOR else entry for entry in row] for row in rows]


def factor_digest(rows: Sequence[Sequence[float]]) -> str:
    """The lowercase hex sha256 of ``L``'s binary64 words, row-major.

    The words are those of the lower triangle including the diagonal (row ``i`` holds
    entries ``0 .. i``), each as big-endian IEEE 754 binary64, the byte order the kernel
    reads its constants in. The zeros above the diagonal are not hashed.
    """
    packed = b"".join(struct.pack(">d", entry) for row in rows for entry in row)
    return sha256_hex(packed)


@dataclass(frozen=True)
class CopulaPlan:
    """What the plan decides: ``alpha`` as canonical decimal text, ``L`` and its digest."""

    alpha: str
    factor: Tuple[Tuple[float, ...], ...]
    digest: str


def _alpha_text(alpha: Decimal) -> str:
    """Canonical decimal text: plain notation, no trailing zeros (``0``, ``0.5``, ``1``)."""
    return format(alpha.normalize(), "f")


def _matrix_of(
    columns: Sequence[str], measure: Measure, pairs: Mapping[Tuple[str, str], Number]
) -> Matrix:
    """``R``: a unit diagonal and the latent correlation off it; a pair not listed is 0."""
    index = {name: position for position, name in enumerate(columns)}
    if len(index) != len(columns):
        raise ValueError("a copula's columns are distinct")
    size = len(columns)
    matrix = [[Decimal(int(i == j)) for j in range(size)] for i in range(size)]
    seen = set()
    for (first, second), value in pairs.items():
        if first not in index or second not in index or first == second:
            raise ValueError("a pair names two distinct columns of the copula")
        i, j = index[first], index[second]
        if (min(i, j), max(i, j)) in seen:
            raise ValueError("a pair is listed once")
        seen.add((min(i, j), max(i, j)))
        matrix[i][j] = matrix[j][i] = latent(value, measure)
    return matrix


def plan_copula(
    columns: Sequence[str], measure: Measure, pairs: Mapping[Tuple[str, str], Number]
) -> CopulaPlan:
    """The plan of decision 5: latent matrix, shrinkage, factor and the factor's digest.

    *pairs* maps two column names (in either order, once) to the published rank correlation.
    """
    matrix = _matrix_of(columns, measure, pairs)
    alpha = shrink(matrix)
    rows = factor(matrix, alpha)
    return CopulaPlan(
        alpha=_alpha_text(alpha),
        factor=tuple(tuple(row) for row in rows),
        digest=factor_digest(rows),
    )


# ---------------------------------------------------------------------------
# The draw
# ---------------------------------------------------------------------------


def uniform(word: int) -> float:
    """``(2 * (w0 >> 12) + 1) * 2**-53``: exact, strictly inside (0, 1), symmetric about 1/2."""
    return float(2 * (word >> 12) + 1) * _TWO_POW_MINUS_53


def uniform_array(words: "np.ndarray") -> "np.ndarray":
    """:func:`uniform` of each uint64 word."""
    odd = (words >> np.uint64(12)) * np.uint64(2) + np.uint64(1)
    result: "np.ndarray" = odd.astype(np.float64) * _TWO_POW_MINUS_53
    return result


def normal(word: int) -> float:
    """``z = Phi^-1(u)`` of a block word's uniform (KERNEL-SPEC-2 section 5.2)."""
    return kernel2.ppnd16(uniform(word))


def normal_array(words: "np.ndarray") -> "np.ndarray":
    """:func:`normal` of each uint64 word, by kernel 0.2.0's array form of ``ppnd16``."""
    return kernel2._ppnd16_array(uniform_array(words))


def latent_row(rows: Sequence[Sequence[float]], z: Sequence[float]) -> List[float]:
    """``x = L z``: entry ``i`` is ``((L[i][0] * z_0 + L[i][1] * z_1) + ...) + L[i][i] * z_i``.

    Strictly left to right, one IEEE multiply and one IEEE add per term.
    """
    out: List[float] = []
    for i, row in enumerate(rows):
        total = row[0] * z[0]
        for j in range(1, i + 1):
            total = total + row[j] * z[j]
        out.append(total)
    return out


def latent_columns(
    rows: Sequence[Sequence[float]], z: Sequence["np.ndarray"]
) -> List["np.ndarray"]:
    """:func:`latent_row` over arrays of rows: ``z[j]`` holds the draw ``j`` of every row.

    One ufunc per IEEE operation (``np.multiply`` then ``np.add``, looping over ``j``), so
    each element equals the per-row result bit for bit.
    """
    kernel2.require_subnormals()
    out: List["np.ndarray"] = []
    for i, row in enumerate(rows):
        total = np.multiply(z[0], row[0])
        for j in range(1, i + 1):
            total = np.add(total, np.multiply(z[j], row[j]))
        out.append(total)
    return out


def threshold(numerator: int, denominator: int) -> float:
    """``t(F)`` for the exact share ``F = numerator / denominator`` in ``[0, 1]``.

    ``Phi^-1(binary64(F))`` for ``F <= 1/2`` and ``-Phi^-1(binary64(1 - F))`` above it; a
    binary64 argument below ``2**-1022`` (zero included) gives ``-inf`` or ``+inf``. Python's
    integer true division is correctly rounded, ties to even.
    """
    if 2 * numerator <= denominator:
        argument = numerator / denominator
        return -math.inf if argument < _SMALLEST_NORMAL else kernel2.ppnd16(argument)
    argument = (denominator - numerator) / denominator
    return math.inf if argument < _SMALLEST_NORMAL else -kernel2.ppnd16(argument)


#: The largest grid width a column's bisection holds in an int64 array; a wider bin is located in Python integers.
_INT64_MAX = (1 << 63) - 1

#: How many ``(bin, grid index)`` thresholds a :class:`ColumnLaw` keeps. The bisection of a wide bin
#: probes midpoints that are rarely probed twice, so an unbounded cache grew by about twenty entries a row.
GRID_CACHE_ENTRIES = 1 << 16


class ColumnLaw:
    """One column's bins, and the two bisections that map a latent value to a bin and a grid value.

    *shares* are the bins' exact shares ``p_b`` (they sum to 1) and *widths* the grid values
    ``w_b`` in each bin (at least 1). A bin of share zero is never chosen: the bin bisection
    starts with ``hi`` at the last bin that has weight, which gives the spec's result for
    every finite ``x`` (the thresholds from there on are ``+inf``) and keeps ``x = +inf`` from
    stopping past it. Bins are numbered ``1 .. B`` and grid values ``0 .. w_b - 1``.
    """

    def __init__(self, shares: Sequence[Fraction], widths: Sequence[int]) -> None:
        if len(shares) != len(widths) or not shares:
            raise ValueError("a column has as many widths as bins")
        if any(share < 0 for share in shares) or sum(shares, Fraction(0)) != 1:
            raise ValueError("a column's shares are not negative and sum to 1")
        if any(width < 1 for width in widths):
            raise ValueError("a bin holds at least one grid value")
        self._bins = len(shares)
        self._denominator = lcm(*(Fraction(share).denominator for share in shares))
        self._numerators = [int(Fraction(share) * self._denominator) for share in shares]
        self._widths = [int(width) for width in widths]
        self._before: List[int] = [0]
        for numerator in self._numerators:
            self._before.append(self._before[-1] + numerator)
        positive = [b for b, numerator in enumerate(self._numerators, 1) if numerator > 0]
        self._last = positive[-1]
        self._bin_thresholds = [threshold(c, self._denominator) for c in self._before]
        self._bin_array = np.array(self._bin_thresholds, dtype=np.float64)
        # A bin of 2**63 grid values or more does not fit an int64 array. Such a column is
        # located one row at a time, in Python integers, which :meth:`locate` always does.
        self._wide = any(width > _INT64_MAX for width in self._widths)
        self._widths_array = None if self._wide else np.array(self._widths, dtype=np.int64)
        # The thresholds are a pure function of (bin, grid index), so a cache only saves work:
        # it is bounded, least recently used first out, and no value depends on what it holds.
        self._grid: "OrderedDict[Tuple[int, int], float]" = OrderedDict()

    @property
    def bins(self) -> int:
        """The number of bins, ``B``."""
        return self._bins

    def bin_threshold(self, m: int) -> float:
        """``t(c_m)``, the threshold of the cumulative share after bin ``m`` (``0 <= m <= B``)."""
        return self._bin_thresholds[m]

    def grid_threshold(self, b: int, j: int) -> float:
        """``t(F(b, j))`` with ``F(b, j) = c_(b-1) + p_b * j / w_b``, exact."""
        key = (b, j)
        grid = self._grid
        cached = grid.get(key)
        if cached is None:
            width = self._widths[b - 1]
            numerator = self._before[b - 1] * width + self._numerators[b - 1] * j
            cached = threshold(numerator, self._denominator * width)
            grid[key] = cached
            if len(grid) > GRID_CACHE_ENTRIES:
                grid.popitem(last=False)
        else:
            try:
                grid.move_to_end(key)
            except KeyError:  # pragma: no cover - evicted by another thread between the two calls
                pass
        return cached

    def locate(self, x: float) -> Tuple[int, int]:
        """The bin ``b`` and grid index ``lo`` of latent value *x*, by the two bisections."""
        lo, hi = 0, self._last
        while hi - lo > 1:
            mid = lo + (hi - lo) // 2
            if self._bin_thresholds[mid] <= x:
                lo = mid
            else:
                hi = mid
        b = lo + 1
        lo, hi = 0, self._widths[b - 1]
        while hi - lo > 1:
            mid = lo + (hi - lo) // 2
            if self.grid_threshold(b, mid) <= x:
                lo = mid
            else:
                hi = mid
        return b, lo

    def _grid_thresholds(self, b: "np.ndarray", mid: "np.ndarray") -> "np.ndarray":
        """``t(F(b, mid))`` for each pair, computed once per distinct pair."""
        pairs, inverse = np.unique(np.stack([b, mid], axis=1), axis=0, return_inverse=True)
        values = np.array(
            [self.grid_threshold(int(p), int(m)) for p, m in pairs.tolist()], dtype=np.float64
        )
        result: "np.ndarray" = values[inverse.reshape(-1)]
        return result

    def locate_columns(self, x: "np.ndarray") -> Tuple["np.ndarray", "np.ndarray"]:
        """:meth:`locate` of each element of *x*: the same probes, run for every row at once.

        Each row takes the midpoints its own bisection takes, so the result equals the
        per-row one whatever the thresholds' order.
        """
        kernel2.require_subnormals()
        size = x.shape[0]
        if self._wide:
            # A grid index can pass int64: the row path's integers hold it, and the index
            # array holds Python integers.
            found = [self.locate(float(value)) for value in x.tolist()]
            wide_bins = np.array([each[0] for each in found], dtype=np.int64)
            wide_index = np.empty(size, dtype=object)
            wide_index[:] = [each[1] for each in found]
            return wide_bins, wide_index
        lo = np.zeros(size, dtype=np.int64)
        hi = np.full(size, self._last, dtype=np.int64)
        while True:
            active = hi - lo > 1
            if not active.any():
                break
            # lo + (hi - lo) // 2, not (lo + hi) // 2: the sum wraps once a bin holds more than 2**62 values.
            mid = lo + (hi - lo) // 2
            below = self._bin_array[mid] <= x
            lo = np.where(active & below, mid, lo)
            hi = np.where(active & ~below, mid, hi)
        b = lo + 1
        lo = np.zeros(size, dtype=np.int64)
        assert self._widths_array is not None
        hi = self._widths_array[b - 1]
        while True:
            active = hi - lo > 1
            if not active.any():
                break
            mid = lo + (hi - lo) // 2
            below = np.zeros(size, dtype=bool)
            below[active] = self._grid_thresholds(b[active], mid[active]) <= x[active]
            lo = np.where(active & below, mid, lo)
            hi = np.where(active & ~below, mid, hi)
        return b, lo
