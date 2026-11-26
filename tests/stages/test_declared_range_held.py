# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A setter keeps to the contract's range on the columns it sets (ADR 0024, the amendment of 2026-11-25).

A range on a history's validity column, or on a projected column that copies a version's start or
end (an events entry's ``at``, and its ``date`` with no ``lag``), is a bound every entity of the
history shares: each version starts inside it and each closed end lies inside it, while an open
last version stays open. A range on a periods entry's period column keeps the periods whose first
day lies inside it. ``verify`` passes on such a run, ``plan`` records ``rangeSetters``, and only a
range that does not meet the window is refused.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout

from ._fleet import CONTRACT, HISTORY, apply_module, bundle, expose, plan_module, repin, rows
from .test_declared_range_run import verify_module

pytestmark = pytest.mark.integration


def _unheld(options: Dict[str, Any]) -> pipeline.PlanEra:
    """The era *options* name, its setters not held to ranges: a plan made before they were."""
    return replace(options.get("era", pipeline.PlanEra()), hold_ranges=False)


LOW, HIGH = "2024-01-01", "2024-12-31"
RANGE = f"between '{LOW}' and '{HIGH}'"

#: ``(expose, column)``: the columns whose setter keeps to the range.
HELD: List[Tuple[str, str]] = [
    ("vehicle_version", "valid_from"),
    ("vehicle_version", "valid_to"),
    ("vehicle_change", "changed_at"),
    ("vehicle_change", "recorded_at"),
    ("vehicle_month", "month"),
]


def _ranged(tmp_path: Path, expose_id: str, column: str, rule: str = RANGE, **options: Any) -> Path:
    target = bundle(tmp_path, **options)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    schema = expose(contract, expose_id)["contract"]["schema"]
    held = next(item for item in schema if item["name"] == column)
    held["validationRules"] = [{"type": "range", "constraint": rule}]
    repin(target, contract)
    return target


def _run(target: Path, state: Path) -> Tuple[Any, Dict[str, Any], RunLayout]:
    planned = plan_module.plan(target, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(state, planned.artifacts["run_id"])
    applied = apply_module.apply(target, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    return planned, json.loads(layout.plan_path.read_text("utf-8")), layout


@pytest.mark.parametrize(("expose_id", "column"), HELD)
def test_a_held_column_lies_inside_its_range_and_verifies(
    tmp_path: Path, expose_id: str, column: str
) -> None:
    target = _ranged(tmp_path, expose_id, column)
    state = tmp_path / "state"
    planned, document, layout = _run(target, state)
    assert document["rangeSetters"] == "held"
    assert f"{expose_id}.{column} is not drawn inside" not in planned.stderr
    values = [row[column] for row in rows(layout, expose_id) if row[column] is not None]
    assert values and all(LOW <= value <= HIGH for value in values)
    verified = verify_module.verify(target, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stdout + verified.stderr


def test_the_range_bounds_the_history_without_closing_a_version(tmp_path: Path) -> None:
    """Every version starts and every closed one ends inside it; the open ones stay open."""
    target = _ranged(tmp_path, HISTORY, "valid_from")
    _planned, document, layout = _run(target, tmp_path / "state")
    versions = rows(layout, HISTORY)
    assert len(versions) == 200 and document["rangeSetters"] == "held"
    assert all(LOW <= row["valid_from"] <= HIGH for row in versions)
    assert all(row["valid_to"] is None or row["valid_to"] <= HIGH for row in versions)
    by_vehicle: Dict[Any, List[Dict[str, Any]]] = {}
    for row in versions:
        by_vehicle.setdefault(row["vehicle_id"], []).append(row)
    assert all(chain[-1]["valid_to"] is None for chain in by_vehicle.values())


def test_a_range_that_cuts_nothing_moves_no_byte_and_is_not_recorded(tmp_path: Path) -> None:
    wide = _ranged(tmp_path, HISTORY, "valid_from", "between '2020-01-01' and '2030-12-31'")
    held = plan_module.plan(wide, state_dir=tmp_path / "s", kernel=KERNEL_V2.version, stage=False)
    assert held.success, held.stderr
    document = json.loads(
        RunLayout.for_state_dir(tmp_path / "s", held.artifacts["run_id"]).plan_path.read_text()
    )
    assert "rangeSetters" not in document
    assert "is not drawn inside" not in held.stderr


@pytest.mark.parametrize(
    ("expose_id", "column", "words"),
    [
        ("vehicle_version", "valid_from", "does not meet the window"),
        ("vehicle_change", "changed_at", "does not meet the window"),
        ("vehicle_month", "month", "holds the first day of none of the 36 periods"),
    ],
)
def test_a_range_that_does_not_meet_the_window_is_refused(
    tmp_path: Path, expose_id: str, column: str, words: str
) -> None:
    target = _ranged(tmp_path, expose_id, column, "between '2012-01-01' and '2012-12-31'")
    refused = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert not refused.success
    assert "FLX-3403" in refused.stderr and words in refused.stderr


def test_an_events_date_with_a_lag_is_not_held(tmp_path: Path) -> None:
    """A lag moves the date past the change it records, so the range on it is only noted."""
    target = _ranged(tmp_path, "vehicle_change", "recorded_at")
    simulation = target / "simulation.flux.yml"
    document = yaml.safe_load(simulation.read_text("utf-8"))
    entry = document["extensions"]["com.agentics.flux/1"]["events"][0]
    entry["lag"] = {"bins": ["0", "5", "30"], "binWeights": ["0.5", "0.5"]}
    simulation.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr
    assert "vehicle_change.recorded_at is not drawn inside" in planned.stderr


def test_an_open_end_outside_the_range_keeps_the_end_column_s_note(tmp_path: Path) -> None:
    target = _ranged(tmp_path, HISTORY, "valid_to", history={"openEnd": "9999-12-31"})
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    schema = expose(contract, HISTORY)["contract"]["schema"]
    next(item for item in schema if item["name"] == "valid_to")["validationRules"] = [
        {"type": "range", "constraint": f"between '{LOW}' and '2030-12-31'"}
    ]
    repin(target, contract)
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr
    assert f"{HISTORY}.valid_to is not drawn inside" in planned.stderr
    assert f"{HISTORY}.valid_from is not drawn inside" not in planned.stderr


def test_a_recorded_plan_without_the_marker_replays_without_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plan made before the setters kept to their ranges has no ``rangeSetters``: apply keeps its rows."""
    target = _ranged(tmp_path, HISTORY, "valid_from")
    state = tmp_path / "state"
    execute = pipeline.execute
    with monkeypatch.context() as patched:
        patched.setattr(
            pipeline, "execute", lambda *a, **k: execute(*a, **{**k, "era": _unheld(k)})
        )
        planned = plan_module.plan(target, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(state, planned.artifacts["run_id"])
    assert "rangeSetters" not in json.loads(layout.plan_path.read_text("utf-8"))
    applied = apply_module.apply(target, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    assert any(row["valid_from"] < LOW for row in rows(layout, HISTORY))


def test_a_recorded_marker_this_engine_does_not_know_is_refused() -> None:
    from flux_engine.core.errors import FluxError

    assert apply_module.plan_range_setters({}) is False
    assert apply_module.plan_range_setters({"rangeSetters": "held"}) is True
    with pytest.raises(FluxError) as caught:
        apply_module.plan_range_setters({"rangeSetters": "clipped"})
    assert caught.value.code.code == "FLX-2102"


def test_a_range_too_narrow_for_the_declared_versions_holds_fewer_rows(tmp_path: Path) -> None:
    """Two days leave each entity room for one version: the shortfall is recorded, and verify passes."""
    target = _ranged(tmp_path, HISTORY, "valid_from", "between '2025-12-01' and '2025-12-02'")
    state = tmp_path / "state"
    planned, document, layout = _run(target, state)
    assert "the validity columns' range leaves" in planned.stderr
    assert "so the history has 60 rows of the 200 declared" in planned.stderr
    versions = rows(layout, HISTORY)
    assert len(versions) == 60 and {row["valid_from"] for row in versions} <= {
        "2025-12-01",
        "2025-12-02",
    }
    assert document["rangeSetters"] == "held"
    verified = verify_module.verify(target, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stdout + verified.stderr


def _events_when(target: Path, when: str) -> None:
    """Give the events entry a ``when``: it records only the versions the condition holds for."""
    simulation = target / "simulation.flux.yml"
    document = yaml.safe_load(simulation.read_text("utf-8"))
    document["extensions"]["com.agentics.flux/1"]["events"][0]["when"] = when
    simulation.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")


def test_an_events_entry_with_a_when_leaves_the_history_unheld(tmp_path: Path) -> None:
    """The range on a column an events entry with a ``when`` copies is only noted.

    The entry records only the versions its condition holds for, so the range says nothing of the
    others: the history keeps its first starts, and verify counts any event outside the range.
    """
    target = _ranged(tmp_path, "vehicle_change", "changed_at")
    _events_when(target, f"valid_from >= DATE '{LOW}'")
    state = tmp_path / "state"
    planned = plan_module.plan(target, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    assert planned.success, planned.stderr
    assert "vehicle_change.changed_at is not drawn inside" in planned.stderr
    layout = RunLayout.for_state_dir(state, planned.artifacts["run_id"])
    assert "rangeSetters" not in json.loads(layout.plan_path.read_text("utf-8"))
    applied = apply_module.apply(target, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    assert any(row["valid_from"] < LOW for row in rows(layout, HISTORY))
    assert all(row["changed_at"] >= LOW for row in rows(layout, "vehicle_change"))


def test_a_parent_bound_history_plans_past_an_events_entry_with_a_when(tmp_path: Path) -> None:
    """A parent that closed before the range's low end still leaves its entities room."""
    from .test_history_parent_run import _bundle as parent_bound

    target = parent_bound(tmp_path)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    schema = expose(contract, "vehicle_change")["contract"]["schema"]
    held = next(item for item in schema if item["name"] == "changed_at")
    held["validationRules"] = [
        {"type": "range", "constraint": "between '2025-06-01' and '2025-12-31'"}
    ]
    repin(target, contract)
    _events_when(target, "valid_from >= DATE '2025-06-01'")
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr


def test_a_parent_closed_before_the_validity_range_is_refused_naming_the_range(
    tmp_path: Path,
) -> None:
    """Every version starts inside the range, so a parent that closed before it leaves no room."""
    from .test_history_parent_run import _bundle as parent_bound

    target = parent_bound(tmp_path)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    schema = expose(contract, HISTORY)["contract"]["schema"]
    held = next(item for item in schema if item["name"] == "valid_from")
    held["validationRules"] = [
        {"type": "range", "constraint": "between '2025-06-01' and '2025-12-31'"}
    ]
    repin(target, contract)
    refused = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert not refused.success and "FLX-3403" in refused.stderr
    assert "from the range on its validity columns (2025-06-01)" in refused.stderr
    assert "widen the range" in refused.stderr
