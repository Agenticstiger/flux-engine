# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Startup budget gate: `import flux_engine.cli` must not pull numpy.

Mirrors forge-cli's tests/perf/test_startup_budget.py idea, scoped to
the CLI module itself: it must stay numpy-free on
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


#: The optional libraries behind the sink extras. Selecting a sink imports
#: them; nothing on the cold path may.
_SINK_LIBRARIES = ("pyarrow", "adbc_driver_manager")

_SINK_PROBE = (
    "import sys, flux_engine, flux_engine.cli, flux_engine.sinks, flux_engine.sinks.errors; "
    f"print([name for name in {_SINK_LIBRARIES!r} if name in sys.modules])"
)


@pytest.mark.unit
def test_importing_the_engine_and_its_sinks_package_loads_no_sink_library() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _SINK_PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]", (
        "the sinks' optional libraries must stay off the cold path: only a sink module "
        "imported because a sink was selected may load them.\n"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
