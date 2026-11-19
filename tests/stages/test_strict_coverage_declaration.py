# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle that declares ``strictCoverage`` is held to it by every stage that runs.

Five starter bundles are built once: with and without a column no mapping reaches (one
that falls back to a type default), and with ``strictCoverage`` true, false or absent.
What these cases hold in place: the declaration refuses a defaulted column with no flag,
the flag cannot turn it off, ``false`` declares nothing, and a run it accepts makes the
rows, the column provenance record and the component digests it makes without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from flux_engine.cli import main
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.run import run
from flux_engine.starter import scaffold
from tests.bundles import add_contract_columns, declare_in_namespace

from .conftest import read_json

pytestmark = pytest.mark.integration

#: ``name -> (has a column that falls back to a default, the namespace lines it declares)``.
BUNDLES: Dict[str, Tuple[bool, List[str]]] = {
    "defaulted-declared": (True, ["strictCoverage: true"]),
    "defaulted-declared-false": (True, ["strictCoverage: false"]),
    "defaulted-plain": (True, []),
    "covered-declared": (False, ["strictCoverage: true"]),
    "covered-plain": (False, []),
}
ASKED_BY_THE_BUNDLE = "without the strictCoverage declaration of the Simulation this is a report"


def _starter(target: Path, *, defaulted: bool, declaration: List[str]) -> Path:
    scaffold(target)
    if defaulted:
        add_contract_columns(target, [("note", "STRING", False)])
    if declaration:
        declare_in_namespace(target, declaration)
    return target


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    root = tmp_path_factory.mktemp("declared")
    return {
        name: _starter(root / name, defaulted=defaulted, declaration=declaration)
        for name, (defaulted, declaration) in BUNDLES.items()
    }


@pytest.mark.parametrize("stage", [plan, apply, run], ids=["plan", "apply", "run"])
def test_the_declaration_refuses_a_defaulted_column_with_no_flag_and_writes_nothing(
    bundles: Dict[str, Path], tmp_path: Path, stage: Callable[..., Any]
) -> None:
    result = stage(bundles["defaulted-declared"], state_dir=tmp_path / "state")

    assert (result.exit_code, result.error_code) == (1, "FLX-3304"), result.stderr
    assert "note" in result.stderr
    assert ASKED_BY_THE_BUNDLE in result.stderr
    assert "--strict-coverage" not in result.stderr, "the flag was never given"
    assert not (tmp_path / "state").exists(), "refused before any row or plan is written"


@pytest.mark.parametrize(
    ("bundle", "says", "never_says"),
    [
        ("defaulted-declared", "without --strict-coverage and the strictCoverage", None),
        (
            "defaulted-plain",
            "without --strict-coverage this is a report and the run",
            "declaration",
        ),
        ("defaulted-declared-false", "without --strict-coverage this is a report", "declaration"),
    ],
    ids=["both-asked", "the-flag-alone", "the-flag-over-a-false"],
)
def test_the_refusal_names_whoever_asked(
    bundles: Dict[str, Path], tmp_path: Path, bundle: str, says: str, never_says: Optional[str]
) -> None:
    result = plan(bundles[bundle], state_dir=tmp_path / "state", strict_coverage=True)

    assert (result.exit_code, result.error_code) == (1, "FLX-3304"), result.stderr
    assert says in result.stderr
    assert never_says is None or never_says not in result.stderr


def test_a_declaration_of_false_declares_nothing(bundles: Dict[str, Path], tmp_path: Path) -> None:
    result = plan(bundles["defaulted-declared-false"], state_dir=tmp_path / "state")

    assert result.exit_code == 0, result.stderr
    assert "coverage: payment_recovery_moment: 1 of 5 columns fell back" in result.stdout
    assert "--strict-coverage refuses the run, and so does strictCoverage in the bundle" in (
        result.stdout
    )


def test_the_command_line_refuses_it_with_the_code_and_the_exit_status(
    bundles: Dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = main(["run", str(bundles["defaulted-declared"]), "--state-dir", str(tmp_path / "s")])

    captured = capsys.readouterr()
    assert status == 1
    assert "FLX-3304" in captured.err and ASKED_BY_THE_BUNDLE in captured.err


@pytest.mark.parametrize(
    ("value", "refused"),
    [("true", False), ("false", False), ("'true'", True), ("1", True), ("[true]", True)],
)
def test_validate_and_plan_agree_on_what_a_declaration_may_be(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str, refused: bool
) -> None:
    bundle = _starter(tmp_path / "b", defaulted=False, declaration=[f"strictCoverage: {value}"])
    state = str(tmp_path / "state")

    for argv in (["validate", str(bundle)], ["plan", str(bundle), "--state-dir", state]):
        status = main(argv)
        said = capsys.readouterr()
        assert (status != 0) is refused
        assert ("strictCoverage is a boolean" in said.err + said.out) is refused


def _single(state: Path, pattern: str) -> Path:
    (path,) = state.glob(pattern)
    return path


def test_a_covered_bundle_runs_under_the_declaration_and_makes_the_rows_it_makes_without_it(
    bundles: Dict[str, Path], tmp_path: Path
) -> None:
    states = {}
    for name in ("covered-declared", "covered-plain"):
        states[name] = tmp_path / name
        result = run(bundles[name], state_dir=states[name])
        assert result.exit_code == 0, result.stderr

    declared, plain = (
        read_json(_single(states[name], "runs/*/run-digest.json"))
        for name in ("covered-declared", "covered-plain")
    )
    assert declared["components"] == plain["components"], "every component digest is the same"
    assert declared["inputDigest"] != plain["inputDigest"], "the declaration is a bundle edit"
    for pattern in ("runs/*/emits/*.ndjson", "runs/*/column-provenance.json"):
        assert (
            _single(states["covered-declared"], pattern).read_bytes()
            == _single(states["covered-plain"], pattern).read_bytes()
        )


def test_the_declaration_leaves_the_plan_with_the_same_keys(
    bundles: Dict[str, Path], tmp_path: Path
) -> None:
    keys = []
    for name in ("covered-declared", "covered-plain"):
        planned = plan(bundles[name], state_dir=tmp_path / name)
        assert planned.exit_code == 0, planned.stderr
        keys.append(sorted(read_json(_single(tmp_path / name, "runs/*/plan.json"))))
    assert keys[0] == keys[1]
