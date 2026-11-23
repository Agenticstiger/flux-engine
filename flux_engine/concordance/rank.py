# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A ``rankCorrelation`` property scored against a profile (ADR 0035 decision 7).

The property names two columns and a measure, ``kendall`` or ``spearman``. Its reference is the
profile's value of the pair, exact or the clamped noisy estimate; the run's side is the plain
statistic over the run's rows, with no row floor, so ``N`` is the number of rows that hold both
values. Both are formed on the profile's grid, a tie being a tie on the grid, so the block's
``fractionalBits`` must equal the profile's.

* **The score** is ``1 - |t - t'| / 2``, exact, rounded down to 12 places as a correlation's is.
* **Within** when ``|t - t'| <= margin + w_noise + w_sampling``. ``w_noise`` is the reach of the
  reference's noise, ``sqrt(10 v)`` for its variance ``v`` (:func:`flux_stats.rank_correlation.rank_reach`),
  and ``0`` for an exact reference. ``w_sampling`` is the same reach of the statistic's variance
  over ``n`` rows of independent columns, the spread a run of that size gives, so a small run
  is not held to a margin its own size cannot meet.
* **Too noisy to tell** when ``margin + w_noise + w_sampling`` is 2 or more: the statistic lies in
  ``[-1, 1]``, so every run is within and the row says so.

Forming the run's side sorts the pair's values over the whole run, ``O(n log n)``, and nothing
here is an estimate: ``S`` and ``A4`` are exact integers. A committed slice and a sample hold
only part of a run, and neither adds up across blocks, so neither scores the kind.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.predicate import ColumnDeclaration, FidelityBlock, PropertyDeclaration
from flux_stats.fidelity import definition_for_property, measure_group
from flux_stats.fixedpoint import require_sample
from flux_stats.rank_correlation import (
    rank_null_variance,
    rank_reach,
    rank_value,
)
from flux_stats.reading import Row

__all__ = [
    "RANK_PLACES",
    "RankComparison",
    "compare_rank",
    "rank_score",
    "run_rank_value",
]

#: A rank correlation's score is rounded down to this many decimal places, as a correlation's is
#: (:data:`flux_engine.concordance.similarity.CORRELATION_PLACES`).
RANK_PLACES = 12

_ZERO = Fraction(0)
_ONE = Fraction(1)
_TWO = Fraction(2)


@dataclass(frozen=True)
class RankComparison:
    """One rank correlation's comparison: both values, the distance, the widenings and the verdict.

    ``noise`` is ``None`` for an exact reference. ``reach`` is the margin and both widenings, the
    most the two values may differ by and still be within; ``too_noisy`` says it is 2 or more.
    """

    measure: str
    records: int
    run: Fraction
    reference: Fraction
    distance: Fraction
    noise: Optional[Fraction]
    sampling: Fraction
    reach: Fraction
    within: bool
    too_noisy: bool
    score: Fraction


def _identifier(declaration: PropertyDeclaration) -> str:
    return definition_for_property(declaration).identifier


def run_rank_value(
    rows: Sequence[Row],
    declaration: PropertyDeclaration,
    columns: Mapping[str, ColumnDeclaration],
    block: FidelityBlock,
    *,
    subject: str,
) -> Tuple[Fraction, int]:
    """The plain rank correlation of the run's rows of a pair, and the rows it counts.

    Refuses (``FLX-5005``) a pair with fewer than two rows that hold both values, a value outside
    its declared range and a null under ``fail``, as every property refuses them.
    """
    group = measure_group(rows, range(len(rows)), "", declaration, columns, block.grid)
    require_sample(group.k, minimum=2, subject=f"{subject}: {declaration.property_id}")
    return rank_value(_identifier(declaration), group, None), group.k


def rank_score(distance: Fraction) -> Fraction:
    """``1 - distance / 2``, rounded down to :data:`RANK_PLACES` places; ``distance`` is at most 2."""
    places = 10**RANK_PLACES
    return Fraction(math.floor((_ONE - distance / _TWO) * places), places)


def compare_rank(
    declaration: PropertyDeclaration,
    margin: Fraction,
    *,
    run: Fraction,
    records: int,
    reference: Fraction,
    variance: Optional[Fraction],
) -> RankComparison:
    """Compare the run's value with the profile's, for a reference of *variance* (``None`` if exact)."""
    identifier = _identifier(declaration)
    distance = abs(run - reference)
    noise = rank_reach(variance) if variance is not None else None
    sampling = rank_reach(rank_null_variance(identifier, records))
    reach = margin + (noise or _ZERO) + sampling
    return RankComparison(
        measure=declaration.measure or "",
        records=records,
        run=run,
        reference=reference,
        distance=distance,
        noise=noise,
        sampling=sampling,
        reach=reach,
        within=distance <= reach,
        too_noisy=reach >= _TWO,
        score=rank_score(distance),
    )
