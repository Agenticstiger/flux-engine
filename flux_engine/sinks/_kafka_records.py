# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A stream's rows as Kafka records (ADR 0038 sections 5 to 7 and 11).

:class:`RecordBuilder` turns each rendered row into the record the sink sends: its key
(:func:`encode_key`), its event time in milliseconds (:func:`epoch_ms`), its value (the row, or
Debezium's envelope for a history's events and a snapshot, with a tombstone after each delete)
and the headers that label it synthetic. Everything is a function of the row and the stream's
:class:`~flux_engine.sinks._kafka_shape.StreamShape`, so the same input gives the same bytes at
every pace and every ``--jobs``.

Values stay rendered (text, integers, booleans, null), never converted: the registry
serializers and the JSON writer decide what a value looks like on the wire. A refusal
names a column and a row's position, never a value.

Standard library and :mod:`flux_engine` only.
"""

from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.sinks._kafka_shape import Envelope, StreamShape
from flux_engine.sinks.errors import FLX_3513

__all__ = [
    "ENVELOPE_SOURCE_FIELDS",
    "KafkaRecord",
    "RecordBuilder",
    "encode_key",
    "epoch_ms",
    "headers_for",
]

#: The fields of the envelope's ``source`` block that Debezium's connectors share.
ENVELOPE_SOURCE_FIELDS: Tuple[str, ...] = (
    "version",
    "connector",
    "name",
    "ts_ms",
    "snapshot",
    "db",
    "table",
)

_COMPACT = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_DAY_MS = 86_400_000
_OPS = ("c", "u", "d")
_INSTANT_KINDS = ("DATE", "TIMESTAMP")


class KafkaRecord(NamedTuple):
    """One record to send: ``value`` is ``None`` for a tombstone.

    A named tuple, not a dataclass: a stream builds one for every record, and a tuple is built
    several times faster. It is as immutable, and equal to another with the same fields.
    """

    key: bytes
    value: Optional[Dict[str, Any]]
    timestamp_ms: int
    headers: Tuple[Tuple[str, bytes], ...]
    row: int
    entity: bytes


@lru_cache(maxsize=65536)
def _instant_ms(kind: str, text: str) -> int:
    """Milliseconds of a rendered DATE or TIMESTAMP, cached by text; ``ValueError`` when invalid."""
    if kind == "DATE":
        if _DATE.fullmatch(text) is None:
            raise ValueError(kind)
        return (date.fromisoformat(text).toordinal() - _EPOCH_ORDINAL) * _DAY_MS
    try:
        return parse_rfc3339_ms(text)
    except FluxError:
        raise ValueError(kind) from None


def epoch_ms(rendered: object, logical_type: str, *, column: str, row: int) -> int:
    """Milliseconds since 1970 (UTC) of a rendered DATE or TIMESTAMP, exactly.

    A DATE is its midnight. ``FLX-3513`` for a null, a value that is not a date or an RFC 3339
    instant, and a column of any other type; a time before 1970 is returned negative. A text's
    milliseconds are remembered, since a stream repeats its dates.
    """
    kind = logical_type.upper()
    if rendered is None:
        raise FluxError(
            FLX_3513, f"column {column!r} is null at row {row}, so the row has no event time"
        )
    if kind not in ("DATE", "TIMESTAMP"):
        raise FluxError(
            FLX_3513,
            f"column {column!r} is a {logical_type}, and an event time must be a DATE or a TIMESTAMP",
        )
    if not isinstance(rendered, str):
        raise FluxError(FLX_3513, f"column {column!r} at row {row} is not a rendered {kind}")
    try:
        return _instant_ms(kind, rendered)
    except ValueError:
        raise FluxError(
            FLX_3513, f"column {column!r} at row {row} is not a rendered {kind}"
        ) from None


def encode_key(
    values: Sequence[object], columns: Sequence[str], key_format: str, *, row: int
) -> bytes:
    """The key bytes of one row: its text (``string``) or its compact JSON object (``json``).

    A JSON key lists the key columns in the order given.
    ``FLX-3513`` for a null value, naming the column and the row.
    """
    for column, value in zip(columns, values, strict=True):
        if value is None:
            raise FluxError(FLX_3513, f"key column {column!r} is null at row {row}")
    if key_format == "json":
        return _COMPACT(dict(zip(columns, values, strict=True))).encode("utf-8")
    if key_format != "string" or len(values) != 1:
        raise FluxError(
            FLX_3513,
            f"a key of {len(values)} columns needs key-format=json; key-format {key_format!r} "
            "writes one column",
        )
    value = values[0]
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, (int, str)):
        return str(value).encode("utf-8")
    raise FluxError(FLX_3513, f"key column {columns[0]!r} at row {row} has no text form")


def _constant_headers(
    run_id: str, expose_id: str, output_digest: str
) -> Tuple[Tuple[str, bytes], ...]:
    return (
        ("flux.synthetic", b"true"),
        ("flux.run-id", run_id.encode("utf-8")),
        ("flux.expose", expose_id.encode("utf-8")),
        ("flux.output-digest", output_digest.encode("utf-8")),
    )


def headers_for(
    *, run_id: str, expose_id: str, output_digest: str, row: int
) -> Tuple[Tuple[str, bytes], ...]:
    """The headers every record carries: it is synthetic, and which run, stream and row it is."""
    return (*_constant_headers(run_id, expose_id, output_digest), ("flux.row", str(row).encode()))


def _instant_kind(text: str) -> str:
    return "DATE" if _DATE.fullmatch(text) is not None else "TIMESTAMP"


class _Instant(NamedTuple):
    """Where a row holds an instant: its position, its type and the column's name."""

    position: int
    kind: str
    column: str


Sources = Tuple[Tuple[str, Optional[int]], ...]


class RecordBuilder:
    """Builds the records of one stream, row by row.

    Everything that depends on the stream and not on the row (where each column sits, which
    column fills each field of the envelope, the headers every record shares) is worked out
    once, here.
    """

    def __init__(
        self, shape: StreamShape, *, run_id: str, output_digest: str, engine_version: str
    ) -> None:
        self._shape = shape
        self._columns = shape.columns
        position = {column: index for index, column in enumerate(shape.columns)}
        kind = {
            column: logical.upper()
            for column, logical in zip(shape.columns, shape.types, strict=True)
        }
        self._constant = _constant_headers(run_id, shape.expose_id, output_digest)
        self._key_positions = tuple(position[column] for column in shape.key_columns)
        self._timestamp: Optional[_Instant] = None
        self._default_ms: Optional[int] = None
        if shape.timestamp_column is not None:
            column = shape.timestamp_column
            self._timestamp = _Instant(position[column], kind[column], column)
        self._envelope_plan: Optional[_EnvelopePlan] = None
        if shape.envelope is not None:
            self._envelope_plan = _EnvelopePlan(
                shape, shape.envelope, position, kind, engine_version
            )

    def records(self, row: Sequence[object], index: int, *, last: bool) -> List[KafkaRecord]:
        """The records of *row* (the stream's *index*-th): one, and a tombstone after a delete.

        *last* marks the stream's final row, which a snapshot's ``source.snapshot`` reports.
        """
        shape = self._shape
        positions = self._key_positions
        if len(positions) == 1 and shape.key_format == "string":
            first = row[positions[0]]
            if isinstance(first, str) and type(first) is str:
                key = first.encode("utf-8")
            elif isinstance(first, int) and type(first) is int:
                key = b"%d" % first
            else:
                key = encode_key([first], shape.key_columns, "string", row=index)
        else:
            key = encode_key(
                [row[position] for position in positions],
                shape.key_columns,
                shape.key_format,
                row=index,
            )
        timestamp = self._timestamp_of(row, index)
        headers = (*self._constant, ("flux.row", b"%d" % index))
        plan = self._envelope_plan
        if plan is None:
            value: Dict[str, Any] = dict(zip(self._columns, row, strict=True))
            return [KafkaRecord(key, value, timestamp, headers, index, key)]
        op, body = plan.build(row, index, timestamp, last=last)
        records = [KafkaRecord(key, body, timestamp, headers, index, key)]
        if op == "d":
            records.append(KafkaRecord(key, None, timestamp, headers, index, key))
        return records

    def _timestamp_of(self, row: Sequence[object], index: int) -> int:
        at = self._timestamp
        if at is not None:
            found = row[at.position]
            if type(found) is str and at.kind in _INSTANT_KINDS:
                try:
                    return _instant_ms(at.kind, found)
                except ValueError:
                    pass  # the call below says what is wrong, naming the column and the row
            return epoch_ms(found, at.kind, column=at.column, row=index)
        if self._default_ms is None:
            shape = self._shape
            default = shape.timestamp_default
            if default is None:
                raise FluxError(
                    FLX_3513,
                    f"expose {shape.expose_id!r} has no column or default for its event time",
                )
            self._default_ms = epoch_ms(default, _instant_kind(default), column="until", row=index)
        return self._default_ms


class _EnvelopePlan:
    """Debezium's envelope for one stream: which column fills each field, worked out once."""

    def __init__(
        self,
        shape: StreamShape,
        envelope: Envelope,
        position: Mapping[str, int],
        kind: Mapping[str, str],
        engine_version: str,
    ) -> None:
        names = tuple(name for name, _ in envelope.fields)
        copied, after, before = dict(envelope.copied), dict(envelope.after), dict(envelope.before)
        keys = {name: copied[name] for name in envelope.key_fields if name in copied}

        def sources(*maps: Mapping[str, str]) -> Sources:
            found = []
            for name in names:
                column = next((m[name] for m in maps if m.get(name) is not None), None)
                found.append((name, None if column is None else position[column]))
            return tuple(found)

        self._after = sources(after, copied)
        self._before_update = sources(before, keys)
        self._before_delete = sources(before, copied)
        self._op: Optional[Tuple[int, str]] = None
        self._date: Optional[_Instant] = None
        self._at: Optional[_Instant] = None
        if envelope.op_column is not None:
            self._op = (position[envelope.op_column], envelope.op_column)
            date_column = envelope.date_column or ""
            self._date = _Instant(position[date_column], kind[date_column], date_column)
            if envelope.at_column is not None:
                at = envelope.at_column
                self._at = _Instant(position[at], kind[at], at)
        self._snapshot = envelope.op_column is None
        self._extra = tuple((column, position[column]) for column, _ in envelope.extra)
        self._constant = {
            "version": engine_version,
            "connector": "flux",
            "name": shape.simulation_id,
        }
        self._db, self._table = shape.product_ref, envelope.table

    def build(
        self, row: Sequence[object], index: int, timestamp: int, *, last: bool
    ) -> Tuple[str, Dict[str, Any]]:
        """The op of *row* and its envelope: ``before``, ``after``, ``source``, ``op``, ``ts_ms``."""
        if self._op is None:
            op = "r"
        else:
            found = row[self._op[0]]
            if not isinstance(found, str) or found not in _OPS:
                raise FluxError(
                    FLX_3513,
                    f"column {self._op[1]!r} at row {index} is not one of c, u or d, so the "
                    "change it records is not known",
                )
            op = found
        after: Optional[Dict[str, Any]] = None
        before: Optional[Dict[str, Any]] = None
        if op != "d":
            after = {name: None if at is None else row[at] for name, at in self._after}
        if op == "u":
            before = {name: None if at is None else row[at] for name, at in self._before_update}
        elif op == "d":
            before = {name: None if at is None else row[at] for name, at in self._before_delete}
        date_at, at_at = self._date, self._at
        if date_at is None:
            happened = source_ms = timestamp
        else:
            happened = epoch_ms(
                row[date_at.position], date_at.kind, column=date_at.column, row=index
            )
            source_ms = (
                happened
                if at_at is None
                else epoch_ms(row[at_at.position], at_at.kind, column=at_at.column, row=index)
            )
        source: Dict[str, Any] = {
            **self._constant,
            "ts_ms": source_ms,
            "snapshot": ("last" if last else "true") if self._snapshot else "false",
            "db": self._db,
            "table": self._table,
        }
        for column, at in self._extra:
            source[column] = row[at]
        return op, {
            "before": before,
            "after": after,
            "source": source,
            "op": op,
            "ts_ms": happened,
        }
