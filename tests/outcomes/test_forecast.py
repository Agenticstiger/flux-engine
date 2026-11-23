# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Forecasts for the next state, sealed in the prediction registry (ADR 0029 sections 3 and 4).

Pinned here: the replicate's effect, exact; the median and the ranks of the interval for odd and
even counts, and the refusal when too few values give a rank; the fixture's forecasts, which are
deterministic; that ``replay`` of a replicate plan reproduces its run digest; the two members a
forecast seals, and that a record written without them seals and verifies as it did; that
registering again changes nothing and a changed forecast is ``FLX-7007``; and ``fluxsim forecast
make`` on the fixture.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.accuracy.records import seal_prediction
from flux_engine.accuracy.store import AccuracyStore, prediction_from_document
from flux_engine.api.accuracy import PredictionRecord
from flux_engine.cli import main
from flux_engine.core.digest import DigestDomain, DigestRef, digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import sweep_effective_seed
from flux_engine.outcomes.forecast import (
    forecast_make,
    interval_rank,
    make_forecasts,
    summarise,
)
from flux_engine.outcomes.responses import (
    REPLICATE_PLAN_KEY,
    Replicate,
    ResponseEstimates,
    estimate_responses,
    replicate_effect,
    replicate_seed,
)
from flux_engine.seam.profile import load_profile
from flux_engine.seam.resolve import declared_responses, load_bundle
from flux_engine.stages.replay import replay
from tests.outcomes.support import (
    BUNDLE,
    PROFILE,
    SIMULATION,
    Edit,
    execute,
    make_bundle,
    namespace,
    without_responses,
)

pytestmark = pytest.mark.unit

REGISTERED = "2026-07-01T00:00:00Z"
HORIZON = "2026-09-30T23:59:59Z"
STATE_HEX = "817592e3a2233384"

#: What the fixture forecasts: the median and the interval of each item over the 20 replicates.
PINNED = {
    f"premium-offered.{STATE_HEX}": (
        "query:renewed-offered/premium",
        "0.8313008130081300813008130081",
        "0.6363636363636363636363636364",
        "1",
    ),
    f"basic-control.{STATE_HEX}": (
        "query:renewed-not-offered/basic",
        "0.4880952380952380952380952381",
        "0.3125",
        "0.6341463414634146341463414634",
    ),
}


def _make(state: Path, **changes: Any) -> Any:
    options: Dict[str, Any] = {
        "registered_at": REGISTERED,
        "horizon_end": HORIZON,
        "state_dir": state,
        "profile": PROFILE,
    }
    options.update(changes)
    bundle = options.pop("bundle", BUNDLE)
    return forecast_make(bundle, **options)


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> Any:
    state = tmp_path_factory.mktemp("forecast") / "state"
    result = _make(state)
    assert result.success, result.stderr
    return state, result


def _estimates() -> ResponseEstimates:
    loaded = load_bundle(BUNDLE)
    families, _ = declared_responses(loaded.flux_documents[SIMULATION])
    return estimate_responses(families, load_profile(PROFILE).profile)


# ------------------------------------------------------------------ the replicate's effect


def test_a_replicates_effect_is_the_midpoint_of_its_part_of_the_interval() -> None:
    low, high = Decimal("0.1"), Decimal("0.3")
    assert replicate_effect(low, high, 1, 20) == Fraction(1, 10) + Fraction(1, 5) * Fraction(1, 40)
    assert replicate_effect(low, high, 20, 20) == Fraction(3, 10) - Fraction(1, 5) * Fraction(1, 40)
    effects = [replicate_effect(low, high, r, 20) for r in range(1, 21)]
    assert effects == sorted(effects) and len(set(effects)) == 20
    assert sum(effects) / 20 == Fraction(
        1, 5
    )  # the parts are equal, so their midpoints average out
    assert all(Fraction(low) < effect < Fraction(high) for effect in effects)


def test_a_replicate_is_a_run_one_to_r() -> None:
    for r, runs in ((0, 20), (21, 20), (True, 20)):
        with pytest.raises(FluxError) as caught:
            Replicate(r, runs)  # type: ignore[arg-type]
        assert caught.value.code.code == "FLX-3673"


def test_a_replicate_without_a_family_is_refused(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, "plain", simulation=without_responses)
    with pytest.raises(FluxError) as caught:
        execute(bundle, replicate=Replicate(1, 20))
    assert caught.value.code.code == "FLX-3673"


def test_a_replicate_takes_the_sweeps_seed_and_its_effect() -> None:
    plain = execute()
    first = execute(replicate=Replicate(1, 20))
    last = execute(replicate=Replicate(20, 20))
    assert first.seed == sweep_effective_seed(plain.seed, 0)
    assert last.seed == sweep_effective_seed(plain.seed, 19)
    found = _estimates()
    assert first.replicate is not None
    effects = {(family, segment): effect for _i, family, segment, effect in first.replicate.effects}
    for estimate in found.estimates:
        assert effects[(estimate.family, estimate.segment)] == replicate_effect(
            estimate.low, estimate.high, 1, 20
        )
    assert first.components.journeys != last.components.journeys != plain.components.journeys


def _with_base_seed(base: int) -> Edit:
    def edit(document: Dict[str, Any]) -> None:
        document["spec"]["sweep"]["baseSeed"] = base

    return edit


@pytest.mark.parametrize("base", [None, 1000, -5])
def test_replicate_r_takes_the_seed_of_the_sweeps_run_r_minus_one(
    tmp_path: Path, base: Optional[int]
) -> None:
    bundle = (
        BUNDLE if base is None else make_bundle(tmp_path, "based", simulation=_with_base_seed(base))
    )
    start = execute(bundle).seed if base is None else base  # no baseSeed: the run's own seed
    for r in (1, 20):
        got = execute(bundle, replicate=Replicate(r, 20)).seed
        assert got == sweep_effective_seed(start, r - 1) == start + r - 1


@pytest.mark.parametrize("base", [None, 1000])
def test_the_replicates_use_base_seed_to_base_seed_plus_r_minus_one_and_never_base_plus_r(
    tmp_path: Path, base: Optional[int]
) -> None:
    bundle = (
        BUNDLE if base is None else make_bundle(tmp_path, "based", simulation=_with_base_seed(base))
    )
    simulation = load_bundle(bundle).flux_documents[SIMULATION]
    start = execute(bundle).seed if base is None else base
    seeds = [
        replicate_seed(simulation, execute(bundle).seed, Replicate(r, 20)) for r in range(1, 21)
    ]
    assert seeds == [sweep_effective_seed(start, k) for k in range(20)]
    assert min(seeds) == start and max(seeds) == start + 19
    assert start + 20 not in seeds


@pytest.mark.parametrize("declared", ["1000", True, 1.5])
def test_a_base_seed_that_is_not_an_integer_is_refused(declared: Any) -> None:
    simulation = {"spec": {"sweep": {"runs": 20, "baseSeed": declared}}}
    with pytest.raises(FluxError) as caught:
        replicate_seed(simulation, 42, Replicate(1, 20))
    assert caught.value.code.code == "FLX-3673"


def test_recorded_effects_are_applied_as_written_and_checked() -> None:
    first = execute(replicate=Replicate(3, 20))
    assert first.replicate is not None
    document = first.replicate.as_document()
    again = execute(replicate=Replicate.from_document(document, where="plan"))
    assert again.components == first.components
    forged = json.loads(json.dumps(document))
    del forged["effects"][0]
    with pytest.raises(FluxError) as caught:
        execute(replicate=Replicate.from_document(forged, where="plan"))
    assert caught.value.code.code == "FLX-3673"
    for broken in ({"r": 3, "R": 20}, {**document, "effects": []}, {**document, "extra": 1}):
        with pytest.raises(FluxError) as caught:
            Replicate.from_document(broken, where="plan")
        assert caught.value.code.code == "FLX-3673"
    bad = json.loads(json.dumps(document))
    bad["effects"][0]["effect"] = "0.25"
    with pytest.raises(FluxError):
        Replicate.from_document(bad, where="plan")


# ------------------------------------------------------------------ the median and its ranks


@pytest.mark.parametrize(
    ("count", "confidence", "rank"),
    [
        (19, "0.9", 0),
        (20, "0.9", 1),
        (39, "0.9", 1),
        (40, "0.9", 2),
        (10, "0.5", 2),
        (3, "0.5", 0),
        (4, "0.5", 1),
    ],
)
def test_the_rank_is_the_largest_whole_number_not_above_half_the_tail(
    count: int, confidence: str, rank: int
) -> None:
    assert interval_rank(count, Fraction(confidence)) == rank


def test_an_odd_count_takes_the_middle_value_and_the_interval_its_ranks() -> None:
    values = [
        Fraction(n, 7)
        for n in (6, 1, 5, 2, 4, 3, 0, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)
    ]
    assert len(values) == 21
    found = summarise(values, Fraction(9, 10))
    assert found.rank == 1 and found.count == 21
    assert found.forecast == Fraction(10, 7)  # rank 11 of 0 to 20 over 7
    assert (found.low, found.high) == (Fraction(0), Fraction(20, 7))


def test_an_even_count_takes_the_mean_of_the_two_middle_values() -> None:
    values = [Fraction(n, 100) for n in range(40, 0, -1)]
    found = summarise(values, Fraction(9, 10))
    assert found.rank == 2 and found.count == 40
    assert found.forecast == (Fraction(20, 100) + Fraction(21, 100)) / 2
    assert (found.low, found.high) == (Fraction(2, 100), Fraction(39, 100))


def test_too_few_values_for_a_rank_are_refused_with_a_named_code() -> None:
    with pytest.raises(FluxError) as caught:
        summarise([Fraction(n, 19) for n in range(19)], Fraction(9, 10), item="premium-offered")
    assert caught.value.code.code == "FLX-7140"
    assert "premium-offered" in str(caught.value)


# ------------------------------------------------------------------ the fixture's forecasts


def test_the_fixtures_forecasts_are_deterministic_and_pinned(made: Any, tmp_path: Path) -> None:
    state, result = made
    forecasts = result.artifacts["forecasts"]
    assert {
        item["predictionId"]: (
            item["metricId"],
            item["predicted"],
            item["intervalLow"],
            item["intervalHigh"],
        )
        for item in forecasts
    } == PINNED
    assert all(item["values"] == 20 and item["created"] for item in forecasts)
    other = _make(tmp_path / "other", jobs=4)
    assert other.success, other.stderr
    assert [item["sealDigest"] for item in other.artifacts["forecasts"]] == [
        item["sealDigest"] for item in forecasts
    ]
    assert [run["runDigest"] for run in other.artifacts["runs"]] == [
        run["runDigest"] for run in result.artifacts["runs"]
    ]


def test_each_replicate_is_a_run_of_its_own(made: Any) -> None:
    _state, result = made
    runs = result.artifacts["runs"]
    assert [run["replicate"] for run in runs] == list(range(1, 21))
    assert len({run["runDigest"] for run in runs}) == 20


# ------------------------------------------------------------------ replay


@pytest.mark.parametrize("r", [1, 9, 20])
def test_replay_of_a_replicate_plan_reproduces_its_run_digest(made: Any, r: int) -> None:
    state, result = made
    run = result.artifacts["runs"][r - 1]
    plan = json.loads((state / "runs" / run["runId"] / "plan.json").read_text(encoding="utf-8"))
    recorded = plan[REPLICATE_PLAN_KEY]
    assert (recorded["r"], recorded["R"]) == (r, 20)
    assert [item["segment"] for item in recorded["effects"]] == ["basic", "premium"]
    expected = {
        estimate.segment: replicate_effect(estimate.low, estimate.high, r, 20)
        for estimate in _estimates().estimates
    }
    assert {item["segment"]: Fraction(item["effect"]) for item in recorded["effects"]} == expected
    again = replay(BUNDLE, run_id=run["runId"], assert_digest=run["runDigest"], state_dir=state)
    assert again.success, again.stderr
    wrong = replay(BUNDLE, run_id=run["runId"], assert_digest="sha256:" + "0" * 64, state_dir=state)
    assert not wrong.success


# ------------------------------------------------------------------ the sealed record


def test_a_registered_forecast_seals_the_state_and_the_input(made: Any) -> None:
    state, result = made
    store = AccuracyStore.for_state_dir(state)
    profile = load_profile(PROFILE)
    plan = json.loads(
        (state / "runs" / result.artifacts["runs"][0]["runId"] / "plan.json").read_text("utf-8")
    )
    for prediction_id, (metric, predicted, low, high) in PINNED.items():
        document = store.read_prediction(prediction_id)  # verified against its seal
        assert document is not None
        assert document["stateDigest"] == profile.digest.value
        assert document["inputDigest"] == plan["inputDigest"]
        assert document["prediction_id"] == prediction_id
        assert prediction_id.split(".")[1] == profile.digest.value.split(":")[1][:16]
        assert (document["metric_id"], document["predicted"]) == (metric, predicted)
        assert (document["interval_low"], document["interval_high"]) == (low, high)
        assert (document["registered_at"], document["horizon_end"]) == (REGISTERED, HORIZON)
        assert isinstance(document["statement"], str) and document["statement"].endswith(".")
        prediction = prediction_from_document(document)
        assert seal_prediction(prediction).value == document["sealDigest"]
    expected_run_digest = [run["runDigest"] for run in result.artifacts["runs"]]
    document = store.read_prediction(f"basic-control.{STATE_HEX}")
    assert document is not None
    assert (
        document["run_digest"]
        == digest_of_document(expected_run_digest, subject="forecastRunDigests").value
    )


def test_a_tampered_state_or_input_digest_fails_the_seal(made: Any, tmp_path: Path) -> None:
    state, _result = made
    path = state / "accuracy" / "predictions" / f"basic-control.{STATE_HEX}.json"
    original = path.read_text(encoding="utf-8")
    for member in ("stateDigest", "inputDigest"):
        document = json.loads(original)
        document[member] = "sha256:" + "0" * 64
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(FluxError) as caught:
            AccuracyStore.for_state_dir(state).read_prediction(f"basic-control.{STATE_HEX}")
        assert caught.value.code.code == "FLX-7008"
    path.write_text(original, encoding="utf-8")


def test_a_record_written_without_the_members_seals_and_verifies_as_it_did(tmp_path: Path) -> None:
    prediction = PredictionRecord(
        prediction_id="recovery-rate-q3",
        run_digest=DigestRef(
            value="sha256:" + "ab" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="runDigest"
        ),
        metric_id="payment_recovery_rate",
        statement="Sixty-two in 100.",
        predicted=Decimal("0.62"),
        horizon_end="2026-09-30T23:59:59Z",
        registered_at="2026-06-30T09:00:00Z",
        interval_low=Decimal("0.55"),
        interval_high=Decimal("0.69"),
    )
    #: Computed with the code before the two members existed.
    assert seal_prediction(prediction).value == (
        "sha256:c5d32e704dbb8a51813fe49b18c1c18c8fd917ddda3f2e27ded3f04029396dcc"
    )
    document = {
        "prediction_id": "recovery-rate-q3",
        "run_digest": "sha256:" + "ab" * 32,
        "metric_id": "payment_recovery_rate",
        "statement": "Sixty-two in 100.",
        "predicted": "0.62",
        "interval_low": "0.55",
        "interval_high": "0.69",
        "horizon_end": "2026-09-30T23:59:59Z",
        "registered_at": "2026-06-30T09:00:00Z",
    }
    store = AccuracyStore.for_state_dir(tmp_path / "state")
    registration = store.register(prediction_from_document(document), document)
    assert registration.seal == seal_prediction(prediction)
    read = store.read_prediction("recovery-rate-q3")
    assert read is not None and "stateDigest" not in read and "inputDigest" not in read


def test_the_members_are_sealed_and_a_wrong_type_is_not_a_prediction() -> None:
    document = {
        "prediction_id": "p-1",
        "run_digest": "sha256:" + "ab" * 32,
        "metric_id": "m",
        "statement": "s",
        "predicted": "0.5",
        "horizon_end": "2026-09-30T23:59:59Z",
        "registered_at": "2026-06-30T09:00:00Z",
    }
    plain = seal_prediction(prediction_from_document(document))
    with_state = seal_prediction(prediction_from_document({**document, "stateDigest": "sha256:1"}))
    with_input = seal_prediction(prediction_from_document({**document, "inputDigest": "sha256:1"}))
    assert len({plain, with_state, with_input}) == 3
    with pytest.raises(TypeError):
        prediction_from_document({**document, "stateDigest": 1})


def test_accuracy_register_accepts_the_two_members(tmp_path: Path, capsys: Any) -> None:
    document = {
        "prediction_id": "p-1",
        "run_digest": "sha256:" + "ab" * 32,
        "metric_id": "m",
        "statement": "s",
        "predicted": "0.5",
        "horizon_end": "2026-09-30T23:59:59Z",
        "registered_at": "2026-06-30T09:00:00Z",
        "stateDigest": "sha256:" + "01" * 32,
        "inputDigest": "sha256:" + "02" * 32,
    }
    path = tmp_path / "prediction.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    code = main(["accuracy", "register", "--file", str(path), "--state-dir", str(tmp_path / "s")])
    assert code == 0, capsys.readouterr()
    record = AccuracyStore.for_state_dir(tmp_path / "s").read_prediction("p-1")
    assert record is not None and record["stateDigest"] == document["stateDigest"]


# ------------------------------------------------------------------ registering again


def test_registering_the_same_forecast_again_changes_nothing(made: Any) -> None:
    state, result = made
    before = {
        path.name: path.read_bytes() for path in (state / "accuracy" / "predictions").iterdir()
    }
    again = _make(state)
    assert again.success, again.stderr
    assert [item["created"] for item in again.artifacts["forecasts"]] == [False, False]
    assert [item["sealDigest"] for item in again.artifacts["forecasts"]] == [
        item["sealDigest"] for item in result.artifacts["forecasts"]
    ]
    after = {
        path.name: path.read_bytes() for path in (state / "accuracy" / "predictions").iterdir()
    }
    assert after == before
    assert "already registered" in again.stdout


def test_another_forecast_under_a_used_id_is_refused(made: Any) -> None:
    state, _result = made
    before = {
        path.name: path.read_bytes() for path in (state / "accuracy" / "predictions").iterdir()
    }
    changed = _make(state, registered_at="2026-07-02T00:00:00Z")
    assert not changed.success
    assert changed.error_code == "FLX-7007"
    after = {
        path.name: path.read_bytes() for path in (state / "accuracy" / "predictions").iterdir()
    }
    assert after == before


def test_a_used_id_from_another_bundle_says_so_and_names_both_bundles(
    made: Any, tmp_path: Path
) -> None:
    state, _result = made
    registered = json.loads(
        next((state / "accuracy" / "predictions").iterdir()).read_text(encoding="utf-8")
    )

    def raise_threshold(document: Dict[str, Any]) -> None:
        document["spec"]["drift"]["threshold"] = 0.3

    other = make_bundle(tmp_path, "other", documents={"playback.flux.yml": raise_threshold})
    refused = _make(state, bundle=other)
    assert refused.error_code == "FLX-7007"
    given = forecast_make(
        other,
        registered_at=REGISTERED,
        horizon_end=HORIZON,
        state_dir=tmp_path / "scratch",
        profile=PROFILE,
    )
    assert given.success, given.stderr
    other_digest = json.loads(
        next((tmp_path / "scratch" / "accuracy" / "predictions").iterdir()).read_text("utf-8")
    )["inputDigest"]
    assert other_digest != registered["inputDigest"]
    message = refused.stderr
    assert "another bundle" in message and "give the item its own id" in message
    assert registered["inputDigest"].rpartition(":")[2][:16] in message
    assert other_digest.rpartition(":")[2][:16] in message


def test_a_changed_forecast_in_the_same_bundle_does_not_blame_another_bundle(made: Any) -> None:
    state, _result = made
    changed = _make(state, registered_at="2026-07-02T00:00:00Z")
    assert changed.error_code == "FLX-7007"
    assert "another bundle" not in changed.stderr


# ------------------------------------------------------------------ refusals


def test_a_horizon_before_the_registration_is_refused(tmp_path: Path) -> None:
    refused = _make(tmp_path / "s", horizon_end="2026-06-30T00:00:00Z")
    assert refused.error_code == "FLX-7142"
    assert not (tmp_path / "s" / "accuracy").exists()


def test_an_instant_that_is_not_rfc_3339_is_refused(tmp_path: Path) -> None:
    refused = _make(tmp_path / "s", registered_at="July 1st")
    assert refused.error_code == "FLX-7005"
    assert "--registered-at" in refused.stderr


def test_a_simulation_without_forecasts_has_nothing_to_make(tmp_path: Path) -> None:
    def drop(document: Dict[str, Any]) -> None:
        namespace(document).pop("forecasts")

    refused = _make(tmp_path / "s", bundle=make_bundle(tmp_path, "none", simulation=drop))
    assert refused.error_code == "FLX-7141"


def test_a_short_sweep_is_refused_before_any_replicate_runs(tmp_path: Path) -> None:
    def short(document: Dict[str, Any]) -> None:
        document["spec"]["sweep"] = {"runs": 19}

    refused = _make(tmp_path / "s", bundle=make_bundle(tmp_path, "short", simulation=short))
    assert refused.error_code == "FLX-3670"
    assert not (tmp_path / "s" / "runs").exists()


def test_an_item_no_subject_reaches_registers_nothing(tmp_path: Path) -> None:
    """A segment no Persona is in gives no value in any replicate: nothing is registered."""

    def ghosts(document: Dict[str, Any]) -> None:
        document["spec"]["query"] = "false"

    bundle = make_bundle(tmp_path, "ghost", documents={"segment-premium.flux.yml": ghosts})
    state = tmp_path / "s"
    refused = _make(state, bundle=bundle)
    assert refused.error_code == "FLX-7140"
    assert "premium-offered" in refused.stderr
    assert not (state / "accuracy" / "predictions").exists()


def test_a_replicate_whose_stage_refuses_is_the_commands_refusal(tmp_path: Path) -> None:
    refused = _make(tmp_path / "s", profile=None)
    assert not refused.success
    assert refused.error_code == "FLX-3109"


def test_make_forecasts_returns_what_it_registered(tmp_path: Path) -> None:
    registered, runs = make_forecasts(
        BUNDLE,
        registered_at=REGISTERED,
        horizon_end=HORIZON,
        state_dir=tmp_path / "s",
        profile=PROFILE,
    )
    assert [item.prediction_id for item in registered] == list(PINNED)
    assert [run.replicate for run in runs] == list(range(1, 21))
    values: List[Any] = [run.values["premium-offered"] for run in runs]
    assert all(isinstance(value, Fraction) and 0 <= value <= 1 for value in values)


# ------------------------------------------------------------------ the command


def test_fluxsim_forecast_make_on_the_fixture(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "forecast",
            "make",
            str(BUNDLE),
            "--profile",
            str(PROFILE),
            "--registered-at",
            REGISTERED,
            "--horizon-end",
            HORIZON,
            "--state-dir",
            str(tmp_path / "state"),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["artifacts"]["replicates"] == 20
    assert {item["predictionId"] for item in out["artifacts"]["forecasts"]} == set(PINNED)
    store = AccuracyStore.for_state_dir(tmp_path / "state")
    assert all(store.read_prediction(name) is not None for name in PINNED)
    # The record's id, metric and sealed members are the ones the command printed.
    again = main(
        [
            "forecast",
            "make",
            str(BUNDLE),
            "--profile",
            str(PROFILE),
            "--registered-at",
            "2026-07-02T00:00:00Z",
            "--horizon-end",
            HORIZON,
            "--state-dir",
            str(tmp_path / "state"),
            "--json",
        ]
    )
    refused = json.loads(capsys.readouterr().out)
    assert again == 1 and refused["error"]["code"] == "FLX-7007"


def test_a_forecast_needs_the_profile_the_simulation_pins(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "forecast",
            "make",
            str(BUNDLE),
            "--registered-at",
            REGISTERED,
            "--horizon-end",
            HORIZON,
            "--state-dir",
            str(tmp_path / "state"),
            "--json",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["error"]["code"] == "FLX-3109"
