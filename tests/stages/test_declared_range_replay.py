# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A recorded plan replays without the cut of a contract's date range (ADR 0024, amendment of 2026-11-25).

A plan written before the cut did not cut a column, and ``apply --run-id`` and ``replay`` run the
bundle again, so the cut must not apply to it: its rows, its run digest and ``replay
--assert-digest`` keep what they were. A plan that cut says so with ``dateRanges: "cut"``, which the
plan digest covers; a plan that cut no column writes no key, so it keeps its bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.core.digest import build_plan_digest, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.stages import pipeline
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.replay import replay
from flux_engine.stages.verify import verify

from ._fleet import VISIT, apply_module, bundle, rows, run_bundle
from .test_declared_range_run import HIGH, LOW, _visits

pytestmark = pytest.mark.integration

KEY = "dateRanges"


def _recorded_digest(state: Path, run_id: str) -> str:
    document = json.loads((state / "runs" / run_id / "run-digest.json").read_text("utf-8"))
    return digest_of_document(document, subject="runDigest").value


def _run_without_the_cut(
    target: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[RunLayout, str]:
    """A run as the engine made it before the cut: nothing is cut and the plan holds no key."""
    built = pipeline.build_plan

    def uncut(*args: Any, **options: Any) -> Any:
        return built(*args, **{**options, "uncut": None})

    with monkeypatch.context() as patched:
        patched.setattr(pipeline, "build_plan", uncut)
        layout = run_bundle(target, state, jobs=1)
    return layout, _recorded_digest(state, layout.run_id)


def _dates(layout: RunLayout) -> List[str]:
    return [row["visited_on"] for row in rows(layout, VISIT)]


def _outside(dates: List[str]) -> int:
    return sum(1 for value in dates if not LOW <= value <= HIGH)


@pytest.mark.parametrize(
    "rule",
    [
        f"between '{LOW}' and '{HIGH}'",
        "between '2012-01-01' and '2013-12-31'",
        f"between '{HIGH}' and '{LOW}'",
    ],
)
def test_a_plan_recorded_without_the_cut_replays_its_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rule: str
) -> None:
    # The second range holds none of the column's dates and the third is empty: no `FLX-3403` for a
    # plan that never cut.
    target = _visits(tmp_path, rule=rule)
    state = tmp_path / "state"
    layout, digest = _run_without_the_cut(target, state, monkeypatch)
    assert KEY not in load_recorded_plan(layout)
    recorded = layout.emit_path(VISIT).read_bytes()
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        assert layout.emit_path(VISIT).read_bytes() == recorded
        assert _recorded_digest(state, layout.run_id) == digest


def test_applying_a_plan_recorded_without_the_cut_writes_no_cut_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _visits(tmp_path)
    state = tmp_path / "state"
    layout, _ = _run_without_the_cut(target, state, monkeypatch)
    recorded = layout.emit_path(VISIT).read_bytes()
    layout.emit_path(VISIT).unlink()
    applied = apply_module.apply(target, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    assert layout.emit_path(VISIT).read_bytes() == recorded
    # The bundle's range is read by `verify` whatever the plan was: those dates are the finding.
    assert not verify(target, run_id=layout.run_id, state_dir=state).success


def test_a_new_plan_that_cut_says_so_and_replays_cut(tmp_path: Path) -> None:
    target = _visits(tmp_path)
    state = tmp_path / "state"
    layout = run_bundle(target, state)
    assert load_recorded_plan(layout)[KEY] == "cut"
    assert _outside(_dates(layout)) == 0
    recorded = layout.emit_path(VISIT).read_bytes()
    digest = _recorded_digest(state, layout.run_id)
    replayed = replay(target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=4)
    assert replayed.success, replayed.stderr
    assert layout.emit_path(VISIT).read_bytes() == recorded


def test_the_cut_moves_the_plan_digest_of_a_plan_that_cut_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _visits(tmp_path)
    cut = run_bundle(target, tmp_path / "cut")
    uncut, _ = _run_without_the_cut(target, tmp_path / "uncut", monkeypatch)
    assert cut.run_id != uncut.run_id
    document: Dict[str, Any] = dict(load_recorded_plan(cut))
    del document[KEY]
    assert build_plan_digest(document).ref.value != document["planDigest"]


def test_a_plan_that_cut_nothing_writes_no_key(tmp_path: Path) -> None:
    layout = run_bundle(bundle(tmp_path), tmp_path / "state")
    assert KEY not in load_recorded_plan(layout)
    inside = _visits(
        tmp_path,
        name="inside",
        bins=["2001-01-01", "2003-01-01", "2005-12-31"],
        weights=["0.6", "0.4"],
    )
    assert KEY not in load_recorded_plan(run_bundle(inside, tmp_path / "inside-state"))


def test_a_plan_with_a_range_value_this_engine_does_not_know_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state"
    layout = run_bundle(_visits(tmp_path), state)
    forged = {**load_recorded_plan(layout), KEY: "tomorrow"}
    forged["planDigest"] = build_plan_digest(forged).ref.value
    moved = RunLayout.for_state_dir(state, run_id_for(forged["planDigest"]))
    moved.run_dir.mkdir(parents=True)
    moved.plan_path.write_text(json.dumps(forged, indent=2, sort_keys=True), "utf-8")
    with pytest.raises(FluxError, match=f"FLX-2102.*{KEY}"):
        load_recorded_plan(moved)
