# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Children per parent become a foreign key's spread (ADR 0014 section 5).

Pinned here, through ``fluxsim init`` and a run:

* a relationship's histogram of children per parent lays its classes over the
  parent's keys, each a share of the keys equal to its share of the parents and
  weighted by its children, the class of none at 0;
* the run's children per parent keep the published classes within the
  documented approximation: about ``e^-m`` more parents of a class of mean ``m``
  receive no child;
* a mean alone leaves the spread even; a relationship the contract does not
  declare is unused, with the reason;
* a foreign key with a histogram of its own keeps each of its bins' children,
  the classes laid out inside each bin.
"""

from __future__ import annotations

import collections
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SAMPLE = {"min": 950, "max": 1050}
BINS = ["0", "1", "2", "3", "5", "10", "20", "100"]
PARENTS = [150, 150, 150, 200, 200, 100, 50]

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: children.shop
name: Children shop
metadata:
  owner:
    team: shop-team
exposes:
  - exposeId: customer
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/customer.parquet }
    contract:
      schema:
        - { name: customer_id, type: INT, required: true }
    semantics:
      entities:
        - { name: customer, type: primary, expr: customer_id }
  - exposeId: orders
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/orders.parquet }
    contract:
      schema:
        - { name: order_id, type: INT, required: true }
        - { name: customer_id, type: INT, required: true }
    semantics:
      entities:
        - { name: order, type: primary, expr: order_id }
      relationships:
        - name: order_to_customer
          to: customer
          fromColumns: [customer_id]
          toColumns: [customer_id]
          cardinality: many_to_one
"""


def _metric(definition: str, value: Any) -> Dict[str, Any]:
    return {
        "definition": definition,
        "status": "F",
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "value": value,
    }


def _histogram(bins: List[str], counts: List[int], k: int) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "bins": bins,
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": k, "binCounts": counts, "status": "F"}],
    }


def _mean(total: int, k: int) -> Dict[str, Any]:
    return {
        "definition": "mean/1",
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": k, "Sx": str(total * 2**16), "status": "F"}],
    }


def _profile(
    statistics: List[Dict[str, Any]],
    *,
    child: Optional[Dict[str, Any]] = None,
    columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    key = {"count": _metric("count/1", 1000), "min": _metric("min/1", "1")}
    key["max"] = _metric("max/1", "1000")
    order_key = {"count": _metric("count/1", 5000), "min": _metric("min/1", "1")}
    order_key["max"] = _metric("max/1", "5000")
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-0-0/FluxProfile.json",
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "acro-default": {
                "dominance": {"k": "0.90", "n": 2},
                "minimumCell": 10,
                "pRatio": "0.10",
                "roundingBase": 5,
                "zerosDisclosive": True,
            }
        },
        "tables": {
            "customer": {
                "rowCount": 1000,
                "columns": {"customer_id": {"domain": "number", "metrics": key}},
            },
            "orders": {
                "rowCount": 5000,
                "columns": {
                    "order_id": {"domain": "number", "metrics": order_key},
                    "customer_id": child
                    or {"domain": "number", "metrics": {"count": _metric("count/1", 5000)}},
                },
            },
        },
        "relationships": [
            {
                "parent": {"table": "customer", "columns": ["customer_id"]},
                "child": {"table": "orders", "columns": columns or ["customer_id"]},
                "statistics": statistics,
            }
        ],
    }


def _init(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    (tmp_path / "shop.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "shop.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "shop.fluid.yaml"),
        profile=str(tmp_path / "shop.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _foreign(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block["emits"]["orders"]["fidelity"]["columns"]["customer_id"]


def _states(result: Any) -> Dict[str, Dict[str, Any]]:
    return {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}


def _children(tmp_path: Path) -> List[int]:
    state = tmp_path / "state"
    outcome = run(tmp_path / "bundle", state_dir=state, profile=tmp_path / "shop.profile.json")
    assert outcome.success, outcome.stderr
    run_id = outcome.artifacts["plan"]["run_id"]
    rows = (state / "runs" / run_id / "emits" / "orders.ndjson").read_text(encoding="utf-8")
    counts = collections.Counter(json.loads(line)[1] for line in rows.splitlines() if line)
    return [counts.get(key, 0) for key in range(1, 1001)]


def test_the_classes_are_laid_over_the_parent_s_keys(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)]))
    spread = _foreign(tmp_path)
    # Busiest class first: 50 keys of 20 or more (its mean solved to 20 from the total of
    # 5000), 100 of 10 to 19, ... and 150 keys of none, at weight 0.
    assert spread["bins"] == ["1", "51", "151", "351", "551", "701", "851", "1000"]
    assert spread["binWeights"] == ["0.2", "0.29", "0.28", "0.14", "0.06", "0.03", "0"]
    states = _states(result)
    assert states["/relationships/0/statistics/0"]["state"] == "used"
    assert states["/relationships/0/statistics/1"]["state"] == "used"
    assert "e^-m" in result.stdout


def test_the_run_keeps_the_classes_within_the_documented_bound(tmp_path: Path) -> None:
    _init(tmp_path, _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)]))
    counts = _children(tmp_path)
    means = [1, 2, 3.5, 7, 14.5, 20]
    extra = sum(n * math.exp(-m) for n, m in zip(PARENTS[1:], means, strict=True)) / 1000
    childless = counts.count(0) / 1000
    assert abs(childless - (0.15 + extra)) < 0.05
    many = sum(1 for count in counts if count >= 10) / 1000
    assert abs(many - 0.15) < 0.05  # 10 or more: 100 + 50 of 1000 parents


def test_a_mean_alone_leaves_the_spread_even(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile([_mean(5000, 1000)]))
    spread = _foreign(tmp_path)
    assert spread["binWeights"] == ["1"]
    assert spread["bins"] == ["1", "1000"]
    assert "a mean alone" in _states(result)["/relationships/0/statistics/0"]["note"]


def test_a_relationship_the_contract_does_not_declare_is_unused(tmp_path: Path) -> None:
    profile = _profile([_histogram(BINS, PARENTS, 1000)], columns=["order_id"])
    result = _init(tmp_path, profile)
    state = _states(result)["/relationships/0/statistics/0"]
    assert state["state"] == "unused"
    assert "declares no such relationship" in state["note"]


def test_a_foreign_key_s_own_histogram_keeps_each_bin_s_children(tmp_path: Path) -> None:
    child = {
        "domain": "number",
        "metrics": {"count": _metric("count/1", 5000)},
        "statistics": [_histogram(["1", "501", "1000"], [4000, 1000], 5000)],
    }
    profile = _profile([_histogram(BINS, PARENTS, 1000), _mean(5000, 1000)], child=child)
    result = _init(tmp_path, profile)
    spread = _foreign(tmp_path)
    edges = [int(edge) for edge in spread["bins"]]
    weights = [float(weight) for weight in spread["binWeights"]]
    below = sum(weight for edge, weight in zip(edges, weights, strict=False) if edge < 501)
    assert abs(below - 0.8) < 1e-6  # the first of its own bins keeps 4000 of 5000 children
    states = _states(result)
    assert states["/tables/orders/columns/customer_id/statistics/0"]["state"] == "used"
