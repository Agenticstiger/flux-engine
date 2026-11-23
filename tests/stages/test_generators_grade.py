# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A declared generator plugin sets the run's grade, and the governance gate checks it (ADR 0032).

The run's grade is the weakest of the profile's and every declared plugin's grade. ``P5``, a learned
model with no privacy account, is below every floor, so a ``pii`` column with a ``P5`` plugin is
refused at ``plan`` (``FLX-4005``) before anything is written; a ``P4`` plugin on a run that pins no
profile makes it ``P4``. Kernel 0.1.0 refuses the key (``FLX-3105``). No plugin is imported or called
here: the declaration is what is graded, so the loading of ADR 0032 section 3 (the person's word, the
installed entry point, the statement) and the generation of section 5 are left out, and
``tests/generators/test_loading.py`` and ``tests/generators/test_generate.py`` hold those.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages._grades import generator_grades, with_generators
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.replay import replay
from flux_engine.stages.run import run
from flux_engine.stages.validate import validate

from .test_minimum_grade import NAMESPACE, _bundle, _edit, _planned

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "ab" * 32


@pytest.fixture(autouse=True)
def _no_plugin_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grade the declaration without loading the plugin it names or handing it an expose."""
    validate_module = importlib.import_module("flux_engine.stages.validate")

    monkeypatch.setattr(pipeline, "load_declared", lambda *args, **kwargs: {})
    monkeypatch.setattr(pipeline, "plugin_exposes", lambda *args, **kwargs: {})
    monkeypatch.setattr(validate_module, "plugin_exposes", lambda *args, **kwargs: {})
    # Nothing is loaded, so the plan cannot record what a plugin ran in; it records what the bundle
    # declares, as a plan of a loaded plugin does (apply, replay and ship refuse a plan that
    # records anything else, flux_engine.stages.stored).
    monkeypatch.setattr(
        importlib.import_module("flux_engine.stages.plan"), "_generator_entries", _declared_entries
    )


def _declared_entries(result: Any) -> Dict[str, Any]:
    from flux_engine.generators.environment import environment_digest
    from flux_engine.seam.resolve import declared_generators

    declared = declared_generators(result.cast.simulation, where=result.cast.simulation_id)
    entries: Dict[str, Any] = {}
    for item in declared:
        entry: Dict[str, Any] = {
            "plugin": item.plugin,
            "distribution": item.distribution,
            "version": item.version,
            "editable": False,
            "grade": item.grade.value,
            "reproducibility": item.reproducibility,
            "environmentDigest": environment_digest(),
        }
        if item.privacy is not None:
            entry["privacy"] = item.privacy.to_document()
        if item.model_digest is not None:
            entry["modelDigest"] = item.model_digest
        entries[item.expose_id] = entry
    return {"generators": entries} if entries else {}


def _plugin(grade: str, **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "plugin": "learned",
        "distribution": "my-package",
        "version": "1.2.0",
        "grade": grade,
        "reproducibility": "bytes",
    }
    if grade == "P4":
        entry["privacy"] = {
            "unit": "one data subject with all of its records",
            "epsilon": "3",
            "delta": "0.00001",
            "accounting": "a Renyi accountant over the training steps",
        }
        entry["model"] = {"digest": DIGEST}
    entry.update(extra)
    return entry


def _declare(bundle: Path, expose_id: str, grade: str, **extra: Any) -> None:
    _edit(
        bundle,
        lambda d: d.setdefault("extensions", {})
        .setdefault(NAMESPACE, {})
        .update(generators={expose_id: _plugin(grade, **extra)}),
    )


def _starter(tmp_path: Path, *, label: str = "pii") -> Path:
    """The hand-written starter project, which pins no profile, with its one column labelled."""
    from flux_engine.starter import scaffold
    from tests.bundles import repin_contract

    bundle = tmp_path / "starter"
    scaffold(bundle)
    contract = bundle / "payment-recovery.fluid.yml"
    text = contract.read_text(encoding="utf-8")
    anchor = "          type: STRING\n    binding:"
    assert text.count(anchor) == 1
    contract.write_text(
        text.replace(
            anchor, f"          type: STRING\n          sensitivity: {label}\n    binding:"
        ),
        encoding="utf-8",
    )
    repin_contract(contract, bundle / "simulation.flux.yml", "telco.gold.payment_recovery_moment")
    return bundle


def _nothing_written(tmp_path: Path, state: str = "state") -> bool:
    return not list((tmp_path / state).glob("runs/*"))


def test_a_pii_column_with_a_p5_plugin_is_refused_at_plan_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, "pii")
    assert _planned(bundle, tmp_path, "before").exit_code == 0, "the profile alone meets the floor"
    _declare(bundle, "subscriber", "P5")
    outcome = _planned(bundle, tmp_path)
    assert outcome.exit_code == 1 and outcome.error_code == "FLX-4005", outcome.stderr
    assert "subscriber.tier is pii, which needs P3 or stronger, and the run's grade is P5" in (
        outcome.stderr
    )
    assert _nothing_written(tmp_path)
    full = run(bundle, state_dir=tmp_path / "state2", profile=tmp_path / "telco.profile.json")
    assert full.error_code == "FLX-4005" and _nothing_written(tmp_path, "state2")


def test_the_gate_refuses_a_p5_plugin_before_the_run_is_generated(tmp_path: Path) -> None:
    from flux_engine.governance.privacy import GradeBelowMinimum

    bundle = _bundle(tmp_path, "pii")
    _declare(bundle, "subscriber", "P5")
    with pytest.raises(GradeBelowMinimum) as refused:
        pipeline.execute(bundle, profile=tmp_path / "telco.profile.json")
    assert refused.value.code.code == "FLX-4005"


@pytest.mark.parametrize("label", ["confidential", "internal"])
def test_a_p5_plugin_is_below_the_floor_of_every_label_that_has_one(
    tmp_path: Path, label: str
) -> None:
    bundle = _starter(tmp_path, label=label)
    assert plan(bundle, state_dir=tmp_path / "state0").exit_code == 0
    _declare(bundle, "payment_recovery_moment", "P5")
    outcome = plan(bundle, state_dir=tmp_path / "state")
    assert outcome.error_code == "FLX-4005", outcome.stderr
    assert f"is {label}, which needs P1 or stronger, and the run's grade is P5" in outcome.stderr


def test_a_p5_plugin_on_a_label_with_no_floor_plans_and_the_plan_records_p5(
    tmp_path: Path,
) -> None:
    bundle = _starter(tmp_path, label="cleartext")
    _declare(bundle, "payment_recovery_moment", "P5")
    outcome = plan(bundle, state_dir=tmp_path / "state")
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.artifacts["plan_document"]["privacyGrade"] == "P5"


def test_a_p4_plugin_and_no_profile_gives_privacy_grade_p4(tmp_path: Path) -> None:
    bundle = _starter(tmp_path)
    assert (
        "privacyGrade" not in plan(bundle, state_dir=tmp_path / "plain").artifacts["plan_document"]
    )
    _declare(bundle, "payment_recovery_moment", "P4")
    outcome = plan(bundle, state_dir=tmp_path / "state")
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.artifacts["plan_document"]["privacyGrade"] == "P4"
    (recorded,) = (tmp_path / "state").glob("runs/*/plan.json")
    assert json.loads(recorded.read_text(encoding="utf-8"))["privacyGrade"] == "P4"


def test_a_p4_plugin_meets_the_floor_of_pii_and_the_profiles_grade_still_counts(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, "pii")
    _declare(bundle, "subscriber", "P4")
    outcome = _planned(bundle, tmp_path)
    assert outcome.exit_code == 0, outcome.stderr
    assert outcome.artifacts["plan_document"]["privacyGrade"] == "P3", "P3 is below P4"


def test_a_p4_plugin_does_not_lift_a_profile_graded_p1(tmp_path: Path) -> None:
    (tmp_path / "exact").mkdir()
    bundle = _bundle(tmp_path / "exact", "pii", row_count=True)
    _declare(bundle, "subscriber", "P4")
    outcome = plan(
        bundle,
        state_dir=tmp_path / "state",
        profile=tmp_path / "exact" / "telco.profile.json",
    )
    assert outcome.error_code == "FLX-4005"
    assert "the run's grade is P1" in outcome.stderr


def test_a_bundle_that_raises_a_floor_above_p4_refuses_a_p4_plugin(tmp_path: Path) -> None:
    bundle = _starter(tmp_path, label="cleartext")
    _edit(
        bundle,
        lambda d: d.setdefault("extensions", {})
        .setdefault(NAMESPACE, {})
        .update(privacy={"minimumGrade": {"cleartext": "P0"}}),
    )
    _declare(bundle, "payment_recovery_moment", "P4")
    outcome = plan(bundle, state_dir=tmp_path / "state")
    assert outcome.error_code == "FLX-4005"
    assert "needs P0 or stronger, and the run's grade is P4" in outcome.stderr


def test_apply_and_replay_enforce_the_declared_grade_from_the_bundle(tmp_path: Path) -> None:
    bundle = _starter(tmp_path)
    _declare(bundle, "payment_recovery_moment", "P4")
    planned = plan(bundle, state_dir=tmp_path / "state")
    assert planned.exit_code == 0, planned.stderr
    run_id = planned.artifacts["run_id"]
    applied = apply(bundle, run_id=run_id, state_dir=tmp_path / "state")
    assert applied.exit_code == 0, applied.stderr
    again = replay(
        bundle,
        run_id=run_id,
        assert_digest=applied.artifacts["run_digest"],
        state_dir=tmp_path / "state",
    )
    assert again.exit_code == 0, again.stderr


def test_the_grade_of_the_run_is_worked_out_from_the_bundle_and_not_from_a_plan(
    tmp_path: Path,
) -> None:
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.resolve import load_bundle

    bundle = _starter(tmp_path)

    def simulation() -> Dict[str, Any]:
        loaded = load_bundle(bundle)
        found: Dict[str, Any] = dict(
            resolve_cast(loaded.flux_documents, simulation_id="sim-payment-recovery").simulation
        )
        return found

    assert generator_grades(simulation(), where="w") == ()
    assert with_generators(Pattern.P3, simulation(), where="w") is Pattern.P3
    _declare(bundle, "payment_recovery_moment", "P5")
    assert generator_grades(simulation(), where="w") == (Pattern.P5,)
    assert with_generators(Pattern.P0, simulation(), where="w") is Pattern.P5
    assert with_generators(Pattern.P1, simulation(), where="w") is Pattern.P5
    _declare(bundle, "payment_recovery_moment", "P4")
    assert with_generators(Pattern.P0, simulation(), where="w") is Pattern.P4
    assert with_generators(Pattern.P3, simulation(), where="w") is Pattern.P3


def test_the_evidence_report_states_each_plugin_and_the_account_it_declares(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path, "pii")
    _declare(bundle, "subscriber", "P4")
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
    assert "Run grade: **P3 (differentially private marginals)**." in text
    assert (
        "- `subscriber`: plugin `learned` from `my-package` `1.2.0`, declared P4 "
        "(differentially private model). Unit of privacy: `one data subject with all of its "
        "records`. Privacy loss: epsilon = 3, delta = 0.00001. Accounting: `a Renyi accountant "
        "over the training steps`." in text
    )
    assert "not how the model was trained" in text


# ---- kernel 0.1.0 and the digests ------------------------------------------------------------


def test_the_key_is_refused_on_kernel_0_1_0_naming_the_kernel(tmp_path: Path) -> None:
    bundle = _starter(tmp_path, label="cleartext")
    _declare(bundle, "payment_recovery_moment", "P5")
    with pytest.raises(FluxError) as raised:
        pipeline.execute(bundle, kernel=KERNEL_V1)
    assert raised.value.code.code == "FLX-3105"
    assert "kernel 0.1.0" in str(raised.value) and "generators" in str(raised.value)
    planned = plan(bundle, state_dir=tmp_path / ".flux", kernel="0.1.0")
    assert not planned.success and planned.error_code == "FLX-3105"
    assert "kernel 0.1.0" in planned.stderr
    assert pipeline.execute(bundle, kernel=KERNEL_V2) is not None


def test_validate_refuses_the_key_where_a_new_plan_would_be_kernel_0_1_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    bundle = _starter(tmp_path)
    _declare(bundle, "payment_recovery_moment", "P5")
    assert validate(bundle).exit_code == 0
    module = importlib.import_module("flux_engine.stages.validate")
    monkeypatch.setattr(module, "new_plan_is_counter_based", lambda: False)
    checked = validate(bundle)
    assert checked.exit_code != 0
    assert "FLX-3105" in checked.stderr + str(checked.artifacts)
    assert "kernel 0.1.0" in checked.stderr + str(checked.artifacts)


def test_validate_refuses_a_bad_declaration_and_so_does_every_command_that_reads_the_bundle(
    tmp_path: Path,
) -> None:
    bundle = _starter(tmp_path)
    _declare(bundle, "payment_recovery_moment", "P3")
    checked = validate(bundle)
    assert checked.exit_code != 0 and "FLX-3105" in checked.stderr + str(checked.artifacts)
    for stage in (plan, run, pipeline.execute):
        if stage is pipeline.execute:
            with pytest.raises(FluxError) as raised:
                pipeline.execute(bundle)
            assert raised.value.code.code == "FLX-3105"
        else:
            outcome = stage(bundle, state_dir=tmp_path / "state")
            assert outcome.error_code == "FLX-3105", outcome.stderr


#: The plan digests of the starter project, computed with the code before the key existed
#: (`flux_engine/starter/bundle`, planned with each kernel): a bundle that declares no generator
#: keeps them.
BEFORE_THE_KEY = {
    "0.1.0": "sha256:8835218acf7b4f8e28b738c7d7e9e8d6b5c5e8bca59e47533fdda09eb90cf501",
    "0.2.0": "sha256:205ba6e504c408208fb15c5da5f96edac2f514606d6024bc19733c6c5cac90cc",
}


@pytest.mark.parametrize("kernel", ["0.1.0", "0.2.0"])
def test_a_bundle_without_the_key_keeps_its_plan_digest_under_both_kernels(
    tmp_path: Path, kernel: str
) -> None:
    from flux_engine.starter import scaffold

    scaffold(tmp_path / "bundle")
    planned = plan(tmp_path / "bundle", state_dir=tmp_path / "state", kernel=kernel)
    assert planned.exit_code == 0, planned.stderr
    document = planned.artifacts["plan_document"]
    assert document["planDigest"] == BEFORE_THE_KEY[kernel]
    assert "privacyGrade" not in document
