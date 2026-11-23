# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The bundle's schema as the consumer SQL check reads it (ADR 0028 section 2).

Each expose of a bundle with its contract columns, their DuckDB types, and the keys the contract
declares unique. This module imports no sqlglot, so building a schema costs nothing on a path that
never checks a query.

**Types.** One DuckDB type per logical type of KERNEL-SPEC section 9, the same choice
:mod:`flux_engine.sinks.arrow` makes for Arrow (that module needs pyarrow, which this one does
not, so its map is repeated here and a test pins the two together):

==============  ==============================
logical type    DuckDB type
==============  ==============================
STRING          ``VARCHAR``
INT             ``BIGINT``
DECIMAL(p,s)    ``DECIMAL(p,s)``, p <= 38
BOOL            ``BOOLEAN``
DATE            ``DATE``
TIMESTAMP       ``TIMESTAMP``
JSON            ``VARCHAR``
BYTES           ``BLOB``
==============  ==============================

DuckDB holds a decimal of at most 38 digits, and Arrow holds up to 76, so a wider one is refused
here (``FLX-3635``) rather than at registration.

**Unique keys.** An expose's ``unique`` is a tuple of keys, each a tuple of column names that the
contract declares unique across the rows of the expose: a one-column key (a primary or unique
entity, or a column the contract marks as a key), a relationship's target key, and any column a
caller adds (a fidelity ``unique: true`` column). The total-order check reads them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Set, Tuple

from flux_engine.consumers.errors import FLX_3635
from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.seam.contract import CompiledExpose, LogicalKind, LogicalType
from flux_engine.seam.keys import ExposeKeys

__all__ = [
    "BundleSchema",
    "ExposeSchema",
    "bundle_schema",
    "expose_schema",
    "sql_type",
]

#: The widest decimal DuckDB holds.
_DUCKDB_DECIMAL_MOST = 38

_SIMPLE: Mapping[LogicalKind, str] = {
    LogicalKind.STRING: "VARCHAR",
    LogicalKind.INT: "BIGINT",
    LogicalKind.BOOL: "BOOLEAN",
    LogicalKind.DATE: "DATE",
    LogicalKind.TIMESTAMP: "TIMESTAMP",
    LogicalKind.JSON: "VARCHAR",
    LogicalKind.BYTES: "BLOB",
}


def sql_type(logical: LogicalType, *, where: str = "") -> str:
    """The DuckDB type that holds *logical*, or ``FLX-3635`` for a decimal DuckDB cannot hold."""
    if logical.kind is LogicalKind.DECIMAL:
        precision, scale = logical.precision, logical.scale
        assert precision is not None and scale is not None  # LogicalType.__post_init__
        if precision > _DUCKDB_DECIMAL_MOST:
            raise FluxError(
                FLX_3635,
                f"{where or 'a column'} is {logical}: DuckDB's widest decimal holds "
                f"{_DUCKDB_DECIMAL_MOST} digits",
            )
        return f"DECIMAL({precision},{scale})"
    return _SIMPLE[logical.kind]


@dataclass(frozen=True)
class ExposeSchema:
    """One expose: its columns with their DuckDB types, and the keys it declares unique."""

    expose_id: str
    #: ``(contract column name, DuckDB type)`` in the contract's order.
    columns: Tuple[Tuple[str, str], ...]
    #: Each key is a tuple of column names; the expose keeps it unique across all its rows.
    unique: Tuple[Tuple[str, ...], ...] = ()

    @property
    def column_names(self) -> Tuple[str, ...]:
        return tuple(name for name, _ in self.columns)


@dataclass(frozen=True)
class BundleSchema:
    """Every expose of a bundle, by exposeId."""

    exposes: Tuple[ExposeSchema, ...]

    def expose(self, expose_id: str) -> Optional[ExposeSchema]:
        for candidate in self.exposes:
            if candidate.expose_id == expose_id:
                return candidate
        return None


def expose_schema(
    compiled: CompiledExpose,
    keys: Optional[ExposeKeys] = None,
    unique_columns: Iterable[str] = (),
) -> ExposeSchema:
    """The schema of one compiled expose.

    *keys* is what :func:`flux_engine.seam.keys.compile_keys` reads for it, if the contract
    declares keys; *unique_columns* are columns declared unique some other way. A column the
    compiled expose marks as a key counts too.
    """
    names = compiled.column_names
    columns = tuple(
        (
            column.name,
            sql_type(
                column.logical_type, where=f"column {column.name!r} of {compiled.expose_id!r}"
            ),
        )
        for column in compiled.columns
    )
    found: Set[Tuple[str, ...]] = {(column.name,) for column in compiled.columns if column.key}
    if keys is not None:
        found.update(tuple(key) for key in keys.keys)
    for raw in unique_columns:
        name = nfc(raw)
        if name not in names:
            raise FluxError(
                FLX_3635,
                f"expose {compiled.expose_id!r} declares {raw!r} unique and has no such column",
            )
        found.add((name,))
    for key in found:
        if not all(name in names for name in key):
            raise FluxError(
                FLX_3635,
                f"expose {compiled.expose_id!r} declares the key {list(key)}, "
                "which names a column it does not have",
            )
    return ExposeSchema(expose_id=compiled.expose_id, columns=columns, unique=tuple(sorted(found)))


def bundle_schema(exposes: Iterable[ExposeSchema]) -> BundleSchema:
    """The schema of a bundle from its exposes' schemas; an exposeId given twice is ``FLX-3635``."""
    seen: Dict[str, ExposeSchema] = {}
    for item in exposes:
        if item.expose_id in seen:
            raise FluxError(FLX_3635, f"the schema holds the expose {item.expose_id!r} twice")
        seen[item.expose_id] = item
    return BundleSchema(exposes=tuple(seen[name] for name in sorted(seen)))
