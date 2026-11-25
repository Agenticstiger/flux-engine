# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history under a history that keeps to its parent's lifetime holds the rows its plan says.

The ``history-fleet`` fixture gets a second history, ``lease_version``: a lease belongs to a
vehicle version and keeps to that version's lifetime, which is the vehicle's ``commissioned_on``
and ``decommissioned_on`` (carried by every version of a vehicle, as the bound needs). Depots that
close within days leave the vehicle history fewer rows than it declares, and the lease history
reads the vehicle history's rows, so it is laid out only once the vehicle history is planned at the
rows it holds. What is pinned, through plan, apply and verify:

* plan succeeds (it did not: the lease layout read rows its parent does not have);
* each history's rows in ``plan.json`` are the rows the run wrote, and verify's rows leg agrees;
* a lease history that also holds fewer rows than it declares is planned at those rows in a
  second pass;
* a count that is still moving after one pass for every emit is refused (``FLX-3403``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify

from ._fleet import CONTRACT, DEPOT, HISTORY, NAMESPACE, UNTIL, expose, plan_module, repin, rows
from ._fleet import bundle as fleet_bundle
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run

pytestmark = pytest.mark.integration

LEASE = "lease_version"
#: Every depot lives a few days, so a vehicle takes at most three versions and the 60 vehicles hold
#: fewer than the 200 declared rows, however they are shared out.
SHORT_DEPOTS = {"range": {"min": "2024-03-05", "max": "2024-03-06"}}
#: A vehicle's lifetime: it is commissioned on one of two days and decommissioned within a week, or
#: (half of them) not at all.
COMMISSIONED = {"range": {"min": "2024-03-01", "max": "2024-03-02"}}
HALF_OPEN = {"nullFraction": "0.5", "range": {"min": "2024-03-05", "max": "2024-03-12"}}
SHORT_VEHICLES = {"range": {"min": "2024-03-05", "max": "2024-03-06"}}


def _lease_table() -> Dict[str, Any]:
    return {
        "exposeId": LEASE,
        "kind": "table",
        "binding": {
            "platform": "aws",
            "format": "glue_table",
            "location": {"database": "fleet", "table": LEASE, "region": "eu-west-1"},
        },
        "contract": {
            "schema": [
                {"name": "lease_sk", "type": "INT", "required": True},
                {"name": "lease_id", "type": "INT", "required": True},
                {"name": "vehicle_sk", "type": "INT", "required": True},
                {"name": "valid_from", "type": "DATE", "required": True},
                {"name": "valid_to", "type": "DATE", "required": False},
                {"name": "tier", "type": "STRING", "required": True},
            ]
        },
        "semantics": {
            "entities": [
                {"name": LEASE, "type": "primary", "expr": "lease_sk"},
                {"name": "lease", "type": "natural", "expr": "lease_id"},
            ],
            "dimensions": [
                {
                    "name": "valid_from",
                    "type": "time",
                    "typeParams": {"validityParams": {"isStart": True}},
                },
                {
                    "name": "valid_to",
                    "type": "time",
                    "typeParams": {"validityParams": {"isEnd": True}},
                },
                {"name": "tier", "type": "categorical"},
            ],
            "relationships": [
                {
                    "name": "vehicle_version",
                    "to": HISTORY,
                    "fromColumns": ["vehicle_sk"],
                    "toColumns": ["vehicle_sk"],
                    "cardinality": "many_to_one",
                    "joinType": "inner",
                }
            ],
        },
    }


def _bundle(
    tmp_path: Path, *, vehicles: Dict[str, Any], leases: int, lease_entities: int = 300
) -> Path:
    """The fixture with depot lifetimes, vehicle lifetimes and a lease history under the vehicles."""
    target = fleet_bundle(tmp_path, versions=200, entities=60, depots=50)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    vehicle = expose(contract, HISTORY)
    vehicle["contract"]["schema"].append(
        {"name": "decommissioned_on", "type": "DATE", "required": False}
    )
    vehicle["semantics"]["dimensions"].append({"name": "decommissioned_on", "type": "time"})
    contract["exposes"].append(_lease_table())
    repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["spec"]["emits"].append(
        {**copy.deepcopy(document["spec"]["emits"][0]), "exposeId": LEASE}
    )
    flux = document["extensions"][NAMESPACE]
    flux["history"][0]["parent"]["lifetime"] = True
    del flux["history"][0]["versions"]
    depots = flux["emits"][DEPOT]["fidelity"]["columns"]
    depots["opened_on"] = {"range": {"min": "2024-03-01", "max": "2024-03-02"}}
    depots["closed_on"] = SHORT_DEPOTS
    columns = flux["emits"][HISTORY]["fidelity"]["columns"]
    columns["commissioned_on"] = COMMISSIONED
    columns["decommissioned_on"] = vehicles
    flux["lifetimes"] = [
        {"emit": DEPOT, "start": "opened_on", "end": "closed_on"},
        {"emit": HISTORY, "start": "commissioned_on", "end": "decommissioned_on"},
    ]
    flux["history"].append(
        {
            "emit": LEASE,
            "entities": lease_entities,
            "parent": {"relationship": "vehicle_version", "minChildren": 1, "lifetime": True},
            "start": {"bins": ["2024-03-01", "2024-03-10"], "binWeights": ["1"]},
            "length": {"bins": ["1", "2", "4"], "binWeights": ["0.5", "0.5"], "closed": "lower"},
            "until": UNTIL,
            "chain": "nextStart",
        }
    )
    table = copy.deepcopy(flux["emits"][HISTORY])
    table["rows"] = leases
    declared = table["fidelity"]["columns"]
    for name in ("vehicle_sk", "vehicle_id", "model", "status", "commissioned_on"):
        declared.pop(name)
    declared.pop("decommissioned_on")
    declared["lease_sk"] = {"range": {"min": "1", "max": str(leases)}}
    declared["lease_id"] = {"range": {"min": "1", "max": str(max(lease_entities, 200))}}
    declared["tier"] = {"levels": {"basic": "0.5", "plus": "0.5"}}
    table["fidelity"]["properties"][0]["column"] = "lease_sk"
    flux["emits"][LEASE] = table
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _planned_rows(layout: RunLayout) -> Dict[str, int]:
    document = json.loads(layout.plan_path.read_text("utf-8"))
    return {name: found["generator"]["rows"] for name, found in document["emits"].items()}


def _run(bundle: Path, state: Path) -> Tuple[RunLayout, str]:
    from ._fleet import apply_module

    planned = plan_module.plan(bundle, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    applied = apply_module.apply(bundle, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr
    return RunLayout.for_state_dir(state, run_id), planned.stderr


@pytest.mark.parametrize(
    ("vehicles", "leases", "lease_rows_shrink"),
    [(HALF_OPEN, 600, False), (SHORT_VEHICLES, 2000, True)],
)
def test_a_history_under_a_shrinking_history_holds_the_rows_its_plan_says(
    tmp_path: Path, vehicles: Dict[str, Any], leases: int, lease_rows_shrink: bool
) -> None:
    bundle = _bundle(tmp_path, vehicles=vehicles, leases=leases)
    layout, notes = _run(bundle, tmp_path / "state")
    written = {name: len(rows(layout, name)) for name in (HISTORY, LEASE)}
    planned = _planned_rows(layout)
    assert written[HISTORY] < 200  # the vehicles' depots leave them fewer rows than declared
    assert (written[LEASE] < leases) is lease_rows_shrink
    assert {name: planned[name] for name in written} == written
    assert f"{written[HISTORY]} rows of the 200 declared" in notes
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stdout + verified.stderr
    text = (verified.stdout + verified.stderr).replace("\n", " ")
    assert "rows: ok" in text
    assert (
        f"history:within_parent {len(set(r['lease_id'] for r in rows(layout, LEASE)))} of" in text
    )


def test_the_lease_history_is_laid_out_against_the_rows_its_parent_holds(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, vehicles=SHORT_VEHICLES, leases=2000)
    layout, _ = _run(bundle, tmp_path / "state")
    vehicle_rows = {row["vehicle_sk"] for row in rows(layout, HISTORY)}
    assert {row["vehicle_sk"] for row in rows(layout, LEASE)} <= vehicle_rows


def test_a_run_repeats_and_its_shares_join_to_the_run(tmp_path: Path) -> None:
    from ._fleet import run_bundle

    bundle = _bundle(tmp_path, vehicles=SHORT_VEHICLES, leases=2000)
    assert_shares_join_to_the_run(bundle, tmp_path, run_bundle, 3, (HISTORY, LEASE))


@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_the_nested_shortfall_replan_is_the_same_run_at_one_and_four_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The vehicles hold fewer rows than declared and so do the leases under them: both are planned
    # again at the rows they hold, the leases after the vehicles. A worker is started for every 40
    # rows, so four jobs cut each emit into slices, and what they write, and the plan, are one
    # job's.
    monkeypatch.setattr(parallel, "MIN_ROWS_PER_JOB", 40)
    bundle = _bundle(tmp_path, vehicles=SHORT_VEHICLES, leases=2000)
    one, four = assert_same_run_at_one_and_four_jobs(bundle, result=True, kernel=KERNEL_V2)
    held = {each.resolved.expose_id: each.writer.lines() for each in one.emits}
    assert 0 < len(held[HISTORY]) < 200 and 0 < len(held[LEASE]) < 2000
    assert parallel.workers_for(len(held[LEASE]), 4) > 1
    assert {each.resolved.expose_id: each.writer.lines() for each in four.emits} == held


def test_a_count_that_keeps_moving_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path, vehicles=HALF_OPEN, leases=600)
    moving = iter(range(199, 100, -1))
    monkeypatch.setattr(pipeline, "_shrunk_histories", lambda *_a, **_k: {1: next(moving)})
    planned = plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success
    assert "FLX-3403" in planned.stderr
    assert "still changing" in planned.stderr
