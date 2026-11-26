# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history that reads, through a link, the snapshot projected from it (ADR 0027).

A self-referencing table is the shape: a vehicle version links to the current row of another
vehicle (``peer``) and derives a column from it, as an employee version links to the current row
of its manager. The snapshot copies the history's columns, so the history reads its own
projection. It plans when the columns it reads through the link do not depend on the link, and is
refused when one does, since the engine does not derive a column more than one step away, through
such a link, from a drawn one (the amendment of 2026-11-26).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from flux_engine.stages import pipeline
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.replay import replay
from flux_engine.stages.verify import verify

from . import _fleet
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_declared_range_replay import run_as_before
from .test_projection_reads_renewed import ZONES, assert_whole, rule_lines, shaped_bundle

pytestmark = pytest.mark.integration

KEY = "projectedReads"
EMITS = [_fleet.HISTORY, _fleet.SNAPSHOT, _fleet.EVENTS, _fleet.PERIODIC]
DATES = {"range": {"min": "2023-01-01", "max": "2025-12-31"}}
STATUSES = {"levels": {"active": "0.8", "retired": "0.2"}}
#: The shapes of the self-reference: the column the history reads of its peer, its type and its
#: statistics, and whether each entity keeps one version or several.
SHAPES: Dict[str, Tuple[str, str, Dict[str, Any], bool]] = {
    "date": ("commissioned_on", "DATE", DATES, True),
    "status": ("status", "STRING", STATUSES, True),
    "multi": ("commissioned_on", "DATE", DATES, False),
}


def _column(name: str, kind: str) -> Dict[str, Any]:
    return {"name": name, "type": kind, "required": True}


def _peer() -> Dict[str, Any]:
    return {
        "name": "peer",
        "to": _fleet.SNAPSHOT,
        "fromColumns": ["peer_id"],
        "toColumns": ["vehicle_id"],
        "cardinality": "many_to_one",
        "joinType": "inner",
    }


def peering_bundle(
    tmp_path: Path,
    shape: str,
    *,
    read: str = "",
    copied: Tuple[str, ...] = (),
    extra: Tuple[Tuple[str, str, str, Dict[str, Any]], ...] = (),
    crossed: bool = False,
) -> Path:
    """The fleet with each version linked to a vehicle, and ``peer_x`` derived from that vehicle.

    ``read`` is the text ``peer_x`` is derived from (the shape's column of the peer by default).
    ``copied`` names the columns of the history the snapshot holds as well. ``extra`` adds columns
    to the history, each ``(name, type, derive text, statistics)``. ``crossed`` grows the fleet with
    a second history first, whose snapshot the vehicle history reads (``szone``) as well, and the
    column read of the peer is that one.
    """
    column, kind, statistics, single = SHAPES[shape]
    if crossed:
        column, kind, statistics, single = "szone", "STRING", {"levels": ZONES}, True
        target = shaped_bundle(tmp_path, "site")
    else:
        target = _fleet.bundle(tmp_path, name=shape)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    flux = document["extensions"][_fleet.NAMESPACE]
    if single:
        flux["history"][0]["versions"] = {"bins": ["1", "2"], "binWeights": ["1"]}
        flux["emits"][_fleet.HISTORY]["rows"] = 60
        declared = flux["emits"][_fleet.HISTORY]["fidelity"]["columns"]
        declared["vehicle_sk"]["range"]["max"] = "60"
    history = _fleet.expose(contract, _fleet.HISTORY)
    history["contract"]["schema"] += [_column("peer_id", "INT"), _column("peer_x", kind)]
    history["semantics"]["relationships"].append(_peer())
    derived = [("peer_x", kind, read or f"peer.{column}", statistics), *extra]
    columns = flux["emits"][_fleet.HISTORY]["fidelity"]["columns"]
    columns["peer_id"] = {"range": {"min": "1", "max": "200"}}
    history["contract"]["invariants"] = history["contract"].get("invariants", []) if crossed else []
    for name, held, text, stats in derived:
        if name != "peer_x":
            history["contract"]["schema"].append(_column(name, held))
        history["contract"]["invariants"].append({"id": f"d_{name}", "derive": name, "expr": text})
        columns[name] = stats
    snapshot = _fleet.expose(contract, _fleet.SNAPSHOT)
    for name in copied:
        held, stats = next((kind_, stats_) for n, kind_, _, stats_ in derived if n == name)
        snapshot["contract"]["schema"].append(_column(name, held))
        flux["emits"][_fleet.SNAPSHOT]["fidelity"]["columns"][name] = stats
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    _fleet.repin(target, contract)
    return target


def peers_that_miss_their_snapshot(
    layout: Any, column: str, read: str, *, first_only: bool = False
) -> int:
    """The versions whose *column* is not the *read* cell of the snapshot row their peer names.

    A version after the first of its entity reads the row another version's pick names, with a
    link to any table (the same run with the history linked to ``depot`` does), so ``first_only``
    holds the first version of each entity to the cell and leaves the rest.
    """
    snapshot = {row["vehicle_id"]: row for row in _fleet.rows(layout, _fleet.SNAPSHOT)}
    versions = _fleet.rows(layout, _fleet.HISTORY)
    assert versions
    if first_only:
        firsts: Dict[Any, Dict[str, Any]] = {}
        for row in versions:
            held = firsts.get(row["vehicle_id"])
            firsts[row["vehicle_id"]] = (
                row if held is None or row["valid_from"] < held["valid_from"] else held
            )
        versions = list(firsts.values())
    return sum(1 for row in versions if row[column] != snapshot[row["peer_id"]][read])


def snapshots_that_miss_their_history(layout: Any, columns: List[str]) -> int:
    """The snapshot rows whose *columns* are not those of their entity's last version."""
    versions = _fleet.by_vehicle(_fleet.rows(layout, _fleet.HISTORY))
    last = {key: max(rows, key=lambda row: row["valid_from"]) for key, rows in versions.items()}
    snapshot = _fleet.rows(layout, _fleet.SNAPSHOT)
    assert snapshot
    return sum(
        1 for row in snapshot if any(row[name] != last[row["vehicle_id"]][name] for name in columns)
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_a_history_that_links_to_its_own_snapshot_derives_from_the_rows_it_writes(
    tmp_path: Path, shape: str
) -> None:
    column = SHAPES[shape][0]
    state = tmp_path / "state"
    target = peering_bundle(tmp_path, shape)
    layout = _fleet.run_bundle(target, state)
    assert load_recorded_plan(layout)[KEY] == "final"
    single = SHAPES[shape][3]
    assert peers_that_miss_their_snapshot(layout, "peer_x", column, first_only=not single) == 0
    assert snapshots_that_miss_their_history(layout, [column]) == 0
    verified = verify(target, run_id=layout.run_id, state_dir=state)
    # A version after the first reads another pick's row, with a link to any table: the derive
    # is whole only where an entity keeps one version.
    assert verified.success == single, verified.stderr
    lines = {
        expose_id: {line["rule"]: line for line in found}
        for expose_id, found in verified.artifacts["rules"]["emits"].items()
    }
    if single:
        assert_whole(lines[_fleet.HISTORY], "d_peer_x")
    assert_whole(lines[_fleet.SNAPSHOT], "snapshot:equals_last_version")
    assert_whole(lines[_fleet.PERIODIC], "period:matches_valid_version")


#: The columns the snapshot copies beside ``szone``, which the vehicle history reads of another
#: history's snapshot: the one it reads of its own peer, or none.
CROSSED = {"copied": ("peer_x",), "not_copied": ()}


@pytest.mark.parametrize("name", CROSSED)
def test_a_history_that_reads_a_snapshot_of_another_history_settles_what_it_reads_of_its_peer(
    tmp_path: Path, name: str
) -> None:
    copied = CROSSED[name]
    state = tmp_path / "state"
    target = peering_bundle(tmp_path, "date", copied=copied, crossed=True)
    layout = _fleet.run_bundle(target, state)
    assert load_recorded_plan(layout)[KEY] == "final"
    assert peers_that_miss_their_snapshot(layout, "peer_x", "szone") == 0
    assert snapshots_that_miss_their_history(layout, ["szone", *copied]) == 0
    verified = verify(target, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stderr
    lines = {
        expose_id: {line["rule"]: line for line in found}
        for expose_id, found in verified.artifacts["rules"]["emits"].items()
    }
    assert_whole(lines[_fleet.HISTORY], "d_peer_x")
    assert_whole(lines[_fleet.SNAPSHOT], "snapshot:equals_last_version")


@pytest.mark.parametrize("name", CROSSED)
def test_the_run_that_reads_two_snapshots_is_the_same_at_one_and_four_jobs_and_in_shares(
    tmp_path: Path, name: str
) -> None:
    target = peering_bundle(tmp_path, "date", copied=CROSSED[name], crossed=True)
    assert_same_run_at_one_and_four_jobs(target)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, EMITS)


@pytest.mark.parametrize("name", CROSSED)
def test_a_plan_recorded_before_the_renewal_replays_when_it_reads_two_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    target = peering_bundle(tmp_path, "date", copied=CROSSED[name], crossed=True)
    state = tmp_path / "state"
    layout, digest = run_as_before(
        target,
        state,
        monkeypatch,
        "_planned_with_projections",
        era=pipeline.PlanEra(projected_reads=False),
    )
    assert KEY not in load_recorded_plan(layout)
    recorded = {name_: layout.emit_path(name_).read_bytes() for name_ in EMITS}
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        for emit, content in recorded.items():
            assert layout.emit_path(emit).read_bytes() == content, emit


def test_a_column_that_depends_on_the_link_may_be_copied_when_it_is_not_read_through_it(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    target = peering_bundle(tmp_path, "date", copied=("peer_x",))
    layout = _fleet.run_bundle(target, state)
    assert peers_that_miss_their_snapshot(layout, "peer_x", "commissioned_on") == 0
    assert snapshots_that_miss_their_history(layout, ["commissioned_on", "peer_x"]) == 0
    assert_whole(rule_lines(state, layout, target, _fleet.SNAPSHOT), "snapshot:equals_last_version")


def test_every_column_read_through_the_link_settles_with_one_the_snapshot_copies(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    target = peering_bundle(
        tmp_path,
        "date",
        copied=("peer_x",),
        extra=(("peer_y", "DATE", "peer.commissioned_on", DATES),),
    )
    layout = _fleet.run_bundle(target, state)
    assert peers_that_miss_their_snapshot(layout, "peer_y", "commissioned_on") == 0
    assert peers_that_miss_their_snapshot(layout, "peer_x", "commissioned_on") == 0
    assert snapshots_that_miss_their_history(layout, ["commissioned_on", "peer_x"]) == 0


@pytest.mark.parametrize(
    ("read", "copied", "extra"),
    [
        # The column read through the link is set from the link.
        ("peer.peer_x", ("peer_x",), ()),
        # It is computed from a column that is.
        (
            "peer.peer_y",
            ("peer_y",),
            (("peer_y", "DATE", "peer.commissioned_on", DATES),),
        ),
    ],
)
def test_a_history_that_reads_a_column_that_depends_on_the_link_is_refused(
    tmp_path: Path, read: str, copied: Tuple[str, ...], extra: Tuple[Any, ...]
) -> None:
    target = peering_bundle(tmp_path, "date", read=read, copied=copied, extra=extra)
    refused = _fleet.plan_refused(target, tmp_path / "state")
    assert refused.error_code == "FLX-3403"
    message = " ".join(refused.stderr.split())
    assert f"{_fleet.HISTORY!r} reads {_fleet.SNAPSHOT}.{copied[0]} through 'peer'" in message
    assert "depends on that link" in message
    assert "still changing" not in message


@pytest.mark.parametrize("shape", SHAPES)
def test_the_run_is_the_same_at_one_and_four_jobs_and_in_shares(tmp_path: Path, shape: str) -> None:
    target = peering_bundle(tmp_path, shape)
    assert_same_run_at_one_and_four_jobs(target)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, EMITS)


@pytest.mark.parametrize("shape", SHAPES)
def test_a_plan_recorded_before_the_renewal_replays_its_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    target = peering_bundle(tmp_path, shape)
    state = tmp_path / "state"
    layout, digest = run_as_before(
        target,
        state,
        monkeypatch,
        "_planned_with_projections",
        era=pipeline.PlanEra(projected_reads=False),
    )
    assert KEY not in load_recorded_plan(layout)
    recorded = {name: layout.emit_path(name).read_bytes() for name in EMITS}
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        for name, content in recorded.items():
            assert layout.emit_path(name).read_bytes() == content, name
