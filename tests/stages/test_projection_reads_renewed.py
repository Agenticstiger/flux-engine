# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Every plan that depends on a projected emit carries the projected plan, however it depends (ADR 0027).

``test_projection_links`` holds the plan that reads a projected emit through a link. Two more ways to
depend on one are held here, in the ``history-fleet`` fixture grown with a second history:

- a summary: a parent counts and takes the latest of columns a child derives from a snapshot it
  links to, and the child carries the snapshot's plan as the rows are written;
- a history that reads a snapshot of another history through a link: the snapshot, events and
  periods projected from it are derived from the history's renewed plan, and so is the plan of a
  table that links to its snapshot.

A recorded plan without ``projectedReads`` replays all of it as it was made.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.stages import pipeline
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.replay import replay
from flux_engine.stages.verify import verify

from . import _fleet
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_declared_range_replay import run_as_before

pytestmark = pytest.mark.integration

KEY = "projectedReads"
GARAGE = "garage"
SITE_HISTORY = "site_version"
SITE = "site_current"
ZONES = {"north": "0.4", "south": "0.35", "west": "0.25"}
MODELS = {"sedan": "0.5", "van": "0.3", "truck": "0.2"}
DATES = {"range": {"min": "2023-01-01", "max": "2025-12-31"}}
SHAPES = ("summed", "site", "chained")
EMITS = {
    "summed": [_fleet.VISIT, GARAGE],
    "site": [_fleet.HISTORY, _fleet.SNAPSHOT, _fleet.EVENTS, _fleet.PERIODIC],
    "chained": [_fleet.VISIT, _fleet.HISTORY, _fleet.SNAPSHOT, _fleet.PERIODIC],
}


def _column(name: str, kind: str, required: bool = True) -> Dict[str, Any]:
    return {"name": name, "type": kind, "required": required}


def _relationship(name: str, to: str, column: str) -> Dict[str, Any]:
    return {
        "name": name,
        "to": to,
        "fromColumns": [column],
        "toColumns": [column],
        "cardinality": "many_to_one",
        "joinType": "inner",
    }


def _fidelity(rows: Optional[int], columns: Dict[str, Any], mean: str) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "fidelity": {
            "encoding": {"fractionalBits": 16, "rounding": "floor"},
            "records": {"min": 1, "max": 1000000},
            "columns": columns,
            "properties": [
                {
                    "property": "mean",
                    "column": mean,
                    "tolerance": {"type": "absolute", "value": "1000000000"},
                }
            ],
            "budget": {"maxChecksPerState": 20, "maxStateAge": "P30D"},
            "failureAction": "withholdBundle",
        }
    }
    if rows is not None:
        document["rows"] = rows
    return document


def _add_emit(
    document: Dict[str, Any],
    contract: Dict[str, Any],
    expose_id: str,
    schema: List[Dict[str, Any]],
    relationships: List[Dict[str, Any]],
    key: str,
    statistics: Dict[str, Any],
) -> Dict[str, Any]:
    document["spec"]["emits"].append(
        {
            "productRef": _fleet.PRODUCT,
            "exposeId": expose_id,
            "fluidVersion": "0.7.7",
            "provenance": {"contractDigest": "sha256:" + "0" * 64},
        }
    )
    document["extensions"][_fleet.NAMESPACE]["emits"][expose_id] = statistics
    contract["exposes"].append(
        {
            "exposeId": expose_id,
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "glue_table",
                "location": {"database": "fleet", "table": expose_id, "region": "eu-west-1"},
            },
            "contract": {"schema": schema},
            "semantics": {
                "entities": [{"name": expose_id, "type": "primary", "expr": key}],
                "relationships": relationships,
            },
        }
    )
    return contract["exposes"][-1]


def _visits_read_the_snapshot(
    contract: Dict[str, Any], flux: Dict[str, Any], derived: Dict[str, str]
) -> None:
    """Link every visit to the snapshot, and derive the columns *derived* names from its row."""
    visit = _fleet.expose(contract, _fleet.VISIT)
    visit["semantics"]["relationships"][0]["to"] = _fleet.SNAPSHOT
    columns = flux["emits"][_fleet.VISIT]["fidelity"]["columns"]
    for name, expression in derived.items():
        visit["contract"]["schema"].append(_column(name, "DATE" if name == "vcomm" else "STRING"))
        visit["contract"].setdefault("invariants", []).append(
            {"id": f"d_{name}", "derive": name, "expr": expression}
        )
        columns[name] = (
            DATES if name == "vcomm" else {"levels": MODELS if name == "vmodel" else ZONES}
        )


def _summed(document: Dict[str, Any], contract: Dict[str, Any]) -> None:
    """A garage that counts its trucks and takes the latest commissioning of its visits' vehicles."""
    flux = document["extensions"][_fleet.NAMESPACE]
    _visits_read_the_snapshot(
        contract,
        flux,
        {"vmodel": "vehicle.model", "vcomm": "vehicle.commissioned_on"},
    )
    visit = _fleet.expose(contract, _fleet.VISIT)
    visit["contract"]["schema"].append(_column("garage_id", "INT"))
    visit["semantics"]["relationships"].append(_relationship("garage", GARAGE, "garage_id"))
    garage = _add_emit(
        document,
        contract,
        GARAGE,
        [
            _column("garage_id", "INT"),
            _column("n_trucks", "INT"),
            _column("last_comm", "DATE", False),
        ],
        [],
        "garage_id",
        _fidelity(
            200,
            {
                "garage_id": {"range": {"min": "1", "max": "200"}},
                "n_trucks": {"range": {"min": "0", "max": "10"}},
                "last_comm": DATES,
            },
            "garage_id",
        ),
    )
    garage["contract"]["invariants"] = [
        {
            "id": "trucks",
            "derive": "n_trucks",
            "aggregate": {
                "function": "count",
                "from": _fleet.VISIT,
                "via": "garage",
                "filter": "vmodel = 'truck'",
            },
        },
        {
            "id": "latest",
            "derive": "last_comm",
            "aggregate": {
                "function": "max",
                "column": "vcomm",
                "from": _fleet.VISIT,
                "via": "garage",
            },
        },
    ]
    flux["counts"] = [
        {
            "emit": _fleet.VISIT,
            "relationship": "garage",
            "children": {"levels": ["3"], "levelWeights": ["1"]},
        }
    ]


def _site(document: Dict[str, Any], contract: Dict[str, Any]) -> None:
    """A history of sites with a snapshot, which the vehicle history reads through a link."""
    flux = document["extensions"][_fleet.NAMESPACE]
    site_columns = {
        "site_sk": {"range": {"min": "1", "max": "200"}},
        "site_id": {"range": {"min": "1", "max": "200"}},
        "valid_from": DATES,
        "valid_to": DATES,
        "zone": {"levels": ZONES},
    }
    history = _add_emit(
        document,
        contract,
        SITE_HISTORY,
        [
            _column("site_sk", "INT"),
            _column("site_id", "INT"),
            _column("depot_id", "INT"),
            _column("valid_from", "DATE"),
            _column("valid_to", "DATE", False),
            _column("zone", "STRING"),
        ],
        [_relationship("depot", _fleet.DEPOT, "depot_id")],
        "site_sk",
        _fidelity(200, site_columns, "site_sk"),
    )
    history["semantics"]["entities"].append({"name": "site", "type": "natural", "expr": "site_id"})
    history["semantics"]["dimensions"] = [
        {"name": "valid_from", "type": "time", "typeParams": {"validityParams": {"isStart": True}}},
        {"name": "valid_to", "type": "time", "typeParams": {"validityParams": {"isEnd": True}}},
    ]
    snapshot_statistics = _fidelity(
        None,
        {"site_id": {"range": {"min": "1", "max": "200"}}, "zone": {"levels": ZONES}},
        "site_id",
    )
    snapshot = _add_emit(
        document,
        contract,
        SITE,
        [_column("site_id", "INT"), _column("zone", "STRING")],
        [],
        "site_id",
        snapshot_statistics,
    )
    snapshot["semantics"]["entities"] = [{"name": "site", "type": "primary", "expr": "site_id"}]
    entry = dict(flux["history"][0])
    entry.update(
        {
            "emit": SITE_HISTORY,
            "entities": 60,
            "parent": {"relationship": "depot", "minChildren": 0},
            "tracked": [
                {
                    "column": "zone",
                    "transitions": {
                        "north": {"south": "1"},
                        "south": {"west": "1"},
                        "west": {"north": "1"},
                    },
                }
            ],
        }
    )
    flux["history"].append(entry)
    flux["snapshots"].append({"emit": SITE, "of": SITE_HISTORY})
    # A vehicle keeps one version, so each version reads the site row its entity's snapshot does.
    flux["history"][0]["versions"] = {"bins": ["1", "2"], "binWeights": ["1"]}
    vehicles = _fleet.expose(contract, _fleet.HISTORY)
    vehicles["contract"]["schema"] += [_column("site_id", "INT"), _column("szone", "STRING")]
    vehicles["semantics"]["relationships"].append(_relationship("site", SITE, "site_id"))
    vehicles["contract"]["invariants"] = [{"id": "zone", "derive": "szone", "expr": "site.zone"}]
    flux["emits"][_fleet.HISTORY]["fidelity"]["columns"]["site_id"] = {
        "range": {"min": "1", "max": "200"}
    }
    for expose_id in (_fleet.HISTORY, _fleet.SNAPSHOT, _fleet.PERIODIC):
        flux["emits"][expose_id]["fidelity"]["columns"]["szone"] = {"levels": ZONES}
    for expose_id in (_fleet.SNAPSHOT, _fleet.PERIODIC):
        _fleet.expose(contract, expose_id)["contract"]["schema"].append(_column("szone", "STRING"))


def shaped_bundle(tmp_path: Path, shape: str) -> Path:
    """The fleet bundle grown into one of :data:`SHAPES`."""
    target = _fleet.bundle(tmp_path, name=shape, versions=60 if shape != "summed" else 200)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    if shape == "summed":
        _summed(document, contract)
    else:
        _site(document, contract)
    if shape == "chained":
        _visits_read_the_snapshot(
            contract, document["extensions"][_fleet.NAMESPACE], {"vzone": "vehicle.szone"}
        )
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    _fleet.repin(target, contract)
    return target


def garages_that_miss_their_visits(layout: Any) -> int:
    """The garages whose summary is not what their written visits give."""
    trucks: Dict[Any, int] = defaultdict(int)
    latest: Dict[Any, Any] = {}
    for visit in _fleet.rows(layout, _fleet.VISIT):
        garage = visit["garage_id"]
        trucks[garage] += visit["vmodel"] == "truck"
        latest[garage] = max(latest.get(garage, visit["vcomm"]), visit["vcomm"])
    garages = _fleet.rows(layout, GARAGE)
    assert garages and latest
    return sum(
        1
        for row in garages
        if row["n_trucks"] != trucks[row["garage_id"]]
        or row["last_comm"] != latest.get(row["garage_id"])
    )


def visits_that_miss_their_snapshot(layout: Any, column: str = "vzone", read: str = "szone") -> int:
    """The visits whose *column* is not the *read* cell of the vehicle's snapshot row."""
    snapshot = {row["vehicle_id"]: row for row in _fleet.rows(layout, _fleet.SNAPSHOT)}
    visits = _fleet.rows(layout, _fleet.VISIT)
    assert visits
    return sum(1 for row in visits if row[column] != snapshot[row["vehicle_id"]][read])


def projections_that_miss_their_history(layout: Any) -> int:
    """The snapshot rows that are not their entity's last version, and the periods that name no version's zone."""
    versions = _fleet.by_vehicle(_fleet.rows(layout, _fleet.HISTORY))
    last = {
        vehicle: max(rows, key=lambda row: row["valid_from"]) for vehicle, rows in versions.items()
    }
    snapshot = _fleet.rows(layout, _fleet.SNAPSHOT)
    assert snapshot
    return sum(1 for row in snapshot if row["szone"] != last[row["vehicle_id"]]["szone"])


def rule_lines(state: Path, layout: Any, target: Path, expose_id: str) -> Dict[str, Any]:
    verified = verify(target, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stderr
    return {line["rule"]: line for line in verified.artifacts["rules"]["emits"][expose_id]}


def assert_whole(lines: Dict[str, Any], *rules: str) -> None:
    for rule in rules:
        line = lines[rule]
        assert line["enforced"] and line["total"] > 0, line
        assert line["satisfied"] == line["total"], line


def test_a_summary_over_a_child_that_reads_a_snapshot_is_what_its_written_children_give(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    target = shaped_bundle(tmp_path, "summed")
    layout = _fleet.run_bundle(target, state)
    assert load_recorded_plan(layout)[KEY] == "final"
    assert garages_that_miss_their_visits(layout) == 0
    assert_whole(rule_lines(state, layout, target, GARAGE), "trucks", "latest")


def test_a_history_that_reads_a_snapshot_keeps_its_projections_equal_to_it(tmp_path: Path) -> None:
    state = tmp_path / "state"
    target = shaped_bundle(tmp_path, "site")
    layout = _fleet.run_bundle(target, state)
    assert load_recorded_plan(layout)[KEY] == "final"
    assert projections_that_miss_their_history(layout) == 0
    assert_whole(rule_lines(state, layout, target, _fleet.SNAPSHOT), "snapshot:equals_last_version")
    assert_whole(rule_lines(state, layout, target, _fleet.PERIODIC), "period:matches_valid_version")
    assert_whole(rule_lines(state, layout, target, _fleet.EVENTS), "event:matches_transition")


def test_a_table_that_links_to_a_snapshot_of_a_history_that_reads_a_snapshot_reads_its_rows(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    target = shaped_bundle(tmp_path, "chained")
    layout = _fleet.run_bundle(target, state)
    assert visits_that_miss_their_snapshot(layout) == 0
    assert projections_that_miss_their_history(layout) == 0
    assert_whole(rule_lines(state, layout, target, _fleet.VISIT), "d_vzone")


@pytest.mark.parametrize("shape", SHAPES)
def test_the_run_is_the_same_at_one_and_four_jobs_and_in_shares(tmp_path: Path, shape: str) -> None:
    target = shaped_bundle(tmp_path, shape)
    assert_same_run_at_one_and_four_jobs(target)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, EMITS[shape])


@pytest.mark.parametrize("shape", SHAPES)
def test_a_plan_recorded_before_the_renewal_replays_its_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    target = shaped_bundle(tmp_path, shape)
    state = tmp_path / "state"
    # As engine 0.12.1 made it: every plan carried the plans as they were before any projection.
    layout, digest = run_as_before(
        target,
        state,
        monkeypatch,
        "_planned_with_projections",
        era=pipeline.PlanEra(projected_reads=False),
    )
    assert KEY not in load_recorded_plan(layout)
    recorded = {name: layout.emit_path(name).read_bytes() for name in EMITS[shape]}
    # What was recorded is the link as it was made: a visit's cells are not the snapshot's row.
    if shape == "summed":
        assert visits_that_miss_their_snapshot(layout, "vmodel", "model")
    if shape == "chained":
        assert visits_that_miss_their_snapshot(layout)
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        for name, content in recorded.items():
            assert layout.emit_path(name).read_bytes() == content, name


def test_a_plan_that_sums_a_projected_emit_says_it_reads_one() -> None:
    from types import SimpleNamespace

    from flux_engine.stages.plan import _reads_projection

    projected = SimpleNamespace(projection=object())
    plain = SimpleNamespace(projection=None)
    assert _reads_projection(SimpleNamespace(reads=(), sums=(("child", "via", projected),)))
    assert _reads_projection(SimpleNamespace(reads=(("target", projected),), sums=()))
    assert not _reads_projection(
        SimpleNamespace(reads=(("target", plain),), sums=(("child", "via", plain),))
    )
