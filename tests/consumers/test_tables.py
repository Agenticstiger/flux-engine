# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The tables a run's emitted files make, and the counts over them (ADR 0028 section 3)."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Sequence

import pytest

pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")

from flux_engine.api.emit import EmitHeader  # noqa: E402
from flux_engine.consumers.runner import CountRequest, run_counts  # noqa: E402
from flux_engine.consumers.tables import RunStream, tables_from_run  # noqa: E402
from flux_engine.core.canonical import canonical_array_bytes  # noqa: E402
from flux_engine.core.digest import digest_of_document  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.safeio import UnsafeFileError  # noqa: E402
from flux_engine.seam.consumers import Limits  # noqa: E402

DIGEST = digest_of_document({"id": "test.sim.subscriptions"}, subject="contractDigest")


def header(expose_id: str = "subscriptions") -> EmitHeader:
    return EmitHeader(
        expose_id=expose_id,
        columns=("plan", "seats", "price", "renewed", "starts"),
        types=("STRING", "INT", "DECIMAL(10,2)", "BOOL", "DATE"),
        contract_digest=DIGEST,
    )


ROWS: List[List[Any]] = [
    ["pro", 3, "10.50", True, "2026-01-02"],
    ["basic", 1, "5.00", False, "2026-02-03"],
    [None, 2, "7.25", True, "2026-03-04"],
]


def write_stream(emits: Path, expose_id: str, rows: Sequence[Sequence[Any]]) -> Path:
    emits.mkdir(parents=True, exist_ok=True)
    path = emits / f"{expose_id}.ndjson"
    path.write_bytes(b"".join(canonical_array_bytes(list(row)) + b"\n" for row in rows))
    return path


def test_each_expose_becomes_an_arrow_table_with_the_contracts_types(tmp_path: Path) -> None:
    write_stream(tmp_path / "emits", "subscriptions", ROWS)
    streams = [RunStream(header(), ("varchar", "smallint", "decimal(10, 2)", "boolean", "date"))]
    tables = tables_from_run(tmp_path / "emits", streams)
    table = tables["subscriptions"]
    assert table.schema.names == ["plan", "seats", "price", "renewed", "starts"]
    assert table.schema.types == [
        pa.string(),
        pa.int16(),
        pa.decimal128(10, 2),
        pa.bool_(),
        pa.date32(),
    ]
    assert table.num_rows == 3
    assert table.column("price").to_pylist() == [Decimal("10.50"), Decimal("5.00"), Decimal("7.25")]
    assert table.column("plan").to_pylist() == ["pro", "basic", None]


def test_an_empty_stream_is_an_empty_table_with_its_schema(tmp_path: Path) -> None:
    (tmp_path / "emits").mkdir()
    (tmp_path / "emits" / "subscriptions.ndjson").write_bytes(b"")
    table = tables_from_run(tmp_path / "emits", [RunStream(header())])["subscriptions"]
    assert table.num_rows == 0 and table.schema.names[0] == "plan"


def test_the_tables_feed_the_counts_end_to_end(tmp_path: Path) -> None:
    write_stream(tmp_path / "emits", "subscriptions", ROWS)
    tables = tables_from_run(tmp_path / "emits", [RunStream(header())])
    requests = [
        CountRequest(
            "renewed",
            'SELECT * FROM "subscriptions" WHERE renewed AND price > 6',
            'SELECT * FROM "subscriptions"',
        ),
        CountRequest(
            "by-plan",
            'SELECT * FROM "subscriptions" WHERE renewed',
            'SELECT * FROM "subscriptions"',
            group_by="plan",
        ),
    ]
    renewed, by_plan = run_counts(tables, requests, Limits(seconds=20, memory=256 << 20)).queries
    assert (renewed.status, renewed.rows, renewed.unit_rows) == ("counted", 2, 3)
    assert by_plan.row_groups == ((None, 1), ("pro", 1))
    assert by_plan.unit_groups == ((None, 1), ("basic", 1), ("pro", 1))


def test_only_the_named_streams_are_read(tmp_path: Path) -> None:
    emits = tmp_path / "emits"
    write_stream(emits, "subscriptions", ROWS)
    (emits / "other.ndjson").write_bytes(b"not json\n")
    assert list(tables_from_run(emits, [RunStream(header())])) == ["subscriptions"]


def test_a_stream_that_is_a_link_is_refused_and_not_followed(tmp_path: Path) -> None:
    emits = tmp_path / "emits"
    secret = write_stream(tmp_path / "elsewhere", "secret", ROWS)
    emits.mkdir()
    os.symlink(secret, emits / "subscriptions.ndjson")
    with pytest.raises(UnsafeFileError) as caught:
        tables_from_run(emits, [RunStream(header())])
    assert caught.value.code.code == "FLX-2117"


def test_an_emits_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    write_stream(real, "subscriptions", ROWS)
    os.symlink(real, tmp_path / "emits")
    with pytest.raises(FluxError) as caught:
        tables_from_run(tmp_path / "emits", [RunStream(header())])
    assert caught.value.code.code == "FLX-2118"


def test_a_missing_stream_is_flx_7135_and_names_the_expose(tmp_path: Path) -> None:
    (tmp_path / "emits").mkdir()
    with pytest.raises(FluxError) as caught:
        tables_from_run(tmp_path / "emits", [RunStream(header())])
    assert caught.value.code.code == "FLX-7135"
    assert "subscriptions" in str(caught.value)


@pytest.mark.parametrize(
    "bad",
    [
        b"not json\n",
        b'["pro", 3, "1.00", true]\n',  # too few values
        b'["pro", 3, "1.00", true, "2026-01-01", 1]\n',  # too many
        b'{"plan": "pro"}\n',  # an object, not an array
        b'["pro", 3, 1.5, true, "2026-01-01"]\n',  # a float
        b'["pro", 3, NaN, true, "2026-01-01"]\n',
        b'["pro", 3, "1.00", true, "2026-01-01"]\n\n["pro", 3, "1.00", true, "2026-01-01"]\n',  # blank
        b"\xff\n",
    ],
)
def test_a_line_that_is_not_a_row_of_its_header_is_flx_7135(tmp_path: Path, bad: bytes) -> None:
    emits = tmp_path / "emits"
    emits.mkdir()
    (emits / "subscriptions.ndjson").write_bytes(bad)
    with pytest.raises(FluxError) as caught:
        tables_from_run(emits, [RunStream(header())])
    assert caught.value.code.code == "FLX-7135"
    assert "pro" not in str(caught.value)  # the message names the line, never a value


@pytest.mark.parametrize("expose_id", ["", "..", "a/b", "a\\b", "a\0b"])
def test_an_expose_id_is_a_single_file_name(tmp_path: Path, expose_id: str) -> None:
    (tmp_path / "emits").mkdir()
    with pytest.raises(FluxError) as caught:
        tables_from_run(tmp_path / "emits", [RunStream(header(expose_id))])
    assert caught.value.code.code == "FLX-7135"


def test_a_value_that_does_not_fit_its_type_is_refused_as_the_sinks_refuse_it(
    tmp_path: Path,
) -> None:
    write_stream(tmp_path / "emits", "subscriptions", [["pro", 40000, "1.00", True, "2026-01-01"]])
    with pytest.raises(FluxError) as caught:
        tables_from_run(
            tmp_path / "emits",
            [RunStream(header(), ("varchar", "smallint", "decimal(10, 2)", "boolean", "date"))],
        )
    assert caught.value.code.code == "FLX-3502"


def test_a_stream_file_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    emits = tmp_path / "emits"
    (emits / "subscriptions.ndjson").mkdir(parents=True)
    with pytest.raises(FluxError):
        tables_from_run(emits, [RunStream(header())])
