# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for flux_engine.accuracy.records."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.accuracy.records import (
    GRADE_ORDER,
    bind_readout,
    clamp_grade_for_basis,
    score,
    seal_prediction,
)
from flux_engine.api.accuracy import AccuracyOutcome, ObservedReadout, PredictionRecord
from flux_engine.api.claims import ClaimBasis
from flux_engine.core.digest import DigestDomain, DigestRef, digest_of_document
from flux_engine.core.errors import FluxError

RUN_DIGEST = DigestRef(
    value="sha256:" + "ab" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="run-digest"
)


def _prediction(**overrides: object) -> PredictionRecord:
    fields = dict(
        prediction_id="pred-1",
        run_digest=RUN_DIGEST,
        metric_id="churn_rate",
        statement="churn rate will be between 0.10 and 0.20",
        predicted=Decimal("0.15"),
        horizon_end="2026-06-01T00:00:00Z",
        registered_at="2026-01-01T00:00:00Z",
        interval_low=Decimal("0.10"),
        interval_high=Decimal("0.20"),
    )
    fields.update(overrides)
    return PredictionRecord(**fields)  # type: ignore[arg-type]


def _readout(**overrides: object) -> ObservedReadout:
    fields = dict(
        prediction_id="pred-1",
        metric_id="churn_rate",
        observed=Decimal("0.15"),
        sample_size=1000,
        basis=ClaimBasis.HISTORICAL_HOLDOUT,
        observed_at="2026-06-02T00:00:00Z",
    )
    fields.update(overrides)
    return ObservedReadout(**fields)  # type: ignore[arg-type]


@pytest.mark.unit
def test_seal_prediction_is_deterministic() -> None:
    a = seal_prediction(_prediction())
    b = seal_prediction(_prediction())
    assert a.value == b.value


@pytest.mark.unit
def test_seal_prediction_changes_when_registered_at_changes() -> None:
    a = seal_prediction(_prediction())
    b = seal_prediction(_prediction(registered_at="2026-01-02T00:00:00Z"))
    assert a.value != b.value


@pytest.mark.unit
def test_seal_prediction_changes_when_any_field_changes() -> None:
    baseline = seal_prediction(_prediction())
    assert seal_prediction(_prediction(statement="different")).value != baseline.value
    assert seal_prediction(_prediction(predicted=Decimal("0.16"))).value != baseline.value


@pytest.mark.unit
def test_bind_readout_accepts_a_legitimate_readout() -> None:
    bind_readout(_prediction(), _readout())  # does not raise


@pytest.mark.unit
def test_bind_readout_rejects_a_mismatched_prediction_id() -> None:
    with pytest.raises(FluxError) as excinfo:
        bind_readout(_prediction(), _readout(prediction_id="other"))
    assert excinfo.value.code.code == "FLX-7002"


@pytest.mark.unit
def test_bind_readout_rejects_a_mismatched_metric_id() -> None:
    with pytest.raises(FluxError) as excinfo:
        bind_readout(_prediction(), _readout(metric_id="other_metric"))
    assert excinfo.value.code.code == "FLX-7002"


@pytest.mark.unit
def test_bind_readout_rejects_a_readout_that_predates_the_prediction() -> None:
    with pytest.raises(FluxError) as excinfo:
        bind_readout(_prediction(), _readout(observed_at="2025-12-31T00:00:00Z"))
    assert excinfo.value.code.code == "FLX-7001"


@pytest.mark.unit
def test_bind_readout_accepts_a_readout_observed_at_the_same_instant_as_registration() -> None:
    bind_readout(_prediction(), _readout(observed_at="2026-01-01T00:00:00Z"))  # not before == ok


@pytest.mark.unit
def test_score_within_interval() -> None:
    verdict = score(_prediction(), _readout(observed=Decimal("0.15")))
    assert verdict.outcome is AccuracyOutcome.WITHIN_INTERVAL
    assert verdict.basis is ClaimBasis.HISTORICAL_HOLDOUT
    assert verdict.evidence  # at least the sealed prediction digest


@pytest.mark.unit
def test_score_outside_interval() -> None:
    verdict = score(_prediction(), _readout(observed=Decimal("0.99")))
    assert verdict.outcome is AccuracyOutcome.OUTSIDE_INTERVAL


@pytest.mark.unit
def test_score_not_scored_when_basis_is_none() -> None:
    verdict = score(_prediction(), _readout(basis=ClaimBasis.NONE))
    assert verdict.outcome is AccuracyOutcome.NOT_SCORED


@pytest.mark.unit
def test_score_not_scored_for_a_point_prediction_with_no_interval() -> None:
    prediction = _prediction(interval_low=None, interval_high=None)
    verdict = score(prediction, _readout())
    assert verdict.outcome is AccuracyOutcome.NOT_SCORED


@pytest.mark.unit
def test_score_carries_the_readouts_source_digest_when_present() -> None:
    source_digest = digest_of_document({"n": 1000}, subject="source-aggregate")
    verdict = score(_prediction(), _readout(source_digest=source_digest))
    assert source_digest in verdict.evidence


@pytest.mark.unit
def test_score_raises_flx_7001_through_bind_readout_before_scoring() -> None:
    with pytest.raises(FluxError) as excinfo:
        score(_prediction(), _readout(observed_at="2025-01-01T00:00:00Z"))
    assert excinfo.value.code.code == "FLX-7001"


@pytest.mark.unit
def test_clamp_grade_for_basis_caps_historical_holdout_at_c() -> None:
    assert clamp_grade_for_basis("A", ClaimBasis.HISTORICAL_HOLDOUT) == "C"
    assert clamp_grade_for_basis("B", ClaimBasis.HISTORICAL_HOLDOUT) == "C"
    assert clamp_grade_for_basis("C", ClaimBasis.HISTORICAL_HOLDOUT) == "C"


@pytest.mark.unit
def test_clamp_grade_for_basis_never_raises_a_worse_grade_to_the_ceiling() -> None:
    assert clamp_grade_for_basis("D", ClaimBasis.HISTORICAL_HOLDOUT) == "D"
    assert clamp_grade_for_basis("F", ClaimBasis.HISTORICAL_HOLDOUT) == "F"


@pytest.mark.unit
def test_clamp_grade_for_basis_leaves_live_ab_uncapped() -> None:
    for grade in GRADE_ORDER:
        assert clamp_grade_for_basis(grade, ClaimBasis.LIVE_AB) == grade


@pytest.mark.unit
def test_clamp_grade_for_basis_rejects_an_unknown_grade() -> None:
    with pytest.raises(ValueError):
        clamp_grade_for_basis("Z", ClaimBasis.LIVE_AB)


@pytest.mark.property
@given(st.sampled_from(GRADE_ORDER))
def test_clamp_grade_for_basis_is_idempotent(grade: str) -> None:
    once = clamp_grade_for_basis(grade, ClaimBasis.HISTORICAL_HOLDOUT)
    twice = clamp_grade_for_basis(once, ClaimBasis.HISTORICAL_HOLDOUT)
    assert once == twice


@pytest.mark.property
@given(st.sampled_from(GRADE_ORDER), st.sampled_from(list(ClaimBasis)))
def test_clamp_grade_for_basis_never_returns_a_grade_outside_the_alphabet(
    grade: str, basis: ClaimBasis
) -> None:
    assert clamp_grade_for_basis(grade, basis) in GRADE_ORDER


@pytest.mark.unit
def test_canonical_prediction_document_is_json_ready() -> None:
    from flux_engine.accuracy.records import canonical_prediction_document

    document = canonical_prediction_document(_prediction())
    assert document["predicted"] == "0.15"
    assert document["interval_low"] == "0.10"
    assert isinstance(document["run_digest"], str)
