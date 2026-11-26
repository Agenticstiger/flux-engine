# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``snapshots``, ``events`` and ``periods`` entries ``fluxsim init`` writes from the contract (ADR 0027 section 9).

For each versioned expose that ``init`` gives a history, :func:`project` reads the contract's other
exposes, the ones without a validity window:

* **a snapshot** when the expose's primary entity is the history's natural entity and every column
  it declares is a column of the history's expose, of the same name and type;
* **change events** when it has a to-one relationship whose ``toColumns`` are the natural entity, a
  column whose declared levels lie within ``c``, ``u`` and ``d``, and a ``defaultAggTimeDimension``;
  ``before_<c>`` and ``after_<c>`` map each history column ``<c>`` that the expose declares;
* **periodic rows** when it has such a relationship, exactly one DATE dimension that declares a
  ``timeGranularity``, and no column of those levels. Its primary key must be a whole-number column
  (ADR 0027 section 5).

An expose that meets part of a rule is not projected: it is recorded as unused, with the part it
misses (ADR 0014). A projected emit declares no ``rows``, since its rows follow from the history.
A profile publishes nothing about a projection, so no lag, no rows per entity and one row per pair
are written. A periods entry reads one thing of its emit's profile table: when the period column
publishes its histogram, the months it covers bound the periods (``from`` and ``through``). A
profile's count of an events or periodic emit's rows per entity follows from the history, so it is
unused, and :func:`say_per_entity` gives the reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.author.basis import Label, assumed
from flux_engine.author.columns import ColumnPlan
from flux_engine.author.history import (
    Published,
    profile_relationship,
    restate_assumptions,
    structural_record,
)
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.expr2.functions import add_months
from flux_engine.seam.contract import CompiledColumn, LogicalKind, compile_expose
from flux_engine.seam.keys import ExposeKeys
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY, SNAPSHOTS_KEY
from flux_stats.definitions import MARGINAL_HISTOGRAM

__all__ = [
    "HistoryTarget",
    "Projected",
    "ProjectionSource",
    "follow_history",
    "history_target",
    "project",
    "published_period_range",
    "release_counts",
    "say_per_entity",
    "span_primary_key",
]

_OPS = frozenset({"c", "u", "d"})
_INSTANTS = (LogicalKind.DATE, LogicalKind.TIMESTAMP)
_GRAINS = ("day", "week", "month", "quarter", "year")

#: What a note says of a projection that is written.
_ROWS_REASON = (
    "the emit is projected from a history, so its rows follow from the history and the "
    "published rowCount is not read"
)


@dataclass(frozen=True)
class HistoryTarget:
    """One expose ``init`` gave a ``history``: its natural entity and its columns, by name."""

    expose_id: str
    natural: Tuple[str, ...]
    columns: Mapping[str, CompiledColumn]
    #: The exposeId the history's ``parent`` is laid out under, ``None`` when it writes no ``parent``.
    parent: Optional[str] = None
    #: The history's ``until``, the horizon its versions start before, ``None`` when it writes none.
    until: Optional[str] = None


@dataclass(frozen=True)
class ProjectionSource:
    """What :func:`project` reads of one expose that has no validity window.

    ``keys`` is the expose's keys, ``counts`` the ``counts`` entries written over its
    relationships. A projected emit takes none: :func:`release_counts` gives the statistic to the
    history's parent.
    """

    expose_id: str
    contract_expose: Mapping[str, Any]
    keys: ExposeKeys
    counts: Tuple[Mapping[str, Any], ...] = ()
    #: The expose's profile table, ``None`` when the profile has none.
    table: Optional[Mapping[str, Any]] = None
    #: The run's ``timeBounds`` as RFC 3339 text, ``None`` when no periods entry may read the table.
    window: Optional[Tuple[str, str]] = None


@dataclass
class Projected:
    """The projections written, and what is said of the exposes that meet only part of a rule."""

    snapshots: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    periods: List[Dict[str, Any]] = field(default_factory=list)
    #: Each projected emit, with the reason its published ``rowCount`` is not read.
    emits: Dict[str, str] = field(default_factory=dict)
    #: Subject and sentence, for what is unused and for the columns a mapping leaves out.
    notes: List[Tuple[str, str]] = field(default_factory=list)
    #: Each projected emit with the history it follows and the columns that hold that history's
    #: values, which the run does not draw from the emit's own statistics.
    carried: Dict[str, Tuple[str, Tuple[str, ...]]] = field(default_factory=dict)
    #: Each ``counts`` entry written over a relationship of an emit that is now projected, by emit.
    released: List[Tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    def declare_in(self, namespace: Dict[str, Any]) -> None:
        """Declare each entry list under its key, in emit order; none declares nothing."""
        for key, entries in (
            (SNAPSHOTS_KEY, self.snapshots),
            (EVENTS_KEY, self.events),
            (PERIODS_KEY, self.periods),
        ):
            if entries:
                namespace[key] = sorted(entries, key=lambda item: str(item["emit"]))


#: One rule of ADR 0027 section 9 read against an expose: ``None`` when the expose is not a
#: candidate for it, else the rule's name and what the expose misses of it (nothing when it is
#: written).
_Verdict = Optional[Tuple[str, List[str]]]


def _primary_columns(keys: ExposeKeys) -> Tuple[str, ...]:
    return tuple(entity.column for entity in keys.entities if entity.kind == "primary")


def _unused(projected: Projected, source: ProjectionSource, rule: str, misses: List[str]) -> None:
    projected.notes.append(
        (
            f"{source.expose_id}: {rule}",
            "unused, the expose meets only part of the rule: " + "; ".join(misses),
        )
    )


def _counted_misses(source: ProjectionSource, target: Optional[HistoryTarget]) -> List[str]:
    """What keeps a ``counts`` entry's foreign key from following the history.

    A projected emit takes the history's value in a column the history holds, so a counted
    relationship's foreign key follows the history only when the history has that column and
    writes a ``parent`` over the same parent expose, whose keys it then holds. Otherwise the
    column would hold the history's drawn values, which are no keys of the parent.
    """
    if target is None:
        return []
    names = {entry.get("relationship") for entry in source.counts}
    found: List[str] = []
    for item in source.keys.relationships:
        if item.name not in names:
            continue
        column = item.from_columns[0]
        if column not in target.columns:
            found.append(
                f"its column {column} is set by a counts entry, which a projected emit "
                f"refuses, and {target.expose_id} has no column of that name to take it from"
            )
        elif target.parent != item.target:
            held = (
                f"writes no parent over {item.target}"
                if target.parent is None
                else f"writes its parent over {target.parent}, not over {item.target}"
            )
            found.append(
                f"its column {column} is set by a counts entry, which a projected emit "
                f"refuses, and {target.expose_id} {held}, so the values it holds in {column} "
                f"are no keys of {item.target}"
            )
    return found


def _snapshot_misses(
    source: ProjectionSource,
    columns: Mapping[str, CompiledColumn],
    target: HistoryTarget,
) -> List[str]:
    """What the expose lacks to be the snapshot of *target*'s history; empty when it is one."""
    misses: List[str] = []
    for name, column in columns.items():
        held = target.columns.get(name)
        if held is None:
            misses.append(f"its column {name} is no column of {target.expose_id}")
        elif str(held.logical_type) != str(column.logical_type):
            misses.append(
                f"its column {name} is {column.logical_type} and {target.expose_id}'s is "
                f"{held.logical_type}"
            )
    return misses + _counted_misses(source, target)


def _as_snapshot(
    source: ProjectionSource,
    columns: Mapping[str, CompiledColumn],
    histories: Mapping[str, HistoryTarget],
    projected: Projected,
) -> _Verdict:
    """A snapshot of the first history whose natural entity is the expose's primary entity."""
    primary = _primary_columns(source.keys)
    verdict: _Verdict = None
    for target in histories.values():
        if primary != target.natural:
            continue
        misses = _snapshot_misses(source, columns, target)
        if not misses:
            projected.snapshots.append(
                {"emit": source.expose_id, "of": target.expose_id, "ended": "keep"}
            )
            projected.carried[source.expose_id] = (target.expose_id, tuple(columns))
            return "snapshots", []
        verdict = verdict or ("snapshots", misses)
    return verdict


_NO_RELATIONSHIP = (
    "it has no to-one relationship, that is no key of the expose, whose toColumns are the "
    "natural entity of an expose with a history"
)


def _followed(held: ExposeKeys, histories: Mapping[str, HistoryTarget]) -> Optional[Any]:
    """The first to-one relationship of *held* that is no key of the expose and reads a history's natural entity."""
    key_sets = {frozenset(key) for key in (*held.keys, *held.instant_keys)}
    for relationship in held.relationships:
        target = histories.get(relationship.target)
        if (
            target is not None
            and relationship.cardinality == "many_to_one"
            and frozenset(relationship.from_columns) not in key_sets
            and tuple(relationship.to_columns) == target.natural
        ):
            return relationship
    return None


def _relationship(
    source: ProjectionSource, histories: Mapping[str, HistoryTarget]
) -> Optional[HistoryTarget]:
    """The history of the first to-one relationship that is no key of the expose and reads its natural entity."""
    relationship = _followed(source.keys, histories)
    return None if relationship is None else histories[relationship.target]


def _default_time(expose: Mapping[str, Any]) -> Optional[str]:
    semantics = expose.get("semantics")
    column = semantics.get("defaultAggTimeDimension") if isinstance(semantics, Mapping) else None
    return column if isinstance(column, str) else None


def _op_columns(columns: Mapping[str, CompiledColumn]) -> List[str]:
    """The columns whose declared levels lie within ``c``, ``u`` and ``d``, in declared order."""
    return [
        name
        for name, column in columns.items()
        if column.enum_values and set(column.enum_values) <= _OPS
    ]


def _mapped(
    columns: Mapping[str, CompiledColumn], target: HistoryTarget, side: str
) -> Tuple[Dict[str, str], List[str]]:
    """``before`` or ``after``: each declared ``<side>_<c>`` for a history column ``<c>``, and what is left out."""
    mapped: Dict[str, str] = {}
    skipped: List[str] = []
    for name, held in target.columns.items():
        column = columns.get(f"{side}_{name}")
        if column is None:
            continue
        if column.required:
            skipped.append(f"{column.name} is required, and an event writes NULL there")
        elif str(column.logical_type) != str(held.logical_type):
            skipped.append(
                f"{column.name} is {column.logical_type} and {target.expose_id}.{name} is "
                f"{held.logical_type}"
            )
        else:
            mapped[column.name] = name
    return mapped, skipped


def _events_misses(
    source: ProjectionSource,
    columns: Mapping[str, CompiledColumn],
    target: Optional[HistoryTarget],
) -> List[str]:
    misses: List[str] = []
    if target is None:
        misses.append(_NO_RELATIONSHIP)
    date = _default_time(source.contract_expose)
    if date is None or date not in columns or columns[date].logical_type.kind not in _INSTANTS:
        misses.append("it declares no defaultAggTimeDimension that is a DATE or TIMESTAMP column")
    return misses + _counted_misses(source, target)


def _events_entry(
    source: ProjectionSource,
    columns: Mapping[str, CompiledColumn],
    op: str,
    target: HistoryTarget,
    projected: Projected,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "emit": source.expose_id,
        "of": target.expose_id,
        "date": _default_time(source.contract_expose),
        "op": op,
    }
    for side in ("before", "after"):
        mapped, skipped = _mapped(columns, target, side)
        if mapped:
            entry[side] = mapped
        for sentence in skipped:
            projected.notes.append(
                (
                    f"{source.expose_id}: events {side}",
                    f"{sentence}, so it is drawn as any column",
                )
            )
    return entry


def _as_events(
    source: ProjectionSource,
    columns: Mapping[str, CompiledColumn],
    histories: Mapping[str, HistoryTarget],
    projected: Projected,
) -> _Verdict:
    """Change events of the history the expose relates to, when a column holds ``c``, ``u`` and ``d``."""
    ops = _op_columns(columns)
    if not ops:
        return None
    target = _relationship(source, histories)
    misses = _events_misses(source, columns, target)
    if misses or target is None:
        return "events", misses
    entry = _events_entry(source, columns, ops[0], target, projected)
    projected.events.append(entry)
    # An event holds the version's value in each column a mapping names, and in each other
    # column that names a column of the history (ADR 0027 section 4), but for its op and date.
    named = {*entry.get("before", {}), *entry.get("after", {})}
    named.update(
        name
        for name in columns
        if name in target.columns and name not in (entry["op"], entry["date"])
    )
    projected.carried[source.expose_id] = (target.expose_id, tuple(sorted(named)))
    return "events", []


_SPAN_MOST = 1_000_000_000

_SPAN_REASON = (
    "A key of an emit projected from a history: its rows follow from the history, which the "
    "profile does not count, so its values span up to the bound of a committed state, and row "
    "i takes the i-th of them."
)


def span_primary_key(plans: Sequence[ColumnPlan], keys: ExposeKeys) -> None:
    """Span a projected emit's whole-number primary key up to the bound of a committed state.

    The published rowCount of the emit does not count its rows: they follow from the history, and
    a key declared over the published count would hold fewer values than the emit has rows. The
    key starts where it did, and the numbers that moved say so.
    """
    columns = _primary_columns(keys)
    for plan in plans:
        bins = plan.declaration.get("bins")
        if plan.name not in columns or not isinstance(bins, list) or len(bins) != 2:
            continue
        if Decimal(str(bins[1])) >= _SPAN_MOST:
            continue
        bins[1] = str(_SPAN_MOST)
        moved = [("bins", "1")]
        range_ = plan.declaration.get("range")
        if isinstance(range_, dict):
            range_["max"] = str(_SPAN_MOST)
            moved.append(("range", "max"))
        plan.labels = [
            (Label(label.tokens, assumed(_SPAN_REASON)) if label.tokens in moved else label)
            for label in plan.labels
        ]
        plan.assumptions.append(_SPAN_REASON)
        plan.choices.append(_SPAN_REASON)


def _grain_dimensions(
    source: ProjectionSource, columns: Mapping[str, CompiledColumn]
) -> List[Tuple[str, str]]:
    """The DATE dimensions that declare a ``timeGranularity``, each with it."""
    semantics = source.contract_expose.get("semantics")
    dimensions = semantics.get("dimensions") if isinstance(semantics, Mapping) else None
    found: List[Tuple[str, str]] = []
    for dimension in dimensions if isinstance(dimensions, list) else ():
        if not isinstance(dimension, Mapping) or not isinstance(dimension.get("name"), str):
            continue
        params = dimension.get("typeParams")
        grain = params.get("timeGranularity") if isinstance(params, Mapping) else None
        column = columns.get(str(dimension["name"]))
        if isinstance(grain, str) and column is not None:
            if column.logical_type.kind is LogicalKind.DATE:
                found.append((str(dimension["name"]), grain))
    return found


def _periods_misses(
    source: ProjectionSource,
    columns: Mapping[str, CompiledColumn],
    dimensions: Sequence[Tuple[str, str]],
    target: Optional[HistoryTarget],
    kinds: Mapping[str, LogicalKind],
) -> List[str]:
    misses: List[str] = []
    if target is None:
        misses.append(_NO_RELATIONSHIP)
    if len(dimensions) != 1:
        misses.append(
            f"it declares {len(dimensions)} DATE dimensions with a timeGranularity, not exactly one"
        )
    elif dimensions[0][1] not in _GRAINS:
        misses.append(
            f"the timeGranularity of {dimensions[0][0]} is {dimensions[0][1]}, which is not one of "
            f"{', '.join(_GRAINS)}"
        )
    for key in _primary_columns(source.keys):
        if kinds.get(key, columns[key].logical_type.kind) is not LogicalKind.INT:
            misses.append(
                f"its primary key {key} is not a whole-number column (ADR 0027 section 5)"
            )
    return misses + _counted_misses(source, target)


def _as_periods(
    source: ProjectionSource,
    columns: Mapping[str, CompiledColumn],
    histories: Mapping[str, HistoryTarget],
    projected: Projected,
    kinds: Mapping[str, LogicalKind],
) -> _Verdict:
    """Periodic rows of the history the expose relates to, on its one DATE dimension with a grain."""
    dimensions = _grain_dimensions(source, columns)
    if not dimensions or _op_columns(columns):
        return None
    target = _relationship(source, histories)
    misses = _periods_misses(source, columns, dimensions, target, kinds)
    if misses or target is None:
        return "periods", misses
    column, grain = dimensions[0]
    # A periodic row carries the version valid in its period in each column that names a column
    # of the history (ADR 0027 section 5), but for the period's own.
    projected.carried[source.expose_id] = (
        target.expose_id,
        tuple(name for name in columns if name in target.columns and name != column),
    )
    entry: Dict[str, Any] = {
        "emit": source.expose_id,
        "of": target.expose_id,
        "column": column,
        "grain": grain,
        "match": "overlap",
    }
    if source.window is not None:
        entry.update(
            published_period_range(source.table, column, grain, source.window, target.until)
        )
    entry["kinds"] = [{"count": 1}]
    projected.periods.append(entry)
    return "periods", []


_DAY_MS = 86_400_000
_EPOCH = date(1970, 1, 1)
_GRAIN_MONTHS = {"month": 1, "quarter": 3, "year": 12}


def _period_on(anchor: date, grain: str, day: date) -> date:
    """The start of the period, counted from *anchor* by *grain*, that holds *day* (on or after *anchor*).

    Periods count from their anchor (ADR 0027 section 5), so a ``from`` on this day keeps every
    period on the days the entry would have without it.
    """
    if grain in ("day", "week"):
        width = 1 if grain == "day" else 7
        return anchor + timedelta(days=(day - anchor).days // width * width)
    step = _GRAIN_MONTHS[grain]
    position = ((day.year - anchor.year) * 12 + day.month - anchor.month) // step
    while position > 0 and add_months(anchor, step * position) > day:
        position -= 1
    return add_months(anchor, step * position)


def _last_day_before(text: str) -> int:
    """The last day, as days since 1970-01-01, that starts before the instant or day *text*."""
    milliseconds = parse_rfc3339_ms(f"{text}T00:00:00Z" if len(text) == 10 else text)
    return -(-milliseconds // _DAY_MS) - 1


def _histogram(table: Optional[Mapping[str, Any]], column: str) -> Optional[Mapping[str, Any]]:
    """The first ungrouped ``marginalHistogram/1`` of *column* in the profile *table*, or ``None``."""
    columns = table.get("columns") if isinstance(table, Mapping) else None
    node = columns.get(column) if isinstance(columns, Mapping) else None
    statistics = node.get("statistics") if isinstance(node, Mapping) else None
    for statistic in statistics if isinstance(statistics, list) else ():
        if (
            isinstance(statistic, Mapping)
            and "groupBy" not in statistic
            and statistic.get("definition") == MARGINAL_HISTOGRAM.identifier
        ):
            return statistic
    return None


def _published_days(table: Optional[Mapping[str, Any]], column: str) -> Optional[List[int]]:
    """The edges, in days, of the published histogram of *column*, or ``None`` without one.

    Only an ungrouped histogram whose one group is published counts, and only when every edge
    is a whole day: a withheld group, or no histogram at all, gives ``None``.
    """
    statistic = _histogram(table, column)
    groups = statistic.get("groups") if statistic is not None else None
    if not isinstance(groups, list) or len(groups) != 1:
        return None
    if not isinstance(groups[0], Mapping) or groups[0].get("status") != "F":
        return None
    edges = statistic.get("bins") if statistic is not None else None
    try:
        values = [Decimal(str(edge)) for edge in edges] if isinstance(edges, list) else []
    except ArithmeticError:
        return None
    if len(values) < 2 or any(value % _DAY_MS != 0 for value in values):
        return None
    return [int(value) // _DAY_MS for value in values]


def published_period_range(
    table: Optional[Mapping[str, Any]],
    column: str,
    grain: str,
    window: Tuple[str, str],
    until: Optional[str],
) -> Dict[str, str]:
    """``from`` and ``through`` for a periods entry on *column* by *grain*, from its published histogram.

    ``from`` is the start of the period that holds the first edge, counted from the window's
    first day as the entry's periods are without it, and ``through`` the last bin's start, so no
    period lies in a month the data holds no row for. Both are held to the window and to
    *until*: a ``from`` on the window's first day is left out, as that day is the entry's anchor
    without one, and so is a ``through`` no earlier than the window's last day. A withheld
    histogram, or none, writes neither (ADR 0027, the amendment of 2026-11-26).
    """
    days = _published_days(table, column)
    if days is None:
        return {}
    first = parse_rfc3339_ms(window[0]) // _DAY_MS
    last = _last_day_before(window[1])
    held = last if until is None else min(last, _last_day_before(until))
    low, high = max(days[0], first), min(days[-2], held)
    if high < low:
        return {}
    found: Dict[str, str] = {}
    anchor = _EPOCH + timedelta(days=first)
    start = _period_on(anchor, grain, _EPOCH + timedelta(days=low))
    if start > anchor:
        found["from"] = start.isoformat()
    if high < last:
        found["through"] = (_EPOCH + timedelta(days=high)).isoformat()
    return found


def _write_one(
    source: ProjectionSource,
    histories: Mapping[str, HistoryTarget],
    kinds: Mapping[str, LogicalKind],
    projected: Projected,
) -> None:
    """Project *source* by the first rule it meets whole, else record the rule it meets in part."""
    columns = {column.name: column for column in compile_expose(source.contract_expose).columns}
    rules: List[Callable[[], _Verdict]] = [
        lambda: _as_snapshot(source, columns, histories, projected),
        lambda: _as_events(source, columns, histories, projected),
        lambda: _as_periods(source, columns, histories, projected, kinds),
    ]
    partial: _Verdict = None
    for rule in rules:
        verdict = rule()
        if verdict is not None and not verdict[1]:
            projected.emits[source.expose_id] = _ROWS_REASON
            projected.released += [(source.expose_id, entry) for entry in source.counts]
            return
        partial = partial or verdict
    if partial is not None:
        _unused(projected, source, partial[0], partial[1])


def project(
    sources: Sequence[ProjectionSource],
    histories: Sequence[HistoryTarget],
    kinds: Mapping[str, Mapping[str, LogicalKind]],
) -> Projected:
    """The projections of each expose in *sources* from the *histories* ``init`` wrote.

    *kinds* holds each expose's planned column types, by exposeId. An expose that no rule reads
    is left as it is, and one that meets part of a rule is listed in ``notes`` as unused.
    """
    projected = Projected()
    held = {target.expose_id: target for target in histories}
    if not held:
        return projected
    for source in sources:
        if source.expose_id not in held:
            _write_one(source, held, kinds.get(source.expose_id, {}), projected)
    return projected


_MOVED = (
    "the emit is projected from a history, which refuses a counts entry over its relationship, so "
    "its children per parent are the history's parent's: {why}"
)
_KEPT = (
    "the emit is projected from a history, which refuses a counts entry over its relationship, so "
    "none is written, and the history's parent does not take this statistic: {why}"
)


_NOT_ENTITIES = (
    "its children per parent count events or periods, not entities, and the history's parent "
    "takes entities per parent"
)


def history_target(
    entry: Mapping[str, Any], held: ExposeKeys, columns: Sequence[CompiledColumn]
) -> HistoryTarget:
    """The target of the ``history`` *entry* of the emit with the keys *held* and *columns*."""
    declared = entry.get("parent")
    name = declared.get("relationship") if isinstance(declared, dict) else None
    return HistoryTarget(
        str(entry["emit"]),
        tuple(item.column for item in held.entities if item.kind == "natural"),
        {column.name: column for column in columns},
        None if name is None else _target_of(held, str(name)),
        None if entry.get("until") is None else str(entry["until"]),
    )


def _target_of(keys: ExposeKeys, name: str) -> Optional[str]:
    """The expose the relationship *name* reaches, or ``None`` when the expose has none of that name."""
    for item in keys.relationships:
        if item.name == name:
            return str(item.target)
    return None


def _history_parent(
    history: Optional[Dict[str, Any]],
    counts: Mapping[str, Any],
    declared_target: str,
    keys_of: Callable[[str], ExposeKeys],
) -> Tuple[Optional[Dict[str, Any]], str]:
    """The ``parent`` of *history* that may take *counts*, or ``None`` with why it may not."""
    parent = history.get("parent") if history is not None else None
    if history is None or not isinstance(parent, dict):
        return None, "the history writes no parent"
    target = _target_of(keys_of(str(history["emit"])), str(parent["relationship"]))
    if target != declared_target:
        return None, f"the history's parent is laid out under {target}, not {declared_target}"
    if "children" in parent:
        return None, "the history's own children per parent are written and are kept"
    if "children" not in counts:
        return None, "the statistic gives no children per parent"
    return parent, ""


def release_counts(
    found: Projected,
    histories: Sequence[Dict[str, Any]],
    keys_of: Callable[[str], ExposeKeys],
    published: Published,
) -> List[Tuple[str, str]]:
    """Take each ``counts`` entry off a projected emit and give its statistic to the history's parent.

    A ``counts`` entry over a relationship of a projected emit is refused (ADR 0027), so the emit
    stays a projection and the entry is dropped. When the history of the emit has a ``parent``
    under the same parent expose and no ``children`` of its own, and the emit is a snapshot (one
    row per entity, so its children per parent are entities per parent), the entry's ``children``
    (and ``minChildren``, when the parent has none) become the parent's; otherwise the statistic is
    unused, with why. An events or periodic emit counts events or periods per parent, so its
    statistic is never given to the parent. *published* holds the profile's relationships and the lists its layout made
    of them, changed in place; *histories* are the ``history`` entries. Returns each
    ``(emit, column)`` whose foreign key the counted layout no longer sets.
    """
    freed: List[Tuple[str, str]] = []
    lists = published.lists()
    sources = published.relationships if isinstance(published.relationships, list) else []
    snapshots = {str(item["emit"]) for item in found.snapshots}
    # By emit, so which snapshot gives its statistic does not depend on the order of the exposes.
    for emit, counts in sorted(found.released, key=lambda item: item[0]):
        name = str(counts["relationship"])
        declared = next(item for item in keys_of(emit).relationships if item.name == name)
        history_id = found.carried[emit][0]
        history = next((item for item in histories if item["emit"] == history_id), None)
        if emit in snapshots:
            parent, why = _history_parent(history, counts, declared.target, keys_of)
        else:
            parent, why = None, _NOT_ENTITIES
        if parent is not None:
            sentence = _MOVED.format(
                why=f"{history_id}'s parent {parent['relationship']} takes it as its children"
            )
            _give_children(parent, counts)
        else:
            sentence = _KEPT.format(why=why)
        _drop_counts(lists, emit, name)
        found_entry, tokens = profile_relationship(sources, emit, declared)
        if tokens is not None and found_entry is not None:
            _move_statistic(lists, (tokens, found_entry), parent, sentence)
        found.notes.append((f"{emit}: counts", sentence))
        freed.append((emit, declared.from_columns[0]))
    return freed


def _give_children(parent: Dict[str, Any], counts: Mapping[str, Any]) -> None:
    """Give a history's *parent* the children per parent of a ``counts`` entry, and its minimum when it has none."""
    parent["children"] = dict(counts["children"])
    if "minChildren" in counts and "minChildren" not in parent:
        parent["minChildren"] = counts["minChildren"]


def _drop_counts(
    lists: Tuple[List[Any], List[Any], List[Any], List[Any], List[Any]], emit: str, name: str
) -> None:
    """Take the ``counts`` entry over relationship *name* of *emit*, and its note, out of *lists*."""
    entries, _, _, _, notes = lists
    entries[:] = [
        item
        for item in entries
        if not (item.get("emit") == emit and item.get("relationship") == name)
    ]
    notes[:] = [item for item in notes if item[0] != f"{emit}.{name}"]


def _move_statistic(
    lists: Tuple[List[Any], List[Any], List[Any], List[Any], List[Any]],
    published: Tuple[Tuple[str, ...], Mapping[str, Any]],
    parent: Optional[Dict[str, Any]],
    sentence: str,
) -> None:
    """Say, in *lists*, where the profile's statistics of a relationship went: *parent* took them, or none did."""
    _, used, unused, records, _ = lists
    tokens, entry = published
    size = len(tokens)
    gone = [item for item in used if tuple(item[0][:size]) == tokens]
    used[:] = [item for item in used if item not in gone]
    records[:] = [item for item in records if tuple(item[0][:size]) != tokens]
    said = structural_record(
        entry,
        parent is not None and "minChildren" in parent,
        "the emit is projected from a history",
    )
    if said:
        records.append((tokens + ("structural",), said))
    if parent is not None:
        used += [(pointer, sentence) for pointer, _ in gone]
    else:
        unused += [(pointer, sentence) for pointer, _ in gone]


#: Why the children per entity of the relationship a projection reads its history through are unused.
_PER_ENTITY = {
    EVENTS_KEY: (
        "the emit is projected from {history} as its change events, one row per change, so its "
        "rows per {entity} are the changes of that entity's versions and follow from the history"
    ),
    PERIODS_KEY: (
        "the emit is projected from {history} as its periodic rows, one row per version and "
        "period it holds, so its rows per {entity} follow from the versions and their periods; "
        "the profile counts the rows, not the periods each row holds, so no rows per period are "
        "read from it"
    ),
}


def _followed_statistic(
    entry: Mapping[str, Any],
    held: Mapping[str, HistoryTarget],
    keys_of: Callable[[str], ExposeKeys],
    sources: Sequence[Any],
) -> Optional[Tuple[HistoryTarget, str, Tuple[str, ...]]]:
    """The history of a projection *entry*, the relationship it follows and its profile pointer, or ``None``."""
    emit, target = str(entry["emit"]), held.get(str(entry["of"]))
    followed = None if target is None else _followed(keys_of(emit), {target.expose_id: target})
    if target is None or followed is None:
        return None
    _, tokens = profile_relationship(sources, emit, followed)
    if tokens is None:
        return None
    return target, str(followed.name), tokens


def say_per_entity(
    found: Projected,
    histories: Sequence[HistoryTarget],
    keys_of: Callable[[str], ExposeKeys],
    published: Published,
) -> None:
    """Say why the rows per entity of each events and periodic emit are not read from the profile.

    The relationship such an emit reads its history through reaches the history's natural entity,
    so the profile's children per parent over it count the emit's rows per entity, which no layout
    places (they are left unused when the relationships are laid out). The projection lays them
    out from the history, so the reason each statistic of that profile entry is unused, in
    *published*, becomes the projection's, and a note says it once per emit.
    """
    _, _, unused, _, _ = published.lists()
    sources = published.relationships if isinstance(published.relationships, list) else []
    held = {target.expose_id: target for target in histories}
    for key, entries in ((EVENTS_KEY, found.events), (PERIODS_KEY, found.periods)):
        for entry in sorted(entries, key=lambda item: str(item["emit"])):
            followed = _followed_statistic(entry, held, keys_of, sources)
            if followed is None:
                continue
            target, name, tokens = followed
            sentence = _PER_ENTITY[key].format(
                history=target.expose_id, entity=", ".join(target.natural)
            )
            unused[:] = [
                (pointer, sentence) if tuple(pointer[: len(tokens)]) == tokens else (pointer, why)
                for pointer, why in unused
            ]
            found.notes.append((f"{entry['emit']}: {name}", sentence))


_FOLLOWS = (
    "The emit is projected from {history}, so its rows follow from the history and the column "
    "holds the value of a version, not a parent the counted layout sets."
)


def follow_history(expose: Any, column: str, history: str) -> None:
    """Say that the foreign key *column* of a projected *expose* follows *history*, not a ``counts`` entry."""
    sentence = _FOLLOWS.format(history=history)
    for plan in expose.columns:
        if plan.name == column:
            plan.assumptions = [sentence]
            plan.choices = [sentence]
            plan.how = "a column of the history the emit is projected from"
    restate_assumptions(expose)
