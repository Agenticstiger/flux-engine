# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Change events generate: a run of ``tests/fixtures/history-projections`` through ``plan``, ``apply`` and ``verify``.

The bundle is a history of 60 subscriptions in 200 versions and ``subscription_change``, an events
emit with one event per change (ADR 0027 section 4), which declares no ``rows``. Every assertion
reads the rows a run wrote or the plan it recorded. What is pinned:

* **The events.** Each is a change of the history, in entity order; the plan records the derived
  count; ``verify`` holds ``event:matches_transition``; kernel 0.1.0 refuses the key.
* **Size.** ``--jobs 1`` and ``--jobs 4`` make the same run, ``--scale`` below 1 follows the scaled
  history, and the shares of ``--shard I/N`` joined are the whole run's bytes.
* **The lag.** ``date`` is ``at`` plus a lag drawn from ``lag``, inside ``[0, until - at]``;
  ``verify`` holds the line with a lag.
* **The window.** Two windows with one ``until`` give the events byte for byte.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify

from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_projection_plan_run import (
    HISTORY,
    NAMESPACE,
    _bundle,
    _plan_document,
    _rows,
    _run,
    plan_module,
)

pytestmark = pytest.mark.integration

EVENTS = "subscription_change"
LAG = {"bins": ["0", "1", "4", "20"], "binWeights": ["0.5", "0.3", "0.2"]}
UNTIL = "2025-12-31"
FINAL = {"mode": "finalVersion", "share": "0.25", "column": "status", "value": "cancelled"}
CLOSE = {"mode": "closeLast", "share": "0.25", "column": "status", "value": "cancelled"}


def _versions(layout: RunLayout) -> Dict[Any, List[Dict[str, Any]]]:
    held: Dict[Any, List[Dict[str, Any]]] = {}
    for row in _rows(layout, HISTORY):
        held.setdefault(row["subscription_id"], []).append(row)
    return held


def _expected(layout: RunLayout, mode: Optional[str]) -> List[Dict[str, Any]]:
    """The events the history's written rows make, in entity order."""
    found: List[Dict[str, Any]] = []

    def event(op: str, about: Dict[str, Any], before: Any, after: Any, at: str) -> None:
        found.append(
            {
                "subscription_id": about["subscription_id"],
                "op": op,
                "changed_at": at,
                "recorded_at": at,
                "before_plan": None if before is None else before["plan"],
                "after_plan": None if after is None else after["plan"],
            }
        )

    for chain in _versions(layout).values():
        final = mode == "finalVersion" and chain[-1]["status"] == "cancelled"
        for index, version in enumerate(chain):
            if index == 0:
                event("c", version, None, version, version["valid_from"])
            elif final and index == len(chain) - 1:
                event("d", version, chain[index - 1], None, version["valid_from"])
            else:
                event("u", version, chain[index - 1], version, version["valid_from"])
        if mode == "closeLast" and chain[-1]["valid_to"] is not None:
            event("d", chain[-1], chain[-1], None, chain[-1]["valid_to"])
    return found


def _fields(rows: List[Dict[str, Any]], *, lagged: bool = False) -> List[Dict[str, Any]]:
    names = ["subscription_id", "op", "changed_at", "recorded_at", "before_plan", "after_plan"]
    if lagged:
        names.remove("recorded_at")
    return [{name: row[name] for name in names} for row in rows]


def _declare(bundle: Path, **options: Any) -> None:
    """Change the events entry of *bundle*."""
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["events"][0].update(options)
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")


# --- plan, apply, verify ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", [None, "finalVersion", "closeLast"])
def test_a_run_writes_one_event_per_change_and_verify_holds_the_line(
    tmp_path: Path, mode: Optional[str]
) -> None:
    end = {None: None, "finalVersion": FINAL, "closeLast": CLOSE}[mode]
    bundle = _bundle(tmp_path, end=end)
    layout = _run(bundle, tmp_path / "state")
    events = _rows(layout, EVENTS)
    assert _fields(events) == _expected(layout, mode)
    assert {row["op"] for row in events} >= {"c", "u"}
    if mode is not None:
        assert "d" in {row["op"] for row in events}
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    assert "event:matches_transition" in verified.stdout + verified.stderr


def test_a_run_is_the_same_run_again(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    first = _run(bundle, tmp_path / "a")
    second = _run(bundle, tmp_path / "b")
    assert first.emit_path(EVENTS).read_bytes() == second.emit_path(EVENTS).read_bytes()


def test_on_keeps_the_listed_ops(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, end=CLOSE)
    _declare(bundle, on=["c", "d"])
    layout = _run(bundle, tmp_path / "state")
    assert _fields(_rows(layout, EVENTS)) == [
        row for row in _expected(layout, "closeLast") if row["op"] in ("c", "d")
    ]
    assert verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state").success


# --- the lag ----------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [None, "closeLast"])
def test_a_lag_dates_each_event_after_its_change_and_before_until(
    tmp_path: Path, mode: Optional[str]
) -> None:
    bundle = _bundle(tmp_path, end=CLOSE if mode else None)
    _declare(bundle, lag=LAG)
    layout = _run(bundle, tmp_path / "state")
    events = _rows(layout, EVENTS)
    expected = _expected(layout, mode)
    assert _fields(events, lagged=True) == [
        {k: v for k, v in row.items() if k != "recorded_at"} for row in expected
    ]
    lags = [(row["recorded_at"] > row["changed_at"]) for row in events]
    assert any(lags) and not all(lags)
    assert all(row["changed_at"] <= row["recorded_at"] <= UNTIL for row in events)
    verified = verify(bundle, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr


@pytest.mark.parametrize("count", [1, 5])
def test_the_shares_of_a_lagged_run_joined_are_the_bytes_of_one_run(
    tmp_path: Path, count: int
) -> None:
    bundle = _bundle(tmp_path, end=CLOSE)
    _declare(bundle, lag=LAG)
    whole = _run(bundle, tmp_path / "whole")
    assert_shares_join_to_the_run(bundle, tmp_path, whole, count, (EVENTS,))


def test_two_jobs_are_not_one_job_apart_with_a_lag(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, end=CLOSE)
    _declare(bundle, lag=LAG)
    assert_same_run_at_one_and_four_jobs(bundle, plan=False, kernel=KERNEL_V2)


# --- the derived count ------------------------------------------------------------------------


def test_the_plan_records_the_derived_rows_and_where_they_come_from(tmp_path: Path) -> None:
    layout = _run(_bundle(tmp_path, end=CLOSE), tmp_path / "state")
    emits = _plan_document(layout)["emits"]
    assert emits[EVENTS]["rows"] == len(_rows(layout, EVENTS))
    assert emits[EVENTS]["rowsFrom"] == {"of": HISTORY, "projection": "events"}
    assert emits[EVENTS]["rows"] > 200  # a change per version, and a close per ended entity
    assert "rowsFrom" not in emits[HISTORY]


def test_a_declared_rows_on_the_events_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    path = bundle / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["emits"][EVENTS]["rows"] = 10
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    planned = plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success
    assert planned.error_code == "FLX-3403" and EVENTS in planned.stderr


def test_kernel_010_refuses_the_events(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_bundle(tmp_path), kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "kernel 0.2.0" in str(caught.value)


# --- jobs, scale, shard -----------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_make_the_run_one_job_makes(tmp_path: Path) -> None:
    versions = 3 * parallel.MIN_ROWS_PER_JOB + 11
    entities = 2 * parallel.MIN_ROWS_PER_JOB + 50
    bundle = _bundle(tmp_path, versions=versions, entities=entities, customers=1200, end=CLOSE)
    _, four = assert_same_run_at_one_and_four_jobs(bundle, result=True, kernel=KERNEL_V2)
    events = next(e for e in four.emits if e.resolved.expose_id == EVENTS)
    assert events.plan is not None and events.plan.rows > versions
    assert events.processes > 1


def test_two_jobs_are_not_one_job_apart_on_a_small_run(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, end=CLOSE)
    assert_same_run_at_one_and_four_jobs(bundle, kernel=KERNEL_V2)


@pytest.mark.parametrize(("scale", "versions"), [("0.5", 100), ("0.25", 50)])
def test_a_scale_below_one_follows_the_scaled_history(
    tmp_path: Path, scale: str, versions: int
) -> None:
    layout = _run(_bundle(tmp_path, end=CLOSE), tmp_path / "state", scale=scale)
    assert len(_rows(layout, HISTORY)) == versions
    events = _rows(layout, EVENTS)
    assert _fields(events) == _expected(layout, "closeLast")
    assert _plan_document(layout)["emits"][EVENTS]["rows"] == len(events)  # not scaled again


def test_a_scale_above_one_follows_the_scaled_history(tmp_path: Path) -> None:
    layout = _run(_bundle(tmp_path), tmp_path / "state", scale="2")
    assert len(_rows(layout, EVENTS)) == 400


@pytest.mark.parametrize("count", [1, 3, 7])
def test_the_shares_joined_in_order_are_the_bytes_of_one_run(tmp_path: Path, count: int) -> None:
    bundle = _bundle(tmp_path, end=CLOSE)
    whole = _run(bundle, tmp_path / "whole")
    assert_shares_join_to_the_run(bundle, tmp_path, whole, count, (EVENTS,))


# --- the window -------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, CLOSE, FINAL])
def test_two_windows_with_one_until_give_the_events_byte_for_byte(
    tmp_path: Path, end: Optional[Dict[str, Any]]
) -> None:
    narrow_bundle = _bundle(tmp_path, name="narrow", end=end)
    wide_bundle = _bundle(tmp_path, name="wide", end=end, window_end="2030-06-30T00:00:00Z")
    _declare(narrow_bundle, lag=LAG)
    _declare(wide_bundle, lag=LAG)
    narrow = _run(narrow_bundle, tmp_path / "a")
    wide = _run(wide_bundle, tmp_path / "b")
    assert narrow.emit_path(EVENTS).read_bytes() == wide.emit_path(EVENTS).read_bytes()
    assert narrow.emit_path(HISTORY).read_bytes() == wide.emit_path(HISTORY).read_bytes()
    assert narrow.run_id != wide.run_id


# --- the command line -------------------------------------------------------------------------


def test_the_command_line_plans_applies_and_verifies_the_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from flux_engine.cli import main

    bundle = _bundle(tmp_path, end=CLOSE)
    state = str(tmp_path / "state")
    assert main(["plan", str(bundle), "--kernel", "0.2.0", "--state-dir", state, "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    run_id = envelope["artifacts"]["run_id"]
    assert main(["apply", str(bundle), "--run-id", run_id, "--state-dir", state, "--json"]) == 0
    capsys.readouterr()
    assert main(["verify", str(bundle), "--run-id", run_id, "--state-dir", state, "--json"]) == 0
    layout = RunLayout.for_state_dir(Path(state), run_id)
    assert _fields(_rows(layout, EVENTS)) == _expected(layout, "closeLast")
    assert _plan_document(layout)["emits"][EVENTS]["rowsFrom"]["of"] == HISTORY
