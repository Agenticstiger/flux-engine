#!/usr/bin/env python3

# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The first five minutes, timed, on a machine that has nothing of ours.

Builds the wheel (or takes a package name once the engine is published), then
does what a newcomer is told to do, through ``uvx``, from a directory that is
not this checkout and with an empty uv cache::

    uvx --from <source> fluxsim init demo
    cd demo
    uvx --from <source> fluxsim run . --verify --ship -o out

and checks that an evidence pack came out and says what it does not prove.

``uvx`` runs a tool in a throwaway environment, so a bare ``fluxsim`` on the
second line would be "command not found": both lines go through ``uvx``. The
second one reuses the environment the first built, which is also what a person
typing them one after the other gets.

**Time to first success** is the two ``uvx`` lines together: from the moment the
newcomer has ``uv`` to the moment the evidence pack is on disk. It leaves out
building the wheel (a developer's step, not a newcomer's) and installing ``uv``.
With a cold cache the first line pays for downloading and installing every
dependency, and the second is mostly the run itself.

Exit 0 when every step succeeded, the pack checks out and the total is within
the budget. Exit 1 when something failed or the budget was exceeded. Exit 2 when
this machine cannot run the check at all (no ``uvx``, or nowhere to write the report).

    make first-run
    python scripts/first_run.py --from flux-sim-engine   # once it is on PyPI
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Five minutes is the promise this check holds the project to.
DEFAULT_BUDGET_SECONDS = 300.0

#: The heading every evidence report carries, and the reason it exists.
NOT_PROVEN_HEADING = "What Was Not Proven"

_COMMAND = "fluxsim"
_PROJECT = "demo"


@dataclass
class StepResult:
    """One command of the first-run path."""

    name: str
    command: List[str]
    seconds: float
    returncode: int
    output_tail: str = ""


@dataclass
class Report:
    """What the whole check saw. ``to_json`` of this is the artifact CI keeps."""

    source: str
    cold_cache: bool
    budget_seconds: float
    steps: List[StepResult] = field(default_factory=list)
    wheel_build_seconds: Optional[float] = None
    problems: List[str] = field(default_factory=list)
    uv: str = ""
    runner: str = ""
    python: str = ""
    commit: str = ""

    @property
    def total_seconds(self) -> float:
        """Time to first success: the ``uvx`` steps, nothing else."""
        return sum(step.seconds for step in self.steps)

    @property
    def ok(self) -> bool:
        return not self.problems and all(step.returncode == 0 for step in self.steps)

    @property
    def within_budget(self) -> bool:
        return self.total_seconds <= self.budget_seconds

    def to_json(self) -> str:
        document = asdict(self)
        document["total_seconds"] = round(self.total_seconds, 2)
        document["ok"] = self.ok and self.within_budget
        for step in document["steps"]:
            step["seconds"] = round(step["seconds"], 2)
        if document["wheel_build_seconds"] is not None:
            document["wheel_build_seconds"] = round(document["wheel_build_seconds"], 2)
        return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_summary(report: Report) -> str:
    """The Markdown for ``$GITHUB_STEP_SUMMARY``: the number, then what it was made of."""
    verdict = "passed" if report.ok and report.within_budget else "FAILED"
    lines = [
        f"### First run on {report.runner or 'this machine'}: {verdict}",
        "",
        f"**Time to first success: {report.total_seconds:.1f} s** "
        f"(budget {report.budget_seconds:.0f} s, "
        f"{'cold' if report.cold_cache else 'warm'} uv cache)",
        "",
        "| step | seconds | exit |",
        "|---|---:|---:|",
    ]
    for step in report.steps:
        label = " ".join(["uvx --from <source>", *step.command[3:]])
        lines.append(f"| `{label}` | {step.seconds:.1f} | {step.returncode} |")
    lines.append("")
    details = [f"source `{report.source}`", f"{report.uv}", f"Python {report.python}"]
    if report.wheel_build_seconds is not None:
        details.append(f"wheel built in {report.wheel_build_seconds:.1f} s (not counted)")
    lines.append(", ".join(d for d in details if d.strip("` ")))
    if not report.within_budget:
        lines += ["", f"**Over budget** by {report.total_seconds - report.budget_seconds:.1f} s."]
    for problem in report.problems:
        lines += ["", f"- {problem}"]
    return "\n".join(lines) + "\n"


def check_evidence(out_dir: Path) -> List[str]:
    """Problems with the evidence pack ``fluxsim run --ship -o <out_dir>`` wrote, if any."""
    archives = sorted(out_dir.glob("*.flux.tgz"))
    if len(archives) != 1:
        return [f"expected one *.flux.tgz in {out_dir}, found {len(archives)}"]
    try:
        with tarfile.open(archives[0]) as archive:
            reports = [m for m in archive.getmembers() if m.name.endswith("/EVIDENCE.md")]
            if not reports:
                return [f"{archives[0].name} holds no EVIDENCE.md"]
            handle = archive.extractfile(reports[0])
            text = handle.read().decode("utf-8") if handle else ""
    except (tarfile.TarError, OSError) as error:
        return [f"{archives[0].name} cannot be read: {error}"]
    problems = []
    if NOT_PROVEN_HEADING not in text:
        problems.append(f'EVIDENCE.md has no "{NOT_PROVEN_HEADING}" section')
    if text.splitlines()[:1] != [f"# Evidence: {_PROJECT}"]:
        problems.append("EVIDENCE.md does not start by naming the bundle")
    return problems


def run_step(
    name: str, command: Sequence[str], *, cwd: Path, env: Dict[str, str], timeout: float
) -> StepResult:
    """Run *command*, timing it, keeping the last lines of what it printed."""
    started = time.perf_counter()
    try:
        done = subprocess.run(
            list(command), cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
        )
        returncode, output = done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired as expired:
        returncode, output = 124, f"timed out after {expired.timeout:.0f} s"
    seconds = time.perf_counter() - started
    tail = "\n".join(output.strip().splitlines()[-15:])
    return StepResult(name, list(command), seconds, returncode, tail)


def _version_of(command: Sequence[str]) -> str:
    try:
        done = subprocess.run(list(command), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


#: Not copied into the tree the wheel is built from: other people's work in
#: progress, tool caches, and above all ``build/``, whose stale ``lib/`` setuptools
#: reuses, so a starter file that was renamed would still ship in a dev machine's
#: wheel.
_NOT_BUILT_FROM = shutil.ignore_patterns(
    ".git",
    ".venv",
    "venv",
    "build",
    "dist",
    "node_modules",
    "*.egg-info",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".flux",
    ".hypothesis",
)


def _build_wheel(work: Path) -> Path:
    """Build the wheel from a copy of this tree without build output or tool caches.

    Anything else in the working tree is built, untracked files included, because
    the point of the check is what a developer is about to commit; CI starts from a
    clean checkout, so there it is exactly what is tracked.
    """
    tree = work / "tree"
    shutil.copytree(REPO_ROOT, tree, ignore=_NOT_BUILT_FROM, symlinks=True)
    out = work / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out), str(tree)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = sorted(out.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one wheel in {out}, found {len(wheels)}")
    return wheels[0]


def first_run(
    source: Optional[str], *, budget_seconds: float, cold_cache: bool, keep: bool
) -> Report:
    """Do the whole path and return what happened. Raises nothing for a failed step."""
    work = Path(tempfile.mkdtemp(prefix="fluxsim-first-run-"))
    report = Report(
        source=source or "(wheel built from this checkout)",
        cold_cache=cold_cache,
        budget_seconds=budget_seconds,
        uv=_version_of(["uv", "--version"]),
        runner=f"{platform.system()} {platform.machine()}",
        python=platform.python_version(),
        commit=os.environ.get("GITHUB_SHA", "")[:12],
    )
    try:
        if source is None:
            started = time.perf_counter()
            try:
                source = str(_build_wheel(work))
            except subprocess.CalledProcessError as error:
                tail = "\n".join(((error.stderr or "") + (error.stdout or "")).splitlines()[-15:])
                report.problems.append(f"building the wheel failed:\n{tail}")
                return report
            except (OSError, RuntimeError) as error:
                report.problems.append(f"building the wheel failed: {error}")
                return report
            report.wheel_build_seconds = time.perf_counter() - started
            report.source = Path(source).name
        env = dict(os.environ, UV_NO_PROGRESS="1")
        if cold_cache:
            env["UV_CACHE_DIR"] = str(work / "uv-cache")
        home = work / "home"
        home.mkdir()
        uvx = ["uvx", "--from", source, _COMMAND]
        init = run_step(
            "init", [*uvx, "init", _PROJECT], cwd=home, env=env, timeout=budget_seconds * 2
        )
        report.steps.append(init)
        if init.returncode == 0:
            run = run_step(
                "run",
                [*uvx, "run", ".", "--verify", "--ship", "-o", "out"],
                cwd=home / _PROJECT,
                env=env,
                timeout=budget_seconds * 2,
            )
            report.steps.append(run)
            if run.returncode == 0:
                report.problems += check_evidence(home / _PROJECT / "out")
        for step in report.steps:
            if step.returncode != 0:
                report.problems.append(f"`{step.name}` exited {step.returncode}")
    finally:
        if keep:
            print(f"kept {work}", file=sys.stderr)
        else:
            shutil.rmtree(work, ignore_errors=True)
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument(
        "--from",
        dest="source",
        default=None,
        help="what `uvx --from` gets: a wheel path or a package name "
        "(default: build a wheel from this checkout)",
    )
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument(
        "--warm-cache",
        action="store_true",
        help="use the uv cache this machine already has (default: an empty one)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    parser.add_argument(
        "--json-out",
        type=Path,
        default=REPO_ROOT / "build" / "first-run" / "first-run.json",
        help="where the timings are written (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    if shutil.which("uvx") is None or shutil.which("uv") is None:
        print(
            "first_run: this check runs `uvx`, and `uv`/`uvx` is not on PATH. "
            "Install uv (https://docs.astral.sh/uv/) and run it again.",
            file=sys.stderr,
        )
        return 2

    try:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(
            f"first_run: cannot write {args.json_out}: {error.strerror or error}", file=sys.stderr
        )
        return 2

    report = first_run(
        args.source,
        budget_seconds=args.budget_seconds,
        cold_cache=not args.warm_cache,
        keep=args.keep,
    )
    args.json_out.write_text(report.to_json(), encoding="utf-8")
    summary = render_summary(report)
    print(summary)
    for step in report.steps:
        if step.returncode != 0 and step.output_tail:
            print(f"--- last output of `{step.name}`:\n{step.output_tail}", file=sys.stderr)
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(summary)
    return 0 if report.ok and report.within_budget else 1


if __name__ == "__main__":
    sys.exit(main())
