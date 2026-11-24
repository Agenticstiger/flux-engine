# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink on the devices bundle, against an in-memory broker (ADR 0038).

No confluent_kafka is needed: the sink runs on :class:`FakeBroker`, which keeps transactions,
partitions by Java's murmur2, committed progress and fencing. The Docker tests of the real client
are in ``test_kafka_integration.py``.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from flux_engine.api.emit import EmitHeader
from flux_engine.core.digest import DigestRef, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.sinks import kafka
from flux_engine.sinks._kafka_broker import ClientSettings, ConfluentBroker, Credentials
from flux_engine.sinks._kafka_records import KafkaRecord
from flux_engine.sinks.kafka import Clock, KafkaSettings, KafkaSink, KafkaTarget
from flux_engine.sinks.receipts import RecordedGrade, SinkReceipt, StreamContext
from flux_engine.sinks.registry import known_of, open_target
from flux_engine.stages.deliver import deliver_run
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.run import run

from ._kafka_fakes import (
    Failure,
    FakeBroker,
    FakeClock,
    FakeSerdes,
    Stored,
    java_partition,
    murmur2,
)
from .test_kafka_shape import GLUE_BINDING, KAFKA_BINDING, swap
from .test_kafka_shape import bundle as shape_bundle

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DEVICES = FIXTURES / "kafka-devices"
PROFILE = FIXTURES / "kafka-devices.profile.json"
SIMULATION = "sim-devices"
TOPICS = (
    "sim.device_change",
    "sim.device_current",
    "sim.device_month",
    "sim.device_version",
    "sim.reading",
    "sim.site",
)
BOOTSTRAP = "127.0.0.1:9092"
INTERNAL: Mapping[str, str] = {"audience": "internal"}
#: The devices run's event times span 2023 to 2026; a clock in 2026 keeps them inside the limits.
WALL_2026 = 1_790_000_000_000


@dataclass(frozen=True)
class Kept:
    bundle: Path
    layout: RunLayout


def _kept(root: Path, *, pinned: bool) -> Kept:
    bundle = root / "bundle"
    shutil.copytree(DEVICES, bundle)
    if not pinned:
        path = bundle / "simulation.flux.yml"
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text[: text.index("    profile:\n")] + text[text.index("    history:") :],
            encoding="utf-8",
        )
    result = run(bundle, state_dir=root / "state", profile=PROFILE if pinned else None)
    assert result.success, result.stderr
    return Kept(bundle, RunLayout.for_state_dir(root / "state", result.artifacts["plan"]["run_id"]))


@pytest.fixture(scope="module")
def kept(tmp_path_factory: pytest.TempPathFactory) -> Kept:
    return _kept(tmp_path_factory.mktemp("pinned"), pinned=True)


@pytest.fixture(scope="module")
def unpinned(tmp_path_factory: pytest.TempPathFactory) -> Kept:
    return _kept(tmp_path_factory.mktemp("unpinned"), pinned=False)


def target_of(
    broker: FakeBroker,
    options: Optional[Mapping[str, str]] = None,
    *,
    clock: Optional[FakeClock] = None,
    serdes: Optional[FakeSerdes] = None,
) -> KafkaTarget:
    settings = KafkaSettings.parse(BOOTSTRAP, {**INTERNAL, **(options or {})})
    fake = clock or FakeClock(WALL_2026)
    return KafkaTarget(settings, broker=broker, serdes=serdes or FakeSerdes(), clock=fake.clock())


def deliver(
    kept: Kept, broker: FakeBroker, options: Optional[Mapping[str, str]] = None, **more: Any
) -> Sequence[SinkReceipt]:
    return deliver_run(kept.layout, kept.bundle, target=target_of(broker, options, **more))


def doc(stored: Stored) -> Dict[str, Any]:
    assert stored.value is not None
    value: Dict[str, Any] = json.loads(stored.value)
    return value


def visible(broker: FakeBroker) -> Dict[str, List[Stored]]:
    return {topic: broker.read(topic) for topic in TOPICS if broker.read(topic)}


@pytest.fixture(scope="module")
def reference(kept: Kept) -> Dict[str, List[Stored]]:
    """What one clean delivery leaves on every topic."""
    broker = FakeBroker()
    deliver(kept, broker)
    return visible(broker)


# -- options -----------------------------------------------------------------------


def test_the_defaults_and_every_option_are_read() -> None:
    parsed = KafkaSettings.parse(BOOTSTRAP, INTERNAL)
    assert parsed.audience == "internal" and parsed.shape.topic_prefix == "sim."
    assert parsed.pace.text == "max" and parsed.commit_every == 1.0 and parsed.timeout == 30.0
    assert parsed.registry is None and parsed.security_protocol == "PLAINTEXT"
    full = KafkaSettings.parse(
        "a.example.com:9092,[::1]:9093",
        {
            "audience": "external",
            "topic-prefix": "staging.",
            "topic.site": "places",
            "key.reading": "reading_id,device_id",
            "key-format": "json",
            "timestamp.site": "opened_on",
            "registry": "https://registry.example.com:8081/",
            "registry-auth-env": "REGISTRY_LOGIN",
            "registry-ca-location": "/etc/ca.pem",
            "value-format": "json",
            "pace": "x2",
            "commit-every": "0.5",
            "partitions": "6",
            "replication-factor": "3",
            "timeout": "10",
            "security-protocol": "SASL_SSL",
            "sasl-mechanism": "SCRAM-SHA-256",
            "sasl-username-env": "KAFKA_USER",
            "sasl-password-env": "KAFKA_PASS",
            "ssl-ca-location": "/etc/ca.pem",
        },
    )
    assert full.shape.topics == {"site": "places"}
    assert full.shape.keys == {"reading": ("reading_id", "device_id")}
    assert full.shape.timestamps == {"site": "opened_on"} and full.shape.key_format == "json"
    assert full.registry == "https://registry.example.com:8081"
    assert full.value_format == "json" and full.pace.text == "x2" and full.commit_every == 0.5
    assert (full.partitions, full.replication_factor, full.timeout) == (6, 3, 10.0)
    assert full.sasl_mechanism == "SCRAM-SHA-256" and full.uses_sasl


REFUSED: Sequence[Any] = [
    ("", {"audience": "internal"}, "needs a target"),
    ("localhost", {"audience": "internal"}, "HOST:PORT"),
    ("localhost:0", {"audience": "internal"}, "HOST:PORT"),
    ("localhost:70000", {"audience": "internal"}, "HOST:PORT"),
    ("kafka://localhost:9092", {"audience": "internal"}, "HOST:PORT"),
    ("user@localhost:9092", {"audience": "internal"}, "HOST:PORT"),
    ("localhost:9092/path", {"audience": "internal"}, "HOST:PORT"),
    ("[::1:9092", {"audience": "internal"}, "HOST:PORT"),
    ("bad_host:9092", {"audience": "internal"}, "HOST:PORT"),
    (BOOTSTRAP, {}, "audience=internal"),
    (BOOTSTRAP, {"audience": "public"}, "audience"),
    (BOOTSTRAP, {"audience": "internal", "retries": "3"}, "takes no option retries"),
    (BOOTSTRAP, {"audience": "internal", "topic-prefix": ""}, "topic-prefix"),
    (BOOTSTRAP, {"audience": "internal", "topic.": "x"}, "needs an expose id"),
    (BOOTSTRAP, {"audience": "internal", "key.site": " , "}, "names no column"),
    (BOOTSTRAP, {"audience": "internal", "key-format": "xml"}, "key-format"),
    (BOOTSTRAP, {"audience": "internal", "pace": "fast"}, "pace"),
    (BOOTSTRAP, {"audience": "internal", "commit-every": "0"}, "commit-every"),
    (BOOTSTRAP, {"audience": "internal", "timeout": "abc"}, "timeout"),
    (BOOTSTRAP, {"audience": "internal", "partitions": "0"}, "partitions"),
    (BOOTSTRAP, {"audience": "internal", "replication-factor": "-1"}, "replication-factor"),
    (BOOTSTRAP, {"audience": "internal", "registry": "ftp://r.example.com"}, "registry"),
    (BOOTSTRAP, {"audience": "internal", "registry": "http://u:p@r.example.com"}, "registry"),
    (BOOTSTRAP, {"audience": "internal", "registry": "http://r.example.com?x=1"}, "registry"),
    (BOOTSTRAP, {"audience": "internal", "registry": "http://r.example.com#x"}, "registry"),
    (BOOTSTRAP, {"audience": "internal", "registry-auth-env": "X"}, "needs --sink-opt registry"),
    (BOOTSTRAP, {"audience": "internal", "value-format": "json"}, "needs --sink-opt registry"),
    (
        BOOTSTRAP,
        {"audience": "internal", "registry": "http://r.example.com", "registry-auth-env": "X"},
        "only over https",
    ),
    (
        BOOTSTRAP,
        {"audience": "internal", "registry": "http://r.example.com", "registry-ca-location": "x"},
        "https registry",
    ),
    (
        BOOTSTRAP,
        {"audience": "internal", "registry": "http://localhost:8081", "value-format": "yaml"},
        "value-format",
    ),
    (BOOTSTRAP, {"audience": "internal", "sasl-username-env": "U"}, "together"),
    (BOOTSTRAP, {"audience": "internal", "security-protocol": "TLS"}, "security-protocol"),
    (BOOTSTRAP, {"audience": "internal", "security-protocol": "SASL_SSL"}, "sasl-username-env"),
    (BOOTSTRAP, {"audience": "internal", "sasl-mechanism": "PLAIN"}, "sasl-"),
    (
        "broker.example.com:9092",
        {
            "audience": "internal",
            "sasl-username-env": "U",
            "sasl-password-env": "P",
            "security-protocol": "SASL_PLAINTEXT",
        },
        "only over TLS",
    ),
    (
        BOOTSTRAP,
        {
            "audience": "internal",
            "sasl-username-env": "U",
            "sasl-password-env": "P",
            "security-protocol": "SSL",
        },
        "SASL_SSL",
    ),
    (
        BOOTSTRAP,
        {
            "audience": "internal",
            "sasl-username-env": "U",
            "sasl-password-env": "P",
            "sasl-mechanism": "GSSAPI",
        },
        "sasl-mechanism",
    ),
    (BOOTSTRAP, {"audience": "internal", "ssl-ca-location": "/x.pem"}, "TLS"),
]


@pytest.mark.parametrize(("target", "options", "fragment"), REFUSED)
def test_every_option_that_cannot_be_used_is_refused_with_the_option_named(
    target: str, options: Mapping[str, str], fragment: str
) -> None:
    with pytest.raises(FluxError) as caught:
        KafkaSettings.parse(target, options)
    assert caught.value.code.code == "FLX-3504"
    assert fragment in str(caught.value)


def test_a_credential_on_this_machine_may_travel_without_tls() -> None:
    parsed = KafkaSettings.parse(
        "localhost:9092",
        {
            "audience": "internal",
            "sasl-username-env": "U",
            "sasl-password-env": "P",
            "security-protocol": "SASL_PLAINTEXT",
            "registry": "http://127.0.0.1:8081",
            "registry-auth-env": "R",
        },
    )
    assert parsed.security_protocol == "SASL_PLAINTEXT" and parsed.sasl_mechanism == "PLAIN"


@pytest.mark.parametrize(
    "option",
    ["password", "sasl-password", "token", "access-key", "secret", "registry-password"],
)
def test_a_credential_option_is_refused_by_the_registry(option: str) -> None:
    with pytest.raises(FluxError) as caught:
        open_target(f"kafka:{BOOTSTRAP}", {"audience": "internal", option: "hunter2hunter2"})
    assert caught.value.code.code == "FLX-3508"
    assert "hunter2hunter2" not in str(caught.value)


def test_a_missing_extra_is_refused_before_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "confluent_kafka", None)
    with pytest.raises(FluxError) as caught:
        kafka.target(BOOTSTRAP, INTERNAL)
    assert caught.value.code.code == "FLX-3501"
    assert "flux-sim-engine[kafka]" in str(caught.value)


def test_the_factory_reads_no_environment_variable_and_contacts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("confluent_kafka")
    monkeypatch.delenv("FLUX_TEST_KAFKA_USER", raising=False)
    monkeypatch.delenv("FLUX_TEST_KAFKA_PASS", raising=False)
    built = kafka.target(
        "127.0.0.1:1",
        {
            "audience": "internal",
            "sasl-username-env": "FLUX_TEST_KAFKA_USER",
            "sasl-password-env": "FLUX_TEST_KAFKA_PASS",
            "security-protocol": "SASL_PLAINTEXT",
        },
    )
    assert built.settings.sasl_username_env == "FLUX_TEST_KAFKA_USER"


# -- the gate and the declarations come before anything is contacted ----------------


def test_an_external_audience_is_refused_on_a_checked_run_before_any_call(kept: Kept) -> None:
    broker = FakeBroker()
    with pytest.raises(FluxError) as caught:
        deliver(kept, broker, {"audience": "external"})
    assert caught.value.code.code == "FLX-3512"
    assert "P1" in str(caught.value) and "external" in str(caught.value)
    assert broker.calls == [] and broker.topics == {}


def test_a_run_that_records_no_grade_is_refused_for_every_audience_before_any_call(
    unpinned: Kept,
) -> None:
    for audience in ("internal", "external"):
        broker = FakeBroker()
        with pytest.raises(FluxError) as caught:
            deliver(unpinned, broker, {"audience": audience})
        assert caught.value.code.code == "FLX-3512"
        assert "records no privacy grade" in str(caught.value)
        assert broker.calls == []


def test_a_stream_handed_no_grade_or_no_bundle_is_refused_before_any_call(kept: Kept) -> None:
    broker = FakeBroker()
    target = target_of(broker)
    base = StreamContext(
        run_id="r",
        expose_id="site",
        rows=0,
        output_digest=digest_of_document({}, subject="outputDigest:site"),
    )
    with pytest.raises(FluxError) as no_grade:
        target.sink_for(base)
    assert no_grade.value.code.code == "FLX-3512"
    graded = RecordedGrade(grade="P1")
    with pytest.raises(FluxError) as no_bundle:
        target.sink_for(
            StreamContext(**{**base.__dict__, "privacy_grade": graded})  # type: ignore[arg-type]
        )
    assert no_bundle.value.code.code == "FLX-3504"
    assert "the engine to hand it the run's bundle" in str(no_bundle.value)
    with pytest.raises(FluxError) as unknown:
        target.sink_for(
            StreamContext(
                **{
                    **base.__dict__,
                    "privacy_grade": graded,
                    "bundle_directory": kept.bundle,
                    "simulation_id": SIMULATION,
                    "expose_id": "nonesuch",
                }
            )
        )
    assert unknown.value.code.code == "FLX-3504"
    assert broker.calls == []


def test_a_stream_the_declarations_cannot_shape_is_refused_before_any_call(
    kept: Kept, tmp_path: Path
) -> None:
    broker = FakeBroker()
    with pytest.raises(FluxError) as caught:
        deliver(kept, broker, {"topic.site": "same", "topic.reading": "same"})
    assert caught.value.code.code == "FLX-3513"
    assert broker.calls == []


def test_a_subject_without_a_registry_is_refused_before_any_call(
    kept: Kept, tmp_path: Path
) -> None:
    bundle = shape_bundle(tmp_path, contract=swap(GLUE_BINDING, KAFKA_BINDING % ("avro", "latest")))
    result = run(bundle, state_dir=tmp_path / "state", profile=PROFILE)
    assert result.success, result.stderr
    layout = RunLayout.for_state_dir(tmp_path / "state", result.artifacts["plan"]["run_id"])
    broker = FakeBroker()
    with pytest.raises(FluxError) as caught:
        deliver_run(layout, bundle, target=target_of(broker))
    assert caught.value.code.code == "FLX-3515"
    assert "site-value" in str(caught.value) and "registry=URL" in str(caught.value)
    assert broker.calls == []


# -- topics, keys, partitions --------------------------------------------------------


def test_every_stream_lands_on_its_topic_keyed_by_its_entity(
    kept: Kept, reference: Mapping[str, List[Stored]]
) -> None:
    assert sorted(reference) == list(TOPICS)
    key_of = {
        "sim.device_version": "device_id",
        "sim.device_current": "device_id",
        "sim.device_change": "device_id",
        "sim.device_month": "device_id",
        "sim.site": "site_id",
        "sim.reading": "reading_id",
    }
    for topic, records in reference.items():
        for record in records:
            if record.value is None:
                continue
            value = doc(record)
            fields = value.get("after") or value if "op" in value else value
            if topic == "sim.device_change":
                fields = value["after"] or value["before"]
            assert record.key == str(fields[key_of[topic]]).encode()


def test_partitions_are_javas_murmur2_over_the_key_bytes(
    reference: Mapping[str, List[Stored]],
) -> None:
    assert [java_partition(f"k{i}".encode(), 3) for i in range(8)] == [2, 2, 0, 1, 1, 0, 1, 1]
    assert murmur2(b"21") == murmur2(b"21") and java_partition(b"21", 1) == 0
    for records in reference.values():
        assert {record.partition for record in records} <= {0, 1, 2}
        assert all(record.partition == java_partition(record.key, 3) for record in records)


def test_one_key_stays_in_one_partition_in_the_streams_order(
    kept: Kept, reference: Mapping[str, List[Stored]]
) -> None:
    broker = FakeBroker()
    deliver(kept, broker)
    versions = broker.by_key("sim.device_version")
    for chain in versions.values():
        assert len({record.partition for record in chain}) == 1
        starts = [doc(record)["valid_from"] for record in chain]
        assert starts == sorted(starts)


def test_a_json_key_is_the_canonical_object_of_the_key_columns(kept: Kept) -> None:
    broker = FakeBroker()
    deliver(kept, broker, {"key-format": "json", "key.reading": "reading_id,device_id"})
    sample = broker.read("sim.reading")[0]
    value = doc(sample)
    assert (
        sample.key
        == json.dumps(
            {"device_id": value["device_id"], "reading_id": value["reading_id"]},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )


def test_the_topic_prefix_and_names_are_the_operators(kept: Kept) -> None:
    broker = FakeBroker()
    deliver(kept, broker, {"topic-prefix": "staging.", "topic.site": "places"})
    assert "staging.places" in broker.topics and "staging.site" not in broker.topics
    assert "staging.reading" in broker.topics and "sim.reading" not in broker.topics


def test_a_missing_topic_is_created_and_a_snapshot_topic_is_compacted(kept: Kept) -> None:
    broker = FakeBroker()
    deliver(kept, broker, {"partitions": "2", "replication-factor": "1"})
    assert broker.topics["sim.device_current"].configs["cleanup.policy"] == "compact"
    assert broker.topics["sim.reading"].configs["cleanup.policy"] == "delete"
    assert broker.topics["sim.reading"].partitions == 2
    assert {r.partition for r in broker.read("sim.reading")} <= {0, 1}


def test_an_existing_topic_is_used_as_it_is(kept: Kept) -> None:
    broker = FakeBroker()
    broker.add_topic("sim.reading", partitions=5)
    deliver(kept, broker)
    assert ("create", "sim.reading") not in broker.calls
    assert max(r.partition for r in broker.read("sim.reading")) <= 4
    assert all(r.partition == java_partition(r.key, 5) for r in broker.read("sim.reading"))


# -- Debezium's envelope ----------------------------------------------------------


def test_events_are_envelopes_with_a_tombstone_after_each_delete(
    reference: Mapping[str, List[Stored]],
) -> None:
    records = reference["sim.device_change"]
    ops = [doc(r)["op"] for r in records if r.value is not None]
    assert {"c", "u", "d"} <= set(ops)
    for index, record in enumerate(records):
        if record.value is None:
            continue
        value = doc(record)
        assert list(value) == ["after", "before", "op", "source", "ts_ms"]
        assert value["source"]["connector"] == "flux" and value["source"]["snapshot"] == "false"
        assert value["source"]["db"] == "fleet.devices"
        assert value["source"]["table"] == "device_version"
        assert value["source"]["name"] == SIMULATION and "event_id" in value["source"]
        if value["op"] == "c":
            assert value["before"] is None and value["after"] is not None
        if value["op"] == "u":
            assert value["before"] is not None and value["after"] is not None
        if value["op"] == "d":
            assert value["after"] is None and value["before"] is not None
            tombstone = records[index + 1]
            assert tombstone.value is None and tombstone.key == record.key
            assert tombstone.timestamp == record.timestamp and tombstone.headers == record.headers
        elif index + 1 < len(records) and records[index + 1].value is None:
            raise AssertionError("a tombstone follows something other than a delete")
    assert sum(1 for r in records if r.value is None) == ops.count("d")


def test_a_snapshot_is_a_read_of_the_table_on_a_compacted_topic(
    reference: Mapping[str, List[Stored]],
) -> None:
    records = reference["sim.device_current"]
    values = [doc(r) for r in records]
    assert {v["op"] for v in values} == {"r"} and all(v["before"] is None for v in values)
    assert [v["source"]["snapshot"] for v in values].count("last") == 1
    assert values[-1]["source"]["snapshot"] == "last"
    assert {v["source"]["snapshot"] for v in values[:-1]} == {"true"}
    assert set(values[0]["after"]) == {"device_id", "firmware", "status", "installed_on"}


def test_a_history_and_its_periods_are_written_as_rows(
    reference: Mapping[str, List[Stored]],
) -> None:
    version = doc(reference["sim.device_version"][0])
    assert list(version) == [
        "device_sk",
        "device_id",
        "site_id",
        "valid_from",
        "valid_to",
        "firmware",
        "status",
        "installed_on",
    ] or set(version) >= {"device_id", "valid_from"}
    assert "op" not in version
    month = doc(reference["sim.device_month"][0])
    assert "month" in month and "after" not in month


def test_timestamps_are_event_times_and_each_record_is_labelled(
    reference: Mapping[str, List[Stored]],
) -> None:
    first = reference["sim.device_version"][0]
    value = doc(first)
    from flux_engine.sinks._kafka_records import epoch_ms

    assert first.timestamp == epoch_ms(value["valid_from"], "DATE", column="valid_from", row=0)
    names = [name for name, _ in first.headers]
    assert names == [
        "flux.synthetic",
        "flux.run-id",
        "flux.expose",
        "flux.output-digest",
        "flux.row",
    ]
    assert dict(first.headers)["flux.synthetic"] == b"true"
    assert dict(first.headers)["flux.expose"] == b"device_version"


# -- topics that will not take the stream ------------------------------------------


def test_a_log_append_time_topic_is_refused(kept: Kept) -> None:
    broker = FakeBroker()
    broker.add_topic("sim.reading", message_timestamp_type="LogAppendTime")
    with pytest.raises(FluxError) as caught:
        deliver(kept, broker)
    assert caught.value.code.code == "FLX-3514"
    assert "sim.reading" in str(caught.value) and "LogAppendTime" in str(caught.value)
    assert broker.read("sim.reading") == []
    assert not any(
        call[0] == "producer" and call[1].endswith("sim.reading") for call in broker.calls
    )


def test_a_snapshot_topic_that_is_not_compacted_is_refused(kept: Kept) -> None:
    broker = FakeBroker()
    broker.add_topic("sim.device_current")
    with pytest.raises(FluxError) as caught:
        deliver(kept, broker)
    assert caught.value.code.code == "FLX-3514"
    assert "not compacted" in str(caught.value) and "sim.device_current" in str(caught.value)
    assert broker.read("sim.device_current") == []


def test_a_compacted_and_deleting_snapshot_topic_is_accepted(kept: Kept) -> None:
    broker = FakeBroker()
    broker.add_topic("sim.device_current", cleanup_policy="compact,delete")
    deliver(kept, broker)
    assert len(broker.read("sim.device_current")) == 60


def test_a_record_further_ahead_than_the_topic_allows_is_refused(kept: Kept) -> None:
    clock = FakeClock(1_680_000_000_000)  # April 2023: later event times are in the future
    broker = FakeBroker()
    with pytest.raises(FluxError) as caught:
        deliver(kept, broker, clock=clock)
    assert caught.value.code.code == "FLX-3514"
    text = str(caught.value)
    assert "message.timestamp.after.max.ms=3600000" in text and "backfill" in text
    assert all(not broker.read(topic) for topic in broker.topics if topic != "sim.device_change")


def test_a_record_further_back_than_the_topic_allows_is_refused(kept: Kept) -> None:
    broker = FakeBroker()
    broker.add_topic("sim.reading", message_timestamp_before_max_ms="86400000")
    with pytest.raises(FluxError) as caught:
        deliver(kept, broker)
    assert caught.value.code.code == "FLX-3514"
    assert "message.timestamp.before.max.ms=86400000" in str(caught.value)
    assert broker.read("sim.reading") == []


def test_a_topic_configured_for_a_backfill_takes_the_same_stream(
    kept: Kept, reference: Mapping[str, List[Stored]]
) -> None:
    broker = FakeBroker()
    for topic in TOPICS:
        broker.add_topic(
            topic,
            message_timestamp_after_max_ms="9223372036854775807",
            cleanup_policy="compact" if topic == "sim.device_current" else "delete",
        )
    deliver(kept, broker, clock=FakeClock(1_680_000_000_000))
    assert broker.read("sim.reading") == reference["sim.reading"]


def test_a_negative_timestamp_is_refused_without_moving_it(kept: Kept) -> None:
    target = target_of(FakeBroker())
    sink = _sink(kept, target, "reading")
    record = KafkaRecord(b"1", {"a": 1}, -1, (), 7, b"1")
    with pytest.raises(FluxError) as caught:
        sink._check_time(record)
    assert caught.value.code.code == "FLX-3514"
    assert "row 7" in str(caught.value) and "1970" in str(caught.value)


def _sink(kept: Kept, target: KafkaTarget, expose_id: str, **more: Any) -> KafkaSink:
    run_receipt = json.loads(kept.layout.receipt_path.read_text(encoding="utf-8"))
    emit = run_receipt["emits"][expose_id]
    context = StreamContext(
        run_id=kept.layout.run_id,
        expose_id=expose_id,
        rows=int(emit["rows"]),
        output_digest=DigestRef(
            value=emit["outputDigest"],
            domain=digest_of_document({}, subject="x").domain,
            subject=f"outputDigest:{expose_id}",
        ),
        privacy_grade=RecordedGrade(grade="P1"),
        bundle_directory=kept.bundle,
        simulation_id=SIMULATION,
        **more,
    )
    return target.sink_for(context)


def _header(sink: KafkaSink) -> EmitHeader:
    return EmitHeader(
        expose_id=sink.shape.expose_id,
        columns=sink.shape.columns,
        types=sink.shape.types,
        contract_digest=DigestRef(
            value=sink.shape.contract_digest,
            domain=digest_of_document({}, subject="x").domain,
            subject="contractDigest",
        ),
    )


def test_a_header_that_is_not_the_bundles_is_refused(kept: Kept) -> None:
    sink = _sink(kept, target_of(FakeBroker()), "site")
    header = _header(sink)
    for bad in (
        EmitHeader("reading", header.columns, header.types, header.contract_digest),
        EmitHeader(
            header.expose_id, header.columns[:-1], header.types[:-1], header.contract_digest
        ),
        EmitHeader(header.expose_id, header.columns, ("INT",) * 3, header.contract_digest),
        EmitHeader(
            header.expose_id,
            header.columns,
            header.types,
            DigestRef(
                value="sha256:" + "0" * 64,
                domain=digest_of_document({}, subject="x").domain,
                subject="x",
            ),
        ),
    ):
        broker = FakeBroker()
        fresh = _sink(kept, target_of(broker), "site")
        with pytest.raises(FluxError) as caught:
            fresh.open(bad)
        assert caught.value.code.code == "FLX-3504"
        fresh.close()
        assert broker.calls == [] and fresh.receipt is None


def test_a_sink_cannot_be_opened_twice_or_written_before_it_is_opened(kept: Kept) -> None:
    sink = _sink(kept, target_of(FakeBroker()), "site")
    with pytest.raises(FluxError) as early:
        sink.write([1, "eu", "2023-01-01"])
    assert early.value.code.code == "FLX-3301"
    sink.open(_header(sink))
    with pytest.raises(FluxError) as again:
        sink.open(_header(sink))
    assert again.value.code.code == "FLX-3301"
    sink.close()
    sink.close()


# -- exactly once ------------------------------------------------------------------------


def _fingerprint(records: Sequence[Stored]) -> List[Any]:
    return sorted(
        ((r.key, r.value, r.timestamp, r.headers, r.partition) for r in records), key=repr
    )


def test_a_clean_delivery_is_one_transaction_per_stream_and_records_its_progress(
    kept: Kept,
) -> None:
    broker = FakeBroker()
    receipts = deliver(kept, broker)
    assert [r.writer["transactions"] for r in receipts] == [1] * 6
    progress = {
        topic: json.loads(text)
        for (group, topic), text in broker.committed.items()
        if group.startswith(f"flux-sink.{kept.layout.run_id}.")
    }
    assert sorted(progress) == list(TOPICS)
    for topic, found in progress.items():
        assert found["complete"] is True and found["order"] == "stream" and found["v"] == 1
        assert found["records"] == len(broker.read(topic))
    group = next(iter(broker.committed))[0]
    assert group == broker.producers[0].transactional_id == broker.producers[0].group


def test_a_delivery_aborted_on_an_error_leaves_nothing_and_the_retry_sends_each_record_once(
    kept: Kept, reference: Mapping[str, List[Stored]]
) -> None:
    broker = FakeBroker()
    broker.failures = [Failure("raise", after=40)]
    with pytest.raises(FluxError) as caught:
        deliver(kept, broker)
    assert "device_change" in str(caught.value)
    assert visible(broker) == {} and broker.committed == {}
    assert ("abort", broker.producers[0].transactional_id) in broker.calls
    deliver(kept, broker)
    assert {t: _fingerprint(r) for t, r in visible(broker).items()} == {
        t: _fingerprint(r) for t, r in reference.items()
    }


def test_a_producer_that_dies_mid_transaction_is_fenced_by_the_next_delivery(
    kept: Kept, reference: Mapping[str, List[Stored]]
) -> None:
    broker = FakeBroker()
    broker.failures = [Failure("die", after=40)]
    with pytest.raises(FluxError):
        deliver(kept, broker)
    zombie = broker.producers[0]
    assert zombie.dead and zombie.pending, "the zombie left its transaction open"
    assert ("abort", zombie.transactional_id) not in broker.calls
    deliver(kept, broker)
    assert zombie.fenced and zombie.pending == []
    assert {t: _fingerprint(r) for t, r in visible(broker).items()} == {
        t: _fingerprint(r) for t, r in reference.items()
    }


def test_a_failed_commit_leaves_the_stream_undelivered(kept: Kept) -> None:
    broker = FakeBroker()
    broker.failures = [Failure("commit")]
    with pytest.raises(FluxError):
        deliver(kept, broker)
    assert visible(broker) == {} and broker.committed == {}


def test_a_third_delivery_sends_nothing(kept: Kept, reference: Mapping[str, List[Stored]]) -> None:
    broker = FakeBroker()
    deliver(kept, broker)
    producers = len(broker.producers)
    third = deliver(kept, broker)
    assert len(broker.producers) == producers, "a finished stream opens no producer"
    assert {t: _fingerprint(r) for t, r in visible(broker).items()} == {
        t: _fingerprint(r) for t, r in reference.items()
    }
    for receipt in third:
        assert receipt.writer["records"] == 0 and receipt.writer["transactions"] == 0
        assert (
            receipt.writer["skipped"] == len(reference[receipt.table or ""]) and receipt.rows >= 0
        )


def test_a_stream_of_a_different_run_is_delivered_again_in_full(kept: Kept) -> None:
    broker = FakeBroker()
    deliver(kept, broker)
    stale = {
        key: text.replace('"digest":"', '"digest":"0') for key, text in broker.committed.items()
    }
    broker.committed = stale
    receipts = deliver(kept, broker)
    assert all(r.writer["skipped"] == 0 and r.writer["records"] > 0 for r in receipts)


def test_a_stream_that_stops_short_is_aborted_and_makes_no_receipt(kept: Kept) -> None:
    broker = FakeBroker()
    sink = _sink(kept, target_of(broker), "site")
    sink.open(_header(sink))
    sink.write([1, "eu", "2023-01-01"])
    sink.close()
    assert sink.receipt is None and broker.read("sim.site") == [] and broker.committed == {}
    assert ("abort", sink.transactional_id) in broker.calls
    assert broker.closes >= 1 and broker.producers[0].closed


def test_a_sink_closed_after_a_failed_write_releases_its_clients_and_sends_nothing(
    kept: Kept,
) -> None:
    broker = FakeBroker()
    sink = _sink(kept, target_of(broker), "site")
    sink.open(_header(sink))
    with pytest.raises(FluxError):
        sink.write([None, "eu", "2023-01-01"])
    sink.close()
    assert sink.receipt is None and broker.committed == {} and broker.producers[0].closed


# -- pace ---------------------------------------------------------------------------


def test_a_paced_stream_is_sent_in_event_time_with_a_commit_each_interval(
    kept: Kept, reference: Mapping[str, List[Stored]]
) -> None:
    clock = FakeClock(WALL_2026)
    broker = FakeBroker()
    receipts = deliver(kept, broker, {"pace": "x86400", "commit-every": "1"}, clock=clock)
    assert {t: _fingerprint(r) for t, r in visible(broker).items()} == {
        t: _fingerprint(r) for t, r in reference.items()
    }
    reading = next(r for r in receipts if r.table == "sim.reading")
    assert reading.writer["pace"] == "x86400" and reading.writer["transactions"] > 10
    assert sum(clock.sleeps) > 100, "event time was waited out, a day a second"
    stamps = [r.timestamp for r in broker.read("sim.reading")]
    assert stamps == sorted(stamps), "a paced stream is sent in event-time order"
    max_receipts = deliver(kept, FakeBroker(), clock=FakeClock(WALL_2026))
    assert [r.output_digest for r in receipts] == [r.output_digest for r in max_receipts]


def test_a_fast_pace_commits_once(kept: Kept) -> None:
    clock = FakeClock(WALL_2026)
    receipts = deliver(kept, FakeBroker(), {"pace": "x1000000000"}, clock=clock)
    assert all(r.writer["transactions"] == 1 for r in receipts)


def test_a_paced_delivery_resumes_after_its_last_committed_record(
    kept: Kept, reference: Mapping[str, List[Stored]]
) -> None:
    options = {"pace": "x86400", "commit-every": "1"}
    broker = FakeBroker()
    broker.failures = [Failure("raise", after=100)]
    with pytest.raises(FluxError):
        deliver(kept, broker, options, clock=FakeClock(WALL_2026))
    group = broker.producers[0].group
    committed = json.loads(broker.committed[(group, "sim.device_change")])
    assert committed["complete"] is False and committed["order"] == "time"
    assert 0 < committed["records"] <= 100
    assert len(broker.read("sim.device_change")) == committed["records"]
    receipts = deliver(kept, broker, options, clock=FakeClock(WALL_2026))
    change = next(r for r in receipts if r.table == "sim.device_change")
    assert change.writer["skipped"] == committed["records"]
    assert change.writer["records"] + change.writer["skipped"] == len(
        reference["sim.device_change"]
    )
    assert {t: _fingerprint(r) for t, r in visible(broker).items()} == {
        t: _fingerprint(r) for t, r in reference.items()
    }


def test_a_delivery_does_not_resume_in_another_order(kept: Kept) -> None:
    broker = FakeBroker()
    broker.failures = [Failure("raise", after=100)]
    with pytest.raises(FluxError):
        deliver(kept, broker, {"pace": "x86400", "commit-every": "1"}, clock=FakeClock(WALL_2026))
    with pytest.raises(FluxError) as caught:
        deliver(kept, broker)
    assert caught.value.code.code == "FLX-3504"
    assert "time order" in str(caught.value) and "stream order" in str(caught.value)


def test_a_finished_paced_stream_is_not_sent_again_at_any_pace(kept: Kept) -> None:
    broker = FakeBroker()
    deliver(kept, broker, {"pace": "x1000000000"}, clock=FakeClock(WALL_2026))
    count = len(broker.producers)
    again = deliver(kept, broker)
    assert len(broker.producers) == count and all(r.writer["records"] == 0 for r in again)


# -- the receipt and what it must not hold -----------------------------------------------


def test_the_receipt_says_what_was_written_and_holds_no_credential(
    kept: Kept, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUX_TEST_KAFKA_USER", "svc-account-name")
    monkeypatch.setenv("FLUX_TEST_KAFKA_PASS", "s3cr3t-passphrase-value")
    broker = FakeBroker()
    options = {
        "sasl-username-env": "FLUX_TEST_KAFKA_USER",
        "sasl-password-env": "FLUX_TEST_KAFKA_PASS",
        "security-protocol": "SASL_PLAINTEXT",
    }
    receipts = deliver(kept, broker, options)
    change = next(r for r in receipts if r.expose_id == "device_change")
    assert (change.sink, change.target, change.table) == ("kafka", BOOTSTRAP, "sim.device_change")
    assert change.rows == 210 and change.run_id == kept.layout.run_id
    writer = change.writer
    assert writer["shape"] == "debezium" and writer["value"] == "json" and writer["key"] == "string"
    assert writer["topic"] == "sim.device_change" and writer["partitions"] == 3
    assert writer["audience"] == "internal" and writer["privacyGrade"] == "P1"
    assert writer["partitioner"] == "murmur2_random" and writer["pace"] == "max"
    assert writer["records"] == len(broker.read("sim.device_change")) and writer["skipped"] == 0
    assert next(r for r in receipts if r.expose_id == "site").writer["shape"] == "row"
    text = json.dumps([r.as_document() for r in receipts])
    assert "s3cr3t-passphrase-value" not in text and "svc-account-name" not in text


def test_a_missing_credential_variable_is_named_and_nothing_is_contacted(
    kept: Kept, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FLUX_TEST_KAFKA_MISSING", raising=False)
    monkeypatch.setenv("FLUX_TEST_KAFKA_PASS", "s3cr3t-passphrase-value")
    broker = FakeBroker()
    with pytest.raises(FluxError) as caught:
        deliver(
            kept,
            broker,
            {
                "sasl-username-env": "FLUX_TEST_KAFKA_MISSING",
                "sasl-password-env": "FLUX_TEST_KAFKA_PASS",
                "security-protocol": "SASL_PLAINTEXT",
            },
        )
    assert caught.value.code.code == "FLX-3504"
    assert "FLUX_TEST_KAFKA_MISSING" in str(caught.value)
    assert "s3cr3t-passphrase-value" not in str(caught.value) and broker.calls == []


def test_the_registry_gets_its_credential_from_the_named_variable(
    kept: Kept, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUX_TEST_REGISTRY_LOGIN", "svc:s3cr3t-registry-value")
    serdes = FakeSerdes()
    receipts = deliver(
        kept,
        FakeBroker(),
        {"registry": "http://127.0.0.1:8081", "registry-auth-env": "FLUX_TEST_REGISTRY_LOGIN"},
        serdes=serdes,
    )
    assert {call[2] for call in serdes.calls} == {"svc:s3cr3t-registry-value"}
    assert {r.writer["value"] for r in receipts} == {f"avro:{r.table}-value@1" for r in receipts}
    assert "s3cr3t-registry-value" not in json.dumps([r.as_document() for r in receipts])


def test_an_envelope_stream_goes_to_the_serializer_as_the_envelope(kept: Kept) -> None:
    serdes = FakeSerdes()
    deliver(
        kept,
        FakeBroker(),
        {"registry": "http://127.0.0.1:8081", "value-format": "json"},
        serdes=serdes,
    )
    assert {call[1] for call in serdes.calls} == {"json"} and len(serdes.calls) == 6


def test_an_error_from_the_client_library_is_scrubbed_and_not_chained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLUX_TEST_KAFKA_PASS", "s3cr3t-passphrase-value")
    settings = KafkaSettings.parse(
        "broker.example.com:9093",
        {
            "audience": "internal",
            "sasl-username-env": "FLUX_TEST_KAFKA_USER",
            "sasl-password-env": "FLUX_TEST_KAFKA_PASS",
        },
    )
    broker = ConfluentBroker(
        settings.client(),
        lambda: Credentials(),
        lambda: kafka.known_strings_of(settings),
    )
    error = broker._fail(
        RuntimeError(
            "login failed for broker.example.com:9093 with s3cr3t-passphrase-value\nsecond line"
        ),
        "could not open",
    )
    assert error.code.code == "FLX-3505"
    text = str(error)
    assert "s3cr3t-passphrase-value" not in text and "broker.example.com:9093" not in text
    assert "second line" not in text and error.__cause__ is None
    assert isinstance(ClientSettings(bootstrap="x"), ClientSettings)


def test_the_registry_scrubs_a_delivery_error_of_credentials_and_not_of_plain_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flux_engine.sinks._secrets import scrub

    monkeypatch.setenv("FLUX_TEST_KAFKA_PASS", "s3cr3t-passphrase-value")
    pytest.importorskip("confluent_kafka")
    built = kafka.target(
        "127.0.0.1:1",
        {
            "audience": "internal",
            "sasl-username-env": "FLUX_TEST_KAFKA_USER",
            "sasl-password-env": "FLUX_TEST_KAFKA_PASS",
            "security-protocol": "SASL_PLAINTEXT",
        },
    )
    monkeypatch.setenv("FLUX_TEST_KAFKA_USER", "svc-account-name")
    known = known_of(built) or ()
    text = scrub(
        "an internal audience; svc-account-name s3cr3t-passphrase-value", known, sink="kafka"
    )
    assert "internal audience" in text
    assert "s3cr3t-passphrase-value" not in text and "svc-account-name" not in text


def test_the_clock_is_real_unless_a_test_replaces_it() -> None:
    clock = Clock()
    assert clock.wall_ms() > 1_700_000_000_000 and clock.monotonic() >= 0
