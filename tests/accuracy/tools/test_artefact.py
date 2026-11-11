# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The nightly artefact joiner: what it counts, and when it says a night failed.

Pass criteria: a night with every component present and green joins to a
passing verdict; each way a night can go wrong -- a failed measured case, a
pytest error the result log never saw, a PractRand FAIL, a digest that differs
between cells, a missing component, a benchmark three times slower than the
snapshot, a weekly mutation score below target -- joins to a failing verdict
that names it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from tests.accuracy.tools.artefact import REQUIRED_FAMILIES, build_artefact, main

JOBS_OK = {
    name: {"result": "success"} for name in ("nightly", "practrand", "determinism-compare", "seed")
} | {"mutation": {"result": "skipped"}}


def _write(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def _junit(failures: int = 0, errors: int = 0) -> str:
    cases = "".join(
        f'<testcase classname="t" name="f{i}"><failure/></testcase>' for i in range(failures)
    )
    cases += "".join(
        f'<testcase classname="t" name="e{i}"><error/></testcase>' for i in range(errors)
    )
    return (
        f'<testsuites><testsuite tests="10" failures="{failures}" errors="{errors}" skipped="1">'
        f"{cases}</testsuite></testsuites>"
    )


def _parts(
    tmp_path: Path,
    *,
    failed_case: bool = False,
    junit_errors: int = 0,
    practrand_fail: bool = False,
    differing_cell: bool = False,
    drop_practrand: bool = False,
    bench_median: float = 1e-5,
    mutation: Optional[Dict[str, str]] = None,
) -> Path:
    parts = tmp_path / "parts"
    records: List[Dict[str, Any]] = [
        {
            "family": family,
            "case": f"{family}-case",
            "test": f"t::{family}",
            "passed": not (failed_case and family == "gate-roc"),
            "criterion": "c",
            "metrics": {},
            "n": 10,
            "tier": "nightly",
            "baseSeed": 1,
        }
        for family in REQUIRED_FAMILIES
    ]
    _write(
        parts / "accuracy-nightly" / "results" / "results-gw0.jsonl",
        "".join(json.dumps(r) + "\n" for r in records),
    )
    _write(parts / "accuracy-nightly" / "junit.xml", _junit(errors=junit_errors))
    _write(
        parts / "accuracy-nightly" / "bench.json",
        {
            "benchmarks": [
                {
                    "name": "test_uniform53",
                    "stats": {
                        "mean": bench_median,
                        "median": bench_median,
                        "stddev": 0.0,
                        "rounds": 5,
                    },
                }
            ]
        },
    )
    if not drop_practrand:
        for mode in ("first-words", "interleave"):
            verdict = "fail" if practrand_fail and mode == "interleave" else "pass"
            _write(
                parts / "accuracy-practrand" / f"practrand-{mode}.json",
                {
                    "verdict": verdict,
                    "max_length_bytes": 1 << 30,
                    "evaluations": {"unusual": 1},
                    "anomalies": [],
                },
            )
    for index, cell in enumerate(("ubuntu-py3.12-numpylatest", "ubuntu-py3.11-numpy1.26.0")):
        digest = "sha256:bb" if differing_cell and index == 1 else "sha256:aa"
        _write(
            parts / f"accuracy-determinism-{cell}" / f"manifest-{cell}.json",
            {"digests": {"kernel/uniform53": digest}, "environment": {"python": "3.12"}},
        )
    if mutation is not None:
        _write(parts / "accuracy-mutation" / "mutation.json", {"killed": 1})
        _write(
            parts / "accuracy-mutation" / "mutation-results.txt",
            "".join(f"    {name}: {status}\n" for name, status in mutation.items()),
        )
    return parts


def _join(
    parts: Path,
    *,
    weekly: bool = False,
    baseline: Optional[Dict[str, Any]] = None,
    jobs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return build_artefact(
        parts=parts,
        run={"tier": "nightly", "baseSeed": 1, "date": "2026-11-11"},
        jobs=jobs if jobs is not None else JOBS_OK,
        baseline=baseline,
        weekly=weekly,
    )


def test_a_green_night_passes(tmp_path: Path) -> None:
    artefact = _join(_parts(tmp_path))
    assert artefact["verdict"] == {
        "passed": True,
        "reasons": [],
        "missing": [],
        "flagged": [],
        "knownDefects": [],
    }
    assert all(artefact["families"][family]["passed"] == 1 for family in REQUIRED_FAMILIES)
    assert artefact["determinism"]["identical"] is True


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"failed_case": True}, "gate-roc: 1 case(s) failed"),
        ({"junit_errors": 2}, "2 test(s) failed or errored"),
        ({"practrand_fail": True}, "PractRand interleave: fail"),
        ({"differing_cell": True}, "determinism: 1 digest(s) differ"),
    ],
)
def test_each_failure_names_itself(tmp_path: Path, change: Dict[str, Any], expected: str) -> None:
    verdict = _join(_parts(tmp_path, **change))["verdict"]
    assert not verdict["passed"]
    assert any(expected in reason for reason in verdict["reasons"]), verdict


def test_a_missing_component_is_never_a_pass(tmp_path: Path) -> None:
    verdict = _join(_parts(tmp_path, drop_practrand=True))["verdict"]
    assert not verdict["passed"]
    assert "PractRand first-words" in verdict["missing"]


def test_a_failed_job_fails_the_night(tmp_path: Path) -> None:
    jobs = {**JOBS_OK, "practrand": {"result": "failure"}}
    verdict = _join(_parts(tmp_path), jobs=jobs)["verdict"]
    assert "job practrand ended failure" in verdict["reasons"]


def test_benchmarks_are_held_to_the_snapshot(tmp_path: Path) -> None:
    baseline = {"benchmarks": [{"name": "test_uniform53", "medianSeconds": 1e-5}]}
    flagged = _join(_parts(tmp_path / "a", bench_median=2e-5), baseline=baseline)["verdict"]
    assert flagged["passed"] and flagged["flagged"]
    failed = _join(_parts(tmp_path / "b", bench_median=4e-5), baseline=baseline)["verdict"]
    assert not failed["passed"]


def test_the_weekly_mutation_score_is_held_to_its_target(tmp_path: Path) -> None:
    weak = {
        f"flux_engine.core.lrm.x_f__mutmut_{i}": ("killed" if i < 2 else "survived")
        for i in range(4)
    }
    verdict = _join(_parts(tmp_path / "a", mutation=weak), weekly=True)["verdict"]
    assert any("below its floor 95%" in reason for reason in verdict["reasons"]), verdict
    modules = (
        "flux_engine.concordance.boundary.statistic",
        "flux_engine.core.fixedpoint",
        "flux_engine.core.lrm",
        "flux_engine.core.rng",
        "flux_engine.core.seedtree",
    )
    strong = {f"{module}.x_f__mutmut_{i}": "killed" for module in modules for i in range(4)}
    assert _join(_parts(tmp_path / "b", mutation=strong), weekly=True)["verdict"]["passed"]
    only_lrm = {f"flux_engine.core.lrm.x_f__mutmut_{i}": "killed" for i in range(4)}
    partial = _join(_parts(tmp_path / "d", mutation=only_lrm), weekly=True)["verdict"]
    assert "mutation score for flux_engine.core.rng" in partial["missing"], partial
    unfinished = dict(strong) | {"flux_engine.core.rng.x_f__mutmut_9": "not checked"}
    cut_short = _join(_parts(tmp_path / "e", mutation=unfinished), weekly=True)["verdict"]
    assert any("incomplete for flux_engine.core.rng" in item for item in cut_short["missing"])
    missing = _join(_parts(tmp_path / "c"), weekly=True)["verdict"]
    assert "mutation score" in missing["missing"]


def test_the_command_line_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "artefact.json"
    assert (
        main(
            [
                "--parts",
                str(_parts(tmp_path)),
                "--out",
                str(out),
                "--base-seed",
                "1",
                "--jobs",
                json.dumps(JOBS_OK),
            ]
        )
        == 0
    )
    assert main(["--verdict", str(out)]) == 0
    failing = tmp_path / "failing.json"
    assert (
        main(
            [
                "--parts",
                str(_parts(tmp_path / "x", failed_case=True)),
                "--out",
                str(failing),
                "--jobs",
                json.dumps(JOBS_OK),
            ]
        )
        == 0
    )
    assert main(["--verdict", str(failing)]) == 1


def test_expected_failures_are_counted_apart_from_skips(tmp_path: Path) -> None:
    parts = _parts(tmp_path)
    _write(
        parts / "accuracy-nightly" / "junit.xml",
        '<testsuites><testsuite tests="3" failures="0" errors="0" skipped="2">'
        '<testcase classname="t" name="x"><skipped type="pytest.xfail" message="defect"/>'
        '</testcase><testcase classname="t" name="s"><skipped type="pytest.skip"/></testcase>'
        '<testcase classname="t" name="p"/></testsuite></testsuites>',
    )
    counts = _join(parts)["pytest"]
    assert (counts["skipped"], counts["xfailed"]) == (1, 1)
    assert counts["passing"] == ["t::p"]


def test_a_known_defect_is_listed_and_does_not_fail_the_night(tmp_path: Path) -> None:
    parts = _parts(tmp_path)
    pinned = {
        "family": "recovery",
        "case": "zero-bound",
        "test": "t::c",
        "passed": False,
        "knownDefect": "accuracy score drops a zero bound",
        "criterion": "c",
        "metrics": {},
        "n": 1,
        "tier": "nightly",
        "baseSeed": 1,
    }
    _write(parts / "accuracy-nightly" / "results" / "results-gw1.jsonl", json.dumps(pinned) + "\n")
    artefact = _join(parts)
    assert artefact["verdict"]["passed"]
    assert artefact["verdict"]["knownDefects"] == ["accuracy score drops a zero bound"]
    assert artefact["families"]["recovery"]["failed"] == 0
    assert artefact["families"]["recovery"]["knownDefects"] == 1


def test_a_case_from_another_tier_or_seed_fails_the_night(tmp_path: Path) -> None:
    parts = _parts(tmp_path)
    stale = {
        "family": "gof-kernel",
        "case": "uniform53",
        "test": "t::stale",
        "passed": True,
        "criterion": "c",
        "metrics": {},
        "n": 1,
        "tier": "pr",
        "baseSeed": 20261111,
    }
    _write(parts / "accuracy-nightly" / "results" / "results-gw2.jsonl", json.dumps(stale) + "\n")
    verdict = _join(parts)["verdict"]
    assert not verdict["passed"]
    assert any("another tier or seed" in reason for reason in verdict["reasons"]), verdict


def _confirmation(parts: Path, *, passed: bool, seed: int = 2) -> None:
    case = {
        "family": "gate-roc",
        "case": "gate-roc-case",
        "test": "t::gate-roc",
        "passed": passed,
        "criterion": "c",
        "metrics": {},
        "n": 10,
        "tier": "nightly",
        "baseSeed": seed,
    }
    _write(
        parts / "accuracy-nightly" / "confirm-results" / "results-gw0.jsonl",
        json.dumps(case) + "\n",
    )
    outcome = "" if passed else "<failure/>"
    _write(
        parts / "accuracy-nightly" / "confirm-junit.xml",
        f'<testsuites><testsuite tests="1" failures="{0 if passed else 1}" errors="0" skipped="0">'
        f'<testcase classname="t" name="f0">{outcome}</testcase></testsuite></testsuites>',
    )


def _failing_first_pass(tmp_path: Path) -> Path:
    parts = _parts(tmp_path, failed_case=True)
    _write(parts / "accuracy-nightly" / "junit.xml", _junit(failures=1))
    return parts


def test_a_failure_the_confirmation_clears_is_flagged_not_red(tmp_path: Path) -> None:
    parts = _failing_first_pass(tmp_path)
    _confirmation(parts, passed=True)
    artefact = build_artefact(
        parts=parts,
        run={"tier": "nightly", "baseSeed": 1, "confirmSeed": 2},
        jobs=JOBS_OK,
        baseline=None,
        weekly=False,
    )
    verdict = artefact["verdict"]
    assert verdict["passed"], verdict
    assert any("cleared at the confirmation seed" in item for item in verdict["flagged"])
    assert artefact["families"]["gate-roc"]["failed"] == 0
    assert artefact["families"]["gate-roc"]["unconfirmed"] == 1


def test_a_failure_the_confirmation_repeats_is_red(tmp_path: Path) -> None:
    parts = _failing_first_pass(tmp_path)
    _confirmation(parts, passed=False)
    verdict = build_artefact(
        parts=parts,
        run={"tier": "nightly", "baseSeed": 1, "confirmSeed": 2},
        jobs=JOBS_OK,
        baseline=None,
        weekly=False,
    )["verdict"]
    assert not verdict["passed"]
    assert any("not cleared by the confirmation pass" in item for item in verdict["reasons"])


def test_a_confirmation_at_the_wrong_seed_is_refused(tmp_path: Path) -> None:
    parts = _failing_first_pass(tmp_path)
    _confirmation(parts, passed=True, seed=99)
    verdict = build_artefact(
        parts=parts,
        run={"tier": "nightly", "baseSeed": 1, "confirmSeed": 2},
        jobs=JOBS_OK,
        baseline=None,
        weekly=False,
    )["verdict"]
    assert not verdict["passed"]
    assert any("another tier or seed" in item for item in verdict["reasons"])


def test_a_family_that_logs_nothing_is_missing(tmp_path: Path) -> None:
    parts = _parts(tmp_path)
    path = parts / "accuracy-nightly" / "results" / "results-gw0.jsonl"
    kept = [line for line in path.read_text().splitlines() if '"family": "journeys"' not in line]
    path.write_text("\n".join(kept) + "\n")
    verdict = _join(parts)["verdict"]
    assert "family journeys logged no case" in verdict["missing"]
