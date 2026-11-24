# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A kind's or an event's ``when`` reads the history's end column (ADR 0027, the amendment of 2026-11-24).

On an ending version the column holds ``end.value``; on any other version it is a drawn level, so a
``when`` that names it has an answer at plan time only where every level the column's statistics
can draw gives the same one. What is pinned, on ``tests/fixtures/history-fleet`` with an
ended history whose ``status`` draws ``active`` or ``maintenance``:

* **A determined read runs.** ``status <> 'retired'`` is false on every ending version and true
  on every other, whatever level was drawn, so no periodic row is about an ending version.
* **A drawn read is refused.** ``status = 'active'`` answers differently for the two levels:
  ``FLX-3403`` at plan, naming the kind and the column.
* **The same for events**, and for a read through ``previous.``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2

from ._fleet import EVENTS, HISTORY, NAMESPACE, PERIODIC, plan_module
from ._fleet import bundle as fleet_bundle
from ._fleet import rows as read_rows
from ._fleet import run as run_bundle

pytestmark = pytest.mark.integration

FINAL = {"mode": "finalVersion", "share": "0.5", "column": "status", "value": "retired"}


def _with_when(bundle: Path, section: str, when: str) -> Path:
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    entry = document["extensions"][NAMESPACE][section][0]
    if section == "periods":
        entry["kinds"] = [{"count": 1, "when": when}]
    else:
        entry["when"] = when
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return bundle


def _ending_starts(layout: Any) -> Dict[Any, str]:
    """The first day of the month each ended entity's ending version starts in."""
    last: Dict[Any, Dict[str, Any]] = {}
    for row in read_rows(layout, HISTORY):
        last[row["vehicle_id"]] = row
    return {
        key: str(row["valid_from"])[:7] + "-01"
        for key, row in last.items()
        if row["status"] == "retired"
    }


def _months_past_the_ending(layout: Any) -> List[Any]:
    ending = _ending_starts(layout)
    assert ending
    return [
        row
        for row in read_rows(layout, PERIODIC)
        if row["vehicle_id"] in ending and row["month"] > ending[row["vehicle_id"]]
    ]


def test_a_kind_when_that_tests_the_end_value_holds_no_pair_of_an_ending_version(
    tmp_path: Path,
) -> None:
    open_kind = run_bundle(fleet_bundle(tmp_path, name="all", end=FINAL), tmp_path / "all")
    # Without a when, an ending version is open to the window's end and holds later months.
    assert _months_past_the_ending(open_kind)
    bundle = _with_when(
        fleet_bundle(tmp_path, name="when", end=FINAL), "periods", "status <> 'retired'"
    )
    layout = run_bundle(bundle, tmp_path / "when")
    assert read_rows(layout, PERIODIC)
    assert not _months_past_the_ending(layout)


def test_a_kind_when_that_reads_a_drawn_level_is_refused_naming_the_kind_and_the_column(
    tmp_path: Path,
) -> None:
    bundle = _with_when(fleet_bundle(tmp_path, end=FINAL), "periods", "status = 'active'")
    planned = plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success
    assert planned.error_code == "FLX-3403"
    assert "kinds[0].when" in planned.stderr and "'status'" in planned.stderr
    assert "depends on a drawn value" in planned.stderr


def test_a_read_of_the_end_column_through_previous_follows_the_same_rule(tmp_path: Path) -> None:
    drawn = _with_when(
        fleet_bundle(tmp_path, name="d", end=FINAL), "periods", "previous.status = 'active'"
    )
    planned = plan_module.plan(
        drawn, state_dir=tmp_path / "d", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success and planned.error_code == "FLX-3403"
    settled = _with_when(
        fleet_bundle(tmp_path, name="s", end=FINAL),
        "periods",
        "previous.status IS NULL OR previous.status <> 'retired'",
    )
    assert run_bundle(settled, tmp_path / "s") is not None


def test_an_events_when_reads_the_end_column_by_the_same_rule(tmp_path: Path) -> None:
    drawn = _with_when(
        fleet_bundle(tmp_path, name="d", end=FINAL), "events", "status = 'maintenance'"
    )
    planned = plan_module.plan(
        drawn, state_dir=tmp_path / "d", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success and planned.error_code == "FLX-3403"
    assert "events (" in planned.stderr and "'status'" in planned.stderr
    settled = _with_when(
        fleet_bundle(tmp_path, name="s", end=FINAL), "events", "status <> 'retired'"
    )
    assert read_rows(run_bundle(settled, tmp_path / "s"), EVENTS) is not None
