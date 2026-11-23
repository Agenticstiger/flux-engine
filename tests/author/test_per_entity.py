# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column stable per entity is a dependency from its key, drawn from its statistic ``per`` it.

A contract ``dependency`` whose ``from`` columns are the entity's key draws each ``to`` column
once per key (EXPR-SPEC-2 section 8). ``fluxsim init`` then reads the profile's statistic counted
per that key (PROFILE-SPEC.md section 3, format 1-3-0) in place of the one that counts rows, and
says so. A per-entity statistic of a column no such dependency sets stays unused, with the reason
that a stable trait is a dependency to declare in the contract. ``init`` never writes the contract.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.author.forms import PER_ENTITY
from flux_engine.cli import main
from flux_engine.cli._init import run_init
from flux_engine.stages import pipeline
from flux_stats.profile import SCHEMA_URL_1_3_0, parse_profile

from .test_given import _group, _metric, _statistic, _with_k

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
CUSTOMERS = 200
LEVELS = ["business", "public", "retail"]
PER_ROW: List[Optional[int]] = [500, 300, 200]
PER_CUSTOMER: List[Optional[int]] = [40, 60, 100]

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: orders.shop
name: Orders shop
metadata:
  owner:
    team: shop-team
exposes:
  - exposeId: customer
    kind: table
    binding:
      platform: local
      format: parquet
      location: {{ path: ./out/customer.parquet }}
    contract:
      schema:
        - {{ name: customer_id, type: INT, required: true }}
    semantics:
      entities:
        - {{ name: customer, type: primary, expr: customer_id }}
  - exposeId: purchase
    kind: table
    binding:
      platform: local
      format: parquet
      location: {{ path: ./out/purchase.parquet }}
    contract:
      schema:
        - {{ name: purchase_id, type: INT, required: true }}
        - {{ name: customer_id, type: INT, required: true }}
        - {{ name: channel, type: "enum('web','shop')", required: true }}
        - {{ name: segment, type: "enum('business','public','retail')", required: true }}
{invariants}    semantics:
      entities:
        - {{ name: purchase, type: primary, expr: purchase_id }}
      relationships:
        - name: purchase_to_customer
          to: customer
          fromColumns: [customer_id]
          toColumns: [customer_id]
          cardinality: many_to_one
"""
DEPENDENCY = """\
      invariants:
        - id: segment_per_customer
          dependency:
            from: [customer_id]
            to: [segment]
"""
OTHER_KEY = DEPENDENCY.replace("from: [customer_id]", "from: [purchase_id]")
SEGMENT = "/tables/purchase/columns/segment/statistics"


def _number_column(count: int, high: int, statistic: Optional[Dict[str, Any]] = None) -> Any:
    column: Dict[str, Any] = {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", count),
            "min": _metric("min/1", "1"),
            "max": _metric("max/1", str(high)),
        },
    }
    if statistic is not None:
        column["statistics"] = [statistic]
    return column


def _profile(*, per: Optional[List[str]] = None) -> Dict[str, Any]:
    per_customer = _statistic(
        "levelHistogram/1",
        [_with_k(_group("", PER_CUSTOMER), CUSTOMERS)],
        levels=LEVELS,
        per=per or ["customer_id"],
    )
    per_row = _statistic("levelHistogram/1", [_with_k(_group("", PER_ROW), 1000)], levels=LEVELS)
    document: Dict[str, Any] = {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": SCHEMA_URL_1_3_0,
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
                "rowCount": CUSTOMERS,
                "columns": {
                    "customer_id": _number_column(
                        CUSTOMERS,
                        CUSTOMERS,
                        _statistic(
                            "mean/1",
                            [
                                {
                                    "group": "",
                                    "k": CUSTOMERS,
                                    "Sx": str(20100 * 65536),
                                    "status": "F",
                                }
                            ],
                        ),
                    )
                },
            },
            "purchase": {
                "rowCount": 1000,
                "columns": {
                    "purchase_id": _number_column(1000, 1000),
                    "customer_id": _number_column(
                        1000,
                        CUSTOMERS,
                        _statistic(
                            "marginalHistogram/1",
                            [_with_k(_group("", [1000], "binCounts"), 1000)],
                            bins=["1", str(CUSTOMERS)],
                        ),
                    ),
                    "segment": {
                        "domain": "category",
                        "metrics": {"count": _metric("count/1", 1000)},
                        "statistics": [per_row, per_customer],
                    },
                },
            },
        },
    }
    parse_profile(copy.deepcopy(document))
    return document


def _init(tmp_path: Path, invariants: str, profile: Dict[str, Any]) -> Any:
    (tmp_path / "shop.fluid.yaml").write_text(CONTRACT.format(invariants=invariants), "utf-8")
    (tmp_path / "shop.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "shop.fluid.yaml"),
        profile=str(tmp_path / "shop.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _fidelity(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block["emits"]["purchase"]["fidelity"]


def _states(result: Any) -> Dict[str, Dict[str, Any]]:
    return {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}


def test_the_per_entity_shares_reach_the_bundle(tmp_path: Path) -> None:
    result = _init(tmp_path, DEPENDENCY, _profile())
    segment = _fidelity(tmp_path)["columns"]["segment"]
    assert segment["levels"] == {"business": "0.2", "public": "0.3", "retail": "0.5"}
    states = _states(result)
    assert states[f"{SEGMENT}/1"]["state"] == "used"
    assert states[f"{SEGMENT}/0"]["state"] == "unused"
    assert "once per customer_id" in states[f"{SEGMENT}/0"]["note"]
    assert "counted per customer_id" in result.stdout
    # A row count is no entity count: no histogram holds the run to the per-entity shares.
    assert [p for p in _fidelity(tmp_path)["properties"] if p.get("column") == "segment"] == []


def test_without_the_dependency_the_per_row_shares_are_drawn_and_the_trait_is_unused(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, "", _profile())
    segment = _fidelity(tmp_path)["columns"]["segment"]
    assert segment["levels"] == {"business": "0.5", "public": "0.3", "retail": "0.2"}
    state = _states(result)[f"{SEGMENT}/1"]
    assert state["state"] == "unused" and state["note"] == PER_ENTITY
    assert "contract dependency from the entity key" in PER_ENTITY


def test_a_dependency_from_what_is_not_a_key_reads_the_per_row_statistic(tmp_path: Path) -> None:
    result = _init(
        tmp_path, DEPENDENCY.replace("from: [customer_id]", "from: [channel]"), _profile()
    )
    state = _states(result)[f"{SEGMENT}/1"]
    assert state["state"] == "unused" and "not a key of the table" in state["note"]


def test_a_dependency_from_other_columns_than_the_statistic_is_counted_per(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, OTHER_KEY, _profile())  # purchase_id is a key, customer_id is not it
    segment = _fidelity(tmp_path)["columns"]["segment"]
    assert segment["levels"]["business"] == "0.5"
    state = _states(result)[f"{SEGMENT}/1"]
    assert state["state"] == "unused" and "not what it is counted per" in state["note"]


def test_init_never_writes_the_contract(tmp_path: Path) -> None:
    _init(tmp_path, "", _profile())
    written = (tmp_path / "bundle" / "shop.fluid.yaml").read_text(encoding="utf-8")
    assert written == CONTRACT.format(invariants="")
    assert "dependency" not in written


@pytest.mark.parametrize("kernel", ["0.1.0", "0.2.0"])
def test_the_runs_per_entity_shares_match_the_profile(tmp_path: Path, kernel: str) -> None:
    from flux_engine.core import kernels

    _init(tmp_path, DEPENDENCY, _profile())
    bundle = tmp_path / "bundle"
    result = (
        pipeline.execute(bundle)
        if kernel == "0.1.0"
        else pipeline.execute(bundle, kernel=kernels.kernel_named(kernel))
    )
    outcome = next(e for e in result.emits if e.resolved.expose_id == "purchase")
    names = list(outcome.expose.column_names)
    rows = [dict(zip(names, row, strict=True)) for row in outcome.rows]
    traits: Dict[Any, set] = {}
    for row in rows:
        traits.setdefault(row["customer_id"], set()).add(row["segment"])
    assert all(len(found) == 1 for found in traits.values())
    shares = Counter(next(iter(found)) for found in traits.values())
    for level, published in zip(LEVELS, [40, 60, 100], strict=True):
        assert abs(shares[level] / len(traits) - published / CUSTOMERS) < 0.1, shares
    # The shares of rows are the per-entity shares carried by the entities' rows, not the
    # per-row shares of the profile.
    rows_by_level = Counter(row["segment"] for row in rows)
    assert rows_by_level["retail"] > rows_by_level["business"]


def test_fluxsim_init_then_run_verify_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "shop.fluid.yaml").write_text(CONTRACT.format(invariants=DEPENDENCY), "utf-8")
    (tmp_path / "shop.profile.json").write_text(json.dumps(_profile()), encoding="utf-8")
    bundle = tmp_path / "bundle"
    args = ["--profile", str(tmp_path / "shop.profile.json")]
    code = main(["init", str(bundle), "--contract", str(tmp_path / "shop.fluid.yaml"), *args])
    assert code == 0, capsys.readouterr().err
    code = main(["run", str(bundle), "--verify", *args, "--state-dir", str(tmp_path / "state")])
    captured = capsys.readouterr()
    assert code == 0, captured.err + captured.out
