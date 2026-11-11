# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Startup budget gate: `import flux_engine.cli` must not pull numpy.

Mirrors forge-cli's tests/perf/test_startup_budget.py idea, scoped to
what W0 actually ships: the CLI module itself must stay numpy-free on
import so `fluxsim --help` (and every other cold invocation) stays fast --
the CLI's command bodies do their heavy imports lazily, inside the
function that needs them, never at module scope.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_PROBE = "import flux_engine.cli; import sys; print('numpy' in sys.modules)"


@pytest.mark.unit
def test_importing_cli_does_not_load_numpy() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False", (
        "importing flux_engine.cli must not pull numpy onto the cold path -- "
        "move whatever new import triggered this behind a function-local "
        f"import in the offending command handler.\nstdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
