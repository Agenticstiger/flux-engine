# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Facts and relationships on the scorecard: scored against the pinned profile (SCORECARD-SPEC.md §3, 1-3-0)."""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List

import pytest
from jsonschema import Draft202012Validator

from flux_engine.core.digest import digest_of_document
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify as run_verify
from flux_stats.profile import SCHEMA_URL_1_2_0

from .test_rules_run import _bundle

pytestmark = pytest.mark.integration

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
SCHEMA = json.loads((SPEC / "scorecard-1-3.schema.json").read_text(encoding="utf-8"))

_BEFORE_2000 = '        - id: before_2000\n          check: "d_year < 2000"\n'
NAMESPACE = "  com.agentics.flux/1:\n"


def _share(true: int, false: int) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": ["false", "true"],
        "sampleSize": {"min": true + false, "max": true + false},
        "disclosure": "none",
        "groups": [{"group": "", "k": true + false, "levelCounts": [false, true], "status": "F"}],
    }


def _profile(tmp_path: Path) -> Path:
    document = {
        "_producer": "urn:example:test-profiler",
        "_schemaURL": SCHEMA_URL_1_2_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {},
        "invariants": [
            {"name": "date_dim.date_of_key", "statistics": [_share(365, 0)]},
            {"name": "date_dim.year_of_date", "statistics": [_share(355, 10)]},
        ],
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _pinned(tmp_path: Path, profile: Path, *, facts: str, name: str = "bundle") -> Path:
    bundle = _bundle(tmp_path, name=name, invariants=_BEFORE_2000)
    digest = digest_of_document(json.loads(profile.read_text("utf-8")), subject="p").value
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    block = f"    profile:\n      digest: {digest}\n" + "".join(
        f"    {line}\n" for line in facts.splitlines()
    )
    simulation.write_text(text.replace(NAMESPACE, NAMESPACE + block, 1), "utf-8")
    return bundle


def _scorecard(tmp_path: Path, facts: str, name: str = "bundle") -> Dict[str, Any]:
    profile = _profile(tmp_path)
    bundle = _pinned(tmp_path, profile, facts=facts, name=name)
    state = tmp_path / ".flux"
    applied = apply(bundle, state_dir=state, profile=profile)
    assert applied.success, applied.stderr
    layout = RunLayout.for_state_dir(state, str(applied.artifacts["run_id"]))
    result = run_verify(
        bundle,
        run_id=layout.run_id,
        state_dir=state,
        profile=profile,
        allow_declared_baseline_drift=True,
    )
    document = result.artifacts["scorecard"]["document"]
    assert isinstance(document, dict)
    return document


def _rows(document: Dict[str, Any], kind: str) -> Dict[str, Dict[str, Any]]:
    return {
        row["propertyId"]: row
        for table in document["tables"]
        for row in table["properties"]
        if row["property"] == kind
    }


@pytest.fixture(scope="module")
def document(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    facts = 'facts:\n  - { emit: date_dim, rule: year_of_date, tolerance: "0.03" }'
    return _scorecard(tmp_path_factory.mktemp("module-scorecard"), facts)


def test_the_scorecard_names_format_1_3_0_and_validates_against_its_schema(
    document: Dict[str, Any],
) -> None:
    assert document["_schemaURL"] == SCHEMA["$id"]
    Draft202012Validator(SCHEMA).validate(document)


def test_a_fact_is_scored_against_the_share_the_profile_measured(document: Dict[str, Any]) -> None:
    facts = _rows(document, "fact")
    held = facts["fact:date_dim.date_of_key"]
    assert held["run"] == {"satisfied": 365, "total": 365, "share": "1"}
    assert held["profile"] == {"satisfied": 365, "total": 365, "share": "1"}
    assert (held["outcome"], held["score"], held["gap"], held["tolerance"]) == (
        "within",
        "1",
        "0",
        "0",
    )
    moved = facts["fact:date_dim.year_of_date"]
    assert moved["profile"]["share"] == "71/73"  # 355 of 365, reduced
    gap = Fraction(10, 365)
    assert moved["gap"] == "2/73" and Fraction(moved["gap"]) == gap
    assert Fraction(moved["score"]) == 1 - gap
    assert moved["tolerance"] == "0.03" and moved["outcome"] == "within"  # 0.0274 <= 0.03


def test_a_fact_the_profile_gave_no_share_for_is_censored_and_left_out_of_the_mean(
    document: Dict[str, Any],
) -> None:
    row = _rows(document, "fact")["fact:date_dim.before_2000"]
    assert row["outcome"] == "censored" and row["score"] is None and row["profile"] is None
    assert row["run"] == {"satisfied": 0, "total": 365, "share": "0"}
    invariants = next(d for d in document["dimensions"] if d["dimension"] == "invariants")
    assert invariants["status"] == "measured"
    assert invariants["counts"] == {"properties": 3, "scored": 2, "censored": 1, "refused": 0}
    expected = (1 + (1 - Fraction(10, 365))) / 2
    assert Fraction(invariants["score"]) == expected


def test_a_relationship_is_scored_for_its_referential_integrity(document: Dict[str, Any]) -> None:
    relationships = _rows(document, "referentialIntegrity")
    assert "relationship:store_sales.item" in relationships
    row = relationships["relationship:store_sales.item"]
    assert (row["outcome"], row["score"]) == ("within", "1")
    assert row["checks"]["found"] == {"satisfied": 4000, "total": 4000, "share": "1"}
    cross = next(d for d in document["dimensions"] if d["dimension"] == "crossTable")
    assert cross["status"] == "measured" and cross["score"] == "1"
    assert cross["counts"]["properties"] == len(relationships)


def test_a_fact_outside_its_tolerance_is_outside(tmp_path: Path) -> None:
    document = _scorecard(tmp_path, "facts: []")
    row = _rows(document, "fact")["fact:date_dim.year_of_date"]
    assert row["outcome"] == "outside" and row["tolerance"] == "0"
    table = next(t for t in document["tables"] if t["exposeId"] == "date_dim")
    assertions = {a["name"]: a for a in table["dataQualityAssertions"]["assertions"]}
    assert assertions["fact:date_dim.year_of_date"]["success"] is False
    assert assertions["fact:date_dim.date_of_key"]["success"] is True
    assert "fact:date_dim.before_2000" not in assertions  # censored: no comparison ran


def test_the_run_is_scored_on_the_new_dimensions_and_a_threshold_reads_them(tmp_path: Path) -> None:
    facts = (
        'facts:\n  - { emit: date_dim, rule: year_of_date, tolerance: "0.03" }\n'
        "scorecard:\n  thresholds:\n    dimensions:\n"
        '      invariants: { mustBeGreaterOrEqualTo: "0.98" }\n'
        '      crossTable: { mustBeGreaterOrEqualTo: "1" }\n'
    )
    document = _scorecard(tmp_path, facts)
    results: List[Dict[str, Any]] = document["thresholds"]
    by_target = {entry["target"]: entry for entry in results}
    assert by_target["dimensions.invariants"]["met"] is True  # 1 and 0.9726 average 0.9863
    assert by_target["dimensions.crossTable"]["met"] is True


def test_a_threshold_on_the_invariants_dimension_misses_when_it_is_above_the_score(
    tmp_path: Path,
) -> None:
    facts = (
        'facts:\n  - { emit: date_dim, rule: year_of_date, tolerance: "0.03" }\n'
        "scorecard:\n  thresholds:\n    dimensions:\n"
        '      invariants: { mustBeGreaterOrEqualTo: "0.99" }\n'
    )
    document = _scorecard(tmp_path, facts)
    (entry,) = document["thresholds"]
    assert entry["met"] is False and entry["target"] == "dimensions.invariants"
