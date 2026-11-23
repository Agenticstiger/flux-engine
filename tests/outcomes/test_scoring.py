# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Scoring every forecast against the next state (ADR 0029 section 5).

Pinned here: the interval score and the absolute error, against hand-computed values on both
tails; that every forecast registered for the bundle takes a verdict and one registered for
another bundle takes none; the observation, read as the next state's ``true`` count over its
``k``; a withheld cell, or a ``k`` of zero, left not scored with its reason; ``FLX-7001`` for an
observation before the registration, with nothing written; ``FLX-7143`` for a next state that is
the pinned state; ``FLX-7144`` for a store with no forecast; the basis of the item; the grades of
both states; that a verdict written before these members reads as it did; and ``fluxsim forecast
score`` on the fixture.
"""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_engine.accuracy.store import AccuracyStore
from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict
from flux_engine.api.claims import ClaimBasis
from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.evidence.grades import grade_profile
from flux_engine.outcomes.forecast import forecast_make
from flux_engine.outcomes.scoring import (
    FLX_7143,
    FLX_7144,
    absolute_error,
    forecast_score,
    interval_score,
    score_forecasts,
)
from flux_engine.seam.profile import load_profile
from tests.outcomes.support import (
    BUNDLE,
    PROFILE,
    make_bundle,
    namespace,
    next_state,
)

pytestmark = pytest.mark.unit

REGISTERED = "2026-07-01T00:00:00Z"
HORIZON = "2026-09-30T23:59:59Z"
OBSERVED = "2026-10-01T00:00:00Z"
STATE_HEX = "817592e3a2233384"
PREMIUM = f"premium-offered.{STATE_HEX}"
BASIC = f"basic-control.{STATE_HEX}"
Q_OFFERED = "renewed-offered"
Q_CONTROL = "renewed-not-offered"


# ------------------------------------------------------------------ the two scores


def _alpha(text: str) -> Fraction:
    return 1 - Fraction(text)


def test_an_observation_inside_the_interval_scores_its_width() -> None:
    score = interval_score(Decimal("0.6"), Decimal("0.8"), Decimal("0.7"), _alpha("0.9"))
    assert score == Decimal("0.2")


def test_an_observation_below_the_interval_adds_two_over_alpha_times_the_gap() -> None:
    # 0.8 - 0.6 + (2 / 0.1)(0.6 - 0.5) = 0.2 + 20 * 0.1
    score = interval_score(Decimal("0.6"), Decimal("0.8"), Decimal("0.5"), _alpha("0.9"))
    assert score == Decimal("2.2")


def test_an_observation_above_the_interval_adds_two_over_alpha_times_the_gap() -> None:
    # 0.8 - 0.6 + (2 / 0.1)(0.95 - 0.8) = 0.2 + 20 * 0.15
    score = interval_score(Decimal("0.6"), Decimal("0.8"), Decimal("0.95"), _alpha("0.9"))
    assert score == Decimal("3.2")


def test_the_penalty_follows_alpha() -> None:
    # alpha 0.5: 2 / alpha = 4, so 0.2 + 4 * 0.1; alpha 0.05: 2 / alpha = 40, so 0.2 + 40 * 0.1
    assert interval_score(Decimal("0.6"), Decimal("0.8"), Decimal("0.5"), _alpha("0.5")) == Decimal(
        "0.6"
    )
    assert interval_score(
        Decimal("0.6"), Decimal("0.8"), Decimal("0.5"), _alpha("0.95")
    ) == Decimal("4.2")


def test_an_observation_on_an_end_of_the_interval_is_inside() -> None:
    for x in ("0.6", "0.8"):
        assert interval_score(Decimal("0.6"), Decimal("0.8"), Decimal(x), _alpha("0.9")) == Decimal(
            "0.2"
        )


def test_a_penalty_that_is_not_a_terminating_decimal_is_written_in_the_kernels_context() -> None:
    # alpha 0.7: 2 / alpha = 20/7, and 0.2 + (20/7)(0.1) = 0.4857142857...
    score = interval_score(Decimal("0.6"), Decimal("0.8"), Decimal("0.5"), _alpha("0.3"))
    assert score == Decimal("0.4857142857142857142857142857")


def test_alpha_is_between_zero_and_one() -> None:
    for alpha in (Fraction(0), Fraction(1)):
        with pytest.raises(ValueError):
            interval_score(Decimal("0.6"), Decimal("0.8"), Decimal("0.7"), alpha)


def test_the_absolute_error_is_the_gap_between_observation_and_forecast() -> None:
    assert absolute_error(Decimal("0.25"), Decimal("0.75")) == Decimal("0.5")
    assert absolute_error(Decimal("0.75"), Decimal("0.25")) == Decimal("0.5")
    assert absolute_error(Decimal("0.5"), Decimal("0.5")) == Decimal("0")


# ------------------------------------------------------------------ the fixture's forecasts


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> Path:
    state = tmp_path_factory.mktemp("scoring") / "state"
    result = forecast_make(
        BUNDLE,
        registered_at=REGISTERED,
        horizon_end=HORIZON,
        state_dir=state,
        profile=PROFILE,
    )
    assert result.success, result.stderr
    return state


@pytest.fixture()
def state(made: Path, tmp_path: Path) -> Path:
    """A copy of the registered store, so a test's verdicts reach no other test."""
    copy = tmp_path / "state"
    shutil.copytree(made, copy)
    return copy


def _score(state: Path, following: Path, **changes: Any) -> Any:
    options: Dict[str, Any] = {
        "profile": PROFILE,
        "next_profile": following,
        "observed_at": OBSERVED,
        "state_dir": state,
    }
    options.update(changes)
    bundle = options.pop("bundle", BUNDLE)
    return score_forecasts(bundle, **options)


def _by_id(report: Any) -> Dict[str, Any]:
    return {item.prediction_id: item for item in report.scores}


def _bounds(state: Path, name: str) -> Tuple[Fraction, Fraction, Fraction]:
    document = AccuracyStore.for_state_dir(state).read_prediction(name)
    assert document is not None
    return (
        Fraction(document["interval_low"]),
        Fraction(document["interval_high"]),
        Fraction(document["predicted"]),
    )


def test_every_registered_forecast_takes_a_verdict(state: Path, tmp_path: Path) -> None:
    following = next_state(tmp_path)
    report = _score(state, following)
    assert set(_by_id(report)) == {PREMIUM, BASIC}
    store = AccuracyStore.for_state_dir(state)
    for name in (PREMIUM, BASIC):
        assert store.read_verdict(name) is not None


def test_the_observation_is_the_true_count_over_the_cells_k(state: Path, tmp_path: Path) -> None:
    following = next_state(
        tmp_path, cells={(Q_OFFERED, "premium"): (60, 120), (Q_CONTROL, "basic"): (100, 50)}
    )
    scores = _by_id(_score(state, following))
    assert scores[PREMIUM].observed == Decimal("0.6666666666666666666666666667")
    assert scores[PREMIUM].sample_size == 180
    assert scores[BASIC].observed == Decimal("0.3333333333333333333333333333")
    assert scores[BASIC].sample_size == 150
    verdict = AccuracyStore.for_state_dir(state).read_verdict(PREMIUM)
    assert verdict is not None
    assert verdict["observed"] == "0.6666666666666666666666666667"
    assert verdict["sampleSize"] == 180
    assert verdict["observedAt"] == OBSERVED


def test_inside_the_interval_scores_the_width_and_the_error(state: Path, tmp_path: Path) -> None:
    following = next_state(tmp_path, cells={(Q_OFFERED, "premium"): (60, 120)})
    scores = _by_id(_score(state, following))
    low, high, forecast = _bounds(state, PREMIUM)
    x = Fraction(120, 180)
    assert low <= x <= high
    assert scores[PREMIUM].outcome is AccuracyOutcome.WITHIN_INTERVAL
    assert scores[PREMIUM].interval_score is not None
    assert abs(Fraction(scores[PREMIUM].interval_score) - (high - low)) < Fraction(1, 10**26)
    assert scores[PREMIUM].absolute_error is not None
    assert abs(Fraction(scores[PREMIUM].absolute_error) - abs(x - forecast)) < Fraction(1, 10**26)


def test_below_the_interval_is_a_miss_with_the_lower_tail_penalty(
    state: Path, tmp_path: Path
) -> None:
    following = next_state(tmp_path, cells={(Q_CONTROL, "basic"): (120, 30)})
    scores = _by_id(_score(state, following))
    low, high, forecast = _bounds(state, BASIC)
    x = Fraction(30, 150)
    assert x < low
    assert scores[BASIC].outcome is AccuracyOutcome.OUTSIDE_INTERVAL
    expected = (high - low) + 20 * (low - x)
    assert scores[BASIC].interval_score is not None
    assert abs(Fraction(scores[BASIC].interval_score) - expected) < Fraction(1, 10**26)
    assert scores[BASIC].absolute_error is not None
    assert abs(Fraction(scores[BASIC].absolute_error) - (forecast - x)) < Fraction(1, 10**26)


def test_above_the_interval_is_a_miss_with_the_upper_tail_penalty(
    state: Path, tmp_path: Path
) -> None:
    following = next_state(tmp_path, cells={(Q_CONTROL, "basic"): (30, 120)})
    scores = _by_id(_score(state, following))
    low, high, forecast = _bounds(state, BASIC)
    x = Fraction(120, 150)
    assert x > high
    assert scores[BASIC].outcome is AccuracyOutcome.OUTSIDE_INTERVAL
    expected = (high - low) + 20 * (x - high)
    assert scores[BASIC].interval_score is not None
    assert abs(Fraction(scores[BASIC].interval_score) - expected) < Fraction(1, 10**26)
    assert scores[BASIC].absolute_error is not None
    assert abs(Fraction(scores[BASIC].absolute_error) - (x - forecast)) < Fraction(1, 10**26)


def test_a_hit_and_a_miss_are_both_written(state: Path, tmp_path: Path) -> None:
    following = next_state(
        tmp_path, cells={(Q_OFFERED, "premium"): (60, 120), (Q_CONTROL, "basic"): (30, 120)}
    )
    _score(state, following)
    store = AccuracyStore.for_state_dir(state)
    assert store.read_verdict(PREMIUM)["outcome"] == "within-interval"  # type: ignore[index]
    assert store.read_verdict(BASIC)["outcome"] == "outside-interval"  # type: ignore[index]


def test_a_withheld_cell_is_not_scored_with_its_reason(state: Path, tmp_path: Path) -> None:
    following = next_state(tmp_path, cells={(Q_OFFERED, "premium"): None})
    scores = _by_id(_score(state, following))
    assert scores[PREMIUM].outcome is AccuracyOutcome.NOT_SCORED
    assert scores[PREMIUM].observed is None
    assert scores[PREMIUM].interval_score is None and scores[PREMIUM].absolute_error is None
    assert "withheld" in scores[PREMIUM].reason
    # The other forecast is scored, so nothing is skipped.
    assert scores[BASIC].outcome is not AccuracyOutcome.NOT_SCORED
    verdict = AccuracyStore.for_state_dir(state).read_verdict(PREMIUM)
    assert verdict is not None
    assert verdict["outcome"] == "not-scored" and "withheld" in verdict["reason"]
    assert "intervalScore" not in verdict and "observed" not in verdict


def test_a_cell_with_no_records_is_not_scored(state: Path, tmp_path: Path) -> None:
    following = next_state(tmp_path, cells={(Q_CONTROL, "basic"): (0, 0)})
    scores = _by_id(_score(state, following))
    assert scores[BASIC].outcome is AccuracyOutcome.NOT_SCORED
    assert "no records" in scores[BASIC].reason


def test_an_observation_before_the_registration_is_refused_and_nothing_is_written(
    state: Path, tmp_path: Path
) -> None:
    following = next_state(tmp_path)
    with pytest.raises(FluxError) as refused:
        _score(state, following, observed_at="2026-06-30T23:59:59Z")
    assert refused.value.code.code == "FLX-7001"
    assert AccuracyStore.for_state_dir(state).read_verdict(PREMIUM) is None
    assert AccuracyStore.for_state_dir(state).read_verdict(BASIC) is None


def test_a_withheld_cell_before_the_registration_is_refused_too(
    state: Path, tmp_path: Path
) -> None:
    following = next_state(tmp_path, cells={(Q_OFFERED, "premium"): None})
    with pytest.raises(FluxError) as refused:
        _score(state, following, observed_at="2026-06-30T23:59:59Z")
    assert refused.value.code.code == "FLX-7001"
    assert AccuracyStore.for_state_dir(state).read_verdict(PREMIUM) is None


def test_the_next_state_may_not_be_the_pinned_state(state: Path) -> None:
    with pytest.raises(FluxError) as refused:
        _score(state, PROFILE)
    assert refused.value.code is FLX_7143
    assert AccuracyStore.for_state_dir(state).read_verdict(PREMIUM) is None


def test_the_profile_is_checked_against_the_pin(state: Path, tmp_path: Path) -> None:
    other = next_state(tmp_path, "other", cells={(Q_OFFERED, "premium"): (60, 120)})
    with pytest.raises(FluxError) as refused:
        _score(state, next_state(tmp_path), profile=other)
    assert refused.value.code.code == "FLX-3110"
    with pytest.raises(FluxError) as missing:
        _score(state, next_state(tmp_path), profile=None)
    assert missing.value.code.code == "FLX-3109"


def test_a_store_with_no_forecast_for_the_bundle_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as refused:
        _score(tmp_path / "empty", next_state(tmp_path))
    assert refused.value.code is FLX_7144


def test_a_forecast_registered_for_another_bundle_takes_no_verdict(
    state: Path, tmp_path: Path
) -> None:
    store = AccuracyStore.for_state_dir(state)
    document = store.read_prediction(PREMIUM)
    assert document is not None
    from flux_engine.accuracy.store import prediction_from_document

    other = {key: value for key, value in document.items() if key != "sealDigest"} | {
        "prediction_id": f"premium-offered-b.{STATE_HEX}",
        "inputDigest": "sha256:" + "0" * 64,
    }
    store.register(prediction_from_document(other), other)
    scores = _by_id(_score(state, next_state(tmp_path)))
    assert set(scores) == {PREMIUM, BASIC}
    assert store.read_verdict(f"premium-offered-b.{STATE_HEX}") is None


def test_the_basis_of_the_verdict_is_the_items(state: Path, tmp_path: Path) -> None:
    scores = _by_id(_score(state, next_state(tmp_path)))
    assert scores[PREMIUM].basis is ClaimBasis.HISTORICAL_HOLDOUT
    assert scores[BASIC].basis is ClaimBasis.HISTORICAL_HOLDOUT


def test_a_live_ab_item_is_scored_on_that_basis(tmp_path: Path) -> None:
    def live(document: Dict[str, Any]) -> None:
        namespace(document)["forecasts"]["items"][0]["basis"] = "live-ab"

    bundle = make_bundle(tmp_path, "live", simulation=live)
    state = tmp_path / "live-state"
    made = forecast_make(
        bundle, registered_at=REGISTERED, horizon_end=HORIZON, state_dir=state, profile=PROFILE
    )
    assert made.success, made.stderr
    scores = _by_id(_score(state, next_state(tmp_path), bundle=bundle))
    assert scores[PREMIUM].basis is ClaimBasis.LIVE_AB
    assert scores[BASIC].basis is ClaimBasis.HISTORICAL_HOLDOUT
    verdict = AccuracyStore.for_state_dir(state).read_verdict(PREMIUM)
    assert verdict is not None and verdict["basis"] == "live-ab"


def test_the_verdict_states_the_grade_of_both_states(state: Path, tmp_path: Path) -> None:
    following = next_state(tmp_path)
    report = _score(state, following)
    expected = grade_profile(load_profile(PROFILE).document, pinned=True).pattern.value
    assert expected == "P1"
    assert dict(report.grades) == {"pinned": expected, "next": expected}
    verdict = AccuracyStore.for_state_dir(state).read_verdict(PREMIUM)
    assert verdict is not None
    assert verdict["grades"] == {"pinned": "P1", "next": "P1"}
    assert verdict["nextStateDigest"] == load_profile(following).digest.value


def test_a_verdict_written_before_these_members_reads_as_it_did(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path / "state")
    plain = AccuracyVerdict(
        prediction_id="p-1",
        metric_id="m-1",
        outcome=AccuracyOutcome.WITHIN_INTERVAL,
        basis=ClaimBasis.LIVE_AB,
    )
    store.write_verdict(plain)
    assert store.read_verdict("p-1") == {
        "prediction_id": "p-1",
        "metric_id": "m-1",
        "outcome": "within-interval",
        "basis": "live-ab",
    }


def test_an_extra_member_never_replaces_one_of_the_four(tmp_path: Path) -> None:
    store = AccuracyStore.for_state_dir(tmp_path / "state")
    plain = AccuracyVerdict(
        prediction_id="p-1",
        metric_id="m-1",
        outcome=AccuracyOutcome.NOT_SCORED,
        basis=ClaimBasis.NONE,
    )
    store.write_verdict(plain, {"outcome": "within-interval", "intervalScore": "1"})
    written = store.read_verdict("p-1")
    assert written is not None
    assert written["outcome"] == "not-scored" and written["intervalScore"] == "1"


def test_scoring_again_replaces_the_verdicts(state: Path, tmp_path: Path) -> None:
    _score(state, next_state(tmp_path, "one", cells={(Q_CONTROL, "basic"): (30, 120)}))
    _score(state, next_state(tmp_path, "two", cells={(Q_CONTROL, "basic"): (80, 70)}))
    verdict = AccuracyStore.for_state_dir(state).read_verdict(BASIC)
    assert verdict is not None and verdict["outcome"] == "within-interval"


def test_the_published_record_is_unchanged_and_counts_the_misses(
    state: Path, tmp_path: Path, capsys: Any
) -> None:
    following = next_state(
        tmp_path, cells={(Q_OFFERED, "premium"): (60, 120), (Q_CONTROL, "basic"): (30, 120)}
    )
    _score(state, following)
    out = tmp_path / "record.json"
    code = main(
        [
            "accuracy",
            "record",
            "--registered-at",
            REGISTERED,
            "--state-dir",
            str(state),
            "--out",
            str(out),
            "--json",
        ]
    )
    capsys.readouterr()
    assert code == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    envelope = {"kind", "schema_version", "generated_at", "generated_by"}
    body = {key: value for key, value in record.items() if key not in envelope}
    assert body["predictions_registered"] == 2
    assert body["predictions_scored"] == 2 and body["passed"] == 1
    assert set(body) == {
        "registered_at",
        "targets",
        "predictions_registered",
        "predictions_scored",
        "passed",
        "predictionDigests",
        "verdictEvidenceDigests",
    }


# ------------------------------------------------------------------ the command


def test_fluxsim_forecast_score_on_the_fixture(state: Path, tmp_path: Path, capsys: Any) -> None:
    following = next_state(
        tmp_path, cells={(Q_OFFERED, "premium"): (60, 120), (Q_CONTROL, "basic"): (30, 120)}
    )
    code = main(
        [
            "forecast",
            "score",
            str(BUNDLE),
            "--profile",
            str(PROFILE),
            "--next",
            str(following),
            "--observed-at",
            OBSERVED,
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    artifacts = out["artifacts"]
    assert artifacts["scored"] == 2
    assert artifacts["grades"] == {"pinned": "P1", "next": "P1"}
    rows = {row["predictionId"]: row for row in artifacts["verdicts"]}
    assert rows[PREMIUM]["outcome"] == "within-interval"
    assert rows[BASIC]["outcome"] == "outside-interval"
    assert Decimal(rows[BASIC]["intervalScore"]) > Decimal(rows[PREMIUM]["intervalScore"])


def test_fluxsim_forecast_score_refuses_a_next_state_that_is_the_pinned_one(
    state: Path, capsys: Any
) -> None:
    code = main(
        [
            "forecast",
            "score",
            str(BUNDLE),
            "--profile",
            str(PROFILE),
            "--next",
            str(PROFILE),
            "--observed-at",
            OBSERVED,
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["error"]["code"] == "FLX-7143"


def test_fluxsim_forecast_score_refuses_an_observation_before_the_registration(
    state: Path, tmp_path: Path, capsys: Any
) -> None:
    code = main(
        [
            "forecast",
            "score",
            str(BUNDLE),
            "--profile",
            str(PROFILE),
            "--next",
            str(next_state(tmp_path)),
            "--observed-at",
            "2026-01-01T00:00:00Z",
            "--state-dir",
            str(state),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["error"]["code"] == "FLX-7001"


def test_forecast_score_as_a_stage_result_names_the_refusal(tmp_path: Path) -> None:
    result = forecast_score(
        BUNDLE,
        profile=PROFILE,
        next_profile=next_state(tmp_path),
        observed_at="not an instant",
        state_dir=tmp_path / "state",
    )
    assert result.exit_code == 1 and result.error_code == "FLX-7005"
