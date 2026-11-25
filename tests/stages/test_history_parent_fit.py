# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history never writes a version outside its parent's lifetime (ADR 0017, amendment of 2026-11-24).

The ``history-fleet`` fixture gets short depot lifetimes, so that some vehicles have less room
than the versions they were given. What is pinned, through plan, apply and verify:

* a vehicle whose depot leaves it room for fewer versions is capped at that room, and the versions
  it loses move to vehicles with room to spare, so the declared rows hold;
* every vehicle keeps to its depot, and ``history:within_parent`` holds on every one of them;
* a depot that leaves no room for even one version is refused at plan (``FLX-3403``);
* when no vehicle has room left the history has fewer rows than declared, and plan, apply and
  verify agree on the count;
* ``--jobs 1`` and ``--jobs 4``, and shares joined, make the same run.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify

from ._fleet import (
    DEPOT,
    EVENTS,
    NAMESPACE,
    SNAPSHOT,
    by_vehicle,
    plan_module,
    plan_refused,
    run_bundle,
)
from ._fleet import HISTORY as EMIT
from ._fleet import bundle as fleet_bundle
from ._fleet import rows as read_rows
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run

pytestmark = pytest.mark.integration

#: Depots that open on one of two days and, four fifths of them, never close; the others close within a
#: few days, which leaves their vehicles room for fewer versions than they are given.
OPENED = {"range": {"min": "2024-03-01", "max": "2024-03-02"}}
SHORT = {"nullFraction": "0.8", "range": {"min": "2024-03-05", "max": "2024-03-12"}}
#: With no closing unit to keep, a depot can close a day earlier and still leave room for a version.
SHORT_FINAL = {"nullFraction": "0.5", "range": {"min": "2024-03-04", "max": "2024-03-07"}}
FINAL = {"mode": "finalVersion", "share": "0.1", "column": "status", "value": "retired"}


def _bundle(
    tmp_path: Path,
    *,
    closed: Dict[str, Any],
    opened: Optional[Dict[str, Any]] = None,
    versions: int = 200,
    entities: int = 60,
    depots: int = 50,
    end: Optional[Dict[str, Any]] = None,
) -> Path:
    """The fixture with depot lifetimes, and the same number of versions for every vehicle."""
    target = fleet_bundle(
        tmp_path,
        versions=versions,
        entities=entities,
        depots=depots,
        end=end,
        columns={"status": {"levels": {"active": "0.7", "maintenance": "0.3", "retired": "0"}}},
    )
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][NAMESPACE]
    flux["history"][0]["parent"]["lifetime"] = True
    del flux["history"][0]["versions"]
    columns = flux["emits"][DEPOT]["fidelity"]["columns"]
    columns["opened_on"] = opened or OPENED
    columns["closed_on"] = closed
    flux["lifetimes"] = [{"emit": DEPOT, "start": "opened_on", "end": "closed_on"}]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _day(value: Optional[str]) -> Optional[date]:
    return None if value is None else date.fromisoformat(value)


def _outside(layout: RunLayout) -> int:
    """How many vehicles leave their depot's lifetime: the first start, a start or an end after its last day."""
    depots = {row["depot_id"]: row for row in read_rows(layout, DEPOT)}
    found = 0
    for versions in by_vehicle(read_rows(layout, EMIT)).values():
        parent = depots[versions[0]["depot_id"]]
        opened, closed = _day(parent["opened_on"]), _day(parent["closed_on"])
        if closed is not None and closed < opened:  # type: ignore[operator]
            closed = None
        held = _day(versions[0]["valid_from"]) >= opened  # type: ignore[operator]
        if closed is not None:
            held &= all(_day(row["valid_from"]) <= closed for row in versions)
            held &= all(
                row["valid_to"] is None or _day(row["valid_to"]) <= closed for row in versions
            )
        found += not held
    return found


def _counts(layout: RunLayout) -> List[int]:
    return [len(versions) for versions in by_vehicle(read_rows(layout, EMIT)).values()]


def _verified(bundle: Path, layout: RunLayout, state: Path) -> str:
    verified = verify(bundle, run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stdout + verified.stderr
    return (verified.stdout + verified.stderr).replace("\n", " ")


@pytest.mark.parametrize("end", [None, {"mode": "closeLast", "share": "0.1"}, FINAL])
def test_a_vehicle_with_less_room_than_versions_is_capped_and_the_versions_move(
    tmp_path: Path, end: Optional[Dict[str, Any]]
) -> None:
    bundle = _bundle(tmp_path, closed=SHORT_FINAL if end is FINAL else SHORT, end=end)
    layout = run_bundle(bundle, tmp_path / "state")
    counts = _counts(layout)
    # Without a shape every vehicle is given three or four versions: some end with fewer, and the
    # versions they lost are somebody else's, so the declared rows hold (and a vehicle with room
    # may end with more than four).
    assert min(counts) < 3 and max(counts) > 4 - 1
    assert sum(counts) == len(read_rows(layout, EMIT)) == 200
    assert _outside(layout) == 0
    assert "history:within_parent 60 of 60" in _verified(bundle, layout, tmp_path / "state")


def test_plan_says_how_many_entities_were_capped_and_how_many_versions_moved(
    tmp_path: Path,
) -> None:
    planned = plan_module.plan(
        _bundle(tmp_path, closed=SHORT),
        state_dir=tmp_path / "state",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
    assert f"history ({EMIT}): " in planned.stderr
    assert "versions moved to entities with room to spare" in planned.stderr


def test_a_depot_that_leaves_no_room_for_one_version_is_refused_at_plan(tmp_path: Path) -> None:
    # A depot open for a day or two: the first start is at the latest the third day before its last
    # day (the last version is closed a day before the last day), so a day or two is too few.
    closed = {"nullFraction": "0.5", "range": {"min": "2024-03-02", "max": "2024-03-03"}}
    planned = plan_refused(_bundle(tmp_path, closed=closed), tmp_path / "state")
    assert "FLX-3403" in planned.stderr
    assert f"history ({EMIT}).parent.lifetime" in planned.stderr
    assert "of 60 entities" in planned.stderr


def test_a_depot_with_room_for_exactly_one_version_is_accepted(tmp_path: Path) -> None:
    closed = {"range": {"min": "2024-03-05", "max": "2024-03-06"}}
    planned = plan_module.plan(
        _bundle(tmp_path, closed=closed),
        state_dir=tmp_path / "state",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr


def test_when_no_vehicle_has_room_left_the_rows_shrink_and_every_stage_agrees(
    tmp_path: Path,
) -> None:
    # Every depot lives three to five days: a vehicle takes at most three versions, so the 60
    # vehicles hold fewer than the 200 declared rows however they are shared out.
    closed = {"range": {"min": "2024-03-05", "max": "2024-03-06"}}
    bundle = _bundle(tmp_path, closed=closed)
    planned = plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr
    layout = run_bundle(bundle, tmp_path / "state2")
    counts = _counts(layout)
    assert 60 <= sum(counts) < 200 and max(counts) <= 3
    assert f"{sum(counts)} rows" in planned.stderr and "200 declared" in planned.stderr
    assert _outside(layout) == 0
    assert "history:within_parent 60 of 60" in _verified(bundle, layout, tmp_path / "state2")
    # The tables projected from the history read the rows it has: a snapshot row for each vehicle.
    assert len(read_rows(layout, SNAPSHOT)) == 60
    assert len(read_rows(layout, EVENTS)) > 0


def test_a_run_repeats_and_its_shares_join_to_the_run(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, closed=SHORT, end={"mode": "closeLast", "share": "0.1"})
    assert_shares_join_to_the_run(bundle, tmp_path, run_bundle, 3, (EMIT,))


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_make_the_run_one_job_makes(tmp_path: Path) -> None:
    versions = 3 * parallel.MIN_ROWS_PER_JOB + 11
    bundle = _bundle(
        tmp_path,
        closed={"nullFraction": "0.8", "range": {"min": "2024-03-08", "max": "2024-03-30"}},
        versions=versions,
        entities=2500,
        depots=1200,
        end=FINAL,
    )
    assert_same_run_at_one_and_four_jobs(bundle, result=True, kernel=KERNEL_V2)
