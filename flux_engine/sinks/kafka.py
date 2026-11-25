# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink: every stream of a run, as keyed records on topics, exactly once.

``fluxsim run BUNDLE --sink kafka:HOST:PORT[,HOST:PORT...] --sink-opt audience=internal`` delivers
each stream of a run to the cluster those bootstrap servers name (ADR 0038). The sink is the
optional ``[kafka]`` extra, on confluent-kafka; nothing here imports it at module scope.

**Who may read it.** Every target says ``audience=internal`` or ``audience=external``. The run's
own plan records a privacy grade, and the engine hands it to the sink with each stream: an internal
audience needs ``P1`` or better, an external one ``P3`` or better, and a run whose plan records no
grade (it pins no profile) is refused for both. The check comes first, before the stream's
declarations are read and before anything is contacted.

**Where it goes.** A stream's topic is ``topic-prefix`` (``sim.``, never empty) plus the contract's
topic for the expose, else the expose id; ``topic.EXPOSE=NAME`` names another. A missing topic is
created (``partitions``, ``replication-factor``; a snapshot's topic is compacted); an existing one
must keep event time (not ``LogAppendTime``) and, for a snapshot, be compacted. Only the
bootstrap servers and the ``registry`` named here are contacted: a topic, host or registry the
contract writes is never used.

**Keys.** A history's records and those of its projections are keyed by the history's natural
entity, any other stream by its primary entity; ``key.EXPOSE=COLUMN[,COLUMN]`` names the columns
and ``key-format=json`` writes them as a compact JSON object, in the order named. The partitioner is
``murmur2_random``, Java's, so a record lands where a Java producer would put it.

**Time.** The record timestamp is the event's time in milliseconds (``timestamp.EXPOSE=COLUMN``
chooses it where the declarations do not). Each timestamp is checked against zero and the topic's
``message.timestamp.before.max.ms`` and ``after.max.ms`` before it is sent; the sink never moves
a timestamp to fit.

**Values.** A history's events and a snapshot are written in Debezium's envelope, each delete
followed by a tombstone; every other stream is written as its rows. With ``registry`` the values
go through the registry's Avro (or, with ``value-format=json``, JSON Schema) serializers: against
the contract's subject when it names one, otherwise a schema derived from the columns and
registered under the topic's ``-value`` subject. Without it a value is compact JSON in the
stream's column order.

**Exactly once.** One idempotent transactional producer per stream and run, under a stable
``transactional.id``. An unpaced stream is one transaction, the whole stream, unless
``commit-every`` is given; then it also commits every that many seconds. A transaction may stay
open for ``transaction-timeout`` seconds (900 by default, as the brokers allow), and an unpaced
stream that cannot finish inside it is refused rather than left to the brokers' timeout. Each
transaction also commits the stream's progress as an offset of a consumer group of the same name,
so a retried delivery resumes after its last committed record and a second delivery of a finished
stream sends nothing. The producer is opened (which fences an earlier one) before the progress is
read. A read committed consumer sees each record once.

**Pace.** ``pace=realtime`` or ``pace=xN`` sends each record when the wall clock reaches its event
time (divided by N), in event-time order within each key's order, and commits at least every
``commit-every`` seconds (1 by default). Keys, values, timestamps and headers are the same at every pace.

**Credentials.** Never an option: ``sasl-username-env``, ``sasl-password-env`` and
``registry-auth-env`` name environment variables, read when a stream starts. A credential goes
only over TLS (``SASL_SSL``, an ``https`` registry), except a SASL credential to brokers that are
all on this machine; the registry's credential never goes in clear, since its client follows the
environment's proxies. Errors are scrubbed of every credential, and librdkafka logs to the
``flux_engine.sinks.kafka`` logger.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from flux_engine.api.emit import EmitHeader
from flux_engine.core.canonical import canonical_bytes, sha256_hex
from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern
from flux_engine.seam.emit import FLX_3301
from flux_engine.sinks._kafka_broker import (
    COMPRESSIONS,
    DEFAULT_COMPRESSION,
    LOGGER,
    Broker,
    ClientSettings,
    ConfluentBroker,
    Credentials,
    TopicInfo,
    TxnProducer,
    is_loopback,
)
from flux_engine.sinks._kafka_gate import parse_audience, require_grade
from flux_engine.sinks._kafka_pace import Pace, Pacer, parse_pace, scheduled_times, time_order
from flux_engine.sinks._kafka_records import KafkaRecord, RecordBuilder
from flux_engine.sinks._kafka_serdes import (
    ConfluentSerdes,
    RegistryAccess,
    Serdes,
    ValueSerializer,
    plain_json,
    require_supported_subject,
)
from flux_engine.sinks._kafka_shape import (
    KEY_FORMATS,
    ShapeSettings,
    StreamShape,
    check_settings,
    stream_shapes,
)
from flux_engine.sinks._optional import require
from flux_engine.sinks._options import int_option, require_known, text_option
from flux_engine.sinks._secrets import Known
from flux_engine.sinks.errors import FLX_3504, FLX_3514, FLX_3515
from flux_engine.sinks.receipts import SinkReceipt, StreamContext

__all__ = [
    "OPTIONS",
    "Clock",
    "KafkaSettings",
    "KafkaSink",
    "KafkaTarget",
    "read_credentials",
    "target",
]

#: The options with a fixed name; ``topic.EXPOSE``, ``key.EXPOSE`` and ``timestamp.EXPOSE`` are
#: the three families (ADR 0038 section 12).
OPTIONS: Tuple[str, ...] = (
    "audience",
    "commit-every",
    "compression",
    "key-format",
    "pace",
    "partitions",
    "registry",
    "registry-auth-env",
    "registry-ca-location",
    "replication-factor",
    "sasl-mechanism",
    "sasl-password-env",
    "sasl-username-env",
    "security-protocol",
    "ssl-ca-location",
    "timeout",
    "topic-prefix",
    "transaction-timeout",
    "value-format",
)
FAMILIES: Tuple[str, ...] = ("topic.", "key.", "timestamp.")

#: How many unpaced records are sent for each read of the wall clock for the timestamp limits.
_CLOCK_EVERY = 1024
#: How many unpaced records are sent between two checks of the age of the open transaction.
_AGE_EVERY = 1024
#: The share of ``transaction-timeout`` an unpaced stream's single transaction may use up.
_TRANSACTION_SHARE = 0.9

#: The longest ``transaction-timeout`` the producer takes: an int32 of milliseconds.
MAX_TRANSACTION_TIMEOUT = 2147483
SCRAM_MECHANISMS: Tuple[str, ...] = ("SCRAM-SHA-256", "SCRAM-SHA-512")
SECURITY_PROTOCOLS: Tuple[str, ...] = ("PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL")
SASL_MECHANISMS: Tuple[str, ...] = ("PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512")
VALUE_FORMATS: Tuple[str, ...] = ("avro", "json")
PROGRESS_VERSION = 1

_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", re.ASCII)
_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?", re.ASCII)


# -- the clock ---------------------------------------------------------------------


@dataclass(frozen=True)
class Clock:
    """The time the sink reads and waits on; a test replaces all three."""

    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000


# -- reading the options -----------------------------------------------------------


def _bootstrap_hosts(target: str) -> Tuple[str, ...]:
    """The hosts of ``HOST:PORT[,HOST:PORT...]``; ``FLX-3504`` for anything else."""
    shape = "the kafka target is HOST:PORT[,HOST:PORT...]: a DNS name, an IPv4 address or [IPv6], and a port"
    if not target:
        raise FluxError(FLX_3504, f"the kafka sink needs a target; {shape}")
    hosts: List[str] = []
    for item in target.split(","):
        if item.startswith("["):
            host, bracket, rest = item[1:].partition("]")
            port = rest[1:] if rest.startswith(":") else ""
            if not bracket:
                raise FluxError(FLX_3504, shape)
            try:
                if not isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
                    raise ValueError
            except ValueError:
                raise FluxError(FLX_3504, shape) from None
            name = f"[{host}]"
        else:
            name, colon, port = item.rpartition(":")
            if not colon or not name or not _hostname(name):
                raise FluxError(FLX_3504, shape)
        if not (port.isascii() and port.isdigit() and 1 <= int(port) <= 65535):
            raise FluxError(FLX_3504, shape)
        hosts.append(name)
    return tuple(hosts)


def _hostname(name: str) -> bool:
    try:
        ipaddress.IPv4Address(name)
        return True
    except ValueError:
        pass
    return len(name) <= 253 and all(_LABEL.fullmatch(label) for label in name.split("."))


def _seconds(options: Mapping[str, str], key: str, default: float) -> float:
    raw = options.get(key)
    if raw is None:
        return default
    text = raw.strip()
    if _SECONDS.fullmatch(text) is None or Fraction(text) <= 0:
        raise FluxError(FLX_3504, f"{key} must be a positive number of seconds")
    return float(Fraction(text))


def _family(options: Mapping[str, str], prefix: str) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for key, value in options.items():
        if key.startswith(prefix):
            expose_id = key[len(prefix) :]
            if not expose_id:
                raise FluxError(FLX_3504, f"the option {prefix}EXPOSE needs an expose id")
            found[expose_id] = value
    return found


def _registry_urls(value: str) -> List[str]:
    """The URLs of ``registry``: one, or several separated by commas (the client fails over)."""
    return [_registry_url(part) for part in value.split(",")]


def _registry_url(value: str) -> str:
    problem = FluxError(
        FLX_3504,
        "the option registry is an http or https URL (or several, separated by commas), each "
        "with no user information, query or fragment: the registry's credentials come from "
        "registry-auth-env",
    )
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise problem from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
        or any(char.isspace() for char in value)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise problem
    return value.rstrip("/")


@dataclass(frozen=True)
class KafkaSettings:
    """The Kafka sink's checked options: everything a delivery needs that needs no network."""

    target: str
    hosts: Tuple[str, ...]
    audience: str
    shape: ShapeSettings = field(default_factory=ShapeSettings)
    registry: Optional[str] = None
    registry_auth_env: Optional[str] = None
    registry_ca_location: Optional[str] = None
    value_format: str = "avro"
    pace: Pace = field(default_factory=lambda: parse_pace(None))
    commit_every: Optional[float] = None
    transaction_timeout: float = 900.0
    compression: str = DEFAULT_COMPRESSION
    partitions: Optional[int] = None
    replication_factor: Optional[int] = None
    timeout: float = 30.0
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: Optional[str] = None
    sasl_username_env: Optional[str] = None
    sasl_password_env: Optional[str] = None
    ssl_ca_location: Optional[str] = None

    @property
    def uses_sasl(self) -> bool:
        return self.sasl_username_env is not None

    @property
    def commit_interval(self) -> float:
        """The seconds between commits of a paced stream: ``commit-every``, else one."""
        return self.commit_every if self.commit_every is not None else 1.0

    def client(self) -> ClientSettings:
        """The settings the broker adapter connects with."""
        return ClientSettings(
            bootstrap=self.target,
            timeout=self.timeout,
            security_protocol=self.security_protocol,
            sasl_mechanism=self.sasl_mechanism,
            ssl_ca_location=self.ssl_ca_location,
            transaction_timeout=self.transaction_timeout,
            loopback_only=self.uses_sasl and self.security_protocol == "SASL_PLAINTEXT",
        )

    @classmethod
    def parse(cls, target: str, options: Mapping[str, str]) -> "KafkaSettings":
        """The settings *options* and *target* name; ``FLX-3504`` for any that cannot be used."""
        hosts = _bootstrap_hosts(target)
        fixed = {k: v for k, v in options.items() if not k.startswith(FAMILIES)}
        require_known("kafka", fixed, (*OPTIONS, *(f"{p}EXPOSE" for p in FAMILIES)))
        audience = parse_audience(text_option(fixed, "audience"))
        prefix = text_option(fixed, "topic-prefix", "sim.")
        key_format = text_option(fixed, "key-format", "string") or "string"
        if key_format not in KEY_FORMATS:
            raise FluxError(
                FLX_3504,
                f"the option key-format takes one of {', '.join(KEY_FORMATS)}, not {key_format!r}",
            )
        keys = {
            expose_id: tuple(column.strip() for column in value.split(","))
            for expose_id, value in _family(options, "key.").items()
        }
        for expose_id, columns in keys.items():
            if not all(columns):
                raise FluxError(FLX_3504, f"the option key.{expose_id} names no column")
        shape = ShapeSettings(
            topic_prefix=prefix if prefix is not None else "sim.",
            topics=_family(options, "topic."),
            keys=keys,
            timestamps=_family(options, "timestamp."),
            key_format=key_format,
        )
        check_settings(shape)
        registry = text_option(fixed, "registry")
        registry_urls: List[str] = []
        if registry is not None:
            registry_urls = _registry_urls(registry)
            registry = ",".join(registry_urls)
        auth_env = text_option(fixed, "registry-auth-env")
        ca = text_option(fixed, "registry-ca-location")
        value_format = text_option(fixed, "value-format")
        if registry is None:
            for name, value in (
                ("registry-auth-env", auth_env),
                ("registry-ca-location", ca),
                ("value-format", value_format),
            ):
                if value is not None:
                    raise FluxError(FLX_3504, f"the option {name} needs --sink-opt registry=URL")
        else:
            secure = all(url.startswith("https://") for url in registry_urls)
            if auth_env is not None and not secure:
                raise FluxError(
                    FLX_3504,
                    "registry-auth-env needs every registry URL to be https: the credential goes "
                    "with every request, and through any proxy the environment names "
                    "(HTTP_PROXY, ALL_PROXY), even for a registry on this machine",
                )
            if ca is not None and not secure:
                raise FluxError(FLX_3504, "registry-ca-location needs an https registry")
        if value_format is not None and value_format not in VALUE_FORMATS:
            raise FluxError(
                FLX_3504,
                f"the option value-format takes one of {', '.join(VALUE_FORMATS)}, "
                f"not {value_format!r}",
            )
        compression = text_option(fixed, "compression", DEFAULT_COMPRESSION) or ""
        if compression not in COMPRESSIONS:
            raise FluxError(
                FLX_3504,
                f"the option compression takes one of {', '.join(COMPRESSIONS)}, "
                f"not {compression!r}",
            )
        user_env = text_option(fixed, "sasl-username-env")
        password_env = text_option(fixed, "sasl-password-env")
        if (user_env is None) != (password_env is None):
            raise FluxError(
                FLX_3504, "sasl-username-env and sasl-password-env are given together or not at all"
            )
        protocol = text_option(fixed, "security-protocol")
        mechanism = text_option(fixed, "sasl-mechanism")
        ssl_ca = text_option(fixed, "ssl-ca-location")
        if protocol is not None and protocol not in SECURITY_PROTOCOLS:
            raise FluxError(
                FLX_3504,
                f"the option security-protocol takes one of {', '.join(SECURITY_PROTOCOLS)}",
            )
        sasl = user_env is not None
        if protocol is None:
            protocol = "SASL_SSL" if sasl else "PLAINTEXT"
        if sasl and not protocol.startswith("SASL_"):
            raise FluxError(
                FLX_3504,
                "sasl credentials need security-protocol SASL_SSL (or SASL_PLAINTEXT on this machine)",
            )
        if not sasl and protocol.startswith("SASL_"):
            raise FluxError(
                FLX_3504,
                f"security-protocol {protocol} needs sasl-username-env and sasl-password-env",
            )
        if sasl and protocol != "SASL_SSL" and not all(is_loopback(host) for host in hosts):
            raise FluxError(
                FLX_3504,
                "a credential goes only over TLS: use security-protocol SASL_SSL, or point the "
                "target at this machine",
            )
        if sasl and protocol == "SASL_PLAINTEXT" and (mechanism or "PLAIN") not in SCRAM_MECHANISMS:
            raise FluxError(
                FLX_3504,
                "PLAIN sends the password itself, so over SASL_PLAINTEXT use "
                "sasl-mechanism=SCRAM-SHA-256 or SCRAM-SHA-512, or security-protocol=SASL_SSL",
            )
        if mechanism is not None and not sasl:
            raise FluxError(FLX_3504, "the option sasl-mechanism needs the sasl-*-env options")
        if mechanism is not None and mechanism not in SASL_MECHANISMS:
            raise FluxError(
                FLX_3504,
                f"the option sasl-mechanism takes one of {', '.join(SASL_MECHANISMS)}",
            )
        if ssl_ca is not None and protocol not in ("SSL", "SASL_SSL"):
            raise FluxError(FLX_3504, "the option ssl-ca-location needs a TLS security-protocol")
        pace = parse_pace(text_option(fixed, "pace"))
        commit_every = _seconds(fixed, "commit-every", 1.0) if "commit-every" in fixed else None
        transaction_timeout = _seconds(fixed, "transaction-timeout", 900.0)
        if transaction_timeout > MAX_TRANSACTION_TIMEOUT:
            raise FluxError(
                FLX_3504,
                f"the option transaction-timeout is at most {MAX_TRANSACTION_TIMEOUT} seconds "
                "(2147483647 ms, the producer's limit)",
            )
        interval = commit_every if commit_every is not None else (1.0 if pace.paced else None)
        if interval is not None and interval > transaction_timeout / 3:
            raise FluxError(
                FLX_3504,
                f"commit-every={interval:g} s is more than a third of "
                f"transaction-timeout={transaction_timeout:g} s; lower commit-every or raise "
                "transaction-timeout",
            )
        return cls(
            target=target,
            hosts=hosts,
            audience=audience,
            shape=shape,
            registry=registry,
            registry_auth_env=auth_env,
            registry_ca_location=ca,
            value_format=value_format or "avro",
            pace=pace,
            commit_every=commit_every,
            transaction_timeout=transaction_timeout,
            compression=compression,
            partitions=int_option(fixed, "partitions", None),
            replication_factor=int_option(fixed, "replication-factor", None),
            timeout=_seconds(fixed, "timeout", 30.0),
            security_protocol=protocol,
            sasl_mechanism=(mechanism or "PLAIN") if sasl else None,
            sasl_username_env=user_env,
            sasl_password_env=password_env,
            ssl_ca_location=ssl_ca,
        )


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise FluxError(FLX_3504, f"the environment variable {name} is not set")
    return value


def read_credentials(settings: KafkaSettings) -> Credentials:
    """The credentials the ``-env`` options name, read now; ``FLX-3504`` naming a missing variable."""
    user = password = auth = None
    if settings.sasl_username_env is not None and settings.sasl_password_env is not None:
        user = _environment(settings.sasl_username_env)
        password = _environment(settings.sasl_password_env)
    if settings.registry_auth_env is not None:
        auth = _environment(settings.registry_auth_env)
        if ":" not in auth:
            raise FluxError(
                FLX_3504,
                f"the environment variable {settings.registry_auth_env} holds USER:PASSWORD, "
                "and this one has no colon",
            )
    return Credentials(sasl_username=user, sasl_password=password, registry_auth=auth)


def known_strings_of(settings: KafkaSettings) -> List[Known]:
    """What a library may echo of this target: the bootstrap servers, the registry, the credentials."""
    known: List[Known] = [settings.target]
    if settings.registry:
        known.append(settings.registry)
    for name in (
        settings.sasl_username_env,
        settings.sasl_password_env,
        settings.registry_auth_env,
    ):
        value = os.environ.get(name) if name else None
        if value:
            known.append((value, f"${name}"))
            for part in value.split(":", 1)[1:]:
                if part:
                    known.append((part, f"${name}"))
    return known


# -- the target --------------------------------------------------------------------


class KafkaTarget:
    """The Kafka sink's checked settings, handing out one sink per stream.

    *broker* and *serdes* are the adapters (:class:`~flux_engine.sinks._kafka_broker.ConfluentBroker`
    and :class:`~flux_engine.sinks._kafka_serdes.ConfluentSerdes` for a real run); a test passes
    fakes. Streams are shaped once per target and bundle.
    """

    name: ClassVar[str] = "kafka"

    def __init__(
        self,
        settings: KafkaSettings,
        *,
        broker: Broker,
        serdes: Serdes,
        clock: Optional[Clock] = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.serdes = serdes
        self.clock = clock or Clock()
        self._shapes: Dict[Tuple[str, str], Mapping[str, StreamShape]] = {}

    def __repr__(self) -> str:
        return f"KafkaTarget(audience={self.settings.audience!r})"

    @property
    def _flux_known_strings(self) -> Tuple[Known, ...]:
        """What the registry scrubs a delivery's errors of: the credentials, not the plain options."""
        return tuple(known_strings_of(self.settings))

    def sink_for(self, context: StreamContext) -> "KafkaSink":
        """A fresh sink for one stream; the gate and the stream's declarations are checked first."""
        settings = self.settings
        grade = require_grade(settings.audience, context.privacy_grade, expose_id=context.expose_id)
        if context.bundle_directory is None or context.simulation_id is None:
            raise FluxError(
                FLX_3504,
                "the kafka sink needs the engine to hand it the run's bundle and the "
                f"Simulation's id; expose {context.expose_id!r} came with neither",
            )
        key = (str(context.bundle_directory), context.simulation_id)
        if key not in self._shapes:
            self._shapes[key] = stream_shapes(
                context.bundle_directory, context.simulation_id, settings.shape
            )
        shape = self._shapes[key].get(context.expose_id)
        if shape is None:
            raise FluxError(
                FLX_3504,
                f"expose {context.expose_id!r} is not a stream of Simulation "
                f"{context.simulation_id!r}",
            )
        require_supported_subject(shape)
        if shape.subject is not None and settings.registry is None:
            raise FluxError(
                FLX_3515,
                f"expose {context.expose_id!r}: its contract names the registry subject "
                f"{shape.subject.subject!r}, so its values are written with registry framing; "
                "pass --sink-opt registry=URL",
            )
        credentials = read_credentials(settings)
        return KafkaSink(self, context, shape, grade, credentials)


# -- the sink ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Progress:
    records: int
    order: str
    complete: bool
    settings: Optional[str]


def _progress_text(records: int, order: str, complete: bool, digest: str, settings: str) -> str:
    document = {
        "v": PROGRESS_VERSION,
        "records": records,
        "order": order,
        "complete": complete,
        "digest": digest,
        "settings": settings,
    }
    return canonical_bytes(document).decode("utf-8")


def _read_progress(text: Optional[str], digest: str) -> Optional[_Progress]:
    """The progress a stream's group committed, when it is this stream's (the same digest)."""
    if not text:
        return None
    try:
        document = json.loads(text)
    except ValueError:
        return None
    if not isinstance(document, dict) or document.get("digest") != digest:
        return None
    records = document.get("records")
    order = document.get("order")
    if (
        document.get("v") != PROGRESS_VERSION
        or isinstance(records, bool)
        or not isinstance(records, int)
        or records < 0
        or order not in ("stream", "time")
    ):
        return None
    settings = document.get("settings")
    return _Progress(
        records,
        str(order),
        bool(document.get("complete")),
        settings if isinstance(settings, str) else None,
    )


class KafkaSink:
    """Writes one stream to one topic, exactly once.

    The three calls arrive as :class:`~flux_engine.api.emit.EmitSink` says: :meth:`open`, then
    :meth:`write` for each row, then :meth:`close`, which also runs when the run failed. A stream
    is delivered only when every row arrived and every record was acknowledged; otherwise its
    transaction is aborted and no receipt is made.
    """

    name: ClassVar[str] = "kafka"

    def __init__(
        self,
        owner: KafkaTarget,
        context: StreamContext,
        shape: StreamShape,
        grade: Pattern,
        credentials: Credentials,
    ) -> None:
        self.receipt: Optional[SinkReceipt] = None
        self._owner = owner
        self._settings = owner.settings
        self.context = context
        self.shape = shape
        self.grade = grade
        self._credentials = credentials
        self._paced = owner.settings.pace.paced
        self._order = "time" if self._paced else "stream"
        self._opened = False
        self._closed = False
        self._failed = False
        self._rows = 0
        self._seq = 0
        self._done = 0
        self._complete = False
        self._sent = 0
        self._skipped = 0
        self._transactions = 0
        self._txn_open = False
        self._txn_since = 0.0
        self._since_age = 0
        self._fingerprint = ""
        self._info: Optional[TopicInfo] = None
        self._limits: Tuple[Tuple[str, int, bool], ...] = ()
        self._now = 0
        self._since_clock = 0
        self._serialize: Optional[ValueSerializer] = None
        self._producer: Optional[TxnProducer] = None
        self._builder: Optional[RecordBuilder] = None
        self._buffer: List[Tuple[KafkaRecord, Optional[bytes]]] = []

    # -- identity ---------------------------------------------------------------

    @property
    def transactional_id(self) -> str:
        """The producer's identity and the progress group: stable for the run, stream and topic."""
        return f"flux-sink.{self.context.run_id}.{self.context.slice_key}.{self.shape.topic}"

    # -- the EmitSink protocol --------------------------------------------------

    def open(self, header: EmitHeader) -> None:
        """Check the stream against its declarations, then the topic, the schema and the progress."""
        if self._opened:
            raise FluxError(FLX_3301, f"expose {header.expose_id!r}: sink already open")
        if self._closed:
            raise FluxError(FLX_3301, f"expose {header.expose_id!r}: sink is closed")
        self._opened = True
        try:
            self._open(header)
        except BaseException:
            # The deliver stage closes only a sink that opened, so one that failed to open lets go
            # of its producer and clients here. The failure that got here is the one shown.
            self._closed = True
            try:
                self._release()
            except Exception:  # noqa: BLE001
                LOGGER.debug("releasing a sink that failed to open failed", exc_info=False)
            raise

    def _open(self, header: EmitHeader) -> None:
        shape = self.shape
        if header.expose_id != self.context.expose_id:
            raise FluxError(
                FLX_3504,
                f"this kafka sink was set up for expose {self.context.expose_id!r} "
                f"and opened on {header.expose_id!r}",
            )
        if (
            tuple(header.columns) != shape.columns
            or tuple(header.types) != shape.types
            or header.contract_digest.value != shape.contract_digest
        ):
            raise FluxError(
                FLX_3504,
                f"expose {header.expose_id!r}: the stream's columns, types or contract digest "
                "are not the ones the bundle declares",
            )
        self._builder = RecordBuilder(
            shape,
            run_id=self.context.run_id,
            output_digest=self.context.output_digest.value,
            engine_version=_engine_version(),
        )
        broker = self._owner.broker
        self._info = self._topic(broker)
        self._limits = self._limits_of(self._info)
        self._serialize = self._serializer()
        self._fingerprint = self._settings_fingerprint()
        # The producer first: opening it fences an earlier producer of this id, so nothing it
        # commits can land after the progress below is read.
        self._producer = broker.transactional_producer(
            self.transactional_id,
            self.transactional_id,
            compression=self._settings.compression,
            paced=self._paced,
        )
        found = _read_progress(
            broker.progress(self.transactional_id, shape.topic), self.context.digest_hex
        )
        if found is not None:
            if found.settings != self._fingerprint:
                raise FluxError(
                    FLX_3504,
                    f"topic {shape.topic!r} holds a delivery of this stream made with other key "
                    "or value settings; deliver it to another topic (topic-prefix or "
                    f"topic.{shape.expose_id}), or delete the topic and the group "
                    f"{self.transactional_id!r}",
                )
            if not found.complete and found.order != self._order:
                raise FluxError(
                    FLX_3504,
                    f"topic {shape.topic!r} holds a delivery of this stream begun in "
                    f"{found.order} order and this one is in {self._order} order; resume it with "
                    "the pace it began with",
                )
            self._done, self._complete = found.records, found.complete
            if found.complete:
                self._skipped = found.records
        if self._complete:
            producer, self._producer = self._producer, None
            producer.close()

    def write(self, row: Sequence[object]) -> None:
        """Take one rendered row: send its records now (unpaced) or keep them for ``close``."""
        if self._builder is None:
            raise FluxError(FLX_3301, "sink written to before open")
        if self._closed:
            raise FluxError(FLX_3301, f"expose {self.context.expose_id!r}: sink is closed")
        index = self._rows
        self._rows += 1
        if self._complete:
            return
        try:
            records = self._builder.records(row, index, last=index == self.context.rows - 1)
            serialize, topic = self._serialize, self.shape.topic
            assert serialize is not None
            for record in records:
                body = record.value
                value = None if body is None else serialize(body, topic=topic)
                if self._paced:
                    # The bytes are kept; the dict they came from is not.
                    self._buffer.append((record._replace(value=None), value))
                    continue
                position = self._seq
                self._seq += 1
                if position < self._done:
                    self._skipped += 1
                    continue
                self._since_age += 1
                if self._since_age >= _AGE_EVERY:
                    self._since_age = 0
                    self._roll(position)
                self._send(record, value)
        except BaseException:
            self._failed = True
            raise

    def close(self) -> None:
        """Commit when the whole stream arrived and was acknowledged; abort otherwise."""
        if self._closed:
            return
        self._closed = True
        try:
            if not self._opened or self._builder is None:
                return
            if self._failed or self._rows != self.context.rows:
                self._abort()
                return
            try:
                if not self._complete:
                    if self._paced:
                        self._send_paced()
                    self._finish()
            except BaseException:
                self._abort()
                raise
            self.receipt = self._make_receipt()
        finally:
            self._release()

    # -- topic, schema -----------------------------------------------------------

    def _topic(self, broker: Broker) -> TopicInfo:
        shape, settings = self.shape, self._settings
        info = broker.describe(shape.topic)
        if info is None:
            info = broker.create(
                shape.topic,
                partitions=settings.partitions,
                replication_factor=settings.replication_factor,
                configs={"cleanup.policy": "compact"} if shape.compacted else {},
            )
        if info.configs.get("message.timestamp.type") == "LogAppendTime":
            raise FluxError(
                FLX_3514,
                f"topic {shape.topic!r} has message.timestamp.type=LogAppendTime, which would "
                "replace each record's event time with the time of the write; set it to "
                "CreateTime, or choose another topic with --sink-opt topic.EXPOSE=NAME",
            )
        if shape.compacted and "compact" not in info.configs.get("cleanup.policy", ""):
            raise FluxError(
                FLX_3514,
                f"topic {shape.topic!r} holds a snapshot and is not compacted "
                f"(cleanup.policy={info.configs.get('cleanup.policy', 'delete')}); set "
                "cleanup.policy=compact, or choose another topic with --sink-opt topic.EXPOSE=NAME",
            )
        return info

    def _serializer(self) -> ValueSerializer:
        settings = self._settings
        if settings.registry is None:
            return plain_json(self.shape)
        access = RegistryAccess(
            url=settings.registry,
            auth=self._credentials.registry_auth,
            ca_location=settings.registry_ca_location,
            timeout=settings.timeout,
        )
        return self._owner.serdes.serializer(
            self.shape, value_format=settings.value_format, registry=access
        )

    # -- sending -----------------------------------------------------------------

    def _limits_of(self, info: TopicInfo) -> Tuple[Tuple[str, int, bool], ...]:
        """The topic's timestamp limits that are numbers: ``(name, limit, how late is refused)``."""
        found = []
        for name, late in (
            ("message.timestamp.before.max.ms", True),
            ("message.timestamp.after.max.ms", False),
        ):
            limit = info.configs.get(name)
            if limit is not None and limit.lstrip("-").isdigit():
                found.append((name, int(limit), late))
        return tuple(found)

    def _check_time(self, record: KafkaRecord) -> None:
        stamp = record.timestamp_ms
        if stamp <= 0:
            raise FluxError(
                FLX_3514,
                f"topic {self.shape.topic!r}: the record of row {record.row} is at or before "
                "1970-01-01T00:00:00Z; brokers refuse a negative timestamp and a producer reads "
                "0 as the current time; run a window that starts after it",
            )
        if not self._limits:
            return
        # A paced stream reads the wall clock for each record, since it waits between them. An
        # unpaced one reads it once for every `_CLOCK_EVERY` records, which take a few
        # milliseconds to send against limits that are hours.
        if self._paced or self._since_clock <= 0:
            self._now = self._owner.clock.wall_ms()
            self._since_clock = _CLOCK_EVERY
        self._since_clock -= 1
        now = self._now
        for name, limit, late in self._limits:
            if (now - stamp > limit) if late else (stamp - now > limit):
                raise FluxError(
                    FLX_3514,
                    f"topic {self.shape.topic!r}: the record of row {record.row} is outside "
                    f"{name}={limit}; configure the topic for a backfill (a larger limit) or run "
                    "a window nearer the present",
                )

    def _begin(self) -> None:
        assert self._producer is not None
        if not self._txn_open:
            self._producer.begin()
            self._txn_open = True
            self._txn_since = self._owner.clock.monotonic()

    def _send(self, record: KafkaRecord, value: Optional[bytes]) -> None:
        producer = self._producer
        assert producer is not None
        self._check_time(record)
        if not self._txn_open:
            self._begin()
        producer.produce(self.shape.topic, record.key, value, record.timestamp_ms, record.headers)
        self._sent += 1

    def _roll(self, position: int) -> None:
        """Between records of an unpaced stream: commit when ``commit-every`` is due, or refuse
        a stream that is using up its one transaction."""
        if not self._txn_open:
            return
        every = self._settings.commit_every
        if every is None:
            self._require_time_left()
        elif self._owner.clock.monotonic() - self._txn_since >= every:
            self._commit(position, complete=False)

    def _require_time_left(self) -> None:
        """``FLX-3514`` when the open transaction has used up its share of ``transaction-timeout``."""
        limit = self._settings.transaction_timeout
        if self._owner.clock.monotonic() - self._txn_since >= _TRANSACTION_SHARE * limit:
            raise FluxError(
                FLX_3514,
                f"topic {self.shape.topic!r}: the stream did not fit in one transaction of "
                f"transaction-timeout={limit:g} s; raise transaction-timeout (and the brokers' "
                "transaction.max.timeout.ms), or pass commit-every=SECONDS to commit it as it goes",
            )

    def _commit(self, records: int, *, complete: bool) -> None:
        assert self._producer is not None
        if self._txn_open and not self._paced and self._settings.commit_every is None:
            self._require_time_left()
        self._begin()
        self._producer.commit(
            topic=self.shape.topic,
            records=records,
            metadata=_progress_text(
                records, self._order, complete, self.context.digest_hex, self._fingerprint
            ),
        )
        self._txn_open = False
        self._transactions += 1

    def _send_paced(self) -> None:
        records = [record for record, _ in self._buffer]
        order = time_order(records)
        scheduled = scheduled_times(records)
        clock = self._owner.clock
        remaining = order[self._done :]
        self._skipped += min(self._done, len(order))
        if not remaining:
            return
        pacer = Pacer(self._settings.pace, monotonic=clock.monotonic, sleep=clock.sleep)
        pacer.start(scheduled[remaining[0]])
        every = self._settings.commit_interval
        position = min(self._done, len(order))
        for index in remaining:
            record, value = self._buffer[index]
            due = scheduled[index]
            delay = pacer.delay(due)
            if self._txn_open and (delay > every or clock.monotonic() - self._txn_since >= every):
                self._commit(position, complete=False)
            pacer.wait(due)
            self._send(record, value)
            position += 1

    def _finish(self) -> None:
        total = self._seq if not self._paced else len(self._buffer)
        self._commit(total, complete=True)

    def _abort(self) -> None:
        producer = self._producer
        if producer is not None and self._txn_open:
            self._txn_open = False
            try:
                producer.abort()
            except FluxError:
                # The producer is fenced or the brokers are gone: the transaction times out and
                # the next delivery's producer aborts it. The error that got us here is shown.
                pass

    def _release(self) -> None:
        producer, self._producer = self._producer, None
        try:
            if producer is not None:
                producer.close()
        finally:
            try:
                self._owner.broker.close()
            finally:
                self._owner.serdes.close()
        self._buffer = []

    def _settings_fingerprint(self) -> str:
        """The first 16 hex digits of a digest of what decides a record's bytes besides its row.

        The key columns and format, and the value form: plain JSON, or the registry's value format
        and subject (never the schema's id, which a registry hands out by its own count).
        """
        shape, settings = self.shape, self._settings
        value: Any
        if settings.registry is None:
            value = "json"
        elif shape.subject is not None:
            value = [shape.subject.value_format.lower(), shape.subject.subject]
        else:
            value = [settings.value_format, f"{shape.topic}-value"]
        document = {
            "key": list(shape.key_columns),
            "keyFormat": shape.key_format,
            "value": value,
            "timestamp": [shape.timestamp_column, shape.timestamp_default],
            "pinnedVersion": shape.subject.version if shape.subject is not None else None,
        }
        return sha256_hex(canonical_bytes(document))[:16]

    # -- the receipt -------------------------------------------------------------

    def _make_receipt(self) -> SinkReceipt:
        shape, settings = self.shape, self._settings
        writer: Dict[str, Any] = {
            "topic": shape.topic,
            "partitions": self._info.partitions if self._info else None,
            "shape": "debezium" if shape.envelope is not None else "row",
            "value": self._serialize.description if self._serialize else "json",
            "key": shape.key_format,
            "pace": settings.pace.text,
            "compression": settings.compression,
            "transactions": self._transactions,
            "records": self._sent,
            "skipped": self._skipped,
            "audience": settings.audience,
            "privacyGrade": self.grade.value,
            "partitioner": "murmur2_random",
        }
        held = self.context.privacy_grade
        if held is not None and held.profile_grade is not None:
            # The grade worked out from the pinned profile the command was given; an external
            # audience is held to this one.
            writer["profileGrade"] = held.profile_grade
        writer.update(self._owner.broker.versions())
        return SinkReceipt(
            sink=self.name,
            run_id=self.context.run_id,
            expose_id=self.context.expose_id,
            output_digest=self.context.output_digest.value,
            rows=self.context.rows,
            target=settings.target,
            table=shape.topic,
            writer=writer,
        )


def _engine_version() -> str:
    import flux_engine

    return str(flux_engine.__version__)


def target(target: str, options: Mapping[str, str]) -> KafkaTarget:
    """The ``kafka`` sink's factory: ``--sink kafka:HOST:PORT[,...]`` and its options.

    Every option is checked here, before the run, and nothing is read from the environment or
    contacted: ``confluent-kafka`` is required (``FLX-3501`` when the ``[kafka]`` extra is not
    installed), and the credentials the ``-env`` options name are read when a stream starts.
    """
    settings = KafkaSettings.parse(target, options)
    require("confluent_kafka", extra="kafka", feature="the Kafka sink")

    def known() -> List[Known]:
        return known_strings_of(settings)

    broker = ConfluentBroker(settings.client(), lambda: read_credentials(settings), known)
    return KafkaTarget(settings, broker=broker, serdes=ConfluentSerdes(known=known))
