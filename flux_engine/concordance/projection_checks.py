# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The lines a projection adds to its emit's rules, checked on the rows a run wrote (ADR 0027 section 10).

A projected emit is a snapshot, change events or periodic rows of a history, and each line is
enforced, so one violating entity or row fails the run. Nothing here reads a draw: the projected
rows and the history's rows are read as written and nothing is regenerated.

* ``snapshot:equals_last_version``: counts entities. An entity holds the line when exactly one
  snapshot row carries it and that row equals, column for column, the entity's last version, the one
  whose start is latest. Under ``ended: drop`` an ended entity holds it only by being absent.
* ``event:matches_transition``: counts entities. An entity holds the line when its events equal its
  changes as a set, whatever the row order: one ``c`` for its first version, one ``u`` for each later
  version, one ``d`` when it ended (under ``finalVersion`` the change into the final version is the
  ``d``, under ``closeLast`` the close of the last version is), each with ``before`` and ``after`` as
  ADR 0027 section 4 defines them, and every ``date`` not before its ``at``. ``on`` keeps the
  listed ops and ``when`` the changes whose version it holds for.
* ``event:transition_matches``: counts ``u`` events. One holds the line when it moves exactly one
  tracked column along a transition whose weight is above zero, or, as an ending change, moves the
  ``end`` column to its value and nothing else is asked of it. It reads the tracked columns that
  ``before`` and ``after`` both carry, so it is present only when some tracked column is carried. A
  ``u`` that moves no mapped column is excused once for each hidden ending, for each change on a
  tracked column the events do not map, and, when the history declares ``untracked``, for each
  change that moves no tracked column, an ending change not counted among those.
* ``period:within_validity``: counts rows. A periodic row holds the line when its period holds a
  version of its entity under the entry's ``match``: ``overlap`` every version valid at some instant
  of the period, ``start`` the one valid at its first instant, ``end`` the one valid at its last
  instant inside the window. The periods are laid from ``timeBounds.start`` by the grain, a month
  added with ``ADD_MONTHS`` from that anchor and never from the period before, the window's end
  excluded. An open version is valid up to the window's end, and a closed one up to its end under
  ``nextStart`` or through its end's day under ``dayBefore``.
* ``period:matches_valid_version``: counts rows. A periodic row holds the line when its columns that
  name a history column, other than the period column and a kind's overrides, equal one version
  that its period holds.
"""

from __future__ import annotations

from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.concordance.history_checks import (
    chain_ended,
    entity_chains,
    is_open,
    open_sentinel,
)
from flux_engine.concordance.shares import Holds
from flux_engine.expr2.functions import add_months
from flux_engine.expr2.scope import Model, Versions
from flux_engine.seam.history import HistoryEntry, history_entries_of
from flux_engine.seam.projections import (
    PROJECTS_KEY,
    EventsEntry,
    PeriodsEntry,
    SnapshotEntry,
    events_entry_of,
    periods_entry_of,
    snapshot_entry_of,
)

__all__ = [
    "EVENT_MATCHES_TRANSITION",
    "EVENT_TRANSITION_MATCHES",
    "PERIOD_MATCHES_VALID_VERSION",
    "PERIOD_WITHIN_VALIDITY",
    "SNAPSHOT_EQUALS_LAST_VERSION",
    "ProjectionEntry",
    "entity_columns",
    "events_shares",
    "periods_shares",
    "projected_history",
    "projection_entry_of",
    "projection_shares",
    "window_of",
    "snapshot_shares",
]

SNAPSHOT_EQUALS_LAST_VERSION = "snapshot:equals_last_version"
EVENT_MATCHES_TRANSITION = "event:matches_transition"
EVENT_TRANSITION_MATCHES = "event:transition_matches"
PERIOD_WITHIN_VALIDITY = "period:within_validity"
PERIOD_MATCHES_VALID_VERSION = "period:matches_valid_version"

ProjectionEntry = Union[SnapshotEntry, EventsEntry, PeriodsEntry]

_Row = Mapping[str, Any]
#: The window's start and end as naive UTC instants.
_Window = Tuple[datetime, datetime]
#: A condition over one history row, the version an event is about.
Covers = Callable[[_Row], bool]


def projection_entry_of(
    extension: Optional[Mapping[str, Any]], emit: str
) -> Optional[ProjectionEntry]:
    """The projection an emit's bound extension holds (the member the bundle bound), or ``None``."""
    if extension is None:
        return None
    snapshot = snapshot_entry_of(extension, emit)
    if snapshot is not None:
        return snapshot
    events = events_entry_of(extension, emit)
    if events is not None:
        return events
    return periods_entry_of(extension, emit)


def projected_history(extension: Optional[Mapping[str, Any]], of: str) -> Optional[HistoryEntry]:
    """The history entry the ``projects`` member of an emit's extension holds for *of*, or ``None``."""
    member = extension.get(PROJECTS_KEY) if extension is not None else None
    held = member.get("entry") if isinstance(member, Mapping) else None
    found = history_entries_of(held if isinstance(held, Mapping) else None, of)
    return found[0] if found else None


def _grouped(rows: Sequence[_Row], columns: Sequence[str]) -> Dict[Tuple[Any, ...], List[_Row]]:
    grouped: Dict[Tuple[Any, ...], List[_Row]] = {}
    for row in rows:
        grouped.setdefault(tuple(row.get(column) for column in columns), []).append(row)
    return grouped


def _equals(row: _Row, version: _Row) -> bool:
    """Whether *row* equals *version* on every column the two share."""
    return all(row[column] == version[column] for column in row if column in version)


def projection_shares(
    entry: ProjectionEntry,
    history: HistoryEntry,
    rows: Sequence[_Row],
    history_rows: Sequence[_Row],
    versions: Versions,
    entity: Sequence[str] = (),
    covers: Optional[Covers] = None,
    window: Optional[_Window] = None,
) -> Tuple[Tuple[str, Holds], ...]:
    """The lines of *entry*'s projection on the written *rows*, each rule's id and the share that holds it.

    *entity* is the projected emit's columns that hold the history's natural entity, in its order
    (the snapshot reads the history's own names), *covers* the entry's ``when`` as a condition
    over a history row and *window* the Simulation's ``timeBounds`` (:func:`window_of`).
    """
    if isinstance(entry, SnapshotEntry):
        return snapshot_shares(entry, history, rows, history_rows, versions)
    if isinstance(entry, PeriodsEntry):
        return periods_shares(entry, history, rows, history_rows, versions, entity, window)
    return events_shares(entry, history, rows, history_rows, versions, entity, covers)


def entity_columns(
    model: Model, emit: str, of: str, versions: Versions
) -> Optional[Tuple[str, ...]]:
    """The columns of *emit* that hold the natural entity of *of*, or ``None`` when it holds none.

    A to-one relationship to *of* whose ``toColumns`` are the entity names them; without one the
    columns of the same names do.
    """
    held = model.exposes[emit]
    for link in held.links.values():
        if link.target == of and set(link.to_columns) == set(versions.entity):
            named = dict(zip(link.to_columns, link.from_columns, strict=True))
            return tuple(named[column] for column in versions.entity)
    if all(column in held.columns for column in versions.entity):
        return tuple(versions.entity)
    return None


def snapshot_shares(
    entry: SnapshotEntry,
    history: HistoryEntry,
    rows: Sequence[_Row],
    history_rows: Sequence[_Row],
    versions: Versions,
) -> Tuple[Tuple[str, Holds], ...]:
    """The line of a snapshot on its written *rows*: its id and the entities that hold it.

    *history_rows* are the history emit's stored rows and *versions* its natural entity and
    validity window. An entity that only the snapshot names, or an expected one it lacks, counts
    as one that fails.
    """
    sentinel = open_sentinel(history, history_rows, versions)
    chains = entity_chains(history_rows, versions, sentinel)
    expected = {
        key: chain[-1]
        for key, chain in chains.items()
        if not (entry.ended == "drop" and chain_ended(chain, versions, history, sentinel))
    }
    seen = _grouped(rows, versions.entity)
    keys = set(expected) | set(seen)
    held = sum(
        1
        for key in keys
        if key in expected and len(seen.get(key, ())) == 1 and _equals(seen[key][0], expected[key])
    )
    return ((SNAPSHOT_EQUALS_LAST_VERSION, Holds(held, len(keys))),)


# ---------------------------------------------------------------------------
# Change events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Change:
    """One change of an entity: its op, the version it is about, the images and its instant."""

    op: str
    about: _Row
    before: Optional[_Row]
    after: Optional[_Row]
    instant: Any


def _moment(value: Any) -> Optional[datetime]:
    """A DATE or TIMESTAMP value as a naive UTC instant, so the two kinds compare."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return None


def _changes(
    chain: Sequence[_Row], versions: Versions, history: HistoryEntry, ended: bool
) -> List[_Change]:
    """The changes of one entity whose versions are *chain* (ADR 0027 section 4), before ``on`` and ``when``."""
    last = len(chain) - 1
    final = ended and history.end is not None and history.end.mode == "finalVersion"
    found: List[_Change] = []
    for index, version in enumerate(chain):
        start = version.get(versions.start)
        if index == 0:
            found.append(_Change("c", version, None, version, start))
        elif final and index == last:
            found.append(_Change("d", version, chain[index - 1], None, start))
        else:
            found.append(_Change("u", version, chain[index - 1], version, start))
    if ended and not final and versions.end is not None:
        closed = chain[-1]
        found.append(_Change("d", closed, closed, None, closed.get(versions.end)))
    return found


def _image(row: Optional[_Row], mapped: Sequence[Tuple[str, str]]) -> Tuple[Any, ...]:
    """The values of the mapped history columns on *row*; NULL on every column when there is none."""
    return tuple(None if row is None else row.get(column) for _, column in mapped)


def _event_op(
    row: _Row,
    entry: EventsEntry,
    before: Sequence[Tuple[str, str]],
    after: Sequence[Tuple[str, str]],
) -> Optional[str]:
    """The op an event row carries: its ``op`` column, else read from which image it leaves NULL."""
    if entry.op is not None:
        cell = row.get(entry.op)
        return None if cell is None else str(cell)
    if before and all(row.get(name) is None for name, _ in before):
        return "c"
    if after and all(row.get(name) is None for name, _ in after):
        return "d"
    return "u" if before or after else None


def _event_key(
    row: _Row,
    op: str,
    entry: EventsEntry,
    before: Sequence[Tuple[str, str]],
    after: Sequence[Tuple[str, str]],
    grain: Optional[Any],
) -> Tuple[Any, ...]:
    """What identifies an event among its entity's: its op, its instant when ``at`` is named, its images."""
    instant = None
    if entry.at is not None:
        instant = _truncated(_moment(row.get(entry.at)), grain)
    return (
        op,
        instant,
        tuple(row.get(name) for name, _ in before),
        tuple(row.get(name) for name, _ in after),
    )


def _truncated(moment: Optional[datetime], grain: Optional[Any]) -> Optional[datetime]:
    """*moment* cut to a day when the ``at`` column's cells are dates."""
    if moment is None or not isinstance(grain, date) or isinstance(grain, datetime):
        return moment
    return datetime(moment.year, moment.month, moment.day)


def _expected_events(
    entry: EventsEntry,
    changes: Sequence[_Change],
    covers: Optional[Covers],
    before: Sequence[Tuple[str, str]],
    after: Sequence[Tuple[str, str]],
    grain: Optional[Any],
) -> List[Tuple[Tuple[Any, ...], Optional[datetime]]]:
    """The keys of the events one entity's changes make, each with its instant, after ``on`` and ``when``."""
    kept = [
        change
        for change in changes
        if change.op in entry.on and (covers is None or covers(change.about))
    ]
    return [
        (
            (
                change.op,
                _truncated(_moment(change.instant), grain) if entry.at is not None else None,
                _image(change.before, before),
                _image(change.after, after),
            ),
            _moment(change.instant),
        )
        for change in kept
    ]


def _recorded_in_time(
    entry: EventsEntry,
    rows: Sequence[_Row],
    keys: Sequence[Tuple[Any, ...]],
    instants: Mapping[Tuple[Any, ...], List[Optional[datetime]]],
) -> bool:
    """Whether no event of an entity is recorded (its ``date``) before its ``at``, or before its change."""
    for row, key in zip(rows, keys, strict=True):
        recorded = _moment(row.get(entry.date))
        if recorded is None:
            return False
        if entry.at is not None:
            limit = _moment(row.get(entry.at))
        else:
            known = [value for value in instants.get(key, []) if value is not None]
            limit = min(known) if known else None
        if limit is not None and recorded < limit:
            return False
    return True


def _entity_holds(
    entry: EventsEntry,
    rows: Sequence[_Row],
    expected: Sequence[Tuple[Tuple[Any, ...], Optional[datetime]]],
    keys: Sequence[Tuple[Any, ...]],
) -> bool:
    if Counter(keys) != Counter(key for key, _ in expected):
        return False
    instants: Dict[Tuple[Any, ...], List[Optional[datetime]]] = {}
    for key, instant in expected:
        instants.setdefault(key, []).append(instant)
    return _recorded_in_time(entry, rows, keys, instants)


def _levels(shared: Any) -> str:
    return "" if shared is None else str(shared)


def _weighted(history: HistoryEntry, column: str, moved_from: Any, moved_to: Any) -> bool:
    """Whether *column*'s transitions give the move a weight above zero."""
    for tracked in history.tracked:
        if tracked.column != column:
            continue
        for level, row in tracked.transitions:
            if level == _levels(moved_from):
                return any(
                    name == _levels(moved_to) and Fraction(weight) > 0 for name, weight in row
                )
    return False


def _ending(row: _Row, entry: EventsEntry, history: HistoryEntry, after: Mapping[str, str]) -> bool:
    """Whether a ``u`` row is the ending change: it moves the end column to ``end.value``."""
    end = history.end
    if end is None or end.column is None or end.value is None:
        return False
    named = dict(entry.type_values).get("end")
    if entry.type_column is not None and named is not None:
        if row.get(entry.type_column) == named:
            return True
    column = after.get(end.column)
    return column is not None and row.get(column) is not None and str(row[column]) == end.value


def _moves_along_a_transition(
    row: _Row,
    history: HistoryEntry,
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> Optional[bool]:
    """Whether a ``u`` row moves one mapped tracked column along a transition with weight.

    The history changes one tracked column per version (ADR 0017 section 6), so two mapped columns
    moving fail. A row that moves none returns ``None``: its change is on a column the events do
    not map, or it is a hidden ending change, which the caller decides.
    """
    moved = [
        tracked.column
        for tracked in history.tracked
        if tracked.column in before
        and tracked.column in after
        and row.get(before[tracked.column]) != row.get(after[tracked.column])
    ]
    if not moved:
        return None
    if len(moved) != 1:
        return False
    column = moved[0]
    return _weighted(history, column, row.get(before[column]), row.get(after[column]))


def _ending_hidden(entry: EventsEntry, history: HistoryEntry, after: Mapping[str, str]) -> bool:
    """Whether the events show a ``closeLast`` history's ending change neither by type nor by ``end.column``."""
    end = history.end
    if end is None or end.mode != "closeLast" or end.column is None or end.value is None:
        return False
    named = dict(entry.type_values).get("end")
    return end.column not in after and (entry.type_column is None or named is None)


def _ended_entities(
    history: HistoryEntry, history_rows: Sequence[_Row], versions: Versions
) -> "Counter[Tuple[Any, ...]]":
    """One for each entity whose last version ended under ``closeLast`` with the end value."""
    end = history.end
    found: "Counter[Tuple[Any, ...]]" = Counter()
    if end is None or end.column is None or end.value is None:
        return found
    sentinel = open_sentinel(history, history_rows, versions)
    for key, chain in entity_chains(history_rows, versions, sentinel).items():
        last = chain[-1].get(end.column) if chain else None
        if chain_ended(chain, versions, history, sentinel) and last is not None:
            if str(last) == end.value:
                found[key] += 1
    return found


def _unmapped_changes(
    history: HistoryEntry,
    history_rows: Sequence[_Row],
    versions: Versions,
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> "Counter[Tuple[Any, ...]]":
    """For each entity, the version changes that fall on a tracked column the events do not map."""
    unmapped = [
        tracked.column
        for tracked in history.tracked
        if tracked.column not in before or tracked.column not in after
    ]
    found: "Counter[Tuple[Any, ...]]" = Counter()
    if not unmapped:
        return found
    sentinel = open_sentinel(history, history_rows, versions)
    for key, chain in entity_chains(history_rows, versions, sentinel).items():
        for earlier, later in zip(chain, chain[1:], strict=False):
            if any(earlier.get(column) != later.get(column) for column in unmapped):
                found[key] += 1
    return found


def _unchanged_tracked(
    history: HistoryEntry, history_rows: Sequence[_Row], versions: Versions
) -> "Counter[Tuple[Any, ...]]":
    """For each entity, the version changes that move no tracked column, when the history declares ``untracked``.

    An ending change is not one of them: the last change of an entity that ended with the end value
    is the one the ending budget or the ``d`` counts, so no change is excused twice.
    """
    found: "Counter[Tuple[Any, ...]]" = Counter()
    if history.untracked is None:
        return found
    end = history.end
    sentinel = open_sentinel(history, history_rows, versions)
    for key, chain in entity_chains(history_rows, versions, sentinel).items():
        pairs = list(zip(chain, chain[1:], strict=False))
        if pairs and end is not None and end.column is not None and end.value is not None:
            last = chain[-1].get(end.column)
            if chain_ended(chain, versions, history, sentinel) and str(last) == end.value:
                pairs.pop()
        for earlier, later in pairs:
            if all(earlier.get(t.column) == later.get(t.column) for t in history.tracked):
                found[key] += 1
    return found


@dataclass(frozen=True)
class _History:
    """The history's written rows and the events' entity columns, which the excuses are counted from."""

    rows: Sequence[_Row]
    versions: Versions
    entity: Sequence[str]


class _Transitions:
    """The ``u`` rows of an events table scored against the history's transitions, with each entity's excuses."""

    def __init__(self, entry: EventsEntry, history: HistoryEntry, read: _History) -> None:
        self.entry, self.history, self.read = entry, history, read
        self.before = dict((column, name) for name, column in entry.before)
        self.after = dict((column, name) for name, column in entry.after)
        # A hidden ending change moves no mapped column: one ``u`` for each entity the history ended.
        self.endings: "Counter[Tuple[Any, ...]]" = Counter()
        if _ending_hidden(entry, history, self.after):
            self.endings = _ended_entities(history, read.rows, read.versions)
        self.elsewhere = _unmapped_changes(
            history, read.rows, read.versions, self.before, self.after
        )
        self.still = _unchanged_tracked(history, read.rows, read.versions)

    def maps_a_column(self) -> bool:
        """Whether the events carry at least one tracked column before and after."""
        return any(
            tracked.column in self.before and tracked.column in self.after
            for tracked in self.history.tracked
        )

    def _excused(self, row: _Row) -> bool:
        """Whether a ``u`` that moves no mapped column is an ending, or a change on an unmapped column or none."""
        key = tuple(row.get(column) for column in self.read.entity)
        for budget in (self.endings, self.elsewhere, self.still):
            if budget[key] > 0:
                budget[key] -= 1
                return True
        return False

    def holds(self, row: _Row) -> bool:
        """Whether the ``u`` *row* is the ending change, a weighted move, or an excused move-less change."""
        if _ending(row, self.entry, self.history, self.after):
            return True
        moves = _moves_along_a_transition(row, self.history, self.before, self.after)
        return self._excused(row) if moves is None else moves


def _transition_share(
    entry: EventsEntry,
    history: HistoryEntry,
    rows: Sequence[_Row],
    ops: Sequence[Optional[str]],
    read: _History,
) -> Optional[Holds]:
    transitions = _Transitions(entry, history, read)
    if not transitions.maps_a_column():
        return None
    updates = [row for row, op in zip(rows, ops, strict=True) if op == "u"]
    return Holds(sum(1 for row in updates if transitions.holds(row)), len(updates))


def events_shares(
    entry: EventsEntry,
    history: HistoryEntry,
    rows: Sequence[_Row],
    history_rows: Sequence[_Row],
    versions: Versions,
    entity: Sequence[str],
    covers: Optional[Covers] = None,
) -> Tuple[Tuple[str, Holds], ...]:
    """The lines of a change events emit on its written *rows*: each rule's id and what holds it.

    *entity* is the events' columns that hold the history's natural entity. No line is returned
    when the events carry neither an ``op`` column nor a ``before`` or ``after`` image, since an
    event's op is then unknown, or when the events lack the entity.
    """
    before, after = entry.before, entry.after
    if len(entity) != len(versions.entity):
        return ()
    ops = [_event_op(row, entry, before, after) for row in rows]
    if any(op is None for op in ops):
        return ()
    entities = _EventEntities(entry, history, rows, history_rows, versions, entity, ops)
    lines = [(EVENT_MATCHES_TRANSITION, entities.share(covers))]
    transitions = _transition_share(
        entry, history, rows, ops, _History(history_rows, versions, entity)
    )
    if transitions is not None:
        lines.append((EVENT_TRANSITION_MATCHES, transitions))
    return tuple(lines)


class _EventEntities:
    """The events of a run and the history's chains, grouped by natural entity."""

    def __init__(
        self,
        entry: EventsEntry,
        history: HistoryEntry,
        rows: Sequence[_Row],
        history_rows: Sequence[_Row],
        versions: Versions,
        entity: Sequence[str],
        ops: Sequence[Optional[str]],
    ) -> None:
        self.entry, self.history, self.rows, self.versions = entry, history, rows, versions
        self.sentinel = open_sentinel(history, history_rows, versions)
        self.chains = entity_chains(history_rows, versions, self.sentinel)
        self.grain = next(
            (row.get(entry.at) for row in rows if entry.at and row.get(entry.at)), None
        )
        self.keys = [
            _event_key(row, str(op), entry, entry.before, entry.after, self.grain)
            for row, op in zip(rows, ops, strict=True)
        ]
        self.seen: Dict[Tuple[Any, ...], List[int]] = {}
        for index, row in enumerate(rows):
            self.seen.setdefault(tuple(row.get(column) for column in entity), []).append(index)

    def _holds(self, key: Tuple[Any, ...], covers: Optional[Covers]) -> bool:
        chain = self.chains.get(key)
        if not chain:
            return False
        ended = chain_ended(chain, self.versions, self.history, self.sentinel)
        expected = _expected_events(
            self.entry,
            _changes(chain, self.versions, self.history, ended),
            covers,
            self.entry.before,
            self.entry.after,
            self.grain,
        )
        indices = self.seen.get(key, [])
        return _entity_holds(
            self.entry, [self.rows[i] for i in indices], expected, [self.keys[i] for i in indices]
        )

    def share(self, covers: Optional[Covers]) -> Holds:
        """The entities whose events equal their changes, over every entity either side names."""
        every = set(self.chains) | set(self.seen)
        return Holds(sum(1 for key in every if self._holds(key, covers)), len(every))


# ---------------------------------------------------------------------------
# Periodic rows
# ---------------------------------------------------------------------------

_Interval = Tuple[_Row, datetime, datetime]
_MONTHS = {"month": 1, "quarter": 3, "year": 12}


def window_of(bounds: Optional[Mapping[str, Any]]) -> Optional[_Window]:
    """A Simulation's ``spec.timeBounds`` as naive UTC instants, or ``None`` when it does not give both."""
    if not isinstance(bounds, Mapping):
        return None
    start, end = (_parsed(bounds.get(name)) for name in ("start", "end"))
    return None if start is None or end is None else (start, end)


def _parsed(text: Any) -> Optional[datetime]:
    if not isinstance(text, str):
        return None
    try:
        return _moment(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _period_start(anchor: date, grain: str, position: int) -> date:
    """Period *position* starts here: counted from *anchor*, never from the period before."""
    if grain == "day":
        return anchor + timedelta(days=position)
    if grain == "week":
        return anchor + timedelta(days=7 * position)
    return add_months(anchor, _MONTHS[grain] * position)


def _periods(
    grain: str, window: _Window, from_day: Optional[str] = None, through_day: Optional[str] = None
) -> Dict[datetime, datetime]:
    """Each period's first instant and its end, which the window's end cuts (ADR 0027 section 5).

    The periods start from *from_day* (the window's start day when none) and the last one starts
    on or before *through_day*.
    """
    anchor = date.fromisoformat(from_day) if from_day is not None else window[0].date()
    last = None
    if through_day:
        try:
            last = _moment(date.fromisoformat(through_day) + timedelta(days=1))
        except OverflowError:
            last = None  # the day after the last day of the calendar: no period starts after it
    found: Dict[datetime, datetime] = {}
    position = 0
    while True:
        begins = _start_moment(anchor, grain, position)
        if begins is None or begins >= window[1] or (last is not None and begins >= last):
            return found
        following = _start_moment(anchor, grain, position + 1)
        found[begins] = window[1] if following is None else min(following, window[1])
        position += 1


def _start_moment(anchor: date, grain: str, position: int) -> Optional[datetime]:
    """The instant period *position* starts, or ``None`` for a period past the calendar."""
    with suppress(ValueError, OverflowError):
        return _moment(_period_start(anchor, grain, position))
    return None


def _intervals(
    chain: Sequence[_Row], versions: Versions, history: HistoryEntry, sentinel: Any, stop: datetime
) -> List[_Interval]:
    """Each version of one entity with the instants it is valid over: from its start up to its end, excluded.

    An open version is valid up to the window's end. Under ``dayBefore`` a DATE end covers its whole day.
    """
    found: List[_Interval] = []
    for row in chain:
        begins = _moment(row.get(versions.start))
        closed = None if versions.end is None else row.get(versions.end)
        if begins is None:
            continue
        if closed is None or is_open(closed, sentinel):
            found.append((row, begins, stop))
            continue
        ends = _moment(closed)
        if ends is None:
            continue
        whole = history.chain == "dayBefore" and not isinstance(closed, datetime)
        found.append((row, begins, ends + timedelta(days=1) if whole else ends))
    return found


def _holds(match: str, interval: _Interval, begins: datetime, ends: datetime) -> bool:
    """Whether a version holds the period ``[begins, ends)`` under *match*."""
    _, valid_from, valid_to = interval
    if match == "start":
        return valid_from <= begins < valid_to
    if match == "end":
        return valid_from < ends <= valid_to
    return valid_from < ends and valid_to > begins


class _Held:
    """The versions each periodic row's entity holds in its period, under the entry's ``match``."""

    def __init__(
        self,
        entry: PeriodsEntry,
        history: HistoryEntry,
        history_rows: Sequence[_Row],
        versions: Versions,
        entity: Sequence[str],
        window: _Window,
    ) -> None:
        self.entry, self.history, self.versions, self.entity = entry, history, versions, entity
        self.window = window
        self.sentinel = open_sentinel(history, history_rows, versions)
        self.chains = entity_chains(history_rows, versions, self.sentinel)
        self.grid = _periods(str(entry.grain), window, entry.from_day, entry.through_day)
        skipped = {entry.column} | {name for kind in entry.kinds for name, _ in kind.overrides}
        self.named = set(history_rows[0]) - skipped if history_rows else set()
        self.intervals: Dict[Tuple[Any, ...], List[_Interval]] = {}

    def of(self, row: _Row) -> List[_Interval]:
        """The versions held by *row*'s period; none when the period is not one of the window's."""
        key = tuple(row.get(column) for column in self.entity)
        if key not in self.intervals:
            self.intervals[key] = _intervals(
                self.chains.get(key, []), self.versions, self.history, self.sentinel, self.window[1]
            )
        begins = _moment(row.get(self.entry.column))
        if begins is None or begins not in self.grid:
            return []
        return [
            interval
            for interval in self.intervals[key]
            if _holds(self.entry.match, interval, begins, self.grid[begins])
        ]

    def equals_one(self, row: _Row, held: Sequence[_Interval]) -> bool:
        """Whether *row* equals a held version on every column that names a history column."""
        return any(
            all(row[name] == version[name] for name in self.named if name in row)
            for version, _, _ in held
        )


def periods_shares(
    entry: PeriodsEntry,
    history: HistoryEntry,
    rows: Sequence[_Row],
    history_rows: Sequence[_Row],
    versions: Versions,
    entity: Sequence[str],
    window: Optional[_Window],
) -> Tuple[Tuple[str, Holds], ...]:
    """The lines of a periodic emit on its written *rows*: each rule's id and the rows that hold it.

    No line is returned without a ``grain`` on the entry, a window, or the entity's columns, since
    the periods cannot be laid without them.
    """
    if entry.grain is None or window is None or len(entity) != len(versions.entity):
        return ()
    reader = _Held(entry, history, history_rows, versions, entity, window)
    inside = matching = 0
    for row in rows:
        held = reader.of(row)
        inside += bool(held)
        matching += reader.equals_one(row, held)
    return (
        (PERIOD_WITHIN_VALIDITY, Holds(inside, len(rows))),
        (PERIOD_MATCHES_VALID_VERSION, Holds(matching, len(rows))),
    )
