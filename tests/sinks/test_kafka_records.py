# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink's records: keys, event time, headers, rows and Debezium's envelope (ADR 0038)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks._kafka_records import (
    ENVELOPE_SOURCE_FIELDS,
    KafkaRecord,
    RecordBuilder,
    encode_key,
    epoch_ms,
    headers_for,
)
from flux_engine.sinks._kafka_shape import Envelope, ShapeSettings, StreamShape, stream_shapes

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "kafka-devices"
DIGEST = "sha256:" + "ab" * 32
DAY = 86_400_000


def refusal(call: Any) -> str:
    with pytest.raises(FluxError) as caught:
        call()
    assert caught.value.code.code == "FLX-3513"
    return str(caught.value)


def shape_of(
    columns: Sequence[Tuple[str, str]],
    *,
    key: Sequence[str],
    timestamp: Optional[str],
    default: Optional[str] = None,
    envelope: Optional[Envelope] = None,
    key_format: str = "string",
    role: str = "table",
) -> StreamShape:
    return StreamShape(
        expose_id="thing",
        role=role,
        topic="sim.thing",
        columns=tuple(name for name, _ in columns),
        types=tuple(kind for _, kind in columns),
        key_columns=tuple(key),
        key_format=key_format,
        timestamp_column=timestamp,
        timestamp_default=default,
        envelope=envelope,
        compacted=role == "snapshot",
        subject=None,
        product_ref="fleet.devices",
        simulation_id="sim-devices",
        contract_digest=DIGEST,
    )


def builder(shape: StreamShape) -> RecordBuilder:
    return RecordBuilder(shape, run_id="run-1", output_digest=DIGEST, engine_version="9.9.9")


# --- epoch_ms --------------------------------------------------------------------------------


def test_a_date_is_its_midnight() -> None:
    assert epoch_ms("1970-01-01", "DATE", column="d", row=0) == 0
    assert epoch_ms("1970-01-02", "DATE", column="d", row=0) == DAY
    assert epoch_ms("2024-02-29", "DATE", column="d", row=0) == 1_709_164_800_000


def test_a_date_before_1970_is_negative() -> None:
    assert epoch_ms("1969-12-31", "DATE", column="d", row=0) == -DAY
    assert epoch_ms("0001-01-01", "DATE", column="d", row=0) == -62_135_596_800_000


def test_a_timestamp_is_exact_to_the_millisecond() -> None:
    assert epoch_ms("1970-01-01T00:00:00.000Z", "TIMESTAMP", column="t", row=0) == 0
    assert epoch_ms("2025-12-31T23:59:59.999Z", "TIMESTAMP", column="t", row=0) == 1_767_225_599_999
    assert epoch_ms("1969-12-31T23:59:59.999Z", "TIMESTAMP", column="t", row=0) == -1


@pytest.mark.parametrize("kind", ["DATE", "TIMESTAMP"])
def test_a_null_has_no_event_time(kind: str) -> None:
    text = refusal(lambda: epoch_ms(None, kind, column="when", row=7))
    assert "when" in text and "row 7" in text and "null" in text


@pytest.mark.parametrize("kind", ["INT", "STRING", "BOOL", "DECIMAL(10,2)"])
def test_another_type_has_no_event_time(kind: str) -> None:
    assert "when" in refusal(lambda: epoch_ms("2024-01-01", kind, column="when", row=0))


@pytest.mark.parametrize(
    ("rendered", "kind"),
    [
        ("2024-13-01", "DATE"),
        ("24-01-01", "DATE"),
        ("2024-01-01T00:00:00Z", "DATE"),
        (5, "DATE"),
        ("2024-01-01", "TIMESTAMP"),
        ("2024-01-01T00:00:00", "TIMESTAMP"),
        (True, "TIMESTAMP"),
    ],
)
def test_text_that_is_not_the_type_is_refused_without_echoing_it(
    rendered: object, kind: str
) -> None:
    text = refusal(lambda: epoch_ms(rendered, kind, column="when", row=3))
    assert "when" in text and "row 3" in text
    if isinstance(rendered, str):
        assert rendered not in text


# --- keys ------------------------------------------------------------------------------------


def test_a_string_key_is_the_rendered_text() -> None:
    assert encode_key(["abc"], ["k"], "string", row=0) == b"abc"
    assert encode_key([42], ["k"], "string", row=0) == b"42"
    assert encode_key([-7], ["k"], "string", row=0) == b"-7"
    assert encode_key([True], ["k"], "string", row=0) == b"true"
    assert encode_key([False], ["k"], "string", row=0) == b"false"
    assert encode_key(["héllo"], ["k"], "string", row=0) == "héllo".encode()


def test_a_json_key_is_the_canonical_object() -> None:
    assert encode_key([5], ["id"], "json", row=0) == b'{"id":5}'
    assert encode_key(["x", 2], ["b", "a"], "json", row=0) == b'{"a":2,"b":"x"}'


@pytest.mark.parametrize("form", ["string", "json"])
def test_a_null_key_value_is_refused_naming_the_column_and_row(form: str) -> None:
    text = refusal(lambda: encode_key([None], ["site_id"], form, row=12))
    assert "site_id" in text and "row 12" in text


def test_a_string_key_of_two_columns_is_refused() -> None:
    assert "key-format=json" in refusal(lambda: encode_key([1, 2], ["a", "b"], "string", row=0))


def test_the_same_key_has_the_same_bytes() -> None:
    assert encode_key([1, "a"], ["x", "y"], "json", row=0) == encode_key(
        [1, "a"], ["x", "y"], "json", row=99
    )


# --- headers ---------------------------------------------------------------------------------


def test_headers_label_a_record_synthetic_and_name_its_stream() -> None:
    assert headers_for(run_id="run-1", expose_id="site", output_digest=DIGEST, row=4) == (
        ("flux.synthetic", b"true"),
        ("flux.run-id", b"run-1"),
        ("flux.expose", b"site"),
        ("flux.output-digest", DIGEST.encode()),
        ("flux.row", b"4"),
    )


# --- rows ------------------------------------------------------------------------------------


TABLE = (
    ("site_id", "INT"),
    ("region", "STRING"),
    ("opened_on", "DATE"),
    ("value", "DECIMAL(10,2)"),
)


def test_a_row_is_one_record_of_its_columns() -> None:
    shape = shape_of(TABLE, key=["site_id"], timestamp="opened_on")
    (record,) = builder(shape).records([7, "eu", "2024-01-02", "10.50"], 3, last=False)
    assert record.key == b"7" and record.entity == b"7"
    assert record.value == {
        "site_id": 7,
        "region": "eu",
        "opened_on": "2024-01-02",
        "value": "10.50",
    }
    assert list(record.value or {}) == ["site_id", "region", "opened_on", "value"]
    assert record.timestamp_ms == epoch_ms("2024-01-02", "DATE", column="d", row=0)
    assert record.row == 3
    assert dict(record.headers)["flux.row"] == b"3"
    assert dict(record.headers)["flux.expose"] == b"thing"


def test_a_json_keyed_row() -> None:
    shape = shape_of(TABLE, key=["site_id", "region"], timestamp="opened_on", key_format="json")
    (record,) = builder(shape).records([7, "eu", "2024-01-02", None], 0, last=False)
    assert record.key == b'{"region":"eu","site_id":7}'
    assert record.value is not None and record.value["value"] is None


def test_a_row_before_1970_keeps_its_negative_timestamp() -> None:
    shape = shape_of(TABLE, key=["site_id"], timestamp="opened_on")
    (record,) = builder(shape).records([1, "eu", "1969-12-31", None], 0, last=False)
    assert record.timestamp_ms == -DAY


def test_a_null_event_time_or_key_stops_the_row() -> None:
    shape = shape_of(TABLE, key=["site_id"], timestamp="opened_on")
    assert "opened_on" in refusal(
        lambda: builder(shape).records([1, "eu", None, None], 5, last=False)
    )
    assert "site_id" in refusal(
        lambda: builder(shape).records([None, "eu", "2024-01-01", None], 5, last=False)
    )


def test_the_default_time_stands_in_when_no_column_has_one() -> None:
    shape = shape_of(TABLE, key=["site_id"], timestamp=None, default="2025-12-31")
    (record,) = builder(shape).records([1, "eu", "2024-01-01", None], 0, last=False)
    assert record.timestamp_ms == epoch_ms("2025-12-31", "DATE", column="d", row=0)


def test_a_default_that_is_an_instant_with_a_time_is_read_as_one() -> None:
    shape = shape_of(TABLE, key=["site_id"], timestamp=None, default="2025-12-31T10:00:00.000Z")
    (record,) = builder(shape).records([1, "eu", "2024-01-01", None], 0, last=False)
    assert record.timestamp_ms == 1_767_175_200_000


def test_the_same_row_gives_equal_records() -> None:
    shape = shape_of(TABLE, key=["site_id"], timestamp="opened_on")
    row = [7, "eu", "2024-01-02", "10.50"]
    assert builder(shape).records(row, 1, last=False) == builder(shape).records(
        list(row), 1, last=False
    )


# --- events and snapshots --------------------------------------------------------------------


# event_id, device_id, op, changed_at, recorded_at, before_firmware, after_firmware
def event(
    op: str, before: Optional[str], after: Optional[str], *, at: str = "2024-03-01"
) -> List[object]:
    return [90, 17, op, at, "2024-03-05", before, after]


def build(
    shape: StreamShape, row: List[object], index: int = 0, *, last: bool = False
) -> List[KafkaRecord]:
    return builder(shape).records(row, index, last=last)


def test_a_create_has_no_before() -> None:
    (record,) = build(shapes_cache()["device_change"], event("c", None, "v2"))
    assert record.value == {
        "before": None,
        "after": {"device_id": 17, "firmware": "v2"},
        "source": {
            "version": "9.9.9",
            "connector": "flux",
            "name": "sim-devices",
            "ts_ms": epoch_ms("2024-03-01", "DATE", column="d", row=0),
            "snapshot": "false",
            "db": "fleet.devices",
            "table": "device_version",
            "event_id": 90,
        },
        "op": "c",
        "ts_ms": epoch_ms("2024-03-05", "DATE", column="d", row=0),
    }
    assert record.timestamp_ms == epoch_ms("2024-03-05", "DATE", column="d", row=0)
    assert record.key == b"17"
    assert list(record.value or {}) == ["before", "after", "source", "op", "ts_ms"]


def test_an_update_has_the_key_and_the_previous_values_before() -> None:
    (record,) = build(shapes_cache()["device_change"], event("u", "v1", "v2"))
    assert record.value is not None
    assert record.value["before"] == {"device_id": 17, "firmware": "v1"}
    assert record.value["after"] == {"device_id": 17, "firmware": "v2"}
    assert record.value["op"] == "u"


def test_a_delete_has_the_last_version_before_and_a_tombstone_after() -> None:
    records = build(shapes_cache()["device_change"], event("d", "v2", None), 4)
    assert len(records) == 2
    delete, tombstone = records
    assert delete.value is not None
    assert delete.value["after"] is None
    assert delete.value["before"] == {"device_id": 17, "firmware": "v2"}
    assert tombstone.value is None
    assert (tombstone.key, tombstone.timestamp_ms, tombstone.headers, tombstone.row) == (
        delete.key,
        delete.timestamp_ms,
        delete.headers,
        delete.row,
    )
    assert tombstone.entity == delete.entity == b"17"


def test_the_source_instant_is_the_change_and_the_event_time_is_the_recording() -> None:
    (record,) = build(shapes_cache()["device_change"], event("c", None, "v2", at="2024-01-01"))
    assert record.value is not None
    assert record.value["source"]["ts_ms"] == epoch_ms("2024-01-01", "DATE", column="d", row=0)
    assert record.value["ts_ms"] == epoch_ms("2024-03-05", "DATE", column="d", row=0)


@pytest.mark.parametrize("op", ["r", "x", "", None, 3])
def test_an_op_that_is_not_c_u_or_d_is_refused(op: object) -> None:
    text = refusal(lambda: build(shapes_cache()["device_change"], event(op, None, "v2"), 6))  # type: ignore[arg-type]
    assert "op" in text and "row 6" in text


def test_an_events_row_without_a_date_has_no_event_time() -> None:
    row = event("c", None, "v2")
    row[4] = None
    assert "recorded_at" in refusal(lambda: build(shapes_cache()["device_change"], row))


def test_an_event_without_an_at_column_uses_the_date_for_the_source() -> None:
    shape = shapes_cache()["device_change"]
    assert shape.envelope is not None
    flat = replace(shape, envelope=replace(shape.envelope, at_column=None))
    (record,) = build(flat, event("c", None, "v2", at="2000-01-01"))
    assert record.value is not None
    assert record.value["source"]["ts_ms"] == record.value["ts_ms"]


def test_a_snapshot_row_is_a_read_of_the_table() -> None:
    shape = shapes_cache()["device_current"]
    row = [17, "v2", "active", "2023-02-02"]
    (first,) = build(shape, row, 0, last=False)
    (final,) = build(shape, row, 1, last=True)
    ts = epoch_ms("2025-12-31", "DATE", column="d", row=0)
    assert first.value == {
        "before": None,
        "after": {
            "device_id": 17,
            "firmware": "v2",
            "status": "active",
            "installed_on": "2023-02-02",
        },
        "source": {
            "version": "9.9.9",
            "connector": "flux",
            "name": "sim-devices",
            "ts_ms": ts,
            "snapshot": "true",
            "db": "fleet.devices",
            "table": "device_version",
        },
        "op": "r",
        "ts_ms": ts,
    }
    assert first.timestamp_ms == ts and first.key == b"17"
    assert final.value is not None and final.value["source"]["snapshot"] == "last"
    assert first.value is not None and first.value["source"]["snapshot"] == "true"


def test_the_other_roles_are_written_as_rows() -> None:
    month = shapes_cache()["device_month"]
    (record,) = build(month, [1, 17, "2024-03-01", "v2"])
    assert record.value == {
        "period_id": 1,
        "device_id": 17,
        "month": "2024-03-01",
        "firmware": "v2",
    }
    assert record.key == b"17"
    assert record.timestamp_ms == epoch_ms("2024-03-01", "DATE", column="d", row=0)


def test_the_envelope_names_the_connectors_source_fields() -> None:
    assert ENVELOPE_SOURCE_FIELDS == (
        "version",
        "connector",
        "name",
        "ts_ms",
        "snapshot",
        "db",
        "table",
    )


def test_envelope_records_are_the_same_every_time() -> None:
    shape = shapes_cache()["device_change"]
    assert build(shape, event("d", "v2", None), 2) == build(shape, event("d", "v2", None), 2)


_CACHE: Dict[str, StreamShape] = {}


def shapes_cache() -> Dict[str, StreamShape]:
    if not _CACHE:
        _CACHE.update(stream_shapes(FIXTURE, "sim-devices", ShapeSettings()))
    return _CACHE
