# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Two rank correlations of a column pair, with the sensitivity a noisy release is charged for.

ADR 0035 decision 1 and ``docs/spec/PROFILE-SPEC.md`` section 5 describe the statistics. Both are read
over the records whose two values are known, after each value is floored onto the profile's grid,
so a tie is a tie on the grid. ``n`` is the number of those records and ``f`` the statistic's
``rowFloor``, a count of at least 2 chosen without the data, or ``n`` when it has none.
``N = max(n, f)``. A row floor is part of the statistic's definition, not a claim that ``n`` is at
least ``f``: a smaller ``n`` only shrinks the value towards zero.

* ``kendallTau/1``: ``S`` is the sum over every pair of records of
  ``sign(x_i - x_j) * sign(y_i - y_j)``, a tied pair adding zero, and the value is
  ``tau = S / C(N, 2)``.
* ``spearmanRho/1``: ``R`` and ``Q`` are the mid-ranks of the records in each column and
  ``A = sum (R_i - (n + 1) / 2) * (Q_i - (n + 1) / 2)``. ``4A`` is an integer, written ``A4``, and
  the value is ``rho = 12 A / (N (N^2 - 1)) = 3 A4 / (N (N^2 - 1))``. Without ties this is Spearman's
  coefficient. With ties it is deliberately not the Pearson correlation of the mid-ranks: that form
  has no bound of the shape ``C / n`` on what one added row can change, and the charge below needs one.

Under adding or removing one record, with the row floor ``f``, the value of Kendall's moves by at
most ``4 / (f + 1)`` and the value of Spearman's by at most ``9 / (f + 1)``. These are the L1 and
the L2 sensitivity of a release of the one value, and :func:`rank_sensitivity` returns them.
``packages/flux-stats/tests/test_rank_correlation_sensitivity.py`` checks both bounds by brute force
over every small data set, ties included.

The two definitions are kept here and not in :mod:`flux_stats.registry`'s table: they are
formed and read by the profile reader and by a profiler, and no fidelity property compares them.
Everything is integer or :class:`fractions.Fraction` arithmetic, so every platform forms the same
numbers.
"""

from __future__ import annotations

from fractions import Fraction
from math import comb, isqrt
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from flux_stats.errors import FluxError
from flux_stats.fixedpoint import FLX_5004
from flux_stats.registry import Check, GroupStatistic, Parameters, StatisticDefinition

__all__ = [
    "KENDALL_TAU",
    "RANK_DEFINITIONS",
    "RANK_IDENTIFIERS",
    "SPEARMAN_RHO",
    "SPEARMAN_RHO_ID",
    "KENDALL_TAU_ID",
    "kendall_s",
    "kendall_tau",
    "rank_denominator_size",
    "rank_null_variance",
    "rank_reach",
    "rank_sensitivity",
    "rank_value",
    "spearman_a4",
    "spearman_rho",
]

KENDALL_TAU_ID = "kendallTau/1"
SPEARMAN_RHO_ID = "spearmanRho/1"


def rank_denominator_size(n: int, floor: int | None) -> int:
    """``N = max(n, f)``, or ``n`` when the statistic has no row floor."""
    return n if floor is None else max(n, floor)


def kendall_s(pairs: Sequence[Tuple[int, int]]) -> int:
    """The sum over every pair of records of ``sign(x_i - x_j) * sign(y_i - y_j)``, exactly.

    Computed in ``O(n log n)``: the records are taken in groups of equal ``x``, and each is compared
    with every record of a smaller ``x`` through a Fenwick tree over the ranks of ``y``. A pair tied in
    ``x`` or in ``y`` adds zero.
    """
    ranks = {value: position for position, value in enumerate(sorted({y for _, y in pairs}), 1)}
    size = len(ranks)
    tree = [0] * (size + 1)

    def prefix(position: int) -> int:
        total = 0
        while position > 0:
            total += tree[position]
            position -= position & -position
        return total

    ordered = sorted(pairs)
    total = 0
    seen = 0
    start = 0
    while start < len(ordered):
        stop = start
        while stop < len(ordered) and ordered[stop][0] == ordered[start][0]:
            stop += 1
        for _, y in ordered[start:stop]:
            rank = ranks[y]
            below = prefix(rank - 1)
            above = seen - prefix(rank)
            total += below - above
        for _, y in ordered[start:stop]:
            position = ranks[y]
            while position <= size:
                tree[position] += 1
                position += position & -position
        seen += stop - start
        start = stop
    return total


def _doubled_centred_ranks(values: Sequence[int]) -> List[int]:
    """``2 R_i - (n + 1)`` for the mid-ranks ``R_i`` of *values*: an integer for every record.

    A group of ``t`` equal values holding the positions ``p .. p + t - 1`` (counted from 1) has the
    mid-rank ``(2 p + t - 1) / 2``, so twice it is the integer ``2 p + t - 1``.
    """
    n = len(values)
    order = sorted(range(n), key=values.__getitem__)
    found = [0] * n
    start = 0
    while start < n:
        stop = start
        while stop < n and values[order[stop]] == values[order[start]]:
            stop += 1
        centred = (start + 1) + stop - (n + 1)
        for position in range(start, stop):
            found[order[position]] = centred
        start = stop
    return found


def spearman_a4(pairs: Sequence[Tuple[int, int]]) -> int:
    """``4 A``, where ``A`` is the sum of the products of the centred mid-ranks of the two columns."""
    left = _doubled_centred_ranks([x for x, _ in pairs])
    right = _doubled_centred_ranks([y for _, y in pairs])
    return sum(a * b for a, b in zip(left, right, strict=True))


def kendall_tau(s: int, n: int, floor: int | None = None) -> Fraction:
    """``S / C(N, 2)`` with ``N = max(n, f)``; ``N`` is at least 2."""
    size = rank_denominator_size(n, floor)
    if size < 2:
        raise ValueError("a rank correlation needs at least two records or a row floor of two")
    return Fraction(s, comb(size, 2))


def spearman_rho(a4: int, n: int, floor: int | None = None) -> Fraction:
    """``3 A4 / (N (N^2 - 1))`` with ``N = max(n, f)``; ``N`` is at least 2."""
    size = rank_denominator_size(n, floor)
    if size < 2:
        raise ValueError("a rank correlation needs at least two records or a row floor of two")
    return Fraction(3 * a4, size * (size * size - 1))


def rank_sensitivity(identifier: str, floor: int) -> Fraction:
    """The L1 sensitivity, and the L2 sensitivity, of a release of one rank correlation's value.

    One record added or removed moves ``tau`` by at most ``4 / (f + 1)`` and ``rho`` by at most
    ``9 / (f + 1)``. The release is one number, so its L1 and L2 sensitivities are the same.
    """
    if floor < 2:
        raise ValueError("a row floor is a count of at least 2")
    constant = {KENDALL_TAU_ID: 4, SPEARMAN_RHO_ID: 9}.get(identifier)
    if constant is None:
        raise KeyError(f"{identifier!r} is no rank correlation")
    return Fraction(constant, floor + 1)


def rank_null_variance(identifier: str, n: int) -> Fraction:
    """The variance of the plain statistic over *n* records of two independent columns, ties aside.

    ``2 (2 n + 5) / (9 n (n - 1))`` for Kendall's ``tau`` and ``1 / (n - 1)`` for Spearman's
    ``rho``: the spread a run of *n* rows gives a rank correlation whose columns are unrelated,
    which bounds the spread of a related pair's. *n* is at least 2.
    """
    if n < 2:
        raise ValueError("a rank correlation needs at least two records")
    if identifier == KENDALL_TAU_ID:
        return Fraction(2 * (2 * n + 5), 9 * n * (n - 1))
    if identifier == SPEARMAN_RHO_ID:
        return Fraction(1, n - 1)
    raise KeyError(f"{identifier!r} is no rank correlation")


def rank_reach(variance: Fraction) -> Fraction:
    """How far a scalar of variance *variance* reaches at the confidence the widenings are taken at.

    ``sqrt(2 t v)`` with ``t`` the confidence exponent of :mod:`flux_stats.noise` (``sqrt(10 v)``),
    rounded up to 12 decimal places in exact integers: a terminating decimal, never below the root.
    """
    # Read where it is used: the noise module is the profile reader's, and importing it here would
    # put it in the closure of everything that reads a fidelity block.
    from flux_stats.noise import CONFIDENCE_EXPONENT

    if variance < 0:
        raise ValueError("a variance is not negative")
    scale = 10**24
    scaled = -((-2 * CONFIDENCE_EXPONENT * variance.numerator * scale) // variance.denominator)
    root = isqrt(scaled)
    if root * root < scaled:
        root += 1
    return Fraction(root, 10**12)


def rank_value(identifier: str, state: GroupStatistic, floor: int | None) -> Fraction:
    """The value of an exact group of rank correlation *identifier*, as a rational."""
    if identifier == KENDALL_TAU_ID:
        return kendall_tau(state.rank_s, state.k, floor)
    if identifier == SPEARMAN_RHO_ID:
        return spearman_rho(state.rank_a4, state.k, floor)
    raise KeyError(f"{identifier!r} is no rank correlation")


# ---------------------------------------------------------------------------
# The definitions
# ---------------------------------------------------------------------------


def _measure_kendall(group: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    return GroupStatistic(group=group, k=len(values), rank_s=kendall_s(values))


def _measure_spearman(group: str, values: Sequence[Any], parameters: Parameters) -> GroupStatistic:
    return GroupStatistic(group=group, k=len(values), rank_a4=spearman_a4(values))


def _spell_kendall(state: GroupStatistic) -> Dict[str, Any]:
    return {"S": str(state.rank_s)}


def _spell_spearman(state: GroupStatistic) -> Dict[str, Any]:
    return {"A4": str(state.rank_a4)}


def _not_compared(synthetic: GroupStatistic, reference: GroupStatistic, check: Check) -> bool:
    raise FluxError(
        FLX_5004,
        f"{check.subject}: a rank correlation is read and drawn from, and no fidelity property compares it",
    )


KENDALL_TAU = StatisticDefinition(
    identifier=KENDALL_TAU_ID,
    kind="kendallTau",
    summary="k and S, the sum of the products of the signs of the differences of one column pair",
    measure=_measure_kendall,
    spell=_spell_kendall,
    compare=_not_compared,
    on_grid=True,
    cell_table=False,
)

SPEARMAN_RHO = StatisticDefinition(
    identifier=SPEARMAN_RHO_ID,
    kind="spearmanRho",
    summary="k and A4, four times the sum of the products of the centred mid-ranks of one column pair",
    measure=_measure_spearman,
    spell=_spell_spearman,
    compare=_not_compared,
    on_grid=True,
    cell_table=False,
)

#: The definitions a ``columnPairs`` entry may name besides ``correlation/1``, by identifier.
RANK_DEFINITIONS: Mapping[str, StatisticDefinition] = {
    KENDALL_TAU_ID: KENDALL_TAU,
    SPEARMAN_RHO_ID: SPEARMAN_RHO,
}
RANK_IDENTIFIERS = frozenset(RANK_DEFINITIONS)
