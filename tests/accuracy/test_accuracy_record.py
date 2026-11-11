# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for flux_engine.accuracy.accuracy_record."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from flux_engine.accuracy.accuracy_record import (
    SCHEMA_VERSION,
    build_accuracy_record,
    write_accuracy_record,
)
from flux_engine.accuracy.records import score
from flux_engine.api.accuracy import ObservedReadout, PredictionRecord
from flux_engine.api.claims import ClaimBasis
from flux_engine.core.digest import DigestDomain, DigestRef

RUN_DIGEST = DigestRef(
    value="sha256:" + "cd" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="run-digest"
)


def _prediction(prediction_id: str, low: str, high: str) -> PredictionRecord:
    return PredictionRecord(
        prediction_id=prediction_id,
        run_digest=RUN_DIGEST,
        metric_id="churn_rate",
        statement="churn rate prediction",
        predicted=Decimal("0.15"),
        horizon_end="2026-06-01T00:00:00Z",
        registered_at="2026-01-01T00:00:00Z",
        interval_low=Decimal(low),
        interval_high=Decimal(high),
    )


def _readout(
    prediction_id: str, observed: str, basis: ClaimBasis = ClaimBasis.HISTORICAL_HOLDOUT
) -> ObservedReadout:
    return ObservedReadout(
        prediction_id=prediction_id,
        metric_id="churn_rate",
        observed=Decimal(observed),
        sample_size=1000,
        basis=basis,
        observed_at="2026-06-02T00:00:00Z",
    )


@pytest.mark.unit
def test_build_accuracy_record_counts_registered_scored_and_passed() -> None:
    hit = _prediction("pred-hit", "0.10", "0.20")
    miss = _prediction("pred-miss", "0.10", "0.20")
    unscored = _prediction("pred-unscored", "0.10", "0.20")

    verdicts = [
        score(hit, _readout("pred-hit", "0.15")),
        score(miss, _readout("pred-miss", "0.99")),
        score(unscored, _readout("pred-unscored", "0.15", basis=ClaimBasis.NONE)),
    ]

    record = build_accuracy_record(
        registered_at="2026-01-01T00:00:00Z",
        targets=["population.lifecycleMix", "population.lifecycleMix"],
        predictions=[hit, miss, unscored],
        verdicts=verdicts,
    )

    assert record["predictions_registered"] == 3
    assert record["predictions_scored"] == 2  # unscored is excluded
    assert record["passed"] == 1  # only the hit
    assert record["targets"] == ["population.lifecycleMix"]  # de-duplicated, sorted


@pytest.mark.unit
def test_build_accuracy_record_carries_no_metric_values() -> None:
    hit = _prediction("pred-hit", "0.10", "0.20")
    record = build_accuracy_record(
        registered_at="2026-01-01T00:00:00Z",
        targets=["t"],
        predictions=[hit],
        verdicts=[score(hit, _readout("pred-hit", "0.15"))],
    )
    serialised = json.dumps(record)
    assert "0.15" not in serialised
    assert "0.10" not in serialised


@pytest.mark.unit
def test_build_accuracy_record_prediction_digests_are_sorted() -> None:
    a = _prediction("pred-a", "0.10", "0.20")
    b = _prediction("pred-b", "0.10", "0.20")
    record = build_accuracy_record(
        registered_at="2026-01-01T00:00:00Z", targets=[], predictions=[b, a], verdicts=[]
    )
    assert record["predictionDigests"] == sorted(record["predictionDigests"])
    assert len(record["predictionDigests"]) == 2


@pytest.mark.unit
def test_write_accuracy_record_uses_the_standard_envelope(tmp_path) -> None:
    hit = _prediction("pred-hit", "0.10", "0.20")
    path = write_accuracy_record(
        tmp_path / "accuracy-record.json",
        registered_at="2026-01-01T00:00:00Z",
        targets=["t"],
        predictions=[hit],
        verdicts=[score(hit, _readout("pred-hit", "0.15"))],
        generated_at="2026-01-01T00:00:00.000Z",
    )
    document = json.loads(path.read_text())
    assert document["kind"] == "AccuracyRecord"
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["generated_at"] == "2026-01-01T00:00:00.000Z"
    assert document["generated_by"] == "flux-engine"


@pytest.mark.unit
def test_write_accuracy_record_is_byte_identical_across_two_calls(tmp_path) -> None:
    hit = _prediction("pred-hit", "0.10", "0.20")
    kwargs = dict(
        registered_at="2026-01-01T00:00:00Z",
        targets=["t"],
        predictions=[hit],
        verdicts=[score(hit, _readout("pred-hit", "0.15"))],
        generated_at="2026-01-01T00:00:00.000Z",
    )
    first = write_accuracy_record(tmp_path / "a.json", **kwargs)
    second = write_accuracy_record(tmp_path / "b.json", **kwargs)
    assert first.read_bytes() == second.read_bytes()
