# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Children per active day, scored on a run's written rows against the pinned profile (ADR 0025 section 8).

A ``within`` entry with ``exposure: true`` makes the number of a parent's children follow the days
the parent is active (ADR 0025 section 4). A profile that publishes an ``exposure`` for the
relationship's parent lifetime columns holds ``children``, the number of child rows whose parent's
start is known, and ``activeDays``, the sum over the parents whose start is known of the whole days
each is active inside the window. Each such entry is one line ``exposure:<relationship>`` of
``verify``: the run's children per active day against the profile's ``children / activeDays``,
passing when the two differ by no more than a relative tolerance. A ``facts`` entry that names the
line gives the tolerance, and otherwise it is :data:`EXPOSURE_TOLERANCE`.

The run's count is formed as the profile's is. A parent is active from the day that holds its start
to the day that holds its end, cut to the run's window (``spec.timeBounds``, both ends inclusive of
their day); a parent whose end is NULL, or is before its start, is active to the window's last day;
a parent whose start is NULL is not counted. The children are the child rows whose key names such a
parent. The days are whole days and are not weighted by any seasonal rates, so the line scores the
number of children against the days, and a column's ``rates`` change where in a lifetime the
children fall and not this ratio.

A line is not scored, with the reason, when the profile withheld a count, when its active days are
none, when the run has no active day, when a table or a column the line reads is hidden (no stream
holds it), or when the rows cannot be read. A line with no published ``exposure`` is never made, so
a run whose profile publishes none adds nothing.

This module counts exact integers and reads the rows a run wrote; it generates nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.concordance.verifier import OUTSIDE, WITHIN
from flux_engine.core.errors import FluxError
from flux_engine.seam.dates import lifetime_entries_of, within_entries_of
from flux_engine.seam.facts import Fact
from flux_stats.profile import ExposureEntry, Profile, RelationshipEntry
from flux_stats.reading import MS_PER_DAY, parse_date_days, parse_rfc3339_ms
from flux_stats.spelling import render_exact

__all__ = [
    "EXPOSURE",
    "EXPOSURE_TOLERANCE",
    "ExposureChecks",
    "ExposureLine",
    "ExposureSource",
    "exposure_checks",
    "exposure_sources",
    "line_name",
]

#: The prefix of a line's name, and the ``property`` of its record.
EXPOSURE = "exposure"

#: The relative tolerance of a line no ``facts`` entry names: ``10 %`` of the profile's children per day.
EXPOSURE_TOLERANCE = Fraction(1, 10)

_HIDDEN_TAIL = "the run's streams hold no hidden column and no hidden table"


def line_name(relationship: str) -> str:
    """The name of a relationship's line: ``exposure:<relationship>``."""
    return f"{EXPOSURE}:{relationship}"


@dataclass(frozen=True)
class ExposureSource:
    """One ``within`` entry with ``exposure: true``, with what the contract and the bundle say about it.

    ``child`` holds the foreign key ``from_columns``, which name the parent's ``to_columns``;
    ``start`` and ``end`` are the parent's lifetime columns. ``tolerance`` is the relative tolerance
    a ``facts`` entry gives the line, and ``hidden`` the reason the line cannot be scored when it
    reads a hidden table or column.
    """

    child: str
    relationship: str
    parent: str
    from_columns: Tuple[str, ...]
    to_columns: Tuple[str, ...]
    start: str
    end: Optional[str]
    tolerance: Optional[Fraction] = None
    hidden: Optional[str] = None


@dataclass(frozen=True)
class ExposureLine:
    """The line ``exposure:<relationship>`` of one child emit: the run's children per active day and the profile's.

    ``gap`` is ``|run - profile| / profile`` (the absolute difference when the profile's rate is 0).
    """

    expose_id: str
    relationship: str
    run_children: int
    run_days: int
    profile_children: int
    profile_days: int
    tolerance: Fraction
    gap: Fraction
    within: bool

    @property
    def name(self) -> str:
        """``exposure:<relationship>``."""
        return line_name(self.relationship)

    def line(self) -> str:
        """The line a person reads: its name and emit, whether it passed, and the two rates."""
        state = "passed" if self.within else "failed"
        return (
            f"{self.name} ({self.expose_id}): {state} -- children per active day "
            f"{self.run_children} over {self.run_days} against {self.profile_children} over "
            f"{self.profile_days}, gap {_shown(self.gap)} (tolerance {_shown(self.tolerance)})"
        )

    def to_document(self) -> Dict[str, Any]:
        """The line as the result document records it: counts and exact decimals, no value."""
        return {
            "line": self.name,
            "emit": self.expose_id,
            "outcome": WITHIN if self.within else OUTSIDE,
            "children": self.run_children,
            "activeDays": self.run_days,
            "profileChildren": self.profile_children,
            "profileActiveDays": self.profile_days,
            "gap": render_exact(self.gap),
            "tolerance": render_exact(self.tolerance),
        }


def _shown(value: Fraction) -> str:
    return f"{float(value):.4f}"


@dataclass(frozen=True)
class ExposureChecks:
    """Every exposure line of a run, and what could not be scored (each ``(name, reason)``)."""

    lines: Tuple[ExposureLine, ...] = ()
    not_scored: Tuple[Tuple[str, str], ...] = ()

    @property
    def empty(self) -> bool:
        """Whether the profile asked for nothing: no line and nothing left unscored."""
        return not (self.lines or self.not_scored)

    def failed(self) -> List[ExposureLine]:
        """The lines outside their tolerance: each fails the stage."""
        return [line for line in self.lines if not line.within]

    def failures(self) -> List[str]:
        """A line outside its tolerance, one entry each: each fails the stage."""
        return [line.line() for line in self.failed()]

    def text(self) -> List[str]:
        """The lines under the legs: each line, then each fact left unscored."""
        return [f"  {line.line()}" for line in self.lines] + [
            f"  facts: not scored -- {name}: {reason}" for name, reason in self.not_scored
        ]

    def artifact(self) -> Dict[str, Any]:
        """The ``exposure`` key of the result: nothing for a run whose profile publishes none."""
        if self.empty:
            return {}
        document: Dict[str, Any] = {
            "met": all(line.within for line in self.lines),
            "lines": [line.to_document() for line in self.lines],
        }
        if self.not_scored:
            document["notScored"] = [
                {"fact": name, "reason": reason} for name, reason in self.not_scored
            ]
        return {"exposure": document}


def _hidden_reason(
    emits: Sequence[Any], reads: Sequence[Tuple[str, str]], tables: Sequence[str]
) -> Optional[str]:
    """Why the line cannot be scored on the run's streams: it reads a hidden column or a hidden table."""
    hidden = {emit.expose_id: {column.name for column in emit.hidden_columns} for emit in emits}
    columns = sorted(
        {f"{expose}.{name}" for expose, name in reads if name in hidden.get(expose, ())}
    )
    held = sorted({emit.expose_id for emit in emits if emit.is_hidden_table} & set(tables))
    parts = []
    if columns:
        parts.append("the hidden column " + ", ".join(columns))
    if held:
        parts.append("the hidden table " + ", ".join(held))
    if not parts:
        return None
    return "it reads " + " and ".join(parts) + f"; {_HIDDEN_TAIL}"


def _source_of(
    emit: Any,
    entry: Any,
    relationships: Mapping[str, Any],
    by_id: Mapping[str, Any],
    tolerances: Mapping[Tuple[str, str], Any],
    resolved: Sequence[Any],
) -> Optional[ExposureSource]:
    """The source of one ``within`` entry of *emit*, or ``None`` when it has no exposure to read.

    An entry without ``exposure: true``, one whose relationship is not declared, and one whose
    parent has no lifetime are left out.
    """
    relationship = relationships.get(entry.relationship[0]) if entry.exposure else None
    parent = by_id.get(relationship.target) if relationship is not None else None
    lifetime = lifetime_entries_of(parent.extension, parent.expose_id)[:1] if parent else ()
    if relationship is None or parent is None or not lifetime:
        return None
    start, end = lifetime[0].start, lifetime[0].end
    reads = [(emit.expose_id, column) for column in relationship.from_columns]
    reads += [(parent.expose_id, column) for column in (*relationship.to_columns, start)]
    if end is not None:
        reads.append((parent.expose_id, end))
    return ExposureSource(
        child=emit.expose_id,
        relationship=entry.relationship[0],
        parent=parent.expose_id,
        from_columns=tuple(relationship.from_columns),
        to_columns=tuple(relationship.to_columns),
        start=start,
        end=end,
        tolerance=tolerances.get((emit.expose_id, line_name(entry.relationship[0]))),
        hidden=_hidden_reason(resolved, reads, [emit.expose_id, parent.expose_id]),
    )


def exposure_sources(resolved: Sequence[Any], facts: Sequence[Fact]) -> Tuple[ExposureSource, ...]:
    """The ``within`` entries with ``exposure: true`` of the Simulation's emits *resolved*, in the order of the emits.

    Each is read with the relationship the contract declares (its columns and its parent), the
    parent's lifetime columns, and the tolerance a ``facts`` entry gives the line
    ``exposure:<relationship>``. An entry whose relationship or parent lifetime cannot be found is
    left out: the bundle's own checks refuse it before a run exists.
    """
    by_id = {emit.expose_id: emit for emit in resolved}
    tolerances = {(fact.emit, fact.rule): fact.tolerance for fact in facts}
    found: List[ExposureSource] = []
    for emit in resolved:
        if emit.keys is None:
            continue
        relationships = {
            relationship.name: relationship for relationship in emit.keys.relationships
        }
        for entry in within_entries_of(emit.extension, emit.expose_id):
            source = _source_of(emit, entry, relationships, by_id, tolerances, resolved)
            if source is not None:
                found.append(source)
    return tuple(found)


# ---------------------------------------------------------------------------
# Reading the rows
# ---------------------------------------------------------------------------


class _Unreadable(Exception):
    """A cell that is no date or instant, or a column the rows do not hold."""


def _column(rows: Sequence[Mapping[str, Any]], name: str) -> Sequence[Any]:
    cells = getattr(rows, "cells", None)
    if cells is not None and getattr(rows, "has", lambda _: True)(name):
        return list(cells(name))
    try:
        return [row[name] for row in rows]
    except KeyError:
        raise _Unreadable(f"the rows hold no column {name}") from None


def _instant(cell: Any, type_name: str, *, end: bool) -> Optional[int]:
    """A lifetime's start or end as epoch milliseconds: a DATE start is its midnight, a DATE end its last millisecond."""
    if cell is None:
        return None
    try:
        if type_name == "DATE":
            days = cell if isinstance(cell, int) else parse_date_days(cell)
            return (days + 1) * MS_PER_DAY - 1 if end else days * MS_PER_DAY
        return parse_rfc3339_ms(cell)
    except (FluxError, TypeError, ValueError, OverflowError):
        raise _Unreadable(f"a cell of a {type_name} column is not a {type_name}") from None


def _active_days(start: int, end: Optional[int], window: Tuple[int, int]) -> int:
    """The whole days a lifetime is active inside *window*, both of its days counted (``0`` when it is not)."""
    low, high = window[0] // MS_PER_DAY, window[1] // MS_PER_DAY
    first = max(start // MS_PER_DAY, low)
    last = high if end is None or end < start else min(end // MS_PER_DAY, high)
    return max(last - first + 1, 0)


def _count(
    source: ExposureSource,
    parents: Sequence[Mapping[str, Any]],
    children: Sequence[Mapping[str, Any]],
    parent_types: Mapping[str, str],
    window: Tuple[int, int],
) -> Tuple[int, int]:
    """``(children, active days)`` of the run: the child rows whose parent's start is known, and those parents' days."""
    starts = _column(parents, source.start)
    ends = _column(parents, source.end) if source.end is not None else [None] * len(starts)
    keys = [_column(parents, name) for name in source.to_columns]
    start_type = parent_types.get(source.start, "TIMESTAMP")
    end_type = parent_types.get(source.end, "TIMESTAMP") if source.end is not None else ""
    held: Set[Tuple[Any, ...]] = set()
    days = 0
    for row, raw_start in enumerate(starts):
        start = _instant(raw_start, start_type, end=False)
        if start is None:
            continue
        end = _instant(ends[row], end_type, end=True) if source.end is not None else None
        days += _active_days(start, end, window)
        key = tuple(column[row] for column in keys)
        if None not in key:
            held.add(key)
    foreign = [_column(children, name) for name in source.from_columns]
    reached = 0
    for row in range(len(children)):
        key = tuple(column[row] for column in foreign)
        if None not in key and key in held:
            reached += 1
    return reached, days


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def _published(profile: Profile, source: ExposureSource) -> Optional[ExposureEntry]:
    """The profile's ``exposure`` for the parent lifetime *source* reads, or ``None`` when it publishes none."""
    for relationship in profile.relationships:
        if not _is_for(relationship, source):
            continue
        for entry in relationship.exposure:
            if entry.start == source.start and entry.end == source.end:
                # An entry that holds neither count publishes nothing to compare with.
                return None if entry.children is None and entry.active_days is None else entry
    return None


def _is_for(relationship: RelationshipEntry, source: ExposureSource) -> bool:
    return (
        relationship.child_table == source.child
        and relationship.parent_table == source.parent
        and tuple(relationship.child_columns) == source.from_columns
        and tuple(relationship.parent_columns) == source.to_columns
    )


def _exposure_line(
    source: ExposureSource,
    entry: ExposureEntry,
    rows_of: Callable[[str], Tuple[Optional[Sequence[Mapping[str, Any]]], Optional[str]]],
    types_of: Callable[[str], Mapping[str, str]],
    window: Tuple[int, int],
) -> Tuple[Optional[ExposureLine], Optional[str]]:
    """``(line, None)`` for a source the run can be compared on, or ``(None, why it is not scored)``.

    The reasons, in order: the source reads a hidden column or table, the profile withheld a count,
    the profile's active days are none, the rows cannot be read, a cell is no date, and no parent
    of the run is active inside the window.
    """
    if source.hidden is not None:
        return None, source.hidden
    if entry.children is None or entry.active_days is None:
        return None, "the profile holds neither count of children nor of active days"
    total, days = entry.children.value, entry.active_days.value
    if total is None or days is None:
        return None, "the profile withheld the count of children or of active days"
    if days == 0:
        return None, "the profile's active days are none, so it has no rate"
    parents, parents_problem = rows_of(source.parent)
    children, children_problem = rows_of(source.child)
    problem = parents_problem or children_problem
    if problem is not None or parents is None or children is None:
        return None, f"the emit's rows cannot be read: {problem}"
    clipped = (max(window[0], entry.window_start), min(window[1], entry.window_end))
    if clipped[0] > clipped[1]:
        return None, "the profile's window and the run's window share no day"
    try:
        found, spent = _count(source, parents, children, types_of(source.parent), clipped)
    except _Unreadable as error:
        return None, str(error)
    if spent == 0:
        return None, "no parent of the run is active inside the window"
    run_rate, profile_rate = Fraction(found, spent), Fraction(int(total), int(days))
    gap = abs(run_rate - profile_rate) / (profile_rate if profile_rate else 1)
    tolerance = source.tolerance if source.tolerance is not None else EXPOSURE_TOLERANCE
    line = ExposureLine(
        expose_id=source.child,
        relationship=source.relationship,
        run_children=found,
        run_days=spent,
        profile_children=int(total),
        profile_days=int(days),
        tolerance=tolerance,
        gap=gap,
        within=gap <= tolerance,
    )
    return line, None


def exposure_checks(
    sources: Sequence[ExposureSource],
    profile: Profile,
    rows_of: Callable[[str], Tuple[Optional[Sequence[Mapping[str, Any]]], Optional[str]]],
    types_of: Callable[[str], Mapping[str, str]],
    window: Tuple[int, int],
) -> ExposureChecks:
    """The lines of the ``within`` entries in *sources* whose relationship the profile publishes an ``exposure`` for.

    *rows_of* gives an emit's rows and, when they cannot be read, why; *types_of* its columns'
    logical types; *window* is the run's ``spec.timeBounds`` in epoch milliseconds. A source the
    profile publishes nothing for makes no line and is not listed.
    """
    lines: List[ExposureLine] = []
    unscored: List[Tuple[str, str]] = []
    for source in sources:
        entry = _published(profile, source)
        if entry is None:
            continue
        where = f"{line_name(source.relationship)} of {source.child}"
        line, reason = _exposure_line(source, entry, rows_of, types_of, window)
        if line is not None:
            lines.append(line)
        elif reason is not None:
            unscored.append((where, reason))
    return ExposureChecks(lines=tuple(lines), not_scored=tuple(sorted(set(unscored))))
