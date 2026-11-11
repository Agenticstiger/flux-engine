# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim run --sink``: the stage, the receipts document, and the command line."""

from __future__ import annotations

import json
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

pa = pytest.importorskip("pyarrow")
from flux_engine.sinks import parquet  # noqa: E402  (after the importorskip)
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
    unknown = run(FIXTURE, state_dir=state, sink="kafka:topic")
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
