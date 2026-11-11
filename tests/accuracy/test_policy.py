# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for flux_engine.accuracy.policy."""

from __future__ import annotations

import pytest

from flux_engine.accuracy.policy import (
    PromotionPolicy,
    default_promotion_policy,
    load_promotion_policy,
    promote,
)
from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict
from flux_engine.api.calibration import Scorecard
from flux_engine.api.claims import ClaimBasis
from flux_engine.core.errors import FluxError

RUN = "run-1"


def _verdict(**overrides: object) -> AccuracyVerdict:
    fields = dict(
        prediction_id="pred-1",
        metric_id="churn_rate",
        outcome=AccuracyOutcome.WITHIN_INTERVAL,
        basis=ClaimBasis.HISTORICAL_HOLDOUT,
        scorecard=Scorecard(),
        evidence=(),
    )
    fields.update(overrides)
    return AccuracyVerdict(**fields)  # type: ignore[arg-type]


@pytest.mark.unit
def test_default_promotion_policy_caps_historical_holdout_at_c() -> None:
    policy = default_promotion_policy()
    assert policy.grade_ceiling[ClaimBasis.HISTORICAL_HOLDOUT.value] == "C"


@pytest.mark.unit
def test_default_promotion_policy_allows_live_ab_and_historical_holdout() -> None:
    policy = default_promotion_policy()
    assert ClaimBasis.LIVE_AB.value in policy.allowed_bases
    assert ClaimBasis.HISTORICAL_HOLDOUT.value in policy.allowed_bases
    assert ClaimBasis.NONE.value not in policy.allowed_bases


@pytest.mark.unit
def test_load_promotion_policy_parses_a_customer_document() -> None:
    document = {
        "policy_id": "acme.promotion/1",
        "grade_ceiling": {"historical-holdout": "D"},
        "allowed_bases": ["historical-holdout"],
    }
    policy = load_promotion_policy(document)
    assert policy.policy_id == "acme.promotion/1"
    assert policy.grade_ceiling == {"historical-holdout": "D"}
    assert policy.allowed_bases == frozenset({"historical-holdout"})


@pytest.mark.unit
def test_load_promotion_policy_defaults_grade_ceiling_and_allowed_bases_when_absent() -> None:
    policy = load_promotion_policy({"policy_id": "acme/1"})
    assert policy.grade_ceiling[ClaimBasis.HISTORICAL_HOLDOUT.value] == "C"
    assert ClaimBasis.LIVE_AB.value in policy.allowed_bases


@pytest.mark.unit
@pytest.mark.parametrize(
    "document",
    [
        {},
        {"policy_id": ""},
        {"policy_id": "  "},
        {"policy_id": "x", "grade_ceiling": "not-a-mapping"},
        {"policy_id": "x", "grade_ceiling": {"not-a-basis": "A"}},
        {"policy_id": "x", "grade_ceiling": {"historical-holdout": "Z"}},
        {"policy_id": "x", "allowed_bases": "not-a-list"},
        {"policy_id": "x", "allowed_bases": ["not-a-basis"]},
    ],
)
def test_load_promotion_policy_refuses_a_malformed_document(document: dict) -> None:
    with pytest.raises(FluxError) as excinfo:
        load_promotion_policy(document)
    assert excinfo.value.code.code == "FLX-7011"


@pytest.mark.unit
def test_load_promotion_policy_refuses_a_non_mapping_document() -> None:
    with pytest.raises(FluxError) as excinfo:
        load_promotion_policy("not-a-mapping")  # type: ignore[arg-type]
    assert excinfo.value.code.code == "FLX-7011"


@pytest.mark.unit
def test_promote_returns_the_verdict_when_allowed() -> None:
    policy = default_promotion_policy()
    verdict = _verdict()
    assert promote(verdict, policy) is verdict


@pytest.mark.unit
def test_promote_returns_none_for_not_scored() -> None:
    policy = default_promotion_policy()
    verdict = _verdict(outcome=AccuracyOutcome.NOT_SCORED, basis=ClaimBasis.NONE)
    assert promote(verdict, policy) is None


@pytest.mark.unit
def test_promote_returns_none_when_the_basis_is_not_allowed() -> None:
    policy = PromotionPolicy(
        policy_id="strict", allowed_bases=frozenset({ClaimBasis.LIVE_AB.value})
    )
    verdict = _verdict(basis=ClaimBasis.HISTORICAL_HOLDOUT)
    assert promote(verdict, policy) is None


@pytest.mark.unit
def test_promote_never_mutates_the_verdict() -> None:
    policy = default_promotion_policy()
    verdict = _verdict()
    promoted = promote(verdict, policy)
    assert promoted is not None
    assert promoted.outcome is verdict.outcome
    assert promoted.basis is verdict.basis
