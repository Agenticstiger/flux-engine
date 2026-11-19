# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How close one synthetic group is to its reference, as a score in ``[0, 1]``.

The scorecard (docs/spec/SCORECARD-SPEC.md) gives every scored property a
similarity beside its outcome. The outcome is still what gates: the registered
definition's comparison under the compiled margin decides within or outside.
The similarity says how close the two sides are, on one scale for every kind,
so that properties add up into a score per table, per dimension and per run.

There is one measure per registered statistic, keyed by its identifier, and
each is formed from the integer quantities the comparison already reads. No
record is touched and nothing divides in floating point:

* ``marginalHistogram/1`` and ``levelHistogram/1``: one minus the total
  variation distance over the compared cells. SDMetrics calls this
  TVComplement (sdv-dev/SDMetrics 0.33.0, MIT), and the accuracy of
  mostlyai-qa (1.10.11, Apache-2.0) is the same quantity, ``1 - L1/2``.
* ``mean/1`` and ``quantile/1``: one minus the absolute difference over the
  width of the declared range, and never below zero. This is the shape of
  SDMetrics' StatisticSimilarity, with the declared range in place of the
  real column's own range, which a profile need not publish.
* ``quantile/2``: the same, with the distance in grid steps from the
  synthetic witness to the reference bin in place of the difference of two
  witnesses, ``0`` inside the bin (ADR 0012).
* ``support/1``: the share of the synthetic group's records whose value is a
  member of the reference support (SDMetrics' CategoryAdherence). A group with
  no record holds no value outside the support, so it scores 1, as its
  comparison is within.
* ``correlation/1``: one minus half the absolute difference of the two
  correlations (SDMetrics' CorrelationSimilarity). A correlation is a ratio
  over a square root, so this score is an exact lower bound: each square root
  is bracketed by :func:`math.isqrt` at 64 bits, the difference is bounded
  above over both brackets, and the result is rounded down to
  :data:`CORRELATION_PLACES` decimal places. It is the same number on every
  platform, and never above the true score.

SDMetrics is a pattern here, never a dependency: it requires ``copulas``,
which is under the Business Source License 1.1.
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Callable, Dict, Optional, Sequence, Tuple

from flux_engine.concordance.errors import FLX_5037
from flux_engine.core.errors import FluxError
from flux_stats import GroupStatistic, StatisticDefinition
from flux_stats.definitions import (
    CORRELATION,
    LEVEL_HISTOGRAM,
    MARGINAL_HISTOGRAM,
    MEAN,
    QUANTILE,
    QUANTILE_BINNED,
    SUPPORT,
    non_member_records,
)
from flux_stats.fixedpoint import FLX_5005, SecondMoments, distance_to_bin

__all__ = [
    "CORRELATION_PLACES",
    "similarity",
    "similarity_identifiers",
    "total_variation",
]

#: The bits each square root is bracketed at, as
#: :func:`flux_stats.fixedpoint._sqrt_lower_bound` takes them.
_ROOT_BITS = 64

#: A correlation's score is rounded down to this many decimal places, so it is
#: written as a terminating decimal. The bracket around each root is tighter
#: than ``2**-60`` of the correlation, far inside the last place kept.
CORRELATION_PLACES = 12

_ONE = Fraction(1)
_ZERO = Fraction(0)


def total_variation(
    counts_a: Sequence[int], count_a: int, counts_b: Sequence[int], count_b: int
) -> Fraction:
    """``(1/2) * sum |a_i/count_a - b_i/count_b|`` over the cells, exactly.

    The same sum :func:`flux_stats.fixedpoint.total_variation_within`
    compares with a margin, formed here as a rational instead.
    """
    if count_a <= 0 or count_b <= 0:
        raise FluxError(FLX_5005, "a total variation needs a record on each side")
    spread = sum(abs(a * count_b - b * count_a) for a, b in zip(counts_a, counts_b, strict=True))
    return Fraction(spread, 2 * count_a * count_b)


def _closeness(distance: Fraction) -> Fraction:
    """``1 - distance``, and never below zero."""
    return max(_ZERO, _ONE - distance)


def _over_width(distance: Fraction, width_units: Optional[int]) -> Fraction:
    """The closeness of two values *distance* grid units apart, over the declared width."""
    if width_units is None or width_units < 0:
        raise FluxError(FLX_5037, "a mean or a quantile is scored over its declared range")
    if width_units == 0:
        # A range of one grid point: the two sides agree or they do not.
        return _ONE if distance == 0 else _ZERO
    return _closeness(distance / width_units)


def _cells(
    statistic: StatisticDefinition, synthetic: GroupStatistic, reference: GroupStatistic
) -> Fraction:
    kind = statistic.kind
    return _ONE - total_variation(
        synthetic.cell_counts(kind), synthetic.k, reference.cell_counts(kind), reference.k
    )


def _mean(synthetic: GroupStatistic, reference: GroupStatistic, width: Optional[int]) -> Fraction:
    if synthetic.k <= 0 or reference.k <= 0:
        raise FluxError(FLX_5005, "a mean needs a record on each side")
    difference = Fraction(
        abs(synthetic.sum_x * reference.k - reference.sum_x * synthetic.k),
        synthetic.k * reference.k,
    )
    return _over_width(difference, width)


def _quantile(
    synthetic: GroupStatistic, reference: GroupStatistic, width: Optional[int]
) -> Fraction:
    if synthetic.quantile_witness is None or reference.quantile_witness is None:
        raise FluxError(FLX_5005, "a quantile needs a witness on each side")
    return _over_width(
        Fraction(abs(synthetic.quantile_witness - reference.quantile_witness)), width
    )


def _quantile_binned(
    synthetic: GroupStatistic,
    reference: GroupStatistic,
    width: Optional[int],
    edges: Tuple[int, ...],
) -> Fraction:
    if synthetic.quantile_witness is None or reference.quantile_bin is None:
        raise FluxError(FLX_5005, "a quantile with bins needs a witness and a published bin")
    return _over_width(
        Fraction(distance_to_bin(synthetic.quantile_witness, edges, reference.quantile_bin)),
        width,
    )


def _support(synthetic: GroupStatistic, reference: GroupStatistic) -> Fraction:
    if synthetic.k == 0:
        return _ONE
    return Fraction(synthetic.k - non_member_records(synthetic, reference), synthetic.k)


def _correlation_bracket(moments: SecondMoments) -> Tuple[Fraction, Fraction]:
    """Exact rationals ``low <= r <= high`` around one sample correlation ``r``.

    ``r = N / sqrt(Qx * Qy)``. The root of the integer ``R = Qx * Qy`` lies in
    ``[s, s + 2**-64]`` with ``s = isqrt(R * 4**64) / 2**64``, and ``R >= 1``
    once both variance floors hold, so ``s >= 1`` and nothing divides by
    zero. The bracket is clipped to ``[-1, 1]``, where ``r`` always lies.
    """
    radicand = moments.qx * moments.qy
    if radicand <= 0:
        raise FluxError(FLX_5005, "a correlation needs variance in both columns")
    scale = 1 << _ROOT_BITS
    root = math.isqrt(radicand * scale * scale)
    low_root = Fraction(root, scale)
    high_root = Fraction(root + 1, scale)
    numerator = Fraction(moments.numerator)
    if numerator >= 0:
        low, high = numerator / high_root, numerator / low_root
    else:
        low, high = numerator / low_root, numerator / high_root
    return max(low, -_ONE), min(high, _ONE)


def _same_correlation(a: SecondMoments, b: SecondMoments) -> bool:
    """Whether ``r_a == r_b`` exactly: same sign, and ``Na**2 * Rb == Nb**2 * Ra``.

    Equal correlations score exactly 1. Without this the two brackets,
    each a hair wide, would round an exact agreement down below 1.
    """
    na, nb = a.numerator, b.numerator
    if (na > 0) != (nb > 0) or (na < 0) != (nb < 0):
        return False
    return na * na * (b.qx * b.qy) == nb * nb * (a.qx * a.qy)


def _correlation(synthetic: GroupStatistic, reference: GroupStatistic) -> Fraction:
    if _same_correlation(synthetic.moments, reference.moments):
        return _ONE
    low_a, high_a = _correlation_bracket(synthetic.moments)
    low_b, high_b = _correlation_bracket(reference.moments)
    # |x - y| over the box [low_a, high_a] x [low_b, high_b] is largest at a
    # corner, so this bounds |r_a - r_b| from above whatever the two roots are.
    gap = max(high_a - low_b, high_b - low_a)
    bound = _closeness(gap / 2)
    places = 10**CORRELATION_PLACES
    return Fraction(math.floor(bound * places), places)


_Scorer = Callable[
    [StatisticDefinition, GroupStatistic, GroupStatistic, Optional[int], Tuple[int, ...]],
    Fraction,
]

#: One measure per registered definition, keyed by the definition itself so
#: its identifier is spelled once, in the registry. A new definition
#: (``mean/2``) needs an entry of its own, and the similarity tests fail
#: until every registered statistic has one.
_SCORERS: Dict[str, _Scorer] = {
    MEAN.identifier: lambda statistic, synthetic, reference, width, edges: _mean(
        synthetic, reference, width
    ),
    MARGINAL_HISTOGRAM.identifier: lambda statistic, synthetic, reference, width, edges: _cells(
        statistic, synthetic, reference
    ),
    LEVEL_HISTOGRAM.identifier: lambda statistic, synthetic, reference, width, edges: _cells(
        statistic, synthetic, reference
    ),
    SUPPORT.identifier: lambda statistic, synthetic, reference, width, edges: _support(
        synthetic, reference
    ),
    CORRELATION.identifier: lambda statistic, synthetic, reference, width, edges: _correlation(
        synthetic, reference
    ),
    QUANTILE.identifier: lambda statistic, synthetic, reference, width, edges: _quantile(
        synthetic, reference, width
    ),
    QUANTILE_BINNED.identifier: (
        lambda statistic, synthetic, reference, width, edges: _quantile_binned(
            synthetic, reference, width, edges
        )
    ),
}


def similarity_identifiers() -> Tuple[str, ...]:
    """The statistic identifiers that have a similarity, sorted."""
    return tuple(sorted(_SCORERS))


def similarity(
    statistic: StatisticDefinition,
    synthetic: GroupStatistic,
    reference: GroupStatistic,
    *,
    width_units: Optional[int] = None,
    edges: Tuple[int, ...] = (),
) -> Fraction:
    """The similarity of one synthetic group to its reference, exactly, in ``[0, 1]``.

    *synthetic* and *reference* are the two groups the comparison read: for a
    count table, its published cells, each side renormalised to them.
    *width_units* is the declared range's width on the grid, which a mean and
    a quantile are scored over; the other kinds ignore it. *edges* are the
    encoded bin edges a ``quantile/2`` is scored against; the others ignore
    them.
    """
    scorer = _SCORERS.get(statistic.identifier)
    if scorer is None:
        raise FluxError(FLX_5037, f"{statistic.identifier} has no similarity")
    return scorer(statistic, synthetic, reference, width_units, edges)
