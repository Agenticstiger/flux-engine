# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The scorecard's ``queries`` dimension (ADR 0028 section 7, scorecard format 1-4-0).

A bundle that declares ``consumers`` has its queries counted on the run (:mod:`flux_engine.consumers.outcome`
forms the rows), and the scorecard holds them in a ``queries`` member of the dimension of that name:

* per query: its name, its set (``calibration`` or ``heldOut``), the digest of its canonical text, the rows
  it returned and its unit's, the rate, its target, the gap, the tolerance, a status, and whether it is
  met; a query with ``groupBy`` also holds one row per group, the groups named by ordinal as every group
  of a scorecard is;
* each requirement the queries put on the copy, met or unmet;
* two shares, of the calibration queries and of the held-out ones that have a target and a tolerance and
  fall within it. The dimension's score is the smaller of the shares that exist;
* the duckdb, sqlglot and pyarrow versions that counted, and the grade of the pinned profile beside the
  held-out profile's.

Without the ``[sql]`` extra the member says the queries were not counted and gives the command that
installs it, and the dimension is ``not measured``, so a threshold declared on it is not met.

This module imports the standard library, the number rendering of the compiler and the seam's
printable-text helper, and nothing of SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from flux_engine.concordance.compiler import render_exact
from flux_engine.seam.coverage import printable

__all__ = [
    "CALIBRATION",
    "COUNTED",
    "HELD_OUT",
    "INSTALL_COMMAND",
    "NOT_COUNTED",
    "GroupRow",
    "QueriesDimension",
    "QueryRow",
    "QueryShare",
    "QueryVersions",
    "RequirementRow",
]

CALIBRATION = "calibration"
HELD_OUT = "heldOut"

#: The member's ``status``: the queries were counted, or could not be (the extra is not installed,
#: or the run's artifacts do not reproduce its digest).
COUNTED = "counted"
NOT_COUNTED = "not counted"

#: What a person runs to be able to count the queries.
INSTALL_COMMAND = "pip install 'flux-sim-engine[sql]'"

#: A status that is a rate compared with a target, as opposed to one that says why it was not.
_COMPARED = "measured"
#: The statuses of a query whose count did not finish or was not compared.
_NOT_ANSWERED = frozenset({"timedOut", "outOfMemory", "crashed", "error", "versionsDiffer"})
_NO_TARGET = frozenset({"noTarget", "withheld"})


def _exact(value: Optional[Fraction]) -> Optional[str]:
    return None if value is None else render_exact(value)


@dataclass(frozen=True)
class GroupRow:
    """The rate of one group of a query with ``groupBy``, named by its ordinal."""

    #: The position in the order of the groups: the null group first, then the levels by code point.
    group: int
    null_group: bool
    rows: int
    unit_rows: int
    rate: Optional[Fraction]
    target: Optional[Fraction]
    gap: Optional[Fraction]
    status: str
    met: Optional[bool]

    def to_document(self) -> Dict[str, Any]:
        return {
            "group": self.group,
            "nullGroup": self.null_group,
            "rows": self.rows,
            "unitRows": self.unit_rows,
            "rate": _exact(self.rate),
            "target": _exact(self.target),
            "gap": _exact(self.gap),
            "status": self.status,
            "met": self.met,
        }


@dataclass(frozen=True)
class QueryRow:
    """One consumer query as counted on the run."""

    name: str
    #: ``calibration`` or ``heldOut``.
    set: str
    #: ``sha256:`` and the hex digest of the canonical text.
    digest: str
    rows: Optional[int]
    unit_rows: Optional[int]
    rate: Optional[Fraction]
    target: Optional[Fraction]
    gap: Optional[Fraction]
    tolerance: Optional[Fraction]
    status: str
    #: Whether the gap is within the tolerance; ``None`` when the query has no target or no tolerance
    #: or its rate was not formed, so that it is in no share.
    met: Optional[bool]
    #: Why a query that was not counted was not, as the query process named it (never a value).
    reason: Optional[str] = None
    groups: Tuple[GroupRow, ...] = ()

    @property
    def in_share(self) -> bool:
        """Whether the query counts in its set's share: it was compared with a target and a tolerance."""
        return self.met is not None

    def to_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "name": self.name,
            "set": self.set,
            "digest": self.digest,
            "rows": self.rows,
            "unitRows": self.unit_rows,
            "rate": _exact(self.rate),
            "target": _exact(self.target),
            "gap": _exact(self.gap),
            "tolerance": _exact(self.tolerance),
            "status": self.status,
            "met": self.met,
            "reason": self.reason,
        }
        if self.groups:
            document["groups"] = [group.to_document() for group in self.groups]
        return document


@dataclass(frozen=True)
class RequirementRow:
    """What a query needs the copy to hold, and whether the run's rows hold it."""

    query: str
    expose_id: str
    column: str
    kind: str
    #: The requirement in words. It repeats what the consumers wrote in the query and never a value of the data.
    description: str
    #: ``met``, ``unmet``, ``versionsDiffer``, or the status the count took when it did not finish.
    status: str

    @property
    def met(self) -> Optional[bool]:
        """``True`` when met, ``False`` when unmet, and ``None`` when it was not decided."""
        return {"met": True, "unmet": False}.get(self.status)

    def to_document(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "exposeId": self.expose_id,
            "column": self.column,
            "kind": self.kind,
            "description": self.description,
            "status": self.status,
            "met": self.met,
        }


@dataclass(frozen=True)
class QueryShare:
    """``covered`` of ``of`` queries within their tolerance, with the exact share when any was compared."""

    covered: int = 0
    of: int = 0

    @property
    def share(self) -> Optional[Fraction]:
        return Fraction(self.covered, self.of) if self.of else None

    def to_document(self) -> Dict[str, Any]:
        return {"covered": self.covered, "of": self.of, "share": _exact(self.share)}


@dataclass(frozen=True)
class QueryVersions:
    """The versions that counted the queries, as the query process reported them."""

    duckdb: str
    sqlglot: str
    pyarrow: str

    def to_document(self) -> Dict[str, str]:
        return {"duckdb": self.duckdb, "sqlglot": self.sqlglot, "pyarrow": self.pyarrow}


@dataclass(frozen=True)
class QueriesDimension:
    """Every query of a run, the requirements, the two shares, the versions and the grades."""

    status: str = COUNTED
    #: Why nothing was counted, for the status ``not counted``.
    detail: str = ""
    queries: Tuple[QueryRow, ...] = ()
    requirements: Tuple[RequirementRow, ...] = ()
    versions: Optional[QueryVersions] = None
    #: The privacy grade of the pinned profile and of the held-out profile (ADR 0020 rule 2), or ``None``
    #: for one that was not read.
    pinned_grade: Optional[str] = None
    held_out_grade: Optional[str] = None

    def share_of(self, which: str) -> QueryShare:
        """The share of the queries of one set (``calibration`` or ``heldOut``) that are within."""
        found = [row for row in self.queries if row.set == which and row.in_share]
        return QueryShare(covered=sum(1 for row in found if row.met), of=len(found))

    @property
    def score(self) -> Optional[Fraction]:
        """The smaller of the shares that were measured, or ``None`` when neither was."""
        shares = [
            share
            for share in (
                self.share_of(CALIBRATION).share,
                self.share_of(HELD_OUT).share,
            )
            if share is not None
        ]
        return min(shares) if shares else None

    def counts(self) -> Dict[str, int]:
        """The counts behind the dimension's score, in the shape every dimension states them.

        ``properties`` is the queries, ``scored`` those compared with a target and a tolerance,
        ``censored`` those with no target to compare with, and ``refused`` those whose count did not
        finish or was not compared (``timedOut``, ``outOfMemory``, ``crashed``, ``error``, ``versionsDiffer``).
        """
        return {
            "properties": len(self.queries),
            "scored": sum(1 for row in self.queries if row.in_share),
            "censored": sum(1 for row in self.queries if row.status in _NO_TARGET),
            "refused": sum(1 for row in self.queries if row.status in _NOT_ANSWERED),
        }

    def requirements_met(self) -> Tuple[int, int]:
        """``(met, decided)`` over the requirements that were decided."""
        decided = [row for row in self.requirements if row.met is not None]
        return sum(1 for row in decided if row.met), len(decided)

    def to_document(self) -> Dict[str, Any]:
        """The ``queries`` member of the dimension (docs/spec/scorecard-1-4.schema.json, ``Queries``)."""
        if self.status != COUNTED:
            return {"status": self.status, "detail": self.detail, "install": INSTALL_COMMAND}
        assert self.versions is not None
        return {
            "status": self.status,
            "versions": self.versions.to_document(),
            "shares": {
                "calibration": self.share_of(CALIBRATION).to_document(),
                "heldOut": self.share_of(HELD_OUT).to_document(),
            },
            "grades": {"pinned": self.pinned_grade, "heldOut": self.held_out_grade},
            "queries": [row.to_document() for row in self.queries],
            "requirements": [row.to_document() for row in self.requirements],
        }

    def lines(self) -> List[str]:
        """What a person reads: the shares and the requirements, then each query that was not within."""
        if self.status != COUNTED:
            return [f"queries: not measured -- {self.detail}; install with: {INSTALL_COMMAND}"]
        parts = []
        for label, which in (("calibration", CALIBRATION), ("held-out", HELD_OUT)):
            share = self.share_of(which)
            parts.append(
                f"{label} {share.covered} of {share.of} within tolerance"
                if share.of
                else f"{label} none compared"
            )
        met, decided = self.requirements_met()
        first = f"queries: {', '.join(parts)}; requirements {met} of {decided} met"
        if self.pinned_grade is not None or self.held_out_grade is not None:
            first += (
                f"; profile grade {self.pinned_grade or 'not read'}, "
                f"held-out profile grade {self.held_out_grade or 'not read'}"
            )
        lines = [first]
        for row in self.queries:
            lines.append("  " + _query_line(row))
        for requirement in self.requirements:
            if requirement.met is False:
                lines.append(
                    f"  requirement of {printable(requirement.query)}: "
                    f"{printable(requirement.expose_id)}.{printable(requirement.column)} needs "
                    f"{printable(requirement.description)}: unmet"
                )
        return lines


def _display(value: Optional[Fraction]) -> str:
    if value is None:
        return "none"
    text = render_exact(value)
    return text if len(text) <= 8 else f"{float(value):.4f}"


def _query_line(row: QueryRow) -> str:
    where = "held-out" if row.set == HELD_OUT else "calibration"
    if row.status != _COMPARED and row.rate is None and row.target is None:
        extra = f" ({row.reason})" if row.reason else ""
        return f"query {row.name} ({where}): {row.status}{extra}"
    line = f"query {row.name} ({where}): rate {_display(row.rate)}"
    if row.target is not None:
        line += f", target {_display(row.target)}"
    if row.gap is not None:
        line += f", gap {_display(row.gap)}"
    if row.tolerance is not None:
        line += f", tolerance {_display(row.tolerance)}"
    verdict = {True: "within", False: "outside", None: row.status}[row.met]
    return f"{line}: {verdict}"
