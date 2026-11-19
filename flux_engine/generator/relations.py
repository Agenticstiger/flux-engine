# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Related tables: keys laid out without repeats, foreign keys taken from the parent's keys.

KERNEL-SPEC section 8.1. The FLUID contract declares the keys of its exposes and
the relationships between them (:mod:`flux_engine.seam.keys`), and the exposes a
Simulation generates from statistics are generated together, so that every key
the contract declares is unique and every foreign key names a row its parent
holds. Nothing is materialised to get there: every value is still a function of
the run, the expose, the row index and the column, so a slice of rows is the same
wherever it is made and ``--jobs`` stays byte-identical.

Keys are derived, never drawn:

* a one-column INT, DECIMAL, DATE or TIMESTAMP key is laid out in ascending order
  over its declared range, or over its declared bins with the rows allotted to
  each bin by its weight (the Largest Remainder Method, KERNEL-SPEC section 5),
  and no value repeats. With no declaration, an INT key counts ``1..rows``;
* a one-column STRING key is an identifier: a keyed permutation of the row index
  over the identifier's values (:func:`flux_engine.core.permute.permute`), so it
  never repeats either, at any length;
* a key of several columns takes one distinct point of the product of its
  columns' domains, through the same permutation. A column that is also a
  foreign key contributes its parent's rows as its domain, so the key holds and
  the foreign key holds at once;
* the permutation key of each comes from the seam digest ``H``, like an
  identifier's, so a key does not move with the seed.

A foreign key takes its parent's key at a parent row, computed on demand
(dbldatagen's pattern: the parent's key generator is rerun for the chosen row,
and the parent table is never held). Which row:

* ``declared``: the column draws as it declares (the profile's histogram of the
  foreign key, say) and the value moves to the nearest key the parent holds, so
  the declared shape of children per parent is kept. Only a one-column
  relationship whose parent key is an ordered layout takes this pick;
* ``uniform``: a parent row drawn uniformly, on the stream of the relationship's
  first column, when the column declares no draw or its parent key is not an
  ordered layout;
* ``distinct``: a ``one_to_one`` relationship, or one whose columns are a key of
  the child, references each parent row at most once: a keyed permutation of
  the parent's rows, read at the child's row index.

A foreign key column keeps its declared ``nullFraction`` when the relationship's
``joinType`` is ``left`` (FLUID's default): a null references no row. An
``inner`` relationship joins every row, so a ``nullFraction`` on its columns is
refused, and so is one on any key column.

Everything that cannot hold is refused before a row is drawn (``FLX-3403``).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple, Union

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.canonical import sha256_hex
from flux_engine.core.errors import FluxError, register
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.permute import PERMUTE_MAX_DOMAIN, permute, permute2
from flux_engine.core.rng import DrawStream, bernoulli, bounded_uint
from flux_engine.core.seedtree import SeedTreeRoot, digest_bytes, path_label
from flux_engine.generator import rows as _rows
from flux_engine.generator.families import ColumnType
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL
from flux_engine.seam.contract import CompiledColumn, LogicalKind
from flux_engine.seam.keys import ContractKeys, ExposeKeys, Relationship

__all__ = [
    "FLX_3403",
    "CompositeKey",
    "ForeignKey",
    "IdentifierKey",
    "KeyGrid",
    "ParentView",
    "RelationReader",
    "Relations",
    "layout_key",
    "related_columns",
    "relate_plans",
]

FLX_3403 = register(
    "FLX-3403",
    "the related tables cannot be generated as declared",
    "FLUX generates the exposes of one contract together: every key the contract "
    "declares is laid out from the row index and never repeats, and every foreign key "
    "takes a key its parent's rows hold. A relationship whose target the Simulation "
    "does not generate from statistics, a key whose declared range or bins hold fewer "
    "values than its rows, a one_to_one child with more rows than its parent, a "
    "nullFraction on a key column or on a relationship whose joinType is inner, a key "
    "declared with a draw that would repeat values, column types that differ between "
    "a foreign key and its target, a key column nothing produces, and two relationships "
    "that set one column without one implying the other are refused before any row is "
    "drawn. The message names the expose, the column or relationship, and the change "
    "to make.",
)

#: Domain-separation label of a key layout's permutation key.
_KEY_LABEL = b"flux-key/1"
#: Kernel ``0.2.0``'s label for the same keys (KERNEL-SPEC-2 section 8).
_KEY_LABEL_2 = kernel2.KEY_LABEL


def _permute(key: bytes, domain: int, index: int, counter_based: bool) -> int:
    """``permute`` under kernel ``0.1.0``, ``permute/2`` under kernel ``0.2.0``."""
    return permute2(key, domain, index) if counter_based else permute(key, domain, index)


_NUL = b"\x00"

#: The orderable kinds a one-column key can be laid out over.
_ORDERED_KINDS = (LogicalKind.INT, LogicalKind.DECIMAL, LogicalKind.DATE, LogicalKind.TIMESTAMP)

#: Values a UUID-shaped identifier carries besides its version and variant bits.
_UUID_SPACE = 1 << 122

#: How many cached picks and decoded points a reader keeps before it starts over.
_CACHE_LIMIT = 1 << 16

#: What a reader holds for a key it has worked out nothing for.
_NOTHING: Mapping[int, Any] = MappingProxyType({})


def _missing(rows: "np.ndarray", done: Mapping[int, Any]) -> "np.ndarray":
    """The rows of *rows* that *done* holds nothing for yet, in their order."""
    if not done:
        return rows
    return np.array([row for row in rows.tolist() if row not in done], dtype=np.int64)


def _row_array(rows: Set[int]) -> "Optional[np.ndarray]":
    """*rows* as an int64 array, or ``None`` when one lies past what such an array holds."""
    if rows and max(rows) >= kernel2.ARRAY_ROWS:
        return None
    return np.fromiter(rows, dtype=np.int64, count=len(rows))


def _refuse(message: str) -> FluxError:
    return FluxError(FLX_3403, message)


# ---------------------------------------------------------------------------
# Grids: the integer units a value of an ordered kind is counted in
# ---------------------------------------------------------------------------


def _exponent(column: CompiledColumn) -> int:
    """A grid unit is ``10 ** exponent`` of the column's value: its scale for a DECIMAL."""
    if column.logical_type.kind is LogicalKind.DECIMAL:
        return -(column.logical_type.scale or 0)
    return 0


_DATE_KIND = LogicalKind.DATE.value
_DECIMAL_KIND = LogicalKind.DECIMAL.value


def _value_of_units(kind: str, exponent: int, units: int) -> Any:
    """The value a grid point stands for, in the class :mod:`flux_engine.seam.conform` renders."""
    if kind == _DATE_KIND:
        return date.fromordinal(units + DATE_EPOCH_ORDINAL)
    if kind == _DECIMAL_KIND:
        return _rows._decimal_from_units(units, exponent)
    return units  # INT, and TIMESTAMP in milliseconds since the epoch


def _units_of_value(kind: str, exponent: int, value: Any) -> int:
    """The grid point a drawn value lies on: the inverse of :func:`_value_of_units`."""
    if kind == LogicalKind.DATE.value:
        return int(value.toordinal() - DATE_EPOCH_ORDINAL)
    if kind == LogicalKind.DECIMAL.value:
        scaled = Fraction(value) * Fraction(10) ** (-exponent)
        return scaled.numerator // scaled.denominator
    return int(value)


def _ceil_units(value: Decimal, exponent: int) -> int:
    scaled = Fraction(value) * Fraction(10) ** (-exponent)
    return -((-scaled.numerator) // scaled.denominator)


def _floor_units(value: Decimal, exponent: int) -> int:
    scaled = Fraction(value) * Fraction(10) ** (-exponent)
    return scaled.numerator // scaled.denominator


# ---------------------------------------------------------------------------
# Key layouts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyGrid:
    """A one-column key laid out in ascending order, one grid point per row.

    ``firsts``, ``widths``, ``counts`` and ``starts`` describe each bin that
    holds a row: its first grid point, how many grid points it spans, how many
    rows it holds and the first of them. Within a bin, row ``start + k`` takes
    the grid point ``first + floor(k * width / count)``; ``count <= width``, so no
    two rows meet.
    """

    kind: str
    exponent: int
    firsts: Tuple[int, ...]
    widths: Tuple[int, ...]
    counts: Tuple[int, ...]
    starts: Tuple[int, ...]
    rows: int

    def units_at(self, row: int) -> int:
        index = bisect_right(self.starts, row) - 1
        offset = row - self.starts[index]
        return self.firsts[index] + (offset * self.widths[index]) // self.counts[index]

    def value_at(self, row: int) -> Any:
        return _value_of_units(self.kind, self.exponent, self.units_at(row))

    def row_near(self, value: Any) -> int:
        """The row whose key is nearest *value*; the lower key when two are as near."""
        units = _units_of_value(self.kind, self.exponent, value)
        index = bisect_right(self.firsts, units) - 1
        if index < 0:
            return 0
        first, width, count, start = (
            self.firsts[index],
            self.widths[index],
            self.counts[index],
            self.starts[index],
        )
        offset = units - first
        if offset >= width:
            low = start + count - 1
        else:
            low = start + (offset * count) // width
        high = low + 1
        if high >= self.rows:
            return low
        below, above = units - self.units_at(low), self.units_at(high) - units
        return low if below <= above else high

    def as_document(self) -> Dict[str, Any]:
        return {
            "layout": "ordered",
            "kind": self.kind,
            "exponent": self.exponent,
            "rows": self.rows,
            "bins": [
                [first, width, count]
                for first, width, count in zip(self.firsts, self.widths, self.counts, strict=True)
            ],
        }


@dataclass(frozen=True)
class IdentifierKey:
    """A one-column STRING key: the row index permuted over the identifier's values."""

    key: bytes
    length: int
    as_uuid: bool
    rows: int
    #: Kernel ``0.2.0``'s ``permute/2`` rather than ``permute`` (KERNEL-SPEC-2 section 8).
    counter_based: bool = False

    @property
    def space(self) -> int:
        return _UUID_SPACE if self.as_uuid else 16**self.length

    def value_at(self, row: int) -> str:
        return self.text_of(_permute(self.key, self.space, row, self.counter_based))

    def text_of(self, value: int) -> str:
        """The identifier the permuted *value* spells: hex digits, or an RFC 9562 UUID."""
        if not self.as_uuid:
            return format(value, f"0{self.length}x")
        # RFC 9562 version 8: 48 bits, the version nibble, 12 bits, the variant
        # bits 10, then 62 bits. Every bit of the permuted value is kept.
        high, middle, low = value >> 74, (value >> 62) & 0xFFF, value & ((1 << 62) - 1)
        whole = (high << 80) | (0x8 << 76) | (middle << 64) | (0x2 << 62) | low
        text = format(whole, "032x")
        return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"

    def as_document(self) -> Dict[str, Any]:
        return {
            "layout": "identifier",
            "length": self.length,
            "uuid": self.as_uuid,
            "rows": self.rows,
        }


@dataclass(frozen=True)
class GridPart:
    """A plain column of a several-column key, spread over the grid points of its range."""

    column: str
    kind: str
    exponent: int
    first: int
    size: int

    @property
    def columns(self) -> Tuple[str, ...]:
        return (self.column,)

    def as_document(self) -> Dict[str, Any]:
        return {
            "column": self.column,
            "kind": self.kind,
            "exponent": self.exponent,
            "first": self.first,
            "size": self.size,
        }


@dataclass(frozen=True)
class LevelPart:
    """A plain column of a several-column key, spread over its declared levels."""

    column: CompiledColumn
    levels: Tuple[str, ...]

    @property
    def columns(self) -> Tuple[str, ...]:
        return (self.column.name,)

    @property
    def size(self) -> int:
        return len(self.levels)

    def as_document(self) -> Dict[str, Any]:
        return {"column": self.column.name, "levels": list(self.levels)}


@dataclass(frozen=True)
class ParentPart:
    """A foreign key inside a several-column key: its domain is its parent's rows."""

    relationship: str
    from_columns: Tuple[str, ...]
    to_columns: Tuple[str, ...]
    parent: "ParentView"

    @property
    def columns(self) -> Tuple[str, ...]:
        return self.from_columns

    @property
    def size(self) -> int:
        return self.parent.rows

    def as_document(self) -> Dict[str, Any]:
        return {
            "relationship": self.relationship,
            "fromColumns": list(self.from_columns),
            "toColumns": list(self.to_columns),
            "parent": self.parent.as_document(),
        }


Part = Union[GridPart, LevelPart, ParentPart]


@dataclass(frozen=True)
class CompositeKey:
    """A key of several columns: one distinct point of the product of its parts per row."""

    columns: Tuple[str, ...]
    key: bytes
    parts: Tuple[Part, ...]
    space: int
    #: Kernel ``0.2.0``'s ``permute/2`` rather than ``permute`` (KERNEL-SPEC-2 section 8).
    counter_based: bool = False

    def indexes_at(self, row: int) -> Tuple[int, ...]:
        return self.indexes_of(_permute(self.key, self.space, row, self.counter_based))

    def indexes_of(self, point: int) -> Tuple[int, ...]:
        """Each part's index in the product point *point*, the last part varying fastest."""
        indexes: List[int] = []
        for part in reversed(self.parts):
            point, index = divmod(point, part.size)
            indexes.append(index)
        return tuple(reversed(indexes))

    def indexes_of_points(self, points: List[int]) -> List[Tuple[int, ...]]:
        """:meth:`indexes_of` of each of *points*, by array division while they fit int64."""
        if self.space >= kernel2.ARRAY_ROWS:
            return [self.indexes_of(point) for point in points]
        remaining = np.array(points, dtype=np.int64)
        columns: List[List[int]] = []
        for part in reversed(self.parts):
            remaining, index = np.divmod(remaining, part.size)
            columns.append(index.tolist())
        return list(zip(*reversed(columns), strict=True))

    def as_document(self) -> Dict[str, Any]:
        return {
            "layout": "composite",
            "columns": list(self.columns),
            "parts": [part.as_document() for part in self.parts],
        }


@dataclass(frozen=True)
class CompositeMember:
    """A column whose value is its part of a several-column key."""

    composite: CompositeKey

    def as_document(self) -> Dict[str, Any]:
        return self.composite.as_document()


@dataclass(frozen=True)
class ForeignKey:
    """The columns of one relationship: the parent's key at a chosen parent row.

    ``pick`` is ``declared``, ``uniform`` or ``distinct`` (see the module
    docstring). ``grid`` is the parent key's layout for a ``declared`` pick;
    ``key`` is the permutation key of a ``distinct`` one; ``first_null`` is the
    declared ``nullFraction`` of the first column, whose stream a ``uniform``
    pick is drawn on after that column's null draw.
    """

    relationship: str
    from_columns: Tuple[str, ...]
    to_columns: Tuple[str, ...]
    pick: str
    parent: "ParentView"
    grid: Optional[KeyGrid] = None
    key: Optional[bytes] = None
    first_null: Optional[Decimal] = None
    #: Kernel ``0.2.0``'s ``permute/2`` for a ``distinct`` pick (KERNEL-SPEC-2 section 8).
    counter_based: bool = False

    def target_of(self, column: str) -> str:
        return self.to_columns[self.from_columns.index(column)]

    def as_document(self) -> Dict[str, Any]:
        return {
            "relationship": self.relationship,
            "fromColumns": list(self.from_columns),
            "toColumns": list(self.to_columns),
            "pick": self.pick,
            "parent": self.parent.as_document(),
        }


Driver = Union[KeyGrid, IdentifierKey, CompositeMember, ForeignKey]


@dataclass(frozen=True)
class ParentView:
    """The columns of one expose a relationship reads, and how each is produced.

    Pure data: a view holds the plans and drivers of the columns it reads, and
    the views those read, so a child's plan carries everything it needs to
    compute a parent's key at any row, in any process, without the parent's
    whole plan and without a cycle.
    """

    sim_id: str
    expose_id: str
    seam_digest: str
    rows: int
    columns: Tuple["_rows.ColumnPlan", ...]
    drivers: Tuple[Tuple[str, Driver], ...]

    def column_plan(self, name: str) -> "_rows.ColumnPlan":
        for plan in self.columns:
            if plan.column.name == name:
                return plan
        raise KeyError(name)  # pragma: no cover - a view is built from the columns it reads

    def driver(self, name: str) -> Optional[Driver]:
        for column, driver in self.drivers:
            if column == name:
                return driver
        return None

    def as_document(self) -> Dict[str, Any]:
        return {
            "simId": self.sim_id,
            "exposeId": self.expose_id,
            "H": self.seam_digest,
            "rows": self.rows,
            "columns": [plan.as_document() for plan in self.columns],
            "drivers": {column: driver.as_document() for column, driver in self.drivers},
        }


@dataclass(frozen=True)
class Relations:
    """How each related column of one expose is produced, by column name."""

    drivers: Tuple[Tuple[str, Driver], ...]

    def driver(self, name: str) -> Optional[Driver]:
        for column, driver in self.drivers:
            if column == name:
                return driver
        return None

    def as_document(self) -> Dict[str, Any]:
        return {column: driver.as_document() for column, driver in self.drivers}


# ---------------------------------------------------------------------------
# Reading related values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Owner:
    sim_id: str
    expose_id: str
    seam_digest: str


class RelationReader:
    """Reads related values during one generation pass over a run root.

    Every value is computed from the run root and the cell's own address, so a
    reader keeps nothing but caches: the parent row each relationship picked
    for a row, the point each several-column key decoded to, which the other
    columns of the same row ask for again, and each parent cell's value. Under
    kernel 0.2.0, a slice computed a column at a time also fills, for its rows,
    each foreign key's null and parent row and each identifier key's text
    (:meth:`prefetch`). Each slice, or each chunk of one, gets a reader of its
    own, so none of this outlives it.
    """

    def __init__(self, root: SeedTreeRoot) -> None:
        self.root = root
        self._streams: Dict[Tuple[str, str], CellStreams] = {}
        self._picks: Dict[Tuple[str, str, str, int], int] = {}
        self._points: Dict[Tuple[str, str, Tuple[str, ...], int], Tuple[int, ...]] = {}
        # Filled by prefetch for one slice of a kernel 0.2.0 pass, row by row: a foreign
        # key's null and parent row, per relationship, and an identifier key's text.
        self._ready: Dict[Tuple[str, str, str], Dict[int, Tuple[bool, int]]] = {}
        self._texts: Dict["IdentifierKey", Dict[int, str]] = {}
        # A parent cell's value is a function of its view, column and row alone, and the
        # rows of a slice name the same parent rows many times over.
        self._views: Dict[Tuple[str, str, str], Dict[int, Any]] = {}

    def streams(self, sim_id: str, expose_id: str) -> CellStreams:
        found = self._streams.get((sim_id, expose_id))
        if found is None:
            found = CellStreams(self.root, sim_id, expose_id)
            self._streams[(sim_id, expose_id)] = found
        return found

    def cell(
        self,
        owner: _Owner,
        column_plan: "_rows.ColumnPlan",
        driver: Driver,
        row: int,
        given_level: Optional[str] = None,
    ) -> Any:
        """The value *driver* gives the cell at *row*, its null draw included."""
        with localcontext(_rows._KERNEL_CONTEXT):
            return self._cell(owner, column_plan, driver, row, given_level)

    def _cell(
        self,
        owner: _Owner,
        column_plan: "_rows.ColumnPlan",
        driver: Driver,
        row: int,
        given_level: Optional[str],
    ) -> Any:
        name = column_plan.column.name
        if isinstance(driver, KeyGrid):
            return driver.value_at(row)
        if isinstance(driver, IdentifierKey):
            # The prefetch caches are empty but for a kernel 0.2.0 slice a column at a time,
            # so the per-row path looks into them only when one filled them.
            text = self._texts.get(driver, _NOTHING).get(row) if self._texts else None
            return text if text is not None else driver.value_at(row)
        if isinstance(driver, CompositeMember):
            return self._member(owner, driver.composite, name, row)
        ready = None
        if self._ready:
            relationship = (owner.sim_id, owner.expose_id, driver.relationship)
            ready = self._ready.get(relationship, _NOTHING).get(row)
        if ready is not None:
            is_ready_null, ready_row = ready
            if is_ready_null:
                return None
            return self.view_value(driver.parent, driver.target_of(name), ready_row)
        # A foreign key's columns are null together: the first column's null
        # draw, on its own stream, decides for all of them (only the first may
        # declare one: _check_key_columns refuses one on any other).
        first = driver.from_columns[0]
        stream = None
        is_null = False
        if driver.first_null is not None:
            stream = self.streams(owner.sim_id, owner.expose_id).stream_at(row, first)
            is_null = bernoulli(stream, driver.first_null)
        if driver.pick == "declared":
            assert driver.grid is not None  # relate_plans sets it for this pick
            if stream is None:  # a declared pick has one column, so first == name
                stream = self.streams(owner.sim_id, owner.expose_id).stream_at(row, name)
            drawn = _rows._draw_value(column_plan, stream, given_level)
            if is_null:
                return None
            parent_row = driver.grid.row_near(drawn)
        else:
            if is_null:
                return None
            parent_row = self._pick(owner, driver, row)
        return self.view_value(driver.parent, driver.target_of(name), parent_row)

    def foreign_on_stream(
        self,
        column_plan: "_rows.ColumnPlan",
        driver: ForeignKey,
        stream: DrawStream,
        *,
        skip_null: bool = False,
    ) -> Any:
        """The value a one-column foreign key takes when its draws are read from *stream*.

        What :meth:`cell` does for the key, with the stream chosen by the caller instead of
        the cell's own: the null draw first when the column declares a ``nullFraction``, then
        the declared draw and the parent's nearest key (a ``declared`` pick) or a parent row
        drawn uniformly (a ``uniform`` one). A contract dependency draws a foreign key ``to``
        column this way, once per key (EXPR-SPEC-2 section 8). A ``distinct`` pick is a
        function of the row, not a draw, and is not taken here.
        """
        if driver.pick not in ("declared", "uniform") or len(driver.from_columns) != 1:
            raise _refuse(
                f"relationship {driver.relationship!r} is not a one-column foreign key with a draw"
            )
        with localcontext(_rows._KERNEL_CONTEXT):
            # With skip_null the null draw is left out: the key's value is a non-NULL one.
            is_null = (
                not skip_null
                and driver.first_null is not None
                and bernoulli(stream, driver.first_null)
            )
            if driver.pick == "declared":
                assert driver.grid is not None  # relate_plans sets it for this pick
                drawn = _rows._draw_value(column_plan, stream, None)
                parent_row = None if is_null else driver.grid.row_near(drawn)
            else:
                parent_row = None if is_null else bounded_uint(stream, driver.parent.rows)
            if parent_row is None:
                return None
            return self.view_value(
                driver.parent, driver.target_of(column_plan.column.name), parent_row
            )

    def declared_given(
        self, owner: _Owner, driver: ForeignKey, column_plan: "_rows.ColumnPlan", drawn: Any
    ) -> Any:
        """The value of a ``declared`` foreign key whose drawn level another column reads."""
        assert driver.grid is not None  # relate_plans allows only a declared pick here
        with localcontext(_rows._KERNEL_CONTEXT):
            parent_row = driver.grid.row_near(drawn)
            return self.view_value(
                driver.parent, driver.target_of(column_plan.column.name), parent_row
            )

    def _pick(self, owner: _Owner, driver: ForeignKey, row: int) -> int:
        """The parent row a ``uniform`` or ``distinct`` relationship takes for *row*."""
        cache_key = (owner.sim_id, owner.expose_id, driver.relationship, row)
        found = self._picks.get(cache_key)
        if found is not None:
            return found
        if driver.pick == "distinct":
            assert driver.key is not None  # relate_plans sets it for this pick
            picked = _permute(driver.key, driver.parent.rows, row, driver.counter_based)
        else:
            first = driver.from_columns[0]
            stream = self.streams(owner.sim_id, owner.expose_id).stream_at(row, first)
            if driver.first_null is not None:
                bernoulli(stream, driver.first_null)
            picked = bounded_uint(stream, driver.parent.rows)
        if len(self._picks) >= _CACHE_LIMIT:
            self._picks.clear()
        self._picks[cache_key] = picked
        return picked

    def _member(self, owner: _Owner, composite: CompositeKey, column: str, row: int) -> Any:
        cache_key = (owner.sim_id, owner.expose_id, composite.columns, row)
        indexes = self._points.get(cache_key)
        if indexes is None:
            indexes = composite.indexes_at(row)
            if len(self._points) >= _CACHE_LIMIT:
                self._points.clear()
            self._points[cache_key] = indexes
        for part, index in zip(composite.parts, indexes, strict=True):
            if column not in part.columns:
                continue
            if isinstance(part, GridPart):
                return _value_of_units(part.kind, part.exponent, part.first + index)
            if isinstance(part, LevelPart):
                return _rows._level_to_value(part.levels[index], part.column)
            target = part.to_columns[part.from_columns.index(column)]
            return self.view_value(part.parent, target, index)
        raise KeyError(column)  # pragma: no cover - a member belongs to a part

    def view_value(self, view: ParentView, column: str, row: int) -> Any:
        """The value *view*'s expose holds in *column* at *row*: what its own pass writes."""
        memo = self._views.setdefault((view.sim_id, view.expose_id, column), {})
        if row in memo:
            return memo[row]
        value = self._view_value(view, column, row)
        if len(memo) >= _CACHE_LIMIT:
            memo.clear()
        memo[row] = value
        return value

    def _view_value(self, view: ParentView, column: str, row: int) -> Any:
        owner = _Owner(view.sim_id, view.expose_id, view.seam_digest)
        column_plan = view.column_plan(column)
        driver = view.driver(column)
        if driver is not None:
            return self._cell(owner, column_plan, driver, row, None)
        streams = self.streams(view.sim_id, view.expose_id)
        return _rows._cell(column_plan, streams, view.sim_id, view.expose_id, row, view.seam_digest)

    # -- kernel 0.2.0, a slice at a time (ADR 0010, step 4) ---------------------------------

    def column_values(
        self,
        owner: _Owner,
        column_plan: "_rows.ColumnPlan",
        driver: Driver,
        rows: "np.ndarray",
    ) -> List[Any]:
        """:meth:`cell` of every row of a slice, read from what :meth:`prefetch` worked out.

        One pass over the column, inside the kernel's ``decimal`` context the caller holds,
        rather than one call into :meth:`cell` per row. A row the prefetch did not reach
        takes the per-row code, so every value is :meth:`cell`'s.
        """
        name = column_plan.column.name
        row_list = rows.tolist()
        if isinstance(driver, KeyGrid):
            return [driver.value_at(row) for row in row_list]
        if isinstance(driver, IdentifierKey):
            texts = self._texts.get(driver, _NOTHING)
            values: List[Any] = []
            for row in row_list:
                text = texts.get(row)
                values.append(text if text is not None else driver.value_at(row))
            return values
        if isinstance(driver, CompositeMember):
            return self._member_values(owner, driver.composite, name, row_list)
        target = driver.target_of(name)
        ready = self._ready.get((owner.sim_id, owner.expose_id, driver.relationship), _NOTHING)
        values = []
        for row in row_list:
            found = ready.get(row)
            if found is None:
                values.append(self._cell(owner, column_plan, driver, row, None))
            elif found[0]:
                values.append(None)
            else:
                values.append(self.view_value(driver.parent, target, found[1]))
        return values

    def _member_values(
        self, owner: _Owner, composite: CompositeKey, column: str, rows: List[int]
    ) -> List[Any]:
        """:meth:`_member` of each of *rows*, with the part that holds *column* found once."""
        position, part = next(
            (position, part)
            for position, part in enumerate(composite.parts)
            if column in part.columns
        )
        prefix = (owner.sim_id, owner.expose_id, composite.columns)
        points = self._points
        indexes = []
        for row in rows:
            point = points.get(prefix + (row,))
            if point is None:
                point = composite.indexes_at(row)
            indexes.append(point[position])
        if isinstance(part, GridPart):
            return [_value_of_units(part.kind, part.exponent, part.first + i) for i in indexes]
        if isinstance(part, LevelPart):
            return [_rows._level_to_value(part.levels[i], part.column) for i in indexes]
        target = part.to_columns[part.from_columns.index(column)]
        return [self.view_value(part.parent, target, index) for index in indexes]

    def prefetch(
        self,
        owner: _Owner,
        columns: Sequence["_rows.ColumnPlan"],
        drivers: Sequence[Optional[Driver]],
        rows: "np.ndarray",
    ) -> None:
        """Work out, for every row of a slice at once, what :meth:`cell` would draw.

        For each driver of the slice's columns: an identifier key's text, a composite
        key's point, and a foreign key's null and parent row, then the same for the parent
        cells those rows name, through every view below them. Each goes into the cache
        :meth:`cell` reads first, so the per-row code is unchanged and reads the values
        the array functions computed, which are its own value for value. A row already
        worked out is skipped, and a layout too wide for an array permutation, or a row
        past what an array holds, is left to the per-row code.
        """
        for column_plan, driver in zip(columns, drivers, strict=True):
            if driver is not None:
                self._prefetch_driver(owner, column_plan, driver, rows)

    def _prefetch_driver(
        self,
        owner: _Owner,
        column_plan: "_rows.ColumnPlan",
        driver: Driver,
        rows: "np.ndarray",
    ) -> None:
        if isinstance(driver, KeyGrid):
            return  # a grid key's value is arithmetic on its row
        if isinstance(driver, IdentifierKey):
            self._prefetch_identifier(driver, rows)
        elif isinstance(driver, CompositeMember):
            self._prefetch_composite(owner, driver.composite, rows)
        else:
            self._prefetch_foreign(owner, column_plan, driver, rows)

    def _prefetch_identifier(self, driver: IdentifierKey, rows: "np.ndarray") -> None:
        if not driver.counter_based or driver.space > kernel2.PERMUTE_ARRAY_MAX_DOMAIN:
            return
        texts = self._texts.setdefault(driver, {})
        rows = _missing(rows, texts)
        if rows.shape[0] == 0:
            return
        images = kernel2.permute2_array(driver.key, driver.space, rows)
        for row, image in zip(rows.tolist(), images, strict=True):
            texts[row] = driver.text_of(image)

    def _prefetch_composite(
        self, owner: _Owner, composite: CompositeKey, rows: "np.ndarray"
    ) -> None:
        if not composite.counter_based or composite.space > kernel2.PERMUTE_ARRAY_MAX_DOMAIN:
            return
        # Every member column of the key asks for it; the rows already decoded are skipped.
        prefix = (owner.sim_id, owner.expose_id, composite.columns)
        points = self._points
        fresh = [row for row in rows.tolist() if prefix + (row,) not in points]
        if not fresh:
            return
        images = kernel2.permute2_array(
            composite.key, composite.space, np.array(fresh, dtype=np.int64)
        )
        decoded = composite.indexes_of_points(images)
        for row, indexes in zip(fresh, decoded, strict=True):
            points[prefix + (row,)] = indexes
        for position, part in enumerate(composite.parts):
            if isinstance(part, ParentPart):
                parent_rows = _row_array({indexes[position] for indexes in decoded})
                if parent_rows is not None:
                    for target in part.to_columns:
                        self._prefetch_view(part.parent, target, parent_rows)

    def _prefetch_foreign(
        self,
        owner: _Owner,
        column_plan: "_rows.ColumnPlan",
        driver: ForeignKey,
        rows: "np.ndarray",
    ) -> None:
        """:meth:`_cell`'s foreign key draws for *rows*: the first column's null, then the pick."""
        ready = self._ready.setdefault((owner.sim_id, owner.expose_id, driver.relationship), {})
        rows = _missing(rows, ready)
        if rows.shape[0] == 0:
            return
        key = self.streams(owner.sim_id, owner.expose_id).column_key(driver.from_columns[0])
        draw = 0
        nulls = [False] * rows.shape[0]
        if driver.first_null is not None:
            nulls = kernel2.column_bernoulli(
                kernel2.column_uniform53(key, rows, 0), driver.first_null
            ).tolist()
            draw = 1
        if driver.pick == "declared":
            from flux_engine.generator import columns as _columns

            assert driver.grid is not None  # relate_plans sets it for this pick
            drawn = _columns._draws(column_plan, key, rows, draw)
            if drawn is None:
                return
            parents = [driver.grid.row_near(value) for value in drawn]
        elif driver.pick == "distinct":
            assert driver.key is not None  # relate_plans sets it for this pick
            if not driver.counter_based or driver.parent.rows > kernel2.PERMUTE_ARRAY_MAX_DOMAIN:
                return
            parents = kernel2.permute2_array(driver.key, driver.parent.rows, rows)
        else:
            parents = kernel2.column_bounded(key, rows, draw, driver.parent.rows).tolist()
        named: Set[int] = set()
        for row, null, parent in zip(rows.tolist(), nulls, parents, strict=True):
            ready[row] = (bool(null), int(parent))
            if not null:
                named.add(int(parent))
        parent_rows = _row_array(named)
        if parent_rows is not None:
            for target in driver.to_columns:
                self._prefetch_view(driver.parent, target, parent_rows)

    def _prefetch_view(self, view: ParentView, column: str, rows: "np.ndarray") -> None:
        driver = view.driver(column)
        if driver is not None and rows.shape[0]:
            owner = _Owner(view.sim_id, view.expose_id, view.seam_digest)
            self._prefetch_driver(owner, view.column_plan(column), driver, rows)


def owner_of(plan: "_rows.GeneratorPlan") -> _Owner:
    return _Owner(plan.sim_id, plan.expose.expose_id, plan.seam_digest)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def related_columns(keys: Optional[ExposeKeys]) -> FrozenSet[str]:
    """The columns of an expose whose values the relational plan decides.

    Every column of every key the contract declares for it, and every column of
    every relationship it declares. :func:`flux_engine.generator.rows.build_plan`
    lets these columns declare no draw of their own.
    """
    if keys is None:
        return frozenset()
    columns: Set[str] = set()
    for key in (*keys.keys, *keys.instant_keys):
        columns.update(key)
    for relationship in keys.relationships:
        columns.update(relationship.from_columns)
    return frozenset(columns)


def layout_key(seam_digest: str, parts: Sequence[str], *, counter_based: bool) -> bytes:
    """KERNEL-SPEC §8.2's key ``k`` of what *parts* lays out: from ``H``, never from the seed.

    ``sha256(domain NUL H_bytes NUL label(parts))``, where *domain* is ``flux-key/1``, or
    ``flux-key/2`` under kernel ``0.2.0`` (KERNEL-SPEC-2 section 8).
    """
    domain = _KEY_LABEL_2 if counter_based else _KEY_LABEL
    material = domain + _NUL + digest_bytes(seam_digest) + _NUL + path_label(tuple(parts))
    return bytes.fromhex(sha256_hex(material))


def _layout_key(plan: "_rows.GeneratorPlan", *parts: str) -> bytes:
    """A layout's permutation key, for a column or a relationship of *plan*'s expose."""
    return layout_key(
        plan.seam_digest,
        ("sim", plan.sim_id, "gen", plan.expose.expose_id, *parts),
        counter_based=plan.counter_based,
    )


#: One relationship as the contract declares it: (expose, fromColumns, target, toColumns, joinType).
_Declared = Tuple[str, Tuple[str, ...], str, Tuple[str, ...], str]


@dataclass(frozen=True)
class _Shape:
    """One expose's relational shape, before parents are read into views."""

    plan: "_rows.GeneratorPlan"
    #: Columns laid out on their own, and how.
    layouts: Mapping[str, Union[KeyGrid, IdentifierKey]]
    #: The several-column key each laid-out member column belongs to.
    members: Mapping[str, Tuple[str, ...]]
    #: The relationship that sets each foreign key column.
    owners: Mapping[str, Relationship]
    #: Relationships that reference each parent row at most once.
    distinct: FrozenSet[str]
    #: Relationships whose columns a several-column key lays out.
    absorbed: FrozenSet[str]

    @property
    def columns(self) -> Tuple[str, ...]:
        """Every column the shape decides, sorted."""
        named = set(self.layouts) | set(self.members)
        named.update(
            column
            for column, relationship in self.owners.items()
            if relationship.name not in self.absorbed
        )
        return tuple(sorted(named))


def relate_plans(
    plans: Mapping[str, "_rows.GeneratorPlan"], keys: ContractKeys
) -> Dict[str, "_rows.GeneratorPlan"]:
    """The plans of one contract's generator-driven exposes, with their relations.

    *plans* maps each exposeId the Simulation generates from statistics to its
    plan. An expose for which the contract declares no key and no relationship
    comes back unchanged, so its ``generatorDigest`` does not move. Every
    refusal is ``FLX-3403``, raised before a row exists.
    """
    declared: FrozenSet[_Declared] = frozenset(
        (
            relationship.source,
            relationship.from_columns,
            relationship.target,
            relationship.to_columns,
            relationship.join_type,
        )
        for expose in keys.exposes.values()
        for relationship in expose.relationships
    )
    shapes = {
        expose_id: _shape(plans[expose_id], keys.expose(expose_id), plans, declared)
        for expose_id in sorted(plans)
    }
    resolver = _Resolver(shapes)
    related: Dict[str, "_rows.GeneratorPlan"] = {}
    for expose_id, plan in plans.items():
        drivers = resolver.drivers(expose_id)
        related[expose_id] = (
            plan if not drivers else replace(plan, relations=Relations(drivers=drivers))
        )
    return related


def _shape(
    plan: "_rows.GeneratorPlan",
    keys: ExposeKeys,
    plans: Mapping[str, "_rows.GeneratorPlan"],
    declared: FrozenSet[_Declared],
) -> _Shape:
    expose_id = plan.expose.expose_id
    by_name = {column_plan.column.name: column_plan for column_plan in plan.columns}
    key_tuples = sorted(set(keys.keys) | set(keys.instant_keys), key=lambda key: (len(key), key))
    key_sets = {frozenset(key) for key in key_tuples}
    relationships = list(keys.relationships)

    for relationship in relationships:
        _check_relationship(plan, relationship, plans, by_name)
    distinct = frozenset(
        relationship.name
        for relationship in relationships
        if relationship.cardinality == "one_to_one"
        or frozenset(relationship.from_columns) in key_sets
    )
    for relationship in relationships:
        parent_rows = plans[relationship.target].rows
        if relationship.name in distinct and plan.rows > parent_rows:
            why = (
                "it is one_to_one"
                if relationship.cardinality == "one_to_one"
                else "its columns are a key of this expose"
            )
            raise _refuse(
                f"expose {expose_id!r} relationship {relationship.name!r} references each row "
                f"of {relationship.target!r} at most once ({why}), but declares {plan.rows} "
                f"rows against its {parent_rows}; declare no more rows than the parent"
            )

    owners = _owners(expose_id, relationships, distinct, declared)
    members, absorbed = _composites(expose_id, key_tuples, owners, distinct)

    layouts: Dict[str, Union[KeyGrid, IdentifierKey]] = {}
    for key in key_tuples:
        if len(key) != 1:
            continue
        column = key[0]
        owner = owners.get(column)
        if owner is None:
            layouts[column] = _one_column_layout(plan, by_name[column])
        elif owner.from_columns != key:
            others = [name for name in owner.from_columns if name != column]
            raise _refuse(
                f"expose {expose_id!r} column {column!r} is a key on its own, but "
                f"relationship {owner.name!r} sets it together with {others}; a key "
                "column takes its values from a relationship of its own, or from none"
            )

    shape = _Shape(
        plan=plan,
        layouts=layouts,
        members=members,
        owners=owners,
        distinct=distinct,
        absorbed=absorbed,
    )
    entity_keys = {
        entity.column for entity in keys.entities if entity.kind in ("primary", "unique")
    }
    # Columns another relationship of the contract points at: a child copies them.
    targeted = {
        column
        for _source, _from, target, to_columns, _join in declared
        if target == expose_id
        for column in to_columns
    }
    _check_key_columns(shape, by_name, relationships, frozenset(entity_keys | targeted), key_tuples)
    return shape


def _check_relationship(
    plan: "_rows.GeneratorPlan",
    relationship: Relationship,
    plans: Mapping[str, "_rows.GeneratorPlan"],
    by_name: Mapping[str, "_rows.ColumnPlan"],
) -> None:
    expose_id = plan.expose.expose_id
    target = plans.get(relationship.target)
    if target is None:
        raise _refuse(
            f"expose {expose_id!r} relationship {relationship.name!r} targets "
            f"{relationship.target!r}, which this Simulation does not generate from "
            "statistics; emit it with a fidelity block in the same Simulation, so the "
            "foreign key can take the keys its rows hold"
        )
    if plan.rows > 0 and target.rows == 0:
        raise _refuse(
            f"expose {expose_id!r} relationship {relationship.name!r} targets "
            f"{relationship.target!r}, which declares no rows, so there is no parent row to "
            "reference; declare rows for it"
        )
    target_columns = {column_plan.column.name: column_plan for column_plan in target.columns}
    for mine, theirs in zip(relationship.from_columns, relationship.to_columns, strict=True):
        own = by_name[mine].column
        other = target_columns[theirs].column
        kind, other_kind = own.logical_type.kind, other.logical_type.kind
        if kind is not other_kind or (
            kind is LogicalKind.DECIMAL
            and (own.logical_type.precision, own.logical_type.scale or 0)
            != (other.logical_type.precision, other.logical_type.scale or 0)
        ):
            raise _refuse(
                f"expose {expose_id!r} relationship {relationship.name!r} pairs {mine!r} "
                f"({own.type_name}) with {relationship.target}.{theirs} ({other.type_name}); "
                "a foreign key holds its parent's values, so declare both columns with one type"
            )


def _owners(
    expose_id: str,
    relationships: Sequence[Relationship],
    distinct: FrozenSet[str],
    declared: FrozenSet[_Declared],
) -> Dict[str, Relationship]:
    """The relationship that sets each foreign key column.

    The relationship with the most columns sets them. Another whose columns all
    lie inside it is kept by it when the first relationship's parent declares
    the same relationship on the paired columns: copying a parent row then keeps
    both (a return's item key inside its sale key, the sale's own item key
    pointing at the item).
    """
    owners: Dict[str, Relationship] = {}
    order = sorted(
        range(len(relationships)), key=lambda i: (-len(relationships[i].from_columns), i)
    )
    for index in order:
        relationship = relationships[index]
        taken = [column for column in relationship.from_columns if column in owners]
        if not taken:
            for column in relationship.from_columns:
                owners[column] = relationship
            continue
        holders = {owners[column].name for column in taken}
        holder = owners[taken[0]]
        if (
            len(holders) == 1
            and set(relationship.from_columns) <= set(holder.from_columns)
            and relationship.name not in distinct
            and _implied(relationship, holder, declared)
        ):
            continue
        raise _refuse(
            f"expose {expose_id!r} relationships {sorted(holders | {relationship.name})} both "
            f"set {sorted(taken)}; two relationships may share a column only when one's "
            "columns lie inside the other's and the other's parent declares the same "
            "relationship on the paired columns"
        )
    return owners


def _implied(nested: Relationship, holder: Relationship, declared: FrozenSet[_Declared]) -> bool:
    """Whether copying *holder*'s parent row keeps *nested* too.

    The parent must declare the same relationship on the paired columns. When
    *nested* is ``inner``, so must the parent's be, or the value it copies could
    be null.
    """
    paired = dict(zip(holder.from_columns, holder.to_columns, strict=True))
    image = tuple(paired[column] for column in nested.from_columns)
    joins = ("inner",) if nested.join_type == "inner" else ("inner", "left")
    return any(
        (holder.target, image, nested.target, nested.to_columns, join) in declared for join in joins
    )


def _composites(
    expose_id: str,
    key_tuples: Sequence[Tuple[str, ...]],
    owners: Mapping[str, Relationship],
    distinct: FrozenSet[str],
) -> Tuple[Dict[str, Tuple[str, ...]], FrozenSet[str]]:
    """The several-column keys that need a layout, by member column, and the relationships inside them.

    A key needs none when a smaller key inside it is unique already (a one-column
    key, or one a distinct relationship sets). Otherwise every relationship that
    sets one of its columns must lie wholly inside it, and becomes one part of
    its layout, whose domain is the parent's rows.
    """
    covered: List[FrozenSet[str]] = []
    members: Dict[str, Tuple[str, ...]] = {}
    absorbed: Set[str] = set()
    distinct_sets = {
        frozenset(relationship.from_columns)
        for relationship in owners.values()
        if relationship.name in distinct
    }
    for key in key_tuples:
        columns = frozenset(key)
        if len(key) == 1 or columns in distinct_sets or any(c <= columns for c in covered):
            covered.append(columns)
            continue
        inside = {owners[column].name: owners[column] for column in key if column in owners}
        for relationship in inside.values():
            if not set(relationship.from_columns) <= columns:
                raise _refuse(
                    f"expose {expose_id!r} key {list(key)} shares columns with relationship "
                    f"{relationship.name!r} {list(relationship.from_columns)}, which reaches "
                    "outside it; a key holds a relationship's columns all or none"
                )
        for column in key:
            if column in members:
                raise _refuse(
                    f"expose {expose_id!r} column {column!r} belongs to keys {list(members[column])} "
                    f"and {list(key)}, and neither holds a smaller unique key; declare one of "
                    "them, or a one-column key inside both"
                )
            members[column] = key
        absorbed.update(inside)
        covered.append(columns)
    return members, frozenset(absorbed)


def _declared_draw_fields(statistics: Any) -> List[str]:
    """The draw fields a key column declares that a key layout cannot keep."""
    fields = []
    for name, spelling in (
        ("levels", "levels"),
        ("support", "support"),
        ("mean", "mean"),
        ("stddev", "stddev"),
        ("rate", "rate"),
        ("given", "given"),
        ("bin_weights_given", "binWeightsGiven"),
        ("level_weights_given", "levelWeightsGiven"),
    ):
        if getattr(statistics, name) not in (None, False, ()):
            fields.append(spelling)
    return fields


def _check_key_columns(
    shape: _Shape,
    by_name: Mapping[str, "_rows.ColumnPlan"],
    relationships: Sequence[Relationship],
    held: FrozenSet[str],
    key_tuples: Sequence[Tuple[str, ...]],
) -> None:
    """Refuse a null on a key column or on an inner relationship.

    A key that is laid out, and a primary or unique entity, holds a value on
    every row. A foreign key whose relationship references each parent row at
    most once may still be null when its ``joinType`` is ``left``: a null row
    references no parent, and the rows that are not null reference distinct
    parents, so its columns stay unique wherever they hold a value. That holds
    only while no other relationship targets those columns: a child would copy
    the null, so a targeted column holds a value on every row too.
    """
    expose_id = shape.plan.expose.expose_id
    # A column may be null only as part of a distinct foreign key whose own
    # columns are the one key it belongs to. *held* names the columns that
    # may never be: a primary or unique entity's, and every column another
    # relationship targets, because a child copies it.
    nullable = set()
    for relationship in relationships:
        if relationship.name not in shape.distinct:
            continue
        own = frozenset(relationship.from_columns)
        for column in relationship.from_columns:
            others = [key for key in key_tuples if column in key and frozenset(key) != own]
            if column not in held and not others:
                nullable.add(column)
    key_columns = {column for key in key_tuples for column in key}
    keyed = (key_columns | set(shape.layouts) | set(shape.members)) - nullable
    for column in sorted(keyed):
        statistics = by_name[column].statistics
        if statistics.null_fraction is not None:
            raise _refuse(
                f"expose {expose_id!r} column {column!r} is part of a key, so it holds a "
                "value on every row; drop its nullFraction"
            )
    for relationship in relationships:
        for column in relationship.from_columns[1:]:
            if by_name[column].statistics.null_fraction is not None:
                raise _refuse(
                    f"expose {expose_id!r} relationship {relationship.name!r} is null on all its "
                    f"columns or none, and its first column {relationship.from_columns[0]!r} "
                    f"decides; drop the nullFraction of {column!r}"
                )
        if relationship.join_type != "inner":
            continue
        holder = shape.owners.get(relationship.from_columns[0], relationship)
        if holder is not relationship and by_name[holder.from_columns[0]].statistics.null_fraction:
            raise _refuse(
                f"expose {expose_id!r} relationship {relationship.name!r} has joinType inner, "
                f"but relationship {holder.name!r}, which sets its columns, may be null; drop "
                f"the nullFraction of {holder.from_columns[0]!r}, or make the joinType left"
            )
        for column in relationship.from_columns:
            if by_name[column].statistics.null_fraction is not None:
                raise _refuse(
                    f"expose {expose_id!r} relationship {relationship.name!r} has joinType "
                    f"inner, so every row references a parent; drop the nullFraction of "
                    f"{column!r}, or make the joinType left"
                )


def _one_column_layout(
    plan: "_rows.GeneratorPlan", column_plan: "_rows.ColumnPlan"
) -> Union[KeyGrid, IdentifierKey]:
    column, statistics = column_plan.column, column_plan.statistics
    expose_id = plan.expose.expose_id
    kind = column.logical_type.kind
    if kind is LogicalKind.STRING:
        length = statistics.identifier_length or _rows.IDENTIFIER_DEFAULT_LENGTH
        as_uuid = column.declared_type.strip().lower() in _rows._UUID_TYPES
        _rows._require_identifier_shape(length, as_uuid)
        layout = IdentifierKey(
            key=_layout_key(plan, "key", column.name),
            length=length,
            as_uuid=as_uuid,
            rows=plan.rows,
            counter_based=plan.counter_based,
        )
        if plan.rows > layout.space:
            raise _refuse(
                f"expose {expose_id!r} key {column.name!r} is an identifier of {length} hex "
                f"characters, which holds {layout.space} values, fewer than its {plan.rows} "
                "rows; declare a longer identifierLength"
            )
        return layout
    if kind not in _ORDERED_KINDS:
        raise _refuse(
            f"expose {expose_id!r} key {column.name!r} is {column.type_name}; a key column is "
            "INT, DECIMAL, DATE, TIMESTAMP or STRING"
        )
    return _key_grid(plan, column_plan)


def _key_grid(plan: "_rows.GeneratorPlan", column_plan: "_rows.ColumnPlan") -> KeyGrid:
    """Lay a one-column ordered key out over its declared range or bins."""
    column, statistics = column_plan.column, column_plan.statistics
    where = f"expose {plan.expose.expose_id!r} key {column.name!r}"
    _check_key_declaration(statistics, plan.rows, where)
    exponent = _exponent(column)
    spans, counts = _key_spans(column, statistics, plan.rows, exponent, where)
    firsts: List[int] = []
    widths: List[int] = []
    kept: List[int] = []
    starts: List[int] = []
    start = 0
    for index, ((first, width), count) in enumerate(zip(spans, counts, strict=True)):
        if count > width:
            what = "its declared range" if len(spans) == 1 else f"bin {index} of its histogram"
            raise _refuse(
                f"{where}: {what} holds {width} values but takes {count} of its {plan.rows} "
                "rows; widen the range, or move weight to wider bins, so that no value repeats"
            )
        if count:
            firsts.append(first)
            widths.append(width)
            kept.append(count)
            starts.append(start)
            start += count
    return KeyGrid(
        kind=column.logical_type.kind.value,
        exponent=exponent,
        firsts=tuple(firsts),
        widths=tuple(widths),
        counts=tuple(kept),
        starts=tuple(starts),
        rows=plan.rows,
    )


def _check_key_declaration(statistics: Any, rows: int, where: str) -> None:
    """Refuse a key declaration a layout cannot keep: a draw that repeats, or too few values."""
    unkept = _declared_draw_fields(statistics)
    family = (statistics.family or "").strip().lower()
    if unkept or family not in ("", "uniform", "empirical", "uniform/empirical", "histogram"):
        named = unkept or [f"family {statistics.family!r}"]
        raise _refuse(
            f"{where} declares {named}, which would repeat values; a key is laid out over "
            "a declared range, or bins with binWeights, and never repeats"
        )
    if statistics.max_cardinality is not None and statistics.max_cardinality < rows:
        raise _refuse(
            f"{where} declares maxCardinality {statistics.max_cardinality} for {rows} rows; "
            "a key holds one value per row"
        )


def _key_spans(
    column: CompiledColumn, statistics: Any, rows: int, exponent: int, where: str
) -> Tuple[List[Tuple[int, int]], List[int]]:
    """Each bin's first grid point and width, and the rows it takes."""
    if statistics.bins is not None:
        edges = statistics.bins
        last_bin = len(edges) - 2
        spans: List[Tuple[int, int]] = []
        for index in range(len(edges) - 1):
            first = _ceil_units(edges[index], exponent)
            if index == last_bin:
                last = _floor_units(edges[index + 1], exponent)
            else:
                last = _ceil_units(edges[index + 1], exponent) - 1
            spans.append((first, max(0, last - first + 1)))
        if statistics.bin_weights is None:
            raise _refuse(f"{where} declares bins without binWeights")
        return spans, _allot(rows, statistics.bin_weights, where)
    if statistics.has_range:
        first = _ceil_units(statistics.range_min, exponent)
        last = _floor_units(statistics.range_max, exponent)
        return [(first, max(0, last - first + 1))], [rows]
    if column.logical_type.kind is LogicalKind.INT:
        return [(1, rows)], [rows]
    raise _refuse(
        f"{where} declares no range; declare a range, or bins with binWeights, that holds at "
        f"least its {rows} rows"
    )


def _allot(rows: int, weights: Sequence[Decimal], where: str) -> List[int]:
    """Rows per bin, by weight: the Largest Remainder Method (KERNEL-SPEC section 5)."""
    if rows == 0:
        return [0] * len(weights)
    exact = [Fraction(weight) for weight in weights]
    total = sum(exact, Fraction(0))
    if total <= 0 or any(weight < 0 for weight in exact):
        raise _refuse(f"{where} declares binWeights that do not share out any rows")
    names = [str(index).zfill(6) for index in range(len(weights))]
    shares = {
        name: _rows._fraction_as_decimal(weight / total)
        for name, weight in zip(names, exact, strict=True)
    }
    counts = largest_remainder(rows, _rows._renormalised(shares))
    return [counts[name] for name in names]


def _declares_draw(column_plan: "_rows.ColumnPlan") -> bool:
    """Whether the column declares a draw of its own (a time-bounds default is none)."""
    return not column_plan.defaulted and _rows._declares_a_draw(column_plan.statistics)


def _plain_part(plan: "_rows.GeneratorPlan", column_plan: "_rows.ColumnPlan") -> Part:
    """A plain column of a several-column key: its declared levels, or its range's grid."""
    column, statistics = column_plan.column, column_plan.statistics
    where = f"expose {plan.expose.expose_id!r} key column {column.name!r}"
    unkept = [
        name for name in _declared_draw_fields(statistics) if name not in ("levels", "support")
    ]
    if unkept:
        raise _refuse(
            f"{where} declares {unkept}; the columns of a several-column key are spread "
            "evenly over their declared range, bins or levels, so that no key repeats"
        )
    if statistics.levels is not None or statistics.support:
        names = tuple(sorted(_rows._level_names(statistics)))
        with localcontext(_rows._KERNEL_CONTEXT):
            held = [_rows._level_to_value(name, column) for name in names]
        if len(set(held)) != len(held):
            raise _refuse(
                f"{where} declares levels that spell one value more than once, so the key "
                "would repeat it; declare each value once"
            )
        return LevelPart(column=column, levels=names)
    kind = column.logical_type.kind
    if kind not in _ORDERED_KINDS:
        raise _refuse(f"{where} is {column.type_name} and declares no levels; declare them")
    exponent = _exponent(column)
    if statistics.bins is not None:
        low, high = statistics.bins[0], statistics.bins[-1]
    elif statistics.has_range:
        assert statistics.range_min is not None and statistics.range_max is not None
        low, high = statistics.range_min, statistics.range_max
    else:
        raise _refuse(f"{where} declares no range, bins or levels to spread its keys over")
    first, last = _ceil_units(low, exponent), _floor_units(high, exponent)
    if last < first:
        raise _refuse(f"{where}: its declared range holds no value of its type")
    return GridPart(
        column=column.name,
        kind=kind.value,
        exponent=exponent,
        first=first,
        size=last - first + 1,
    )


class _Resolver:
    """Turns shapes into drivers, reading each parent into a view on the way.

    A driver is built once per (expose, column) and a view once per (expose,
    columns). A column whose driver needs itself, through a chain of keys
    across exposes, is refused: no order of the rows could produce it.
    """

    def __init__(self, shapes: Mapping[str, _Shape]) -> None:
        self.shapes = shapes
        self._drivers: Dict[Tuple[str, str], Optional[Driver]] = {}
        self._views: Dict[Tuple[str, Tuple[str, ...]], ParentView] = {}
        self._composites: Dict[Tuple[str, Tuple[str, ...]], CompositeKey] = {}
        self._foreign: Dict[Tuple[str, str], ForeignKey] = {}
        self._active: List[Tuple[str, str]] = []

    def drivers(self, expose_id: str) -> Tuple[Tuple[str, Driver], ...]:
        shape = self.shapes[expose_id]
        found: List[Tuple[str, Driver]] = []
        for column in shape.columns:
            driver = self.driver(expose_id, column)
            if driver is not None:
                found.append((column, driver))
        self._check_undriven(shape, dict(found))
        self._check_givens(shape, dict(found))
        return tuple(found)

    def _check_undriven(self, shape: _Shape, drivers: Mapping[str, Driver]) -> None:
        """Refuse a key column that nothing produces.

        A column of a several-column key needs no layout when a smaller unique
        key lies inside that key, and is then drawn as it declares. One that
        declares no draw would have no value at all, so it is refused here,
        before any row, rather than when its first row is drawn.
        """
        expose_id = shape.plan.expose.expose_id
        # A column that a derive without `when` sets on every row is produced by its rule.
        ruled = frozenset() if shape.plan.rules is None else shape.plan.rules.covers_every_row
        for column_plan in shape.plan.columns:
            name = column_plan.column.name
            if (
                column_plan.family is None
                and column_plan.column_type is not ColumnType.IDENTIFIER
                and name not in drivers
                and name not in ruled
            ):
                raise _refuse(
                    f"expose {expose_id!r} column {name!r} is part of a key that a smaller "
                    "unique key inside it already keeps unique, so it is drawn as it "
                    "declares, and it declares nothing; declare a range, bins or levels for it"
                )

    def driver(self, expose_id: str, column: str) -> Optional[Driver]:
        address = (expose_id, column)
        if address in self._drivers:
            return self._drivers[address]
        if address in self._active:
            chain = " -> ".join(f"{e}.{c}" for e, c in [*self._active, address])
            raise _refuse(
                f"the keys {chain} each take their values from the next, round to the first; "
                "break the cycle so that one of them is laid out on its own"
            )
        self._active.append(address)
        try:
            driver = self._build(expose_id, column)
        finally:
            self._active.pop()
        self._drivers[address] = driver
        return driver

    def _build(self, expose_id: str, column: str) -> Optional[Driver]:
        shape = self.shapes[expose_id]
        layout = shape.layouts.get(column)
        if layout is not None:
            return layout
        key = shape.members.get(column)
        if key is not None:
            return CompositeMember(self.composite(expose_id, key))
        relationship = shape.owners.get(column)
        if relationship is not None and relationship.name not in shape.absorbed:
            return self.foreign(expose_id, relationship)
        return None

    def view(self, expose_id: str, columns: Sequence[str]) -> ParentView:
        names = tuple(sorted(set(columns)))
        found = self._views.get((expose_id, names))
        if found is not None:
            return found
        plan = self.shapes[expose_id].plan
        drivers: List[Tuple[str, Driver]] = []
        for name in names:
            if plan.column_plan(name).statistics.given is not None:
                # A parent's value is read at its row alone, with no other
                # column of that row in hand to condition on.
                raise _refuse(
                    f"expose {expose_id!r} column {name!r} is read by a relationship and is "
                    "drawn given another column; drop its given"
                )
            driver = self.driver(expose_id, name)
            if driver is not None:
                drivers.append((name, driver))
        view = ParentView(
            sim_id=plan.sim_id,
            expose_id=expose_id,
            seam_digest=plan.seam_digest,
            rows=plan.rows,
            columns=tuple(plan.column_plan(name) for name in names),
            drivers=tuple(drivers),
        )
        self._views[(expose_id, names)] = view
        return view

    def foreign(self, expose_id: str, relationship: Relationship) -> ForeignKey:
        found = self._foreign.get((expose_id, relationship.name))
        if found is not None:
            return found
        shape = self.shapes[expose_id]
        plan = shape.plan
        parent = self.view(relationship.target, relationship.to_columns)
        first = plan.column_plan(relationship.from_columns[0])
        pick, grid, key = "uniform", None, None
        if relationship.name in shape.distinct:
            pick, key = "distinct", _layout_key(plan, "rel", relationship.name)
        elif len(relationship.from_columns) == 1:
            target = parent.driver(relationship.to_columns[0])
            if isinstance(target, KeyGrid) and _declares_draw(first):
                pick, grid = "declared", target
        foreign = ForeignKey(
            relationship=relationship.name,
            from_columns=relationship.from_columns,
            to_columns=relationship.to_columns,
            pick=pick,
            parent=parent,
            grid=grid,
            key=key,
            first_null=first.statistics.null_fraction,
            counter_based=plan.counter_based,
        )
        self._foreign[(expose_id, relationship.name)] = foreign
        return foreign

    def composite(self, expose_id: str, key: Tuple[str, ...]) -> CompositeKey:
        found = self._composites.get((expose_id, key))
        if found is not None:
            return found
        shape = self.shapes[expose_id]
        plan = shape.plan
        parts: List[Part] = []
        held: Set[str] = set()
        seen: Set[str] = set()
        for column in key:
            relationship = shape.owners.get(column)
            if relationship is None or relationship.name in seen:
                continue
            seen.add(relationship.name)
            parts.append(
                ParentPart(
                    relationship=relationship.name,
                    from_columns=relationship.from_columns,
                    to_columns=relationship.to_columns,
                    parent=self.view(relationship.target, relationship.to_columns),
                )
            )
            held.update(relationship.from_columns)
        for column in key:
            if column not in held:
                parts.append(_plain_part(plan, plan.column_plan(column)))
        parts.sort(key=lambda part: min(key.index(column) for column in part.columns))
        space = 1
        for part in parts:
            space *= part.size
        where = f"expose {expose_id!r} key {list(key)}"
        if space < plan.rows:
            raise _refuse(
                f"{where} can take {space} distinct values over its columns' declared domains, "
                f"fewer than its {plan.rows} rows; widen a column's range or levels, or add "
                "parent rows"
            )
        if space > PERMUTE_MAX_DOMAIN:
            raise _refuse(
                f"{where} spans more than 2**256 values over its columns' declared domains; "
                "narrow a range"
            )
        composite = CompositeKey(
            columns=key,
            key=_layout_key(plan, "key", *key),
            parts=tuple(parts),
            space=space,
            counter_based=plan.counter_based,
        )
        self._composites[(expose_id, key)] = composite
        return composite

    def _check_givens(self, shape: _Shape, drivers: Mapping[str, Driver]) -> None:
        """Refuse a column drawn given a related column whose value is not drawn."""
        expose_id = shape.plan.expose.expose_id
        for column_plan in shape.plan.columns:
            given = column_plan.statistics.given
            if given is None:
                continue
            name = column_plan.column.name
            if name in drivers:
                driver = drivers[name]
                if not (isinstance(driver, ForeignKey) and driver.pick == "declared"):
                    raise _refuse(
                        f"expose {expose_id!r} column {name!r} takes its values from keys and "
                        "is not drawn, so it cannot be drawn given another column; drop its given"
                    )
            source = drivers.get(given)
            if source is not None and not (
                isinstance(source, ForeignKey) and source.pick == "declared"
            ):
                raise _refuse(
                    f"expose {expose_id!r} column {name!r} is drawn given {given!r}, whose "
                    "values come from keys and are not drawn; drop the given, or give "
                    f"{given!r} a declared draw over an ordered parent key"
                )
