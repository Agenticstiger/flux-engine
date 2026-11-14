# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The derived statistic ``S`` -- SEAM-FIDELITY-SPEC §4.

    **S** (derived statistic): per property the aggregate quantities only
    -- bucket counts on the declared edges, level counts on the declared
    support, ``Sx``, ``Sy``, ``Sxy``, ``Sxx``, ``Syy``, the sorted
    committed support, the quantile witness, ``n``, ``k`` per property and
    per group.

The same function runs on both sides. §5 says the verifier "recomputes
D′'s statistics on the same grid", and the only way two sides can be
compared in integer cross-multiplied form is if one implementation
produced both -- a second implementation of the quantiser, the bin rule or
the null policy would be a second grid wearing the first one's name. What
is boundary-only is the *store* (the blinding value, the check counter),
not the arithmetic.

Everything a statistic keeps is an aggregate. No function here returns a
record, a value, or anything from which one could be read back: a bin
count, a sum of encoded values, a sorted member list and one order
statistic, and nothing else. Diagnostics name a column, a row index and a
value class, never a value (KERNEL-SPEC §9).

Fail-closed conditions of §5 -- ``k = 0``, a count outside ``records``, a
group below ``records.min``, a value outside its range or its declared
support, a column absent -- are raised as ``FLX-5005`` (or ``FLX-5031``
for the absent column) from here, so a refusal can never be mistaken for a
verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import partial
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar

from flux_engine.concordance.compiler import CompiledFidelity, CompiledProperty
from flux_engine.concordance.errors import FLX_5031
from flux_engine.concordance.predicate import (
    ColumnDeclaration,
    FidelityBlock,
    NullPolicy,
    PropertyKind,
    ValueDomain,
)
from flux_engine.core.canonical import nfc
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.core.fixedpoint import (
    FLX_5005,
    Grid,
    SecondMoments,
    order_statistic_index,
    require_sample,
)

__all__ = [
    "UNGROUPED",
    "DerivedStatistic",
    "GroupStatistic",
    "PropertyStatistic",
    "Row",
    "bin_index",
    "compute_property_statistic",
    "compute_statistic",
    "encode_value",
    "level_cells",
    "require_record_count",
]

#: One record as it reaches a statistic: a mapping from column name to a
#: value that is a string, an int, a ``Decimal`` or ``None``. Never a
#: ``float`` -- KERNEL-SPEC §2.1 keeps binary floating point out of every
#: hashed path, and both the committed slice reader and the emit stream
#: hand over exact values.
Row = Mapping[str, Any]

#: The ungrouped group's key inside a :class:`PropertyStatistic`. A string
#: rather than ``None`` keeps the canonical document's mapping keys
#: uniformly strings. It is not reserved: the empty string is also a
#: category a grouped property can hold, as a row value or as a declared
#: level, so whether a property is grouped is read from its declaration
#: (``group_by``) and never from this key.
UNGROUPED = ""


# ---------------------------------------------------------------------------
# Reading one value
# ---------------------------------------------------------------------------


def _exact(value: Any, subject: str) -> Fraction:
    if isinstance(value, bool):
        raise FluxError(FLX_5005, f"{subject}: value class 'bool'")
    if isinstance(value, float):
        raise FluxError(FLX_5005, f"{subject}: value class 'float'")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise FluxError(FLX_5005, f"{subject}: value class 'non-finite Decimal'")
        return Fraction(value)
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise FluxError(FLX_5005, f"{subject}: value class 'non-numeric string'") from None
        if not parsed.is_finite():
            raise FluxError(FLX_5005, f"{subject}: value class 'non-finite Decimal'")
        return Fraction(parsed)
    raise FluxError(FLX_5005, f"{subject}: value class {type(value).__name__!r}")


def encode_value(
    value: Any,
    column: ColumnDeclaration,
    grid: Grid,
    *,
    row_index: int,
) -> int:
    """Encode one non-null value of a ``RANGE`` column onto the grid.

    Refuses (``FLX-5005``) a value outside the declared range -- §5's
    fail-closed condition "a value outside its range". The diagnostic
    names the column, the row index and the value's class, never the
    value itself.
    """
    subject = f"column {column.name!r} row {row_index}"
    if column.domain is ValueDomain.TIMESTAMP:
        if not isinstance(value, str):
            raise FluxError(FLX_5005, f"{subject}: value class {type(value).__name__!r}")
        exact = Fraction(parse_rfc3339_ms(value))
    else:
        exact = _exact(value, subject)
    interval = column.interval
    if not interval.contains(exact):
        raise FluxError(FLX_5005, f"{subject}: value outside the declared range")
    return grid.quantise(exact, subject=subject)


def _category(value: Any, column: ColumnDeclaration, *, row_index: int) -> str:
    subject = f"column {column.name!r} row {row_index}"
    if not isinstance(value, str):
        raise FluxError(FLX_5005, f"{subject}: value class {type(value).__name__!r}")
    return nfc(value)


def _cell(row: Row, column: ColumnDeclaration, *, row_index: int) -> Any:
    """Return the raw cell for *column*, refusing an absent column.

    §5's fail-closed list names "a column absent" -- absent from the data,
    not from the declaration, so it is ``FLX-5031`` (a column the check
    needs that the rows do not carry) rather than a sample refusal.
    """
    if column.name not in row:
        raise FluxError(
            FLX_5031,
            f"column {column.name!r} is absent from row {row_index}",
        )
    return row[column.name]


def level_cells(column: ColumnDeclaration) -> Tuple[str, ...]:
    """The cells a level histogram counts over *column*, in their one order.

    The column's declared support levels, sorted: NFC-normalised strings
    ordered by code point, the order "the sorted committed support" uses.
    Both support spellings resolve to distinct NFC levels where they are
    read (:func:`flux_engine.concordance.predicate.read_support_levels`),
    so the cells are distinct. They come from the declaration and never
    from the data, so the shape of a ``levelCounts`` list is fixed by the
    contract the way a histogram's bin count is, and a level the state
    never holds is a cell of count zero rather than an absent one.
    """
    return tuple(sorted(column.support_values))


def bin_index(units: int, edges: Sequence[int]) -> int:
    """The bin a grid value falls in, given encoded edges.

    Bins are half-open ``[e_i, e_{i+1})`` except the last, which is closed
    at the top so a record exactly at the declared maximum is binned
    rather than dropped. Edges partition the declared range (§2) and a
    value outside it has already been refused, so the answer always
    exists.
    """
    last = len(edges) - 2
    for index in range(last + 1):
        if units < edges[index + 1]:
            return index
    return last


# ---------------------------------------------------------------------------
# The statistic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupStatistic:
    """The aggregate quantities for one property inside one group.

    ``k`` is the number of records that contributed after the null policy
    and the grouping; every other field is a sum, a count, a sorted member
    list or one order statistic over exactly those ``k`` records.
    """

    group: str
    k: int
    sum_x: int = 0
    sum_y: int = 0
    sum_xx: int = 0
    sum_yy: int = 0
    sum_xy: int = 0
    bin_counts: Tuple[int, ...] = ()
    #: Records per declared level of a level histogram's column, in
    #: :func:`level_cells` order.
    level_counts: Tuple[int, ...] = ()
    support: Tuple[str, ...] = ()
    #: Records carried by each member of :attr:`support`, in the same order.
    #: Held in process only -- §4 publishes "the sorted committed support"
    #: and nothing per-value about the real data, so :meth:`to_document`
    #: (the ``C2`` commitment) does not carry it. It exists so the SUPPORT
    #: comparison can bound how *much* of the synthetic dataset is
    #: off-support, not merely how many different strange values it holds.
    support_counts: Tuple[int, ...] = ()
    quantile_witness: Optional[int] = None
    quantile_below: int = 0
    quantile_at_or_below: int = 0

    def cell_counts(self, kind: PropertyKind) -> Tuple[int, ...]:
        """The counts a total-variation comparison reads for a *kind* property.

        A histogram's bin counts, or a level histogram's level counts: one
        count per declared cell, in the cell order both sides share.
        """
        return self.level_counts if kind is PropertyKind.LEVEL_HISTOGRAM else self.bin_counts

    @property
    def moments(self) -> SecondMoments:
        """The second-moment view a correlation comparison consumes."""
        return SecondMoments(
            count=self.k,
            sum_x=self.sum_x,
            sum_y=self.sum_y,
            sum_xx=self.sum_xx,
            sum_yy=self.sum_yy,
            sum_xy=self.sum_xy,
        )

    def to_document(self, kind: PropertyKind) -> Dict[str, Any]:
        """The canonical form of this group, for the ``C2`` commitment.

        Counts are JSON integers because ``records.max`` keeps them inside
        the safe-integer domain RFC 8785 serialises through. Encoded sums
        and the quantile witness are decimal *strings*: a sum of a hundred
        thousand encoded values has no safe-integer spelling, and
        KERNEL-SPEC §2.1 refuses an integer above 2^53-1 rather than
        letting it round.
        """
        document: Dict[str, Any] = {"group": self.group, "k": self.k}
        if kind is PropertyKind.MEAN:
            document["Sx"] = str(self.sum_x)
        elif kind is PropertyKind.MARGINAL_HISTOGRAM:
            document["binCounts"] = list(self.bin_counts)
        elif kind is PropertyKind.LEVEL_HISTOGRAM:
            document["levelCounts"] = list(self.level_counts)
        elif kind is PropertyKind.SUPPORT:
            document["support"] = list(self.support)
        elif kind is PropertyKind.QUANTILE:
            document["quantileWitness"] = (
                str(self.quantile_witness) if self.quantile_witness is not None else None
            )
            document["quantileBelow"] = self.quantile_below
            document["quantileAtOrBelow"] = self.quantile_at_or_below
        else:
            document["Sx"] = str(self.sum_x)
            document["Sy"] = str(self.sum_y)
            document["Sxx"] = str(self.sum_xx)
            document["Sxy"] = str(self.sum_xy)
            document["Syy"] = str(self.sum_yy)
        return document


@dataclass(frozen=True)
class PropertyStatistic:
    """Every group's quantities for one property, plus the property's ``n``.

    ``n`` is the number of records the state holds; ``k`` (on each group)
    is the number that reached this property's comparison. They differ
    exactly when a null was excluded or the property is grouped, which is
    why §4 asks for both.
    """

    property_id: str
    kind: PropertyKind
    n: int
    groups: Tuple[GroupStatistic, ...]

    def group(self, key: str) -> Optional[GroupStatistic]:
        """Return the group with *key*, or ``None`` when it has no records."""
        for entry in self.groups:
            if entry.group == key:
                return entry
        return None

    def to_document(self) -> Dict[str, Any]:
        """The canonical form of this property's quantities."""
        return {
            "propertyId": self.property_id,
            "property": self.kind.value,
            "n": self.n,
            "groups": [entry.to_document(self.kind) for entry in self.groups],
        }


@dataclass(frozen=True)
class DerivedStatistic:
    """``S``: the aggregate quantities of one record set, per property.

    This is the whole of what §4 lets out of the boundary alongside the
    commitments -- "``S``, ``C1``, ``C2``, ``stateOrdinal``, record count
    (or declared range), commitment time. **Never a record.**"
    """

    expose_id: str
    record_count: int
    properties: Mapping[str, PropertyStatistic]

    def to_document(self) -> Dict[str, Any]:
        """The canonical document ``C2`` is taken over."""
        return {
            "exposeId": self.expose_id,
            "recordCount": self.record_count,
            "properties": [self.properties[key].to_document() for key in sorted(self.properties)],
        }


# ---------------------------------------------------------------------------
# Computing it
# ---------------------------------------------------------------------------


def _group_keys(
    rows: Sequence[Row],
    compiled: CompiledProperty,
    statistic_columns: Mapping[str, ColumnDeclaration],
) -> List[Tuple[str, List[int]]]:
    """Partition row indices by the property's ``groupBy`` value.

    Groups are returned in NFC-sorted key order, never in the order the
    rows happened to arrive: two record sets with the same contents and
    different orderings must produce the same statistic, byte for byte.
    """
    group_by = compiled.declaration.group_by
    if group_by is None:
        return [(UNGROUPED, list(range(len(rows))))]
    column = statistic_columns[group_by]
    # A level histogram is a table of declared cells on both axes: its
    # groups, like its levels, must be levels the declaration names. The
    # other grouped properties keep grouping by whatever the rows hold.
    declared = (
        frozenset(column.support_values)
        if compiled.declaration.kind is PropertyKind.LEVEL_HISTOGRAM
        else None
    )
    buckets: Dict[str, List[int]] = {}
    for index, row in enumerate(rows):
        cell = _cell(row, column, row_index=index)
        if cell is None:
            if column.null_policy is NullPolicy.FAIL:
                raise FluxError(FLX_5005, f"column {group_by!r} row {index}: null is refused")
            continue
        key = _category(cell, column, row_index=index)
        if declared is not None and key not in declared:
            raise FluxError(
                FLX_5005,
                f"column {group_by!r} row {index}: value outside the declared support",
            )
        buckets.setdefault(key, []).append(index)
    return [(key, buckets[key]) for key in sorted(buckets)]


_Read = TypeVar("_Read")


def _read_column(
    rows: Sequence[Row],
    indices: Sequence[int],
    column: ColumnDeclaration,
    read: Callable[..., _Read],
) -> List[_Read]:
    """Read one column over *indices* with *read*; return what it read, in row order.

    A null under ``exclude`` drops the record from this property; a null
    under ``fail`` refuses the check. Both are decided by the column's own
    declaration, never by what would make the comparison pass. *read* is
    handed each non-null cell and its ``row_index`` in row order, so the
    first record that refuses is the one a refusal names, whichever rule it
    breaks.
    """
    values: List[_Read] = []
    for index in indices:
        cell = _cell(rows[index], column, row_index=index)
        if cell is None:
            if column.null_policy is NullPolicy.FAIL:
                raise FluxError(FLX_5005, f"column {column.name!r} row {index}: null is refused")
            continue
        values.append(read(cell, row_index=index))
    return values


def _level_histogram_group(
    rows: Sequence[Row],
    indices: Sequence[int],
    key: str,
    column: ColumnDeclaration,
) -> GroupStatistic:
    """One group of a level histogram: records per declared level of *column*."""
    slots = {level: slot for slot, level in enumerate(level_cells(column))}

    def slot_of(cell: Any, *, row_index: int) -> int:
        slot = slots.get(_category(cell, column, row_index=row_index))
        if slot is None:
            # A level the declaration does not name has no cell, as a value
            # outside the declared range has no bin: refused, never dropped
            # and never counted into a neighbour.
            raise FluxError(
                FLX_5005,
                f"column {column.name!r} row {row_index}: value outside the declared support",
            )
        return slot

    found = _read_column(rows, indices, column, slot_of)
    level_counts = [0] * len(slots)
    for slot in found:
        level_counts[slot] += 1
    return GroupStatistic(group=key, k=len(found), level_counts=tuple(level_counts))


def _group_statistic(
    rows: Sequence[Row],
    indices: Sequence[int],
    key: str,
    compiled: CompiledProperty,
    block_columns: Mapping[str, ColumnDeclaration],
    grid: Grid,
) -> GroupStatistic:
    declaration = compiled.declaration
    kind = declaration.kind

    if kind is PropertyKind.SUPPORT:
        column = block_columns[declaration.columns[0]]
        members: Dict[str, int] = {}
        categories = _read_column(rows, indices, column, partial(_category, column=column))
        for category in categories:
            members[category] = members.get(category, 0) + 1
        count = len(categories)
        support = tuple(sorted(members))
        maximum = column.max_cardinality
        if maximum is not None and len(support) > maximum:
            raise FluxError(
                FLX_5005,
                f"column {column.name!r}: {len(support)} distinct members exceed the "
                f"declared maxCardinality {maximum}",
            )
        return GroupStatistic(
            group=key,
            k=count,
            support=support,
            support_counts=tuple(members[value] for value in support),
        )

    if kind is PropertyKind.LEVEL_HISTOGRAM:
        return _level_histogram_group(rows, indices, key, block_columns[declaration.columns[0]])

    if kind is PropertyKind.CORRELATION:
        first = block_columns[declaration.columns[0]]
        second = block_columns[declaration.columns[1]]
        xs: List[int] = []
        ys: List[int] = []
        for index in indices:
            left = _cell(rows[index], first, row_index=index)
            right = _cell(rows[index], second, row_index=index)
            if left is None or right is None:
                # A correlation reads a pair. A record missing either half
                # contributes to neither sum -- but §5's fail-closed list
                # names "a null in a column declared ``null: fail``", and a
                # row null in BOTH halves is null in each of them. Reading
                # the policy from one column only would let a `null: fail`
                # declaration on the second column never fire whenever the
                # first is `null: exclude`, so both are consulted.
                for missing, cell in ((first, left), (second, right)):
                    if cell is None and missing.null_policy is NullPolicy.FAIL:
                        raise FluxError(
                            FLX_5005, f"column {missing.name!r} row {index}: null is refused"
                        )
                continue
            xs.append(encode_value(left, first, grid, row_index=index))
            ys.append(encode_value(right, second, grid, row_index=index))
        return GroupStatistic(
            group=key,
            k=len(xs),
            sum_x=sum(xs),
            sum_y=sum(ys),
            sum_xx=sum(x * x for x in xs),
            sum_yy=sum(y * y for y in ys),
            sum_xy=sum(x * y for x, y in zip(xs, ys, strict=True)),
        )

    column = block_columns[declaration.columns[0]]
    values = _read_column(rows, indices, column, partial(encode_value, column=column, grid=grid))

    if kind is PropertyKind.MEAN:
        return GroupStatistic(group=key, k=len(values), sum_x=sum(values))

    if kind is PropertyKind.MARGINAL_HISTOGRAM:
        edges = [grid.quantise(edge, subject="bin edge") for edge in declaration.bins]
        counts = [0] * (len(edges) - 1)
        for units in values:
            counts[bin_index(units, edges)] += 1
        return GroupStatistic(group=key, k=len(values), bin_counts=tuple(counts))

    # QUANTILE
    if not values:
        return GroupStatistic(group=key, k=0)
    assert declaration.level is not None  # guaranteed by the predicate parser
    ordered = sorted(values)
    index = order_statistic_index(len(ordered), declaration.level)
    witness = ordered[index - 1]
    below = sum(1 for units in ordered if units < witness)
    at_or_below = sum(1 for units in ordered if units <= witness)
    return GroupStatistic(
        group=key,
        k=len(ordered),
        quantile_witness=witness,
        quantile_below=below,
        quantile_at_or_below=at_or_below,
    )


def require_record_count(rows: Sequence[Row], block: FidelityBlock, *, subject: str) -> None:
    """Refuse (``FLX-5005``) a record count outside the declared ``records``.

    The one fail-closed condition of §5 that is a property of the record
    set rather than of a property: it is checked once, before any
    statistic is formed, because a dataset of the wrong size makes every
    comparison over it meaningless rather than one of them.
    """
    require_sample(
        len(rows),
        minimum=block.records.minimum,
        maximum=block.records.maximum,
        subject=subject,
    )


def compute_property_statistic(
    rows: Sequence[Row],
    compiled: CompiledProperty,
    block: FidelityBlock,
    *,
    subject: str = "record set",
) -> PropertyStatistic:
    """Form one property's quantities over *rows*.

    Refuses (``FLX-5005``) a group below ``records.min``, an empty
    ungrouped sample (``k = 0``), a value outside its declared range, and
    a null in a column declared ``null: fail``; refuses (``FLX-5031``) a
    column the rows do not carry.

    Per property, not per dataset, so a refusal on one property leaves the
    others answerable. §5's list is a list of conditions under which *a
    comparison* declines, and a caller reading one refusal should not have
    to guess whether the rest would also have refused.
    """
    grouped = compiled.declaration.group_by is not None
    groups: List[GroupStatistic] = []
    for ordinal, (key, indices) in enumerate(_group_keys(rows, compiled, block.columns)):
        group = _group_statistic(rows, indices, key, compiled, block.columns, block.grid)
        # The group is named by its ORDINAL in the NFC-sorted group order,
        # never by its key. The key is a value read out of the owner's
        # committed slice -- a market, a diagnosis code, an employer -- and
        # KERNEL-SPEC §9 keeps values out of diagnostics. This refusal in
        # particular fires only for groups below ``records.min``, which are
        # exactly the rare, re-identifying ones, and it travels all the way
        # out to the operator's terminal and the ``--json`` envelope. The
        # floor applies to every group of a grouped property, the group of
        # an empty-string category included: it is decided by the
        # declaration, never by the key (see UNGROUPED).
        where = f" group #{ordinal}" if grouped else ""
        require_sample(
            group.k,
            minimum=block.records.minimum if grouped else 1,
            subject=f"{subject}: {compiled.property_id}{where}",
        )
        groups.append(group)
    if not groups:
        raise FluxError(
            FLX_5005,
            f"{subject}: {compiled.property_id} formed no group",
        )
    return PropertyStatistic(
        property_id=compiled.property_id,
        kind=compiled.declaration.kind,
        n=len(rows),
        groups=tuple(groups),
    )


def compute_statistic(
    rows: Sequence[Row],
    compiled: CompiledFidelity,
    *,
    subject: str = "record set",
) -> DerivedStatistic:
    """Form the whole of ``S`` over *rows*, or refuse.

    The boundary's path: a committed state is published whole or not at
    all, so the first fail-closed condition any property meets refuses the
    commitment. The verifier's path goes property by property through
    :func:`compute_property_statistic` instead.
    """
    block = compiled.block
    require_record_count(rows, block, subject=subject)
    statistics: Dict[str, PropertyStatistic] = {
        entry.property_id: compute_property_statistic(rows, entry, block, subject=subject)
        for entry in compiled.properties
    }
    return DerivedStatistic(
        expose_id=block.expose_id,
        record_count=len(rows),
        properties=MappingProxyType(dict(sorted(statistics.items()))),
    )
