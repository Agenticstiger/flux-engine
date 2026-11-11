# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §1's table: six column types, four families.

§1 in full::

    | Column type (6)                          | Default family        | Families allowed (4) |
    | STRING categorical (declared support …)  | categorical           | categorical, empirical-histogram |
    | INT                                      | poisson/exponential-rate (counts) or uniform (ids) | categorical, bounded-normal, rate, uniform/empirical |
    | DECIMAL(p,s)                             | bounded-normal        | bounded-normal, rate, uniform/empirical, categorical |
    | BOOL                                     | categorical (Bernoulli) | categorical |
    | DATE / TIMESTAMP                         | uniform within timeBounds or rate (inter-arrival) | uniform/empirical, rate |
    | identifier (STRING/uuid with `unique`)   | derived deterministically from (H, ordinal) — never random | — |

:data:`FAMILY_TABLE` is that table, transcribed row for row and closed:
adding a family or a column type changes what a bundle can declare and is
a ``kernelVersion`` bump.

**Two rows are conditional, and this module resolves them explicitly.**
§1 gives INT and DATE/TIMESTAMP a *pair* of defaults ("counts ... or ids",
"uniform ... or rate") without saying which applies when. The rule taken
here, and stated in :func:`choose_family`, is that the declaration decides:
a declared rate means a rate family, a declared support or set of levels
means categorical, a declared range means uniform. Nothing is chosen by
looking at a column's *name*, which would be guessing about a contract
from a string.

**Two logical types have no row at all.** §1's table names six column
types and neither ``JSON`` nor ``BYTES`` is among them. A column of either
is refused by name (``FLX-3401``) rather than given a family the
specification does not grant it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Mapping, Optional, Tuple

from flux_engine.core.errors import FluxError, register
from flux_engine.generator.statistics import FLX_3402, ColumnStatistics
from flux_engine.seam.contract import CompiledColumn, LogicalKind

__all__ = [
    "FAMILY_TABLE",
    "FLX_3401",
    "ColumnType",
    "Family",
    "FamilyRow",
    "choose_family",
    "classify_column",
    "is_identifier",
]

FLX_3401 = register(
    "FLX-3401",
    "no distribution family is available for this column",
    "SEAM-FIDELITY-SPEC §1 pairs six column types with four distribution "
    "families, and the pairing is closed. A column whose logical type has "
    "no row in that table, or whose declaration asks for a family the row "
    "does not allow, cannot be generated: choosing a family outside the "
    "table would produce a dataset the fidelity check was never specified "
    "over.",
)


class Family(Enum):
    """§1's four distribution families.

    ``UNIFORM_EMPIRICAL`` covers both of §1's spellings: plain uniform over
    a declared range, and the "empirical-histogram" case where declared bin
    edges and weights shape the draw. They are one family because they are
    one sampler -- the histogram picks a bin, then draws uniformly inside
    it -- and because §1's allowed-families column names at most one of the
    two per row.
    """

    CATEGORICAL = "categorical"
    BOUNDED_NORMAL = "bounded-normal"
    RATE = "rate"
    UNIFORM_EMPIRICAL = "uniform/empirical"


#: Accepted spellings of a family name in a declaration, normalised.
_FAMILY_ALIASES: Mapping[str, Family] = {
    "categorical": Family.CATEGORICAL,
    "bernoulli": Family.CATEGORICAL,
    "bounded-normal": Family.BOUNDED_NORMAL,
    "bounded_normal": Family.BOUNDED_NORMAL,
    "boundednormal": Family.BOUNDED_NORMAL,
    "rate": Family.RATE,
    "poisson": Family.RATE,
    "exponential": Family.RATE,
    "uniform": Family.UNIFORM_EMPIRICAL,
    "empirical": Family.UNIFORM_EMPIRICAL,
    "uniform/empirical": Family.UNIFORM_EMPIRICAL,
    "empirical-histogram": Family.UNIFORM_EMPIRICAL,
}


class ColumnType(Enum):
    """§1's six column types -- the rows of the table."""

    STRING_CATEGORICAL = "STRING categorical"
    INT = "INT"
    DECIMAL = "DECIMAL(p,s)"
    BOOL = "BOOL"
    DATE_TIMESTAMP = "DATE / TIMESTAMP"
    IDENTIFIER = "identifier"


@dataclass(frozen=True)
class FamilyRow:
    """One row of §1's table."""

    column_type: ColumnType
    allowed: FrozenSet[Family]
    note: str


#: §1's table. ``IDENTIFIER`` allows no family at all -- its dash in the
#: specification is load-bearing: an identifier is derived from ``(H,
#: ordinal)`` and "never random", so there is no distribution to pick.
FAMILY_TABLE: Mapping[ColumnType, FamilyRow] = {
    ColumnType.STRING_CATEGORICAL: FamilyRow(
        column_type=ColumnType.STRING_CATEGORICAL,
        allowed=frozenset({Family.CATEGORICAL, Family.UNIFORM_EMPIRICAL}),
        note="declared support or at most 64 distinct levels",
    ),
    ColumnType.INT: FamilyRow(
        column_type=ColumnType.INT,
        allowed=frozenset(
            {Family.CATEGORICAL, Family.BOUNDED_NORMAL, Family.RATE, Family.UNIFORM_EMPIRICAL}
        ),
        note="counts take a rate; identifiers take a uniform",
    ),
    ColumnType.DECIMAL: FamilyRow(
        column_type=ColumnType.DECIMAL,
        allowed=frozenset(
            {Family.BOUNDED_NORMAL, Family.RATE, Family.UNIFORM_EMPIRICAL, Family.CATEGORICAL}
        ),
        note="bounded-normal by default, over a declared range",
    ),
    ColumnType.BOOL: FamilyRow(
        column_type=ColumnType.BOOL,
        allowed=frozenset({Family.CATEGORICAL}),
        note="a two-level categorical, which is a Bernoulli",
    ),
    ColumnType.DATE_TIMESTAMP: FamilyRow(
        column_type=ColumnType.DATE_TIMESTAMP,
        allowed=frozenset({Family.UNIFORM_EMPIRICAL, Family.RATE}),
        note="uniform within the declared bounds, or an inter-arrival rate",
    ),
    ColumnType.IDENTIFIER: FamilyRow(
        column_type=ColumnType.IDENTIFIER,
        allowed=frozenset(),
        note="derived deterministically from (H, ordinal); never random",
    ),
}

#: Declared type names that make a STRING column an identifier on their own.
_IDENTIFIER_TYPE_NAMES: FrozenSet[str] = frozenset({"uuid", "uniqueidentifier", "guid"})

#: ``semanticType`` and tag values that mark a column as an identifier.
_IDENTIFIER_MARKERS: FrozenSet[str] = frozenset({"identifier", "primary-key", "primary_key"})


def is_identifier(column: CompiledColumn, statistics: ColumnStatistics) -> bool:
    """Whether *column* is §1's identifier row.

    §1 spells the row "identifier (STRING/uuid with ``unique``)". The FLUID
    column schema carries no ``unique`` key, so the four markers below are
    the ways a contract can say it, and they are all *declarations*:

    * the fidelity block declares ``unique: true`` for the column;
    * the declared type is ``uuid``, ``uniqueidentifier`` or ``guid``;
    * ``semanticType`` is ``identifier``;
    * the column is tagged ``identifier`` or ``primary-key``.

    A column's *name* is deliberately not among them. Reading "id" out of
    ``subscriber_id`` would be an inference about a contract from a string,
    and a contract that meant something else would silently get identifiers
    where it asked for data.
    """
    if statistics.unique:
        return True
    if column.declared_type.strip().lower() in _IDENTIFIER_TYPE_NAMES:
        return True
    if column.semantic_type is not None and column.semantic_type.lower() in _IDENTIFIER_MARKERS:
        return True
    return any(tag.lower() in _IDENTIFIER_MARKERS for tag in column.tags)


def classify_column(column: CompiledColumn, statistics: ColumnStatistics) -> ColumnType:
    """Map a compiled column onto one of §1's six column types."""
    kind = column.logical_type.kind
    if is_identifier(column, statistics):
        if kind is not LogicalKind.STRING:
            raise FluxError(
                FLX_3401,
                f"column {column.name!r} is marked as an identifier but is "
                f"{column.type_name}; §1's identifier row is STRING/uuid",
            )
        return ColumnType.IDENTIFIER
    if kind is LogicalKind.STRING:
        return ColumnType.STRING_CATEGORICAL
    if kind is LogicalKind.INT:
        return ColumnType.INT
    if kind is LogicalKind.DECIMAL:
        return ColumnType.DECIMAL
    if kind is LogicalKind.BOOL:
        return ColumnType.BOOL
    if kind in (LogicalKind.DATE, LogicalKind.TIMESTAMP):
        return ColumnType.DATE_TIMESTAMP
    raise FluxError(
        FLX_3401,
        f"column {column.name!r} is {column.type_name}; SEAM-FIDELITY-SPEC §1's table "
        f"names six column types and {kind.value} is not one of them",
    )


def _declared_family(statistics: ColumnStatistics, column_type: ColumnType) -> Optional[Family]:
    if statistics.family is None:
        return None
    family = _FAMILY_ALIASES.get(statistics.family.lower())
    if family is None:
        raise FluxError(
            FLX_3401,
            f"{statistics.column}: family {statistics.family!r} is not one of "
            f"{sorted(member.value for member in Family)}",
        )
    allowed = FAMILY_TABLE[column_type].allowed
    if family not in allowed:
        raise FluxError(
            FLX_3401,
            f"{statistics.column}: §1 allows {sorted(member.value for member in allowed)} "
            f"for a {column_type.value} column, not {family.value!r}",
        )
    return family


def choose_family(
    column: CompiledColumn, statistics: ColumnStatistics, column_type: ColumnType
) -> Optional[Family]:
    """Pick the family for *column*, or ``None`` for §1's identifier row.

    An explicit ``family`` in the declaration wins, checked against the
    row's allowed set. Otherwise the default comes from what the column
    declares, in this order -- the order is the resolution of §1's two
    conditional default cells, and it runs from the most specific
    declaration to the least:

    1. ``levels`` or a listed ``support`` -> ``categorical``;
    2. ``rate`` (or, for an ``INT``, a ``mean`` with no range) -> ``rate``;
    3. declared ``bins`` -> ``uniform/empirical`` (the histogram case);
    4. the row's own default: ``bounded-normal`` for ``DECIMAL``,
       ``uniform/empirical`` for ``INT`` and ``DATE``/``TIMESTAMP``,
       ``categorical`` for ``STRING`` and ``BOOL``.

    The choice is refused (``FLX-3402``) when the row's default needs a
    declaration the column does not carry -- a categorical with no levels
    and no support, a bounded-normal or a uniform with no range. §1 says
    parameters come from declared aggregate statistics; with none declared
    there is nothing to come from, and inventing one is what this refusal
    exists to prevent.
    """
    if column_type is ColumnType.IDENTIFIER:
        return None

    declared = _declared_family(statistics, column_type)
    allowed = FAMILY_TABLE[column_type].allowed
    if declared is not None:
        return declared

    if statistics.levels is not None or statistics.support or statistics.support_is_declared:
        # ``support: declared`` counts as declaring a categorical intent even
        # though it names no levels: the author asked for the contract's own
        # support set, and the refusal that follows says why the contract
        # cannot supply one. Falling through to the generic "nothing is
        # declared" branch would answer a question nobody asked.
        return _require_allowed(Family.CATEGORICAL, allowed, statistics, column_type)
    if statistics.rate is not None:
        return _require_allowed(Family.RATE, allowed, statistics, column_type)
    if column_type is ColumnType.INT and statistics.mean is not None and not statistics.has_range:
        return _require_allowed(Family.RATE, allowed, statistics, column_type)
    if statistics.bins is not None:
        return _require_allowed(Family.UNIFORM_EMPIRICAL, allowed, statistics, column_type)
    if statistics.has_range:
        default = (
            Family.BOUNDED_NORMAL if column_type is ColumnType.DECIMAL else Family.UNIFORM_EMPIRICAL
        )
        return _require_allowed(default, allowed, statistics, column_type)

    raise FluxError(
        FLX_3402,
        f"{statistics.column}: a {column_type.value} column declares no range, support, "
        "levels, rate or bins, so no family of §1's table has parameters. Declare one "
        f"under emits[].fidelity.columns[{statistics.column!r}].",
    )


def _require_allowed(
    family: Family,
    allowed: FrozenSet[Family],
    statistics: ColumnStatistics,
    column_type: ColumnType,
) -> Family:
    if family not in allowed:
        raise FluxError(
            FLX_3401,
            f"{statistics.column}: the declaration implies {family.value!r}, which §1 does "
            f"not allow for a {column_type.value} column "
            f"({sorted(member.value for member in allowed)})",
        )
    return family


def family_choices() -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """§1's table as plain data, for a test that pins it against the spec."""
    return tuple(
        (row.column_type.value, tuple(sorted(member.value for member in row.allowed)))
        for row in FAMILY_TABLE.values()
    )
