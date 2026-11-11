# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for flux_engine.calibrate.declared."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.api.calibration import Calibrator, Credibility, Provenance
from flux_engine.calibrate.declared import (
    DECLARED_METHOD_ID,
    DeclaredCalibrator,
    max_seam_credibility,
    require_min_credibility,
    scorecard_from_playback,
)
from flux_engine.core.errors import FluxError

PLAYBACK_WITH_SCORECARD = {
    "kind": "Playback",
    "id": "pb-billing-history",
    "spec": {
        "worldRef": "q3-retention-world",
        "scorecard": {
            "backtestWindow": "P90D",
            "grade": "B",
            "intervalCoverage": 0.92,
            "credibility": 0.78,
        },
    },
}

PLAYBACK_NO_SCORECARD = {
    "kind": "Playback",
    "id": "pb-no-scorecard",
    "spec": {"worldRef": "q3-retention-world"},
}


@pytest.mark.unit
def test_declared_calibrator_satisfies_the_api_protocol() -> None:
    assert isinstance(DeclaredCalibrator(playback=PLAYBACK_WITH_SCORECARD), Calibrator)


@pytest.mark.unit
def test_scorecard_from_playback_never_invents_a_grade() -> None:
    scorecard = scorecard_from_playback(PLAYBACK_WITH_SCORECARD)
    assert scorecard.grade is None
    assert scorecard.provenance is Provenance.DECLARED
    assert scorecard.credibility is Credibility.DECLARED
    assert scorecard.method_id == DECLARED_METHOD_ID


@pytest.mark.unit
def test_scorecard_from_playback_reads_interval_coverage_and_backtest_window() -> None:
    scorecard = scorecard_from_playback(PLAYBACK_WITH_SCORECARD)
    assert scorecard.interval_coverage == Decimal("0.92")
    assert scorecard.backtest_window == "P90D"


@pytest.mark.unit
def test_scorecard_from_playback_evidence_digest_covers_the_whole_authored_block() -> None:
    scorecard = scorecard_from_playback(PLAYBACK_WITH_SCORECARD)
    assert scorecard.evidence_digest is not None
    assert scorecard.evidence_digest.value.startswith("sha256:")

    # Changing an authored field (the letter grade -- never surfaced on
    # Scorecard.grade) still moves the digest: the digest covers the
    # whole authored block, not only the fields Scorecard re-typed.
    changed = {
        **PLAYBACK_WITH_SCORECARD,
        "spec": {
            **PLAYBACK_WITH_SCORECARD["spec"],
            "scorecard": {**PLAYBACK_WITH_SCORECARD["spec"]["scorecard"], "grade": "A"},
        },
    }
    other = scorecard_from_playback(changed)
    assert other.evidence_digest is not None
    assert other.evidence_digest.value != scorecard.evidence_digest.value


@pytest.mark.unit
def test_scorecard_from_playback_refuses_to_invent_a_grade_when_absent() -> None:
    scorecard = scorecard_from_playback(PLAYBACK_NO_SCORECARD)
    assert scorecard.grade is None
    assert scorecard.credibility is Credibility.NOT_ESTABLISHED
    assert scorecard.evidence_digest is None
    assert scorecard.interval_coverage is None
    assert scorecard.backtest_window is None


@pytest.mark.unit
def test_scorecard_from_playback_handles_a_playback_with_no_spec() -> None:
    scorecard = scorecard_from_playback({"kind": "Playback", "id": "pb-empty"})
    assert scorecard.credibility is Credibility.NOT_ESTABLISHED


@pytest.mark.unit
def test_declared_calibrator_returns_dials_unchanged() -> None:
    calibrator = DeclaredCalibrator(playback=PLAYBACK_WITH_SCORECARD)
    dials = {"engagement": Decimal("0.4")}
    settings, scorecard = calibrator.calibrate({"some": "aggregate"}, dials)
    assert settings == dials
    assert settings is not dials  # a fresh mapping, never the caller's own
    assert scorecard.provenance is Provenance.DECLARED


@pytest.mark.unit
def test_max_seam_credibility_takes_the_maximum_over_matching_playbacks() -> None:
    other = {
        "kind": "Playback",
        "id": "pb-other",
        "spec": {
            "worldRef": "q3-retention-world",
            "scorecard": {"grade": "A", "credibility": 0.95},
        },
    }
    docs = {"pb-billing-history": PLAYBACK_WITH_SCORECARD, "pb-other": other}
    assert max_seam_credibility(docs, world_ref="q3-retention-world") == Decimal("0.95")


@pytest.mark.unit
def test_max_seam_credibility_ignores_playbacks_for_a_different_world() -> None:
    docs = {"pb-billing-history": PLAYBACK_WITH_SCORECARD}
    assert max_seam_credibility(docs, world_ref="some-other-world") is None


@pytest.mark.unit
def test_max_seam_credibility_ignores_non_playback_documents() -> None:
    docs = {"sim-1": {"kind": "Simulation", "id": "sim-1", "spec": {}}}
    assert max_seam_credibility(docs, world_ref="q3-retention-world") is None


@pytest.mark.unit
def test_require_min_credibility_passes_when_the_gate_is_met() -> None:
    docs = {"pb-billing-history": PLAYBACK_WITH_SCORECARD}
    best = require_min_credibility(docs, world_ref="q3-retention-world", min_credibility=0.7)
    assert best == Decimal("0.78")


@pytest.mark.unit
def test_require_min_credibility_raises_flx_7010_when_no_playback_calibrates_the_world() -> None:
    with pytest.raises(FluxError) as excinfo:
        require_min_credibility({}, world_ref="q3-retention-world", min_credibility=0.7)
    assert excinfo.value.code.code == "FLX-7010"


@pytest.mark.unit
def test_require_min_credibility_raises_flx_7010_when_credibility_is_too_low() -> None:
    docs = {"pb-billing-history": PLAYBACK_WITH_SCORECARD}
    with pytest.raises(FluxError) as excinfo:
        require_min_credibility(docs, world_ref="q3-retention-world", min_credibility=0.9)
    assert excinfo.value.code.code == "FLX-7010"


@pytest.mark.property
@given(st.decimals(min_value="0", max_value="1", allow_nan=False, places=2))
def test_declared_scorecard_grade_is_always_none_regardless_of_authored_credibility(
    credibility: Decimal,
) -> None:
    playback = {
        "kind": "Playback",
        "id": "pb-property",
        "spec": {"worldRef": "w", "scorecard": {"grade": "A", "credibility": float(credibility)}},
    }
    scorecard = scorecard_from_playback(playback)
    assert scorecard.grade is None


@pytest.mark.unit
def test__decimal_accepts_decimal_int_float_and_string() -> None:
    from flux_engine.calibrate.declared import _decimal

    assert _decimal(Decimal("1.5"), subject="x") == Decimal("1.5")
    assert _decimal(2, subject="x") == Decimal(2)
    assert _decimal(0.5, subject="x") == Decimal("0.5")
    assert _decimal("0.25", subject="x") == Decimal("0.25")


@pytest.mark.unit
def test__decimal_rejects_a_bool() -> None:
    from flux_engine.calibrate.declared import _decimal

    with pytest.raises(TypeError, match="got bool"):
        _decimal(True, subject="x")


@pytest.mark.unit
def test__decimal_rejects_an_unsupported_type() -> None:
    from flux_engine.calibrate.declared import _decimal

    with pytest.raises(TypeError, match="expected a number"):
        _decimal(None, subject="x")


@pytest.mark.unit
def test_scorecard_from_playback_rejects_a_non_string_backtest_window() -> None:
    playback = {
        "kind": "Playback",
        "id": "pb-bad",
        "spec": {"worldRef": "w", "scorecard": {"credibility": 0.5, "backtestWindow": 90}},
    }
    with pytest.raises(TypeError, match="backtestWindow"):
        scorecard_from_playback(playback)
