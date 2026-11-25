# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column's sensitivity sets the minimum grade of the run, and the gate holds (``FLX-4005``).

The run's grade is read from the profile its Simulation pins and from nothing the bundle says about
itself: a fully noisy profile grades P3, any exact statistic anywhere in it, a row count included,
grades P1, and a run that pins no profile is P0. ``plan`` refuses a run below a column's floor before
anything is written, ``apply`` (and so ``apply --run-id`` and ``replay``) and ``ship`` refuse it too,
and a bundle that relabels the numbers that cite the profile as assumptions changes nothing.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.cli._init import run_init
from flux_engine.core.grades import Pattern
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.replay import replay
from flux_engine.stages.run import run
from flux_engine.stages.ship import ship
from flux_engine.stages.validate import validate
from tests.bundles import edit_simulation as _edit

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "private-telco"
NAMESPACE = "com.agentics.flux/1"


def _contract(label: str) -> str:
    """The fixture's contract with the ``tier`` column labelled *label*."""
    text = (FIXTURE / "telco.fluid.yaml").read_text(encoding="utf-8")
    column = "- { name: tier, type: \"enum('gold','silver')\", required: true }"
    assert column in text
    return text.replace(column, column.replace(" }", f", sensitivity: {label} }}"))


def _bundle(
    tmp_path: Path,
    label: str,
    *,
    row_count: bool = False,
    privacy: Optional[Any] = None,
) -> Path:
    profile: Dict[str, Any] = json.loads((FIXTURE / "telco.profile.json").read_text("utf-8"))
    if row_count:
        profile["tables"]["subscriber"]["rowCount"] = 9195
    (tmp_path / "telco.fluid.yaml").write_text(_contract(label), encoding="utf-8")
    (tmp_path / "telco.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "telco.fluid.yaml"),
        profile=str(tmp_path / "telco.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    if privacy is not None:
        _edit(tmp_path / "bundle", lambda d: d["extensions"][NAMESPACE].update(privacy=privacy))
    return tmp_path / "bundle"


def _planned(bundle: Path, tmp_path: Path, state: str = "state") -> Any:
    return plan(bundle, state_dir=tmp_path / state, profile=tmp_path / "telco.profile.json")


def _nothing_written(tmp_path: Path, state: str = "state") -> bool:
    return not list((tmp_path / state).glob("runs/*"))


def test_a_pii_column_of_a_fully_noisy_profile_plans_and_the_plan_records_p3(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, "pii")
    outcome = _planned(bundle, tmp_path)
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.artifacts["plan_document"]["privacyGrade"] == "P3"
    (recorded,) = (tmp_path / "state").glob("runs/*/plan.json")
    assert json.loads(recorded.read_text(encoding="utf-8"))["privacyGrade"] == "P3"


def test_an_exact_row_count_in_a_private_profile_is_p1_and_refuses_a_pii_column(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, "pii", row_count=True)
    outcome = _planned(bundle, tmp_path)
    assert outcome.exit_code == 1 and outcome.error_code == "FLX-4005"
    assert "subscriber.tier is pii, which needs P3 or stronger, and the run's grade is P1" in (
        outcome.stderr
    )
    assert _nothing_written(tmp_path)
    full = run(bundle, state_dir=tmp_path / "state2", profile=tmp_path / "telco.profile.json")
    assert full.error_code == "FLX-4005" and _nothing_written(tmp_path, "state2")


def test_relabelling_the_numbers_that_cite_the_profile_does_not_change_the_grade(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, "pii", row_count=True)
    assert _planned(bundle, tmp_path).error_code == "FLX-4005"

    def relabel(document: Dict[str, Any]) -> None:
        register = document["extensions"][NAMESPACE]["parameters"]
        for entry in register:
            if entry.get("source") == "profile":
                entry["source"] = "assumption"
                entry.pop("statistic", None)
                entry["reason"] = "typed by hand"

    _edit(bundle, relabel)
    outcome = _planned(bundle, tmp_path, "state-after")
    assert outcome.exit_code == 1 and outcome.error_code == "FLX-4005", outcome.stderr
    assert _nothing_written(tmp_path, "state-after")


def test_a_run_that_pins_no_profile_is_p0_and_meets_every_floor(tmp_path: Path) -> None:
    from flux_engine.starter import scaffold
    from tests.bundles import repin_contract

    # the starter project is hand-written: no profile is pinned
    bundle = tmp_path / "starter"
    scaffold(bundle)
    contract = bundle / "payment-recovery.fluid.yml"
    text = contract.read_text(encoding="utf-8")
    anchor = "          type: STRING\n    binding:"
    assert text.count(anchor) == 1
    contract.write_text(
        text.replace(anchor, "          type: STRING\n          sensitivity: pii\n    binding:"),
        encoding="utf-8",
    )
    repin_contract(contract, bundle / "simulation.flux.yml", "telco.gold.payment_recovery_moment")
    outcome = plan(bundle, state_dir=tmp_path / "state")
    assert outcome.exit_code == 0, outcome.stderr
    assert "privacyGrade" not in outcome.artifacts["plan_document"]


def test_a_label_that_sets_no_floor_and_one_that_sets_p1_plan_whatever_the_grade(
    tmp_path: Path,
) -> None:
    for label in ("cleartext", "confidential"):
        (tmp_path / label).mkdir()
        bundle = _bundle(tmp_path / label, label, row_count=True)
        outcome = plan(
            bundle,
            state_dir=tmp_path / label / "state",
            profile=tmp_path / label / "telco.profile.json",
        )
        assert outcome.exit_code == 0, outcome.stderr


def test_a_bundle_raises_the_floor_of_a_label_and_the_run_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path, "confidential", row_count=True, privacy={"minimumGrade": {"confidential": "P3"}}
    )
    outcome = _planned(bundle, tmp_path)
    assert outcome.error_code == "FLX-4005"
    assert "subscriber.tier is confidential, which needs P3 or stronger" in outcome.stderr
    assert validate(bundle).exit_code == 0, "the bundle is valid: only the run is refused"


def test_a_bundle_that_lowers_a_floor_is_refused_by_validate_and_by_plan(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "pii", privacy={"minimumGrade": {"pii": "P1"}})
    checked = validate(bundle)
    assert checked.exit_code != 0
    assert "FLX-3105" in checked.stderr + str(checked.artifacts)
    outcome = _planned(bundle, tmp_path)
    assert outcome.error_code == "FLX-3105"
    assert "may raise a floor and never lower one" in outcome.stderr


def test_apply_run_id_and_replay_enforce_the_minimum_against_the_recorded_grade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path, "pii")
    planned = _planned(bundle, tmp_path)
    run_id = planned.artifacts["run_id"]
    # a plan that names P3 meets the floor of pii
    applied = apply(bundle, run_id=run_id, state_dir=tmp_path / "state")
    assert applied.exit_code == 0, applied.stderr
    digest = applied.artifacts["run_digest"]
    assert (
        replay(bundle, run_id=run_id, assert_digest=digest, state_dir=tmp_path / "state").exit_code
        == 0
    )
    # a plan that pins a profile and names no grade (an engine that did not grade) is read as P1
    import importlib

    apply_module = importlib.import_module("flux_engine.stages.apply")
    monkeypatch.setattr(apply_module, "recorded_grade", lambda document, *, pinned: Pattern.P1)
    refused = apply(bundle, run_id=run_id, state_dir=tmp_path / "state")
    assert refused.exit_code == 1 and refused.error_code == "FLX-4005"
    again = replay(bundle, run_id=run_id, assert_digest=digest, state_dir=tmp_path / "state")
    assert again.exit_code == 1 and again.error_code == "FLX-4005"


def test_a_plan_that_pins_a_profile_and_records_no_grade_is_read_as_p1() -> None:
    from flux_engine.stages._grades import recorded_grade

    assert recorded_grade({}, pinned=False) is Pattern.P0
    assert recorded_grade({}, pinned=True) is Pattern.P1
    assert recorded_grade({"profileDigest": "sha256:x"}, pinned=True) is Pattern.P1
    assert recorded_grade({"profileDigest": "sha256:x", "privacyGrade": "P3"}, pinned=True) is (
        Pattern.P3
    )
    assert recorded_grade({"profileDigest": "sha256:x", "privacyGrade": "P9"}, pinned=True) is (
        Pattern.P1
    )
    # a plan that names no profile does not make the run P0 when the Simulation pins one
    assert recorded_grade({"privacyGrade": "P3"}, pinned=False) is Pattern.P0


def test_apply_and_ship_read_pinned_from_the_simulation_not_from_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    from flux_engine.stages._grades import recorded_grade as real

    seen: List[bool] = []

    def spy(document: Any, *, pinned: bool) -> Pattern:
        seen.append(pinned)
        return real(document, pinned=pinned)

    apply_module = importlib.import_module("flux_engine.stages.apply")
    grades_module = importlib.import_module("flux_engine.stages._grades")
    monkeypatch.setattr(apply_module, "recorded_grade", spy)
    monkeypatch.setattr(grades_module, "recorded_grade", spy)
    bundle = _bundle(tmp_path, "pii")
    planned = _planned(bundle, tmp_path)
    run_id = planned.artifacts["run_id"]
    assert apply(bundle, run_id=run_id, state_dir=tmp_path / "state").exit_code == 0
    shipped = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        profile=tmp_path / "telco.profile.json",
    )
    assert shipped.exit_code == 0, shipped.stderr
    # the Simulation pins the profile, so both stages ask for the grade of a pinned run, whatever
    # the plan's own profileDigest says
    assert seen and all(seen), seen


def test_ship_refuses_a_run_below_the_minimum_and_the_evidence_table_lists_every_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path, "pii")
    outcome = run(
        bundle,
        state_dir=tmp_path / "state",
        profile=tmp_path / "telco.profile.json",
        verify=True,
        ship=True,
        out_dir=tmp_path / "out",
    )
    assert outcome.success, outcome.stderr + outcome.stdout
    (evidence,) = (tmp_path / "state").glob("runs/*/EVIDENCE.md")
    text = evidence.read_text(encoding="utf-8")
    assert "| Expose | Column | Sensitivity | Minimum | Met |" in text
    assert "| `subscriber` | `tier` | pii | P3 or stronger | yes |" in text
    assert "| `subscriber` | `region` | none | any pattern | yes |" in text
    assert text.count("| `subscriber` |") == 5, "every column of the run is listed"
    run_id = outcome.artifacts["plan"]["run_id"]
    import importlib

    grades_module = importlib.import_module("flux_engine.stages._grades")

    monkeypatch.setattr(grades_module, "recorded_grade", lambda document, *, pinned: Pattern.P1)
    run_dir = tmp_path / "state" / "runs" / run_id
    before = {path.name: path.stat().st_mtime_ns for path in run_dir.rglob("*") if path.is_file()}
    refused = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "out2",
        state_dir=tmp_path / "state",
        profile=tmp_path / "telco.profile.json",
    )
    assert refused.exit_code == 1 and refused.error_code == "FLX-4005", refused.stderr
    after = {path.name: path.stat().st_mtime_ns for path in run_dir.rglob("*") if path.is_file()}
    assert after == before, "a refused ship writes nothing into the run directory"
    shutil.rmtree(tmp_path / "out2", ignore_errors=True)


def _read_the_plan_as_p1(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every stage read the recorded plan as a P1 run, as one from an engine that did not grade."""
    import importlib

    for name in ("flux_engine.stages.apply", "flux_engine.stages._grades"):
        monkeypatch.setattr(
            importlib.import_module(name), "recorded_grade", lambda document, *, pinned: Pattern.P1
        )


def _committed_files(state: Path, run_id: str) -> List[Path]:
    return sorted((state / "runs" / run_id).glob("emits/*"))


def test_a_staged_plan_is_committed_only_when_its_recorded_grade_meets_the_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path, "pii")
    state = tmp_path / "state"
    planned = _planned(bundle, tmp_path)
    run_id = planned.artifacts["run_id"]
    assert (state / "runs" / run_id / ".stage").is_dir(), "the plan stages its execution"
    # `apply --run-id` commits the stage without generating: the recorded grade is enforced there
    _read_the_plan_as_p1(monkeypatch)
    refused = apply(bundle, run_id=run_id, state_dir=state)
    assert refused.exit_code == 1 and refused.error_code == "FLX-4005", refused.stderr
    assert "subscriber.tier is pii, which needs P3 or stronger" in refused.stderr
    assert _committed_files(state, run_id) == [], "nothing was committed from the stage"
    monkeypatch.undo()
    committed = apply(bundle, run_id=run_id, state_dir=state)
    assert committed.exit_code == 0, committed.stderr
    assert committed.artifacts.get("committed_from_stage") is True
    assert _committed_files(state, run_id)


def test_an_unstaged_plan_is_applied_by_run_id_only_when_its_grade_meets_the_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path, "pii")
    state = tmp_path / "state"
    planned = plan(bundle, state_dir=state, profile=tmp_path / "telco.profile.json", stage=False)
    assert planned.exit_code == 0, planned.stderr
    run_id = planned.artifacts["run_id"]
    assert not (state / "runs" / run_id / ".stage").exists()
    _read_the_plan_as_p1(monkeypatch)
    refused = apply(bundle, run_id=run_id, state_dir=state)
    assert refused.exit_code == 1 and refused.error_code == "FLX-4005", refused.stderr
    assert _committed_files(state, run_id) == []
    monkeypatch.undo()
    applied = apply(bundle, run_id=run_id, state_dir=state)
    assert applied.exit_code == 0, applied.stderr
    assert not applied.artifacts.get("committed_from_stage")
    assert _committed_files(state, run_id)


def test_a_p1_profile_is_refused_by_plan_and_writes_no_stage(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "pii", row_count=True)
    for stage in (True, False):
        outcome = plan(
            bundle,
            state_dir=tmp_path / f"state-{stage}",
            profile=tmp_path / "telco.profile.json",
            stage=stage,
        )
        assert outcome.exit_code == 1 and outcome.error_code == "FLX-4005", outcome.stderr
        assert not list((tmp_path / f"state-{stage}").glob("runs/*"))


def test_run_is_refused_for_a_pii_column_under_a_p1_profile_and_runs_under_p3(
    tmp_path: Path,
) -> None:
    (tmp_path / "p1").mkdir()
    (tmp_path / "p3").mkdir()
    refused = run(
        _bundle(tmp_path / "p1", "pii", row_count=True),
        state_dir=tmp_path / "state-p1",
        profile=tmp_path / "p1" / "telco.profile.json",
    )
    assert refused.exit_code == 1 and refused.error_code == "FLX-4005", refused.stderr
    assert not list((tmp_path / "state-p1").glob("runs/*/emits"))
    allowed = run(
        _bundle(tmp_path / "p3", "pii"),
        state_dir=tmp_path / "state-p3",
        profile=tmp_path / "p3" / "telco.profile.json",
    )
    assert allowed.success, allowed.stderr


def test_privacy_grade_is_inside_the_plan_digest(tmp_path: Path) -> None:
    from flux_engine.core.digest import build_plan_digest

    bundle = _bundle(tmp_path, "pii")
    planned = _planned(bundle, tmp_path)
    run_id = planned.artifacts["run_id"]
    path = tmp_path / "state" / "runs" / run_id / "plan.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    digest = document.pop("planDigest")
    assert build_plan_digest(document).ref.value == digest
    grade = document.pop("privacyGrade")
    assert grade == "P3"
    assert build_plan_digest(document).ref.value != digest, "the grade moves the plan digest"
    # a plan whose recorded grade was edited no longer agrees with its own digest
    document["privacyGrade"] = "P0"
    document["planDigest"] = digest
    path.write_text(json.dumps(document), encoding="utf-8")
    moved = apply(bundle, run_id=run_id, state_dir=tmp_path / "state")
    assert moved.exit_code == 1 and moved.error_code == "FLX-2102", moved.stderr


def test_a_staged_run_states_the_grade_in_its_evidence(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "pii")
    state = tmp_path / "state"
    planned = _planned(bundle, tmp_path)
    run_id = planned.artifacts["run_id"]
    committed = apply(bundle, run_id=run_id, state_dir=state)
    assert committed.artifacts.get("committed_from_stage") is True, committed.stderr
    shipped = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "out",
        state_dir=state,
        profile=tmp_path / "telco.profile.json",
    )
    assert shipped.exit_code == 0, shipped.stderr
    text = (state / "runs" / run_id / "EVIDENCE.md").read_text(encoding="utf-8")
    assert "Privacy Grade" in text and "P3" in text
    assert "| `subscriber` | `tier` | pii | P3 or stronger | yes |" in text


def _init(tmp_path: Path, label: str, *, row_count: bool) -> Any:
    profile: Dict[str, Any] = json.loads((FIXTURE / "telco.profile.json").read_text("utf-8"))
    if row_count:
        profile["tables"]["subscriber"]["rowCount"] = 9195
    (tmp_path / "telco.fluid.yaml").write_text(_contract(label), encoding="utf-8")
    (tmp_path / "telco.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "telco.fluid.yaml"),
        profile=str(tmp_path / "telco.profile.json"),
    )


def test_init_warns_of_a_floor_the_profile_cannot_meet_and_suggests_no_run(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, "pii", row_count=True)
    assert result.exit_code == 0, result.stderr
    assert "grade: this profile gives the run P1" in result.stdout
    assert "subscriber.tier is pii (needs P3 or stronger)" in result.stdout
    assert "FLX-4005" in result.stdout
    assert "next:" not in result.stdout and "--profile" not in result.stdout
    assert result.artifacts["next"] == []
    assert result.artifacts["grade"] == {
        "grade": "P1",
        "belowMinimum": [
            {
                "exposeId": "subscriber",
                "column": "tier",
                "sensitivity": "pii",
                "minimum": "P3",
            }
        ],
    }
    # `plan` does refuse it: the warning is not a guess.
    refused = _planned(tmp_path / "bundle", tmp_path)
    assert refused.error_code == "FLX-4005"


def test_init_prints_the_run_command_when_the_profile_meets_every_floor(tmp_path: Path) -> None:
    result = _init(tmp_path, "pii", row_count=False)
    assert result.exit_code == 0, result.stderr
    assert "grade:" not in result.stdout
    assert "next:" in result.stdout and "--profile" in result.stdout
    assert result.artifacts["next"] and result.artifacts["grade"]["belowMinimum"] == []
    assert _planned(tmp_path / "bundle", tmp_path).exit_code == 0


def test_an_unlabelled_bundle_has_no_grade_line_at_init(tmp_path: Path) -> None:
    result = _init(tmp_path, "none", row_count=True)
    assert "grade:" not in result.stdout and "next:" in result.stdout


def test_the_refusal_of_a_grade_below_the_minimum_carries_its_remedy(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "pii", row_count=True)
    outcome = _planned(bundle, tmp_path)
    assert outcome.error_code == "FLX-4005"
    assert "release every statistic of the profile under noise" in outcome.stderr
    assert "correct the label" in outcome.stderr


def test_the_remedy_names_steps_that_lead_out_of_the_refusal_not_back_into_it(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, "pii", row_count=True)
    refused = _planned(bundle, tmp_path)
    assert refused.error_code == "FLX-4005"
    assert "run `fluxsim init` again with that profile" in refused.stderr
    assert "write the bundle by hand without a profile" in refused.stderr
    assert "run without the profile" not in refused.stderr
    # the step the old text named does lead back: the bundle pins the profile
    without = plan(bundle, state_dir=tmp_path / "state-without")
    assert without.error_code == "FLX-3109" and "pins profile" in without.stderr


def test_a_pinned_run_whose_plan_records_p0_is_read_as_p1() -> None:
    from flux_engine.stages._grades import recorded_grade

    assert recorded_grade({"privacyGrade": "P0"}, pinned=True) is Pattern.P1
    assert recorded_grade({"privacyGrade": "P0"}, pinned=False) is Pattern.P0


def test_a_pinned_run_records_only_what_a_pinned_run_can_be_graded() -> None:
    from flux_engine.stages._grades import recorded_grade

    for held in ("P1", "P3", "P5"):  # the profile's P1 or P3, or a generator's weaker P5
        assert recorded_grade({"privacyGrade": held}, pinned=True) is Pattern(held)
    for stronger in ("P0", "P4"):
        assert recorded_grade({"privacyGrade": stronger}, pinned=True) is Pattern.P1


@pytest.mark.parametrize("stronger", ["P0", "P4"])
def test_a_hand_edited_plan_that_records_p0_does_not_pass_the_floor_of_a_pinned_run(
    tmp_path: Path, stronger: str
) -> None:
    from flux_engine.core.digest import build_plan_digest

    bundle = _bundle(tmp_path, "pii")
    state = tmp_path / "state"
    planned = _planned(bundle, tmp_path)
    run_id = planned.artifacts["run_id"]
    path = state / "runs" / run_id / "plan.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.pop("planDigest")
    document["privacyGrade"] = stronger
    # the forger also rewrites the digest so that the plan agrees with itself
    document["planDigest"] = build_plan_digest(document).ref.value
    path.write_text(json.dumps(document), encoding="utf-8")
    # ... and renames the run directory, which is named after the digest
    forged_id = document["planDigest"].split(":")[1][:16]
    path.parent.rename(state / "runs" / forged_id)
    run_id = forged_id
    refused = apply(bundle, run_id=run_id, state_dir=state)
    assert refused.exit_code == 1 and refused.error_code == "FLX-4005", refused.stderr
    assert "needs P3 or stronger, and the run's grade is P1" in refused.stderr
    assert _committed_files(state, run_id) == []
