# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pins both kernels' runs of ``tests/fixtures/versioned`` to ``tests/golden/versioned/``.

The bundle is a customer, a versioned subscription (a validity window and no history key) and four
exposes that read it: ``invoice``, ``subscription_current``, ``subscription_change`` and
``subscription_month``. It declares none of ADR 0017's or ADR 0027's keys (``history``, ``asOf``,
``snapshots``, ``events``, ``periods``), so it is the bundle a contract had before those keys
existed. It is planned with ``fluxsim plan --kernel 0.1.0`` and with ``--kernel 0.2.0``, each
applied as written:

* ``with-validity-window/kernel-0.1.0/`` and ``with-validity-window/kernel-0.2.0/`` hold each
  run's masked run digest, row counts and replay record. Their run ids, run digests and ``emits``
  digests are **the ones origin/main (1c317c0) gives for the same bundle under that kernel**: they
  were computed by running the bundle on origin/main's code, and
  ``test_the_pinned_sets_are_what_origin_main_gives`` holds the pin to those values, so a change
  that moves them is a bundle that no longer replays. Every later change is held to them, with
  ``--jobs 1`` and ``--jobs 4``.

Regenerate with ``python tests/golden/versioned/_generate_golden.py`` and add a
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
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "versioned"
GOLDEN_ROOT = Path(__file__).resolve().parent / "versioned"
CHANGELOG_PATH = REPO_ROOT / "GOLDEN-CHANGELOG.md"
EXPOSE_IDS = (
    "customer",
    "subscription_version",
    "invoice",
    "subscription_current",
    "subscription_change",
    "subscription_month",
)
KERNELS = ("0.1.0", "0.2.0")

#: What origin/main gives for ``tests/fixtures/versioned`` planned and applied with each
#: ``--kernel``, from running that bundle on origin/main's code (1c317c0).
MAIN_RUN_ID = {"0.1.0": "f8710a9c5e2c2619", "0.2.0": "5721b474e54a0ad2"}
MAIN_RUN_DIGEST = {
    "0.1.0": "sha256:308b00c8a3dd2091c7dbd309af9e0545b7e40780597474509b05b3afb44e7bf5",
    "0.2.0": "sha256:f255feb69d328a741c505bc5065c1406b56dc0fde37f3f3de2a5c21d18cdb020",
}
MAIN_EMITS = {
    "0.1.0": {
        "customer": "sha256:439dfad3567777f435ddab8e1dc302c1d37d7f5df4f195de5cf2c75668db4381",
        "invoice": "sha256:ceeeb77024bbae27c6b71f0864350c5019b551aa94e84b655770ffe3c68cc95a",
        "subscription_change": "sha256:6f10b919c4b6c165ddee94ca2a8960f4ef86ac907b1c1e75ed9377f99d387504",
        "subscription_current": "sha256:ba28f6747a6c90dcd6fd3eb9bbd8017abcd1d3ae582106e22ac30c92296768eb",
        "subscription_month": "sha256:0912414c560f0679cc1ac5bd4a1c6425d0951ffd0366b6fdb027d811cf11a50c",
        "subscription_version": "sha256:94cd80f2e8aa6ba3d21a9e86f96e17935e7b7129bb8ee6f7fc25114eff63f9fe",
    },
    "0.2.0": {
        "customer": "sha256:fb2a0556122d513d4666a452bc07b7b714fedce36f3e54a19e08014e59d1df5e",
        "invoice": "sha256:0b10406d40e15f32915bca8644cb1f917c0260bc3a6cd38de7884b9276fc54b1",
        "subscription_change": "sha256:f44bccbc7473aaa362a65f0faf831f6832eb94ff08e67a06a60f28b2bce31f31",
        "subscription_current": "sha256:47c50f3a216451991cec8de91a0857b173f0415d64f268dbc66e27cd8087bece",
        "subscription_month": "sha256:b8d1e6f9b3d8504363bc24f67f9cd30204195bfddcc3e10bce7362b1c9f9d38a",
        "subscription_version": "sha256:0d48d388462b4af4637eb816d052236b5638181e980a39eefbbdd3f31bf33a04",
    },
}

pytestmark = pytest.mark.integration


def _golden_dir(kernel: str) -> Path:
    return GOLDEN_ROOT / "with-validity-window" / f"kernel-{kernel}"


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
    return tmp_path_factory.mktemp("golden-versioned-state") / ".flux"


@pytest.fixture(scope="module")
def runs(state_dir: Path) -> Dict[str, RunLayout]:
    return {kernel: _run(state_dir, kernel, jobs=1) for kernel in KERNELS}


@pytest.fixture(scope="module")
def parallel_runs(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, RunLayout]:
    state_dir = tmp_path_factory.mktemp("golden-versioned-jobs4-state") / ".flux"
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
        f"{CHANGELOG_PATH.name} has no row for {digest}; "
        f"tests/golden/versioned/with-validity-window/kernel-{kernel}/run-digest.json "
        "changed with no changelog row"
    )


def test_a_golden_change_requires_a_changelog_row() -> None:
    """Move one table's digest and check that the gate turns red for it."""
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    pinned = _read_golden("0.2.0", "run-digest.json")
    moved = json.loads(json.dumps(pinned))
    moved["components"]["emits"]["subscription_month"] = "sha256:" + "ab" * 32
    assert _run_digest_value_of(moved) != _run_digest_value_of(pinned)
    assert _run_digest_value_of(moved) not in changelog
    assert _run_digest_value_of(pinned) in changelog
