# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A link into a projected emit reads the rows the emit writes (ADR 0027).

A snapshot is the last version of each entity, every column its final value. A table that links to
it and keeps a date within its lifetime (ADR 0024 section 5) reads the snapshot's row, so every
date holds against the cell the snapshot writes. A recorded plan without ``projectedReads`` replays
the link as it was made, reading the cell the snapshot would draw without its projection, and the
run is the same at one job and at four.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.core.digest import build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.stages import pipeline
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.replay import replay

from . import _fleet
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_declared_range_replay import recorded_digest, run_as_before

pytestmark = pytest.mark.integration

KEY = "projectedReads"


NOTE = "visit_note"
NOTE_EXPOSE = {
    "exposeId": NOTE,
    "kind": "table",
    "binding": {
        "platform": "aws",
        "format": "glue_table",
        "location": {"database": "fleet", "table": NOTE, "region": "eu-west-1"},
    },
    "contract": {
        "schema": [
            {"name": "note_id", "type": "INT", "required": True},
            {"name": "visit_id", "type": "INT", "required": True},
            {"name": "noted_on", "type": "DATE", "required": True},
        ]
    },
    "semantics": {
        "entities": [{"name": NOTE, "type": "primary", "expr": "note_id"}],
        "dimensions": [{"name": "noted_on", "type": "time"}],
        "relationships": [
            {
                "name": "visit",
                "to": _fleet.VISIT,
                "fromColumns": ["visit_id"],
                "toColumns": ["visit_id"],
                "cardinality": "many_to_one",
                "joinType": "inner",
            }
        ],
    },
}


def linked_bundle(tmp_path: Path, *, within: bool = True, notes: bool = False) -> Path:
    """The fleet bundle with a service visit that links to the snapshot, within its commissioning.

    A visit's ``vehicle`` relationship reaches ``vehicle_current``, whose lifetime starts at
    ``commissioned_on``; the visit's date is kept within it. Without *within* the link is declared
    and read by nothing. With *notes*, a note is dated within its visit's lifetime, which starts at
    the visit's date: it reads the snapshot through the visit.
    """
    target = _fleet.bundle(tmp_path)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][_fleet.NAMESPACE]
    if notes:
        document["spec"]["emits"].append(
            {
                "productRef": _fleet.PRODUCT,
                "exposeId": NOTE,
                "fluidVersion": "0.7.7",
                "provenance": {"contractDigest": "sha256:" + "0" * 64},
            }
        )
        flux["emits"][NOTE] = _note_fidelity()
    if within:
        flux["lifetimes"] = [{"emit": _fleet.SNAPSHOT, "start": "commissioned_on"}]
        flux["within"] = [{"emit": _fleet.VISIT, "relationship": "vehicle", "date": "visited_on"}]
    if notes:
        flux["lifetimes"].append({"emit": _fleet.VISIT, "start": "visited_on"})
        flux["within"].append({"emit": NOTE, "relationship": "visit", "date": "noted_on"})
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    _fleet.expose(contract, _fleet.VISIT)["semantics"]["relationships"][0]["to"] = _fleet.SNAPSHOT
    if notes:
        contract["exposes"].append(NOTE_EXPOSE)
    _fleet.repin(target, contract)
    return target


def _note_fidelity() -> Dict[str, Any]:
    """The statistics of ``visit_note``: 400 notes over the 600 visits, dated across the window."""
    columns = {
        "note_id": {"range": {"min": "1", "max": "400"}},
        "visit_id": {"range": {"min": "1", "max": "600"}},
        "noted_on": {"range": {"min": "2023-01-01", "max": "2025-12-31"}},
    }
    return {
        "rows": 400,
        "fidelity": {
            "encoding": {"fractionalBits": 16, "rounding": "floor"},
            "records": {"min": 1, "max": 1000000},
            "columns": columns,
            "properties": [
                {
                    "property": "mean",
                    "column": "note_id",
                    "tolerance": {"type": "absolute", "value": "1000"},
                }
            ],
            "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
            "failureAction": "withholdBundle",
        },
    }


def early_visits(layout: RunLayout) -> List[Dict[str, Any]]:
    """The visits dated before the commissioning the snapshot writes for their vehicle."""
    commissioned = {
        row["vehicle_id"]: row["commissioned_on"] for row in _fleet.rows(layout, _fleet.SNAPSHOT)
    }
    visits = _fleet.rows(layout, _fleet.VISIT)
    assert visits
    return [row for row in visits if row["visited_on"] < commissioned[row["vehicle_id"]]]


def test_a_date_within_a_snapshots_lifetime_holds_for_every_row(tmp_path: Path) -> None:
    target = linked_bundle(tmp_path)
    state = tmp_path / "state"
    layout = _fleet.run_bundle(target, state)
    assert early_visits(layout) == []
    assert load_recorded_plan(layout)[KEY] == "final"
    recorded = layout.emit_path(_fleet.VISIT).read_bytes()
    digest = recorded_digest(state, layout.run_id)
    replayed = replay(target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=4)
    assert replayed.success, replayed.stderr
    assert layout.emit_path(_fleet.VISIT).read_bytes() == recorded


def test_a_date_within_a_lifetime_that_reads_a_snapshot_holds_through_the_chain(
    tmp_path: Path,
) -> None:
    layout = _fleet.run_bundle(linked_bundle(tmp_path, notes=True), tmp_path / "state")
    visits = {row["visit_id"]: row for row in _fleet.rows(layout, _fleet.VISIT)}
    commissioned = {
        row["vehicle_id"]: row["commissioned_on"] for row in _fleet.rows(layout, _fleet.SNAPSHOT)
    }
    notes = _fleet.rows(layout, NOTE)
    assert notes
    assert [row for row in notes if row["noted_on"] < visits[row["visit_id"]]["visited_on"]] == []
    assert [
        row
        for row in notes
        if row["noted_on"] < commissioned[visits[row["visit_id"]]["vehicle_id"]]
    ] == []
    assert early_visits(layout) == []


def test_a_date_within_a_snapshots_lifetime_is_verified_for_every_row(tmp_path: Path) -> None:
    from flux_engine.stages.verify import verify

    state = tmp_path / "state"
    layout = _fleet.run_bundle(linked_bundle(tmp_path), state)
    verified = verify(tmp_path / "bundle", run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stderr
    lines = {line["rule"]: line for line in verified.artifacts["rules"]["emits"][_fleet.VISIT]}
    line = lines["within:vehicle.visited_on"]
    assert line["enforced"] and line["total"] > 0 and line["satisfied"] == line["total"], line


def test_the_run_is_the_same_at_one_and_four_jobs_and_in_shares(tmp_path: Path) -> None:
    target = linked_bundle(tmp_path, notes=True)
    assert_same_run_at_one_and_four_jobs(target)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, [_fleet.VISIT, NOTE])


def test_a_plan_recorded_before_the_link_read_the_projection_replays_its_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = linked_bundle(tmp_path)
    state = tmp_path / "state"
    # As engine 0.12.1 made it: the link read the rows the snapshot would draw unprojected.
    layout, digest = run_as_before(
        target,
        state,
        monkeypatch,
        "_planned_with_projections",
        era=pipeline.PlanEra(projected_reads=False),
    )
    assert KEY not in load_recorded_plan(layout)
    recorded = layout.emit_path(_fleet.VISIT).read_bytes()
    assert early_visits(layout)
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        assert layout.emit_path(_fleet.VISIT).read_bytes() == recorded


def test_a_plan_with_no_link_read_into_a_projection_writes_no_key(tmp_path: Path) -> None:
    for name, target in (
        ("plain", _fleet.bundle(tmp_path, name="plain")),
        ("unread", linked_bundle(tmp_path / "unread", within=False)),
    ):
        layout = _fleet.run_bundle(target, tmp_path / name)
        assert KEY not in load_recorded_plan(layout)


def test_a_plan_with_a_value_this_engine_does_not_know_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    layout = _fleet.run_bundle(linked_bundle(tmp_path), state)
    forged = {**load_recorded_plan(layout), KEY: "drawn"}
    forged["planDigest"] = build_plan_digest(forged).ref.value
    moved = RunLayout.for_state_dir(state, run_id_for(forged["planDigest"]))
    moved.run_dir.mkdir(parents=True)
    moved.plan_path.write_text(json.dumps(forged, indent=2, sort_keys=True), "utf-8")
    with pytest.raises(FluxError, match=f"FLX-2102.*{KEY}"):
        load_recorded_plan(moved)
