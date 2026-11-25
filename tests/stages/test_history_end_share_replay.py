# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A plan written before the end share was a share of every entity replays its own bytes.

``tests/fixtures/end-share-run-0.11.1`` is a plan and run digest that flux-sim-engine 0.11.1
wrote for the fleet bundle with an ``end`` that declares a column: 60 entities, 33 of them with two
versions or more, and a share of 0.25. That engine ended a share of the eligible entities (8). A
new plan ends a share of every entity (15) and carries ``historyEndShare`` so that it says so
(ADR 0017, amendment of 2026-11-24). A plan without the key replays the old allocation, so the run
id a plan made keeps the digests it had; planning the same bundle again moves the plan digest and
with it the run id.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_engine.core.digest import build_plan_digest, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.generator.history import ended_count
from flux_engine.stages.apply import load_recorded_plan
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.replay import replay
from flux_engine.stages.verify import verify

from ._fleet import HISTORY, apply_module, bundle, by_vehicle, plan_module, rows

pytestmark = pytest.mark.integration

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "end-share-run-0.11.1"
MARKER = "historyEndShare"
#: The fixture's 60 entities and the 33 of them that hold two versions or more.
ENTITIES, ELIGIBLE = 60, 33
#: What flux-sim-engine 0.11.1 wrote: the run id and the digest of the run.
RECORDED_RUN_ID = "a28e95573384a621"
RECORDED_RUN_DIGEST = "sha256:88092ead8742505fd7fff666c04fbc0bf370fb383caa6804b499fb6d3595b457"


def _copy(tmp_path: Path) -> Tuple[Path, Path, str]:
    shutil.copytree(RECORDED, tmp_path / "run")
    bundle, state = tmp_path / "run" / "bundle", tmp_path / "run" / "state"
    (run_id,) = [entry.name for entry in (state / "runs").iterdir()]
    return bundle, state, run_id


def _recorded(state: Path, run_id: str) -> Dict[str, Any]:
    """The run digest document of a run, with its ``runDigest`` (KERNEL-SPEC section 11) added."""
    document = json.loads((state / "runs" / run_id / "run-digest.json").read_text("utf-8"))
    assert isinstance(document, dict)
    return {**document, "runDigest": digest_of_document(document, subject="runDigest").value}


def _retired(layout: RunLayout) -> int:
    """The entities of the history that end: those whose last version is retired."""
    chains = by_vehicle(rows(layout, HISTORY)).values()
    return sum(1 for versions in chains if versions[-1]["status"] == "retired")


def test_the_recorded_plan_carries_no_end_share_marker(tmp_path: Path) -> None:
    _, state, run_id = _copy(tmp_path)
    document = json.loads((state / "runs" / run_id / "plan.json").read_text("utf-8"))
    assert MARKER not in document and document["kernelVersion"] == "0.2.0"
    assert run_id == RECORDED_RUN_ID
    assert _recorded(state, run_id)["runDigest"] == RECORDED_RUN_DIGEST


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_plan_written_before_the_end_share_moved_replays_its_run_digest(
    tmp_path: Path, jobs: int
) -> None:
    bundle, state, run_id = _copy(tmp_path)
    recorded = _recorded(state, run_id)
    replayed = replay(
        bundle, run_id=run_id, assert_digest=RECORDED_RUN_DIGEST, state_dir=state, jobs=jobs
    )
    assert replayed.success, replayed.stderr
    again = _recorded(state, run_id)
    assert again["components"]["emits"] == recorded["components"]["emits"]
    assert again["runDigest"] == RECORDED_RUN_DIGEST
    assert (
        _retired(RunLayout.for_state_dir(state, run_id))
        == ended_count(ENTITIES, ELIGIBLE, "0.25", of_eligible=True)
        == 8
    )


def test_the_replayed_run_verifies(tmp_path: Path) -> None:
    bundle, state, run_id = _copy(tmp_path)
    applied = apply_module.apply(bundle, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr
    verified = verify(bundle, run_id=run_id, state_dir=state)
    assert verified.exit_code == 0, verified.stderr


def test_planning_the_bundle_again_ends_a_share_of_every_entity(tmp_path: Path) -> None:
    bundle, state, old_id = _copy(tmp_path)
    old = _recorded(state, old_id)
    fresh = tmp_path / "fresh"
    planned = plan_module.plan(bundle, state_dir=fresh, kernel="0.2.0", stage=False)
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    document = load_recorded_plan(RunLayout.for_state_dir(fresh, run_id))
    assert document[MARKER] == "entities"
    assert document["planDigest"] != old["planDigest"] and run_id != old_id
    applied = apply_module.apply(bundle, run_id=run_id, state_dir=fresh)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(fresh, run_id)
    assert _retired(layout) == ended_count(ENTITIES, ELIGIBLE, "0.25") == 15
    assert _recorded(fresh, run_id)["runDigest"] != old["runDigest"]


def test_a_new_plan_replays_its_own_allocation(tmp_path: Path) -> None:
    bundle, _, _ = _copy(tmp_path)
    state = tmp_path / "fresh"
    planned = plan_module.plan(bundle, state_dir=state, kernel="0.2.0", stage=False)
    run_id = planned.artifacts["run_id"]
    applied = apply_module.apply(bundle, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr
    digest = _recorded(state, run_id)["runDigest"]
    replayed = replay(bundle, run_id=run_id, assert_digest=digest, state_dir=state)
    assert replayed.success, replayed.stderr


def test_a_plan_without_an_ineligible_entity_carries_no_marker_and_keeps_its_count(
    tmp_path: Path,
) -> None:
    # closeLast with no column leaves every entity eligible, so E' = E and both counts are one.
    target = bundle(
        tmp_path,
        history={"activation": "commissioned_on"},
        end={"mode": "closeLast", "share": "0.25"},
    )
    planned = plan_module.plan(target, state_dir=tmp_path / "state", kernel="0.2.0", stage=False)
    assert planned.success, planned.stderr
    document = load_recorded_plan(
        RunLayout.for_state_dir(tmp_path / "state", planned.artifacts["run_id"])
    )
    assert MARKER not in document
    assert ended_count(ENTITIES, ENTITIES, "0.25") == ended_count(
        ENTITIES, ENTITIES, "0.25", of_eligible=True
    )


def test_a_plan_with_a_marker_this_engine_does_not_know_is_refused(tmp_path: Path) -> None:
    bundle, state, run_id = _copy(tmp_path)
    path = state / "runs" / run_id / "plan.json"
    document = json.loads(path.read_text("utf-8"))
    document[MARKER] = "tomorrow"
    digest = build_plan_digest(document).ref.value
    document["planDigest"] = digest
    moved = state / "runs" / run_id_for(digest)
    moved.mkdir()
    (moved / "plan.json").write_text(json.dumps(document, indent=2, sort_keys=True), "utf-8")
    with pytest.raises(FluxError) as refused:
        load_recorded_plan(RunLayout.for_state_dir(state, run_id_for(digest)))
    assert MARKER in str(refused.value)
    del bundle


def test_a_long_unknown_marker_is_quoted_short(tmp_path: Path) -> None:
    bundle, state, run_id = _copy(tmp_path)
    path = state / "runs" / run_id / "plan.json"
    document = json.loads(path.read_text("utf-8"))
    document[MARKER] = "tomorrow" * 100_000
    digest = build_plan_digest(document).ref.value
    document["planDigest"] = digest
    moved = state / "runs" / run_id_for(digest)
    moved.mkdir()
    (moved / "plan.json").write_text(json.dumps(document, indent=2, sort_keys=True), "utf-8")
    with pytest.raises(FluxError) as refused:
        load_recorded_plan(RunLayout.for_state_dir(state, run_id_for(digest)))
    message = str(refused.value)
    assert len(message) < 600
    assert "tomorrowtomorrow" in message
    assert "a newer or incompatible version" in message
    del bundle
