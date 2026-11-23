# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Iceberg sink: a stub catalogue that records its calls, and a real one where it can run."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks import registry
from flux_engine.stages.deliver import receipt_line

from .conftest import compiled, expected_ref, header_of, pyarrow_or_skip, rendered
from .test_files import SCHEMA, Stream, _deliver, _rows

pa = pyarrow_or_skip()
pyiceberg = pytest.importorskip("pyiceberg")
catalog_module = pytest.importorskip("pyiceberg.catalog")
from flux_engine.sinks import iceberg  # noqa: E402  (after the importorskip)
from flux_engine.sinks.arrow import output_digest_of  # noqa: E402
from flux_engine.sinks.receipts import StreamContext  # noqa: E402

pytestmark = pytest.mark.unit

Identifier = Tuple[str, ...]


class StubTable:
    """What ``load_table`` returns: it records what is written and makes a snapshot of it."""

    def __init__(self, catalog: "StubCatalog", identifier: Identifier) -> None:
        self.catalog = catalog
        self.identifier = identifier

    def current_snapshot(self) -> Any:
        return SimpleNamespace(snapshot_id=self.catalog.snapshot_ids[self.identifier])

    def append(self, table: Any) -> None:
        self.catalog.write("append", self.identifier, table)

    def overwrite(self, table: Any) -> None:
        self.catalog.write("overwrite", self.identifier, table)


class StubTransaction:
    def __init__(self, catalog: "StubCatalog", identifier: Identifier, schema: Any) -> None:
        self.catalog, self.identifier, self.schema = catalog, identifier, schema
        self.appended: Any = None

    def append(self, table: Any) -> None:
        self.appended = table

    def commit_transaction(self) -> None:
        self.catalog.write("create", self.identifier, self.appended)


class StubCatalog:
    """A catalogue that keeps each table's rows in memory and records every call."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Identifier]] = []
        self.tables: Dict[Identifier, Any] = {}
        self.snapshot_ids: Dict[Identifier, int] = {}
        self.fail_with: Any = None
        self._next = 9_007_199_254_740_993  # beyond what a double keeps

    def write(self, how: str, identifier: Identifier, table: Any) -> None:
        self.calls.append((how, identifier))
        if self.fail_with is not None:
            raise self.fail_with
        if how == "append":
            table = pa.concat_tables([self.tables[identifier], table])
        self.tables[identifier] = table
        self.snapshot_ids[identifier] = self._next
        self._next += 1

    def table_exists(self, identifier: Identifier) -> bool:
        assert isinstance(identifier, tuple)
        return identifier in self.tables

    def create_table_transaction(self, identifier: Identifier, schema: Any) -> StubTransaction:
        assert isinstance(identifier, tuple)
        return StubTransaction(self, identifier, schema)

    def load_table(self, identifier: Identifier) -> StubTable:
        assert isinstance(identifier, tuple)
        return StubTable(self, identifier)


class Loader:
    """A stand-in for ``load_catalog`` that records what it was asked for."""

    def __init__(self, catalog: Any) -> None:
        self.catalog = catalog
        self.calls: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.catalog


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with no catalogue file, and a home and PYICEBERG_HOME elsewhere."""
    work, elsewhere = tmp_path / "work", tmp_path / "home"
    work.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(elsewhere))
    monkeypatch.delenv("PYICEBERG_HOME", raising=False)
    return elsewhere


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch, home: Path) -> Loader:
    loader = Loader(StubCatalog())
    monkeypatch.setattr(catalog_module, "load_catalog", loader)
    return loader


def _stream(count: int, *, expose_id: str = "payment_recovery_moment") -> Stream:
    stream = Stream(0)
    stream.expose = compiled(SCHEMA, expose_id=expose_id)
    stream.header = header_of(stream.expose)
    stream.rows = rendered(stream.expose, _rows(count))
    stream.context = StreamContext(
        run_id="run-1",
        expose_id=expose_id,
        rows=len(stream.rows),
        output_digest=expected_ref(stream.expose, stream.rows),
    )
    return stream


def _deliver_to(target: str, stream: Stream, **options: str) -> Any:
    sink = iceberg.target(target, options).sink_for(stream.context)
    receipt = _deliver(sink, stream.header, stream.rows)
    assert receipt is not None
    return receipt


def _stub(loader: Loader) -> StubCatalog:
    assert isinstance(loader.catalog, StubCatalog)
    return loader.catalog


def test_the_catalogue_is_loaded_by_name_alone(catalog: Loader) -> None:
    _deliver_to("prod/sim", _stream(3))
    assert catalog.calls == [(("prod",), {})]


def test_a_table_is_named_by_a_tuple_so_a_dotted_expose_id_stays_one_name(
    catalog: Loader,
) -> None:
    receipt = _deliver_to("prod/sim/events", _stream(3, expose_id="events.v2"))
    assert _stub(catalog).calls == [("create", ("sim", "events", "events.v2"))]
    assert receipt.table == ("sim", "events", "events.v2")


def test_create_makes_the_table_from_the_streams_arrow_schema(catalog: Loader) -> None:
    stream = _stream(6)
    receipt = _deliver_to("prod/sim", stream)
    written = _stub(catalog).tables[("sim", "payment_recovery_moment")]
    assert written.num_rows == 6
    assert (
        output_digest_of(written, header=stream.header).value == stream.context.output_digest.value
    )
    assert receipt.writer["mode"] == "create"


def test_the_receipt_records_the_target_the_identifier_the_snapshot_and_the_writer(
    catalog: Loader,
) -> None:
    receipt = _deliver_to("prod/sim/events", _stream(2))
    assert receipt.sink == "iceberg" and receipt.rows == 2
    assert receipt.target == "iceberg:prod/sim/events"
    assert receipt.table == ("sim", "events", "payment_recovery_moment")
    assert receipt.writer["snapshotId"] == "9007199254740993"
    assert receipt.writer["writer"] == f"pyiceberg {pyiceberg.__version__}"
    document = receipt.as_document()
    assert document["table"] == ["sim", "events", "payment_recovery_moment"]
    assert receipt.files == () and "uri" not in str(document).lower()
    assert "into sim/events/payment_recovery_moment at iceberg:prod/sim/events" in receipt_line(
        receipt
    )


def test_create_refuses_a_table_that_exists_and_writes_nothing(catalog: Loader) -> None:
    stream = _stream(3)
    _deliver_to("prod/sim", stream)
    again = iceberg.target("prod/sim", {}).sink_for(stream.context)
    with pytest.raises(FluxError) as caught:
        _deliver(again, stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3505"
    assert "already exists" in str(caught.value) and again.receipt is None
    assert len(_stub(catalog).calls) == 1


def test_append_adds_to_a_table_that_exists(catalog: Loader) -> None:
    stream = _stream(3)
    _deliver_to("prod/sim", stream)
    receipt = _deliver_to("prod/sim", stream, mode="append")
    assert _stub(catalog).calls[-1] == ("append", ("sim", "payment_recovery_moment"))
    assert _stub(catalog).tables[("sim", "payment_recovery_moment")].num_rows == 6
    assert receipt.writer["snapshotId"] == "9007199254740994"


def test_overwrite_replaces_a_table_that_exists(catalog: Loader) -> None:
    _deliver_to("prod/sim", _stream(5))
    receipt = _deliver_to("prod/sim", _stream(2), mode="overwrite")
    assert _stub(catalog).calls[-1] == ("overwrite", ("sim", "payment_recovery_moment"))
    assert _stub(catalog).tables[("sim", "payment_recovery_moment")].num_rows == 2
    assert receipt.writer["mode"] == "overwrite"


@pytest.mark.parametrize("mode", ["append", "overwrite"])
def test_a_mode_that_writes_into_a_table_refuses_one_that_is_absent(
    mode: str, catalog: Loader
) -> None:
    stream = _stream(3)
    sink = iceberg.target("prod/sim", {"mode": mode}).sink_for(stream.context)
    with pytest.raises(FluxError) as caught:
        _deliver(sink, stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3505"
    assert "does not exist" in str(caught.value) and "mode=create" in str(caught.value)
    assert _stub(catalog).calls == [] and sink.receipt is None


def test_a_stream_that_stopped_short_loads_no_catalogue(catalog: Loader) -> None:
    stream = _stream(6)
    sink = iceberg.target("prod/sim", {}).sink_for(stream.context)
    sink.open(stream.header)
    for row in stream.rows[:2]:
        sink.write(row)
    sink.close()
    assert sink.receipt is None and catalog.calls == []


@pytest.mark.parametrize("target", ["", "prod", "prod/", "/"])
def test_a_namespace_is_required(target: str, catalog: Loader) -> None:
    with pytest.raises(FluxError) as caught:
        iceberg.target(target, {})
    assert caught.value.code.code in {"FLX-3504", "FLX-3510"}
    assert catalog.calls == []


@pytest.mark.parametrize(
    "target",
    [
        "prod//sim",
        "/sim",
        "prod/./sim",
        "prod/sim/..",
        "../sim",
        "prod/sim//x",
        "prod/a\\b",
        "prod/a%2Fb",
        "prod/a%2fb",
        "prod/a%5Cb",
        "prod/%2e%2e",
        "pro%2Fd/sim",
        "prod\\x/sim",
    ],
)
def test_a_name_or_level_that_could_leave_its_place_is_refused(
    target: str, catalog: Loader
) -> None:
    with pytest.raises(FluxError) as caught:
        iceberg.target(target, {})
    assert caught.value.code.code == "FLX-3510"
    assert catalog.calls == []


def test_a_trailing_slash_is_dropped_and_dotted_names_are_one_level() -> None:
    assert iceberg.parse_target("prod/sim/") == ("prod", ("sim",))
    assert iceberg.parse_target("my.cat/a.b/c") == ("my.cat", ("a.b", "c"))


def _put_catalogue_file(directory: Path) -> None:
    (directory / ".pyiceberg.yaml").write_text("catalog: {}\n", encoding="utf-8")


def test_a_catalogue_file_in_the_current_directory_is_refused_before_loading(
    catalog: Loader,
) -> None:
    _put_catalogue_file(Path.cwd())
    with pytest.raises(FluxError) as caught:
        iceberg.target("prod/sim", {})
    assert caught.value.code.code == "FLX-3511"
    assert ".pyiceberg.yaml" in str(caught.value) and catalog.calls == []


def test_the_catalogue_file_is_checked_again_just_before_the_catalogue_is_loaded(
    catalog: Loader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _stream(2)
    sink = iceberg.target("prod/sim", {}).sink_for(stream.context)
    carrying = tmp_path / "bundle"
    carrying.mkdir()
    _put_catalogue_file(carrying)
    monkeypatch.chdir(carrying)
    with pytest.raises(FluxError) as caught:
        _deliver(sink, stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3511"
    assert catalog.calls == [] and sink.receipt is None


def test_a_catalogue_file_in_the_home_directory_is_the_persons_own(
    catalog: Loader, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_catalogue_file(home)
    monkeypatch.chdir(home)
    _deliver_to("prod/sim", _stream(2))
    assert catalog.calls == [(("prod",), {})]


def test_a_catalogue_file_in_pyiceberg_home_is_the_persons_own(
    catalog: Loader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    _put_catalogue_file(configured)
    monkeypatch.setenv("PYICEBERG_HOME", str(configured))
    monkeypatch.chdir(configured)
    _deliver_to("prod/sim", _stream(2))
    assert catalog.calls == [(("prod",), {})]


def test_a_link_to_the_home_directory_is_the_home_directory(
    catalog: Loader, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_catalogue_file(home)
    link = tmp_path / "link"
    link.symlink_to(home, target_is_directory=True)
    monkeypatch.chdir(link)
    iceberg.target("prod/sim", {})


def test_a_working_directory_with_no_catalogue_file_loads_the_catalogue(catalog: Loader) -> None:
    _deliver_to("prod/sim", _stream(2))
    assert len(catalog.calls) == 1


def test_the_options_stay_closed(catalog: Loader) -> None:
    assert iceberg.OPTIONS == ("mode",)
    for key in ("uri", "properties", "warehouse", "catalog-type", "endpoint", "verify"):
        with pytest.raises(FluxError) as caught:
            iceberg.target("prod/sim", {key: "x"})
        assert caught.value.code.code == "FLX-3504"
    with pytest.raises(FluxError) as bad_mode:
        iceberg.target("prod/sim", {"mode": "replace"})
    assert bad_mode.value.code.code == "FLX-3504"
    assert catalog.calls == []


def test_a_failed_write_is_the_engines_error_scrubbed_and_unchained(
    catalog: Loader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIsecretvalue")
    _stub(catalog).fail_with = RuntimeError(
        "PUT https://user:hunter2@host.example/b/x?X-Amz-Signature=abcd1234 failed "
        "with wJalrXUtnFEMIsecretvalue"
    )
    stream = _stream(3)
    sink = iceberg.target("prod/sim", {}).sink_for(stream.context)
    with pytest.raises(FluxError) as caught:
        _deliver(sink, stream.header, stream.rows)
    error = caught.value
    assert error.code.code == "FLX-3505"
    assert error.__cause__ is None and error.__suppress_context__
    text = str(error)
    for leak in ("hunter2", "abcd1234", "wJalrXUtnFEMIsecretvalue"):
        assert leak not in text
    assert "RuntimeError" in text and "iceberg:prod/sim" in text
    assert sink.receipt is None


def test_a_failure_loading_the_catalogue_is_scrubbed_and_unchained(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise ValueError("cannot reach https://user:hunter2@catalog.example/v1?token=abcd1234")

    monkeypatch.setattr(catalog_module, "load_catalog", refuse)
    stream = _stream(2)
    sink = iceberg.target("prod/sim", {}).sink_for(stream.context)
    with pytest.raises(FluxError) as caught:
        _deliver(sink, stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3505"
    assert caught.value.__cause__ is None
    assert "hunter2" not in str(caught.value) and "abcd1234" not in str(caught.value)
    assert "catalog.example" in str(caught.value)


def test_without_pyiceberg_the_sink_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pyiceberg", None)
    monkeypatch.delitem(sys.modules, "flux_engine.sinks.iceberg")
    with pytest.raises(FluxError) as caught:
        registry.open_target("iceberg:prod/sim")
    assert caught.value.code.code == "FLX-3501"
    assert "flux-sim-engine[publish]" in str(caught.value)


def test_iceberg_is_a_built_in_behind_the_publish_extra() -> None:
    sinks = {sink.name: sink for sink in registry.available()}
    assert sinks["iceberg"].extra == "publish" and sinks["iceberg"].source == "built-in"
    assert sinks["iceberg"].value == "flux_engine.sinks.iceberg:target"


def test_a_real_catalogue_holds_the_stream_through_create_append_and_overwrite(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("sqlalchemy")
    memory = pytest.importorskip("pyiceberg.catalog.memory")
    real = memory.InMemoryCatalog("local", warehouse=f"file://{tmp_path / 'warehouse'}")
    real.create_namespace("sim")
    monkeypatch.setattr(catalog_module, "load_catalog", Loader(real))
    stream = _stream(12, expose_id="events.v2")
    identifier = ("sim", "events.v2")

    created = _deliver_to("local/sim", stream)
    assert created.table == identifier
    table = real.load_table(identifier)
    assert created.writer["snapshotId"] == str(table.current_snapshot().snapshot_id)
    read = table.scan().to_arrow()
    assert output_digest_of(read, header=stream.header).value == stream.context.output_digest.value

    appended = _deliver_to("local/sim", stream, mode="append")
    assert real.load_table(identifier).scan().to_arrow().num_rows == 24
    assert appended.writer["snapshotId"] != created.writer["snapshotId"]

    smaller = _stream(3, expose_id="events.v2")
    overwritten = _deliver_to("local/sim", smaller, mode="overwrite")
    assert real.load_table(identifier).scan().to_arrow().num_rows == 3
    assert overwritten.writer["snapshotId"] == str(
        real.load_table(identifier).current_snapshot().snapshot_id
    )

    with pytest.raises(FluxError) as caught:
        _deliver(
            iceberg.target("local/sim", {}).sink_for(stream.context), stream.header, stream.rows
        )
    assert caught.value.code.code == "FLX-3505"


_HOSTILE = "x\x1b[2J\nFLX-0000: forged‮"


def _inert(text: str) -> None:
    assert "\x1b" not in text and "‮" not in text
    assert len(text.strip().splitlines()) == 1


def test_a_refusal_never_prints_the_directory_or_the_names_it_was_given_raw(
    catalog: Loader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _stream(2)
    sink = iceberg.target(f"{_HOSTILE}/sim", {"mode": "append"}).sink_for(stream.context)
    with pytest.raises(FluxError) as missing:
        _deliver(sink, stream.header, stream.rows)
    assert missing.value.code.code == "FLX-3505"
    _inert(str(missing.value))
    _deliver_to("prod/sim", stream)
    again = iceberg.target("prod/sim", {}).sink_for(stream.context)
    carrying = tmp_path / "e\x1b[2Jlsewhere‮"
    carrying.mkdir()
    _put_catalogue_file(carrying)
    monkeypatch.chdir(carrying)
    with pytest.raises(FluxError) as filed:
        _deliver(again, stream.header, stream.rows)
    assert filed.value.code.code == "FLX-3511"
    _inert(str(filed.value))
