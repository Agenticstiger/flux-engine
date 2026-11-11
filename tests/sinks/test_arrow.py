# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Arrow batching layer: the type map, batching, errors, and the way back.

The round trip is checked against the engine's own emit path, not against
the module under test: rows are rendered by ``render_row`` and the expected
``outputDigest`` is §8's formula over ``row_line``, so a batching layer that
agreed with itself while drifting from the seam would fail here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core.canonical import JCS_MAX_SAFE_INTEGER, canonical_bytes
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.seam.contract import LogicalKind, parse_logical_type
from flux_engine.seam.emit import emit_header_document

from .conftest import CONTRACT_DIGEST, EVERY_TYPE, compiled, expected_digest, header_of, rendered

pa = pytest.importorskip("pyarrow")
from flux_engine.sinks import arrow as sink_arrow  # noqa: E402  (after the importorskip)

pytestmark = pytest.mark.unit

SAMPLE_ROWS: List[Dict[str, Any]] = [
    {
        "s": "café",
        "i": -JCS_MAX_SAFE_INTEGER,
        "d": Decimal("12345678901234.5678"),
        "w": Decimal("-1234567890123456789012345678901234567890.0000000001"),
        "b": True,
        "day": date(2026, 7, 1),
        "at": datetime(2026, 7, 1, 12, 30, 45, 123000, tzinfo=timezone.utc),
        "j": {"b": [1, "x", None], "a": True},
        "raw": b"\x00\xffbytes",
    },
    {name: None for name in (column["name"] for column in EVERY_TYPE)},
    {
        "s": "",
        "i": JCS_MAX_SAFE_INTEGER,
        "d": Decimal("-0.0000"),
        "w": 0,
        "b": False,
        "day": "0001-01-01",
        "at": "1970-01-01T00:00:00Z",
        "j": [],
        "raw": b"",
    },
]


def test_header_document_is_the_seams_own() -> None:
    expose = compiled(EVERY_TYPE)
    assert sink_arrow.header_document(header_of(expose)) == emit_header_document(
        expose, CONTRACT_DIGEST
    )


def test_every_logical_type_maps_to_its_exact_arrow_type() -> None:
    schema = sink_arrow.arrow_schema(header_of(compiled(EVERY_TYPE)))
    assert [str(field.type) for field in schema] == [
        "string",
        "int64",
        "decimal128(18, 4)",
        "decimal256(50, 10)",
        "bool",
        "date32[day]",
        "timestamp[ms, tz=UTC]",
        "string",
        "binary",
    ]
    assert [field.metadata[sink_arrow.TYPE_KEY].decode() for field in schema] == [
        "STRING",
        "INT",
        "DECIMAL(18,4)",
        "DECIMAL(50,10)",
        "BOOL",
        "DATE",
        "TIMESTAMP",
        "JSON",
        "BYTES",
    ]


def test_the_schema_carries_the_header_as_its_canonical_bytes() -> None:
    expose = compiled(EVERY_TYPE)
    schema = sink_arrow.arrow_schema(header_of(expose), metadata={"flux.runId": "r1"})
    assert schema.metadata[sink_arrow.HEADER_KEY] == canonical_bytes(
        emit_header_document(expose, CONTRACT_DIGEST)
    )
    assert schema.metadata[b"flux.runId"] == b"r1"


def test_the_header_key_cannot_be_overwritten() -> None:
    with pytest.raises(ValueError, match="reserved"):
        sink_arrow.arrow_schema(
            header_of(compiled(EVERY_TYPE)), metadata={"flux.header": "something else"}
        )


def test_decimals_never_pass_through_float() -> None:
    expose = compiled(EVERY_TYPE)
    rows = rendered(expose, SAMPLE_ROWS)
    builder = sink_arrow.BatchBuilder(header_of(expose))
    batch = list(builder.extend(rows))[0]
    assert batch.column(2).to_pylist()[0] == Decimal("12345678901234.5678")
    assert batch.column(3).to_pylist()[0] == Decimal(
        "-1234567890123456789012345678901234567890.0000000001"
    )
    # The engine renders -0 as 0.0000, and the decimal keeps the scale.
    assert batch.column(2).to_pylist()[2] == Decimal("0.0000")
    assert batch.column(6).type == pa.timestamp("ms", tz="UTC")


def test_round_trip_of_every_type_reproduces_the_rows_and_the_digest() -> None:
    expose = compiled(EVERY_TYPE)
    rows = rendered(expose, SAMPLE_ROWS)
    builder = sink_arrow.BatchBuilder(header_of(expose), batch_size=2)
    table = pa.Table.from_batches(list(builder.extend(rows)), schema=builder.schema)
    assert list(sink_arrow.rows_from_table(table)) == rows
    assert sink_arrow.output_digest_of(table).value == expected_digest(expose, rows)


def test_batches_are_cut_at_the_batch_size() -> None:
    expose = compiled([{"name": "n", "type": "INT"}])
    builder = sink_arrow.BatchBuilder(header_of(expose), batch_size=4)
    sizes = [batch.num_rows for batch in builder.extend([[index] for index in range(10)])]
    assert sizes == [4, 4, 2]
    assert builder.rows == 10
    assert builder.flush() is None


@pytest.mark.parametrize("bad", [0, -1, True, 2.5, "8"])
def test_batch_size_must_be_a_positive_integer(bad: Any) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        sink_arrow.BatchBuilder(header_of(compiled(EVERY_TYPE)), batch_size=bad)


def test_an_empty_batch_has_the_schema() -> None:
    builder = sink_arrow.BatchBuilder(header_of(compiled(EVERY_TYPE)))
    empty = builder.empty_batch()
    assert empty.num_rows == 0
    assert empty.schema == builder.schema


def test_a_row_of_the_wrong_width_is_refused() -> None:
    builder = sink_arrow.BatchBuilder(header_of(compiled(EVERY_TYPE)))
    with pytest.raises(ValueError, match="row 0 has 2 values for 9 columns"):
        builder.append(["a", 1])


def test_an_int_outside_int64_names_the_column_and_row_but_not_the_value() -> None:
    expose = compiled([{"name": "n", "type": "INT"}])
    builder = sink_arrow.BatchBuilder(header_of(expose), batch_size=2)
    secret = 2**64 + 12345
    assert builder.append([1]) is None
    builder.append([2])  # completes the first batch
    builder.append([3])
    with pytest.raises(FluxError) as caught:
        builder.append([secret])
    assert caught.value.code.code == "FLX-3502"
    message = str(caught.value)
    assert "column 'n' (INT), row 3" in message
    assert "outside the signed 64-bit range" in message
    assert str(secret) not in message


def test_a_decimal_wider_than_76_digits_is_refused_before_any_row() -> None:
    expose = compiled([{"name": "huge", "type": "DECIMAL(77,2)"}])
    with pytest.raises(FluxError) as caught:
        sink_arrow.BatchBuilder(header_of(expose))
    assert caught.value.code.code == "FLX-3502"
    assert "'huge'" in str(caught.value)


def test_text_kinds_keep_the_rendering_itself() -> None:
    expose = compiled(EVERY_TYPE)
    rows = rendered(expose, SAMPLE_ROWS)
    builder = sink_arrow.BatchBuilder(
        header_of(expose), text_kinds={LogicalKind.DECIMAL, LogicalKind.TIMESTAMP}
    )
    table = pa.Table.from_batches(list(builder.extend(rows)), schema=builder.schema)
    assert str(table.schema.field("d").type) == "string"
    assert str(table.schema.field("at").type) == "string"
    assert table.column("d").to_pylist()[0] == "12345678901234.5678"
    assert table.column("at").to_pylist()[0] == "2026-07-01T12:30:45.123Z"
    assert list(sink_arrow.rows_from_table(table)) == rows
    assert sink_arrow.output_digest_of(table).value == expected_digest(expose, rows)


@pytest.mark.parametrize("kind", [LogicalKind.INT, LogicalKind.BOOL])
def test_kinds_that_do_not_render_as_text_cannot_be_kept_as_text(kind: LogicalKind) -> None:
    with pytest.raises(ValueError, match="do not render as text"):
        sink_arrow.BatchBuilder(header_of(compiled(EVERY_TYPE)), text_kinds={kind})


def test_reading_back_tolerates_what_databases_hand_back() -> None:
    """A database returns microsecond timestamps in its session zone and BOOL as 0/1."""
    expose = compiled(
        [
            {"name": "at", "type": "TIMESTAMP"},
            {"name": "flag", "type": "BOOL"},
            {"name": "day", "type": "DATE"},
        ]
    )
    rows = rendered(
        expose,
        [
            {"at": "2026-01-02T03:04:05.678Z", "flag": True, "day": "2026-01-02"},
            {"at": None, "flag": False, "day": None},
        ],
    )
    table = pa.table(
        {
            "at": pa.array([1767323045678000, None], pa.timestamp("us", tz="Europe/Zurich")),
            "flag": pa.array([1, 0], pa.int64()),
            "day": pa.array([date(2026, 1, 2), None], pa.date32()),
        }
    )
    assert list(sink_arrow.rows_from_table(table, types=list(expose.type_names))) == rows
    assert sink_arrow.output_digest_of(table, header=header_of(expose)).value == expected_digest(
        expose, rows
    )


@pytest.mark.parametrize("kind", ["STRING", "DATE", "TIMESTAMP", "JSON", "BYTES", "DECIMAL(18,4)"])
@pytest.mark.parametrize("rows", [0, 3])
def test_a_column_of_nulls_reads_back_whatever_type_it_was_stored_as(kind: str, rows: int) -> None:
    """SQLite's driver reports an all-null or empty column as int64."""
    table = pa.table({"x": pa.array([None] * rows, pa.int64())})
    assert list(sink_arrow.rows_from_table(table, types=[kind])) == [[None]] * rows


def test_sub_millisecond_timestamps_are_refused_on_the_way_back() -> None:
    table = pa.table({"at": pa.array([1767323045678001], pa.timestamp("us", tz="UTC"))})
    with pytest.raises(pa.ArrowInvalid):
        list(sink_arrow.rows_from_table(table, types=["TIMESTAMP"]))


@pytest.mark.parametrize(
    "table, types, needle",
    [
        (lambda: pa.table({"x": ["a"]}), None, "flux.type"),
        (lambda: pa.table({"x": ["true"]}), ["BOOL"], "stored as text"),
        (lambda: pa.table({"x": pa.array([2], pa.int64())}), ["BOOL"], "other than 0 or 1"),
        (lambda: pa.table({"x": pa.array([1], pa.int64())}), ["DATE"], "stored as Arrow int64"),
        (
            lambda: pa.table({"x": pa.array([Decimal("1.5")], pa.decimal128(10, 1))}),
            ["DECIMAL(10,2)"],
            "scale 1",
        ),
        (lambda: pa.table({"x": ["a"]}), ["STRING", "INT"], "2 logical types"),
    ],
)
def test_data_that_is_not_the_stream_is_refused(table: Any, types: Any, needle: str) -> None:
    with pytest.raises(FluxError) as caught:
        list(sink_arrow.rows_from_table(table(), types=types))
    assert caught.value.code.code == "FLX-3503"
    assert needle in str(caught.value)


def test_a_renamed_or_reordered_copy_does_not_digest() -> None:
    expose = compiled(EVERY_TYPE)
    rows = rendered(expose, SAMPLE_ROWS)
    builder = sink_arrow.BatchBuilder(header_of(expose))
    table = pa.Table.from_batches(list(builder.extend(rows)), schema=builder.schema)
    reordered = table.select(list(reversed(table.schema.names)))
    with pytest.raises(FluxError, match="not the stream's declared columns"):
        sink_arrow.output_digest_of(reordered)
    bare = table.replace_schema_metadata(None)
    with pytest.raises(FluxError, match="flux.header"):
        sink_arrow.output_digest_of(bare)


def test_a_forged_header_is_refused() -> None:
    expose = compiled([{"name": "n", "type": "INT"}])
    builder = sink_arrow.BatchBuilder(header_of(expose))
    table = pa.Table.from_batches(list(builder.extend([[1]])), schema=builder.schema)
    forged = table.replace_schema_metadata({sink_arrow.HEADER_KEY: json.dumps({"columns": ["n"]})})
    with pytest.raises(FluxError, match="not a KERNEL-SPEC"):
        sink_arrow.output_digest_of(forged)


# ---------------------------------------------------------------------------
# The property: rows -> Arrow -> rows is lossless for every type the seam renders
# ---------------------------------------------------------------------------

_NAMES = st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True)

#: Timestamps from year 1000 on: the engine renders the year with strftime's
#: %Y, which only some C libraries zero-pad below 1000 (KERNEL-SPEC §9 fixes
#: four digits). Dates use isoformat() and cover the whole range.
_TS_MIN = parse_rfc3339_ms("1000-01-01T00:00:00.000Z")
_TS_MAX = parse_rfc3339_ms("9999-12-31T23:59:59.999Z")

_JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-JCS_MAX_SAFE_INTEGER, max_value=JCS_MAX_SAFE_INTEGER)
    | st.text(max_size=6),
    lambda children: st.lists(children, max_size=3)
    | st.dictionaries(st.text(max_size=4), children, max_size=3),
    max_leaves=6,
)


@st.composite
def _column(draw: Any) -> str:
    kind = draw(
        st.sampled_from(["STRING", "INT", "DECIMAL", "BOOL", "DATE", "TIMESTAMP", "JSON", "BYTES"])
    )
    if kind != "DECIMAL":
        return str(kind)
    precision = draw(st.integers(min_value=1, max_value=76))
    scale = draw(st.integers(min_value=0, max_value=precision))
    return f"DECIMAL({precision},{scale})"


def _values(spelling: str) -> Any:
    logical = parse_logical_type(spelling)
    kind = logical.kind
    if kind is LogicalKind.STRING:
        value = st.text(max_size=12)
    elif kind is LogicalKind.INT:
        # The stream is JCS, so an INT on it is a JSON safe integer.
        value = st.integers(min_value=-JCS_MAX_SAFE_INTEGER, max_value=JCS_MAX_SAFE_INTEGER)
    elif kind is LogicalKind.DECIMAL:
        assert logical.precision is not None and logical.scale is not None
        bound = 10**logical.precision - 1
        scale = logical.scale
        # From the exact string: Decimal.scaleb would round to the default
        # 28-digit context and invent an integer digit at high precision.
        value = st.integers(min_value=-bound, max_value=bound).map(
            lambda unscaled: Decimal(f"{unscaled}e-{scale}")
        )
    elif kind is LogicalKind.BOOL:
        value = st.booleans()
    elif kind is LogicalKind.DATE:
        value = st.dates()
    elif kind is LogicalKind.TIMESTAMP:
        value = st.integers(min_value=_TS_MIN, max_value=_TS_MAX)
    elif kind is LogicalKind.JSON:
        value = _JSON
    else:
        value = st.binary(max_size=12)
    return st.none() | value


@st.composite
def _stream(draw: Any) -> Any:
    names = draw(st.lists(_NAMES, min_size=1, max_size=6, unique=True))
    schema = [{"name": name, "type": draw(_column())} for name in names]
    expose = compiled(schema)
    raw_rows = draw(
        st.lists(
            st.fixed_dictionaries(
                {
                    name: _values(spelling)
                    for name, spelling in zip(names, expose.type_names, strict=True)
                }
            ),
            max_size=25,
        )
    )
    return expose, rendered(expose, raw_rows)


@pytest.mark.property
@settings(max_examples=200)
@given(
    stream=_stream(),
    batch_size=st.integers(min_value=1, max_value=9),
    text_kinds=st.sets(st.sampled_from(sorted(sink_arrow.TEXT_RENDERED))),
)
def test_rows_to_arrow_to_rows_is_lossless(stream: Any, batch_size: int, text_kinds: Any) -> None:
    expose, rows = stream
    builder = sink_arrow.BatchBuilder(
        header_of(expose), batch_size=batch_size, text_kinds=text_kinds
    )
    batches = list(builder.extend(rows))
    assert all(batch.num_rows <= batch_size for batch in batches)
    table = pa.Table.from_batches(batches, schema=builder.schema)
    assert table.num_rows == len(rows)
    assert list(sink_arrow.rows_from_table(table)) == rows
    assert sink_arrow.output_digest_of(table).value == expected_digest(expose, rows)


# ---------------------------------------------------------------------------
# Without the extra
# ---------------------------------------------------------------------------


def test_without_pyarrow_the_import_names_the_extra() -> None:
    probe = (
        "import sys; sys.modules['pyarrow'] = None\n"
        "from flux_engine.core.errors import FluxError\n"
        "try:\n"
        "    import flux_engine.sinks.arrow\n"
        "except FluxError as error:\n"
        "    print(error)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.startswith("FLX-3501: ")
    assert "pip install 'flux-sim-engine[arrow]'" in result.stdout
