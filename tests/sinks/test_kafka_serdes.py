# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink's registry serializers, against confluent-kafka's mock registry (ADR 0038 section 8).

A schema derived from a stream's columns registers under the topic's ``-value`` subject and every
record round-trips through the library's own deserializers; a subject the contract names is read,
never written to, and checked against the stream's columns first; and a value is converted by the
field types of the schema it is written against, exactly.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

pytest.importorskip("confluent_kafka")

from confluent_kafka.schema_registry import Schema  # noqa: E402
from confluent_kafka.schema_registry._sync.mock_schema_registry_client import (  # noqa: E402
    MockSchemaRegistryClient,
)
from confluent_kafka.schema_registry.avro import AvroDeserializer  # noqa: E402
from confluent_kafka.schema_registry.json_schema import JSONDeserializer  # noqa: E402
from confluent_kafka.serialization import MessageField, SerializationContext  # noqa: E402

from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.sinks._kafka_records import RecordBuilder  # noqa: E402
from flux_engine.sinks._kafka_serdes import (  # noqa: E402
    ConfluentSerdes,
    RegistryAccess,
    avro_converter,
    avro_schema_for,
    json_converter,
    json_schema_for,
    namespace_of,
)
from flux_engine.sinks._kafka_shape import ShapeSettings, StreamShape, stream_shapes  # noqa: E402
from flux_engine.stages.layout import RunLayout  # noqa: E402
from flux_engine.stages.run import run  # noqa: E402

from .test_kafka_shape import GLUE_BINDING, KAFKA_BINDING, swap  # noqa: E402
from .test_kafka_shape import bundle as shape_bundle  # noqa: E402

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROFILE = FIXTURES / "kafka-devices.profile.json"
SIMULATION = "sim-devices"
ACCESS = RegistryAccess(url="http://registry.invalid")
EXPOSES = ("site", "device_version", "reading", "device_current", "device_change", "device_month")


def mock_registry() -> MockSchemaRegistryClient:
    return MockSchemaRegistryClient({"url": "http://registry.invalid"})


def serdes_over(client: MockSchemaRegistryClient) -> ConfluentSerdes:
    return ConfluentSerdes(client_factory=lambda access: client)


class Stream:
    """A run of the devices bundle: its shapes, and the rows of each stream."""

    def __init__(self, root: Path) -> None:
        self.bundle = root / "bundle"
        shutil.copytree(FIXTURES / "kafka-devices", self.bundle)
        result = run(self.bundle, state_dir=root / "state", profile=PROFILE)
        assert result.success, result.stderr
        self.layout = RunLayout.for_state_dir(root / "state", result.artifacts["plan"]["run_id"])
        self.shapes = stream_shapes(self.bundle, SIMULATION, ShapeSettings())

    def rows(self, expose_id: str) -> List[List[Any]]:
        text = self.layout.emit_path(expose_id).read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def values(self, expose_id: str) -> List[Optional[Dict[str, Any]]]:
        shape = self.shapes[expose_id]
        builder = RecordBuilder(shape, run_id="r", output_digest="sha256:x", engine_version="1")
        rows = self.rows(expose_id)
        return [
            record.value
            for index, row in enumerate(rows)
            for record in builder.records(row, index, last=index == len(rows) - 1)
        ]


@pytest.fixture(scope="module")
def stream(tmp_path_factory: pytest.TempPathFactory) -> Stream:
    return Stream(tmp_path_factory.mktemp("serdes"))


def _plain(value: Any) -> Any:
    """A deserialized value as the rendered text it came from."""
    if isinstance(value, dt.datetime):
        return (
            value.astimezone(dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _same(decoded: Mapping[str, Any], sent: Mapping[str, Any]) -> None:
    """*decoded* holds what was *sent*, field for field, with a DECIMAL as its exact digits."""
    for name, value in sent.items():
        got = decoded[name]
        if isinstance(value, Mapping):
            _same(got, value)
        elif isinstance(got, Decimal):
            assert got == Decimal(value), name
        else:
            assert _plain(got) == value, name


# -- derived schemas -------------------------------------------------------------------


@pytest.mark.parametrize("expose_id", EXPOSES)
def test_a_derived_avro_schema_registers_and_every_record_round_trips(
    stream: Stream, expose_id: str
) -> None:
    client = mock_registry()
    shape = stream.shapes[expose_id]
    serializer = serdes_over(client).serializer(shape, value_format="avro", registry=ACCESS)
    assert serializer.description == f"avro:{shape.topic}-value@1"
    assert client.get_subjects() == [f"{shape.topic}-value"]
    registered = client.get_latest_version(f"{shape.topic}-value")
    assert registered.schema.schema_type == "AVRO"
    document = json.loads(registered.schema.schema_str)
    assert document["namespace"] == namespace_of(shape.topic) and document == avro_schema_for(shape)
    decode = AvroDeserializer(client)
    context = SerializationContext(shape.topic, MessageField.VALUE)
    for value in stream.values(expose_id):
        if value is None:
            continue
        payload = serializer(value, topic=shape.topic)
        assert payload[0] == 0, "Confluent framing: a magic byte, then the schema's id"
        _same(decode(payload, context), value)
    assert client.get_subjects() == [f"{shape.topic}-value"]


def test_the_envelope_schema_is_debeziums_with_the_events_columns_in_source(stream: Stream) -> None:
    document = avro_schema_for(stream.shapes["device_change"])
    assert document["name"] == "Envelope" and document["namespace"] == "flux.sim_device_change"
    assert [field["name"] for field in document["fields"]] == [
        "before",
        "after",
        "source",
        "op",
        "ts_ms",
    ]
    before, after, source = (field["type"] for field in document["fields"][:3])
    assert before[0] == "null" and before[1]["name"] == "Value"
    assert after == ["null", "flux.sim_device_change.Value"]
    names = [field["name"] for field in source["fields"]]
    assert names == ["version", "connector", "name", "ts_ms", "snapshot", "db", "table", "event_id"]
    assert source["fields"][7]["type"] == ["null", "long"]
    assert all(field["type"] == "string" for field in source["fields"][:3])


def test_avro_types_follow_the_flux_types(stream: Stream) -> None:
    fields = {f["name"]: f["type"] for f in avro_schema_for(stream.shapes["reading"])["fields"]}
    assert fields["reading_id"] == ["null", "long"]
    assert fields["read_on"] == ["null", {"type": "int", "logicalType": "date"}]
    assert fields["value"] == [
        "null",
        {"type": "bytes", "logicalType": "decimal", "precision": 10, "scale": 2},
    ]


@pytest.mark.parametrize("expose_id", EXPOSES)
def test_a_derived_json_schema_registers_and_every_record_round_trips(
    stream: Stream, expose_id: str
) -> None:
    client = mock_registry()
    shape = stream.shapes[expose_id]
    serializer = serdes_over(client).serializer(shape, value_format="json", registry=ACCESS)
    assert serializer.description == f"json-schema:{shape.topic}-value@1"
    registered = client.get_latest_version(f"{shape.topic}-value")
    assert registered.schema.schema_type == "JSON"
    assert json.loads(registered.schema.schema_str) == json_schema_for(shape)
    decode = JSONDeserializer(registered.schema.schema_str, schema_registry_client=client)
    context = SerializationContext(shape.topic, MessageField.VALUE)
    for value in stream.values(expose_id):
        if value is None:
            continue
        payload = serializer(value, topic=shape.topic)
        assert payload[0] == 0
        assert decode(payload, context) is not None
        body = json.loads(payload[5:], parse_float=Decimal)
        _same(body, value)


def test_a_decimal_is_written_as_a_json_number_with_every_digit(stream: Stream) -> None:
    client = mock_registry()
    shape = stream.shapes["reading"]
    serializer = serdes_over(client).serializer(shape, value_format="json", registry=ACCESS)
    value = {"reading_id": 1, "device_id": 2, "read_on": "2024-02-29", "value": "12.30"}
    payload = serializer(value, topic=shape.topic)
    assert b'"value":12.30' in payload and b'"read_on":"2024-02-29"' in payload
    assert b"12.3," not in payload and b"e" not in payload.split(b'"value":')[1].split(b"}")[0]


# -- a subject the contract names ---------------------------------------------------


SITE_AVRO = {
    "type": "record",
    "name": "Site",
    "namespace": "fleet",
    "fields": [
        {"name": "site_id", "type": "long"},
        {"name": "region", "type": ["null", "string"], "default": None},
        {"name": "opened_on", "type": {"type": "int", "logicalType": "date"}},
    ],
}


def subject_stream(tmp_path: Path, stream: Stream, binding: str) -> StreamShape:
    directory = shape_bundle(tmp_path, contract=swap(GLUE_BINDING, binding))
    return stream_shapes(directory, SIMULATION, ShapeSettings())["site"]


def preregister(client: MockSchemaRegistryClient, document: Mapping[str, Any]) -> int:
    return int(client.register_schema("site-value", Schema(json.dumps(document), "AVRO")))


def test_a_contract_subject_is_read_and_never_written(tmp_path: Path, stream: Stream) -> None:
    shape = subject_stream(tmp_path, stream, KAFKA_BINDING % ("avro", "latest"))
    client = mock_registry()
    schema_id = preregister(client, SITE_AVRO)
    serializer = serdes_over(client).serializer(shape, value_format="avro", registry=ACCESS)
    assert serializer.description == f"avro:site-value@{schema_id}"
    row = {"site_id": 7, "region": "eu", "opened_on": "2023-05-04"}
    payload = serializer(row, topic=shape.topic)
    assert payload[1:5] == schema_id.to_bytes(4, "big")
    decoded = AvroDeserializer(client)(payload, SerializationContext("sites", MessageField.VALUE))
    assert decoded == {"site_id": 7, "region": "eu", "opened_on": dt.date(2023, 5, 4)}
    assert client.get_subjects() == ["site-value"]
    assert client.get_versions("site-value") == [1]


def test_a_numbered_version_is_written_against_that_version(tmp_path: Path, stream: Stream) -> None:
    shape = subject_stream(tmp_path, stream, KAFKA_BINDING % ("avro", "1"))
    client = mock_registry()
    first = preregister(client, SITE_AVRO)
    newer = dict(SITE_AVRO)
    newer["fields"] = [
        *SITE_AVRO["fields"],
        {"name": "note", "type": ["null", "string"], "default": None},
    ]
    second = preregister(client, newer)
    assert second != first
    serializer = serdes_over(client).serializer(shape, value_format="avro", registry=ACCESS)
    assert serializer.description == f"avro:site-value@{first}"
    payload = serializer({"site_id": 1, "region": None, "opened_on": "2023-01-01"}, topic="sites")
    assert payload[1:5] == first.to_bytes(4, "big")


def test_a_required_field_no_column_fills_is_refused_naming_it(
    tmp_path: Path, stream: Stream
) -> None:
    shape = subject_stream(tmp_path, stream, KAFKA_BINDING % ("avro", "latest"))
    client = mock_registry()
    wider = dict(SITE_AVRO)
    wider["fields"] = [*SITE_AVRO["fields"], {"name": "elevation", "type": "long"}]
    preregister(client, wider)
    with pytest.raises(FluxError) as caught:
        serdes_over(client).serializer(shape, value_format="avro", registry=ACCESS)
    assert caught.value.code.code == "FLX-3515"
    assert "'elevation'" in str(caught.value) and "no column" in str(caught.value)


def test_a_column_the_subject_has_no_field_for_is_refused_naming_it(
    tmp_path: Path, stream: Stream
) -> None:
    shape = subject_stream(tmp_path, stream, KAFKA_BINDING % ("avro", "latest"))
    client = mock_registry()
    narrower = dict(SITE_AVRO)
    narrower["fields"] = SITE_AVRO["fields"][:2]
    preregister(client, narrower)
    with pytest.raises(FluxError) as caught:
        serdes_over(client).serializer(shape, value_format="avro", registry=ACCESS)
    assert caught.value.code.code == "FLX-3515"
    assert "'opened_on'" in str(caught.value) and "no field" in str(caught.value)


def test_a_json_subject_is_checked_and_written_the_same_way(tmp_path: Path, stream: Stream) -> None:
    shape = subject_stream(tmp_path, stream, KAFKA_BINDING % ("json", "latest"))
    client = mock_registry()
    document = {
        "type": "object",
        "properties": {
            "site_id": {"type": "integer"},
            "region": {"type": ["string", "null"]},
            "opened_on": {"type": "string", "format": "date"},
        },
        "required": ["site_id", "opened_on"],
    }
    schema_id = client.register_schema("site-value", Schema(json.dumps(document), "JSON"))
    serializer = serdes_over(client).serializer(shape, value_format="json", registry=ACCESS)
    assert serializer.description == f"json-schema:site-value@{schema_id}"
    payload = serializer({"site_id": 3, "region": "us", "opened_on": "2024-01-02"}, topic="sites")
    assert json.loads(payload[5:]) == {"site_id": 3, "region": "us", "opened_on": "2024-01-02"}
    document["required"].append("elevation")
    document["properties"]["elevation"] = {"type": "integer"}
    client.register_schema("site-value", Schema(json.dumps(document), "JSON"))
    with pytest.raises(FluxError) as caught:
        serdes_over(client).serializer(shape, value_format="json", registry=ACCESS)
    assert caught.value.code.code == "FLX-3515" and "'elevation'" in str(caught.value)


def test_protobuf_and_glue_subjects_are_refused(tmp_path: Path, stream: Stream) -> None:
    protobuf = (
        subject_stream(tmp_path / "p", stream, KAFKA_BINDING % ("protobuf", "latest"))
        if (tmp_path / "p").mkdir() is None
        else None
    )
    glue_binding = KAFKA_BINDING % ("avro", "latest")
    glue_binding = glue_binding.replace(
        "kind: confluent, url: http://localhost:8081,", "kind: glue,"
    )
    (tmp_path / "g").mkdir()
    glue = subject_stream(tmp_path / "g", stream, glue_binding)
    assert protobuf is not None
    for shape, word in ((protobuf, "Protobuf"), (glue, "glue")):
        with pytest.raises(FluxError) as caught:
            serdes_over(mock_registry()).serializer(shape, value_format="avro", registry=ACCESS)
        assert caught.value.code.code == "FLX-3515" and word in str(caught.value)


def test_a_subject_the_registry_does_not_hold_is_a_registry_error_without_the_url(
    tmp_path: Path, stream: Stream
) -> None:
    shape = subject_stream(tmp_path, stream, KAFKA_BINDING % ("avro", "latest"))
    with pytest.raises(FluxError) as caught:
        serdes_over(mock_registry()).serializer(shape, value_format="avro", registry=ACCESS)
    assert caught.value.code.code == "FLX-3505"
    assert "registry.invalid" not in str(caught.value) and caught.value.__cause__ is None


# -- exact values ----------------------------------------------------------------------


def _avro(fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "record", "name": "T", "fields": fields}


def test_a_decimal_is_exact_or_refused() -> None:
    convert = avro_converter(
        _avro(
            [
                {
                    "name": "amount",
                    "type": {"type": "bytes", "logicalType": "decimal", "precision": 6, "scale": 2},
                }
            ]
        )
    )
    assert convert({"amount": "1234.50"}) == {"amount": Decimal("1234.50")}
    assert convert({"amount": "7"}) == {"amount": Decimal("7.00")}
    assert convert({"amount": "-0.05"}) == {"amount": Decimal("-0.05")}
    for bad in ("1.234", "12345.67", "abc", "NaN", "1e400"):
        with pytest.raises(FluxError) as caught:
            convert({"amount": bad})
        assert caught.value.code.code == "FLX-3515"
        assert "'amount'" in str(caught.value) and bad not in str(caught.value)


def test_a_date_is_its_day_number_and_a_timestamp_its_milliseconds_or_microseconds() -> None:
    convert = avro_converter(
        _avro(
            [
                {"name": "d", "type": {"type": "int", "logicalType": "date"}},
                {"name": "t", "type": {"type": "long", "logicalType": "timestamp-millis"}},
                {"name": "u", "type": {"type": "long", "logicalType": "timestamp-micros"}},
                {"name": "l", "type": {"type": "long", "logicalType": "local-timestamp-millis"}},
                {"name": "s", "type": "string"},
            ]
        )
    )
    row = {
        "d": "1969-12-31",
        "t": "2024-02-29T23:59:59.123Z",
        "u": "2024-02-29T23:59:59.123Z",
        "l": "1970-01-01T00:00:00.001Z",
        "s": "2024-02-29",
    }
    assert convert(row) == {
        "d": -1,
        "t": 1_709_251_199_123,
        "u": 1_709_251_199_123_000,
        "l": 1,
        "s": "2024-02-29",
    }
    assert convert({**row, "d": "2024-02-29"})["d"] == 19_782
    for field, bad in (("d", "2024-13-01"), ("d", "yesterday"), ("t", "2024-02-30T00:00:00Z")):
        with pytest.raises(FluxError) as caught:
            convert({**row, field: bad})
        assert caught.value.code.code == "FLX-3515" and bad not in str(caught.value)


def test_integers_booleans_bytes_and_nulls() -> None:
    convert = avro_converter(
        _avro(
            [
                {"name": "n", "type": "long"},
                {"name": "b", "type": "boolean"},
                {"name": "raw", "type": "bytes"},
                {"name": "maybe", "type": ["null", "string"], "default": None},
                {"name": "must", "type": "string"},
            ]
        )
    )
    good = {"n": 5, "b": True, "raw": "AAEC", "maybe": None, "must": "x"}
    assert convert(good) == {"n": 5, "b": True, "raw": b"\x00\x01\x02", "maybe": None, "must": "x"}
    for field, bad in (("n", "5"), ("n", True), ("b", 1), ("raw", "not base64!"), ("must", None)):
        with pytest.raises(FluxError) as caught:
            convert({**good, field: bad})
        assert caught.value.code.code == "FLX-3515" and f"'{field}'" in str(caught.value)


def test_a_float_field_cannot_hold_a_rendered_value() -> None:
    for kind in ("double", "float"):
        with pytest.raises(FluxError) as caught:
            avro_converter(_avro([{"name": "x", "type": kind}]))
        assert caught.value.code.code == "FLX-3515" and "'x'" in str(caught.value)


def test_json_values_are_integers_decimals_and_text() -> None:
    convert = json_converter(
        {
            "type": "object",
            "properties": {
                "n": {"type": "integer"},
                "x": {"type": ["number", "null"]},
                "s": {"type": "string", "format": "date"},
                "b": {"type": "boolean"},
            },
        }
    )
    assert convert({"n": 3, "x": "0.10", "s": "2024-01-01", "b": False}) == {
        "n": 3,
        "x": Decimal("0.10"),
        "s": "2024-01-01",
        "b": False,
    }
    assert convert({"n": 3, "x": None})["x"] is None
    for bad in ({"n": "3"}, {"x": "ten"}, {"n": None}):
        with pytest.raises(FluxError) as caught:
            convert(bad)
        assert caught.value.code.code == "FLX-3515"
