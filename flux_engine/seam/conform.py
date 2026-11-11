# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Value rendering and the closed-world row check -- KERNEL-SPEC §9.

§9 in full:

    Closed ``LogicalType`` set: ``STRING, INT, DECIMAL(p,s), BOOL, DATE,
    TIMESTAMP, JSON, BYTES``. FLUID types are normalised case-insensitively;
    anything outside the set fails by name (``FLX-3001``). Rendering:
    STRING -> NFC string; INT -> JSON integer; DECIMAL -> string at scale
    ``s``; BOOL -> JSON bool; DATE -> ``YYYY-MM-DD``; TIMESTAMP -> RFC 3339
    UTC with millisecond precision and ``Z``; JSON -> canonical JSON
    string; BYTES -> base64 string. ``float`` -> ``FLX-3002``. A row
    missing a ``required`` column -> ``FLX-3202``; an undeclared column ->
    ``FLX-3201``; errors name column, row index and value **class**, never
    the value.

The type half of §9 is :mod:`flux_engine.seam.contract`; this module is
the value half. Every diagnostic here obeys the last clause literally --
``_class_of`` is the only description of a value this module will ever
produce, and it is a type name, never a repr. A conformance error is
routinely written into a log a data owner reads; a value that failed
conformance is exactly the kind of value nobody promised was safe to
print.

**The closed world.** A row is checked against the declared column set in
both directions before any value is rendered: an undeclared column is
``FLX-3201`` even when its value would have rendered, and a required
column with no value -- absent, or present as ``None`` -- is ``FLX-3202``.
"Present as ``None``" counts as missing on purpose: a required column
whose value is null carries no value, and admitting it would let a stream
satisfy a required-column contract with a column of nulls.

**Floats are checked before types are dispatched.** A float in a ``BOOL``
column is ``FLX-3002``, not a "wrong value class" report, and a float
nested three levels inside a ``JSON`` column is ``FLX-3002`` too. §9 gives
floats their own code because they are the one value class that is
reproducibility-fatal rather than merely wrong.
"""

from __future__ import annotations

import base64
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.canonical import canonical_bytes, nfc
from flux_engine.core.clock import format_rfc3339_ms, parse_rfc3339_ms
from flux_engine.core.errors import FluxError, register
from flux_engine.seam.contract import CompiledColumn, CompiledExpose, LogicalKind, LogicalType

__all__ = [
    "FLX_3002",
    "FLX_3003",
    "FLX_3201",
    "FLX_3202",
    "FLX_3203",
    "DATE_PATTERN",
    "RenderedRow",
    "Row",
    "render_cell",
    "render_row",
    "render_rows",
]

FLX_3002 = register(
    "FLX-3002",
    "binary floating-point value at the seam",
    "KERNEL-SPEC §9 ends with 'float -> FLX-3002' and §2.1 forbids a float "
    "anywhere in a hashed value: a binary float has no reproducible "
    "spelling across platforms and libm versions, so a stream containing "
    "one cannot be replayed. Carry the number as an int or a "
    "decimal.Decimal, which the seam renders exactly.",
)

FLX_3003 = register(
    "FLX-3003",
    "value cannot be rendered as its declared logical type",
    "The seam renders each of KERNEL-SPEC §9's eight logical types from a "
    "closed set of Python value classes, so a stream's spelling is a "
    "function of the declaration alone. A value of another class -- or one "
    "outside the declared type's domain, such as a decimal too large for "
    "its declared precision -- is refused. The diagnostic names the column, "
    "the row index and the value's class, never the value.",
)

FLX_3201 = register(
    "FLX-3201",
    "row carries a column the expose does not declare",
    "The seam is closed-world (KERNEL-SPEC §9): a row may carry only the "
    "columns the referenced FLUID contract declares, because the emitted "
    "array is positional and an undeclared value has no position. Add the "
    "column to the contract, or stop producing it.",
)

FLX_3202 = register(
    "FLX-3202",
    "row is missing a required column",
    "The referenced FLUID contract marks this column required, and the row "
    "carries no value for it -- either absent, or present as null. A "
    "required column whose value is null carries no value, so admitting it "
    "would let a stream satisfy the contract with a column of nulls.",
)

FLX_3203 = register(
    "FLX-3203",
    "positional row does not match the declared column count",
    "Rows travel as arrays aligned to the ordered header (KERNEL-SPEC §8), "
    "so a row with a different length has no alignment: value k is not "
    "column k. Pass the row as a mapping if the producer cannot guarantee "
    "the declared order.",
)

#: ``DATE`` renders as ``YYYY-MM-DD`` (§9), so a string input must already
#: be spelled that way. ``date.fromisoformat`` alone would accept ISO 8601
#: forms §9 does not name (``20260701``, and week and ordinal dates), and
#: silently re-spelling them would mean the declaration no longer decides
#: what the column looks like.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: What a caller may hand the renderer as one row: either a mapping from
#: column name to value, or a sequence already aligned to the declared
#: column order.
Row = Union[Mapping[str, Any], Sequence[Any]]

#: One rendered row: JSON-ready values, positionally aligned to the
#: expose's declared columns.
RenderedRow = Tuple[Any, ...]


def _class_of(value: Any) -> str:
    """The only description of a value this module produces (§9's last clause)."""
    return type(value).__name__


def _at(column: str, row_index: Optional[int]) -> str:
    where = f"column {column!r}"
    if row_index is not None:
        where = f"row {row_index}, {where}"
    return where


def _reject_floats(value: Any) -> None:
    """Raise ``FLX-3002`` if *value* contains a float at any depth.

    Run before type dispatch, so a float is reported as a float wherever
    it sits -- including inside a ``JSON`` column's nested structure,
    where the canonicaliser would otherwise be the one to notice.
    """
    if isinstance(value, float):
        raise FluxError(FLX_3002, "value class 'float'")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_floats(key)
            _reject_floats(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_floats(item)


def _render_string(value: Any) -> str:
    if not isinstance(value, str):
        raise FluxError(FLX_3003, f"STRING requires a str, got {_class_of(value)}")
    return nfc(value)


def _render_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FluxError(FLX_3003, f"INT requires an int, got {_class_of(value)}")
    return value


def _render_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise FluxError(FLX_3003, f"BOOL requires a bool, got {_class_of(value)}")
    return value


def _render_decimal(value: Any, logical_type: LogicalType) -> str:
    """Render a number as a decimal string at the declared scale (§9).

    ``int`` and ``Decimal`` are the accepted classes: both carry an exact
    value. A value with more decimal places than the declared scale is
    **quantised**, half-to-even, which is the rounding a SQL engine
    applies on the same cast and is exact in the ``decimal`` module on
    every platform. A value whose integer part does not fit in
    ``precision - scale`` digits is refused (``FLX-3003``) rather than
    rounded: rounding cannot make it fit, and truncating it would put a
    different number into the stream than the one the producer computed.
    """
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise FluxError(
            FLX_3003,
            f"DECIMAL requires an int or a decimal.Decimal, got {_class_of(value)}",
        )
    number = Decimal(value)
    if not number.is_finite():
        raise FluxError(FLX_3003, "DECIMAL requires a finite value")
    scale = logical_type.scale
    precision = logical_type.precision
    assert scale is not None and precision is not None  # LogicalType.__post_init__
    exponent = Decimal(1).scaleb(-scale)
    # A context wide enough that quantise() never raises on the integer
    # part; the integer-digit check below is what decides whether the
    # value fits the declared precision.
    with localcontext() as context:
        context.prec = max(precision, 1) + scale + 40
        try:
            quantised = number.quantize(exponent)
        except InvalidOperation as exc:  # pragma: no cover - context is wide enough
            raise FluxError(FLX_3003, "DECIMAL value is out of the declared domain") from exc
    digits = len(quantised.as_tuple().digits)
    integer_digits = max(digits + int(quantised.as_tuple().exponent), 0)
    if integer_digits > precision - scale:
        raise FluxError(
            FLX_3003,
            f"DECIMAL({precision},{scale}) admits {precision - scale} integer digits; "
            f"the value carries {integer_digits}",
        )
    rendered = format(quantised, "f")
    if rendered.startswith("-") and quantised == 0:
        # -0.0000 and 0.0000 are one number; two spellings would be two digests.
        rendered = rendered[1:]
    return rendered


def _render_date(value: Any) -> str:
    """Render ``YYYY-MM-DD`` (§9) from a ``datetime.date`` or an exact string."""
    if isinstance(value, datetime):
        raise FluxError(
            FLX_3003,
            "DATE requires a datetime.date; a datetime carries a time of day the "
            "declared type cannot render",
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        if DATE_PATTERN.match(value) is None:
            raise FluxError(FLX_3003, "DATE requires a string spelled YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise FluxError(FLX_3003, "DATE string is not a calendar date") from exc
        return parsed.isoformat()
    raise FluxError(FLX_3003, f"DATE requires a datetime.date or a str, got {_class_of(value)}")


def _render_timestamp(value: Any) -> str:
    """Render RFC 3339 UTC at millisecond precision with ``Z`` (§9).

    Delegates to :mod:`flux_engine.core.clock`, so the seam and the
    virtual clock agree on one grammar and one rendering: a timestamp
    that the scheduler can hold is exactly a timestamp the seam can emit.
    An integer is read as milliseconds since the Unix epoch, which is the
    clock's own carrier type.

    The clock refuses a malformed timestamp with its own numerics-band
    code (``FLX-5022``). At the seam that is the wrong band: a bad value
    in a row is a conformance failure, and a caller catching the seam's
    codes should not have to know which kernel module the renderer happens
    to delegate to. :func:`_from_the_clock` re-raises it as ``FLX-3003``,
    keeping the clock's own explanation of what was wrong with it.
    """
    if isinstance(value, bool):
        raise FluxError(
            FLX_3003, f"TIMESTAMP requires a datetime, str or int, got {_class_of(value)}"
        )
    if isinstance(value, int):
        return _from_the_clock(lambda: format_rfc3339_ms(value))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise FluxError(
                FLX_3003,
                "TIMESTAMP requires an aware datetime; a naive one names no instant",
            )
        moment = value.astimezone(timezone.utc)
        if moment.microsecond % 1000:
            raise FluxError(
                FLX_3003,
                "TIMESTAMP renders at millisecond precision; the value carries microseconds",
            )
        return _from_the_clock(lambda: format_rfc3339_ms(parse_rfc3339_ms(_isoformat_z(moment))))
    if isinstance(value, str):
        return _from_the_clock(lambda: format_rfc3339_ms(parse_rfc3339_ms(value)))
    raise FluxError(FLX_3003, f"TIMESTAMP requires a datetime, str or int, got {_class_of(value)}")


def _from_the_clock(render: Callable[[], str]) -> str:
    """Run a clock conversion, re-banding its refusal as a seam refusal.

    The clock's diagnostic already obeys §9's rule about never naming a
    value, so it is carried through verbatim; only the code changes.
    """
    try:
        return render()
    except FluxError as exc:
        if exc.code is FLX_3003:  # pragma: no cover - the clock raises its own code
            raise
        raise FluxError(FLX_3003, f"TIMESTAMP: {exc.detail or exc.code.title}") from exc


def _isoformat_z(moment: datetime) -> str:
    """Spell an aware UTC datetime in the grammar ``parse_rfc3339_ms`` accepts."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{moment.microsecond // 1000:03d}Z"


def _render_json(value: Any) -> str:
    """Render a nested value as its canonical JSON string (§9).

    The string that lands in the row is the RFC 8785 canonicalisation from
    :mod:`flux_engine.core.canonical` -- the same bytes the rest of the
    engine digests -- so a ``JSON`` column cannot become a place where two
    spellings of one document survive into the stream.
    """
    try:
        return canonical_bytes(value).decode("utf-8")
    except FluxError as exc:
        raise FluxError(FLX_3003, f"JSON column: {exc.detail or exc.code.title}") from exc


def _render_bytes(value: Any) -> str:
    if not isinstance(value, (bytes, bytearray)):
        raise FluxError(FLX_3003, f"BYTES requires bytes, got {_class_of(value)}")
    return base64.b64encode(bytes(value)).decode("ascii")


def render_cell(value: Any, logical_type: LogicalType) -> Any:
    """Render one value as its declared logical type (KERNEL-SPEC §9).

    The result is JSON-ready: ``str``, ``int``, ``bool`` or ``None``. It is
    never a ``float`` -- §9's rendering table has no float in it, which is
    what makes an emitted row canonicalisable.

    ``None`` renders as JSON ``null``. Whether ``None`` is *allowed* is a
    row-level question (a required column may not be null), so it is
    :func:`render_row` that raises ``FLX-3202``, not this function.
    """
    _reject_floats(value)
    if value is None:
        return None
    kind = logical_type.kind
    if kind is LogicalKind.STRING:
        return _render_string(value)
    if kind is LogicalKind.INT:
        return _render_int(value)
    if kind is LogicalKind.DECIMAL:
        return _render_decimal(value, logical_type)
    if kind is LogicalKind.BOOL:
        return _render_bool(value)
    if kind is LogicalKind.DATE:
        return _render_date(value)
    if kind is LogicalKind.TIMESTAMP:
        return _render_timestamp(value)
    if kind is LogicalKind.JSON:
        return _render_json(value)
    return _render_bytes(value)


def _values_in_declared_order(
    row: Row, expose: CompiledExpose, row_index: Optional[int]
) -> Tuple[Any, ...]:
    """Align *row* to the declared column order, closing the world both ways."""
    if isinstance(row, Mapping):
        return _mapping_values(row, expose, row_index)
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        values = tuple(row)
        if len(values) != len(expose.columns):
            raise FluxError(
                FLX_3203,
                f"{_at('<positional>', row_index)}: expose {expose.expose_id!r} declares "
                f"{len(expose.columns)} columns; the row carries {len(values)} values",
            )
        return values
    raise FluxError(
        FLX_3003,
        f"row {row_index}: a row is a mapping or a sequence, got {_class_of(row)}",
    )


def _mapping_values(
    row: Mapping[str, Any], expose: CompiledExpose, row_index: Optional[int]
) -> Tuple[Any, ...]:
    normalised: Dict[str, Any] = {}
    for raw_key, value in row.items():
        if not isinstance(raw_key, str):
            raise FluxError(
                FLX_3201,
                f"row {row_index}: column name is {_class_of(raw_key)}, not a str",
            )
        key = nfc(raw_key)
        if key in normalised:
            raise FluxError(
                FLX_3201,
                f"{_at(key, row_index)}: two keys of the row normalise onto this name",
            )
        normalised[key] = value

    declared = set(expose.column_names)
    for key in sorted(normalised):
        if key not in declared:
            raise FluxError(
                FLX_3201,
                f"{_at(key, row_index)}: expose {expose.expose_id!r} declares "
                f"{sorted(declared)}",
            )
    return tuple(normalised.get(column.name) for column in expose.columns)


def _require_present(column: CompiledColumn, value: Any, row_index: Optional[int]) -> None:
    if column.required and value is None:
        raise FluxError(
            FLX_3202,
            f"{_at(column.name, row_index)}: the contract marks it required",
        )


def render_row(row: Row, expose: CompiledExpose, *, row_index: Optional[int] = None) -> RenderedRow:
    """Render one row into the positional array KERNEL-SPEC §8 emits.

    *row* is a mapping from declared column name to value, or a sequence
    already in the declared column order. The mapping form is closed-world
    in both directions (``FLX-3201`` / ``FLX-3202``); the sequence form is
    length-checked (``FLX-3203``) and then required-checked the same way.

    Every diagnostic carries the column, the row index and the value's
    class, and no diagnostic carries the value (§9).
    """
    values = _values_in_declared_order(row, expose, row_index)
    rendered: List[Any] = []
    for column, value in zip(expose.columns, values, strict=True):
        _require_present(column, value, row_index)
        try:
            rendered.append(render_cell(value, column.logical_type))
        except FluxError as exc:
            raise FluxError(
                exc.code,
                f"{_at(column.name, row_index)}: {exc.detail or exc.code.title}",
            ) from exc
    return tuple(rendered)


def render_rows(rows: Iterable[Row], expose: CompiledExpose) -> Tuple[RenderedRow, ...]:
    """Render *rows* in order, numbering them from zero for diagnostics."""
    return tuple(render_row(row, expose, row_index=index) for index, row in enumerate(rows))
