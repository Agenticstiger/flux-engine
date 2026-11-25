# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history keeps to its parent's lifetime, through plan, apply and verify (ADR 0017, amendment of 2026-11-24).

The ``history-fleet`` fixture gets a ``lifetimes`` entry on its depot (``opened_on`` to
``closed_on``) and ``parent.lifetime: true`` on the history. What is pinned:

* every version starts inside its depot's lifetime and ends by the depot's last day, and an
  entity of a closed depot has ended;
* ``verify`` reports ``history:within_parent`` and it holds on every covered entity;
* ``--jobs 1`` and ``--jobs 4``, shards and a smaller ``--scale`` make the same run;
* a bundle that declares the key with no ``lifetimes`` entry on the parent is refused (``FLX-3403``).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify

from ._fleet import (
    DEPOT,
    NAMESPACE,
    UNTIL,
    apply_module,
    by_vehicle,
    plan_module,
    plan_refused,
    run_bundle,
)
from ._fleet import HISTORY as EMIT
from ._fleet import bundle as fleet_bundle
from ._fleet import rows as read_rows
from ._same_run import (
    assert_same_run_at_one_and_four_jobs,
    assert_shares_join_to_the_run,
    assert_the_run_repeats,
)

pytestmark = pytest.mark.integration


def _bundle(
    tmp_path: Path,
    *,
    versions: int = 200,
    entities: int = 60,
    depots: int = 50,
    end: Optional[Dict[str, Any]] = None,
    lifetimes: bool = True,
) -> Path:
    """The fixture with a depot lifetime (``opened_on`` to ``closed_on``) the history keeps to."""
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
    columns = flux["emits"][DEPOT]["fidelity"]["columns"]
    columns["opened_on"] = {"range": {"min": "2023-01-01", "max": "2023-05-31"}}
    columns["closed_on"] = {
        "nullFraction": "0.4",
        "range": {"min": "2023-06-01", "max": "2025-12-30"},
    }
    if lifetimes:
        flux["lifetimes"] = [{"emit": DEPOT, "start": "opened_on", "end": "closed_on"}]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _depots(layout: RunLayout) -> Dict[Any, Dict[str, Any]]:
    return {row["depot_id"]: row for row in read_rows(layout, "depot")}


def _day(value: Optional[str]) -> Optional[date]:
    return None if value is None else date.fromisoformat(value)


def _check_bound(layout: RunLayout, mode: Optional[str]) -> int:
    """Every entity keeps to its depot's lifetime; returns how many depots' entities ended."""
    depots = _depots(layout)
    ended = 0
    for versions in by_vehicle(read_rows(layout, EMIT)).values():
        parent = depots[versions[0]["depot_id"]]
        opened, closed = _day(parent["opened_on"]), _day(parent["closed_on"])
        if closed is not None and closed < opened:  # type: ignore[operator]
            closed = None
        assert _day(versions[0]["valid_from"]) >= opened  # type: ignore[operator]
        if closed is None:
            continue
        assert all(_day(row["valid_from"]) <= closed for row in versions)
        assert all(row["valid_to"] is None or _day(row["valid_to"]) <= closed for row in versions)
        ended += 1
        last = versions[-1]
        if mode == "finalVersion":
            assert last["status"] == "retired"
        else:
            assert last["valid_to"] is not None
    return ended


def _lines(verified: Any) -> str:
    return verified.stdout + verified.stderr


@pytest.mark.parametrize(
    "end",
    [
        None,
        {"mode": "closeLast", "share": "0.1"},
        {"mode": "finalVersion", "share": "0.1", "column": "status", "value": "retired"},
    ],
)
def test_a_run_keeps_every_entity_to_its_depots_lifetime_and_verify_holds_it(
    tmp_path: Path, end: Optional[Dict[str, Any]]
) -> None:
    bundle = _bundle(tmp_path, end=end)
    layout = run_bundle(bundle, tmp_path / "state")
    assert _check_bound(layout, None if end is None else end["mode"]) > 0
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    assert "history:within_parent 60 of 60" in _lines(verified).replace("\n", " ")


def test_a_run_is_the_same_run_again(tmp_path: Path) -> None:
    assert_the_run_repeats(_bundle(tmp_path), tmp_path, run_bundle, EMIT)


def test_the_shares_joined_are_the_bytes_of_onerun_bundle(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, end={"mode": "closeLast", "share": "0.1"})
    assert_shares_join_to_the_run(bundle, tmp_path, run_bundle, 3, (EMIT,))


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_make_the_run_one_job_makes(tmp_path: Path) -> None:
    versions = 3 * parallel.MIN_ROWS_PER_JOB + 11
    bundle = _bundle(
        tmp_path,
        versions=versions,
        entities=2500,
        depots=1200,
        end={"mode": "finalVersion", "share": "0.1", "column": "status", "value": "retired"},
    )
    assert_same_run_at_one_and_four_jobs(bundle, result=True, kernel=KERNEL_V2)


def test_a_smaller_scale_keeps_the_bound(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    planned = plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False, scale="0.5"
    )
    assert planned.success, planned.stderr
    applied = apply_module.apply(
        bundle, run_id=planned.artifacts["run_id"], state_dir=tmp_path / "state"
    )
    assert applied.success, applied.stderr
    assert (
        _check_bound(RunLayout.for_state_dir(tmp_path / "state", planned.artifacts["run_id"]), None)
        > 0
    )


def test_the_key_without_a_lifetimes_entry_on_the_parent_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, lifetimes=False)
    planned = plan_refused(bundle, tmp_path / "state")
    assert "FLX-3403" in planned.stderr and "parent.lifetime" in planned.stderr


def test_the_key_is_refused_on_kernel_010(tmp_path: Path) -> None:
    from flux_engine.core.kernels import KERNEL_V1

    bundle = _bundle(tmp_path)
    with pytest.raises(FluxError):
        pipeline.execute(bundle, jobs=1, kernel=KERNEL_V1)


def test_the_line_counts_an_entity_that_leaves_its_parents_lifetime() -> None:
    from flux_engine.concordance.history_checks import within_parent_share
    from flux_engine.expr2.scope import Versions
    from flux_engine.seam.history import Histogram, HistoryEntry, HistoryParent

    held = HistoryEntry(
        emit=EMIT,
        entities=4,
        length=Histogram(("30", "90"), ("1",)),
        until=UNTIL,
        parent=HistoryParent("depot", lifetime=True),
    )
    versions = Versions(("vehicle",), "valid_from", "valid_to")
    parents = {
        (1,): {"o": date(2024, 1, 1), "c": date(2024, 12, 31)},
        (2,): {"o": date(2024, 1, 1), "c": None},
    }

    def row(vehicle: int, depot: int, start: date, end: Optional[date]) -> Dict[str, Any]:
        return {"vehicle": vehicle, "depot": depot, "valid_from": start, "valid_to": end}

    rows = [
        row(1, 1, date(2024, 2, 1), date(2024, 6, 1)),  # keeps to it, and ends
        row(1, 1, date(2024, 6, 1), date(2024, 9, 1)),
        row(2, 1, date(2023, 12, 1), None),  # starts before its parent opened
        row(3, 1, date(2024, 2, 1), None),  # its parent closed, and it has not ended
        row(4, 2, date(2024, 3, 1), None),  # an open parent holds anything after its start
    ]
    found = within_parent_share(held, rows, versions, parents, ("depot",), ("o", "c"))
    assert (found.satisfied, found.total) == (2, 4)


@pytest.mark.parametrize(
    ("until", "forced"),
    [
        ("2024-06-30T00:00:00Z", False),
        ("2024-06-30T12:00:00Z", False),
        ("2024-06-30T23:59:59.999Z", True),
        ("2024-07-01T00:00:00Z", True),
    ],
)
def test_a_date_parent_end_is_the_last_millisecond_of_its_day_for_a_timestamp_chain(
    until: str, forced: bool
) -> None:
    from datetime import datetime, timezone

    from flux_engine.concordance.history_checks import within_parent_share
    from flux_engine.expr2.scope import Versions
    from flux_engine.seam.history import Histogram, HistoryEntry, HistoryParent

    held = HistoryEntry(
        emit=EMIT,
        entities=1,
        length=Histogram(("30", "90"), ("1",)),
        until=until,
        parent=HistoryParent("depot", lifetime=True),
    )
    versions = Versions(("vehicle",), "valid_from", "valid_to")
    parents = {(1,): {"o": date(2024, 1, 1), "c": date(2024, 6, 30)}}
    start = datetime(2024, 2, 1, 8, tzinfo=timezone.utc)
    # An entity that keeps to its parent and has not ended: the generator leaves such an entity
    # open when its parent's last millisecond is after ``until``, and ends it otherwise.
    rows = [{"vehicle": 1, "depot": 1, "valid_from": start, "valid_to": None}]
    found = within_parent_share(held, rows, versions, parents, ("depot",), ("o", "c"))
    assert (found.satisfied, found.total) == (0 if forced else 1, 1)


def test_plan_says_how_many_entities_end_with_their_parent_in_one_version(tmp_path: Path) -> None:
    from ._fleet import FINAL

    planned = plan_module.plan(
        _bundle(tmp_path, end=FINAL),
        state_dir=tmp_path / "state",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
    assert f"history ({EMIT}): " in planned.stderr
    assert "entities end with their parent and have one version" in planned.stderr


@pytest.mark.parametrize(("closed", "held"), [(None, 1), (date(2024, 5, 1), 0)])
def test_a_history_with_no_end_closes_a_last_version_only_when_its_parent_ended_by_until(
    closed: Optional[date], held: int
) -> None:
    from flux_engine.concordance.history_checks import within_parent_share
    from flux_engine.expr2.scope import Versions
    from flux_engine.seam.history import Histogram, HistoryEntry, HistoryParent

    entry = HistoryEntry(
        emit=EMIT,
        entities=1,
        length=Histogram(("30", "90"), ("1",)),
        until="2024-03-31",
        parent=HistoryParent("depot", lifetime=True),
    )
    assert entry.end is None
    versions = Versions(("vehicle",), "valid_from", "valid_to")
    # The depot's last day is after ``until``: nothing forces the vehicle to end, so a closed
    # last version is a violation of the bound, and an open one holds it.
    parents = {(1,): {"o": date(2024, 1, 1), "c": date(2024, 12, 31)}}
    rows = [{"vehicle": 1, "depot": 1, "valid_from": date(2024, 2, 1), "valid_to": closed}]
    found = within_parent_share(entry, rows, versions, parents, ("depot",), ("o", "c"))
    assert (found.satisfied, found.total) == (held, 1)
