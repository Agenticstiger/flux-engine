# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The contract-schema compiler: a FLUID expose becomes an ordered column set.

Implements the first half of KERNEL-SPEC §9 -- the closed ``LogicalType``
set and the rule that "FLUID types are normalised case-insensitively;
anything outside the set fails by name (``FLX-3001``)".

The seam is where a declaration stops being prose and starts being bytes.
Everything downstream of this module -- the row renderer
(:mod:`flux_engine.seam.conform`), the ordered emit header
(:mod:`flux_engine.seam.emit`), the contract-schema generator
(:mod:`flux_engine.generator`) -- reads a :class:`CompiledExpose` and never
re-reads the YAML, so there is exactly one place that decides what a
declared type means.

Three decisions this module takes, each because the alternative fails open.

**The FLUID type vocabulary is wide; the logical set is eight.** A FLUID
contract may spell a column ``bigint``, ``int8`` or ``INTEGER``; all three
are ``INT``. :data:`TYPE_ALIASES` is the whole mapping, written out rather
than derived from a prefix rule, so a reader can see every accepted
spelling and a new one is a deliberate edit.

**Binary floating-point column types are refused by name.** ``float``,
``double`` and ``real`` are legal FLUID types and have no reproducible
rendering: KERNEL-SPEC §9 ends with "``float`` -> ``FLX-3002``", and §2.1
forbids a float anywhere in a hashed value. Rather than admit the column
and fail on every row, the compiler refuses the *declaration*
(``FLX-3001``), naming the type. The same holds for ``time``,
``interval`` and the geospatial types, which the closed set has no
rendering for.

**A bare ``decimal`` is not ``DECIMAL(p,s)``.** §9's set member carries a
precision and a scale, and "DECIMAL -> string at scale ``s``" has no
meaning without one. Choosing a default scale here would quietly decide
how every number in the stream is spelled, so an unparameterised decimal
is refused by name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError, register
from flux_engine.seam.range_rules import declared_range_bounds
from flux_stats.fidelity import FLX_3001, declared_enum_values, split_type_params

__all__ = [
    "FLX_3001",
    "FLX_3004",
    "CompiledColumn",
    "CompiledExpose",
    "LogicalKind",
    "LogicalType",
    "TYPE_ALIASES",
    "UNSUPPORTED_TYPES",
    "compile_expose",
    "declared_enum_values",
    "find_expose",
    "parse_logical_type",
]

FLX_3004 = register(
    "FLX-3004",
    "expose schema cannot be compiled into an ordered column set",
    "The seam needs an ordered, non-empty list of uniquely-named columns: "
    "rows travel as arrays aligned to that order (KERNEL-SPEC §8), so a "
    "missing schema, an empty one, a column with no name, or two columns "
    "that share a name after NFC normalisation leaves no way to place a "
    "value. The declaration is refused rather than repaired.",
)


class LogicalKind(str, Enum):
    """KERNEL-SPEC §9's closed logical type set.

    Closed in the strong sense: adding a member changes what the seam can
    render and is a ``kernelVersion`` bump with a ``GOLDEN-CHANGELOG.md``
    row, because every existing stream's header carries these names.
    """

    STRING = "STRING"
    INT = "INT"
    DECIMAL = "DECIMAL"
    BOOL = "BOOL"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    JSON = "JSON"
    BYTES = "BYTES"


#: Every FLUID / SQL type name this seam accepts, mapped onto its logical
#: kind. Keys are lowercase with internal whitespace collapsed to a single
#: space; the lookup normalises the declared text the same way.
#:
#: The vocabulary is the enum of the vendored FLUID column schema, so a
#: contract that validates against FLUID either compiles here or is named
#: in :data:`UNSUPPORTED_TYPES` -- there is no third outcome where a
#: declared type silently becomes something else.
TYPE_ALIASES: Mapping[str, LogicalKind] = {
    # STRING
    "string": LogicalKind.STRING,
    "text": LogicalKind.STRING,
    "varchar": LogicalKind.STRING,
    "varchar2": LogicalKind.STRING,
    "nvarchar": LogicalKind.STRING,
    "char": LogicalKind.STRING,
    "nchar": LogicalKind.STRING,
    "character": LogicalKind.STRING,
    "clob": LogicalKind.STRING,
    "enum": LogicalKind.STRING,
    "uuid": LogicalKind.STRING,
    "uniqueidentifier": LogicalKind.STRING,
    "guid": LogicalKind.STRING,
    # INT
    "int": LogicalKind.INT,
    "integer": LogicalKind.INT,
    "int2": LogicalKind.INT,
    "int4": LogicalKind.INT,
    "int8": LogicalKind.INT,
    "int16": LogicalKind.INT,
    "int32": LogicalKind.INT,
    "int64": LogicalKind.INT,
    "tinyint": LogicalKind.INT,
    "smallint": LogicalKind.INT,
    "mediumint": LogicalKind.INT,
    "bigint": LogicalKind.INT,
    "long": LogicalKind.INT,
    "longint": LogicalKind.INT,
    "serial": LogicalKind.INT,
    "bigserial": LogicalKind.INT,
    "year": LogicalKind.INT,
    # DECIMAL -- always parameterised; see _require_decimal_parameters.
    "decimal": LogicalKind.DECIMAL,
    "dec": LogicalKind.DECIMAL,
    "numeric": LogicalKind.DECIMAL,
    "number": LogicalKind.DECIMAL,
    "bignumeric": LogicalKind.DECIMAL,
    "money": LogicalKind.DECIMAL,
    # BOOL
    "boolean": LogicalKind.BOOL,
    "bool": LogicalKind.BOOL,
    "bit": LogicalKind.BOOL,
    # DATE
    "date": LogicalKind.DATE,
    # TIMESTAMP
    "timestamp": LogicalKind.TIMESTAMP,
    "timestamptz": LogicalKind.TIMESTAMP,
    "timestamp_tz": LogicalKind.TIMESTAMP,
    "timestamp_ntz": LogicalKind.TIMESTAMP,
    "timestamp_ltz": LogicalKind.TIMESTAMP,
    "timestampntz": LogicalKind.TIMESTAMP,
    "timestamp with time zone": LogicalKind.TIMESTAMP,
    "timestamp without time zone": LogicalKind.TIMESTAMP,
    "datetime": LogicalKind.TIMESTAMP,
    "datetime2": LogicalKind.TIMESTAMP,
    "smalldatetime": LogicalKind.TIMESTAMP,
    # JSON
    "json": LogicalKind.JSON,
    "jsonb": LogicalKind.JSON,
    "super": LogicalKind.JSON,
    "variant": LogicalKind.JSON,
    "object": LogicalKind.JSON,
    "array": LogicalKind.JSON,
    "struct": LogicalKind.JSON,
    "map": LogicalKind.JSON,
    "record": LogicalKind.JSON,
    "row": LogicalKind.JSON,
    # BYTES
    "binary": LogicalKind.BYTES,
    "varbinary": LogicalKind.BYTES,
    "bytes": LogicalKind.BYTES,
    "blob": LogicalKind.BYTES,
    "bytea": LogicalKind.BYTES,
    "raw": LogicalKind.BYTES,
}

#: FLUID type names this seam refuses on purpose, each with the reason
#: quoted in the ``FLX-3001`` detail. Refusing by name -- rather than
#: letting the alias lookup miss -- is what turns "we do not support this"
#: into a sentence an author can act on.
UNSUPPORTED_TYPES: Mapping[str, str] = {
    "float": "binary floating point has no reproducible rendering (KERNEL-SPEC §2.1)",
    "float4": "binary floating point has no reproducible rendering (KERNEL-SPEC §2.1)",
    "float8": "binary floating point has no reproducible rendering (KERNEL-SPEC §2.1)",
    "float32": "binary floating point has no reproducible rendering (KERNEL-SPEC §2.1)",
    "float64": "binary floating point has no reproducible rendering (KERNEL-SPEC §2.1)",
    "double": "binary floating point has no reproducible rendering (KERNEL-SPEC §2.1)",
    "double precision": "binary floating point has no reproducible rendering (KERNEL-SPEC §2.1)",
    "real": "binary floating point has no reproducible rendering (KERNEL-SPEC §2.1)",
    "time": "the closed set carries DATE and TIMESTAMP, not a bare time of day",
    "interval": "the closed set carries no duration rendering",
    "geography": "the closed set carries no geospatial rendering",
    "geometry": "the closed set carries no geospatial rendering",
    "geom": "the closed set carries no geospatial rendering",
    "point": "the closed set carries no geospatial rendering",
    "hll": "a sketch is not a value the seam can render",
}

#: A type's name. ``name`` or ``name(a, b)`` is cut by ``split_type_params``
#: first, which steps over quoted text, so a parameter list may hold a
#: parenthesis inside quotes (``enum('a (b)')``); what is left before the list
#: has to be a name.
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_ ]*")

#: Kinds whose parameters (if any) do not change how a value is rendered.
#: ``varchar(255)`` renders exactly as ``varchar`` does; the length is a
#: storage hint the seam does not enforce, and pretending otherwise would
#: put a truncation rule into the digest that no specification names.
_PARAMETERS_IGNORED: FrozenSet[LogicalKind] = frozenset(
    {
        LogicalKind.STRING,
        LogicalKind.INT,
        LogicalKind.BOOL,
        LogicalKind.DATE,
        LogicalKind.TIMESTAMP,
        LogicalKind.JSON,
        LogicalKind.BYTES,
    }
)


#: The width, in bits, of each integer type a contract may name. A bare ``int`` and
#: ``year`` name none: the closed set's INT is any whole number a row can carry, and the
#: generator and the sinks hold such a column to the signed 64 bits the rendering allows.
_INTEGER_BITS: Mapping[str, int] = {
    "tinyint": 8,
    "smallint": 16,
    "int2": 16,
    "int16": 16,
    "mediumint": 24,
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


def integer_bits(declared_type: str) -> Optional[int]:
    """The width in bits the contract's own spelling gives an integer type, or ``None``.

    Read from what the contract wrote (``smallint``, ``INTEGER(11)``, ``BigInt``), which no
    digest covers: the header carries the closed set's ``INT`` alone, so a width moves
    no digest.
    """
    name, _ = split_type_params(_normalise_type_text(declared_type))
    return _INTEGER_BITS.get(" ".join(name.split()))


@dataclass(frozen=True)
class LogicalType:
    """One member of KERNEL-SPEC §9's closed set, with its parameters.

    :attr:`precision` and :attr:`scale` are set only for
    :attr:`LogicalKind.DECIMAL`, where §9's rendering rule ("string at
    scale ``s``") depends on them. ``str(logical_type)`` is the canonical
    spelling that travels in the emit header's ``types`` array, so it is
    part of what :func:`flux_engine.seam.emit.output_digest` covers.
    """

    kind: LogicalKind
    precision: Optional[int] = None
    scale: Optional[int] = None

    def __post_init__(self) -> None:
        if self.kind is LogicalKind.DECIMAL:
            if self.precision is None or self.scale is None:
                raise FluxError(FLX_3001, "DECIMAL requires a precision and a scale")
            if self.precision < 1:
                raise FluxError(FLX_3001, f"DECIMAL precision must be >= 1, got {self.precision}")
            if not 0 <= self.scale <= self.precision:
                raise FluxError(
                    FLX_3001,
                    f"DECIMAL scale must lie in [0, precision], got {self.scale} "
                    f"with precision {self.precision}",
                )
        elif self.precision is not None or self.scale is not None:
            raise FluxError(FLX_3001, f"{self.kind.value} carries no precision or scale")

    def __str__(self) -> str:
        if self.kind is LogicalKind.DECIMAL:
            return f"DECIMAL({self.precision},{self.scale})"
        return self.kind.value

    @property
    def is_decimal(self) -> bool:
        """Whether this is the parameterised ``DECIMAL(p,s)`` member."""
        return self.kind is LogicalKind.DECIMAL


def _normalise_type_text(declared: str) -> str:
    """Lowercase *declared* and collapse internal whitespace to single spaces."""
    return " ".join(nfc(declared).strip().lower().split())


def parse_logical_type(declared: Any) -> LogicalType:
    """Map a declared FLUID type name onto its :class:`LogicalType`.

    KERNEL-SPEC §9: "FLUID types are normalised case-insensitively;
    anything outside the set fails by name (``FLX-3001``)". The failure
    quotes the declared spelling exactly as the contract wrote it, because
    that is the string the author has to find and change.
    """
    if not isinstance(declared, str) or not declared.strip():
        raise FluxError(FLX_3001, f"declared type must be a non-empty string, got {declared!r}")
    text = _normalise_type_text(declared)
    name_text, raw_params = split_type_params(text)
    if _TYPE_NAME.fullmatch(name_text) is None:
        raise FluxError(FLX_3001, f"declared type {declared!r} is not a type name")
    name = " ".join(name_text.split())

    reason = UNSUPPORTED_TYPES.get(name)
    if reason is not None:
        raise FluxError(FLX_3001, f"declared type {declared!r} is refused: {reason}")

    kind = TYPE_ALIASES.get(name)
    if kind is None:
        raise FluxError(
            FLX_3001,
            f"declared type {declared!r} maps onto no member of the closed set "
            f"({', '.join(member.value for member in LogicalKind)})",
        )

    if kind is LogicalKind.DECIMAL:
        precision, scale = _require_decimal_parameters(declared, raw_params)
        return LogicalType(kind=kind, precision=precision, scale=scale)
    if kind in _PARAMETERS_IGNORED:
        return LogicalType(kind=kind)
    raise FluxError(
        FLX_3001, f"declared type {declared!r} has no parameter rule"
    )  # pragma: no cover


def _require_decimal_parameters(declared: str, raw_params: Optional[str]) -> Tuple[int, int]:
    """Return ``(precision, scale)`` for a decimal, or raise ``FLX-3001``.

    §9's set member is ``DECIMAL(p,s)``. An unparameterised ``decimal``
    leaves the rendering scale undecided, and a default chosen here would
    silently fix the spelling of every number in the stream, so it is
    refused by name.
    """
    if raw_params is None:
        raise FluxError(
            FLX_3001,
            f"declared type {declared!r} carries no precision and scale; the closed set's "
            "member is DECIMAL(p,s), as in DECIMAL(18,4)",
        )
    parts = [part.strip() for part in raw_params.split(",")]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise FluxError(
            FLX_3001,
            f"declared type {declared!r} does not carry two integer parameters (p,s)",
        )
    return int(parts[0]), int(parts[1])


@dataclass(frozen=True)
class CompiledColumn:
    """One column of a compiled expose.

    Attributes
    ----------
    name
        The column name in NFC. Positional in the emitted row array.
    logical_type
        The §9 member the declared type normalised onto.
    required
        The contract's ``required`` flag. A row that carries no value for
        a required column is refused (``FLX-3202``); a non-required column
        renders a missing value as JSON ``null``.
    declared_type
        The contract's own spelling, kept verbatim for diagnostics. Never
        hashed: the header carries ``str(logical_type)``, so two contracts
        that spell one type differently produce one stream digest.
    semantic_type, tags
        Declaration-level markers the generator reads to recognise an
        identifier column (SEAM-FIDELITY-SPEC §1). Kept here so the
        generator never re-reads the YAML.
    key
        Whether the contract makes this column a one-column key of its
        expose (keys from the contract, :mod:`flux_engine.seam.keys`). Set by
        :meth:`flux_engine.seam.resolve.ResolvedEmit.compile`, which sees
        the whole contract; :func:`compile_expose` sees one expose and
        leaves it ``False``.
    hidden
        Whether the column is a hidden column of the emit (ADR 0024 section 2): planned and
        generated as a contract column is, and never written. Only a column of a plan's
        expose carries it; a compiled expose that is written never does.
    declared_range
        The inclusive ``(low, high)`` milliseconds a DATE or TIMESTAMP column's ``range`` rules
        declare (:func:`~flux_engine.seam.range_rules.declared_range_bounds`), or ``None``; an end
        not declared is ``None``, and a strict end is held one millisecond inside. Never hashed.
    declared_range_strict
        Which ends of ``declared_range`` the contract spells strict, for ``verify``'s text.
    """

    name: str
    logical_type: LogicalType
    required: bool
    declared_type: str
    semantic_type: Optional[str] = None
    tags: Tuple[str, ...] = ()
    key: bool = False
    hidden: bool = False
    declared_range: Optional[Tuple[Optional[int], Optional[int]]] = None
    declared_range_strict: Tuple[bool, bool] = (False, False)

    @property
    def type_name(self) -> str:
        """The canonical spelling that travels in the emit header."""
        return str(self.logical_type)

    @property
    def enum_values(self) -> Tuple[str, ...]:
        """The levels the declared type names, or ``()`` -- see
        :func:`declared_enum_values`.

        Derived from :attr:`declared_type` rather than stored, so a column
        constructed anywhere in the engine cannot carry a level set its own
        declaration does not support.
        """
        return declared_enum_values(self.declared_type)


@dataclass(frozen=True)
class CompiledExpose:
    """An expose reduced to what the seam needs: an id and ordered columns.

    The column order is the contract's declared order and is load-bearing:
    KERNEL-SPEC §8 emits rows as "arrays aligned to an ordered header ...
    (JCS sorts object keys, so objects would erase the order)". Reordering
    the schema in the contract is therefore a change to the stream, and
    the digest says so.
    """

    expose_id: str
    columns: Tuple[CompiledColumn, ...]

    @property
    def hidden_count(self) -> int:
        """How many columns, at the end, are hidden: the columns a row holds and no stream writes."""
        return sum(1 for column in self.columns if column.hidden)

    @property
    def written(self) -> "CompiledExpose":
        """This expose without its hidden columns, which come after the written ones."""
        if not self.hidden_count:
            return self
        return CompiledExpose(
            expose_id=self.expose_id,
            columns=tuple(column for column in self.columns if not column.hidden),
        )

    @property
    def column_names(self) -> Tuple[str, ...]:
        """Declared column names, in declared order."""
        return tuple(column.name for column in self.columns)

    @property
    def type_names(self) -> Tuple[str, ...]:
        """Canonical logical type spellings, positionally aligned to the columns."""
        return tuple(column.type_name for column in self.columns)

    @property
    def column_types(self) -> Mapping[str, str]:
        """Column name -> the contract's own type spelling, NFC-sorted.

        The member
        :class:`flux_engine.concordance.predicate.ExposeSchema` needs so a
        fidelity block's ``support: declared`` can be resolved against the
        declared type (:func:`declared_enum_values`) from either a
        compiled expose or one read straight out of a parsed contract.
        """
        return {
            column.name: column.declared_type
            for column in sorted(self.columns, key=lambda c: c.name)
        }

    @property
    def index_of(self) -> Mapping[str, int]:
        """Column name -> position, for the mapping-to-array conversion."""
        return {column.name: index for index, column in enumerate(self.columns)}

    def column(self, name: str) -> CompiledColumn:
        """Return the column called *name*, or raise ``FLX-3004``."""
        wanted = nfc(name)
        for candidate in self.columns:
            if candidate.name == wanted:
                return candidate
        raise FluxError(FLX_3004, f"expose {self.expose_id!r} declares no column {name!r}")


def find_expose(contract_document: Mapping[str, Any], expose_id: str) -> Mapping[str, Any]:
    """Return the ``exposes[]`` entry called *expose_id*.

    Raises ``FLX-3004`` when the document declares no such expose. The
    seam-level "this product has no such expose" failure is
    ``FLX-3103`` and is raised by :mod:`flux_engine.seam.resolve`, which
    knows which emit asked; this function is the plain lookup.
    """
    wanted = nfc(expose_id)
    exposes = contract_document.get("exposes")
    if not isinstance(exposes, Sequence) or isinstance(exposes, (str, bytes)):
        raise FluxError(FLX_3004, "contract document declares no exposes[] array")
    for entry in exposes:
        if isinstance(entry, Mapping) and nfc(str(entry.get("exposeId", ""))) == wanted:
            return entry
    raise FluxError(FLX_3004, f"contract declares no expose {expose_id!r}")


def compile_expose(expose: Mapping[str, Any]) -> CompiledExpose:
    """Compile one FLUID ``exposes[]`` entry into an ordered column set.

    Every declared type is resolved through :func:`parse_logical_type`, so
    the whole schema is accepted or the first refused column names itself
    (``FLX-3001``). A schema that cannot be turned into an ordered,
    uniquely-named, non-empty column list is refused with ``FLX-3004``.

    Column names are NFC-normalised: two spellings of one name are one
    name (KERNEL-SPEC §2.1), and a schema that carries both is a
    collision, not two columns.
    """
    expose_id = expose.get("exposeId")
    if not isinstance(expose_id, str) or not expose_id.strip():
        raise FluxError(FLX_3004, "exposes[] entry has no non-empty string exposeId")
    contract = expose.get("contract")
    if not isinstance(contract, Mapping):
        raise FluxError(FLX_3004, f"expose {expose_id!r} has no contract block")
    schema = contract.get("schema")
    if not isinstance(schema, Sequence) or isinstance(schema, (str, bytes)) or not schema:
        raise FluxError(
            FLX_3004,
            f"expose {expose_id!r} declares no non-empty contract.schema; the seam emits "
            "rows as arrays aligned to a declared column order",
        )

    columns: List[CompiledColumn] = []
    seen: Dict[str, int] = {}
    for index, entry in enumerate(schema):
        if not isinstance(entry, Mapping):
            raise FluxError(
                FLX_3004,
                f"expose {expose_id!r} column {index}: schema entry is "
                f"{type(entry).__name__}, not a mapping",
            )
        raw_name = entry.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise FluxError(
                FLX_3004, f"expose {expose_id!r} column {index}: no non-empty string name"
            )
        name = nfc(raw_name)
        if name in seen:
            raise FluxError(
                FLX_3004,
                f"expose {expose_id!r}: columns {seen[name]} and {index} share the name "
                f"{name!r} after NFC normalisation",
            )
        seen[name] = index
        declared_type = entry.get("type")
        try:
            logical_type = parse_logical_type(declared_type)
        except FluxError as exc:
            raise FluxError(
                exc.code,
                f"expose {expose_id!r} column {name!r}: {exc.detail}",
            ) from exc
        held_range, held_strict = _declared_range(entry, logical_type)
        columns.append(
            CompiledColumn(
                name=name,
                logical_type=logical_type,
                required=bool(entry.get("required", False)),
                declared_type=str(declared_type),
                semantic_type=_optional_nfc(entry.get("semanticType")),
                tags=_string_tuple(entry.get("tags")),
                declared_range=held_range,
                declared_range_strict=held_strict,
            )
        )
    return CompiledExpose(expose_id=nfc(expose_id), columns=tuple(columns))


def _declared_range(
    entry: Mapping[str, Any], logical_type: LogicalType
) -> Tuple[Optional[Tuple[Optional[int], Optional[int]]], Tuple[bool, bool]]:
    """The inclusive ``(low, high)`` milliseconds a DATE or TIMESTAMP column's ``range`` rules declare, and which ends are strict.

    The grammar is :func:`flux_engine.seam.range_rules.declared_range_bounds`'s. A strict end is moved one
    millisecond inward, so every consumer compares inclusively: the draw holds the instants beyond
    the end, and a DATE's day is the one whose midnight lies beyond it.
    """
    none: Tuple[Optional[Tuple[Optional[int], Optional[int]]], Tuple[bool, bool]] = (
        None,
        (False, False),
    )
    if logical_type.kind not in (LogicalKind.DATE, LogicalKind.TIMESTAMP):
        return none
    if not entry.get("validationRules"):
        return none
    bounds = declared_range_bounds(entry)
    if bounds is None:
        return none
    low = bounds.low + 1 if bounds.low is not None and bounds.low_strict else bounds.low
    high = bounds.high - 1 if bounds.high is not None and bounds.high_strict else bounds.high
    return (low, high), (bounds.low_strict, bounds.high_strict)


def _optional_nfc(value: Any) -> Optional[str]:
    return nfc(value) if isinstance(value, str) else None


def _string_tuple(value: Any) -> Tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(nfc(item) for item in value if isinstance(item, str))
    return ()
