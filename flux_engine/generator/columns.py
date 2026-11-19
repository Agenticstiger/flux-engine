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

What stays per row, as ADR 0010 says: a column this module does not draw (it is given to
the per-row path, cell by cell), a plan whose columns are drawn ``given`` another (it takes
the per-row path whole), and rendering. A slice in which anything is refused, or anything
else is raised, is left to the per-row path entirely, so what it raises is what it always
raised, at the row and column it always raised it for. A long slice is taken in chunks, so
the arrays of one chunk are all a pass holds.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from flux_engine.core import kernel2, rng
from flux_engine.core.errors import FluxError
from flux_engine.generator import rows as _rows
from flux_engine.generator.families import ColumnType, Family
from flux_engine.generator.paths import CellStreams
from flux_engine.seam.contract import LogicalKind

__all__ = ["column_rows"]

Rows = Tuple[Tuple[Any, ...], ...]

#: Rows a chunk holds. Its arrays, and the relation reader made for it, are all a pass
#: keeps. A cache the reader clears when it is full costs time to fill again, never a value.
CHUNK_ROWS = 1 << 14


def column_rows(
    plan: "_rows.GeneratorPlan", start: int, stop: int, streams: CellStreams
) -> Optional[Rows]:
    """Rows *start* to *stop* - 1 of a kernel 0.2.0 *plan*, a column at a time.

    ``None`` when the slice is the per-row path's: a conditional plan, a slice in which
    anything is refused or raised, or rows past what an int64 array holds.
    """
    if _rows._given_columns(plan) or stop > kernel2.ARRAY_ROWS:
        return None
    out: List[Tuple[Any, ...]] = []
    for first in range(start, stop, CHUNK_ROWS):
        rows = np.arange(first, min(stop, first + CHUNK_ROWS), dtype=np.int64)
        try:
            with localcontext(_rows._KERNEL_CONTEXT):
                columns = _chunk(plan, streams, rows)
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


def _chunk(
    plan: "_rows.GeneratorPlan", streams: CellStreams, rows: "np.ndarray"
) -> List[List[Any]]:
    """Every column of *plan* over *rows*: drawn as arrays, or read from the relations."""
    if plan.relations is None:
        return [_column(column_plan, plan, streams, rows) for column_plan in plan.columns]
    from flux_engine.generator.relations import owner_of

    reader = _rows._relation_reader(streams)
    owner = owner_of(plan)
    drivers = [plan.relations.driver(column_plan.column.name) for column_plan in plan.columns]
    reader.prefetch(owner, plan.columns, drivers, rows)
    columns: List[List[Any]] = []
    for column_plan, driver in zip(plan.columns, drivers, strict=True):
        if driver is None:
            columns.append(_column(column_plan, plan, streams, rows))
        else:
            columns.append(reader.column_values(owner, column_plan, driver, rows))
    return columns


def _column(
    column_plan: "_rows.ColumnPlan",
    plan: "_rows.GeneratorPlan",
    streams: CellStreams,
    rows: "np.ndarray",
) -> List[Any]:
    values = _vectorized(column_plan, plan, streams, rows)
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
) -> Optional[List[Any]]:
    """The column's values, as :func:`_rows._cell_in_context` gives them, or ``None``."""
    column, statistics = column_plan.column, column_plan.statistics
    if statistics.given is not None:
        return None
    if column_plan.column_type is ColumnType.IDENTIFIER:
        length = statistics.identifier_length or _rows.IDENTIFIER_DEFAULT_LENGTH
        as_uuid = column.declared_type.strip().lower() in _rows._UUID_TYPES
        _rows._require_identifier_shape(length, as_uuid)
        digits = kernel2.column_identifier_digits(
            _rows._seam_bytes(plan.seam_digest), streams.key_label(column.name), rows
        )
        return [_rows._identifier_of_digits(d, length=length, as_uuid=as_uuid) for d in digits]
    key = streams.column_key(column.name)
    if statistics.null_fraction is None:
        return _draws(column_plan, key, rows, 0)
    # The null draw comes first and always happens; the value is drawn at index 1 either way.
    nulls = kernel2.column_bernoulli(
        kernel2.column_uniform53(key, rows, 0), statistics.null_fraction
    ).tolist()
    values = _draws(column_plan, key, rows, 1)
    if values is None:
        return None
    return [None if null else value for null, value in zip(nulls, values, strict=True)]


def _draws(
    column_plan: "_rows.ColumnPlan", key: kernel2.Key, rows: "np.ndarray", draw: int
) -> Optional[List[Any]]:
    """:func:`_rows._draw_value` for every row, its first draw at index *draw*."""
    column, statistics, family = column_plan.column, column_plan.statistics, column_plan.family
    kind = column.logical_type.kind
    if family is Family.CATEGORICAL:
        levels = _rows._categorical_levels(statistics, column, None)
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
        return _histogram(column_plan, key, rows, draw)
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
    return [_rows._from_grid(_rows._decimal_from_units(value, exponent), column) for value in units]


def _histogram(
    column_plan: "_rows.ColumnPlan", key: kernel2.Key, rows: "np.ndarray", draw: int
) -> Optional[List[Any]]:
    """:func:`_rows._draw_histogram` for every row: a bin at *draw*, a grid point at *draw* + 1."""
    column, statistics = column_plan.column, column_plan.statistics
    if statistics.bin_weights_given is not None:
        return None
    bins = statistics.bins or ()
    table = _rows._bin_levels(tuple(statistics.bin_weights or ()))
    if table is None:
        raise FluxError(_rows.FLX_3402, f"{column.name}: binWeights sum to zero")
    levels, positions = table
    names = kernel2.column_categorical(
        kernel2.column_uniform53(key, rows, draw), tuple(levels.items())
    )
    members: Dict[int, List[int]] = {}
    for position, name in enumerate(names):
        members.setdefault(positions[name], []).append(position)
    step = _rows._grid_step(column)
    integral = column.logical_type.kind is LogicalKind.INT
    values: List[Any] = [None] * len(names)
    for index, chosen_rows in members.items():
        low_units, step_units, points, exponent = _rows._grid_of(
            _rows._exact(bins[index]),
            _rows._exact(_rows._bin_top(bins, index, step)),
            _rows._exact(step),
        )
        picked = kernel2.column_bounded(key, rows[chosen_rows], draw + 1, points).tolist()
        if integral and exponent == 0:
            # A grid counted in whole units: int() of each point is its units.
            for position, k in zip(chosen_rows, picked, strict=True):
                values[position] = low_units + k * step_units
            continue
        for position, k in zip(chosen_rows, picked, strict=True):
            drawn = _rows._decimal_from_units(low_units + k * step_units, exponent)
            values[position] = int(drawn) if integral else _rows._from_grid(drawn, column)
    return values


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
        return [int(_rows._clamp(Decimal(count), statistics)) for count in counts]
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
