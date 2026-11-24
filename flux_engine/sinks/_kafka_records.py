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
serializers and the canonical JSON writer decide what a value looks like on the wire. A refusal
names a column and a row's position, never a value.

Standard library and :mod:`flux_engine` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flux_engine.core.canonical import canonical_bytes
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

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_DAY_MS = 86_400_000
_OPS = ("c", "u", "d")


@dataclass(frozen=True)
class KafkaRecord:
    """One record to send: ``value`` is ``None`` for a tombstone."""

    key: bytes
    value: Optional[Dict[str, Any]]
    timestamp_ms: int
    headers: Tuple[Tuple[str, bytes], ...]
    row: int
    entity: bytes


def epoch_ms(rendered: object, logical_type: str, *, column: str, row: int) -> int:
    """Milliseconds since 1970 (UTC) of a rendered DATE or TIMESTAMP, exactly.

    A DATE is its midnight. ``FLX-3513`` for a null, a value that is not a date or an RFC 3339
    instant, and a column of any other type; a time before 1970 is returned negative.
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
    if kind == "DATE":
        if _DATE.fullmatch(rendered) is None:
            raise FluxError(FLX_3513, f"column {column!r} at row {row} is not a rendered DATE")
        try:
            day = date.fromisoformat(rendered)
        except ValueError:
            raise FluxError(
                FLX_3513, f"column {column!r} at row {row} is not a rendered DATE"
            ) from None
        return (day.toordinal() - _EPOCH_ORDINAL) * _DAY_MS
    try:
        return parse_rfc3339_ms(rendered)
    except FluxError:
        raise FluxError(
            FLX_3513, f"column {column!r} at row {row} is not a rendered TIMESTAMP"
        ) from None


def encode_key(
    values: Sequence[object], columns: Sequence[str], key_format: str, *, row: int
) -> bytes:
    """The key bytes of one row: its text (``string``) or its canonical JSON object (``json``).

    ``FLX-3513`` for a null value, naming the column and the row.
    """
    for column, value in zip(columns, values, strict=True):
        if value is None:
            raise FluxError(FLX_3513, f"key column {column!r} is null at row {row}")
    if key_format == "json":
        return canonical_bytes(dict(zip(columns, values, strict=True)))
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


def headers_for(
    *, run_id: str, expose_id: str, output_digest: str, row: int
) -> Tuple[Tuple[str, bytes], ...]:
    """The headers every record carries: it is synthetic, and which run, stream and row it is."""
    return (
        ("flux.synthetic", b"true"),
        ("flux.run-id", run_id.encode("utf-8")),
        ("flux.expose", expose_id.encode("utf-8")),
        ("flux.output-digest", output_digest.encode("utf-8")),
        ("flux.row", str(row).encode("ascii")),
    )


def _instant_kind(text: str) -> str:
    return "DATE" if _DATE.fullmatch(text) is not None else "TIMESTAMP"


class RecordBuilder:
    """Builds the records of one stream, row by row."""

    def __init__(
        self, shape: StreamShape, *, run_id: str, output_digest: str, engine_version: str
    ) -> None:
        self._shape = shape
        self._run_id = run_id
        self._output_digest = output_digest
        self._engine_version = engine_version
        self._position = {column: index for index, column in enumerate(shape.columns)}
        self._kind = dict(zip(shape.columns, shape.types, strict=True))

    def records(self, row: Sequence[object], index: int, *, last: bool) -> List[KafkaRecord]:
        """The records of *row* (the stream's *index*-th): one, and a tombstone after a delete.

        *last* marks the stream's final row, which a snapshot's ``source.snapshot`` reports.
        """
        shape = self._shape
        key = encode_key(
            [row[self._position[column]] for column in shape.key_columns],
            shape.key_columns,
            shape.key_format,
            row=index,
        )
        timestamp = self._timestamp(row, index)
        headers = headers_for(
            run_id=self._run_id,
            expose_id=shape.expose_id,
            output_digest=self._output_digest,
            row=index,
        )
        envelope = shape.envelope
        if envelope is None:
            value: Dict[str, Any] = {
                column: row[self._position[column]] for column in shape.columns
            }
            return [KafkaRecord(key, value, timestamp, headers, index, key)]
        op = "r" if envelope.op_column is None else self._op(row, envelope.op_column, index)
        body = self._envelope(row, envelope, op, timestamp, index, last=last)
        records = [KafkaRecord(key, body, timestamp, headers, index, key)]
        if op == "d":
            records.append(KafkaRecord(key, None, timestamp, headers, index, key))
        return records

    def _timestamp(self, row: Sequence[object], index: int) -> int:
        shape = self._shape
        if shape.timestamp_column is not None:
            return epoch_ms(
                row[self._position[shape.timestamp_column]],
                self._kind[shape.timestamp_column],
                column=shape.timestamp_column,
                row=index,
            )
        default = shape.timestamp_default
        if default is None:
            raise FluxError(
                FLX_3513, f"expose {shape.expose_id!r} has no column or default for its event time"
            )
        return epoch_ms(default, _instant_kind(default), column="until", row=index)

    def _op(self, row: Sequence[object], column: str, index: int) -> str:
        op = row[self._position[column]]
        if not isinstance(op, str) or op not in _OPS:
            raise FluxError(
                FLX_3513,
                f"column {column!r} at row {index} is not one of c, u or d, so the "
                "change it records is not known",
            )
        return op

    def _fields(
        self,
        row: Sequence[object],
        envelope: Envelope,
        *named: Dict[str, str],
    ) -> Dict[str, Any]:
        """A record of the history's fields, each from the first mapping that names a column."""
        record: Dict[str, Any] = {}
        for name, _ in envelope.fields:
            value = None
            for mapping in named:
                column = mapping.get(name)
                if column is not None:
                    value = row[self._position[column]]
                    break
            record[name] = value
        return record

    def _envelope(
        self,
        row: Sequence[object],
        envelope: Envelope,
        op: str,
        timestamp: int,
        index: int,
        *,
        last: bool,
    ) -> Dict[str, Any]:
        shape = self._shape
        copied = dict(envelope.copied)
        after_map, before_map = dict(envelope.after), dict(envelope.before)
        after: Optional[Dict[str, Any]] = None
        before: Optional[Dict[str, Any]] = None
        if op != "d":
            after = self._fields(row, envelope, after_map, copied)
        if op == "u":
            keys = {name: copied[name] for name in envelope.key_fields if name in copied}
            before = self._fields(row, envelope, before_map, keys)
        elif op == "d":
            before = self._fields(row, envelope, before_map, copied)
        if envelope.op_column is None:
            happened = timestamp
            source_ms = timestamp
        else:
            date_column = envelope.date_column or ""
            happened = epoch_ms(
                row[self._position[date_column]],
                self._kind[date_column],
                column=date_column,
                row=index,
            )
            at_column = envelope.at_column
            source_ms = (
                happened
                if at_column is None
                else epoch_ms(
                    row[self._position[at_column]],
                    self._kind[at_column],
                    column=at_column,
                    row=index,
                )
            )
        source: Dict[str, Any] = {
            "version": self._engine_version,
            "connector": "flux",
            "name": shape.simulation_id,
            "ts_ms": source_ms,
            "snapshot": ("last" if last else "true") if envelope.op_column is None else "false",
            "db": shape.product_ref,
            "table": envelope.table,
        }
        for column, _ in envelope.extra:
            source[column] = row[self._position[column]]
        return {"before": before, "after": after, "source": source, "op": op, "ts_ms": happened}
