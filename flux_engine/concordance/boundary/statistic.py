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

Reading a record -- the cell a column names, the null policy, the grouping
and every refusal a record can meet -- is :mod:`flux_stats.reading`, and
the arithmetic over what it read is one registered definition per statistic
(:mod:`flux_stats`). A profiler shares both without importing this module
or the rest of the engine. This module picks, per property, the columns it
reads and the definition that measures them.

Everything a statistic keeps is an aggregate. No function here returns a
record, a value, or anything from which one could be read back: a bin
count, a sum of encoded values, a sorted member list and one order
statistic, and nothing else. Diagnostics name a column, a row index and a
value class, never a value (KERNEL-SPEC §9).

Fail-closed conditions of §5 -- ``k = 0``, a count outside ``records``, a
group below ``records.min``, a value outside its range or its declared
support, a column absent -- are raised as ``FLX-5005`` (or ``FLX-5031``
for the absent column) from here and from the reader, so a refusal can
never be mistaken for a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.compiler import CompiledFidelity, CompiledProperty
from flux_engine.concordance.decoded import DecodedRows, group_keys_decoded, measure_decoded
from flux_engine.concordance.predicate import (
    FidelityBlock,
    PropertyKind,
)
from flux_engine.core.errors import FluxError
from flux_stats import (
    GroupStatistic,
    bin_index,
    definition_for_kind,
)
from flux_stats.fidelity import definition_for_property, measure_group
from flux_stats.fixedpoint import (
    FLX_5005,
    require_sample,
)
from flux_stats.reading import (
    UNGROUPED,
    Row,
    encode_value,
    group_keys,
    level_cells,
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

# ---------------------------------------------------------------------------
# The statistic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PropertyStatistic:
    """Every group's quantities for one property, plus the property's ``n``.

    ``n`` is the number of records the state holds; ``k`` (on each group)
    is the number that reached this property's comparison. They differ
    exactly when a null was excluded or the property is grouped, which is
    why §4 asks for both.

    ``binned`` says the property is a quantile that declares bins, formed by
    ``quantile/2`` (ADR 0012): its groups spell the bin that holds the order
    statistic and never the witness.
    """

    property_id: str
    kind: PropertyKind
    n: int
    groups: Tuple[GroupStatistic, ...]
    binned: bool = False

    def group(self, key: str) -> Optional[GroupStatistic]:
        """Return the group with *key*, or ``None`` when it has no records."""
        for entry in self.groups:
            if entry.group == key:
                return entry
        return None

    def to_document(self) -> Dict[str, Any]:
        """The canonical form of this property's quantities."""
        statistic = definition_for_kind(self.kind, binned=self.binned)
        return {
            "propertyId": self.property_id,
            "property": self.kind.value,
            "n": self.n,
            "groups": [entry.to_document(statistic) for entry in self.groups],
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
    declaration = compiled.declaration
    grouped = declaration.group_by is not None
    grouping = block.columns[declaration.group_by] if declaration.group_by is not None else None
    # A level histogram's groups, like its levels, are declared cells.
    declared_only = declaration.kind is PropertyKind.LEVEL_HISTOGRAM
    keys: Optional[Sequence[Tuple[str, Sequence[int]]]] = None
    if isinstance(rows, DecodedRows):
        # Columns decoded once (flux_engine.concordance.decoded); anything it cannot read is
        # read from the rows below, which is the reader that refuses and says why.
        keys = group_keys_decoded(rows, grouping, declared_only=declared_only)
    if keys is None:
        keys = group_keys(rows, grouping, declared_only=declared_only)
    groups: List[GroupStatistic] = []
    for ordinal, (key, indices) in enumerate(keys):
        group = (
            measure_decoded(rows, indices, key, declaration, block.columns, block.grid)
            if isinstance(rows, DecodedRows)
            else None
        )
        if group is None:
            group = measure_group(
                rows, indices, key, compiled.declaration, block.columns, block.grid
            )
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
        binned=definition_for_property(compiled.declaration).binned,
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
