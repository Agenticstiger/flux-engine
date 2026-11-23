# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle that declares no ``conditions`` replays to the digests the engine gave it before they existed.

``tests/fixtures/conditions`` is planned and applied once under each kernel. The pinned run ids,
run digests and emit digests below are **the ones the engine gave for the same bundle before
any code of conditioned rows existed**. They were computed by extracting the ``flux_engine``
package of a commit from before that code (``git archive <commit> flux_engine``), putting it
first on ``PYTHONPATH``, and running ``plan`` and ``apply`` on the fixture with ``--kernel
0.1.0`` and ``--kernel 0.2.0``; the run digest is the ``jcs-document`` digest of
``run-digest.json``. A change that moves them is a bundle that no longer replays.

The same bundle with groups declared is another run: its ``H`` and so its seed moved, and its rows
hold the groups' values.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.replay import replay
from tests.seam.test_conditions_block import TWO_GROUPS, bundle_with

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "conditions"

BASE = {
    "0.1.0": {
        "runId": "57aed5936c924307",
        "runDigest": "sha256:f0781abb5b68d32cac621d842c5bb4c92e9c4d436fb0b62af620a728b4e91b4d",
        "emit": "sha256:286a13c96572de8a9ec874ab553e07a0858622c41cf3915d86280d2146d39c2b",
    },
    "0.2.0": {
        "runId": "cf53312ca45f1667",
        "runDigest": "sha256:d8bba92318e50d8c3b00b5f56993ea37ab9b162e371ca92caae5b316463afadc",
        "emit": "sha256:11cf35c9b267f2e09ae0ed1eca9552ccc5cccc953e82adc2fdbbc028b9442df4",
    },
}

pytestmark = pytest.mark.integration


def _run(bundle: Path, state: Path, kernel: str) -> RunLayout:
    planned = plan(bundle, state_dir=state, kernel=kernel)
    assert planned.success, planned.stderr
    applied = apply(
        bundle, run_id=str(planned.artifacts["run_id"]), state_dir=state, auto_plan=False
    )
    assert applied.success, applied.stderr
    return RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))


@pytest.fixture(scope="module", params=sorted(BASE))
def kernel(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def plain(tmp_path_factory: pytest.TempPathFactory, kernel: str) -> Dict[str, Any]:
    root = tmp_path_factory.mktemp(f"golden-conditions-{kernel}")
    bundle = root / "bundle"
    shutil.copytree(FIXTURE_DIR, bundle)
    layout = _run(bundle, root / ".flux", kernel)
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    return {"layout": layout, "document": document, "bundle": bundle, "state": root / ".flux"}


def test_a_bundle_without_conditions_keeps_the_digests_it_had(
    plain: Dict[str, Any], kernel: str
) -> None:
    pinned = BASE[kernel]
    assert plain["layout"].run_id == pinned["runId"]
    assert digest_of_document(plain["document"], subject="runDigest").value == pinned["runDigest"]
    assert plain["document"]["components"]["emits"] == {"subscriptions": pinned["emit"]}
    assert plain["document"]["kernelVersion"] == kernel


def test_it_replays_to_the_pinned_run_digest(plain: Dict[str, Any], kernel: str) -> None:
    replayed = replay(
        plain["bundle"],
        run_id=plain["layout"].run_id,
        assert_digest=BASE[kernel]["runDigest"],
        state_dir=plain["state"],
    )
    assert replayed.success, replayed.stderr


def test_declaring_groups_makes_another_run_under_kernel_0_2_0(
    tmp_path: Path, plain: Dict[str, Any]
) -> None:
    layout = _run(bundle_with(tmp_path, TWO_GROUPS), tmp_path / ".flux", "0.2.0")
    document = json.loads(layout.run_digest_path.read_text(encoding="utf-8"))
    assert document["components"]["emits"] != {"subscriptions": BASE["0.2.0"]["emit"]}
    assert layout.run_id != BASE["0.2.0"]["runId"]
    lines = [
        json.loads(line)
        for line in layout.emit_path("subscriptions").read_text("utf-8").splitlines()
        if line
    ]
    assert len(lines) == 1000
