# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The loop a skill teaches is the loop that runs: its command lines, in order, on the starter bundle.

Each line goes through ``flux_engine.cli.main``, never a shell, in a temporary folder, after
``fluxsim init bundle``. ``RUN_ID`` becomes the id the latest ``plan`` wrote, and ``OUT`` a
temporary folder.
"""

from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path
from typing import List, Optional

import pytest

from flux_engine.cli import main
from tests.skills.support import REPO_ROOT, check_skills

pytestmark = pytest.mark.unit

SKILLS = REPO_ROOT / "skills"
NEEDS_ARROW = ("parquet:", "arrow-ipc:")


def latest_run_id(folder: Path) -> Optional[str]:
    runs = folder / ".flux" / "runs"
    found = sorted(runs.iterdir(), key=lambda p: p.stat().st_mtime) if runs.is_dir() else []
    return found[-1].name if found else None


def assert_inside(line: str) -> None:
    """A skill's command names no path outside the folder it runs in."""
    for word in shlex.split(line)[1:]:
        value = word.split("=", 1)[-1].split(":", 1)[-1]
        assert not value.startswith(("/", "~")), f"an absolute path in {line!r}"
        assert ".." not in Path(value).parts, f"a path that climbs out in {line!r}"


def run_lines(skill: str, folder: Path) -> List[str]:
    """Run the skill's command lines; return the lines that ran."""
    out = folder / "out"
    out.mkdir()
    assert main(["init", "bundle"]) == 0
    ran: List[str] = []
    for line in check_skills.command_lines(SKILLS / skill / "SKILL.md"):
        if (
            any(sink in line for sink in NEEDS_ARROW)
            and importlib.util.find_spec("pyarrow") is None
        ):
            continue
        run_id = latest_run_id(folder)
        if "RUN_ID" in line and run_id is None:
            pytest.fail(f"no plan has run before: {line}")
        assert_inside(line)
        words = [
            w.replace("RUN_ID", run_id or "").replace("OUT", str(out))
            for w in shlex.split(line)[1:]
        ]
        assert main(words) == 0, line
        ran.append(line)
    return ran


@pytest.mark.parametrize("skill", ["flux-run-and-verify", "flux-scale-and-ship"])
def test_a_skills_commands_run_in_order(
    skill: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ran = run_lines(skill, tmp_path)
    assert any(line.startswith("fluxsim plan") for line in ran)


@pytest.mark.parametrize(
    "line", ["fluxsim ship bundle -o /tmp/x", "fluxsim plan ../bundle", "fluxsim plan ~/bundle"]
)
def test_a_command_that_leaves_its_folder_is_not_run(line: str) -> None:
    with pytest.raises(AssertionError):
        assert_inside(line)
