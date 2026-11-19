# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``--jobs`` and ``FLUX_JOBS``: on every command that generates rows.

``--jobs`` is checked while parsing; ``FLUX_JOBS`` right after it, so the
parser (and the tree ``fluxsim describe`` prints) never depends on the
environment. The last test runs ``fluxsim run`` with one job and with two on
a bundle large enough for two workers, and asks for the same digests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, List

import pytest

import flux_engine.cli as cli
from flux_engine.cli import build_parser, main
from flux_engine.stages import _parallel as parallel
from tests.bundles import bundle_with_rows

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"

_COMMANDS: List[List[str]] = [
    ["plan", "bundle"],
    ["apply", "bundle"],
    ["run", "bundle"],
    ["replay", "bundle", "--run-id", "r1", "--assert-digest", "sha256:" + "0" * 64],
]


@pytest.fixture(autouse=True)
def _no_jobs_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLUX_JOBS", raising=False)


@pytest.mark.unit
@pytest.mark.parametrize("command", _COMMANDS, ids=lambda c: c[0])
def test_every_command_that_generates_rows_takes_jobs(command: List[str]) -> None:
    parser = build_parser()
    assert parser.parse_args(command).jobs is None
    assert parser.parse_args([*command, "--jobs", "3"]).jobs == 3
    assert parser.parse_args([*command, "--jobs", "AUTO"]).jobs == parallel.cpu_jobs()


@pytest.mark.unit
@pytest.mark.parametrize("text", ["0", "-2", "x", "1.5", "", "\u00b2", "+3"])
def test_a_job_count_that_is_not_one_is_a_usage_error(
    text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(["run", "bundle", "--jobs", text])
    assert stopped.value.code == 2
    assert "expected a positive integer or 'auto'" in capsys.readouterr().err


def _jobs_seen(argv: List[str], monkeypatch: pytest.MonkeyPatch) -> int:
    """The job count ``fluxsim run`` would hand the stage, without running it."""
    seen: List[int] = []
    monkeypatch.setattr(cli, "_cmd_run", lambda args: seen.append(args.jobs) or 0)
    assert main(argv) == 0
    return seen[0]


@pytest.mark.unit
@pytest.mark.parametrize(
    "environment,argv,jobs",
    [
        (None, ["run", "bundle"], 1),
        ("4", ["run", "bundle"], 4),
        (" 4 ", ["run", "bundle"], 4),
        ("", ["run", "bundle"], 1),
        ("  ", ["run", "bundle"], 1),
        ("4", ["run", "bundle", "--jobs", "2"], 2),
    ],
)
def test_flux_jobs_is_the_default_blank_is_unset_and_the_flag_wins(
    environment: Any, argv: List[str], jobs: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    if environment is not None:
        monkeypatch.setenv("FLUX_JOBS", environment)
    assert _jobs_seen(argv, monkeypatch) == jobs


@pytest.mark.unit
def test_a_bad_flux_jobs_is_named_in_the_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FLUX_JOBS", "many")
    with pytest.raises(SystemExit) as stopped:
        _jobs_seen(["run", "bundle"], monkeypatch)
    assert stopped.value.code == 2
    assert "FLUX_JOBS: expected a positive integer or 'auto'" in capsys.readouterr().err


@pytest.mark.unit
def test_describe_does_not_depend_on_flux_jobs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def described() -> str:
        assert main(["describe", "--self", "--json"]) == 0
        return capsys.readouterr().out

    unset = described()
    for value in ("4", "auto", "many", ""):
        monkeypatch.setenv("FLUX_JOBS", value)
        assert described() == unset


def _digests(argv: List[str], capsys: pytest.CaptureFixture[str]) -> Any:
    """The run digest and each emit's outputDigest, from the run's --json envelope."""
    assert main(argv) == 0
    applied = json.loads(capsys.readouterr().out)["artifacts"]["apply"]
    emits = applied["receipt"]["emits"]
    return applied["run_digest"], {name: emit["outputDigest"] for name, emit in emits.items()}


@pytest.mark.slow
@pytest.mark.integration
def test_two_jobs_give_the_run_digest_one_job_gives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = bundle_with_rows(_FIXTURE, tmp_path / "bundle", 2 * parallel.MIN_ROWS_PER_JOB + 5)

    one = _digests(
        ["run", str(bundle), "--jobs", "1", "--state-dir", str(tmp_path / "one"), "--json"],
        capsys,
    )
    two = _digests(
        ["run", str(bundle), "--jobs", "2", "--state-dir", str(tmp_path / "two"), "--json"],
        capsys,
    )
    assert one[0].startswith("sha256:") and one[1]
    assert two == one


_RUN_AS_FLUXSIM = (
    "import os, sys\n"
    "from flux_engine.cli import main\n"
    "sys.argv = ['fluxsim', '--version']\n"
    "code = main()\n"
    "print(code, os.environ.get('PYTHONSAFEPATH'))\n"
)


@pytest.mark.integration
@pytest.mark.parametrize("before", [None, ""])
def test_fluxsim_runs_its_children_with_a_safe_path(before: Any) -> None:
    """As the program itself, the CLI sets PYTHONSAFEPATH once, for its own process.

    An empty value is unset to Python, so it is set too.
    """
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONSAFEPATH"}
    if before is not None:
        environment["PYTHONSAFEPATH"] = before
    done = subprocess.run(
        [sys.executable, "-c", _RUN_AS_FLUXSIM],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "0 1"


@pytest.mark.unit
def test_a_caller_of_main_keeps_its_own_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    assert main(["--version"]) == 0
    capsys.readouterr()
    assert "PYTHONSAFEPATH" not in os.environ
