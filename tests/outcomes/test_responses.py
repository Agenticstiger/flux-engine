# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Response families in the runtime (ADR 0029 section 2).

Pinned here: the estimate each segment gets from the pinned profile's cells, and the cells that
give none; the probability an untreated and a treated subject draws against; that a subject whose
probability is unchanged, a subject in no segment and a journey the family does not name draw
exactly as before; that the register lists the estimates; that a bundle without ``responses``
keeps the digests it had on this branch's base; that ``--jobs 1`` and ``--jobs 4`` agree; and the
whole loop (plan, apply, verify, replay) on the ``outcomes`` fixture.
"""

from __future__ import annotations

import copy
import json
import shutil
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from flux_engine.cli import main
from flux_engine.core.errors import FluxError
from flux_engine.outcomes.responses import (
    RESPONSES_PLAN_KEY,
    ResponseEstimates,
    estimate_responses,
    response_rate,
)
from flux_engine.runtime.experiment import ArmCounts, ReadoutRow, TwoProportionReadout
from flux_engine.seam.profile import load_profile
from flux_engine.seam.resolve import declared_responses, load_bundle
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.replay import replay
from flux_engine.stages.run import run
from flux_engine.stages.verify import verify
from flux_stats.profile import parse_profile
from tests.outcomes.support import (
    BUNDLE,
    JOURNEY,
    NAMESPACE,
    PROFILE,
    SIMULATION,
    execute,
    make_bundle,
    namespace,
    profile_document,
    without_responses,
)

pytestmark = pytest.mark.unit

#: What a bundle without ``responses`` gave before any code of response families: the fixture's
#: bundle with its two keys removed, computed with the code of a commit from before that code.
BASE_RUN_DIGEST = "sha256:b8e13f2d7f766d887c7810145ec87860843258b6f0cf91f1a99c5f80727ad9c2"
BASE_PLAN_DIGEST = "sha256:8dc6380e2d76f7e6434f0951b54cbaa4b75d80dfd67854daf08675b63bffcbcf"
BASE_INPUT_DIGEST = "sha256:92dd587526b99238f4475c193b59f0d6cb62d85dd073a251f9ca2ff806e05c26"
BASE_COMPONENTS = {
    "population": "sha256:6b5431339664740381eec891db94623649e3edaae7eee174592909f88bdcdf87",
    "events": "sha256:4f7f28882b84f21bf9c8962fa591a5c61f7073db0b04bb45c46a898b5e150602",
    "journeys": "sha256:5bbd8bd9a1f094ae8f84b897525db2e1727104789b4af158a9acfa6c7e8d5cb2",
    "delivery": "sha256:8cb7bc3b220c4dc3133c30b0fefb2908d97bff2ac987cee033d5b3dd42af9db6",
    "assignment": "sha256:b97e3f40f044c86ef3987bdf2106846378033d496ec6667633b4ac689f1ebcf9",
}


def _families() -> Any:
    loaded = load_bundle(BUNDLE)
    families, _forecasts = declared_responses(loaded.flux_documents[SIMULATION])
    return families


def _estimate(document: Dict[str, Any]) -> ResponseEstimates:
    return estimate_responses(_families(), parse_profile(document))


def _cells(document: Dict[str, Any], query: str) -> List[Dict[str, Any]]:
    entry = next(item for item in document["queries"] if item["name"] == query)
    cells: List[Dict[str, Any]] = entry["statistics"][0]["groups"]
    return cells


def _cell(document: Dict[str, Any], query: str, plan_name: str) -> Dict[str, Any]:
    return next(cell for cell in _cells(document, query) if cell["group"] == plan_name)


def _success(result: Any) -> Dict[Tuple[int, int], bool]:
    return {(item.ordinal, item.step): item.success for item in result.transitions}


# ------------------------------------------------------------------ the estimate


def test_an_estimate_is_read_from_the_two_cells_of_a_segment() -> None:
    found = estimate_responses(_families(), load_profile(PROFILE).profile)
    assert found.unestimated == ()
    basic, premium = (found.get(0, "basic"), found.get(0, "premium"))
    assert basic is not None and premium is not None
    assert (basic.control, basic.treated) == ((150, 75), (450, 282))
    assert (premium.control, premium.treated) == ((60, 36), (180, 144))
    assert basic.p0 == Decimal("0.5") and premium.p0 == Decimal("0.6")
    assert premium.effect == Decimal("0.2")
    assert basic.effect == Decimal(282) / Decimal(450) - Decimal("0.5")
    assert basic.low < basic.effect < basic.high
    assert basic.cells == (
        "queries/renewed-not-offered/basic",
        "queries/renewed-offered/basic",
    )
    assert basic.confidence == "0.9"


def test_the_runtimes_readout_gives_the_effect_and_its_interval() -> None:
    row = TwoProportionReadout().compare(
        ArmCounts("control", 60, 36), ArmCounts("treated", 180, 144), confidence=Decimal("0.9")
    )
    found = estimate_responses(_families(), load_profile(PROFILE).profile)
    premium = found.get(0, "premium")
    assert premium is not None
    assert (premium.p0, premium.effect, premium.low, premium.high) == (
        row.control_rate,
        row.difference,
        row.interval_low,
        row.interval_high,
    )


def test_another_readout_behind_the_protocol_is_the_one_called() -> None:
    seen: List[Tuple[ArmCounts, ArmCounts, Decimal]] = []

    class Fixed:
        engine = "fixed"

        def compare(
            self, control: ArmCounts, treatment: ArmCounts, *, confidence: Decimal
        ) -> ReadoutRow:
            seen.append((control, treatment, confidence))
            return ReadoutRow(
                variant="treated",
                control="control",
                trials=treatment.trials,
                successes=treatment.successes,
                rate=Decimal("0.7"),
                control_rate=Decimal("0.25"),
                difference=Decimal("0.45"),
                interval_low=Decimal("0.4"),
                interval_high=Decimal("0.5"),
                confidence=confidence,
                engine="fixed",
            )

    found = estimate_responses(
        _families(), load_profile(PROFILE).profile, statistics=Fixed()  # type: ignore[arg-type]
    )
    assert len(seen) == 2 and all(item[2] == Decimal("0.9") for item in seen)
    assert seen[0][0] == ArmCounts("control", 150, 75)
    estimate = found.get(0, "basic")
    assert estimate is not None
    assert (estimate.p0, estimate.effect) == (Decimal("0.25"), Decimal("0.45"))


def _with_disclosure_open(document: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of the profile whose rule withholds no zero and no small cell."""
    changed = copy.deepcopy(document)
    rule = changed["disclosureRules"]["acro-default"]
    rule.update(minimumCell=1, zerosDisclosive=False)
    rule.pop("dominance", None)
    rule.pop("pRatio", None)
    return changed


def test_a_withheld_cell_gives_the_segment_no_estimate() -> None:
    document = profile_document()
    document["queries"][0]["statistics"][0]["groups"][
        [c["group"] for c in _cells(document, "renewed-offered")].index("premium")
    ] = {"group": "premium", "status": "A"}
    found = _estimate(document)
    assert [(item.segment, item.family) for item in found.unestimated] == [
        ("premium", "offer-by-plan")
    ]
    assert "withheld" in found.unestimated[0].reason
    assert [item.segment for item in found.estimates] == ["basic"]


def test_a_withheld_cell_inside_a_published_group_gives_no_estimate() -> None:
    document = profile_document()
    cell = _cell(document, "renewed-not-offered", "basic")
    cell.update(levelCounts=[None, cell["levelCounts"][1]], cellStatus=["A", "F"])
    found = _estimate(document)
    assert [item.segment for item in found.unestimated] == ["basic"]
    assert [item.segment for item in found.estimates] == ["premium"]


def test_an_arm_with_no_record_gives_no_estimate_and_zero_matches_is_an_estimate() -> None:
    document = _with_disclosure_open(profile_document())
    _cell(document, "renewed-offered", "basic").update(k=0, levelCounts=[0, 0])
    zero = _cell(document, "renewed-not-offered", "premium")
    zero.update(k=60, levelCounts=[60, 0])
    found = _estimate(document)
    assert [item.segment for item in found.unestimated] == ["basic"]
    assert "no records" in found.unestimated[0].reason
    premium = found.get(0, "premium")
    assert premium is not None
    assert premium.p0 == 0 and premium.control == (60, 0)
    assert premium.effect == Decimal(144) / Decimal(180)


def test_a_segment_the_profile_holds_no_cell_for_is_reported() -> None:
    document = profile_document()
    document["queries"][1]["statistics"][0]["groups"] = [
        cell for cell in _cells(document, "renewed-not-offered") if cell["group"] != "premium"
    ]
    found = _estimate(document)
    assert [item.segment for item in found.unestimated] == ["premium"]
    assert "no cell" in found.unestimated[0].reason


def test_the_estimates_survive_the_plan_and_a_forged_plan_is_refused() -> None:
    found = estimate_responses(_families(), load_profile(PROFILE).profile)
    document = json.loads(json.dumps(found.as_document()))
    assert ResponseEstimates.from_document(document, _families(), where="plan") == found
    forged = copy.deepcopy(document)
    forged["estimates"][0]["segment"] = "gold"
    with pytest.raises(FluxError) as caught:
        ResponseEstimates.from_document(forged, _families(), where="plan")
    assert caught.value.code.code == "FLX-3672"
    short = copy.deepcopy(document)
    del short["estimates"][1]
    with pytest.raises(FluxError) as caught:
        ResponseEstimates.from_document(short, _families(), where="plan")
    assert caught.value.code.code == "FLX-3672"


# ------------------------------------------------------------------ the rate a draw meets


def test_a_treated_rate_is_p0_plus_the_effect_inside_zero_and_one() -> None:
    p0 = Decimal("0.6")
    assert response_rate(p0, Fraction(1, 5), treated=False) == p0
    assert response_rate(p0, Fraction(1, 5), treated=True) == Decimal("0.8")
    assert response_rate(p0, Fraction(2, 5), treated=True) == 1
    assert response_rate(Decimal("0.1"), Fraction(-3, 10), treated=True) == 0
    assert response_rate(Decimal("0.5"), Fraction(1, 3), treated=True) == Decimal(5) / Decimal(6)


def test_each_subject_draws_against_the_rate_of_its_segment_and_arm() -> None:
    result = execute()
    runtime = result.responses
    assert runtime is not None
    found = runtime.estimates
    seen = set()
    for ordinal in result.population.ordinals:
        segment, arm = runtime.segment_of(0, ordinal), runtime.arm_of(0, ordinal)
        assert segment is not None and arm is not None
        estimate = found.get(0, segment)
        assert estimate is not None
        wanted = estimate.p0 if arm == "control" else estimate.p0 + estimate.effect
        got = runtime.probability(JOURNEY, ordinal, Decimal("0.99"))
        assert abs(got - wanted) < Decimal(10) ** -20
        seen.add((segment, arm))
    assert seen == {
        ("basic", "control"),
        ("basic", "treated"),
        ("premium", "control"),
        ("premium", "treated"),
    }


def _pinned_to(tmp_path: Path, document: Dict[str, Any]) -> Tuple[Path, Path]:
    """A copy of the fixture bundle pinned to *document*, and *document*'s file."""
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest = load_profile(path).digest.value

    def pin(simulation: Dict[str, Any]) -> None:
        namespace(simulation)["profile"] = {"digest": digest}
        # The register cites the profile the bundle was written from; this test runs no plan.
        namespace(simulation).pop("parameters", None)
        namespace(simulation).pop("unused", None)

    return make_bundle(tmp_path, "pinned", simulation=pin), path


def test_a_subject_whose_segment_has_no_estimate_keeps_the_probability_it_declared(
    tmp_path: Path, baseline: Any
) -> None:
    document = profile_document()
    document["queries"][0]["statistics"][0]["groups"][
        [c["group"] for c in _cells(document, "renewed-offered")].index("premium")
    ] = {"group": "premium", "status": "A"}
    bundle, profile = _pinned_to(tmp_path, document)
    result = execute(bundle, profile=profile)
    runtime = result.responses
    assert runtime is not None
    assert [item.segment for item in runtime.estimates.unestimated] == ["premium"]
    before, after = _success(baseline), _success(result)
    premium = [key for key in before if runtime.segment_of(0, key[0]) == "premium"]
    assert premium and all(after.get(key) == before[key] for key in premium)
    assert any(
        after.get(key) != before[key]
        for key in before
        if runtime.segment_of(0, key[0]) == "basic" and runtime.arm_of(0, key[0]) == "treated"
    )


# ------------------------------------------------------------------ what a draw does


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> Any:
    bundle = make_bundle(tmp_path_factory.mktemp("base"), "bundle", simulation=without_responses)
    return execute(bundle)


@pytest.fixture(scope="module")
def with_family() -> Any:
    return execute()


def test_the_assignment_is_the_assignment_it_was(baseline: Any, with_family: Any) -> None:
    assert dict(with_family.assignment) == dict(baseline.assignment)
    assert with_family.components.assignment == baseline.components.assignment
    assert with_family.components.population == baseline.components.population
    assert with_family.components.events == baseline.components.events


def test_an_untreated_subject_keeps_its_outcome_and_a_treated_one_can_only_gain(
    baseline: Any, with_family: Any
) -> None:
    """The fixture's declared rates are the control rates, so only the offer moves a draw."""
    runtime = with_family.responses
    before, after = _success(baseline), _success(with_family)
    gained = 0
    for (ordinal, step), success in sorted(after.items()):
        if runtime.arm_of(0, ordinal) != "treated" and (ordinal, step) in before:
            assert before[(ordinal, step)] == success, (ordinal, step)
        if (ordinal, step) in before and runtime.arm_of(0, ordinal) == "treated":
            assert success or not before[(ordinal, step)], (ordinal, step)
            gained += success and not before[(ordinal, step)]
    assert gained > 0
    assert with_family.components.journeys != baseline.components.journeys
    assert sum(after.values()) > 0


def test_a_subject_in_no_segment_draws_as_before(tmp_path: Path, baseline: Any) -> None:
    def only_basic(document: Dict[str, Any]) -> None:
        family = namespace(document)["responses"][0]
        family["segments"] = {"basic": "seg-basic"}
        namespace(document)["forecasts"]["items"] = [
            item for item in namespace(document)["forecasts"]["items"] if item["segment"] == "basic"
        ]

    result = execute(make_bundle(tmp_path, "basic", simulation=only_basic))
    before, after = _success(baseline), _success(result)
    runtime = result.responses
    outside = [key for key in before if runtime.segment_of(0, key[0]) is None]
    assert outside and all(after.get(key) == before[key] for key in outside)
    inside_treated = [
        key
        for key in before
        if runtime.segment_of(0, key[0]) == "basic" and runtime.arm_of(0, key[0]) == "treated"
    ]
    assert inside_treated and any(
        after.get(key) != before[key] for key in inside_treated if key in after
    )


def test_a_journey_the_family_does_not_name_draws_as_before(tmp_path: Path) -> None:
    journey = yaml.safe_load((BUNDLE / "journey.flux.yml").read_text(encoding="utf-8"))
    journey["id"] = "jr-other"

    def resources(document: Dict[str, Any]) -> None:
        document["spec"]["resources"].append("jr-other")

    documents = {"module.flux.yml": resources}
    add = {"journey-other.flux.yml": journey}
    plain = execute(
        make_bundle(tmp_path, "plain", simulation=without_responses, documents=documents, add=add)
    )
    family = execute(make_bundle(tmp_path, "family", documents=documents, add=add))
    same = [item for item in plain.transitions if item.journey_id == "jr-other"]
    kept = [item for item in family.transitions if item.journey_id == "jr-other"]
    assert same and kept == same
    assert [item for item in family.transitions if item.journey_id == JOURNEY] != [
        item for item in plain.transitions if item.journey_id == JOURNEY
    ]


# ------------------------------------------------------------------ the register


def test_the_register_lists_each_estimate_with_the_cells_it_was_read_from() -> None:
    parameters = execute().parameters
    assert parameters is not None
    listed = [
        (item.estimate.kind, item.estimate.response, item.estimate.segment, item.estimate.cells)
        for item in parameters.derived
    ]
    assert listed == [
        ("responseControl", 0, "basic", ("queries/renewed-not-offered/basic",)),
        (
            "responseEffect",
            0,
            "basic",
            ("queries/renewed-not-offered/basic", "queries/renewed-offered/basic"),
        ),
        ("responseControl", 0, "premium", ("queries/renewed-not-offered/premium",)),
        (
            "responseEffect",
            0,
            "premium",
            ("queries/renewed-not-offered/premium", "queries/renewed-offered/premium"),
        ),
    ]
    effect = parameters.derived[3].estimate
    assert effect.value == "0.2" and effect.interval is not None
    assert effect.interval.confidence == "0.9"


# ------------------------------------------------------------------ a bundle without responses


@pytest.mark.parametrize("jobs", [1, 4])
def test_a_bundle_without_responses_keeps_the_digests_it_had(tmp_path: Path, jobs: int) -> None:
    bundle = make_bundle(tmp_path, "plain", simulation=without_responses)
    result = execute(bundle, jobs=jobs)
    assert result.responses is None
    assert result.input_digest.ref.value == BASE_INPUT_DIGEST
    found = result.components
    assert {name: getattr(found, name).value for name in BASE_COMPONENTS} == BASE_COMPONENTS
    ran = run(bundle, state_dir=tmp_path / "state", profile=PROFILE, jobs=jobs)
    assert ran.success, ran.stderr
    receipt = ran.artifacts["apply"]["receipt"]
    assert receipt["runDigest"] == BASE_RUN_DIGEST
    assert receipt["planDigest"] == BASE_PLAN_DIGEST
    plan_document = json.loads(next((tmp_path / "state" / "runs").glob("*/plan.json")).read_text())
    assert RESPONSES_PLAN_KEY not in plan_document


def test_jobs_one_and_four_agree_on_a_run_with_a_family(tmp_path: Path) -> None:
    digests = []
    for jobs in (1, 4):
        ran = run(BUNDLE, state_dir=tmp_path / f"s{jobs}", profile=PROFILE, jobs=jobs)
        assert ran.success, ran.stderr
        receipt = ran.artifacts["apply"]["receipt"]
        digests.append((receipt["runDigest"], receipt["planDigest"]))
    assert digests[0] == digests[1]
    assert digests[0][0] != BASE_RUN_DIGEST


# ------------------------------------------------------------------ refusals


def test_a_family_with_no_pinned_profile_is_refused(tmp_path: Path) -> None:
    def unpinned(document: Dict[str, Any]) -> None:
        namespace(document).pop("profile", None)
        namespace(document).pop("parameters", None)
        namespace(document).pop("unused", None)

    bundle = make_bundle(tmp_path, "unpinned", simulation=unpinned)
    with pytest.raises(FluxError) as caught:
        execute(bundle)
    assert caught.value.code.code == "FLX-3671"
    assert NAMESPACE in str(caught.value)


def test_the_profile_is_the_one_the_simulation_pins(tmp_path: Path) -> None:
    other = profile_document()
    other["queries"][0]["name"] = "renewed-offered-again"
    path = tmp_path / "other.json"
    path.write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(FluxError) as caught:
        execute(profile=path)
    assert caught.value.code.code == "FLX-3110"


# ------------------------------------------------------------------ the whole loop


def test_plan_apply_verify_and_replay_on_the_fixture(tmp_path: Path) -> None:
    state = tmp_path / "state"
    planned = plan(BUNDLE, state_dir=state, profile=PROFILE, stage=False)
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    plan_path = state / "runs" / run_id / "plan.json"
    recorded = json.loads(plan_path.read_text(encoding="utf-8"))
    assert [item["segment"] for item in recorded[RESPONSES_PLAN_KEY]["estimates"]] == [
        "basic",
        "premium",
    ]
    # apply and replay read the plan, never the profile.
    applied = apply(BUNDLE, run_id=run_id, state_dir=state, auto_plan=False)
    assert applied.success, applied.stderr
    digest = applied.artifacts["run_digest"]
    assert digest != BASE_RUN_DIGEST
    checked = verify(BUNDLE, run_id=run_id, state_dir=state, profile=PROFILE)
    assert checked.success, checked.stderr
    again = replay(BUNDLE, run_id=run_id, assert_digest=digest, state_dir=state)
    assert again.success, again.stderr
    register = json.loads(
        (state / "runs" / run_id / "parameter-register.json").read_text(encoding="utf-8")
    )
    kinds = [entry["kind"] for entry in register["entries"] if "kind" in entry]
    assert kinds == ["responseControl", "responseEffect"] * 2


def test_recorded_estimates_replace_the_profile_and_a_forged_record_is_refused() -> None:
    recorded = estimate_responses(_families(), load_profile(PROFILE).profile).as_document()
    from_plan = execute(profile=None, responses=recorded)
    assert from_plan.responses is not None
    assert from_plan.components == execute().components
    del recorded["estimates"][1]
    with pytest.raises(FluxError) as caught:
        execute(profile=None, responses=recorded)
    assert caught.value.code.code == "FLX-3672"


def test_fluxsim_run_on_the_fixture_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digests = []
    for name in ("one", "two"):
        shutil.rmtree(tmp_path / name, ignore_errors=True)
        code = main(
            [
                "run",
                str(BUNDLE),
                "--profile",
                str(PROFILE),
                "--state-dir",
                str(tmp_path / name),
                "--verify",
                "--json",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 0, out
        digests.append(out["artifacts"]["apply"]["run_digest"])
    assert digests[0] == digests[1] and digests[0] != BASE_RUN_DIGEST
