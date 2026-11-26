# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a candidate scores: the sum of the squared gaps to the pinned profile's targets (ADR 0028 section 8).

A calibration query's row (:class:`~flux_engine.concordance.queries.QueryRow`) carries its gap, the rate
less the target, as an exact fraction. The objective adds the squares, so it is an exact fraction as well,
and two candidates that tie are equal and not close. This module imports the standard library and the
engine's row model, and no SQL library.
"""

from __future__ import annotations

from fractions import Fraction
from typing import AbstractSet, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from flux_engine.calibrate.search import Evaluation
from flux_engine.concordance.queries import QueryRow
from flux_engine.consumers.outcome import QueryTarget
from flux_engine.seam.consumers import ConsumerQuery

__all__ = ["score", "stated_queries"]

#: The statuses that say the profile stated no target, so that no gap is owed.
_NO_TARGET_STATUSES = frozenset({"noTarget", "withheld"})


def stated_queries(
    queries: Iterable[ConsumerQuery], targets: Mapping[str, Tuple[QueryTarget, ...]]
) -> FrozenSet[str]:
    """The ids of *queries* the profile states a rate for: a statistic grouped as the query is, with a rate.

    A query with ``groupBy`` is compared with a statistic grouped by the same column, so a profile that
    states only its rate as a whole states none for it.
    """
    return frozenset(
        query.id
        for query in queries
        if any(
            target.rate is not None
            for statistic in targets.get(query.id, ())
            if statistic.group_by == query.group_by
            for target in statistic.groups.values()
        )
    )


def query_weights(targets: Mapping[str, Tuple[QueryTarget, ...]]) -> Mapping[str, Fraction]:
    """The weight of each query a noisy release states a rate for (profile format 1-5-0).

    A query's squared gaps weigh the inverse of its rate's variance: the mean over its groups for a
    grouped one. The weights are scaled so the least noisy query weighs 1, as a query with an exact
    rate does, and a query with no noisy rate is not listed (its weight is 1).
    """
    variances: Dict[str, Fraction] = {}
    for name, statistics in targets.items():
        found = [
            target.variance
            for statistic in statistics
            for target in statistic.groups.values()
            if target.rate is not None and target.variance is not None and target.variance > 0
        ]
        if found:
            variances[name] = sum(found, Fraction(0)) / len(found)
    if not variances:
        return {}
    least = min(variances.values())
    return {name: least / variance for name, variance in variances.items()}


def inside_noise(targets: Mapping[str, Tuple[QueryTarget, ...]]) -> FrozenSet[str]:
    """The queries a noisy release states only rates inside the noise for (profile format 1-5-0)."""
    return frozenset(
        name
        for name, statistics in targets.items()
        if any(
            target.inside_noise for statistic in statistics for target in statistic.groups.values()
        )
        and not any(
            target.rate is not None
            for statistic in statistics
            for target in statistic.groups.values()
        )
    )


def score(
    rows: Sequence[QueryRow],
    stated: AbstractSet[str],
    weights: Optional[Mapping[str, Fraction]] = None,
) -> Evaluation:
    """The objective of one candidate and the gap of each calibration query.

    A measured query adds the square of its gap (each group's for a query with ``groupBy``). A query
    the profile states a rate for (*stated*) and that was not measured (it timed out, ran out of memory,
    returned more rows than its unit, or its unit is empty) adds 1, the largest square a rate gap can
    have, so a candidate that cannot be counted never beats one that can. A query with no target adds
    nothing. A query's reported gap is its own, or for a grouped query the group gap of the largest size.
    A query in *weights* (a noisy release's, :func:`query_weights`) weighs its squared gaps by its
    weight; every other query weighs 1.
    """
    total = Fraction(0)
    gaps: List[Tuple[str, Optional[Fraction]]] = []
    for row in rows:
        diffs = [group.gap for group in row.groups if group.gap is not None]
        if row.gap is not None:
            diffs.append(row.gap)
        weight = (weights or {}).get(row.name, Fraction(1))
        total += weight * sum((gap * gap for gap in diffs), Fraction(0))
        if not diffs and row.status not in _NO_TARGET_STATUSES and row.name in stated:
            total += 1
        gaps.append((row.name, max(diffs, key=abs) if diffs else None))
    return Evaluation(objective=total, gaps=tuple(gaps))
