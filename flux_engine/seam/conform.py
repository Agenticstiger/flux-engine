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
    string; BYTES -> base64 string. ``float`` -> ``FLX-3002``.

    INT renders from an ``int`` only: any other value class, ``bool`` and
    ``Decimal`` included, -> ``FLX-3003``. A scenario-driven INT column
    (``seam/mapping.py``) is decided at plan time, never per value: its
    mapped source must be **integral**, every value it can hold a whole
    number. The integral sources are the integer scalars
    (``subject.ordinal``, ``event.time``, ``moment.firstTime``,
    ``moment.lastTime``, ``moment.eventCount``) and each derived attribute,
    trait or payload field whose every declared family draws only whole
    numbers (``RateCount``; ``BoundedNormal`` at scale 0; ``Constant``
    holding a whole number), across every lifecycle override, Persona and
    Signal that declares it. Any other source -> ``FLX-3303`` before a row
    exists. A ``Decimal`` from an integral source is converted exactly
    (``int(v)``, after checking ``v`` is finite and whole, else
    ``FLX-3003``) before it is rendered, so the class rule holds for every
    caller.

    A row missing a ``required`` column -> ``FLX-3202``; an undeclared
    column -> ``FLX-3201``; errors name column, row index and value
    **class**, never the value.

The type half of §9 is :mod:`flux_engine.seam.contract`; this module is
the value half, and the plan-time half of the INT rule is
:mod:`flux_engine.seam.mapping`'s: :func:`render_cell` never converts a
``Decimal`` into an ``INT``, whoever calls it. Every diagnostic here obeys
the last clause literally -- ``_class_of`` is the only description of a
value this module will ever produce, and it is a type name, never a repr.
A conformance error is routinely written into a log a data owner reads; a
value that failed conformance is exactly the kind of value nobody promised
was safe to print.

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
from json.encoder import encode_basestring
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.canonical import JCS_MAX_SAFE_INTEGER, canonical_bytes, nfc
from flux_engine.core.clock import format_rfc3339_ms, format_seconds, parse_rfc3339_ms
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
    "render_columns",
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

#: Value classes that hold no float at any depth, so :func:`_reject_floats`
#: has nothing to look inside. Matched on the exact class: a subclass takes
#: the general walk.
_FLOAT_FREE = frozenset({str, int, bool, Decimal, type(None), date, datetime, bytes, bytearray})

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
    if type(value) in _FLOAT_FREE:
        return
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
    return format_seconds(moment) + f".{moment.microsecond // 1000:03d}Z"


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
    values: Tuple[Any, ...]
    if type(row) is tuple:
        # The generator's own rows, so the common case: a tuple is neither a
        # mapping nor a string, which the branches below would ask first.
        values = row
    elif isinstance(row, Mapping):
        return _mapping_values(row, expose, row_index)
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        values = tuple(row)
    else:
        raise FluxError(
            FLX_3003,
            f"row {row_index}: a row is a mapping or a sequence, got {_class_of(row)}",
        )
    if len(values) != len(expose.columns):
        raise FluxError(
            FLX_3203,
            f"{_at('<positional>', row_index)}: expose {expose.expose_id!r} declares "
            f"{len(expose.columns)} columns; the row carries {len(values)} values",
        )
    return values


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


# ---------------------------------------------------------------------------
# A slice a column at a time
# ---------------------------------------------------------------------------

#: Rows a pass renders before it frames them. A lookup table is built per pass.
_COLUMN_CHUNK = 1 << 14

_NULL = "null"
_NONE = type(None)

#: The value classes a column of each kind may hold for the column renderer to take it. Any
#: other class (a ``bool`` in an INT column, a string spelling a DATE, a subclass of ``str``)
#: is the row renderer's, which decides what it renders and what it refuses.
_TABLE_CLASSES: Mapping[LogicalKind, frozenset] = {
    LogicalKind.STRING: frozenset({str, _NONE}),
    LogicalKind.BOOL: frozenset({bool, _NONE}),
    LogicalKind.DATE: frozenset({date, _NONE}),
    LogicalKind.TIMESTAMP: frozenset({int, str, datetime, _NONE}),
}


def render_columns(rows: Sequence[Row], expose: CompiledExpose) -> List[str]:
    """The framed line of each of *rows*, ``JCS(render_row(row))`` and a newline, a column at a time.

    A line holds only JSON integers, booleans, nulls and strings: a decimal is a string at a
    fixed scale, a date is ``YYYY-MM-DD``. So each column renders to its text tokens once,
    through a lookup table where the column draws from few values (levels, dates, booleans)
    and by integer or decimal formatting where it does not, and a row's line is its tokens
    joined. The bytes are the bytes ``row_line(render_row(row))`` makes.

    Raises for anything it does not take: a row that is not a tuple, a value of a class the
    row renderer treats on its own terms, a refusal the row renderer would make, an integer
    outside JCS's range. The caller then renders the rows one at a time, so what is refused
    is refused by the row renderer, at its row and column, with its message.
    """
    if not rows:
        return []
    if not expose.columns:
        raise ValueError("an expose with no column is rendered row by row")
    lines: List[str] = []
    for first in range(0, len(rows), _COLUMN_CHUNK):
        chunk = rows[first : first + _COLUMN_CHUNK]
        if set(map(type, chunk)) != {tuple}:
            raise ValueError("a row that is not a tuple is rendered row by row")
        columns = list(zip(*chunk, strict=True))
        if len(columns) != len(expose.columns):
            raise ValueError("a row of the wrong length is rendered row by row")
        tokens = [
            _column_tokens(values, column)
            for values, column in zip(columns, expose.columns, strict=True)
        ]
        lines.extend(["[" + ",".join(cells) + "]\n" for cells in zip(*tokens, strict=True)])
    return lines


def _table_tokens(
    values: Sequence[Any],
    classes: set,
    allowed: frozenset,
    has_null: bool,
    logical_type: LogicalType,
) -> List[str]:
    """The columns whose value classes are a small closed set: a fast path or one token per value."""
    kind = logical_type.kind
    if not classes <= allowed:
        raise ValueError("a value class the row renderer decides")
    if kind is LogicalKind.BOOL:
        return ["null" if v is None else "true" if v else "false" for v in values]
    if kind is LogicalKind.TIMESTAMP and classes <= {int, _NONE}:
        if _instants_in_range(values, has_null):
            return _instant_tokens(values)
    elif kind is LogicalKind.STRING:
        return _string_tokens(values)
    table: Dict[Any, str] = {}
    for value in set(values):
        table[value] = _NULL if value is None else _scalar_token(render_cell(value, logical_type))
    return [table[value] for value in values]


def _column_tokens(values: Sequence[Any], column: CompiledColumn) -> List[str]:
    """Each value of one column as its JSON text, as :func:`render_cell` then JCS writes it."""
    classes = set(map(type, values))
    has_null = _NONE in classes
    if has_null and column.required:
        raise ValueError("a required column with a null is refused by the row renderer")
    logical_type = column.logical_type
    kind = logical_type.kind
    allowed = _TABLE_CLASSES.get(kind)
    if allowed is not None:
        return _table_tokens(values, classes, allowed, has_null, logical_type)
    if kind is LogicalKind.INT:
        if not classes <= {int, _NONE}:
            raise ValueError("a value class the row renderer decides")
        present = [v for v in values if v is not None] if has_null else values
        if present and (
            min(present) < -JCS_MAX_SAFE_INTEGER or max(present) > JCS_MAX_SAFE_INTEGER
        ):
            raise ValueError("an integer outside JCS's range")
        return (
            [_NULL if v is None else str(v) for v in values] if has_null else list(map(str, values))
        )
    if kind is LogicalKind.DECIMAL:
        if not classes <= {Decimal, _NONE}:
            raise ValueError("a value class the row renderer decides")
        return _decimal_tokens(values, logical_type, has_null)
    # JSON and BYTES: one value at a time, through the cell renderer.
    return [_NULL if v is None else _scalar_token(render_cell(v, logical_type)) for v in values]


_DAY_MS = 86_400_000

#: The instants ``format_rfc3339_ms`` can spell: 0001-01-01T00:00:00.000Z to
#: 9999-12-31T23:59:59.999Z, as milliseconds from the epoch. Outside it the clock's own
#: arithmetic overflows, and the row renderer decides what is raised.
_INSTANT_MIN_MS = -62_135_596_800_000
_INSTANT_MAX_MS = 253_402_300_799_999

#: ``.mmm`` and the closing ``Z`` and quote of a timestamp token, by millisecond.
_MS_TAIL = tuple(f'.{ms:03d}Z"' for ms in range(1000))


def _instants_in_range(values: Sequence[Any], has_null: bool) -> bool:
    present = [v for v in values if v is not None] if has_null else values
    if not present:
        return True
    return _INSTANT_MIN_MS <= min(present) and max(present) <= _INSTANT_MAX_MS


def _instant_tokens(values: Sequence[Any]) -> List[str]:
    """TIMESTAMP cells held as integer milliseconds: :func:`format_rfc3339_ms` for a column.

    An instant is a day and a time of day. The text of a day (``"YYYY-MM-DDT``) is asked of
    the clock once per distinct day, by formatting that day's first instant, so the clock
    stays the one source of the date's spelling on every platform; the time of day is
    ``HH:MM:SS`` from integer arithmetic (pinned against the clock for every second of a
    day) and ``.mmm`` from a table. The day count floors, as ``timedelta`` does, so an
    instant before 1970 gets the same day and the same time the clock gives it.
    """
    days = {}
    for day in {v // _DAY_MS for v in values if v is not None}:
        # The clock's text ends in "HH:MM:SS.mmmZ", thirteen characters, at midnight.
        days[day] = '"' + format_rfc3339_ms(day * _DAY_MS)[:-13]
    seconds: Dict[int, str] = {}
    tails = _MS_TAIL
    out: List[str] = []
    append = out.append
    for value in values:
        if value is None:
            append(_NULL)
            continue
        day, rest = divmod(value, _DAY_MS)
        second, ms = divmod(rest, 1000)
        clock = seconds.get(second)
        if clock is None:
            clock = seconds[second] = (
                f"{second // 3600:02d}:{second // 60 % 60:02d}:{second % 60:02d}"
            )
        append(days[day] + clock + tails[ms])
    return out


def _string_tokens(values: Sequence[Any]) -> List[str]:
    """STRING cells as JSON strings: NFC and escape each distinct value once.

    An ASCII string is its own NFC form, so it skips the normaliser. The text is what
    ``_scalar_token(render_cell(value))`` makes of it.
    """
    table: Dict[Any, str] = {
        value: (
            _NULL if value is None else encode_basestring(value if value.isascii() else nfc(value))
        )
        for value in set(values)
    }
    return [table[value] for value in values]


def _scalar_token(rendered: Any) -> str:
    """The JSON text JCS writes for one rendered cell: a string, an integer, a boolean or null."""
    if rendered is None:
        return _NULL
    kind = type(rendered)
    if kind is str:
        return encode_basestring(nfc(rendered))
    if kind is bool:
        return "true" if rendered else "false"
    if kind is int and abs(rendered) <= JCS_MAX_SAFE_INTEGER:
        return str(rendered)
    raise ValueError("a rendered cell the column renderer does not spell")


def _decimal_tokens(values: Sequence[Any], logical_type: LogicalType, has_null: bool) -> List[str]:
    """DECIMAL(p,s) cells as strings at scale *s*: :func:`_render_decimal` for a whole column.

    The extremes of the column decide the precision check for every value (quantising is
    monotonic), and ``format(value, '.<s>f')`` rounds as ``quantize`` does, under the same
    ambient context.
    """
    scale, precision = logical_type.scale, logical_type.precision
    assert scale is not None and precision is not None  # LogicalType.__post_init__
    present = [v for v in values if v is not None] if has_null else list(values)
    spelled: List[str] = []
    with localcontext() as context:
        context.prec = max(precision, 1) + scale + 40
        if present:
            exponent = Decimal(1).scaleb(-scale)
            bound = Decimal(10) ** (precision - scale)
            for extreme in (min(present), max(present)):
                quantised = extreme.quantize(exponent)
                if not quantised.is_finite() or abs(quantised) >= bound:
                    raise ValueError("a decimal the row renderer refuses")
            fmt = f".{scale}f"
            spelled = [format(v, fmt) for v in present]
    # -0.00 and 0.00 are one number: _render_decimal spells it without the sign.
    for position, text in enumerate(spelled):
        if text[:2] == "-0" and not text.strip("-0."):
            spelled[position] = text[1:]
    tokens = ['"' + text + '"' for text in spelled]
    if not has_null:
        return tokens
    ahead = iter(tokens)
    return [_NULL if v is None else next(ahead) for v in values]
