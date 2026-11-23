# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An emit decoded once into exact integer columns, and the statistics formed from them.

``verify`` reads every emit several ways: row conformance, each declared property, the shape
flags. Reading a row there means one ``Fraction`` per number, one NFC string per category and
a dictionary per row, again for every reader. Here each column is decoded once, into the
integers its values already are:

* a decimal written at a fixed scale (``"356.59"``) is its **units** at that scale, an int64
  (``35659``, scale 2), and an integer is its own units at scale 0;
* a date is its **days** since 1970-01-01 and an instant its **milliseconds**, as the profile
  reads them;
* a category, or a boolean read as ``"false"`` and ``"true"``, is a **level code** into the
  column's distinct NFC strings.

A statistic is then formed from the arrays with numpy: a mean from integer sums (split so
that no int64 wraps), a histogram or a quantile's bin by one sorted search, a level histogram
by a count. Every value is an integer at a known scale, so each result is exactly the one the
row-by-row reading of :mod:`flux_stats.reading` forms: ``test_decoded.py`` holds the two to
each other over generated columns.

Whenever a cell is not exactly what the fast reading takes (a number spelled in another form,
a value outside its declared range, a null under ``null: fail``, anything that is refused),
the function returns ``None`` and the caller reads the rows as it always did, so what is
refused is refused by the reader that always refused it, naming the row it always named.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union, overload

import numpy as np

from flux_engine.core.errors import FluxError
from flux_stats.fidelity import (
    ColumnDeclaration,
    PropertyDeclaration,
    PropertyKind,
    definition_for_property,
)
from flux_stats.fixedpoint import Grid, order_statistic_index
from flux_stats.reading import (
    MS_PER_DAY,
    UNGROUPED,
    NullPolicy,
    ValueDomain,
    level_cells,
    nfc,
    parse_date_days,
    parse_rfc3339_ms,
)
from flux_stats.registry import GroupStatistic, Parameters

__all__ = [
    "DecodedRows",
    "bin_cells",
    "Exact",
    "Levels",
    "group_keys_decoded",
    "measure_decoded",
    "safe_sum",
]

_NONE = type(None)
_INT64 = (1 << 63) - 1


@dataclass(frozen=True)
class Exact:
    """A column of numbers as ``units * 10**-scale``: int64 units, and which cells hold one."""

    units: "np.ndarray"
    scale: int
    #: ``None`` when no cell is null; else true where the cell holds a value.
    valid: Optional["np.ndarray"] = None


@dataclass(frozen=True)
class Levels:
    """A column of categories as codes into its distinct NFC strings; ``-1`` is a null."""

    codes: "np.ndarray"
    levels: Tuple[str, ...]


def safe_sum(values: "np.ndarray") -> int:
    """The exact sum of an int64 array, as a Python int: no partial sum wraps.

    The high and the low halves are summed apart (each fits an int64 for any array an int64
    index can address) and put back together.
    """
    if values.size == 0:
        return 0
    high = int((values >> 32).sum(dtype=np.int64))
    low = int((values & 0xFFFFFFFF).sum(dtype=np.int64))
    return (high << 32) + low


def _digits(scale: int) -> "re.Pattern[str]":
    body = r"-?[0-9]+" + (rf"\.[0-9]{{{scale}}}" if scale else "")
    return re.compile(rf"(?:{body}\n)*{body}")


_CANONICAL_INSTANT = re.compile(
    r"(?:[1-9][0-9]{3}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z\n)*"
    r"[1-9][0-9]{3}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
)
_CANONICAL_DAY = re.compile(
    r"(?:[1-9][0-9]{3}-[0-9]{2}-[0-9]{2}\n)*[1-9][0-9]{3}-[0-9]{2}-[0-9]{2}"
)


class DecodedRows:
    """One emit's rows as a sequence of mappings, and its columns decoded on demand.

    A ``Sequence`` of the mappings :meth:`flux_engine.concordance.gate.PackedEmit.as_mappings`
    gives, so every reader of rows takes it as it takes the list; and the columns, each
    decoded once, for the readers that take them: :meth:`exact`, :meth:`levels`,
    :meth:`instants`. Nothing is copied: the rows are the decoded JSON arrays.
    """

    def __init__(self, names: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        names = tuple(names)
        if rows and set(map(len, rows)) != {len(names)}:
            raise ValueError("a row's width is not the header's")
        self.names = names
        self.rows = rows
        self._index: Dict[str, int] = {name: i for i, name in enumerate(names)}
        self._columns: Optional[List[Tuple[Any, ...]]] = None
        self._cache: Dict[Tuple[str, str], Any] = {}

    # -- the Sequence of mappings -------------------------------------------

    def __len__(self) -> int:
        return len(self.rows)

    @overload
    def __getitem__(self, index: int) -> Mapping[str, Any]: ...

    @overload
    def __getitem__(self, index: slice) -> List[Mapping[str, Any]]: ...

    def __getitem__(
        self, index: Union[int, slice]
    ) -> Union[Mapping[str, Any], List[Mapping[str, Any]]]:
        if isinstance(index, slice):
            return [dict(zip(self.names, row, strict=True)) for row in self.rows[index]]
        return dict(zip(self.names, self.rows[index], strict=True))

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        for row in self.rows:
            yield dict(zip(self.names, row, strict=True))

    # -- the columns ---------------------------------------------------------

    def has(self, name: str) -> bool:
        return name in self._index

    def cells(self, name: str) -> Tuple[Any, ...]:
        """Every cell of the column, as the JSON gave it."""
        if self._columns is None:
            # No row has no cell, and still every named column is there to be read (empty).
            self._columns = (
                list(zip(*self.rows, strict=True)) if self.rows else [() for _ in self.names]
            )
        return self._columns[self._index[name]]

    def exact(self, name: str, domain: ValueDomain) -> Optional[Exact]:
        """The column read as a range column of *domain* is, or ``None`` when it cannot be so."""
        key = (name, domain.value)
        if key not in self._cache:
            try:
                self._cache[key] = self._exact(name, domain)
            except (ValueError, OverflowError, FluxError):
                self._cache[key] = None
        found: Optional[Exact] = self._cache[key]
        return found

    def _exact(self, name: str, domain: ValueDomain) -> Optional[Exact]:
        cells = self.cells(name)
        kinds = set(map(type, cells))
        if kinds <= {int, _NONE}:
            # A number is days already for a date column, and a number for a number column;
            # an instant is always a string (flux_stats.reading.read_exact).
            return None if domain is ValueDomain.TIMESTAMP else _from_ints(cells)
        if not kinds <= {str, _NONE}:
            return None
        if domain is ValueDomain.NUMBER:
            return _from_decimals(cells)
        if domain is ValueDomain.DATE:
            return _from_days(cells)
        return _from_instants(cells, dates_as_days=False)

    def instants(self, name: str) -> Optional[Exact]:
        """The column read as a profile reads a timestamp: epoch milliseconds, a date as its day's start."""
        key = (name, "instants")
        if key not in self._cache:
            try:
                cells = self.cells(name)
                found = (
                    _from_instants(cells, dates_as_days=True)
                    if set(map(type, cells)) <= {str, _NONE}
                    else None
                )
            except (ValueError, OverflowError, FluxError):
                found = None
            self._cache[key] = found
        result: Optional[Exact] = self._cache[key]
        return result

    def levels(self, name: str) -> Optional[Levels]:
        """The column as categories: NFC strings, a boolean as ``"false"`` or ``"true"``, a whole
        number as its decimal text."""
        key = (name, "levels")
        if key not in self._cache:
            self._cache[key] = self._levels(name)
        found: Optional[Levels] = self._cache[key]
        return found

    def _levels(self, name: str) -> Optional[Levels]:
        cells = self.cells(name)
        if not set(map(type, cells)) <= {str, bool, int, _NONE}:
            return None
        labels: Dict[Any, Optional[str]] = {}
        for raw in set(cells):
            labels[raw] = None if raw is None else _label(raw)
        distinct = tuple(sorted({label for label in labels.values() if label is not None}))
        position = {label: i for i, label in enumerate(distinct)}
        codes_of = {
            raw: (-1 if label is None else position[label]) for raw, label in labels.items()
        }
        codes = np.fromiter((codes_of[cell] for cell in cells), dtype=np.int64, count=len(cells))
        return Levels(codes, distinct)


def _label(raw: Union[str, bool, int]) -> str:
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, int):
        return str(raw)
    return nfc(raw)


def _present(cells: Sequence[Any]) -> Tuple[List[Any], Optional["np.ndarray"]]:
    """The cells that hold a value, and which cells those are (``None`` when every one does)."""
    missing = [cell is None for cell in cells]
    if not any(missing):
        return list(cells), None
    return [cell for cell in cells if cell is not None], ~np.array(missing, dtype=bool)


def _spread(values: Sequence[int], valid: Optional["np.ndarray"], count: int) -> "np.ndarray":
    """*values*, one per valid cell, as an int64 array of *count* cells, zero where null."""
    packed = np.array(values, dtype=np.int64)
    if valid is None:
        return packed
    full = np.zeros(count, dtype=np.int64)
    full[valid] = packed
    return full


def _from_ints(cells: Sequence[Any]) -> Exact:
    present, valid = _present(cells)
    return Exact(_spread(present, valid, len(cells)), 0, valid)


def _from_decimals(cells: Sequence[Any]) -> Optional[Exact]:
    """Decimal strings at one scale, such as an emit writes a DECIMAL column, as units."""
    present, valid = _present(cells)
    if not present:
        return Exact(np.zeros(len(cells), dtype=np.int64), 0, valid)
    first = present[0]
    scale = len(first) - first.index(".") - 1 if "." in first else 0
    if _digits(scale).fullmatch("\n".join(present)) is None:
        return None
    units = [int(text.replace(".", "")) for text in present]
    return Exact(_spread(units, valid, len(cells)), scale, valid)


def _from_days(cells: Sequence[Any]) -> Optional[Exact]:
    present, valid = _present(cells)
    if not present:
        return Exact(np.zeros(len(cells), dtype=np.int64), 0, valid)
    if _CANONICAL_DAY.fullmatch("\n".join(present)) is None:
        return None
    try:
        days = np.array(present, dtype="datetime64[D]").astype(np.int64)
    except ValueError:
        days = None
    if days is None or not _agrees(present, days, lambda text: parse_date_days(text)):
        table = {text: parse_date_days(text) for text in set(present)}
        days = np.array([table[text] for text in present], dtype=np.int64)
    return Exact(_spread(days.tolist(), valid, len(cells)), 0, valid)


def _from_instants(cells: Sequence[Any], *, dates_as_days: bool) -> Optional[Exact]:
    present, valid = _present(cells)
    if not present:
        return Exact(np.zeros(len(cells), dtype=np.int64), 0, valid)
    millis: Optional["np.ndarray"] = None
    if _CANONICAL_INSTANT.fullmatch("\n".join(present)) is not None:
        try:
            millis = np.array([text[:-1] for text in present], dtype="datetime64[ms]").astype(
                np.int64
            )
        except ValueError:
            millis = None
        if millis is not None and not _agrees(present, millis, parse_rfc3339_ms):
            millis = None
    if millis is None:

        def read(text: str) -> int:
            if dates_as_days and len(text) == 10:
                return parse_date_days(text) * MS_PER_DAY
            return parse_rfc3339_ms(text)

        table = {text: read(text) for text in set(present)}
        millis = np.array([table[text] for text in present], dtype=np.int64)
    return Exact(_spread(millis.tolist(), valid, len(cells)), 0, valid)


def _agrees(present: Sequence[str], fast: "np.ndarray", read: Any) -> bool:
    """Whether the vectorised reading equals the reference reader on the first, last and a middle cell.

    The vectorised parse only ever sees text of one canonical shape, which numpy and the
    reference reader both take as the same instant; this is the guard against the two
    disagreeing about a calendar edge, checked where it is cheap.
    """
    count = len(present)
    for position in {0, count // 2, count - 1}:
        if int(fast[position]) != read(present[position]):
            return False
    return True


# ---------------------------------------------------------------------------
# Grouping and measuring a property over decoded columns
# ---------------------------------------------------------------------------


def _selection(indices: Sequence[int], count: int) -> Optional["np.ndarray"]:
    """*indices* as an array, or ``None`` when they are every row in order."""
    if isinstance(indices, range) and indices == range(count):
        return None
    return np.asarray(indices, dtype=np.int64)


def group_keys_decoded(
    rows: DecodedRows, column: Optional[ColumnDeclaration], *, declared_only: bool = False
) -> Optional[List[Tuple[str, Any]]]:
    """:func:`flux_stats.reading.group_keys` over decoded columns, or ``None`` to read the rows.

    ``None`` for anything that reading would refuse: an absent column, a value that is not a
    category, a null under ``fail``, a level outside the declared support.
    """
    if column is None:
        return [(UNGROUPED, range(len(rows)))]
    if not rows.has(column.name):
        return None
    found = rows.levels(column.name)
    if found is None:
        return None
    codes, levels = found.codes, found.levels
    missing = codes < 0
    if missing.any() and column.null_policy is NullPolicy.FAIL:
        return None
    if declared_only and not set(levels) <= set(column.support_values):
        return None
    order = np.argsort(codes, kind="stable")
    ends = np.cumsum(np.bincount(codes[~missing], minlength=len(levels)))
    first = int(missing.sum())  # the nulls sort first, as code -1
    keyed: List[Tuple[str, Any]] = []
    start = first
    for code, level in enumerate(levels):
        stop = first + int(ends[code])
        keyed.append((level, order[start:stop]))
        start = stop
    return keyed


def _encoded(
    rows: DecodedRows,
    indices: Sequence[int],
    column: ColumnDeclaration,
    grid: Grid,
) -> Optional[Union["np.ndarray", List[int]]]:
    """:func:`flux_stats.reading.encode_value` of every value of the column over *indices*.

    ``None`` when any value would be refused (outside the declared range, a null under
    ``fail``) or cannot be read as exact integers, so the rows' own reader says which.
    """
    if not rows.has(column.name):
        return None
    found = rows.exact(column.name, column.domain)
    if found is None:
        return None
    selected = _selection(indices, len(rows))
    units = found.units if selected is None else found.units[selected]
    valid = found.valid if found.valid is None or selected is None else found.valid[selected]
    if valid is not None and not valid.all():
        if column.null_policy is NullPolicy.FAIL:
            return None
        units = units[valid]
    return _on_grid(units, found.scale, column, grid)


def _on_grid(
    units: "np.ndarray", scale: int, column: ColumnDeclaration, grid: Grid
) -> Optional[Union["np.ndarray", List[int]]]:
    if units.size == 0:
        return units
    low, high = int(units.min()), int(units.max())
    denominator = 10**scale
    interval = column.interval
    if low * interval.lo.denominator < interval.lo.numerator * denominator:
        return None
    if high * interval.hi.denominator > interval.hi.numerator * denominator:
        return None
    bits = grid.fractional_bits
    if max(abs(low), abs(high)) < (1 << (63 - bits)) and denominator <= _INT64:
        return (units << bits) // denominator
    return [(u << bits) // denominator for u in units.tolist()]


def _encoded_edges(declaration: PropertyDeclaration, grid: Grid) -> Tuple[int, ...]:
    return tuple(grid.quantise(edge, subject="bin edge") for edge in declaration.bins)


def _levels_in(
    rows: DecodedRows, indices: Sequence[int], column: ColumnDeclaration
) -> Optional[Tuple["np.ndarray", Tuple[str, ...]]]:
    if not rows.has(column.name):
        return None
    found = rows.levels(column.name)
    if found is None:
        return None
    selected = _selection(indices, len(rows))
    codes = found.codes if selected is None else found.codes[selected]
    missing = codes < 0
    if missing.any():
        if column.null_policy is NullPolicy.FAIL:
            return None
        codes = codes[~missing]
    return codes, found.levels


def measure_decoded(
    rows: DecodedRows,
    indices: Sequence[int],
    key: str,
    declaration: PropertyDeclaration,
    columns: Mapping[str, ColumnDeclaration],
    grid: Grid,
) -> Optional[GroupStatistic]:
    """:func:`flux_stats.fidelity.measure_group` over decoded columns, or ``None`` to read the rows.

    The same group, field for field, for a mean, a histogram, a quantile (binned or not), a level
    histogram and a support; a correlation, and anything the rows' reader would refuse, is the
    rows'.
    """
    kind = declaration.kind
    column = columns[declaration.columns[0]]
    if kind is PropertyKind.CORRELATION:
        return None
    if kind is PropertyKind.LEVEL_HISTOGRAM:
        return _measure_levels(rows, indices, key, column)
    if kind is PropertyKind.SUPPORT:
        return _measure_support(rows, indices, key, declaration, column)
    values = _encoded(rows, indices, column, grid)
    if values is None:
        return None
    array = values if isinstance(values, np.ndarray) else None
    count = len(values)
    if kind is PropertyKind.MEAN:
        total = safe_sum(array) if array is not None else sum(values)
        return GroupStatistic(group=key, k=count, sum_x=total)
    edges = _encoded_edges(declaration, grid)
    if kind is PropertyKind.MARGINAL_HISTOGRAM:
        if array is None or len(edges) < 2 or _wide(edges):
            return None
        return _measure_bins(key, array, edges)
    if array is None:
        return None
    return _measure_quantile(key, array, edges, declaration)


def _measure_bins(key: str, values: "np.ndarray", edges: Tuple[int, ...]) -> GroupStatistic:
    """:func:`flux_stats.definitions.bin_index` of each value: half-open bins, the last closed."""
    last = len(edges) - 2
    inner = np.array(edges[1 : last + 1], dtype=np.int64)
    cell = np.minimum(np.searchsorted(inner, values, side="right"), last)
    counts = np.bincount(cell, minlength=last + 1)
    return GroupStatistic(group=key, k=int(values.size), bin_counts=tuple(int(c) for c in counts))


def _wide(edges: Sequence[int]) -> bool:
    return any(abs(edge) > _INT64 for edge in edges)


def _measure_quantile(
    key: str, values: "np.ndarray", edges: Tuple[int, ...], declaration: PropertyDeclaration
) -> Optional[GroupStatistic]:
    if values.size == 0:
        return GroupStatistic(group=key, k=0)
    if declaration.level is None:
        return None
    ordered = np.sort(values)
    count = int(ordered.size)
    index = order_statistic_index(count, declaration.level)
    witness = int(ordered[index - 1])
    state = GroupStatistic(
        group=key,
        k=count,
        quantile_witness=witness,
        quantile_below=int(np.searchsorted(ordered, witness, side="left")),
        quantile_at_or_below=int(np.searchsorted(ordered, witness, side="right")),
    )
    if not definition_for_property(declaration).binned:
        return state
    if len(edges) < 2 or _wide(edges):
        return None
    inner = np.array(edges[1:-1], dtype=np.int64)
    position = min(int(np.searchsorted(inner, witness, side="right")), len(edges) - 2)
    last = position == len(edges) - 2
    return replace(
        state,
        quantile_bin=position,
        bin_below=int(np.searchsorted(ordered, edges[position], side="left")),
        bin_through=(
            count if last else int(np.searchsorted(ordered, edges[position + 1], side="left"))
        ),
    )


def _measure_levels(
    rows: DecodedRows, indices: Sequence[int], key: str, column: ColumnDeclaration
) -> Optional[GroupStatistic]:
    found = _levels_in(rows, indices, column)
    if found is None:
        return None
    codes, levels = found
    slots = {level: slot for slot, level in enumerate(level_cells(column))}
    slot_of = np.array([slots.get(level, -1) for level in levels], dtype=np.int64)
    slot = slot_of[codes] if levels else codes
    if slot.size and int(slot.min()) < 0:
        return None  # a level outside the declared support: refused by the rows' reader
    counts = np.bincount(slot, minlength=len(slots))
    return GroupStatistic(group=key, k=int(codes.size), level_counts=tuple(int(c) for c in counts))


def _measure_support(
    rows: DecodedRows,
    indices: Sequence[int],
    key: str,
    declaration: PropertyDeclaration,
    column: ColumnDeclaration,
) -> Optional[GroupStatistic]:
    found = _levels_in(rows, indices, column)
    if found is None:
        return None
    codes, levels = found
    categories = [levels[code] for code in codes.tolist()]
    group = definition_for_property(declaration).measure(key, categories, Parameters())
    maximum = column.max_cardinality
    if maximum is not None and len(group.support) > maximum:
        return None  # refused by the rows' reader, with its message
    return group


def bin_cells(
    units: "np.ndarray",
    scale: int,
    edges: Tuple[int, ...],
    bounds: Tuple[Any, Any],
    grid: Grid,
) -> Optional["np.ndarray"]:
    """The cell of the profile's bin table holding each value, ``-1`` for a value in none.

    What ``_Cells.locator`` computes one value at a time: a value is inside the profiled range
    when it lies between the first and the last edge exactly, and inside it falls in the bin
    its grid value gives by bisection (the last bin closed at the top). *units* are the values at
    *scale*, so each comparison is an integer one. ``None`` when an integer would leave int64.
    """
    denominator = 10**scale
    low, high = bounds
    first = -((-low.numerator * denominator) // low.denominator)  # ceil(low * 10**scale)
    last_units = (high.numerator * denominator) // high.denominator  # floor(high * 10**scale)
    if not all(-_INT64 <= bound <= _INT64 for bound in (first, last_units)) or _wide(edges):
        return None
    if units.size and max(abs(int(units.min())), abs(int(units.max()))) >= 1 << (
        63 - grid.fractional_bits
    ):
        return None
    if denominator > _INT64:
        return None
    inside = (units >= first) & (units <= last_units)
    quantised = (units << grid.fractional_bits) // denominator
    cell = np.searchsorted(np.array(edges, dtype=np.int64), quantised, side="right") - 1
    cell = np.minimum(cell, len(edges) - 2)
    return np.where(inside, cell, -1)
