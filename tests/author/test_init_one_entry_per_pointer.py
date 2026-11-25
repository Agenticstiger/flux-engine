# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --window`` gives each pointer of the parameter register one entry.

Two facts can place the same bins of a date column: the profile withholds the minimum,
so the rows below the outermost published value are assumed to sit in the outermost
bin, and the window ``--window`` gives cuts the bins. The register refuses two entries
for one pointer, so the bundle carries one, whose reason names both. Pinned through the
command and the plan stage, on a parent's and a child's date column, neither of which
declares a range.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.plan import plan
from tests.author.test_binned_quantiles import _DATE_LADDER, _binned
from tests.author.test_columns import _metric
from tests.author.test_ledger import _profile as _ledger_profile

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: library.lending
name: Lending library
metadata:
  owner:
    team: library-team
exposes:
  - exposeId: branches
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/branches.parquet }
    contract:
      schema:
        - { name: branch_id, type: INT, required: true }
        - name: opened_on
          type: DATE
          required: true
    semantics:
      entities:
        - { name: branch, type: primary, expr: branch_id }
  - exposeId: loans
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/loans.parquet }
    contract:
      schema:
        - { name: loan_id, type: INT, required: true }
        - { name: branch_id, type: INT, required: true }
        - name: loaned_on
          type: DATE
          required: true
    semantics:
      entities:
        - { name: loan, type: primary, expr: loan_id }
      relationships:
        - name: loan_to_branch
          to: branches
          fromColumns: [branch_id]
          toColumns: [branch_id]
          cardinality: many_to_one
"""


def _dated(rows: int, maximum: str) -> Dict[str, Any]:
    """A date column over three binned quantiles, the minimum and the quantiles' values withheld."""
    return {
        "domain": "timestamp",
        "metrics": {
            "count": _metric("count/1", rows),
            "min": _metric("min/1", status="A"),
            "max": _metric("max/1", maximum),
        },
        "statistics": [
            {**_binned(_DATE_LADDER, 1, rows // 5, rows // 2), "level": "0.25"},
            {**_binned(_DATE_LADDER, 3, rows // 2, rows), "level": "0.5"},
            {**_binned(_DATE_LADDER, 4, rows, rows + rows * 3 // 5), "level": "0.75"},
        ],
    }


def _profile() -> Dict[str, Any]:
    base = _ledger_profile()
    base["_schemaURL"] = base["_schemaURL"].replace("1-0-0", "1-1-0")
    ids = {"domain": "number", "metrics": {"count": _metric("count/1", 100)}}
    key = lambda rows: {  # noqa: E731
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", rows),
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", str(rows)),
        },
    }
    base["tables"] = {
        "branches": {
            "rowCount": 100,
            "columns": {
                "branch_id": key(100),
                "opened_on": _dated(100, "1788220800000"),
            },
        },
        "loans": {
            "rowCount": 500,
            "columns": {
                "loan_id": key(500),
                "branch_id": ids,
                "loaned_on": _dated(500, "1788220800000"),
            },
        },
    }
    base["relationships"] = [
        {
            "parent": {"table": "branches", "columns": ["branch_id"]},
            "child": {"table": "loans", "columns": ["branch_id"]},
            "statistics": base["relationships"][0]["statistics"],
        }
    ]
    return base


def _author(
    root: Path, window: Optional[Tuple[str, str]] = None, contract: str = CONTRACT
) -> Tuple[Any, Path]:
    root.mkdir(parents=True)
    (root / "c.fluid.yaml").write_text(contract, encoding="utf-8")
    (root / "p.json").write_text(json.dumps(_profile()), encoding="utf-8")
    bundle = root / "bundle"
    result = run_init(
        str(bundle),
        contract=str(root / "c.fluid.yaml"),
        profile=str(root / "p.json"),
        window=window,
    )
    return result, bundle


def _pointers(bundle: Path) -> List[Tuple[str, str]]:
    text = (bundle / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]["parameters"]
    return [(entry["document"], entry["pointer"]) for entry in block]


@pytest.mark.parametrize(
    "window", [None, ("2021-01-01", "2026-12-31"), ("2023-06-01", "2026-12-31")]
)
def test_each_pointer_has_one_entry_and_the_bundle_plans(
    window: Optional[Tuple[str, str]], tmp_path: Path
) -> None:
    result, bundle = _author(tmp_path / "x", window)
    assert result.exit_code == 0, result.stderr
    pointers = _pointers(bundle)
    assert len(pointers) == len(set(pointers)), sorted(p for p in pointers if pointers.count(p) > 1)
    outcome = plan(bundle, state_dir=tmp_path / "state", profile=tmp_path / "x" / "p.json")
    assert outcome.success, outcome.stderr


def test_a_window_cut_names_the_withheld_minimum_it_replaces(tmp_path: Path) -> None:
    result, bundle = _author(tmp_path / "x", ("2023-06-01", "2026-12-31"))
    assert result.exit_code == 0, result.stderr
    text = (bundle / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]["parameters"]
    weights = [
        entry for entry in block if entry["pointer"].endswith("/columns/loaned_on/binWeights")
    ]
    assert len(weights) == 1
    reason = weights[0]["reason"]
    assert "withheld min" in reason and "reach beyond the run's time window" in reason
