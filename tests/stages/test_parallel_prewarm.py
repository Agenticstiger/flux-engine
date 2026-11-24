# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The forkserver starts before the run's own work, and imports only what a worker needs.

Proved by what is done and in which order (modules imported, the order of events, a
counter), never by a time: a clock on a shared machine proves nothing about either.
"""

from __future__ import annotations

import json
import multiprocessing.forkserver as forkserver_module
import subprocess
import sys
from pathlib import Path
from typing import Any, List

import pytest

from flux_engine.stages import _parallel as parallel
from flux_engine.stages import _prewarm, pipeline
from tests.bundles import bundle_with_rows

from .conftest import FIXTURE_BUNDLE, REPO_ROOT


def _imported_by(statement: str) -> List[str]:
    """The modules a fresh interpreter holds after running *statement*."""
    code = f"import sys\n{statement}\nprint(__import__('json').dumps(sorted(sys.modules)))"
    done = subprocess.run(
        [sys.executable, "-P", "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**_clean_environment(), "PYTHONPATH": str(REPO_ROOT)},
    )
    assert done.returncode == 0, done.stderr
    return list(json.loads(done.stdout.splitlines()[-1]))


def _clean_environment() -> dict[str, str]:
    import os

    return {key: value for key, value in os.environ.items() if key != "PYTHONSAFEPATH"}


_NOT_A_WORKERS_BUSINESS = (
    "flux_engine.stages.apply",
    "flux_engine.stages.verify",
    "flux_engine.stages.ship",
    "flux_engine.stages.replay",
    "flux_engine.stages.plan",
    "flux_engine.stages.run",
    "flux_engine.stages.validate",
    "flux_engine.stages.pipeline",
    "flux_engine.concordance.compiler",
    "flux_engine.concordance.verifier",
    "flux_engine.concordance.gate",
    "flux_engine.concordance.record",
    "flux_engine.concordance.boundary",
)


@pytest.mark.unit
def test_the_preload_is_the_worker_module_and_imports_no_stage() -> None:
    assert _prewarm.PRELOAD_MODULE == parallel.__name__
    modules = _imported_by(f"import {_prewarm.PRELOAD_MODULE}")
    assert "flux_engine.stages._parallel" in modules
    assert [name for name in _NOT_A_WORKERS_BUSINESS if name in modules] == []
    assert "importlib.metadata" not in modules


@pytest.mark.unit
def test_the_early_start_imports_nothing_of_the_engine() -> None:
    modules = _imported_by("import flux_engine.stages._prewarm")
    engine = [name for name in modules if name.startswith(("flux_engine", "flux_stats"))]
    assert sorted(engine) == ["flux_engine", "flux_engine.stages", "flux_engine.stages._prewarm"]


@pytest.mark.unit
def test_every_public_stage_name_is_still_the_stage_function() -> None:
    probe = (
        "import flux_engine.stages.verify\n"  # the module first, as a caller may import it
        "from flux_engine.stages import apply, plan, replay, run, ship, validate, verify\n"
        "import flux_engine.stages as stages\n"
        "print(all(callable(f) and f.__name__ == n for n, f in "
        "zip(stages.__all__, [stages.apply, stages.plan, stages.replay, stages.run, "
        "stages.ship, stages.validate, stages.verify])))\n"
        "print(sorted(stages.__all__))"
    )
    done = subprocess.run(
        [sys.executable, "-P", "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**_clean_environment(), "PYTHONPATH": str(REPO_ROOT)},
    )
    assert done.returncode == 0, done.stderr
    first, second = done.stdout.splitlines()[-2:]
    assert first == "True"
    assert second == str(["apply", "plan", "replay", "run", "ship", "validate", "verify"])


@pytest.mark.unit
def test_the_version_is_read_when_asked_for() -> None:
    assert "importlib.metadata" not in _imported_by("import flux_engine")
    assert "importlib.metadata" in _imported_by("import flux_engine; flux_engine.__version__")
    import flux_engine

    assert isinstance(flux_engine.__version__, str) and flux_engine.__version__


@pytest.mark.unit
def test_a_package_of_the_concordance_keeps_every_public_name() -> None:
    import flux_engine.concordance as concordance

    assert all(hasattr(concordance, name) for name in concordance.__all__)


# ---------------------------------------------------------------------------
# When the server starts, and when it does not
# ---------------------------------------------------------------------------


class _Started:
    """A stand-in for the server's ``ensure_running`` that counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> _Started:
    counter = _Started()
    monkeypatch.setattr(forkserver_module, "ensure_running", counter)
    monkeypatch.setattr(_prewarm, "cpu_jobs", lambda: 8)
    return counter


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="the forkserver is a POSIX start method")
@pytest.mark.parametrize("jobs", [None, 0, 1, True, "4"])
def test_nothing_starts_at_one_job(started: _Started, jobs: Any) -> None:
    assert _prewarm.start_forkserver_early(jobs) is False
    assert started.calls == 0


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="the forkserver is a POSIX start method")
def test_more_than_one_job_starts_the_server_once_with_the_workers_preload(
    started: _Started,
) -> None:
    assert _prewarm.start_forkserver_early(4) is True
    assert started.calls == 1
    assert forkserver_module._forkserver._preload_modules == _prewarm.preload_modules()
    assert parallel.__name__ in _prewarm.preload_modules()


@pytest.mark.unit
def test_one_cpu_or_a_spawning_platform_starts_nothing(
    started: _Started, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_prewarm, "cpu_jobs", lambda: 1)
    assert _prewarm.start_forkserver_early(4) is False
    monkeypatch.setattr(_prewarm, "cpu_jobs", lambda: 8)
    monkeypatch.setattr(_prewarm.sys, "platform", "win32")
    assert _prewarm.start_forkserver_early(4) is False
    assert started.calls == 0


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="the forkserver is a POSIX start method")
def test_the_early_start_reserves_no_worker(started: _Started) -> None:
    before = parallel._BUDGET._in_use
    _prewarm.start_forkserver_early(4)
    after = parallel._BUDGET._in_use
    assert before == after


class _StopHere(Exception):
    pass


def _events_of_a_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, jobs: int) -> List[str]:
    events: List[str] = []
    monkeypatch.setattr(
        pipeline, "start_forkserver_early", lambda requested: events.append(f"start:{requested}")
    )

    def load(*args: Any, **kwargs: Any) -> Any:
        events.append("load")
        raise _StopHere

    monkeypatch.setattr(pipeline, "load_bundle", load)
    bundle = bundle_with_rows(FIXTURE_BUNDLE, tmp_path / "bundle", 10)
    with pytest.raises(_StopHere):
        pipeline.execute(bundle, jobs=jobs)
    return events


@pytest.mark.unit
def test_the_server_is_started_before_the_bundle_is_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _events_of_a_run(monkeypatch, tmp_path, 4) == ["start:4", "load"]


@pytest.mark.unit
def test_a_one_job_run_asks_for_no_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The call is made with the job count and declines itself: the count is the whole decision.
    assert _events_of_a_run(monkeypatch, tmp_path, 1) == ["start:1", "load"]
    assert _prewarm.start_forkserver_early(1) is False


@pytest.mark.unit
def test_the_command_line_starts_the_server_before_the_stage_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from flux_engine import cli

    events: List[str] = []
    monkeypatch.setattr(
        _prewarm, "start_forkserver_early", lambda jobs: events.append(f"start:{jobs}")
    )

    def stage(args: Any) -> Any:
        events.append("stage")
        raise _StopHere

    monkeypatch.setattr(cli, "_cmd_plan", stage)
    # The command line reports a stage's failure as its own exit code, not as an exception.
    assert cli.main(["plan", str(tmp_path), "--jobs", "4"]) != 0
    assert events == ["start:4", "stage"]


# ---------------------------------------------------------------------------
# The bytes
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_a_run_with_the_server_started_early_gives_the_bytes_one_job_gives(
    tmp_path: Path,
) -> None:
    from flux_engine.stages.plan import build_plan_document

    rows = 3 * parallel.MIN_ROWS_PER_JOB + 5
    bundle = bundle_with_rows(FIXTURE_BUNDLE, tmp_path / "bundle", rows)
    assert _prewarm.start_forkserver_early(4) is True
    four = pipeline.execute(bundle, jobs=4)
    one = pipeline.execute(bundle, jobs=1)
    assert build_plan_document(four) == build_plan_document(one)
    for mine, theirs in zip(four.emits, one.emits, strict=True):
        assert mine.writer.lines() == theirs.writer.lines()


# ---------------------------------------------------------------------------
# Ctrl-C while the server imports
# ---------------------------------------------------------------------------

_SLOW_IMPORT = """
import pathlib, sys, time
pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "marker").write_text("importing")
time.sleep(3)
"""

_SERVER = """
import multiprocessing, pathlib, signal, sys, time
from multiprocessing import forkserver
from flux_engine.stages import _prewarm

directory = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(directory))
context = multiprocessing.get_context("forkserver")
context.set_forkserver_preload({preload})
forkserver.ensure_running()
deadline = time.monotonic() + 60
while not (directory / "marker").exists() and time.monotonic() < deadline:
    time.sleep(0.01)
print("importing", flush=True)
try:
    time.sleep(5)
except KeyboardInterrupt:
    pass
"""


def _interrupt_while_the_server_imports(tmp_path: Path, preload: str) -> str:
    """The stderr of a process group whose forkserver is mid-import when Ctrl-C reaches it."""
    import os
    import signal

    (tmp_path / "slowmod.py").write_text(
        _SLOW_IMPORT.replace(
            'sys.argv[1] if len(sys.argv) > 1 else "marker"', f'r"{tmp_path / "marker"}"'
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, "-P", "-c", _SERVER.format(preload=preload), str(tmp_path)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={**_clean_environment(), "PYTHONPATH": str(REPO_ROOT)},
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "importing"
    # What a terminal does with Ctrl-C: SIGINT to every process of the foreground group.
    os.killpg(process.pid, signal.SIGINT)
    try:
        _, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:  # pragma: no cover - a server that took the signal and hung
        os.killpg(process.pid, signal.SIGKILL)
        _, stderr = process.communicate()
    return stderr


@pytest.mark.unit
def test_the_preload_ignores_sigint_first_and_restores_the_handler_last() -> None:
    assert _prewarm.preload_modules() == [
        "flux_engine.stages._sigint_guard",
        "flux_engine.stages._parallel",
        "flux_engine.stages._sigint_restore",
    ]
    assert _prewarm.preload_modules("slowmod")[1] == "slowmod"
    probe = (
        "import signal\n"
        "import flux_engine.stages._sigint_guard\n"
        "print(signal.getsignal(signal.SIGINT) == signal.SIG_IGN)\n"
        "import flux_engine.stages._sigint_restore\n"
        "print(signal.getsignal(signal.SIGINT) is signal.default_int_handler)\n"
    )
    done = subprocess.run(
        [sys.executable, "-P", "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**_clean_environment(), "PYTHONPATH": str(REPO_ROOT)},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["True", "True"]


@pytest.mark.skipif(sys.platform == "win32", reason="a forkserver is a POSIX facility")
@pytest.mark.unit
def test_a_ctrl_c_while_the_server_imports_prints_no_traceback(tmp_path: Path) -> None:
    stderr = _interrupt_while_the_server_imports(tmp_path, "_prewarm.preload_modules('slowmod')")
    assert "KeyboardInterrupt" not in stderr and "Traceback" not in stderr, stderr


@pytest.mark.skipif(sys.platform == "win32", reason="a forkserver is a POSIX facility")
@pytest.mark.unit
def test_without_the_guard_the_same_ctrl_c_does_print_one(tmp_path: Path) -> None:
    """The control: the stock preload list, with the same slow import, shows the traceback the guard removes."""
    stderr = _interrupt_while_the_server_imports(tmp_path, "['slowmod']")
    assert "KeyboardInterrupt" in stderr, stderr
