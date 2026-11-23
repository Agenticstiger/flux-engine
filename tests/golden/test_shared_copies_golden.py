# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pins kernel 0.2.0's runs of ``tests/fixtures/shared-copies`` to ``tests/golden/shared-copies/``.

The bundle declares a ``sharedColumns`` and a ``sharedAttribute`` (ADR 0022 section 4), and each
is planned with ``fluxsim plan --kernel 0.2.0`` and applied twice:

* ``without-copies/`` is the bundle as it is written. A copy generates only where the Simulation
  declares ``copies: true``, so this run is the one a contract that already listed the two
  declarations had before copies existed. Its pinned run digest, ``emits`` digests and run id are
  **the ones origin/main (v0.9.0, 2739cae) gives for the same bundle under kernel 0.2.0**: they
  were computed by running the bundle on origin/main's code, and ``test_without_copies_is_what_origin_main_gives``
  holds the pin to those values, so a change that moves them is a bundle that no longer replays.
* ``with-copies/`` adds ``copies: true`` under the Simulation's namespace, so its accounts and
  invoices copy their parents' columns. Origin/main refuses the key, so it has no baseline there.

Regenerate with ``python tests/golden/shared-copies/_generate_golden.py`` and add a
``GOLDEN-CHANGELOG.md`` row for each set that moved in the same commit;
``test_a_golden_change_requires_a_changelog_row`` enforces the rows.
"""

from __future__ import annotations

import json
import shutil
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
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "shared-copies"
GOLDEN_ROOT = Path(__file__).resolve().parent / "shared-copies"
CHANGELOG_PATH = REPO_ROOT / "GOLDEN-CHANGELOG.md"
EXPOSE_IDS = ("customer", "account", "invoice")
KERNEL = "0.2.0"
_NAMESPACE = "  com.agentics.flux/1:\n"

#: What origin/main gives for ``tests/fixtures/shared-copies`` planned and applied with
#: ``--kernel 0.2.0``, from running that bundle on origin/main's code (2739cae).
MAIN_RUN_ID = "8390ebf68bfba5a0"
MAIN_RUN_DIGEST = "sha256:39a0aaa228415f8e0961996a7b7311e497546bd1fe70bf13db3bf34040ec338d"
MAIN_EMITS = {
    "account": "sha256:2855c01f38eb357c718a18ef53e1a27aad9ffb06544ad7ccf944cc449e970e1c",
    "customer": "sha256:8128a6ae2854daa4a2cf02e039f0a4c6a72a18090139351de38fddab1a5a43b2",
    "invoice": "sha256:820b697eb686f3059247e155747111f1031cc5a49024a903a4046462e087761c",
}

pytestmark = pytest.mark.integration


def _read_golden(variant: str, name: str) -> Dict[str, Any]:
    return json.loads((GOLDEN_ROOT / variant / name).read_text(encoding="utf-8"))


def _bundle(root: Path, *, copies: bool) -> Path:
    """The fixture, with ``copies: true`` under the namespace when *copies*."""
    target = root / ("with-copies" if copies else "without-copies")
    shutil.copytree(FIXTURE_DIR, target)
    if copies:
        path = target / "simulation.flux.yml"
        text = path.read_text(encoding="utf-8")
        assert text.count(_NAMESPACE) == 1
        path.write_text(text.replace(_NAMESPACE, _NAMESPACE + "    copies: true\n"), "utf-8")
    return target


@pytest.fixture(scope="module", params=["without-copies", "with-copies"])
def variant(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    root = tmp_path_factory.mktemp("golden-shared-copies")
    return {
        "without-copies": _bundle(root, copies=False),
        "with-copies": _bundle(root, copies=True),
    }


@pytest.fixture(scope="module")
def state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("golden-shared-copies-state") / ".flux"


@pytest.fixture(scope="module")
def runs(bundles: Dict[str, Path], state_dir: Path) -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    for name, bundle in bundles.items():
        planned = plan(bundle, state_dir=state_dir, kernel=KERNEL)
        assert planned.success, planned.stderr
        applied = apply(
            bundle,
            run_id=str(planned.artifacts["run_id"]),
            state_dir=state_dir,
            auto_plan=False,
        )
        assert applied.success, applied.stderr
        found[name] = RunLayout.for_state_dir(state_dir, str(applied.artifacts["run_id"]))
    return found


def test_the_runs_are_kernel_0_2_0s(runs: Dict[str, Any], variant: str) -> None:
    document = json.loads(runs[variant].plan_path.read_text(encoding="utf-8"))
    assert (document["kernelVersion"], document["seedTreeVersion"]) == (KERNEL, "flux-seed/2")


def test_run_digest_matches_the_pinned_baseline(runs: Dict[str, Any], variant: str) -> None:
    actual = json.loads(runs[variant].run_digest_path.read_text(encoding="utf-8"))
    pinned = _read_golden(variant, "run-digest.json")
    assert mask_document(actual, self_referential=SELF_REFERENTIAL_FIELDS) == pinned
    assert pinned["kernelVersion"] == KERNEL
    assert sorted(pinned["components"]["emits"]) == sorted(EXPOSE_IDS)


def test_every_table_emits_the_pinned_row_count(runs: Dict[str, Any], variant: str) -> None:
    pinned = _read_golden(variant, "row-count.json")
    for expose_id in EXPOSE_IDS:
        lines = runs[variant].emit_path(expose_id).read_text(encoding="utf-8").splitlines()
        assert sum(1 for line in lines if line) == pinned[f"emit-{expose_id}"] > 0


def test_the_run_id_and_run_digest_are_the_pinned_ones(runs: Dict[str, Any], variant: str) -> None:
    pinned = _read_golden(variant, "replay.json")
    layout = runs[variant]
    assert layout.run_id == pinned["runId"]
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    assert digest_of_document(document, subject="runDigest").value == pinned["runDigest"]


def test_replay_reproduces_the_pinned_run_digest(
    runs: Dict[str, Any], bundles: Dict[str, Path], state_dir: Path, variant: str
) -> None:
    replayed = replay(
        bundles[variant],
        run_id=runs[variant].run_id,
        assert_digest=_read_golden(variant, "replay.json")["runDigest"],
        state_dir=state_dir,
    )
    assert replayed.success, replayed.stderr


def test_without_copies_is_what_origin_main_gives() -> None:
    """The bundle as written replays to the digests origin/main (v0.9.0) gives under kernel 0.2.0."""
    pinned = _read_golden("without-copies", "run-digest.json")
    assert _read_golden("without-copies", "replay.json") == {
        "runDigest": MAIN_RUN_DIGEST,
        "runId": MAIN_RUN_ID,
    }
    assert pinned["components"]["emits"] == MAIN_EMITS


def test_copies_true_moves_every_table_the_copies_reach() -> None:
    """The accounts and the invoices copy a parent's column, so their rows move; so do the others'
    run seed, which reads the Simulation's ``inputDigest``."""
    without = _read_golden("without-copies", "run-digest.json")["components"]["emits"]
    asked = _read_golden("with-copies", "run-digest.json")["components"]["emits"]
    assert asked["account"] != without["account"] and asked["invoice"] != without["invoice"]


def _run_digest_of(document: Dict[str, Any]) -> str:
    return digest_of_document(document, subject="runDigest").value


@pytest.mark.parametrize("name", ["without-copies", "with-copies"])
def test_the_pinned_baselines_have_a_changelog_row(name: str) -> None:
    digest = _run_digest_of(_read_golden(name, "run-digest.json"))
    assert digest in CHANGELOG_PATH.read_text(encoding="utf-8"), (
        f"{CHANGELOG_PATH.name} has no row for {digest} -- "
        f"tests/golden/shared-copies/{name}/run-digest.json changed with no changelog row"
    )


def test_a_golden_change_requires_a_changelog_row() -> None:
    """Move one table's digest and prove the gate turns red for it."""
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")
    pinned = _read_golden("with-copies", "run-digest.json")
    moved = json.loads(json.dumps(pinned))
    moved["components"]["emits"]["invoice"] = "sha256:" + "ab" * 32
    assert _run_digest_of(moved) != _run_digest_of(pinned)
    assert _run_digest_of(moved) not in changelog
    assert _run_digest_of(pinned) in changelog
