# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Delta sink: a real write into a local directory, and an object target with no network."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks import registry
from flux_engine.stages.deliver import receipt_line

from .conftest import pyarrow_or_skip
from .test_files import Stream, _deliver

pyarrow_or_skip()
deltalake = pytest.importorskip("deltalake")
from flux_engine.sinks import delta  # noqa: E402  (after the importorskip)
from flux_engine.sinks.arrow import output_digest_of  # noqa: E402

pytestmark = pytest.mark.unit


def _deliver_to(target: str, stream: Stream, **options: str) -> Any:
    sink = delta.target(target, options).sink_for(stream.context)
    receipt = _deliver(sink, stream.header, stream.rows)
    assert receipt is not None
    return receipt


def _table(directory: Path, stream: Stream) -> Any:
    return deltalake.DeltaTable(str(directory / stream.context.expose_id))


def test_a_stream_is_written_as_a_table_that_reads_back_as_the_stream(tmp_path: Path) -> None:
    stream = Stream(25)
    receipt = _deliver_to(str(tmp_path), stream)
    read = _table(tmp_path, stream).to_pyarrow_table()
    assert read.num_rows == 25
    assert output_digest_of(read, header=stream.header).value == stream.context.output_digest.value
    assert receipt.rows == 25 and receipt.sink == "delta"
    assert receipt.output_digest == stream.context.output_digest.value


def test_the_receipt_records_the_target_the_table_the_version_and_the_writer(
    tmp_path: Path,
) -> None:
    stream = Stream(4)
    receipt = _deliver_to(str(tmp_path), stream)
    assert receipt.target == str(tmp_path.resolve())
    assert receipt.table == str(tmp_path.resolve() / stream.context.expose_id)
    assert receipt.writer["tableVersion"] == _table(tmp_path, stream).version() == 0
    assert receipt.writer["writer"] == f"deltalake {deltalake.__version__}"
    assert receipt.writer["mode"] == "error"
    document = receipt.as_document()
    assert document["table"] == receipt.table and document["synthetic"] is True
    assert "delivered payment_recovery_moment: 4 rows into" in receipt_line(receipt)


def test_mode_error_refuses_a_table_that_exists_and_leaves_it_alone(tmp_path: Path) -> None:
    stream = Stream(5)
    _deliver_to(str(tmp_path), stream)
    again = delta.target(str(tmp_path), {}).sink_for(stream.context)
    with pytest.raises(FluxError) as caught:
        _deliver(again, stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3505"
    assert again.receipt is None
    assert _table(tmp_path, stream).version() == 0
    assert _table(tmp_path, stream).to_pyarrow_table().num_rows == 5


def test_mode_append_adds_the_rows_as_a_new_version(tmp_path: Path) -> None:
    stream = Stream(5)
    _deliver_to(str(tmp_path), stream)
    receipt = _deliver_to(str(tmp_path), stream, mode="append")
    assert receipt.writer["tableVersion"] == 1 and receipt.writer["mode"] == "append"
    assert _table(tmp_path, stream).to_pyarrow_table().num_rows == 10


def test_mode_append_creates_a_table_that_is_absent(tmp_path: Path) -> None:
    stream = Stream(3)
    receipt = _deliver_to(str(tmp_path / "new"), stream, mode="append")
    assert receipt.writer["tableVersion"] == 0
    assert _table(tmp_path / "new", stream).to_pyarrow_table().num_rows == 3


def test_mode_overwrite_replaces_the_rows(tmp_path: Path) -> None:
    _deliver_to(str(tmp_path), Stream(8))
    smaller = Stream(3, offset=100)
    receipt = _deliver_to(str(tmp_path), smaller, mode="overwrite")
    assert receipt.writer["tableVersion"] == 1
    read = _table(tmp_path, smaller).to_pyarrow_table()
    assert read.num_rows == 3
    assert (
        output_digest_of(read, header=smaller.header).value == smaller.context.output_digest.value
    )


def test_an_empty_stream_still_makes_its_table(tmp_path: Path) -> None:
    stream = Stream(0)
    _deliver_to(str(tmp_path), stream)
    read = _table(tmp_path, stream).to_pyarrow_table()
    assert read.num_rows == 0 and read.schema.names == list(stream.header.columns)


def test_a_stream_that_stopped_short_writes_nothing(tmp_path: Path) -> None:
    stream = Stream(6)
    sink = delta.target(str(tmp_path), {}).sink_for(stream.context)
    sink.open(stream.header)
    for row in stream.rows[:4]:
        sink.write(row)
    sink.close()
    assert sink.receipt is None
    assert list(tmp_path.iterdir()) == []


class Recorder:
    """A stand-in for ``write_deltalake`` and ``DeltaTable`` that records its calls."""

    def __init__(self) -> None:
        self.writes: List[Any] = []

    def write_deltalake(self, *args: Any, **kwargs: Any) -> None:
        self.writes.append((args, kwargs))

    def table(self, *args: Any, **kwargs: Any) -> Any:
        recorder = self

        class Stub:
            def version(self) -> int:
                return len(recorder.writes) - 1

        return Stub()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    stand_in = Recorder()
    monkeypatch.setattr(deltalake, "write_deltalake", stand_in.write_deltalake)
    monkeypatch.setattr(deltalake, "DeltaTable", stand_in.table)
    return stand_in


@pytest.mark.parametrize("scheme", ["s3", "gs", "az", "abfs"])
def test_an_object_target_is_written_with_no_storage_options(
    scheme: str, recorder: Recorder
) -> None:
    stream = Stream(3)
    receipt = _deliver_to(f"{scheme}://bucket/some/prefix/", stream, mode="append")
    location = f"{scheme}://bucket/some/prefix/payment_recovery_moment"
    ((args, kwargs),) = recorder.writes
    assert args[0] == location and args[1].num_rows == 3
    assert kwargs == {"mode": "append"}
    assert receipt.target == f"{scheme}://bucket/some/prefix"
    assert receipt.table == location
    assert receipt.writer["tableVersion"] == 0


@pytest.mark.parametrize(
    "target",
    [
        "s3://b",
        "s3://b/",
        "s3://b/a/../b",
        "s3://b//a",
        "s3://b/a%2Fb",
        "s3://b/a\\b",
        "az://%2e/a",
    ],
)
def test_an_object_prefix_that_could_leave_its_place_is_refused(
    target: str, recorder: Recorder
) -> None:
    with pytest.raises(FluxError) as caught:
        delta.target(target, {})
    assert caught.value.code.code == "FLX-3510"
    assert recorder.writes == []


@pytest.mark.parametrize("target", ["http://host/p", "file:///tmp/x", ""])
def test_a_target_that_is_neither_a_directory_nor_an_object_url_is_refused(target: str) -> None:
    with pytest.raises(FluxError) as caught:
        delta.target(target, {})
    assert caught.value.code.code == "FLX-3504"


@pytest.mark.parametrize(
    "target", ["s3://user:pass@bucket/p", "az://acct/c?sig=abc", "gs://bucket/p#frag"]
)
def test_a_credential_bearing_target_is_refused_before_the_sink_is_built(target: str) -> None:
    with pytest.raises(FluxError) as caught:
        registry.open_target(f"delta:{target}")
    assert caught.value.code.code == "FLX-3509"


def test_a_directory_that_is_a_symbolic_link_is_refused(tmp_path: Path, recorder: Recorder) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(FluxError) as caught:
        delta.target(str(link), {})
    assert caught.value.code.code == "FLX-2118"
    assert recorder.writes == [] and list(real.iterdir()) == []


def test_a_table_directory_that_is_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    stream = Stream(2)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    (base / stream.context.expose_id).symlink_to(elsewhere, target_is_directory=True)
    sink = delta.target(str(base), {}).sink_for(stream.context)
    with pytest.raises(FluxError) as caught:
        _deliver(sink, stream.header, stream.rows)
    assert caught.value.code.code == "FLX-2118"
    assert list(elsewhere.iterdir()) == []


def test_a_directory_that_is_a_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    with pytest.raises(FluxError) as caught:
        delta.target(str(tmp_path / "f"), {})
    assert caught.value.code.code == "FLX-2118"


def test_a_relative_directory_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    stream = Stream(2)
    receipt = _deliver_to("out", stream)
    assert receipt.target == str((tmp_path / "out").resolve())


def test_the_options_stay_closed(tmp_path: Path) -> None:
    assert delta.OPTIONS == ("instance-identity", "mode")
    for key in ("storage-options", "endpoint", "storage_options", "partition-by", "verify"):
        with pytest.raises(FluxError) as caught:
            delta.target(str(tmp_path), {key: "x"})
        assert caught.value.code.code == "FLX-3504"
    with pytest.raises(FluxError) as bad_mode:
        delta.target(str(tmp_path), {"mode": "merge"})
    assert bad_mode.value.code.code == "FLX-3504"


def test_a_failed_write_is_the_engines_error_scrubbed_and_unchained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIsecretvalue")

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "PUT https://user:hunter2@host.example/b/x?X-Amz-Signature=abcd1234 failed "
            "with wJalrXUtnFEMIsecretvalue"
        )

    monkeypatch.setattr(deltalake, "write_deltalake", refuse)
    stream = Stream(3)
    sink = delta.target("s3://b/p", {}).sink_for(stream.context)
    with pytest.raises(FluxError) as caught:
        _deliver(sink, stream.header, stream.rows)
    error = caught.value
    assert error.code.code == "FLX-3505"
    assert error.__cause__ is None and error.__suppress_context__
    text = str(error)
    for leak in ("hunter2", "abcd1234", "wJalrXUtnFEMIsecretvalue"):
        assert leak not in text
    assert "RuntimeError" in text and "s3://b/p" in text
    assert sink.receipt is None


def test_without_deltalake_the_sink_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "deltalake", None)
    monkeypatch.delitem(sys.modules, "flux_engine.sinks.delta")
    with pytest.raises(FluxError) as caught:
        registry.open_target("delta:s3://b/p")
    assert caught.value.code.code == "FLX-3501"
    assert "flux-sim-engine[publish]" in str(caught.value)


def test_delta_is_a_built_in_behind_the_publish_extra() -> None:
    sinks: Dict[str, Any] = {sink.name: sink for sink in registry.available()}
    assert sinks["delta"].extra == "publish" and sinks["delta"].source == "built-in"
    assert sinks["delta"].value == "flux_engine.sinks.delta:target"
