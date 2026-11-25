# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The rows a periodic emit may be estimated to derive (ADR 0027 section 5, the amendment of 2026-11-25).

``plan`` estimates a periods entry's rows from its versions before it lists a period, and refuses
(``FLX-3403``) an estimate above the budget: 50,000,000 rows unless ``fluxsim plan --max-rows``
sets another, which ``plan.json`` records as ``maxRows`` so ``apply --run-id`` holds the run to it.
"""

from __future__ import annotations

import json
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from flux_engine.cli import main
from flux_engine.cli.commands.plan import DEFAULT_MAX_ROWS, MOST_MAX_ROWS
from flux_engine.core.errors import FluxError
from flux_engine.core.sizing import MOST_ROWS
from flux_engine.generator.periods import DEFAULT_PERIODIC_ROWS
from flux_engine.stages.layout import RunLayout

from . import _fleet

apply_module = _fleet.apply_module
plan_module = _fleet.plan_module

pytestmark = pytest.mark.integration

#: Five thousand years of months: 60,000 periods.
LONG_WINDOW = "7023-01-01T00:00:00Z"


def _plan(target: Path, state: Path, **options: Any) -> Any:
    return plan_module.plan(
        target, state_dir=state, kernel=_fleet.KERNEL_V2.version, stage=False, **options
    )


def test_a_long_monthly_window_is_refused_before_its_periods_are_laid(tmp_path: Path) -> None:
    target = _fleet.bundle(
        tmp_path, entities=1000, versions=2500, depots=50, window_end=LONG_WINDOW
    )
    tracemalloc.start()
    try:
        refused = _plan(target, tmp_path / "state")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert not refused.success
    words = refused.stderr
    assert "FLX-3403" in words
    assert f"periods ({_fleet.PERIODIC})" in words
    assert "grain month" in words and "60000 periods" in words
    assert "2023-01-01T00:00:00.000Z to 7023-01-01T00:00:00.000Z" in words
    assert f"above the budget of {DEFAULT_PERIODIC_ROWS} rows" in words
    assert "narrow the window" in words and "--max-rows N" in words
    # Sixty million rows would take gigabytes; the refusal comes before the periods are listed.
    assert peak < 32 * 1024 * 1024, peak


def test_a_budget_below_the_estimate_refuses_and_names_it(tmp_path: Path) -> None:
    target = _fleet.bundle(tmp_path)
    refused = _plan(target, tmp_path / "state", max_rows=100)
    assert not refused.success
    assert "FLX-3403" in refused.stderr
    assert "above the budget of 100 rows" in refused.stderr


def test_a_raised_budget_is_recorded_and_the_recorded_plan_replays(tmp_path: Path) -> None:
    long_window = _fleet.bundle(tmp_path, name="long", window_end="2226-01-01T00:00:00Z")
    state = tmp_path / "state"
    assert not _plan(long_window, state, max_rows=10_000).success
    planned = _plan(long_window, state, max_rows=1_000_000)
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    document = json.loads(RunLayout.for_state_dir(state, run_id).plan_path.read_text("utf-8"))
    assert document["maxRows"] == 1_000_000
    applied = apply_module.apply(long_window, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr


def test_the_default_budget_is_not_recorded(tmp_path: Path) -> None:
    target = _fleet.bundle(tmp_path)
    state = tmp_path / "state"
    planned = _plan(target, state, max_rows=DEFAULT_PERIODIC_ROWS)
    assert planned.success, planned.stderr
    plain = _plan(target, tmp_path / "plain")
    assert planned.artifacts["run_id"] == plain.artifacts["run_id"]
    layout = RunLayout.for_state_dir(state, planned.artifacts["run_id"])
    assert "maxRows" not in json.loads(layout.plan_path.read_text("utf-8"))


@pytest.mark.parametrize("named", [0, MOST_ROWS + 1, "1000", True, 1.5])
def test_a_recorded_budget_this_engine_cannot_read_is_refused(named: object) -> None:
    with pytest.raises(FluxError) as caught:
        apply_module.plan_max_rows({"maxRows": named})
    assert caught.value.code.code == "FLX-2102"
    assert "maxRows" in str(caught.value)


def test_a_recorded_budget_is_read_back() -> None:
    assert apply_module.plan_max_rows({}) is None
    assert apply_module.plan_max_rows({"maxRows": MOST_ROWS}) == MOST_ROWS


@pytest.mark.parametrize("asked", [0, MOST_ROWS + 1])
def test_plan_takes_a_budget_from_1_to_the_most_rows(tmp_path: Path, asked: int) -> None:
    with pytest.raises(ValueError, match="max_rows"):
        _plan(_fleet.bundle(tmp_path), tmp_path / "state", max_rows=asked)


def test_the_flag_states_the_engine_s_default_and_ceiling() -> None:
    assert DEFAULT_MAX_ROWS == DEFAULT_PERIODIC_ROWS
    assert MOST_MAX_ROWS == MOST_ROWS


def test_the_flag_sets_the_budget_the_plan_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _fleet.bundle(tmp_path)
    state = tmp_path / ".flux"
    argv = ["-j", "plan", str(target), "--state-dir", str(state), "--max-rows", "100"]
    assert main(argv) != 0
    assert "above the budget of 100 rows" in capsys.readouterr().out
    assert main([*argv[:-1], "2000000"]) == 0
    run_id = json.loads(capsys.readouterr().out)["artifacts"]["run_id"]
    document = json.loads(RunLayout.for_state_dir(state, run_id).plan_path.read_text("utf-8"))
    assert document["maxRows"] == 2_000_000


@pytest.mark.parametrize("asked", ["0", "-5", "many", str(MOST_ROWS + 1)])
def test_a_budget_outside_1_to_the_most_rows_is_a_usage_error(
    tmp_path: Path, asked: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["plan", str(tmp_path), "--max-rows", asked])
    assert exited.value.code == 2
    assert "--max-rows" in capsys.readouterr().err
