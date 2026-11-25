# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The three lines a history adds to its emit's rules, checked on the rows a run wrote (ADR 0017 section 14).

Each line counts entities, never rows: an entity is the natural entity's columns, and it holds the
line when every one of its versions does. Every line is enforced, so one violating entity fails the
run, and none reads a draw: the rows are read as written and nothing is regenerated.

* ``history:no_gaps_no_overlap``: the versions ordered by start, then end. Under ``nextStart`` every
  closed version starts before it ends and ends where the next starts. Under ``dayBefore`` it starts
  no later than it ends and ends the day before the next starts. Only the last version may be open:
  its end is NULL, or the ``openEnd`` sentinel.
* ``history:one_current``: an entity has one open version, its last, unless it ended under
  ``closeLast``, which is allowed only when the entry declares that end, and then it has none and
  its last version carries ``end.value`` when the entry names an ``end.column``.
* ``history:ended_is_last``: with an ``end.column`` and ``end.value``, the value appears only on an
  entity's last version.

:class:`AsOfRows` is the same history read the other way: the rows a rule reads through an as-of
link (ADR 0017 section 8), each reading the version of the linked entity valid on its own date.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.shares import Holds
from flux_engine.expr2.evaluate import key_value
from flux_engine.expr2.rows import MemoryRow, Rows
from flux_engine.expr2.scope import Link, Model, Versions
from flux_engine.seam.asof import AsOfEntry
from flux_engine.seam.history import HistoryEntry

__all__ = [
    "AsOfRows",
    "ENDED_IS_LAST",
    "HISTORY_RULE_IDS",
    "NO_GAPS_NO_OVERLAP",
    "ONE_CURRENT",
    "WITHIN_PARENT",
    "chain_ended",
    "entity_chains",
    "history_shares",
    "is_open",
    "open_sentinel",
    "within_parent_share",
]

NO_GAPS_NO_OVERLAP = "history:no_gaps_no_overlap"
ONE_CURRENT = "history:one_current"
ENDED_IS_LAST = "history:ended_is_last"
WITHIN_PARENT = "history:within_parent"
HISTORY_RULE_IDS = (NO_GAPS_NO_OVERLAP, ONE_CURRENT, ENDED_IS_LAST, WITHIN_PARENT)

_Row = Mapping[str, Any]


def _instant(text: Optional[str], like: Any) -> Any:
    """The ``openEnd`` text as the stored value of a column whose cells look like *like*."""
    if text is None:
        return None
    if isinstance(like, datetime):
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    return date.fromisoformat(text)


def is_open(end: Any, sentinel: Any) -> bool:
    """Whether an end value leaves a version open: NULL, or the ``openEnd`` sentinel."""
    return end is None or (sentinel is not None and end == sentinel)


def _order(rows: Sequence[_Row], versions: Versions, sentinel: Any) -> List[_Row]:
    """An entity's versions by start, then end; an open end sorts after any closed one."""
    end = versions.end

    def key(row: _Row) -> Tuple[bool, Any, bool, Any]:
        stop = row.get(end) if end is not None else None
        start = row.get(versions.start)
        return (start is not None, start or 0, is_open(stop, sentinel), stop or 0)

    return sorted(rows, key=key)


def _joined(chain: str, stop: Any, following: Any) -> bool:
    """Whether a closed version's end meets the next start as the chain mode writes it."""
    if chain == "dayBefore":
        return bool(stop == following - timedelta(days=1))
    return bool(stop == following)


def _well_formed(chain: str, start: Any, stop: Any) -> bool:
    return bool(start <= stop) if chain == "dayBefore" else bool(start < stop)


def _version_holds(
    row: _Row, following: Optional[_Row], versions: Versions, chain: str, sentinel: Any
) -> bool:
    """Whether one version is well formed and, unless it is last, meets *following*."""
    if versions.end is None:
        return False
    start, stop = row.get(versions.start), row.get(versions.end)
    if start is None:
        return False
    if is_open(stop, sentinel):
        return following is None
    if not _well_formed(chain, start, stop):
        return False
    if following is None:
        return True
    next_start = following.get(versions.start)
    return next_start is not None and _joined(chain, stop, next_start)


def _chain_holds(ordered: Sequence[_Row], versions: Versions, chain: str, sentinel: Any) -> bool:
    """Whether one entity's ordered versions leave no gap, no overlap and no empty version."""
    if versions.end is None:
        return False
    last = len(ordered) - 1
    return all(
        _version_holds(row, ordered[index + 1] if index < last else None, versions, chain, sentinel)
        for index, row in enumerate(ordered)
    )


def _carries(row: _Row, entry: HistoryEntry) -> bool:
    end = entry.end
    if end is None or end.column is None or end.value is None:
        return False
    cell = row.get(end.column)
    return cell is not None and str(cell) == end.value


def _current_holds(
    ordered: Sequence[_Row], versions: Versions, entry: HistoryEntry, sentinel: Any
) -> bool:
    """Whether one entity has the open versions its end mode allows."""
    if versions.end is None:
        return False
    opened = [
        index for index, row in enumerate(ordered) if is_open(row.get(versions.end), sentinel)
    ]
    if opened == [len(ordered) - 1]:
        return True
    if opened:
        return False
    if entry.end is None:
        # A history that keeps to its parent's lifetime closes the last version of an entity
        # whose parent ended, though it declares no end (ADR 0017, amendment of 2026-11-24).
        return bool(entry.parent is not None and entry.parent.lifetime)
    if entry.end.mode != "closeLast":
        return False
    return entry.end.column is None or _carries(ordered[-1], entry)


def _ended_holds(ordered: Sequence[_Row], entry: HistoryEntry) -> bool:
    """Whether the end value appears on no version but the last."""
    return not any(_carries(row, entry) for row in ordered[:-1])


def open_sentinel(entry: HistoryEntry, rows: Sequence[_Row], versions: Versions) -> Any:
    """The ``openEnd`` sentinel of *entry* as the stored value its start column's cells have, or ``None``."""
    sample = next((row.get(versions.start) for row in rows if row.get(versions.start)), None)
    return _instant(entry.open_end, sample)


def chain_ended(
    ordered: Sequence[_Row], versions: Versions, entry: HistoryEntry, sentinel: Any
) -> bool:
    """Whether the history ended an entity whose versions are *ordered* (ADR 0017 section 5).

    Under ``closeLast`` its last version is closed; under ``finalVersion`` its last version carries
    the end value.
    """
    end = entry.end
    if end is None:
        bound = entry.parent is not None and entry.parent.lifetime
        return bool(
            bound
            and ordered
            and versions.end is not None
            and not is_open(ordered[-1].get(versions.end), sentinel)
        )
    if not ordered:
        return False
    if end.mode == "closeLast":
        return versions.end is not None and not is_open(ordered[-1].get(versions.end), sentinel)
    return _carries(ordered[-1], entry)


def entity_chains(
    rows: Sequence[_Row], versions: Versions, sentinel: Any
) -> Dict[Tuple[Any, ...], List[_Row]]:
    """Each natural entity's version rows, ordered by start, then end."""
    return {
        key: _order(group, versions, sentinel) for key, group in _entities(rows, versions).items()
    }


def _entities(rows: Sequence[_Row], versions: Versions) -> Dict[Tuple[Any, ...], List[_Row]]:
    grouped: Dict[Tuple[Any, ...], List[_Row]] = {}
    for row in rows:
        grouped.setdefault(tuple(row.get(column) for column in versions.entity), []).append(row)
    return grouped


def history_shares(
    entry: HistoryEntry, rows: Sequence[_Row], versions: Versions
) -> Tuple[Tuple[str, Holds], ...]:
    """The lines of *entry*'s history on the written *rows*: each rule's id and the entities that hold it.

    *rows* are the emit's stored rows (dates and instants as values, as the evaluator reads them) and
    *versions* the emit's natural entity and validity window as the contract declares them. The
    third line is present only when the entry names both ``end.column`` and ``end.value``.
    """
    grouped = _entities(rows, versions)
    sentinel = open_sentinel(entry, rows, versions)
    ordered = {key: _order(group, versions, sentinel) for key, group in grouped.items()}
    total = len(ordered)
    gaps = sum(
        1 for chain in ordered.values() if _chain_holds(chain, versions, entry.chain, sentinel)
    )
    current = sum(
        1 for chain in ordered.values() if _current_holds(chain, versions, entry, sentinel)
    )
    lines = [(NO_GAPS_NO_OVERLAP, Holds(gaps, total)), (ONE_CURRENT, Holds(current, total))]
    if entry.end is not None and entry.end.column is not None and entry.end.value is not None:
        held = sum(1 for chain in ordered.values() if _ended_holds(chain, entry))
        lines.append((ENDED_IS_LAST, Holds(held, total)))
    return tuple(lines)


def _facts() -> Any:
    """The date facts, imported when first read: ``date_facts`` imports ``rule_shares``, which imports this module."""
    from flux_engine.concordance import date_facts

    return date_facts


def _last_instant(end: Any, like: Any) -> Any:
    """A DATE lifetime *end* as the last millisecond of its day when *like* is a timestamp.

    The generator forces an entity to end when its parent's last day, to the last millisecond, is
    not after ``until``; the day alone would call a parent that ends on ``until``'s day an ended one
    whatever the time of day ``until`` falls at.
    """
    if isinstance(like, datetime) and not isinstance(end, datetime):
        return datetime.combine(end, time(23, 59, 59, 999000), like.tzinfo)
    return end


def _within_parent_holds(
    ordered: Sequence[_Row],
    parent: _Row,
    lifetime: Tuple[str, Optional[str]],
    versions: Versions,
    entry: HistoryEntry,
    sentinel: Any,
) -> Optional[bool]:
    """Whether one entity keeps to its parent's lifetime, or ``None`` when its parent has no start.

    Its first start is not before the parent's start. When the parent has ended (an end not before
    its start), no version starts after the parent's last day, every closed end is at or before it,
    and an entity whose parent ended by ``until`` has ended: its last version is closed, or under
    ``finalVersion`` carries the end value. Any other entity of a history with no ``end`` keeps its
    last version open: nothing but the parent's end closes it.
    """
    start = parent.get(lifetime[0])
    if start is None or versions.end is None:
        return None
    first = ordered[0].get(versions.start)
    if first is None or not _facts().at_or_after_start(first, start):
        return False
    stop = None if lifetime[1] is None else parent.get(lifetime[1])
    if stop is None or _facts().is_open(start, stop):
        return _open_last_kept(ordered, versions, entry, sentinel)
    if not _ends_by(ordered, (versions.start, versions.end), stop, sentinel):
        return False
    until = _instant(entry.until, first)
    if until is not None and not _facts().at_or_before_end(_last_instant(stop, until), until):
        # The parent ended after the horizon: nothing forces the entity to end.
        return _open_last_kept(ordered, versions, entry, sentinel)
    return chain_ended(ordered, versions, entry, sentinel)


def _open_last_kept(
    ordered: Sequence[_Row], versions: Versions, entry: HistoryEntry, sentinel: Any
) -> bool:
    """Whether an entity nothing forces to end keeps its last version open, when the history has no ``end``.

    A history with no ``end`` closes a last version only to end with its parent, when the parent's
    last day is at most ``until``; with an ``end`` declared the history's own end mode may close it.
    """
    if entry.end is not None or versions.end is None:
        return True
    return is_open(ordered[-1].get(versions.end), sentinel)


def _ends_by(ordered: Sequence[_Row], columns: Tuple[str, str], stop: Any, sentinel: Any) -> bool:
    """Whether no version starts after *stop* and every closed end is at or before it."""
    for row in ordered:
        begun, closed = row.get(columns[0]), row.get(columns[1])
        if begun is None or not _facts().at_or_before_end(begun, stop):
            return False
        if not is_open(closed, sentinel) and not _facts().at_or_before_end(closed, stop):
            return False
    return True


def within_parent_share(
    entry: HistoryEntry,
    rows: Sequence[_Row],
    versions: Versions,
    parents: Mapping[Tuple[Any, ...], _Row],
    key: Sequence[str],
    lifetime: Tuple[str, Optional[str]],
) -> Holds:
    """``history:within_parent``: the entities that keep to their parent's lifetime (ADR 0017, amendment of 2026-11-24).

    *parents* are the parent's rows by the columns they are joined on, *key* the columns of an
    entity's versions that name its parent, and *lifetime* the parent's start and end columns.
    An entity whose parent has no start, or is not reached, is not covered.
    """
    sentinel = open_sentinel(entry, rows, versions)
    covered = held = 0
    for chain in entity_chains(rows, versions, sentinel).values():
        point = tuple(chain[0].get(column) for column in key)
        parent = parents.get(point)
        if parent is None or None in point:
            continue
        found = _within_parent_holds(chain, parent, lifetime, versions, entry, sentinel)
        if found is None:
            continue
        covered += 1
        held += bool(found)
    return Holds(held, covered)


# ---------------------------------------------------------------------------
# The as-of join (ADR 0017 section 8)
# ---------------------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MILLISECOND = timedelta(milliseconds=1)


def moment_ms(value: Any) -> int:
    """A stored DATE or TIMESTAMP value as milliseconds since the epoch; a DATE is its midnight UTC."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int((moment - _EPOCH) // _MILLISECOND)
    return (value.toordinal() - date(1970, 1, 1).toordinal()) * 86_400_000


class _Versions:
    """One entity's versions by start: each start and the first instant it no longer covers, in milliseconds."""

    __slots__ = ("ends", "positions", "starts")

    def __init__(self, found: Sequence[Tuple[int, Optional[int], int]]) -> None:
        ordered = sorted(found, key=lambda item: item[0])
        self.starts = [start for start, _end, _position in ordered]
        self.ends = [end for _start, end, _position in ordered]
        self.positions = [position for _start, _end, position in ordered]

    def valid_on(self, moment: int) -> Optional[int]:
        """The position of the version valid at *moment*, or ``None``."""
        found = bisect_right(self.starts, moment) - 1
        if found < 0:
            return None
        end = self.ends[found]
        return self.positions[found] if end is None or moment < end else None


class AsOfRows(Rows):
    """Rows whose as-of links read the version valid on the row's date, as the generator does.

    *entries* are the as-of entries of the rows' emit and *histories* the history of each emit
    that has one. A link with an entry reaches, among the target rows of its entity, the one whose
    start is not after the date and whose end is open or, under ``nextStart``, after it, or, under
    ``dayBefore``, not before it, a DATE end covering its whole day.
    """

    def __init__(
        self,
        model: Model,
        tables: Mapping[str, Sequence[Mapping[str, Any]]],
        entries: Sequence[AsOfEntry],
        histories: Mapping[str, HistoryEntry],
    ) -> None:
        super().__init__(model, tables)
        self.dates: Dict[Tuple[str, str], str] = {
            (entry.emit, entry.relationship): entry.date for entry in entries
        }
        self._histories = histories
        self._found: Dict[Tuple[str, Tuple[str, ...]], Dict[Tuple[Any, ...], _Versions]] = {}

    def row(self, expose: str, index: int) -> MemoryRow:
        super().row(expose, index)
        return AsOfRow(self, expose, index)

    def rows(self, expose: str) -> List[MemoryRow]:
        return [AsOfRow(self, expose, index) for index in range(len(self.tables[expose]))]

    def key_of(
        self, expose: str, columns: Sequence[str], row: Mapping[str, Any]
    ) -> Tuple[Any, ...]:
        """The key *row* of *expose* holds in *columns*, compared as the evaluator compares."""
        held = self.model.exposes[expose].columns
        return tuple(
            key_value(held[name], row.get(name)) if name in held else row.get(name)
            for name in columns
        )

    def version_of(self, link: Link, key: Tuple[Any, ...], moment: int) -> Optional[int]:
        """The index of the row of *link*'s target that holds *key* and is valid at *moment*."""
        address = (link.target, link.to_columns)
        chains = self._found.get(address)
        if chains is None:
            chains = self._chains(link)
            self._found[address] = chains
        held = chains.get(key)
        return None if held is None else held.valid_on(moment)

    def _chains(self, link: Link) -> Dict[Tuple[Any, ...], _Versions]:
        target = self.model.exposes[link.target]
        versions = target.versions
        entry = self._histories.get(link.target)
        if versions is None or entry is None or versions.end is None:
            return {}
        table = self.tables[link.target]
        sentinel = open_sentinel(entry, table, versions)
        reach = 86_400_000 if entry.chain == "dayBefore" else 0
        grouped: Dict[Tuple[Any, ...], List[Tuple[int, Optional[int], int]]] = {}
        for position, row in enumerate(table):
            start = row.get(versions.start)
            if start is None:
                continue
            stop = row.get(versions.end)
            end = None if is_open(stop, sentinel) else moment_ms(stop) + reach
            key = self.key_of(link.target, link.to_columns, row)
            grouped.setdefault(key, []).append((moment_ms(start), end, position))
        return {key: _Versions(found) for key, found in grouped.items()}

    def misses(self, expose: str, relationship: str) -> int:
        """How many rows of *expose* read no version through *relationship*."""
        return sum(1 for view in self.rows(expose) if view.follow(relationship) is None)


class AsOfRow(MemoryRow):
    """A row of :class:`AsOfRows`: its links with an as-of entry read the version valid on its date."""

    __slots__ = ()

    def _wrap(self, row: Optional[MemoryRow]) -> Optional["AsOfRow"]:
        return None if row is None else AsOfRow(self._rows, row.expose, row.index)

    def follow(self, relationship: str) -> Optional[MemoryRow]:
        rows = self._rows
        assert isinstance(rows, AsOfRows)
        date_column = rows.dates.get((self.expose, relationship))
        if date_column is None:
            return self._wrap(super().follow(relationship))
        link = self._scope.links[relationship]
        data = self._data
        key = rows.key_of(self.expose, link.from_columns, data)
        moment = data.get(date_column)
        if None in key or moment is None:
            return None
        found = rows.version_of(link, key, moment_ms(moment))
        return None if found is None else AsOfRow(rows, link.target, found)

    def previous(self) -> Optional[MemoryRow]:
        return self._wrap(super().previous())

    def children(self, expose: str, relationship: str) -> List[MemoryRow]:
        return [
            AsOfRow(self._rows, row.expose, row.index)
            for row in super().children(expose, relationship)
        ]

    def siblings(self, relationship: str) -> List[MemoryRow]:
        return [
            AsOfRow(self._rows, row.expose, row.index) for row in super().siblings(relationship)
        ]
