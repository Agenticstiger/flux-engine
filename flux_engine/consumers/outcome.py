# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What counting the consumers' queries on a run says, as scorecard rows (ADR 0028 sections 4 and 7).

The runner counts rows (:mod:`flux_engine.consumers.runner`), :mod:`flux_engine.consumers.rates` turns a
pair of counts into an exact rate and a status, and this module sets each rate against its target and
forms the rows of the scorecard's ``queries`` dimension (:mod:`flux_engine.concordance.queries`).

**Targets.** A calibration query's target is read from the entry of its name in the ``queries`` section
of the profile the bundle pins, and a held-out query's from the held-out profile, a profile document read
with the same reader. A query's rate is the ``true`` count over ``k``, the two counts of its
``levelHistogram/1`` over ``["false", "true"]``; with ``groupBy`` (profile format 1-3-0) each group of the
column has its own. A cell the profile withheld is no target, and says so (``withheld``). The pinned
profile may not name a held-out query and the held-out profile may not name a calibration query
(:func:`require_apart`), so a target of one set never comes from the other's profile.

**Tolerance.** A query's own ``tolerance``, or for a held-out query the registry's
``calibration.heldOutTolerance``. A query with no tolerance is compared with no bound and is in no share.

**Statuses.** ``measured``, ``noRows`` and ``notARate`` are :mod:`flux_engine.consumers.rates`' own;
``noTarget`` and ``withheld`` say the profile gave nothing to compare a measured rate with;
``timedOut``, ``outOfMemory``, ``crashed`` and ``error`` are the runner's; ``versionsDiffer`` says the
bundle recorded another sqlglot version than the one running, so the query is never compared. A query
without a unit has no rate: it is ``measured`` when it returned rows and ``noRows`` when it returned none.

This module imports the standard library, the profile model and the engine's own pure modules, and no SQL
library.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.queries import CALIBRATION, HELD_OUT, GroupRow, QueryRow
from flux_engine.consumers.rates import MEASURED, NO_ROWS, NOT_A_RATE, Rate, group_rates, rate_of
from flux_engine.consumers.verify_errors import FLX_3701, FLX_3702
from flux_engine.core.errors import FluxError
from flux_engine.seam.consumers import ConsumerQuery, ConsumerRegistry
from flux_stats.profile import Profile, StatisticEntry

__all__ = [
    "NO_TARGET",
    "VERSIONS_DIFFER",
    "WITHHELD",
    "QueryTarget",
    "Target",
    "profile_targets",
    "query_rows",
    "require_apart",
]

NO_TARGET = "noTarget"
WITHHELD = "withheld"
VERSIONS_DIFFER = "versionsDiffer"


@dataclass(frozen=True)
class Target:
    """A rate a profile states for a query, or why it states none (``noTarget`` or ``withheld``)."""

    rate: Optional[Fraction]
    reason: str = NO_TARGET


@dataclass(frozen=True)
class QueryTarget:
    """One statistic of a query's entry: ungrouped (``group_by`` is ``None``, one group named ``""``)
    or grouped by a column of the query's unit, one target per level."""

    group_by: Optional[str]
    groups: Mapping[str, Target]


def _target_of(statistic: StatisticEntry) -> Dict[str, Target]:
    out: Dict[str, Target] = {}
    for group in statistic.groups:
        if not group.published or group.suppressed_cells or group.statistic is None:
            out[group.key] = Target(None, WITHHELD)
            continue
        counts = group.statistic.level_counts
        total = sum(counts)
        out[group.key] = (
            Target(Fraction(counts[1], total)) if len(counts) == 2 and total > 0 else Target(None)
        )
    return out


def profile_targets(profile: Profile) -> Mapping[str, Tuple[QueryTarget, ...]]:
    """Each query of the profile's ``queries`` section with the rates its statistics state."""
    return {
        entry.name: tuple(
            QueryTarget(statistic.group_by, _target_of(statistic)) for statistic in entry.statistics
        )
        for entry in profile.queries
    }


def require_apart(
    registry: ConsumerRegistry, pinned: Optional[Profile], held_out: Optional[Profile]
) -> None:
    """Refuse a pinned profile that names a held-out query and a held-out profile that names a
    calibration query, each naming it (``FLX-3701``, ``FLX-3702``).

    A held-out profile handed to a registry that declares no held-out query is refused too: there is
    nothing it could answer.
    """
    held_out_ids = frozenset(query.id for query in registry.queries if query.held_out)
    calibration_ids = frozenset(query.id for query in registry.queries if not query.held_out)
    if pinned is not None:
        for entry in pinned.queries:
            if entry.name in held_out_ids:
                raise FluxError(
                    FLX_3701,
                    f"the pinned profile names the held-out query {entry.name!r}; a held-out "
                    "query's target comes from the held-out profile only",
                )
    if held_out is not None:
        if not held_out_ids:
            raise FluxError(
                FLX_3702,
                "a held-out profile was given and the bundle declares no held-out query "
                "(heldOut: true) for it to answer",
            )
        for entry in held_out.queries:
            if entry.name in calibration_ids:
                raise FluxError(
                    FLX_3702,
                    f"the held-out profile names the calibration query {entry.name!r}; it answers "
                    "held-out queries only",
                )


def _lookup(
    found: Optional[Tuple[QueryTarget, ...]], group_by: Optional[str]
) -> Mapping[str, Target]:
    for statistic in found or ():
        if statistic.group_by == group_by:
            return statistic.groups
    return {}


def _gap(
    rate: Rate, target: Optional[Target], tolerance: Optional[Fraction]
) -> Tuple[str, Optional[Fraction], Optional[bool]]:
    """``(status, gap, met)`` of one rate set against its target and the query's tolerance."""
    if rate.status != MEASURED:
        return rate.status, None, None
    if target is None or target.rate is None:
        return (target.reason if target is not None else NO_TARGET), None, None
    assert rate.value is not None
    gap = rate.value - target.rate
    return MEASURED, gap, (None if tolerance is None else abs(gap) <= tolerance)


def _row(
    query: ConsumerQuery,
    digest: str,
    counted: Any,
    targets: Optional[Tuple[QueryTarget, ...]],
    tolerance: Optional[Fraction],
) -> QueryRow:
    which = HELD_OUT if query.held_out else CALIBRATION
    if counted is None:
        return QueryRow(
            query.id, which, digest, None, None, None, None, None, tolerance, VERSIONS_DIFFER, None
        )
    if counted.status != "counted":
        return QueryRow(
            query.id,
            which,
            digest,
            None,
            None,
            None,
            None,
            None,
            tolerance,
            counted.status,
            None,
            counted.reason,
        )
    rows, unit_rows = counted.rows, counted.unit_rows
    if query.unit is None:
        status = MEASURED if rows else NO_ROWS
        return QueryRow(
            query.id, which, digest, rows, None, None, None, None, tolerance, status, None
        )
    whole = rate_of(rows, unit_rows)
    if query.group_by is None:
        found = _lookup(targets, None).get("")
        status, gap, met = _gap(whole, found, tolerance)
        return QueryRow(
            query.id,
            which,
            digest,
            rows,
            unit_rows,
            whole.value,
            found.rate if found is not None else None,
            gap,
            tolerance,
            status,
            met,
        )
    return _grouped_row(query, which, digest, counted, targets, tolerance, whole)


def _grouped_row(
    query: ConsumerQuery,
    which: str,
    digest: str,
    counted: Any,
    targets: Optional[Tuple[QueryTarget, ...]],
    tolerance: Optional[Fraction],
    whole: Rate,
) -> QueryRow:
    by_level = _lookup(targets, query.group_by)
    groups = []
    for ordinal, entry in enumerate(
        group_rates(counted.row_groups or (), counted.unit_groups or ())
    ):
        found = by_level.get(entry.key) if entry.key is not None else None
        status, gap, met = _gap(entry.rate, found, tolerance)
        groups.append(
            GroupRow(
                group=ordinal,
                null_group=entry.key is None,
                rows=entry.rate.rows,
                unit_rows=entry.rate.unit_rows,
                rate=entry.rate.value,
                target=found.rate if found is not None else None,
                gap=gap,
                status=status,
                met=met,
            )
        )
    statuses = [group.status for group in groups]
    if not groups:
        status = whole.status
    elif NOT_A_RATE in statuses:
        status = NOT_A_RATE
    elif all(item in (NO_TARGET, WITHHELD) for item in statuses):
        status = WITHHELD if WITHHELD in statuses else NO_TARGET
    else:
        status = MEASURED
    decided = [group.met for group in groups if group.met is not None]
    return QueryRow(
        query.id,
        which,
        digest,
        counted.rows,
        counted.unit_rows,
        whole.value,
        None,
        None,
        tolerance,
        status,
        all(decided) if decided else None,
        groups=tuple(groups),
    )


def query_rows(
    registry: ConsumerRegistry,
    digests: Mapping[str, str],
    counts: Optional[Sequence[Any]],
    pinned: Optional[Mapping[str, Tuple[QueryTarget, ...]]],
    held_out: Optional[Mapping[str, Tuple[QueryTarget, ...]]],
) -> Tuple[QueryRow, ...]:
    """One row per query of *registry*, in its order.

    *digests* maps each query's id to the digest of its canonical text, and *counts* holds one
    :class:`~flux_engine.consumers.runner.QueryCounts` per query in the same order, or ``None`` when
    the registry's recorded versions differ from the running ones and nothing was counted.
    """
    rows = []
    for position, query in enumerate(registry.queries):
        source = held_out if query.held_out else pinned
        tolerance = query.tolerance
        if tolerance is None and query.held_out:
            tolerance = registry.calibration.held_out_tolerance
        rows.append(
            _row(
                query,
                digests[query.id],
                counts[position] if counts is not None else None,
                source.get(query.id) if source is not None else None,
                tolerance,
            )
        )
    return tuple(rows)
