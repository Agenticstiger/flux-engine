# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The release body that .github/workflows/release.yml writes, run as a script.

Nothing else exercises that heredoc before a tag, so a mistake in it would show up
after the tag is pushed. These tests extract it, run it with a sample release, and
read the notes it writes. The install section follows the ``PYPI_PUBLISH`` setting:
a ``pip install`` line only when the workflow will upload the package, and never a
sentence saying the repository is private or the package is not on PyPI.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
VERSION = "9.9.9"


def _script() -> str:
    step = WORKFLOW.read_text(encoding="utf-8").split("- name: Write the release body", 1)[1]
    heredoc = re.search(r"python3 - <<'PY'\n(.*?)\n\s+PY\n", step, re.DOTALL)
    assert heredoc, "the release body step no longer runs a python heredoc"
    return textwrap.dedent(heredoc.group(1))


def _notes(tmp_path: Path, publish: str) -> str:
    (tmp_path / "hashes.txt").write_text(
        f"abc  flux_sim_engine-{VERSION}-py3-none-any.whl\n", encoding="utf-8"
    )
    env = {
        **os.environ,
        "VERSION": VERSION,
        "GITHUB_REF_NAME": f"v{VERSION}",
        "GITHUB_REPOSITORY": "example-org/flux-engine",
        "GITHUB_SHA": "0" * 40,
        "RUNNER_TEMP": str(tmp_path),
        "PYPI_PUBLISH": publish,
    }
    env.pop("PIN_STATUS", None)
    done = subprocess.run(
        [sys.executable, "-c", _script()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return (tmp_path / "notes.md").read_text(encoding="utf-8")


def test_with_publishing_on_the_notes_give_the_pip_command_and_the_wheel(tmp_path: Path) -> None:
    notes = _notes(tmp_path, "true")
    assert f"`pip install flux-sim-engine=={VERSION}`" in notes
    assert f"gh attestation verify flux_sim_engine-{VERSION}-py3-none-any.whl" in notes


def test_with_publishing_off_the_notes_give_only_the_wheel(tmp_path: Path) -> None:
    notes = _notes(tmp_path, "")
    assert "pip install flux-sim-engine==" not in notes
    assert f"gh attestation verify flux_sim_engine-{VERSION}-py3-none-any.whl" in notes


@pytest.mark.parametrize("publish", ["true", ""])
def test_the_notes_never_call_the_repository_private_or_the_package_unpublished(
    tmp_path: Path, publish: str
) -> None:
    notes = _notes(tmp_path, publish)
    assert not re.search(r"(?i)\bprivate\b|not on pypi|git\+https", notes), notes
