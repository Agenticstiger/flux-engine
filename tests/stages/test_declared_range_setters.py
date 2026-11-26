# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A contract range on a column something else sets is not cut, is no refusal, and is noted (ADR 0024).

A history sets its validity dates, an events or periods entry sets its cells, a snapshot is the
history's columns, and a rule or a relationship sets its columns. Such a column is not drawn from its
own statistics, so the cut leaves it alone: the plan keeps the bytes of the bundle without the range,
a range that misses its statistics is no ``FLX-3403``, ``plan`` says once what sets the column, and
``verify`` counts the rows outside the range.

A history's validity columns, the columns that copy a version's start or end, and a periods entry's
period column keep to their range since the amendment of 2026-11-25
(``tests/stages/test_declared_range_held.py``); here they are run as a recorded plan without
``rangeSetters`` replays them, with the setters not held.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages._range_setters import SetBy, range_setter_notes
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.layout import RunLayout

from ._fleet import CONTRACT, apply_module, bundle, expose, plan_module, repin
from .test_declared_range_run import verify_module

pytestmark = pytest.mark.integration


def _unheld(options: Dict[str, Any]) -> pipeline.PlanEra:
    """The era *options* name, its setters not held to ranges: a plan made before they were."""
    return replace(options.get("era", pipeline.PlanEra()), hold_ranges=False)


RANGE = "between '2024-01-01' and '2024-12-31'"
MISSING = "between '2012-01-01' and '2012-12-31'"

#: ``(expose, column, what sets it)``: the four columns the history's layout writes, and a snapshot's.
SET_ELSEWHERE: List[Tuple[str, str, str]] = [
    ("vehicle_version", "valid_from", "its history"),
    ("vehicle_version", "valid_to", "its history"),
    ("vehicle_change", "changed_at", "its events entry"),
    ("vehicle_month", "month", "its periods entry"),
    ("vehicle_current", "commissioned_on", "the history it is projected from"),
]


@pytest.fixture(autouse=True)
def unheld(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every run here as a plan made before the setters kept to their ranges replays."""
    execute = pipeline.execute
    monkeypatch.setattr(
        pipeline, "execute", lambda *a, **k: execute(*a, **{**k, "era": _unheld(k)})
    )


def _ranged(tmp_path: Path, expose_id: str, column: str, rule: str, name: str = "ranged") -> Path:
    target = bundle(tmp_path, name=name)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    schema = expose(contract, expose_id)["contract"]["schema"]
    held = next(item for item in schema if item["name"] == column)
    held["validationRules"] = [{"type": "range", "constraint": rule}]
    repin(target, contract)
    return target


def _notes(stderr: str) -> List[str]:
    return [line for line in stderr.splitlines() if line.startswith("note: ")]


@pytest.mark.parametrize(("expose_id", "column", "setter"), SET_ELSEWHERE)
@pytest.mark.parametrize("rule", [RANGE, MISSING])
def test_a_column_something_else_sets_is_not_cut_and_is_noted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expose_id: str,
    column: str,
    setter: str,
    rule: str,
) -> None:
    target = _ranged(tmp_path, expose_id, column, rule)
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr
    low, high = rule.split("'")[1], rule.split("'")[3]
    assert _notes(planned.stderr) == [
        f"note: {expose_id}.{column} is not drawn inside the contract's range {low} to {high}: "
        f"{setter} sets the column, and verify counts the rows outside the range"
    ]
    assert "dateRanges" not in load_recorded_plan(
        RunLayout.for_state_dir(tmp_path / "state", planned.artifacts["run_id"])
    )
    # What the plan holds is what the bundle holds with no cut at all.
    built = pipeline.build_plan
    with monkeypatch.context() as patched:
        patched.setattr(pipeline, "build_plan", lambda *a, **k: built(*a, **{**k, "uncut": None}))
        uncut = plan_module.plan(
            target, state_dir=tmp_path / "uncut", kernel=KERNEL_V2.version, stage=False
        )
    assert uncut.artifacts["run_id"] == planned.artifacts["run_id"]


@pytest.mark.parametrize(("expose_id", "column", "setter"), SET_ELSEWHERE[:4])
def test_verify_counts_the_rows_of_a_column_something_else_sets(
    tmp_path: Path, expose_id: str, column: str, setter: str
) -> None:
    del setter
    target = _ranged(tmp_path, expose_id, column, RANGE)
    state = tmp_path / "state"
    planned = plan_module.plan(target, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    assert apply_module.apply(target, run_id=run_id, state_dir=state).success
    verified = verify_module.verify(target, run_id=run_id, state_dir=state)
    assert not verified.success
    assert any(
        line.startswith(f"  ranges: {expose_id}.{column}:") for line in verified.stdout.splitlines()
    )


def test_a_drawn_column_of_the_same_bundle_is_still_cut(tmp_path: Path) -> None:
    target = _ranged(tmp_path, "depot", "opened_on", RANGE)
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr
    assert "depot.opened_on is drawn inside the contract's range" in planned.stderr
    assert "dateRanges" in load_recorded_plan(
        RunLayout.for_state_dir(tmp_path / "state", planned.artifacts["run_id"])
    )


def test_a_column_a_rule_or_a_relationship_sets_is_noted(tmp_path: Path) -> None:
    target = _ranged(tmp_path, "depot", "opened_on", RANGE)
    result = pipeline.execute(target, kernel=KERNEL_V2)
    outcome = next(item for item in result.emits if item.resolved.expose_id == "depot")
    assert outcome.plan is not None
    emits = [item.resolved for item in result.emits]
    rule_plan = replace(
        outcome.plan, rules=SimpleNamespace(covers_every_row=frozenset({"opened_on"}))
    )
    notes = range_setter_notes(outcome.resolved, rule_plan, emits, SetBy())
    assert len(notes) == 1 and "a rule of the emit sets the column" in notes[0]
    notes = range_setter_notes(
        outcome.resolved, outcome.plan, emits, SetBy(frozenset({"opened_on"}))
    )
    assert len(notes) == 1 and "its key or relationship sets the column" in notes[0]
    assert range_setter_notes(outcome.resolved, outcome.plan, emits, SetBy()) == ()
