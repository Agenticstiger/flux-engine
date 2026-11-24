# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle is called calibrated only when held-out error holds (ADR 0028 section 9).

The bundle is the fit bundle of :mod:`tests.calibrate._fit_bundle`: one calibration query is fitted and one
query, ``renewed-since``, is held out. Two held-out profiles state its rate: the fixture's, whose target the
calibrated run lands near, and one whose target is far from it.

* **Order.** The fit reads the pinned profile only. The patch is written and its digest taken before the
  held-out profile is opened, and the register records that digest.
* **The gate.** Calibrated only when at least one held-out query has a target and every one that has a target
  is within its tolerance. Then the new bundle is written. Otherwise the patch and the report stand, the
  command exits with status 1 and names each query that missed with its gap, and no bundle is written.
* **Grades.** The held-out profile is graded under ADR 0020's rule 2 and its grade is stated beside the pinned
  profile's.
* **End to end.** ``calibrate --fit`` and then ``plan``, ``apply`` and ``verify`` on the new bundle.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("cma")

from flux_engine.calibrate import fit as fit_module  # noqa: E402
from flux_engine.calibrate.fit import FitRequest, HeldOutNeeded, run_fit  # noqa: E402
from flux_engine.cli import main  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.evidence.grades import grade_profile  # noqa: E402
from flux_engine.seam.profile import load_profile  # noqa: E402
from tests.bundles import edit_simulation  # noqa: E402
from tests.calibrate._fit_bundle import (  # noqa: E402
    NAMESPACE,
    SIMULATION,
    copy_fit_bundle,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "consumers"
PROFILE = FIXTURES / "subscriptions.profile.json"
HELD_OUT = FIXTURES / "subscriptions.held-out-profile.json"


def make(root: Path, *, tolerance: Optional[str] = "0.1", evaluations: int = 6) -> Path:
    """The fit bundle, with a held-out tolerance and a small budget (the gate does not need more)."""

    def change(document: Dict[str, Any]) -> None:
        block = document["extensions"][NAMESPACE]["consumers"]["calibration"]
        block["evaluations"] = evaluations
        if tolerance is not None:
            block["heldOutTolerance"] = tolerance

    return edit_simulation(copy_fit_bundle(root), change)


def held_out_profile(root: Path, name: str, change: Any) -> Path:
    document = json.loads(HELD_OUT.read_text(encoding="utf-8"))
    change(document)
    path = root / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def far(document: Dict[str, Any]) -> None:
    """A target of one in five, where the calibrated run counts about two in three."""
    document["queries"][0]["statistics"][0]["groups"][0]["levelCounts"] = [960, 240]


def request(
    bundle: Path, root: Path, held_out: Optional[Path] = HELD_OUT, **more: Any
) -> FitRequest:
    return FitRequest(
        bundle=bundle,
        profile=PROFILE,
        out=root / "calibrated",
        held_out=held_out,
        state_dir=root / "state",
        **more,
    )


def snapshot(directory: Path) -> Dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


@pytest.fixture(scope="module")
def passed(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    root = tmp_path_factory.mktemp("passed")
    bundle = make(root)
    before = snapshot(bundle)
    return {
        "root": root,
        "bundle": bundle,
        "before": before,
        "outcome": run_fit(request(bundle, root)),
    }


# ----------------------------------------------------------------------------- the gate holds


def test_the_bundle_is_written_when_the_held_out_error_holds(passed: Dict[str, Any]) -> None:
    outcome, root = passed["outcome"], passed["root"]
    assert outcome.calibrated and outcome.gate is not None and outcome.gate.calibrated
    assert outcome.bundle is not None and outcome.bundle.path == root / "calibrated"
    assert (root / "calibrated" / SIMULATION).is_file()
    assert (root / "calibrated.patch.json").read_text(encoding="utf-8") == outcome.patch_text
    assert snapshot(passed["bundle"]) == passed["before"]  # the input is never touched


def test_the_held_out_query_is_judged_against_the_block_s_tolerance(passed: Dict[str, Any]) -> None:
    (query,) = passed["outcome"].gate.queries
    assert (query.id, query.status, query.within) == ("renewed-since", "measured", True)
    assert query.tolerance == Fraction(1, 10) and query.gap is not None
    assert abs(query.gap) <= query.tolerance
    assert passed["outcome"].gate.untargeted == ()


def test_the_register_records_the_patch_the_targets_and_the_held_out_evidence(
    passed: Dict[str, Any],
) -> None:
    outcome = passed["outcome"]
    document = yaml.safe_load((passed["root"] / "calibrated" / SIMULATION).read_text("utf-8"))
    entries = [
        entry
        for entry in document["extensions"][NAMESPACE]["parameters"]
        if entry["source"] == "calibrated"
    ]
    assert len(outcome.operations) == 1
    (entry,) = entries
    calibration = entry["calibration"]
    assert calibration["patch"] == outcome.patch_digest
    assert calibration["targets"] == load_profile(PROFILE).digest.value
    assert calibration["queries"] == ["premium-renewed"]
    assert calibration["after"] == outcome.operations[0].value and calibration["before"] == "0.1"
    held = calibration["heldOut"]
    assert held["profile"] == load_profile(HELD_OUT).digest.value
    assert held["tolerance"] == "0.1"
    gap = abs(outcome.gate.queries[0].gap)
    assert Fraction(held["largestGap"]) >= gap  # never understates the gap
    assert Fraction(held["largestGap"]) - gap < Fraction(1, 10**11)


def test_the_report_states_the_two_grades_side_by_side(passed: Dict[str, Any]) -> None:
    gate = passed["outcome"].gate
    expected_pinned = grade_profile(load_profile(PROFILE).document, pinned=True).pattern.value
    expected_held = grade_profile(load_profile(HELD_OUT).document, pinned=True).pattern.value
    assert (gate.pinned_grade, gate.grade) == (expected_pinned, expected_held)
    assert gate.profile == load_profile(HELD_OUT).digest.value


# ----------------------------------------------------------------------------- the order


def test_the_held_out_profile_is_opened_only_after_the_patch_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: List[Tuple[str, bool, Optional[str]]] = []
    real = fit_module.load_profile
    patch = tmp_path / "calibrated.patch.json"

    def watching(path: Path) -> Any:
        digest = None
        if patch.exists():
            digest = fit_module.patch_digest(patch.read_text(encoding="utf-8"))
        calls.append((Path(path).name, patch.exists(), digest))
        return real(path)

    monkeypatch.setattr(fit_module, "load_profile", watching)
    bundle = make(tmp_path)
    outcome = run_fit(request(bundle, tmp_path))
    assert [name for name, _, _ in calls] == [PROFILE.name, HELD_OUT.name]
    assert calls[0][1] is False  # the pinned profile is read before anything is written
    assert calls[1][1] is True  # the held-out profile, only once the patch is there
    assert (
        calls[1][2] == outcome.patch_digest
    )  # and the digest taken is the one the register carries


def test_the_fit_never_reads_the_held_out_file_before_the_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: List[Tuple[str, bool]] = []
    patch = tmp_path / "calibrated.patch.json"
    real_read = Path.read_text

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == HELD_OUT.name:
            opened.append((self.name, patch.exists()))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    run_fit(request(make(tmp_path), tmp_path))
    assert opened == [(HELD_OUT.name, True)]


# ----------------------------------------------------------------------------- the gate fails


def test_a_held_out_query_that_misses_leaves_the_patch_and_no_bundle(tmp_path: Path) -> None:
    held = held_out_profile(tmp_path, "far.json", far)
    outcome = run_fit(request(make(tmp_path), tmp_path, held))
    assert not outcome.calibrated and outcome.bundle is None
    assert outcome.gate is not None and not outcome.gate.calibrated
    (missed,) = outcome.gate.missed
    assert (
        missed.id == "renewed-since"
        and missed.gap is not None
        and abs(missed.gap) > Fraction(1, 10)
    )
    assert "larger than its tolerance" in missed.why
    assert (tmp_path / "calibrated.patch.json").is_file()
    assert not (tmp_path / "calibrated").exists()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []


def test_the_command_exits_with_status_1_and_names_the_query_that_missed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    held = held_out_profile(tmp_path, "far.json", far)
    status = main(
        [
            "calibrate",
            "--fit",
            str(make(tmp_path)),
            "--profile",
            str(PROFILE),
            "--held-out",
            str(held),
            "--out",
            str(tmp_path / "calibrated"),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    captured = capsys.readouterr()
    assert status == 1
    assert "held-out query renewed-since: gap" in captured.out and "missed" in captured.out
    assert "FLX-3712" in captured.err and "renewed-since" in captured.err and "gap" in captured.err
    assert (tmp_path / "calibrated.patch.json").is_file() and not (tmp_path / "calibrated").exists()


def test_the_json_report_carries_the_held_out_check_when_it_misses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    held = held_out_profile(tmp_path, "far.json", far)
    status = main(
        [
            "calibrate",
            "--fit",
            str(make(tmp_path)),
            "--profile",
            str(PROFILE),
            "--held-out",
            str(held),
            "--out",
            str(tmp_path / "calibrated"),
            "--state-dir",
            str(tmp_path / "state"),
            "--json",
        ]
    )
    document = json.loads(capsys.readouterr().out)
    assert status == 1 and "FLX-3712" in json.dumps(document)
    artifacts = document["artifacts"]
    assert artifacts["calibrated"] is False and artifacts["bundle"] is None
    (query,) = artifacts["heldOut"]["queries"]
    assert query["id"] == "renewed-since" and query["within"] is False and query["gap"] is not None
    assert artifacts["heldOut"]["grade"] and artifacts["heldOut"]["pinnedGrade"]


def test_a_fit_with_no_held_out_target_is_never_called_calibrated(tmp_path: Path) -> None:
    held = held_out_profile(tmp_path, "none.json", lambda document: document.update(queries=[]))
    outcome = run_fit(request(make(tmp_path), tmp_path, held))
    assert not outcome.calibrated and outcome.bundle is None
    assert outcome.gate is not None and outcome.gate.queries == ()
    assert outcome.gate.untargeted == ("renewed-since",)
    assert not (tmp_path / "calibrated").exists() and (tmp_path / "calibrated.patch.json").is_file()


def test_a_held_out_query_with_no_tolerance_cannot_be_called_within(tmp_path: Path) -> None:
    outcome = run_fit(request(make(tmp_path, tolerance=None), tmp_path))
    assert not outcome.calibrated
    (missed,) = outcome.gate.missed if outcome.gate is not None else ()
    assert "no tolerance" in missed.why


def test_a_command_with_no_held_out_target_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    held = held_out_profile(tmp_path, "none.json", lambda document: document.update(queries=[]))
    status = main(
        [
            "calibrate",
            "--fit",
            str(make(tmp_path)),
            "--profile",
            str(PROFILE),
            "--held-out",
            str(held),
            "--out",
            str(tmp_path / "calibrated"),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    assert status == 1 and "no held-out query has a target" in capsys.readouterr().err


# ----------------------------------------------------------------------------- refusals


def test_a_held_out_profile_that_names_a_calibration_query_is_refused_after_the_patch(
    tmp_path: Path,
) -> None:
    def names(document: Dict[str, Any]) -> None:
        pinned = json.loads(PROFILE.read_text(encoding="utf-8"))
        document["queries"].append(pinned["queries"][0])

    held = held_out_profile(tmp_path, "names.json", names)
    with pytest.raises(FluxError) as error:
        run_fit(request(make(tmp_path), tmp_path, held))
    assert error.value.code.code == "FLX-3702" and "premium-renewed" in str(error.value)
    assert not (tmp_path / "calibrated.patch.json").exists()  # a refusal leaves no patch behind
    assert not (tmp_path / "calibrated").exists()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []


def test_an_unreadable_held_out_profile_leaves_no_patch_and_the_rerun_is_not_refused(
    tmp_path: Path,
) -> None:
    held = tmp_path / "corrupt.json"
    held.write_text("{ not a profile", encoding="utf-8")
    bundle = make(tmp_path)
    with pytest.raises(FluxError) as error:
        run_fit(request(bundle, tmp_path, held))
    assert error.value.code.code == "FLX-5036"
    assert not (tmp_path / "calibrated.patch.json").exists()
    assert not (tmp_path / "calibrated").exists()
    outcome = run_fit(request(bundle, tmp_path))  # not FLX-3710 "already exists"
    assert outcome.calibrated


def without_held_out_query(root: Path) -> Path:

    def change(document: Dict[str, Any]) -> None:
        for query in document["extensions"][NAMESPACE]["consumers"]["queries"]:
            query.pop("heldOut", None)

    return edit_simulation(make(root), change)


def fit_command(bundle: Path, root: Path, *more: str) -> int:
    return main(
        [
            "calibrate",
            "--fit",
            str(bundle),
            "--profile",
            str(PROFILE),
            "--out",
            str(root / "calibrated"),
            "--state-dir",
            str(root / "state"),
            *more,
        ]
    )


def test_a_bundle_with_no_held_out_query_is_fitted_without_a_held_out_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = fit_command(without_held_out_query(tmp_path), tmp_path)
    captured = capsys.readouterr()
    assert status == 1 and "FLX-3712" in captured.err
    assert "the bundle declares no held-out query" in captured.out
    assert "no held-out check was made" in captured.out
    assert (tmp_path / "calibrated.patch.json").is_file() and not (tmp_path / "calibrated").exists()


def test_the_json_document_says_no_held_out_check_was_made(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fit_command(without_held_out_query(tmp_path), tmp_path, "--json")
    artifacts = json.loads(capsys.readouterr().out)["artifacts"]
    assert artifacts["heldOut"] is None and artifacts["calibrated"] is False
    assert "declares no held-out query" in artifacts["heldOutNote"]


def test_a_held_out_profile_given_to_a_bundle_with_no_held_out_query_is_not_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{ not a profile", encoding="utf-8")
    fit_command(without_held_out_query(tmp_path), tmp_path, "--held-out", str(corrupt), "--json")
    artifacts = json.loads(capsys.readouterr().out)["artifacts"]
    assert artifacts["heldOut"] is None
    assert "was not read" in artifacts["heldOutNote"]
    assert (tmp_path / "calibrated.patch.json").is_file()


def test_a_bundle_with_a_held_out_query_still_needs_a_held_out_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = fit_command(make(tmp_path), tmp_path)
    assert status == 2 and "--fit needs --held-out" in capsys.readouterr().err
    assert not (tmp_path / "calibrated.patch.json").exists() and not (tmp_path / "state").exists()


def test_a_held_out_profile_that_is_not_a_file_is_refused_before_any_run(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as error:
        run_fit(request(make(tmp_path), tmp_path, tmp_path / "missing.json"))
    assert error.value.code.code == "FLX-3109"
    assert not (tmp_path / "calibrated.patch.json").exists() and not (tmp_path / "state").exists()


def test_the_library_refuses_a_held_out_query_with_no_held_out_profile(tmp_path: Path) -> None:
    with pytest.raises(HeldOutNeeded):
        run_fit(request(make(tmp_path), tmp_path, None))
    assert not (tmp_path / "calibrated.patch.json").exists()


def test_nothing_is_left_under_the_state_directory(passed: Dict[str, Any]) -> None:
    assert list((passed["root"] / "state").iterdir()) == []


# ----------------------------------------------------------------------------- end to end


def test_fit_then_plan_apply_and_verify_the_calibrated_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = make(tmp_path)
    status = main(
        [
            "calibrate",
            "--fit",
            str(bundle),
            "--profile",
            str(PROFILE),
            "--held-out",
            str(HELD_OUT),
            "--out",
            str(tmp_path / "calibrated"),
            "--state-dir",
            str(tmp_path / "fit-state"),
            "--json",
        ]
    )
    fitted = json.loads(capsys.readouterr().out)
    assert status == 0 and fitted["artifacts"]["calibrated"] is True
    calibrated = tmp_path / "calibrated"
    assert fitted["artifacts"]["bundle"] == str(calibrated)
    state = str(tmp_path / "run-state")

    assert (
        main(["plan", str(calibrated), "--profile", str(PROFILE), "--state-dir", state, "--json"])
        == 0
    )
    planned = json.loads(capsys.readouterr().out)
    run_id = planned["artifacts"]["run_id"]
    assert main(["apply", str(calibrated), "--run-id", run_id, "--state-dir", state, "--json"]) == 0
    capsys.readouterr()
    status = main(
        [
            "verify",
            str(calibrated),
            "--run-id",
            run_id,
            "--profile",
            str(PROFILE),
            "--held-out",
            str(HELD_OUT),
            "--state-dir",
            state,
            "--json",
        ]
    )
    verified = json.loads(capsys.readouterr().out)
    assert status == 0
    register = json.loads(
        (tmp_path / "run-state" / "runs" / run_id / "parameter-register.json").read_text()
    )
    if fitted["artifacts"]["patch"]["operations"]:
        assert register["counts"]["calibrated"] == 1
    assert verified["artifacts"]["queries"]["status"] == "counted"


def test_a_fit_that_moves_nothing_writes_no_bundle_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    bundle = make(tmp_path)
    monkeypatch.setattr(fit_module, "_operations", lambda parameters, values: ())
    outcome = run_fit(request(bundle, tmp_path))
    assert not outcome.calibrated and outcome.bundle is None
    assert not (tmp_path / "calibrated").exists()
    from flux_engine.cli.commands.calibrate import _not_calibrated

    assert "no candidate beat the start point" in str(_not_calibrated(outcome))
