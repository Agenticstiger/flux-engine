# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A snapshot generates: a run of ``tests/fixtures/history-projections`` through ``plan``, ``apply`` and ``verify``.

The bundle is a history of 60 subscriptions in 200 versions and ``subscription_current``, the
snapshot of their last versions, which declares no ``rows`` (ADR 0027). Every assertion reads the
rows a run wrote or the plan it recorded. What is pinned:

* **The count.** The plan records the derived ``rows`` and ``rowsFrom`` of the snapshot, and no
  other emit has them; the count enters ``planDigest`` and not an entry for ``H``; a declared
  ``rows`` is refused; kernel 0.1.0 refuses the snapshot.
* **The rows.** Each is its entity's last version, in entity order; ``ended: drop`` leaves out the
  ended entities; ``verify`` holds ``snapshot:equals_last_version``.
* **Size.** ``--jobs 1`` and ``--jobs 4`` make the same run, ``--scale`` below 1 follows the scaled
  history, and the shares of ``--shard I/N`` joined are the whole run's bytes.
* **The window.** Two windows with one ``until`` give the snapshot byte for byte.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator.history import ended_count
from flux_engine.stages import _parallel as parallel
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify

from ._same_run import (
    assert_same_run_at_one_and_four_jobs,
    assert_shares_join_to_the_run,
    assert_the_run_repeats,
)
from .test_projection_plan_run import (
    HISTORY,
    SNAPSHOT,
    _bundle,
    _plan_document,
    _rows,
    _run,
)

pytestmark = pytest.mark.integration

SNAPSHOT_COLUMNS = ["subscription_id", "plan", "status", "started_on"]


def _last_versions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The last version of each entity, in the order the entities first appear."""
    last: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        last[row["subscription_id"]] = row
    return list(last.values())


def _columns(row: Dict[str, Any], names: List[str]) -> Dict[str, Any]:
    return {name: row[name] for name in names}


# --- plan, apply, verify ----------------------------------------------------------------------


def test_a_run_writes_one_row_per_entity_and_verify_holds_the_snapshot_line(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    layout = _run(bundle, tmp_path / "state")
    versions = _rows(layout, HISTORY)
    snapshot = _rows(layout, SNAPSHOT)
    assert len(versions) == 200 and len(snapshot) == 60
    assert snapshot == [_columns(row, SNAPSHOT_COLUMNS) for row in _last_versions(versions)]
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    assert "snapshot:equals_last_version" in verified.stdout + verified.stderr


def test_a_run_is_the_same_run_again(tmp_path: Path) -> None:
    assert_the_run_repeats(_bundle(tmp_path), tmp_path, _run, SNAPSHOT)


# --- the derived count ------------------------------------------------------------------------


# --- ended ------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["closeLast", "finalVersion"])
def test_ended_drop_leaves_out_the_ended_entities(tmp_path: Path, mode: str) -> None:
    end = {"mode": mode, "share": "0.25", "column": "status", "value": "cancelled"}
    bundle = _bundle(tmp_path, end=end, ended="drop")
    layout = _run(bundle, tmp_path / "state")
    versions = _rows(layout, HISTORY)
    snapshot = _rows(layout, SNAPSHOT)
    held = {}
    for row in versions:
        held.setdefault(row["subscription_id"], []).append(row)
    eligible = sum(1 for chain in held.values() if len(chain) >= 2)
    # An end with a column ends entities of two versions or more, of a share of every entity.
    ended = ended_count(len(held), eligible, "0.25")
    assert ended > 0
    assert len(snapshot) == 60 - ended
    assert all(row["status"] != "cancelled" for row in snapshot)
    assert _plan_document(layout)["emits"][SNAPSHOT]["rows"] == 60 - ended
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr


# --- jobs, scale, shard -----------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_make_the_run_one_job_makes(tmp_path: Path) -> None:
    versions = 3 * parallel.MIN_ROWS_PER_JOB + 11
    entities = 2 * parallel.MIN_ROWS_PER_JOB + 50
    bundle = _bundle(tmp_path, versions=versions, entities=entities, customers=1200)
    _, four = assert_same_run_at_one_and_four_jobs(bundle, result=True, kernel=KERNEL_V2)
    snapshot = next(e for e in four.emits if e.resolved.expose_id == SNAPSHOT)
    assert snapshot.plan is not None and snapshot.plan.rows == entities
    assert snapshot.processes > 1


def test_two_jobs_are_not_one_job_apart_on_a_small_run(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    assert_same_run_at_one_and_four_jobs(bundle, kernel=KERNEL_V2)


@pytest.mark.parametrize(("scale", "entities", "rows"), [("0.5", 30, 100), ("0.25", 15, 50)])
def test_a_scale_below_one_follows_the_scaled_history(
    tmp_path: Path, scale: str, entities: int, rows: int
) -> None:
    layout = _run(_bundle(tmp_path), tmp_path / "state", scale=scale)
    versions = _rows(layout, HISTORY)
    snapshot = _rows(layout, SNAPSHOT)
    assert len(versions) == rows and len(snapshot) == entities
    assert snapshot == [_columns(row, SNAPSHOT_COLUMNS) for row in _last_versions(versions)]
    emits = _plan_document(layout)["emits"]
    assert emits[SNAPSHOT]["rows"] == entities  # not scaled again


def test_a_scale_above_one_follows_the_scaled_history(tmp_path: Path) -> None:
    layout = _run(_bundle(tmp_path), tmp_path / "state", scale="2")
    assert len(_rows(layout, SNAPSHOT)) == 120


@pytest.mark.parametrize("count", [1, 3, 7])
@pytest.mark.parametrize("ended", [None, "drop"])
def test_the_shares_joined_in_order_are_the_bytes_of_one_run(
    tmp_path: Path, count: int, ended: Optional[str]
) -> None:
    end = {"mode": "finalVersion", "share": "0.25", "column": "status", "value": "cancelled"}
    bundle = _bundle(tmp_path, ended=ended, end=end if ended else None)
    assert_shares_join_to_the_run(bundle, tmp_path, _run, count, (HISTORY, SNAPSHOT))


# --- the window -------------------------------------------------------------------------------


@pytest.mark.parametrize("ended", [None, "drop"])
def test_two_windows_with_one_until_give_the_snapshot_byte_for_byte(
    tmp_path: Path, ended: Optional[str]
) -> None:
    end = {"mode": "closeLast", "share": "0.25", "column": "status", "value": "cancelled"}
    options: Dict[str, Any] = {"ended": ended, "end": end if ended else None}
    narrow = _run(_bundle(tmp_path, name="narrow", **options), tmp_path / "a")
    wide = _run(
        _bundle(tmp_path, name="wide", window_end="2030-06-30T00:00:00Z", **options), tmp_path / "b"
    )
    assert narrow.emit_path(SNAPSHOT).read_bytes() == wide.emit_path(SNAPSHOT).read_bytes()
    assert narrow.emit_path(HISTORY).read_bytes() == wide.emit_path(HISTORY).read_bytes()
    assert narrow.run_id != wide.run_id


# --- the command line -------------------------------------------------------------------------


def test_the_command_line_plans_applies_and_verifies_a_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from flux_engine.cli import main

    bundle = _bundle(tmp_path)
    state = str(tmp_path / "state")
    assert main(["plan", str(bundle), "--kernel", "0.2.0", "--state-dir", state, "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    run_id = envelope["artifacts"]["run_id"]
    assert main(["apply", str(bundle), "--run-id", run_id, "--state-dir", state, "--json"]) == 0
    capsys.readouterr()
    assert main(["verify", str(bundle), "--run-id", run_id, "--state-dir", state, "--json"]) == 0
    layout = RunLayout.for_state_dir(Path(state), run_id)
    assert len(_rows(layout, SNAPSHOT)) == 60
    assert _plan_document(layout)["emits"][SNAPSHOT]["rowsFrom"]["of"] == HISTORY
