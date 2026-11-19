# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation declares thresholds on its scorecard under the namespace's ``scorecard`` key.

docs/spec/SCORECARD-SPEC.md. The block is closed and every threshold must be
able to be met: one in a Simulation that pins no profile, on a dimension or
a table whose score is never formed, or with an operator or an operand a
floor cannot take, is refused with ``FLX-3105`` by every command, before
anything is written. The plan records the thresholds, so ``verify`` and
``ship`` read them through the plan's digest.
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam import resolve as resolve_module
from flux_engine.seam.resolve import FLX_3105, load_bundle, resolve_emit, scorecard_declaration
from flux_engine.seam.scorecard import (
    DeclaredProperty,
    ScorecardDeclaration,
    declaration_from_plan,
    read_scorecard_block,
    thresholds_document,
)
from flux_engine.stages.plan import PROFILE_PIN_KEY, SCORECARD_PLAN_KEY, plan

from .conftest import minimal_simulation

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
DIGEST = "sha256:" + "0" * 64
EMITS: Mapping[str, Optional[Sequence[DeclaredProperty]]] = {
    "moment": (
        DeclaredProperty(kind="mean", grouped=False),
        DeclaredProperty(kind="support", grouped=False),
    ),
    "plain": None,
}


def _read(block: Any, *, pinned: bool = True) -> ScorecardDeclaration:
    return read_scorecard_block(block, pinned=pinned, emits=EMITS, where="sim", code=FLX_3105)


def _refusal(block: Any, *, pinned: bool = True) -> str:
    with pytest.raises(FluxError) as raised:
        _read(block, pinned=pinned)
    assert raised.value.code.code == "FLX-3105"
    return raised.value.detail


def test_a_scorecard_block_declares_floors_on_scores() -> None:
    declaration = _read(
        {
            "thresholds": {
                "overall": {"mustBeGreaterOrEqualTo": "0.9"},
                "dimensions": {"marginals": {"mustBeGreaterThan": Decimal("0.95")}},
                "tables": {"moment": {"mustBeGreaterOrEqualTo": 1}},
            }
        }
    )
    assert [
        (threshold.target, threshold.operator, threshold.value, threshold.declared)
        for threshold in declaration.thresholds
    ] == [
        ("overall", "mustBeGreaterOrEqualTo", Fraction(9, 10), "0.9"),
        ("dimensions.marginals", "mustBeGreaterThan", Fraction(19, 20), "0.95"),
        ("tables.moment", "mustBeGreaterOrEqualTo", Fraction(1), "1"),
    ]
    floor = declaration.thresholds[1]
    assert floor.met_by(Fraction(96, 100)) and not floor.met_by(Fraction(95, 100))
    assert not floor.met_by(None)


@pytest.mark.parametrize(
    "block, needle",
    [
        ({"thresholds": {}}, "declares at least one threshold"),
        ({}, "holds thresholds"),
        (
            {"thresholds": {"overall": {"mustBeGreaterOrEqualTo": "0.9"}}, "weights": {}},
            "declares ['weights']",
        ),
        ({"thresholds": {"overalls": {}}}, "declares ['overalls']"),
        ({"thresholds": {"dimensions": {}}}, "names at least one dimension"),
        (
            {"thresholds": {"dimensions": {"shape": {"mustBeGreaterThan": "0.5"}}}},
            "declares ['shape']",
        ),
        (
            {"thresholds": {"dimensions": {"temporal": {"mustBeGreaterThan": "0.5"}}}},
            "no property kind of this engine is scored in temporal",
        ),
        (
            {"thresholds": {"dimensions": {"joint": {"mustBeGreaterThan": "0.5"}}}},
            "no declared fidelity property is scored in joint",
        ),
        (
            {"thresholds": {"tables": {"elsewhere": {"mustBeGreaterThan": "0.5"}}}},
            "does not emit 'elsewhere'",
        ),
        (
            {"thresholds": {"tables": {"plain": {"mustBeGreaterThan": "0.5"}}}},
            "'plain' declares no fidelity block",
        ),
        (
            {"thresholds": {"overall": {"mustBeLessThan": "0.5"}}},
            "a threshold is a floor on a score",
        ),
        (
            {"thresholds": {"overall": {"mustBeBetween": ["0.5", "1"]}}},
            "a threshold is a floor on a score",
        ),
        ({"thresholds": {"overall": {"atLeast": "0.5"}}}, "uses 'atLeast'"),
        (
            {"thresholds": {"overall": {"mustBeGreaterThan": "0.5", "mustBe": "1"}}},
            "holds exactly one operator",
        ),
        ({"thresholds": {"overall": {"mustBeGreaterThan": 0.5}}}, "never a float"),
        ({"thresholds": {"overall": {"mustBeGreaterThan": True}}}, "never a float or a bool"),
        ({"thresholds": {"overall": {"mustBeGreaterThan": "1.5"}}}, "between 0 and 1"),
        ({"thresholds": {"overall": {"mustBeGreaterThan": "1"}}}, "no score is above 1"),
        ({"thresholds": {"overall": {"mustBeGreaterThan": "-0.5"}}}, "no sign"),
        ({"thresholds": {"overall": {"mustBeGreaterThan": "5E-1"}}}, "no exponent"),
        ({"thresholds": {"overall": {"mustBeGreaterThan": "0." + "5" * 1000}}}, "no exponent"),
        ({"thresholds": "overall"}, "is a mapping"),
    ],
)
def test_a_threshold_that_could_never_be_read_as_written_is_refused(
    block: Any, needle: str
) -> None:
    assert needle in _refusal(block)


def test_a_scorecard_needs_a_pinned_profile() -> None:
    detail = _refusal({"thresholds": {"overall": {"mustBeGreaterThan": "0.5"}}}, pinned=False)
    assert "formed against a pinned profile" in detail


def test_the_plan_records_the_thresholds_and_reads_them_back() -> None:
    declaration = _read(
        {
            "thresholds": {
                "overall": {"mustBeGreaterOrEqualTo": "0.9"},
                "dimensions": {"structure": {"mustBeGreaterThan": "0.5"}},
                "tables": {"moment": {"mustBeGreaterOrEqualTo": "0.75"}},
            }
        }
    )
    recorded = thresholds_document(declaration)
    assert recorded == {
        "thresholds": [
            {"target": "overall", "operator": "mustBeGreaterOrEqualTo", "value": "0.9"},
            {"target": "dimensions.structure", "operator": "mustBeGreaterThan", "value": "0.5"},
            {"target": "tables.moment", "operator": "mustBeGreaterOrEqualTo", "value": "0.75"},
        ]
    }
    assert declaration_from_plan(recorded, code=FLX_3105) == declaration
    assert declaration_from_plan(None, code=FLX_3105) is None
    for broken in (
        {"thresholds": []},
        {"thresholds": [{"target": "dimensions.shape", "operator": "mustBe", "value": "1"}]},
        {"thresholds": [{"target": "elsewhere", "operator": "mustBeGreaterThan", "value": "1"}]},
    ):
        with pytest.raises(FluxError):
            declaration_from_plan(broken, code=FLX_3105)


def _simulation(namespace: Any) -> Dict[str, Any]:
    scored = {"moment": {"fidelity": {"properties": [{"property": "mean", "column": "x"}]}}}
    return {
        "spec": {"emits": [{"exposeId": "moment"}]},
        "extensions": {resolve_module.EXTENSION_NAMESPACE: {"emits": scored, **namespace}},
    }


def test_the_namespace_reads_the_block_and_its_pin() -> None:
    block = {"thresholds": {"overall": {"mustBeGreaterThan": "0.5"}}}
    assert scorecard_declaration(_simulation({"profile": {"digest": DIGEST}})) is None
    assert scorecard_declaration(_simulation({"scorecard": None})) is None
    pinned = scorecard_declaration(_simulation({"profile": {"digest": DIGEST}, "scorecard": block}))
    assert pinned is not None and pinned.thresholds[0].target == "overall"
    with pytest.raises(FluxError) as raised:
        scorecard_declaration(_simulation({"scorecard": block}), where="sim-x")
    assert raised.value.detail.startswith("sim-x: extensions['com.agentics.flux/1'].scorecard")


def test_resolving_an_emit_refuses_a_malformed_block(write_bundle: Any) -> None:
    documents = minimal_simulation()
    documents[1]["extensions"] = {
        resolve_module.EXTENSION_NAMESPACE: {
            "profile": {"digest": DIGEST},
            "scorecard": {"thresholds": {"overall": {"mustBeLessThan": "0.5"}}},
        }
    }
    root = write_bundle(flux_documents=documents)
    with pytest.raises(FluxError) as raised:
        resolve_emit(load_bundle(root), simulation_id="sim-widget")
    assert raised.value.code.code == "FLX-3105"
    assert "a threshold is a floor on a score" in raised.value.detail


def _pinned_bundle(tmp_path: Path, digest: str, scorecard: str = "") -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL, bundle)
    (bundle / "flux-manifest.json").unlink(missing_ok=True)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text(encoding="utf-8")
    anchor = "  com.agentics.flux/1:\n    emits:\n"
    assert text.count(anchor) == 1
    simulation.write_text(
        text.replace(
            anchor,
            f"  com.agentics.flux/1:\n    profile:\n      digest: {digest}\n{scorecard}    emits:\n",
        ),
        encoding="utf-8",
    )
    return bundle


def test_the_plan_records_the_pin_and_the_thresholds_only_when_pinned(tmp_path: Path) -> None:
    from flux_engine.seam.profile import load_profile
    from tests.seam.test_profile_pin import _profile

    profile = _profile(tmp_path / "pinned.json")
    pinned = load_profile(profile).digest.value
    block = (
        "    scorecard:\n      thresholds:\n        overall: { mustBeGreaterOrEqualTo: 0.9 }\n"
        '        dimensions:\n          structure: { mustBeGreaterThan: "0.5" }\n'
    )
    bundle = _pinned_bundle(tmp_path, pinned, block)
    planned = plan(bundle, state_dir=tmp_path / "state", profile=profile)
    assert planned.success, planned.stderr
    document = planned.artifacts["plan_document"]
    assert document[PROFILE_PIN_KEY] == pinned
    assert document[SCORECARD_PLAN_KEY] == {
        "thresholds": [
            {"target": "overall", "operator": "mustBeGreaterOrEqualTo", "value": "0.9"},
            {"target": "dimensions.structure", "operator": "mustBeGreaterThan", "value": "0.5"},
        ]
    }
    plain = plan(SMALL, state_dir=tmp_path / "plain")
    assert plain.success, plain.stderr
    unpinned = plain.artifacts["plan_document"]
    assert PROFILE_PIN_KEY not in unpinned and SCORECARD_PLAN_KEY not in unpinned


def test_a_scorecard_block_on_a_dimension_the_bundle_never_scores_is_refused_by_plan(
    tmp_path: Path,
) -> None:
    from flux_engine.seam.profile import load_profile
    from tests.seam.test_profile_pin import _profile

    profile = _profile(tmp_path / "pinned.json")
    block = (
        "    scorecard:\n      thresholds:\n        dimensions:\n"
        '          joint: { mustBeGreaterThan: "0.5" }\n'
    )
    bundle = _pinned_bundle(tmp_path, load_profile(profile).digest.value, block)
    refused = plan(bundle, state_dir=tmp_path / "state", profile=profile)
    assert refused.error_code == "FLX-3105", refused.stderr
    assert "no declared fidelity property is scored in joint" in refused.stderr
    assert not (tmp_path / "state" / "runs").exists(), "a refused plan writes no run directory"


def test_two_spellings_of_one_name_are_refused() -> None:
    composed, decomposed = "café", "café"
    with pytest.raises(FluxError) as raised:
        read_scorecard_block(
            {
                "thresholds": {
                    "tables": {
                        composed: {"mustBeGreaterOrEqualTo": "0.1"},
                        decomposed: {"mustBeGreaterOrEqualTo": "0.99"},
                    }
                }
            },
            pinned=True,
            emits={composed: (DeclaredProperty(kind="mean", grouped=False),)},
            where="sim",
            code=FLX_3105,
        )
    assert "twice, in two spellings of one name" in raised.value.detail


@pytest.mark.parametrize(
    "operand",
    [10**5000, 2, -1, Decimal("1E-9999999"), Decimal("1E+5"), Decimal("-0")],
    ids=["huge-int", "two", "minus-one", "tiny-decimal", "large-decimal", "negative-zero"],
)
def test_an_operand_out_of_range_is_refused_before_it_is_spelled(operand: Any) -> None:
    detail = _refusal({"thresholds": {"overall": {"mustBeGreaterOrEqualTo": operand}}})
    assert "between 0 and 1" in detail or "no sign" in detail


def test_an_overall_threshold_needs_a_scored_property_somewhere() -> None:
    with pytest.raises(FluxError) as raised:
        read_scorecard_block(
            {"thresholds": {"overall": {"mustBeGreaterOrEqualTo": "0.9"}}},
            pinned=True,
            emits={"plain": None},
            where="sim",
            code=FLX_3105,
        )
    assert "the overall score is never formed" in raised.value.detail


def test_a_coverage_block_declares_floors_on_how_much_of_the_contract_is_covered() -> None:
    declaration = _read(
        {
            "thresholds": {"overall": {"mustBeGreaterOrEqualTo": "0.9"}},
            "coverage": {"relationships": "0.5", "columns": Decimal("0.8")},
        }
    )
    assert [
        (threshold.target, threshold.operator, threshold.value, threshold.declared)
        for threshold in declaration.thresholds
    ] == [
        ("overall", "mustBeGreaterOrEqualTo", Fraction(9, 10), "0.9"),
        ("coverage.columns", "mustBeGreaterOrEqualTo", Fraction(4, 5), "0.8"),
        ("coverage.relationships", "mustBeGreaterOrEqualTo", Fraction(1, 2), "0.5"),
    ]
    alone = _read({"coverage": {"columns": "0.8"}})
    assert [threshold.target for threshold in alone.thresholds] == ["coverage.columns"]
    assert alone.thresholds[0].met_by(Fraction(4, 5)) and not alone.thresholds[0].met_by(None)
    again = declaration_from_plan(thresholds_document(declaration), code=FLX_3105)
    assert again == declaration, "the plan records coverage floors beside the thresholds"


@pytest.mark.parametrize(
    "block, needle",
    [
        ({"coverage": {}}, "names at least one of"),
        ({"coverage": {"rows": "0.5"}}, "declares ['rows']"),
        ({"coverage": {"columns": {"mustBeGreaterOrEqualTo": "0.5"}}}, "exact decimal"),
        ({"coverage": {"columns": "1.5"}}, "between 0 and 1"),
        ({"coverage": {"columns": 0.5}}, "never a float"),
        ({"coverage": ["columns"]}, "is a mapping"),
    ],
)
def test_a_malformed_coverage_floor_is_refused(block: Any, needle: str) -> None:
    assert needle in _refusal(block)


def test_a_coverage_floor_needs_a_pinned_profile_and_a_scored_property() -> None:
    assert "pinned profile" in _refusal({"coverage": {"columns": "0.5"}}, pinned=False)
    with pytest.raises(FluxError) as raised:
        read_scorecard_block(
            {"coverage": {"columns": "0.5"}},
            pinned=True,
            emits={"plain": None},
            where="sim",
            code=FLX_3105,
        )
    assert "could only withhold" in raised.value.detail
