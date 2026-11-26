# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle to run in tests: ``tests/fixtures/history-fleet`` with its sizes, end mode and keys changed.

The fixture is a depot (the parent, with a lifetime of ``opened_on`` to ``closed_on``), a versioned
vehicle (``vehicle_version``, a history under it), a plain child ``service_visit`` and the four
tables a history projects: ``vehicle_current`` (the snapshot), ``vehicle_change`` (the events) and
``vehicle_month`` (the periods). ``bundle`` copies it and applies the options a test names;
``repin`` re-pins the contract digest after a test edits the contract.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import yaml

from flux_engine.core.digest import digest_of_document
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.layout import RunLayout

apply_module = import_module("flux_engine.stages.apply")
plan_module = import_module("flux_engine.stages.plan")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "history-fleet"
NAMESPACE = "com.agentics.flux/1"
CONTRACT = "vehicles.fluid.yml"
PRODUCT = "fleet.vehicles"
DEPOT = "depot"
HISTORY = "vehicle_version"
SNAPSHOT = "vehicle_current"
EVENTS = "vehicle_change"
PERIODIC = "vehicle_month"
VISIT = "service_visit"
UNTIL = "2025-12-31"
#: The levels of ``status`` when a history ends in a final version: ``retired`` is drawn by no row.
STATUSES = {"status": {"levels": {"active": "0.7", "maintenance": "0.3", "retired": "0"}}}
#: The end mode that leaves a quarter of the entities in a ``retired`` final version.
FINAL = {"mode": "finalVersion", "share": "0.25", "column": "status", "value": "retired"}


@dataclass(frozen=True)
class Names:
    """What ``bundle`` calls the tables and keys of a fixture it copies."""

    parent: str
    parent_key: str
    history: str
    history_sk: str
    key: str
    snapshot: str
    statuses: Dict[str, Any]


FLEET = Names(DEPOT, "depot_id", HISTORY, "vehicle_sk", "vehicle_id", SNAPSHOT, STATUSES)


def bundle(
    tmp_path: Path,
    *,
    fixture: Path = FIXTURE,
    names: Names = FLEET,
    name: str = "bundle",
    versions: int = 200,
    entities: int = 60,
    depots: int = 50,
    window_end: Optional[str] = None,
    ended: Optional[str] = None,
    end: Optional[Dict[str, Any]] = None,
    snapshot_rows: Optional[int] = None,
    minimum: Optional[int] = None,
    history: Optional[Dict[str, Any]] = None,
    columns: Optional[Dict[str, Any]] = None,
) -> Path:
    """A copy of ``fixture`` with its sizes, window end, end mode or snapshot ``ended`` changed."""
    target = tmp_path / name
    shutil.copytree(fixture, target)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][NAMESPACE]
    emits = flux["emits"]
    emits[names.parent]["rows"] = depots
    emits[names.parent]["fidelity"]["columns"][names.parent_key]["range"]["max"] = str(depots)
    emits[names.history]["rows"] = versions
    declared = emits[names.history]["fidelity"]["columns"]
    declared[names.history_sk]["range"]["max"] = str(versions)
    declared[names.key]["range"]["max"] = str(max(entities, 200))
    snapshot_columns = emits[names.snapshot]["fidelity"]["columns"]
    snapshot_columns[names.key]["range"]["max"] = str(max(entities, 200))
    flux["history"][0]["entities"] = entities
    if minimum is not None:
        flux["history"][0]["parent"]["minChildren"] = minimum
    if end is not None:
        flux["history"][0]["end"] = end
        declared.update(names.statuses)
        snapshot_columns.update(names.statuses)
    if ended is not None:
        flux["snapshots"][0]["ended"] = ended
    if snapshot_rows is not None:
        emits[names.snapshot]["rows"] = snapshot_rows
    if window_end is not None:
        document["spec"]["timeBounds"]["end"] = window_end
    flux["history"][0].update(history or {})
    declared.update(columns or {})
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def repin(target: Path, contract: Dict[str, Any]) -> None:
    """Write ``contract`` into the bundle and pin its digest in every emit of the simulation."""
    (target / CONTRACT).write_text(yaml.safe_dump(contract, sort_keys=False), "utf-8")
    digest = digest_of_document(contract, subject=f"contractDigest:{PRODUCT}").value
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    for emit in document["spec"]["emits"]:
        emit["provenance"]["contractDigest"] = digest
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")


def expose(contract: Dict[str, Any], expose_id: str) -> Dict[str, Any]:
    """The expose ``expose_id`` of a loaded contract."""
    return next(item for item in contract["exposes"] if item["exposeId"] == expose_id)


def with_range(target: Path, expose_id: str, column: str, rule: str) -> Path:
    """Declare ``rule`` as the contract's range on ``expose_id``.``column``, and re-pin the bundle."""
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    schema = expose(contract, expose_id)["contract"]["schema"]
    held = next(item for item in schema if item["name"] == column)
    held["validationRules"] = [{"type": "range", "constraint": rule}]
    repin(target, contract)
    return target


def rows(layout: RunLayout, expose_id: str) -> List[Dict[str, Any]]:
    """The rows a run wrote for one emit, as dicts."""
    names = json.loads(layout.seam_attestation_path(expose_id).read_text("utf-8"))["columns"]
    return [
        dict(zip(names, json.loads(line), strict=True))
        for line in layout.emit_path(expose_id).read_text("utf-8").splitlines()
    ]


def run_bundle(target: Path, state: Path, **options: Any) -> RunLayout:
    """Plan and apply a bundle under kernel 0.2.0; the run's layout."""
    planned = plan_module.plan(
        target, state_dir=state, kernel=KERNEL_V2.version, stage=False, **options
    )
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    applied = apply_module.apply(target, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr
    return RunLayout.for_state_dir(state, run_id)


def plan_outcome(target: Path, state: Path, **options: Any) -> Any:
    """Plan a bundle under kernel 0.2.0, staging nothing; the outcome, whether it planned or not."""
    return plan_module.plan(
        target, state_dir=state, kernel=KERNEL_V2.version, stage=False, **options
    )


def plan_refused(target: Path, state: Path) -> Any:
    """Plan a bundle under kernel 0.2.0, which refuses it; the refused outcome."""
    planned = plan_outcome(target, state)
    assert not planned.success
    return planned


def group_by(
    rows: Iterable[Mapping[str, Any]], column: str, keys: Optional[Iterable[Any]] = None
) -> Dict[Any, List[Any]]:
    """``rows`` grouped by ``column``, in the order they were written; ``keys`` fixes the groups."""
    found: Dict[Any, List[Any]] = defaultdict(list) if keys is None else {k: [] for k in keys}
    for row in rows:
        found[row[column]].append(row)
    return dict(found)


def by_vehicle(history_rows: List[Dict[str, Any]]) -> Dict[Any, List[Dict[str, Any]]]:
    """A history's rows grouped by vehicle, in the order the rows were written."""
    return group_by(history_rows, "vehicle_id")
