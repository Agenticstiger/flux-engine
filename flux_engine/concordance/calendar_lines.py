# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A date column's calendar, scored on a run's written rows against the pinned profile (ADR 0025 section 8).

A profile that publishes a ``calendar`` for a table holds, for a DATE or TIMESTAMP column, up to
three ``levelHistogram/1`` statistics of the column's known values: by month, by ISO day of week
(Monday is ``1``) and by hour of week (Monday 00:00 UTC is ``000``), all read in UTC. Each is
one line ``calendar:<column>`` of ``verify``, passing when every published histogram of the column is
within its tolerance of the run's.

The comparison is the one a ``levelHistogram/1`` column statistic gets against a profile
(:meth:`flux_engine.concordance.verifier.ClearProfile._score`): the registered definition's total
variation inequality over the published cells, the cells a rule suppressed left out on both sides, a
noisy reference's margin widened by its noise, and a group the profile withheld not scored. The
distance printed is the total variation distance of that comparison, ``1`` less the similarity it
forms. The tolerance is :data:`CALENDAR_TOLERANCE`: a calendar declares none, so it is the one
``fluxsim init`` writes for a distance.

A DATE has no hour, so a published ``hourOfWeek`` of a DATE column is listed as not scored. A hidden
column or table is listed as not scored with the reason the facts over dates give, since the run's
streams hold no hidden column and no hidden table. A line without a published calendar is never made,
so a run whose profile holds none adds nothing.

Each histogram a line scored is also a row of the scorecard's ``temporal`` dimension
(:func:`calendar_rows`, SCORECARD-SPEC.md section 3, format 1-4-0), read from the same comparison
and never formed a second time.

This module counts exact integers and reads the rows a run wrote; it generates nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from flux_engine.concordance.decoded import DecodedRows
from flux_engine.concordance.scorecard import PropertyScore, display
from flux_engine.concordance.verifier import CENSORED, OUTSIDE, WITHIN, ClearProfile, Refusal
from flux_engine.core.errors import FluxError
from flux_stats.definitions import LEVEL_HISTOGRAM
from flux_stats.fixedpoint import FLX_5005
from flux_stats.profile import CalendarEntry, Profile, StatisticEntry
from flux_stats.reading import (
    MS_PER_DAY,
    ValueDomain,
    parse_date_days,
    parse_rfc3339_ms,
)
from flux_stats.registry import Check, GroupStatistic
from flux_stats.spelling import render_exact

__all__ = [
    "CALENDAR",
    "CALENDAR_TOLERANCE",
    "NOT_GATED_REASON",
    "NO_HOUR_REASON",
    "CalendarChecks",
    "CalendarLine",
    "CalendarPart",
    "calendar_counts",
    "calendar_for_emit",
    "calendar_row",
    "calendar_rows",
    "hidden_in_plan",
    "hidden_not_scored",
    "line_name",
]

#: The ``property`` of a calendar histogram's row in the scorecard.
CALENDAR = "calendar"

#: The tolerance a calendar histogram is held to: a total variation distance of ``0.10``, the one a
#: ``levelHistogram`` is written with when its author names none (``fluxsim init``). A calendar's
#: entry carries no tolerance of its own.
CALENDAR_TOLERANCE = Fraction(1, 10)

#: Why a published ``hourOfWeek`` of a DATE column is not scored.
NO_HOUR_REASON = "a DATE has no hour"

#: Why a line outside its tolerance on a column no ``rates`` entry shapes does not fail the run: the
#: bundle has no way to move that column's calendar, so a distance there says nothing it can act on.
NOT_GATED_REASON = "not gated: no rates entry shapes this column"

_HIDDEN_TAIL = "the run's streams hold no hidden column and no hidden table"
_MS_PER_HOUR = 3_600_000
#: 1970-01-01 was a Thursday: ISO day 4, and the 72nd hour of an ISO week that starts on Monday.
_EPOCH_DAY_OF_WEEK = 3
_EPOCH_HOUR_OF_WEEK = 72
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_HOURS_PER_WEEK = 168

_KEYS = ("month", "dayOfWeek", "hourOfWeek")


def line_name(column: str) -> str:
    """The name of a column's line: ``calendar:<column>``."""
    return f"calendar:{column}"


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def _counts_of_days(days: "np.ndarray") -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """The counts of *days* (since 1970-01-01) by month, and by ISO day of week from Monday."""
    months = days.astype("datetime64[D]").astype("datetime64[M]").astype(np.int64) % 12
    weekdays = (days + _EPOCH_DAY_OF_WEEK) % 7
    return (
        tuple(int(n) for n in np.bincount(months, minlength=12)),
        tuple(int(n) for n in np.bincount(weekdays, minlength=7)),
    )


def _present(values: "np.ndarray", valid: Optional["np.ndarray"]) -> "np.ndarray":
    return values if valid is None else values[valid]


def _fast(
    rows: DecodedRows, column: str, is_date: bool
) -> Optional[Tuple[Tuple[int, ...], Tuple[int, ...], Optional[Tuple[int, ...]]]]:
    """The counts read from the column's decoded integers, or ``None`` when it cannot be so read."""
    if is_date:
        exact = rows.exact(column, ValueDomain.DATE)
        if exact is None:
            return None
        months, weekdays = _counts_of_days(_present(exact.units, exact.valid))
        return months, weekdays, None
    exact = rows.instants(column)
    if exact is None:
        return None
    millis = _present(exact.units, exact.valid)
    months, weekdays = _counts_of_days(millis // MS_PER_DAY)
    hours = (millis // _MS_PER_HOUR + _EPOCH_HOUR_OF_WEEK) % _HOURS_PER_WEEK
    return months, weekdays, tuple(int(n) for n in np.bincount(hours, minlength=_HOURS_PER_WEEK))


def _slow(
    rows: Sequence[Mapping[str, Any]], column: str, is_date: bool
) -> Tuple[Tuple[int, ...], Tuple[int, ...], Optional[Tuple[int, ...]]]:
    """The same counts, one cell at a time; a cell that is no date or instant is refused (``FLX-5005``)."""
    months = [0] * 12
    weekdays = [0] * 7
    hours = [0] * _HOURS_PER_WEEK
    for index, row in enumerate(rows):
        cell = row[column]
        if cell is None:
            continue
        try:
            if is_date:
                days = cell if isinstance(cell, int) else parse_date_days(cell)
            else:
                millis = parse_rfc3339_ms(cell)
                days = millis // MS_PER_DAY
                hours[(millis // _MS_PER_HOUR + _EPOCH_HOUR_OF_WEEK) % _HOURS_PER_WEEK] += 1
            months[date.fromordinal(days + _EPOCH_ORDINAL).month - 1] += 1
        except (FluxError, ValueError, OverflowError, TypeError):
            kind = "a calendar date" if is_date else "an RFC 3339 instant"
            raise FluxError(
                FLX_5005, f"column {column!r} row {index}: the value is not {kind}"
            ) from None
        weekdays[(days + _EPOCH_DAY_OF_WEEK) % 7] += 1
    return tuple(months), tuple(weekdays), None if is_date else tuple(hours)


def calendar_counts(
    rows: Sequence[Mapping[str, Any]], column: str, type_name: str
) -> Dict[str, Tuple[int, ...]]:
    """The non-NULL values of *column* counted by month, by ISO day of week and, for a TIMESTAMP, by hour of week.

    Everything is UTC: a DATE is its own day, a TIMESTAMP the instant it names. The counts are in the
    order of :data:`flux_stats.profile.CALENDAR_LEVELS` (months ``01`` to ``12``, Monday first,
    Monday 00:00 first), keyed by the profile's names ``month``, ``dayOfWeek`` and ``hourOfWeek``;
    a DATE has no ``hourOfWeek``. A cell that is not a date or an instant is refused (``FLX-5005``),
    naming its row and never quoting it.
    """
    is_date = type_name == "DATE"
    found = _fast(rows, column, is_date) if isinstance(rows, DecodedRows) else None
    month, weekday, hour = found if found is not None else _slow(rows, column, is_date)
    counts: Dict[str, Tuple[int, ...]] = {"month": month, "dayOfWeek": weekday}
    if hour is not None:
        counts["hourOfWeek"] = hour
    return counts


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarPart:
    """One published histogram of a calendar entry, compared with the run's.

    ``distance`` is the total variation distance over the cells compared and ``tolerance`` the
    margin it was held to, widened when the profile's histogram is noisy; both are ``None`` for a
    histogram that was censored or refused. ``records`` is the run's count of known values.
    """

    key: str
    outcome: str
    records: int
    distance: Optional[Fraction] = None
    tolerance: Optional[Fraction] = None
    widening: Optional[Fraction] = None
    refusal: Optional[Refusal] = None

    @property
    def within(self) -> bool:
        """Whether the histogram landed inside its tolerance."""
        return self.outcome == WITHIN

    @property
    def censored(self) -> bool:
        """Whether the profile withheld the histogram, so it was not scored."""
        return self.outcome == CENSORED

    def text(self) -> str:
        """``month distance 0.0123 (tolerance 0.1)``, or what kept the histogram from a distance."""
        if self.refusal is not None:
            return f"{self.key} refused ({self.refusal.code}: {self.refusal.reason})"
        if self.distance is None or self.tolerance is None:
            return f"{self.key} withheld by the profile"
        state = "" if self.within else " (outside)"
        return (
            f"{self.key} distance {display(self.distance)} "
            f"(tolerance {display(self.tolerance)}){state}"
        )

    def to_document(self) -> Dict[str, Any]:
        """The histogram as the result document records it: exact decimals, no value."""
        document: Dict[str, Any] = {
            "histogram": self.key,
            "outcome": self.outcome,
            "records": self.records,
        }
        if self.distance is not None and self.tolerance is not None:
            document["distance"] = render_exact(self.distance)
            document["tolerance"] = render_exact(self.tolerance)
        if self.widening is not None:
            document["widening"] = render_exact(self.widening)
        if self.refusal is not None:
            document["refusal"] = {"code": self.refusal.code, "reason": self.refusal.reason}
        return document


@dataclass(frozen=True)
class CalendarLine:
    """The line ``calendar:<column>`` of one emit: each published histogram of the column, compared."""

    expose_id: str
    column: str
    parts: Tuple[CalendarPart, ...]
    #: Whether the line can fail the run. A line is gated unless the bundle shapes no calendar for
    #: its column (no ``rates`` entry names it); an ungated line is still computed and printed.
    gated: bool = True

    @property
    def name(self) -> str:
        """``calendar:<column>``."""
        return line_name(self.column)

    @property
    def failed(self) -> bool:
        """Whether the line fails the run: outside its tolerance, on a column the bundle shapes."""
        return self.gated and not self.within

    @property
    def within(self) -> bool:
        """Whether the line passes: none refused, every scored histogram within, and one scored.

        As an emit's comparison with the profile is within: a censored histogram is left out, but a
        line whose every histogram was censored scored nothing and is not within.
        """
        scored = [part for part in self.parts if not part.censored]
        return all(part.within for part in scored) and bool(scored)

    def line(self) -> str:
        """The line a person reads: its name and emit, whether it passed, and each histogram's distance."""
        if self.within:
            state = "passed"
        else:
            state = "failed" if self.gated else f"outside, {NOT_GATED_REASON}"
        detail = "; ".join(part.text() for part in self.parts)
        return f"{self.name} ({self.expose_id}): {state} -- {detail}"

    def to_document(self) -> Dict[str, Any]:
        """The line as the result document records it."""
        document: Dict[str, Any] = {
            "line": self.name,
            "emit": self.expose_id,
            "outcome": WITHIN if self.within else OUTSIDE,
            "histograms": [part.to_document() for part in self.parts],
        }
        if not self.gated:
            document["gated"] = False
        return document


@dataclass(frozen=True)
class CalendarChecks:
    """Every calendar line of a run, and what could not be scored (each ``(name, reason)``)."""

    lines: Tuple[CalendarLine, ...] = ()
    not_scored: Tuple[Tuple[str, str], ...] = ()

    @property
    def empty(self) -> bool:
        """Whether the profile asked for nothing: no line and nothing left unscored."""
        return not (self.lines or self.not_scored)

    def failed(self) -> List[CalendarLine]:
        """The lines that fail the stage: outside their tolerance, on a column the bundle shapes."""
        return [line for line in self.lines if line.failed]

    def failures(self) -> List[str]:
        """A gated line outside its tolerance, one entry each: each fails the stage."""
        return [line.line() for line in self.failed()]

    def gating(self, shaped: Mapping[str, FrozenSet[str]]) -> "CalendarChecks":
        """These lines, each gated only when *shaped* lists its column under its emit.

        *shaped* maps an ``exposeId`` to the columns a ``rates`` entry of the bundle names (ADR 0025
        section 2). A calendar of any other column is computed and printed as it was, but cannot
        fail the run: nothing in the bundle shapes it.
        """
        return CalendarChecks(
            lines=tuple(
                replace(line, gated=line.column in shaped.get(line.expose_id, frozenset()))
                for line in self.lines
            ),
            not_scored=self.not_scored,
        )

    def text(self) -> List[str]:
        """The lines under the legs: each line, then each fact left unscored, as the date facts print one."""
        return [f"  {line.line()}" for line in self.lines] + [
            f"  facts: not scored -- {name}: {reason}" for name, reason in self.not_scored
        ]

    def artifact(self) -> Dict[str, Any]:
        """The ``calendar`` key of the result: nothing for a run whose profile publishes none."""
        if self.empty:
            return {}
        document: Dict[str, Any] = {
            "met": not self.failed(),
            "lines": [line.to_document() for line in self.lines],
        }
        if self.not_scored:
            document["notScored"] = [
                {"fact": name, "reason": reason} for name, reason in self.not_scored
            ]
        return {"calendar": document}

    def merged(self, other: "CalendarChecks") -> "CalendarChecks":
        """These and *other*'s, in the order of the emits and of the profile's entries."""
        return CalendarChecks(
            lines=self.lines + other.lines,
            not_scored=tuple(sorted(set(self.not_scored + other.not_scored))),
        )


def _calendar_document(
    column: str, part: CalendarPart, outcome: str, score: Optional[Fraction]
) -> Dict[str, Any]:
    """The row document of one histogram: counts and exact decimals, and no value of the data.

    A histogram with no *score* carries none of the score, the distance or the tolerance, and a
    refusal is listed with its code and reason.
    """
    document: Dict[str, Any] = {
        "propertyId": f"{CALENDAR}:{column}.{part.key}",
        "property": CALENDAR,
        "column": column,
        "histogram": part.key,
        "dimension": "temporal",
        "outcome": outcome,
        "score": None if score is None else render_exact(score),
        "weight": "1",
        "records": part.records,
        "distance": None if score is None or part.distance is None else render_exact(part.distance),
        "tolerance": (
            None if score is None or part.tolerance is None else render_exact(part.tolerance)
        ),
        "refusals": (
            [{"code": part.refusal.code, "reason": part.refusal.reason}]
            if part.refusal is not None
            else []
        ),
    }
    if score is not None and part.widening is not None:
        document["widening"] = render_exact(part.widening)
    return document


def calendar_row(expose_id: str, column: str, part: CalendarPart) -> PropertyScore:
    """One published histogram of a calendar as its row in the ``temporal`` dimension of its table.

    The score is one minus the total variation distance the line compared, exact, and the row is
    ``within`` or ``outside`` as the line's histogram is. A histogram the profile withheld is
    ``censored``, and one the comparison refused is ``refused``: neither has a score. The row holds
    counts and exact decimals and no value of the data.
    """
    refused = part.refusal is not None
    scored = part.distance is not None and part.tolerance is not None and not refused
    score = (1 - part.distance) if scored and part.distance is not None else None
    outcome = "refused" if refused else (CENSORED if score is None else part.outcome)
    document = _calendar_document(column, part, outcome, score)
    return PropertyScore(
        expose_id=expose_id,
        property_id=str(document["propertyId"]),
        kind=CALENDAR,
        statistic="",
        columns=(),
        group_by=None,
        dimension="temporal",
        outcome=outcome,
        score=score,
        groups_scored=0,
        groups_censored=0,
        groups_outside=(),
        tolerance=(
            {"type": "distance", "value": render_exact(part.tolerance)}
            if score is not None and part.tolerance is not None
            else {}
        ),
        margin=None,
        refusals=(part.refusal,) if part.refusal is not None else (),
        document=document,
        label=f"calendar {part.key} of {column}",
    )


def calendar_rows(checks: Optional[CalendarChecks]) -> Mapping[str, Sequence[PropertyScore]]:
    """Every scored histogram of *checks* as a ``temporal`` row, by the ``exposeId`` of its table.

    In the order of the lines and of each line's histograms. A run whose profile publishes no
    calendar gives none; what a line lists as not scored (a hidden column, an absent one, an hour
    of a DATE) is not a row.
    """
    found: Dict[str, List[PropertyScore]] = {}
    for line in checks.lines if checks is not None else ():
        for part in line.parts:
            found.setdefault(line.expose_id, []).append(
                calendar_row(line.expose_id, line.column, part)
            )
    return found


def _compare(key: str, entry: StatisticEntry, counts: Tuple[int, ...], where: str) -> CalendarPart:
    """One histogram of the profile against the run's counts, as a ``levelHistogram/1`` column statistic is."""
    records = sum(counts)
    group = entry.groups[0]
    if not group.published or group.statistic is None:
        return CalendarPart(key=key, outcome=CENSORED, records=records)
    synthetic = GroupStatistic(group=group.key, k=records, level_counts=counts)
    check = Check(
        subject=where,
        tolerance="distance",
        margin=CALENDAR_TOLERANCE,
        reference_side="profile",
    )
    try:
        outcome, score, widened, _sampled, _cells = ClearProfile._score(
            LEVEL_HISTOGRAM, check, group, synthetic, where, 1, None, entry.cell_variance
        )
    except FluxError as error:
        return CalendarPart(
            key=key,
            outcome=OUTSIDE,
            records=records,
            refusal=Refusal(code=error.code.code, reason=error.detail, property_id=where),
        )
    if score is None:
        return CalendarPart(key=key, outcome=CENSORED, records=records)
    return CalendarPart(
        key=key,
        outcome=outcome,
        records=records,
        distance=1 - score,
        tolerance=CALENDAR_TOLERANCE + (widened if widened is not None else Fraction(0)),
        widening=widened,
    )


def _held(entry: CalendarEntry) -> List[Tuple[str, StatisticEntry]]:
    found: Dict[str, Optional[StatisticEntry]] = {
        "month": entry.month,
        "dayOfWeek": entry.day_of_week,
        "hourOfWeek": entry.hour_of_week,
    }
    return [(key, held) for key in _KEYS if (held := found[key]) is not None]


def _hidden_reason(expose_id: str, column: str) -> str:
    return f"it reads the hidden column {expose_id}.{column}; {_HIDDEN_TAIL}"


def _unscored_reason(
    expose_id: str,
    column: str,
    type_name: Optional[str],
    hidden_columns: Sequence[str],
    unreadable: Optional[str],
) -> Optional[str]:
    """Why a published calendar column is not scored, or ``None`` when it is.

    A hidden column comes first, then rows that cannot be read, then a column the run did not write,
    then a column that is neither DATE nor TIMESTAMP.
    """
    if column in hidden_columns:
        return _hidden_reason(expose_id, column)
    if unreadable is not None:
        return f"the emit's rows cannot be read: {unreadable}"
    if type_name is None:
        return f"the run's emit {expose_id} holds no column {column}"
    if type_name not in ("DATE", "TIMESTAMP"):
        return f"the column is a {type_name}, not a DATE or a TIMESTAMP"
    return None


def _column_parts(
    entry: CalendarEntry,
    name: str,
    rows: Sequence[Mapping[str, Any]],
    type_name: str,
    unscored: List[Tuple[str, str]],
) -> List[CalendarPart]:
    """The compared histograms of one readable calendar column, in the profile's order.

    The ``hourOfWeek`` of a DATE column is appended to *unscored* instead. A column whose counts the
    run's rows refuse gives each histogram the one refusal.
    """
    refusal: Optional[Refusal] = None
    counts: Dict[str, Tuple[int, ...]] = {}
    try:
        counts = calendar_counts(rows, entry.column, type_name)
    except FluxError as error:
        refusal = Refusal(code=error.code.code, reason=error.detail, property_id=name)
    parts: List[CalendarPart] = []
    for key, statistic in _held(entry):
        if key == "hourOfWeek" and type_name == "DATE":
            unscored.append((f"{name}.{key}", NO_HOUR_REASON))
        elif refusal is not None:
            parts.append(CalendarPart(key=key, outcome=OUTSIDE, records=0, refusal=refusal))
        else:
            parts.append(_compare(key, statistic, counts[key], f"{name}.{key}"))
    return parts


def calendar_for_emit(
    expose_id: str,
    calendar: Sequence[CalendarEntry],
    rows: Optional[Sequence[Mapping[str, Any]]],
    types: Mapping[str, str],
    hidden_columns: Sequence[str] = (),
    unreadable: Optional[str] = None,
) -> CalendarChecks:
    """The lines of one emit's published *calendar*, compared with the run's *rows*.

    *types* maps each column the run wrote to its logical type, *hidden_columns* are the emit's
    hidden columns, and *unreadable* is why *rows* cannot be read, when they cannot (``rows`` is then
    ignored). A column that is hidden, absent, or neither DATE nor TIMESTAMP is listed as not
    scored with the reason; so is a published ``hourOfWeek`` of a DATE column.
    """
    lines: List[CalendarLine] = []
    unscored: List[Tuple[str, str]] = []
    for entry in calendar:
        column = entry.column
        name = line_name(column)
        reason = _unscored_reason(expose_id, column, types.get(column), hidden_columns, unreadable)
        if reason is not None:
            unscored.append((name, reason))
            continue
        assert rows is not None
        parts = _column_parts(entry, name, rows, types[column], unscored)
        if parts:
            lines.append(CalendarLine(expose_id=expose_id, column=column, parts=tuple(parts)))
    return CalendarChecks(lines=tuple(lines), not_scored=tuple(unscored))


# ---------------------------------------------------------------------------
# What the plan says is hidden
# ---------------------------------------------------------------------------


def hidden_in_plan(
    plan_document: Mapping[str, Any],
) -> Tuple[FrozenSet[str], Mapping[str, Tuple[str, ...]]]:
    """The hidden tables of a recorded plan, and each emit's hidden columns.

    Read from the plan's own record of each emit (``hidden``, and ``generator.hiddenColumns``), so
    no bundle is read.
    """
    emits = plan_document.get("emits")
    emits = emits if isinstance(emits, Mapping) else {}
    tables: set[str] = set()
    columns: Dict[str, Tuple[str, ...]] = {}
    for expose_id, entry in emits.items():
        if not isinstance(entry, Mapping):
            continue
        if entry.get("hidden") is True:
            tables.add(str(expose_id))
        generator = entry.get("generator")
        listed = generator.get("hiddenColumns") if isinstance(generator, Mapping) else None
        if isinstance(listed, Sequence) and not isinstance(listed, (str, bytes)) and listed:
            columns[str(expose_id)] = tuple(str(name) for name in listed)
    return frozenset(tables), columns


def hidden_not_scored(profile: Profile, plan_document: Mapping[str, Any]) -> CalendarChecks:
    """The published calendars of hidden tables, each listed as not scored: no stream holds their rows."""
    tables, _ = hidden_in_plan(plan_document)
    unscored: List[Tuple[str, str]] = []
    for expose_id in sorted(tables):
        table = profile.tables.get(expose_id)
        for entry in table.calendar if table is not None else ():
            unscored.append(
                (
                    line_name(entry.column),
                    f"it reads the hidden table {expose_id}; {_HIDDEN_TAIL}",
                )
            )
    return CalendarChecks(not_scored=tuple(unscored))
