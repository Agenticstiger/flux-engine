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
  the parent's rows, read at the child's row index;
* ``weighted`` (kernel 0.2.0): a Simulation's ``links`` entry names a number column of the
  parent, and the parent row is the first row ``i`` with ``u < T_i``, ``u`` one ``uniform53``
  drawn where the ``uniform`` pick draws, ``T_i = floor(2^53 * (w_0 + ... + w_i) / W)`` on
  exact rationals over the parent's final weights in row order (ADR 0023 section 4);
* ``counted`` (kernel 0.2.0): a Simulation's ``counts`` entry gives each parent a whole number of
  children, and child row ``i`` takes the parent whose run of consecutive child rows holds it
  (:mod:`flux_engine.generator.counts`, KERNEL-SPEC-2 section 8.1). The foreign key column draws
  nothing;
* ``history`` (kernel 0.2.0): the parent relationship of a history's emit (ADR 0017 section 3).
  Version row ``i`` belongs to entity ``e`` of the version layout, entity ``e`` to the parent row
  ``A.parent_of(e)`` of the entity layout, and the foreign key copies that parent row whole on
  every version of ``e``. It draws nothing. The natural entity's column is laid out over the
  entities and not over the rows (:class:`EntityColumn`).

* ``copied`` (kernel 0.2.0): a ``sharedColumns`` copy through another relationship ``R`` into this
  relationship's one foreign key column (ADR 0022 section 4 amendment of 2026-11-24). The parent row
  is the one the parent of ``R`` reaches through the relationship that holds the same column as its
  own foreign key, so the copied value is a key the target holds by construction. It draws nothing
  and costs two picks, with no index and no search over keys.

A foreign key column keeps its declared ``nullFraction`` when the relationship's
``joinType`` is ``left`` (FLUID's default): a null references no row. An
``inner`` relationship joins every row, so a ``nullFraction`` on its columns is
refused, and so is one on any key column.

Everything that cannot hold is refused before a row is drawn (``FLX-3403``).
"""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from math import lcm
from operator import itemgetter
from threading import local
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)
from weakref import WeakKeyDictionary

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.canonical import nfc, sha256_hex
from flux_engine.core.errors import FluxError, register
from flux_engine.core.lrm import largest_remainder
from flux_engine.core.permute import PERMUTE_MAX_DOMAIN, permute, permute2
from flux_engine.core.rng import DrawStream, bernoulli, bounded_uint, uniform53
from flux_engine.core.seedtree import SeedTreeRoot, digest_bytes, path_label
from flux_engine.expr2.errors import FLX_6106, FLX_6208, FLX_6210, Expr2Error
from flux_engine.generator import rows as _rows
from flux_engine.generator.counts import (
    CountedLayout,
    CountedPick,
    ExpectedCountHook,
    TooFewChildRows,
    TooManyChildRows,
    check_minimum,
    classes_of,
    count_key,
    counted_layout,
    expected_count_hooks,
)
from flux_engine.generator.families import ColumnType, Family
from flux_engine.generator.history import (
    Chain,
    DeferredBound,
    HistoryBinding,
    HistoryPick,
    HistoryPlan,
    OuterInputs,
    TrackedSpec,
    chain_columns,
    chain_unit_kind,
    check_layout,
    entity_chain,
    entity_states,
    instant_ms,
    open_end_unit,
    plan_history,
    tracked_levels_kept,
    tracked_spec,
    unit_of,
    version_valid_on,
)
from flux_engine.generator.parent_bounds import deferred_bound
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.reference import (
    ReferenceColumn,
    check_reference_exposes,
    is_reference_plan,
    reference_levels,
)
from flux_engine.generator.rules import (
    FORM_DEPENDENCY,
    ParentCondition,
    PickCopy,
    RulePlan,
    parent_condition,
)
from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL
from flux_engine.seam.contract import CompiledColumn, LogicalKind
from flux_engine.seam.counts import CountsEntry
from flux_engine.seam.keys import ContractKeys, ExposeKeys, Relationship
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY, SNAPSHOTS_KEY

__all__ = [
    "FLX_3403",
    "ChainColumn",
    "CompositeKey",
    "EntityColumn",
    "TrackedColumn",
    "ForeignKey",
    "IdentifierKey",
    "KeyGrid",
    "KeySpaceTooSmall",
    "TrackedLevels",
    "TrackedLevelsCut",
    "LinkedGiven",
    "ParentView",
    "ReferenceColumn",
    "RelationReader",
    "Relations",
    "check_links_resolved",
    "layout_key",
    "related_columns",
    "relate_plans",
]

FLX_3403 = register(
    "FLX-3403",
    "the related tables cannot be generated as declared",
    "FLUX generates the exposes of one contract together: every key the contract declares is laid "
    "out from the row index and never repeats, and every foreign key takes a key its parent's rows "
    "hold. A relationship whose target the Simulation does not generate from statistics, a key "
    "whose declared range or bins hold fewer values than its rows, a one_to_one child with more "
    "rows than its parent, a nullFraction on a key column or on a relationship whose joinType is "
    "inner, a key declared with a draw that would repeat values, column types that differ between "
    "a foreign key and its target, a key column nothing produces, and two relationships that set "
    "one column without one implying the other are refused before any row is drawn. So are the "
    "declarations that shape how children are placed: a counts entry whose minChildren the child "
    "rows or the shape cannot meet (too few child rows for the eligible parents, or above what the "
    "shape allows), whose parentWhen leaves no parent eligible or reads beyond the parent's own "
    "row, or sits on a relationship the layout cannot place (a key, a bridge side, an implied or "
    "weighted relationship); a required sum, min or max over the children with no ifNone where a "
    "parent can have no children, which would leave it NULL; a link condition on an inner "
    "relationship, on a key, or on a relationship another one implies; a bridge whose side is "
    "named twice, is one_to_one, carries a nullFraction or has more rows than the pairs of its "
    "parents; and a weight on a relationship that cannot pick parents by one, on a non-numeric "
    "parent column, or with negative or all-zero published values. A reference expose is refused "
    "when a published cell does not fit its column (a decimal with too many digits, a number "
    "outside its type's range). The message names the expose, the column or relationship, and the "
    "change to make (for a parentWhen with no eligible parent, the condition too). A history is "
    "refused when its emit has no validity window (one natural entity with a start and an end of "
    "one DATE or TIMESTAMP type), when its until, openEnd, chain or start bins leave no room "
    "inside timeBounds for an entity's versions, when a tracked column is not categorical, names "
    "an undeclared level or has no transition row for a level a chain can reach, when a tracked "
    "column that is a foreign key has a level that is no key its parent holds in the plan (a "
    "--scale below 1 cuts the parent's keys and leaves the levels), or when its parent is not a "
    "to-one relationship. A history's parent.lifetime is refused when the parent emit declares no "
    "lifetimes entry, or declares a history of its own that sets a lifetime column (a validity, "
    "activation, tracked, end or rule-set column), which then differs between a parent's versions; "
    "when the parent's lifetime leaves no room for one version of an entity (the message names "
    "`history(<emit>).parent.lifetime`, the entity count and the parents' lifetimes); or when the "
    "rows of the histories that keep to their parent's lifetime still move after one pass for "
    "every emit (a history under a history that holds fewer rows than it declares is laid out "
    "after it). An asOf entry is refused when its relationship does not reach the natural entity "
    "of an emit that declares a history, its date is not a DATE or TIMESTAMP column, or a rule "
    "sets a column the contract requires from a row read as of a date where its entity has no "
    "version, which would write NULL. A snapshots, events or periods entry is refused when its of "
    "names no emit with a history (or the emit itself), when a column it names is missing from the "
    "history's emit or differs in type, when the projected emit has rows of its own, when an "
    "events type has no reason text, when a before or after column maps a column the contract "
    "requires and the kept ops leave NULL, when a when reads a column the version does not hold, "
    "or reads the history's end column where the history does not carry it (a rule, link or the "
    "history sets it), or when a periods entry has no grain, no timeBounds, or more periods than a "
    "run holds, a `from` before `timeBounds.start`'s day, a `from` or `through` not before "
    "`timeBounds.end` or not a `YYYY-MM-DD` text, or a `through` before the `from`. A DATE or "
    "TIMESTAMP column of a history's emit is refused when it declares no range or bins, or a range "
    "or bins outside timeBounds, as is a relationship whose parent is a periodic emit. A copy "
    "through a sharedColumns entry into another relationship's foreign key is refused when the "
    "relationship it goes through reaches an emit that declares a history or is projected from "
    "one, whose rows hold a version's cells, not the pick the copy follows; the message names both "
    "relationships. An events or periods entry is also refused when a column of its emit is drawn "
    "given a column the entry sets, which the draw would read as the level it drew; the message "
    "names both columns. A DATE or TIMESTAMP column drawn from declared statistics is refused when "
    "its contract's `range` rule holds none of the dates it is drawn from, or would cut the bins "
    "of a column another column is drawn given; so is a column whose `range` rule is empty (its "
    "low end beyond its high end); the message names the column and the range.",
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

#: How many final rows of one emit a run keeps. A ``previous`` read goes back one version at a
#: time and a slice run in order reads rows near the last, so the oldest rows are the ones to drop.
_FINALS_KEPT = 1 << 12

#: An emit that fits in :data:`_FINALS_KEPT` rows is made whole, in one slice, once a process has
#: worked out one row in this many of it a row at a time (and at least :data:`_FILL_FLOOR` rows):
#: a slice of one row pays the column pass's fixed cost for a single row, so a process that reads
#: most of a small parent row by row pays it again and again. This is ski rental: the rows already
#: bought one by one cost about what the whole emit costs, so renting longer would only lose.
_FILL_SHARE = 16
_FILL_FLOOR = 32


class _Finals(OrderedDict[int, Tuple[Any, ...]]):
    """One emit's kept final rows, with what is needed to decide when to make it whole."""

    #: Rows worked out one at a time because they were not kept.
    singles: int = 0
    #: Set while the emit is being made whole, so a read inside that pass takes the one-row path.
    filling: bool = False


# The final rows of each emit a run has read, held by the run's root and gone with it. A reader is
# made for every slice and a row's final value is worked out by a slice of its own, so what a
# reader kept died with it and a ``previous`` read worked a chain out again for every row.
# Each thread keeps its own, so no lock is needed.
_FINALS_HERE = local()


def _finals_store() -> "WeakKeyDictionary[SeedTreeRoot, Dict[Tuple[str, str], Tuple[Any, Any]]]":
    found = getattr(_FINALS_HERE, "store", None)
    if found is None:
        found = WeakKeyDictionary()
        _FINALS_HERE.store = found
    return found


def _finals_of(root: SeedTreeRoot, plan: "_rows.GeneratorPlan") -> _Finals:
    """The final rows kept for *plan*'s emit under *root*: nothing another run or plan has seen."""
    store = _finals_store()
    by_emit = store.get(root)
    if by_emit is None:
        by_emit = {}
        store[root] = by_emit
    address = (plan.sim_id, plan.expose.expose_id)
    held = by_emit.get(address)
    if held is None or held[0] is not plan:
        held = (plan, _Finals())
        by_emit[address] = held
    return held[1]


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


class KeySpaceTooSmall(FluxError):
    """``FLX-3403`` for a key whose columns' declared domains hold fewer values than its rows.

    A class of its own so a plan under ``--scale`` can say how the scale moved the key's
    values against its rows (:func:`flux_engine.stages.scaling.key_space_note`).
    """


@dataclass(frozen=True)
class TrackedLevels:
    """A tracked foreign key's levels and the key of the drawn parent that has to hold them.

    ``parent`` and ``key`` are the parent's exposeId and key column, ``child`` and ``column`` the
    history's expose and its tracked column, ``levels`` the column's levels.
    """

    parent: str
    key: str
    child: str
    column: str
    levels: Tuple[str, ...]


class TrackedLevelsCut(FluxError):
    """``FLX-3403`` for a tracked foreign key whose level is no key its drawn parent holds in this plan.

    A class of its own so a plan under ``--scale`` below 1 can keep the parent's keys the level
    names (:func:`flux_engine.stages.scaling.floored_rows`) and plan again; the text is the
    refusal's, and ``tracked`` says what was asked of the parent.
    """

    def __init__(self, message: str, tracked: TrackedLevels) -> None:
        super().__init__(FLX_3403, message)
        self.tracked = tracked


# ---------------------------------------------------------------------------
# Grids: the integer units a value of an ordered kind is counted in
# ---------------------------------------------------------------------------


def _exponent(column: CompiledColumn) -> int:
    """A grid unit is ``10 ** exponent`` of the column's value: its scale for a DECIMAL."""
    if column.logical_type.kind is LogicalKind.DECIMAL:
        return -(column.logical_type.scale or 0)
    return 0


#: Array arithmetic on grid points stays exact while every number and product is under this.
_HALF_INT64 = 1 << 62

_DATE_KIND = LogicalKind.DATE.value
_DECIMAL_KIND = LogicalKind.DECIMAL.value


def _value_of_units(kind: str, exponent: int, units: int) -> Any:
    """The value a grid point stands for, in the class :mod:`flux_engine.seam.conform` renders."""
    if kind == _DATE_KIND:
        return date.fromordinal(units + DATE_EPOCH_ORDINAL)
    if kind == _DECIMAL_KIND:
        return _rows._decimal_from_units(units, exponent)
    return units  # INT, and TIMESTAMP in milliseconds since the epoch


def _values_of_units(kind: str, exponent: int, units: List[int]) -> List[Any]:
    """:func:`_value_of_units` of each of *units*: the one rule, applied to a column at once."""
    if kind == _DATE_KIND:
        return [date.fromordinal(unit + DATE_EPOCH_ORDINAL) for unit in units]
    if kind == _DECIMAL_KIND:
        build = _rows._decimal_from_units
        return [build(unit, exponent) for unit in units]
    return units  # INT, and TIMESTAMP in milliseconds since the epoch


def _units_of_value(kind: str, exponent: int, value: Any) -> int:
    """The grid point a drawn value lies on: the inverse of :func:`_value_of_units`."""
    if kind == LogicalKind.DATE.value:
        return int(value.toordinal() - DATE_EPOCH_ORDINAL)
    if kind == LogicalKind.DECIMAL.value:
        if isinstance(value, (Decimal, int)):
            # Exact integers all the way, with no Fraction built for each value.
            numerator, denominator = value.as_integer_ratio()
            if exponent <= 0:
                return (numerator * 10**-exponent) // denominator
            return numerator // (denominator * 10**exponent)
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
    # The bins as int64 arrays, built the first time a column is read at once (``False`` when
    # a bin does not fit one): a cache, in no comparison, hash or document.
    _arrays: Any = field(default=None, init=False, repr=False, compare=False, hash=False)

    def units_at(self, row: int) -> int:
        index = bisect_right(self.starts, row) - 1
        offset = row - self.starts[index]
        return self.firsts[index] + (offset * self.widths[index]) // self.counts[index]

    def value_at(self, row: int) -> Any:
        return _value_of_units(self.kind, self.exponent, self.units_at(row))

    def _bins(self) -> "Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]":
        """The bins' ``firsts``, ``widths``, ``counts`` and ``starts`` as int64 arrays, or ``None``.

        ``None`` when a number does not fit int64 or the product of a width and a count could
        overflow it, so the array arithmetic below is exactly the Python integer arithmetic of
        :meth:`units_at` and :meth:`row_near` whenever it runs.
        """
        found = self._arrays
        if found is None:
            found = False
            if (
                self.firsts
                and max(self.widths) * max(self.counts) < _HALF_INT64
                and max(self.firsts) + max(self.widths) < _HALF_INT64
                and min(self.firsts) > -_HALF_INT64
            ):
                try:
                    found = tuple(
                        np.array(column, dtype=np.int64)
                        for column in (self.firsts, self.widths, self.counts, self.starts)
                    )
                except OverflowError:
                    found = False
            object.__setattr__(self, "_arrays", found)
        return found or None

    def units_of_rows(self, rows: "np.ndarray") -> "Optional[np.ndarray]":
        """:meth:`units_at` of every row of an int64 array, or ``None`` for the per-row code.

        A column read at once: one ``searchsorted`` over the bins' first rows in place of one
        ``bisect_right`` per row, the same integer arithmetic on arrays.
        """
        bins = self._bins()
        if bins is None or rows.shape[0] == 0 or int(rows.min()) < 0:
            return None
        firsts, widths, counts, starts = bins
        index = np.searchsorted(starts, rows, side="right") - 1
        offset = rows - starts[index]
        if int(offset.max()) * int(widths.max()) >= _HALF_INT64:
            return None
        return firsts[index] + (offset * widths[index]) // counts[index]

    def values_at(self, rows: List[int]) -> Optional[List[Any]]:
        """:meth:`value_at` of each of *rows*, or ``None`` when the per-row code must."""
        if not rows or max(rows) >= kernel2.ARRAY_ROWS:
            return None
        units = self.units_of_rows(np.array(rows, dtype=np.int64))
        if units is None:
            return None
        return _values_of_units(self.kind, self.exponent, units.tolist())

    def rows_near(self, values: Sequence[Any]) -> "Optional[np.ndarray]":
        """:meth:`row_near` of each of *values* as an int64 array, or ``None`` for the per-row code.

        The grid point of each distinct value is worked out once, and the bin each point
        lies in, the row it rounds to and which neighbour is nearer are found for all of them
        by array arithmetic, in the integers :meth:`row_near` uses.
        """
        bins = self._bins()
        if bins is None or not values:
            return None
        points: Dict[Any, int] = {}
        for value in values:
            if value not in points:
                points[value] = _units_of_value(self.kind, self.exponent, value)
        try:
            units = np.array([points[value] for value in values], dtype=np.int64)
        except OverflowError:
            return None
        if int(units.max()) >= _HALF_INT64 or int(units.min()) <= -_HALF_INT64:
            return None
        firsts, widths, counts, starts = bins
        index = np.searchsorted(firsts, units, side="right") - 1
        before = index < 0
        index = np.maximum(index, 0)
        first, width, count, start = firsts[index], widths[index], counts[index], starts[index]
        offset = np.maximum(units - first, 0)
        inside = np.minimum(offset, width - 1)
        low = np.where(offset >= width, start + count - 1, start + (inside * count) // width)
        high = low + 1
        # The nearer of the two rows, the lower when they are as near; the last row has no upper.
        top = high >= self.rows
        above_row = np.where(top, low, high)
        below_gap = units - self._units_at_rows(low)
        above_gap = self._units_at_rows(above_row) - units
        near = np.where(top | (below_gap <= above_gap), low, high)
        return np.where(before, 0, near)

    def _units_at_rows(self, rows: "np.ndarray") -> "np.ndarray":
        """:meth:`units_at` of rows already known to be in range and the bins to fit."""
        firsts, widths, counts, starts = self._arrays
        index = np.searchsorted(starts, rows, side="right") - 1
        return firsts[index] + ((rows - starts[index]) * widths[index]) // counts[index]

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

    def holds(self, value: Any) -> bool:
        """Whether some row's key is *value*: the nearest row's key is the value itself."""
        try:
            units = _units_of_value(self.kind, self.exponent, value)
        except (ArithmeticError, AttributeError, TypeError, ValueError):
            return False
        return self.units_at(self.row_near(value)) == units

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

    ``pick`` is ``declared``, ``uniform``, ``distinct``, ``weighted`` or ``counted`` (see the module
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
    grid: Optional[Union[KeyGrid, ReferenceColumn]] = None
    key: Optional[bytes] = None
    first_null: Optional[Decimal] = None
    #: Kernel ``0.2.0``'s ``permute/2`` for a ``distinct`` pick (KERNEL-SPEC-2 section 8).
    counter_based: bool = False
    #: A ``weighted`` pick's weight column of the parent, and, when the parent is a reference
    #: expose, the published values (the parent's plan is read for any other parent).
    weight: Optional[str] = None
    weight_values: Optional[ReferenceColumn] = None
    #: A ``counted`` pick's declared shape and the child's rows; ``None`` for every other pick. On
    #: a ``history`` pick it is the entity layout's: the ``parent`` entry's keys and ``E`` for
    #: the rows the layout totals.
    counted: Optional[CountedPick] = None
    #: A ``history`` pick's chain; ``None`` for every other pick.
    history: Optional[HistoryPick] = None
    #: A ``copied`` pick's source; ``None`` for every other pick.
    copied: Optional[PickCopy] = None

    def target_of(self, column: str) -> str:
        return self.to_columns[self.from_columns.index(column)]

    def as_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {
            "relationship": self.relationship,
            "fromColumns": list(self.from_columns),
            "toColumns": list(self.to_columns),
            "pick": self.pick,
            "parent": self.parent.as_document(),
        }
        if self.weight is not None:
            document["weight"] = self.weight
        if self.counted is not None:
            document["counts"] = self.counted.as_document()
        if self.history is not None:
            document["history"] = self.history.as_document()
        if self.copied is not None:
            document["copiedThrough"] = {
                "relationship": self.copied.through,
                "source": self.copied.source,
            }
        return document


@dataclass(frozen=True)
class EntityColumn:
    """The natural entity's column of a history's emit: one value per entity, copied on every version.

    ``layout`` is the column's one-column layout (KERNEL-SPEC section 8.2) over the ``E``
    entities, the entity index standing for the row index, and ``history`` the chain that says
    which entity a version row belongs to (ADR 0017 section 3).
    """

    layout: Union[KeyGrid, IdentifierKey]
    history: HistoryPick

    def as_document(self) -> Dict[str, Any]:
        return {
            "layout": "entity",
            "entities": self.history.entities,
            "of": self.layout.as_document(),
            "history": self.history.as_document(),
        }


@dataclass(frozen=True)
class ChainColumn:
    """The validity start or end of a history's emit, or its activation: a version's place in its entity's chain.

    ``role`` is ``"start"``, ``"end"`` or ``"activation"``, and ``history`` the chain that says
    which entity and which version a row is (ADR 0017 sections 4 and 5). An open end is NULL, or
    the entry's ``openEnd``; the activation is the entity's first start on every version.
    """

    role: str
    history: HistoryPick

    def as_document(self) -> Dict[str, Any]:
        return {"layout": "chain", "role": self.role, "history": self.history.as_document()}

    def value_of(self, unit: Optional[int]) -> Any:
        """The cell for an instant in the chain's unit; ``None`` for no instant (a NULL end)."""
        return None if unit is None else _value_of_units(self.history.kind.value, 0, unit)

    def pick_of(self, chain: Chain, version: int) -> Optional[int]:
        """The instant this column takes at *version* of *chain*: the open end is ``openEnd`` or ``None``."""
        if self.role == "start":
            return chain.starts[version]
        if self.role == "activation":
            return chain.starts[0]
        end = chain.ends[version]
        return open_end_unit(self.history.entry, self.history.kind) if end is None else end


@dataclass(frozen=True)
class TrackedColumn:
    """A tracked column of a history's emit: its level on each version of an entity's chain.

    ``index`` is the column's place in the entry's ``tracked`` (the ``256 + c`` of its draws) and
    ``specs`` the initial levels and transition rows of every tracked column, which a chain's
    changes read together (ADR 0017 section 6). The specs follow from the entry and the columns'
    own statistics, so they are not part of the plan's document.
    """

    history: HistoryPick
    index: int
    specs: Tuple[TrackedSpec, ...]

    def as_document(self) -> Dict[str, Any]:
        return {
            "layout": "tracked",
            "index": self.index,
            "column": self.specs[self.index].column,
            "history": self.history.as_document(),
        }


Driver = Union[
    KeyGrid,
    IdentifierKey,
    CompositeMember,
    ForeignKey,
    ReferenceColumn,
    EntityColumn,
    ChainColumn,
    TrackedColumn,
]


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
class LinkedGiven:
    """A column drawn given a column of the row a link names (``given: <relationship>.<column>``).

    ``levels`` pairs each level the parent column draws with the value it renders to, so the
    level of the parent's final value is found by its value. The weight tables of the child
    column name exactly these levels (SEAM-FIDELITY-SPEC section 2).
    """

    column: str
    relationship: str
    parent: str
    parent_column: str
    levels: Tuple[Tuple[str, Any], ...]
    _index: Dict[Tuple[type, Any], str] = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_index", {(type(value), value): level for level, value in self.levels}
        )

    def level_of(self, value: Any) -> str:
        """The level *value* is: the parent's final value is one of the levels it draws."""
        found = self._index.get((type(value), value))
        if found is not None:
            return found
        raise _refuse(
            f"column {self.column!r} is drawn given {self.relationship}.{self.parent_column}, and "
            "the linked row holds a value that is none of the levels it draws"
        )

    def as_document(self) -> Dict[str, Any]:
        return {
            "given": {
                "relationship": self.relationship,
                "parent": self.parent,
                "column": self.parent_column,
                "levels": [level for level, _ in self.levels],
            }
        }


@dataclass(frozen=True)
class Relations:
    """How each related column of one expose is produced, by column name.

    ``givens`` holds each column drawn given a column of a linked row; empty for every plan
    that has none, and the document then has no entry for them.
    """

    drivers: Tuple[Tuple[str, Driver], ...]
    givens: Tuple[LinkedGiven, ...] = ()

    def driver(self, name: str) -> Optional[Driver]:
        for column, driver in self.drivers:
            if column == name:
                return driver
        return None

    def given_of(self, name: str) -> Optional[LinkedGiven]:
        """The linked given of column *name*, or ``None``."""
        for given in self.givens:
            if given.column == name:
                return given
        return None

    def as_document(self) -> Dict[str, Any]:
        document = {column: driver.as_document() for column, driver in self.drivers}
        for given in self.givens:
            document[given.column] = given.as_document()
        return document


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
        # A composite key's decoded point of each row, by expose and key columns.
        self._points: Dict[Tuple[str, str, Tuple[str, ...]], Dict[int, Tuple[int, ...]]] = {}
        # Filled by prefetch for one slice of a kernel 0.2.0 pass, row by row: a foreign
        # key's null and parent row, per relationship, and an identifier key's text.
        self._ready: Dict[Tuple[str, str, str], Dict[int, Tuple[bool, int]]] = {}
        # The parent row each relationship took for a row, None for a NULL key: a record of
        # what the picks drew, never a source (parent_row computes a row it has not seen).
        self._parents: Dict[Tuple[str, str, str], Dict[int, Optional[int]]] = {}
        self._texts: Dict["IdentifierKey", Dict[int, str]] = {}
        # A parent cell's value is a function of its view, column and row alone, and the
        # rows of a slice name the same parent rows many times over.
        self._views: Dict[Tuple[str, str, str], Dict[int, Any]] = {}
        # The plans the pass reads, by exposeId: a weighted pick sums the weights of its parent's
        # final rows, which only the parent's resolved plan can write (know).
        self._plans: Dict[str, "_rows.GeneratorPlan"] = {}
        # The counted layout of each counted relationship this reader has read (_layout).
        self._layouts: Dict[Tuple[str, str, str], CountedLayout] = {}
        # The layout of each history this reader has read: by expose and relationship, the
        # relationship being ``""`` for the chain alone (_chain_of, _history_of).
        self._histories: Dict[Tuple[str, str, str], HistoryPlan] = {}
        # The chains this reader has worked out, by expose and entity (_chain_cell).
        self._chains: Dict[Tuple[str, str, int], Chain] = {}
        # The tracked levels of the entities this reader has worked out, by expose and entity.
        self._states: Dict[Tuple[str, str, int], List[Tuple[str, ...]]] = {}

    def know(self, plan: "_rows.GeneratorPlan") -> None:
        """Make *plan* and every plan it reads known to the reader (a weighted pick reads them)."""
        expose_id = plan.expose.expose_id
        if expose_id in self._plans:
            return
        self._plans[expose_id] = plan
        for _, read in plan.reads:
            self.know(read)

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
        if isinstance(driver, (KeyGrid, ReferenceColumn)):
            return driver.value_at(row)
        if isinstance(driver, EntityColumn):
            return self._entity_value(
                owner, driver, self._chain_of(owner, driver.history).entity_of(row)
            )
        if isinstance(driver, ChainColumn):
            return self._chain_cell(owner, driver, row)
        if isinstance(driver, TrackedColumn):
            return self._tracked_cell(owner, column_plan, driver, row)
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
        parent_row = self._foreign_parent(owner, column_plan, driver, row, given_level)
        self._record(owner, driver.relationship, row, parent_row)
        if parent_row is None:
            return None
        return self.view_value(driver.parent, driver.target_of(name), parent_row)

    def _foreign_parent(
        self,
        owner: _Owner,
        column_plan: "_rows.ColumnPlan",
        driver: ForeignKey,
        row: int,
        given_level: Optional[str],
    ) -> Optional[int]:
        """The parent row a foreign key names at *row*, or ``None`` for a NULL key.

        The null draw first, then the pick: the draws :meth:`_cell` has always made, in the
        same order on the same streams.
        """
        if driver.pick == "copied":
            return _copied_parent(self, self._plans[owner.expose_id], driver, row)
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
            if stream is None:  # a declared pick has one column, so first is its name
                stream = self.streams(owner.sim_id, owner.expose_id).stream_at(row, first)
            drawn = _rows._draw_value(column_plan, stream, given_level)
            if is_null:
                return None
            return driver.grid.row_near(drawn)
        if is_null:
            return None
        return self._pick(owner, driver, row)

    def _record(
        self, owner: _Owner, relationship: str, row: int, parent_row: Optional[int]
    ) -> None:
        """Keep the parent row *relationship* named at *row* (``None`` for a NULL key)."""
        memo = self._parents.setdefault((owner.sim_id, owner.expose_id, relationship), {})
        if len(memo) >= _CACHE_LIMIT:
            memo.clear()
        memo[row] = parent_row

    def parent_row(
        self,
        plan: "_rows.GeneratorPlan",
        relationship: str,
        row: int,
        values: Optional[Sequence[Any]] = None,
    ) -> Optional[int]:
        """The row of *relationship*'s target the link names at *row* of *plan*'s expose.

        ``None`` when the link reaches no row. *values* is the row's values in
        ``plan.columns`` order: when one of the relationship's columns is ``None`` in it the
        link reaches no row, whatever the pick recorded, because the row's values win. The
        record is a cache and never a source: a row not recorded is computed with the draws
        :meth:`_cell` makes (ADR 0022 section 2).
        """
        self.know(plan)
        driver = _foreign_driver(plan, relationship)
        part = _composite_part(plan, relationship) if driver is None else None
        if driver is None and part is None:
            raise KeyError(
                f"relationship {relationship!r} has no foreign key or key part of its own in "
                f"{plan.expose.expose_id!r}"
            )
        if driver is not None:
            from_columns = driver.from_columns
        else:
            assert part is not None
            from_columns = part[2].from_columns
        names = [column_plan.column.name for column_plan in plan.columns]
        if values is not None and any(
            values[names.index(column)] is None for column in from_columns
        ):
            return None
        owner = owner_of(plan)
        if part is not None:
            composite, position, _ = part
            return self._point(owner, composite, row)[position]
        assert driver is not None
        key = (owner.sim_id, owner.expose_id, relationship)
        memo = self._parents.get(key)
        if memo is not None and row in memo:
            return memo[row]
        ready = self._ready.get(key, _NOTHING).get(row)
        if ready is not None:
            return None if ready[0] else ready[1]
        column_plan = plan.column_plan(driver.from_columns[0])
        parent_row: Optional[int]
        if driver.pick == "declared" and (
            column_plan.statistics.parents or driver.from_columns[0] in _rows._given_columns(plan)
        ):
            # The drawn level is another column's: the key's value is the parent's key at the
            # row the pick took, so the row is the key's nearest row.
            if values is None:
                raise ValueError(
                    f"parent_row: relationship {relationship!r} reads a level another column "
                    "draws, so it needs the row's values"
                )
            assert driver.grid is not None  # relate_plans sets it for this pick
            with localcontext(_rows._KERNEL_CONTEXT):
                parent_row = driver.grid.row_near(values[names.index(driver.from_columns[0])])
        else:
            with localcontext(_rows._KERNEL_CONTEXT):
                parent_row = self._foreign_parent(owner, column_plan, driver, row, None)
        self._record(owner, relationship, row, parent_row)
        return parent_row

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
        if driver.pick in ("counted", "history", "copied"):
            raise _has_no_draw(driver)
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
        self,
        owner: _Owner,
        driver: ForeignKey,
        column_plan: "_rows.ColumnPlan",
        drawn: Any,
        row: int,
    ) -> Any:
        """The value of a ``declared`` foreign key whose drawn level another column reads."""
        assert driver.grid is not None  # relate_plans allows only a declared pick here
        with localcontext(_rows._KERNEL_CONTEXT):
            parent_row = driver.grid.row_near(drawn)
            self._record(owner, driver.relationship, row, parent_row)
            return self.view_value(
                driver.parent, driver.target_of(column_plan.column.name), parent_row
            )

    def _pick(self, owner: _Owner, driver: ForeignKey, row: int) -> int:
        """The parent row a ``uniform``, ``distinct``, ``weighted``, ``counted`` or ``history`` relationship takes for *row*."""
        if driver.pick == "counted":  # a bisection over the layout: nothing to cache
            return self._layout(owner, driver).parent_of(row)
        if driver.pick == "history":  # two bisections: the row's entity, and its parent
            parent = self._history_of(owner, driver).parent_row_of(row)
            assert parent is not None  # a history pick has a parent layout
            return parent
        cache_key = (owner.sim_id, owner.expose_id, driver.relationship, row)
        found = self._picks.get(cache_key)
        if found is not None:
            return found
        if driver.pick == "distinct":
            assert driver.key is not None  # relate_plans sets it for this pick
            picked = _permute(driver.key, driver.parent.rows, row, driver.counter_based)
        elif driver.pick == "weighted":
            first = driver.from_columns[0]
            stream = self.streams(owner.sim_id, owner.expose_id).stream_at(row, first)
            if driver.first_null is not None:
                bernoulli(stream, driver.first_null)
            picked = bisect_right(self._thresholds(driver), uniform53(stream))
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

    def _layout(self, owner: _Owner, driver: ForeignKey) -> CountedLayout:
        """The counted layout of a ``counted`` relationship, worked out once per process.

        A pure function of the run root, the relationship's address, the child's rows ``N``, the
        parent's rows ``P`` and the declared shape (KERNEL-SPEC-2 section 8.1); a refusal
        (``FLX-3403``) is raised when it is first worked out.
        """
        assert driver.counted is not None  # relate_plans sets it for this pick
        address = (owner.sim_id, owner.expose_id, driver.relationship)
        known = self._layouts.get(address)
        if known is not None:
            return known
        root = self.root
        shape = driver.counted.shape()
        condition = driver.counted.parent_when
        hook = driver.counted.hook
        given = driver.counted.given
        parent = (
            None
            if condition is None and hook is None and given is None
            else self._parent_plan(owner, driver)
        )
        held = (
            root.digest,
            root.seed_tree_version,
            owner.sim_id,
            owner.expose_id,
            driver.relationship,
            owner.seam_digest,
            driver.parent.seam_digest,
            driver.counted.child_rows,
            driver.parent.rows,
            None if parent is None else parent.digest().value,
            driver.counted,
            # The hook itself, not its id: the key keeps it alive, so no later hook can take its id.
            hook,
        )
        found = _LAYOUTS.get(held)
        if found is None:
            eligible: Optional[List[int]] = None
            expected: Optional[List[Fraction]] = None
            groups: Optional[List[int]] = None
            if parent is not None:
                eligible, expected, groups = self._read_parent(
                    parent, condition, hook, driver.relationship, driver.counted
                )
            try:
                found = counted_layout(
                    count_key(root, owner.sim_id, owner.expose_id, driver.relationship),
                    driver.parent.rows,
                    driver.counted.child_rows,
                    shape=shape,
                    minimum=driver.counted.minimum or 0,
                    eligible=eligible,
                    expected=expected,
                    one_to_one=driver.counted.one_to_one,
                    where=(
                        f"expose {owner.expose_id!r} relationship {driver.relationship!r}"
                        + ("" if condition is None else f" with parentWhen {condition.when!r}")
                    ),
                    class_shapes=_class_shapes(driver.counted),
                    groups=groups,
                )
            except TooFewChildRows as short:
                short.child, short.parent = owner.expose_id, driver.parent.expose_id
                raise
            if len(_LAYOUTS) >= _LAYOUTS_KEPT:
                _LAYOUTS.clear()
            _LAYOUTS[held] = found
        self._layouts[address] = found
        return found

    def _chain_of(self, owner: _Owner, pick: HistoryPick) -> HistoryPlan:
        """The version layout of *owner*'s history, worked out once per process (ADR 0017 section 3).

        It reads no parent: which entity a version row belongs to does not depend on who the
        entity's parent is, so the natural entity's column needs nothing else.
        """
        if pick.parent_lifetime is not None:
            return self._history_of(owner, _bound_driver(self._plans.get(owner.expose_id), pick))
        address = (owner.sim_id, owner.expose_id, "")
        found = self._histories.get(address)
        if found is None:
            found = plan_history(
                self.root,
                owner.sim_id,
                owner.expose_id,
                pick.entry,
                kind=pick.kind,
                rows=pick.rows,
                entities=pick.entities,
                time_bounds=(pick.window_start,),
            )
            self._histories[address] = found
        return found

    def _history_of(self, owner: _Owner, driver: ForeignKey) -> HistoryPlan:
        """The two layouts of a ``history`` relationship: entities per parent, versions per entity.

        The entity layout reads the parent as a ``counts`` entry would, from the declared shape
        and, for a ``parentWhen`` or an expected-count hook, from the parent's final rows.
        """
        assert driver.history is not None and driver.counted is not None  # a history pick
        address = (owner.sim_id, owner.expose_id, driver.relationship)
        found = self._histories.get(address)
        if found is not None:
            return found
        counted = driver.counted
        condition, hook = counted.parent_when, counted.hook
        parent = None if condition is None and hook is None else self._parent_plan(owner, driver)
        eligible: Optional[List[int]] = None
        expected: Optional[List[Fraction]] = None
        if parent is not None:
            eligible, expected, _ = self._read_parent(parent, condition, hook, driver.relationship)
        found = plan_history(
            self.root,
            owner.sim_id,
            owner.expose_id,
            driver.history.entry,
            kind=driver.history.kind,
            rows=driver.history.laid_rows,
            entities=driver.history.entities,
            time_bounds=(driver.history.window_start,),
            outer=OuterInputs(
                relationship=driver.relationship,
                parents=driver.parent.rows,
                shape=counted.children,
                minimum=counted.minimum or 0,
                eligible=eligible,
                expected=expected,
                identity=(None if parent is None else parent.digest().value, hook),
            ),
            bound=_deferred_bound(self, owner, driver),
        )
        self._histories[address] = found
        return found

    def _entity_chain(
        self, owner: _Owner, pick: HistoryPick, history: HistoryPlan, entity: int
    ) -> Chain:
        """Entity *entity*'s chain, drawn once and kept."""
        address = (owner.sim_id, owner.expose_id, entity)
        chain = self._chains.get(address)
        if chain is None:
            chain = entity_chain(
                self.root, owner.sim_id, owner.expose_id, pick.entry, pick.kind, history, entity
            )
            if len(self._chains) >= _CACHE_LIMIT:
                self._chains.clear()
            self._chains[address] = chain
        return chain

    def _chain_cell(self, owner: _Owner, driver: ChainColumn, row: int) -> Any:
        """The start or end of version row *row*: its entity's chain, drawn once and kept."""
        history = self._chain_of(owner, driver.history)
        entity = history.entity_of(row)
        chain = self._entity_chain(owner, driver.history, history, entity)
        return driver.value_of(driver.pick_of(chain, history.version_of(row)))

    def asof_row(self, plan: "_rows.GeneratorPlan", row: int, moment: Any) -> Optional[int]:
        """The version row of *row*'s entity valid on *moment* (a DATE or TIMESTAMP cell), or ``None``.

        *plan* is the plan of the history's emit (ADR 0017 section 8). The version is found by
        bisection over the entity's starts, which its chain gives without reading the table.
        """
        pick = history_pick_of(plan)
        assert pick is not None, "an as-of link reaches a history's emit"
        owner = owner_of(plan)
        history = self._chain_of(owner, pick)
        entity = history.entity_of(row)
        chain = self._entity_chain(owner, pick, history, entity)
        version = version_valid_on(chain, pick.entry, pick.kind, instant_ms(moment))
        return None if version is None else int(history.versions.offsets[entity]) + version

    def _chain_values(self, owner: _Owner, driver: ChainColumn, rows: "np.ndarray") -> List[Any]:
        """:meth:`_chain_cell` of every row of a slice, by the arrays of the column path."""
        pick = driver.history
        starts, ends, opened = chain_columns(
            self.root,
            owner.sim_id,
            owner.expose_id,
            pick.entry,
            pick.kind,
            self._chain_of(owner, pick),
            rows,
        )
        if driver.role == "start":
            return [driver.value_of(unit) for unit in starts.tolist()]
        if driver.role == "activation":
            held = self._chain_of(owner, pick)
            firsts = held.first_starts[held.entities_of(rows)]
            return [driver.value_of(unit) for unit in firsts.tolist()]
        sentinel = open_end_unit(pick.entry, pick.kind)
        return [
            driver.value_of(sentinel if is_open else unit)
            for unit, is_open in zip(ends.tolist(), opened.tolist(), strict=True)
        ]

    def _tracked_cell(
        self, owner: _Owner, column_plan: "_rows.ColumnPlan", driver: TrackedColumn, row: int
    ) -> Any:
        """The level of version row *row*: its entity's tracked levels, drawn once and kept."""
        history = self._chain_of(owner, driver.history)
        entity = history.entity_of(row)
        address = (owner.sim_id, owner.expose_id, entity)
        states = self._states.get(address)
        if states is None:
            states = entity_states(
                self.root, owner.sim_id, owner.expose_id, driver.specs, history, entity
            )
            if len(self._states) >= _CACHE_LIMIT:
                self._states.clear()
            self._states[address] = states
        level = states[history.version_of(row)][driver.index]
        return _rows._level_to_value(level, column_plan.column)

    def _tracked_values(
        self,
        owner: _Owner,
        column_plan: "_rows.ColumnPlan",
        driver: TrackedColumn,
        rows: "np.ndarray",
    ) -> List[Any]:
        """:meth:`_tracked_cell` of every row of a slice, by the arrays of the column path."""
        levels = tracked_levels_kept(
            self.root,
            (owner.sim_id, owner.expose_id),
            driver.specs,
            self._chain_of(owner, driver.history),
            rows,
        )[driver.index]
        converted: Dict[str, Any] = {}
        column = column_plan.column
        for level in set(levels.tolist()):
            converted[level] = _rows._level_to_value(level, column)
        return [converted[level] for level in levels.tolist()]

    def first_row_of(self, owner: _Owner, pick: HistoryPick, row: int) -> int:
        """The first version row of the entity of *row*, the row a carried column is drawn on."""
        return self._chain_of(owner, pick).first_row_of(row)

    def first_rows_of(self, owner: _Owner, pick: HistoryPick, rows: "np.ndarray") -> "np.ndarray":
        """:meth:`first_row_of` for an array of rows."""
        return self._chain_of(owner, pick).first_rows_of(rows)

    def ending_rows(self, owner: _Owner, pick: HistoryPick, rows: "np.ndarray") -> "np.ndarray":
        """Which of *rows* are an ended entity's ending version, which takes the end value."""
        return self._chain_of(owner, pick).ending_rows(rows)

    def _entity_value(self, owner: _Owner, driver: EntityColumn, entity: int) -> Any:
        """The natural entity's value for *entity*: its layout at the entity index."""
        layout = driver.layout
        if isinstance(layout, IdentifierKey):
            text = self._texts.get(layout, _NOTHING).get(entity) if self._texts else None
            return text if text is not None else layout.value_at(entity)
        return layout.value_at(entity)

    def _parent_plan(self, owner: _Owner, driver: ForeignKey) -> "_rows.GeneratorPlan":
        """The parent's plan as the child's plan reads it, for a ``parentWhen`` or a hook (before its sums)."""
        child = self._plans.get(owner.expose_id)
        if child is not None:
            for expose_id, read in child.reads:
                if expose_id == driver.parent.expose_id:
                    return read
        raise _refuse(
            f"relationship {driver.relationship!r} counts the children of "
            f"{driver.parent.expose_id!r} by its final rows (a parentWhen or an expected count), "
            "whose plan this pass "
            "does not read"
        )

    def _read_parent(
        self,
        parent: "_rows.GeneratorPlan",
        condition: Optional[ParentCondition],
        hook: Optional[ExpectedCountHook],
        relationship: str,
        counted: Optional[CountedPick] = None,
    ) -> Tuple[Optional[List[int]], Optional[List[Fraction]], Optional[List[int]]]:
        """The eligible rows of *parent*, the hook's expected count of each, and their classes, in one pass.

        The rows on which *condition* is TRUE on the final row, ascending (``None`` without a
        condition: every row), with a *hook*, ``expected`` of each of them in rank order
        (``None`` without one), and, when *counted* has a ``given`` column, the index of the class
        of that column on each eligible row's final row, in rank order (``None`` without one).
        Worked out over every parent row, a chunk at a time, from the
        cells the parent's own pass writes (:func:`generate_row_slice`), as a weighted pick reads
        the parent's weights. A value that is not an exact, non-negative ``Fraction`` or ``int``
        is ``FLX-3403``, and so is a final value of the given column that is no class of it.
        """
        names = [column_plan.column.name for column_plan in parent.columns]
        classify = (
            None if counted is None or counted.given is None else _given_classifier(parent, counted)
        )
        position = -1 if classify is None or counted is None else names.index(str(counted.given))
        found: List[int] = []
        values: List[Fraction] = []
        classes: List[int] = []
        for row, final_row in self._eligible(parent, condition, names):
            found.append(row)
            if classify is not None:
                classes.append(classify(final_row[position], row))
            if hook is not None:
                values.append(
                    _expected_of(hook.expected(parent, row, final_row, relationship), row)
                )
        return (
            None if condition is None else found,
            None if hook is None else values,
            None if classify is None else classes,
        )

    def _eligible(
        self,
        parent: "_rows.GeneratorPlan",
        condition: Optional[ParentCondition],
        names: Sequence[str],
    ) -> Iterator[Tuple[int, Tuple[Any, ...]]]:
        """The rows of *parent* on which *condition* is TRUE on the final row, a chunk at a time."""
        for start in range(0, parent.rows, _WEIGHT_CHUNK):
            stop = min(start + _WEIGHT_CHUNK, parent.rows)
            chunk = _rows.generate_row_slice(parent, start, stop, root=self.root)
            holds = condition.holds(names, chunk, start) if condition is not None else None
            yield from _eligible_rows(chunk, start, holds)

    def children_of(
        self, child_plan: "_rows.GeneratorPlan", relationship: str, parent_row: int
    ) -> range:
        """The child rows of *child_plan* that name parent row *parent_row* through *relationship*.

        The relationship's counted layout, worked out through the child plan's own foreign key
        exactly as the child's pass does (ADR 0016 section 2), so a summary over the children and
        the child's own rows see one layout.
        """
        self.know(child_plan)
        driver = _foreign_driver(child_plan, relationship)
        if driver is None or driver.pick != "counted":
            raise _refuse(
                f"relationship {relationship!r} of {child_plan.expose.expose_id!r} is not on the "
                "counted layout, so its parent's rows have no run of children"
            )
        return self._layout(owner_of(child_plan), driver).children_of(parent_row)

    def _thresholds(self, driver: ForeignKey) -> Tuple[int, ...]:
        """The ``T_i`` of a weighted pick, worked out once per process (ADR 0023 section 4).

        A pure function of the parent's final weights, which are its published values for a
        reference expose and, for any other, the cells its own pass writes (:func:`generate_row_slice`
        over the plan the child reads, a chunk at a time). A negative weight, or weights that
        total zero, is ``FLX-3403``.
        """
        assert driver.weight is not None  # relate_plans sets it for this pick
        if driver.weight_values is not None:
            reference = driver.weight_values
            held = ("reference", reference.digest, reference.column)
            found = _THRESHOLDS.get(held)
            if found is None:
                found = weight_thresholds(
                    reference.values, f"reference column {reference.column!r}"
                )
                _remember(held, found)
            return found
        parent = self._plans.get(driver.parent.expose_id)
        if parent is None:
            raise _refuse(
                f"relationship {driver.relationship!r} picks parents by the weights of "
                f"{driver.parent.expose_id!r}, whose plan this pass does not read"
            )
        root = self.root
        key = (
            root.digest,
            root.seed_tree_version,
            parent.sim_id,
            parent.expose.expose_id,
            parent.digest().value,
            driver.weight,
        )
        found = _THRESHOLDS.get(key)
        if found is None:
            position = [column_plan.column.name for column_plan in parent.columns].index(
                driver.weight
            )
            weights: List[Any] = []
            for start in range(0, parent.rows, _WEIGHT_CHUNK):
                stop = min(start + _WEIGHT_CHUNK, parent.rows)
                weights.extend(
                    row[position]
                    for row in _rows.generate_row_slice(parent, start, stop, root=root)
                )
            found = weight_thresholds(
                weights, f"column {driver.weight!r} of {parent.expose.expose_id!r}"
            )
            _remember(key, found)
        return found

    def _point(self, owner: _Owner, composite: CompositeKey, row: int) -> Tuple[int, ...]:
        """Each part's index in the point *composite* decoded to at *row*, cached."""
        points = self._points.setdefault((owner.sim_id, owner.expose_id, composite.columns), {})
        indexes = points.get(row)
        if indexes is None:
            indexes = composite.indexes_at(row)
            if len(points) >= _CACHE_LIMIT:
                points.clear()
            points[row] = indexes
        return indexes

    def _member(self, owner: _Owner, composite: CompositeKey, column: str, row: int) -> Any:
        indexes = self._point(owner, composite, row)
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

    def final_row(self, plan: "_rows.GeneratorPlan", row: int) -> Tuple[Any, ...]:
        """Row *row* of *plan*'s expose exactly as the expose's own pass writes it.

        Its cells, its dependencies and rules, and a column drawn ``given`` another: the
        function the expose's own pass runs, over a slice of one row (ADR 0022 section 3).
        A pure function of the plan, the root and the row, so a kept row is never stale.
        """
        memo = _finals_of(self.root, plan)
        found = memo.get(row)
        if found is None:
            memo.singles += 1
            if (
                not memo.filling
                and plan.rows <= _FINALS_KEPT
                and memo.singles >= max(_FILL_FLOOR, plan.rows // _FILL_SHARE)
                and self._keep_whole(plan, memo)
            ):
                return memo[row]
            found = _rows.generate_row_slice(plan, row, row + 1, root=self.root)[0]
            memo[row] = found
            if len(memo) > _FINALS_KEPT:
                memo.popitem(last=False)
        else:
            memo.move_to_end(row)
        return found

    def _keep_whole(self, plan: "_rows.GeneratorPlan", memo: _Finals) -> bool:
        """Make every row of *plan* in one slice and keep them; ``False`` leaves *memo* as it was.

        Row ``i`` of a slice is row ``i`` of any other (SEAM-FIDELITY-SPEC section 1), so these
        are the rows a slice of one row each would have made. A refusal on a row nothing has read
        is left to the one-row path, which raises it only for a row that is read; so is any other
        failure of the whole pass (a recursion limit, say), which the one-row path may not meet.
        """
        memo.filling = True
        try:
            whole = _rows.generate_row_slice(plan, 0, plan.rows, root=self.root)
        except Exception:
            memo.singles = -(1 << 62)  # do not try again
            return False
        finally:
            memo.filling = False
        for index, made in enumerate(whole):
            memo[index] = made
        return True

    def linked_level(self, plan: "_rows.GeneratorPlan", given: LinkedGiven, row: int) -> str:
        """The level the row's link reaches: that of the parent's final value (ADR 0023 section 3).

        The parent row is the one the relationship names at *row* (:meth:`parent_row`), and the
        value is what the parent's own pass writes there (:meth:`final_row`), read through the
        plan the child carries in ``reads``. The cell then draws on its own path at the index it
        always takes: only its weight table changes.
        """
        parent_row = self.parent_row(plan, given.relationship, row)
        if (
            parent_row is None
        ):  # pragma: no cover - relate_plans refuses a link that may reach no row
            raise _refuse(
                f"column {given.column!r} is drawn given {given.relationship}.{given.parent_column}, "
                "and the link reaches no row"
            )
        parent_plan = dict(plan.reads)[given.parent]
        names = [column_plan.column.name for column_plan in parent_plan.columns]
        value = self.final_row(parent_plan, parent_row)[names.index(given.parent_column)]
        return given.level_of(value)

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

    def view_values(self, view: ParentView, column: str, rows: List[int]) -> List[Any]:
        """:meth:`view_value` of each of *rows*: each distinct row's value worked out once.

        The rows of a slice name the same parent rows many times over, and the kinds of column
        a view reads are mostly arithmetic on the row, so the distinct rows not yet held are
        worked out together (:meth:`_view_batch`) and then gathered to every row that names
        them. Called with the kernel's ``decimal`` context held, as :meth:`column_values` is.
        """
        if not rows:
            return []
        memo = self._views.setdefault((view.sim_id, view.expose_id, column), {})
        got: Dict[int, Any] = {}
        todo: List[int] = []
        distinct = dict.fromkeys(rows)
        if memo:
            for row in distinct:
                if row in memo:
                    got[row] = memo[row]
                else:
                    todo.append(row)
        else:
            todo = list(distinct)
        if todo:
            values = self._view_batch(view, column, todo)
            got.update(zip(todo, values, strict=True))
            if len(memo) + len(todo) >= _CACHE_LIMIT:
                memo.clear()
            if len(todo) < _CACHE_LIMIT:
                memo.update(zip(todo, values, strict=True))
        return list(map(got.__getitem__, rows))

    def _view_batch(self, view: ParentView, column: str, rows: List[int]) -> List[Any]:
        """:meth:`_view_value` of each of *rows*, a column at a time where the driver has a way."""
        driver = view.driver(column)
        if isinstance(driver, KeyGrid):
            found = driver.values_at(rows)
            if found is not None:
                return found
        elif isinstance(driver, ReferenceColumn):
            return list(map(driver.values.__getitem__, rows))
        elif isinstance(driver, IdentifierKey):
            return self._identifier_values(driver, rows)
        elif isinstance(driver, CompositeMember):
            owner = _Owner(view.sim_id, view.expose_id, view.seam_digest)
            return self._member_values(owner, driver.composite, column, rows)
        elif isinstance(driver, ForeignKey):
            owner = _Owner(view.sim_id, view.expose_id, view.seam_digest)
            return self._foreign_values(owner, view.column_plan(column), driver, rows)
        return [self._view_value(view, column, row) for row in rows]

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
        if isinstance(driver, (KeyGrid, ReferenceColumn)):
            return [driver.value_at(row) for row in row_list]
        if isinstance(driver, EntityColumn):
            chain = self._chain_of(owner, driver.history)
            return [
                self._entity_value(owner, driver, entity)
                for entity in chain.entities_of(rows).tolist()
            ]
        if isinstance(driver, ChainColumn):
            return self._chain_values(owner, driver, rows)
        if isinstance(driver, TrackedColumn):
            return self._tracked_values(owner, column_plan, driver, rows)
        if isinstance(driver, IdentifierKey):
            return self._identifier_values(driver, row_list)
        if isinstance(driver, CompositeMember):
            return self._member_values(owner, driver.composite, name, row_list)
        return self._foreign_values(owner, column_plan, driver, row_list)

    def _identifier_values(self, driver: IdentifierKey, rows: List[int]) -> List[Any]:
        texts = self._texts.get(driver, _NOTHING)
        found = list(map(texts.get, rows))
        if None in found:
            return [
                driver.value_at(row) if text is None else text
                for row, text in zip(rows, found, strict=True)
            ]
        return found

    def _foreign_values(
        self,
        owner: _Owner,
        column_plan: "_rows.ColumnPlan",
        driver: Any,
        rows: List[int],
    ) -> List[Any]:
        target = driver.target_of(column_plan.column.name)
        ready = self._ready.get((owner.sim_id, owner.expose_id, driver.relationship), _NOTHING)
        entries = list(map(ready.get, rows))
        if rows and None not in entries:
            # Every row's draws are held: no row takes the per-row code.
            nulls, taken = zip(*entries, strict=True)
            if not any(nulls):
                return self.view_values(driver.parent, target, list(taken))
        values: List[Any] = [None] * len(rows)
        slots: List[int] = []
        parent_rows: List[int] = []
        for slot, row in enumerate(rows):
            found = ready.get(row)
            if found is None:
                values[slot] = self._cell(owner, column_plan, driver, row, None)
            elif not found[0]:
                slots.append(slot)
                parent_rows.append(found[1])
        # Each distinct parent row's value is worked out once, then gathered to the rows.
        for slot, value in zip(
            slots, self.view_values(driver.parent, target, parent_rows), strict=True
        ):
            values[slot] = value
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
        points = self._points.get((owner.sim_id, owner.expose_id, composite.columns), _NOTHING)
        decoded: List[Any] = list(map(points.get, rows))
        if None in decoded:
            decoded = [
                composite.indexes_at(row) if point is None else point
                for row, point in zip(rows, decoded, strict=True)
            ]
        indexes = list(map(itemgetter(position), decoded))
        if isinstance(part, ParentPart):
            target = part.to_columns[part.from_columns.index(column)]
            return self.view_values(part.parent, target, indexes)
        # A part has few indexes, so each distinct one is converted once.
        distinct = list(set(indexes))
        if isinstance(part, GridPart):
            converted = _values_of_units(
                part.kind, part.exponent, [part.first + index for index in distinct]
            )
        else:
            converted = [_rows._level_to_value(part.levels[i], part.column) for i in distinct]
        made = dict(zip(distinct, converted, strict=True))
        return list(map(made.__getitem__, indexes))

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
        if isinstance(driver, (KeyGrid, ReferenceColumn)):
            return  # a grid key's value is arithmetic on its row, a published one is read off
        if isinstance(driver, (ChainColumn, TrackedColumn)):
            return  # worked out whole by column_values, or per entity by cell
        if isinstance(driver, EntityColumn):
            if isinstance(driver.layout, IdentifierKey) and rows.shape[0]:
                chain = self._chain_of(owner, driver.history)
                # The texts are the entities', so each entity is worked out once, whatever its
                # number of versions among the rows.
                self._prefetch_identifier(driver.layout, np.unique(chain.entities_of(rows)))
            return
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
        texts.update(zip(rows.tolist(), map(driver.text_of, images), strict=True))

    def _prefetch_composite(
        self, owner: _Owner, composite: CompositeKey, rows: "np.ndarray"
    ) -> None:
        if not composite.counter_based or composite.space > kernel2.PERMUTE_ARRAY_MAX_DOMAIN:
            return
        # Every member column of the key asks for it; the rows already decoded are skipped.
        points = self._points.setdefault((owner.sim_id, owner.expose_id, composite.columns), {})
        fresh = np.asarray(_missing(rows, points), dtype=np.int64)
        if fresh.shape[0] == 0:
            return
        images = kernel2.permute2_array(composite.key, composite.space, fresh)
        decoded = composite.indexes_of_points(images)
        points.update(zip(fresh.tolist(), decoded, strict=True))
        for position, part in enumerate(composite.parts):
            if isinstance(part, ParentPart):
                parent_rows = _row_array(set(map(itemgetter(position), decoded)))
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
        if driver.pick in ("weighted", "copied"):
            return  # these read other rows' picks or final rows: the per-row code takes them
        ready = self._ready.setdefault((owner.sim_id, owner.expose_id, driver.relationship), {})
        rows = _missing(rows, ready)
        if rows.shape[0] == 0:
            return
        key = self.streams(owner.sim_id, owner.expose_id).column_key(driver.from_columns[0])
        draw = 0
        nulls = np.zeros(rows.shape[0], dtype=np.bool_)
        if driver.first_null is not None:
            nulls = np.asarray(
                kernel2.column_bernoulli(kernel2.column_uniform53(key, rows, 0), driver.first_null),
                dtype=np.bool_,
            )
            draw = 1
        parents = self._parents_of_rows(owner, column_plan, driver, (key, draw), rows)
        if parents is None:
            return
        try:
            taken = np.asarray(parents, dtype=np.int64)
        except OverflowError:
            # Parent rows past what an array holds: kept as they are, and no view is prefetched.
            kept = zip(nulls.tolist(), map(int, parents), strict=True)
            ready.update(zip(rows.tolist(), kept, strict=True))
            return
        # One C-level pass in place of a tuple and a store for each row.
        ready.update(
            zip(rows.tolist(), zip(nulls.tolist(), taken.tolist(), strict=True), strict=True)
        )
        parent_rows = np.unique(taken[~nulls])
        for target in driver.to_columns:
            self._prefetch_view(driver.parent, target, parent_rows)

    def _parents_of_rows(
        self,
        owner: _Owner,
        column_plan: "_rows.ColumnPlan",
        driver: ForeignKey,
        stream: Tuple[kernel2.Key, int],
        rows: "np.ndarray",
    ) -> "Union[List[int], np.ndarray, None]":
        """The parent row each of *rows* takes, or ``None`` when the per-row code must.

        *stream* is the first column's key and the draw index its pick is drawn at. A ``counted``
        pick draws nothing: the rows' parents are searched in the layout's offsets at once, and a
        ``history`` pick's the same through its entity layout.
        """
        key, draw = stream
        if driver.pick == "counted":
            return self._layout(owner, driver).parents_of(rows)
        if driver.pick == "history":
            return self._history_of(owner, driver).parent_rows_of(rows)
        if driver.pick == "declared":
            from flux_engine.generator import columns as _columns

            assert driver.grid is not None  # relate_plans sets it for this pick
            drawn = _columns._draws(column_plan, key, rows, draw)
            if drawn is None:
                return None
            if isinstance(driver.grid, KeyGrid):
                near = driver.grid.rows_near(drawn)
                if near is not None:
                    return near
            return [driver.grid.row_near(value) for value in drawn]
        if driver.pick == "distinct":
            assert driver.key is not None  # relate_plans sets it for this pick
            if not driver.counter_based or driver.parent.rows > kernel2.PERMUTE_ARRAY_MAX_DOMAIN:
                return None
            return kernel2.permute2_array(driver.key, driver.parent.rows, rows)
        bounded: List[int] = kernel2.column_bounded(key, rows, draw, driver.parent.rows).tolist()
        return bounded

    def _prefetch_view(self, view: ParentView, column: str, rows: "np.ndarray") -> None:
        driver = view.driver(column)
        if driver is not None and rows.shape[0]:
            owner = _Owner(view.sim_id, view.expose_id, view.seam_digest)
            self._prefetch_driver(owner, view.column_plan(column), driver, rows)


#: How many parent rows are written at once when the weights of a parent are read.
_WEIGHT_CHUNK = 4096
#: The thresholds of each weighted pick this process has worked out, and how many it keeps.
_THRESHOLDS: Dict[Tuple[Any, ...], Tuple[int, ...]] = {}
_THRESHOLDS_KEPT = 16
#: The counted layouts this process has worked out, and how many it keeps (each holds two arrays
#: over the parent's rows).
_LAYOUTS: Dict[Tuple[Any, ...], CountedLayout] = {}
_LAYOUTS_KEPT = 4


def _remember(key: Tuple[Any, ...], thresholds: Tuple[int, ...]) -> None:
    if len(_THRESHOLDS) >= _THRESHOLDS_KEPT:
        _THRESHOLDS.clear()
    _THRESHOLDS[key] = thresholds


def _expected_of(value: Any, row: int) -> Fraction:
    """One hook value as the exact rational it is; anything else, or a negative one, is ``FLX-3403``."""
    if isinstance(value, bool) or not isinstance(value, (Fraction, int)):
        raise _refuse(
            f"an expected-count hook gave {value!r} for parent row {row}; it gives an exact "
            "Fraction or int"
        )
    if value < 0:
        raise _refuse(f"an expected-count hook gave {value} for parent row {row}, below zero")
    return Fraction(value)


def _weight_of(value: Any, where: str) -> Fraction:
    """One weight as the exact rational it is: NULL weighs zero; a negative one is ``FLX-3403``."""
    if value is None:
        return Fraction(0)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise _refuse(f"{where} holds {value!r}, which is no number")
    try:
        weight = Fraction(value)
    except (ValueError, OverflowError):
        raise _refuse(f"{where} holds {value!r}, which is no finite number") from None
    if weight < 0:
        raise _refuse(f"{where} holds a negative weight ({value}); a weight is zero or more")
    return weight


def weight_thresholds(values: Sequence[Any], where: str) -> Tuple[int, ...]:
    """``T_i = floor(2^53 * (w_0 + ... + w_i) / W)`` for the *values* taken as weights (ADR 0023 section 4).

    Exact: the weights are brought to one denominator and summed as integers. ``FLX-3403`` for a
    negative weight, a value that is no number, or weights that total zero.
    """
    weights = [_weight_of(value, where) for value in values]
    denominator = lcm(*(weight.denominator for weight in weights)) if weights else 1
    scaled = [weight.numerator * (denominator // weight.denominator) for weight in weights]
    total = sum(scaled)
    if total <= 0:
        raise _refuse(
            f"{where} weighs nothing: every weight is zero or NULL, so no parent can be picked"
        )
    thresholds: List[int] = []
    running = 0
    for weight in scaled:
        running += weight
        thresholds.append((running << 53) // total)
    return tuple(thresholds)


def _foreign_driver(plan: "_rows.GeneratorPlan", relationship: str) -> Optional[ForeignKey]:
    """The :class:`ForeignKey` of *relationship* in *plan*'s relations, if it has one."""
    if plan.relations is not None:
        for _, driver in plan.relations.drivers:
            if isinstance(driver, ForeignKey) and driver.relationship == relationship:
                return driver
    return None


def _bound_driver(plan: Optional["_rows.GeneratorPlan"], pick: HistoryPick) -> ForeignKey:
    """The ``parent`` relationship's driver of a history that keeps to its parent's lifetime.

    Such a history's layout reads the parents, so it is :meth:`RelationReader._history_of`'s.
    """
    assert pick.entry.parent is not None
    driver = None if plan is None else _foreign_driver(plan, pick.entry.parent.relationship)
    if driver is None:
        raise _refuse(
            f"history ({pick.entry.emit}) keeps to its parent's lifetime, whose plan this pass "
            "does not read"
        )
    return driver


def _deferred_bound(
    reader: "RelationReader", owner: _Owner, driver: ForeignKey
) -> Optional[DeferredBound]:
    """The lifetimes of *driver*'s parent rows when its history keeps to them (ADR 0017), else ``None``."""
    history = driver.history
    if history is None or history.parent_lifetime is None:
        return None
    parent = reader._parent_plan(owner, driver)
    return deferred_bound(reader.root, parent, history.parent_lifetime, history.kind)


def _composite_part(
    plan: "_rows.GeneratorPlan", relationship: str
) -> Optional[Tuple[CompositeKey, int, ParentPart]]:
    """The several-column key that absorbs *relationship*, the position of its part, and the part."""
    if plan.relations is not None:
        for _, driver in plan.relations.drivers:
            if not isinstance(driver, CompositeMember):
                continue
            for position, part in enumerate(driver.composite.parts):
                if isinstance(part, ParentPart) and part.relationship == relationship:
                    return driver.composite, position, part
    return None


def history_pick_of(plan: "_rows.GeneratorPlan") -> Optional[HistoryPick]:
    """The history *plan*'s emit is laid out by, or ``None`` for an emit with none."""
    if plan.relations is None:
        return None
    for _name, driver in plan.relations.drivers:
        if isinstance(driver, (EntityColumn, ChainColumn, TrackedColumn)):
            return driver.history
    return None


def bound_history_of(plan: "_rows.GeneratorPlan", root: SeedTreeRoot) -> HistoryPlan:
    """The layout of *plan*'s history emit, which keeps to its parent's lifetime, for a reader with no reader.

    A projection reads the chain of its history without a pass of its own, so the layout that reads
    the parents' lifetimes is worked out here, on a reader of its own; the plans and the parents'
    lifetimes are kept by their digests, so the work is done once per process.
    """
    pick = history_pick_of(plan)
    assert pick is not None and pick.parent_lifetime is not None
    reader = RelationReader(root)
    reader.know(plan)
    return reader._chain_of(owner_of(plan), pick)


def carried_columns(plan: "_rows.GeneratorPlan") -> FrozenSet[str]:
    """The columns of a history's emit that take, on every version, the first version row's cell.

    Every column the history does not set (the keys, the parent's foreign key, the validity
    columns, the activation and the tracked columns are its own) and that no rule or link sets
    (ADR 0017 section 6). A foreign key other than the parent's is one of them: it is picked on
    the first version row. Empty for an emit with no history.
    """
    if history_pick_of(plan) is None or plan.relations is None:
        return frozenset()
    ruled: FrozenSet[str] = frozenset()
    if plan.rules is not None:
        ruled = plan.rules.determined | frozenset(
            column for link in plan.rules.links for column in link.columns
        )
    carried = set()
    for column_plan in plan.columns:
        name = column_plan.column.name
        driver = plan.relations.driver(name)
        free = driver is None or (isinstance(driver, ForeignKey) and driver.pick != "history")
        if free and name not in ruled:
            carried.add(name)
    return frozenset(carried)


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
class _HistoryShape:
    """One history's emit as its relational shape reads it (ADR 0017 section 3).

    ``pick`` is what the plan carries, ``natural`` the natural entity's column, ``parent`` the
    ``parent`` entry read as a ``counts`` entry, and ``hook`` the member and hook an expected-count
    hook bound for the parent relationship, if any.
    """

    pick: HistoryPick
    natural: str
    parent: Optional[CountsEntry]
    hook: Optional[Tuple[ExpectedCountHook, Mapping[str, Any]]] = None
    start: str = ""
    end: Optional[str] = None
    #: Whether a tracked foreign key's levels are checked against its parent's keys.
    tracked_levels: bool = True

    @property
    def activation(self) -> Optional[str]:
        """The column that holds an entity's first start on every version, if declared."""
        return self.pick.entry.activation

    @property
    def tracked(self) -> Tuple[str, ...]:
        """The tracked columns, in the entry's order."""
        return tuple(tracked.column for tracked in self.pick.entry.tracked)


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
    #: The several-column keys a bridge declares (ADR 0022 section 8), each with its two
    #: relationships: the key is laid out as the pair's point, under the label ``pair``.
    bridges: Mapping[Tuple[str, ...], Tuple[str, str]] = field(default_factory=dict)
    #: The weight column each weighted relationship names (ADR 0023 section 4).
    weights: Mapping[str, str] = field(default_factory=dict)
    #: The counts entry of each relationship on the counted layout (ADR 0016 section 1).
    counts: Mapping[str, CountsEntry] = field(default_factory=dict)
    #: The checked ``parentWhen`` of each counted relationship that declares one.
    conditions: Mapping[str, ParentCondition] = field(default_factory=dict)
    #: The member a hook bound for each relationship, with the hook (ADR 0016 section 3).
    hooked: Mapping[str, Tuple[ExpectedCountHook, Mapping[str, Any]]] = field(default_factory=dict)
    #: The emit's history, when the Simulation declares one (ADR 0017).
    history: Optional[_HistoryShape] = None
    #: The copy that sets each relationship's pick, by relationship (ADR 0022 section 4).
    copied: Mapping[str, PickCopy] = field(default_factory=dict)

    @property
    def columns(self) -> Tuple[str, ...]:
        """Every column the shape decides, sorted."""
        named = set(self.layouts) | set(self.members)
        if self.history is not None:
            named.add(self.history.start)
            if self.history.end is not None:
                named.add(self.history.end)
            if self.history.activation is not None:
                named.add(self.history.activation)
            named.update(self.history.tracked)
        named.update(
            column
            for column, relationship in self.owners.items()
            if relationship.name not in self.absorbed
        )
        return tuple(sorted(named))


def relate_plans(
    plans: Mapping[str, "_rows.GeneratorPlan"],
    keys: ContractKeys,
    bridges: Optional[Mapping[str, Sequence[Tuple[str, str]]]] = None,
    weights: Optional[Mapping[str, Mapping[str, str]]] = None,
    counts: Optional[Mapping[str, Mapping[str, CountsEntry]]] = None,
    contract: Optional[Mapping[str, Any]] = None,
    namespace: Optional[Mapping[str, Any]] = None,
    histories: Optional[Mapping[str, HistoryBinding]] = None,
) -> Dict[str, "_rows.GeneratorPlan"]:
    """The plans of one contract's generator-driven exposes, with their relations.

    *plans* maps each exposeId the Simulation generates from statistics to its
    plan. An expose for which the contract declares no key and no relationship
    comes back unchanged, so its ``generatorDigest`` does not move. Every
    refusal is ``FLX-3403``, raised before a row exists.

    *bridges* maps an exposeId to the pairs of relationships its Simulation declares as bridge
    tables (ADR 0022 section 8): each pair is laid out together as one key of two parts, so no
    pair repeats. Kernel 0.1.0 refuses a bridge (``FLX-3105``).

    *weights* maps an exposeId to the weight column each relationship it configures names, by
    relationship (a ``links`` entry's ``weight``, ADR 0023 section 4): the relationship's parent
    row is then picked by the weights of the parent's final rows. Refused (``FLX-3403``) when the
    column is no number column of the target, on a ``distinct`` relationship, a ``declared`` pick,
    a bridge side or a reference expose's relationship, and, at plan time for a reference parent,
    when a published weight is negative or they total zero. Kernel 0.1.0 refuses it (``FLX-3105``).

    *counts* maps an exposeId to the ``counts`` entry of each relationship it lays out, by
    relationship (ADR 0016 section 1): the relationship's parent rows are then the counted layout's
    (KERNEL-SPEC-2 section 8.1). Refused (``FLX-3403``) when the expose does not declare the
    relationship, is not generated from statistics or is a reference expose, on a ``distinct``
    relationship, one that is part of a key, implied by another, a bridge side or a
    self-relationship, when the first column declares a ``nullFraction`` or a draw of its own, when
    a ``links`` entry gives the relationship a condition or a weight, when a dependency sets its
    columns, and when the shape is not strictly ascending whole counts with weights that sum to 1.
    Kernel 0.1.0 refuses it (``FLX-3105``). *contract* is the contract document, which a
    ``parentWhen`` is checked against: the parent's own columns only (``FLX-3403`` for a read of
    a link, a previous version, children, siblings or a column a summary over children sets).

    *namespace* is the Simulation's FLUX namespace (``extensions['com.agentics.flux/1']``), which
    every registered expected-count hook reads at plan time (ADR 0016 section 3): a relationship
    a hook binds a member for is on the counted layout as if it had a ``counts`` entry, every
    refusal above applies to it, and two hooks binding one relationship is ``FLX-3403``.

    *histories* maps an exposeId to its ``history`` entry and the scaled ``entities`` (ADR 0017
    section 3): the emit's rows are then entities and their versions, the natural entity's column
    is laid out over the entities, copied on every version, and the ``parent`` relationship's
    foreign key copies the parent row of the version's entity. The ``parent`` entry is read as a
    ``counts`` entry is, over the entities, and an expected-count hook binds it as it would a
    ``counts`` entry's relationship. Refused (``FLX-3403``): an emit that is not generated from
    statistics, has no ``timeBounds`` or no natural entity, a natural entity another key or a
    relationship also sets, a layout of no entity for its rows or of no parent for its entities,
    and an empty first-start interval. Kernel 0.1.0 refuses it (``FLX-3105``).
    """
    declared = _declared_relationships(keys)
    bridges = bridges or {}
    weights = weights or {}
    counts = counts or {}
    hooked = _hooked(plans, keys, namespace or {})
    histories = histories or {}
    periodic = _periodic_emits(namespace or {})
    _check_histories(plans, keys, histories)
    _refuse_history_targets(plans, keys, histories, counts)
    counts, hooked, parents = _split_history_parents(counts, hooked, histories)
    _check_weighted(plans, weights)
    _check_counted(plans, counts, hooked)
    _check_reached(plans, keys)
    _check_bridged(plans, bridges)
    check_reference_exposes(plans, keys)
    shapes = {
        expose_id: _shape(
            plans[expose_id],
            keys.expose(expose_id),
            plans,
            declared,
            tuple(bridges.get(expose_id, ())),
            weights.get(expose_id, {}),
            counts.get(expose_id, {}),
            contract,
            hooked.get(expose_id, {}),
            _history_shape_of(expose_id, histories.get(expose_id), plans, keys, parents),
            nfc(expose_id) in periodic,
        )
        for expose_id in sorted(plans)
    }
    _refuse_copies_into_histories(shapes, histories, _projected_emits(namespace or {}))
    related = _with_drivers(plans, _Resolver(shapes))
    _check_asof_reads(related, keys)
    return _attach_sums(_attach_reads(_link_givens(related, keys), keys))


def _refuse_copies_into_histories(
    shapes: Mapping[str, _Shape],
    histories: Mapping[str, HistoryBinding],
    projected: Mapping[str, str],
) -> None:
    """Refuse (``FLX-3403``) a copy through a relationship whose target declares a history or is projected from one (ADR 0022 section 4).

    A history's emit holds the foreign keys of an entity's first version on every later version, so
    the row a copy reaches from a later version, followed through the relationship that holds the
    copied column, is not the row the copied cell names there: a relationship into a slowly
    changing target is out of reach of a copy, as it is of a one_to_one pick. An emit projected
    from a history (a snapshot, events or periods, ADR 0027) holds the history's cells on the row
    it reads, not the pick of a draw of its own, so the row its relationship names is not the
    row the copy reaches either. *projected* maps each such emit, in NFC, to the kind of its
    entry.
    """
    held = {nfc(expose_id) for expose_id in histories}
    for expose_id in sorted(shapes):
        for copied in shapes[expose_id].copied.values():
            reach = nfc(copied.reach)
            where = (
                f"expose {expose_id!r} copies {copied.column!r} through relationship "
                f"{copied.through!r} into relationship {copied.relationship!r}"
            )
            if reach in held:
                raise _refuse(
                    f"{where}, but {copied.reach!r} declares a history, whose foreign keys are "
                    "those of an entity's first version on each of its versions, so the copy "
                    f"would not equal the row {copied.relationship!r} names on a later version; "
                    "read the column from a table with no history, or leave the copy out"
                )
            if reach in projected:
                raise _refuse(
                    f"{where}, but {copied.reach!r} is projected from a history ({projected[reach]} "
                    f"entry), so each of its rows holds the cells of a version, not the pick of a "
                    f"draw of its own, and the copy would not equal the row "
                    f"{copied.relationship!r} names there; read the column from a table that is "
                    "no projection, or leave the copy out"
                )


def _projected_emits(namespace: Mapping[str, Any]) -> Dict[str, str]:
    """The emits a ``snapshots``, ``events`` or ``periods`` entry of the namespace names, in NFC, with the entry's kind."""
    found: Dict[str, str] = {}
    for kind in (SNAPSHOTS_KEY, EVENTS_KEY, PERIODS_KEY):
        entries = namespace.get(kind)
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and isinstance(entry.get("emit"), str):
                found.setdefault(nfc(entry["emit"]), kind)
    return found


def _periodic_emits(namespace: Mapping[str, Any]) -> FrozenSet[str]:
    """The emits a ``periods`` entry of the namespace names, in NFC."""
    entries = namespace.get("periods")
    if not isinstance(entries, (list, tuple)):
        return frozenset()
    return frozenset(
        nfc(entry["emit"])
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("emit"), str)
    )


def _check_histories(
    plans: Mapping[str, "_rows.GeneratorPlan"],
    keys: ContractKeys,
    histories: Mapping[str, HistoryBinding],
) -> None:
    """Refuse a history on an emit that cannot lay one out (``FLX-3403``; ``FLX-3105`` on kernel 0.1.0)."""
    for expose_id in sorted(histories):
        binding = histories[expose_id]
        where = f"history ({expose_id})"
        plan = plans.get(expose_id)
        if plan is None:
            raise _refuse(
                f"{where}: the Simulation does not generate {expose_id!r} from statistics"
            )
        if not plan.counter_based:
            from flux_engine.seam.resolve import FLX_3105

            raise FluxError(
                FLX_3105, f"{where}: a history needs kernel 0.2.0; this run is kernel 0.1.0"
            )
        if is_reference_plan(plan):
            raise _refuse(f"{where}: {expose_id!r} is a reference expose, released whole")
        if plan.time_bounds is None:
            raise _refuse(
                f"{where}: the first starts are drawn from timeBounds.start; declare timeBounds"
            )
        natural = [e.column for e in keys.expose(expose_id).entities if e.kind == "natural"]
        if len(natural) != 1:
            raise _refuse(
                f"{where}: {expose_id!r} declares {len(natural)} natural entities, not one"
            )
        try:
            kind = plan.column_plan(binding.start).column.logical_type.kind
        except FluxError:
            raise _refuse(f"{where}: {expose_id!r} has no start column {binding.start!r}") from None
        chain_unit_kind(kind)
        _refuse_rules_on_end_column(where, binding, plan)


def _refuse_rules_on_end_column(
    where: str, binding: HistoryBinding, plan: "_rows.GeneratorPlan"
) -> None:
    """Refuse (``FLX-6203``) a rule that sets the column ``end.column`` names (ADR 0017, ADR 0027).

    The history writes ``end.value`` into the column on an ending version and a drawn level on the
    others, and the rows are held to that, so a rule on the column, whatever its ``when``, would be
    a second source for it.
    """
    end = binding.entry.end
    rules = plan.rules
    if end is None or end.column is None or rules is None:
        return
    setters: Dict[str, str] = {}
    for rule in rules.rules:
        for column in (*rule.determines, *rule.to_columns):
            setters.setdefault(column, f"the rule {rule.id!r}")
    for column in rules.determined:
        setters.setdefault(column, "a rule")
    if end.column in setters:
        from flux_engine.expr2.errors import FLX_6203

        raise FluxError(
            FLX_6203,
            f"column {end.column!r} has more than one source: {where} sets it as its end column "
            f"(end.column), and {setters[end.column]} sets it too; leave the column to the "
            "history, or name another column in end.column",
        )


def _refuse_history_targets(
    plans: Mapping[str, "_rows.GeneratorPlan"],
    keys: ContractKeys,
    histories: Mapping[str, HistoryBinding],
    counts: Mapping[str, Mapping[str, CountsEntry]],
) -> None:
    """Refuse a ``distinct`` or ``one_to_one`` relationship into a history's emit, and a ``counts`` entry on one.

    A child picks a row of the history's emit as any child does, so an entity is picked in
    proportion to its versions; a pick that names each row at most once, or a counted layout of
    the versions, has no meaning there in this version (ADR 0017 section 8, ``FLX-3403``).
    """
    if not histories:
        return
    for expose_id in sorted(plans):
        held = keys.expose(expose_id)
        key_sets = {frozenset(key) for key in (*held.keys, *held.instant_keys)}
        for relationship in held.relationships:
            if relationship.target not in histories:
                continue
            where = (
                f"expose {expose_id!r} relationship {relationship.name!r} reaches "
                f"{relationship.target!r}, which declares a history"
            )
            if relationship.cardinality == "one_to_one" or (
                frozenset(relationship.from_columns) in key_sets
            ):
                raise _refuse(
                    f"{where}, and is one_to_one or a key of {expose_id!r}: it would reference "
                    "each version at most once, which this version does not lay out"
                )
            if relationship.name in counts.get(expose_id, {}):
                raise _refuse(
                    f"{where}, and a counts entry lays it out: this version does not count the "
                    "children of a version"
                )


def _entity_columns_of(plan: "_rows.GeneratorPlan") -> FrozenSet[str]:
    """The natural entity's columns of a history's emit, which carry one value for every version."""
    if plan.relations is None:
        return frozenset()
    return frozenset(
        name for name, driver in plan.relations.drivers if isinstance(driver, EntityColumn)
    )


def readable_without_asof(plan: "_rows.GeneratorPlan", keys: ContractKeys) -> FrozenSet[str]:
    """The columns of a history's emit a path may read with no as-of entry (ADR 0017 section 8).

    The columns that are the same on every version of an entity: its key, its parent's key, its
    activation and the carried columns. A tracked column, a validity column, the end column and a
    column a rule sets are not among them.
    """
    pick = history_pick_of(plan)
    if pick is None:
        return frozenset()
    entry = pick.entry
    held = set(carried_columns(plan)) | _entity_columns_of(plan)
    if entry.activation is not None:
        held.add(entry.activation)
    if entry.parent is not None:
        link = next(
            (
                r
                for r in keys.expose(plan.expose.expose_id).relationships
                if r.name == entry.parent.relationship
            ),
            None,
        )
        if link is not None:
            held.update(link.from_columns)
    if entry.end is not None and entry.end.column is not None:
        held.discard(entry.end.column)
    return frozenset(held)


def _hop_source(expose_id: str, path: Sequence[str], keys: ContractKeys) -> str:
    """The expose the last hop of *path* leaves."""
    source = expose_id
    for name in path[:-1]:
        hop = next((r for r in keys.expose(source).relationships if r.name == name), None)
        if hop is None:  # pragma: no cover - the rule's text was checked against the keys
            break
        source = hop.target
    return source


def _check_asof_reads(related: Mapping[str, "_rows.GeneratorPlan"], keys: ContractKeys) -> None:
    """Refuse what a path reads of a history's emit, and what an as-of link cannot promise (ADR 0017 section 8).

    Without an as-of entry a path may read only what every version of an entity holds
    (:func:`readable_without_asof`); a tracked column, a validity column or a column a rule sets
    is ``FLX-6106``, and the message names ``asOf``. With an entry, a required column set by a
    rule that reads through the link is ``FLX-3403``, since a row dated where its entity has no
    version would write NULL there, unless the history has no ``end`` and the declared range of the
    ``date`` starts at or after ``until``.
    """
    for expose_id in sorted(related):
        plan = related[expose_id]
        if plan.rules is None:
            continue
        for path, read_expose, column in sorted(plan.rules.link_paths()):
            _check_path_read(expose_id, path, read_expose, column, related, keys)
        for entry in plan.rules.asof:
            _check_asof_required(plan, entry, related, keys)


def _check_path_read(
    expose_id: str,
    path: Tuple[str, ...],
    read_expose: str,
    column: str,
    related: Mapping[str, "_rows.GeneratorPlan"],
    keys: ContractKeys,
) -> None:
    """Refuse a path that reads, without an as-of entry, a column that differs between versions."""
    target = related.get(read_expose)
    if target is None or history_pick_of(target) is None:
        return
    source = _hop_source(expose_id, path, keys)
    holder = related[source].rules
    if holder is not None and holder.asof_date(path[-1]) is not None:
        return
    if column not in readable_without_asof(target, keys):
        raise Expr2Error(
            FLX_6106,
            f"a rule of {expose_id!r} reads {read_expose}.{column} through "
            f"{'.'.join(path)}, which differs from one version of an entity to the next "
            f"(a tracked column, a validity column or a column a rule sets); declare "
            f"asOf for {source}.{path[-1]} to read the version valid on a date",
        )


def _first_date(plan: "_rows.GeneratorPlan", column: str) -> Optional[int]:
    """The least instant (milliseconds) the declared range or bins of *column* gives, or ``None``."""
    column_plan = plan.column_plan(column)
    if column_plan.defaulted or (column_plan.statistics.null_fraction or 0) > 0:
        return None
    statistics = column_plan.statistics
    low = statistics.range_min
    if low is None and statistics.bins:
        low = statistics.bins[0]
    if low is None:
        return None
    unit = 86_400_000 if column_plan.column.logical_type.kind is LogicalKind.DATE else 1
    return int(low) * unit


def _check_asof_required(
    plan: "_rows.GeneratorPlan",
    entry: Any,
    related: Mapping[str, "_rows.GeneratorPlan"],
    keys: ContractKeys,
) -> None:
    """Refuse (``FLX-3403``) a required column a rule sets from a link that can reach no version."""
    rules = plan.rules
    assert rules is not None
    relationship = next(
        (
            r
            for r in keys.expose(plan.expose.expose_id).relationships
            if r.name == entry.relationship
        ),
        None,
    )
    if relationship is None:
        return
    target = related.get(relationship.target)
    pick = None if target is None else history_pick_of(target)
    if pick is None:
        return
    required = sorted(
        column
        for rule in rules.rules
        if any(path[0] == entry.relationship for path in rule.paths)
        for column in rule.determines
        if plan.column_plan(column).column.required
    )
    if not required:
        return
    if pick.entry.end is None and entry.date not in rules.determined:
        first = _first_date(plan, entry.date)
        until = unit_of(pick.entry.until, pick.kind) * (
            86_400_000 if pick.kind is LogicalKind.DATE else 1
        )
        if first is not None and first >= until:
            return
    raise _refuse(
        f"asOf ({plan.expose.expose_id}.{entry.relationship}): a rule sets {required[0]!r}, which "
        f"the contract requires, from a row of {relationship.target!r} read as of "
        f"{entry.date!r}; a row dated where its entity has no version would write NULL there. "
        "Declare the column optional, or give the history no end and the date a range that "
        "starts at or after until"
    )


_Hooked = Tuple[ExpectedCountHook, Mapping[str, Any]]


def _split_history_parents(
    counts: Mapping[str, Mapping[str, CountsEntry]],
    hooked: Mapping[str, Mapping[str, _Hooked]],
    histories: Mapping[str, HistoryBinding],
) -> Tuple[
    Dict[str, Mapping[str, CountsEntry]],
    Dict[str, Dict[str, _Hooked]],
    Dict[str, Tuple[CountsEntry, Optional[_Hooked]]],
]:
    """The counts and hooks without the history parents, and each parent read as a ``counts`` entry.

    A history's ``parent`` lays out its entities, not its rows, so it does not take the rows'
    counted layout; a ``counts`` entry that names the same relationship is two layouts of one
    foreign key (``FLX-3403``). The expected-count hook that binds the relationship gives the
    entity layout its ``expected``, as it would give a ``counts`` entry's.
    """
    kept_counts: Dict[str, Mapping[str, CountsEntry]] = {k: dict(v) for k, v in counts.items()}
    kept_hooks: Dict[str, Dict[str, _Hooked]] = {k: dict(v) for k, v in hooked.items()}
    parents: Dict[str, Tuple[CountsEntry, Optional[_Hooked]]] = {}
    for expose_id, binding in sorted(histories.items()):
        parent = binding.entry.parent
        if parent is None:
            continue
        name = parent.relationship
        if name in counts.get(expose_id, {}):
            raise _refuse(
                f"history ({expose_id}) parent {name!r} is also named by a counts entry; a "
                "relationship has one layout of its children"
            )
        kept_hooks.get(expose_id, {}).pop(name, None)
        parents[expose_id] = (
            CountsEntry(
                emit=expose_id,
                relationship=name,
                children=parent.children,
                min_children=parent.min_children,
                parent_when=parent.parent_when,
            ),
            hooked.get(expose_id, {}).get(name),
        )
    return kept_counts, kept_hooks, parents


def _history_shape_of(
    expose_id: str,
    binding: Optional[HistoryBinding],
    plans: Mapping[str, "_rows.GeneratorPlan"],
    keys: ContractKeys,
    parents: Mapping[str, Tuple[CountsEntry, Optional[_Hooked]]],
) -> Optional[_HistoryShape]:
    """The relational shape of one history's emit, or ``None`` for an emit with none."""
    if binding is None:
        return None
    plan = plans[expose_id]
    assert plan.time_bounds is not None  # _check_histories
    natural = next(e.column for e in keys.expose(expose_id).entities if e.kind == "natural")
    pick = HistoryPick(
        entry=binding.entry,
        kind=plan.column_plan(binding.start).column.logical_type.kind,
        rows=plan.rows,
        entities=binding.entities,
        window_start=plan.time_bounds[0],
        parent_lifetime=binding.parent_lifetime,
        declared_rows=binding.declared_rows,
    )
    # The refusals that follow from declared values alone, before any layout is worked out.
    declared = binding.entry.parent
    _check_parent_lifetime(expose_id, binding, plans, keys)
    target = None
    if declared is not None:
        target = next(
            (
                plans[r.target].rows
                for r in keys.expose(expose_id).relationships
                if r.name == declared.relationship and r.target in plans
            ),
            None,
        )
    check_layout(
        pick.entry,
        pick.kind,
        pick.laid_rows,
        pick.entities,
        pick.window_start,
        target,
    )
    held = parents.get(expose_id)
    return _HistoryShape(
        pick=pick,
        natural=natural,
        parent=None if held is None else held[0],
        hook=None if held is None else held[1],
        start=binding.start,
        end=binding.end,
        tracked_levels=binding.tracked_levels,
    )


def _check_parent_lifetime(
    expose_id: str,
    binding: HistoryBinding,
    plans: Mapping[str, "_rows.GeneratorPlan"],
    keys: ContractKeys,
) -> None:
    """Refuse (``FLX-3403``) a ``parent.lifetime`` whose parent emit gives no lifetime columns to read.

    The entry needs a ``lifetimes`` entry on the emit its ``parent.relationship`` reaches, and that
    emit must be generated in the Simulation, with the entry's start and end among its columns.
    """
    declared = binding.entry.parent
    if declared is None or not declared.lifetime:
        return
    where = f"history ({expose_id}).parent.lifetime"
    reached = next(
        (r for r in keys.expose(expose_id).relationships if r.name == declared.relationship), None
    )
    lifetime = binding.parent_lifetime
    if reached is None or lifetime is None or reached.target not in plans:
        raise _refuse(
            f"{where} needs a lifetimes entry on the emit {declared.relationship!r} reaches, and "
            "that emit generated in this Simulation"
        )
    named = {column_plan.column.name for column_plan in plans[reached.target].columns}
    for column in (lifetime[0], *(() if lifetime[1] is None else (lifetime[1],))):
        if column not in named:
            raise _refuse(f"{where}: {reached.target!r} has no column {column!r}")


def _declared_relationships(keys: ContractKeys) -> FrozenSet[_Declared]:
    """Every relationship the contract declares, as its source, columns, target and join type."""
    return frozenset(
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


def _with_drivers(
    plans: Mapping[str, "_rows.GeneratorPlan"], resolver: "_Resolver"
) -> Dict[str, "_rows.GeneratorPlan"]:
    """Each plan with the drivers the resolver settled for its columns.

    A plan with none, and a reference plan, comes back unchanged.
    """
    related: Dict[str, "_rows.GeneratorPlan"] = {}
    for expose_id, plan in plans.items():
        drivers = resolver.drivers(expose_id)
        related[expose_id] = (
            plan
            if not drivers or is_reference_plan(plan)
            else replace(plan, relations=Relations(drivers=drivers))
        )
    return related


def _paths_of(plan: "_rows.GeneratorPlan") -> List[Tuple[str, ...]]:
    """The relationship paths *plan* reads, sorted: those of its rules and of its linked givens.

    None for a plan that reads no other expose's row.
    """
    rules = plan.rules
    paths = {path for rule in rules.rules for path in rule.paths} if rules else set()
    if plan.relations is not None:
        paths.update((given.relationship,) for given in plan.relations.givens)
        # A weighted pick reads the weights of its parent's final rows (a reference expose's are
        # published values, read off its driver).
        paths.update(
            (driver.relationship,)
            for _, driver in plan.relations.drivers
            if isinstance(driver, ForeignKey)
            and driver.pick == "weighted"
            and driver.weight_values is None
        )
        # A copied pick reads the pick of the row its relationship reaches.
        paths.update(
            (driver.copied.through,)
            for _, driver in plan.relations.drivers
            if isinstance(driver, ForeignKey) and driver.copied is not None
        )
        # A parentWhen, a given column and an expected-count hook read the parent's final rows, and the child's
        # digest holds the parent's.
        paths.update(
            (driver.relationship,)
            for _, driver in plan.relations.drivers
            if isinstance(driver, ForeignKey)
            and driver.counted is not None
            and (
                driver.counted.parent_when is not None
                or driver.counted.hook is not None
                or driver.counted.given is not None
                or (driver.history is not None and driver.history.parent_lifetime is not None)
            )
        )
        # A foreign key into a history that keeps to its parent's lifetime reads that history's cells,
        # whose layout needs the history's own plan and its parent's.
        paths.update(
            (driver.relationship,)
            for _, driver in plan.relations.drivers
            if isinstance(driver, ForeignKey) and _reaches_bound_history(driver)
        )
    return sorted(paths)


def _reaches_bound_history(driver: ForeignKey) -> bool:
    """Whether *driver*'s parent is the emit of a history that keeps to its parent's lifetime."""
    return any(
        isinstance(held, (EntityColumn, ChainColumn, TrackedColumn))
        and held.history.parent_lifetime is not None
        for _, held in driver.parent.drivers
    )


def _refuse_given(message: str) -> FluxError:
    """A column drawn given a linked column that cannot be, before any row is drawn (``FLX-3402``)."""
    return FluxError(_rows.FLX_3402, message)


def _link_givens(
    related: Dict[str, "_rows.GeneratorPlan"], keys: ContractKeys
) -> Dict[str, "_rows.GeneratorPlan"]:
    """*related* with every column drawn ``given <relationship>.<column>`` resolved against its parent.

    ADR 0023 section 3. A plan with no such column comes back unchanged. Each is refused
    (``FLX-3402``) unless the relationship is a to-one one the expose declares, its columns are
    set by a foreign key or key part of the expose and no rule, and it reaches a row on every row;
    the parent column is drawn from its own declared levels with no ``nullFraction``, is the
    ``to`` column of a contract dependency, or is a column of a reference expose; and the weight
    tables name exactly the levels the parent column draws.
    """
    resolved = dict(related)
    for expose_id in sorted(related):
        plan = related[expose_id]
        found = [
            _linked_given(plan, column_plan, related, keys)
            for column_plan in plan.columns
            if column_plan.statistics.given_link is not None
        ]
        if not found:
            continue
        assert plan.relations is not None  # _linked_given refused a link with no driver
        resolved[expose_id] = replace(plan, relations=replace(plan.relations, givens=tuple(found)))
    _refuse_given_cycle(resolved)
    return resolved


def check_links_resolved(plan: "_rows.GeneratorPlan") -> None:
    """Refuse (``FLX-3402``) a plan whose column is drawn given a link that was never resolved.

    :func:`relate_plans` resolves every such column. A plan the contract relates nothing in
    comes back from the pipeline untouched, and a column drawn given a link would have none to
    read, so it is refused here rather than at its first row.
    """
    resolved = set() if plan.relations is None else {g.column for g in plan.relations.givens}
    if _summaries(plan) and not plan.sums:
        raise Expr2Error(
            FLX_6210,
            f"expose {plan.expose.expose_id!r} summarises its children, and no plan of them was "
            "attached; relate the plans before generating their rows",
        )
    for column_plan in plan.columns:
        link = column_plan.statistics.given_link
        if link is not None and column_plan.column.name not in resolved:
            raise _refuse_given(
                f"expose {plan.expose.expose_id!r} column {column_plan.column.name!r} is drawn "
                f"given {'.'.join(link)}, and the contract declares no relationship {link[0]!r} "
                "for the expose; declare it, or draw the column given a column of this expose"
            )


def _linked_given(
    plan: "_rows.GeneratorPlan",
    column_plan: "_rows.ColumnPlan",
    related: Mapping[str, "_rows.GeneratorPlan"],
    keys: ContractKeys,
) -> LinkedGiven:
    link = column_plan.statistics.given_link
    assert link is not None  # the caller picked the columns that have one
    name, (rel_name, parent_column) = column_plan.column.name, link
    expose_id = plan.expose.expose_id
    where = f"expose {expose_id!r} column {name!r} is drawn given {rel_name}.{parent_column}"
    if not plan.counter_based:
        raise _refuse_given(f"{where}, which kernel 0.2.0 generates; this run is on kernel 0.1.0")
    relationship = next(
        (r for r in keys.expose(expose_id).relationships if r.name == rel_name), None
    )
    if relationship is None:
        raise _refuse_given(
            f"{where}, and the contract declares no relationship {rel_name!r} for the expose"
        )
    first = plan.column_plan(relationship.from_columns[0])
    if plan.rules is not None and any(link.relationship == rel_name for link in plan.rules.links):
        raise _refuse_given(
            f"{where}, and relationship {rel_name!r} has a links condition, so it may reach no "
            "row; a given link reaches a row on every row"
        )
    if first.statistics.null_fraction is not None:
        raise _refuse_given(
            f"{where}, and relationship {rel_name!r} may reach no row (its first column "
            f"{first.column.name!r} declares a nullFraction); a given link reaches a row on every row"
        )
    foreign = _foreign_driver(plan, rel_name)
    if foreign is None and _composite_part(plan, rel_name) is None:
        raise _refuse_given(
            f"{where}, and another relationship sets the columns of relationship {rel_name!r}; "
            "name the relationship that sets them"
        )
    ruled = frozenset() if plan.rules is None else plan.rules.determined
    if any(column in ruled for column in relationship.from_columns):
        raise _refuse_given(f"{where}, and a rule sets the columns of relationship {rel_name!r}")
    if (
        foreign is not None
        and foreign.pick == "declared"
        and (first.statistics.parents or first.column.name in _rows._given_columns(plan))
    ):
        raise _refuse_given(
            f"{where}, and the foreign key {first.column.name!r} of that relationship is itself "
            "drawn given another column; this version reads a link whose parent row is picked"
        )
    parent = related.get(relationship.target)
    if parent is None:  # pragma: no cover - _check_relationship refused a target with no plan
        raise _refuse_given(f"{where}, and {relationship.target!r} has no plan")
    levels = _parent_levels(parent, parent_column, where)
    held = [level for level, _ in levels]
    statistics = column_plan.statistics
    table, declared = (
        ("binWeightsGiven", sorted(statistics.bin_weights_given))
        if statistics.bin_weights_given is not None
        else ("levelWeightsGiven", sorted(statistics.level_weights_given or ()))
    )
    if declared != sorted(held):
        raise _refuse_given(
            f"{name}: {table} declares levels {declared}; {rel_name}.{parent_column} draws "
            f"{sorted(held)}"
        )
    return LinkedGiven(
        column=name,
        relationship=rel_name,
        parent=relationship.target,
        parent_column=parent_column,
        levels=levels,
    )


def _parent_levels(
    parent: "_rows.GeneratorPlan", column: str, where: str
) -> Tuple[Tuple[str, Any], ...]:
    """The levels a linked parent column draws, as ``(level, value)``, or ``FLX-3402``.

    A column of a reference expose draws its distinct published values. Any other is a
    categorical drawn from its own declared levels with no ``nullFraction``, which a contract
    dependency may set (it draws once per key from those levels) and no derive or check rule
    may.
    """
    try:
        column_plan = parent.column_plan(column)
    except FluxError:
        raise _refuse_given(
            f"{where}, and {parent.expose.expose_id!r} has no column {column!r}"
        ) from None
    if is_reference_plan(parent):
        assert parent.relations is not None  # a reference plan
        driver = parent.relations.driver(column)
        assert isinstance(driver, ReferenceColumn)
        published = reference_levels(column_plan.column, driver)
        if published is None:
            raise _refuse_given(
                f"{where}, which publishes NULL in some row; a NULL has no level to condition on"
            )
        return published
    statistics = column_plan.statistics
    levels_declared = statistics.levels is not None or bool(statistics.support)
    ruled = () if parent.rules is None else parent.rules.rules_of(column)
    if (
        column_plan.family is not Family.CATEGORICAL
        or not levels_declared
        or statistics.parents
        or statistics.given_link is not None
        or statistics.level_weights_given is not None
        or (parent.relations is not None and parent.relations.driver(column) is not None)
    ):
        raise _refuse_given(
            f"{where}, which is not a categorical drawn from its own declared levels, so it has "
            "no level to condition on"
        )
    if statistics.null_fraction is not None:
        raise _refuse_given(
            f"{where}, which declares a nullFraction, and a NULL has no level to condition on"
        )
    if any(rule.form != FORM_DEPENDENCY for rule in ruled):
        raise _refuse_given(
            f"{where}, which a rule sets; only a contract dependency's `to` column, drawn from "
            "its own levels, may be the parent of a linked given"
        )
    names = _rows._level_names(statistics)
    with localcontext(_rows._KERNEL_CONTEXT):
        values = [_rows._level_to_value(level, column_plan.column) for level in names]
    if len({(type(value), value) for value in values}) != len(values):
        raise _refuse_given(f"{where}, whose levels spell one value more than once")
    return tuple(zip(names, values, strict=True))


def _refuse_given_cycle(plans: Mapping[str, "_rows.GeneratorPlan"]) -> None:
    """Refuse (``FLX-3402``) exposes whose given columns read each other round to the first."""
    reads: Dict[str, Set[str]] = {
        expose_id: {given.parent for given in plan.relations.givens}
        for expose_id, plan in plans.items()
        if plan.relations is not None and plan.relations.givens
    }
    state: Dict[str, int] = {}

    def visit(expose_id: str, trail: List[str]) -> None:
        if state.get(expose_id) == 2:
            return
        if state.get(expose_id) == 1:
            cycle = [*trail[trail.index(expose_id) :], expose_id]
            raise _refuse_given(
                "columns drawn given a linked column read in a cycle: "
                + " -> ".join(repr(name) for name in cycle)
                + "; generating a row would need a row of an expose that needs it in turn"
            )
        state[expose_id] = 1
        for target in sorted(reads.get(expose_id, ())):
            visit(target, [*trail, expose_id])
        state[expose_id] = 2

    for expose_id in sorted(reads):
        visit(expose_id, [])


def _blame_of(plan: "_rows.GeneratorPlan") -> Dict[Tuple[str, ...], str]:
    """What names each path of *plan*'s copies in a refusal: the ``sharedAttribute`` and its members."""
    rules = plan.rules
    return (
        {path: rule.blame for rule in rules.rules if rule.blame for path in rule.paths}
        if rules
        else {}
    )


def _check_reached(plans: Mapping[str, "_rows.GeneratorPlan"], keys: ContractKeys) -> None:
    """Refuse a rule that reads through a link into an expose that has no plan (``FLX-6210``).

    Run before the plans are related, so that a path into an expose the Simulation does not
    generate from statistics is refused as a rule, ahead of the foreign key it goes through.
    """
    for expose_id in sorted(plans):
        _crossed(
            expose_id, _paths_of(plans[expose_id]), plans, keys, None, _blame_of(plans[expose_id])
        )


def _attach_reads(
    related: Dict[str, "_rows.GeneratorPlan"], keys: ContractKeys
) -> Dict[str, "_rows.GeneratorPlan"]:
    """*related*, with every plan whose rules read through links carrying the plans it reaches.

    ADR 0022 section 3. Each path is walked hop by hop through the contract's relationships; a
    relationship with no driver of its own (another relationship sets its columns) and a cycle
    among the exposes that rules read through are ``FLX-6210``, raised before a row exists. A plan
    whose rules read no link comes back unchanged. A reached plan carries the plans of the exposes
    its own rules reach.
    """
    crossed: Dict[str, Set[str]] = {}
    for expose_id in sorted(related):
        paths = _paths_of(related[expose_id])
        if paths:
            crossed[expose_id] = _crossed(
                expose_id, paths, related, keys, related, _blame_of(related[expose_id])
            )
    if not crossed:
        return related
    attached = dict(related)
    for expose_id in _reading_order(crossed):
        reads = tuple((target, attached[target]) for target in sorted(crossed[expose_id]))
        attached[expose_id] = replace(attached[expose_id], reads=reads)
    return attached


def _summaries(plan: "_rows.GeneratorPlan") -> List[Tuple[str, str]]:
    """The ``(child exposeId, relationship)`` each summary of *plan*'s rules reads, sorted."""
    rules = plan.rules
    if rules is None:
        return []
    return sorted({rule.summary for rule in rules.rules if rule.summary is not None})


def _attach_sums(related: Dict[str, "_rows.GeneratorPlan"]) -> Dict[str, "_rows.GeneratorPlan"]:
    """*related*, with every plan that summarises children carrying the plans of those children.

    ADR 0016 section 5. Each child must be a plan of the Simulation whose relationship is on the
    counted layout (``FLX-6210`` otherwise). The children are attached after the reads, so a child
    reads the parent's plan as it is before its sums are attached and no plan holds itself; a
    child that reads a column a summary sets, or one a rule computes from it, is a cycle
    (``FLX-6208``), and a plan outside the children that reads one is ``FLX-6210``. A summed child
    carries its own sums first. A plan with no summary comes back unchanged.
    """
    summing = {expose_id: _summaries(plan) for expose_id, plan in sorted(related.items())}
    summing = {expose_id: found for expose_id, found in summing.items() if found}
    if not summing:
        return related
    for expose_id, found in summing.items():
        for child, relationship in found:
            _check_summed(expose_id, child, relationship, related)
    _refuse_summary_reads(related, summing)
    attached = dict(related)
    state: Dict[str, int] = {}  # 1 on the way down, 2 done

    def attach(expose_id: str, trail: List[str]) -> None:
        if state.get(expose_id) == 2:
            return
        if state.get(expose_id) == 1:
            cycle = [*trail[trail.index(expose_id) :], expose_id]
            raise Expr2Error(
                FLX_6208,
                "summaries over children are in a cycle: "
                + " -> ".join(repr(name) for name in cycle)
                + "; each sums rows that sum the other",
            )
        state[expose_id] = 1
        for child, _ in summing.get(expose_id, ()):
            attach(child, [*trail, expose_id])
        state[expose_id] = 2
        if expose_id in summing:
            attached[expose_id] = replace(
                attached[expose_id],
                sums=tuple((child, via, attached[child]) for child, via in summing[expose_id]),
            )

    for expose_id in sorted(summing):
        attach(expose_id, [])
    return attached


def _check_summed(
    parent: str, child: str, relationship: str, plans: Mapping[str, "_rows.GeneratorPlan"]
) -> None:
    """Refuse (``FLX-6210``) a summary over a child that has no plan or whose relationship is not counted."""
    where = f"a summary of {parent!r} over {child}.{relationship}"
    source = plans.get(child)
    if source is None:
        raise Expr2Error(
            FLX_6210,
            f"{where}, but this Simulation does not generate {child!r} from statistics, so there "
            "are no child rows to summarise. Emit it with a fidelity block in the same Simulation",
        )
    driver = _foreign_driver(source, relationship)
    if driver is None or driver.pick != "counted":
        raise Expr2Error(
            FLX_6210,
            f"{where}, whose relationship is not on the counted layout; lay {child}.{relationship} "
            "out with a counts entry",
        )
    _check_summary_nulls(plans[parent], child, relationship, driver.counted)


def _check_summary_nulls(
    plan: "_rows.GeneratorPlan", child: str, relationship: str, counted: Optional[CountedPick]
) -> None:
    """Refuse (``FLX-3403``) a required sum, min or max with no ``ifNone`` over parents that can have no children.

    Such a parent's summary is NULL (ADR 0016 section 5), and a required column cannot hold it,
    which generating the rows would otherwise find only as a missing column.
    """
    if counted is None or plan.rules is None:
        return
    shape = counted.shape()
    classes = None if shape is None else classes_of(shape, "children")
    childless = counted.parent_when is not None or (
        (counted.minimum or 0) == 0
        and (classes[0].low == 0 if classes is not None else counted.child_rows < plan.rows)
    )
    required = {column.column.name for column in plan.columns if column.column.required}
    for rule in plan.rules.rules:
        if (
            childless
            and rule.summary == (child, relationship)
            and rule.column in required
            and rule.text.split("(", 1)[0] in ("SUM", "MIN", "MAX")
        ):
            raise _refuse(
                f"expose {plan.expose.expose_id!r} column {rule.column!r} is a required aggregate "
                f"over {child}.{relationship} with no ifNone, but the counts entry can leave a "
                f"parent with no {child} rows, whose summary is NULL; add ifNone to the aggregate "
                "or, with no parentWhen, set minChildren to 1 and a shape with no zero count"
            )


def _reads_closure(plan: "_rows.GeneratorPlan") -> Set[str]:
    """The exposeId of *plan* and of every plan its rules read through, however far."""
    found = {plan.expose.expose_id}
    for _, read in plan.reads:
        found |= _reads_closure(read)
    return found


def _refuse_summary_reads(
    related: Mapping[str, "_rows.GeneratorPlan"], summing: Mapping[str, Sequence[Tuple[str, str]]]
) -> None:
    """Refuse a rule that reads, through a link, a column a summary sets or one computed from it.

    A child, or an expose a child reads, that does is a cycle (``FLX-6208``): its rows decide the
    summary the column holds. Any other reader would read the parent as its plan is before its
    sums, so this version refuses it (``FLX-6210``).
    """
    for parent in sorted(summing):
        rules = related[parent].rules
        assert rules is not None  # only a plan with rules summarises
        held = rules.summarised
        feeding: Set[str] = set()
        for child, _ in summing[parent]:
            feeding |= _reads_closure(related[child])
        for expose_id in sorted(related):
            reader_rules = related[expose_id].rules
            if reader_rules is None:
                continue
            names = sorted(
                column
                for read, column in reader_rules.link_reads()
                if read == parent and column in held
            )
            if not names:
                continue
            what = (
                f"{expose_id!r} reads {parent}.{names[0]} through a link, a column that "
                f"{parent!r} summarises over its children or computes from a summary"
            )
            if expose_id in feeding:
                raise Expr2Error(
                    FLX_6208,
                    f"{what}, and its rows decide that summary: the column and its rows are in a "
                    "cycle",
                )
            raise Expr2Error(
                FLX_6210,
                f"{what}; this version reads a summary from the children's rows, not through a "
                "link",
            )


def _crossed(
    expose_id: str,
    paths: Sequence[Tuple[str, ...]],
    plans: Mapping[str, "_rows.GeneratorPlan"],
    keys: ContractKeys,
    related: Optional[Mapping[str, "_rows.GeneratorPlan"]],
    blame: Optional[Mapping[Tuple[str, ...], str]] = None,
) -> Set[str]:
    """Every expose the *paths* of *expose_id*'s rules cross, refusing a hop that cannot be read.

    With *related*, a hop must also go through a relationship that has a driver of its own.
    *blame* names the declaration a path of a copy comes from, so the refusal names it.
    """
    reached: Set[str] = set()
    for path in paths:
        current = expose_id
        for name in path:
            relationship = next(
                (r for r in keys.expose(current).relationships if r.name == name), None
            )
            where = f"a rule of {expose_id!r} reads {'.'.join(path)}"
            if blame and path in blame:
                where = f"{blame[path]}, which {expose_id!r} copies along {'.'.join(path)}"
            if relationship is None:  # pragma: no cover - the rule's text checked against the keys
                raise Expr2Error(FLX_6210, f"{where}: {current!r} has no relationship {name!r}")
            if relationship.target not in plans:
                raise Expr2Error(
                    FLX_6210,
                    f"{where}, which reaches {relationship.target!r}; this Simulation does not "
                    "generate that expose from statistics, so there is no row of it to read. Emit "
                    "it with a fidelity block in the same Simulation",
                )
            if related is not None:
                source = related[current]
                if _foreign_driver(source, name) is None and _composite_part(source, name) is None:
                    raise Expr2Error(
                        FLX_6210,
                        f"{where}, through relationship {name!r} of {current!r}, whose columns "
                        "another relationship sets; this version reads a link through the "
                        "relationship that sets them",
                    )
            reached.add(relationship.target)
            current = relationship.target
    return reached


def _reading_order(crossed: Mapping[str, Set[str]]) -> List[str]:
    """The reading exposes, each after the ones it reaches; ``FLX-6210`` for a cycle among them."""
    order: List[str] = []
    state: Dict[str, int] = {}  # 1 on the way down, 2 done

    def visit(expose_id: str, trail: List[str]) -> None:
        if state.get(expose_id) == 2:
            return
        if state.get(expose_id) == 1:
            cycle = [*trail[trail.index(expose_id) :], expose_id]
            raise Expr2Error(
                FLX_6210,
                "rules read through links in a cycle: "
                + " -> ".join(repr(name) for name in cycle)
                + "; generating a row would need a row of an expose that needs it in turn",
            )
        state[expose_id] = 1
        for target in sorted(crossed.get(expose_id, ())):
            visit(target, [*trail, expose_id])
        state[expose_id] = 2
        if expose_id in crossed:
            order.append(expose_id)

    for expose_id in sorted(crossed):
        visit(expose_id, [])
    return order


def _distinct_relationships(
    plan: "_rows.GeneratorPlan",
    relationships: Sequence[Relationship],
    key_sets: Set[FrozenSet[str]],
    plans: Mapping[str, "_rows.GeneratorPlan"],
    counts: Mapping[str, CountsEntry],
    hooked: Mapping[str, Tuple[ExpectedCountHook, Mapping[str, Any]]],
) -> FrozenSet[str]:
    """The relationships that reference each parent row at most once, refused when rows exceed it."""
    distinct = frozenset(
        relationship.name
        for relationship in relationships
        if relationship.cardinality == "one_to_one"
        or frozenset(relationship.from_columns) in key_sets
    )
    expose_id = plan.expose.expose_id
    for relationship in relationships:
        parent_rows = plans[relationship.target].rows
        if relationship.name in distinct and plan.rows > parent_rows:
            why = (
                "it is one_to_one"
                if relationship.cardinality == "one_to_one"
                else "its columns are a key of this expose"
            )
            counted = relationship.name in counts or relationship.name in hooked
            raise (TooManyChildRows if counted else FluxError)(
                FLX_3403,
                f"expose {expose_id!r} relationship {relationship.name!r} references each row "
                f"of {relationship.target!r} at most once ({why}), but declares {plan.rows} "
                f"rows against its {parent_rows}; declare no more rows than the parent",
            )
    return distinct


def _own_keys(
    expose_id: str,
    keys: ExposeKeys,
    relationships: Sequence[Relationship],
    declared: FrozenSet[_Declared],
) -> Set[Tuple[str, ...]]:
    """The columns a one_to_one relationship makes a key of the expose, and no other key holds."""
    # A one_to_one relationship makes its own columns a key of this expose, and a layout of counts
    # keeps them unique by giving no parent a second child; a key anything else declares stays one.
    declared_keys = {
        entity.column for entity in keys.entities if entity.kind in ("primary", "unique")
    }
    targeted_keys = {
        tuple(to_columns) for _s, _f, target, to_columns, _j in declared if target == expose_id
    }
    return (
        {
            tuple(relationship.from_columns)
            for relationship in relationships
            if relationship.cardinality == "one_to_one"
        }
        - {(column,) for column in declared_keys}
        - targeted_keys
    )


def _key_layouts(
    plan: "_rows.GeneratorPlan",
    key_tuples: Sequence[Tuple[str, ...]],
    owners: Mapping[str, Relationship],
    by_name: Mapping[str, "_rows.ColumnPlan"],
    stepped: FrozenSet[str],
    history: Optional[_HistoryShape],
) -> Dict[str, Union[KeyGrid, IdentifierKey]]:
    """The layout of each one-column key no relationship sets."""
    expose_id = plan.expose.expose_id
    layouts: Dict[str, Union[KeyGrid, IdentifierKey]] = {}
    natural = None if history is None else history.natural
    entities = None if history is None else history.pick.entities
    for key in key_tuples:
        if len(key) != 1:
            continue
        column = key[0]
        owner = owners.get(column)
        if owner is None:
            # A history's natural entity is laid out over its entities, not over its rows.
            layouts[column] = (
                _stepped_key(plan, by_name[column])
                if column in stepped
                else _one_column_layout(
                    plan, by_name[column], entities if column == natural else None
                )
            )
        elif owner.from_columns != key:
            others = [name for name in owner.from_columns if name != column]
            raise _refuse(
                f"expose {expose_id!r} column {column!r} is a key on its own, but "
                f"relationship {owner.name!r} sets it together with {others}; a key "
                "column takes its values from a relationship of its own, or from none"
            )
    return layouts


def _entity_keys(
    expose_id: str, keys: ExposeKeys, declared: FrozenSet[_Declared]
) -> FrozenSet[str]:
    """The key columns of the expose, and the columns another relationship points at."""
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
    return frozenset(entity_keys | targeted)


def _shape(
    plan: "_rows.GeneratorPlan",
    keys: ExposeKeys,
    plans: Mapping[str, "_rows.GeneratorPlan"],
    declared: FrozenSet[_Declared],
    bridges: Sequence[Tuple[str, str]] = (),
    weights: Mapping[str, str] = MappingProxyType({}),
    counts: Mapping[str, CountsEntry] = MappingProxyType({}),
    contract: Optional[Mapping[str, Any]] = None,
    hooked: Mapping[str, Tuple[ExpectedCountHook, Mapping[str, Any]]] = MappingProxyType({}),
    history: Optional[_HistoryShape] = None,
    periodic: bool = False,
) -> _Shape:
    if is_reference_plan(plan):
        # A reference expose's columns are all published; its keys are checked on the rows
        # (check_reference_exposes), and nothing in it is laid out or picked.
        return _Shape(
            plan=plan,
            layouts={},
            members={},
            owners={},
            distinct=frozenset(),
            absorbed=frozenset(),
        )
    expose_id = plan.expose.expose_id
    by_name = {column_plan.column.name: column_plan for column_plan in plan.columns}
    key_tuples = sorted(set(keys.keys) | set(keys.instant_keys), key=lambda key: (len(key), key))
    key_sets = {frozenset(key) for key in key_tuples}
    relationships = list(keys.relationships)

    for relationship in relationships:
        _check_relationship(plan, relationship, plans, by_name)
    distinct = _distinct_relationships(plan, relationships, key_sets, plans, counts, hooked)

    owners = _owners(expose_id, relationships, distinct, declared)
    on_counts = {name: True for name in counts}
    for name in hooked:
        on_counts.setdefault(name, False)
    _check_conditional_links(plan, relationships, key_tuples, distinct, owners, on_counts)
    _check_weights(
        plan, relationships, key_tuples, distinct, owners, plans, bridges, weights, on_counts
    )
    # A one_to_one relationship makes its own columns a key of this expose, and a layout of counts
    # keeps them unique by giving no parent a second child; a key anything else declares stays one.
    own_keys = _own_keys(expose_id, keys, relationships, declared)
    counted_keys = [key for key in key_tuples if key not in own_keys]
    conditions = _check_counts(
        plan,
        relationships,
        counted_keys,
        distinct,
        owners,
        bridges,
        weights,
        counts,
        (plans, contract),
        sorted(hooked),
    )
    if history is not None and history.parent is not None:
        conditions.update(
            _check_history_parent(
                plan,
                history,
                (relationships, counted_keys, distinct, owners, bridges, weights),
                (plans, contract),
            )
        )
    bridge_keys = _check_bridges(plan, relationships, key_tuples, distinct, owners, plans, bridges)
    if bridge_keys:
        key_tuples = sorted({*key_tuples, *bridge_keys}, key=lambda key: (len(key), key))
    members, absorbed = _composites(expose_id, key_tuples, owners, distinct)

    stepped = _check_periodic(plan, keys, by_name) if periodic else frozenset()
    if history is not None:
        _check_history_allocation(plan)
    layouts = _key_layouts(plan, key_tuples, owners, by_name, stepped, history)

    if history is not None:
        _lay_out_natural(plan, history, layouts, members, owners, by_name)
    shape = _Shape(
        plan=plan,
        layouts=layouts,
        members=members,
        owners=owners,
        distinct=distinct,
        absorbed=absorbed,
        bridges=bridge_keys,
        weights=weights,
        counts=counts,
        conditions=conditions,
        hooked=hooked,
        history=history,
        copied=_check_copied(
            plan,
            plans,
            relationships,
            key_tuples,
            owners,
            (distinct, absorbed, on_counts, weights, bridges, history),
        ),
    )
    _check_key_columns(
        shape, by_name, relationships, _entity_keys(expose_id, keys, declared), key_tuples
    )
    return shape


def _first_pick(
    shape: _Shape, relationship: Relationship, parent: ParentView, first: Any
) -> Tuple[str, Optional[Any], Optional[Union[KeyGrid, ReferenceColumn]]]:
    """How the parent is first picked: its pick, the key of a distinct layout, a declared grid."""
    plan = shape.plan
    if relationship.name in shape.distinct:
        return "distinct", _layout_key(plan, "rel", relationship.name), None
    if len(relationship.from_columns) == 1:
        target = parent.driver(relationship.to_columns[0])
        if isinstance(target, (KeyGrid, ReferenceColumn)) and _declares_draw(first):
            if isinstance(target, ReferenceColumn):
                _check_declared_keys(plan, first, relationship, target)
            return "declared", None, target
    return "uniform", None, None


def _check_copied(
    plan: "_rows.GeneratorPlan",
    plans: Mapping[str, "_rows.GeneratorPlan"],
    relationships: Sequence[Relationship],
    key_tuples: Sequence[Tuple[str, ...]],
    owners: Mapping[str, Relationship],
    layouts: Tuple[
        FrozenSet[str],
        FrozenSet[str],
        Mapping[str, bool],
        Mapping[str, str],
        Sequence[Tuple[str, str]],
        Optional[_HistoryShape],
    ],
) -> Dict[str, PickCopy]:
    """The copies that set a relationship's pick, each refused (``FLX-3403``) when it cannot (ADR 0022 section 4).

    A copy of a column into the foreign key of another relationship ``S`` makes ``S``'s pick
    follow the relationship it is copied through, so ``S`` may be nothing that picks or lays its
    parents out on its own: not one that references each parent once, is part of a key or a bridge,
    is counted, weighted or a history's parent, and its column declares no draw, ``nullFraction``,
    ``allocation`` or ``given``, and no rule sets it. The relationship it is copied through
    sets its pick on a draw of its own, with no conditional link that clears it and no rule that
    sets its columns, and its target is generated from statistics (not a reference expose); the
    source relationship of that target is read the same way. A cycle of such copies is
    ``FLX-6210``, found when the plans read each other.
    """
    rules = plan.rules
    found: Dict[str, PickCopy] = {}
    if rules is None or not rules.pick_copies:
        return found
    by_name = {relationship.name: relationship for relationship in relationships}
    for copy in rules.pick_copies:
        where = (
            f"expose {plan.expose.expose_id!r} copies {copy.column!r} through relationship "
            f"{copy.through!r} into relationship {copy.relationship!r}"
        )
        reasons = _copy_layout_reasons(copy, key_tuples, layouts) + _copy_column_reasons(
            copy, plan, by_name[copy.relationship], owners
        )
        if reasons:
            raise _refuse(f"{where}, but " + " and ".join(reasons) + "; a copy sets the pick alone")
        through = _copy_through(where, copy, by_name, owners)
        if _sets_columns(rules, copy.through, through.from_columns):
            raise _refuse(
                f"{where}, but a conditional link or a rule sets the columns of "
                f"{copy.through!r}, so the row it reaches is not the pick; drop it, or the copy"
            )
        reached = plans.get(copy.reach)
        if reached is None or is_reference_plan(reached):
            raise _refuse(
                f"{where}, but {copy.reach!r} is not generated from statistics with rows of its "
                "own to read a pick from"
            )
        _check_copy_givens(where, copy, (plan, through.from_columns), (reached, (copy.column,)))
        if _sets_columns(reached.rules, copy.source, (copy.column,)):
            raise _refuse(
                f"{where}, but a conditional link or a rule of {copy.reach!r} sets the columns of "
                f"{copy.source!r}, so the row it reaches is not the pick"
            )
        found[copy.relationship] = copy
    return found


def _copy_layout_reasons(
    copy: PickCopy,
    key_tuples: Sequence[Tuple[str, ...]],
    layouts: Tuple[
        FrozenSet[str],
        FrozenSet[str],
        Mapping[str, bool],
        Mapping[str, str],
        Sequence[Tuple[str, str]],
        Optional[_HistoryShape],
    ],
) -> List[str]:
    """What in the layout of the relationship a copy sets keeps it from setting the pick."""
    distinct, absorbed, counted, weights, bridges, history = layouts
    reasons: List[str] = []
    if copy.relationship in distinct:
        reasons.append("it references each parent row at most once")
    if copy.relationship in absorbed or any(copy.column in key for key in key_tuples):
        reasons.append(f"{copy.column!r} is part of a key")
    if copy.relationship in {name for pair in bridges for name in pair}:
        reasons.append("it is a side of a bridge")
    if copy.relationship in counted:
        reasons.append("it is on the counted layout")
    if copy.relationship in weights:
        reasons.append("a links entry weights it")
    if history is not None and (
        history.parent is not None and history.parent.relationship == copy.relationship
    ):
        reasons.append("it is the history's parent")
    return reasons


def _copy_column_reasons(
    copy: PickCopy,
    plan: "_rows.GeneratorPlan",
    mine: Relationship,
    owners: Mapping[str, Relationship],
) -> List[str]:
    """What the copied column declares, or another relationship or a rule sets, that a copy cannot keep."""
    assert plan.rules is not None
    first = plan.column_plan(copy.column)
    statistics = first.statistics
    reasons: List[str] = []
    if owners.get(copy.column) is not mine:
        reasons.append(f"another relationship sets {copy.column!r}")
    if statistics.null_fraction is not None:
        reasons.append(f"{copy.column!r} declares a nullFraction")
    if _declares_draw(first) or statistics.allocation is not None or statistics.parents:
        reasons.append(f"{copy.column!r} declares a draw, an allocation or a given")
    if copy.column in plan.rules.determined:
        reasons.append(f"a rule sets {copy.column!r}")
    return reasons


def _copy_through(
    where: str,
    copy: PickCopy,
    by_name: Mapping[str, Relationship],
    owners: Mapping[str, Relationship],
) -> Relationship:
    """The relationship a copy goes through, which sets its columns' pick on a draw of its own."""
    through = by_name.get(copy.through)
    if through is None or owners.get(through.from_columns[0]) is not through:
        raise _refuse(
            f"{where}, but another relationship sets the columns of {copy.through!r}, so it "
            "has no pick of its own to follow"
        )
    return through


def _sets_columns(rules: Optional[RulePlan], relationship: str, columns: Sequence[str]) -> bool:
    """Whether a conditional link on *relationship* or a rule of *rules* sets any of *columns*."""
    return rules is not None and (
        any(link.relationship == relationship for link in rules.links)
        or any(column in rules.determined for column in columns)
    )


def _check_copy_givens(
    where: str,
    copy: PickCopy,
    *sources: Tuple["_rows.GeneratorPlan", Sequence[str]],
) -> None:
    """Refuse a copy whose pick is another column's level or is read as one (neither source may be)."""
    for held_plan, columns in sources:
        given = _rows._given_columns(held_plan)
        for column in columns:
            if held_plan.column_plan(column).statistics.parents or column in given:
                raise _refuse(
                    f"{where}, but {column!r} of {held_plan.expose.expose_id!r} is drawn given "
                    "another column or another column is drawn given it, so its pick is not "
                    "read from the row alone; drop the given, or the copy"
                )


def _copied_parent(
    reader: "RelationReader", child: "_rows.GeneratorPlan", driver: ForeignKey, row: int
) -> Optional[int]:
    """The parent row a ``copied`` pick takes: the row its source relationship reaches.

    The row ``through`` reaches at *row* is read as a link reads it, and the row the source
    relationship of that row's expose reaches there is this relationship's parent. ``None``
    when either reaches no row. A pure function of the two plans and the seed tree.
    """
    copied = driver.copied
    assert copied is not None  # relate_plans sets it for this pick
    held = reader.parent_row(child, copied.through, row)
    if held is None:
        return None
    reached = next(read for expose_id, read in child.reads if expose_id == copied.reach)
    return reader.parent_row(reached, copied.source, held)


def _has_no_draw(driver: ForeignKey) -> FluxError:
    """A dependency cannot draw the key of a relationship that a layout or a copy sets (``FLX-3403``)."""
    if driver.copied is not None:
        return _refuse(
            f"relationship {driver.relationship!r} takes its parent from a copy through "
            f"{driver.copied.through!r}, so a dependency cannot draw its key"
        )
    return _refuse(
        f"relationship {driver.relationship!r} is on a counted layout, which no draw "
        "makes, so a dependency cannot draw its key; let the layout set it"
    )


def _check_copy_source(held: Optional[Driver], expose_id: str, copied: PickCopy) -> None:
    """Refuse (``FLX-3403``) a copy whose source relationship has no pick of its own to follow.

    A copy follows the pick of the relationship of ``reach`` that holds the copied column as
    its foreign key (:class:`~flux_engine.generator.rules.PickCopy`), so that relationship
    needs a foreign key or a part of a key it is laid out as in ``reach``'s plan.
    """
    if isinstance(held, CompositeMember) or (
        isinstance(held, ForeignKey) and held.relationship == copied.source
    ):
        return
    raise _refuse(
        f"expose {expose_id!r} copies {copied.column!r} through relationship "
        f"{copied.through!r} into relationship {copied.relationship!r}, but relationship "
        f"{copied.source!r} of {copied.reach!r} has no foreign key of its own to follow "
        f"({copied.column!r} is laid out another way there)"
    )


def _projected_columns(plan: "_rows.GeneratorPlan") -> FrozenSet[str]:
    """The columns of a snapshot, which are its history's last versions (ADR 0027 section 3): none is drawn."""
    if not plan.projected:
        return frozenset()
    return frozenset(column_plan.column.name for column_plan in plan.columns)


def _refuse_on_counts(
    expose_id: str, relationship: str, on_counts: Mapping[str, bool], gives: str, why: str
) -> None:
    """Refuse a ``links`` entry on a counted relationship; a relationship that is not counted passes.

    *on_counts* holds each counted relationship of the expose, mapped to whether it has a
    ``counts`` entry; one a hook counts has none, so the message names the expected count. One
    refusal serves a ``when`` and a ``weight``, ahead of every advice that reads a relationship
    that is not counted (``FLX-3403``, ADR 0016 section 1).
    """
    if relationship not in on_counts:
        return
    layout = "its counts entry" if on_counts[relationship] else "the expected count a hook declares"
    raise _refuse(
        f"expose {expose_id!r} relationship {relationship!r} is on the counted layout ({layout}), "
        f"so its links entry cannot give it a {gives}; {why}"
    )


def _check_history_parent(
    plan: "_rows.GeneratorPlan",
    history: _HistoryShape,
    held: Tuple[
        Sequence[Relationship],
        Sequence[Tuple[str, ...]],
        FrozenSet[str],
        Mapping[str, Relationship],
        Sequence[Tuple[str, str]],
        Mapping[str, str],
    ],
    reading: Tuple[Mapping[str, "_rows.GeneratorPlan"], Optional[Mapping[str, Any]]],
) -> Dict[str, ParentCondition]:
    """Refuse a history ``parent`` the entity layout cannot place, as a ``counts`` entry is (``FLX-3403``).

    The entities are the children of the parent rows, so the plan is read with the entities for
    its rows: ``minChildren`` is checked against ``E``, and the relationship must be one that
    takes a parent row from nothing but its own address (:func:`_check_counts`).
    """
    assert history.parent is not None
    relationships, key_tuples, distinct, owners, bridges, weights = held
    name = history.parent.relationship
    return _check_counts(
        replace(plan, rows=history.pick.entities),
        relationships,
        key_tuples,
        distinct,
        owners,
        bridges,
        weights,
        {name: history.parent},
        reading,
        [name] if history.hook is not None else [],
    )


def _lay_out_natural(
    plan: "_rows.GeneratorPlan",
    history: _HistoryShape,
    layouts: Dict[str, Union[KeyGrid, IdentifierKey]],
    members: Mapping[str, Tuple[str, ...]],
    owners: Mapping[str, Relationship],
    by_name: Mapping[str, "_rows.ColumnPlan"],
) -> None:
    """Lay the natural entity's column out over the entities (ADR 0017 section 3).

    It is its own key: a column that a several-column key or a relationship also sets would have
    two sources, so it is refused (``FLX-3403``). A column already laid out as a key of the
    contract is the same layout over the entities.
    """
    natural = history.natural
    expose_id = plan.expose.expose_id
    if natural in members or natural in owners:
        raise _refuse(
            f"history ({expose_id}): the natural entity {natural!r} is part of a several-column "
            "key or set by a relationship, and a history lays it out over its entities on its "
            "own; give it a key of its own"
        )
    if natural not in layouts:
        layouts[natural] = _one_column_layout(plan, by_name[natural], history.pick.entities)


def _check_conditional_links(
    plan: "_rows.GeneratorPlan",
    relationships: Sequence[Relationship],
    key_tuples: Sequence[Tuple[str, ...]],
    distinct: FrozenSet[str],
    owners: Mapping[str, Relationship],
    on_counts: Mapping[str, bool] = MappingProxyType({}),
) -> None:
    """Refuse a condition on a link it cannot clear (``FLX-3403``, ADR 0022 section 5).

    A condition clears the relationship's columns where it is not TRUE. That is refused on an
    ``inner`` relationship, which references a parent on every row, on one whose columns are a key
    of the expose or part of one (a ``distinct`` pick, a member of a several-column key), whose
    rows could not then hold the key, on one another relationship implies, whose columns that
    relationship sets, and on one that implies another, whose link clearing it would clear too.
    """
    rules = plan.rules
    if rules is None or not rules.links:
        return
    expose_id = plan.expose.expose_id
    by_name = {relationship.name: relationship for relationship in relationships}
    key_columns = {column for key in key_tuples for column in key}
    for link in rules.links:
        relationship = by_name.get(link.relationship)
        if relationship is None:
            continue
        _refuse_on_counts(
            expose_id,
            relationship.name,
            on_counts,
            "when",
            "a condition that clears a child's foreign key would drop its parent below its "
            "allotted count",
        )
        _check_conditional_link(
            expose_id, relationship, relationships, distinct, key_columns, owners
        )


def _check_conditional_link(
    expose_id: str,
    relationship: Relationship,
    relationships: Sequence[Relationship],
    distinct: FrozenSet[str],
    key_columns: Set[str],
    owners: Mapping[str, Relationship],
) -> None:
    """Refuse a condition on one relationship that is not counted, where it could not clear it."""
    where = f"expose {expose_id!r} relationship {relationship.name!r} has a condition"
    if relationship.join_type == "inner":
        raise _refuse(
            f"{where}, but its joinType is inner, so every row references a parent; make "
            "the joinType left"
        )
    if relationship.name in distinct or key_columns & set(relationship.from_columns):
        raise _refuse(
            f"{where}, but its columns are a key of the expose or part of one, so every row "
            "holds them"
        )
    if owners.get(relationship.from_columns[0]) is not relationship:
        holder = owners[relationship.from_columns[0]].name
        raise _refuse(
            f"{where}, but relationship {holder!r} sets its columns and implies it; put "
            f"the condition on {holder!r}"
        )
    inside = sorted(
        other.name
        for other in relationships
        if other is not relationship and set(other.from_columns) <= set(relationship.from_columns)
    )
    if inside:
        raise _refuse(
            f"{where}, but its columns hold those of relationship {inside[0]!r}, which it "
            "implies, so clearing them would clear that link too; this version conditions a "
            "relationship that implies no other"
        )


def _check_bridged(
    plans: Mapping[str, "_rows.GeneratorPlan"], bridges: Mapping[str, Sequence[Tuple[str, str]]]
) -> None:
    """Refuse a bridge on kernel 0.1.0 (``FLX-3105``) and one on an expose that has no plan (``FLX-3403``)."""
    for expose_id in sorted(bridges):
        if not bridges[expose_id]:
            continue
        plan = plans.get(expose_id)
        if plan is None:
            raise _refuse(
                f"expose {expose_id!r} declares a bridge, but the Simulation does not generate "
                "it from statistics"
            )
        if not plan.counter_based:
            from flux_engine.seam.resolve import FLX_3105

            raise FluxError(
                FLX_3105,
                f"expose {expose_id!r} declares a bridge, which needs kernel 0.2.0; this run is "
                "kernel 0.1.0",
            )


def _check_weighted(
    plans: Mapping[str, "_rows.GeneratorPlan"], weights: Mapping[str, Mapping[str, str]]
) -> None:
    """Refuse a weight on kernel 0.1.0 (``FLX-3105``), on an expose with no plan or a reference one (``FLX-3403``)."""
    for expose_id in sorted(weights):
        if not weights[expose_id]:
            continue
        plan = plans.get(expose_id)
        if plan is None:
            raise _refuse(
                f"expose {expose_id!r} weights a relationship, but the Simulation does not "
                "generate it from statistics"
            )
        if not plan.counter_based:
            from flux_engine.seam.resolve import FLX_3105

            raise FluxError(
                FLX_3105,
                f"expose {expose_id!r} weights a relationship, which needs kernel 0.2.0; this run "
                "is kernel 0.1.0",
            )
        if is_reference_plan(plan):
            raise _refuse(
                f"expose {expose_id!r} is a reference expose, released whole, and picks no parent"
            )


def _check_weights(
    plan: "_rows.GeneratorPlan",
    relationships: Sequence[Relationship],
    key_tuples: Sequence[Tuple[str, ...]],
    distinct: FrozenSet[str],
    owners: Mapping[str, Relationship],
    plans: Mapping[str, "_rows.GeneratorPlan"],
    bridges: Sequence[Tuple[str, str]],
    weights: Mapping[str, str],
    on_counts: Mapping[str, bool] = MappingProxyType({}),
) -> None:
    """Refuse a weight on a relationship that cannot pick its parents by one (``FLX-3403``, ADR 0023 section 4).

    The weight names a number column of the relationship's target. The relationship must be one
    that draws a parent row on a stream of its own: not ``distinct`` (a keyed permutation), not
    part of a key or of a bridge, not implied by another relationship, its first column drawn
    given no other column and set by no dependency. A reference parent's published weights are
    read here: a negative one, or all zero, is refused before any row exists.
    """
    expose_id = plan.expose.expose_id
    by_name = {relationship.name: relationship for relationship in relationships}
    key_columns = {column for key in key_tuples for column in key}
    bridged = {name for pair in bridges for name in pair}
    for name in sorted(weights):
        column = weights[name]
        where = f"expose {expose_id!r} relationship {name!r} is weighted by {column!r}"
        relationship = by_name.get(name)
        if relationship is None:
            raise _refuse(f"{where}, and the expose declares no relationship {name!r}")
        _refuse_on_counts(
            expose_id,
            name,
            on_counts,
            "weight",
            "a weight picks each child's parent on a second layout, which would drop a parent "
            "below its allotted count",
        )
        first = plan.column_plan(relationship.from_columns[0])
        if name in distinct:
            raise _refuse(
                f"{where}, but it references each parent row at most once (it is one_to_one, or "
                "its columns are a key), so no weight picks its parents"
            )
        if name in bridged:
            raise _refuse(f"{where}, but it is a side of a bridge, which has no weight")
        if key_columns & set(relationship.from_columns):
            raise _refuse(f"{where}, but its columns are part of a key of the expose")
        if owners.get(relationship.from_columns[0]) is not relationship:
            holder = owners[relationship.from_columns[0]].name
            raise _refuse(
                f"{where}, but relationship {holder!r} sets its columns and implies it; weight "
                f"{holder!r}"
            )
        if first.statistics.parents or first.column.name in _rows._given_columns(plan):
            raise _refuse(
                f"{where}, but its first column {first.column.name!r} is drawn given another "
                "column, or another is given it; a weight picks the parent on its own"
            )
        if plan.rules is not None and any(
            set(relationship.from_columns) & set(rule.foreign_columns) for rule in plan.rules.rules
        ):
            raise _refuse(f"{where}, but a dependency sets its columns")
        target = plans[relationship.target]
        weight = next((c for c in target.columns if c.column.name == column), None)
        if weight is None:
            raise _refuse(
                f"{where}, and {relationship.target!r} has no column {column!r} "
                f"(it has {', '.join(c.column.name for c in target.columns)})"
            )
        kind = weight.column.logical_type.kind
        if kind not in (LogicalKind.INT, LogicalKind.DECIMAL):
            raise _refuse(
                f"{where}, but {relationship.target}.{column} is {weight.column.type_name}, "
                "and a weight is a number column (INT or DECIMAL)"
            )
        if is_reference_plan(target):
            assert target.relations is not None  # a reference plan
            driver = target.relations.driver(column)
            assert isinstance(driver, ReferenceColumn)  # every column of one is published
            weight_thresholds(driver.values, f"{where}: the published column {column!r}")


def _member_of(
    hook: ExpectedCountHook,
    namespace: Mapping[str, Any],
    plan: "_rows.GeneratorPlan",
    relationship: Any,
) -> Optional[Mapping[str, Any]]:
    """*hook*'s member for *relationship*, told the parent expose when the hook keeps its state by it (``member_to``)."""
    keyed = getattr(hook, "member_to", None)
    if keyed is None:
        return hook.member(namespace, plan, relationship.name)
    found: Optional[Mapping[str, Any]] = keyed(
        namespace, plan, relationship.name, relationship.target
    )
    return found


def _refuse_shared_key(
    hooks: Sequence[ExpectedCountHook],
    members: Sequence[Tuple[ExpectedCountHook, Mapping[str, Any]]],
    keyed: Dict[Tuple[int, str, str], str],
    expose_id: str,
    relationship: Any,
) -> None:
    """``FLX-3403`` when two exposes bind a keyed hook through one relationship name to one parent.

    A keyed hook counts a parent's children by ``(simulation, relationship, parent)`` alone, so two
    child exposes that declare it through same-named relationships to one parent could not be told
    apart. *keyed* holds the first such expose of each key.
    """
    for hook, _ in members:
        if not hasattr(hook, "member_to"):
            continue
        key = (hooks.index(hook), relationship.name, relationship.target)
        if key in keyed:
            raise _refuse(
                f"exposes {keyed[key]!r} and {expose_id!r} both declare an expected count through "
                f"relationship {relationship.name!r} to {relationship.target!r}; the parent's "
                "counts cannot tell the two apart, so at most one declares it"
            )
        keyed[key] = expose_id


def _hooked(
    plans: Mapping[str, "_rows.GeneratorPlan"], keys: ContractKeys, namespace: Mapping[str, Any]
) -> Dict[str, Dict[str, Tuple[ExpectedCountHook, Mapping[str, Any]]]]:
    """The member each registered hook binds, by exposeId and relationship (ADR 0016 section 3).

    Every hook is asked about every relationship an expose declares; two that both return a
    member for one relationship are ``FLX-3403``. With no hook registered nothing is asked.
    """
    hooks = expected_count_hooks()
    found: Dict[str, Dict[str, Tuple[ExpectedCountHook, Mapping[str, Any]]]] = {}
    if not hooks:
        return found
    keyed: Dict[Tuple[int, str, str], str] = {}
    for expose_id in sorted(plans):
        for relationship in keys.expose(expose_id).relationships:
            members = [
                (hook, member)
                for hook in hooks
                if (member := _member_of(hook, namespace, plans[expose_id], relationship))
                is not None
            ]
            _refuse_shared_key(hooks, members, keyed, expose_id, relationship)
            if len(members) > 1:
                names = sorted(type(hook).__name__ for hook, _ in members)
                raise _refuse(
                    f"expose {expose_id!r} relationship {relationship.name!r} has an expected "
                    f"count from more than one hook ({names}); at most one declares it"
                )
            if members:
                found.setdefault(expose_id, {})[relationship.name] = members[0]
    return found


def _eligible_rows(
    chunk: Sequence[Tuple[Any, ...]], start: int, holds: Optional[Sequence[bool]]
) -> Iterator[Tuple[int, Tuple[Any, ...]]]:
    """The rows of a chunk that begins at row *start* on which the condition holds, with their numbers."""
    for offset, final_row in enumerate(chunk):
        if holds is None or holds[offset]:
            yield start + offset, final_row


def _given_members(
    entry: Optional[CountsEntry],
    given: Optional[Tuple[Tuple[Tuple[str, Tuple[str, ...]], ...], Tuple[str, ...]]],
) -> Dict[str, Any]:
    """The ``given`` members of a counted layout: the column, every class's weights and the defaulted ones."""
    return {
        "given": None if entry is None else entry.given,
        "children_given": None if given is None else given[0],
        "given_default": () if given is None else given[1],
    }


def _check_counted(
    plans: Mapping[str, "_rows.GeneratorPlan"],
    counts: Mapping[str, Mapping[str, CountsEntry]],
    hooked: Mapping[str, Mapping[str, Any]] = MappingProxyType({}),
) -> None:
    """Refuse a counts entry or a hook's member on kernel 0.1.0 (``FLX-3105``), on an expose with no plan or a reference one (``FLX-3403``)."""
    for expose_id in sorted(set(counts) | set(hooked)):
        if not counts.get(expose_id) and not hooked.get(expose_id):
            continue
        plan = plans.get(expose_id)
        if plan is None:
            raise _refuse(
                f"expose {expose_id!r} declares counts or an expected count, but the Simulation "
                "does not generate it "
                "from statistics"
            )
        if not plan.counter_based:
            from flux_engine.seam.resolve import FLX_3105

            raise FluxError(
                FLX_3105,
                f"expose {expose_id!r} declares counts or an expected count, which need kernel "
                "0.2.0; this run is kernel 0.1.0",
            )
        if is_reference_plan(plan):
            raise _refuse(
                f"expose {expose_id!r} is a reference expose, released whole, and picks no parent"
            )


def _check_counts(
    plan: "_rows.GeneratorPlan",
    relationships: Sequence[Relationship],
    key_tuples: Sequence[Tuple[str, ...]],
    distinct: FrozenSet[str],
    owners: Mapping[str, Relationship],
    bridges: Sequence[Tuple[str, str]],
    weights: Mapping[str, str],
    counts: Mapping[str, CountsEntry],
    reading: Tuple[Mapping[str, "_rows.GeneratorPlan"], Optional[Mapping[str, Any]]],
    hooked: Sequence[str] = (),
) -> Dict[str, ParentCondition]:
    """Refuse a counts entry on a relationship the layout cannot place (``FLX-3403``, ADR 0016 section 1).

    The layout sets the relationship's columns, so the relationship must be one that takes a parent
    row from nothing but its own address: not ``distinct``, not part of a key or of a bridge, not
    implied by another relationship, not a self-relationship, its first column drawing nothing
    (no ``nullFraction``, no draw, no ``given``), and no condition, weight or dependency that
    would clear or set its columns after the layout placed them. The shape is read here, so a
    shape that cannot be laid out is refused before any row exists. The checked ``parentWhen`` of
    each entry that declares one comes back by relationship; *reading* is the plans and the contract
    document it is checked against.
    """
    plans, contract = reading
    conditions: Dict[str, ParentCondition] = {}
    expose_id = plan.expose.expose_id
    by_name = {relationship.name: relationship for relationship in relationships}
    key_columns = {column for key in key_tuples for column in key}
    bridged = {name for pair in bridges for name in pair}
    conditioned = (
        {link.relationship for link in plan.rules.links} if plan.rules is not None else set()
    )
    determined = (
        {
            column
            for rule in plan.rules.rules
            if not rule.by_layout
            for column in rule.foreign_columns
        }
        if plan.rules is not None
        else set()
    )
    for name in sorted({*counts, *hooked}):
        where = f"expose {expose_id!r} relationship {name!r} is counted"
        relationship = by_name.get(name)
        if relationship is None:
            raise _refuse(f"{where}, and the expose declares no relationship {name!r}")
        reason = _uncountable(
            plan,
            relationship,
            owners,
            dict(
                # A one_to_one relationship is laid out with no parent over one child; the
                # columns of a key are refused as keyed below.
                distinct=name in distinct and relationship.cardinality != "one_to_one",
                bridged=name in bridged,
                keyed=bool(key_columns & set(relationship.from_columns)),
                conditioned=name in conditioned,
                weighted=name in weights,
                determined=bool(determined & set(relationship.from_columns)),
            ),
        )
        if reason is not None:
            raise _refuse(f"{where}, but {reason}")
        parent = plans[relationship.target]
        # A relationship only a hook counts has the entry that declares nothing.
        entry = counts.get(name) or CountsEntry(emit=expose_id, relationship=name)
        if entry.parent_when is not None:
            conditions[name] = _check_parent_when(plan, relationship, entry, parent, contract)
        _check_given(plan, relationship, entry, parent)
        if relationship.cardinality == "one_to_one":
            _check_one_to_one(plan, relationship, entry)
        _check_minimum(plan, relationship, entry, parent.rows)
    return conditions


def _check_parent_when(
    plan: "_rows.GeneratorPlan",
    relationship: Relationship,
    entry: CountsEntry,
    parent: "_rows.GeneratorPlan",
    contract: Optional[Mapping[str, Any]],
) -> ParentCondition:
    """The ``parentWhen`` of *entry* checked against the parent's own columns (``FLX-3403`` otherwise).

    It reads the parent's own columns and nothing beyond its row, and none a summary over the
    parent's children sets, since those hold nothing until the children are laid out.
    """
    where = f"expose {plan.expose.expose_id!r} relationship {relationship.name!r} has a parentWhen"
    if contract is None:
        raise _refuse(f"{where}, which is checked against the contract: pass contract=")
    assert entry.parent_when is not None
    try:
        condition, reaches = parent_condition(contract, relationship.target, entry.parent_when)
    except Expr2Error as error:
        # The text's own refusal, with the key it came from in front of it.
        raise Expr2Error(
            error.code,
            f"counts[{plan.expose.expose_id}.{relationship.name}].parentWhen "
            f"{entry.parent_when!r}: " + error.detail.rsplit(" (at offset", 1)[0],
            position=error.position,
        ) from None
    if reaches:
        raise _refuse(
            f"{where} that reads beyond the row of {relationship.target!r} (a link, previous, "
            "children, siblings or a window); parentWhen reads the parent's own columns"
        )
    summed = parent.rules.summarised if parent.rules is not None else frozenset()
    if summed & set(condition.reads):
        raise _refuse(
            f"{where} that reads {', '.join(sorted(summed & set(condition.reads)))} of "
            f"{relationship.target!r}, which a summary over its children sets, and the children "
            "wait on the condition"
        )
    return condition


def _given_classes(parent: "_rows.GeneratorPlan", column: str) -> Tuple[str, ...]:
    """The classes of the parent's column a ``given`` names: its levels sorted, or its bins in order.

    A bin is named by the text of its lower edge as the column's ``bins`` write it
    (ADR 0036 decision 3).
    """
    held = parent.column_plan(column)
    names = _rows.class_names(held)
    return names if _rows.is_binned(held) else tuple(sorted(names))


def _check_given(
    plan: "_rows.GeneratorPlan",
    relationship: Relationship,
    entry: CountsEntry,
    parent: "_rows.GeneratorPlan",
) -> None:
    """Refuse a ``given`` and ``childrenGiven`` the layout cannot read (``FLX-3403``, ADR 0037 decision 4).

    ``given`` is a ``category`` column of the parent with declared levels and no ``nullFraction``,
    or a column drawn from bins with none; ``children`` comes with it; each key of
    ``childrenGiven`` is a class of the column and each list holds one weight for each class of
    ``children`` and sums to 1. As for a ``parentWhen``, the column may not read a link, a previous
    version, children or siblings, nor be set by a summary over the parent's children, since the
    children wait on the parent's final rows.
    """
    where = f"expose {plan.expose.expose_id!r} relationship {relationship.name!r}"
    if entry.given is None:
        if entry.children_given is not None:
            raise _refuse(f"{where} has childrenGiven and no given column to read it by")
        return
    if entry.children is None:
        raise _refuse(f"{where} has a given column and no children shape for its classes to weigh")
    column = entry.given
    names = {item.column.name: item for item in parent.columns}
    held = names.get(column)
    if held is None:
        raise _refuse(
            f"{where} is given {column!r}, which {relationship.target!r} does not have as a column"
        )
    _check_given_column(where, relationship.target, parent, held)
    classes = _given_classes(parent, column)
    shape = entry.children.as_member()
    base = classes_of(shape, f"{where} children")
    keyed = dict(entry.children_given or ())
    for key in keyed:
        if key not in classes:
            raise _refuse(
                f"{where} childrenGiven names the class {key!r}, which {column!r} of "
                f"{relationship.target!r} does not have (its classes: {list(classes)})"
            )
    names_key = "binWeights" if entry.children.kind == "bins" else "levelWeights"
    for key, weights in sorted(keyed.items()):
        if not weights:
            raise _refuse(f"{where} childrenGiven[{key}] has no weight")
        if len(weights) != len(base):
            raise _refuse(
                f"{where} childrenGiven[{key}] has {len(weights)} weights; children has "
                f"{len(base)} classes"
            )
        classes_of({**shape, names_key: list(weights)}, f"{where} childrenGiven[{key}]")


def _check_given_column(
    where: str, target: str, parent: "_rows.GeneratorPlan", held: "_rows.ColumnPlan"
) -> None:
    """Refuse a given column that is not a class to read from a final row of *parent* (``FLX-3403``)."""
    column = held.column.name
    binned = _rows.is_binned(held)
    if not binned and (held.family is not Family.CATEGORICAL or not _rows.class_names(held)):
        raise _refuse(
            f"{where} is given {column!r}, which is no category column with declared levels "
            f"nor a column drawn from bins in {target!r}"
        )
    if held.statistics.null_fraction is not None:
        raise _refuse(
            f"{where} is given {column!r}, which declares a nullFraction, and a null has no "
            "class to weigh the children by"
        )
    rules = parent.rules
    if held.statistics.given_link is not None:
        raise _refuse(
            f"{where} is given {column!r}, which is drawn given a column of a linked row; a "
            "given column reads the parent's own row"
        )
    if rules is None:
        return
    if column in rules.summarised:
        raise _refuse(
            f"{where} is given {column!r}, which a summary over the children of {target!r} "
            "sets, and the children wait on the given column"
        )
    for rule in rules.rules:
        if column not in rule.determines:
            continue
        if binned:
            raise _refuse(
                f"{where} is given {column!r}, which a rule sets; a column drawn from bins is "
                "given as it is drawn"
            )
        if rule.paths or rule.previous or rule.windowed or rule.summary is not None:
            raise _refuse(
                f"{where} is given {column!r}, which a rule sets from a link, a previous "
                "version, children or siblings; a given column reads the parent's own row"
            )


def _class_shapes(counted: CountedPick) -> Optional[List[Dict[str, Sequence[str]]]]:
    """The ``children`` mapping of each class of the given column, each with its own weights."""
    if counted.given is None or counted.children is None or counted.children_given is None:
        return None
    names = (
        ("levels", "levelWeights") if counted.children.kind == "levels" else ("bins", "binWeights")
    )
    return [
        {names[0]: list(counted.children.values), names[1]: list(weights)}
        for _, weights in counted.children_given
    ]


def _given_classifier(
    parent: "_rows.GeneratorPlan", counted: CountedPick
) -> Callable[[Any, int], int]:
    """A function from the given column's final value on a parent row to its class's index.

    The index is the position of the class in ``counted.children_given``. A column drawn from bins
    gives the bin its value falls in; a category column gives the level that renders to the value
    (a level ``"1.50"`` and the value it renders to are different keys). A value that is no
    class, a NULL included, is ``FLX-3403``.
    """
    assert counted.given is not None and counted.children_given is not None
    held = parent.column_plan(counted.given)
    index = {name: position for position, (name, _) in enumerate(counted.children_given)}
    where = f"parent {parent.expose.expose_id!r} column {counted.given!r}"
    if _rows.is_binned(held):

        def binned(value: Any, row: int) -> int:
            try:
                return index[_rows.bin_class(held, value)]
            except (FluxError, KeyError):
                raise _refuse(f"{where} has a value on row {row} that falls in no bin") from None

        return binned
    rendered = {
        _hashable(_rows._level_to_value(name, held.column)): position
        for name, position in index.items()
    }

    def leveled(value: Any, row: int) -> int:
        found = rendered.get(_hashable(value))
        if found is None:
            raise _refuse(f"{where} has a value on row {row} that is none of its levels")
        return found

    return leveled


def _hashable(value: Any) -> Any:
    """*value* as a key that tells a BOOL from an INT (``True == 1`` would not)."""
    return (type(value).__name__, value)


def given_children(
    entry: CountsEntry, parent: "_rows.GeneratorPlan"
) -> Tuple[Tuple[Tuple[str, Tuple[str, ...]], ...], Tuple[str, ...]]:
    """The weights of every class of the given column, and the classes that took the shape's own.

    Sorted by class as :func:`_given_classes` gives them: a class the entry gave no list holds
    ``children``'s weights, which the plan states (ADR 0037 decision 4).
    """
    assert entry.given is not None and entry.children is not None
    held = dict(entry.children_given or ())
    classes = _given_classes(parent, entry.given)
    fallback = entry.children.weights
    return (
        tuple((name, tuple(held.get(name, fallback))) for name in classes),
        tuple(name for name in classes if name not in held),
    )


def _check_minimum(
    plan: "_rows.GeneratorPlan", relationship: Relationship, entry: CountsEntry, parents: int
) -> None:
    """Read the shape and refuse a minimum the shape or the child rows cannot meet (``FLX-3403``).

    Without a ``parentWhen`` every parent is eligible, so ``e`` is the parent's rows and
    ``N - m * e < 0`` is known here; with one, it is refused when the layout is first worked out.
    """
    where = f"expose {plan.expose.expose_id!r} relationship {relationship.name!r}"
    minimum = entry.min_children or 0
    if entry.children is not None:
        classes = classes_of(entry.children.as_member(), f"{where} children")
        if minimum > classes[-1].high:
            raise _refuse(
                f"{where} has minChildren {minimum}, above the most children its shape allows "
                f"({classes[-1].high}); lower minChildren or raise the shape's top count"
            )
    if entry.parent_when is None:
        try:
            check_minimum(plan.rows, parents, minimum)
        except TooFewChildRows as short:
            short.child, short.parent = plan.expose.expose_id, relationship.target
            raise


def _check_one_to_one(
    plan: "_rows.GeneratorPlan", relationship: Relationship, entry: CountsEntry
) -> None:
    """Refuse a ``one_to_one`` entry that asks a parent for more than one child (``FLX-3403``).

    A ``children`` shape that allows a count above one, and a ``minChildren`` above one. That the
    child rows are no more than the parent's is checked with the rows of any ``one_to_one``
    relationship, and, with a ``parentWhen``, that they are no more than the eligible parents when
    the layout is first worked out.
    """
    where = f"expose {plan.expose.expose_id!r} relationship {relationship.name!r} is one_to_one"
    if entry.children is not None:
        classes = classes_of(entry.children.as_member(), f"{where}, and its children")
        if classes[-1].high > 1:
            raise _refuse(
                f"{where}, so no parent has more than one child, but its children shape allows "
                f"{classes[-1].high}; keep every level and bin at 0 or 1"
            )
    if (entry.min_children or 0) > 1:
        raise _refuse(
            f"{where}, so no parent has more than one child, but its minChildren is "
            f"{entry.min_children}; use 0 or 1"
        )


def _uncountable(
    plan: "_rows.GeneratorPlan",
    relationship: Relationship,
    owners: Mapping[str, Relationship],
    held: Mapping[str, bool],
) -> Optional[str]:
    """Why *relationship* cannot be laid out by counts, or ``None`` when it can.

    *held* says which of the declared things the relationship is: ``distinct``, ``bridged`` (a
    bridge side), ``keyed`` (part of a key), ``conditioned`` (a ``links`` condition),
    ``weighted`` (a ``links`` weight) and ``determined`` (a dependency sets its columns).
    """
    first = plan.column_plan(relationship.from_columns[0])
    if held["distinct"]:
        return "its columns are a key of the expose, which a layout of counts would not keep unique"
    if held["bridged"]:
        return "it is a side of a bridge, whose pairs are laid out together"
    if held["keyed"]:
        return "its columns are part of a key of the expose"
    if owners.get(relationship.from_columns[0]) is not relationship:
        holder = owners[relationship.from_columns[0]].name
        return f"relationship {holder!r} sets its columns and implies it; count {holder!r}"
    if relationship.target == plan.expose.expose_id:
        return "it points at its own expose, whose rows are the ones being laid out"
    if first.statistics.null_fraction is not None:
        return (
            f"its first column {first.column.name!r} declares a nullFraction, and the layout "
            "gives every child a parent; drop it"
        )
    if (
        _declares_draw(first)
        or first.statistics.parents
        or first.column.name in _rows._given_columns(plan)
    ):
        return (
            f"its first column {first.column.name!r} declares a draw of its own (levels, bins, a "
            "mean, a rate or given), and the layout sets it; drop the declaration"
        )
    if held["conditioned"]:
        return (
            "a links entry gives it a condition (when), which would clear keys the layout "
            "allotted and break the counts; drop the condition"
        )
    if held["weighted"]:
        return (
            "a links entry weights it, and a weight is a second layout of the same children; drop "
            "the weight"
        )
    if held["determined"]:
        return "a dependency sets its columns, which the layout sets"
    return None


def _check_bridges(
    plan: "_rows.GeneratorPlan",
    relationships: Sequence[Relationship],
    key_tuples: Sequence[Tuple[str, ...]],
    distinct: FrozenSet[str],
    owners: Mapping[str, Relationship],
    plans: Mapping[str, "_rows.GeneratorPlan"],
    bridges: Sequence[Tuple[str, str]],
) -> Dict[Tuple[str, ...], Tuple[str, str]]:
    """The several-column key each bridge lays out, or the refusal it earns (``FLX-3403``, ADR 0022 section 8).

    A bridge's two relationships are one key of two parts, *a*'s columns then *b*'s. Refused: a
    relationship that is in two bridges or does not exist, a bridge with more rows than pairs,
    a ``nullFraction`` on either relationship's first column, a relationship that is
    ``one_to_one``, whose columns are a key, that is already part of a key or that another
    relationship implies, and one a ``links`` condition configures.
    """
    expose_id = plan.expose.expose_id
    by_name = {relationship.name: relationship for relationship in relationships}
    by_column = {column_plan.column.name: column_plan for column_plan in plan.columns}
    key_columns = {column for key in key_tuples for column in key}
    conditioned = (
        {link.relationship for link in plan.rules.links} if plan.rules is not None else set()
    )
    used: Dict[str, Tuple[str, str]] = {}
    found: Dict[Tuple[str, ...], Tuple[str, str]] = {}
    for pair in bridges:
        where = f"expose {expose_id!r} bridge ({', '.join(pair)})"
        sides = []
        for name in pair:
            relationship = by_name.get(name)
            if relationship is None:
                raise _refuse(f"{where} names {name!r}, which the expose does not declare")
            if name in used:
                raise _refuse(
                    f"{where} names {name!r}, which bridge ({', '.join(used[name])}) already holds"
                )
            used[name] = pair
            sides.append(relationship)
        first, second = sides
        if set(first.from_columns) & set(second.from_columns):
            raise _refuse(f"{where}: the two relationships set a column in common")
        for relationship in sides:
            name = relationship.name
            if relationship.cardinality == "one_to_one" or name in distinct:
                raise _refuse(
                    f"{where}: {name!r} references each parent row at most once (it is "
                    "one_to_one, or its columns are a key); a bridge's side is many to one"
                )
            if key_columns & set(relationship.from_columns):
                raise _refuse(f"{where}: the columns of {name!r} are already part of a key")
            if owners.get(relationship.from_columns[0]) is not relationship:
                holder = owners[relationship.from_columns[0]].name
                raise _refuse(f"{where}: {name!r} is implied by {holder!r}, which sets its columns")
            if name in conditioned:
                raise _refuse(f"{where}: {name!r} has a links condition, which a bridge side lacks")
            if by_column[relationship.from_columns[0]].statistics.null_fraction is not None:
                raise _refuse(
                    f"{where}: {name!r} declares a nullFraction on "
                    f"{relationship.from_columns[0]!r}, and a bridge's pairs are never NULL; "
                    "drop it"
                )
        pairs = plans[first.target].rows * plans[second.target].rows
        if plan.rows > pairs:
            raise _refuse(
                f"{where} declares {plan.rows} rows against the {pairs} pairs of its parents' "
                f"{plans[first.target].rows} and {plans[second.target].rows} rows; declare no "
                "more rows than pairs"
            )
        found[(*first.from_columns, *second.from_columns)] = pair
    return found


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
            f"expose {expose_id!r} relationships {', '.join(sorted(holders | {relationship.name}))} both "
            f"set ({', '.join(sorted(taken))}); two relationships may share a column only when one's "
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
                    f"expose {expose_id!r} key ({', '.join(key)}) shares columns with relationship "
                    f"{relationship.name!r} ({', '.join(relationship.from_columns)}), which reaches "
                    "outside it; a key holds a relationship's columns all or none"
                )
        for column in key:
            if column in members:
                raise _refuse(
                    f"expose {expose_id!r} column {column!r} belongs to keys ({', '.join(members[column])}) "
                    f"and ({', '.join(key)}), and neither holds a smaller unique key; declare one of "
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
        ("given_columns", "given"),
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
    plan: "_rows.GeneratorPlan", column_plan: "_rows.ColumnPlan", entities: Optional[int] = None
) -> Union[KeyGrid, IdentifierKey]:
    """A one-column key's layout over the rows, or over *entities* for a history's natural entity.

    The check that the key's declared values hold what it is laid out over counts the entities,
    ``E``, for a history's emit (ADR 0017 section 3): every version repeats its entity's key.
    """
    column, statistics = column_plan.column, column_plan.statistics
    expose_id = plan.expose.expose_id
    rows = plan.rows if entities is None else entities
    noun = "rows" if entities is None else "entities"
    kind = column.logical_type.kind
    if kind is LogicalKind.STRING:
        length = statistics.identifier_length or _rows.IDENTIFIER_DEFAULT_LENGTH
        as_uuid = column.declared_type.strip().lower() in _rows._UUID_TYPES
        _rows._require_identifier_shape(length, as_uuid)
        layout = IdentifierKey(
            key=_layout_key(plan, "key", column.name),
            length=length,
            as_uuid=as_uuid,
            rows=rows,
            counter_based=plan.counter_based,
        )
        if rows > layout.space:
            raise _refuse(
                f"expose {expose_id!r} key {column.name!r} is an identifier of {length} hex "
                f"characters, which holds {layout.space} values, fewer than its {rows} "
                f"{noun}; declare a longer identifierLength"
            )
        return layout
    if kind not in _ORDERED_KINDS:
        raise _refuse(
            f"expose {expose_id!r} key {column.name!r} is {column.type_name}; a key column is "
            "INT, DECIMAL, DATE, TIMESTAMP or STRING"
        )
    return _key_grid(plan, column_plan, entities)


def _check_history_allocation(plan: "_rows.GeneratorPlan") -> None:
    """Refuse (``FLX-3403``) a column of a history's emit under ``allocation: exact``.

    A column of a history's emit takes its values from the history, a tracked column along its
    matrix and a carried column from its entity's first version row, so a per-row allotment over
    the version rows cannot hold (ADR 0017 section 6).
    """
    for column_plan in plan.columns:
        if column_plan.statistics.allocation is not None:
            raise _refuse(
                f"expose {plan.expose.expose_id!r} is a history's emit and column "
                f"{column_plan.column.name!r} declares allocation exact; its values follow the "
                "entity, not the row, so declare no allocation"
            )


def _check_periodic(
    plan: "_rows.GeneratorPlan", keys: ExposeKeys, by_name: Mapping[str, "_rows.ColumnPlan"]
) -> FrozenSet[str]:
    """The primary key columns of a periodic emit, which are laid out one step per row (ADR 0027 section 5).

    Refused (``FLX-3403``), naming the emit: a primary key that is not a whole-number column, and
    a column under ``allocation: exact``, which reads the row count the window moves.
    """
    expose_id = plan.expose.expose_id
    for column_plan in plan.columns:
        if column_plan.statistics.allocation is not None:
            raise _refuse(
                f"expose {expose_id!r} is a periodic emit and column {column_plan.column.name!r} "
                "declares allocation exact, which reads the row count; a periodic emit's rows "
                "grow with its window, so declare no allocation"
            )
    stepped = set()
    for entity in keys.entities:
        if entity.kind != "primary":
            continue
        column = by_name[entity.column].column
        if column.logical_type.kind is not LogicalKind.INT:
            raise _refuse(
                f"expose {expose_id!r} is a periodic emit and its primary key {column.name!r} is "
                f"{column.type_name}; a periodic emit's primary key is a whole-number column, "
                "laid out one step per row"
            )
        stepped.add(column.name)
    return frozenset(stepped)


def _stepped_key(plan: "_rows.GeneratorPlan", column_plan: "_rows.ColumnPlan") -> KeyGrid:
    """A periodic emit's whole-number key: row ``i`` takes the low end of its range plus ``i``.

    The key never depends on the row count, so a longer window appends keys and keeps every
    earlier one. The range still has to hold the rows.
    """
    column, statistics = column_plan.column, column_plan.statistics
    where = f"expose {plan.expose.expose_id!r} key {column.name!r}"
    rows = plan.rows
    _check_key_declaration(statistics, rows, where)
    exponent = _exponent(column)
    spans, _counts = _key_spans(column, statistics, rows, exponent, where)
    first = spans[0][0]
    last = spans[-1][0] + spans[-1][1] - 1
    if last - first + 1 < rows:
        raise _refuse(
            f"{where}: its declared range holds {last - first + 1} values but takes its {rows} "
            "rows; widen the range, so that no value repeats"
        )
    return KeyGrid(
        kind=column.logical_type.kind.value,
        exponent=exponent,
        firsts=(first,),
        widths=(rows,),
        counts=(rows,),
        starts=(0,),
        rows=rows,
    )


def _key_grid(
    plan: "_rows.GeneratorPlan", column_plan: "_rows.ColumnPlan", entities: Optional[int] = None
) -> KeyGrid:
    """Lay a one-column ordered key out over its declared range or bins.

    Over the rows, or over the *entities* of a history's emit, one grid point each.
    """
    column, statistics = column_plan.column, column_plan.statistics
    where = f"expose {plan.expose.expose_id!r} key {column.name!r}"
    rows = plan.rows if entities is None else entities
    noun = "rows" if entities is None else "entities"
    _check_key_declaration(statistics, rows, where, noun)
    exponent = _exponent(column)
    spans, counts = _key_spans(column, statistics, rows, exponent, where, noun)
    firsts: List[int] = []
    widths: List[int] = []
    kept: List[int] = []
    starts: List[int] = []
    start = 0
    for index, ((first, width), count) in enumerate(zip(spans, counts, strict=True)):
        if count > width:
            what = "its declared range" if len(spans) == 1 else f"bin {index} of its histogram"
            raise _refuse(
                f"{where}: {what} holds {width} values but takes {count} of its {rows} "
                f"{noun}; widen the range, or move weight to wider bins, so that no value repeats"
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
        rows=rows,
    )


def _check_key_declaration(statistics: Any, rows: int, where: str, noun: str = "rows") -> None:
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
            f"{where} declares maxCardinality {statistics.max_cardinality} for {rows} {noun}; "
            f"a key holds one value per {'row' if noun == 'rows' else 'entity'}"
        )


def _key_spans(
    column: CompiledColumn,
    statistics: Any,
    rows: int,
    exponent: int,
    where: str,
    noun: str = "rows",
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
        f"least its {rows} {noun}"
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


def _key_span(parent: Union[KeyGrid, ReferenceColumn]) -> str:
    """The keys a parent holds in a plan, for a refusal: ``keys 101 to 107, 7 rows``."""
    rows = parent.rows
    noun = "row" if rows == 1 else "rows"
    if isinstance(parent, KeyGrid):  # an ordered layout: the first row's key is the lowest
        return f"keys {parent.value_at(0)} to {parent.value_at(rows - 1)}, {rows} {noun}"
    held = [value for value in parent.values if value is not None]
    if not held:
        return f"no key, {rows} {noun}"
    return f"keys {min(held)} to {max(held)}, {rows} {noun}"


def _check_declared_keys(
    plan: "_rows.GeneratorPlan",
    first: "_rows.ColumnPlan",
    relationship: Relationship,
    target: ReferenceColumn,
) -> None:
    """Refuse (``FLX-3403``) a declared pick over a reference key whose levels are not all keys.

    The pick takes the reference row whose key equals the drawn level, so the column declares
    levels (or a support), and every one of them is a key the reference expose publishes.
    """
    statistics = first.statistics
    where = f"expose {plan.expose.expose_id!r} relationship {relationship.name!r}"
    if statistics.levels is None and not statistics.support:
        raise _refuse(
            f"{where} points at {relationship.target!r}, a reference expose, and column "
            f"{first.column.name!r} declares a draw that is not a list of levels; declare the "
            "keys it takes as levels or a support, or declare no draw to pick a row uniformly"
        )
    with localcontext(_rows._KERNEL_CONTEXT):
        for level in _rows._level_names(statistics):
            try:
                held = target.holds(_rows._level_to_value(level, first.column))
            except (ArithmeticError, ValueError, FluxError):
                held = False
            if not held:
                raise _refuse(
                    f"{where} points at {relationship.target!r}, a reference expose, and column "
                    f"{first.column.name!r} declares a level that is no key it publishes; every "
                    "declared level is a key of the reference expose"
                )


def _check_tracked_keys(
    shape: _Shape, column: str, view: Callable[[str, Sequence[str]], ParentView]
) -> None:
    """Refuse (``FLX-3403``) a tracked foreign key one of whose levels is no key its parent holds.

    A tracked column writes the level it is at as the cell and takes no parent row, so the cell
    is a key of the parent only while the parent's rows hold that level. The parent's keys are
    the ones in this plan: a ``--scale`` below 1 cuts the parent's rows and its key range and
    leaves the levels, so the refusal shows the cut in the span it names. A pick that takes the
    nearest key (``declared``) never writes a level, so it holds at any scale and is not asked.
    """
    relationship = shape.owners.get(column)
    if (
        relationship is None
        or relationship.name in shape.absorbed
        or len(relationship.from_columns) != 1
    ):
        return
    parent = view(relationship.target, relationship.to_columns).driver(relationship.to_columns[0])
    if not isinstance(parent, (KeyGrid, ReferenceColumn)):
        return
    statistics = shape.plan.column_plan(column).statistics
    with localcontext(_rows._KERNEL_CONTEXT):
        for level in _rows._level_names(statistics):
            try:
                held = parent.holds(
                    _rows._level_to_value(level, shape.plan.column_plan(column).column)
                )
            except (ArithmeticError, ValueError, FluxError):
                held = False
            if not held:
                message = (
                    f"expose {shape.plan.expose.expose_id!r} column {column!r} is tracked by "
                    f"its history over levels and points at {relationship.target!r}, but level "
                    f"{level!r} is no key {relationship.target!r} holds in this plan "
                    f"({_key_span(parent)}); a tracked foreign key writes its "
                    "level as the key, so every level is a key of its parent: plan without "
                    "--scale, which cuts the parent's keys, or stop tracking the column so "
                    "its foreign key takes the nearest key the parent holds"
                )
                if not isinstance(parent, KeyGrid):
                    raise _refuse(message)
                raise TrackedLevelsCut(
                    message,
                    TrackedLevels(
                        relationship.target,
                        relationship.to_columns[0],
                        shape.plan.expose.expose_id,
                        column,
                        tuple(_rows._level_names(statistics)),
                    ),
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
        if is_reference_plan(shape.plan):
            assert shape.plan.relations is not None  # a reference plan
            return tuple(shape.plan.relations.drivers)
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
        # A column a generator plugin generates is produced by the plugin (ADR 0032 section 5).
        made = frozenset() if shape.plan.plugin is None else frozenset(shape.plan.plugin.names)
        projected = _projected_columns(shape.plan)
        for column_plan in shape.plan.columns:
            name = column_plan.column.name
            if (
                column_plan.family is None
                and column_plan.column_type is not ColumnType.IDENTIFIER
                and name not in drivers
                and name not in ruled
                and name not in made
                and name not in projected
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
        if is_reference_plan(shape.plan):
            assert shape.plan.relations is not None  # a reference plan
            return shape.plan.relations.driver(column)
        if shape.history is not None:
            chained = self._history_driver(shape, shape.history, column)
            if chained is not None:
                return chained
        layout = shape.layouts.get(column)
        if layout is not None:
            if shape.history is not None and column == shape.history.natural:
                return EntityColumn(layout=layout, history=shape.history.pick)
            return layout
        key = shape.members.get(column)
        if key is not None:
            return CompositeMember(self.composite(expose_id, key))
        relationship = shape.owners.get(column)
        if relationship is not None and relationship.name not in shape.absorbed:
            return self.foreign(expose_id, relationship)
        return None

    def _history_driver(
        self, shape: _Shape, history: _HistoryShape, column: str
    ) -> Optional[Driver]:
        """The driver of a history's start, end, activation or tracked column, else ``None``."""
        for role in ("start", "end", "activation"):
            if column == getattr(history, role):
                return ChainColumn(role, history.pick)
        if column in history.tracked:
            if history.tracked_levels:
                _check_tracked_keys(shape, column, self.view)
            return self._tracked(shape, history.tracked.index(column))
        return None

    @staticmethod
    def _tracked(shape: _Shape, index: int) -> TrackedColumn:
        """The driver of tracked column *index*: every tracked column's draws, read against its own levels."""
        assert shape.history is not None
        entry = shape.history.pick.entry
        specs = tuple(
            tracked_spec(
                tracked, _rows._column_levels(shape.plan.column_plan(tracked.column).statistics)
            )
            for tracked in entry.tracked
        )
        return TrackedColumn(shape.history.pick, index, specs)

    def view(self, expose_id: str, columns: Sequence[str]) -> ParentView:
        names = tuple(sorted(set(columns)))
        found = self._views.get((expose_id, names))
        if found is not None:
            return found
        plan = self.shapes[expose_id].plan
        drivers: List[Tuple[str, Driver]] = []
        for name in names:
            if plan.column_plan(name).statistics.parents:
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
        weight = shape.weights.get(relationship.name)
        pick, key, grid = _first_pick(shape, relationship, parent, first)
        counted = self._counted(shape, relationship, self.shapes[relationship.target].plan)
        history: Optional[HistoryPick] = None
        if counted is not None:
            pick, grid = "counted", None
            history = self._history_pick(shape, relationship)
            if history is not None:
                pick = "history"
        copied = shape.copied.get(relationship.name)
        if copied is not None:
            pick, grid, key = "copied", None, None
            _check_copy_source(self.driver(copied.reach, copied.column), expose_id, copied)
        weight_values: Optional[ReferenceColumn] = None
        if weight is not None:
            weight_values = self._weighted(shape, relationship, pick, first, weight)
            pick = "weighted"
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
            weight=weight,
            weight_values=weight_values,
            counted=counted,
            history=history,
            copied=copied,
        )
        self._foreign[(expose_id, relationship.name)] = foreign
        return foreign

    @staticmethod
    def _history_pick(shape: _Shape, relationship: Relationship) -> Optional[HistoryPick]:
        """The history's pick when *relationship* is the one its entities are counted over."""
        history = shape.history
        if history is None or history.parent is None:
            return None
        return history.pick if relationship.name == history.parent.relationship else None

    def _weighted(
        self,
        shape: _Shape,
        relationship: Relationship,
        pick: str,
        first: Any,
        weight: str,
    ) -> Optional[ReferenceColumn]:
        """The published weights of a weighted relationship, when its parent is a reference expose."""
        if pick == "declared":
            raise _refuse(
                f"expose {shape.plan.expose.expose_id!r} relationship {relationship.name!r} is "
                f"weighted by {weight!r}, but its first column {first.column.name!r} declares a "
                "draw that picks the parent by its key; drop the draw or the weight"
            )
        held = self.shapes[relationship.target].plan
        if not is_reference_plan(held):
            return None
        assert held.relations is not None  # a reference plan
        found = held.relations.driver(weight)
        assert isinstance(found, ReferenceColumn)  # _check_weights read it
        return found

    @staticmethod
    def _counted(
        shape: _Shape, relationship: Relationship, parent: "_rows.GeneratorPlan"
    ) -> Optional[CountedPick]:
        """The counted layout of *relationship*, when its Simulation declares one (ADR 0016)."""
        history = shape.history
        if (
            history is not None
            and history.parent is not None
            and relationship.name == history.parent.relationship
        ):
            return _Resolver._parent_pick(shape, history, relationship)
        entry = shape.counts.get(relationship.name)
        bound = shape.hooked.get(relationship.name)
        if entry is None and bound is None:
            return None
        given = None if entry is None or entry.given is None else given_children(entry, parent)
        return CountedPick(
            children=None if entry is None else entry.children,
            child_rows=shape.plan.rows,
            minimum=None if entry is None else entry.min_children,
            parent_when=shape.conditions.get(relationship.name),
            expected_count=None if bound is None else bound[1],
            hook=None if bound is None else bound[0],
            one_to_one=relationship.cardinality == "one_to_one",
            **_given_members(entry, given),
        )

    @staticmethod
    def _parent_pick(
        shape: _Shape, history: _HistoryShape, relationship: Relationship
    ) -> CountedPick:
        """The layout of a history's parent relationship: the entities are the parent's children."""
        assert history.parent is not None
        # The layout totals E, not the rows.
        return CountedPick(
            children=history.parent.children,
            child_rows=history.pick.entities,
            minimum=history.parent.min_children,
            parent_when=shape.conditions.get(relationship.name),
            expected_count=None if history.hook is None else history.hook[1],
            hook=None if history.hook is None else history.hook[0],
        )

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
        where = f"expose {expose_id!r} key ({', '.join(key)})"
        if space < plan.rows:
            raise KeySpaceTooSmall(
                FLX_3403,
                f"{where} can take {space} distinct values over its columns' declared domains, "
                f"fewer than its {plan.rows} rows; widen a column's range or levels, or add "
                "parent rows",
            )
        if space > PERMUTE_MAX_DOMAIN:
            raise _refuse(
                f"{where} spans more than 2**256 values over its columns' declared domains; "
                "narrow a range"
            )
        pair = shape.bridges.get(key)
        composite = CompositeKey(
            columns=key,
            key=(
                _layout_key(plan, "pair", *pair)
                if pair is not None
                else _layout_key(plan, "key", *key)
            ),
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
            parents = column_plan.statistics.parents
            if not parents:
                continue
            name = column_plan.column.name
            if name in drivers:
                driver = drivers[name]
                if not (isinstance(driver, ForeignKey) and driver.pick == "declared"):
                    raise _refuse(
                        f"expose {expose_id!r} column {name!r} takes its values from keys and "
                        "is not drawn, so it cannot be drawn given another column; drop its given"
                    )
            for given in parents:
                source = drivers.get(given)
                if source is not None and not (
                    isinstance(source, ForeignKey) and source.pick == "declared"
                ):
                    raise _refuse(
                        f"expose {expose_id!r} column {name!r} is drawn given {given!r}, whose "
                        "values come from keys and are not drawn; drop the given, or give "
                        f"{given!r} a declared draw over an ordered parent key"
                    )
