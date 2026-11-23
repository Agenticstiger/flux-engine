# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim calibrate --fit`` fits declared parameters into a calibration patch (ADR 0028 section 8).

The bundle is ``tests/fixtures/consumers/bundle``. Its calibration block moves one assumed value, the share
of ``renewed`` the generator leaves NULL, which starts at a tenth and which the profile's rate of
``premium-renewed`` pulls down to a few hundredths.

* **The patch.** It is deterministic: the same bundle and profile give the same bytes, whatever the state
  directory. Its operations are RFC 6902 ``replace`` operations on declared pointers only, and its header
  names the method, the versions, the seed, the evaluations run, the scale, the profile's digest and the gaps.
* **One process.** The whole fit counts its candidates in one query process, and no run or copy of the
  bundle is left under the state directory.
* **Refusals, each with its code.** A parameter whose source is not assumption or default, a document outside
  the bundle's set, a pointer that reaches no number, evaluations past the ceiling, a taken output name,
  a missing extra, and a bundle with nothing to fit.
* **The protocol.** The fit is a :class:`~flux_engine.api.calibration.Calibrator`.
"""

from __future__ import annotations

import copy
import importlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("cma")

from flux_engine.api.calibration import Calibrator, Credibility, Provenance  # noqa: E402
from flux_engine.calibrate import fit as fit_module  # noqa: E402
from flux_engine.calibrate.fit import (  # noqa: E402
    FitRequest,
    check_output,
    fit_ceilings,
    patch_path_of,
    run_fit,
)
from flux_engine.calibrate.parameters import FitParameter  # noqa: E402
from flux_engine.calibrate.search import CmaCalibrator, Evaluation, cma_seed  # noqa: E402
from flux_engine.cli import main  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.seam.parameters import Source  # noqa: E402
from tests.calibrate._fit_bundle import (  # noqa: E402
    NAMESPACE,
    NULLS,
    SIMULATION,
    copy_fit_bundle,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "consumers"
PROFILE = FIXTURES / "subscriptions.profile.json"
HELD_OUT = FIXTURES / "subscriptions.held-out-profile.json"
LEVELS = "/extensions/com.agentics.flux~11/emits/subscriptions/fidelity/columns/renewed/levels/true"


def copy_bundle(root: Path, change: Optional[Any] = None) -> Path:
    """A copy of the bundle a fit can move (:mod:`tests.calibrate._fit_bundle`), edited by *change*."""
    bundle = copy_fit_bundle(root)
    if change is not None:
        path = bundle / SIMULATION
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        change(document["extensions"][NAMESPACE])
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle


def request(bundle: Path, root: Path, **more: Any) -> FitRequest:
    return FitRequest(
        bundle=bundle,
        profile=PROFILE,
        out=root / "calibrated",
        state_dir=root / "state",
        **more,
    )


def calibration(**more: Any) -> Any:
    """A *change* for :func:`copy_bundle` that replaces keys of the fixture's calibration block."""

    def change(namespace: Dict[str, Any]) -> None:
        block = copy.deepcopy(namespace["consumers"]["calibration"])
        block.update(more)
        namespace["consumers"]["calibration"] = block

    return change


def parameter(**more: Any) -> Any:
    """A *change* that replaces keys of the fixture's one calibration parameter."""

    def change(namespace: Dict[str, Any]) -> None:
        item = namespace["consumers"]["calibration"]["parameters"][0]
        item.update(more)

    return change


@pytest.fixture(scope="module")
def fitted(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    """The fit of the fixture, twice, under two state directories and two output names."""
    roots = [tmp_path_factory.mktemp("fit-a"), tmp_path_factory.mktemp("fit-b")]
    outcomes = []
    for root in roots:
        bundle = copy_bundle(root)
        outcomes.append(run_fit(request(bundle, root)))
    return {"outcomes": outcomes, "roots": roots}


# ----------------------------------------------------------------------------- the patch


def test_the_same_bundle_and_profile_give_the_same_patch_bytes(fitted: Dict[str, Any]) -> None:
    first, second = fitted["outcomes"]
    assert first.patch_text == second.patch_text
    assert first.patch_digest == second.patch_digest
    assert first.patch_path.read_text(encoding="utf-8") == first.patch_text


def test_the_patch_is_pinned(fitted: Dict[str, Any]) -> None:
    """The patch is the record, not the search: another platform may find another (ADR 0028 section 8).

    So what does not depend on the search is pinned exactly, and what the search finds is held to
    its bounds and grid: on the maintainers' machine the fit finds ``0.03`` in 24 evaluations, and
    a platform whose linear algebra rounds otherwise may stop at another point of the grid. Every
    evaluation draws the same rows (common random numbers), so the fit follows the value and not seed
    noise: with no NULLs at all the premium plan renews in about a ninth of the rows, short of the
    profile's rate, and the gap that remains is the bundle's, not the search's.
    """
    outcome = fitted["outcomes"][0]
    patch = json.loads(outcome.patch_text)
    (operation,) = patch["operations"]
    assert {key: operation[key] for key in ("document", "op", "path")} == {
        "document": SIMULATION,
        "op": "replace",
        "path": NULLS,
    }
    value = Fraction(operation["value"])
    assert Fraction(0) <= value <= Fraction(1, 2) and (value * 100).denominator == 1
    assert patch["method"] == "flux.calibration.cma/1"
    assert (patch["seed"], patch["scale"]) == (7, "0.25")
    assert 1 <= patch["evaluations"] <= 64
    assert (
        patch["profile"]
        == "sha256:ef1a063d4ef33fabbd008bcf167218bc2e69cb9d2eed66d321d0009abcf55eda"
    )
    assert [item["query"] for item in patch["gaps"]] == ["premium-renewed", "renewed-offered"]
    assert Fraction(-1, 20) <= Fraction(patch["gaps"][0]["gap"]) < 0
    assert patch["gaps"][1]["gap"] is None


def test_the_header_names_the_versions_of_what_made_it(fitted: Dict[str, Any]) -> None:
    import cma
    import duckdb
    import sqlglot

    patch = json.loads(fitted["outcomes"][0].patch_text)
    assert patch["versions"] == {
        "cma": cma.__version__,
        "duckdb": duckdb.__version__,
        "sqlglot": sqlglot.__version__,
    }
    assert set(patch) == {
        "method",
        "versions",
        "seed",
        "evaluations",
        "scale",
        "profile",
        "gaps",
        "operations",
    }


def test_every_operation_is_a_replace_on_a_declared_pointer(fitted: Dict[str, Any]) -> None:
    outcome = fitted["outcomes"][0]
    declared = {(item.document, item.pointer) for item in outcome.registry.calibration.parameters}
    for operation in json.loads(outcome.patch_text)["operations"]:
        assert set(operation) == {"document", "op", "path", "value"}
        assert operation["op"] == "replace"
        assert (operation["document"], operation["path"]) in declared
        assert isinstance(operation["value"], str)  # exact decimal text, never a float


def test_the_moved_value_is_on_the_step_and_inside_the_range(fitted: Dict[str, Any]) -> None:
    (operation,) = fitted["outcomes"][0].operations
    value = Fraction(operation.value)
    assert Fraction(0) <= value <= Fraction(1, 2)
    assert value % Fraction(1, 100) == 0


def test_the_fit_writes_the_patch_beside_the_output_and_nothing_else(
    fitted: Dict[str, Any],
) -> None:
    root = fitted["roots"][0]
    assert (root / "calibrated.patch.json").is_file()
    assert not (root / "calibrated").exists()
    assert patch_path_of(root / "calibrated") == root / "calibrated.patch.json"


def test_at_most_the_declared_evaluations_are_run(fitted: Dict[str, Any]) -> None:
    result = fitted["outcomes"][0].result
    assert 1 <= result.evaluations <= 24
    assert result.runs <= result.evaluations


def test_the_input_bundle_is_never_changed(fitted: Dict[str, Any], tmp_path: Path) -> None:
    root = fitted["roots"][0]
    pristine = copy_fit_bundle(tmp_path)
    before = {path.name: path.read_bytes() for path in pristine.iterdir()}
    after = {path.name: path.read_bytes() for path in (root / "bundle").iterdir()}
    assert before == after


# ----------------------------------------------------------------------------- one process


def test_the_whole_fit_counts_in_one_query_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: List[Any] = []
    real = subprocess.Popen

    def counting(command: Any, *args: Any, **kwargs: Any) -> Any:
        if "flux_engine.consumers._child" in list(command):
            started.append(command)
        return real(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counting)
    bundle = copy_bundle(tmp_path, calibration(evaluations=8))
    outcome = run_fit(request(bundle, tmp_path))
    assert outcome.result.runs >= 2
    assert len(started) == 1


def test_no_run_and_no_copy_is_left_under_the_state_directory(fitted: Dict[str, Any]) -> None:
    state = fitted["roots"][0] / "state"
    assert state.is_dir()
    assert list(state.iterdir()) == []


def test_a_candidate_that_is_refused_stops_the_fit_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.api.stages import StageResult

    run_stage = importlib.import_module(
        "flux_engine.stages.run"
    )  # the package re-exports the function

    def refused(*args: Any, **kwargs: Any) -> StageResult:
        return StageResult(
            stage="run", exit_code=1, stderr="FLX-3104: not a bundle", error_code="FLX-3104"
        )

    monkeypatch.setattr(run_stage, "run_on_common_draws", refused)
    bundle = copy_bundle(tmp_path)
    with pytest.raises(FluxError) as error:
        run_fit(request(bundle, tmp_path))
    assert error.value.code.code == "FLX-7101"
    assert "evaluation 1" in str(error.value) and "FLX-3104" in str(error.value)
    assert list((tmp_path / "state").iterdir()) == []
    assert not (tmp_path / "calibrated.patch.json").exists()


# ----------------------------------------------------------------------------- refusals


def refusal(tmp_path: Path, change: Any, **more: Any) -> FluxError:
    bundle = copy_bundle(tmp_path, change)
    with pytest.raises(FluxError) as error:
        run_fit(request(bundle, tmp_path, **more))
    assert not list((tmp_path / "state").glob("*"))
    assert not (tmp_path / "calibrated.patch.json").exists()
    return error.value


def test_a_value_the_profile_states_is_refused_with_its_source(tmp_path: Path) -> None:
    error = refusal(tmp_path, parameter(pointer=LEVELS, min="0.5", max="0.9", step="0.01"))
    assert error.code.code == "FLX-3705"
    assert "profile" in str(error) and "levels/true" in str(error)


def test_a_value_the_contract_states_is_refused(tmp_path: Path) -> None:
    pointer = LEVELS.replace("renewed/levels/true", "plan/maxCardinality")
    error = refusal(tmp_path, parameter(pointer=pointer, min="1", max="5", step="1"))
    assert error.code.code == "FLX-3705" and "contract" in str(error)


def test_a_default_value_may_be_moved(tmp_path: Path) -> None:
    """Dropping the register's entry leaves the number a default, which a fit may move."""

    def drop(namespace: Dict[str, Any]) -> None:
        namespace["parameters"] = [
            item for item in namespace["parameters"] if item["pointer"] != NULLS
        ]

    bundle = copy_bundle(tmp_path, drop)
    from flux_engine.calibrate.fit import _prepare

    prepared = _prepare(request(bundle, tmp_path), fit_ceilings(None, None, None))
    assert [item.source for item in prepared.parameters] == [Source.DEFAULT]


def test_a_document_outside_the_bundle_s_flux_files_is_refused(tmp_path: Path) -> None:
    error = refusal(tmp_path, parameter(document="subscriptions.fluid.yaml"))
    assert error.code.code == "FLX-3706" and "subscriptions.fluid.yaml" in str(error)
    error = refusal(tmp_path / "b", parameter(document="elsewhere.flux.yml"))
    assert error.code.code == "FLX-3706"


def test_a_pointer_that_reaches_no_number_is_refused(tmp_path: Path) -> None:
    error = refusal(tmp_path, parameter(pointer=NULLS.replace("renewed", "absent")))
    assert error.code.code == "FLX-3706" and "reaches no single number" in str(error)
    error = refusal(tmp_path / "b", parameter(pointer="/spec/emits"))
    assert error.code.code == "FLX-3706"


def test_a_value_outside_its_range_and_a_step_with_no_value_are_refused(tmp_path: Path) -> None:
    error = refusal(tmp_path, parameter(min="0.2", max="0.5"))
    assert error.code.code == "FLX-3706" and "outside min" in str(error)
    error = refusal(tmp_path / "b", parameter(min="0.09", max="0.11", step="0.5"))
    assert error.code.code == "FLX-3706" and "no multiple of step" in str(error)


def test_two_parameters_that_name_one_number_are_refused(tmp_path: Path) -> None:
    def twice(namespace: Dict[str, Any]) -> None:
        block = namespace["consumers"]["calibration"]
        block["parameters"] = [block["parameters"][0], copy.deepcopy(block["parameters"][0])]

    error = refusal(tmp_path, twice)
    assert error.code.code == "FLX-3706" and "same number" in str(error)


def test_evaluations_past_the_engine_ceiling_are_refused_in_the_bundle(tmp_path: Path) -> None:
    error = refusal(tmp_path, calibration(evaluations=65))
    assert error.code.code == "FLX-3616"


@pytest.mark.parametrize("value", [1, 63, 4097])
def test_max_evaluations_outside_its_range_is_refused(tmp_path: Path, value: int) -> None:
    error = refusal(tmp_path, None, max_evaluations=value)
    assert error.code.code == "FLX-3704" and "--max-evaluations" in str(error)


def test_a_query_flag_outside_its_range_is_still_flx_3700(tmp_path: Path) -> None:
    error = refusal(tmp_path, None, max_query_seconds=700)
    assert error.code.code == "FLX-3700"


def test_the_budget_is_the_bundle_s_value_when_it_declares_one_else_the_raised_ceiling(
    tmp_path: Path,
) -> None:
    from flux_engine.calibrate.fit import _prepare

    declared = copy_bundle(tmp_path / "declared", calibration(evaluations=24))
    undeclared = copy_bundle(
        tmp_path / "undeclared", lambda ns: ns["consumers"]["calibration"].pop("evaluations")
    )
    for ceiling, expected in ((None, (24, 64)), (200, (24, 200)), (4096, (24, 4096))):
        ceilings = fit_ceilings(None, None, ceiling)
        got = tuple(
            _prepare(request(bundle, tmp_path), ceilings).evaluations
            for bundle in (declared, undeclared)
        )
        assert got == expected


def test_a_flag_never_reaches_the_loader(tmp_path: Path) -> None:
    """The bundle is read against the engine's own ceilings, so a raised flag cannot admit a larger value."""
    error = refusal(tmp_path, calibration(evaluations=100), max_evaluations=200)
    assert error.code.code == "FLX-3616"


def test_a_bundle_with_no_calibration_parameters_has_nothing_to_fit(tmp_path: Path) -> None:
    error = refusal(tmp_path, lambda ns: ns["consumers"].pop("calibration"))
    assert error.code.code == "FLX-3707"


def test_a_bundle_that_declares_no_consumers_has_nothing_to_fit(tmp_path: Path) -> None:
    error = refusal(tmp_path, lambda ns: ns.pop("consumers"))
    assert error.code.code == "FLX-3707"


def test_a_profile_that_states_no_calibration_rate_has_nothing_to_fit(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    document = json.loads(PROFILE.read_text(encoding="utf-8"))
    document["queries"] = []
    profile = tmp_path / "empty.profile.json"
    profile.write_text(json.dumps(document), encoding="utf-8")
    from flux_engine.seam.profile import load_profile

    path = bundle / SIMULATION
    simulation = yaml.safe_load(path.read_text(encoding="utf-8"))
    simulation["extensions"][NAMESPACE]["profile"]["digest"] = load_profile(profile).digest.value
    path.write_text(yaml.safe_dump(simulation, sort_keys=False), encoding="utf-8")
    with pytest.raises(FluxError) as error:
        run_fit(FitRequest(bundle, profile, tmp_path / "calibrated", state_dir=tmp_path / "state"))
    assert error.value.code.code == "FLX-3707" and "no calibration query" in str(error.value)


def test_a_profile_that_names_a_held_out_query_is_refused_before_a_run(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    document = json.loads(PROFILE.read_text(encoding="utf-8"))
    document["queries"].append(
        json.loads((FIXTURES / "subscriptions.held-out-profile.json").read_text())["queries"][0]
    )
    profile = tmp_path / "leaky.profile.json"
    profile.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FluxError) as error:
        run_fit(FitRequest(bundle, profile, tmp_path / "calibrated", state_dir=tmp_path / "state"))
    assert error.value.code.code == "FLX-3701" and "renewed-since" in str(error.value)


def test_another_sqlglot_than_the_one_that_compiled_the_queries_is_refused(tmp_path: Path) -> None:
    def other(namespace: Dict[str, Any]) -> None:
        namespace["consumers"]["compiledWith"] = {"sqlglot": "1.0.0"}

    error = refusal(tmp_path, other)
    assert error.code.code == "FLX-3709" and "1.0.0" in str(error)


def test_a_missing_extra_is_refused_with_the_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fit_module, "is_available", lambda name: name != "cma")
    bundle = copy_bundle(tmp_path)
    with pytest.raises(FluxError) as error:
        run_fit(request(bundle, tmp_path))
    assert error.value.code.code == "FLX-3708"
    assert "cma" in str(error.value) and "flux-sim-engine[calibrate]" in str(error.value)


def test_an_output_name_that_is_taken_is_refused_before_the_fit_starts(tmp_path: Path) -> None:
    bundle = copy_bundle(tmp_path)
    (tmp_path / "calibrated").mkdir()
    with pytest.raises(FluxError) as error:
        run_fit(request(bundle, tmp_path))
    assert error.value.code.code == "FLX-3710"
    (tmp_path / "calibrated").rmdir()
    (tmp_path / "calibrated.patch.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FluxError):
        run_fit(request(bundle, tmp_path))
    assert not (tmp_path / "state").exists()  # nothing ran


def test_the_output_may_not_be_the_bundle_or_inside_it_or_in_a_missing_directory(
    tmp_path: Path,
) -> None:
    bundle = copy_bundle(tmp_path)
    for out in (bundle, bundle / "inside", tmp_path / "missing" / "calibrated"):
        with pytest.raises(FluxError) as error:
            check_output(out, bundle)
        assert error.value.code.code == "FLX-3710"


# ----------------------------------------------------------------------------- the command


def command(*arguments: str) -> int:
    return main(["calibrate", *arguments])


def test_the_command_fits_and_reports_the_patch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = copy_bundle(tmp_path, calibration(evaluations=4, heldOutTolerance="0.5"))
    status = command(
        "--fit",
        str(bundle),
        "--profile",
        str(PROFILE),
        "--held-out",
        str(HELD_OUT),
        "--out",
        str(tmp_path / "calibrated"),
        "--state-dir",
        str(tmp_path / "state"),
        "--json",
    )
    document = json.loads(capsys.readouterr().out)
    assert status == 0
    patch = tmp_path / "calibrated.patch.json"
    assert document["artifacts"]["patch"]["path"] == str(patch)
    assert document["artifacts"]["patch"]["digest"].startswith("sha256:")
    assert document["artifacts"]["method"] == "flux.calibration.cma/1"
    assert patch.is_file()


def test_the_command_reports_a_refusal_with_its_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = copy_bundle(tmp_path)
    status = command(
        "--fit",
        str(bundle),
        "--profile",
        str(PROFILE),
        "--held-out",
        str(HELD_OUT),
        "--out",
        str(tmp_path / "calibrated"),
        "--max-evaluations",
        "10",
        "--json",
    )
    document = json.loads(capsys.readouterr().out)
    assert status == 1
    assert "FLX-3704" in json.dumps(document)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--fit", "b", "--out", "o"],
        ["--fit", "b", "--profile", "p"],
        ["--fit", "b", "--profile", "p", "--out", "o"],
        ["b", "--fit", "b", "--profile", "p", "--out", "o"],
        ["b", "--profile", "p"],
        ["b", "--out", "o"],
        ["b", "--held-out", "h"],
        ["b", "--max-evaluations", "100"],
        [],
    ],
)
def test_the_fit_options_belong_to_the_fit(arguments: List[str], capsys: Any) -> None:
    with pytest.raises(SystemExit) as stop:
        command(*arguments)
    assert stop.value.code == 2
    capsys.readouterr()


def test_the_declared_mode_is_unchanged(tmp_path: Path, capsys: Any) -> None:
    starter = REPO_ROOT / "flux_engine" / "starter" / "bundle"
    status = command(str(starter))
    out = capsys.readouterr().out
    assert status in (0, 1)  # the starter bundle has a Playback or not; the point is it is read
    assert "fit" not in out


# ----------------------------------------------------------------------------- the calibrator


def toy(name: str, low: str, high: str, step: str, before: str) -> FitParameter:
    return FitParameter(
        document="d.flux.yml",
        doc_id="d",
        pointer=f"/{name}",
        tokens=(name,),
        minimum=Fraction(low),
        maximum=Fraction(high),
        step=Fraction(step),
        before=Fraction(before),
        source=Source.ASSUMPTION,
    )


def bowl(target: str) -> Any:
    goal = Fraction(target)

    def evaluate(values: Any) -> Evaluation:
        total = sum(((value - goal) ** 2 for value in values), Fraction(0))
        return Evaluation(total, (("q", total),))

    return evaluate


def test_the_fit_is_a_calibrator_with_its_method_id() -> None:
    calibrator = CmaCalibrator(
        [toy("a", "0", "1", "0.01", "0.5")], bowl("0.3"), evaluations=4, seed=1
    )
    assert isinstance(calibrator, Calibrator)
    assert calibrator.method_id == "flux.calibration.cma/1"


def test_calibrate_returns_the_fitted_dials_and_a_scorecard_that_grades_nothing() -> None:
    parameters = [toy("a", "0", "1", "0.01", "0.5"), toy("b", "0", "1", "0.01", "0.5")]
    calibrator = CmaCalibrator(parameters, bowl("0.3"), evaluations=60, seed=7)
    dials, scorecard = calibrator.calibrate(
        {"profile": "sha256:" + "0" * 64, "queries": ["q"]},
        {"d.flux.yml#/a": __import__("decimal").Decimal("0.5")},
    )
    assert set(dials) == {"d.flux.yml#/a", "d.flux.yml#/b"}
    assert all(Fraction(value) % Fraction(1, 100) == 0 for value in dials.values())
    assert all(
        abs(value - __import__("decimal").Decimal("0.3")) < __import__("decimal").Decimal("0.2")
        for value in dials.values()
    )
    assert scorecard.provenance is Provenance.FITTED
    assert scorecard.credibility is Credibility.NOT_ESTABLISHED
    assert scorecard.grade is None and scorecard.method_id == "flux.calibration.cma/1"
    assert scorecard.evidence_digest is not None


def test_a_seed_gives_the_same_search_and_another_seed_another() -> None:
    parameters = [toy("a", "0", "1", "0.001", "0.5"), toy("b", "0", "1", "0.001", "0.5")]
    seen: Dict[int, List[Any]] = {}
    for seed in (3, 3, 4):
        trail: List[Any] = []

        def evaluate(values: Any, trail: List[Any] = trail) -> Evaluation:
            trail.append(values)
            return bowl("0.3")(values)

        CmaCalibrator(parameters, evaluate, evaluations=12, seed=seed).calibrate({}, {})
        seen.setdefault(seed, []).append(trail)
    assert seen[3][0] == seen[3][1]
    assert seen[3][0] != seen[4][0]


@pytest.mark.parametrize("evaluations", [1, 3, 7, 13])
def test_at_most_the_budget_is_evaluated_even_inside_a_generation(evaluations: int) -> None:
    parameters = [toy("a", "0", "1", "0.001", "0.5"), toy("b", "0", "1", "0.001", "0.5")]
    calls: List[Any] = []

    def evaluate(values: Any) -> Evaluation:
        calls.append(values)
        return bowl("0.3")(values)

    calibrator = CmaCalibrator(parameters, evaluate, evaluations=evaluations, seed=2)
    calibrator.calibrate({}, {})
    assert calibrator.result is not None
    assert calibrator.result.evaluations == evaluations
    assert len(calls) == calibrator.result.runs <= evaluations


def test_one_parameter_is_fitted_although_cma_has_no_one_dimensional_search() -> None:
    calibrator = CmaCalibrator(
        [toy("a", "0", "1", "0.01", "0.9")], bowl("0.3"), evaluations=30, seed=5
    )
    dials, _ = calibrator.calibrate({}, {})
    assert abs(float(dials["d.flux.yml#/a"]) - 0.3) < 0.15


def test_a_candidate_that_rounds_to_an_earlier_one_is_not_run_again() -> None:
    calls: List[Any] = []

    def evaluate(values: Any) -> Evaluation:
        calls.append(values)
        return bowl("0.3")(values)

    calibrator = CmaCalibrator([toy("a", "0", "1", "0.5", "0.5")], evaluate, evaluations=20, seed=1)
    calibrator.calibrate({}, {})
    assert calibrator.result is not None
    assert calibrator.result.evaluations > len(calls)  # candidates counted, runs fewer
    assert len(calls) <= 3  # the grid 0, 0.5, 1 holds three values


def test_of_equal_candidates_the_earlier_wins() -> None:
    order: List[Any] = []

    def flat(values: Any) -> Evaluation:
        order.append(values)
        return Evaluation(Fraction(0), ())

    calibrator = CmaCalibrator(
        [toy("a", "0", "1", "0.01", "0.5"), toy("b", "0", "1", "0.01", "0.5")],
        flat,
        evaluations=10,
        seed=9,
    )
    calibrator.calibrate({}, {})
    assert calibrator.result is not None
    assert calibrator.result.values == order[0]


def test_a_seed_of_zero_is_not_read_as_the_clock() -> None:
    assert cma_seed(0) == 1
    assert 1 <= cma_seed(-5) <= 2**31 - 1
    assert 1 <= cma_seed(2**40) <= 2**31 - 1
    assert cma_seed(3) != cma_seed(4)


def test_a_fit_puts_numpy_s_global_generator_back() -> None:
    import numpy as np

    np.random.seed(123)
    expected = np.random.get_state()[1][:5].tolist()
    np.random.seed(123)
    CmaCalibrator(
        [toy("a", "0", "1", "0.01", "0.5")], bowl("0.3"), evaluations=6, seed=1
    ).calibrate({}, {})
    assert np.random.get_state()[1][:5].tolist() == expected


def test_a_value_is_rounded_to_its_step_and_kept_inside_its_range() -> None:
    item = toy("a", "0.05", "0.60", "0.0001", "0.1")
    assert item.snap(0.0) == Fraction(5, 100)
    assert item.snap(1.0) == Fraction(60, 100)
    assert item.snap(0.5) == Fraction(5, 100) + Fraction(55, 100) * Fraction(1, 2)
    assert (item.snap(0.123456789) / Fraction(1, 10000)).denominator == 1
    coarse = toy("a", "0.12", "0.58", "0.25", "0.25")
    assert {coarse.snap(unit) for unit in (0.0, 0.3, 0.5, 1.0)} <= {Fraction(1, 4), Fraction(1, 2)}
    assert item.unit_of(Fraction("0.05")) == 0.0 and item.unit_of(Fraction("0.6")) == 1.0


def test_importing_the_fit_imports_neither_cma_nor_the_sql_libraries() -> None:
    code = (
        "import sys, flux_engine.calibrate.fit, flux_engine.calibrate.search;"
        "print(sorted(m for m in ('cma','sqlglot','duckdb','pyarrow') if m in sys.modules))"
    )
    done = subprocess.run(
        [__import__("sys").executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "[]"
