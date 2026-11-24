# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How fast the Kafka sink turns a stream into records: plain JSON, Avro and JSON Schema.

``make bench`` runs this module with pytest-benchmark. It scales the devices bundle so that its
events stream (a change event for each change of a device, written in Debezium's envelope, with a
tombstone after each delete) and its ``reading`` rows hold about ``FLUX_BENCH_ROWS`` rows each
(100,000 unless set), generates the run once, then times one sink handling one stream over a broker
that keeps nothing: the sink builds each record (key, event time, headers, value), serializes its
value and queues it. Every case's ``extra_info`` records the records per second, the megabytes per
second of the values, and the sink's time as a share of the time the whole bundle takes to
generate; each figure is the best of ``FLUX_BENCH_ROUNDS`` rounds (5 unless set), the least
disturbed by whatever else the machine is doing. ``cpu_records_per_second`` counts the process's
CPU seconds instead of the wall clock, which stays steady when other work shares the machine.

Avro and JSON Schema values go through confluent-kafka's serializers over its in-memory registry
client. The broker here counts what it is given, so the figures are the sink's own; the figures
against a real broker come from ``python -m tests.bench.kafka_real_broker``.

Marked ``slow`` and skipped without pytest-benchmark or confluent-kafka.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

pytest.importorskip("pytest_benchmark", reason="needs pytest-benchmark (the accuracy group)")
pytest.importorskip("confluent_kafka", reason="needs confluent-kafka (the kafka extra)")

from confluent_kafka.schema_registry._sync.mock_schema_registry_client import (  # noqa: E402
    MockSchemaRegistryClient,
)

from flux_engine.sinks._kafka_broker import TopicInfo  # noqa: E402
from flux_engine.sinks._kafka_serdes import ConfluentSerdes  # noqa: E402
from flux_engine.sinks.kafka import KafkaSettings, KafkaTarget  # noqa: E402
from flux_engine.stages import pipeline  # noqa: E402

from ._kafka_streams import PROFILE, Prepared, Stream, generate, scaled_bundle  # noqa: E402

pytestmark = pytest.mark.slow

_ROWS = int(os.environ.get("FLUX_BENCH_ROWS", "100000"))
_ROUNDS = int(os.environ.get("FLUX_BENCH_ROUNDS", "5"))

_TIMES: Dict[str, float] = {}


class NullTxn:
    """A producer that keeps nothing: it counts the records and the bytes of their values."""

    def __init__(self, broker: "NullBroker") -> None:
        self.broker = broker

    def begin(self) -> None:
        pass

    def produce(
        self,
        topic: str,
        key: bytes,
        value: Optional[bytes],
        timestamp_ms: int,
        headers: Sequence[Tuple[str, bytes]],
    ) -> None:
        self.broker.records += 1
        self.broker.value_bytes += 0 if value is None else len(value)

    def commit(self, *, topic: str, records: int, metadata: str) -> None:
        pass

    def abort(self) -> None:
        pass

    def close(self) -> None:
        pass


class NullBroker:
    """The sink's broker with no cluster: one topic of three partitions, nothing stored."""

    def __init__(self) -> None:
        self.records = 0
        self.value_bytes = 0
        self.options: Dict[str, Any] = {}

    def describe(self, topic: str) -> Optional[TopicInfo]:
        return TopicInfo(partitions=3, configs={})

    def create(
        self,
        topic: str,
        *,
        partitions: Optional[int],
        replication_factor: Optional[int],
        configs: Mapping[str, str],
    ) -> TopicInfo:
        return TopicInfo(partitions=3, configs=dict(configs))

    def progress(self, group: str, topic: str) -> Optional[str]:
        return None

    def transactional_producer(self, transactional_id: str, group: str, **options: Any) -> NullTxn:
        self.options = dict(options)
        return NullTxn(self)

    def versions(self) -> Mapping[str, str]:
        return {}

    def close(self) -> None:
        pass


def deliver(stream: Stream, options: Mapping[str, str]) -> NullBroker:
    """One sink writes *stream* to a broker that keeps nothing; returns the broker's counts."""
    broker = NullBroker()
    settings = KafkaSettings.parse("127.0.0.1:9092", {"audience": "internal", **options})
    serdes = ConfluentSerdes(
        client_factory=lambda access: MockSchemaRegistryClient({"url": "http://registry.invalid"})
    )
    sink = KafkaTarget(settings, broker=broker, serdes=serdes).sink_for(stream.context)
    sink.open(stream.header)
    write = sink.write
    for row in stream.rows:
        write(row)
    sink.close()
    assert sink.receipt is not None
    return broker


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> Prepared:
    root = tmp_path_factory.mktemp("kafka-bench")
    started = time.perf_counter()
    pipeline.execute(scaled_bundle(root / "generation", _ROWS), profile=PROFILE)
    _TIMES["generation"] = time.perf_counter() - started
    return generate(root, _ROWS)


def _record(benchmark: Any, name: str, stream: Stream, broker: NullBroker, cpu: float) -> None:
    if benchmark.stats is None:
        # pytest-benchmark is off (under xdist): the sink ran once, untimed.
        return
    best = benchmark.stats.stats.min
    benchmark.extra_info["rows"] = len(stream.rows)
    benchmark.extra_info["records"] = broker.records
    benchmark.extra_info["records_per_second"] = round(broker.records / best)
    benchmark.extra_info["cpu_records_per_second"] = round(broker.records / cpu)
    benchmark.extra_info["value_megabytes_per_second"] = round(broker.value_bytes / best / 1e6, 1)
    benchmark.extra_info["share_of_generation_pct"] = round(100 * best / _TIMES["generation"], 2)
    _TIMES[name] = best


_CASES = [
    ("events-json", "device_change", {}),
    ("events-avro", "device_change", {"registry": "http://registry.invalid"}),
    ("rows-json", "reading", {}),
    ("rows-avro", "reading", {"registry": "http://registry.invalid"}),
    (
        "rows-jsonschema",
        "reading",
        {"registry": "http://registry.invalid", "value-format": "json"},
    ),
]


@pytest.mark.parametrize(("name", "expose", "options"), _CASES, ids=[c[0] for c in _CASES])
def test_kafka_sink(
    benchmark: Any, prepared: Prepared, name: str, expose: str, options: Mapping[str, str]
) -> None:
    stream = prepared.stream(expose)
    broker: List[NullBroker] = []
    cpu: List[float] = []

    def once() -> None:
        started = time.process_time()
        broker[:] = [deliver(stream, options)]
        cpu.append(time.process_time() - started)

    benchmark.pedantic(once, rounds=_ROUNDS, iterations=1)
    assert broker[0].records >= len(stream.rows)
    _record(benchmark, name, stream, broker[0], min(cpu))
