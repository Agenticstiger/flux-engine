# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The population-500 copy, not the 25 000-subject one. Two tests in this
#: module alone burned 264s of its 265s against the full-size fixture,
#: while every other suite that exercises the same code paths
#: (acceptance/, golden/) already uses the small copy. What this module
#: tests is the real binary's argv/exit-code/envelope behaviour, which is
#: identical at either population.
FIXTURE_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

pytestmark = pytest.mark.integration


@pytest.fixture()
def bundle_dir() -> Path:
    assert FIXTURE_BUNDLE.is_dir(), FIXTURE_BUNDLE
    return FIXTURE_BUNDLE


def run_flux(args: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess:
    """Invoke the real ``python -m flux_engine.cli`` binary as a subprocess."""
    argv: List[str] = [sys.executable, "-m", "flux_engine.cli", *args]
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120)


def parse_json_stdout(result: subprocess.CompletedProcess) -> dict:
    return json.loads(result.stdout)
