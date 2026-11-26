# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Kernel 0.2.0's generated rows a column at a time (ADR 0010, step 4).

Under kernel 0.2.0 a cell's draw ``d`` is the block of counter ``(row, 0, d, attempt)`` under
its column's key (KERNEL-SPEC-2 section 2), so the draws of every row of a slice for one
column are one Philox call. This module computes a slice that way: each column's values
as arrays, by the array functions of :mod:`flux_engine.core.kernel2`, then the rows from
the columns. Every value is the one the per-row path (:func:`flux_engine.generator.rows._cell`)
computes for the same cell, and ``tests/generator/test_columns.py`` holds the two to each
other, for every family and for refused declarations too.

A plan the contract relates is computed the same way. The relation reader first works out,
for the whole slice, every identifier and composite key, foreign key null and parent row
its cells need, by the array functions (:meth:`RelationReader.prefetch`), then reads its
cells per row from what it worked out.

A column drawn ``given`` another stays on this path. Its uniform is the one it draws
without ``given``; each row's level of the parent column picks the cumulative table the
uniform is searched in (the parent's levels are drawn first, from the parent's own key),
which is what the per-row path's ``categorical`` does with the weights declared for the
level the parent drew in that row.

What stays per row, as ADR 0010 says: a column this module does not draw (it is given to
the per-row path, cell by cell), a column drawn ``given`` another that a relation drives,
and rendering. A slice in which anything is refused, or anything
else is raised, is left to the per-row path entirely, so what it raises is what it always
raised, at the row and column it always raised it for. A long slice is taken in chunks, so
the arrays of one chunk are all a pass holds.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, cast

import numpy as np

from flux_engine.core import kernel2, rng
from flux_engine.core.errors import FluxError
from flux_engine.generator import rows as _rows
from flux_engine.generator.allocation import allocated_given, allocated_levels
from flux_engine.generator.families import ColumnType, Family
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.statistics import integer_width_bounds, joint_level
from flux_engine.seam.contract import LogicalKind

__all__ = ["column_rows"]

Rows = Tuple[Tuple[Any, ...], ...]

#: Rows a chunk holds. Its arrays, and the relation reader made for it, are all a pass
#: keeps. A cache the reader clears when it is full costs time to fill again, never a value.
CHUNK_ROWS = 1 << 14


def column_rows(
    plan: "_rows.GeneratorPlan",
    start: int,
    stop: int,
    streams: CellStreams,
    held: Optional["_rows.HeldLevels"] = None,
) -> Optional[Rows]:
    """Rows *start* to *stop* - 1 of a kernel 0.2.0 *plan*, a column at a time.

    ``None`` when the slice is the per-row path's: a conditional plan, a slice in which
    anything is refused or raised, or rows past what an int64 array holds. *held* gives the
    level each row holds for a column a projection sets, which a column drawn given it reads
    (:func:`flux_engine.generator.rows.cells_before_rules`).
    """
    if stop > kernel2.ARRAY_ROWS or _related_given(plan) or _carried_given(plan):
        return None
    out: List[Tuple[Any, ...]] = []
    for first in range(start, stop, CHUNK_ROWS):
        rows = np.arange(first, min(stop, first + CHUNK_ROWS), dtype=np.int64)
        try:
            with localcontext(_rows._KERNEL_CONTEXT):
                columns = _chunk(plan, streams, rows, _held_chunk(held, first - start, rows))
        except Exception:
            # The per-row path works row by row and this one column by column, so the two
            # can meet an error in another order. Whatever this path meets, the slice is
            # the per-row path's, and so is any error raised for it.
            return None
        if columns:
            out.extend(zip(*columns, strict=True))
        else:
            out.extend(() for _ in range(rows.shape[0]))
    return tuple(out)


def _related_given(plan: "_rows.GeneratorPlan") -> bool:
    """Whether a relation drives a column that is drawn ``given`` another, or that is given.

    The relation reader takes the given level into its own cells, so such a plan is the
    per-row path's. So is a plan with a column drawn given a column of a linked row: its level
    is the final value of another expose's row, which the row pass reads (ADR 0023 section 3).
    So is a plan with a foreign key whose parent is picked by a weight column (section 4): the
    thresholds sum the weights of the parent's final rows.
    """
    if plan.relations is None:
        return False
    from flux_engine.generator.relations import ForeignKey

    if plan.relations.givens:
        return True
    if any(
        isinstance(driver, ForeignKey) and driver.pick == "weighted"
        for _, driver in plan.relations.drivers
    ):
        return True
    involved = _rows._given_columns(plan) | {
        column_plan.column.name for column_plan in plan.columns if column_plan.statistics.parents
    }
    return any(plan.relations.driver(name) is not None for name in involved)


def _carried_given(plan: "_rows.GeneratorPlan") -> bool:
    """Whether a history's column that is not carried is drawn ``given`` a carried one.

    A carried column draws its level on its entity's first version row and the other column on its
    own row, so the two cannot share one array of parent levels: the per-row path takes the slice.
    """
    from flux_engine.generator.relations import carried_columns

    carried = carried_columns(plan)
    return any(
        column_plan.column.name not in carried
        and any(parent in carried for parent in column_plan.statistics.parents)
        for column_plan in plan.columns
    )


def _held_chunk(
    held: Optional["_rows.HeldLevels"], offset: int, rows: "np.ndarray"
) -> Dict[str, List[Optional[str]]]:
    """The held levels of the chunk *rows*, which starts *offset* rows into the slice."""
    if not held:
        return {}
    stop = offset + rows.shape[0]
    return {name: list(levels[offset:stop]) for name, levels in held.items()}


def _chunk(
    plan: "_rows.GeneratorPlan",
    streams: CellStreams,
    rows: "np.ndarray",
    held: Optional[Dict[str, List[Optional[str]]]] = None,
) -> List[List[Any]]:
    """Every column of *plan* over *rows*: drawn as arrays, or read from the relations.

    A column *held* names is given by the levels *held* gives it: they stand in the chunk's
    levels, which every column drawn given it reads, in place of the levels its own draw makes.
    A row that holds no level (``None``) keeps the level its own draw makes.
    """
    levels = _with_own_levels(plan, streams, rows, held or {})
    if plan.relations is None:
        return [_column(column_plan, plan, streams, rows, levels) for column_plan in plan.columns]
    from flux_engine.generator.relations import carried_columns, history_pick_of, owner_of

    reader = _rows._relation_reader(streams, plan)
    owner = owner_of(plan)
    drivers = [plan.relations.driver(column_plan.column.name) for column_plan in plan.columns]
    pick = history_pick_of(plan)
    carried = carried_columns(plan)
    if pick is None or not carried:
        reader.prefetch(owner, plan.columns, drivers, rows)
        columns: List[List[Any]] = []
        for column_plan, driver in zip(plan.columns, drivers, strict=True):
            if driver is None:
                columns.append(_column(column_plan, plan, streams, rows, levels))
            else:
                columns.append(reader.column_values(owner, column_plan, driver, rows))
        return columns
    return _carried_chunk(plan, streams, rows, reader, owner, drivers, pick, carried, levels)


def _with_own_levels(
    plan: "_rows.GeneratorPlan",
    streams: CellStreams,
    rows: "np.ndarray",
    held: Mapping[str, List[Optional[str]]],
) -> Dict[str, List[str]]:
    """The chunk's levels of the *held* columns: the held level, or the own draw's where a row holds none.

    A column held on some rows only is drawn for the chunk, its parents first, and each row that
    holds no level takes the level that draw makes, as the per-row path keeps its own level there.
    """
    levels: Dict[str, List[str]] = {}
    partly: Dict[str, List[Optional[str]]] = {}
    for name, found in held.items():
        if None in found:
            partly[name] = found
        else:
            levels[name] = cast(List[str], found)
    for index in _rows._draw_order(plan) if partly else ():
        name = plan.columns[index].column.name
        if name in partly:
            drawn = _levels_of(plan, name, streams, rows, levels)
            levels[name] = [
                own if level is None else level
                for level, own in zip(partly[name], drawn, strict=True)
            ]
    return levels


def _carried_chunk(
    plan: "_rows.GeneratorPlan",
    streams: CellStreams,
    rows: "np.ndarray",
    reader: Any,
    owner: Any,
    drivers: Sequence[Any],
    pick: Any,
    carried: Any,
    levels: Dict[str, List[str]],
) -> List[List[Any]]:
    """The columns of a history's slice: a carried one is drawn once for each entity it reaches."""
    # A history's carried columns are drawn once for each entity the slice reaches, on its first
    # version row, and gathered to every version row of the slice (ADR 0017 section 6).
    unique, inverse = np.unique(reader.first_rows_of(owner, pick, rows), return_inverse=True)
    names = [column_plan.column.name for column_plan in plan.columns]
    own = [None if name in carried else d for name, d in zip(names, drivers, strict=True)]
    held = [d if name in carried else None for name, d in zip(names, drivers, strict=True)]
    reader.prefetch(owner, plan.columns, own, rows)
    reader.prefetch(owner, plan.columns, held, unique)
    gather = inverse.reshape(-1).tolist()
    first_levels: Dict[str, List[str]] = {}
    columns: List[List[Any]] = []
    for column_plan, driver in zip(plan.columns, drivers, strict=True):
        if column_plan.column.name in carried:
            if driver is None:
                values = _column(column_plan, plan, streams, unique, first_levels)
            else:
                values = reader.column_values(owner, column_plan, driver, unique)
            columns.append([values[index] for index in gather])
        elif driver is None:
            columns.append(_column(column_plan, plan, streams, rows, levels))
        else:
            columns.append(reader.column_values(owner, column_plan, driver, rows))
    return columns


def _column(
    column_plan: "_rows.ColumnPlan",
    plan: "_rows.GeneratorPlan",
    streams: CellStreams,
    rows: "np.ndarray",
    levels: Dict[str, List[str]],
) -> List[Any]:
    values = _vectorized(column_plan, plan, streams, rows, levels)
    if values is not None:
        return values
    sim_id, expose_id, h = plan.sim_id, plan.expose.expose_id, plan.seam_digest
    return [
        _rows._cell_in_context(column_plan, streams, sim_id, expose_id, row, h)
        for row in rows.tolist()
    ]


def _vectorized(
    column_plan: "_rows.ColumnPlan",
    plan: "_rows.GeneratorPlan",
    streams: CellStreams,
    rows: "np.ndarray",
    levels: Dict[str, List[str]],
) -> Optional[List[Any]]:
    """The column's values, as :func:`_rows._cell_in_context` gives them, or ``None``."""
    column, statistics = column_plan.column, column_plan.statistics
    parents = (
        _given_levels(plan, statistics.parents, streams, rows, levels)
        if statistics.parents
        else None
    )
    if column_plan.column_type is ColumnType.IDENTIFIER:
        length = statistics.identifier_length or _rows.IDENTIFIER_DEFAULT_LENGTH
        as_uuid = column.declared_type.strip().lower() in _rows._UUID_TYPES
        _rows._require_identifier_shape(length, as_uuid)
        digits = kernel2.column_identifier_digits(
            _rows._seam_bytes(plan.seam_digest), streams.key_label(column.name), rows
        )
        return [_rows._identifier_of_digits(d, length=length, as_uuid=as_uuid) for d in digits]
    if column_plan.allocated_rows is not None:
        # The draws a cell makes are left unread: the class is the one of its row's slot.
        labels = _allocated(column_plan, plan, streams, rows)
        if statistics.bins is None:
            as_values = {
                name: _rows._level_to_value(name, column)
                for name in set(labels)
                if name is not None
            }
            return [None if name is None else as_values[name] for name in labels]
        # The histogram's value is its grid draw inside the bin, at the index it always had.
        draw = 0 if statistics.null_fraction is None else 1
        return _histogram(column_plan, streams.column_key(column.name), rows, draw, None, labels)
    key = streams.column_key(column.name)
    if column_plan.copula is not None:
        return _copula_values(column_plan, streams, rows, key)
    if statistics.null_fraction is None:
        values = _draws(column_plan, key, rows, 0, parents)
        _set_conditioned(column_plan, plan, streams, rows, levels, key, values)
        return values
    # The null draw comes first and always happens; the value is drawn at index 1 either way.
    nulls = kernel2.column_bernoulli(
        kernel2.column_uniform53(key, rows, 0), statistics.null_fraction
    ).tolist()
    values = _draws(column_plan, key, rows, 1, parents)
    if values is None:
        return None
    return [None if null else value for null, value in zip(nulls, values, strict=True)]


def _copula_values(
    column_plan: "_rows.ColumnPlan", streams: CellStreams, rows: "np.ndarray", key: kernel2.Key
) -> List[Any]:
    """A copula column's values: the copula's draws for *rows*, and its NULL draw at index 0.

    What :func:`_rows._copula_cell` makes for each row; the copula's draws are shared by the
    columns of the chunk (:meth:`~flux_engine.generator.copula.CopulaDraw.column_values`).
    """
    copula = column_plan.copula
    assert copula is not None
    values = copula.column_values(streams.root, rows, _rows.nfc(column_plan.column.name))
    fraction = column_plan.statistics.null_fraction
    if fraction is None:
        return values
    nulls = kernel2.column_bernoulli(kernel2.column_uniform53(key, rows, 0), fraction).tolist()
    return [None if null else value for null, value in zip(nulls, values, strict=True)]


def _set_conditioned(
    column_plan: "_rows.ColumnPlan",
    plan: "_rows.GeneratorPlan",
    streams: CellStreams,
    rows: "np.ndarray",
    levels: Dict[str, List[str]],
    key: kernel2.Key,
    values: Optional[List[Any]],
) -> None:
    """Put into *values* the level each row's group decides for the column, where it does."""
    column = column_plan.column
    conditions = plan.conditions
    if values is None or conditions is None or column.name not in conditions.touched:
        return
    u53 = kernel2.column_uniform53(key, rows, 0)
    decided = conditions.column_levels(
        column.name, rows, u53, lambda name: _levels_of(plan, name, streams, rows, levels)
    )
    if _rows.is_binned(column_plan):
        # The group chose each row's bin; the place inside it is the histogram's own draw at
        # index 1, the bin's pick at index 0 left unread (ADR 0036 decision 5).
        names = _rows.class_names(column_plan)
        positions = sorted(decided)
        if not positions:
            return
        drawn = _histogram(
            column_plan,
            key,
            rows[positions],
            0,
            None,
            [names.index(decided[position]) for position in positions],
        )
        for position, value in zip(positions, drawn or [], strict=True):
            values[position] = value
        return
    for position, level in decided.items():
        values[position] = _rows._level_to_value(level, column)


def _allocated(
    column_plan: "_rows.ColumnPlan",
    plan: "_rows.GeneratorPlan",
    streams: CellStreams,
    rows: "np.ndarray",
) -> List[Any]:
    """The class of each of *rows* under exact allocation: a level, a bin's index or ``None``."""
    assert column_plan.allocated_rows is not None
    if column_plan.allocated_given is not None:
        return allocated_given(
            column_plan.statistics,
            column_plan.allocated_given,
            streams.root,
            plan.sim_id,
            plan.expose.expose_id,
            column_plan.allocated_rows,
            rows,
        )
    return allocated_levels(
        column_plan.statistics,
        streams.root,
        plan.sim_id,
        plan.expose.expose_id,
        column_plan.allocated_rows,
        rows,
    )


def _levels_of(
    plan: "_rows.GeneratorPlan",
    name: str,
    streams: CellStreams,
    rows: "np.ndarray",
    cache: Dict[str, List[str]],
) -> List[str]:
    """The level each of *rows* draws for the categorical column *name* (a given column's parent).

    What :func:`_rows._given_cell` returns as its level: the column's first draw, through
    the weights of its own parent's level when it is drawn given another. Kept for the
    chunk, so a parent several columns are given is drawn once.
    """
    if name in cache:
        return cache[name]
    column_plan = plan.column_plan(name)
    statistics, column = column_plan.statistics, column_plan.column
    if _rows.is_binned(column_plan):
        # A column drawn from bins is given by the bin of the value it draws.
        drawn = _vectorized(column_plan, plan, streams, rows, cache)
        if drawn is None:
            raise FluxError(_rows.FLX_3402, f"{name}: not drawn by arrays")
        cache[name] = [_rows.bin_class(column_plan, value) for value in drawn]
        return cache[name]
    if column_plan.allocated_rows is not None:
        # The first draw is left unread: the level is the one of the row's slot.
        cache[name] = [str(label) for label in _allocated(column_plan, plan, streams, rows)]
        return cache[name]
    u53 = kernel2.column_uniform53(streams.column_key(name), rows, 0)
    if not statistics.parents:
        names = kernel2.column_categorical(
            u53, tuple(_rows._categorical_levels(statistics, column, None).items())
        )
    else:
        parents = _given_levels(plan, statistics.parents, streams, rows, cache)

        def pick(level: str, take: "np.ndarray") -> List[Any]:
            weights = _rows._categorical_levels(statistics, column, level)
            return kernel2.column_categorical(u53[take], tuple(weights.items()))

        names = _by_parent(parents, pick)
    conditions = plan.conditions
    if conditions is not None and name in conditions.touched:
        # The groups fix this column, or choose the table its uniform is searched in.
        decided = conditions.column_levels(
            name, rows, u53, lambda other: _levels_of(plan, other, streams, rows, cache)
        )
        for position, level in decided.items():
            names[position] = level
    cache[name] = names
    return names


def _given_levels(
    plan: "_rows.GeneratorPlan",
    names: Tuple[str, ...],
    streams: CellStreams,
    rows: "np.ndarray",
    cache: Dict[str, List[str]],
) -> List[str]:
    """The level each row is drawn given: its parent's, or the combination of its parents'.

    A column drawn given one column takes that column's level. Given several, each row takes
    :func:`~flux_engine.generator.statistics.joint_level` of one level of each, in the order
    ``given`` lists them, which is how the weight tables are keyed. The combinations are found
    once for the chunk, as integer codes, and a row's key is looked up from its code.
    """
    columns = [_levels_of(plan, name, streams, rows, cache) for name in names]
    if len(columns) == 1:
        return columns[0]
    uniques: List[List[str]] = []
    codes = []
    for level_list in columns:
        found, inverse = np.unique(np.array(level_list), return_inverse=True)
        uniques.append(found.tolist())
        codes.append(inverse.reshape(-1))
    shape = tuple(len(found) for found in uniques)
    combined = np.ravel_multi_index(tuple(codes), shape)
    keys: Dict[int, str] = {}
    for code in np.unique(combined).tolist():
        parts = np.unravel_index(code, shape)
        keys[code] = joint_level([uniques[i][int(part)] for i, part in enumerate(parts)])
    return [keys[code] for code in combined.tolist()]


def _by_parent(parents: List[str], make: Any) -> List[Any]:
    """Each row's value from ``make(level, positions)``, called once per level of *parents*.

    ``make`` returns the values of the rows at *positions*, which are the rows whose parent
    drew that level; the result is in row order.
    """
    out: List[Any] = [None] * len(parents)
    if not parents:
        return out
    uniques, inverse = np.unique(np.array(parents), return_inverse=True)
    order = np.argsort(inverse, kind="stable")
    edges = np.concatenate(([0], np.cumsum(np.bincount(inverse, minlength=uniques.shape[0]))))
    for index, level in enumerate(uniques.tolist()):
        take = order[edges[index] : edges[index + 1]]
        for position, value in zip(take.tolist(), make(level, take), strict=True):
            out[position] = value
    return out


def _draws(
    column_plan: "_rows.ColumnPlan",
    key: kernel2.Key,
    rows: "np.ndarray",
    draw: int,
    parents: Optional[List[str]] = None,
) -> Optional[List[Any]]:
    """:func:`_rows._draw_value` for every row, its first draw at index *draw*.

    A column drawn given another takes the parent's level of each row in *parents*, and is
    drawn once per level, over the rows that level holds.
    """
    if parents is None:
        return _draws_at(column_plan, key, rows, draw, None)

    def pick(level: str, take: "np.ndarray") -> List[Any]:
        drawn = _draws_at(column_plan, key, rows[take], draw, level)
        if drawn is None:
            raise FluxError(_rows.FLX_3402, f"{column_plan.column.name}: not drawn by arrays")
        return drawn

    return _by_parent(parents, pick)


def _draws_at(
    column_plan: "_rows.ColumnPlan",
    key: kernel2.Key,
    rows: "np.ndarray",
    draw: int,
    given_level: Optional[str],
) -> Optional[List[Any]]:
    column, statistics, family = column_plan.column, column_plan.statistics, column_plan.family
    kind = column.logical_type.kind
    if family is Family.CATEGORICAL:
        levels = _rows._categorical_levels(statistics, column, given_level)
        names = kernel2.column_categorical(
            kernel2.column_uniform53(key, rows, draw), tuple(levels.items())
        )
        values = {name: _rows._level_to_value(name, column) for name in set(names)}
        return [values[name] for name in names]
    if family is Family.BOUNDED_NORMAL:
        return _bounded_normal(column_plan, key, rows, draw)
    if family is Family.RATE:
        return _rate(column_plan, key, rows, draw)
    if family is not Family.UNIFORM_EMPIRICAL:
        return None
    if statistics.bins is not None:
        return _histogram(column_plan, key, rows, draw, given_level)
    low, high = statistics.range_min, statistics.range_max
    if low is None or high is None:
        return None
    step = _rows._grid_step(column) if kind is LogicalKind.DECIMAL else Decimal(1)
    return _on_grid(column_plan, key, rows, draw, low, high, step)


def _on_grid(
    column_plan: "_rows.ColumnPlan",
    key: kernel2.Key,
    rows: "np.ndarray",
    draw: int,
    low: Decimal,
    high: Decimal,
    step: Decimal,
) -> List[Any]:
    """:func:`_rows._uniform_on_grid` then :func:`_rows._from_grid`, for every row."""
    low_units, step_units, points, exponent = _rows._grid_of(
        _rows._exact(low), _rows._exact(high), _rows._exact(step)
    )
    chosen = kernel2.column_bounded(key, rows, draw, points).tolist()
    return _grid_values(column_plan, [low_units + k * step_units for k in chosen], exponent)


def _grid_values(column_plan: "_rows.ColumnPlan", units: List[int], exponent: int) -> List[Any]:
    """:func:`_rows._from_grid` of ``units * 10**exponent`` for each of *units*."""
    column = column_plan.column
    kind = column.logical_type.kind
    if exponent == 0 and kind in (LogicalKind.INT, LogicalKind.TIMESTAMP):
        # A grid counted in whole units: ``to_integral_value`` leaves each point as it is.
        # (A grid's exponent is never above zero: _rows._common_exponent caps it there.)
        return units
    # A column repeats grid points, so each distinct point is built once and shared (a
    # Decimal is immutable).
    built = {
        value: _rows._from_grid(_rows._decimal_from_units(value, exponent), column)
        for value in set(units)
    }
    return [built[value] for value in units]


def _bin_picks(
    column_plan: "_rows.ColumnPlan",
    key: kernel2.Key,
    rows: "np.ndarray",
    draw: int,
    given_level: Optional[str],
) -> List[Any]:
    """The bin index each row's pick at *draw* gives, by the column's bin weights."""
    column, statistics = column_plan.column, column_plan.statistics
    if statistics.bin_weights_given is not None:
        if given_level is None:
            raise FluxError(_rows.FLX_3402, f"{column.name}: a conditional column needs a level")
        weights = statistics.bin_weights_given[given_level]  # a KeyError leaves it to the rows
    else:
        weights = statistics.bin_weights or ()
    table = _rows._bin_levels(tuple(weights))
    if table is None:
        raise FluxError(_rows.FLX_3402, f"{column.name}: binWeights sum to zero")
    levels, positions = table
    names = kernel2.column_categorical(
        kernel2.column_uniform53(key, rows, draw), tuple(levels.items())
    )
    return [positions[name] for name in names]


def _histogram(
    column_plan: "_rows.ColumnPlan",
    key: kernel2.Key,
    rows: "np.ndarray",
    draw: int,
    given_level: Optional[str] = None,
    forced: Optional[List[Any]] = None,
) -> Optional[List[Any]]:
    """:func:`_rows._draw_histogram` for every row: a bin at *draw*, a grid point at *draw* + 1.

    With *forced*, each row's bin index (``None`` for a row whose value is NULL), the pick is
    left unread and a row of ``None`` keeps ``None``: its grid draw is made and not read.
    """
    column, statistics = column_plan.column, column_plan.statistics
    bins = statistics.bins or ()
    picks = _bin_picks(column_plan, key, rows, draw, given_level) if forced is None else forced
    if forced is None:
        index_of = np.asarray(picks, dtype=np.int64)
    else:
        index_of = np.array([-1 if pick is None else pick for pick in picks], dtype=np.int64)
    step = _rows._grid_step(column)
    integral = column.logical_type.kind is LogicalKind.INT
    out = np.full(len(picks), None, dtype=object)
    # Rows grouped by bin (a NULL row, index -1, is in no group).
    order = np.argsort(index_of, kind="stable")
    present, firsts = np.unique(index_of[order], return_index=True)
    ends = [*firsts.tolist()[1:], len(order)]
    for index, first, end in zip(present.tolist(), firsts.tolist(), ends, strict=True):
        if index < 0:
            continue
        chosen_rows = order[first:end]
        low_units, step_units, points, exponent = _rows._grid_of(
            _rows._exact(bins[index]),
            _rows._exact(_rows._bin_top(bins, index, step)),
            _rows._exact(step),
        )
        picked = kernel2.column_bounded(key, rows[chosen_rows], draw + 1, points)
        # Each distinct grid point is converted once; the rows index into the results.
        distinct, inverse = np.unique(picked, return_inverse=True)
        converted = np.empty(distinct.shape[0], dtype=object)
        if integral and exponent == 0:
            # A grid counted in whole units: int() of each point is its units.
            converted[:] = [low_units + k * step_units for k in distinct.tolist()]
        else:
            drawn = (
                _rows._decimal_from_units(low_units + k * step_units, exponent)
                for k in distinct.tolist()
            )
            converted[:] = [int(d) if integral else _rows._from_grid(d, column) for d in drawn]
        out[chosen_rows] = converted[inverse]
    return out.tolist()


def _bounded_normal(
    column_plan: "_rows.ColumnPlan", key: kernel2.Key, rows: "np.ndarray", draw: int
) -> List[Any]:
    """The bounded-normal branch of :func:`_rows._draw_value`, through ``truncated_normal``'s rules."""
    column, statistics = column_plan.column, column_plan.statistics
    low, high = statistics.range_min, statistics.range_max
    assert low is not None and high is not None  # _require_parameters
    mean = statistics.mean if statistics.mean is not None else (low + high) / 2
    stddev = (
        statistics.stddev
        if statistics.stddev is not None
        else (high - low) / Decimal(_rows._RANGE_SIGMAS)
    )
    scale = column.logical_type.scale or 0
    # Kernel 0.1.0's refusals, which hold under 0.2.0 (rng.truncated_normal makes them first).
    if stddev < 0 or low > high:
        raise FluxError(rng.FLX_5021, f"{column.name}: a bounded normal it cannot draw")
    lo_q, hi_q = rng._inward_bounds(low, high, scale)
    if stddev == 0:
        drawn = [rng._clamp(rng._quantise(mean, scale), lo_q, hi_q)] * rows.shape[0]
    else:
        plan = kernel2.normal_plan(mean, stddev, low, high, scale)
        units = kernel2.column_truncated_normal_units(
            kernel2.column_uniform53(key, rows, draw), plan
        ).tolist()
        if column.logical_type.kind is LogicalKind.INT and scale == 0:
            return units  # int() of a whole number of units at scale 0 is the number
        drawn = kernel2.column_units_at(units, scale)
    if column.logical_type.kind is LogicalKind.INT:
        return [int(value) for value in drawn]
    return drawn


def _rate(
    column_plan: "_rows.ColumnPlan", key: kernel2.Key, rows: "np.ndarray", draw: int
) -> List[Any]:
    """:func:`_rows._draw_rate` for every row: Poisson counts, or exponential waits."""
    column, statistics = column_plan.column, column_plan.statistics
    u53 = kernel2.column_uniform53(key, rows, draw)
    if column_plan.column_type is ColumnType.INT:
        mean = statistics.mean
        assert mean is not None  # _require_parameters
        counts = kernel2.column_poisson(u53, mean).tolist()
        clamped = [int(_rows._clamp(Decimal(count), statistics)) for count in counts]
        width = integer_width_bounds(column)
        # A count never leaves the width the contract declares for the column, as _draw_rate.
        return clamped if width is None else [max(width[0], min(width[1], c)) for c in clamped]
    rate = statistics.rate
    assert rate is not None  # _require_parameters
    if rate <= 0:
        raise FluxError(rng.FLX_5021, f"{column.name}: an exponential rate it cannot draw")
    scale = max(column.logical_type.scale or 0, 0)
    waited = kernel2.column_units_at(
        kernel2.column_exponential_units(u53, rate, scale).tolist(), scale
    )
    if column.logical_type.kind is LogicalKind.DECIMAL:
        return [_rows._quantise(_rows._clamp(value, statistics), column) for value in waited]
    low = statistics.range_min
    assert low is not None  # _require_parameters
    return [_rows._from_grid(_rows._clamp(low + value, statistics), column) for value in waited]
