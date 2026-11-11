# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Parquet and Arrow IPC sinks: layout, manifest, determinism, and the read-back proof.

The proof the sinks exist to make is the last group of tests: a real run's
stream goes into a file, the file is read back and re-rendered, and the
``outputDigest`` it digests to is the one the engine computed above the
sink.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import pytest

from flux_engine.api.emit import EmitHeader
from flux_engine.core.errors import FluxError
from flux_engine.sinks.receipts import SinkReceipt, StreamContext

from .conftest import compiled, expected_ref, header_of, rendered

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
from flux_engine.sinks import arrow_ipc, parquet  # noqa: E402  (after the importorskip)
from flux_engine.sinks._files import MANIFEST_VERSION, load_manifest  # noqa: E402
from flux_engine.sinks.arrow import output_digest_of  # noqa: E402

pytestmark = pytest.mark.unit

SCHEMA = [
    {"name": "subscriber_id", "type": "STRING", "required": True},
    {"name": "aborted_at", "type": "TIMESTAMP"},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "market", "type": "STRING"},
]

SINKS: Dict[str, Callable[..., Any]] = {
    "parquet": parquet.ParquetSink,
    "arrow-ipc": arrow_ipc.ArrowIpcSink,
}
READERS: Dict[str, Callable[[Any], Any]] = {
    "parquet": parquet.read_stream,
    "arrow-ipc": arrow_ipc.read_stream,
}


def _rows(count: int, *, offset: int = 0) -> List[Dict[str, Any]]:
    return [
        {
            "subscriber_id": f"s-{offset + index:04d}",
            "aborted_at": datetime(2026, 7, 1, 12, 0, index % 60, tzinfo=timezone.utc),
            "amount": None if index % 5 == 0 else Decimal(index) / 8,
            "market": ["IE", "GB", "FR", "DE"][index % 4],
        }
        for index in range(count)
    ]


class Stream:
    """One stream: its expose, header, rendered rows and context."""

    def __init__(self, count: int, *, offset: int = 0, run_id: str = "run-1") -> None:
        self.expose = compiled(SCHEMA, expose_id="payment_recovery_moment")
        self.header: EmitHeader = header_of(self.expose)
        self.rows = rendered(self.expose, _rows(count, offset=offset))
        self.context = StreamContext(
            run_id=run_id,
            expose_id=self.expose.expose_id,
            rows=len(self.rows),
            output_digest=expected_ref(self.expose, self.rows),
        )


def _deliver(sink: Any, header: EmitHeader, rows: Sequence[Sequence[Any]]) -> Optional[SinkReceipt]:
    sink.open(header)
    try:
        for row in rows:
            sink.write(row)
    finally:
        sink.close()
    receipt: Optional[SinkReceipt] = sink.receipt
    return receipt


def _data_files(directory: Path) -> List[str]:
    return sorted(
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and not path.name.startswith(("_", "."))
    )


def _every_file(directory: Path) -> List[str]:
    return sorted(path.name for path in directory.rglob("*") if path.is_file())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(params=sorted(SINKS))
def kind(request: Any) -> str:
    return str(request.param)


# ---------------------------------------------------------------------------
# Layout and manifest
# ---------------------------------------------------------------------------


def test_one_stream_lands_as_one_slice_and_a_manifest(tmp_path: Path, kind: str) -> None:
    stream = Stream(10)
    receipt = _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    assert receipt is not None
    key = stream.context.slice_key
    suffix = SINKS[kind].suffix
    assert _every_file(tmp_path) == [f"_manifest-{key}-{kind}.json", f"{key}-00000{suffix}"]

    manifest_path = tmp_path / "payment_recovery_moment" / f"_manifest-{key}-{kind}.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["version"] == MANIFEST_VERSION
    assert manifest["synthetic"] is True
    assert manifest["sink"] == kind
    assert manifest["runId"] == "run-1"
    assert manifest["exposeId"] == "payment_recovery_moment"
    assert manifest["outputDigest"] == stream.context.output_digest.value
    assert manifest["rows"] == 10
    assert manifest["header"]["columns"] == [column["name"] for column in SCHEMA]
    assert manifest["writer"]["version"] == pa.__version__
    [entry] = manifest["files"]
    data = tmp_path / entry["path"]
    assert entry["sha256"] == _sha256(data)
    assert entry["bytes"] == data.stat().st_size
    assert entry["rows"] == 10
    # The receipt says the same as the manifest.
    assert receipt.as_document()["files"] == manifest["files"]
    assert receipt.rows == 10 and receipt.sink == kind and receipt.target == str(tmp_path)


@pytest.mark.parametrize("rows_per_file", [None, 3])
@pytest.mark.parametrize("batch_size", [1, 4, 1000])
def test_reading_the_files_back_gives_the_streams_output_digest(
    tmp_path: Path, kind: str, rows_per_file: Optional[int], batch_size: int
) -> None:
    stream = Stream(10)
    sink = SINKS[kind](tmp_path, stream.context, rows_per_file=rows_per_file, batch_size=batch_size)
    _deliver(sink, stream.header, stream.rows)
    manifest = (
        tmp_path / "payment_recovery_moment" / f"_manifest-{stream.context.slice_key}-{kind}.json"
    )
    table = READERS[kind](manifest)
    assert table.num_rows == 10
    assert output_digest_of(table).value == stream.context.output_digest.value


def test_parquet_bytes_do_not_depend_on_the_batch_size(tmp_path: Path) -> None:
    stream = Stream(10)
    hashes = []
    for batch_size in (1, 3, 1000):
        directory = tmp_path / str(batch_size)
        _deliver(
            parquet.ParquetSink(directory, stream.context, batch_size=batch_size, row_group_size=4),
            stream.header,
            stream.rows,
        )
        [path] = [directory / name for name in _data_files(directory)]
        hashes.append(_sha256(path))
    assert len(set(hashes)) == 1


def test_the_same_stream_twice_writes_the_same_bytes_under_the_same_names(
    tmp_path: Path, kind: str
) -> None:
    stream = Stream(10)
    first = _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    before = _every_file(tmp_path)
    second = _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    assert _every_file(tmp_path) == before
    assert first is not None and second is not None
    assert first.files == second.files


def test_a_rerun_with_fewer_files_removes_the_slices_stale_files(tmp_path: Path, kind: str) -> None:
    stream = Stream(10)
    _deliver(SINKS[kind](tmp_path, stream.context, rows_per_file=4), stream.header, stream.rows)
    assert len(_data_files(tmp_path)) == 3
    _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    assert len(_data_files(tmp_path)) == 1
    manifest = (
        tmp_path / "payment_recovery_moment" / f"_manifest-{stream.context.slice_key}-{kind}.json"
    )
    assert output_digest_of(READERS[kind](manifest)).value == stream.context.output_digest.value


def test_another_run_of_the_expose_lands_beside_the_first(tmp_path: Path, kind: str) -> None:
    first, second = Stream(10), Stream(6, offset=100, run_id="run-2")
    _deliver(SINKS[kind](tmp_path, first.context), first.header, first.rows)
    _deliver(SINKS[kind](tmp_path, second.context), second.header, second.rows)
    assert len(_data_files(tmp_path)) == 2
    for stream in (first, second):
        manifest = (
            tmp_path
            / "payment_recovery_moment"
            / f"_manifest-{stream.context.slice_key}-{kind}.json"
        )
        assert json.loads(manifest.read_text())["runId"] == stream.context.run_id


def test_rows_per_file_cuts_files_at_exact_counts(tmp_path: Path, kind: str) -> None:
    stream = Stream(10)
    receipt = _deliver(
        SINKS[kind](tmp_path, stream.context, rows_per_file=4, batch_size=3),
        stream.header,
        stream.rows,
    )
    assert receipt is not None
    assert [entry.rows for entry in receipt.files] == [4, 4, 2]
    assert [entry.path.rsplit("-", 1)[1] for entry in receipt.files] == [
        f"0000{index}{SINKS[kind].suffix}" for index in range(3)
    ]


def test_parquet_row_groups_hold_exactly_the_row_group_size(tmp_path: Path) -> None:
    stream = Stream(10)
    receipt = _deliver(
        parquet.ParquetSink(tmp_path, stream.context, row_group_size=4, batch_size=3),
        stream.header,
        stream.rows,
    )
    assert receipt is not None
    metadata = pq.ParquetFile(tmp_path / receipt.files[0].path).metadata
    assert [metadata.row_group(index).num_rows for index in range(metadata.num_row_groups)] == [
        4,
        4,
        2,
    ]
    assert metadata.row_group(0).column(0).compression == "ZSTD"


def test_ipc_record_batches_hold_exactly_the_batch_size(tmp_path: Path) -> None:
    stream = Stream(10)
    receipt = _deliver(
        arrow_ipc.ArrowIpcSink(tmp_path, stream.context, batch_size=4), stream.header, stream.rows
    )
    assert receipt is not None
    with pa.ipc.open_file(str(tmp_path / receipt.files[0].path)) as reader:
        assert [reader.get_batch(index).num_rows for index in range(reader.num_record_batches)] == [
            4,
            4,
            2,
        ]


def test_the_files_say_they_are_synthetic_and_which_stream_they_hold(tmp_path: Path) -> None:
    stream = Stream(3)
    receipt = _deliver(parquet.ParquetSink(tmp_path, stream.context), stream.header, stream.rows)
    assert receipt is not None
    metadata = pq.ParquetFile(tmp_path / receipt.files[0].path).metadata.metadata
    assert metadata[b"flux.synthetic"] == b"true"
    assert metadata[b"flux.runId"] == b"run-1"
    assert metadata[b"flux.outputDigest"] == stream.context.output_digest.value.encode()
    assert json.loads(metadata[b"flux.header"])["exposeId"] == "payment_recovery_moment"


def test_an_empty_stream_writes_one_file_with_the_schema(tmp_path: Path, kind: str) -> None:
    stream = Stream(0)
    receipt = _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    assert receipt is not None and receipt.rows == 0
    manifest = (
        tmp_path / "payment_recovery_moment" / f"_manifest-{stream.context.slice_key}-{kind}.json"
    )
    table = READERS[kind](manifest)
    assert table.num_rows == 0
    assert table.schema.names == [column["name"] for column in SCHEMA]
    assert output_digest_of(table).value == stream.context.output_digest.value


# ---------------------------------------------------------------------------
# Failure leaves nothing behind
# ---------------------------------------------------------------------------


def test_a_stream_that_stops_short_is_discarded(tmp_path: Path, kind: str) -> None:
    stream = Stream(10)
    sink = SINKS[kind](tmp_path, stream.context, rows_per_file=2)
    receipt = _deliver(sink, stream.header, stream.rows[:7])
    assert receipt is None
    assert _every_file(tmp_path) == []


def test_a_failure_mid_stream_leaves_no_part_files(tmp_path: Path, kind: str) -> None:
    stream = Stream(10)
    sink = SINKS[kind](tmp_path, stream.context, rows_per_file=2, batch_size=1)
    sink.open(stream.header)
    with pytest.raises(RuntimeError, match="boom"):
        try:
            for index, row in enumerate(stream.rows):
                if index == 5:
                    raise RuntimeError("boom")
                sink.write(row)
        finally:
            sink.close()
    assert sink.receipt is None
    assert _every_file(tmp_path) == []


def test_a_failed_stream_does_not_touch_an_earlier_publish(tmp_path: Path, kind: str) -> None:
    stream = Stream(10)
    _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    before = {name: _sha256(tmp_path / name) for name in _data_files(tmp_path)}
    _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows[:3])
    assert {name: _sha256(tmp_path / name) for name in _data_files(tmp_path)} == before


def test_verify_refuses_files_that_are_not_the_stream(tmp_path: Path, kind: str) -> None:
    stream, other = Stream(10), Stream(10, offset=500)
    wrong = StreamContext(
        run_id="run-1",
        expose_id=stream.context.expose_id,
        rows=10,
        output_digest=other.context.output_digest,
    )
    sink = SINKS[kind](tmp_path, wrong, verify=True)
    with pytest.raises(FluxError) as caught:
        _deliver(sink, stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3505"
    assert sink.receipt is None
    assert _every_file(tmp_path) == []


def test_verify_passes_the_real_stream(tmp_path: Path, kind: str) -> None:
    stream = Stream(10)
    receipt = _deliver(
        SINKS[kind](tmp_path, stream.context, verify=True), stream.header, stream.rows
    )
    assert receipt is not None


# ---------------------------------------------------------------------------
# Refusals before anything is written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expose_id", ["../escape", "a/b", "..", ".hidden", "trailing."])
def test_an_expose_id_that_is_not_one_path_segment_is_refused(
    tmp_path: Path, kind: str, expose_id: str
) -> None:
    context = StreamContext(
        run_id="r", expose_id=expose_id, rows=0, output_digest=Stream(0).context.output_digest
    )
    with pytest.raises(FluxError) as caught:
        SINKS[kind](tmp_path, context)
    assert caught.value.code.code == "FLX-3504"


def test_a_directory_that_is_a_file_is_refused(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "taken"
    target.write_text("not a directory")
    with pytest.raises(FluxError, match="not a directory"):
        SINKS[kind](target, Stream(1).context)


def test_a_sink_opened_on_another_stream_is_refused(tmp_path: Path, kind: str) -> None:
    stream = Stream(1)
    other = header_of(compiled(SCHEMA, expose_id="another_stream"))
    sink = SINKS[kind](tmp_path, stream.context)
    with pytest.raises(FluxError) as caught:
        sink.open(other)
    assert caught.value.code.code == "FLX-3504"
    assert "another_stream" in str(caught.value)


@pytest.mark.parametrize(
    "options, needle",
    [
        ({"compression": "lzo"}, "compression 'lzo'"),
        ({"batch_size": 0}, "batch-size"),
        ({"rows_per_file": -2}, "rows-per-file"),
        ({"verify": "yes"}, "verify"),
    ],
)
def test_bad_options_name_the_option(
    tmp_path: Path, kind: str, options: Dict[str, Any], needle: str
) -> None:
    with pytest.raises(FluxError) as caught:
        SINKS[kind](tmp_path, Stream(1).context, **options)
    assert caught.value.code.code == "FLX-3504"
    assert needle in str(caught.value)


@pytest.mark.parametrize("options", [{"row_group_size": 0}, {"compression_level": True}])
def test_bad_parquet_options_are_refused(tmp_path: Path, options: Dict[str, Any]) -> None:
    with pytest.raises(FluxError, match="must be a positive integer"):
        parquet.ParquetSink(tmp_path, Stream(1).context, **options)


def test_writing_before_open_or_after_close_is_out_of_order(tmp_path: Path, kind: str) -> None:
    stream = Stream(1)
    sink = SINKS[kind](tmp_path, stream.context)
    with pytest.raises(FluxError, match="before open"):
        sink.write(stream.rows[0])
    sink.open(stream.header)
    with pytest.raises(FluxError, match="already open"):
        sink.open(stream.header)
    sink.write(stream.rows[0])
    sink.close()
    sink.close()  # harmless
    with pytest.raises(FluxError, match="closed"):
        sink.write(stream.rows[0])


@pytest.mark.parametrize(
    "path", ["../outside.parquet", "/etc/passwd", "payment_recovery_moment/../x", "other/x"]
)
def test_a_manifest_cannot_point_outside_its_stream(tmp_path: Path, path: str) -> None:
    stream = Stream(2)
    _deliver(parquet.ParquetSink(tmp_path, stream.context), stream.header, stream.rows)
    manifest = (
        tmp_path / "payment_recovery_moment" / f"_manifest-{stream.context.slice_key}-parquet.json"
    )
    document = json.loads(manifest.read_text())
    document["files"][0]["path"] = path
    manifest.write_text(json.dumps(document))
    with pytest.raises(FluxError) as caught:
        load_manifest(manifest, sink="parquet")
    assert caught.value.code.code == "FLX-3503"


def test_a_manifest_of_another_sink_is_refused(tmp_path: Path) -> None:
    stream = Stream(2)
    _deliver(arrow_ipc.ArrowIpcSink(tmp_path, stream.context), stream.header, stream.rows)
    manifest = (
        tmp_path
        / "payment_recovery_moment"
        / f"_manifest-{stream.context.slice_key}-arrow-ipc.json"
    )
    with pytest.raises(FluxError, match="parquet sink"):
        parquet.read_stream(manifest)


# ---------------------------------------------------------------------------
# The proof on a real run
# ---------------------------------------------------------------------------

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"


@pytest.fixture(scope="module")
def real_run() -> Any:
    from flux_engine.stages import pipeline

    return pipeline.execute(_FIXTURE)


def test_a_real_runs_stream_round_trips_through_every_file_sink(
    tmp_path: Path, kind: str, real_run: Any
) -> None:
    [outcome] = real_run.emits
    assert outcome.result.rows > 0
    context = StreamContext(
        run_id="real",
        expose_id=outcome.resolved.expose_id,
        rows=outcome.result.rows,
        output_digest=outcome.result.output_digest,
    )
    header = EmitHeader(
        expose_id=outcome.expose.expose_id,
        columns=outcome.expose.column_names,
        types=outcome.expose.type_names,
        contract_digest=outcome.resolved.contract_digest,
    )
    # What a delivery hands a sink: the very lines the digest covers, decoded.
    rows = [json.loads(line) for line in outcome.writer.lines()]
    receipt = _deliver(SINKS[kind](tmp_path, context, verify=True), header, rows)
    assert receipt is not None
    manifest = tmp_path / context.expose_id / f"_manifest-{context.slice_key}-{kind}.json"
    assert output_digest_of(READERS[kind](manifest)) == outcome.result.output_digest


# ---------------------------------------------------------------------------
# Without the extra
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["flux_engine.sinks.parquet", "flux_engine.sinks.arrow_ipc"])
def test_without_pyarrow_the_file_sinks_name_the_extra(module: str) -> None:
    probe = (
        "import sys; sys.modules['pyarrow'] = None\n"
        "from flux_engine.core.errors import FluxError\n"
        "try:\n"
        f"    import {module}\n"
        "except FluxError as error:\n"
        "    print(error)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.startswith("FLX-3501: ")
    assert "pip install 'flux-sim-engine[arrow]'" in result.stdout


# ---------------------------------------------------------------------------
# Whole or absent, whatever fails
# ---------------------------------------------------------------------------


def _failing_on(sink: Any, call: int, error: BaseException) -> Any:
    original = sink._write_unit
    calls = {"n": 0}

    def write_unit(writer: Any, table: Any) -> None:
        calls["n"] += 1
        if calls["n"] == call:
            raise error
        original(writer, table)

    sink._write_unit = write_unit
    return sink


@pytest.mark.parametrize("error", [OSError(28, "No space left on device"), ValueError("bad")])
def test_a_failure_on_the_batch_that_completes_the_stream_publishes_nothing(
    tmp_path: Path, kind: str, error: BaseException
) -> None:
    """The failed batch's rows were counted; the count must not pass for a whole stream."""
    stream = Stream(4)
    options: Dict[str, Any] = {"batch_size": 2}
    if kind == "parquet":
        options["row_group_size"] = 2
    sink = _failing_on(SINKS[kind](tmp_path, stream.context, **options), 2, error)
    with pytest.raises(type(error)):
        _deliver(sink, stream.header, stream.rows)
    assert sink.receipt is None
    assert _every_file(tmp_path) == []


def test_a_value_that_does_not_convert_in_the_last_batch_publishes_nothing(
    tmp_path: Path, kind: str
) -> None:
    stream = Stream(4)
    rows = [list(row) for row in stream.rows]
    rows[-1][2] = "not a number"
    sink = SINKS[kind](tmp_path, stream.context, batch_size=2)
    with pytest.raises(FluxError, match="FLX-3502"):
        _deliver(sink, stream.header, rows)
    assert sink.receipt is None
    assert _every_file(tmp_path) == []


def test_an_error_while_closing_leaves_no_part_file(tmp_path: Path, kind: str) -> None:
    stream = Stream(3)
    # The unit written by close(): Parquet holds every row until then (one
    # row group), Arrow IPC writes a record batch per two rows as they come.
    call = 1 if kind == "parquet" else 2
    sink = _failing_on(SINKS[kind](tmp_path, stream.context, batch_size=2), call, ValueError("x"))
    with pytest.raises(ValueError):
        _deliver(sink, stream.header, stream.rows)
    assert _every_file(tmp_path) == []
    assert sink._current is None


def test_a_failure_while_publishing_takes_the_whole_slice_away(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.sinks import _files

    stream = Stream(4)
    real_replace = _files.os.replace
    calls = {"n": 0}

    def replace(source: Any, target: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(5, "Input/output error")
        real_replace(source, target)

    monkeypatch.setattr(_files.os, "replace", replace)
    with pytest.raises(FluxError) as caught:
        _deliver(SINKS[kind](tmp_path, stream.context, rows_per_file=2), stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3505"
    assert _every_file(tmp_path) == []


def test_a_failure_before_the_first_rename_keeps_an_earlier_slice(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.sinks import _files

    stream = Stream(4)
    _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    before = _every_file(tmp_path)

    def replace(source: Any, target: Any) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(_files.os, "replace", replace)
    with pytest.raises(FluxError):
        _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    assert _every_file(tmp_path) == before


def test_a_directory_that_cannot_be_synced_still_publishes(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.sinks import _files

    def refuse(path: Any, flags: int) -> int:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(_files.os, "open", refuse)
    stream = Stream(4)
    assert _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows) is not None


def test_stale_files_with_six_digit_indexes_are_removed_too(tmp_path: Path, kind: str) -> None:
    stream = Stream(2)
    suffix = SINKS[kind].suffix
    stream_dir = tmp_path / "payment_recovery_moment"
    stream_dir.mkdir(parents=True)
    for index in ("00007", "100000", "100001"):
        (stream_dir / f"{stream.context.slice_key}-{index}{suffix}").write_bytes(b"old")
    _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    assert _data_files(tmp_path) == [
        f"payment_recovery_moment/{stream.context.slice_key}-00000{suffix}"
    ]


def test_a_closed_sink_cannot_be_opened(tmp_path: Path, kind: str) -> None:
    stream = Stream(1)
    sink = SINKS[kind](tmp_path, stream.context)
    sink.close()
    with pytest.raises(FluxError, match="sink is closed"):
        sink.open(stream.header)


@pytest.mark.parametrize(
    "compression, level, needle",
    [
        ("gzip", 12, "between 1 and 9, got 12"),
        ("snappy", 3, "takes no compression-level"),
        ("none", 1, "takes no compression-level"),
    ],
)
def test_a_compression_level_the_codec_cannot_use_is_refused_before_the_run(
    tmp_path: Path, compression: str, level: int, needle: str
) -> None:
    with pytest.raises(FluxError) as caught:
        parquet.ParquetSink(
            tmp_path, Stream(1).context, compression=compression, compression_level=level
        )
    assert caught.value.code.code == "FLX-3504"
    assert needle in str(caught.value)


@pytest.mark.parametrize("compression, level", [("zstd", 3), ("gzip", 9), ("snappy", None)])
def test_the_level_a_codec_used_is_the_one_recorded(
    tmp_path: Path, compression: str, level: Optional[int]
) -> None:
    stream = Stream(2)
    receipt = _deliver(
        parquet.ParquetSink(tmp_path, stream.context, compression=compression),
        stream.header,
        stream.rows,
    )
    assert receipt is not None and receipt.writer["compressionLevel"] == level


def test_parquet_and_arrow_ipc_in_one_directory_keep_their_own_manifests(tmp_path: Path) -> None:
    stream = Stream(4)
    for kind in ("parquet", "arrow-ipc"):
        _deliver(SINKS[kind](tmp_path, stream.context), stream.header, stream.rows)
    key = stream.context.slice_key
    assert _every_file(tmp_path) == [
        f"_manifest-{key}-arrow-ipc.json",
        f"_manifest-{key}-parquet.json",
        f"{key}-00000.arrow",
        f"{key}-00000.parquet",
    ]
    for kind in ("parquet", "arrow-ipc"):
        manifest = tmp_path / "payment_recovery_moment" / f"_manifest-{key}-{kind}.json"
        assert output_digest_of(READERS[kind](manifest)) == stream.context.output_digest
