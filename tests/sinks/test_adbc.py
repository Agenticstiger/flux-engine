# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The database sink, against the two ADBC drivers that need no server.

SQLite and DuckDB run in-process, so every behaviour here -- the twin
table, the provenance columns, the replayed slice, the refusals, the
rollback -- is checked on every change with no service to start. The
PostgreSQL test at the end starts a container and skips itself without
Docker or without the PostgreSQL driver.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import pytest

from flux_engine.api.emit import EmitHeader
from flux_engine.core.errors import FluxError
from flux_engine.sinks.receipts import SinkReceipt, StreamContext

from .conftest import compiled, expected_ref, header_of, pyarrow_or_skip, rendered

pyarrow_or_skip()
pytest.importorskip("adbc_driver_manager")
from flux_engine.sinks import adbc  # noqa: E402  (after the importorskip)
from flux_engine.sinks.arrow import output_digest_of  # noqa: E402

pytestmark = pytest.mark.unit

SCHEMA = [
    {"name": "subscriber_id", "type": "STRING", "required": True},
    {"name": "aborted_at", "type": "TIMESTAMP"},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "recovered", "type": "BOOL"},
    {"name": "day", "type": "DATE"},
    {"name": "wide", "type": "DECIMAL(50,10)"},
]
TABLE = "sim_payment_recovery_moment"


def _raw(count: int, offset: int) -> List[Dict[str, Any]]:
    return [
        {
            "subscriber_id": f"s-{offset + index:04d}",
            "aborted_at": datetime(2026, 7, 1, 12, 0, index % 60, 125000, tzinfo=timezone.utc),
            "amount": None if index % 5 == 0 else Decimal(index) / 8,
            "recovered": index % 2 == 0,
            "day": f"2026-07-{1 + index % 28:02d}",
            "wide": Decimal("12345678901234567890123456789012345678.0000000001") * (index % 3),
        }
        for index in range(count)
    ]


class Stream:
    def __init__(
        self, count: int, *, offset: int = 0, run_id: str = "run-1", schema: Any = None
    ) -> None:
        self.expose = compiled(schema or SCHEMA, expose_id="payment_recovery_moment")
        self.header: EmitHeader = header_of(self.expose)
        raw = _raw(count, offset)
        if schema is not None:
            names = {column["name"] for column in schema}
            raw = [{key: value for key, value in row.items() if key in names} for row in raw]
        self.rows = rendered(self.expose, raw)
        self.context = StreamContext(
            run_id=run_id,
            expose_id="payment_recovery_moment",
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


class Database:
    """One test database and a way to query it outside the sink."""

    def __init__(self, driver: str, path: Path) -> None:
        self.driver = driver
        self.uri = str(path)
        module = {"sqlite": "adbc_driver_sqlite.dbapi", "duckdb": "adbc_driver_duckdb.dbapi"}[
            driver
        ]
        self._module = pytest.importorskip(module)

    def sink(self, stream: Stream, **options: Any) -> Any:
        return adbc.AdbcSink(self.driver, stream.context, uri=self.uri, **options)

    def query(self, sql: str) -> Any:
        connection = self._module.connect(self.uri)
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            table = cursor.fetch_arrow_table()
            cursor.close()
            return table
        finally:
            connection.close()

    def execute(self, sql: str) -> None:
        connection = self._module.connect(self.uri)
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            connection.commit()
            cursor.close()
        finally:
            connection.close()

    def has_table(self, table: str) -> bool:
        connection = self._module.connect(self.uri)
        try:
            objects = connection.adbc_get_objects(depth="tables").read_all().to_pylist()
        finally:
            connection.close()
        return any(
            entry["table_name"] == table
            for catalog in objects
            for schema in catalog["catalog_db_schemas"] or []
            for entry in schema["db_schema_tables"] or []
        )

    def count(self, table: str = TABLE) -> int:
        return int(self.query(f'SELECT count(*) AS n FROM "{table}"').column(0)[0].as_py())

    def slice_digest(self, stream: Stream, table: str = TABLE) -> str:
        columns = ", ".join(f'"{name}"' for name in stream.header.columns)
        data = self.query(
            f'SELECT {columns} FROM "{table}" '
            f"WHERE \"_flux_output_digest\" = '{stream.context.output_digest.value}' "
            'ORDER BY "_flux_row"'
        )
        return output_digest_of(data, header=stream.header).value


@pytest.fixture(params=["sqlite", "duckdb"])
def database(request: Any, tmp_path: Path) -> Database:
    suffix = {"sqlite": ".sqlite", "duckdb": ".duckdb"}[request.param]
    return Database(str(request.param), tmp_path / f"sim{suffix}")


# ---------------------------------------------------------------------------
# Where the rows land and what they carry
# ---------------------------------------------------------------------------


def test_a_stream_lands_in_a_twin_table_with_its_provenance(database: Database) -> None:
    stream = Stream(12)
    receipt = _deliver(database.sink(stream), stream.header, stream.rows)
    assert receipt is not None
    assert receipt.table == TABLE and receipt.rows == 12
    assert receipt.target == f"{database.driver}:{database.uri}"
    assert receipt.writer["mode"] == "append" and receipt.writer["driver"] == database.driver
    data = database.query(f'SELECT * FROM "{TABLE}" ORDER BY "_flux_row"')
    assert data.schema.names == [column["name"] for column in SCHEMA] + list(
        adbc.PROVENANCE_COLUMNS
    )
    assert {bool(value) for value in data.column("_flux_synthetic").to_pylist()} == {True}
    assert set(data.column("_flux_run_id").to_pylist()) == {"run-1"}
    assert set(data.column("_flux_output_digest").to_pylist()) == {
        stream.context.output_digest.value
    }
    assert data.column("_flux_row").to_pylist() == list(range(12))


def test_reading_the_slice_back_gives_the_streams_output_digest(database: Database) -> None:
    stream = Stream(12)
    _deliver(database.sink(stream, batch_size=5), stream.header, stream.rows)
    assert database.slice_digest(stream) == stream.context.output_digest.value


def test_a_replay_replaces_its_own_slice_and_never_duplicates(database: Database) -> None:
    stream = Stream(12)
    for _ in range(3):
        _deliver(database.sink(stream), stream.header, stream.rows)
    assert database.count() == 12
    assert database.slice_digest(stream) == stream.context.output_digest.value


def test_another_seed_lands_beside_the_first(database: Database) -> None:
    first, second = Stream(12), Stream(7, offset=300, run_id="run-2")
    _deliver(database.sink(first), first.header, first.rows)
    _deliver(database.sink(second), second.header, second.rows)
    _deliver(database.sink(first), first.header, first.rows)
    assert database.count() == 19
    assert database.slice_digest(first) == first.context.output_digest.value
    assert database.slice_digest(second) == second.context.output_digest.value


def test_an_empty_stream_creates_its_table(database: Database) -> None:
    stream = Stream(0)
    receipt = _deliver(database.sink(stream), stream.header, stream.rows)
    assert receipt is not None and receipt.rows == 0
    assert database.count() == 0


def test_a_prefix_names_the_table(database: Database) -> None:
    stream = Stream(3)
    receipt = _deliver(database.sink(stream, prefix="twin_"), stream.header, stream.rows)
    assert receipt is not None and receipt.table == "twin_payment_recovery_moment"
    assert database.count("twin_payment_recovery_moment") == 3


def test_sqlite_keeps_decimals_and_timestamps_as_their_rendered_text(tmp_path: Path) -> None:
    database = Database("sqlite", tmp_path / "sim.sqlite")
    stream = Stream(3)
    _deliver(database.sink(stream), stream.header, stream.rows)
    data = database.query(
        f'SELECT typeof("amount") AS t, "amount", "aborted_at", typeof("wide") AS w '
        f'FROM "{TABLE}" ORDER BY "_flux_row"'
    )
    assert data.column("t").to_pylist() == ["null", "text", "text"]
    assert data.column("amount").to_pylist() == [None, "0.1250", "0.2500"]
    assert data.column("aborted_at").to_pylist()[0] == "2026-07-01T12:00:00.125Z"
    assert set(data.column("w").to_pylist()) == {"text"}


def test_duckdb_keeps_exact_decimals_and_wide_ones_as_text(tmp_path: Path) -> None:
    database = Database("duckdb", tmp_path / "sim.duckdb")
    stream = Stream(3)
    _deliver(database.sink(stream), stream.header, stream.rows)
    data = database.query(f'SELECT "amount", "wide" FROM "{TABLE}" ORDER BY "_flux_row"')
    assert str(data.schema.field("amount").type) == "decimal128(18, 4)"
    assert data.column("amount").to_pylist()[1] == Decimal("0.1250")
    assert str(data.schema.field("wide").type) == "string"


def test_duckdb_labels_the_table_synthetic(tmp_path: Path) -> None:
    database = Database("duckdb", tmp_path / "sim.duckdb")
    stream = Stream(3)
    _deliver(database.sink(stream), stream.header, stream.rows)
    comment = (
        database.query(f"SELECT comment FROM duckdb_tables() WHERE table_name = '{TABLE}'")
        .column(0)[0]
        .as_py()
    )
    label = json.loads(comment)
    assert label["synthetic"] is True
    assert label["outputDigest"] == stream.context.output_digest.value
    assert label["runId"] == "run-1" and label["exposeId"] == "payment_recovery_moment"
    assert label["generator"].startswith("flux-sim-engine ")


def test_a_schema_is_created_and_used(tmp_path: Path) -> None:
    # The file is named like the schema on purpose: DuckDB names the catalog
    # after the file, and the sink must still say which "sim" it means.
    database = Database("duckdb", tmp_path / "sim.duckdb")
    stream = Stream(3)
    receipt = _deliver(database.sink(stream, schema="sim"), stream.header, stream.rows)
    assert receipt is not None and receipt.table == f"sim.{TABLE}"
    assert database.query(f'SELECT count(*) FROM "sim"."sim"."{TABLE}"').column(0)[0].as_py() == 3
    # A second load finds the table in its schema and replays its slice.
    _deliver(database.sink(stream, schema="sim"), stream.header, stream.rows)
    assert database.query(f'SELECT count(*) FROM "sim"."sim"."{TABLE}"').column(0)[0].as_py() == 3


# ---------------------------------------------------------------------------
# Modes and the tables the sink will not touch
# ---------------------------------------------------------------------------


def test_create_refuses_an_existing_table(database: Database) -> None:
    stream = Stream(4)
    _deliver(database.sink(stream), stream.header, stream.rows)
    with pytest.raises(FluxError) as caught:
        _deliver(database.sink(stream, mode="create"), stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3506"
    assert database.count() == 4


def test_replace_rebuilds_a_managed_table(database: Database) -> None:
    first, second = Stream(12), Stream(5, offset=50, run_id="run-2")
    _deliver(database.sink(first), first.header, first.rows)
    _deliver(database.sink(second, mode="replace"), second.header, second.rows)
    assert database.count() == 5
    assert database.slice_digest(second) == second.context.output_digest.value


def test_replace_may_change_the_layout_append_may_not(database: Database) -> None:
    old = Stream(4, schema=SCHEMA[:3])
    new = Stream(4)
    _deliver(database.sink(old), old.header, old.rows)
    with pytest.raises(FluxError) as caught:
        _deliver(database.sink(new), new.header, new.rows)
    assert caught.value.code.code == "FLX-3506"
    assert "mode=replace" in str(caught.value)
    assert database.count() == 4
    _deliver(database.sink(new, mode="replace"), new.header, new.rows)
    assert database.slice_digest(new) == new.context.output_digest.value


@pytest.mark.parametrize("mode", adbc.MODES)
def test_a_table_the_sink_did_not_create_is_never_touched(database: Database, mode: str) -> None:
    database.execute(f'CREATE TABLE "{TABLE}" ("subscriber_id" TEXT, "balance" INTEGER)')
    database.execute(f"INSERT INTO \"{TABLE}\" VALUES ('customer-1', 100)")
    stream = Stream(4)
    with pytest.raises(FluxError) as caught:
        _deliver(database.sink(stream, mode=mode), stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3506"
    assert database.query(f'SELECT * FROM "{TABLE}"').to_pylist() == [
        {"subscriber_id": "customer-1", "balance": 100}
    ]


# ---------------------------------------------------------------------------
# Rolling back
# ---------------------------------------------------------------------------


def test_a_stream_that_stops_short_leaves_no_table(database: Database) -> None:
    stream = Stream(12)
    receipt = _deliver(database.sink(stream, batch_size=3), stream.header, stream.rows[:7])
    assert receipt is None
    assert not database.has_table(TABLE)


def test_a_failed_replay_keeps_the_earlier_slice(database: Database) -> None:
    stream = Stream(12)
    _deliver(database.sink(stream), stream.header, stream.rows)
    sink = database.sink(stream, batch_size=2)
    sink.open(stream.header)
    with pytest.raises(RuntimeError, match="boom"):
        try:
            for index, row in enumerate(stream.rows):
                if index == 9:
                    raise RuntimeError("boom")
                sink.write(row)
        finally:
            sink.close()
    assert sink.receipt is None
    assert database.count() == 12
    assert database.slice_digest(stream) == stream.context.output_digest.value


def test_verify_commits_the_real_stream_and_rolls_back_a_wrong_one(database: Database) -> None:
    stream, other = Stream(12), Stream(12, offset=900)
    assert _deliver(database.sink(stream, verify=True), stream.header, stream.rows) is not None
    wrong = StreamContext(
        run_id="run-x",
        expose_id="payment_recovery_moment",
        rows=12,
        output_digest=other.context.output_digest,
    )
    sink = adbc.AdbcSink(database.driver, wrong, uri=database.uri, verify=True)
    with pytest.raises(FluxError) as caught:
        _deliver(sink, stream.header, stream.rows)
    assert caught.value.code.code == "FLX-3505"
    assert database.count() == 12


# ---------------------------------------------------------------------------
# Refusals before anything is written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "driver, options, needle",
    [
        ("mysql", {"uri": "x"}, "driver 'mysql' is not one of"),
        ("sqlite", {}, "needs an explicit uri"),
        ("duckdb", {"uri": ""}, "needs an explicit uri"),
        ("sqlite", {"uri": "x", "mode": "upsert"}, "mode 'upsert'"),
        ("sqlite", {"uri": "x", "prefix": "sim-"}, "prefix 'sim-'"),
        ("sqlite", {"uri": "x", "prefix": "p" * 64}, "longer than 63"),
        ("sqlite", {"uri": "x", "schema": "sim"}, "has no schemas"),
        ("duckdb", {"uri": "x", "schema": "a b"}, "schema 'a b'"),
        ("sqlite", {"uri": "x", "db_options": {"k": "v"}}, "takes no db options"),
        ("bigquery", {"uri": "bigquery://p"}, "takes db options"),
        ("sqlite", {"uri": "x", "batch_size": 0}, "batch-size"),
        ("sqlite", {"uri": "x", "verify": 1}, "verify"),
    ],
)
def test_bad_targets_and_options_are_refused(
    driver: str, options: Dict[str, Any], needle: str
) -> None:
    with pytest.raises(FluxError) as caught:
        adbc.AdbcSink(driver, Stream(1).context, **options)
    assert caught.value.code.code == "FLX-3504"
    assert needle in str(caught.value)


def test_a_column_with_the_sinks_own_prefix_is_refused(tmp_path: Path) -> None:
    schema = [{"name": "_flux_row", "type": "INT"}]
    stream = Stream(0, schema=schema)
    sink = adbc.AdbcSink("sqlite", stream.context, uri=str(tmp_path / "x.sqlite"))
    with pytest.raises(FluxError, match="_flux_ prefix"):
        sink.open(stream.header)


@pytest.mark.parametrize(
    "name",
    [
        'sim_t" FROM "sim_t"; COPY (SELECT 1) TO \'{marker}\'; SELECT "sim_t',
        'a", (SELECT sqlite_version()) AS "x',
        "a`b",
        "back\\slash",
        "line\nbreak",
    ],
)
def test_a_column_name_that_could_leave_a_quoted_identifier_never_reaches_sql(
    database: Database, tmp_path: Path, name: str
) -> None:
    """A contract can come from someone else; its column names are not SQL."""
    marker = tmp_path / "written-by-injected-sql"
    schema = [{"name": name.replace("{marker}", str(marker)), "type": "STRING"}]
    stream = Stream(0, schema=schema)
    sink = database.sink(stream, verify=True)
    with pytest.raises(FluxError) as caught:
        sink.open(stream.header)
    assert caught.value.code.code == "FLX-3504"
    assert "rename them in the FLUID contract" in str(caught.value)
    assert not marker.exists()
    assert not database.has_table(TABLE)


def test_unusual_but_harmless_column_names_load_and_verify(database: Database) -> None:
    schema = [
        {"name": "Order ID", "type": "STRING", "required": True},
        {"name": "montant (€)", "type": "DECIMAL(18,4)"},
        {"name": "select", "type": "INT"},
    ]
    expose = compiled(schema, expose_id="payment_recovery_moment")
    rows = rendered(
        expose,
        [{"Order ID": "a", "montant (€)": Decimal("1.5"), "select": 1}, {"Order ID": "b"}],
    )
    context = StreamContext(
        run_id="run-1",
        expose_id="payment_recovery_moment",
        rows=len(rows),
        output_digest=expected_ref(expose, rows),
    )
    sink = adbc.AdbcSink(database.driver, context, uri=database.uri, verify=True)
    assert _deliver(sink, header_of(expose), rows) is not None
    assert database.count() == 2


@pytest.mark.parametrize("name", ['a"b', "a b", "1st", "", "x;y"])
def test_the_dialect_quotes_identifiers_only(name: str) -> None:
    with pytest.raises(FluxError, match="not an identifier the sink will quote"):
        adbc.DIALECTS["duckdb"].quoted(name)


def test_the_table_name_writes_dots_and_hyphens_as_underscores() -> None:
    assert adbc.table_name("sim_", "telco.gold-recovery") == "sim_telco_gold_recovery"


@pytest.mark.parametrize(
    "uri, shown",
    [
        (
            "postgresql://user:s3cret@db.internal:5432/sim?sslmode=require",
            "postgresql://db.internal:5432/sim",
        ),
        ("postgresql://db/sim", "postgresql://db/sim"),
        ("user:s3cret@account/db/schema?warehouse=w&password=s3cret", "account/db/schema"),
        ("sim.duckdb", "sim.duckdb"),
        ("/data/sim.sqlite", "/data/sim.sqlite"),
        ("file:sim.db?mode=rwc", "file:sim.db"),
    ],
)
def test_targets_are_recorded_without_credentials(uri: str, shown: str) -> None:
    assert adbc.redact_uri(uri) == shown
    assert "s3cret" not in adbc.redact_uri(uri)


def test_driver_errors_are_scrubbed_of_the_uri_and_its_secrets() -> None:
    uri = "postgresql://user:hunter2-long@db:5432/sim?sslkey=my-private-key-path"
    sink = adbc.AdbcSink("postgresql", Stream(1).context, uri=uri)
    error = sink._fail(
        RuntimeError(f"connect to {uri} failed: password hunter2-long rejected\nmore detail"),
        "could not connect",
    )
    message = str(error)
    assert error.code.code == "FLX-3505"
    for secret in ("hunter2-long", "my-private-key-path", "user:"):
        assert secret not in message
    assert "postgresql://db:5432/sim" in message
    assert "more detail" not in message


def test_a_driver_that_is_not_installed_names_both_packages(tmp_path: Path) -> None:
    if importlib.util.find_spec("adbc_driver_postgresql") is not None:
        pytest.skip("the PostgreSQL driver is installed here")
    stream = Stream(1)
    sink = adbc.AdbcSink("postgresql", stream.context, uri="postgresql://localhost/sim")
    with pytest.raises(FluxError) as caught:
        sink.open(stream.header)
    assert caught.value.code.code == "FLX-3501"
    assert "pip install 'flux-sim-engine[adbc]' adbc-driver-postgresql" in str(caught.value)


def test_a_duckdb_too_old_for_the_sink_is_refused_with_the_upgrade_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("adbc_driver_duckdb")
    monkeypatch.setattr(adbc, "_distribution_version", lambda name: "1.4.3")
    stream = Stream(1)
    sink = adbc.AdbcSink("duckdb", stream.context, uri=str(tmp_path / "x.duckdb"))
    with pytest.raises(FluxError) as caught:
        sink.open(stream.header)
    assert caught.value.code.code == "FLX-3501"
    assert "needs duckdb 1.5 or later, and 1.4.3 is installed" in str(caught.value)
    assert "pip install 'duckdb>=1.5'" in str(caught.value)


def test_without_the_extra_the_import_names_it() -> None:
    probe = (
        "import sys; sys.modules['adbc_driver_manager'] = None\n"
        "from flux_engine.core.errors import FluxError\n"
        "try:\n"
        "    import flux_engine.sinks.adbc\n"
        "except FluxError as error:\n"
        "    print(error)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.startswith("FLX-3501: ")
    assert "pip install 'flux-sim-engine[adbc]'" in result.stdout


# ---------------------------------------------------------------------------
# A real run, end to end
# ---------------------------------------------------------------------------

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"


@pytest.fixture(scope="module")
def real_run() -> Any:
    from flux_engine.stages import pipeline

    return pipeline.execute(_FIXTURE)


def _real_stream(real_run: Any) -> Any:
    [outcome] = real_run.emits
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
    rows = [json.loads(line) for line in outcome.writer.lines()]
    return context, header, rows


def test_a_real_runs_stream_round_trips_through_the_database(
    database: Database, real_run: Any
) -> None:
    context, header, rows = _real_stream(real_run)
    sink = adbc.AdbcSink(database.driver, context, uri=database.uri, verify=True)
    assert _deliver(sink, header, rows) is not None
    columns = ", ".join(f'"{name}"' for name in header.columns)
    data = database.query(f'SELECT {columns} FROM "sim_{context.expose_id}" ORDER BY "_flux_row"')
    assert output_digest_of(data, header=header) == context.output_digest


# ---------------------------------------------------------------------------
# PostgreSQL, in a container
# ---------------------------------------------------------------------------


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=30)
    return probe.returncode == 0


def _free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


@pytest.fixture(scope="module")
def postgres_uri() -> Iterator[str]:
    pytest.importorskip("adbc_driver_postgresql", reason="needs adbc-driver-postgresql")
    if not _docker_works():
        pytest.skip("needs Docker to start a PostgreSQL container")
    port = _free_port()
    name = f"flux-sink-test-{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", f"POSTGRES_PASSWORD={password}", "-e", "POSTGRES_DB=sim",
            "-p", f"127.0.0.1:{port}:5432", "postgres:17-alpine",
        ],
        check=True, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "sim"],
                capture_output=True,
                text=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("the PostgreSQL container did not become ready")
        time.sleep(1)
        yield f"postgresql://postgres:{password}@127.0.0.1:{port}/sim"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


@pytest.mark.integration
@pytest.mark.slow
def test_postgresql_loads_replays_and_round_trips(postgres_uri: str, real_run: Any) -> None:
    import adbc_driver_postgresql.dbapi as postgresql

    context, header, rows = _real_stream(real_run)
    for _ in range(2):
        sink = adbc.AdbcSink(
            "postgresql", context, uri=postgres_uri, schema="sim", verify=True, batch_size=1000
        )
        receipt = _deliver(sink, header, rows)
        assert receipt is not None
        assert receipt.target.startswith("postgresql://127.0.0.1:")
        assert "postgres:" not in receipt.target
    connection = postgresql.connect(postgres_uri)
    try:
        cursor = connection.cursor()
        columns = ", ".join(f'"{name}"' for name in header.columns)
        cursor.execute(
            f'SELECT {columns} FROM "sim"."sim_{context.expose_id}" ORDER BY "_flux_row"'
        )
        data = cursor.fetch_arrow_table()
        assert data.num_rows == len(rows)
        assert output_digest_of(data, header=header) == context.output_digest
        cursor.execute(
            f"SELECT obj_description('sim.sim_{context.expose_id}'::regclass, 'pg_class')"
        )
        assert json.loads(cursor.fetchone()[0])["synthetic"] is True
        cursor.close()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Commit only a whole stream
# ---------------------------------------------------------------------------


def test_a_failed_ingest_of_the_last_batch_rolls_the_replay_back(database: Database) -> None:
    """Its rows were counted; the count must not pass for a whole stream."""
    stream = Stream(4)
    _deliver(database.sink(stream), stream.header, stream.rows)
    sink = database.sink(stream, batch_size=2)
    sink.open(stream.header)
    real_ingest = sink._cursor.adbc_ingest
    calls = {"n": 0}

    def ingest(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the connection dropped")
        return real_ingest(*args, **kwargs)

    sink._cursor.adbc_ingest = ingest
    with pytest.raises(RuntimeError, match="connection dropped"):
        try:
            for row in stream.rows:
                sink.write(row)
        finally:
            sink.close()
    assert sink.receipt is None
    assert database.count() == 4
    assert database.slice_digest(stream) == stream.context.output_digest.value


def test_a_value_that_does_not_convert_in_the_last_batch_rolls_back(database: Database) -> None:
    stream = Stream(4)
    _deliver(database.sink(stream), stream.header, stream.rows)
    rows = [list(row) for row in stream.rows]
    # BOOL converts on every database; SQLite keeps a DECIMAL as its text.
    rows[-1][3] = "not a bool"
    sink = database.sink(stream, batch_size=2)
    with pytest.raises(FluxError, match="FLX-3502"):
        _deliver(sink, stream.header, rows)
    assert sink.receipt is None
    assert database.slice_digest(stream) == stream.context.output_digest.value


def test_a_closed_database_sink_cannot_be_opened(database: Database) -> None:
    stream = Stream(1)
    sink = database.sink(stream)
    sink.close()
    with pytest.raises(FluxError, match="sink is closed"):
        sink.open(stream.header)
