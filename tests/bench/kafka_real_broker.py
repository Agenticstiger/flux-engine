# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink against a real broker and registry: records and megabytes per second.

Run it from the repository root, with the ``[kafka]`` extra and Docker::

    python -m tests.bench.kafka_real_broker [--rows N] [--compression CODEC] [--runs N]

It starts one Apache Kafka node (KRaft) and one Karapace registry on a Docker network of their own,
scales the devices bundle so its change events number about 200,000 (``--rows`` is the
``device_version`` row count, 190,000 by default), plans and applies it once, and then delivers
at ``pace=max``:

* the events stream alone, as plain JSON and as Avro through the registry, ``--runs`` times each
  on a fresh topic prefix: records per second and megabytes per second of the values, from
  the first row to the commit that makes the stream readable;
* every stream of the bundle, as plain JSON and as Avro: the wall time of the delivery, and with
  the plan and apply time, of the whole run.

It prints one table and removes the containers, the volumes and the network it started, also when
it fails or is interrupted. ``--compression`` is passed to the sink when given (the sink's
default otherwise), so the same command runs against an older checkout that has no such option.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from confluent_kafka.admin import AdminClient

from flux_engine.sinks._kafka_broker import ConfluentBroker, Credentials
from flux_engine.sinks._kafka_serdes import ConfluentSerdes
from flux_engine.sinks.kafka import KafkaSettings, KafkaTarget
from flux_engine.stages.deliver import deliver_run

from ._kafka_streams import Prepared, generate

KAFKA_IMAGE = "apache/kafka:4.4.0"
REGISTRY_IMAGE = "ghcr.io/aiven-open/karapace:6.2.4"
EVENTS = "device_change"


def _docker(*args: str, timeout: int = 300) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=timeout)


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _wait(what: str, ready: Any, seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if ready():
                return
        except Exception:  # noqa: BLE001 - a service that is still starting refuses or resets
            pass
        time.sleep(1)
    raise SystemExit(f"{what} did not become ready in {seconds} s")


class Cluster:
    """One Kafka node and one registry in Docker, removed on exit."""

    def __enter__(self) -> "Cluster":
        tag = uuid.uuid4().hex[:10]
        self.network, self.broker, self.registry = (
            f"flux-bench-{kind}-{tag}" for kind in ("net", "kafka", "registry")
        )
        port, registry_port = _free_port(), _free_port()
        self.bootstrap = f"127.0.0.1:{port}"
        self.url = f"http://127.0.0.1:{registry_port}"
        try:
            _docker("network", "create", self.network)
            _docker(
                "run", "-d", "--name", self.broker, "--network", self.network,
                "-p", f"127.0.0.1:{port}:{port}",
                "-e", "KAFKA_NODE_ID=1",
                "-e", "KAFKA_PROCESS_ROLES=broker,controller",
                "-e", f"KAFKA_LISTENERS=INTERNAL://:19092,EXTERNAL://:{port},CONTROLLER://:9093",
                "-e", f"KAFKA_ADVERTISED_LISTENERS=INTERNAL://{self.broker}:19092,EXTERNAL://127.0.0.1:{port}",
                "-e", "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=INTERNAL:PLAINTEXT,EXTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT",
                "-e", "KAFKA_INTER_BROKER_LISTENER_NAME=INTERNAL",
                "-e", "KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER",
                "-e", f"KAFKA_CONTROLLER_QUORUM_VOTERS=1@{self.broker}:9093",
                "-e", "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1",
                "-e", "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1",
                "-e", "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1",
                "-e", "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0",
                "-e", "KAFKA_NUM_PARTITIONS=3",
                "-e", "KAFKA_AUTO_CREATE_TOPICS_ENABLE=false",
                "-e", "KAFKA_MESSAGE_TIMESTAMP_AFTER_MAX_MS=9223372036854775807",
                KAFKA_IMAGE,
            )  # fmt: skip
            _docker(
                "run", "-d", "--name", self.registry, "--network", self.network,
                "-p", f"127.0.0.1:{registry_port}:8081",
                "-e", f"KARAPACE_BOOTSTRAP_URI={self.broker}:19092",
                "-e", "KARAPACE_HOST=0.0.0.0",
                "-e", "KARAPACE_PORT=8081",
                "-e", f"KARAPACE_ADVERTISED_HOSTNAME={self.registry}",
                "-e", "KARAPACE_TOPIC_NAME=_schemas",
                "-e", "KARAPACE_GROUP_ID=karapace",
                "-e", "KARAPACE_MASTER_ELIGIBILITY=true",
                "-e", "KARAPACE_LOG_LEVEL=WARNING",
                REGISTRY_IMAGE, "python3", "-m", "karapace",
            )  # fmt: skip
            admin = AdminClient({"bootstrap.servers": self.bootstrap, "log_level": 0})
            _wait("Kafka", lambda: admin.list_topics(timeout=3) is not None, 120)

            def registry_ready() -> bool:
                with urllib.request.urlopen(f"{self.url}/subjects", timeout=3) as reply:
                    return bool(reply.status == 200)

            _wait("the registry", registry_ready, 180)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_: object) -> None:
        subprocess.run(
            ["docker", "rm", "-f", "-v", self.registry, self.broker], capture_output=True
        )
        subprocess.run(["docker", "network", "rm", self.network], capture_output=True)


class _CountingTxn:
    """A producer that counts the bytes of the records it is given, and passes them on."""

    def __init__(self, inner: Any, owner: "CountingBroker") -> None:
        self._inner = inner
        self._owner = owner

    def begin(self) -> None:
        self._inner.begin()

    def produce(
        self,
        topic: str,
        key: bytes,
        value: Optional[bytes],
        timestamp_ms: int,
        headers: Sequence[Tuple[str, bytes]],
    ) -> None:
        self._owner.key_bytes += len(key)
        self._owner.value_bytes += 0 if value is None else len(value)
        self._inner.produce(topic, key, value, timestamp_ms, headers)

    def commit(self, *, topic: str, records: int, metadata: str) -> None:
        self._inner.commit(topic=topic, records=records, metadata=metadata)

    def abort(self) -> None:
        self._inner.abort()

    def close(self) -> None:
        self._inner.close()


class CountingBroker:
    """The sink's broker adapter, counting the bytes of every key and value it sends."""

    def __init__(self, inner: ConfluentBroker) -> None:
        self._inner = inner
        self.key_bytes = 0
        self.value_bytes = 0

    def describe(self, topic: str) -> Any:
        return self._inner.describe(topic)

    def create(self, topic: str, **kwargs: Any) -> Any:
        return self._inner.create(topic, **kwargs)

    def progress(self, group: str, topic: str) -> Optional[str]:
        return self._inner.progress(group, topic)

    def transactional_producer(self, transactional_id: str, group: str, **kwargs: Any) -> Any:
        return _CountingTxn(
            self._inner.transactional_producer(transactional_id, group, **kwargs), self
        )

    def versions(self) -> Mapping[str, str]:
        return self._inner.versions()

    def close(self) -> None:
        self._inner.close()


def _target(cluster: Cluster, options: Mapping[str, str]) -> Tuple[KafkaTarget, CountingBroker]:
    settings = KafkaSettings.parse(
        cluster.bootstrap, {"audience": "internal", "timeout": "120", **options}
    )
    broker = CountingBroker(ConfluentBroker(settings.client(), lambda: Credentials(), lambda: []))
    return KafkaTarget(settings, broker=broker, serdes=ConfluentSerdes()), broker


def events_only(
    cluster: Cluster, prepared: Prepared, options: Mapping[str, str]
) -> Dict[str, float]:
    """Deliver the events stream through one sink; records, seconds and value bytes."""
    stream = prepared.stream(EVENTS)
    target, counted = _target(cluster, options)
    started, cpu_started = time.perf_counter(), time.process_time()
    sink = target.sink_for(stream.context)
    sink.open(stream.header)
    for row in stream.rows:
        sink.write(row)
    sink.close()
    seconds, cpu = time.perf_counter() - started, time.process_time() - cpu_started
    assert sink.receipt is not None
    records = int(sink.receipt.writer["records"])
    return {
        "rows": len(stream.rows),
        "records": records,
        "seconds": seconds,
        "cpu": cpu,
        "bytes": counted.value_bytes,
        "keys": counted.key_bytes,
    }


def whole_bundle(
    cluster: Cluster, prepared: Prepared, options: Mapping[str, str]
) -> Dict[str, float]:
    """Deliver every stream of the run through one sink; the records and the seconds."""
    target, _ = _target(cluster, options)
    started = time.perf_counter()
    receipts = deliver_run(prepared.layout, prepared.bundle, target=target)
    seconds = time.perf_counter() - started
    return {
        "records": float(sum(int(r.writer["records"]) for r in receipts)),
        "seconds": seconds,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.bench.kafka_real_broker")
    parser.add_argument("--rows", type=int, default=190_000)
    parser.add_argument("--compression", default=None)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    common: Dict[str, str] = {} if args.compression is None else {"compression": args.compression}
    with tempfile.TemporaryDirectory(prefix="flux-kafka-bench-") as scratch, Cluster() as cluster:
        started = time.perf_counter()
        prepared = generate(Path(scratch), args.rows)
        plan_apply = time.perf_counter() - started
        registry = {"registry": cluster.url}
        cases: List[Tuple[str, Dict[str, str]]] = [
            ("json", {**common}),
            ("avro", {**common, **registry}),
        ]
        print(
            f"compression: {args.compression or 'sink default'}; plan + generate + apply: {plan_apply:.1f} s"
        )
        print(
            f"{'case':<28}{'records':>10}{'seconds':>9}{'cpu s':>8}{'records/s':>11}{'value MB/s':>12}"
        )
        results: Dict[str, Dict[str, float]] = {}
        for name, options in cases:
            best: Optional[Dict[str, float]] = None
            for run_number in range(args.runs):
                prefix = f"ev{run_number}{name}{uuid.uuid4().hex[:6]}."
                found = events_only(cluster, prepared, {**options, "topic-prefix": prefix})
                if best is None or found["seconds"] < best["seconds"]:
                    best = found
            assert best is not None
            results[name] = best
            print(
                f"{'events, ' + name + ' (best of ' + str(args.runs) + ')':<28}"
                f"{int(best['records']):>10}{best['seconds']:>9.2f}{best['cpu']:>8.2f}"
                f"{best['records'] / best['seconds']:>11,.0f}"
                f"{best['bytes'] / best['seconds'] / 1e6:>12.1f}"
            )
        for name, options in cases:
            prefix = f"all{name}{uuid.uuid4().hex[:6]}."
            whole = whole_bundle(cluster, prepared, {**options, "topic-prefix": prefix})
            print(
                f"{'every stream, ' + name:<28}{int(whole['records']):>10}{whole['seconds']:>9.2f}"
                f"{'':>8}{whole['records'] / whole['seconds']:>11,.0f}"
                f"{'':>12}  plan+apply+deliver {plan_apply + whole['seconds']:.1f} s"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
