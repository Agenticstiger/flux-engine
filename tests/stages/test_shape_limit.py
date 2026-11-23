# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation may declare ``scorecard.shape.maxFlags``, and a run past it is withheld.

docs/spec/SCORECARD-SPEC.md §4 and §6. The shape flags inform and withhold nothing unless the
Simulation says otherwise. Here a constant boolean column, which the profile shows as half
``false`` and half ``true``, is the flag:

* without the key, ``verify`` passes and ``ship`` ships, as they always did, and the plan holds
  no ``scorecard`` key;
* with ``maxFlags: 0``, ``verify`` fails, the scorecard is not met, ``ship`` withholds the emit
  with the check ``scorecard.shape``, and the plan records the limit;
* with a limit the flags stay under, nothing changes but the plan.
"""

from __future__ import annotations

import copy
import json
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from flux_engine.concordance.compiler import compile_fidelity
from flux_engine.concordance.predicate import expose_from_contract
from flux_engine.core.errors import FluxError
from flux_engine.seam.scorecard import (
    declaration_from_plan,
    read_scorecard_block,
    thresholds_document,
)
from flux_engine.stages.run import run
from tests.bundles import add_contract_columns
from tests.stats.profiles import profile_from_rows

from .conftest import FIXTURE_BUNDLE
from .test_scorecard_stages import EXPOSE, Pinned

pytestmark = pytest.mark.unit

_FLAG_COLUMN = (
    "            flag:\n"
    "              support:\n"
    '                values: ["true"]\n'
    "              maxCardinality: 2\n"
    "              null: fail\n"
)


def _bundle(root: Path) -> Path:
    """The small telco bundle with a BOOL column ``flag`` that every row holds true in."""
    bundle = root / "source"
    shutil.copytree(FIXTURE_BUNDLE, bundle)
    (bundle / "flux-manifest.json").unlink(missing_ok=True)
    add_contract_columns(bundle, [("flag", "BOOL", False)])
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    anchor = "            aborted_at:\n"
    assert text.count(anchor) == 1
    simulation.write_text(text.replace(anchor, _FLAG_COLUMN + anchor), encoding="utf-8")
    return bundle


def _profile(root: Path, bundle: Path) -> Dict[str, Any]:
    """A profile of the bundle's own run, which also describes ``flag`` as half false, half true."""
    state = root / "profile-state"
    result = run(bundle, state_dir=state)
    assert result.success, result.stderr
    run_dir = state / "runs" / result.artifacts["plan"]["run_id"]
    fidelity_input = json.loads((run_dir / f"fidelity-input-{EXPOSE}.json").read_text())
    contract = fidelity_input["contract"]
    compiled = compile_fidelity(
        contract=contract,
        emit=fidelity_input["emit"],
        expose=expose_from_contract(contract, EXPOSE),
    )
    columns = json.loads((run_dir / f"seam-attestation-{EXPOSE}.json").read_text())["columns"]
    lines = (run_dir / "emits" / f"{EXPOSE}.ndjson").read_text().splitlines()
    rows = [dict(zip(columns, json.loads(line), strict=True)) for line in lines if line]
    assert {row["flag"] for row in rows} == {True}
    document = profile_from_rows(rows, compiled)
    levels = {
        "definition": "levelHistogram/1",
        "sampleSize": {"min": 1},
        "disclosure": "none",
        "groups": [{"group": "", "k": 100, "levelCounts": [50, 50], "status": "F"}],
        "levels": ["false", "true"],
    }
    document["tables"][EXPOSE]["columns"]["flag"] = {"domain": "category", "statistics": [levels]}
    return document


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, Dict[str, Any]]:
    root = tmp_path_factory.mktemp("shape")
    bundle = _bundle(root)
    return bundle, _profile(root, bundle)


def _pinned(root: Path, world: Tuple[Path, Dict[str, Any]], shape: str = "") -> Pinned:
    bundle, profile = world
    return Pinned(root, copy.deepcopy(profile), scorecard=shape, source=bundle)


def test_a_constant_boolean_column_is_flagged_and_passes_without_the_key(
    tmp_path: Path, world: Tuple[Path, Dict[str, Any]]
) -> None:
    pinned = _pinned(tmp_path, world)
    result = pinned.verify(profile=pinned.profile)
    assert result.exit_code == 0, result.stderr
    document = result.artifacts["scorecard"]["document"]
    assert document["met"] is True
    flagged = [
        column
        for table in document["shapeFlags"]["tables"]
        for column in table.get("columns", ())
        if column.get("flags")
    ]
    assert [column["column"] for column in flagged] == ["flag"]
    assert "CONSTANT" in {flag["alert"] for flag in flagged[0]["flags"]}
    assert "they inform and withhold nothing" in result.stdout
    plan = json.loads((pinned.run_dir / "plan.json").read_text())
    assert "scorecard" not in plan
    shipped = pinned.ship(tmp_path / "out", profile=pinned.profile)
    assert shipped.exit_code == 0, shipped.stderr


def test_a_constant_boolean_column_fails_verify_and_withholds_ship_under_max_flags_zero(
    tmp_path: Path, world: Tuple[Path, Dict[str, Any]]
) -> None:
    pinned = _pinned(tmp_path, world, "    scorecard:\n      shape: { maxFlags: 0 }\n")
    result = pinned.verify(profile=pinned.profile)
    assert result.exit_code == 1
    assert "maxFlags" in result.stderr and f"{EXPOSE}.flag (" in result.stderr
    assert "CONSTANT" in result.stderr
    assert result.artifacts["scorecard"]["met"] is False
    assert result.artifacts["scorecard"]["document"]["met"] is False
    assert "at most 0 allowed" in result.stdout
    plan = json.loads((pinned.run_dir / "plan.json").read_text())
    assert plan["scorecard"] == {"shape": {"maxFlags": 0}}
    out = tmp_path / "out"
    shipped = pinned.ship(out, profile=pinned.profile)
    assert shipped.exit_code == 1 and shipped.error_code == "FLX-8001", shipped.stderr
    assert not out.exists() or not any(out.iterdir())
    checks = shipped.artifacts["decisions"][EXPOSE]["checks"]
    failed = [check for check in checks if not check["passed"]]
    assert [check["check"] for check in failed] == ["scorecard.shape"]
    assert "maxFlags" in failed[0]["detail"]


def test_a_limit_the_flags_stay_under_changes_no_verdict(
    tmp_path: Path, world: Tuple[Path, Dict[str, Any]]
) -> None:
    pinned = _pinned(tmp_path, world, "    scorecard:\n      shape: { maxFlags: 50 }\n")
    result = pinned.verify(profile=pinned.profile)
    assert result.exit_code == 0, result.stderr
    assert result.artifacts["scorecard"]["met"] is True
    assert pinned.ship(tmp_path / "out", profile=pinned.profile).exit_code == 0


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------

_EMITS: Dict[str, Any] = {"orders": None}


def _read(block: Any, *, pinned: bool = True) -> Any:
    from flux_engine.seam.resolve import FLX_3105

    return read_scorecard_block(
        block, pinned=pinned, emits=_EMITS, where="scorecard", code=FLX_3105
    )


def test_the_shape_key_alone_is_a_declaration_and_round_trips_through_the_plan() -> None:
    declaration = _read({"shape": {"maxFlags": 3}})
    assert declaration.max_shape_flags == 3 and declaration.thresholds == ()
    document = thresholds_document(declaration)
    assert document == {"shape": {"maxFlags": 3}}
    from flux_engine.seam.resolve import FLX_3105

    back = declaration_from_plan(document, code=FLX_3105)
    assert back is not None and back.max_shape_flags == 3 and back.thresholds == ()


@pytest.mark.parametrize(
    "shape",
    [
        {"maxFlags": -1},
        {"maxFlags": True},
        {"maxFlags": 1.5},
        {"maxFlags": "0"},
        {},
        {"maxFlags": 0, "other": 1},
        "none",
        None,
    ],
)
def test_a_shape_key_that_is_not_a_whole_number_limit_is_refused(shape: Any) -> None:
    with pytest.raises(FluxError) as caught:
        _read({"shape": shape})
    assert caught.value.code.code == "FLX-3105"


def test_the_shape_key_needs_a_pinned_profile() -> None:
    with pytest.raises(FluxError):
        _read({"shape": {"maxFlags": 0}}, pinned=False)


def test_a_declaration_without_a_shape_key_writes_the_plan_it_always_wrote() -> None:
    from flux_engine.seam.scorecard import ScorecardDeclaration, Threshold

    threshold = Threshold(
        scope="overall",
        name=None,
        operator="mustBeGreaterOrEqualTo",
        value=Fraction(9, 10),
        declared="0.9",
    )
    document = thresholds_document(ScorecardDeclaration(thresholds=(threshold,)))
    assert document == {
        "thresholds": [{"target": "overall", "operator": "mustBeGreaterOrEqualTo", "value": "0.9"}]
    }
