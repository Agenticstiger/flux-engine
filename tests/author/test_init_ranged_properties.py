# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes no property of a column its fidelity block declares no range for.

A number column the profile publishes no histogram of is drawn evenly over the range its contract
declares, and declares bins and no range. A histogram property, grouped by a linked column or not,
and a rank correlation property each compare a column over its declared range, so ``validate``
refuses one of such a column (``FLX-5034``). Pinned here, through the command and a plan: the bundle
init writes from a garden's beds and plots validates and plans; the column is still drawn given the
bed's soil, and the copula still draws the two columns together, and no property scores either;
a column that does declare a range keeps its property.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.plan import plan
from flux_stats.profile import SCHEMA_URL_1_4_0
from tests.author import rank_profile as rp

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
PLOTS = 19800
BEDS = ("b1", "b2", "b3", "b4", "b5", "b6")
SOILS = ("clay", "loam", "sand")
BAND = {"min": 1000, "max": 9999}
EDGES = ["0", "10", "20", "30", "40"]

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: garden.beds
name: Beds
metadata:
  owner:
    team: allotments
exposes:
  - exposeId: bed
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/bed.parquet }
    contract:
      schema:
        - { name: bed_id, type: STRING, required: true }
        - { name: soil, type: STRING, required: true }
    semantics:
      entities:
        - { name: bed, type: primary, expr: bed_id }
  - exposeId: plot
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/plot.parquet }
    contract:
      schema:
        - { name: plot_id, type: STRING, required: true }
        - { name: bed_id, type: STRING, required: true }
        - name: depth_cm
          type: INT
          required: true
          validationRules:
            - { type: range, constraint: between 0 and 40 }
        - name: spread_cm
          type: INT
          required: true
          validationRules:
            - { type: range, constraint: between 0 and 60 }
        - name: width_cm
          type: INT
          required: true
          validationRules:
            - { type: range, constraint: between 0 and 30 }
    semantics:
      entities:
        - { name: plot, type: primary, expr: plot_id }
      relationships:
        - name: bed
          to: bed
          fromColumns: [bed_id]
          toColumns: [bed_id]
          cardinality: many_to_one
"""


def _metrics() -> Dict[str, Any]:
    return {
        "count": {
            "definition": "count/1",
            "disclosure": "none",
            "sampleSize": {"min": 1},
            "status": "F",
            "value": PLOTS,
        }
    }


def _level_counts(levels: List[str]) -> Dict[str, Any]:
    each = PLOTS // len(levels)
    return {
        "definition": "levelHistogram/1",
        "levels": levels,
        "sampleSize": BAND,
        "disclosure": "none",
        "groups": [
            {
                "group": "",
                "k": each * len(levels),
                "levelCounts": [each] * len(levels),
                "status": "F",
            }
        ],
    }


def _depth_by_soil() -> Dict[str, Any]:
    """The depth of a plot in bins of ten, counted per soil of the bed it lies in."""
    shapes = {"clay": [40, 30, 20, 10], "loam": [10, 40, 30, 20], "sand": [20, 10, 30, 40]}
    return {
        "definition": "marginalHistogram/1",
        "bins": EDGES,
        "sampleSize": BAND,
        "disclosure": "none",
        "groupByLink": {
            "parent": {"table": "bed", "columns": ["bed_id"]},
            "columns": ["bed_id"],
            "column": "soil",
        },
        "groups": [
            {
                "group": soil,
                "k": PLOTS // 3,
                "binCounts": [share * PLOTS // 300 for share in shape],
                "status": "F",
            }
            for soil, shape in shapes.items()
        ],
    }


def _profile(*, linked: bool, ranked: bool) -> Dict[str, Any]:
    """Beds published whole; plots with no histogram of their own, grouped or ranked as asked."""
    depth: Dict[str, Any] = {"domain": "number", "metrics": _metrics()}
    if linked:
        depth["statistics"] = [_depth_by_soil()]
    document: Dict[str, Any] = {
        "_producer": "urn:example:garden-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}, "noisy": {}},
        "tables": {
            "plot": {
                "rowCount": PLOTS,
                "columns": {
                    "plot_id": {"domain": "category"},
                    "bed_id": {"domain": "category", "statistics": [_level_counts(list(BEDS))]},
                    "depth_cm": depth,
                    "spread_cm": {"domain": "number", "metrics": _metrics()},
                    "width_cm": {"domain": "number", "metrics": _metrics()},
                },
            }
        },
        "references": {
            "bed": {
                "disclosure": "none",
                "columns": ["bed_id", "soil"],
                "rows": [[name, SOILS[position % 3]] for position, name in enumerate(BEDS)],
            }
        },
    }
    if ranked:
        document["tables"]["plot"]["columnPairs"] = [
            {"columns": ["spread_cm", "width_cm"], "statistics": [rp.noisy_pair("0.4")]}
        ]
        document["privacy"] = {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "1000"},
            "privacyUnit": {"expose": "plot"},
            "publicStructure": True,
        }
    return document


def _author(tmp_path: Path, **asked: bool) -> Dict[str, Any]:
    """Init the garden, plan the bundle it wrote, and return the plot's fidelity block."""
    (tmp_path / "garden.fluid.yml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "profile.json").write_text(json.dumps(_profile(**asked)), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "garden.fluid.yml"),
        profile=str(tmp_path / "profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    planned = plan(
        tmp_path / "bundle", state_dir=tmp_path / "state", profile=tmp_path / "profile.json"
    )
    assert planned.success, planned.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    fidelity: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]["plot"][
        "fidelity"
    ]
    return fidelity


def _on(fidelity: Dict[str, Any], column: str) -> List[Dict[str, Any]]:
    return [
        each
        for each in fidelity["properties"]
        if column in (each.get("columns") or [each.get("column")])
    ]


def test_a_column_drawn_over_its_contract_range_is_drawn_given_the_beds_soil_and_scored_by_nothing(
    tmp_path: Path,
) -> None:
    fidelity = _author(tmp_path, linked=True, ranked=False)
    depth = fidelity["columns"]["depth_cm"]
    assert depth["given"] == "bed.soil"
    assert sorted(depth["binWeightsGiven"]) == sorted(SOILS)
    assert "range" not in depth
    assert _on(fidelity, "depth_cm") == []


def test_two_columns_drawn_over_their_contract_ranges_are_drawn_by_one_copula_and_scored_by_nothing(
    tmp_path: Path,
) -> None:
    fidelity = _author(tmp_path, linked=False, ranked=True)
    assert fidelity["copula"]["columns"] == ["spread_cm", "width_cm"]
    assert fidelity["copula"]["rankCorrelations"]["pairs"][0]["value"] == "0.4"
    assert all("range" not in fidelity["columns"][name] for name in ("spread_cm", "width_cm"))
    assert [each for each in fidelity["properties"] if each["property"] == "rankCorrelation"] == []


def test_a_column_that_declares_a_range_keeps_its_rank_correlation_property(
    tmp_path: Path,
) -> None:
    """The refusal is of a column with no range only: a pair of columns with one is still scored."""
    document = _profile(linked=False, ranked=True)
    columns = document["tables"]["plot"]["columns"]
    for name, bins in (
        ("spread_cm", ["0", "20", "40", "60"]),
        ("width_cm", ["0", "10", "20", "30"]),
    ):
        count = PLOTS // (len(bins) - 1)
        columns[name]["statistics"] = [
            {
                "definition": "marginalHistogram/1",
                "bins": bins,
                "sampleSize": BAND,
                "disclosure": "none",
                "groups": [
                    {"group": "", "k": PLOTS, "binCounts": [count] * (len(bins) - 1), "status": "F"}
                ],
            }
        ]
    (tmp_path / "garden.fluid.yml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "profile.json").write_text(json.dumps(document), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "garden.fluid.yml"),
        profile=str(tmp_path / "profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    fidelity = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]["plot"]["fidelity"]
    assert all("range" in fidelity["columns"][name] for name in ("spread_cm", "width_cm"))
    ranked = [each for each in fidelity["properties"] if each["property"] == "rankCorrelation"]
    assert [each["columns"] for each in ranked] == [["spread_cm", "width_cm"]]
