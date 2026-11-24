# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Change events of a history: one event per change (ADR 0027 section 4).

An entity's events are, in entity order, a ``c`` for its first version, a ``u`` for each later
version and a ``d`` when the history ended it: under ``finalVersion`` the change into its final
version is the ``d``, and under ``closeLast`` the close of its last version is, which follows the
last version's own event. ``on`` keeps the listed ops and ``when`` the changes whose version it
holds for, so the count is known at plan time and no cell is drawn for it.

An event is about a version: the new version of a ``c`` or a ``u``, the final version of a
``finalVersion`` ``d`` and the last version of a ``closeLast`` ``d``. Its columns are:

* the ``op`` column, the op's code;
* the ``at`` column, the change's instant: the version's start, or for a ``closeLast`` ``d`` the
  last version's end, and the ``date`` column, when the event was recorded: that instant plus a
  lag, which is the instant itself while the entry has no ``lag``;
* the ``type`` column, the text of the event's reason: ``c``, ``d``, ``end``, or the tracked
  column its ``u`` changes;
* each ``before`` column, the previous version's value (NULL on a ``c``, and on the ``d`` of a
  ``closeLast`` entity the last version's own value, the state that was deleted), and each
  ``after`` column, this version's value (NULL on a ``d``);
* each other column that names a column of the history's emit, the value of the version the event
  is about;
* every remaining column, drawn from the event emit's own statistics on the generator cell path
  at its row.

:data:`KIND` is the dispatch entry of :mod:`flux_engine.generator.projections`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.rng import bounded_uint, uniform53
from flux_engine.core.seedtree import SeedTreeRoot, counter_stream, key_label, path_key
from flux_engine.generator.history import HistoryPlan, _StartTable, _table_over, unit_of
from flux_engine.generator.paths import LAG_PATH_SHAPE, lag_path
from flux_engine.generator.rows import GeneratorPlan, cells_before_rules, generate_row_slice
from flux_engine.generator.versionview import (
    DAY_MS,
    VersionView,
    unit_value,
    value_unit,
    view_of,
)
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.history import Histogram
from flux_engine.seam.projections import EventsEntry

if TYPE_CHECKING:
    from flux_engine.generator.projections import ProjectionPlan

__all__ = [
    "EventLayout",
    "convert_unit",
    "events_rows",
    "lag_at",
    "lag_table",
    "lags",
    "layout_of",
]

#: The op codes of an event, in the order of the arrays below.
OPS = ("c", "u", "d")
_C, _U, _D = 0, 1, 2
#: How many layouts a process keeps (each holds arrays over the events).
_LAYOUTS_KEPT = 4
_LAYOUTS: Dict[Tuple[Any, ...], "EventLayout"] = {}


def _refuse(message: str) -> FluxError:
    from flux_engine.generator.projections import refuse

    return refuse(message)


def clear_layouts() -> None:
    """Drop the event layouts this process keeps."""
    _LAYOUTS.clear()


@dataclass(frozen=True, eq=False)
class EventLayout:
    """Each event of a history, in entity order: the version it is about and what it changes.

    ``about`` is the version row the event is about, ``op`` its op (0 ``c``, 1 ``u``, 2 ``d``),
    ``before`` the version row its ``before`` columns read (-1 for none), ``closing`` whether it
    is the ``d`` that closes a ``closeLast`` entity's last version, and ``version`` the version
    ordinal the event's draws are addressed by (one past the last version for a closing ``d``).
    """

    about: "np.ndarray"
    op: "np.ndarray"
    before: "np.ndarray"
    closing: "np.ndarray"
    version: "np.ndarray"

    @property
    def count(self) -> int:
        """The number of events."""
        return int(self.about.shape[0])


def _candidates(chain: HistoryPlan, view: VersionView) -> EventLayout:
    """Every change of the history, before ``on`` and ``when``, in entity order."""
    rows = np.arange(chain.rows, dtype=np.int64)
    entities, ordinals = view.entities, view.ordinals
    counts = chain.versions.counts[entities]
    ended = np.zeros(chain.entities, dtype=bool) if chain.ended is None else chain.ended
    last = ordinals == counts - 1
    final = ended[entities] & last & (not chain.closes_last)
    op = np.where(ordinals == 0, _C, np.where(final, _D, _U)).astype(np.int8)
    before = np.where(ordinals == 0, -1, rows - 1)
    about = rows
    version = ordinals
    closing = np.zeros(chain.rows, dtype=bool)
    if chain.closes_last and view.end is not None and chain.ended is not None:
        closed = np.flatnonzero(chain.ended)
        ends = chain.versions.offsets[closed] + chain.versions.counts[closed] - 1
        order = np.argsort(np.concatenate((2 * rows, 2 * ends + 1)), kind="stable")
        about = np.concatenate((rows, ends))[order]
        op = np.concatenate((op, np.full(closed.shape[0], _D, dtype=np.int8)))[order]
        before = np.concatenate((before, ends))[order]
        version = np.concatenate((ordinals, chain.versions.counts[closed]))[order]
        closing = np.concatenate((closing, np.ones(closed.shape[0], dtype=bool)))[order]
    return EventLayout(about, op, before, closing, version)


def _kept(layout: EventLayout, mask: "np.ndarray") -> EventLayout:
    return EventLayout(
        layout.about[mask],
        layout.op[mask],
        layout.before[mask],
        layout.closing[mask],
        layout.version[mask],
    )


def layout_of(chain: HistoryPlan, projection: "ProjectionPlan") -> EventLayout:
    """The events of the history, worked out once per process: ``on`` and ``when`` applied."""
    entry = projection.entry
    assert isinstance(entry, EventsEntry) and projection.root is not None
    root = projection.root
    held = (
        root.digest,
        root.seed_tree_version,
        projection.history.digest().value,
        repr(entry),
    )
    found = _LAYOUTS.get(held)
    if found is None:
        view = view_of(projection.history, chain, root)
        found = _candidates(chain, view)
        mask = np.isin(found.op, [OPS.index(code) for code in entry.on])
        if entry.when is not None:
            checked = view.compile_when(entry.when)
            mask[mask] = view.holds(checked, found.about[mask])
        found = _kept(found, mask)
        if len(_LAYOUTS) >= _LAYOUTS_KEPT:
            _LAYOUTS.clear()
        _LAYOUTS[held] = found
    return found


def count(chain: HistoryPlan, projection: "ProjectionPlan") -> int:
    """The number of events (ADR 0027 section 4), from the plan alone."""
    return layout_of(chain, projection).count


def sources(chain: HistoryPlan, projection: "ProjectionPlan") -> EventLayout:
    """The events, each with the version it is about."""
    return layout_of(chain, projection)


# -- the cells ----------------------------------------------------------------------------------


def convert_unit(unit: int, source: LogicalKind, target: LogicalKind) -> int:
    """An instant in *source*'s unit as an instant in *target*'s: a day is a midnight, a millisecond is cut to its day."""
    if source is target:
        return unit
    if source is LogicalKind.DATE:
        return unit * DAY_MS
    return unit // DAY_MS


def _kind_of(plan: GeneratorPlan, column: str) -> LogicalKind:
    return plan.column_plan(column).column.logical_type.kind


# -- the lag ------------------------------------------------------------------------------------


@lru_cache(maxsize=4096)
def lag_table(
    histogram: Histogram, kind: LogicalKind, hi: int, where: str
) -> Optional[_StartTable]:
    """``lag`` as a table over ``[0, hi]``, in the date column's unit; ``None`` when no bin meets it.

    The edges are whole days, read at the unit of *kind* (a day, or the milliseconds inside it),
    and the table is built as EXPR-SPEC-2 section 6 builds a histogram's part inside an interval.
    """
    scale = DAY_MS if kind is LogicalKind.TIMESTAMP else 1
    try:
        edges = [int(Decimal(text)) * scale for text in histogram.edges]
    except (InvalidOperation, ValueError):
        raise _refuse(f"{where}.lag: the bin edges are whole days") from None
    return _table_over(histogram, edges, 0, hi, f"{where}.lag")


def _lag_key(root: SeedTreeRoot, sim_id: str, expose_id: str) -> kernel2.Key:
    label, _ordinals = key_label(lag_path(sim_id, expose_id, 0, 0), LAG_PATH_SHAPE)
    return path_key(root, label)


def lag_at(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    histogram: Histogram,
    kind: LogicalKind,
    entity: int,
    version: int,
    hi: int,
) -> int:
    """The lag of event ``(entity, version)`` as drawn on its own stream: the per-row path.

    One ``uniform53`` at position 0 of ``("sim", sim_id, "gen", exposeId, "lag", entity,
    version)`` through the part of ``lag`` inside ``[0, hi]``, or one ``bounded`` at the same
    position over that interval when no bin meets it.
    """
    table = lag_table(histogram, kind, hi, expose_id)
    stream = counter_stream(root, lag_path(sim_id, expose_id, entity, version), LAG_PATH_SHAPE)
    if table is not None:
        return table.value(uniform53(stream))
    return bounded_uint(stream, hi + 1)


def lags(
    root: SeedTreeRoot,
    sim_id: str,
    expose_id: str,
    histogram: Histogram,
    kind: LogicalKind,
    entities: "np.ndarray",
    versions: "np.ndarray",
    his: "np.ndarray",
) -> "np.ndarray":
    """:func:`lag_at` for each event, by the arrays of the column path: one Philox call for the uniforms."""
    zero = np.zeros(entities.shape[0], dtype=np.uint64)
    words = kernel2.philox_arrays(
        entities.astype(np.uint64),
        versions.astype(np.uint64),
        zero,
        zero,
        _lag_key(root, sim_id, expose_id),
    )[0]
    drawn = words >> np.uint64(11)
    found = np.zeros(entities.shape[0], dtype=np.int64)
    for hi in np.unique(his).tolist():
        chosen = np.flatnonzero(his == hi)
        table = lag_table(histogram, kind, hi, expose_id)
        if table is not None:
            found[chosen] = table.values(drawn[chosen])
            continue
        for index in chosen.tolist():
            found[index] = bounded_uint(
                counter_stream(
                    root,
                    lag_path(sim_id, expose_id, int(entities[index]), int(versions[index])),
                    LAG_PATH_SHAPE,
                ),
                hi + 1,
            )
    return found


def _lagged(
    plan: GeneratorPlan,
    view: VersionView,
    layout: EventLayout,
    start: int,
    stop: int,
    entry: EventsEntry,
    moments: List[int],
    kind: LogicalKind,
    root: SeedTreeRoot,
) -> List[int]:
    """Each event's instant plus its lag, in the date column's unit; no lag leaves the instants."""
    if entry.lag is None:
        return moments
    until = convert_unit(unit_of(view.pick.entry.until, view.kind), view.kind, kind)
    his = np.maximum(until - np.array(moments, dtype=np.int64), 0)
    about = layout.about[start:stop]
    drawn = lags(
        root,
        plan.sim_id,
        plan.expose.expose_id,
        entry.lag,
        kind,
        view.entities[about],
        layout.version[start:stop],
        his,
    )
    return (np.array(moments, dtype=np.int64) + drawn).tolist()


# -- the type -----------------------------------------------------------------------------------


def _reason_places(
    chain: HistoryPlan, view: VersionView, about: "np.ndarray", ops: "np.ndarray", names: List[str]
) -> "np.ndarray":
    """The place in ``tracked`` of each event's reason: 0 for an event with none.

    Only the ``u`` of an ordinary change has a reason: a ``c`` and a ``d`` take their op's text, and an
    ending version's change is no ordinary one, so its index may lie past ``T``.
    """
    places = np.zeros(about.shape[0], dtype=np.int64)
    if chain.reasons is None or not names:
        return places
    changes = view.ordinals[about] - 1
    if chain.changes is not None:
        changes = changes + chain.changes[view.entities[about]]
    ordinary = (ops == _U) & ~chain.ending_rows(about)
    if ordinary.any():
        places[ordinary] = chain.reasons.columns_of(changes[ordinary])
    return places


def _word(op: int, ends: bool, place: int, names: List[str]) -> str:
    """The key of an event's text in ``type.values``: its op, ``end``, or its reason's column."""
    if op != _U:
        return OPS[op]
    if ends:
        return "end"
    return names[place] if names else "u"


def _types(
    chain: HistoryPlan,
    view: VersionView,
    layout: EventLayout,
    start: int,
    stop: int,
    entry: EventsEntry,
) -> List[str]:
    """The text of each event's type: its op, ``end`` for the ``u`` of an ending version, else its reason's column."""
    texts = dict(entry.type_values)
    about, codes = layout.about[start:stop], layout.op[start:stop]
    names = [tracked.column for tracked in view.pick.entry.tracked]
    places = _reason_places(chain, view, about, codes, names).tolist()
    found = []
    for op, ends, place in zip(
        codes.tolist(), chain.ending_rows(about).tolist(), places, strict=True
    ):
        word = _word(op, ends, place, names)
        if word not in texts:
            raise _refuse(f"events ({entry.emit}).type.values has no text for {word!r}")
        found.append(texts[word])
    return found


def _history_rows(
    projection: "ProjectionPlan",
    layout: EventLayout,
    start: int,
    stop: int,
    root: SeedTreeRoot,
) -> Tuple[int, Sequence[Tuple[Any, ...]]]:
    """The history's rows the events *start* to *stop* - 1 read, from their first to their last."""
    about, before = layout.about[start:stop], layout.before[start:stop]
    low = int(about.min())
    if (before >= 0).any():
        low = min(low, int(before[before >= 0].min()))
    high = int(about.max()) + 1
    return low, generate_row_slice(projection.history, low, high, root=root)


def _instants(
    view: VersionView,
    layout: EventLayout,
    start: int,
    stop: int,
    low: int,
    rows: Sequence[Tuple[Any, ...]],
    position: Dict[str, int],
    target: LogicalKind,
) -> List[int]:
    """Each event's instant in the units of *target*: the version's start, or a closing ``d``'s end."""
    assert view.start is not None
    starts, ends = position[view.start], position[view.end] if view.end is not None else -1
    found = []
    for about, closing in zip(
        layout.about[start:stop].tolist(), layout.closing[start:stop].tolist(), strict=True
    ):
        cell = rows[about - low][ends if closing else starts]
        found.append(convert_unit(value_unit(cell), view.kind, target))
    return found


def _image(
    rows: Sequence[Tuple[Any, ...]], low: int, sources_: Sequence[int], index: int
) -> List[Any]:
    """Column *index* at each history row of *sources_*; NULL for -1."""
    return [None if source < 0 else rows[source - low][index] for source in sources_]


def events_rows(
    plan: GeneratorPlan, layout: EventLayout, start: int, stop: int, root: SeedTreeRoot
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* - 1 of the events emit *plan*, before the emit's rules run."""
    projection = plan.projection
    assert projection is not None and isinstance(projection.entry, EventsEntry)
    entry = projection.entry
    if start == stop:
        return ()
    chain = _chain(projection, root)
    view = view_of(projection.history, chain, root)
    low, history_rows = _history_rows(projection, layout, start, stop, root)
    position = _positions(projection.history)
    about = layout.about[start:stop].tolist()
    ops = layout.op[start:stop].tolist()
    before_rows = layout.before[start:stop].tolist()
    after_rows = _after_rows(ops, about)
    before_map, after_map = dict(entry.before), dict(entry.after)
    names = [column.column.name for column in plan.columns]
    free = _free_columns(names, entry, before_map, after_map, position)
    # The emit's own cells, before its rules: they run over the row once every column the
    # event takes from the history is set (projections.projection_rows).
    own = cells_before_rules(replace(plan, projection=None), start, stop, root=root) if free else ()
    columns: List[List[Any]] = []
    for index, name in enumerate(names):
        if name == entry.op:
            columns.append([OPS[op] for op in ops])
        elif name in (entry.at, entry.date):
            kind = _kind_of(plan, name)
            moments = _instants(view, layout, start, stop, low, history_rows, position, kind)
            if name == entry.date:
                moments = _lagged(plan, view, layout, start, stop, entry, moments, kind, root)
            columns.append([unit_value(kind, moment) for moment in moments])
        elif name == entry.type_column:
            columns.append(_types(chain, view, layout, start, stop, entry))
        elif name in before_map:
            columns.append(_image(history_rows, low, before_rows, position[before_map[name]]))
        elif name in after_map:
            columns.append(_image(history_rows, low, after_rows, position[after_map[name]]))
        elif name in position:
            columns.append(_image(history_rows, low, about, position[name]))
        else:
            columns.append([row[index] for row in own])
    return tuple(zip(*columns, strict=True))


def _positions(history: GeneratorPlan) -> Dict[str, int]:
    """The place of each column of the history's rows."""
    return {column.column.name: index for index, column in enumerate(history.columns)}


def _after_rows(ops: Sequence[int], about: Sequence[int]) -> List[int]:
    """The history row each event leaves its after image at; -1 for a delete."""
    return [-1 if op == _D else row for op, row in zip(ops, about, strict=True)]


def _free_columns(
    names: Sequence[str],
    entry: EventsEntry,
    before_map: Mapping[str, str],
    after_map: Mapping[str, str],
    position: Mapping[str, int],
) -> List[str]:
    """The columns the event rows take from the emit's own declarations, none from the history."""
    named = (entry.op, entry.at, entry.date, entry.type_column, *before_map, *after_map, *position)
    return [name for name in names if name not in named]


def _chain(projection: "ProjectionPlan", root: SeedTreeRoot) -> HistoryPlan:
    from flux_engine.generator.projections import chain_of

    return chain_of(projection.history, root)
