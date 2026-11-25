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
are written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from flux_engine.seam.contract import CompiledColumn, LogicalKind, compile_expose
from flux_engine.seam.keys import ExposeKeys
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY, SNAPSHOTS_KEY

__all__ = [
    "HistoryTarget",
    "Projected",
    "ProjectionSource",
    "follow_history",
    "history_target",
    "project",
    "release_counts",
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


def _relationship(
    source: ProjectionSource, histories: Mapping[str, HistoryTarget]
) -> Optional[HistoryTarget]:
    """The history of the first to-one relationship that is no key of the expose and reads its natural entity."""
    held = source.keys
    key_sets = {frozenset(key) for key in (*held.keys, *held.instant_keys)}
    for relationship in held.relationships:
        target = histories.get(relationship.target)
        if (
            target is not None
            and relationship.cardinality == "many_to_one"
            and frozenset(relationship.from_columns) not in key_sets
            and tuple(relationship.to_columns) == target.natural
        ):
            return target
    return None


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
    projected.periods.append(
        {
            "emit": source.expose_id,
            "of": target.expose_id,
            "column": column,
            "grain": grain,
            "match": "overlap",
            "kinds": [{"count": 1}],
        }
    )
    return "periods", []


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
