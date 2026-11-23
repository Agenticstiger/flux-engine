# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What to run next, for a person at a terminal.

A command that succeeds leaves the reader at a fork: the loop has six steps and
the one just finished does not say which comes next. This module computes that
sentence from what the command already knows, the bundle it was given and the run
id it produced, so the reader copies a line instead of reading the reference.

It is a courtesy, not output. The lines go to stderr, only on a terminal, never
under ``--json``, and ``FLUX_NO_PROGRESS`` silences them, the same gate as the
progress lines (:func:`flux_engine.cli._progress.progress_enabled`). stdout and
every artifact are untouched, so a pipe, a CI log and a test see exactly what they
saw before this module existed.
"""

from __future__ import annotations

import argparse
import shlex
from typing import Any, List, Mapping, Optional

__all__ = ["next_steps"]

_PROG = "fluxsim"
_OUT = "out"


def _run_id(stage: str, args: argparse.Namespace, artifacts: Mapping[str, Any]) -> Optional[str]:
    """The id of the run this command produced or acted on."""
    direct = artifacts.get("run_id")
    if isinstance(direct, str) and direct:
        return direct
    for key in ("apply", "plan"):  # `run` nests the stages it ran
        nested = artifacts.get(key)
        if isinstance(nested, Mapping) and isinstance(nested.get("run_id"), str):
            return str(nested["run_id"])
    given = getattr(args, "run_id", None)
    return str(given) if given else None


def _command(*parts: str) -> str:
    return " ".join([_PROG, *parts])


def _carried(args: argparse.Namespace, *names: str) -> List[str]:
    """The flags of the command just run that the next one needs again.

    ``plan --simulation-id X`` names the Simulation; ``apply`` re-executes the
    pipeline and refuses a bundle of several Simulations without it, so a hint that
    dropped the flag was a command that fails. A committed slice goes the same way
    from ``verify`` to ``ship``. Keys do not: argparse has already read them into
    bytes, and a record signed without one says so.
    """
    carried: List[str] = []
    for name in names:
        value = getattr(args, name, None)
        flag = "--" + name.replace("_", "-")
        if isinstance(value, str) and value:
            carried += [flag, shlex.quote(value)]
        elif isinstance(value, (list, tuple)):
            for item in value:
                carried += [flag, shlex.quote(str(item))]
    return carried


def next_steps(stage: str, args: argparse.Namespace, artifacts: Mapping[str, Any]) -> List[str]:
    """The lines to print after *stage* succeeded, or none.

    Only the six-step loop has a next step. Every other command, a lookup or a
    one-off check, leaves the reader nowhere in particular to go.
    """
    bundle = getattr(args, "bundle", None)
    if not bundle:
        return []
    where = shlex.quote(str(bundle))
    state_dir = getattr(args, "state_dir", None)
    state = ["--state-dir", shlex.quote(state_dir)] if state_dir else []
    raw_id = _run_id(stage, args, artifacts)
    run_id = shlex.quote(raw_id) if raw_id else None
    slices = _carried(args, "committed_slice")
    ship = [
        _command("ship", where, "--run-id", run_id or "<runId>", *state, "-o", _OUT, *slices),
    ]

    if stage == "validate":
        return [_command("run", where, "--verify", "--ship", "-o", _OUT)]
    if stage == "plan" and run_id:
        return [
            _command(
                "apply", where, "--run-id", run_id, *state, *_carried(args, "simulation_id", "zone")
            )
        ]
    if stage == "apply" and run_id:
        return [_command("verify", where, "--run-id", run_id, *state)]
    if stage == "verify":
        return ship
    if stage == "run":
        if getattr(args, "ship", False):
            return [_read_the_report(raw_id, state_dir)] if raw_id else []
        return ship if getattr(args, "verify", False) else _verify(where, run_id, state, slices)
    if stage == "ship" and raw_id:
        return [_read_the_report(raw_id, state_dir)]
    return []


def _verify(where: str, run_id: Optional[str], state: List[str], slices: List[str]) -> List[str]:
    if not run_id:
        return []
    return [_command("verify", where, "--run-id", run_id, *state, *slices)]


def _read_the_report(run_id: str, state_dir: Optional[str]) -> str:
    base = state_dir or ".flux"
    path = f"{base.rstrip('/')}/runs/{run_id}/EVIDENCE.md"
    return f"read {shlex.quote(path)}: it says what this run does not prove"
