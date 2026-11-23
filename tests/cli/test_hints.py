# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``next:`` lines: the command that follows, with the flags it needs.

The lines are computed from the arguments of the command just run and the
artifacts it returned, so they can be checked without running anything.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List

import pytest

from flux_engine.cli._hints import next_steps

pytestmark = pytest.mark.unit


def _args(**values: Any) -> argparse.Namespace:
    defaults: Dict[str, Any] = {
        "bundle": "./bundle",
        "state_dir": None,
        "run_id": None,
        "simulation_id": None,
        "zone": None,
        "committed_slice": None,
        "verify": False,
        "ship": False,
    }
    defaults.update(values)
    return argparse.Namespace(**defaults)


def _steps(stage: str, artifacts: Dict[str, Any], **values: Any) -> List[str]:
    return next_steps(stage, _args(**values), artifacts)


def test_validate_points_at_the_whole_loop() -> None:
    assert _steps("validate", {}) == ["fluxsim run ./bundle --verify --ship -o out"]


def test_plan_hands_apply_the_simulation_and_the_zone_it_was_given() -> None:
    """``apply`` re-executes the pipeline: without the id a bundle of two Simulations refuses."""
    assert _steps(
        "plan", {"run_id": "abc123"}, simulation_id="sim-b", zone="eu-west", state_dir="s"
    ) == [
        "fluxsim apply ./bundle --run-id abc123 --state-dir s --simulation-id sim-b --zone eu-west"
    ]


def test_apply_points_at_verify() -> None:
    assert _steps("apply", {"run_id": "abc123"}) == ["fluxsim verify ./bundle --run-id abc123"]


def test_verify_points_at_ship_and_carries_the_committed_slices() -> None:
    steps = _steps(
        "verify",
        {},
        run_id="abc123",
        committed_slice=["a=file:///x.ndjson", "b=file:///y.ndjson"],
    )
    assert steps == [
        "fluxsim ship ./bundle --run-id abc123 -o out "
        "--committed-slice a=file:///x.ndjson --committed-slice b=file:///y.ndjson"
    ]


def test_a_run_names_the_stage_it_stopped_before() -> None:
    plan_and_apply = {"plan": {"run_id": "abc123"}, "apply": {"run_id": "abc123"}}
    assert _steps("run", plan_and_apply) == ["fluxsim verify ./bundle --run-id abc123"]
    assert _steps("run", plan_and_apply, verify=True) == [
        "fluxsim ship ./bundle --run-id abc123 -o out"
    ]


def test_a_run_that_shipped_points_at_the_report() -> None:
    steps = _steps("run", {"apply": {"run_id": "abc123"}}, verify=True, ship=True, state_dir="st")
    assert steps == ["read st/runs/abc123/EVIDENCE.md: it says what this run does not prove"]


def test_ship_points_at_the_report_in_the_default_state_directory() -> None:
    assert _steps("ship", {}, run_id="abc123") == [
        "read .flux/runs/abc123/EVIDENCE.md: it says what this run does not prove"
    ]


@pytest.mark.parametrize("stage", ["replay", "calibrate", "explain", "describe", "init", "plugins"])
def test_commands_outside_the_loop_suggest_nothing(stage: str) -> None:
    assert _steps(stage, {"run_id": "abc123"}) == []


def test_a_command_with_no_bundle_suggests_nothing() -> None:
    assert next_steps("validate", argparse.Namespace(), {}) == []


def test_every_word_that_came_from_the_user_is_quoted_for_the_shell() -> None:
    steps = _steps(
        "plan",
        {"run_id": "abc123"},
        bundle="my bundle",
        state_dir="my state",
        simulation_id="a;b",
    )
    assert steps == [
        "fluxsim apply 'my bundle' --run-id abc123 --state-dir 'my state' --simulation-id 'a;b'"
    ]
