# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How each stream of a bundle is written to Kafka (ADR 0038 sections 4 to 7).

:func:`stream_shapes` reads the bundle the way ``fluxsim`` does (the contract's keys and Kafka
binding, the Simulation's ``history``, ``snapshots``, ``events`` and ``periods`` entries, through
the seam's own readers) and answers, for every expose the run emits: its role, its topic, its
key, the column its record time comes from, whether it is written as Debezium's envelope, and
the registry subject its contract names. A stream whose declarations leave one of these open is
refused with ``FLX-3513``, naming the expose and the ``--sink-opt`` that would settle it, and
before anything is contacted.

Nothing here touches the network and nothing is cached: the answer is computed once per target
and held by the caller. A refusal names an expose, a column or an option, never a value.

Standard library and :mod:`flux_engine` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.errors import ErrorCode, FluxError
from flux_engine.seam.history import HistoryEntry, history_entries_of
from flux_engine.seam.keys import window_ends
from flux_engine.seam.projections import (
    EventsEntry,
    PeriodsEntry,
    SnapshotEntry,
    events_entry_of,
    periods_entry_of,
    snapshot_entry_of,
)
from flux_engine.seam.resolve import ResolvedEmit, load_bundle, resolve_all_emits
from flux_engine.sinks.errors import FLX_3504, FLX_3513, FLX_3515

__all__ = [
    "KEY_FORMATS",
    "SOURCE_FIELDS",
    "Envelope",
    "ShapeSettings",
    "StreamShape",
    "SubjectRef",
    "check_settings",
    "stream_shapes",
]

#: The two ways a key is written (ADR 0038 section 5).
KEY_FORMATS: Tuple[str, ...] = ("string", "json")

#: The fields Debezium's ``source`` block has, which an events column may not be named like.
SOURCE_FIELDS: Tuple[str, ...] = (
    "version",
    "connector",
    "name",
    "ts_ms",
    "snapshot",
    "db",
    "table",
)

_TOPIC = re.compile(r"[A-Za-z0-9._-]{1,249}", re.ASCII)
_PREFIX = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)
_INSTANTS = ("DATE", "TIMESTAMP")
_VALUE_FORMATS: Mapping[str, str] = {
    "avro": "AVRO",
    "avro-confluent": "AVRO",
    "json": "JSON",
    "json-confluent": "JSON",
    "protobuf": "PROTOBUF",
}


@dataclass(frozen=True)
class ShapeSettings:
    """The options that decide a stream's shape: ``--sink-opt`` ``topic-prefix``, ``topic.EXPOSE``,
    ``key.EXPOSE``, ``timestamp.EXPOSE`` and ``key-format``."""

    topic_prefix: str = "sim."
    topics: Mapping[str, str] = field(default_factory=dict)
    keys: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    timestamps: Mapping[str, str] = field(default_factory=dict)
    key_format: str = "string"


@dataclass(frozen=True)
class SubjectRef:
    """A registry subject the contract names: ``binding.properties.registry`` of a Kafka binding.

    ``subject`` is ``registry.subject``, else ``<the contract's topic>-value``; ``version`` is
    ``"latest"`` or a number; ``value_format`` is ``AVRO``, ``JSON`` or ``PROTOBUF``, from
    ``binding.serialization.value`` (Avro when it names none).
    """

    kind: str
    subject: str
    version: Union[str, int]
    value_format: str


@dataclass(frozen=True)
class Envelope:
    """How an events or snapshot row becomes Debezium's envelope (ADR 0038 section 7).

    ``fields`` are the history's columns the before and after records carry, with their logical
    types. ``before`` and ``after`` map a field to the event column the entry's ``before`` or
    ``after`` fills it from, and ``copied`` maps a field to the event column named like it.
    ``extra`` is every other event column, which becomes a field of ``source``. ``table`` is the
    history's emit.
    """

    op_column: Optional[str]
    date_column: Optional[str]
    at_column: Optional[str]
    fields: Tuple[Tuple[str, str], ...]
    before: Mapping[str, str]
    after: Mapping[str, str]
    copied: Mapping[str, str]
    key_fields: Tuple[str, ...]
    extra: Tuple[Tuple[str, str], ...]
    table: str


@dataclass(frozen=True)
class StreamShape:
    """Everything the sink needs to know about one stream before its first row."""

    expose_id: str
    role: str
    topic: str
    columns: Tuple[str, ...]
    types: Tuple[str, ...]
    key_columns: Tuple[str, ...]
    key_format: str
    timestamp_column: Optional[str]
    timestamp_default: Optional[str]
    envelope: Optional[Envelope]
    compacted: bool
    subject: Optional[SubjectRef]
    product_ref: str
    simulation_id: str
    contract_digest: str


def _refuse(expose_id: str, what: str, option: Optional[str] = None) -> FluxError:
    hint = f"; pass --sink-opt {option}" if option else ""
    return FluxError(FLX_3513, f"expose {expose_id!r}: {what}{hint}")


def stream_shapes(
    bundle_directory: Path, simulation_id: str, settings: ShapeSettings
) -> Mapping[str, StreamShape]:
    """One shape per emitted expose of the Simulation, by expose id.

    ``FLX-3504`` for a setting that names an expose the bundle does not emit, a column it does
    not have or a prefix or key format that cannot be used; ``FLX-3513`` for a stream whose
    declarations leave out how it is written, and for two streams on one topic.
    """
    check_settings(settings)
    loaded = load_bundle(Path(bundle_directory))
    everything = resolve_all_emits(loaded, simulation_id=simulation_id)
    by_id = {entry.expose_id: entry for entry in everything}
    emitted = [entry for entry in everything if not entry.is_hidden_table]
    _check_named(settings, {entry.expose_id for entry in emitted})
    shapes = {
        entry.expose_id: _shape_of(entry, by_id, settings, simulation_id) for entry in emitted
    }
    owner: Dict[str, str] = {}
    for expose_id, shape in shapes.items():
        if shape.topic in owner:
            raise FluxError(
                FLX_3513,
                f"exposes {owner[shape.topic]!r} and {expose_id!r} are both written to one "
                f"topic; pass --sink-opt topic.{expose_id}=NAME to give one of them another",
            )
        owner[shape.topic] = expose_id
    _refuse_contract_collisions(everything, shapes)
    return shapes


def _refuse_contract_collisions(
    everything: Sequence[ResolvedEmit], shapes: Mapping[str, StreamShape]
) -> None:
    """``FLX-3513`` for a topic, ``FLX-3515`` for a subject, that is also one a contract names.

    Any expose's binding counts, whether or not the run emits it: a synthetic stream must never
    be written to the topic of the contract's own product, or registered under its subject.
    """
    topics: Dict[str, str] = {}
    subjects: Dict[str, str] = {}
    for entry in everything:
        topic = _contract_topic(_binding_of(entry.expose))
        if topic is not None:
            topics.setdefault(topic, entry.expose_id)
        try:
            named = _subject_of(entry)
        except FluxError:
            continue  # a binding the sink cannot write is refused where its own stream is read
        if named is not None:
            subjects.setdefault(named.subject, entry.expose_id)
    for expose_id, shape in shapes.items():
        if shape.topic in topics:
            raise FluxError(
                FLX_3513,
                f"expose {expose_id!r}: its topic {shape.topic!r} is the topic the contract of "
                f"expose {topics[shape.topic]!r} names; pass --sink-opt topic-prefix=PREFIX or "
                f"topic.{expose_id}=NAME so a synthetic stream never lands on it",
            )
        derived = f"{shape.topic}-value"
        if shape.subject is None and derived in subjects:
            raise FluxError(
                FLX_3515,
                f"expose {expose_id!r}: its schema would be registered under {derived!r}, the "
                f"subject the contract of expose {subjects[derived]!r} names; pass --sink-opt "
                f"topic-prefix=PREFIX or topic.{expose_id}=NAME to give it another",
            )


def check_settings(settings: ShapeSettings) -> None:
    """``FLX-3504`` for a topic prefix or key format that cannot be used; nothing is read."""
    if not settings.topic_prefix:
        raise FluxError(
            FLX_3504,
            "the option topic-prefix may not be empty: a synthetic topic must never have the "
            "name of the topic the contract declares",
        )
    if _PREFIX.fullmatch(settings.topic_prefix) is None:
        raise FluxError(
            FLX_3504,
            "the option topic-prefix may hold only letters, digits, '.', '_' and '-'",
        )
    if settings.key_format not in KEY_FORMATS:
        raise FluxError(
            FLX_3504,
            f"the option key-format takes one of {', '.join(KEY_FORMATS)}, "
            f"not {settings.key_format!r}",
        )


def _check_named(settings: ShapeSettings, emitted: "set[str]") -> None:
    for option, named in (
        ("topic", settings.topics),
        ("key", settings.keys),
        ("timestamp", settings.timestamps),
    ):
        for expose_id in named:
            if expose_id not in emitted:
                raise FluxError(
                    FLX_3504,
                    f"the option {option}.{expose_id} names an expose the run does not emit "
                    f"(it emits {', '.join(sorted(emitted))})",
                )


@dataclass(frozen=True)
class _Origin:
    """The history a projection reads: its emit, entry, natural entity columns and start column."""

    emit: str
    entry: Optional[HistoryEntry]
    natural: Tuple[str, ...]
    start: Optional[str]
    columns: Tuple[str, ...]
    types: Tuple[str, ...]


def _natural_of(entry: ResolvedEmit) -> Tuple[str, ...]:
    keys = entry.keys
    if keys is None:
        return ()
    return tuple(item.column for item in keys.entities if item.kind == "natural")


def _origin_of(of: str, expose_id: str, by_id: Mapping[str, ResolvedEmit]) -> _Origin:
    held = by_id.get(of)
    if held is None:
        raise _refuse(
            expose_id, f"it projects the history of {of!r}, which the bundle does not emit"
        )
    compiled = held.compile()
    entries = history_entries_of(held.extension, of)
    return _Origin(
        emit=of,
        entry=entries[0] if entries else None,
        natural=_natural_of(held),
        start=window_ends(held.expose.get("semantics")).get("start"),
        columns=compiled.column_names,
        types=compiled.type_names,
    )


def _shape_of(
    entry: ResolvedEmit,
    by_id: Mapping[str, ResolvedEmit],
    settings: ShapeSettings,
    simulation_id: str,
) -> StreamShape:
    expose_id = entry.expose_id
    compiled = entry.compile()
    columns, types = compiled.column_names, compiled.type_names
    kinds = dict(zip(columns, types, strict=True))
    snapshot = snapshot_entry_of(entry.extension, expose_id)
    events = events_entry_of(entry.extension, expose_id)
    periods = periods_entry_of(entry.extension, expose_id)
    own_history = history_entries_of(entry.extension, expose_id)
    projection: Union[SnapshotEntry, EventsEntry, PeriodsEntry, None] = (
        snapshot or events or periods
    )
    origin = _origin_of(projection.of, expose_id, by_id) if projection is not None else None
    role = (
        "snapshot"
        if snapshot is not None
        else (
            "events"
            if events is not None
            else "periods" if periods is not None else "history" if own_history else "table"
        )
    )
    key_columns = _key_columns(entry, role, origin, columns, settings)
    if settings.key_format == "string" and len(key_columns) > 1:
        raise _refuse(
            expose_id,
            f"its key has {len(key_columns)} columns, which a string key cannot hold",
            "key-format=json",
        )
    timestamp_column, timestamp_default = _timestamp(
        entry, role, origin, snapshot, events, periods, kinds, settings
    )
    subject = _subject_of(entry)
    envelope: Optional[Envelope] = None
    if subject is None and events is not None and origin is not None:
        envelope = _events_envelope(expose_id, events, origin, columns, types)
    elif subject is None and snapshot is not None and origin is not None:
        envelope = _snapshot_envelope(columns, types, key_columns, origin)
    return StreamShape(
        expose_id=expose_id,
        role=role,
        topic=_topic_of(entry, settings),
        columns=columns,
        types=types,
        key_columns=key_columns,
        key_format=settings.key_format,
        timestamp_column=timestamp_column,
        timestamp_default=timestamp_default,
        envelope=envelope,
        compacted=role == "snapshot",
        subject=subject,
        product_ref=entry.product_ref,
        simulation_id=simulation_id,
        contract_digest=entry.contract_digest.value,
    )


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------


def _binding_of(expose: Mapping[str, Any]) -> Mapping[str, Any]:
    binding = expose.get("binding")
    return binding if isinstance(binding, Mapping) else {}


def _is_kafka(binding: Mapping[str, Any]) -> bool:
    platform = binding.get("platform")
    return platform == "kafka" or (
        platform == "confluent" and binding.get("format") == "kafka_topic"
    )


def _contract_topic(binding: Mapping[str, Any]) -> Optional[str]:
    if not _is_kafka(binding):
        return None
    location = binding.get("location")
    topic = location.get("topic") if isinstance(location, Mapping) else None
    return topic if isinstance(topic, str) and topic else None


def _topic_of(entry: ResolvedEmit, settings: ShapeSettings) -> str:
    expose_id = entry.expose_id
    name = settings.topics.get(expose_id) or _contract_topic(_binding_of(entry.expose)) or expose_id
    topic = settings.topic_prefix + name
    if _TOPIC.fullmatch(topic) is None or topic in (".", ".."):
        raise _refuse(
            expose_id,
            "its topic name is not one Kafka allows (1 to 249 letters, digits, '.', '_' or '-', "
            "and not '.' or '..')",
            f"topic.{expose_id}=NAME",
        )
    return topic


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _key_columns(
    entry: ResolvedEmit,
    role: str,
    origin: Optional[_Origin],
    columns: Sequence[str],
    settings: ShapeSettings,
) -> Tuple[str, ...]:
    expose_id = entry.expose_id
    named = settings.keys.get(expose_id)
    if named is not None:
        if not named:
            raise FluxError(FLX_3504, f"the option key.{expose_id} names no column")
        for column in named:
            if column not in columns:
                raise FluxError(
                    FLX_3504, f"the option key.{expose_id} names {column!r}, which is not a column"
                )
        return tuple(named)
    natural: Tuple[str, ...] = ()
    if role == "history":
        natural = _natural_of(entry)
    elif origin is not None:
        natural = origin.natural
    if natural:
        missing = [column for column in natural if column not in columns]
        if missing:
            raise _refuse(
                expose_id,
                f"the entity of its history is {', '.join(natural)}, and the expose has no "
                f"column {missing[0]!r}",
                f"key.{expose_id}=COLUMN",
            )
        return natural
    primary = (
        [] if entry.keys is None else [k.column for k in entry.keys.entities if k.kind == "primary"]
    )
    if primary and primary[0] in columns:
        return (primary[0],)
    raise _refuse(
        expose_id,
        "the contract names no entity to key its records by",
        f"key.{expose_id}=COLUMN[,COLUMN...]",
    )


# ---------------------------------------------------------------------------
# Event time
# ---------------------------------------------------------------------------


def _instant_column(
    expose_id: str, column: str, kinds: Mapping[str, str], option: str, *, refusal: ErrorCode
) -> str:
    kind = kinds.get(column)
    if kind is None:
        raise FluxError(
            refusal,
            f"expose {expose_id!r} has no column {column!r} for its event time; pass --sink-opt {option}",
        )
    if kind.upper() not in _INSTANTS:
        raise FluxError(
            refusal,
            f"expose {expose_id!r}: column {column!r} is a {kind}, and an event time must be a "
            f"DATE or a TIMESTAMP; pass --sink-opt {option}",
        )
    return column


def _timestamp(
    entry: ResolvedEmit,
    role: str,
    origin: Optional[_Origin],
    snapshot: Optional[SnapshotEntry],
    events: Optional[EventsEntry],
    periods: Optional[PeriodsEntry],
    kinds: Mapping[str, str],
    settings: ShapeSettings,
) -> Tuple[Optional[str], Optional[str]]:
    expose_id = entry.expose_id
    option = f"timestamp.{expose_id}=COLUMN"
    named = settings.timestamps.get(expose_id)
    if named is not None:
        return _instant_column(expose_id, named, kinds, option, refusal=FLX_3504), None
    if role == "history":
        start = window_ends(entry.expose.get("semantics")).get("start")
        if start is None:
            raise _refuse(expose_id, "it declares no validity start column", option)
        return _instant_column(expose_id, start, kinds, option, refusal=FLX_3513), None
    if events is not None:
        return _instant_column(expose_id, events.date, kinds, option, refusal=FLX_3513), None
    if periods is not None:
        return _instant_column(expose_id, periods.column, kinds, option, refusal=FLX_3513), None
    if snapshot is not None and origin is not None:
        if origin.start is not None and origin.start in kinds:
            return _instant_column(expose_id, origin.start, kinds, option, refusal=FLX_3513), None
        if origin.entry is None:
            raise _refuse(expose_id, f"the history of {origin.emit!r} has no entry", option)
        return None, origin.entry.until
    return _table_timestamp(entry, kinds, option), None


def _table_timestamp(entry: ResolvedEmit, kinds: Mapping[str, str], option: str) -> Optional[str]:
    expose_id = entry.expose_id
    semantics = entry.expose.get("semantics")
    semantics = semantics if isinstance(semantics, Mapping) else {}
    default = semantics.get("defaultAggTimeDimension")
    if isinstance(default, str) and default:
        return _instant_column(expose_id, default, kinds, option, refusal=FLX_3513)
    dimensions = semantics.get("dimensions")
    found: List[str] = []
    if isinstance(dimensions, Sequence) and not isinstance(dimensions, (str, bytes)):
        for dimension in dimensions:
            if not isinstance(dimension, Mapping) or dimension.get("type") != "time":
                continue
            name = dimension.get("name")
            if isinstance(name, str) and kinds.get(name, "").upper() in _INSTANTS:
                found.append(name)
    if len(found) == 1:
        return found[0]
    raise _refuse(
        expose_id,
        "it has no event time: no defaultAggTimeDimension and not exactly one time dimension "
        "that is a DATE or TIMESTAMP column",
        option,
    )


# ---------------------------------------------------------------------------
# Debezium's envelope
# ---------------------------------------------------------------------------


def _events_envelope(
    expose_id: str,
    events: EventsEntry,
    origin: _Origin,
    columns: Sequence[str],
    types: Sequence[str],
) -> Envelope:
    if events.op is None:
        raise _refuse(
            expose_id,
            "its events entry has no op column, so nothing says which change a row is",
        )
    before = {field_: column for column, field_ in events.before}
    after = {field_: column for column, field_ in events.after}
    mapped = {column for column, _ in events.before} | {column for column, _ in events.after}
    reserved = {events.op, events.date}
    if events.at is not None:
        reserved.add(events.at)
    kinds = dict(zip(columns, types, strict=True))
    history_columns = set(origin.columns)
    copied = {
        column: column
        for column in columns
        if column in history_columns and column not in mapped and column not in reserved
    }
    taken = set(copied) | mapped | reserved
    fields: List[Tuple[str, str]] = []
    for name in origin.columns:
        source = after.get(name) or before.get(name) or copied.get(name)
        if source is not None:
            fields.append((name, kinds[source]))
    extra = tuple((column, kinds[column]) for column in columns if column not in taken)
    for column, _ in extra:
        if column in SOURCE_FIELDS:
            raise _refuse(
                expose_id,
                f"column {column!r} is named like a field of the envelope's source, which "
                "every other events column becomes; rename it in the contract",
            )
    names = {name for name, _ in fields}
    return Envelope(
        op_column=events.op,
        date_column=events.date,
        at_column=events.at,
        fields=tuple(fields),
        before=before,
        after=after,
        copied=copied,
        key_fields=tuple(column for column in origin.natural if column in names),
        extra=extra,
        table=origin.emit,
    )


def _snapshot_envelope(
    columns: Sequence[str],
    types: Sequence[str],
    key_columns: Sequence[str],
    origin: _Origin,
) -> Envelope:
    return Envelope(
        op_column=None,
        date_column=None,
        at_column=None,
        fields=tuple(zip(columns, types, strict=True)),
        before={},
        after={},
        copied={column: column for column in columns},
        key_fields=tuple(key_columns),
        extra=(),
        table=origin.emit,
    )


# ---------------------------------------------------------------------------
# The subject a contract names
# ---------------------------------------------------------------------------


def _subject_of(entry: ResolvedEmit) -> Optional[SubjectRef]:
    binding = _binding_of(entry.expose)
    if not _is_kafka(binding):
        return None
    properties = binding.get("properties")
    registry = properties.get("registry") if isinstance(properties, Mapping) else None
    if isinstance(registry, str):
        registry = {"subject": registry}
    if not isinstance(registry, Mapping):
        return None
    expose_id = entry.expose_id
    subject = registry.get("subject")
    if not isinstance(subject, str) or not subject:
        subject = f"{_contract_topic(binding) or expose_id}-value"
    kind = str(registry.get("kind", "confluent")).lower()
    version: Union[str, int] = registry.get("version", "latest")
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        raise FluxError(
            FLX_3515, f"expose {expose_id!r}: its registry version is not 'latest' or a number"
        )
    if isinstance(version, str) and version.isascii() and version.isdigit():
        version = int(version)
    elif isinstance(version, str) and version != "latest":
        raise FluxError(
            FLX_3515, f"expose {expose_id!r}: its registry version is not 'latest' or a number"
        )
    serialization = binding.get("serialization")
    named = serialization.get("value") if isinstance(serialization, Mapping) else None
    if named is None:
        value_format = "AVRO"
    else:
        value_format = _VALUE_FORMATS.get(str(named).lower(), "")
        if not value_format:
            raise FluxError(
                FLX_3515,
                f"expose {expose_id!r}: its binding serialization.value is not avro, json or protobuf",
            )
    return SubjectRef(kind=kind, subject=subject, version=version, value_format=value_format)
