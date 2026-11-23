# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected emit's rows, taken from the history it projects (ADR 0027).

A projected emit declares no ``rows``. Its count is worked out at plan time from the history's
layouts alone, and each of its rows names the history row it is read from. One entry of
:data:`KINDS` stands for each kind of projection that has a generator, with two members:

* ``count``: the number of rows the history gives the projection;
* ``sources``: for a range of the projection's rows, the history row each is taken from.

Change events (section 4, :mod:`flux_engine.generator.events`) and periodic rows (section 5,
:mod:`flux_engine.generator.periods`) take their own members of :class:`ProjectionKind`, and
build their rows from the history's rows and the emit's own cells rather than copy one history
row a row.

A snapshot (section 3) is one row per entity, in entity order: each row is the entity's last
version, every column its final value, the rules included. ``ended: drop`` leaves out the entities
the history ended, so its count is the entities less the ended ones. It draws nothing and adds no
path.

:func:`project_plan` turns a planned emit into a projected one: it sets the emit's ``rows`` to the
derived count and attaches a :class:`ProjectionPlan`, which :func:`projection_rows` reads when a
slice of the emit is asked for. Every process that writes a slice works out what the slice needs
of the history once (:func:`sources_of`) and keeps it. A projection draws nothing, so the plan
document holds the count and the history's digest, and no seed path is added.

Every refusal is ``FLX-3403``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import numpy as np

from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot
from flux_engine.core.sizing import FLX_3405, MOST_ROWS
from flux_engine.generator import events as _events
from flux_engine.generator import periods as _periods
from flux_engine.generator.history import HistoryPlan, plan_history
from flux_engine.generator.rows import GeneratorPlan, generate_row_slice
from flux_engine.generator.versionview import clear_views
from flux_engine.seam.projections import (
    EVENTS_KEY,
    GENERATED_PROJECTIONS,
    PERIODS_KEY,
    PROJECTION_KEYS,
    SNAPSHOTS_KEY,
    events_entry_of,
    periods_entry_of,
    snapshot_entry_of,
)

__all__ = [
    "KINDS",
    "ProjectionKind",
    "ProjectionPlan",
    "chain_of",
    "derived_rows",
    "refuse_rows",
    "projection_of",
    "projection_rows",
    "project_plan",
    "refuse",
    "sources_of",
]

#: How many sources a process keeps (each holds an array over the projection's rows).
_SOURCES_KEPT = 4
_SOURCES: Dict[Tuple[Any, ...], "np.ndarray"] = {}


def _code() -> Any:
    from flux_engine.generator import relations

    return relations.FLX_3403


def _refuse(message: str) -> FluxError:
    """``FLX-3403`` (imported here: relations imports the generator's rows)."""
    return FluxError(_code(), message)


refuse = _refuse


@dataclass(frozen=True)
class ProjectionPlan:
    """What a projected emit's plan carries: its kind, the history's plan and the entry's options.

    ``history`` is the plan of the emit the entry's ``of`` names, as the relational plan left it.
    ``ended`` is a snapshot's ``ended`` (``keep`` or ``drop``) and ``entry`` the entry of an
    events or a periods projection. ``root`` is the run root a kind's functions read; it is set
    for the moment of a call and is no part of the plan.
    """

    kind: str
    of: str
    history: GeneratorPlan
    ended: str = "keep"
    entry: Any = None
    root: Optional[SeedTreeRoot] = field(default=None, compare=False, repr=False)
    window: Optional[Tuple[int, int]] = None

    def as_document(self) -> Dict[str, Any]:
        """The ``projection`` member of the plan's document: where the rows come from."""
        document: Dict[str, Any] = {
            "kind": self.kind,
            "of": self.of,
            "ended": self.ended,
            "history": self.history.digest().value,
        }
        if self.entry is not None:
            document["entry"] = self.entry.as_member()
        return document


@dataclass(frozen=True)
class ProjectionKind:
    """One kind of projection: how many rows it has and which history row each is read from.

    ``sources`` is, for a snapshot, an array of the history row each row copies. A kind that
    builds its rows itself gives ``rows``, which takes the plan, what ``sources`` returned, the
    range of rows and the run root.
    """

    count: Callable[[HistoryPlan, ProjectionPlan], int]
    sources: Callable[[HistoryPlan, ProjectionPlan], Any]
    rows: Optional[
        Callable[[GeneratorPlan, Any, int, int, SeedTreeRoot], Tuple[Tuple[Any, ...], ...]]
    ] = None


# -- the history's layout -----------------------------------------------------------------------


def chain_of(history: GeneratorPlan, root: SeedTreeRoot) -> HistoryPlan:
    """The version layout of *history*'s emit: its entities, versions per entity and ended entities.

    Worked out once per process (:func:`~flux_engine.generator.history.plan_history` keeps it).
    """
    from flux_engine.generator.relations import history_pick_of

    pick = history_pick_of(history)
    if pick is None:
        raise _refuse(
            f"projection of {history.expose.expose_id!r}: the emit has no history to project"
        )
    return plan_history(
        root,
        history.sim_id,
        history.expose.expose_id,
        pick.entry,
        kind=pick.kind,
        rows=pick.rows,
        entities=pick.entities,
        time_bounds=(pick.window_start,),
    )


# -- snapshots ----------------------------------------------------------------------------------


def _kept_entities(chain: HistoryPlan, projection: ProjectionPlan) -> "np.ndarray":
    """The entities a snapshot holds, in entity order: all of them, or the live ones under ``drop``."""
    if projection.ended == "drop" and chain.ended is not None:
        return np.flatnonzero(~chain.ended).astype(np.int64)
    return np.arange(chain.entities, dtype=np.int64)


def _snapshot_count(chain: HistoryPlan, projection: ProjectionPlan) -> int:
    """``E`` less the ended entities under ``ended: drop`` (ADR 0027 section 3), else ``E``."""
    if projection.ended == "drop" and chain.ended is not None:
        return chain.entities - int(chain.ended.sum())
    return chain.entities


def _snapshot_sources(chain: HistoryPlan, projection: ProjectionPlan) -> "np.ndarray":
    """The last version row of each kept entity, in entity order."""
    kept = _kept_entities(chain, projection)
    return (chain.versions.offsets[kept] + chain.versions.counts[kept] - 1).astype(np.int64)


KINDS: Dict[str, ProjectionKind] = {
    SNAPSHOTS_KEY: ProjectionKind(count=_snapshot_count, sources=_snapshot_sources),
    EVENTS_KEY: ProjectionKind(
        count=_events.count, sources=_events.sources, rows=_events.events_rows
    ),
    PERIODS_KEY: ProjectionKind(
        count=_periods.count, sources=_periods.sources, rows=_periods.periods_rows
    ),
}


# -- the entry of an emit -----------------------------------------------------------------------


def projection_of(
    extension: Optional[Mapping[str, Any]], emit: str
) -> Optional[Tuple[str, str, Any]]:
    """The generated projection *emit*'s bound extension holds: its key, its ``of`` and its options.

    The options are a snapshot's ``ended`` and, for events and periods, the entry itself. ``None``
    for an emit with none, or with a kind this build does not generate (the plan refuses that
    one earlier, :func:`~flux_engine.seam.projections.refuse_projection`).
    """
    if extension is None:
        return None
    for key in PROJECTION_KEYS:
        if key not in extension or key not in GENERATED_PROJECTIONS:
            continue
        found = _entry_of_key(key, extension, emit)
        if found is not None:
            return found
    return None


def _entry_of_key(
    key: str, extension: Mapping[str, Any], emit: str
) -> Optional[Tuple[str, str, Any]]:
    """The key, ``of`` and options of *emit*'s entry under *key*, or ``None`` for none."""
    if key == SNAPSHOTS_KEY:
        snapshot = snapshot_entry_of(extension, emit, refusal=_code())
        return None if snapshot is None else (key, snapshot.of, snapshot.ended)
    entry: Any
    if key == EVENTS_KEY:
        entry = events_entry_of(extension, emit, refusal=_code())
    elif key == PERIODS_KEY:
        entry = periods_entry_of(extension, emit, refusal=_code())
    else:
        return None
    return None if entry is None else (key, entry.of, entry)


# -- the plan -----------------------------------------------------------------------------------


def _projection(
    key: str, of: str, options: Any, history: GeneratorPlan, plan: GeneratorPlan
) -> ProjectionPlan:
    """The projection of *plan*'s emit, after the checks that need the history's plan."""
    if key not in KINDS:
        raise _refuse(f"{key} ({plan.expose.expose_id}) has no generator")
    if key == SNAPSHOTS_KEY:
        names = {column.column.name for column in history.columns}
        missing = [column.column.name for column in plan.columns if column.column.name not in names]
        if missing:
            raise _refuse(
                f"{key} ({plan.expose.expose_id}): {missing[0]!r} is not a column of {of!r}"
            )
        return ProjectionPlan(key, of, history, options)
    return ProjectionPlan(key, of, history, entry=options, window=plan.time_bounds)


def refuse_rows(label: str, rows: int) -> None:
    """Refuse (``FLX-3405``, as a declared count is) a derived count above ``MOST_ROWS``."""
    if rows > MOST_ROWS:
        raise FluxError(
            FLX_3405, f"{label} derives {rows} rows; a run holds at most {MOST_ROWS} rows"
        )


def _count(key: str, chain: Any, projection: ProjectionPlan, plan: GeneratorPlan) -> int:
    rows = KINDS[key].count(chain, projection)
    refuse_rows(f"{key} ({plan.expose.expose_id})", rows)
    return rows


def derived_rows(
    key: str, of: str, options: Any, history: GeneratorPlan, plan: GeneratorPlan, root: SeedTreeRoot
) -> int:
    """The number of rows *plan*'s emit takes from *history*, worked out from the plan alone."""
    projection = replace(_projection(key, of, options, history, plan), root=root)
    return _count(key, chain_of(history, root), projection, plan)


def project_plan(
    plan: GeneratorPlan,
    key: str,
    of: str,
    options: Any,
    history: GeneratorPlan,
    root: SeedTreeRoot,
) -> GeneratorPlan:
    """*plan* as a projected emit's: its ``rows`` the derived count, and the projection attached."""
    projection = _projection(key, of, options, history, plan)
    count = _count(key, chain_of(history, root), replace(projection, root=root), plan)
    return replace(plan, rows=count, projection=projection)


# -- the rows -----------------------------------------------------------------------------------


def sources_of(plan: GeneratorPlan, root: SeedTreeRoot) -> Any:
    """The history row each row of the projected *plan* is read from, worked out once per process."""
    projection = plan.projection
    assert projection is not None  # the caller routes a projected plan only
    held = (
        root.digest,
        root.seed_tree_version,
        plan.digest().value,
    )
    found = _SOURCES.get(held)
    if found is None:
        found = KINDS[projection.kind].sources(
            chain_of(projection.history, root), replace(projection, root=root)
        )
        if len(_SOURCES) >= _SOURCES_KEPT:
            _SOURCES.clear()
        _SOURCES[held] = found
    return found


def clear_sources() -> None:
    """Drop the sources, the event layouts and the version views this process keeps."""
    _SOURCES.clear()
    _events.clear_layouts()
    _periods.clear_layouts()
    clear_views()


def projection_rows(
    plan: GeneratorPlan, start: int, stop: int, root: Optional[SeedTreeRoot]
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* - 1 of the projected *plan*, each its source row's columns.

    The history's rows come as its own pass writes them, over the range its sources span, so a
    column holds its final value with the rules applied.
    """
    projection = plan.projection
    assert projection is not None  # the caller routes a projected plan only
    if root is None:
        raise _refuse(
            f"projection of {projection.of!r}: a projected emit is generated over the run root"
        )
    if start == stop:
        return ()
    made = KINDS[projection.kind].rows
    if made is not None:
        return made(plan, sources_of(plan, root), start, stop, root)
    sources = sources_of(plan, root)[start:stop]
    low, high = int(sources[0]), int(sources[-1]) + 1
    rows = generate_row_slice(projection.history, low, high, root=root)
    position = {
        column.column.name: index for index, column in enumerate(projection.history.columns)
    }
    taken = [position[column.column.name] for column in plan.columns]
    return tuple(
        tuple(rows[int(source) - low][index] for index in taken) for source in sources.tolist()
    )
