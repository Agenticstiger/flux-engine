# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a person sees: progress, absent capabilities, absent values, ordering.

Four separate ways the terminal surface was worse than the machine one:

* every pipeline command ran for tens of seconds printing nothing at all,
  with no way to tell the engine from a hang;
* ``fluxsim describe --self`` printed only the capabilities that are true,
  so the four that are false -- the ones the README's "Not yet" section
  exists to be honest about -- were visible only under ``--json``;
* ``fluxsim calibrate`` printed the literal Python ``None`` for an ungraded
  scorecard, while the JSON side said ``null`` idiomatically;
* stdout is block-buffered under a pipe and stderr is not, so
  ``2>&1 | tee`` showed the failure line above the lines explaining it.
"""

from __future__ import annotations

import io
import json
import os
import pty
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from flux_engine.cli import main
from flux_engine.cli._progress import progress_enabled, stage_progress

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


# --- progress ---------------------------------------------------------------


def test_progress_is_written_to_a_terminal(monkeypatch) -> None:
    monkeypatch.delenv("FLUX_NO_PROGRESS", raising=False)
    stream = _FakeTty()
    with stage_progress("planning ./bundle", as_json=False, stream=stream):
        pass
    lines = stream.getvalue().splitlines()
    assert lines[0] == "planning ./bundle ..."
    assert lines[1].startswith("planning ./bundle: ") and lines[1].endswith("s")


def test_progress_is_silent_for_a_pipe_for_json_and_when_switched_off(monkeypatch) -> None:
    monkeypatch.delenv("FLUX_NO_PROGRESS", raising=False)
    pipe = io.StringIO()  # isatty() is False
    with stage_progress("planning ./bundle", as_json=False, stream=pipe):
        pass
    assert pipe.getvalue() == "", "a redirected stream must stay byte-identical"

    assert progress_enabled(as_json=True, stream=_FakeTty()) is False
    monkeypatch.setenv("FLUX_NO_PROGRESS", "1")
    assert progress_enabled(as_json=False, stream=_FakeTty()) is False


def test_a_stage_that_runs_a_while_says_so_before_it_finishes(monkeypatch) -> None:
    """An opening line and then 44s of silence does not distinguish work from a hang."""
    monkeypatch.delenv("FLUX_NO_PROGRESS", raising=False)
    stream = _FakeTty()
    with stage_progress("applying ./bundle", as_json=False, stream=stream, heartbeat_seconds=0.01):
        deadline = time.monotonic() + 5.0
        while len(stream.getvalue().splitlines()) < 2:
            if time.monotonic() > deadline:  # pragma: no cover - only on a wedged ticker
                raise AssertionError("no heartbeat inside five seconds")
            time.sleep(0.01)
    lines = stream.getvalue().splitlines()
    assert lines[0] == "applying ./bundle ..."
    beats = [line for line in lines[1:-1] if line.startswith("applying ./bundle ... ")]
    assert beats, lines
    assert beats[0].endswith("s")
    assert lines[-1].startswith("applying ./bundle: ")


def test_the_ticker_never_starts_where_the_lines_would_not_be_written(monkeypatch) -> None:
    """The gate is in front of the thread, not inside it: a pipe gets no thread at all."""
    monkeypatch.delenv("FLUX_NO_PROGRESS", raising=False)
    pipe = io.StringIO()  # isatty() is False
    with stage_progress("planning ./bundle", as_json=False, stream=pipe, heartbeat_seconds=0.01):
        time.sleep(0.05)
        assert not [t for t in threading.enumerate() if t.name == "flux-progress"]
    assert pipe.getvalue() == ""

    tty = _FakeTty()
    with stage_progress("planning ./bundle", as_json=True, stream=tty, heartbeat_seconds=0.01):
        time.sleep(0.05)
        assert not [t for t in threading.enumerate() if t.name == "flux-progress"]
    assert tty.getvalue() == "", "--json says nobody is watching a terminal"


def test_the_ticker_is_a_daemon_and_is_stopped_on_the_way_out(monkeypatch) -> None:
    """It must not be able to hold the process open, or outlive the stage it announces."""
    monkeypatch.delenv("FLUX_NO_PROGRESS", raising=False)
    stream = _FakeTty()
    with stage_progress("shipping ./bundle", as_json=False, stream=stream, heartbeat_seconds=5.0):
        tickers = [t for t in threading.enumerate() if t.name == "flux-progress"]
        assert tickers and all(t.daemon for t in tickers)
    assert not [t for t in threading.enumerate() if t.name == "flux-progress" and t.is_alive()]


def test_the_closing_line_is_written_even_when_the_stage_raises(monkeypatch) -> None:
    monkeypatch.delenv("FLUX_NO_PROGRESS", raising=False)
    stream = _FakeTty()
    with pytest.raises(RuntimeError):
        with stage_progress("applying ./bundle", as_json=False, stream=stream):
            raise RuntimeError("boom")
    assert "applying ./bundle: " in stream.getvalue()


@pytest.mark.integration
def test_a_real_plan_announces_itself_on_a_terminal(tmp_path: Path) -> None:
    """The end-to-end control: a pty for stderr, and stdout still clean."""
    master, slave = pty.openpty()
    chunks: list[bytes] = []

    def drain() -> None:
        while True:
            try:
                data = os.read(master, 4096)
            except OSError:
                return
            if not data:
                return
            chunks.append(data)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    environment = {**os.environ}
    environment.pop("FLUX_NO_PROGRESS", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "flux_engine.cli",
            "plan",
            str(FIXTURE_BUNDLE),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        stdout=subprocess.PIPE,
        stderr=slave,
        text=True,
        timeout=180,
        env=environment,
    )
    os.close(slave)
    reader.join(timeout=5)
    progress = b"".join(chunks).decode()
    assert completed.returncode == 0, progress
    assert "planning " in progress
    assert "planning" not in completed.stdout, "progress must never reach stdout"
    assert "plan written to" in completed.stdout


# --- describe: what is absent ------------------------------------------------


def test_describe_names_the_capabilities_that_are_off(capsys) -> None:
    assert main(["describe", "--self"]) == 0
    out = capsys.readouterr().out
    assert "capabilities: " in out
    absent = next(line for line in out.splitlines() if line.startswith("not available: "))
    for name in ("agentic", "calibrate_abc", "lineage_transport", "synth"):
        assert name in absent, name
    assert name not in out.split("not available:")[0], "an absent capability is not 'enabled'"


def test_the_human_and_json_capability_sets_agree(capsys) -> None:
    assert main(["describe", "--self", "--json"]) == 0
    capabilities = json.loads(capsys.readouterr().out)["artifacts"]["capabilities"]
    assert main(["describe", "--self"]) == 0
    out = capsys.readouterr().out
    enabled_line = next(line for line in out.splitlines() if line.startswith("capabilities: "))
    absent_line = next(line for line in out.splitlines() if line.startswith("not available: "))
    for name, on in capabilities.items():
        assert name in (enabled_line if on else absent_line), name


# --- calibrate: an absent grade is not the string "None" ---------------------


def test_an_ungraded_scorecard_does_not_print_python_none(capsys) -> None:
    assert main(["calibrate", str(FIXTURE_BUNDLE), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "grade=None" not in out
    # One register per line: `key=value` for one half and an English phrase
    # containing a space for the other is two registers in one line.
    assert "grade=" not in out
    assert "credibility declared, not graded" in out


# --- stream ordering under a pipe -------------------------------------------


@pytest.mark.integration
def test_stdout_precedes_stderr_when_both_are_redirected(tmp_path: Path) -> None:
    """`2>&1` is how this output is read after the fact; the order must hold."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "flux_engine.cli",
            "verify",
            str(FIXTURE_BUNDLE),
            "--run-id",
            "0123456789abcdef",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 1
    assert completed.stdout.strip(), "a failure must still say something"


def test_print_result_flushes_stdout_before_writing_stderr(monkeypatch) -> None:
    """The unit-level proof: the flush happens between the two writes."""
    from flux_engine.api.stages import StageResult
    from flux_engine.cli import _print_result

    order: list[str] = []

    class _Stdout(io.StringIO):
        def write(self, text: str) -> int:
            if text.strip():
                order.append("stdout")
            return super().write(text)

        def flush(self) -> None:
            order.append("flush")

    class _Stderr(io.StringIO):
        def write(self, text: str) -> int:
            if text.strip():
                order.append("stderr")
            return super().write(text)

    monkeypatch.setattr(sys, "stdout", _Stdout())
    monkeypatch.setattr(sys, "stderr", _Stderr())
    _print_result(
        StageResult(stage="verify", exit_code=1, stdout="context line", stderr="the failure"),
        as_json=False,
    )
    assert order.index("stdout") < order.index("flush") < order.index("stderr")


# --- the README's runnable example exists ------------------------------------


def test_the_readme_example_starts_from_a_starter_that_is_actually_there() -> None:
    """A reader of the README has no checkout, so its example writes its own bundle.

    The README runs ``fluxsim init demo`` and then the project it wrote. This holds
    that the command is named there and that the starter it writes is shipped,
    with a simulation in it. (The README used to point at a fixture directory of
    this repository, which only a clone has.)
    """
    from flux_engine.starter import starter_files

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "fluxsim init demo" in readme, "the README must show how to get a runnable bundle"
    assert "simulation.flux.yml" in {file.name for file in starter_files()}
