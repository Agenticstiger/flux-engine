# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""FLUID SQL/1's six types, the column-type table, and fitting a value to its column.

RFC-data-rules.md §2.3 maps every FLUID column type onto one of six kinds
(bool, number, string, date, timestamp, timestamp with time zone) or onto
none, and §2.4 gives every number a static scale. :func:`column_type` is
that table, read case-insensitively with internal white space collapsed,
as the seam reads FLUID types. A type the table does not list has no
FLUID SQL/1 type, so an expression cannot name the column (``FLX-6111``).

:func:`fit` is the row-time check of EXPR-SPEC-2 §12's ``FLX-6112``: a
computed value is written only when it fits the column's precision, scale
and integer range. The static checker has already made sure the value's
scale fits; ``fit`` still checks it, because it is cheap and the check is
what the code's title promises.

Integer widths follow the names: PostgreSQL's ``int2``/``int4``/``int8``
count bytes and ``int16``/``int32``/``int64`` count bits; ``long``,
``longint`` and ``bigserial`` are 64-bit, as in Avro and Spark. SQL's
spelled-out ``timestamp with time zone`` and ``timestamp without time
zone`` read as ``timestamptz`` and ``timestamp``, and ``bit(1)`` as ``bit``,
as forge-cli's ``fluid-sql-vectors.json`` reads them.

A string column may declare placeholders (RFC §4): values, and prefixes a
value may start with, that stand in for a real value not known yet. A
placeholder is exempt from the column's enum, so comparing the column with
one is not ``FLX-6109``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import List, Mapping, Optional, Sequence, Tuple

from flux_engine.expr2.errors import FLX_6112, Expr2Error
from flux_stats.fidelity import split_type_params

__all__ = [
    "ColumnType",
    "Kind",
    "MAX_DECIMAL_PRECISION",
    "MAX_ROUND_SCALE",
    "NULL_TYPE",
    "SqlType",
    "column_type",
    "fit",
]

#: The widest ``decimal(p,s)`` flux-expr/2 reads: PostgreSQL's numeric limit,
#: the widest decimal any engine FLUID targets declares. A wider declaration
#: has no FLUID SQL/1 type, so ``fit`` never builds a power of ten from a
#: precision a contract can make arbitrarily large.
MAX_DECIMAL_PRECISION = 1000

#: ROUND's scale is at most this (maintainers' decision, 2026-11-16): the
#: widest decimal scale of DuckDB, Snowflake, BigQuery, SQL Server and Arrow's
#: decimal128. Without a bound, ``ROUND(a / 3, 999999999)`` would build a
#: billion-digit number.
MAX_ROUND_SCALE = 38


class Kind(str, Enum):
    """The six FLUID SQL/1 types (RFC-data-rules.md §2.3)."""

    BOOL = "bool"
    NUMBER = "number"
    STRING = "string"
    DATE = "date"
    TIMESTAMP = "timestamp"
    TIMESTAMPTZ = "timestamptz"

    @property
    def orderable(self) -> bool:
        """Whether ``<``, ``BETWEEN``, MIN, MAX and ORDER BY take this kind."""
        return self is not Kind.BOOL


@dataclass(frozen=True)
class SqlType:
    """A static type: a kind and, for a number, its scale (``None`` is unbounded).

    ``kind`` is ``None`` only for the ``NULL`` literal while it is checked:
    it takes its type from a typed operand beside it, and a construct with
    no such operand is refused (EXPR-SPEC-2 §4), so no accepted text has it.
    """

    kind: Optional[Kind]
    scale: Optional[int] = None

    @property
    def is_null(self) -> bool:
        return self.kind is None

    def describe(self) -> str:
        if self.kind is None:
            return "NULL"
        if self.kind is Kind.NUMBER:
            return (
                "number (unbounded scale)" if self.scale is None else f"number (scale {self.scale})"
            )
        return self.kind.value


#: The type of the ``NULL`` literal.
NULL_TYPE = SqlType(None)


@dataclass(frozen=True)
class ColumnType:
    """A column's declared FLUID type, read through RFC-data-rules.md §2.3.

    ``kind`` is ``None`` for a type outside the table. ``scale`` is the
    static scale of a number column (``None`` for a float or a decimal
    without ``(p,s)``). ``precision`` is a decimal's ``p``;
    ``integer_range`` an integer type's inclusive bounds. ``enum`` holds the
    levels the column's ``validationRules`` declare, if any, and
    ``placeholder_values`` and ``placeholder_prefixes`` its placeholders
    (RFC §4), NFC-normalized as the contract's own texts are (RFC §2.3).
    """

    declared: str
    kind: Optional[Kind]
    scale: Optional[int] = None
    precision: Optional[int] = None
    integer_range: Optional[Tuple[int, int]] = None
    is_float: bool = False
    enum: Optional[Tuple[str, ...]] = None
    placeholder_values: Tuple[str, ...] = ()
    placeholder_prefixes: Tuple[str, ...] = ()

    def is_placeholder(self, value: str) -> bool:
        """Whether *value* is one of the column's placeholders: exact and case-sensitive (RFC §4)."""
        return value in self.placeholder_values or any(
            value.startswith(prefix) for prefix in self.placeholder_prefixes
        )

    @property
    def sql_type(self) -> SqlType:
        return SqlType(self.kind, self.scale if self.kind is Kind.NUMBER else None)

    @property
    def derive_target_problem(self) -> Optional[str]:
        """Why a ``derive`` cannot write this column, or ``None`` when it can."""
        if self.kind is None:
            return f"its type {self.declared!r} has no FLUID SQL/1 type"
        if self.is_float:
            return f"{self.declared!r} is a float column, which is never a derive target"
        if self.kind is Kind.NUMBER and self.scale is None:
            return f"{self.declared!r} declares no (p,s), which a derive target needs"
        return None


def _bits(width: int) -> Tuple[int, int]:
    return (-(2 ** (width - 1)), 2 ** (width - 1) - 1)


_INTEGER_WIDTHS: Mapping[str, int] = {
    "tinyint": 8,
    "smallint": 16,
    "int2": 16,
    "int16": 16,
    "mediumint": 24,
    "int": 32,
    "integer": 32,
    "int4": 32,
    "int32": 32,
    "serial": 32,
    "bigint": 64,
    "int8": 64,
    "int64": 64,
    "long": 64,
    "longint": 64,
    "bigserial": 64,
}
_DECIMALS = frozenset({"decimal", "dec", "numeric", "number", "bignumeric"})
_FLOATS = frozenset(
    {"float", "float4", "float8", "float32", "float64", "double", "double precision", "real"}
)
_BOOLS = frozenset({"boolean", "bool", "bit"})
_STRINGS = frozenset(
    {
        "string",
        "text",
        "varchar",
        "varchar2",
        "nvarchar",
        "char",
        "nchar",
        "character",
        "clob",
        "uuid",
        "uniqueidentifier",
        "guid",
        "enum",
    }
)
_TIMESTAMPS = frozenset(
    {
        "datetime",
        "datetime2",
        "smalldatetime",
        "timestamp",
        "timestamp_ntz",
        "timestampntz",
        "timestamp without time zone",
    }
)
_TIMESTAMPTZ = frozenset(
    {"timestamptz", "timestamp_tz", "timestamp_ltz", "timestamp with time zone"}
)

#: A type's name. ``name`` or ``name(params)`` is cut by ``split_type_params``
#: first, which steps over quoted text as the seam and forge-cli's FLUID SQL/1
#: reader do, so an enum level may hold a parenthesis: ``enum('a (b)')`` is a
#: string column.
_TYPE_NAME = re.compile(r"[a-z_][a-z0-9_ ]*")


def column_type(
    declared: str,
    *,
    enum: Optional[Sequence[str]] = None,
    placeholder_values: Sequence[str] = (),
    placeholder_prefixes: Sequence[str] = (),
) -> ColumnType:
    """Read one declared FLUID column type through the §2.3 table.

    Never raises: a type the table does not list, or one whose parameters
    do not parse, gets ``kind=None``, which the checker reports as
    ``FLX-6111`` only if a text names the column. An enum and placeholders
    are kept for a string column only, the one family that takes them.
    """
    # The contract's own texts are NFC-normalized (RFC §2.3), as the text's literals are.
    levels = tuple(_nfc(level) for level in enum) if enum is not None else None
    text = " ".join(declared.strip().lower().split())
    name, params = split_type_params(text)
    if _TYPE_NAME.fullmatch(name) is None:
        return ColumnType(declared, None, enum=levels)
    if name in _BOOLS and (params is None or (name == "bit" and params.strip() == "1")):
        return ColumnType(declared, Kind.BOOL)
    if name in _INTEGER_WIDTHS:
        # A width in parentheses (MySQL's ``int(11)``) is a display width.
        return ColumnType(
            declared, Kind.NUMBER, scale=0, integer_range=_bits(_INTEGER_WIDTHS[name])
        )
    if name in _DECIMALS:
        if params is None:
            return ColumnType(declared, Kind.NUMBER)
        parts = [part.strip() for part in params.split(",")]
        # p and s are SQL unsigned integers, read by value: leading zeros are dropped
        # before the four-digit cap, which keeps int() off a contract's long digit runs.
        digits = [part.lstrip("0") or "0" for part in parts]
        if (
            len(parts) in (1, 2)
            and all(part.isascii() and part.isdigit() for part in parts)
            and all(len(value) <= 4 for value in digits)
        ):
            precision = int(digits[0])
            scale = int(digits[1]) if len(digits) == 2 else 0
            # The SQL standard's rule, as RFC §2.3 reads it: 1 <= p and 0 <= s <= p.
            if 1 <= precision <= MAX_DECIMAL_PRECISION and scale <= precision:
                return ColumnType(declared, Kind.NUMBER, scale=scale, precision=precision)
        return ColumnType(declared, None)
    if name in _FLOATS:
        return ColumnType(declared, Kind.NUMBER, is_float=True)
    if name in _STRINGS:
        return ColumnType(
            declared,
            Kind.STRING,
            enum=levels,
            placeholder_values=tuple(_nfc(value) for value in placeholder_values),
            placeholder_prefixes=tuple(_nfc(prefix) for prefix in placeholder_prefixes),
        )
    if name == "date" and params is None:
        return ColumnType(declared, Kind.DATE)
    if name in _TIMESTAMPS:
        return ColumnType(declared, Kind.TIMESTAMP)
    if name in _TIMESTAMPTZ:
        return ColumnType(declared, Kind.TIMESTAMPTZ)
    return ColumnType(declared, None)


def _nfc(text: str) -> str:
    # column_type never raises: a level that is not a string stays as given.
    return unicodedata.normalize("NFC", text) if isinstance(text, str) else text


_POW10: List[int] = [10**power for power in range(64)]


def _pow10(power: int) -> int:
    return _POW10[power] if 0 <= power < 64 else 10**power


def _where(column_name: str, row: Optional[int]) -> str:
    return f"column {column_name!r}" + ("" if row is None else f", row {row}")


def fit(
    value: object, column: ColumnType, *, column_name: str, row: Optional[int] = None
) -> object:
    """Return *value* as the column holds it, or refuse it with ``FLX-6112``.

    A number comes back as an ``int`` for an integer column and as an exact
    :class:`~decimal.Decimal` at the column's scale for a decimal one, so a
    writer never sees a :class:`~fractions.Fraction`. Every other kind is
    returned as it is. The diagnostic names the column, the row and what is
    wrong, never the value (KERNEL-SPEC §9).
    """
    if value is None or column.kind is not Kind.NUMBER:
        return value
    assert isinstance(value, Fraction), "a number is a Fraction inside flux-expr/2"
    if column.scale is not None:
        # value * 10**scale, in integers: the product is whole when the value's denominator
        # divides 10**scale, and then its numerator is the units.
        factor = _pow10(column.scale)
        denominator = value.denominator
        if factor % denominator:
            where = _where(column_name, row)
            raise Expr2Error(
                FLX_6112, f"{where}: the value has more decimal places than scale {column.scale}"
            )
        units = value.numerator * (factor // denominator)
        if column.precision is not None and abs(units) >= _pow10(column.precision):
            where = _where(column_name, row)
            raise Expr2Error(
                FLX_6112,
                f"{where}: the value needs more than the {column.precision - column.scale} "
                f"integer digits that {column.declared!r} holds",
            )
        if column.integer_range is not None:
            low, high = column.integer_range
            if not low <= units <= high:
                where = _where(column_name, row)
                raise Expr2Error(
                    FLX_6112,
                    f"{where}: the value is outside the integer range of {column.declared!r}",
                )
            return units
        # Decimal(int) is exact, and a digit tuple sets the exponent without the
        # 28-digit default context that scaleb or a division would round in.
        sign, digits, _ = Decimal(units).as_tuple()
        return Decimal((sign, digits, -column.scale))
    return value
