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
from flux_engine.seam.contract import CompiledColumn, LogicalKind, compile_expose
from flux_engine.seam.keys import ExposeKeys
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY, SNAPSHOTS_KEY

__all__ = [
    "HistoryTarget",
    "Projected",
    "ProjectionSource",
    "project",
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


@dataclass(frozen=True)
class ProjectionSource:
    """What :func:`project` reads of one expose that has no validity window.

    ``keys`` is the expose's keys, ``counted`` whether a ``counts`` entry is written over one of
    its relationships.
    """

    expose_id: str
    contract_expose: Mapping[str, Any]
    keys: ExposeKeys
    counted: bool = False


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

_COUNTED = "a counts entry is written over one of its relationships, which a projected emit refuses"


def _primary_columns(keys: ExposeKeys) -> Tuple[str, ...]:
    return tuple(entity.column for entity in keys.entities if entity.kind == "primary")


def _unused(projected: Projected, source: ProjectionSource, rule: str, misses: List[str]) -> None:
    projected.notes.append(
        (
            f"{source.expose_id}: {rule}",
            "unused, the expose meets only part of the rule: " + "; ".join(misses),
        )
    )


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
    if source.counted:
        misses.append(_COUNTED)
    return misses


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
    if source.counted:
        misses.append(_COUNTED)
    return misses


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
    if source.counted:
        misses.append(_COUNTED)
    return misses


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
