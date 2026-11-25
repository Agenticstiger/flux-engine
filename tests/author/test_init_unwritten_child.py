# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes no ``counts`` entry for a table it does not write.

A table made only of foreign keys has nothing for its fidelity block to declare once a ``counts``
entry has set its keys, so it is not written (``FLX-5034``). The entry was laid out before that
was known, and the bundle named an emit the Simulation does not write (``FLX-3105``), so ``init``
refused its own output. Pinned here, through ``fluxsim init`` and ``plan``, on a private release
of a world where a link table has two parents and the profile publishes neither its rows nor
anything of its keys:

* the bundle validates and plans, whether the rows are withheld or published;
* the link table is told as not written, with the reason, and no ``counts`` entry names it;
* each statistic of the two relationships into it is listed unused, with the reason;
* a relationship between tables that are written keeps its entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.plan import plan

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SCHEMA = "https://agenticstiger.github.io/flux/engine/spec/profile/1-4-0/FluxProfile.json"
FILES = ("library.fluid.yaml", "library.profile.json")
SAMPLE = {"min": 0}
BINS = [str(count) for count in range(0, 10)]

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: library.loans
name: Library loans
metadata:
  owner:
    team: library-team
exposes:
  - exposeId: branch
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/branch.parquet }
    contract:
      schema:
        - { name: branch_id, type: INT, required: true }
        - { name: size, type: "enum('large','small')", required: true }
    semantics:
      entities:
        - { name: branch, type: primary, expr: branch_id }
  - exposeId: member
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/member.parquet }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
        - { name: tier, type: "enum('basic','plus')", required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
  - exposeId: loan
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/loan.parquet }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
        - { name: branch_id, type: INT, required: true }
    semantics:
      relationships:
        - name: loan_to_member
          to: member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
        - name: loan_to_branch
          to: branch
          fromColumns: [branch_id]
          toColumns: [branch_id]
          cardinality: many_to_one
  - exposeId: review
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/review.parquet }
    contract:
      schema:
        - { name: review_id, type: INT, required: true }
        - { name: member_id, type: INT, required: true }
    semantics:
      entities:
        - { name: review, type: primary, expr: review_id }
      relationships:
        - name: review_to_member
          to: member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
"""

_NOISE = {"mechanism": "discreteGaussian", "scale": "2", "privacyLoss": {"rho": "0.125"}}


def _levels(levels: List[str], values: List[str]) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "sampleSize": SAMPLE,
        "disclosure": "noisy",
        "levels": levels,
        "noise": _NOISE,
        "groups": [{"group": "", "status": "F", "noisyCounts": values}],
    }


def _per_parent(values: List[int]) -> Dict[str, Any]:
    """A noisy histogram of children per parent over the classes 0 to 8 children."""
    return {
        "definition": "marginalHistogram/1",
        "sampleSize": SAMPLE,
        "disclosure": "noisy",
        "bins": BINS,
        # A unit reaches up to 20 children, so a count table spends 800 / (2 * scale^2).
        "noise": {**_NOISE, "privacyLoss": {"rho": "100"}},
        "groups": [{"group": "", "status": "F", "noisyCounts": [str(count) for count in values]}],
    }


def _profile(*, loans: Optional[int] = None) -> Dict[str, Any]:
    """A private release: no table publishes its rows, and the link table publishes nothing at all."""
    link: Dict[str, Any] = {
        "columns": {"member_id": {"domain": "number"}, "branch_id": {"domain": "number"}}
    }
    if loans is not None:
        link["rowCount"] = loans
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": SCHEMA,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"noisy": {}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "1000"},
            "privacyUnit": {
                "expose": "member",
                "follows": [
                    {
                        "parent": {"table": "member", "columns": ["member_id"]},
                        "child": {"table": child, "columns": ["member_id"]},
                        "maxChildren": 20,
                    }
                    for child in ("loan", "review")
                ],
            },
            "publicStructure": True,
        },
        "tables": {
            "branch": {
                "columns": {
                    "branch_id": {"domain": "number"},
                    "size": {"domain": "category"},
                }
            },
            "member": {
                "columns": {
                    "member_id": {"domain": "number"},
                    "tier": {
                        "domain": "category",
                        "statistics": [_levels(["basic", "plus"], ["400", "250"])],
                    },
                }
            },
            "loan": link,
            "review": {
                "columns": {"review_id": {"domain": "number"}, "member_id": {"domain": "number"}}
            },
        },
        "relationships": [
            {
                "parent": {"table": "member", "columns": ["member_id"]},
                "child": {"table": "loan", "columns": ["member_id"]},
                "statistics": [_per_parent([90, 120, 100, 80, 60, 40, 30, 20, 10])],
            },
            {
                "parent": {"table": "branch", "columns": ["branch_id"]},
                "child": {"table": "loan", "columns": ["branch_id"]},
                "statistics": [_per_parent([2, 3, 6, 8, 9, 7, 4, 2, 1])],
            },
            {
                "parent": {"table": "member", "columns": ["member_id"]},
                "child": {"table": "review", "columns": ["member_id"]},
                "statistics": [_per_parent([210, 190, 120, 70, 30, 20, 10, 0, 0])],
            },
        ],
    }


def _written(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    (tmp_path / FILES[0]).write_text(CONTRACT, encoding="utf-8")
    (tmp_path / FILES[1]).write_text(json.dumps(profile), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / FILES[0]),
        profile=str(tmp_path / FILES[1]),
    )


def _namespace(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block


@pytest.mark.parametrize("loans", [None, 500], ids=["rows withheld", "rows published"])
def test_a_link_table_with_nothing_to_declare_is_left_out_with_its_entries(
    tmp_path: Path, loans: Optional[int]
) -> None:
    result = _written(tmp_path, _profile(loans=loans))
    assert result.exit_code == 0, result.stderr
    block = _namespace(tmp_path)
    assert "loan" not in block["emits"]
    assert [entry["emit"] for entry in block.get("counts", [])] == ["review"]
    assert "loan: not written" in result.stdout
    assert "FLX-5034" in result.stdout
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / FILES[1],
        kernel="0.2.0",
        stage=False,
    )
    assert planned.success, planned.stderr


def test_each_statistic_into_the_link_table_is_listed_unused_with_the_reason(
    tmp_path: Path,
) -> None:
    result = _written(tmp_path, _profile())
    assert result.exit_code == 0, result.stderr
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    for relationship in (0, 1):
        state = states[f"/relationships/{relationship}/statistics/0"]
        assert state["state"] == "unused"
        assert "no generator emit of one of its two tables" in state["note"]
    assert states["/relationships/2/statistics/0"]["state"] == "used"
