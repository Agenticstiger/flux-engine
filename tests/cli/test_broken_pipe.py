# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A reader that closes stdout early does not turn fluxsim's work into a failure.

``fluxsim <command> | head -1`` closes the pipe once it has its line. A command whose
work was done leaves quietly with its own status; one still working when the pipe broke
leaves quietly with 141 (128 plus SIGPIPE). Neither prints a traceback or the
``FLUX_DEBUG`` hint, and neither lets the interpreter's flush at shutdown raise again.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
from typing import Any, List

import pytest

from flux_engine.api.stages import StageResult
from flux_engine.cli import _run_command
from flux_engine.cli._output import _print_result

pytestmark = pytest.mark.unit

_FORBIDDEN = ("Traceback", "BrokenPipeError", "FLUX_DEBUG")


class _ClosedPipe(io.StringIO):
    """A stdout whose reader has gone: every write or flush raises."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def _args() -> argparse.Namespace:
    return argparse.Namespace(json=False, stage_name="explain", command="explain")


def test_a_finished_result_keeps_its_status_when_the_pipe_is_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdout", _ClosedPipe())
    result = StageResult(stage="explain", exit_code=0, _artifacts={}, stdout="a line")
    assert _print_result(result, as_json=False) == 0
    refused = StageResult(stage="explain", exit_code=1, _artifacts={}, stdout="x", stderr="no")
    assert _print_result(refused, as_json=False) == 1
    assert capsys.readouterr().err == ""


def test_a_command_cut_short_by_a_closed_pipe_exits_141_quietly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FLUX_DEBUG", raising=False)

    def working(args: Any) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    assert _run_command(working, _args()) == 141
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command", [["explain", "--list"], ["--json", "explain", "--list"]])
def test_fluxsim_leaves_quietly_when_stdout_is_already_closed(command: List[str]) -> None:
    """A real process whose stdout is a pipe with no reader: the first write fails, every time."""
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        done = subprocess.run(
            [sys.executable, "-m", "flux_engine.cli", *command],
            stdout=write_end,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
            env={k: v for k, v in os.environ.items() if k != "FLUX_DEBUG"},
        )
    finally:
        os.close(write_end)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    for word in _FORBIDDEN:
        assert word not in done.stderr
