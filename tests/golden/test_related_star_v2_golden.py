# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pins kernel 0.2.0's related-tables run of ``tests/fixtures/related-star`` to ``tests/golden/related-star-v2/``.

The bundle and the files are ``tests/golden/related-star/``'s, and the run is planned with
``fluxsim plan --kernel 0.2.0`` (KERNEL-SPEC-2): counter-based streams, ``permute/2``, keys
labelled ``flux-key/2`` and identifiers from Philox. A set pinned on one platform and checked
on every cell of the matrix is what holds kernel 0.2.0's bits to be the same on every
operating system and CPU. Kernel 0.1.0's set is unchanged beside it.

Regenerate with ``python tests/golden/related-star-v2/_generate_golden.py`` and add a
``GOLDEN-CHANGELOG.md`` row in the same commit; ``test_a_golden_change_requires_a_changelog_row``
enforces the row.
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

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_DIR = REPO_ROOT / "tests" / "fixtures" / "related-star"
GOLDEN_DIR = Path(__file__).resolve().parent / "related-star-v2"
V1_GOLDEN_DIR = Path(__file__).resolve().parent / "related-star"
CHANGELOG_PATH = REPO_ROOT / "GOLDEN-CHANGELOG.md"
EXPOSE_IDS = ("date_dim", "item", "customer", "store_sales", "store_returns")
KERNEL = "0.2.0"

pytestmark = pytest.mark.integration


def _read_golden(name: str, directory: Path = GOLDEN_DIR) -> Dict[str, Any]:
    return json.loads((directory / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("golden-related-star-v2") / ".flux"


@pytest.fixture(scope="module")
def run_id(state_dir: Path) -> str:
    planned = plan(BUNDLE_DIR, state_dir=state_dir, kernel=KERNEL)
    assert planned.success, planned.stderr
    applied = apply(
        BUNDLE_DIR, run_id=str(planned.artifacts["run_id"]), state_dir=state_dir, auto_plan=False
    )
    assert applied.success, applied.stderr
    return str(applied.artifacts["run_id"])


@pytest.fixture(scope="module")
def layout(state_dir: Path, run_id: str) -> RunLayout:
    return RunLayout.for_state_dir(state_dir, run_id)


def test_the_run_is_kernel_0_2_0s(layout: RunLayout) -> None:
    plan_document = json.loads(layout.plan_path.read_text(encoding="utf-8"))
    assert (plan_document["kernelVersion"], plan_document["seedTreeVersion"]) == (
        KERNEL,
        "flux-seed/2",
    )


def test_run_digest_matches_the_pinned_baseline(layout: RunLayout) -> None:
    actual = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    pinned = _read_golden("run-digest.json")
    assert mask_document(actual, self_referential=SELF_REFERENTIAL_FIELDS) == pinned
    assert pinned["kernelVersion"] == KERNEL
    assert sorted(pinned["components"]["emits"]) == sorted(EXPOSE_IDS)


def test_every_table_emits_the_pinned_row_count(layout: RunLayout) -> None:
    pinned = _read_golden("row-count.json")
    assert pinned == _read_golden("row-count.json", V1_GOLDEN_DIR)
    for expose_id in EXPOSE_IDS:
        lines = layout.emit_path(expose_id).read_text(encoding="utf-8").splitlines()
        assert sum(1 for line in lines if line) == pinned[f"emit-{expose_id}"] > 0


def test_the_kernel_moves_exactly_the_tables_it_draws_for() -> None:
    """A table the kernel draws for has rows of its own; one it draws nothing for does not.

    ``date_dim``'s key fills its declared range exactly, laid out in row order, and its one
    year level has weight 1, so no value of it depends on a draw and its rows are kernel
    0.1.0's. Every other table draws, and every one of them differs.
    """
    emits = _read_golden("run-digest.json")["components"]["emits"]
    v1_emits = _read_golden("run-digest.json", V1_GOLDEN_DIR)["components"]["emits"]
    moved = {expose_id for expose_id in EXPOSE_IDS if emits[expose_id] != v1_emits[expose_id]}
    assert moved == {"item", "customer", "store_sales", "store_returns"}


def test_the_run_id_and_run_digest_are_the_pinned_ones(run_id: str, layout: RunLayout) -> None:
    pinned = _read_golden("replay.json")
    assert run_id == pinned["runId"]
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    assert digest_of_document(document, subject="runDigest").value == pinned["runDigest"]


def test_replay_reproduces_the_pinned_run_digest(state_dir: Path, run_id: str) -> None:
    replayed = replay(
        BUNDLE_DIR,
        run_id=run_id,
        assert_digest=_read_golden("replay.json")["runDigest"],
        state_dir=state_dir,
    )
    assert replayed.success, replayed.stderr


def _run_digest_of(document: Dict[str, Any]) -> str:
    return digest_of_document(document, subject="runDigest").value


def test_the_pinned_baseline_has_a_changelog_row() -> None:
    digest = _run_digest_of(_read_golden("run-digest.json"))
    assert digest in CHANGELOG_PATH.read_text(encoding="utf-8"), (
        f"{CHANGELOG_PATH.name} has no row for {digest} -- "
        "tests/golden/related-star-v2/run-digest.json changed with no changelog row"
    )


def test_a_golden_change_requires_a_changelog_row() -> None:
    """Move one table's digest and prove the gate turns red for it."""
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    pinned = _read_golden("run-digest.json")
    moved = json.loads(json.dumps(pinned))
    moved["components"]["emits"]["store_returns"] = "sha256:" + "ab" * 32
    assert _run_digest_of(moved) != _run_digest_of(pinned)
    assert _run_digest_of(moved) not in changelog
    assert _run_digest_of(pinned) in changelog
