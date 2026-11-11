# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SDMetrics' fidelity metrics, computed from the profile S alone.

SDMetrics 0.33.0 (MIT, https://github.com/sdv-dev/SDMetrics) defines the
single-column and column-pair fidelity metrics a synthetic-data practitioner
reads first. It computes them from DataFrames of records. The fidelity check
never holds records on the committed side -- only S
(``flux_engine.concordance.boundary.statistic``: bucket counts, moment sums,
the sorted support, ``n`` and ``k``) -- so the definitions are borrowed and
re-implemented here on those aggregates. No SDMetrics code is copied, and
SDMetrics is not a dependency.

======================  ==================================================
SDMetrics metric        Computed from S as
======================  ==================================================
TVComplement            ``1 - TVD``, ``TVD = 1/2 * sum |c_a/k_a - c_b/k_b|``
                        over a histogram property's bins
KSComplement            ``1 - max_e |F_a(e) - F_b(e)|`` over the declared
                        edges, ``F`` from cumulative bin counts
CorrelationSimilarity   ``1 - |r_a - r_b| / 2``, Pearson's ``r`` from
                        ``k, Sx, Sy, Sxx, Syy, Sxy``
ContingencySimilarity   ``1 - TVD`` over the (group, bin) table of a
                        grouped histogram property
======================  ==================================================

KSComplement is the one that is not equal to its record-level definition:
S keeps counts per bin, so the supremum of the CDF difference can only be
taken at the declared edges. That is a lower bound on the record-level KS
distance, so the aggregate KSComplement is an upper bound on the record-level
one, and it is reported as such.

Exact where the definition allows it: TV, KS and contingency are
``Fraction``\\ s; Pearson's ``r`` needs one square root and is evaluated in
``decimal`` at 50 digits.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Dict, Optional, Sequence, Tuple

from flux_engine.concordance.boundary.statistic import (
    UNGROUPED,
    GroupStatistic,
    PropertyStatistic,
)
from flux_engine.concordance.predicate import PropertyKind

__all__ = [
    "contingency_similarity",
    "correlation_similarity",
    "ks_complement",
    "pearson_r",
    "tv_complement",
]


def _group(statistic: PropertyStatistic, key: str) -> GroupStatistic:
    found = statistic.group(key)
    if found is None:
        raise KeyError(f"{statistic.property_id}: no group {key!r}")
    return found


def _require(statistic: PropertyStatistic, kind: PropertyKind) -> None:
    if statistic.kind is not kind:
        raise ValueError(
            f"{statistic.property_id} is a {statistic.kind.value} property, not {kind.value}"
        )


def _frequencies(group: GroupStatistic) -> Tuple[Fraction, ...]:
    if group.k <= 0:
        raise ValueError("a group with no records has no frequencies")
    return tuple(Fraction(count, group.k) for count in group.bin_counts)


def _tvd(a: Sequence[Fraction], b: Sequence[Fraction]) -> Fraction:
    if len(a) != len(b):
        raise ValueError("the two sides bin differently")
    return Fraction(1, 2) * sum((abs(x - y) for x, y in zip(a, b, strict=True)), Fraction(0))


def tv_complement(a: PropertyStatistic, b: PropertyStatistic, group: str = UNGROUPED) -> Fraction:
    """SDMetrics TVComplement of one histogram property, from its bin counts."""
    _require(a, PropertyKind.MARGINAL_HISTOGRAM)
    _require(b, PropertyKind.MARGINAL_HISTOGRAM)
    return 1 - _tvd(_frequencies(_group(a, group)), _frequencies(_group(b, group)))


def ks_complement(a: PropertyStatistic, b: PropertyStatistic, group: str = UNGROUPED) -> Fraction:
    """SDMetrics KSComplement, with the supremum taken over the declared edges.

    ``F(e)`` at an interior edge is the share of records strictly below it
    (bins are half-open), so it is the cumulative count of the bins before
    the edge. At the outer edges both CDFs are 0 and 1 and contribute
    nothing, so only interior edges are compared.
    """
    _require(a, PropertyKind.MARGINAL_HISTOGRAM)
    _require(b, PropertyKind.MARGINAL_HISTOGRAM)
    left, right = _frequencies(_group(a, group)), _frequencies(_group(b, group))
    if len(left) != len(right):
        raise ValueError("the two sides bin differently")
    cumulative_a = cumulative_b = Fraction(0)
    distance = Fraction(0)
    for x, y in zip(left[:-1], right[:-1], strict=True):
        cumulative_a += x
        cumulative_b += y
        distance = max(distance, abs(cumulative_a - cumulative_b))
    return 1 - distance


def pearson_r(group: GroupStatistic) -> Optional[Decimal]:
    """Pearson's ``r`` from one group's moment sums, or ``None`` when a variance is zero.

    ``r = (k*Sxy - Sx*Sy) / sqrt((k*Sxx - Sx**2) * (k*Syy - Sy**2))``. Every
    term is an integer count of grid units, and ``r`` is invariant to the
    grid's affine encoding, so it is the record-level ``r`` of the encoded
    values.
    """
    k = group.k
    numerator = k * group.sum_xy - group.sum_x * group.sum_y
    qx = k * group.sum_xx - group.sum_x * group.sum_x
    qy = k * group.sum_yy - group.sum_y * group.sum_y
    if qx <= 0 or qy <= 0:
        return None
    with localcontext() as context:
        context.prec = 50
        r = Decimal(numerator) / (Decimal(qx) * Decimal(qy)).sqrt()
        # Cauchy-Schwarz bounds |r| by 1 exactly; the one rounded square
        # root can overshoot it in the last digit for collinear data.
        return max(Decimal(-1), min(Decimal(1), r))


def correlation_similarity(
    a: PropertyStatistic, b: PropertyStatistic, group: str = UNGROUPED
) -> Decimal:
    """SDMetrics CorrelationSimilarity (Pearson) of one correlation property."""
    _require(a, PropertyKind.CORRELATION)
    _require(b, PropertyKind.CORRELATION)
    r_a, r_b = pearson_r(_group(a, group)), pearson_r(_group(b, group))
    if r_a is None or r_b is None:
        raise ValueError("a side has zero variance; Pearson's r is undefined")
    with localcontext() as context:
        context.prec = 50
        return 1 - abs(r_a - r_b) / 2


def contingency_similarity(a: PropertyStatistic, b: PropertyStatistic) -> Fraction:
    """SDMetrics ContingencySimilarity of (group, bin), from a grouped histogram property.

    Each side's joint frequency of ``(group g, bin i)`` is ``c_{g,i} / sum_g k_g``.
    A group present on one side only contributes its whole mass, as a group
    absent from a contingency table does.
    """
    _require(a, PropertyKind.MARGINAL_HISTOGRAM)
    _require(b, PropertyKind.MARGINAL_HISTOGRAM)

    def joint(statistic: PropertyStatistic) -> Dict[Tuple[str, int], Fraction]:
        total = sum(group.k for group in statistic.groups)
        if total <= 0:
            raise ValueError(f"{statistic.property_id} holds no records")
        return {
            (group.group, index): Fraction(count, total)
            for group in statistic.groups
            for index, count in enumerate(group.bin_counts)
        }

    left, right = joint(a), joint(b)
    cells = sorted(set(left) | set(right))
    distance = Fraction(1, 2) * sum(
        (abs(left.get(cell, Fraction(0)) - right.get(cell, Fraction(0))) for cell in cells),
        Fraction(0),
    )
    return 1 - distance
