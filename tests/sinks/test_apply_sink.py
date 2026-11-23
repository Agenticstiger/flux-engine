# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim apply --sink``, the column types the file sinks write, and what a receipt records."""

from __future__ import annotations

import json
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks.receipts import StreamContext
from flux_engine.stages import pipeline
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan

from .conftest import (
    CONTRACT_DIGEST,
    compiled,
    expected_ref,
    header_of,
    pyarrow_or_skip,
    rendered,
)

pa = pyarrow_or_skip()
from flux_engine.sinks import parquet  # noqa: E402  (after the skip)
from flux_engine.sinks.arrow import BatchBuilder, column_layouts, output_digest_of  # noqa: E402

pq = import_module("pyarrow.parquet")

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"


def _document(state: Path, run_id: str) -> Dict[str, Any]:
    document: Dict[str, Any] = json.loads(
        (state / "runs" / run_id / "sink-receipts.json").read_text(encoding="utf-8")
    )
    return document


def test_apply_writes_a_sink_from_a_fresh_plan(tmp_path: Path) -> None:
    state, out = tmp_path / "state", tmp_path / "pq"
    result = apply(FIXTURE, state_dir=state, sink=f"parquet:{out}", sink_options={"verify": "true"})
    assert result.success, result.stderr
    [receipt] = result.artifacts["sinks"]["receipts"]
    assert receipt["outputDigest"] == (
        result.artifacts["receipt"]["emits"]["payment_recovery_moment"]["outputDigest"]
    )
    assert _document(state, result.artifacts["run_id"])["receipts"] == [receipt]
    assert "delivered payment_recovery_moment: 64 rows in 1 parquet file" in result.stdout


def test_apply_run_id_writes_the_sink_from_the_committed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, out = tmp_path / "state", tmp_path / "pq"
    planned = plan(FIXTURE, state_dir=state)
    assert planned.success, planned.stderr

    def generated(*args: object, **kwargs: object) -> None:
        raise AssertionError("apply generated rows")

    monkeypatch.setattr(pipeline, "execute", generated)
    result = apply(
        FIXTURE, run_id=planned.artifacts["run_id"], state_dir=state, sink=f"parquet:{out}"
    )
    assert result.success, result.stderr
    assert result.artifacts["committed_from_stage"] is True
    [receipt] = result.artifacts["sinks"]["receipts"]
    manifest = (
        out
        / "payment_recovery_moment"
        / (f"_manifest-{receipt['outputDigest'][7:23]}-parquet.json")
    )
    assert output_digest_of(parquet.read_stream(manifest)).value == receipt["outputDigest"]


def test_a_stream_that_no_longer_matches_the_receipt_is_not_delivered(tmp_path: Path) -> None:
    from flux_engine.sinks.registry import open_target
    from flux_engine.stages.deliver import deliver_run
    from flux_engine.stages.layout import RunLayout

    state, out = tmp_path / "state", tmp_path / "pq"
    planned = plan(FIXTURE, state_dir=state, stage=False)
    run_id = planned.artifacts["run_id"]
    assert apply(FIXTURE, run_id=run_id, state_dir=state).success
    layout = RunLayout.for_state_dir(state, run_id)
    stream = layout.emit_path("payment_recovery_moment")
    stream.write_bytes(stream.read_bytes().replace(b"1", b"2", 1))
    with pytest.raises(FluxError) as refused:
        deliver_run(layout, FIXTURE, target=open_target(f"parquet:{out}"))
    assert refused.value.code.code == "FLX-3505"
    assert not out.exists() or not list(out.rglob("*.parquet"))


def test_a_sink_set_up_wrong_stops_apply_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def generated(*args: object, **kwargs: object) -> None:
        raise AssertionError("work started")

    monkeypatch.setattr(pipeline, "execute", generated)
    result = apply(FIXTURE, state_dir=tmp_path / "state", sink="nonesuch:x")
    assert result.exit_code == 2 and result.error_code is not None
    assert apply(FIXTURE, state_dir=tmp_path, sink_options={"a": "b"}).exit_code == 2
    assert apply(FIXTURE, state_dir=tmp_path, sink="parquet:x", shard="1/2").exit_code == 2


# --- the types a file sink writes --------------------------------------------------------------


def _types(*declared: str) -> Any:
    schema = [{"name": f"c{i}", "type": text} for i, text in enumerate(declared)]
    return compiled(schema)


def test_an_integer_column_takes_the_width_its_contract_names() -> None:
    declared = ("smallint", "INTEGER(11)", "bigint", "int", "tinyint", "mediumint", "year")
    expose = _types(*declared)
    layouts = column_layouts(
        header_of(expose),
        declared_types=[column.declared_type for column in expose.columns],
    )
    assert [str(layout.arrow_type) for layout in layouts] == [
        "int16",
        "int32",
        "int64",
        "int64",
        "int8",
        "int32",
        "int64",
    ]
    # Without the contract's spellings, as before: every integer is an int64.
    assert {str(layout.arrow_type) for layout in column_layouts(header_of(expose))} == {"int64"}


def test_a_value_the_declared_width_cannot_hold_is_refused_by_name() -> None:
    expose = _types("smallint")
    builder = BatchBuilder(header_of(expose), declared_types=["smallint"])
    builder.append([40000])
    with pytest.raises(FluxError) as refused:
        builder.flush()
    assert refused.value.code.code == "FLX-3502"
    assert "int16" in str(refused.value) and "smallint" in str(refused.value)


def test_a_parquet_file_carries_the_contract_widths_and_decimal_precision(
    tmp_path: Path,
) -> None:
    expose = _types("smallint", "integer", "bigint", "decimal(7, 2)", "DECIMAL(18,4)")
    rows = rendered(
        expose,
        [
            {
                "c0": 7,
                "c1": 70000,
                "c2": 7_000_000_000,
                "c3": Decimal("12.50"),
                "c4": Decimal("1.2345"),
            },
            {"c0": -3, "c1": -1, "c2": 1, "c3": Decimal("0.01"), "c4": Decimal("0.0001")},
        ],
    )
    context = StreamContext(
        run_id="r",
        expose_id=expose.expose_id,
        rows=len(rows),
        output_digest=expected_ref(expose, rows),
        declared_types=tuple(column.declared_type for column in expose.columns),
    )
    sink = parquet.ParquetSink(tmp_path, context)
    sink.open(header_of(expose, CONTRACT_DIGEST))
    for row in rows:
        sink.write(row)
    sink.close()
    receipt = sink.receipt
    assert receipt is not None
    schema = pq.read_schema(str(tmp_path / receipt.files[0].path))
    assert [str(field.type) for field in schema] == [
        "int16",
        "int32",
        "int64",
        "decimal128(7, 2)",
        "decimal128(18, 4)",
    ]
    # The digest of the stream does not depend on the width the file stores it in.
    assert output_digest_of(pq.read_table(str(tmp_path / receipt.files[0].path))).value == (
        context.output_digest.value
    )


def test_a_receipt_names_the_parquet_writer_and_its_version(tmp_path: Path) -> None:
    result = apply(FIXTURE, state_dir=tmp_path / "state", sink=f"parquet:{tmp_path / 'pq'}")
    assert result.success, result.stderr
    [receipt] = result.artifacts["sinks"]["receipts"]
    assert receipt["writer"]["library"] == "pyarrow"
    assert receipt["writer"]["version"] == pa.__version__
    assert receipt["writer"]["writer"] == f"parquet-cpp-arrow version {pa.__version__}"
