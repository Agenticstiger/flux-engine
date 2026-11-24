# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How each stream of the kafka-devices bundle is shaped for Kafka (ADR 0038 sections 4 to 7)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Mapping, Optional

import pytest
import yaml

from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.sinks._kafka_shape import ShapeSettings, StreamShape, stream_shapes

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "kafka-devices"
SIMULATION = "sim-devices"
CONTRACT = "devices.fluid.yml"
SIM_FILE = "simulation.flux.yml"


def bundle(
    tmp_path: Path,
    *,
    contract: Optional[Callable[[str], str]] = None,
    simulation: Optional[Callable[[str], str]] = None,
) -> Path:
    """A copy of the fixture with the contract and the Simulation edited, digests kept in step."""
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    old = digest_of_document(
        yaml.safe_load((FIXTURE / CONTRACT).read_text()), subject="contractDigest"
    ).value
    if contract is not None:
        edited = contract((target / CONTRACT).read_text())
        (target / CONTRACT).write_text(edited)
    new = digest_of_document(
        yaml.safe_load((target / CONTRACT).read_text()), subject="contractDigest"
    ).value
    text = (target / SIM_FILE).read_text().replace(old, new)
    if simulation is not None:
        text = simulation(text)
    (target / SIM_FILE).write_text(text)
    return target


def swap(old: str, new: str) -> Callable[[str], str]:
    def edit(text: str) -> str:
        assert old in text, old
        return text.replace(old, new)

    return edit


def shapes(directory: Path, **settings: object) -> Mapping[str, StreamShape]:
    return stream_shapes(directory, SIMULATION, ShapeSettings(**settings))  # type: ignore[arg-type]


def refused(code: str, directory: Path, **settings: object) -> str:
    with pytest.raises(FluxError) as caught:
        shapes(directory, **settings)
    assert caught.value.code.code == code
    return str(caught.value)


@pytest.fixture()
def plain(tmp_path: Path) -> Path:
    return bundle(tmp_path)


def test_every_emitted_expose_has_a_shape(plain: Path) -> None:
    found = shapes(plain)
    assert sorted(found) == [
        "device_change",
        "device_current",
        "device_month",
        "device_version",
        "reading",
        "site",
    ]
    for expose_id, shape in found.items():
        assert shape.expose_id == expose_id
        assert shape.topic == f"sim.{expose_id}"
        assert shape.product_ref == "fleet.devices"
        assert shape.simulation_id == SIMULATION
        assert shape.contract_digest.startswith("sha256:")
        assert shape.key_format == "string"
        assert shape.subject is None
        assert len(shape.columns) == len(shape.types)


def test_a_table_is_keyed_by_its_primary_entity_and_timed_by_its_time_dimension(
    plain: Path,
) -> None:
    site = shapes(plain)["site"]
    assert site.role == "table"
    assert site.key_columns == ("site_id",)
    assert site.timestamp_column == "opened_on"
    assert site.timestamp_default is None
    assert site.envelope is None and not site.compacted
    assert site.columns == ("site_id", "region", "opened_on")
    assert site.types == ("INT", "STRING", "DATE")


def test_a_tables_default_time_dimension_wins(plain: Path) -> None:
    reading = shapes(plain)["reading"]
    assert reading.role == "table"
    assert reading.key_columns == ("reading_id",)
    assert reading.timestamp_column == "read_on"


def test_a_history_is_keyed_by_its_natural_entity_and_timed_by_its_validity_start(
    plain: Path,
) -> None:
    version = shapes(plain)["device_version"]
    assert version.role == "history"
    assert version.key_columns == ("device_id",)
    assert version.timestamp_column == "valid_from"
    assert version.envelope is None and not version.compacted


def test_a_snapshot_is_compacted_and_dated_by_the_histories_end(plain: Path) -> None:
    current = shapes(plain)["device_current"]
    assert current.role == "snapshot"
    assert current.compacted
    assert current.key_columns == ("device_id",)
    assert current.timestamp_column is None
    assert current.timestamp_default == "2025-12-31"
    envelope = current.envelope
    assert envelope is not None
    assert (
        envelope.op_column is None and envelope.date_column is None and envelope.at_column is None
    )
    assert envelope.fields == (
        ("device_id", "INT"),
        ("firmware", "STRING"),
        ("status", "STRING"),
        ("installed_on", "DATE"),
    )
    assert envelope.copied == {name: name for name, _ in envelope.fields}
    assert envelope.before == {} and envelope.after == {} and envelope.extra == ()
    assert envelope.key_fields == ("device_id",)
    assert envelope.table == "device_version"


def test_a_snapshot_that_carries_the_validity_start_uses_it(tmp_path: Path) -> None:
    directory = bundle(
        tmp_path,
        contract=swap(
            "        - { name: firmware, type: STRING, required: true }\n"
            "        - { name: status, type: STRING, required: true }\n"
            "        - { name: installed_on, type: DATE, required: true }\n"
            "    semantics:\n"
            "      entities:\n"
            "        - { name: device, type: primary, expr: device_id }",
            "        - { name: firmware, type: STRING, required: true }\n"
            "        - { name: status, type: STRING, required: true }\n"
            "        - { name: installed_on, type: DATE, required: true }\n"
            "        - { name: valid_from, type: DATE, required: true }\n"
            "    semantics:\n"
            "      entities:\n"
            "        - { name: device, type: primary, expr: device_id }",
        ),
    )
    current = shapes(directory)["device_current"]
    assert current.timestamp_column == "valid_from"
    assert current.timestamp_default is None


def test_events_become_debezium_envelopes(plain: Path) -> None:
    change = shapes(plain)["device_change"]
    assert change.role == "events"
    assert change.key_columns == ("device_id",)
    assert change.timestamp_column == "recorded_at"
    assert not change.compacted
    envelope = change.envelope
    assert envelope is not None
    assert (envelope.op_column, envelope.date_column, envelope.at_column) == (
        "op",
        "recorded_at",
        "changed_at",
    )
    assert envelope.before == {"firmware": "before_firmware"}
    assert envelope.after == {"firmware": "after_firmware"}
    assert envelope.copied == {"device_id": "device_id"}
    assert envelope.fields == (("device_id", "INT"), ("firmware", "STRING"))
    assert envelope.key_fields == ("device_id",)
    assert envelope.extra == (("event_id", "INT"),)
    assert envelope.table == "device_version"


def test_periodic_rows_are_timed_by_their_period_column(plain: Path) -> None:
    month = shapes(plain)["device_month"]
    assert month.role == "periods"
    assert month.key_columns == ("device_id",)
    assert month.timestamp_column == "month"
    assert month.envelope is None and not month.compacted


def test_options_replace_topic_key_and_timestamp(plain: Path) -> None:
    found = shapes(
        plain,
        topic_prefix="test.",
        topics={"site": "people"},
        keys={"site": ("region",)},
        timestamps={"site": "opened_on"},
    )
    assert found["site"].topic == "test.people"
    assert found["site"].key_columns == ("region",)
    assert found["reading"].topic == "test.reading"


def test_a_json_key_may_have_several_columns(plain: Path) -> None:
    found = shapes(plain, key_format="json", keys={"site": ("site_id", "region")})
    assert found["site"].key_columns == ("site_id", "region")
    assert found["site"].key_format == "json"


def test_a_timestamp_option_may_move_a_history_to_another_column(plain: Path) -> None:
    found = shapes(plain, timestamps={"device_version": "installed_on"})
    assert found["device_version"].timestamp_column == "installed_on"


# --- refusals: the options (FLX-3504) --------------------------------------------------------


@pytest.mark.parametrize("option", ["topics", "keys", "timestamps"])
def test_an_option_naming_an_expose_the_run_does_not_emit_is_refused(
    plain: Path, option: str
) -> None:
    value = {"nothing": "x"} if option == "topics" else {"nothing": ("x",)}
    if option == "timestamps":
        value = {"nothing": "x"}
    text = refused("FLX-3504", plain, **{option: value})
    assert "nothing" in text


def test_a_key_column_the_expose_lacks_is_refused(plain: Path) -> None:
    text = refused("FLX-3504", plain, keys={"site": ("missing",)})
    assert "key.site" in text and "missing" in text


def test_an_empty_key_option_is_refused(plain: Path) -> None:
    assert "key.site" in refused("FLX-3504", plain, keys={"site": ()})


def test_a_timestamp_column_that_is_not_an_instant_is_refused(plain: Path) -> None:
    text = refused("FLX-3504", plain, timestamps={"site": "region"})
    assert "region" in text and "DATE" in text


def test_a_timestamp_column_the_expose_lacks_is_refused(plain: Path) -> None:
    assert "nope" in refused("FLX-3504", plain, timestamps={"site": "nope"})


@pytest.mark.parametrize("prefix", ["", "a b", "sim/", "é."])
def test_a_prefix_that_is_empty_or_not_legal_is_refused(plain: Path, prefix: str) -> None:
    assert "topic-prefix" in refused("FLX-3504", plain, topic_prefix=prefix)


def test_a_key_format_that_is_not_known_is_refused(plain: Path) -> None:
    assert "key-format" in refused("FLX-3504", plain, key_format="avro")


# --- refusals: the declarations (FLX-3513) -----------------------------------------------------


def test_an_expose_with_no_entity_is_refused_naming_the_option(tmp_path: Path) -> None:
    directory = bundle(
        tmp_path,
        contract=swap("type: primary, expr: site_id", "type: foreign, expr: site_id"),
    )
    text = refused("FLX-3513", directory)
    assert "site" in text and "key.site" in text


def test_an_expose_with_no_event_time_is_refused_naming_the_option(tmp_path: Path) -> None:
    directory = bundle(
        tmp_path,
        contract=swap(
            "- { name: opened_on, type: time }", "- { name: opened_on, type: categorical }"
        ),
    )
    text = refused("FLX-3513", directory)
    assert "site" in text and "timestamp.site" in text


def test_a_timestamp_option_settles_a_missing_event_time(tmp_path: Path) -> None:
    directory = bundle(
        tmp_path,
        contract=swap(
            "- { name: opened_on, type: time }", "- { name: opened_on, type: categorical }"
        ),
    )
    found = shapes(directory, timestamps={"site": "opened_on"})
    assert found["site"].timestamp_column == "opened_on"


def test_a_time_dimension_that_is_not_an_instant_column_is_not_a_candidate(tmp_path: Path) -> None:
    directory = bundle(
        tmp_path,
        contract=swap(
            "- { name: opened_on, type: time }",
            "- { name: opened_on, type: time }\n        - { name: region, type: time }",
        ),
    )
    # region is a STRING column, so only opened_on counts.
    assert shapes(directory)["site"].timestamp_column == "opened_on"


def test_an_events_entry_without_op_is_refused(tmp_path: Path) -> None:
    directory = bundle(tmp_path, simulation=swap("        op: op\n", ""))
    text = refused("FLX-3513", directory)
    assert "device_change" in text and "op" in text


def test_a_composite_key_with_a_string_format_is_refused_naming_json(plain: Path) -> None:
    text = refused("FLX-3513", plain, keys={"site": ("site_id", "region")})
    assert "key-format=json" in text and "site" in text


def test_two_streams_on_one_topic_are_refused(plain: Path) -> None:
    text = refused("FLX-3513", plain, topics={"site": "same", "reading": "same"})
    assert "site" in text and "reading" in text and "topic." in text


def test_an_events_column_named_like_a_source_field_is_refused(tmp_path: Path) -> None:
    directory = bundle(
        tmp_path,
        contract=swap("event_id", "version"),
        simulation=swap("event_id", "version"),
    )
    text = refused("FLX-3513", directory)
    assert "version" in text and "device_change" in text


@pytest.mark.parametrize("name", ["bad topic", "a/b", "x" * 300, "\u00e9"])
def test_a_topic_name_kafka_does_not_allow_is_refused(plain: Path, name: str) -> None:
    text = refused("FLX-3513", plain, topics={"site": name})
    assert "topic.site" in text


def test_a_topic_of_only_dots_is_refused_even_behind_an_empty_name(plain: Path) -> None:
    # The prefix may not be empty, so "." and ".." cannot be reached through a name alone.
    assert "topic-prefix" in refused("FLX-3504", plain, topic_prefix="")
    assert "topic.site" in refused("FLX-3513", plain, topic_prefix=".", topics={"site": "."})


def test_a_history_the_bundle_does_not_emit_is_refused(tmp_path: Path) -> None:
    directory = bundle(
        tmp_path,
        simulation=swap("of: device_version\n        at:", "of: elsewhere\n        at:"),
    )
    with pytest.raises(FluxError) as caught:
        shapes(directory)
    assert caught.value.code.code.startswith("FLX-")


# --- the contract's own topic and subject -----------------------------------------------------


KAFKA_BINDING = (
    "    binding:\n"
    "      platform: kafka\n"
    "      format: json\n"
    "      location: { topic: sites }\n"
    "      serialization: { value: %s }\n"
    "      properties:\n"
    "        registry: { kind: confluent, url: http://localhost:8081, subject: site-value, version: %s }\n"
)
GLUE_BINDING = (
    "    binding:\n"
    "      platform: aws\n"
    "      format: glue_table\n"
    "      location: { database: fleet, table: site, region: eu-west-1 }\n"
)


def kafka_bundle(tmp_path: Path, serialization: str = "avro", version: str = "latest") -> Path:
    return bundle(tmp_path, contract=swap(GLUE_BINDING, KAFKA_BINDING % (serialization, version)))


def test_a_kafka_binding_names_the_topic_behind_the_prefix(tmp_path: Path) -> None:
    site = shapes(kafka_bundle(tmp_path))["site"]
    assert site.topic == "sim.sites"


def test_a_registry_subject_the_contract_names_is_carried(tmp_path: Path) -> None:
    site = shapes(kafka_bundle(tmp_path, "avro-confluent", "3"))["site"]
    assert site.subject is not None
    assert (site.subject.kind, site.subject.subject) == ("confluent", "site-value")
    assert site.subject.version == 3
    assert site.subject.value_format == "AVRO"


@pytest.mark.parametrize(
    ("named", "wanted"), [("json", "JSON"), ("json-confluent", "JSON"), ("protobuf", "PROTOBUF")]
)
def test_the_subjects_format_follows_the_bindings_serialization(
    tmp_path: Path, named: str, wanted: str
) -> None:
    subject = shapes(kafka_bundle(tmp_path, named))["site"].subject
    assert subject is not None and subject.value_format == wanted


def test_a_subject_defaults_to_the_contract_topics_value_subject(tmp_path: Path) -> None:
    directory = bundle(
        tmp_path,
        contract=swap(
            GLUE_BINDING,
            KAFKA_BINDING.replace(" subject: site-value,", "") % ("avro", "latest"),
        ),
    )
    subject = shapes(directory)["site"].subject
    assert subject is not None and subject.subject == "sites-value"
    assert subject.version == "latest"


def test_a_confluent_kafka_topic_binding_counts_as_kafka(tmp_path: Path) -> None:
    binding = (KAFKA_BINDING % ("avro", "latest")).replace(
        "platform: kafka\n      format: json", "platform: confluent\n      format: kafka_topic"
    )
    site = shapes(bundle(tmp_path, contract=swap(GLUE_BINDING, binding)))["site"]
    assert site.topic == "sim.sites" and site.subject is not None


def test_a_subject_replaces_the_envelope_whatever_the_role(tmp_path: Path) -> None:
    glue = (
        "    binding:\n"
        "      platform: aws\n"
        "      format: glue_table\n"
        "      location: { database: fleet, table: device_change, region: eu-west-1 }\n"
    )
    kafka = (
        "    binding:\n"
        "      platform: kafka\n"
        "      format: json\n"
        "      location: { topic: changes }\n"
        "      properties: { registry: { kind: confluent, url: http://localhost:8081, subject: changes-value } }\n"
    )
    directory = bundle(tmp_path, contract=swap(glue, kafka))
    change = shapes(directory)["device_change"]
    assert change.role == "events"
    assert change.envelope is None
    assert change.subject is not None and change.subject.subject == "changes-value"
    assert change.topic == "sim.changes"


def test_a_settings_topic_beats_the_contracts(tmp_path: Path) -> None:
    found = shapes(kafka_bundle(tmp_path), topics={"site": "mine"})
    assert found["site"].topic == "sim.mine"


def test_the_bindings_registry_kind_is_read(tmp_path: Path) -> None:
    binding = (KAFKA_BINDING % ("avro", "latest")).replace(
        "kind: confluent, url: http://localhost:8081", "kind: glue"
    )
    subject = shapes(bundle(tmp_path, contract=swap(GLUE_BINDING, binding)))["site"].subject
    assert subject is not None and subject.kind == "glue"


def test_a_serialization_that_is_not_known_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        shapes(kafka_bundle(tmp_path, "xml"))
    assert caught.value.code.code == "FLX-3515"


def test_the_shape_does_not_change_between_calls(plain: Path) -> None:
    assert shapes(plain) == shapes(plain)
