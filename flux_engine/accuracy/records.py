# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Sealing a prediction, binding a readout to it, and the honest comparison.

Three things live here, in the order they happen.

1. :func:`seal_prediction` digests a
   :class:`~flux_engine.api.accuracy.PredictionRecord` -- ``registered_at``
   included -- so the digest is a fingerprint of *exactly* what was
   claimed and *when*, and any later edit produces a different digest
   rather than a silently updated one.
2. :func:`bind_readout` enforces the one rule that makes a validity claim
   mean something: a readout that names events before the prediction was
   sealed is refused (``FLX-7001``), and a readout answering a different
   prediction or a different metric is refused (``FLX-7002``).
3. :func:`score` compares an interval prediction with its bound readout by
   plain containment -- not a proper scoring rule. That mechanism is
   :mod:`flux_engine.accuracy._scoring`, and it does not exist in v0.1
   (its module docstring says so). Containment is enough to answer the one
   question v0.1 needs answered: did the observed value land inside the
   interval that was declared before it was known?

:func:`clamp_grade_for_basis` is the promotion-policy primitive
:mod:`flux_engine.accuracy.policy` applies: a grade earned against a
historical holdout can never outrank one earned against a live randomised
comparison, so a basis of :attr:`~flux_engine.api.claims.ClaimBasis.HISTORICAL_HOLDOUT`
caps the grade at ``"C"`` regardless of what a future scoring mechanism
computes. The function is pure and total over the six FLUX letter grades
today so :mod:`flux_engine.accuracy._scoring` has a policy to call the day
it exists, rather than inventing one under deadline.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from flux_engine.api.accuracy import (
    AccuracyOutcome,
    AccuracyVerdict,
    ObservedReadout,
    PredictionRecord,
)
from flux_engine.api.calibration import Scorecard
from flux_engine.api.claims import ClaimBasis
from flux_engine.core.canonical import canonicalise
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.digest import DigestRef, digest_of_document
from flux_engine.core.errors import FluxError, register

__all__ = [
    "FLX_7001",
    "FLX_7002",
    "FLX_7004",
    "FLX_7005",
    "FLX_7006",
    "GRADE_ORDER",
    "bind_readout",
    "clamp_grade_for_basis",
    "score",
    "seal_prediction",
]

FLX_7001 = register(
    "FLX-7001",
    "observed readout predates its registered prediction",
    "A PredictionRecord is sealed by its registered_at instant; an "
    "ObservedReadout naming an observed_at before that instant could only "
    "have been known before the prediction was made, which makes it "
    "evidence for the prediction rather than a test of it. Register the "
    "prediction before the outcome window opens, or read the outcome "
    "again once it has.",
)

FLX_7002 = register(
    "FLX-7002",
    "observed readout does not match its claimed prediction",
    "An ObservedReadout's prediction_id or metric_id does not match the "
    "PredictionRecord it names. Scoring one metric against another's "
    "readout is the failure this refusal exists to make impossible -- "
    "readouts are bound to the exact prediction they answer, never the "
    "nearest one.",
)

FLX_7004 = register(
    "FLX-7004",
    "no prediction was registered at that registration timestamp",
    "`fluxsim accuracy record --registered-at T` publishes the outcomes-only "
    "record for the predictions sealed at T, and for those alone. No "
    "prediction under the state directory carries that registered_at, so "
    "there is nothing to publish -- and a record written anyway would name "
    "a registration window it does not cover. Register the predictions "
    "first, or pass the registered_at the predictions actually carry.",
)

FLX_7006 = register(
    "FLX-7006",
    "an input document named on the command line does not exist",
    "`fluxsim accuracy register --file` and `fluxsim accuracy score --readout` "
    "read a JSON document from a path the caller supplies. There is no file "
    "at that path -- a typo, a relative path read from a different working "
    "directory, or a document that was never written. A file that does not "
    "exist has no shape, so this is its own refusal rather than a report "
    "about fields: check the path, then re-run the same command. FLX-7005 "
    "stays for a document that is there and wrong.",
)

FLX_7005 = register(
    "FLX-7005",
    "accuracy document does not have the required shape",
    "A PredictionRecord or ObservedReadout handed to the CLI is absent, is "
    "not JSON, is missing a required field, carries a value of the wrong "
    "type, or names a value outside a closed set. Every problem found is "
    "reported together, with the legal values of each closed set, so the "
    "document can be fixed in one pass rather than one field per run. The "
    "field list is also in the flag's own --help.",
)

#: KERNEL-SPEC §9's closed grade alphabet (FLUX ``Playback.spec.scorecard.grade``),
#: best first. :func:`clamp_grade_for_basis` never introduces a value
#: outside this tuple.
GRADE_ORDER: tuple = ("A", "B", "C", "D", "E", "F")

#: FLUX RFC-05's own rule, restated as a ceiling: a historical-holdout
#: basis alone never earns better than a "C" -- the grade a live
#: randomised comparison could have earned is not on offer from a
#: backtest, however well the backtest went.
_HISTORICAL_HOLDOUT_CEILING = "C"


def _prediction_document(prediction: PredictionRecord) -> Dict[str, Any]:
    """Render *prediction* to the mapping :func:`seal_prediction` digests.

    Every field travels, ``registered_at`` included -- the sealed digest
    is a fingerprint of the whole claim, not a subset of it.
    """
    return {
        "prediction_id": prediction.prediction_id,
        "run_digest": prediction.run_digest.value,
        "metric_id": prediction.metric_id,
        "statement": prediction.statement,
        "predicted": prediction.predicted,
        "interval_low": prediction.interval_low,
        "interval_high": prediction.interval_high,
        "horizon_end": prediction.horizon_end,
        "registered_at": prediction.registered_at,
    }


def seal_prediction(prediction: PredictionRecord) -> DigestRef:
    """Return the ``jcs-document`` digest that fixes *prediction* in time.

    Two calls with an identical *prediction* return an identical digest
    (canonicalisation is a pure function of its argument); changing any
    field -- ``registered_at`` included -- changes the digest.
    """
    return digest_of_document(
        _prediction_document(prediction), subject=f"prediction:{prediction.prediction_id}"
    )


def bind_readout(prediction: PredictionRecord, readout: ObservedReadout) -> None:
    """Raise ``FLX-7002`` or ``FLX-7001`` when *readout* may not answer *prediction*.

    Returns ``None`` (raises nothing) when the readout is a legitimate
    answer to this exact prediction, observed no earlier than it was
    registered. Called before :func:`score` -- :func:`score` trusts that
    this has already passed.
    """
    if (
        readout.prediction_id != prediction.prediction_id
        or readout.metric_id != prediction.metric_id
    ):
        raise FluxError(
            FLX_7002,
            f"readout for ({readout.prediction_id!r}, {readout.metric_id!r}) does not "
            f"match prediction ({prediction.prediction_id!r}, {prediction.metric_id!r})",
        )
    registered_ms = parse_rfc3339_ms(prediction.registered_at)
    observed_ms = parse_rfc3339_ms(readout.observed_at)
    if observed_ms < registered_ms:
        raise FluxError(
            FLX_7001,
            f"prediction {prediction.prediction_id!r} registered at "
            f"{prediction.registered_at!r}, readout observed at {readout.observed_at!r}",
        )


def score(prediction: PredictionRecord, readout: ObservedReadout) -> AccuracyVerdict:
    """Compare *readout* with *prediction* by plain interval containment.

    Calls :func:`bind_readout` first, so a mismatched or premature readout
    fails the same way whether or not a caller remembered to bind it
    itself. The outcome is
    :attr:`~flux_engine.api.accuracy.AccuracyOutcome.NOT_SCORED` when
    *readout*'s basis is :attr:`~flux_engine.api.claims.ClaimBasis.NONE`
    (nothing was measured against the world) or when *prediction* declared
    no interval (a point prediction has nothing to contain the observation
    in) -- never a fabricated pass or fail.
    """
    bind_readout(prediction, readout)

    if readout.basis is ClaimBasis.NONE:
        outcome = AccuracyOutcome.NOT_SCORED
    elif prediction.interval_low is None or prediction.interval_high is None:
        outcome = AccuracyOutcome.NOT_SCORED
    elif prediction.interval_low <= readout.observed <= prediction.interval_high:
        outcome = AccuracyOutcome.WITHIN_INTERVAL
    else:
        outcome = AccuracyOutcome.OUTSIDE_INTERVAL

    evidence: Tuple[Any, ...] = (seal_prediction(prediction),)
    if readout.source_digest is not None:
        evidence = evidence + (readout.source_digest,)

    return AccuracyVerdict(
        prediction_id=prediction.prediction_id,
        metric_id=prediction.metric_id,
        outcome=outcome,
        basis=readout.basis,
        scorecard=Scorecard(),
        evidence=evidence,
    )


def clamp_grade_for_basis(
    grade: str,
    basis: ClaimBasis,
    *,
    ceiling: Optional[Mapping[str, str]] = None,
) -> str:
    """Cap *grade* (one of :data:`GRADE_ORDER`) at *basis*'s declared ceiling.

    *ceiling* maps a :class:`~flux_engine.api.claims.ClaimBasis` value to
    the best grade that basis alone may report; a basis with no entry is
    uncapped. Defaults to the one ceiling FLUX RFC-05 declares today:
    :attr:`~flux_engine.api.claims.ClaimBasis.HISTORICAL_HOLDOUT` capped at
    ``"C"``. The cap only ever lowers a grade -- a *worse* grade than the
    ceiling (further down :data:`GRADE_ORDER`) passes through unchanged,
    because the ceiling states a limit on how good the basis can make a
    result look, not a floor.
    """
    if grade not in GRADE_ORDER:
        raise ValueError(f"grade must be one of {GRADE_ORDER}, got {grade!r}")
    effective_ceiling = (
        {ClaimBasis.HISTORICAL_HOLDOUT.value: _HISTORICAL_HOLDOUT_CEILING}
        if ceiling is None
        else ceiling
    )
    cap = effective_ceiling.get(basis.value)
    if cap is None:
        return grade
    return grade if GRADE_ORDER.index(grade) >= GRADE_ORDER.index(cap) else cap


# Re-exported so a caller building a prediction document elsewhere (e.g. a
# future storage layer) canonicalises Decimal/None fields exactly the way
# seal_prediction does, without re-deriving the rule.
def canonical_prediction_document(prediction: PredictionRecord) -> Dict[str, Any]:
    """Return the canonicalised (JSON-ready) form of *prediction*'s sealed document."""
    result = canonicalise(_prediction_document(prediction))
    assert isinstance(result, dict)  # canonicalise keeps mappings mappings
    return result
