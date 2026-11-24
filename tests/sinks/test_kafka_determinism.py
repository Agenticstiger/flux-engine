# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Kafka stream does not depend on how it was made or how fast it was sent (ADR 0038 section 10).

The devices bundle is run through the engine's run stage with ``--jobs 1`` and ``--jobs 2``, and
delivered to an in-memory broker at ``pace=max`` and at a pace of a million times real time, with
a fake clock. For every topic, the records of each key (key, value bytes, timestamp, headers) are
the same in all four deliveries, in the same order, and the topic holds the same records as a
multiset.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

import pytest

from flux_engine.sinks import registry
from flux_engine.sinks.kafka import KafkaSettings, KafkaTarget
from flux_engine.stages.run import run

from ._kafka_fakes import FakeBroker, FakeClock, FakeSerdes, Stored

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROFILE = FIXTURES / "kafka-devices.profile.json"
CASES = [(1, "max"), (2, "max"), (1, "x1000000"), (2, "x1000000")]

#: ``(key, value, timestamp, headers)``: a record without the partition it was placed in.
Entry = Tuple[bytes, Any, int, Tuple[Tuple[str, bytes], ...]]


def _delivered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobs: int, pace: str) -> FakeBroker:
    bundle = tmp_path / "bundle"
    shutil.copytree(FIXTURES / "kafka-devices", bundle)
    broker = FakeBroker()
    clock = FakeClock(1_790_000_000_000)

    def factory(target: str, options: Mapping[str, str]) -> KafkaTarget:
        return KafkaTarget(
            KafkaSettings.parse(target, options),
            broker=broker,
            serdes=FakeSerdes(),
            clock=clock.clock(),
        )

    monkeypatch.setattr(registry, "load_factory", lambda name: factory)
    result = run(
        bundle,
        state_dir=tmp_path / "state",
        profile=PROFILE,
        sink="kafka:127.0.0.1:9092",
        sink_options={"audience": "internal", "pace": pace},
        jobs=jobs,
    )
    assert result.success, result.stderr
    return broker


def _entries(broker: FakeBroker) -> Dict[str, Dict[bytes, List[Entry]]]:
    found: Dict[str, Dict[bytes, List[Entry]]] = {}
    for topic in broker.topics:
        keys: Dict[bytes, List[Entry]] = {}
        for record in broker.read(topic):
            keys.setdefault(record.key, []).append(_entry(record))
        found[topic] = keys
    return found


def _entry(record: Stored) -> Entry:
    return (record.key, record.value, record.timestamp, record.headers)


def test_keys_values_timestamps_and_headers_do_not_depend_on_jobs_or_pace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = []
    for index, (jobs, pace) in enumerate(CASES):
        root = tmp_path / f"case{index}"
        root.mkdir()
        runs.append(_entries(_delivered(root, monkeypatch, jobs, pace)))
    first = runs[0]
    assert len(first) == 6 and all(first.values())
    for other in runs[1:]:
        assert other == first, "the records of every key are the same, in the same order"
        for topic in first:
            assert sorted(
                (e for chain in other[topic].values() for e in chain), key=repr
            ) == sorted((e for chain in first[topic].values() for e in chain), key=repr)


def test_a_paced_delivery_is_in_event_time_and_holds_the_same_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("fast", "plain"):
        (tmp_path / name).mkdir()
    fast = _delivered(tmp_path / "fast", monkeypatch, 1, "x1000000")
    plain = _delivered(tmp_path / "plain", monkeypatch, 1, "max")
    stamps = [r.timestamp for r in fast.read("sim.reading")]
    assert stamps == sorted(stamps)
    assert sorted(fast.read("sim.reading"), key=repr) == sorted(plain.read("sim.reading"), key=repr)
