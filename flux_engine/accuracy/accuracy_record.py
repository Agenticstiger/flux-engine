# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The published accuracy record: outcomes only, one entry per registration.

A registration is one batch of predictions fixed at the same instant --
"per registration" in the sense that the record is keyed by
``registered_at``, not by individual prediction. What is published is
deliberately thin: how many predictions went in, how many could be
scored, how many passed, and the digests that let a reader re-take every
number. No metric value, no observed value, no interval, no letter grade
travels here -- this is the record a customer can show a third party
without handing over anything the prediction or the readout actually
said, per :class:`~flux_engine.api.claims.Claim`'s scope discipline
(SEAM-FIDELITY-SPEC §6 applies the same discretion to the fidelity
record; this is its accuracy-side counterpart).

"Passed" means
:attr:`~flux_engine.api.accuracy.AccuracyOutcome.WITHIN_INTERVAL` --
never inferred from a missing readout, which is
:attr:`~flux_engine.api.accuracy.AccuracyOutcome.NOT_SCORED` and counted
separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Sequence, Union

from flux_engine.accuracy.records import seal_prediction
from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict, PredictionRecord
from flux_engine.core.artifact import write_artifact

__all__ = ["SCHEMA_VERSION", "build_accuracy_record", "write_accuracy_record"]

SCHEMA_VERSION = "accuracy-record/1"


def build_accuracy_record(
    *,
    registered_at: str,
    targets: Sequence[str],
    predictions: Sequence[PredictionRecord],
    verdicts: Sequence[AccuracyVerdict],
) -> Dict[str, Any]:
    """Build one registration's published accuracy record.

    ``predictions`` and ``verdicts`` need not be the same length -- a
    prediction with no verdict yet (its horizon has not closed) is counted
    among ``predictions_registered`` but not among ``predictions_scored``.
    Digests are sorted (KERNEL-SPEC's "iteration over any mapping is by
    sorted key" applied to a list of digests: order is a fact about the
    engine's internal iteration, not about the record).
    """
    scored = [v for v in verdicts if v.outcome is not AccuracyOutcome.NOT_SCORED]
    passed = [v for v in scored if v.outcome is AccuracyOutcome.WITHIN_INTERVAL]

    prediction_digests = sorted(seal_prediction(p).value for p in predictions)
    verdict_digests = sorted(
        {digest.value for v in verdicts for digest in v.evidence if digest is not None}
    )

    return {
        "registered_at": registered_at,
        "targets": sorted(set(targets)),
        "predictions_registered": len(predictions),
        "predictions_scored": len(scored),
        "passed": len(passed),
        "predictionDigests": prediction_digests,
        "verdictEvidenceDigests": verdict_digests,
    }


def write_accuracy_record(
    path: Union[str, Path],
    *,
    registered_at: str,
    targets: Sequence[str],
    predictions: Sequence[PredictionRecord],
    verdicts: Sequence[AccuracyVerdict],
    generated_at: str,
    generated_by: str = "flux-engine",
) -> Path:
    """Build and write one registration's accuracy record via the standard artifact envelope."""
    record = build_accuracy_record(
        registered_at=registered_at, targets=targets, predictions=predictions, verdicts=verdicts
    )
    return write_artifact(
        path,
        record,
        kind="AccuracyRecord",
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        generated_by=generated_by,
    )
