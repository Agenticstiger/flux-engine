# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Gaussian copula's pinned arithmetic (ADR 0035 decisions 5 and 6).

The plan (``decimal`` at 50 digits, rounding half to even, in a local context that never
touches the caller's): :func:`latent` turns a published rank correlation into a latent
Gaussian correlation, :func:`passes` and :func:`shrink` make the matrix valid, :func:`factor`
takes its Cholesky factor in binary64, :func:`factor_digest` names it, and
:func:`plan_copula` runs the five steps.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import List, Literal, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.canonical import sha256_hex
from flux_engine.core.rng import _pi, kernel_context

__all__ = ["CopulaPlan", "factor", "factor_digest", "latent", "passes", "plan_copula", "shrink"]

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
