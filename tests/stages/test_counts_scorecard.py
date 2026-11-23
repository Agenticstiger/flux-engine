# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The children-per-parent row of the scorecard, format 1-4-0 (ADR 0016 section 8, SCORECARD-SPEC.md §3).

The bundle is ``tests/fixtures/exact-counts`` with a ``counts`` entry for the lines of each invoice
(one, three or six, in shares of one half, three tenths and a fifth). The pinned profile describes
the relationship with a ``marginalHistogram/1`` of children per parent over three bins. What is pinned:

* **Within.** A run whose invoices have the profile's lines has the row, scored 1, within, and names
  format 1-4-0 and validates against that schema.
* **Outside.** A profile whose bins disagree puts the run outside by the total variation distance,
  and the entry's ``tolerance`` decides within or outside.
* **Without it.** A profile that describes no relationship leaves the scorecard in format 1-3-0 with
  the rows it always had, valid against the 1-3-0 schema.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from jsonschema import Draft202012Validator

from flux_engine.concordance.integrity import link_integrity
from flux_engine.concordance.module_scores import _binned
from flux_engine.core.digest import digest_of_document
from flux_engine.seam.keys import Relationship
from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.verify import verify as run_verify
from flux_stats.profile import SCHEMA_URL_1_2_0

from .test_counts_run import bundle_with

pytestmark = pytest.mark.integration

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
SCHEMA_1_4 = json.loads((SPEC / "scorecard-1-4.schema.json").read_text(encoding="utf-8"))
SCHEMA_1_3 = json.loads((SPEC / "scorecard-1-3.schema.json").read_text(encoding="utf-8"))

INVOICES = 1200
#: One line, three lines and six lines fall in three bins of the profile; 1200 invoices in shares
#: of one half, three tenths and a fifth.
BINS = ["1", "2", "4", "7"]
RUN_COUNTS = [600, 360, 240]
ROW = "children:invoice_line.invoice"
NAMESPACE = "  com.agentics.flux/1:\n"


def _histogram(counts: List[int]) -> Dict[str, Any]:
    total = sum(counts)
    return {
        "definition": "marginalHistogram/1",
        "bins": BINS,
        "sampleSize": {"min": total, "max": total},
        "disclosure": "none",
        "groups": [{"group": "", "k": total, "binCounts": counts, "status": "F"}],
    }


def _column() -> Dict[str, Any]:
    return {"domain": "number", "metrics": {}}


def _profile(tmp_path: Path, counts: Optional[List[int]]) -> Path:
    document: Dict[str, Any] = {
        "_producer": "urn:example:test-profiler",
        "_schemaURL": SCHEMA_URL_1_2_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "invoice": {"columns": {"invoice_id": _column()}},
            "invoice_line": {"columns": {"invoice_id": _column()}},
        },
    }
    if counts is not None:
        document["relationships"] = [
            {
                "parent": {"table": "invoice", "columns": ["invoice_id"]},
                "child": {"table": "invoice_line", "columns": ["invoice_id"]},
                "statistics": [_histogram(counts)],
            }
        ]
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _scorecard(
    tmp_path: Path, counts: Optional[List[int]], *, tolerance: Optional[str] = None
) -> Dict[str, Any]:
    bundle = bundle_with(tmp_path, "bundle")
    profile = _profile(tmp_path, counts)
    simulation = bundle / "simulation.flux.yml"
    text = simulation.read_text("utf-8")
    digest = digest_of_document(json.loads(profile.read_text("utf-8")), subject="p").value
    text = text.replace(NAMESPACE, NAMESPACE + f"    profile:\n      digest: {digest}\n", 1)
    if tolerance is not None:
        old = "emit: invoice_line, relationship: invoice,"
        assert text.count(old) == 1
        text = text.replace(old, f'{old} tolerance: "{tolerance}",')
    simulation.write_text(text, "utf-8")
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


def _row(document: Dict[str, Any], property_id: str = ROW) -> Dict[str, Any]:
    (found,) = (
        row
        for table in document["tables"]
        for row in table["properties"]
        if row["propertyId"] == property_id
    )
    return found


def test_a_run_that_has_the_profiles_children_per_parent_is_within_and_names_format_1_4_0(
    tmp_path: Path,
) -> None:
    document = _scorecard(tmp_path, RUN_COUNTS)
    assert document["_schemaURL"] == SCHEMA_1_4["$id"]
    Draft202012Validator(SCHEMA_1_4).validate(document)
    assert list(Draft202012Validator(SCHEMA_1_3).iter_errors(document))
    row = _row(document)
    assert (row["property"], row["dimension"], row["outcome"]) == (
        "childrenPerParent",
        "crossTable",
        "within",
    )
    assert (row["score"], row["distance"], row["tolerance"], row["weight"]) == (
        "1",
        "0",
        "0.1",
        "1",
    )
    assert row["run"] == {"parents": INVOICES, "binCounts": RUN_COUNTS}
    assert row["profile"] == {"parents": INVOICES, "binCounts": RUN_COUNTS}
    assert row["bins"] == BINS
    assert (row["child"], row["parent"], row["relationship"]) == (
        "invoice_line",
        "invoice",
        "invoice",
    )
    table = next(t for t in document["tables"] if t["exposeId"] == "invoice_line")
    assert any(
        a["name"] == ROW and a["success"] for a in table["dataQualityAssertions"]["assertions"]
    )
    crossed = next(d for d in document["dimensions"] if d["dimension"] == "crossTable")
    assert crossed["status"] == "measured"


def test_children_per_parent_that_miss_the_profile_are_outside_by_the_distance(
    tmp_path: Path,
) -> None:
    document = _scorecard(tmp_path, list(reversed(RUN_COUNTS)))
    Draft202012Validator(SCHEMA_1_4).validate(document)
    row = _row(document)
    distance = Fraction(3, 10)  # half of |0.5 - 0.2| + 0 + |0.2 - 0.5|
    assert Fraction(row["distance"]) == distance
    assert Fraction(row["score"]) == 1 - distance
    assert row["outcome"] == "outside" and row["tolerance"] == "0.1"
    table = next(t for t in document["tables"] if t["exposeId"] == "invoice_line")
    assert any(
        a["name"] == ROW and a["success"] is False
        for a in table["dataQualityAssertions"]["assertions"]
    )


def test_the_counts_entrys_tolerance_decides_within_or_outside(tmp_path: Path) -> None:
    row = _row(_scorecard(tmp_path, list(reversed(RUN_COUNTS)), tolerance="0.30"))
    assert Fraction(row["distance"]) == Fraction(3, 10)
    assert (row["tolerance"], row["outcome"]) == ("0.3", "within")


def test_a_profile_that_describes_no_relationship_leaves_the_scorecard_in_format_1_3_0(
    tmp_path: Path,
) -> None:
    document = _scorecard(tmp_path, None)
    assert document["_schemaURL"] == SCHEMA_1_3["$id"]
    Draft202012Validator(SCHEMA_1_3).validate(document)
    kinds = {row["property"] for table in document["tables"] for row in table["properties"]}
    assert kinds and "childrenPerParent" not in kinds and "referentialIntegrity" in kinds
    assert "children:" not in json.dumps(document)


def test_the_two_ways_of_counting_children_per_parent_agree() -> None:
    link = Relationship(
        name="parent",
        source="child",
        target="parent",
        from_columns=("p",),
        to_columns=("id",),
        cardinality="many_to_one",
        join_type="inner",
    )
    parents = [[1], [2], [3], [4], [None]]
    children = [[1], [1], [1], [3], [3], [9]]
    found = link_integrity(link, ["p"], children, ["id"], parents)
    # Parent 1 has three children, 3 has two, 2 and 4 have none; the NULL key is no parent.
    assert dict(found.parents_by_children or {}) == {0: 2, 2: 1, 3: 1}
    assert (
        link_integrity(
            link, ["p"], children, ["id"], parents, parent_versioned=True
        ).parents_by_children
        is None
    )


def test_binning_parents_by_children_is_linear_in_the_parents_counted() -> None:
    import time

    edges = [Fraction(n) for n in range(0, 3001)]
    spread = {0: 5, 1: 2, 1500: 1, 200_000: 1}
    started = time.perf_counter()
    counts = _binned(spread, edges)
    assert time.perf_counter() - started < 0.5
    assert len(counts) == 3000
    assert (counts[0], counts[1], counts[1500], counts[-1]) == (5, 2, 1, 1)
    assert sum(counts) == 9


def test_binning_matches_a_scan_of_the_bins() -> None:
    edges = [Fraction(1), Fraction(3), Fraction(3), Fraction(7, 2), Fraction(10)]
    spread = {n: n + 1 for n in range(0, 14)}

    def scan() -> List[int]:
        counts = [0] * (len(edges) - 1)
        for children, parents in spread.items():
            for index in range(len(counts)):
                if children < edges[index + 1]:
                    counts[index] += parents
                    break
            else:
                counts[-1] += parents
        return counts

    assert list(_binned(spread, edges)) == scan()
