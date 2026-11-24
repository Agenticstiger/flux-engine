# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim run --sink``: the stage, the receipts document, and the command line."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks import registry
from flux_engine.sinks.receipts import SinkReceipt, StreamContext
from flux_engine.stages.deliver import RECEIPTS_VERSION
from flux_engine.stages.run import run

from .conftest import pyarrow_or_skip

pa = pyarrow_or_skip()
from flux_engine.sinks import parquet  # noqa: E402  (after the skip)
from flux_engine.sinks.arrow import output_digest_of  # noqa: E402

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"


def _receipts_document(state: Path, run_id: str) -> Dict[str, Any]:
    path = state / "runs" / run_id / "sink-receipts.json"
    document: Dict[str, Any] = json.loads(path.read_text())
    return document


def test_run_with_a_parquet_sink_writes_the_streams_and_their_receipts(tmp_path: Path) -> None:
    state, out = tmp_path / "state", tmp_path / "parquet"
    result = run(FIXTURE, state_dir=state, sink=f"parquet:{out}", sink_options={"verify": "true"})
    assert result.success, result.stderr
    sinks = result.artifacts["sinks"]
    assert sinks["ok"] is True and sinks["sink"] == "parquet"
    [receipt] = sinks["receipts"]
    applied = result.artifacts["apply"]["receipt"]
    stream = applied["emits"]["payment_recovery_moment"]
    assert receipt["outputDigest"] == stream["outputDigest"]
    assert receipt["rows"] == stream["rows"] == 64
    document = _receipts_document(state, result.artifacts["plan"]["run_id"])
    assert document["version"] == RECEIPTS_VERSION
    assert document["receipts"] == [receipt]
    manifest = (
        out / "payment_recovery_moment" / f"_manifest-{stream['outputDigest'][7:23]}-parquet.json"
    )
    assert output_digest_of(parquet.read_stream(manifest)).value == stream["outputDigest"]
    assert "delivered payment_recovery_moment: 64 rows in 1 parquet file" in result.stdout


def test_receipts_from_two_places_accumulate_and_a_redelivery_replaces_its_own(
    tmp_path: Path,
) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    state = tmp_path / "state"
    database = tmp_path / "sim.sqlite"
    first = run(FIXTURE, state_dir=state, sink=f"parquet:{tmp_path / 'pq'}")
    second = run(FIXTURE, state_dir=state, sink="adbc:sqlite", sink_options={"uri": str(database)})
    third = run(FIXTURE, state_dir=state, sink="adbc:sqlite", sink_options={"uri": str(database)})
    assert first.success and second.success and third.success
    run_id = first.artifacts["plan"]["run_id"]
    document = _receipts_document(state, run_id)
    assert [(entry["sink"], entry.get("table")) for entry in document["receipts"]] == [
        ("adbc", "sim_payment_recovery_moment"),
        ("parquet", None),
    ]


def test_a_misnamed_sink_fails_before_the_simulation_runs(tmp_path: Path) -> None:
    state = tmp_path / "state"
    result = run(FIXTURE, state_dir=state, sink="parquet", sink_options={})
    assert result.exit_code == 2 and result.error_code == "FLX-3504"
    assert not state.exists(), "nothing was planned or applied"
    unknown = run(FIXTURE, state_dir=state, sink="nonesuch:topic")
    assert unknown.exit_code == 2 and unknown.error_code == "FLX-3507"


def test_sink_options_without_a_sink_are_a_usage_error(tmp_path: Path) -> None:
    result = run(FIXTURE, state_dir=tmp_path, sink_options={"uri": "x"})
    assert result.exit_code == 2
    assert "--sink-opt needs --sink" in result.stderr


class _FailingSink:
    def __init__(self, context: StreamContext) -> None:
        self.context, self.receipt, self.closed = context, None, False

    def open(self, header: Any) -> None:
        pass

    def write(self, row: Any) -> None:
        raise OSError("the disk is full")

    def close(self) -> None:
        self.closed = True


class _FailingTarget:
    name = "failing"

    def sink_for(self, context: StreamContext) -> _FailingSink:
        return _FailingSink(context)


class _SilentTarget:
    """A sink that accepts every row and then publishes nothing."""

    name = "silent"

    def sink_for(self, context: StreamContext) -> Any:
        class Silent(_FailingSink):
            def write(self, row: Any) -> None:
                pass

        return Silent(context)


def test_a_sink_that_fails_stops_the_run_before_ship(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(target: str, options: Mapping[str, str]) -> _FailingTarget:
        return _FailingTarget()

    monkeypatch.setattr(registry, "load_factory", lambda name: factory)
    result = run(
        FIXTURE, state_dir=tmp_path / "state", sink="failing:x", ship=True, out_dir=tmp_path / "o"
    )
    assert result.exit_code == 1 and result.error_code == "FLX-3505"
    assert "the failing sink failed on expose 'payment_recovery_moment'" in result.stderr
    assert "OSError: the disk is full" in result.stderr
    assert result.artifacts["ship"] == {"ran": False, "reason": "the failing sink failed"}
    assert not (tmp_path / "o").exists()


def test_a_sink_that_publishes_nothing_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def factory(target: str, options: Mapping[str, str]) -> _SilentTarget:
        return _SilentTarget()

    monkeypatch.setattr(registry, "load_factory", lambda name: factory)
    result = run(
        FIXTURE, state_dir=tmp_path / "state", sink="silent:x", ship=True, out_dir=tmp_path / "o"
    )
    assert result.exit_code == 1 and result.error_code == "FLX-3505"
    assert "did not publish expose 'payment_recovery_moment'" in result.stderr
    assert result.artifacts["sinks"] == {"ran": True, "ok": False, "sink": "silent"}
    assert result.artifacts["ship"]["ran"] is False
    assert not (tmp_path / "o").exists()


def test_a_third_party_sink_receives_the_digests_own_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: List[Any] = []

    class Recording:
        name = "recording"

        def sink_for(self, context: StreamContext) -> Any:
            target = self

            class Sink:
                receipt = None

                def open(self, header: Any) -> None:
                    target.header = header

                def write(self, row: Any) -> None:
                    rows.append(row)

                def close(self) -> None:
                    self.receipt = SinkReceipt(
                        sink="recording",
                        run_id=context.run_id,
                        expose_id=context.expose_id,
                        output_digest=context.output_digest.value,
                        rows=len(rows),
                        target="memory",
                    )

            return Sink()

    monkeypatch.setattr(registry, "load_factory", lambda name: lambda target, options: Recording())
    result = run(FIXTURE, state_dir=tmp_path / "state", sink="recording:")
    assert result.success, result.stderr
    run_dir = Path(result.artifacts["apply"]["run_dir"])
    on_disk = (run_dir / "emits" / "payment_recovery_moment.ndjson").read_text().splitlines()
    assert rows == [json.loads(line) for line in on_disk]


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _fluxsim(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "flux_engine.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_the_cli_writes_a_sink_and_reports_it_in_the_envelope(tmp_path: Path) -> None:
    result = _fluxsim(
        "--json",
        "run",
        str(FIXTURE),
        "--state-dir",
        str(tmp_path / "state"),
        "--sink",
        f"arrow-ipc:{tmp_path / 'ipc'}",
        "--sink-opt",
        "compression=lz4",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    [receipt] = envelope["artifacts"]["sinks"]["receipts"]
    assert receipt["sink"] == "arrow-ipc" and receipt["writer"]["compression"] == "lz4"
    assert (tmp_path / "ipc" / receipt["files"][0]["path"]).is_file()


def test_the_cli_refuses_a_malformed_sink_option(tmp_path: Path) -> None:
    result = _fluxsim(
        "run", str(FIXTURE), "--sink", "parquet:x", "--sink-opt", "verify", cwd=tmp_path
    )
    assert result.returncode == 2
    assert "expected KEY=VALUE" in result.stderr


def test_plugins_lists_the_sinks_and_whether_they_are_installed(tmp_path: Path) -> None:
    result = _fluxsim("--json", "plugins", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    sinks = {entry["name"]: entry for entry in json.loads(result.stdout)["artifacts"]["sinks"]}
    assert set(sinks) >= {"parquet", "arrow-ipc", "adbc"}
    assert sinks["parquet"]["installed"] is True
    assert sinks["adbc"]["extra"] == "flux-sim-engine[adbc]"
    text = _fluxsim("plugins", cwd=tmp_path).stdout
    assert "sinks (fluxsim run --sink NAME:TARGET):" in text
    assert "needs flux-sim-engine[arrow] (installed)" in text


_PROBE = """
import sys
from flux_engine.cli import main
try:
    main({argv!r})
except SystemExit:
    pass
print("LOADED", [m for m in ("pyarrow", "adbc_driver_manager") if m in sys.modules])
"""


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["run", "--help"],
        ["plugins"],
        ["run", str(FIXTURE), "--state-dir", "{state}"],
    ],
)
def test_nothing_without_a_sink_imports_a_sink_library(tmp_path: Path, argv: List[str]) -> None:
    argv = [part.replace("{state}", str(tmp_path / "state")) for part in argv]
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(argv=argv)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert "LOADED []" in result.stdout, result.stdout[-2000:] + result.stderr[-2000:]


def test_a_second_sink_flag_is_a_usage_error(tmp_path: Path) -> None:
    result = _fluxsim(
        "run", str(FIXTURE), "--sink", "parquet:a", "--sink", "parquet:b", cwd=tmp_path
    )
    assert result.returncode == 2
    assert "--sink was given twice; a run writes to one sink" in result.stderr
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_two_tables_in_one_database_keep_two_receipts(tmp_path: Path) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    state, database = tmp_path / "state", str(tmp_path / "sim.sqlite")
    for prefix in ("a_", "b_"):
        result = run(
            FIXTURE,
            state_dir=state,
            sink="adbc:sqlite",
            sink_options={"uri": database, "prefix": prefix},
        )
        assert result.success, result.stderr
    document = _receipts_document(state, result.artifacts["plan"]["run_id"])
    assert sorted(entry["table"] for entry in document["receipts"]) == [
        "a_payment_recovery_moment",
        "b_payment_recovery_moment",
    ]


def test_streams_published_before_a_failure_keep_their_receipts(tmp_path: Path) -> None:
    import types

    from flux_engine.stages import pipeline
    from flux_engine.stages.deliver import deliver, write_receipts
    from flux_engine.stages.layout import RunLayout

    executed = pipeline.execute(FIXTURE)
    [first] = executed.emits
    second = types.SimpleNamespace(
        resolved=types.SimpleNamespace(
            expose_id="second_stream", contract_digest=first.resolved.contract_digest
        ),
        result=first.result,
        expose=types.SimpleNamespace(
            expose_id="second_stream",
            column_names=first.expose.column_names,
            type_names=first.expose.type_names,
            columns=first.expose.columns,
        ),
        writer=first.writer,
    )
    result = types.SimpleNamespace(emits=(first, second))
    target = registry.open_target(f"parquet:{tmp_path / 'out'}")

    class SecondFails:
        name = "parquet"

        def sink_for(self, context: StreamContext) -> Any:
            sink = target.sink_for(context)
            if context.expose_id == "second_stream":

                def refuse(header: Any) -> None:
                    raise FluxError("FLX-3505", "the second stream's destination refused")

                sink.open = refuse
            return sink

    layout = RunLayout.for_state_dir(tmp_path / "state", "0123456789abcdef")
    layout.run_dir.mkdir(parents=True)
    with pytest.raises(FluxError, match="second stream"):
        deliver(
            result,  # type: ignore[arg-type]
            run_id=layout.run_id,
            target=SecondFails(),  # type: ignore[arg-type]
            record=lambda receipt: write_receipts(layout, [receipt]),
        )
    document = json.loads(layout.sink_receipts_path.read_text())
    assert [entry["exposeId"] for entry in document["receipts"]] == ["payment_recovery_moment"]


def test_a_receipts_file_with_no_receipt_list_is_rewritten(tmp_path: Path) -> None:
    from flux_engine.stages.deliver import write_receipts
    from flux_engine.stages.layout import RunLayout

    layout = RunLayout.for_state_dir(tmp_path, "0123456789abcdef")
    layout.run_dir.mkdir(parents=True)
    layout.sink_receipts_path.write_text(
        json.dumps({"version": RECEIPTS_VERSION, "runId": layout.run_id, "receipts": None})
    )
    receipt = SinkReceipt(
        sink="memory",
        run_id=layout.run_id,
        expose_id="e",
        output_digest="sha256:" + "0" * 64,
        rows=0,
        target="memory",
    )
    write_receipts(layout, [receipt])
    assert json.loads(layout.sink_receipts_path.read_text())["receipts"][0]["sink"] == "memory"


@pytest.mark.parametrize("sink", ["parquet", "arrow-ipc"])
@pytest.mark.parametrize("url", ["file:///tmp/x", "memory://x/y", "ftp://host/dir"])
def test_a_url_scheme_that_is_not_an_object_store_is_refused_before_the_run(
    sink: str, url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    state = tmp_path / "state"
    result = run(FIXTURE, state_dir=state, sink=f"{sink}:{url}")
    assert not result.success
    assert "FLX-3504" in result.stderr
    for scheme in ("s3", "gs", "az", "abfs"):
        assert f"{scheme}://" in result.stderr
    assert "plain path" in result.stderr
    assert list(work.iterdir()) == [], "no directory was left in the working directory"
    assert not (state / "runs").exists(), "no run was generated"


def test_a_uri_read_from_the_environment_is_never_printed_or_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    hidden = tmp_path / "hunter2-hidden"
    hidden.mkdir()
    monkeypatch.setenv("FLUX_TEST_SINK_URI", str(hidden / "sim.sqlite"))
    state = tmp_path / "state"
    result = run(
        FIXTURE, state_dir=state, sink="adbc:sqlite", sink_options={"uri-env": "FLUX_TEST_SINK_URI"}
    )
    assert result.success, result.stderr
    run_id = result.artifacts["plan"]["run_id"]
    assert "sqlite:$FLUX_TEST_SINK_URI" in result.stdout
    assert "hunter2-hidden" not in result.stdout + result.stderr
    assert "hunter2-hidden" not in json.dumps(result.artifacts, default=str)
    assert "hunter2-hidden" not in (state / "runs" / run_id / "sink-receipts.json").read_text()

    failing = tmp_path / "missing" / "hunter2-hidden" / "sim.sqlite"
    monkeypatch.setenv("FLUX_TEST_SINK_URI", str(failing))
    failed = run(
        FIXTURE,
        state_dir=tmp_path / "other",
        sink="adbc:sqlite",
        sink_options={"uri-env": "FLUX_TEST_SINK_URI"},
    )
    assert not failed.success
    assert "FLX-3505" in failed.stderr
    assert "hunter2-hidden" not in failed.stdout + failed.stderr


@pytest.mark.parametrize(
    "uri",
    [
        "host=127.0.0.1 dbname=x password=FAKEVAL1",
        "host=127.0.0.1 dbname=x password='FAKEVAL1'",
        "file:x.db?mode=rwc;sig=FAKEVAL1",
        "file:x.db#access_token=FAKEVAL1",
        "//user:FAKEVAL1@host/db",
        "user:FAKEVAL1@account/db",
        "Server=db;Database=x;PWD=FAKEVAL1",
    ],
)
def test_the_cli_refuses_an_inline_secret_in_any_dsn_form_and_prints_none(
    tmp_path: Path, uri: str
) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    state = tmp_path / "state"
    for flags in ((), ("--json",)):
        result = _fluxsim(
            *flags,
            "run",
            str(FIXTURE),
            "--state-dir",
            str(state),
            "--sink",
            "adbc:sqlite",
            "--sink-opt",
            f"uri={uri}",
            cwd=tmp_path,
        )
        assert result.returncode != 0
        assert "FLX-3504" in result.stderr and "uri-env" in result.stderr
        assert "FAKEVAL1" not in result.stdout + result.stderr
    assert not (state / "runs").exists(), "nothing was generated"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"FAKEVAL1" not in path.read_bytes(), path


# ---------------------------------------------------------------------------
# A target is printed and stored only as a label of its non-secret parts
# ---------------------------------------------------------------------------

#: A fake secret with no four-character piece that ordinary output holds.
_SECRET = "Zq7FAKEVAL1xY"


def _leaked(blob: str, secret: str = _SECRET) -> List[str]:
    """Every piece of *secret*, four characters or more, that *blob* holds."""
    return [
        secret[start:stop]
        for start in range(len(secret))
        for stop in range(start + 4, len(secret) + 1)
        if secret[start:stop] in blob
    ]


def _everything_written(root: Path) -> str:
    return "\n".join(
        path.read_text(errors="replace") for path in sorted(root.rglob("*")) if path.is_file()
    )


#: Targets and ``uri`` values that a shape list misses (each carries ``_SECRET``).
_BYPASS_URIS = [
    f"host=h password\n=\n{_SECRET}",
    f"host=h password\x0b=\x0c{_SECRET}",
    f"password=a;b{_SECRET}",
    f"Password={{a}}}}{_SECRET}}}",
    f"postgresql://u:pa#{_SECRET}@h/db",
    f"postgresql://u:pa?{_SECRET}@h/db",
    f"postgresql://h/db?pass%77ord={_SECRET}",
    f"postgresql://h/db?%70assword={_SECRET}",
    f"jdbc:mysql://u:{_SECRET}@h/db",
    f"jdbc:mysql://h/db?user=u&password={_SECRET}",
    f"u:{_SECRET}@h:5432/db",
    f"Server=h;Pwd={_SECRET}",
]


@pytest.mark.parametrize("uri", _BYPASS_URIS)
def test_the_cli_never_prints_or_stores_a_secret_a_shape_list_misses_in_a_uri(
    tmp_path: Path, uri: str
) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    work = tmp_path / "cwd"
    work.mkdir()
    state = tmp_path / "state"
    for flags in ((), ("--json",)):
        result = _fluxsim(
            *flags,
            "run",
            str(FIXTURE),
            "--state-dir",
            str(state),
            "--sink",
            "adbc:sqlite",
            "--sink-opt",
            f"uri={uri}",
            cwd=work,
        )
        assert _leaked(result.stdout + result.stderr) == [], result.stdout + result.stderr
    assert _leaked(_everything_written(state)) == []


@pytest.mark.parametrize(
    "sink",
    [
        f"adbc:u:{_SECRET}@h:5432/db",
        f"parquet:u:{_SECRET}@h:5432/db",
        f"parquet:postgresql://u:pa#{_SECRET}@h/db",
        f"arrow-ipc:u:{_SECRET}@h:5432/db",
        f"delta:u:{_SECRET}@h:5432/db",
        f"parquet:host=h password\n=\n{_SECRET}",
    ],
)
def test_the_cli_never_echoes_a_secret_in_a_sink_target(tmp_path: Path, sink: str) -> None:
    pytest.importorskip("pyarrow")
    state = tmp_path / "state"
    for flags in ((), ("--json",)):
        result = _fluxsim(
            *flags, "run", str(FIXTURE), "--state-dir", str(state), "--sink", sink, cwd=tmp_path
        )
        assert _leaked(result.stdout + result.stderr) == [], result.stdout + result.stderr
    assert _leaked(_everything_written(state)) == []


def test_the_cli_shows_a_harmless_target_by_its_label(tmp_path: Path) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    pytest.importorskip("pyarrow")
    result = _fluxsim(
        "run",
        str(FIXTURE),
        "--state-dir",
        str(tmp_path / "state"),
        "--sink",
        f"parquet:{tmp_path / 'out'}",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert f"under {tmp_path / 'out'}/" in result.stdout


def test_two_deliveries_through_one_variable_name_keep_two_receipts(tmp_path: Path) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    state = tmp_path / "state"
    hidden = [tmp_path / "hunter2-one", tmp_path / "hunter2-two"]
    for place in hidden:
        place.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "flux_engine.cli",
                "--json",
                "run",
                str(FIXTURE),
                "--state-dir",
                str(state),
                "--sink",
                "adbc:sqlite",
                "--sink-opt",
                "uri-env=FLUX_TEST_SINK_URI",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "FLUX_TEST_SINK_URI": (place / "sim.sqlite").as_uri()},
        )
        assert result.returncode == 0, result.stderr
    run_id = json.loads(result.stdout)["artifacts"]["plan"]["run_id"]
    text = (state / "runs" / run_id / "sink-receipts.json").read_text()
    document = json.loads(text)
    assert len(document["receipts"]) == 2
    assert len({entry["targetId"] for entry in document["receipts"]}) == 2
    assert all(entry["target"] == "sqlite:$FLUX_TEST_SINK_URI" for entry in document["receipts"])
    assert "hunter2" not in text
    for place in hidden:
        assert (place / "sim.sqlite").is_file()


def test_a_receipts_file_written_before_the_target_id_still_reads(tmp_path: Path) -> None:
    from flux_engine.stages.deliver import write_receipts
    from flux_engine.stages.layout import RunLayout

    layout = RunLayout.for_state_dir(tmp_path, "0123456789abcdef")
    layout.run_dir.mkdir(parents=True)
    old = {
        "sink": "adbc",
        "runId": layout.run_id,
        "exposeId": "e",
        "outputDigest": "sha256:" + "0" * 64,
        "rows": 1,
        "target": "sqlite:$VAR",
        "table": "e",
        "synthetic": True,
        "writer": {},
    }
    layout.sink_receipts_path.write_text(
        json.dumps({"version": RECEIPTS_VERSION, "runId": layout.run_id, "receipts": [old]})
    )
    receipt = SinkReceipt(
        sink="adbc",
        run_id=layout.run_id,
        expose_id="e",
        output_digest="sha256:" + "0" * 64,
        rows=1,
        target="sqlite:$VAR",
        table="e",
        target_id="1a2b3c4d5e6f",
    )
    write_receipts(layout, [receipt])
    entries = json.loads(layout.sink_receipts_path.read_text())["receipts"]
    assert sorted("targetId" in entry for entry in entries) == [False, True]


def _three_streams(first: Any) -> Any:
    import types

    def stream(expose_id: str) -> Any:
        return types.SimpleNamespace(
            resolved=types.SimpleNamespace(
                expose_id=expose_id, contract_digest=first.resolved.contract_digest
            ),
            result=first.result,
            expose=types.SimpleNamespace(
                expose_id=expose_id,
                column_names=first.expose.column_names,
                type_names=first.expose.type_names,
                columns=first.expose.columns,
            ),
            writer=first.writer,
        )

    return types.SimpleNamespace(emits=(stream("stream_a"), stream("stream_b"), stream("stream_c")))


@pytest.mark.parametrize("raises", [FluxError("FLX-3505", "the destination refused"), OSError("x")])
def test_a_failure_on_the_second_of_three_streams_names_it_and_counts_the_delivered(
    tmp_path: Path, raises: Exception
) -> None:
    from flux_engine.stages import pipeline
    from flux_engine.stages.deliver import deliver, write_receipts
    from flux_engine.stages.layout import RunLayout

    result = _three_streams(pipeline.execute(FIXTURE).emits[0])
    target = registry.open_target(f"parquet:{tmp_path / 'out'}")

    class SecondFails:
        name = "parquet"

        def sink_for(self, context: StreamContext) -> Any:
            sink = target.sink_for(context)
            if context.expose_id == "stream_b":

                def refuse(header: Any) -> None:
                    raise raises

                sink.open = refuse
            return sink

    layout = RunLayout.for_state_dir(tmp_path / "state", "0123456789abcdef")
    layout.run_dir.mkdir(parents=True)
    with pytest.raises(FluxError) as caught:
        deliver(
            result,
            run_id=layout.run_id,
            target=SecondFails(),  # type: ignore[arg-type]
            record=lambda receipt: write_receipts(layout, [receipt]),
        )
    message = str(caught.value)
    assert caught.value.code.code == "FLX-3505"
    assert "'stream_b'" in message
    assert "1 of 3 streams were delivered" in message
    assert "sink-receipts.json" in message
    document = json.loads(layout.sink_receipts_path.read_text())
    assert [entry["exposeId"] for entry in document["receipts"]] == ["stream_a"]


@pytest.mark.parametrize(
    ("uri", "secret"),
    [
        ("host=db.internal user=u password=hunter2 dbname=sim", "hunter2"),
        ("Driver=x;PWD=abc;Server=h", "abc"),
        ("postgresql://u:pa/ss@h/db", "pa/ss"),
    ],
)
def test_a_uri_that_is_not_a_clean_url_gets_no_target_id(uri: str, secret: str) -> None:
    from flux_engine.sinks.adbc import AdbcSink

    sink = object.__new__(AdbcSink)
    sink.uri, sink.uri_env = uri, "SIM_URI"
    assert sink._target_id() is None


def test_a_clean_url_still_gets_a_target_id() -> None:
    from flux_engine.sinks.adbc import AdbcSink

    sink = object.__new__(AdbcSink)
    sink.uri, sink.uri_env = "postgresql://u:s3cret@h/db?sslmode=require", "SIM_URI"
    target_id = sink._target_id()
    assert target_id is not None and "s3cret" not in target_id
