# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim ship`` counts the consumers' queries with the functions ``verify`` counts them with (ADR 0028 section 7).

The bundle is the one of ``test_verify_queries``. A ``queries`` threshold gates a ship as it fails a
verify, the held-out profile and the ceiling flags are read the same way, and without the ``[sql]``
extra the dimension is not measured in both, with the install command.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from jsonschema import Draft202012Validator

pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from flux_engine.cli import main  # noqa: E402
from flux_engine.concordance.scorecard import SCORECARD_FILE  # noqa: E402
from flux_engine.consumers import runner  # noqa: E402
from flux_engine.core.digest import digest_of_document  # noqa: E402
from flux_engine.core.grades import Pattern  # noqa: E402
from flux_engine.evidence import grades  # noqa: E402
from flux_engine.evidence.grades import ProfileGrade  # noqa: E402
from flux_engine.stages import _grades, _queries  # noqa: E402
from flux_engine.stages.ship import ship  # noqa: E402

from . import test_verify_queries  # noqa: E402
from .test_verify_queries import FIXTURES, HELD_OUT, SCHEMA_1_4, Built, _json  # noqa: E402

pytestmark = pytest.mark.integration

MISSED = {"scorecard": {"thresholds": {"dimensions": {"queries": {"mustBeGreaterThan": "0.99"}}}}}
MET = {"scorecard": {"thresholds": {"dimensions": {"queries": {"mustBeGreaterOrEqualTo": "0.5"}}}}}


def _shipped(built: Built, out: Path, **kwargs: Any) -> Any:
    out.mkdir(exist_ok=True)
    kwargs.setdefault("profile", built.profile)
    return ship(built.bundle, run_id=built.run_id, out_dir=out, state_dir=built.state, **kwargs)


def _dimension(document: Dict[str, Any]) -> Dict[str, Any]:
    return {item["dimension"]: item for item in document["dimensions"]}["queries"]


def test_a_ship_forms_the_same_queries_dimension_as_verify(tmp_path: Path) -> None:
    built = Built(tmp_path / "run", consumers={"calibration": {"heldOutTolerance": "0.5"}})
    verified = built.verify(held_out=HELD_OUT)
    assert verified.success, verified.stderr
    shipped = _shipped(built, tmp_path / "out", held_out=HELD_OUT)
    assert shipped.success, shipped.stderr
    written = _json(built.layout.run_dir / SCORECARD_FILE)
    Draft202012Validator(SCHEMA_1_4).validate(written)
    assert _dimension(written)["queries"] == verified.artifacts["queries"]
    assert _dimension(written) == _dimension(verified.artifacts["scorecard"]["document"])


def test_a_missed_queries_threshold_withholds_the_ship_as_it_fails_verify(tmp_path: Path) -> None:
    built = Built(
        tmp_path / "run",
        consumers={"calibration": {"heldOutTolerance": "0.001"}},
        extension=MISSED,
    )
    assert built.verify(held_out=HELD_OUT).exit_code == 1
    out = tmp_path / "out"
    shipped = _shipped(built, out, held_out=HELD_OUT)
    assert shipped.exit_code == 1 and shipped.error_code == "FLX-8001"
    assert "dimensions.queries" in shipped.stdout
    assert not any(out.iterdir()), "a withheld ship writes nothing"


def test_a_met_queries_threshold_ships(tmp_path: Path) -> None:
    built = Built(
        tmp_path / "run", consumers={"calibration": {"heldOutTolerance": "0.5"}}, extension=MET
    )
    assert _shipped(built, tmp_path / "out", held_out=HELD_OUT).success


def test_without_the_extra_the_dimension_is_not_measured_in_ship_and_a_threshold_withholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path / "run", extension=MET)
    monkeypatch.setattr(_queries, "is_available", lambda name: name != "duckdb")
    shipped = _shipped(built, tmp_path / "out")
    assert shipped.exit_code == 1 and shipped.error_code == "FLX-8001"
    dimension = _dimension(shipped.artifacts["scorecard"])
    assert (dimension["status"], dimension["score"]) == ("not measured", None)
    assert dimension["queries"]["status"] == "not counted"
    assert dimension["queries"]["install"] == "pip install 'flux-sim-engine[sql]'"
    verified = built.verify()
    assert verified.exit_code == 1
    assert _dimension(verified.artifacts["scorecard"]["document"]) == dimension


def test_the_flags_and_the_held_out_profile_are_read_as_verify_reads_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = Built(tmp_path / "run", consumers={"limits": None})
    seen: List[Any] = []
    real = runner.run_counts

    def watching(tables: Any, requests: Any, limits: Any, **kwargs: Any) -> Any:
        seen.append(limits)
        return real(tables, requests, limits, **kwargs)

    monkeypatch.setattr(runner, "run_counts", watching)
    refused = _shipped(built, tmp_path / "o1", max_query_seconds=601)
    assert (refused.exit_code, refused.error_code) == (2, "FLX-3700") and seen == []
    assert _shipped(built, tmp_path / "o2", max_query_seconds=90, max_query_memory=2 << 30).success
    assert [(limits.seconds, limits.memory) for limits in seen] == [(90, 2 << 30)]

    bad = _json(HELD_OUT)
    bad["queries"].append(_json(built.profile)["queries"][0])
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    apart = _shipped(built, tmp_path / "o3", held_out=path)
    assert (apart.exit_code, apart.error_code) == (1, "FLX-3702")
    assert not any((tmp_path / "o3").iterdir()), "a refused ship writes nothing"


def test_the_command_line_ship_takes_the_same_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built = Built(tmp_path / "run", consumers={"calibration": {"heldOutTolerance": "0.5"}})
    base = [
        "ship",
        str(built.bundle),
        "--state-dir",
        str(built.state),
        "--run-id",
        built.run_id,
        "--profile",
        str(built.profile),
    ]
    assert main([*base, "-o", str(tmp_path / "o1"), "--max-query-seconds", "9999"]) == 2
    assert "FLX-3700" in capsys.readouterr().err
    code = main(
        [
            *base,
            "-o",
            str(tmp_path / "o2"),
            "--held-out",
            str(HELD_OUT),
            "--max-query-memory",
            "2GiB",
        ]
    )
    assert code == 0, capsys.readouterr().err


def _labelled_fixture(root: Path, monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    """Point the bundle builder at a copy of the fixture whose ``region`` column carries *label*."""
    copy = root / "fixtures"
    shutil.copytree(FIXTURES, copy)
    contract = copy / "bundle" / "subscriptions.fluid.yaml"
    text = contract.read_text(encoding="utf-8")
    column = "    - name: region\n      type: enum('north','south','east','west')\n      required: true\n"
    assert column in text
    contract.write_text(
        text.replace(column, column + f"      sensitivity: {label}\n"), encoding="utf-8"
    )
    # The contract changed, so the digest the Simulation declared for it no longer names it.
    declared = yaml.safe_load((FIXTURES / "bundle" / "subscriptions.fluid.yaml").read_text("utf-8"))
    old = digest_of_document(declared, subject="contractDigest:telco.silver.subscriptions").value
    new = digest_of_document(
        yaml.safe_load(contract.read_text("utf-8")),
        subject="contractDigest:telco.silver.subscriptions",
    ).value
    simulation = copy / "bundle" / "simulation.flux.yml"
    simulation.write_text(simulation.read_text("utf-8").replace(old, new), encoding="utf-8")
    monkeypatch.setattr(test_verify_queries, "FIXTURES", copy)


def _pinned_grade_is_p3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grade every profile but the held-out one P3, as a noisy pinned profile would be.

    The held-out profile is graded as it is (P1, since its queries cannot carry noise).
    """
    held = _json(HELD_OUT)

    def graded(profile: Any, *, pinned: bool) -> Any:
        if profile == held:
            return real_grade(profile, pinned=pinned)
        return ProfileGrade(Pattern.P3, (), None, pinned)

    real_grade = grades.grade_profile
    monkeypatch.setattr(grades, "grade_profile", graded)
    monkeypatch.setattr(_grades, "grade_profile", graded)


def test_a_held_out_profile_weaker_than_the_pinned_one_sets_the_grade_ship_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pinned_grade_is_p3(monkeypatch)
    built = Built(tmp_path / "run", consumers={"calibration": {"heldOutTolerance": "0.5"}})
    shipped = _shipped(built, tmp_path / "out", held_out=HELD_OUT)
    assert shipped.success, shipped.stderr
    text = (built.layout.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")
    assert "Run grade: **P3 (differentially private marginals)**." in text
    assert (
        "Weakest grade of every profile this run read: **P1 (checked)**. It covers the pinned "
        "profile and the held-out profile of the consumer queries." in text
    )


def test_a_ship_that_reads_one_profile_states_no_weakest_grade(tmp_path: Path) -> None:
    built = Built(tmp_path / "run")
    assert _shipped(built, tmp_path / "out").success
    assert "Weakest grade" not in (built.layout.run_dir / "EVIDENCE.md").read_text(encoding="utf-8")


def test_a_held_out_profile_below_a_columns_minimum_refuses_the_ship_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _labelled_fixture(tmp_path, monkeypatch, "pii")
    _pinned_grade_is_p3(monkeypatch)
    built = Built(tmp_path / "run", consumers={"calibration": {"heldOutTolerance": "0.5"}})
    before = {path for path in built.layout.run_dir.rglob("*") if path.is_file()}
    out = tmp_path / "out"
    refused = _shipped(built, out, held_out=HELD_OUT)
    assert (refused.exit_code, refused.error_code) == (1, "FLX-4005"), refused.stderr
    assert "the held-out profile's grade is P1" in refused.stderr
    assert "region is pii, which needs P3 or stronger" in refused.stderr
    assert not any(out.iterdir()), "a refused ship writes nothing"
    after = {path for path in built.layout.run_dir.rglob("*") if path.is_file()}
    assert after == before, "no fidelity record, scorecard or EVIDENCE.md is left behind"
    assert not (built.layout.run_dir / "EVIDENCE.md").exists()
    assert not (built.layout.run_dir / SCORECARD_FILE).exists()
    # The same bundle ships without the held-out profile: the pinned grade alone meets the minimum.
    assert _shipped(built, tmp_path / "out2").success
