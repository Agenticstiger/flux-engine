# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The lifetimes of a history's parent rows, read once per process (ADR 0017, amendment of 2026-11-24).

A history that keeps to its parent's lifetime draws each entity's first start and fits its versions
to the parent row it belongs to, so its layout needs the start and the end of every parent row, in
the chain's unit. :func:`deferred_bound` reads them from the cells the parent's own pass writes, a
chunk at a time into arrays (no row of the parent is kept), as a weighted pick reads its parent's
weights, and keeps them by the parent's digest: a lifetime is a function of the parent's final
cells. A parent whose start is NULL bounds nothing, and one with no end, a NULL end or an end
before its start has none to cut at (ADR 0024 section 4). The read waits for the layout that needs
it: a layout this process holds is found by the bound's identity alone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Hashable, Optional, Tuple

import numpy as np

from flux_engine.core.seedtree import SeedTreeRoot
from flux_engine.generator.dates import (
    KIND_DATE,
    KIND_TIMESTAMP,
    lifetimes_in_units,
)
from flux_engine.generator.history import DeferredBound, ParentBound
from flux_engine.seam.contract import LogicalKind

if TYPE_CHECKING:
    from flux_engine.generator import rows as _rows

__all__ = ["deferred_bound"]

#: How many parent rows are written at once when the lifetimes are read.
_CHUNK = 4096
#: The lifetimes of a history's parent rows, by the parent's digest and the columns read.
_BOUNDS: Dict[Tuple[Any, ...], ParentBound] = {}
_BOUNDS_KEPT = 4


def deferred_bound(
    root: SeedTreeRoot,
    parent: "_rows.GeneratorPlan",
    columns: Tuple[str, Optional[str]],
    kind: LogicalKind,
) -> DeferredBound:
    """The lifetime of every row of *parent*, read at *columns* and put in the chain's unit when asked for."""
    chain_kind = _kind_name(kind)
    held = (
        root.digest,
        root.seed_tree_version,
        parent.sim_id,
        parent.expose.expose_id,
        parent.digest().value,
        columns[0],
        columns[1],
        chain_kind,
    )

    def read() -> ParentBound:
        found = _BOUNDS.pop(held, None)
        if found is None:
            found = _read_bound(parent, root, columns, chain_kind, held)
            while len(_BOUNDS) >= _BOUNDS_KEPT:
                del _BOUNDS[next(iter(_BOUNDS))]  # the one used least recently
        _BOUNDS[held] = found
        return found

    return DeferredBound(held, read)


def _kind_name(kind: LogicalKind) -> str:
    """A DATE or TIMESTAMP column's kind as :mod:`flux_engine.generator.dates` names it."""
    return KIND_DATE if kind is LogicalKind.DATE else KIND_TIMESTAMP


def _read_bound(
    parent: "_rows.GeneratorPlan",
    root: SeedTreeRoot,
    columns: Tuple[str, Optional[str]],
    chain_kind: str,
    identity: Hashable,
) -> ParentBound:
    """The lifetimes of *parent*'s rows in the chain's unit, filled into arrays a chunk at a time.

    Only the two lifetime cells of each row are kept, and a chunk's rows are let go once they are.
    """
    from flux_engine.generator import rows as _rows  # relations, which imports this, imports rows
    from flux_engine.generator.rules import _epoch_of

    start_column, end_column = columns
    names = [column_plan.column.name for column_plan in parent.columns]
    at_start = names.index(start_column)
    at_end = None if end_column is None else names.index(end_column)
    from_kind = _kind_name(parent.column_plan(start_column).column.logical_type.kind)
    size = parent.rows
    starts = np.zeros(size, dtype=np.int64)
    ends = np.zeros(size, dtype=np.int64)
    has_start = np.zeros(size, dtype=bool)
    has_end = np.zeros(size, dtype=bool)
    for begin in range(0, size, _CHUNK):
        stop = min(begin + _CHUNK, size)
        chunk = _rows.generate_row_slice(parent, begin, stop, root=root)
        for offset, row in enumerate(chunk, start=begin):
            if row[at_start] is not None:
                starts[offset], has_start[offset] = _epoch_of(row[at_start], from_kind), True
            if at_end is not None and row[at_end] is not None:
                ends[offset], has_end[offset] = _epoch_of(row[at_end], from_kind), True
    low, found_start, high, found_end = lifetimes_in_units(
        starts, ends, has_start, has_end, from_kind, chain_kind
    )
    return ParentBound(low, found_start, high, found_end, identity)
