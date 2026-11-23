# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pins both kernels' runs of ``tests/fixtures/exact-counts`` to ``tests/golden/exact-counts/``.

The bundle is four related tables, ``product``, ``account``, ``invoice`` and ``invoice_line``,
joined by inner many-to-one relationships. It declares none of ADR 0016's keys (``counts``,
``allocation``, ``sampling``), so it is the bundle a contract had before those keys existed. It is
planned with ``fluxsim plan --kernel 0.1.0`` and with ``--kernel 0.2.0``, each applied as written:

* ``without-counts/kernel-0.1.0/`` and ``without-counts/kernel-0.2.0/`` hold each run's masked
  run digest, row counts and replay record. Their run ids, run digests and ``emits`` digests are
  **the ones origin/main (1c317c0) gives for the same bundle under that kernel**: they were
  computed by running the bundle on origin/main's code, and
  ``test_the_pinned_sets_are_what_origin_main_gives`` holds the pin to those values, so a change
  that moves them is a bundle that no longer replays. Every later change is held to them, with
  ``--jobs 1`` and ``--jobs 4``.

Regenerate with ``python tests/golden/exact-counts/_generate_golden.py`` and add a
``GOLDEN-CHANGELOG.md`` row for each set that moved in the same commit;
``test_a_golden_change_requires_a_changelog_row`` enforces the rows.
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
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "exact-counts"
GOLDEN_ROOT = Path(__file__).resolve().parent / "exact-counts"
CHANGELOG_PATH = REPO_ROOT / "GOLDEN-CHANGELOG.md"
EXPOSE_IDS = ("product", "account", "invoice", "invoice_line")
KERNELS = ("0.1.0", "0.2.0")

#: What origin/main gives for ``tests/fixtures/exact-counts`` planned and applied with each
#: ``--kernel``, from running that bundle on origin/main's code (1c317c0).
MAIN_RUN_ID = {"0.1.0": "a0fb975af9251f1d", "0.2.0": "503e4434e2f0537f"}
MAIN_RUN_DIGEST = {
    "0.1.0": "sha256:0b6ef0c8d017d45e41fbaff78f43cfdbf490a5e762c865d12248fca905a92d77",
    "0.2.0": "sha256:20e5cb01a85c900bcccb19daff812343f5334d96841a2b3603d8271e9cb3f13a",
}
MAIN_EMITS = {
    "0.1.0": {
        "account": "sha256:a25f2f8cddc31710eeb1ead1a5af64d8f2e61146da826fade1f5a4544cf9bb1f",
        "invoice": "sha256:d7ff23d781ae7f7c658fad7f5f59a69d1ce155420df5bffd3514f182dce45aa9",
        "invoice_line": "sha256:b1135b24dc55056922c68bd8cf71aba8f31c55abe9aa66ae1398f94f71ba935b",
        "product": "sha256:4f24b70254ea69faa616f6b75d5c92f7ccf107dee1520bbce89e1d87cb94a958",
    },
    "0.2.0": {
        "account": "sha256:6a14dd7302fb3ab7cfc5bc7d3a964efbc508ec4263e71e0db3bcc1b9208d2828",
        "invoice": "sha256:883f0318ab40c90caea835e9523194dbe36254fbb5abc90bab8651d6c1171ddf",
        "invoice_line": "sha256:0f553c89daf28a512badfa365bbbf65f8c3833c45be0ac887333f8529c48d7ec",
        "product": "sha256:4c48798d389c3c0aecf08db255c7cb62128bac7abbc885c64e0d4537c11a2384",
    },
}

pytestmark = pytest.mark.integration


def _golden_dir(kernel: str) -> Path:
    return GOLDEN_ROOT / "without-counts" / f"kernel-{kernel}"


def _read_golden(kernel: str, name: str) -> Dict[str, Any]:
    return json.loads((_golden_dir(kernel) / name).read_text(encoding="utf-8"))


def _run_digest_value_of(document: Dict[str, Any]) -> str:
    return digest_of_document(document, subject="runDigest").value


def _run(state_dir: Path, kernel: str, *, jobs: int) -> RunLayout:
    planned = plan(FIXTURE_DIR, state_dir=state_dir, kernel=kernel, jobs=jobs)
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


@pytest.fixture(scope="module", params=KERNELS)
def kernel(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("golden-exact-counts-state") / ".flux"


@pytest.fixture(scope="module")
def runs(state_dir: Path) -> Dict[str, RunLayout]:
    return {kernel: _run(state_dir, kernel, jobs=1) for kernel in KERNELS}


@pytest.fixture(scope="module")
def parallel_runs(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, RunLayout]:
    state_dir = tmp_path_factory.mktemp("golden-exact-counts-jobs4-state") / ".flux"
    return {kernel: _run(state_dir, kernel, jobs=4) for kernel in KERNELS}


def test_the_runs_are_the_kernels_they_were_planned_with(
    runs: Dict[str, RunLayout], kernel: str
) -> None:
    document = json.loads(runs[kernel].plan_path.read_text(encoding="utf-8"))
    expected_tree = "flux-seed/2" if kernel == "0.2.0" else "flux-seed/1"
    assert (document["kernelVersion"], document["seedTreeVersion"]) == (kernel, expected_tree)


def test_run_digest_matches_the_pinned_baseline(runs: Dict[str, RunLayout], kernel: str) -> None:
    actual = json.loads(runs[kernel].run_digest_path.read_text(encoding="utf-8"))
    pinned = _read_golden(kernel, "run-digest.json")
    assert mask_document(actual, self_referential=SELF_REFERENTIAL_FIELDS) == pinned
    assert pinned["kernelVersion"] == kernel
    assert sorted(pinned["components"]["emits"]) == sorted(EXPOSE_IDS)


def test_every_table_emits_the_pinned_row_count(runs: Dict[str, RunLayout], kernel: str) -> None:
    pinned = _read_golden(kernel, "row-count.json")
    for expose_id in EXPOSE_IDS:
        lines = runs[kernel].emit_path(expose_id).read_text(encoding="utf-8").splitlines()
        assert sum(1 for line in lines if line) == pinned[f"emit-{expose_id}"] > 0


def test_the_run_id_and_run_digest_are_the_pinned_ones(
    runs: Dict[str, RunLayout], kernel: str
) -> None:
    pinned = _read_golden(kernel, "replay.json")
    assert runs[kernel].run_id == pinned["runId"]
    document = json.loads(runs[kernel].run_digest_path.read_text(encoding="utf-8"))
    assert _run_digest_value_of(document) == pinned["runDigest"]


def test_replay_reproduces_the_pinned_run_digest(
    runs: Dict[str, RunLayout], state_dir: Path, kernel: str
) -> None:
    replayed = replay(
        FIXTURE_DIR,
        run_id=runs[kernel].run_id,
        assert_digest=_read_golden(kernel, "replay.json")["runDigest"],
        state_dir=state_dir,
    )
    assert replayed.success, replayed.stderr


def test_four_jobs_give_the_pinned_run_digest(
    parallel_runs: Dict[str, RunLayout], kernel: str
) -> None:
    """``--jobs 4`` plans and applies to the run id, run digest and rows ``--jobs 1`` gives."""
    layout = parallel_runs[kernel]
    pinned = _read_golden(kernel, "replay.json")
    assert layout.run_id == pinned["runId"]
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    assert _run_digest_value_of(document) == pinned["runDigest"]
    assert mask_document(document, self_referential=SELF_REFERENTIAL_FIELDS) == _read_golden(
        kernel, "run-digest.json"
    )


@pytest.mark.parametrize("kernel", KERNELS)
def test_the_pinned_sets_are_what_origin_main_gives(kernel: str) -> None:
    """The bundle as written replays to the digests origin/main (1c317c0) gives under each kernel."""
    pinned = _read_golden(kernel, "run-digest.json")
    assert _read_golden(kernel, "replay.json") == {
        "runDigest": MAIN_RUN_DIGEST[kernel],
        "runId": MAIN_RUN_ID[kernel],
    }
    assert pinned["components"]["emits"] == MAIN_EMITS[kernel]


@pytest.mark.parametrize("kernel", KERNELS)
def test_the_pinned_baselines_have_a_changelog_row(kernel: str) -> None:
    digest = _run_digest_value_of(_read_golden(kernel, "run-digest.json"))
    assert digest in CHANGELOG_PATH.read_text(encoding="utf-8"), (
        f"{CHANGELOG_PATH.name} has no row for {digest} -- "
        f"tests/golden/exact-counts/without-counts/kernel-{kernel}/run-digest.json "
        "changed with no changelog row"
    )


def test_a_golden_change_requires_a_changelog_row() -> None:
    """Move one table's digest and prove the gate turns red for it."""
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    pinned = _read_golden("0.2.0", "run-digest.json")
    moved = json.loads(json.dumps(pinned))
    moved["components"]["emits"]["invoice_line"] = "sha256:" + "ab" * 32
    assert _run_digest_value_of(moved) != _run_digest_value_of(pinned)
    assert _run_digest_value_of(moved) not in changelog
    assert _run_digest_value_of(pinned) in changelog
