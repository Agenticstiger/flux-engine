# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the kernel 0.2.0 run of ``tests/fixtures/projected-rules`` to ``tests/golden/projected-rules/``.

The bundle is ``tests/fixtures/history-fleet`` (a history of vehicle versions, the
snapshot of their last versions, an events emit and a periodic emit) with contract derives on the
two projected emits that read the cells the projection sets: the period column, a column of the
version, a kind's override, an event's ``op``, its instants and its after image. The history ends
in a final version and the events carry a lag. It is planned with ``fluxsim plan --kernel 0.2.0``
and applied as written:

* ``kernel-0.2.0/`` holds the masked run digest, the row counts and the replay record. Before a
  projected row's rules ran after its projection (ADR 0027, the amendment of 2026-11-24), those
  rows failed their own rule lines, so this set is the run that fix makes.
* ``verify`` passes and every derive line of both projected emits is 100 %.
* The run replays, and ``--jobs 4`` gives the same run id, run digest and rows as ``--jobs 1``.

Regenerate with ``python tests/golden/projected-rules/_generate_golden.py`` and add a
``GOLDEN-CHANGELOG.md`` row for the set in the same commit;
``test_a_golden_change_requires_a_changelog_row`` enforces the row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.canonical import SELF_REFERENTIAL_FIELDS, mask_document
from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.replay import replay
from flux_engine.stages.verify import verify

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "projected-rules"
GOLDEN_DIR = Path(__file__).resolve().parent / "projected-rules" / "kernel-0.2.0"
CHANGELOG_PATH = REPO_ROOT / "GOLDEN-CHANGELOG.md"
EXPOSE_IDS = (
    "depot",
    "service_visit",
    "vehicle_version",
    "vehicle_current",
    "vehicle_change",
    "vehicle_month",
)
#: The derives of the two projected emits (``tests/stages/test_projected_rules_run.py``).
PROJECTED_DERIVES = {
    "vehicle_month": ("due_on", "is_legacy", "visit_key", "due_after"),
    "vehicle_change": ("is_create", "lag_days", "after_van"),
}
KERNEL = "0.2.0"

pytestmark = pytest.mark.integration


def _read_golden(name: str) -> Dict[str, Any]:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _run_digest_value_of(document: Dict[str, Any]) -> str:
    return digest_of_document(document, subject="runDigest").value


def _run(state_dir: Path, *, jobs: int) -> RunLayout:
    planned = plan(FIXTURE_DIR, state_dir=state_dir, kernel=KERNEL, jobs=jobs)
    assert planned.success, planned.stderr
    applied = apply(
        FIXTURE_DIR,
        run_id=str(planned.artifacts["run_id"]),
        state_dir=state_dir,
        auto_plan=False,
        jobs=jobs,
    )
    assert applied.success, applied.stderr
    return RunLayout.for_state_dir(state_dir, str(applied.artifacts["run_id"]))


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("golden-projected-rules-state") / ".flux"


@pytest.fixture(scope="module")
def layout(state_dir: Path) -> RunLayout:
    return _run(state_dir, jobs=1)


@pytest.fixture(scope="module")
def parallel_layout(tmp_path_factory: pytest.TempPathFactory) -> RunLayout:
    return _run(tmp_path_factory.mktemp("golden-projected-rules-jobs4-state") / ".flux", jobs=4)


def test_the_run_is_planned_under_kernel_0_2_0(layout: RunLayout) -> None:
    document = json.loads(layout.plan_path.read_text(encoding="utf-8"))
    assert (document["kernelVersion"], document["seedTreeVersion"]) == (KERNEL, "flux-seed/2")


def test_run_digest_matches_the_pinned_baseline(layout: RunLayout) -> None:
    actual = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    pinned = _read_golden("run-digest.json")
    assert mask_document(actual, self_referential=SELF_REFERENTIAL_FIELDS) == pinned
    assert pinned["kernelVersion"] == KERNEL
    assert sorted(pinned["components"]["emits"]) == sorted(EXPOSE_IDS)


def test_every_table_emits_the_pinned_row_count(layout: RunLayout) -> None:
    pinned = _read_golden("row-count.json")
    for expose_id in EXPOSE_IDS:
        lines = layout.emit_path(expose_id).read_text(encoding="utf-8").splitlines()
        assert sum(1 for line in lines if line) == pinned[f"emit-{expose_id}"] > 0


def test_the_run_id_and_run_digest_are_the_pinned_ones(layout: RunLayout) -> None:
    pinned = _read_golden("replay.json")
    assert layout.run_id == pinned["runId"]
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    assert _run_digest_value_of(document) == pinned["runDigest"]


def test_replay_matches_the_pinned_run_digest(layout: RunLayout, state_dir: Path) -> None:
    replayed = replay(
        FIXTURE_DIR,
        run_id=layout.run_id,
        assert_digest=_read_golden("replay.json")["runDigest"],
        state_dir=state_dir,
    )
    assert replayed.success, replayed.stderr


def test_four_jobs_give_the_pinned_run_digest(parallel_layout: RunLayout) -> None:
    """``--jobs 4`` plans and applies to the run id, run digest and rows ``--jobs 1`` gives."""
    pinned = _read_golden("replay.json")
    assert parallel_layout.run_id == pinned["runId"]
    document = json.loads(parallel_layout.run_digest_path.read_text(encoding="utf-8"))
    assert _run_digest_value_of(document) == pinned["runDigest"]
    assert mask_document(document, self_referential=SELF_REFERENTIAL_FIELDS) == _read_golden(
        "run-digest.json"
    )


def test_verify_holds_every_derive_line_of_both_projected_emits(
    layout: RunLayout, state_dir: Path
) -> None:
    result = verify(FIXTURE_DIR, run_id=layout.run_id, state_dir=state_dir)
    assert result.success, result.stderr
    lines = [
        line.strip() for line in result.stdout.splitlines() if line.strip().startswith("rules ")
    ]
    for expose_id, derive_ids in PROJECTED_DERIVES.items():
        line = next(line for line in lines if line.startswith(f"rules {expose_id}:"))
        assert "violated" not in line
        for derive_id in derive_ids:
            assert f"{derive_id} " in line, line
            assert "(100.00%)" in line.split(f"{derive_id} ", 1)[1].split(",")[0], line


def test_the_pinned_baseline_has_a_changelog_row() -> None:
    digest = _run_digest_value_of(_read_golden("run-digest.json"))
    assert digest in CHANGELOG_PATH.read_text(encoding="utf-8"), (
        f"{CHANGELOG_PATH.name} has no row for {digest}; "
        "tests/golden/projected-rules/kernel-0.2.0/run-digest.json changed with no changelog row"
    )


def test_a_golden_change_requires_a_changelog_row() -> None:
    """Move one table's digest and check that the gate turns red for it."""
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    pinned = _read_golden("run-digest.json")
    moved = json.loads(json.dumps(pinned))
    moved["components"]["emits"]["vehicle_month"] = "sha256:" + "ab" * 32
    assert _run_digest_value_of(moved) != _run_digest_value_of(pinned)
    assert _run_digest_value_of(moved) not in changelog
    assert _run_digest_value_of(pinned) in changelog
