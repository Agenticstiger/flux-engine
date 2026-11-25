# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a Kafka record's value looks like on the wire (ADR 0038 section 8).

Without a registry a value is the compact JSON of its object (:func:`plain_json`), its fields in
the order the stream declares them. With one, :class:`ConfluentSerdes` builds confluent-kafka's registry serializers:

* a stream whose contract names a subject is written against that subject, never registering
  anything under it, after its fields are checked against the stream's columns;
* any other stream gets a schema derived from its columns (Avro, or JSON Schema for
  ``value-format=json``) registered under the topic's ``-value`` subject.

Once a schema's id is known (it is registered, or looked up, when the stream opens) the bytes of a
value are written here, behind Confluent's framing: Avro with fastavro, and JSON Schema as compact
JSON. They equal what confluent-kafka's serializers write (the tests compare them), without the
lookups and the validation they repeat for every record. A subject whose schema has data-contract
rules, or references another schema, keeps confluent-kafka's Avro serializer; a contract's JSON
subject keeps its ``JSONSerializer`` and validation.

Values come in rendered (text, integers, booleans, null) and are converted by the field types of
the schema they are written against, exactly: a decimal text becomes a ``Decimal`` that fits the
field's precision and scale, a date its day number, a timestamp its milliseconds. A value a field
cannot hold exactly is ``FLX-3515``, naming the field and never the value. A float never appears.

The converters and the schema derivation are standard library only; ``confluent_kafka`` and
``fastavro`` (which confluent-kafka's Avro extra installs) are imported inside
:class:`ConfluentSerdes` when it builds a serializer.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import struct
from collections import abc as _abc
from dataclasses import dataclass, field
from datetime import date
from decimal import Context, Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.sinks._kafka_shape import Envelope, StreamShape
from flux_engine.sinks._secrets import Known, scrub
from flux_engine.sinks.errors import FLX_3515

__all__ = [
    "ConfluentSerdes",
    "RegistryAccess",
    "Serdes",
    "ValueSerializer",
    "avro_schema_for",
    "compact_json",
    "json_schema_for",
    "plain_json",
    "require_supported_subject",
]

_DECIMAL = re.compile(r"DECIMAL\(\s*([0-9]+)\s*,\s*([0-9]+)\s*\)", re.ASCII | re.IGNORECASE)
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_NAME_CHARS = re.compile(r"[^A-Za-z0-9_]")
_AVRO_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)


@dataclass(frozen=True)
class RegistryAccess:
    """The registry the operator named: its URL, and what the environment gave for it."""

    url: str
    auth: Optional[str] = field(default=None, repr=False)
    ca_location: Optional[str] = None
    #: The seconds the client waits for the registry to answer one request.
    timeout: float = 30.0


class ValueSerializer(Protocol):
    """Turns one record's value object into the bytes of its value."""

    description: str

    def __call__(self, value: Mapping[str, Any], *, topic: str) -> bytes:
        """The bytes of *value* for a record of *topic*."""


class Serdes(Protocol):
    """Builds the value serializer of one stream against a registry."""

    def serializer(
        self, shape: StreamShape, *, value_format: str, registry: RegistryAccess
    ) -> ValueSerializer:
        """The serializer for *shape*; may fetch or register a schema."""

    def close(self) -> None:
        """Release the registry client; a later call opens it again."""


#: Compact JSON with text as it is and no NaN: one encoder, built once.
_COMPACT = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode


def compact_json(value: Any) -> bytes:
    """The compact JSON of *value* in the order its mappings hold, as UTF-8.

    Deterministic, since the order is the stream's own, and readable by every JSON parser.
    """
    return _COMPACT(value).encode("utf-8")


class _PlainJson:
    description = "json"

    def __call__(self, value: Mapping[str, Any], *, topic: str) -> bytes:
        return _COMPACT(value).encode("utf-8")


def plain_json(shape: StreamShape) -> ValueSerializer:
    """The compact JSON of a value's rendered object, fields in the stream's order: no registry."""
    del shape
    return _PlainJson()


def require_supported_subject(shape: StreamShape) -> None:
    """``FLX-3515`` for a contract subject this version cannot write: Protobuf, or a Glue registry."""
    subject = shape.subject
    if subject is None:
        return
    if subject.kind != "confluent":
        raise FluxError(
            FLX_3515,
            f"expose {shape.expose_id!r}: its contract names a {subject.kind} registry, which "
            "this version cannot write to; only a Confluent-compatible registry is supported",
        )
    if subject.value_format not in ("AVRO", "JSON"):
        raise FluxError(
            FLX_3515,
            f"expose {shape.expose_id!r}: its subject is {subject.value_format.title()}, which "
            "this version cannot write; only Avro and JSON subjects are supported",
        )


# -- logical types ------------------------------------------------------------------


def _kind_of(logical: str, *, column: str) -> Tuple[str, int, int]:
    """The base type of a FLUX logical type text, and a DECIMAL's precision and scale."""
    text = logical.strip()
    match = _DECIMAL.fullmatch(text)
    if match is not None:
        return "DECIMAL", int(match.group(1)), int(match.group(2))
    kind = text.upper()
    if kind in ("STRING", "INT", "BOOL", "DATE", "TIMESTAMP", "JSON", "BYTES"):
        return kind, 0, 0
    raise FluxError(FLX_3515, f"column {column!r} has a type no registry schema is derived for")


def _avro_type(logical: str, *, column: str) -> Any:
    kind, precision, scale = _kind_of(logical, column=column)
    if kind in ("STRING", "JSON"):
        return "string"
    if kind == "INT":
        return "long"
    if kind == "BOOL":
        return "boolean"
    if kind == "BYTES":
        return "bytes"
    if kind == "DATE":
        return {"type": "int", "logicalType": "date"}
    if kind == "TIMESTAMP":
        return {"type": "long", "logicalType": "timestamp-millis"}
    return {"type": "bytes", "logicalType": "decimal", "precision": precision, "scale": scale}


def _json_type(logical: str, *, column: str) -> Dict[str, Any]:
    kind, _, _ = _kind_of(logical, column=column)
    if kind == "INT":
        return {"type": ["integer", "null"]}
    if kind == "DECIMAL":
        return {"type": ["number", "null"]}
    if kind == "BOOL":
        return {"type": ["boolean", "null"]}
    if kind == "DATE":
        return {"type": ["string", "null"], "format": "date"}
    if kind == "TIMESTAMP":
        return {"type": ["string", "null"], "format": "date-time"}
    if kind == "BYTES":
        return {"type": ["string", "null"], "contentEncoding": "base64"}
    return {"type": ["string", "null"]}


def _checked_name(name: str, *, what: str) -> str:
    if _AVRO_NAME.fullmatch(name) is None:
        raise FluxError(
            FLX_3515,
            f"{what} {name!r} is not a name an Avro schema can hold (letters, digits and "
            "underscores, not starting with a digit)",
        )
    return name


def namespace_of(topic: str) -> str:
    """``flux.<topic>`` with every character Avro does not allow in a name written as ``_``."""
    body = _NAME_CHARS.sub("_", topic)
    if body[:1].isdigit():
        body = "_" + body
    return f"flux.{body}"


def _nullable(avro_type: Any) -> List[Any]:
    return ["null", avro_type]


def _record_fields(columns: Sequence[Tuple[str, str]]) -> List[Dict[str, Any]]:
    return [
        {
            "name": _checked_name(name, what="column"),
            "type": _nullable(_avro_type(logical, column=name)),
            "default": None,
        }
        for name, logical in columns
    ]


def _envelope_avro(shape: StreamShape, envelope: Envelope, namespace: str) -> Dict[str, Any]:
    value = {
        "type": "record",
        "name": "Value",
        "namespace": namespace,
        "fields": _record_fields(envelope.fields),
    }
    source_fields: List[Dict[str, Any]] = [
        {"name": "version", "type": "string"},
        {"name": "connector", "type": "string"},
        {"name": "name", "type": "string"},
        {"name": "ts_ms", "type": "long"},
        {"name": "snapshot", "type": "string"},
        {"name": "db", "type": "string"},
        {"name": "table", "type": "string"},
    ]
    source_fields += _record_fields(envelope.extra)
    return {
        "type": "record",
        "name": "Envelope",
        "namespace": namespace,
        "fields": [
            {"name": "before", "type": _nullable(value), "default": None},
            {"name": "after", "type": _nullable(f"{namespace}.Value"), "default": None},
            {
                "name": "source",
                "type": {
                    "type": "record",
                    "name": "Source",
                    "namespace": namespace,
                    "fields": source_fields,
                },
            },
            {"name": "op", "type": "string"},
            {"name": "ts_ms", "type": "long"},
        ],
    }


def avro_schema_for(shape: StreamShape) -> Dict[str, Any]:
    """The Avro schema derived from *shape*: the row's record, or Debezium's envelope."""
    namespace = namespace_of(shape.topic)
    if shape.envelope is not None:
        return _envelope_avro(shape, shape.envelope, namespace)
    return {
        "type": "record",
        "name": "Row",
        "namespace": namespace,
        "fields": _record_fields(list(zip(shape.columns, shape.types, strict=True))),
    }


def _object_schema(columns: Sequence[Tuple[str, str]], *, title: str) -> Dict[str, Any]:
    return {
        "type": "object",
        "title": title,
        "properties": {name: _json_type(logical, column=name) for name, logical in columns},
        "additionalProperties": False,
    }


def json_schema_for(shape: StreamShape) -> Dict[str, Any]:
    """The JSON Schema derived from *shape*: the row's object, or Debezium's envelope."""
    envelope = shape.envelope
    if envelope is None:
        schema = _object_schema(list(zip(shape.columns, shape.types, strict=True)), title="Row")
        schema["$schema"] = "http://json-schema.org/draft-07/schema#"
        return schema
    value = _object_schema(envelope.fields, title="Value")
    value["type"] = ["object", "null"]
    source = _object_schema(envelope.extra, title="Source")
    fixed = {
        "version": {"type": "string"},
        "connector": {"type": "string"},
        "name": {"type": "string"},
        "ts_ms": {"type": "integer"},
        "snapshot": {"type": "string"},
        "db": {"type": "string"},
        "table": {"type": "string"},
    }
    source["properties"] = {**fixed, **source["properties"]}
    source["required"] = list(fixed)
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "title": "Envelope",
        "properties": {
            "before": value,
            "after": value,
            "source": source,
            "op": {"type": "string"},
            "ts_ms": {"type": "integer"},
        },
        "required": ["source", "op", "ts_ms"],
        "additionalProperties": False,
    }


# -- converting rendered values by the schema's field types ------------------------


Converter = Callable[[Any], Any]


def _refuse(at: str, why: str) -> FluxError:
    return FluxError(FLX_3515, f"field {at!r}: {why}")


def _fit_decimal(precision: int, scale: int, at: str) -> Callable[[str], Decimal]:
    """The check of a decimal text against ``decimal(precision, scale)``, built once."""
    step = Decimal(1).scaleb(-scale)
    # Wide enough to hold any value that fits, so only a value that does not fit is refused.
    context = Context(prec=max(precision, 28) + 2)

    def fit(text: str) -> Decimal:
        try:
            value = Decimal(text)
        except (InvalidOperation, ValueError):
            raise _refuse(at, "a value is not a decimal number") from None
        if not value.is_finite():
            raise _refuse(at, "a value is not a finite decimal number")
        try:
            fitted = value.quantize(step, context=context)
        except InvalidOperation:
            raise _refuse(at, f"a value does not fit decimal({precision},{scale})") from None
        if fitted != value or len(fitted.as_tuple().digits) > precision:
            raise _refuse(at, f"a value does not fit decimal({precision},{scale}) exactly")
        return fitted

    return fit


@lru_cache(maxsize=65536)
def _day_number(text: str) -> int:
    """The day number of a rendered DATE; ``ValueError`` when it is not one. Cached by text."""
    if _DATE.fullmatch(text) is None:
        raise ValueError(text)
    return date.fromisoformat(text).toordinal() - _EPOCH_ORDINAL


@lru_cache(maxsize=65536)
def _milliseconds(text: str) -> int:
    """The milliseconds of a rendered TIMESTAMP; ``ValueError`` when it is not one. Cached."""
    try:
        return parse_rfc3339_ms(text)
    except FluxError:
        raise ValueError(text) from None


def _days(text: str, at: str) -> int:
    try:
        return _day_number(text)
    except ValueError:
        raise _refuse(at, "a value is not a rendered DATE") from None


def _millis(text: str, at: str) -> int:
    try:
        return _milliseconds(text)
    except ValueError:
        raise _refuse(at, "a value is not a rendered TIMESTAMP") from None


def _text_of(value: Any, at: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    raise _refuse(at, "a value has no text form")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _avro_leaf(node: Any, at: str) -> Converter:
    """The converter of a non-union, non-record Avro type."""
    logical: Optional[str] = None
    if isinstance(node, Mapping):
        logical = node.get("logicalType")
        base = node.get("type")
    else:
        base = node
    if base == "string":

        def to_string(value: Any) -> Any:
            return value if type(value) is str else _text_of(value, at)

        return to_string
    if base in ("int", "long"):
        if logical == "date":

            def to_days(value: Any) -> Any:
                return _days(value, at) if isinstance(value, str) else _wrong(at, "DATE")

            return to_days
        if logical in (
            "timestamp-millis",
            "local-timestamp-millis",
            "timestamp-micros",
            "local-timestamp-micros",
        ):
            factor = 1000 if logical.endswith("micros") else 1

            def to_millis(value: Any) -> Any:
                if not isinstance(value, str):
                    return _wrong(at, "TIMESTAMP")
                return _millis(value, at) * factor

            return to_millis

        low, high = (-(2**31), 2**31 - 1) if base == "int" else (-(2**63), 2**63 - 1)

        def to_int(value: Any) -> Any:
            if type(value) is int or _is_int(value):
                if low <= value <= high:
                    return value
                raise _refuse(at, f"a value is outside the range of an Avro {base}")
            raise _refuse(at, "a value is not an integer")

        return to_int
    if base == "boolean":

        def to_bool(value: Any) -> Any:
            if isinstance(value, bool):
                return value
            raise _refuse(at, "a value is not a boolean")

        return to_bool
    if base == "bytes":
        if logical == "decimal" and isinstance(node, Mapping):
            fit = _fit_decimal(int(node.get("precision", 0)), int(node.get("scale", 0)), at)

            def to_decimal(value: Any) -> Any:
                if not isinstance(value, str):
                    return _wrong(at, "DECIMAL")
                return fit(value)

            return to_decimal

        def to_bytes(value: Any) -> Any:
            if not isinstance(value, str):
                raise _refuse(at, "a value is not base64 text")
            try:
                return base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                raise _refuse(at, "a value is not base64 text") from None

        return to_bytes
    raise _refuse(at, f"the type {_type_name(node)} cannot hold a rendered value exactly")


def _wrong(at: str, kind: str) -> Any:
    raise _refuse(at, f"a value is not a rendered {kind}")


def _type_name(node: Any) -> str:
    if isinstance(node, Mapping):
        return str(node.get("logicalType") or node.get("type"))
    return str(node)


def _avro_converter(node: Any, at: str, names: Dict[str, Any]) -> Tuple[Converter, bool]:
    """The converter of an Avro type, and whether it takes null."""
    if isinstance(node, list):
        branches = [item for item in node if item != "null"]
        nullable = len(branches) != len(node)
        if len(branches) != 1:
            raise _refuse(at, "a union of several types cannot take a rendered value")
        inner, _ = _avro_converter(branches[0], at, names)
        return inner, nullable
    if isinstance(node, str) and node not in _PRIMITIVES:
        known = names.get(node)
        if known is None:
            raise _refuse(at, "the schema names a type it does not define")
        return _avro_converter(known, at, names)
    if isinstance(node, Mapping) and node.get("type") == "record":
        return _avro_record(node, at, names), False
    if isinstance(node, Mapping) and isinstance(node.get("type"), (list, Mapping)):
        return _avro_converter(node["type"], at, names)
    return _avro_leaf(node, at), False


_PRIMITIVES = frozenset({"null", "boolean", "int", "long", "float", "double", "bytes", "string"})


def _qualified(record: Mapping[str, Any]) -> str:
    name = str(record.get("name", ""))
    namespace = record.get("namespace")
    return name if "." in name or not namespace else f"{namespace}.{name}"


_MISSING = object()


def _avro_record(node: Mapping[str, Any], at: str, names: Dict[str, Any]) -> Converter:
    names[_qualified(node)] = node
    fields: List[Tuple[str, Converter, bool, bool, str]] = []
    for item in node.get("fields", []):
        name = str(item["name"])
        where = f"{at}.{name}" if at else name
        convert, nullable = _avro_converter(item["type"], where, names)
        fields.append((name, convert, nullable, "default" in item, where))
    plan = tuple(fields)

    def to_record(value: Any) -> Any:
        if value is None:
            return None
        if type(value) is not dict and not isinstance(value, _abc.Mapping):
            value = {}
        out: Dict[str, Any] = {}
        for name, convert, nullable, has_default, where in plan:
            item = value.get(name, _MISSING)
            if item is _MISSING:
                if has_default:
                    continue
                raise _refuse(where, "the value has no such field")
            if item is None:
                if not nullable:
                    raise _refuse(where, "a null cannot go into a field that takes none")
                out[name] = None
            else:
                out[name] = convert(item)
        return out

    return to_record


def avro_converter(schema: Mapping[str, Any]) -> Converter:
    """The converter of rendered values for the Avro *schema* (a record)."""
    if schema.get("type") != "record":
        raise FluxError(FLX_3515, "the subject's schema is not a record, so no row fits it")
    return _avro_record(schema, "", {})


def _json_converter(node: Mapping[str, Any], at: str) -> Tuple[Converter, bool]:
    declared = node.get("type")
    kinds: List[str]
    if isinstance(declared, list):
        kinds = [str(item) for item in declared]
    elif declared is None and isinstance(node.get("oneOf"), list):
        kinds = []
        for branch in node["oneOf"]:
            kinds.append(str(branch.get("type")))
        if sorted(kinds) != ["null", kinds[0] if kinds[0] != "null" else kinds[1]]:
            raise _refuse(at, "a choice of several types cannot take a rendered value")
        inner = next(item for item in node["oneOf"] if item.get("type") != "null")
        convert, _ = _json_converter(inner, at)
        return convert, True
    else:
        kinds = [str(declared)]
    nullable = "null" in kinds
    kinds = [item for item in kinds if item != "null"]
    if len(kinds) != 1:
        raise _refuse(at, "a field of several types cannot take a rendered value")
    kind = kinds[0]
    if kind == "integer":

        def to_int(value: Any) -> Any:
            if type(value) is int or _is_int(value):
                return value
            raise _refuse(at, "a value is not an integer")

        return to_int, nullable
    if kind == "number":

        def to_number(value: Any) -> Any:
            if isinstance(value, str):
                try:
                    number = Decimal(value)
                except (InvalidOperation, ValueError):
                    raise _refuse(at, "a value is not a decimal number") from None
                if not number.is_finite():
                    raise _refuse(at, "a value is not a finite decimal number")
                return number
            if _is_int(value):
                return Decimal(value)
            raise _refuse(at, "a value is not a number")

        return to_number, nullable
    if kind == "string":

        def to_string(value: Any) -> Any:
            return value if type(value) is str else _text_of(value, at)

        return to_string, nullable
    if kind == "boolean":

        def to_bool(value: Any) -> Any:
            if isinstance(value, bool):
                return value
            raise _refuse(at, "a value is not a boolean")

        return to_bool, nullable
    if kind == "object":
        return _json_object(node, at), nullable
    raise _refuse(at, f"the type {kind} cannot hold a rendered value exactly")


def _json_object(node: Mapping[str, Any], at: str) -> Converter:
    properties = node.get("properties")
    if not isinstance(properties, Mapping):
        raise _refuse(at or "(value)", "an object with no declared properties cannot be checked")
    fields: List[Tuple[str, Converter, bool, str]] = []
    for name, sub in properties.items():
        where = f"{at}.{name}" if at else str(name)
        convert, nullable = _json_converter(sub, where)
        fields.append((str(name), convert, nullable, where))
    plan = tuple(fields)
    listed = node.get("required")
    required = frozenset(str(name) for name in listed) if isinstance(listed, list) else frozenset()

    def to_object(value: Any) -> Any:
        if value is None:
            return None
        if type(value) is not dict and not isinstance(value, _abc.Mapping):
            value = {}
        out: Dict[str, Any] = {}
        for name, convert, nullable, where in plan:
            item = value.get(name, _MISSING)
            if item is _MISSING:
                if name in required:
                    raise _refuse(where, "the value has no such field")
                continue
            if item is None:
                if not nullable:
                    raise _refuse(where, "a null cannot go into a field that takes none")
                out[name] = None
            else:
                out[name] = convert(item)
        return out

    return to_object


def json_converter(schema: Mapping[str, Any]) -> Converter:
    """The converter of rendered values for the JSON Schema *schema* (an object)."""
    if schema.get("type") != "object":
        raise FluxError(FLX_3515, "the subject's schema is not an object, so no row fits it")
    return _json_object(schema, "")


_KEY = json.encoder.encode_basestring_ascii
_TEXT = json.encoder.encode_basestring


def _json_dumps(value: Any) -> str:
    """JSON text of a converted value; a ``Decimal`` is written as the number it is, exactly.

    Compact, in the order a mapping holds. A key is written ASCII-only and a value as it is.
    """
    kind = type(value)
    if kind is str:
        return _TEXT(value)
    if kind is int:
        return int.__repr__(value)
    if value is None:
        return "null"
    if kind is bool:
        return "true" if value else "false"
    if kind is Decimal:
        return format(value, "f")
    if kind is dict:
        return "{" + ",".join([f"{_KEY(str(k))}:{_json_dumps(v)}" for k, v in value.items()]) + "}"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{_KEY(str(k))}:{_json_dumps(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_json_dumps(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


# -- checking a subject's fields against the stream's columns ---------------------


def _check_fields(shape: StreamShape, fields: Sequence[Tuple[str, bool]], subject: str) -> None:
    """``FLX-3515`` for a required field no column fills and for a column no field takes."""
    names = {name for name, _ in fields}
    missing = [name for name, required in fields if required and name not in shape.columns]
    unmatched = [column for column in shape.columns if column not in names]
    if missing or unmatched:
        parts: List[str] = []
        if missing:
            parts.append(f"field(s) {', '.join(repr(n) for n in missing)} have no column")
        if unmatched:
            parts.append(f"column(s) {', '.join(repr(n) for n in unmatched)} have no field")
        raise FluxError(
            FLX_3515,
            f"expose {shape.expose_id!r}: subject {subject!r}: " + "; ".join(parts),
        )


def _avro_field_list(schema: Mapping[str, Any]) -> List[Tuple[str, bool]]:
    return [(str(item["name"]), "default" not in item) for item in schema.get("fields", [])]


def _json_field_list(schema: Mapping[str, Any]) -> List[Tuple[str, bool]]:
    required = set(schema.get("required") or [])
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    return [(str(name), name in required) for name in properties]


# -- the serializers ---------------------------------------------------------------


def _serializer_failure(
    exc: BaseException,
    known: Callable[[], Sequence[Known]],
    doing: str = "the registry serializer failed",
) -> FluxError:
    """``FLX-3505`` for a registry error: one scrubbed line, the library's exception unchained."""
    from flux_engine.sinks.errors import FLX_3505

    lines = scrub(str(exc), known(), sink="kafka").strip().splitlines()
    return FluxError(
        FLX_3505,
        f"kafka: {doing}: {type(exc).__name__}{': ' + lines[0][:300] if lines else ''}",
    )


#: Confluent's framing: a zero byte, then the schema's id as four big-endian bytes.
def _frame_head(schema_id: int) -> bytes:
    return b"\x00" + struct.pack(">I", schema_id)


class _FramedAvro:
    """Avro bytes behind Confluent's framing, written by fastavro into one reused buffer.

    The schema's id is known before the first record (it was registered or looked up once), so
    nothing here asks the registry again. The bytes are those of confluent-kafka's
    ``AvroSerializer`` for the same schema, id and value, which a test compares.
    """

    def __init__(
        self,
        description: str,
        schema_id: int,
        parsed: Any,
        convert: Converter,
        known: Callable[[], Sequence[Known]],
    ) -> None:
        from fastavro import schemaless_writer

        self.description = description
        self._parsed = parsed
        self._convert = convert
        self._known = known
        self._write = schemaless_writer
        head = _frame_head(schema_id)
        self._head = len(head)
        self._buffer = io.BytesIO(head)
        self._buffer.seek(self._head)

    def __call__(self, value: Mapping[str, Any], *, topic: str) -> bytes:
        converted = self._convert(value)
        buffer = self._buffer
        buffer.seek(self._head)
        buffer.truncate()
        try:
            self._write(buffer, self._parsed, converted)
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _serializer_failure(exc, self._known) from None
        return buffer.getvalue()


class _FramedJson:
    """JSON text behind Confluent's framing, for a schema this module derived.

    confluent-kafka's ``JSONSerializer`` validates every record against the schema with
    ``jsonschema``, which is most of its cost. A derived schema holds nothing the converter does
    not already hold a value to (each field's type and whether it takes null, and no field the
    schema does not list), so the converter is the check, built from the same schema; a test
    validates every kind of record against the registered schema. The bytes are those of
    ``JSONSerializer`` for the same schema, id and value.
    """

    def __init__(self, description: str, schema_id: int, convert: Converter) -> None:
        self.description = description
        self._head = _frame_head(schema_id)
        self._convert = convert

    def __call__(self, value: Mapping[str, Any], *, topic: str) -> bytes:
        return self._head + _json_dumps(self._convert(value)).encode("utf-8")


class _Registered:
    """A serializer built over a confluent-kafka serializer and a converter."""

    def __init__(
        self,
        description: str,
        inner: Any,
        convert: Converter,
        known: Callable[[], Sequence[Known]],
        message_field: Any,
        serialization_context: Any,
    ) -> None:
        self.description = description
        self._inner = inner
        self._convert = convert
        self._known = known
        self._field = message_field
        self._context = serialization_context

    def __call__(self, value: Mapping[str, Any], *, topic: str) -> bytes:
        converted = self._convert(value)
        try:
            data = self._inner(converted, self._context(topic, self._field))
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _serializer_failure(exc, self._known) from None
        if not isinstance(data, bytes):
            raise FluxError(FLX_3515, "the registry serializer returned no bytes")
        return data


class ConfluentSerdes:
    """Registry serializers over confluent-kafka's Avro and JSON Schema serdes.

    *client_factory* builds the registry client from a :class:`RegistryAccess`; the default is
    ``SchemaRegistryClient``, a test passes one that returns a mock. *known* gives the strings an
    error is scrubbed of.
    """

    def __init__(
        self,
        *,
        client_factory: Optional[Callable[[RegistryAccess], Any]] = None,
        known: Callable[[], Sequence[Known]] = lambda: (),
    ) -> None:
        self._client_factory = client_factory
        self._known = known
        self._clients: Dict[RegistryAccess, Any] = {}

    def _client(self, registry: RegistryAccess) -> Any:
        """The one client of a registry, opened when first needed and kept until :meth:`close`."""
        if registry not in self._clients:
            self._clients[registry] = self._new_client(registry)
        return self._clients[registry]

    def _new_client(self, registry: RegistryAccess) -> Any:
        if self._client_factory is not None:
            return self._client_factory(registry)
        from confluent_kafka.schema_registry import SchemaRegistryClient

        conf: Dict[str, Any] = {"url": registry.url, "timeout": registry.timeout}
        if registry.auth:
            conf["basic.auth.user.info"] = registry.auth
        if registry.ca_location:
            conf["ssl.ca.location"] = registry.ca_location
        return SchemaRegistryClient(conf)

    def _fail(self, exc: BaseException, doing: str) -> FluxError:
        return _serializer_failure(exc, self._known, doing)

    def close(self) -> None:
        """Close the registry clients; the next serializer opens one again."""
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            closer = getattr(client, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    def serializer(
        self, shape: StreamShape, *, value_format: str, registry: RegistryAccess
    ) -> ValueSerializer:
        """The serializer of *shape*: against its contract's subject, or against a derived schema."""
        require_supported_subject(shape)
        try:
            client = self._client(registry)
            if shape.subject is not None:
                return self._contract(shape, client)
            return self._derived(shape, client, value_format)
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc, "the registry could not be used") from None

    def _contract(self, shape: StreamShape, client: Any) -> ValueSerializer:
        from confluent_kafka.schema_registry.avro import AvroSerializer
        from confluent_kafka.schema_registry.json_schema import JSONSerializer
        from confluent_kafka.serialization import MessageField, SerializationContext

        subject = shape.subject
        assert subject is not None
        name = subject.subject
        registered = (
            client.get_latest_version(name)
            if subject.version == "latest"
            else client.get_version(name, int(subject.version))
        )
        text = registered.schema.schema_str
        document = json.loads(text)
        conf: Dict[str, Any] = {
            "auto.register.schemas": False,
            "subject.name.strategy": lambda _ctx, _record: name,
        }
        if subject.version == "latest":
            conf["use.latest.version"] = True
        else:
            conf["use.schema.id"] = registered.schema_id
        if subject.value_format == "AVRO":
            _check_fields(shape, _avro_field_list(document), name)
            convert = avro_converter(document)
            description = f"avro:{name}@{registered.schema_id}"
            if registered.schema.rule_set is None and not registered.schema.references:
                # No data-contract rules run inside the serializer and no other schema is
                # referenced, so the bytes are the framing and fastavro's own.
                from fastavro import parse_schema

                return _FramedAvro(
                    description, registered.schema_id, parse_schema(document), convert, self._known
                )
            inner: Any = AvroSerializer(client, registered.schema, conf=conf)
            kind = "avro"
        else:
            _check_fields(shape, _json_field_list(document), name)
            convert = json_converter(document)
            inner = JSONSerializer(registered.schema, client, conf=conf, json_encode=_json_dumps)
            kind = "json-schema"
        return _Registered(
            f"{kind}:{name}@{registered.schema_id}",
            inner,
            convert,
            self._known,
            MessageField.VALUE,
            SerializationContext,
        )

    def _derived(self, shape: StreamShape, client: Any, value_format: str) -> ValueSerializer:
        """Register the schema derived from *shape*, once, and write each value behind its id."""
        from confluent_kafka.schema_registry import Schema

        subject = f"{shape.topic}-value"
        if value_format == "json":
            document = json_schema_for(shape)
            schema_id = client.register_schema(
                subject, Schema(json.dumps(document, sort_keys=True), "JSON")
            )
            return _FramedJson(
                f"json-schema:{subject}@{schema_id}", schema_id, json_converter(document)
            )
        from fastavro import parse_schema

        document = avro_schema_for(shape)
        schema_id = client.register_schema(
            subject, Schema(json.dumps(document, sort_keys=True), "AVRO")
        )
        return _FramedAvro(
            f"avro:{subject}@{schema_id}",
            schema_id,
            parse_schema(document),
            avro_converter(document),
            self._known,
        )
