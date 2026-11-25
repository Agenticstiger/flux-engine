# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink's real adapter against librdkafka's in-process mock cluster (ADR 0038).

No Docker and no broker process: ``test.mock.num.brokers`` makes one client host a small cluster
inside the process, which every other client reaches on 127.0.0.1. The mock supports transactions,
the idempotent producer, murmur2 partitioning and read-committed consumers, so the delivery of the
devices run goes through the real :class:`~flux_engine.sinks._kafka_broker.ConfluentBroker` and is
read back and compared with what the in-memory broker holds for the same run.

The mock does not answer ``CreateTopics`` or ``DescribeConfigs`` (the first fails, the second never
returns), and it does not keep a consumer group's committed offsets, so the topics are described
from a stand-in and no delivery resumes here: those parts stay with the Docker tests of
``test_kafka_integration.py`` and the in-memory broker's tests.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterator, List, Sequence, Tuple

import pytest

pytest.importorskip("confluent_kafka")

from confluent_kafka import Consumer, Producer, TopicPartition  # noqa: E402

from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.sinks._kafka_broker import ConfluentBroker, Credentials, TopicInfo  # noqa: E402
from flux_engine.sinks.kafka import KafkaSettings, KafkaTarget  # noqa: E402
from flux_engine.stages.deliver import deliver_run  # noqa: E402

from ._kafka_fakes import DEFAULT_CONFIGS, FakeBroker, FakeClock, FakeSerdes  # noqa: E402
from .test_kafka_sink import (  # noqa: E402
    TOPICS,
    WALL_2026,
    Kept,
    _header,
    _kept,
    _sink,
    deliver,
)

pytestmark = pytest.mark.unit

PARTITIONS = 4
Row = Tuple[int, bytes, Any, int, Tuple[Tuple[str, bytes], ...]]


class _MockedAdmin(ConfluentBroker):
    """The real adapter, except that the topic is described from a stand-in (see the module)."""

    def describe(self, topic: str) -> TopicInfo:
        # Asking for a topic's metadata makes the mock cluster create it, with four partitions.
        found = self._admin_client().list_topics(topic=topic, timeout=20).topics[topic]
        assert len(found.partitions) == PARTITIONS
        configs = dict(DEFAULT_CONFIGS)
        if topic == "sim.device_current":
            configs["cleanup.policy"] = "compact"  # the snapshot's topic
        return TopicInfo(partitions=PARTITIONS, configs=configs)


@pytest.fixture(scope="module")
def bootstrap() -> Iterator[str]:
    holder = Producer({"test.mock.num.brokers": 3, "enable.metrics.push": False})
    brokers = holder.list_topics(timeout=10).brokers.values()
    yield ",".join(f"{broker.host}:{broker.port}" for broker in brokers)
    del holder


@pytest.fixture(scope="module")
def kept(tmp_path_factory: pytest.TempPathFactory) -> Kept:
    return _kept(tmp_path_factory.mktemp("mock-cluster"), pinned=True)


def _target(bootstrap: str) -> KafkaTarget:
    settings = KafkaSettings.parse(bootstrap, {"audience": "internal", "timeout": "20"})
    broker = _MockedAdmin(settings.client(), lambda: Credentials(), lambda: [])
    return KafkaTarget(
        settings, broker=broker, serdes=FakeSerdes(), clock=FakeClock(WALL_2026).clock()
    )


def _read_committed(bootstrap: str, topic: str, expected: int) -> List[Row]:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"reader-{topic}",
            "enable.auto.commit": False,
            "isolation.level": "read_committed",
            "enable.metrics.push": False,
        }
    )
    try:
        consumer.assign([TopicPartition(topic, p, 0) for p in range(PARTITIONS)])
        found: List[Row] = []
        deadline = time.monotonic() + 30
        while len(found) < expected and time.monotonic() < deadline:
            message = consumer.poll(0.5)
            if message is None or message.error():
                continue
            headers = tuple((name, bytes(value)) for name, value in (message.headers() or []))
            found.append(
                (
                    message.partition(),
                    message.key(),
                    message.value(),
                    message.timestamp()[1],
                    headers,
                )
            )
        return found
    finally:
        consumer.close()


def _normal(rows: Sequence[Row]) -> List[Row]:
    return sorted(rows, key=repr)


def test_a_run_delivered_through_the_real_adapter_reads_back_as_the_in_memory_broker_holds_it(
    kept: Kept, bootstrap: str
) -> None:
    receipts = deliver_run(kept.layout, kept.bundle, target=_target(bootstrap))
    reference = FakeBroker(partitions=PARTITIONS)
    deliver(kept, reference)
    assert len(receipts) == len(TOPICS)
    for topic in TOPICS:
        wanted = [
            (r.partition, r.key, r.value, r.timestamp, r.headers) for r in reference.read(topic)
        ]
        got = _read_committed(bootstrap, topic, len(wanted))
        assert len(got) == len(wanted), topic
        assert _normal(got) == _normal(wanted), topic


def test_a_record_at_the_epoch_is_refused_before_anything_is_sent(
    kept: Kept, bootstrap: str
) -> None:
    sink = _sink(kept, _target(bootstrap), "site")
    sink.open(_header(sink))
    with pytest.raises(FluxError) as caught:
        sink.write([1, "eu", "1970-01-01"])
    assert caught.value.code.code == "FLX-3514"
    sink.close()
    assert sink.receipt is None


def test_the_progress_of_a_group_the_mock_does_not_keep_reads_as_no_progress(
    bootstrap: str,
) -> None:
    settings = KafkaSettings.parse(bootstrap, {"audience": "internal", "timeout": "20"})
    broker = ConfluentBroker(settings.client(), lambda: Credentials(), lambda: [])
    _MockedAdmin(settings.client(), lambda: Credentials(), lambda: []).describe("sim.progress")
    assert broker.progress("flux-sink.no-such-run", "sim.progress") is None
    assert set(broker.versions()) == {"confluentKafka", "librdkafka"}


def test_the_clients_carry_no_metrics_push_setting_the_library_rejects(bootstrap: str) -> None:
    settings = KafkaSettings.parse(bootstrap, {"audience": "internal"})
    broker = ConfluentBroker(settings.client(), lambda: Credentials(), lambda: [])
    producer = broker.transactional_producer("flux-sink.conf-check", "flux-sink.conf-check")
    producer.close()
    conf: Dict[str, Any] = broker._conf()
    assert conf["enable.metrics.push"] is False
