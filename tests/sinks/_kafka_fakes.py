# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Fakes for the Kafka sink's tests: a broker with transactions, a registry stand-in and a clock.

:class:`FakeBroker` implements :class:`flux_engine.sinks._kafka_broker.Broker` in memory: topics
with partitions chosen by Java's murmur2, transactions whose records become visible only when they
commit, progress committed per group and topic with the transaction, fencing of an earlier producer
under the same ``transactional.id``, and failures injected into a producer. Nothing here imports
confluent_kafka.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.sinks._kafka_broker import TopicInfo, TxnProducer
from flux_engine.sinks._kafka_serdes import RegistryAccess, ValueSerializer, compact_json
from flux_engine.sinks._kafka_shape import StreamShape
from flux_engine.sinks.errors import FLX_3505
from flux_engine.sinks.kafka import Clock

DEFAULT_CONFIGS: Mapping[str, str] = {
    "cleanup.policy": "delete",
    "message.timestamp.type": "CreateTime",
    "message.timestamp.before.max.ms": "9223372036854775807",
    "message.timestamp.after.max.ms": "3600000",
}


def murmur2(data: bytes) -> int:
    """Kafka's ``Utils.murmur2``, as an unsigned 32-bit integer."""
    seed, m, r = 0x9747B28C, 0x5BD1E995, 24
    h = (seed ^ len(data)) & 0xFFFFFFFF
    for offset in range(0, len(data) - len(data) % 4, 4):
        k = int.from_bytes(data[offset : offset + 4], "little")
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
    tail = data[len(data) - len(data) % 4 :]
    if len(tail) == 3:
        h ^= tail[2] << 16
    if len(tail) >= 2:
        h ^= tail[1] << 8
    if len(tail) >= 1:
        h ^= tail[0]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def java_partition(key: bytes, partitions: int) -> int:
    """The partition Kafka's default partitioner gives *key*: ``toPositive(murmur2(key)) % n``."""
    return (murmur2(key) & 0x7FFFFFFF) % partitions


@dataclass(frozen=True)
class Stored:
    """One committed record, as a read-committed consumer sees it."""

    partition: int
    key: bytes
    value: Optional[bytes]
    timestamp: int
    headers: Tuple[Tuple[str, bytes], ...]


class FakeClock:
    """Time that moves only when something sleeps."""

    def __init__(self, wall_ms: int = 1_800_000_000_000) -> None:
        self.now = 1000.0
        self.wall = wall_ms
        self.sleeps: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.wall += int(seconds * 1000)

    def wall_ms(self) -> int:
        return self.wall

    def clock(self) -> Clock:
        return Clock(monotonic=self.monotonic, sleep=self.sleep, wall_ms=self.wall_ms)


@dataclass
class Failure:
    """A fault for the next producer: after *after* produced records it ``raise``s or ``die``s.

    A producer that dies leaves its transaction open and answers nothing (a zombie); the next
    producer under the same ``transactional.id`` fences it. ``commit`` makes the commit fail.
    """

    kind: str
    after: int = 0


class FakeTxn:
    """:class:`TxnProducer` in memory."""

    def __init__(
        self, broker: "FakeBroker", transactional_id: str, group: str, failure: Optional[Failure]
    ) -> None:
        self.broker = broker
        self.transactional_id = transactional_id
        self.group = group
        self.failure = failure
        self.fenced = False
        self.dead = False
        self.open = False
        self.closed = False
        self.pending: List[
            Tuple[str, bytes, Optional[bytes], int, Tuple[Tuple[str, bytes], ...]]
        ] = []
        self.produced = 0
        self.commits = 0

    def _alive(self) -> None:
        if self.fenced:
            raise FluxError(FLX_3505, "kafka: the producer was fenced by a newer one")
        if self.dead:
            raise FluxError(FLX_3505, "kafka: the producer is gone")

    def begin(self) -> None:
        self._alive()
        self.broker.calls.append(("begin", self.transactional_id))
        self.open = True

    def produce(
        self,
        topic: str,
        key: bytes,
        value: Optional[bytes],
        timestamp_ms: int,
        headers: Sequence[Tuple[str, bytes]],
    ) -> None:
        self._alive()
        failure = self.failure
        if (
            failure is not None
            and failure.kind in ("raise", "die")
            and self.produced >= failure.after
        ):
            if failure.kind == "die":
                self.dead = True
            raise FluxError(FLX_3505, "kafka: a record was not acknowledged")
        self.produced += 1
        self.pending.append((topic, key, value, timestamp_ms, tuple(headers)))

    def commit(self, *, topic: str, records: int, metadata: str) -> None:
        self._alive()
        if self.failure is not None and self.failure.kind == "commit":
            raise FluxError(FLX_3505, "kafka: could not commit the transaction")
        self.broker.calls.append(("commit", self.transactional_id, str(records)))
        for name, key, value, stamp, headers in self.pending:
            info = self.broker.topics[name]
            partition = java_partition(key, info.partitions)
            self.broker.logs[name].append(Stored(partition, key, value, stamp, headers))
        self.pending = []
        self.broker.committed[(self.group, topic)] = metadata
        self.open = False
        self.commits += 1

    def abort(self) -> None:
        if self.dead or self.fenced:
            return
        self.broker.calls.append(("abort", self.transactional_id))
        self.pending = []
        self.open = False

    def close(self) -> None:
        self.closed = True


class FakeBroker:
    """:class:`~flux_engine.sinks._kafka_broker.Broker` in memory.

    ``calls`` lists what the sink asked of it, in order, so a test can assert that nothing was
    contacted. ``topics`` maps a name to its :class:`TopicInfo`, ``logs`` to its committed
    records in commit order, ``committed`` holds the progress metadata per ``(group, topic)``.
    """

    def __init__(self, *, partitions: int = 3) -> None:
        self.default_partitions = partitions
        self.topics: Dict[str, TopicInfo] = {}
        self.logs: Dict[str, List[Stored]] = {}
        self.committed: Dict[Tuple[str, str], str] = {}
        self.calls: List[Tuple[str, ...]] = []
        self.producers: List[FakeTxn] = []
        self.producer_options: List[Dict[str, Any]] = []
        self.failures: List[Failure] = []
        self.closes = 0
        self.cluster = "fake-cluster"
        self._open: Dict[str, FakeTxn] = {}

    def add_topic(self, topic: str, *, partitions: Optional[int] = None, **configs: str) -> None:
        """Create *topic* ahead of a delivery; ``configs`` use ``_`` for ``.`` in a name."""
        named = {key.replace("_", "."): value for key, value in configs.items()}
        self.topics[topic] = TopicInfo(
            partitions=partitions or self.default_partitions, configs={**DEFAULT_CONFIGS, **named}
        )
        self.logs.setdefault(topic, [])

    def describe(self, topic: str) -> Optional[TopicInfo]:
        self.calls.append(("describe", topic))
        return self.topics.get(topic)

    def create(
        self,
        topic: str,
        *,
        partitions: Optional[int],
        replication_factor: Optional[int],
        configs: Mapping[str, str],
    ) -> TopicInfo:
        self.calls.append(("create", topic))
        self.topics[topic] = TopicInfo(
            partitions=partitions or self.default_partitions,
            configs={**DEFAULT_CONFIGS, **configs},
        )
        self.logs.setdefault(topic, [])
        return self.topics[topic]

    def progress(self, group: str, topic: str) -> Optional[str]:
        self.calls.append(("progress", group, topic))
        return self.committed.get((group, topic))

    def transactional_producer(
        self, transactional_id: str, group: str, *, compression: str = "none", paced: bool = False
    ) -> TxnProducer:
        self.calls.append(("producer", transactional_id))
        self.producer_options.append({"compression": compression, "paced": paced})
        earlier = self._open.get(transactional_id)
        if earlier is not None:
            earlier.fenced = True
            earlier.pending = []
        failure = self.failures.pop(0) if self.failures else None
        producer = FakeTxn(self, transactional_id, group, failure)
        self._open[transactional_id] = producer
        self.producers.append(producer)
        return producer

    def versions(self) -> Mapping[str, str]:
        return {}

    def cluster_id(self) -> Optional[str]:
        return self.cluster

    def close(self) -> None:
        self.closes += 1

    def drop_progress(self) -> None:
        """The brokers expire every committed offset (``offsets.retention.minutes``)."""
        self.committed.clear()

    # -- reading, as a read-committed consumer would ------------------------------

    def read(self, topic: str) -> List[Stored]:
        return list(self.logs.get(topic, []))

    def by_key(self, topic: str) -> Dict[bytes, List[Stored]]:
        found: Dict[bytes, List[Stored]] = {}
        for record in self.logs.get(topic, []):
            found.setdefault(record.key, []).append(record)
        return found


class FakeSerdes:
    """Serdes that need no registry: values are compact JSON, labelled like a registry's."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Optional[str]]] = []
        self.timeouts: List[float] = []
        self.closes = 0

    def serializer(
        self, shape: StreamShape, *, value_format: str, registry: RegistryAccess
    ) -> ValueSerializer:
        self.calls.append((shape.topic, value_format, registry.auth))
        self.timeouts.append(registry.timeout)
        subject = shape.subject.subject if shape.subject else f"{shape.topic}-value"
        return _Labelled(f"{value_format}:{subject}@1")

    def close(self) -> None:
        self.closes += 1


class _Labelled:
    def __init__(self, description: str) -> None:
        self.description = description

    def __call__(self, value: Mapping[str, Any], *, topic: str) -> bytes:
        return compact_json(dict(value))
