# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Arrow batching layer: rendered rows in, typed RecordBatches out, and back.

Every columnar sink (Parquet, Arrow IPC, ADBC) starts here. A sink receives
rows whose values KERNEL-SPEC §9 has already rendered -- ``str``, ``int``,
``bool`` or ``None``, never a float -- and this module turns runs of them
into :class:`pyarrow.RecordBatch` objects typed from the stream's declared
logical types, then turns batches back into the same rendered rows.

**The type map.** One Arrow type per §9 logical type, chosen so the value
survives exactly:

==============  ==============================  =======================================
logical type    Arrow type                      from the rendered value
==============  ==============================  =======================================
STRING          ``string``                      as is (already NFC)
INT             ``int64``                       as is (JCS bounds it to 2**53 - 1)
DECIMAL(p,s)    ``decimal128(p,s)``, p <= 38    Arrow's own string cast, exact;
                ``decimal256(p,s)``, p <= 76    p > 76 is ``FLX-3502``
BOOL            ``bool``                        as is
DATE            ``date32``                      Arrow's string cast of ``YYYY-MM-DD``
TIMESTAMP       ``timestamp[ms, tz=UTC]``       Arrow's string cast of the RFC 3339 text
JSON            ``string``                      the canonical JSON text, as is
BYTES           ``binary``                      base64-decoded
==============  ==============================  =======================================

A DECIMAL never passes through ``float``: the rendered string is parsed by
Arrow's C++ cast, which refuses (rather than rounds) a value with more
fractional digits than the scale or more integer digits than the precision.

**Text kinds.** A destination without an exact type for a kind (SQLite has
no decimal, and its ADBC driver writes a timestamp as text without the
``Z``) asks for that kind as *text*: the column is a ``string`` holding the
§9 rendering itself, which is exact by construction. ``text_kinds`` is that
request.

**The metadata carries the stream.** Each field records its logical type
under ``flux.type``, and the schema records the §8 header document, JCS
bytes and all, under ``flux.header``. A file or a table written from these
batches therefore says which stream it is, and :func:`output_digest_of`
recomputes that stream's ``outputDigest`` from the data alone: it turns the
batches back into rendered rows (:func:`rows_from_table`), frames each one
as ``JCS(row) + b"\\n"`` and digests them under the header, the same three
steps :class:`flux_engine.seam.emit.EmitWriter` takes. Equal digests mean
the destination holds the stream the run is answerable for, row for row.

Importing this module imports pyarrow, which is what the ``[arrow]`` extra
installs; without it the import fails with ``FLX-3501`` and the install
line. Nothing on the CLI's cold path imports it.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from flux_engine.api.emit import EmitHeader
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.clock import format_rfc3339_ms
from flux_engine.core.digest import DigestRef
from flux_engine.core.errors import FluxError
from flux_engine.seam.conform import render_cell
from flux_engine.seam.contract import LogicalKind, LogicalType, parse_logical_type
from flux_engine.seam.emit import output_digest, row_line
from flux_engine.sinks._optional import require
from flux_engine.sinks.errors import FLX_3502, FLX_3503

if TYPE_CHECKING:  # pragma: no cover - annotations only
    import pyarrow

pa = require("pyarrow", extra="arrow", feature="the Arrow batching layer")

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "HEADER_KEY",
    "TEXT_RENDERED",
    "TYPE_KEY",
    "BatchBuilder",
    "ColumnLayout",
    "arrow_schema",
    "column_layouts",
    "header_document",
    "output_digest_of",
    "rows_from_table",
]

#: Rows per RecordBatch when the caller does not choose: 2**16, large enough
#: that Arrow's per-batch overhead is noise, small enough that one batch of a
#: wide stream stays a few megabytes.
DEFAULT_BATCH_SIZE = 65_536

#: Schema metadata key holding the §8 header document as JCS bytes.
HEADER_KEY = b"flux.header"

#: Field metadata key holding the column's logical type spelling.
TYPE_KEY = b"flux.type"

_DECIMAL128_MAX_PRECISION = 38
_DECIMAL256_MAX_PRECISION = 76

#: The kinds whose §9 rendering is a string, and so the kinds a destination
#: may keep as text. INT and BOOL render as a JSON integer and a JSON bool:
#: there is no text of theirs to keep, and every destination stores both.
TEXT_RENDERED: FrozenSet[LogicalKind] = frozenset(
    {
        LogicalKind.STRING,
        LogicalKind.DECIMAL,
        LogicalKind.DATE,
        LogicalKind.TIMESTAMP,
        LogicalKind.JSON,
        LogicalKind.BYTES,
    }
)

TableLike = Union["pyarrow.Table", "pyarrow.RecordBatch"]


def header_document(header: EmitHeader) -> Dict[str, Any]:
    """KERNEL-SPEC §8's header document for *header*: the four fields, no others.

    The same document :func:`flux_engine.seam.emit.emit_header_document`
    builds from a compiled expose; a test pins that the two agree.
    """
    return {
        "exposeId": header.expose_id,
        "columns": list(header.columns),
        "types": list(header.types),
        "contractDigest": header.contract_digest.value,
    }


@dataclass(frozen=True)
class ColumnLayout:
    """How one declared column is stored in Arrow."""

    name: str
    logical: LogicalType
    arrow_type: "pyarrow.DataType"
    as_text: bool

    @property
    def spelling(self) -> str:
        """The logical type's canonical spelling, e.g. ``DECIMAL(18,4)``."""
        return str(self.logical)


def _arrow_type(name: str, logical: LogicalType, as_text: bool) -> "pyarrow.DataType":
    kind = logical.kind
    if as_text or kind in (LogicalKind.STRING, LogicalKind.JSON):
        return pa.string()
    if kind is LogicalKind.INT:
        return pa.int64()
    if kind is LogicalKind.BOOL:
        return pa.bool_()
    if kind is LogicalKind.DATE:
        return pa.date32()
    if kind is LogicalKind.TIMESTAMP:
        return pa.timestamp("ms", tz="UTC")
    if kind is LogicalKind.BYTES:
        return pa.binary()
    precision, scale = logical.precision, logical.scale
    assert precision is not None and scale is not None  # LogicalType.__post_init__
    if precision <= _DECIMAL128_MAX_PRECISION:
        return pa.decimal128(precision, scale)
    if precision <= _DECIMAL256_MAX_PRECISION:
        return pa.decimal256(precision, scale)
    raise FluxError(
        FLX_3502,
        f"column {name!r} is {logical}: Arrow's widest decimal holds "
        f"{_DECIMAL256_MAX_PRECISION} digits",
    )


def column_layouts(
    header: EmitHeader,
    *,
    text_kinds: Iterable[LogicalKind] = (),
    decimal_text_above: Optional[int] = None,
) -> Tuple[ColumnLayout, ...]:
    """Map each of *header*'s columns to its Arrow storage, in declared order.

    *decimal_text_above* keeps a DECIMAL whose precision exceeds it as text,
    for a destination whose widest exact decimal is narrower than Arrow's
    (most databases stop at 38 digits). Refuses (``FLX-3502``) a column no
    Arrow type holds exactly, before a single row is converted.
    """
    if not header.columns or len(header.columns) != len(header.types):
        raise ValueError(
            f"expose {header.expose_id!r}: {len(header.columns)} columns and "
            f"{len(header.types)} types; a stream declares at least one column, each typed"
        )
    as_text = frozenset(text_kinds)
    if not as_text <= TEXT_RENDERED:
        refused = ", ".join(sorted(kind.value for kind in as_text - TEXT_RENDERED))
        raise ValueError(f"{refused} do not render as text and cannot be kept as text")
    layouts = []
    for name, spelling in zip(header.columns, header.types, strict=True):
        logical = parse_logical_type(spelling)
        text = logical.kind in as_text or (
            decimal_text_above is not None
            and logical.kind is LogicalKind.DECIMAL
            and (logical.precision or 0) > decimal_text_above
        )
        layouts.append(
            ColumnLayout(
                name=name,
                logical=logical,
                arrow_type=_arrow_type(name, logical, text),
                as_text=text,
            )
        )
    return tuple(layouts)


def _schema_of(
    header: EmitHeader,
    layouts: Sequence[ColumnLayout],
    metadata: Optional[Mapping[str, str]],
) -> "pyarrow.Schema":
    fields = [
        pa.field(layout.name, layout.arrow_type, metadata={TYPE_KEY: layout.spelling.encode()})
        for layout in layouts
    ]
    schema_metadata: Dict[bytes, bytes] = {HEADER_KEY: canonical_bytes(header_document(header))}
    for key, value in (metadata or {}).items():
        encoded = key.encode("utf-8")
        if encoded == HEADER_KEY:
            raise ValueError(f"metadata key {key!r} is reserved for the stream header")
        schema_metadata[encoded] = value.encode("utf-8")
    return pa.schema(fields, metadata=schema_metadata)


def arrow_schema(
    header: EmitHeader,
    *,
    text_kinds: Iterable[LogicalKind] = (),
    metadata: Optional[Mapping[str, str]] = None,
) -> "pyarrow.Schema":
    """The Arrow schema for *header*'s stream, with the stream in its metadata.

    *metadata* adds schema-level entries (a sink's run id, its synthetic
    label); the ``flux.header`` key is reserved.
    """
    return _schema_of(header, column_layouts(header, text_kinds=text_kinds), metadata)


def _value_class(value: Any) -> str:
    return type(value).__name__


def _convert(layout: ColumnLayout, values: Sequence[Any]) -> "pyarrow.Array":
    """One column of rendered values as an Arrow array of the layout's type."""
    kind = layout.logical.kind
    if layout.as_text or kind in (LogicalKind.STRING, LogicalKind.JSON):
        return pa.array(values, type=pa.string())
    if kind is LogicalKind.INT:
        return pa.array(values, type=pa.int64())
    if kind is LogicalKind.BOOL:
        return pa.array(values, type=pa.bool_())
    if kind is LogicalKind.BYTES:
        return pa.array(
            [None if value is None else base64.b64decode(value, validate=True) for value in values],
            type=pa.binary(),
        )
    # DECIMAL, DATE and TIMESTAMP arrive as their §9 text. Arrow parses that
    # text in C++ and refuses, rather than rounds, anything it cannot hold.
    return pa.array(values, type=pa.string()).cast(layout.arrow_type)


_CONVERSION_ERRORS = (OverflowError, TypeError, ValueError, binascii.Error, pa.ArrowException)


def _unrepresentable(layout: ColumnLayout, values: Sequence[Any], offset: int) -> FluxError:
    """Name the first row of *values* that does not convert, by index and class only."""
    for index, value in enumerate(values):
        try:
            _convert(layout, [value])
        except _CONVERSION_ERRORS:
            reason = (
                "outside the signed 64-bit range"
                if layout.logical.kind is LogicalKind.INT and isinstance(value, int)
                else "not convertible"
            )
            return FluxError(
                FLX_3502,
                f"column {layout.name!r} ({layout.spelling}), row {offset + index}: "
                f"a {_value_class(value)} value is {reason}",
            )
    return FluxError(  # pragma: no cover - every value converted alone
        FLX_3502,
        f"column {layout.name!r} ({layout.spelling}): rows {offset}.."
        f"{offset + len(values) - 1} did not convert together",
    )


class BatchBuilder:
    """Collects rendered rows and cuts them into RecordBatches of *batch_size* rows.

    :meth:`append` returns a batch each time one fills, :meth:`flush`
    returns the remainder (or ``None`` when there is none). Row indexes in
    error messages count from the first row this builder saw, so they match
    the stream's own row numbering.
    """

    def __init__(
        self,
        header: EmitHeader,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        text_kinds: Iterable[LogicalKind] = (),
        decimal_text_above: Optional[int] = None,
        metadata: Optional[Mapping[str, str]] = None,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError(f"batch_size must be a positive integer, got {batch_size!r}")
        self.header = header
        self.batch_size = batch_size
        self.layouts = column_layouts(
            header, text_kinds=text_kinds, decimal_text_above=decimal_text_above
        )
        self.schema = _schema_of(header, self.layouts, metadata)
        self._pending: List[Sequence[Any]] = []
        self._rows = 0
        self._flushed = 0

    @property
    def rows(self) -> int:
        """How many rows have been appended so far."""
        return self._rows

    def append(self, row: Sequence[Any]) -> Optional["pyarrow.RecordBatch"]:
        """Add one rendered row; returns a full batch when this row completes one."""
        if len(row) != len(self.layouts):
            raise ValueError(
                f"expose {self.header.expose_id!r}: row {self._rows} has {len(row)} values "
                f"for {len(self.layouts)} columns"
            )
        self._pending.append(row)
        self._rows += 1
        if len(self._pending) >= self.batch_size:
            return self.flush()
        return None

    def extend(self, rows: Iterable[Sequence[Any]]) -> Iterator["pyarrow.RecordBatch"]:
        """Append every row of *rows*, yielding each batch as it fills, then the rest."""
        for row in rows:
            batch = self.append(row)
            if batch is not None:
                yield batch
        tail = self.flush()
        if tail is not None:
            yield tail

    def flush(self) -> Optional["pyarrow.RecordBatch"]:
        """The rows appended since the last batch, as one batch; ``None`` when none."""
        if not self._pending:
            return None
        rows, self._pending = self._pending, []
        offset, self._flushed = self._flushed, self._flushed + len(rows)
        columns = list(zip(*rows, strict=True))
        arrays = []
        for layout, values in zip(self.layouts, columns, strict=True):
            try:
                arrays.append(_convert(layout, values))
            except _CONVERSION_ERRORS as exc:
                raise _unrepresentable(layout, values, offset) from exc
        return pa.RecordBatch.from_arrays(arrays, schema=self.schema)

    def empty_batch(self) -> "pyarrow.RecordBatch":
        """A zero-row batch with this builder's schema, for a stream with no rows."""
        return pa.RecordBatch.from_arrays(
            [pa.array([], type=layout.arrow_type) for layout in self.layouts], schema=self.schema
        )


# ---------------------------------------------------------------------------
# Back to rendered rows
# ---------------------------------------------------------------------------


def _types_of(data: TableLike, types: Optional[Sequence[str]]) -> Tuple[LogicalType, ...]:
    if types is not None:
        if len(types) != data.num_columns:
            raise FluxError(
                FLX_3503,
                f"{len(types)} logical types for a table with {data.num_columns} columns",
            )
        return tuple(parse_logical_type(spelling) for spelling in types)
    parsed = []
    for field in data.schema:
        spelling = (field.metadata or {}).get(TYPE_KEY)
        if spelling is None:
            raise FluxError(
                FLX_3503,
                f"column {field.name!r} carries no {TYPE_KEY.decode()} metadata; "
                "pass the stream's logical types explicitly",
            )
        parsed.append(parse_logical_type(spelling.decode("utf-8")))
    return tuple(parsed)


def _is_text(arrow_type: Any) -> bool:
    types = pa.types
    return bool(
        types.is_string(arrow_type)
        or types.is_large_string(arrow_type)
        or types.is_string_view(arrow_type)
    )


def _is_binary(arrow_type: Any) -> bool:
    types = pa.types
    return bool(
        types.is_binary(arrow_type)
        or types.is_large_binary(arrow_type)
        or types.is_fixed_size_binary(arrow_type)
        or types.is_binary_view(arrow_type)
    )


def _bool_of(name: str, value: int) -> bool:
    """A database's 0/1 integer as a BOOL (SQLite has no boolean storage class)."""
    if value not in (0, 1):
        raise FluxError(FLX_3503, f"BOOL column {name!r} holds an integer other than 0 or 1")
    return value == 1


def _storage(column: Any) -> Any:
    """An extension-typed column as its storage, anything else as it is.

    A driver can hand back a type it has no Arrow equivalent for as an
    extension type over its text: PostgreSQL's NUMERIC arrives as
    ``arrow.opaque`` over ``string``, holding the number at the column's
    scale. The text is then read like any other text-stored value, and a
    value that is not the stream's rendering still fails the digest.
    """
    if not isinstance(column.type, pa.BaseExtensionType):
        return column
    if isinstance(column, pa.ChunkedArray):
        return pa.chunked_array(
            [chunk.storage for chunk in column.chunks], type=column.type.storage_type
        )
    return column.storage


def _exact_decimal(name: str, logical: LogicalType, value: Any) -> str:
    """A stored decimal (a ``Decimal`` or its text) as the §9 rendering, or refused.

    Databases keep the number and not always its spelling: PostgreSQL's
    driver creates an unconstrained NUMERIC that drops trailing zeros, and a
    wider decimal column pads them. The value is accepted when it is exactly
    representable at the declared scale and rendered again by the seam's own
    renderer; a value that would need rounding is not the stream's. (Not
    Arrow's decimal-to-string cast: it follows Java's BigDecimal.toString and
    writes a zero at scale 10 as ``0E-10``.)
    """
    assert logical.scale is not None and logical.precision is not None
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FluxError(
            FLX_3503, f"DECIMAL column {name!r} holds text that is not a number"
        ) from exc
    if not number.is_finite():
        raise FluxError(FLX_3503, f"DECIMAL column {name!r} holds a value that is not finite")
    with localcontext() as context:
        context.prec = logical.precision + logical.scale + 80
        exact = number.quantize(Decimal(1).scaleb(-logical.scale)) == number
    if not exact:
        raise FluxError(
            FLX_3503,
            f"DECIMAL column {name!r} ({logical}) holds a value with more digits than its scale",
        )
    try:
        return str(render_cell(number, logical))
    except FluxError as exc:
        raise FluxError(FLX_3503, f"DECIMAL column {name!r}: {exc.detail}") from exc


def _rendered(name: str, logical: LogicalType, column: Any) -> List[Any]:
    """One Arrow column back as §9 rendered values."""
    column = _storage(column)
    if column.null_count == len(column):
        # Nothing to read but nulls, which render as null in every type. A
        # database that saw no value may report the column as any type it
        # likes: SQLite's driver calls an all-null or empty column int64.
        return [None] * len(column)
    arrow_type = column.type
    kind = logical.kind
    types = pa.types
    if kind is LogicalKind.DECIMAL and (_is_text(arrow_type) or types.is_decimal(arrow_type)):
        return [
            None if value is None else _exact_decimal(name, logical, value)
            for value in column.to_pylist()
        ]
    if _is_text(arrow_type):
        # STRING and JSON, and any kind a text-kinds destination kept as text.
        if kind is LogicalKind.BOOL:
            raise FluxError(FLX_3503, f"column {name!r} is BOOL but stored as text")
        return list(column.to_pylist())
    if kind is LogicalKind.INT and types.is_integer(arrow_type):
        return list(column.to_pylist())
    if kind is LogicalKind.BOOL and types.is_boolean(arrow_type):
        return list(column.to_pylist())
    if kind is LogicalKind.BOOL and types.is_integer(arrow_type):
        return [None if value is None else _bool_of(name, value) for value in column.to_pylist()]
    if kind is LogicalKind.DATE and (types.is_date32(arrow_type) or types.is_date64(arrow_type)):
        return [None if day is None else day.isoformat() for day in column.to_pylist()]
    if kind is LogicalKind.TIMESTAMP and types.is_timestamp(arrow_type):
        # A safe cast: a value with sub-millisecond digits fails instead of
        # being truncated into a different instant.
        millis = column.cast(pa.timestamp("ms", tz="UTC")).cast(pa.int64()).to_pylist()
        return [None if value is None else format_rfc3339_ms(value) for value in millis]
    if kind is LogicalKind.BYTES and _is_binary(arrow_type):
        return [
            None if value is None else base64.b64encode(value).decode("ascii")
            for value in column.to_pylist()
        ]
    raise FluxError(FLX_3503, f"column {name!r} is {logical} but stored as Arrow {arrow_type}")


def rows_from_table(
    data: TableLike, *, types: Optional[Sequence[str]] = None
) -> Iterator[List[Any]]:
    """Yield *data*'s rows as §9 rendered values, in table order.

    The inverse of :class:`BatchBuilder`, for a table or batch written by
    it, read back from a file, or selected from a database in stream order.
    *types* gives the logical type spellings; without it they come from each
    field's ``flux.type`` metadata.
    """
    logical_types = _types_of(data, types)
    columns = [
        _rendered(field.name, logical, data.column(index))
        for index, (field, logical) in enumerate(zip(data.schema, logical_types, strict=True))
    ]
    for row in zip(*columns, strict=True):
        yield list(row)


def _header_of(data: TableLike) -> Dict[str, Any]:
    raw = (data.schema.metadata or {}).get(HEADER_KEY)
    if raw is None:
        raise FluxError(
            FLX_3503,
            f"the table carries no {HEADER_KEY.decode()} metadata; pass the stream's header",
        )
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or set(document) != {
        "exposeId",
        "columns",
        "types",
        "contractDigest",
    }:
        raise FluxError(FLX_3503, f"{HEADER_KEY.decode()} is not a KERNEL-SPEC §8 header")
    return document


def output_digest_of(data: TableLike, *, header: Optional[EmitHeader] = None) -> DigestRef:
    """Recompute the ``outputDigest`` of the stream *data* holds (KERNEL-SPEC §8).

    The header comes from *header* or, without it, from the schema's
    ``flux.header`` metadata. The table's column names must be the header's
    columns, in order, so a re-ordered or renamed copy cannot pass.
    """
    document = header_document(header) if header is not None else _header_of(data)
    names = list(data.schema.names)
    if names != list(document["columns"]):
        raise FluxError(
            FLX_3503,
            f"the table's columns {names} are not the stream's declared columns "
            f"{list(document['columns'])}",
        )
    lines = (row_line(row) for row in rows_from_table(data, types=document["types"]))
    return output_digest(document, lines, subject=f"outputDigest:{document['exposeId']}")
