# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink against a real broker and a real registry (ADR 0038).

A module fixture starts one Apache Kafka node (KRaft) and one Karapace registry on a Docker network
of their own, and removes both, and the network, when the module ends or fails. The tests deliver
the devices bundle and read every topic back with a read-committed consumer:

* every record arrives, in each key's order, on the partition Java's murmur2 chooses, with the event
  time as its timestamp and a tombstone after each delete; an external audience is refused before a
  topic is created;
* a delivery whose producer dies mid-transaction, and one that is aborted on an error, leave nothing
  visible, the next clean delivery leaves each record exactly once, and the one after sends nothing;
* every value decodes through the registry, Avro and JSON Schema, to what was sent.

Skipped without confluent-kafka or a working Docker.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import socket
import subprocess
import time
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import pytest

confluent_kafka = pytest.importorskip("confluent_kafka")

from confluent_kafka import Consumer, KafkaError  # noqa: E402
from confluent_kafka.admin import AdminClient  # noqa: E402
from confluent_kafka.schema_registry import SchemaRegistryClient  # noqa: E402
from confluent_kafka.schema_registry.avro import AvroDeserializer  # noqa: E402
from confluent_kafka.schema_registry.json_schema import JSONDeserializer  # noqa: E402
from confluent_kafka.serialization import MessageField, SerializationContext  # noqa: E402

import flux_engine  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.sinks._kafka_broker import (  # noqa: E402
    Broker,
    ConfluentBroker,
    Credentials,
    TopicInfo,
    TxnProducer,
)
from flux_engine.sinks._kafka_records import KafkaRecord, RecordBuilder  # noqa: E402
from flux_engine.sinks._kafka_serdes import ConfluentSerdes  # noqa: E402
from flux_engine.sinks._kafka_shape import ShapeSettings, StreamShape, stream_shapes  # noqa: E402
from flux_engine.sinks.errors import FLX_3505  # noqa: E402
from flux_engine.sinks.kafka import KafkaSettings, KafkaTarget  # noqa: E402
from flux_engine.stages.deliver import deliver_run  # noqa: E402
from flux_engine.stages.layout import RunLayout  # noqa: E402
from flux_engine.stages.run import run  # noqa: E402

from ._kafka_fakes import java_partition  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROFILE = FIXTURES / "kafka-devices.profile.json"
SIMULATION = "sim-devices"
EXPOSES = ("site", "device_version", "reading", "device_current", "device_change", "device_month")
KAFKA_IMAGE = "apache/kafka:4.4.0"
REGISTRY_IMAGE = "ghcr.io/aiven-open/karapace:6.2.4"
PARTITIONS = 3


# -- the cluster ------------------------------------------------------------------------


def _docker_works() -> bool:
    import shutil as _shutil

    if _shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
    return probe.returncode == 0


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


@dataclass(frozen=True)
class Cluster:
    bootstrap: str
    registry: str


def _docker(*args: str, timeout: int = 300) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=timeout)


def _wait(what: str, ready: Any, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if ready():
                return
        except Exception:  # noqa: BLE001 - a service that is still starting refuses or resets
            pass
        time.sleep(1)
    pytest.fail(f"{what} did not become ready in {seconds} s")


@pytest.fixture(scope="module")
def cluster() -> Iterator[Cluster]:
    if not _docker_works():
        pytest.skip("needs Docker to start Kafka and a registry")
    tag = uuid.uuid4().hex[:10]
    network, broker, registry = (f"flux-sink-{kind}-{tag}" for kind in ("net", "kafka", "registry"))
    port, registry_port = _free_port(), _free_port()
    try:
        _docker("network", "create", network)
        _docker(
            "run", "-d", "--name", broker, "--network", network, "-p", f"127.0.0.1:{port}:{port}",
            "-e", "KAFKA_NODE_ID=1",
            "-e", "KAFKA_PROCESS_ROLES=broker,controller",
            "-e", f"KAFKA_LISTENERS=INTERNAL://:19092,EXTERNAL://:{port},CONTROLLER://:9093",
            "-e", f"KAFKA_ADVERTISED_LISTENERS=INTERNAL://{broker}:19092,EXTERNAL://127.0.0.1:{port}",
            "-e", "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT",
            "-e", "KAFKA_INTER_BROKER_LISTENER_NAME=INTERNAL",
            "-e", "KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER",
            "-e", f"KAFKA_CONTROLLER_QUORUM_VOTERS=1@{broker}:9093",
            "-e", "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1",
            "-e", "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1",
            "-e", "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1",
            "-e", "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0",
            "-e", f"KAFKA_NUM_PARTITIONS={PARTITIONS}",
            "-e", "KAFKA_AUTO_CREATE_TOPICS_ENABLE=false",
            KAFKA_IMAGE,
        )  # fmt: skip
        _docker(
            "run", "-d", "--name", registry, "--network", network,
            "-p", f"127.0.0.1:{registry_port}:8081",
            "-e", f"KARAPACE_BOOTSTRAP_URI={broker}:19092",
            "-e", "KARAPACE_HOST=0.0.0.0",
            "-e", "KARAPACE_PORT=8081",
            "-e", f"KARAPACE_ADVERTISED_HOSTNAME={registry}",
            "-e", "KARAPACE_TOPIC_NAME=_schemas",
            "-e", "KARAPACE_GROUP_ID=karapace",
            "-e", "KARAPACE_MASTER_ELIGIBILITY=true",
            "-e", "KARAPACE_LOG_LEVEL=WARNING",
            REGISTRY_IMAGE, "python3", "-m", "karapace",
        )  # fmt: skip
        bootstrap = f"127.0.0.1:{port}"
        url = f"http://127.0.0.1:{registry_port}"
        admin = AdminClient({"bootstrap.servers": bootstrap})
        _wait("Kafka", lambda: admin.list_topics(timeout=3) is not None, 120)

        def registry_ready() -> bool:
            with urllib.request.urlopen(f"{url}/subjects", timeout=3) as reply:
                return bool(reply.status == 200)

        _wait("the registry", registry_ready, 180)
        yield Cluster(bootstrap=bootstrap, registry=url)
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", registry, broker], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


# -- the run, and what it should have produced -------------------------------------------


@dataclass(frozen=True)
class Kept:
    bundle: Path
    layout: RunLayout
    shapes: Mapping[str, StreamShape]

    def expected(self, expose_id: str, prefix: str = "sim.") -> List[KafkaRecord]:
        """The records the sink should send for *expose_id*, built from the run's own stream."""
        receipt = json.loads(self.layout.receipt_path.read_text(encoding="utf-8"))
        digest = receipt["emits"][expose_id]["outputDigest"]
        shape = self.shapes[expose_id]
        builder = RecordBuilder(
            shape,
            run_id=self.layout.run_id,
            output_digest=digest,
            engine_version=str(flux_engine.__version__),
        )
        text = self.layout.emit_path(expose_id).read_text(encoding="utf-8")
        rows = [json.loads(line) for line in text.splitlines()]
        return [
            record
            for index, row in enumerate(rows)
            for record in builder.records(row, index, last=index == len(rows) - 1)
        ]


@pytest.fixture(scope="module")
def kept(tmp_path_factory: pytest.TempPathFactory) -> Kept:
    root = tmp_path_factory.mktemp("kafka-run")
    bundle = root / "bundle"
    shutil.copytree(FIXTURES / "kafka-devices", bundle)
    result = run(bundle, state_dir=root / "state", profile=PROFILE)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(root / "state", result.artifacts["plan"]["run_id"])
    return Kept(bundle, layout, stream_shapes(bundle, SIMULATION, ShapeSettings()))


def _options(cluster: Cluster, **more: str) -> Dict[str, str]:
    return {"audience": "internal", "registry": cluster.registry, "timeout": "60", **more}


# -- reading back ------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    partition: int
    key: bytes
    value: Optional[bytes]
    timestamp: int
    headers: Tuple[Tuple[str, bytes], ...]


def read_back(
    cluster: Cluster, counts: Mapping[str, int], *, settle: float = 4.0, patience: float = 90.0
) -> Dict[str, List[Message]]:
    """Every record the topics hold for a read-committed consumer.

    Waits until each topic has at least its count (when it is not zero), then *settle* seconds more
    for anything extra, so a duplicate is seen.
    """
    consumer = Consumer(
        {
            "bootstrap.servers": cluster.bootstrap,
            "group.id": f"flux-sink-reader-{uuid.uuid4().hex}",
            "auto.offset.reset": "earliest",
            "isolation.level": "read_committed",
            "enable.auto.commit": False,
        }
    )
    found: Dict[str, List[Message]] = {topic: [] for topic in counts}
    try:
        consumer.subscribe(list(counts))
        deadline = time.monotonic() + patience
        satisfied: Optional[float] = None
        while time.monotonic() < deadline:
            message = consumer.poll(1.0)
            if message is not None and message.error() is None:
                found[message.topic()].append(
                    Message(
                        message.partition(),
                        message.key(),
                        message.value(),
                        message.timestamp()[1],
                        tuple((name, bytes(value)) for name, value in message.headers() or ()),
                    )
                )
                satisfied = None
            elif message is not None and message.error().code() != KafkaError.UNKNOWN_TOPIC_OR_PART:
                raise AssertionError(str(message.error()))
            if all(len(found[topic]) >= count for topic, count in counts.items()):
                satisfied = satisfied if satisfied is not None else time.monotonic()
                if time.monotonic() - satisfied >= settle:
                    break
    finally:
        consumer.close()
    return found


def _plain(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return (
            value.astimezone(dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _same(decoded: Mapping[str, Any], sent: Mapping[str, Any]) -> None:
    for name, value in sent.items():
        got = decoded[name]
        if isinstance(value, Mapping):
            _same(got, value)
        elif isinstance(got, Decimal):
            assert got == Decimal(value), name
        else:
            assert _plain(got) == value, name


def _check_topic(
    messages: Sequence[Message], expected: Sequence[KafkaRecord], partitions: int = PARTITIONS
) -> None:
    """Every expected record is there once: key, partition, timestamp, headers, per-key order."""
    assert len(messages) == len(expected)
    by_key: Dict[bytes, List[Message]] = {}
    for message in messages:
        by_key.setdefault(message.key, []).append(message)
    want: Dict[bytes, List[KafkaRecord]] = {}
    for record in expected:
        want.setdefault(record.key, []).append(record)
    assert set(by_key) == set(want)
    for key, records in want.items():
        got = by_key[key]
        assert [(m.timestamp, m.headers, m.value is None) for m in got] == [
            (r.timestamp_ms, r.headers, r.value is None) for r in records
        ], "each key's records arrive in the stream's order"
        assert {m.partition for m in got} == {java_partition(key, partitions)}


# -- (1) delivery, order, partitions, time, tombstones -----------------------------------


def test_a_delivery_reads_back_in_order_on_javas_partitions_with_event_time(
    cluster: Cluster, kept: Kept, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle"
    shutil.copytree(kept.bundle, bundle)
    result = run(
        bundle,
        state_dir=tmp_path / "state",
        profile=PROFILE,
        sink=f"kafka:{cluster.bootstrap}",
        sink_options=_options(cluster),
    )
    assert result.success, result.stderr
    expected = {f"sim.{expose}": kept.expected(expose) for expose in EXPOSES}
    # The run id is the same for the same bundle, so the topics hold what `kept` expects.
    assert result.artifacts["plan"]["run_id"] == kept.layout.run_id
    found = read_back(cluster, {topic: len(records) for topic, records in expected.items()})
    for topic, records in expected.items():
        _check_topic(found[topic], records)
    changes = found["sim.device_change"]
    decode = AvroDeserializer(SchemaRegistryClient({"url": cluster.registry}))
    assert any(m.value is None for m in changes), "the history ends some entities"
    for index, message in enumerate(changes):
        if message.value is None:
            before = changes[index - 1]
            assert before.key == message.key and before.timestamp == message.timestamp
            assert before.value is not None
            decoded = decode(
                before.value, SerializationContext("sim.device_change", MessageField.VALUE)
            )
            assert decoded["op"] == "d" and decoded["after"] is None
    receipts = result.artifacts["sinks"]["receipts"]
    assert {r["writer"]["partitions"] for r in receipts} == {PARTITIONS}
    assert all(r["writer"]["transactions"] == 1 and r["writer"]["skipped"] == 0 for r in receipts)


def test_an_external_audience_is_refused_before_any_topic_is_created(
    cluster: Cluster, kept: Kept, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle"
    shutil.copytree(kept.bundle, bundle)
    result = run(
        bundle,
        state_dir=tmp_path / "state",
        profile=PROFILE,
        sink=f"kafka:{cluster.bootstrap}",
        sink_options=_options(cluster, audience="external", **{"topic-prefix": "refused."}),
    )
    assert result.exit_code == 1 and result.error_code == "FLX-3512"
    topics = AdminClient({"bootstrap.servers": cluster.bootstrap}).list_topics(timeout=10).topics
    assert not [name for name in topics if name.startswith("refused.")]


# -- (2) exactly once --------------------------------------------------------------------


class _Faulty:
    """A producer that fails after *after* records: it dies silently (a zombie) or raises."""

    def __init__(self, inner: TxnProducer, after: int, *, zombie: bool) -> None:
        self.inner, self.after, self.zombie = inner, after, zombie
        self.sent = 0
        self.dead = False

    def begin(self) -> None:
        self.inner.begin()

    def produce(
        self,
        topic: str,
        key: bytes,
        value: Optional[bytes],
        timestamp_ms: int,
        headers: Sequence[Tuple[str, bytes]],
    ) -> None:
        if self.sent >= self.after:
            self.dead = self.zombie
            raise FluxError(FLX_3505, "kafka: an injected failure")
        self.sent += 1
        self.inner.produce(topic, key, value, timestamp_ms, headers)

    def commit(self, *, topic: str, records: int, metadata: str) -> None:
        self.inner.commit(topic=topic, records=records, metadata=metadata)

    def abort(self) -> None:
        if not self.dead:
            self.inner.abort()

    def close(self) -> None:
        if not self.dead:
            self.inner.close()


class _FaultyBroker:
    """The real broker adapter, with a fault for the first producer it hands out."""

    def __init__(self, inner: Broker) -> None:
        self.inner = inner
        self.faults: List[Tuple[int, bool]] = []
        self.zombies: List[_Faulty] = []

    def describe(self, topic: str) -> Optional[TopicInfo]:
        return self.inner.describe(topic)

    def create(self, topic: str, **more: Any) -> TopicInfo:
        return self.inner.create(topic, **more)

    def progress(self, group: str, topic: str) -> Optional[str]:
        return self.inner.progress(group, topic)

    def transactional_producer(
        self, transactional_id: str, group: str, *, compression: str = "none", paced: bool = False
    ) -> TxnProducer:
        producer = self.inner.transactional_producer(
            transactional_id, group, compression=compression, paced=paced
        )
        if not self.faults:
            return producer
        after, zombie = self.faults.pop(0)
        faulty = _Faulty(producer, after, zombie=zombie)
        self.zombies.append(faulty)
        return faulty

    def versions(self) -> Mapping[str, str]:
        return self.inner.versions()

    def close(self) -> None:
        self.inner.close()


def _target(cluster: Cluster, **more: str) -> Tuple[KafkaTarget, _FaultyBroker]:
    settings = KafkaSettings.parse(cluster.bootstrap, _options(cluster, **more))
    real = ConfluentBroker(settings.client(), lambda: Credentials(), lambda: [])
    broker = _FaultyBroker(real)
    return KafkaTarget(settings, broker=broker, serdes=ConfluentSerdes()), broker  # type: ignore[arg-type]


def test_a_dead_producer_and_an_aborted_one_leave_nothing_and_a_clean_delivery_leaves_each_record_once(
    cluster: Cluster, kept: Kept
) -> None:
    prefix = {"topic-prefix": "once."}
    expected = {f"once.{expose}": kept.expected(expose) for expose in EXPOSES}
    counts = {topic: len(records) for topic, records in expected.items()}
    target, broker = _target(cluster, **prefix)

    broker.faults = [(50, True)]
    with pytest.raises(FluxError):
        deliver_run(kept.layout, kept.bundle, target=target)
    assert broker.zombies[0].dead, "the producer died with its transaction open"
    assert all(not found for found in read_back(cluster, counts, settle=0, patience=8).values())

    broker.faults = [(70, False)]
    with pytest.raises(FluxError):
        deliver_run(kept.layout, kept.bundle, target=target)
    assert all(not found for found in read_back(cluster, counts, settle=0, patience=8).values())

    receipts = deliver_run(kept.layout, kept.bundle, target=target)
    found = read_back(cluster, counts)
    for topic, records in expected.items():
        _check_topic(found[topic], records)
    assert all(r.writer["skipped"] == 0 and r.writer["transactions"] == 1 for r in receipts)

    again = deliver_run(kept.layout, kept.bundle, target=target)
    assert all(r.writer["records"] == 0 and r.writer["transactions"] == 0 for r in again)
    assert {r.table: r.writer["skipped"] for r in again} == counts
    after = read_back(cluster, counts)
    assert {topic: len(messages) for topic, messages in after.items()} == counts


def test_a_paced_delivery_commits_as_it_goes_and_a_resume_sends_the_rest_once(
    cluster: Cluster, kept: Kept
) -> None:
    expected = kept.expected("site")
    counts = {"paced.site": len(expected)}
    target, broker = _target(
        cluster, **{"topic-prefix": "paced.", "pace": "x100000000", "commit-every": "0.05"}
    )
    # The first producer of the run belongs to device_change; the site stream is the last one, so
    # fail the sixth producer: it is the one for `site`.
    broker.faults = [(1_000_000, False)] * 5 + [(30, False)]
    with pytest.raises(FluxError):
        deliver_run(kept.layout, kept.bundle, target=target)
    partial = read_back(cluster, counts, settle=0, patience=15)["paced.site"]
    assert 0 < len(partial) < len(expected), "a paced stream commits before it is finished"
    broker.faults = []
    receipts = deliver_run(kept.layout, kept.bundle, target=target)
    site = next(r for r in receipts if r.expose_id == "site")
    assert site.writer["skipped"] == len(partial)
    _check_topic(read_back(cluster, counts)["paced.site"], expected)


# -- (3) values decode through the registry ----------------------------------------------


def test_every_avro_value_decodes_through_the_registry_to_what_was_sent(
    cluster: Cluster, kept: Kept
) -> None:
    prefix = "avro."
    target, _ = _target(cluster, **{"topic-prefix": prefix})
    deliver_run(kept.layout, kept.bundle, target=target)
    expected = {f"{prefix}{expose}": kept.expected(expose) for expose in EXPOSES}
    found = read_back(cluster, {topic: len(records) for topic, records in expected.items()})
    decode = AvroDeserializer(SchemaRegistryClient({"url": cluster.registry}))
    registry = SchemaRegistryClient({"url": cluster.registry})
    for topic, records in expected.items():
        assert registry.get_latest_version(f"{topic}-value").schema.schema_type == "AVRO"
        by_key: Dict[bytes, List[Message]] = {}
        for message in found[topic]:
            by_key.setdefault(message.key, []).append(message)
        sent: Dict[bytes, List[KafkaRecord]] = {}
        for record in records:
            sent.setdefault(record.key, []).append(record)
        for key, chain in sent.items():
            for message, record in zip(by_key[key], chain, strict=True):
                if record.value is None:
                    assert message.value is None
                    continue
                assert message.value is not None and message.value[0] == 0
                decoded = decode(message.value, SerializationContext(topic, MessageField.VALUE))
                assert decoded is not None
                _same(decoded, record.value)


def test_a_json_schema_topic_decodes_through_the_registry_to_what_was_sent(
    cluster: Cluster, kept: Kept
) -> None:
    prefix = "jsonschema."
    target, _ = _target(cluster, **{"topic-prefix": prefix, "value-format": "json"})
    deliver_run(kept.layout, kept.bundle, target=target)
    expected = {
        f"{prefix}{expose}": kept.expected(expose) for expose in ("device_current", "reading")
    }
    found = read_back(cluster, {topic: len(records) for topic, records in expected.items()})
    registry = SchemaRegistryClient({"url": cluster.registry})
    for topic, records in expected.items():
        registered = registry.get_latest_version(f"{topic}-value")
        assert registered.schema.schema_type == "JSON"
        decode = JSONDeserializer(registered.schema.schema_str, schema_registry_client=registry)
        by_key: Dict[bytes, List[Message]] = {}
        for message in found[topic]:
            by_key.setdefault(message.key, []).append(message)
        sent: Dict[bytes, List[KafkaRecord]] = {}
        for record in records:
            sent.setdefault(record.key, []).append(record)
        for key, chain in sent.items():
            for message, record in zip(by_key[key], chain, strict=True):
                assert record.value is not None and message.value is not None
                assert decode(message.value, SerializationContext(topic, MessageField.VALUE))
                body = json.loads(message.value[5:], parse_float=Decimal)
                _same(body, record.value)
