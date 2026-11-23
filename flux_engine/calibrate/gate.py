# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle is called calibrated only when held-out error holds (ADR 0028 section 9).

The fit reads the pinned profile only. Once the calibration patch is written and its digest taken, the
held-out profile is opened, the calibrated values are run at the calibration's scale and the held-out
queries are counted. Each held-out query that has a target is set against its own tolerance, or else the
block's ``heldOutTolerance``:

* a query is **within** when it was measured, it has a tolerance, and its gap (each group's, for a query
  with ``groupBy``) is no larger than the tolerance in size;
* a calibration is **calibrated** only when at least one held-out query has a target and every held-out
  query that has one is within. A fit with no held-out target is never called calibrated, and a held-out
  query that could not be counted, or that has no tolerance to be held to, is a miss.

This module imports the standard library and the engine's row model, and no SQL library.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil
from typing import AbstractSet, List, Optional, Sequence, Tuple

from flux_engine.calibrate.bundle import HeldOutEvidence
from flux_engine.concordance.queries import QueryRow
from flux_stats.spelling import render_exact

__all__ = ["GateReport", "HeldOutQuery", "decimal_bound", "judge"]

#: The decimals a gap that does not terminate in decimal is rounded up to, for the register.
_BOUND_DIGITS = 12


@dataclass(frozen=True)
class HeldOutQuery:
    """One held-out query that has a target, and whether it falls within its tolerance."""

    id: str
    status: str
    #: The gap of the largest size (for a grouped query, the group's); ``None`` when none was formed.
    gap: Optional[Fraction]
    tolerance: Optional[Fraction]
    within: bool
    #: Why it missed, in a sentence with no value of the run in it; empty when it is within.
    why: str = ""


@dataclass(frozen=True)
class GateReport:
    """The held-out check: the profile read, its grade beside the pinned one's, and each query."""

    #: The held-out profile's ``sha256:`` digest.
    profile: str
    grade: str
    pinned_grade: str
    #: The held-out queries that have a target, in the registry's order.
    queries: Tuple[HeldOutQuery, ...]
    #: The held-out queries that have no target (the profile states none for them).
    untargeted: Tuple[str, ...] = ()

    @property
    def missed(self) -> Tuple[HeldOutQuery, ...]:
        return tuple(query for query in self.queries if not query.within)

    @property
    def calibrated(self) -> bool:
        """At least one held-out query has a target and every one that has one is within."""
        return bool(self.queries) and not self.missed

    def evidence(self) -> HeldOutEvidence:
        """The register's record of the check: the profile, the largest gap and the tolerance it met.

        Only a calibrated report has one. The gap is rounded up to :data:`_BOUND_DIGITS` decimals when
        it does not terminate, so that it never understates the largest gap.
        """
        assert self.calibrated, "only a calibrated check has evidence to record"
        sized = [query for query in self.queries if query.gap is not None]
        widest = max(sized, key=lambda query: abs(query.gap or Fraction(0)))
        assert widest.gap is not None and widest.tolerance is not None
        return HeldOutEvidence(
            profile=self.profile,
            largest_gap=decimal_bound(abs(widest.gap)),
            tolerance=decimal_bound(widest.tolerance),
        )


def decimal_bound(value: Fraction) -> str:
    """*value* as exact decimal text; one that does not terminate is rounded up, never down."""
    text = render_exact(value)
    if "/" not in text:
        return text
    scale = 10**_BOUND_DIGITS
    return render_exact(Fraction(ceil(value * scale), scale))


def _diffs(row: QueryRow) -> List[Fraction]:
    found = [group.gap for group in row.groups if group.gap is not None]
    if row.gap is not None:
        found.append(row.gap)
    return found


def judge(
    rows: Sequence[QueryRow], stated: AbstractSet[str]
) -> Tuple[Tuple[HeldOutQuery, ...], Tuple[str, ...]]:
    """Each held-out row the profile states a rate for (*stated*) set against its tolerance, and the ids
    of the rows it states none for."""
    judged: List[HeldOutQuery] = []
    untargeted: List[str] = []
    for row in rows:
        if row.name not in stated:
            untargeted.append(row.name)
            continue
        diffs = _diffs(row)
        largest = max(diffs, key=abs) if diffs else None
        if not diffs:
            why = f"it was not measured ({row.status})"
        elif row.tolerance is None:
            why = "it has no tolerance (neither its own nor calibration.heldOutTolerance)"
        elif abs(largest or Fraction(0)) > row.tolerance:
            why = "its gap is larger than its tolerance"
        else:
            why = ""
        judged.append(
            HeldOutQuery(
                id=row.name,
                status=row.status,
                gap=largest,
                tolerance=row.tolerance,
                within=not why,
                why=why,
            )
        )
    return tuple(judged), tuple(untargeted)
