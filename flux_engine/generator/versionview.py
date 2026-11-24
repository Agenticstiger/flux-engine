# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's versions as its projections read them (ADR 0027 sections 4 and 5).

A change event and a periodic row are each about a version of an entity. What they need of a
version is its validity, its activation, its tracked columns and the version before it, and none
of it needs a cell: the chain's arrays and the tracked levels are the history's own functions
(:func:`~flux_engine.generator.history.chain_columns`,
:func:`~flux_engine.generator.history.tracked_levels`), read over the version rows once.

:class:`VersionView` holds those arrays for one history and one run root. It gives:

* each version's validity as instants in milliseconds (:meth:`VersionView.valid_ms`), an open
  version valid up to the window's end, and a closed one up to its end under ``nextStart`` or
  through its end's day under ``dayBefore``;
* each column a ``when`` may read, as the stored values the history's rows hold
  (:meth:`VersionView.values`);
* a ``when`` compiled over the history's columns (:meth:`VersionView.compile_when`) and
  evaluated over version rows, with ``previous`` the version before in the entity's chain
  (:meth:`VersionView.holds`).

A ``when`` reads nothing a cell draws, so its answer is known at plan time and the projection's
count with it. The one column a cell draws that a ``when`` may name is the history's end column
(ADR 0027, the amendment of 2026-11-24): it holds the end value on an ending version and a drawn
level on any other, so the answer is known only where every level the column's statistics can
draw gives the same one (:meth:`VersionView.holds` refuses ``FLX-3403`` where they differ).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot
from flux_engine.expr2.check import PREDICATE, Checked, check
from flux_engine.expr2.parser import Side, parse
from flux_engine.expr2.scope import Expose, Model, Versions
from flux_engine.expr2.types import column_type
from flux_engine.generator import rows as _rows
from flux_engine.generator.drawnend import DrawnEnd
from flux_engine.generator.history import (
    HistoryPlan,
    chain_columns,
    open_end_unit,
    tracked_levels,
)
from flux_engine.generator.relations import (
    FLX_3403,
    ChainColumn,
    EntityColumn,
    TrackedColumn,
    history_pick_of,
)
from flux_engine.seam.contract import LogicalKind

__all__ = ["DAY_MS", "VersionView", "unit_value", "value_unit", "view_of"]

DAY_MS = 86_400_000
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()


def value_unit(value: Any) -> int:
    """A DATE or TIMESTAMP cell as an instant in its own unit: days, or milliseconds since the epoch."""
    if isinstance(value, date):
        return value.toordinal() - _EPOCH_ORDINAL
    return int(value)


def unit_value(kind: LogicalKind, unit: int) -> Any:
    """An instant in *kind*'s unit as the cell the generator writes: a date, or milliseconds."""
    return date.fromordinal(unit + _EPOCH_ORDINAL) if kind is LogicalKind.DATE else unit


class _Version:
    """One version row as a :class:`~flux_engine.expr2.evaluate.RowView`: its own columns and the version before."""

    __slots__ = ("_extra", "_level", "_row", "_view")

    def __init__(
        self,
        view: "VersionView",
        row: int,
        extra: Optional[Mapping[str, Any]] = None,
        level: Any = None,
    ) -> None:
        self._view, self._row, self._extra, self._level = view, row, extra, level

    @property
    def key(self) -> Hashable:
        return self._row

    def get(self, column: str) -> Any:
        if self._extra is not None and column in self._extra:
            return self._extra[column]
        drawn = self._view.drawn
        if drawn is not None and column == drawn.column:
            return drawn.at(self._row, self._level)
        return self._view.values(column)[self._row]

    def follow(self, relationship: str) -> Optional["_Version"]:  # pragma: no cover
        raise NotImplementedError("a when reads the version's own columns")

    def previous(self) -> Optional["_Version"]:
        if self._view.first[self._row]:
            return None
        return _Version(self._view, self._row - 1, level=self._level)

    def children(self, expose: str, relationship: str) -> Sequence[Any]:  # pragma: no cover
        raise NotImplementedError("a when reads the version's own columns")

    def siblings(self, relationship: str) -> Sequence[Any]:  # pragma: no cover
        raise NotImplementedError("a when reads the version's own columns")


class VersionView:
    """The versions of one history, for one run root: instants, columns and conditions."""

    def __init__(
        self, history: "_rows.GeneratorPlan", chain: HistoryPlan, root: SeedTreeRoot
    ) -> None:
        pick = history_pick_of(history)
        assert pick is not None  # the caller checked that the emit has a history
        self.history, self.chain, self.root, self.pick = history, chain, root, pick
        drivers = history.relations.drivers if history.relations is not None else ()
        roles: Dict[str, Optional[str]] = {"start": None, "end": None, "activation": None}
        self._chains: Dict[str, ChainColumn] = {}
        self._tracked: Dict[str, TrackedColumn] = {}
        entity: List[str] = []
        for name, driver in drivers:
            if isinstance(driver, ChainColumn):
                roles[driver.role] = name
                self._chains[name] = driver
            elif isinstance(driver, TrackedColumn):
                self._tracked[name] = driver
            elif isinstance(driver, EntityColumn):
                entity.append(name)
        self.start, self.end, self.activation = roles["start"], roles["end"], roles["activation"]
        self.entity = tuple(entity)
        rows = np.arange(chain.rows, dtype=np.int64)
        self.entities = chain.entities_of(rows)
        self.ordinals = rows - chain.versions.offsets[self.entities]
        self.first = self.ordinals == 0
        self._instants: Optional[Tuple["np.ndarray", "np.ndarray", "np.ndarray"]] = None
        self._values: Dict[str, List[Any]] = {}
        self.drawn = DrawnEnd.of(history, chain, pick.entry.end, self._tracked)

    # -- the chain --------------------------------------------------------------------------

    @property
    def kind(self) -> LogicalKind:
        """The chain's unit: DATE days or TIMESTAMP milliseconds."""
        return self.pick.kind

    def instants(self) -> Tuple["np.ndarray", "np.ndarray", "np.ndarray"]:
        """Each version row's start and end in the chain's unit, and whether it is open."""
        if self._instants is None:
            self._instants = chain_columns(
                self.root,
                self.history.sim_id,
                self.history.expose.expose_id,
                self.pick.entry,
                self.pick.kind,
                self.chain,
                np.arange(self.chain.rows, dtype=np.int64),
            )
        return self._instants

    def valid_ms(self, window_end: int) -> Tuple["np.ndarray", "np.ndarray"]:
        """The instants each version is valid over, in milliseconds: from its start up to its end, excluded.

        An open version is valid up to *window_end*. Under ``dayBefore`` a DATE end covers its whole day.
        """
        starts, ends, opened = self.instants()
        scale = DAY_MS if self.kind is LogicalKind.DATE else 1
        whole = DAY_MS if self.pick.entry.chain == "dayBefore" else 0
        valid_from = starts.astype(np.int64) * scale
        valid_to = np.where(opened, window_end, ends.astype(np.int64) * scale + whole)
        return valid_from, valid_to.astype(np.int64)

    # -- the columns a when reads -----------------------------------------------------------

    def values(self, name: str) -> List[Any]:
        """The stored value of column *name* on every version row, as the history's rows hold it."""
        held = self._values.get(name)
        if held is None:
            held = self._read(name)
            self._values[name] = held
        return held

    def _read(self, name: str) -> List[Any]:
        rows = np.arange(self.chain.rows, dtype=np.int64)
        driver = self._chains.get(name)
        if driver is not None:
            return self._chain_values(driver, rows)
        tracked = self._tracked.get(name)
        if tracked is not None:
            return self._tracked_values(name, tracked, rows)
        raise KeyError(name)

    def _chain_values(self, driver: ChainColumn, rows: "np.ndarray") -> List[Any]:
        starts, ends, opened = self.instants()
        if driver.role == "start":
            return [driver.value_of(unit) for unit in starts.tolist()]
        if driver.role == "activation":
            firsts = self.chain.first_starts[self.entities]
            return [driver.value_of(unit) for unit in firsts.tolist()]
        sentinel = open_end_unit(self.pick.entry, self.pick.kind)
        return [
            driver.value_of(sentinel if is_open else unit)
            for unit, is_open in zip(ends.tolist(), opened.tolist(), strict=True)
        ]

    def _tracked_values(self, name: str, driver: TrackedColumn, rows: "np.ndarray") -> List[Any]:
        levels = tracked_levels(
            self.root,
            self.history.sim_id,
            self.history.expose.expose_id,
            driver.specs,
            self.chain,
            rows,
        )[driver.index]
        column = self.history.column_plan(name).column
        converted = {level: _rows._level_to_value(level, column) for level in set(levels.tolist())}
        found = [converted[level] for level in levels.tolist()]
        end = self.pick.entry.end
        if end is not None and end.column == name and end.value is not None:
            ending = self.chain.ending_rows(rows)
            value = _rows._level_to_value(end.value, column)
            found = [
                value if held else old for old, held in zip(found, ending.tolist(), strict=True)
            ]
        return found

    # -- a condition over versions ----------------------------------------------------------

    def compile_when(self, text: str, extra: Optional[Mapping[str, str]] = None) -> Checked:
        """*text*, a FLUID SQL/1 predicate, checked over the history's columns and *extra* ones.

        *extra* names columns that only the row reads (a period column) with their declared types.
        """
        columns = {}
        for column_plan in self.history.columns:
            column = column_plan.column
            columns[column.name] = column_type(
                column.declared_type, enum=column.enum_values or None
            )
        for name, declared in (extra or {}).items():
            columns[name] = column_type(declared)
        assert self.start is not None
        versions = Versions(self.entity, self.start, self.end)
        scope = Expose(self.history.expose.expose_id, columns, {}, (), versions)
        model = Model({scope.name: scope})
        return check(parse(text, side=Side.FLUX), model, scope.name, position=PREDICATE)

    def holds(
        self,
        checked: Checked,
        rows: "np.ndarray",
        extra: Optional[Mapping[str, Any]] = None,
        where: str = "when",
    ) -> "np.ndarray":
        """Whether *checked* is TRUE on each version row of *rows* (*extra* gives a row's own further columns)."""
        if self.drawn is not None and self.drawn.is_read_by(checked):
            return _holds_over_levels(self, checked, rows, extra, where)
        return np.array(
            [bool(checked.evaluate(_Version(self, int(row), extra))) for row in rows.tolist()],
            dtype=bool,
        )


def _holds_over_levels(
    view: VersionView,
    checked: Checked,
    rows: "np.ndarray",
    extra: Optional[Mapping[str, Any]],
    where: str,
) -> "np.ndarray":
    """:meth:`VersionView.holds` for a condition that reads the history's drawn end column.

    It is evaluated over each level the column's statistics can draw, and refused (``FLX-3403``,
    naming *where*) on a version where the levels give different answers: the answer would depend
    on a draw.
    """
    drawn = view.drawn
    assert drawn is not None
    found = []
    for row in rows.tolist():
        answers = {
            bool(checked.evaluate(_Version(view, int(row), extra, level))) for level in drawn.levels
        }
        if len(answers) > 1:
            raise FluxError(
                FLX_3403,
                f"{where} reads {drawn.column!r}, the history's end column, and its result "
                f"depends on a drawn value: {drawn.column!r} holds {drawn.value!r} on an ending "
                f"version and one of {list(drawn.levels)} on any other, and version row {row} "
                "answers differently for them; read it only where every level gives the same "
                "answer, as in a test for the end value",
            )
        found.append(answers.pop() if answers else False)
    return np.array(found, dtype=bool)


#: How many views a process keeps (each holds arrays over the history's versions).
_VIEWS_KEPT = 2
_VIEWS: Dict[Tuple[Any, ...], VersionView] = {}


def view_of(history: "_rows.GeneratorPlan", chain: HistoryPlan, root: SeedTreeRoot) -> VersionView:
    """The view of *history*'s versions for *root*, worked out once per process."""
    held = (root.digest, root.seed_tree_version, history.digest().value)
    found = _VIEWS.get(held)
    if found is None:
        found = VersionView(history, chain, root)
        if len(_VIEWS) >= _VIEWS_KEPT:
            _VIEWS.clear()
        _VIEWS[held] = found
    return found


def clear_views() -> None:
    """Drop the views this process keeps."""
    _VIEWS.clear()
