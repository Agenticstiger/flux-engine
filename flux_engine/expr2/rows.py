# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Rows held in memory, read through :class:`~flux_engine.expr2.evaluate.RowView`.

The reference implementation of what a view must answer, used by the
vector files and the tests; the generator supplies views over the rows it
makes. Matching follows RFC-data-rules.md §2.2 and EXPR-SPEC-2 §3 and §5:

* a relationship reaches the first row of its target whose ``toColumns``
  equal this row's ``fromColumns``; a NULL in a ``fromColumns`` value
  reaches nothing;
* the children of a row are the rows of the child expose whose
  relationship reaches it, in table order;
* a row's partition is the rows of its expose whose relationship columns
  are equal to its own, NULL equal to NULL, as SQL's PARTITION BY groups;
* the previous version is the row of the same entity whose validity starts
  latest before this row's start; a row with a NULL start or a NULL
  entity column has none.

Values are compared as flux-expr/2 compares them, so ``Decimal("1.0")``
and ``1`` are one key.
"""

from __future__ import annotations

from typing import Any, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple

from flux_engine.expr2.evaluate import key_value
from flux_engine.expr2.scope import Expose, Link, Model
from flux_engine.expr2.types import ColumnType

__all__ = ["MemoryRow", "Rows"]


def _key(expose: Expose, columns: Sequence[str], row: Mapping[str, Any]) -> Tuple[Any, ...]:
    out = []
    for column in columns:
        declared: Optional[ColumnType] = expose.columns.get(column)
        value = row.get(column)
        out.append(value if declared is None else key_value(declared, value))
    return tuple(out)


class Rows:
    """Every expose's rows, by expose name, with the indexes the views need."""

    def __init__(self, model: Model, tables: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        unknown = sorted(set(tables) - set(model.exposes))
        if unknown:
            raise ValueError(f"rows given for exposes the model does not hold: {unknown}")
        self.model = model
        self.tables: Dict[str, List[Mapping[str, Any]]] = {
            name: [dict(row) for row in tables.get(name, [])] for name in model.exposes
        }
        self._targets: Dict[Tuple[str, Tuple[str, ...]], Dict[Tuple[Any, ...], int]] = {}
        self._children: Dict[Tuple[str, str], Dict[Tuple[Any, ...], List[int]]] = {}
        self._partitions: Dict[Tuple[str, str], Dict[Tuple[Any, ...], List[int]]] = {}
        self._versions: Dict[str, Dict[Tuple[Any, ...], List[Tuple[Any, int]]]] = {}

    def row(self, expose: str, index: int) -> "MemoryRow":
        if not 0 <= index < len(self.tables[expose]):
            raise IndexError(f"{expose} has no row {index}")
        return MemoryRow(self, expose, index)

    def rows(self, expose: str) -> List["MemoryRow"]:
        return [MemoryRow(self, expose, index) for index in range(len(self.tables[expose]))]

    # ------------------------------------------------------------ indexes

    def target_index(self, link: Link) -> Dict[Tuple[Any, ...], int]:
        cache_key = (link.target, link.to_columns)
        if cache_key not in self._targets:
            target = self.model.exposes[link.target]
            index: Dict[Tuple[Any, ...], int] = {}
            for position, row in enumerate(self.tables[link.target]):
                index.setdefault(_key(target, link.to_columns, row), position)
            self._targets[cache_key] = index
        return self._targets[cache_key]

    def children_index(self, child: str, link: Link) -> Dict[Tuple[Any, ...], List[int]]:
        cache_key = (child, link.name)
        if cache_key not in self._children:
            expose = self.model.exposes[child]
            index: Dict[Tuple[Any, ...], List[int]] = {}
            for position, row in enumerate(self.tables[child]):
                key = _key(expose, link.from_columns, row)
                if None not in key:
                    index.setdefault(key, []).append(position)
            self._children[cache_key] = index
        return self._children[cache_key]

    def partition_index(self, expose_name: str, link: Link) -> Dict[Tuple[Any, ...], List[int]]:
        cache_key = (expose_name, link.name)
        if cache_key not in self._partitions:
            expose = self.model.exposes[expose_name]
            index: Dict[Tuple[Any, ...], List[int]] = {}
            for position, row in enumerate(self.tables[expose_name]):
                index.setdefault(_key(expose, link.from_columns, row), []).append(position)
            self._partitions[cache_key] = index
        return self._partitions[cache_key]

    def version_index(self, expose_name: str) -> Dict[Tuple[Any, ...], List[Tuple[Any, int]]]:
        if expose_name not in self._versions:
            expose = self.model.exposes[expose_name]
            versions = expose.versions
            assert versions is not None
            index: Dict[Tuple[Any, ...], List[Tuple[Any, int]]] = {}
            for position, row in enumerate(self.tables[expose_name]):
                entity = _key(expose, versions.entity, row)
                (start,) = _key(expose, (versions.start,), row)
                if None in entity or start is None:
                    continue
                index.setdefault(entity, []).append((start, position))
            self._versions[expose_name] = index
        return self._versions[expose_name]


class MemoryRow:
    """One row of :class:`Rows`, as a :class:`~flux_engine.expr2.evaluate.RowView`."""

    __slots__ = ("_rows", "expose", "index")

    def __init__(self, rows: Rows, expose: str, index: int) -> None:
        self._rows = rows
        self.expose = expose
        self.index = index

    @property
    def key(self) -> Hashable:
        return (self.expose, self.index)

    @property
    def _data(self) -> Mapping[str, Any]:
        return self._rows.tables[self.expose][self.index]

    @property
    def _scope(self) -> Expose:
        return self._rows.model.exposes[self.expose]

    def get(self, column: str) -> Any:
        return self._data.get(column)

    def follow(self, relationship: str) -> Optional["MemoryRow"]:
        link = self._scope.links[relationship]
        key = _key(self._scope, link.from_columns, self._data)
        if None in key:
            return None
        target = self._rows.target_index(link).get(key)
        return None if target is None else MemoryRow(self._rows, link.target, target)

    def previous(self) -> Optional["MemoryRow"]:
        scope = self._scope
        versions = scope.versions
        if versions is None:
            return None
        entity = _key(scope, versions.entity, self._data)
        (start,) = _key(scope, (versions.start,), self._data)
        if None in entity or start is None:
            return None
        best: Optional[Tuple[Any, int]] = None
        for other_start, position in self._rows.version_index(self.expose).get(entity, []):
            if other_start < start and (best is None or other_start > best[0]):
                best = (other_start, position)
        return None if best is None else MemoryRow(self._rows, self.expose, best[1])

    def children(self, expose: str, relationship: str) -> List["MemoryRow"]:
        link = self._rows.model.exposes[expose].links[relationship]
        key = _key(self._scope, link.to_columns, self._data)
        if None in key:
            return []
        positions = self._rows.children_index(expose, link).get(key, [])
        return [MemoryRow(self._rows, expose, position) for position in positions]

    def siblings(self, relationship: str) -> List["MemoryRow"]:
        link = self._scope.links[relationship]
        key = _key(self._scope, link.from_columns, self._data)
        positions = self._rows.partition_index(self.expose, link)[key]
        return [MemoryRow(self._rows, self.expose, position) for position in positions]
