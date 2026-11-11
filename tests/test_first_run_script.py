# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``scripts/first_run.py`` and the workflow that runs it.

The script is a gate in the sense that matters: it is the only thing that runs
the path a newcomer follows, from a built wheel, through ``uvx``. What is held
here is everything that can be checked without a network: how it measures,
what it refuses, what it reports, and that the workflow still calls it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "first_run.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "first-run.yml"

pytestmark = pytest.mark.unit


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("first_run_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


first_run = _load()


def _pack(directory: Path, *, report: str, name: str = "demo.flux.tgz") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    data = report.encode("utf-8")
    with tarfile.open(directory / name, "w:gz") as archive:
        member = tarfile.TarInfo("demo/run/EVIDENCE.md")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))


_GOOD_REPORT = "# Evidence: demo\n\n## What Was Not Proven\n\nNo validity claim is made.\n"


# --- the evidence check ------------------------------------------------------


def test_a_good_pack_has_no_problems(tmp_path: Path) -> None:
    _pack(tmp_path, report=_GOOD_REPORT)
    assert first_run.check_evidence(tmp_path) == []


def test_a_missing_pack_is_a_problem(tmp_path: Path) -> None:
    assert "found 0" in first_run.check_evidence(tmp_path)[0]


def test_a_pack_without_the_not_proven_section_is_a_problem(tmp_path: Path) -> None:
    _pack(tmp_path, report="# Evidence: demo\n\nAll good.\n")
    assert any("What Was Not Proven" in p for p in first_run.check_evidence(tmp_path))


def test_a_report_that_does_not_name_the_bundle_is_a_problem(tmp_path: Path) -> None:
    _pack(tmp_path, report="# Evidence: \n\n## What Was Not Proven\n")
    assert any("naming the bundle" in p for p in first_run.check_evidence(tmp_path))


def test_an_unreadable_archive_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / "demo.flux.tgz").write_bytes(b"not a tarball")
    assert any("cannot be read" in p for p in first_run.check_evidence(tmp_path))


# --- timing and the budget ---------------------------------------------------


def _step(name: str, seconds: float, returncode: int = 0) -> object:
    return first_run.StepResult(name, ["uvx", "--from", "x", "fluxsim", name], seconds, returncode)


def test_time_to_first_success_is_the_two_uvx_steps_and_nothing_else() -> None:
    report = first_run.Report(source="x", cold_cache=True, budget_seconds=300)
    report.wheel_build_seconds = 90.0
    report.steps = [_step("init", 40.0), _step("run", 3.5)]
    assert report.total_seconds == pytest.approx(43.5)
    assert report.ok and report.within_budget


def test_over_budget_fails_even_when_every_step_succeeded() -> None:
    report = first_run.Report(source="x", cold_cache=True, budget_seconds=10)
    report.steps = [_step("init", 9.0), _step("run", 3.0)]
    assert report.ok
    assert not report.within_budget
    assert json.loads(report.to_json())["ok"] is False
    assert "Over budget" in first_run.render_summary(report)


def test_a_failed_step_fails_the_report() -> None:
    report = first_run.Report(source="x", cold_cache=True, budget_seconds=300)
    report.steps = [_step("init", 1.0, returncode=1)]
    assert not report.ok
    assert "FAILED" in first_run.render_summary(report)


def test_the_summary_leads_with_the_number() -> None:
    report = first_run.Report(source="x.whl", cold_cache=True, budget_seconds=300, runner="Linux")
    report.steps = [_step("init", 12.34), _step("run", 2.0)]
    summary = first_run.render_summary(report)
    assert "**Time to first success: 14.3 s**" in summary
    assert "budget 300 s, cold uv cache" in summary
    assert "| `uvx --from <source> fluxsim init` | 12.3 | 0 |" in summary


def test_the_json_artifact_carries_the_numbers_a_trend_needs() -> None:
    report = first_run.Report(source="x", cold_cache=False, budget_seconds=300)
    report.steps = [_step("init", 1.234), _step("run", 2.0)]
    document = json.loads(report.to_json())
    assert document["total_seconds"] == 3.23
    assert document["cold_cache"] is False
    assert [s["name"] for s in document["steps"]] == ["init", "run"]


# --- running a step ----------------------------------------------------------


def test_run_step_times_a_command_and_keeps_the_tail_of_its_output(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "print('\\n'.join(str(i) for i in range(40)))"]
    result = first_run.run_step("count", command, cwd=tmp_path, env={}, timeout=30)
    assert result.returncode == 0
    assert result.seconds >= 0
    assert result.output_tail.splitlines()[-1] == "39"
    assert len(result.output_tail.splitlines()) == 15


def test_run_step_reports_a_timeout_instead_of_raising(tmp_path: Path) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    result = first_run.run_step("slow", command, cwd=tmp_path, env={}, timeout=0.2)
    assert result.returncode == 124
    assert "timed out" in result.output_tail


# --- a build that fails ------------------------------------------------------


def test_a_failed_wheel_build_is_a_problem_in_the_report_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(work: Path) -> Path:
        raise first_run.subprocess.CalledProcessError(
            1, ["uv", "build"], output="", stderr="error: no such file: pyproject.toml\n"
        )

    monkeypatch.setattr(first_run, "_build_wheel", broken)
    report = first_run.first_run(None, budget_seconds=300, cold_cache=True, keep=False)
    assert not report.ok
    assert report.steps == []
    assert "building the wheel failed" in report.problems[0]
    assert "no such file: pyproject.toml" in report.problems[0]
    assert "FAILED" in first_run.render_summary(report)


def test_the_wheel_is_built_from_a_clean_copy_without_stale_build_output(tmp_path: Path) -> None:
    ignored = first_run._NOT_BUILT_FROM("/x", ["build", "dist", ".venv", ".git", "flux_engine"])
    assert set(ignored) == {"build", "dist", ".venv", ".git"}


def test_main_stops_before_the_long_run_when_the_report_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(first_run.shutil, "which", lambda name: "/usr/bin/" + name)
    ran = []
    monkeypatch.setattr(first_run, "first_run", lambda *a, **k: ran.append(1))
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    assert first_run.main(["--json-out", str(blocker / "sub" / "out.json")]) == 2
    assert ran == []
    assert "cannot write" in capsys.readouterr().err


# --- a machine that cannot run it --------------------------------------------


def test_main_says_what_to_install_when_uvx_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(first_run.shutil, "which", lambda name: None)
    assert first_run.main([]) == 2
    assert "Install uv" in capsys.readouterr().err


# --- the workflow ------------------------------------------------------------


def test_the_workflow_runs_the_make_target_and_keeps_macos_off_the_pull_request_path() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "run: make first-run" in text
    # macOS may only appear in the expression that is selected for the weekly
    # and manual runs.
    assert text.count("macos-latest") == 1
    expression = text[text.index("include: >-") :]
    assert expression.index("schedule") < expression.index("macos-latest")
    assert expression.index("macos-latest") < expression.index("|| fromJSON")


def test_the_makefile_target_calls_the_script() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\nfirst-run:\n\t$(BIN)python scripts/first_run.py\n" in makefile
