# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The seam attestation publishes the one ``H`` SEAM-FIDELITY-SPEC §3 defines.

``build_seam_attestation`` called ``seam_digests(contract, emit, columns)``
and omitted the fourth positional argument, the emit's entry in
``extensions["com.agentics.flux/1"].emits``. Every other consumer --
``plan.json``, the seed root, the fidelity record -- uses the
with-extension ``H`` that ``stages/pipeline.py`` computes, so the
attestation published a digest pair no part of the engine used.

The second consequence is worse than the first: with the extension
dropped, the emit entry no longer carries the ``fidelity`` block that
``seam_reusable_digest``'s excluded-set strip operates on, so ``G ⊂ H``
collapsed to ``G == H``. Two digests that can never disagree make §8's
reuse rule ("tolerance-only edit → ``H`` moves, ``G`` unchanged → reuse
the committed statistic; anything else → ``G`` moves → new committed
state required") undecidable from the artifact that publishes them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout

REPO_ROOT = Path(__file__).resolve().parents[2]
FIDELITY_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
EXPOSE_ID = "payment_recovery_moment"

pytestmark = pytest.mark.integration


@pytest.fixture()
def applied(tmp_path: Path) -> RunLayout:
    result = apply(FIDELITY_BUNDLE, state_dir=tmp_path / ".flux")
    assert result.success, result.stderr
    return RunLayout.for_state_dir(tmp_path / ".flux", str(result.artifacts["run_id"]))


def _attestation(layout: RunLayout) -> dict:
    return json.loads(layout.seam_attestation_path(EXPOSE_ID).read_text(encoding="utf-8"))


def _planned(layout: RunLayout) -> dict:
    return json.loads(layout.plan_path.read_text(encoding="utf-8"))["emits"][EXPOSE_ID]


def test_the_attestations_h_and_g_are_the_plans(applied: RunLayout) -> None:
    """One run, one ``H``: a consumer cross-checking the two must not see a mismatch."""
    attestation = _attestation(applied)
    planned = _planned(applied)
    assert attestation["H"] == planned["H"]
    assert attestation["G"] == planned["G"]


def test_h_and_g_are_different_digests_for_an_emit_that_declares_a_block(
    applied: RunLayout,
) -> None:
    """``G ⊂ H``: for a bundle carrying tolerances, they must be able to disagree."""
    attestation = _attestation(applied)
    assert (
        attestation["H"] != attestation["G"]
    ), "H == G makes SEAM-FIDELITY-SPEC §8's reuse rule undecidable from this artifact"
    assert _planned(applied)["declaresFidelity"] is True
