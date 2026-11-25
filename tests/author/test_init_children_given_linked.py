# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` never places a counted child by a parent column drawn given a linked row.

A library has members, and each member has loans. The members' ``tier`` is drawn given the
``district`` of the library they belong to (a statistic grouped by a link), and the loans per member
are published grouped by ``tier`` and by ``card``. A ``counts`` entry reads the parent's own row, so
the plan refuses a given column that is itself drawn given a column of a linked row. Pinned here,
through the command and a plan: the grouping by ``tier`` is passed over in the ledger with the
reason, the entry is given the next column that can be one (or none), and the bundle plans.
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
from tests.author.test_init_children_given import (
    EDGES,
    SAMPLE,
    SEGMENTS,
    _histogram,
    _ledger,
    _metric,
    _ungrouped,
)

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
MEMBERS = 4000
LOANS = 6000
LIBRARIES = ["L1", "L2", "L3", "L4"]
DISTRICTS = {"L1": "east", "L2": "east", "L3": "west", "L4": "west"}
TIERS = ("adult", "junior")
CARDS = ("basic", "plus")

#: The loans-per-member counts of each level, in the order of the levels.
_SHAPES = [SEGMENTS[name][1] for name in sorted(SEGMENTS)]

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: town.library
name: Library
metadata:
  owner:
    team: town
exposes:
  - exposeId: library
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/library.parquet }
    contract:
      schema:
        - { name: library_id, type: STRING, required: true }
        - { name: district, type: STRING, required: true }
    semantics:
      entities:
        - { name: library, type: primary, expr: library_id }
  - exposeId: member
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/member.parquet }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
        - { name: library_id, type: STRING, required: true }
        - { name: tier, type: STRING, required: true }
        - { name: card, type: STRING, required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
      relationships:
        - name: member_library
          to: library
          fromColumns: [library_id]
          toColumns: [library_id]
          cardinality: many_to_one
  - exposeId: loan
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/loan.parquet }
    contract:
      schema:
        - { name: loan_id, type: INT, required: true }
        - { name: member_id, type: INT, required: true }
    semantics:
      entities:
        - { name: loan, type: primary, expr: loan_id }
      relationships:
        - name: loan_member
          to: member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
"""


def _levels(names: List[str], counts: List[int]) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "levels": names,
        "sampleSize": SAMPLE,
        "disclosure": "none",
        "groups": [{"group": "", "k": sum(counts), "levelCounts": counts, "status": "F"}],
    }


def _tier_by_district() -> Dict[str, Any]:
    """The members' tier grouped by the district of their library: the link draw of ``tier``."""
    statistic = _levels(list(TIERS), [1, 1])
    statistic["groupByLink"] = {
        "parent": {"table": "library", "columns": ["library_id"]},
        "columns": ["library_id"],
        "column": "district",
    }
    statistic["groups"] = [
        {"group": "east", "k": 2000, "levelCounts": [1500, 500], "status": "F"},
        {"group": "west", "k": 2000, "levelCounts": [1000, 1000], "status": "F"},
    ]
    return statistic


def _grouped_by(column: str) -> Dict[str, Any]:
    """The loans per member grouped by *column* of the member: one group a level."""
    levels = TIERS if column == "tier" else CARDS
    statistic = _histogram(EDGES, [0, 0, 0], more={"groupBy": column})
    statistic["groups"] = [
        {"group": level, "k": sum(counts), "binCounts": counts, "status": "F"}
        for level, counts in zip(levels, _SHAPES, strict=True)
    ]
    return statistic


def _profile(statistics: List[Dict[str, Any]]) -> Dict[str, Any]:
    key = {
        "count": _metric("count/1", MEMBERS),
        "min": _metric("min/1", "1"),
        "max": _metric("max/1", str(MEMBERS)),
        "nullCount": _metric("nullCount/1", 0),
    }
    loan_key = {
        "count": _metric("count/1", LOANS),
        "min": _metric("min/1", "1"),
        "max": _metric("max/1", str(LOANS)),
    }
    even = [MEMBERS // len(LIBRARIES)] * len(LIBRARIES)
    sizes = [SEGMENTS[name][0] for name in sorted(SEGMENTS)]
    return {
        "_producer": "urn:example:children-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "member": {
                "rowCount": MEMBERS,
                "columns": {
                    "member_id": {"domain": "number", "metrics": key},
                    "library_id": {
                        "domain": "category",
                        "statistics": [_levels(LIBRARIES, even)],
                    },
                    "tier": {
                        "domain": "category",
                        "statistics": [_levels(list(TIERS), sizes[::-1]), _tier_by_district()],
                    },
                    "card": {
                        "domain": "category",
                        "statistics": [_levels(list(CARDS), sizes[::-1])],
                    },
                },
            },
            "loan": {
                "rowCount": LOANS,
                "columns": {
                    "loan_id": {"domain": "number", "metrics": loan_key},
                    "member_id": {
                        "domain": "number",
                        "metrics": {"count": _metric("count/1", LOANS)},
                    },
                },
            },
        },
        "references": {
            "library": {
                "disclosure": "none",
                "columns": ["library_id", "district"],
                "rows": [[name, DISTRICTS[name]] for name in LIBRARIES],
            }
        },
        "relationships": [
            {
                "parent": {"table": "member", "columns": ["member_id"]},
                "child": {"table": "loan", "columns": ["member_id"]},
                "statistics": statistics,
            }
        ],
    }


def _init(tmp_path: Path, statistics: List[Dict[str, Any]]) -> Any:
    (tmp_path / "library.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "library.profile.json").write_text(
        json.dumps(_profile(statistics)), encoding="utf-8"
    )
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "library.fluid.yaml"),
        profile=str(tmp_path / "library.profile.json"),
    )


def _block(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)["extensions"][NAMESPACE]  # type: ignore[no-any-return]


def _planned(tmp_path: Path) -> Any:
    return plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "library.profile.json",
    )


def test_a_given_column_drawn_given_a_linked_row_is_passed_over_and_the_next_is_used(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, [_ungrouped(), _grouped_by("tier"), _grouped_by("card")])
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert (
        block["emits"]["member"]["fidelity"]["columns"]["tier"]["given"]
        == "member_library.district"
    )
    (entry,) = block["counts"]
    assert entry["given"] == "card"
    skipped = _ledger(result, "relationships/0/statistics/1")
    assert skipped["state"] == "unused"
    assert "drawn given a column of a linked row" in skipped["note"]
    assert _ledger(result, "relationships/0/statistics/2")["state"] == "used"
    assert _planned(tmp_path).success


def test_with_no_other_grouping_the_entry_has_no_given_and_the_statistic_says_why(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, [_ungrouped(), _grouped_by("tier")])
    assert result.exit_code == 0, result.stderr
    (entry,) = _block(tmp_path)["counts"]
    assert "given" not in entry
    skipped = _ledger(result, "relationships/0/statistics/1")
    assert skipped["state"] == "unused"
    assert "drawn given a column of a linked row" in skipped["note"]
    assert _planned(tmp_path).success
