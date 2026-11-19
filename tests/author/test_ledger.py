# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init``'s ledger and ``--strict`` (ADR 0014 sections 1 and 2).

Pinned here, through the command:

* every statistic and metric of the profile is listed once, by its JSON
  Pointer, as used, unused with its reason, or withheld;
* the Simulation lists the unused ones under ``unused``, so the record of a run
  names register format 1-2-0 and carries them, and ``ship``'s evidence names
  them under "What Was Not Proven";
* ``--strict`` writes nothing and names every unused statistic and every gap;
  ``--allow`` accepts the ones at or below a pointer, and an ``--allow`` that
  accepts nothing is refused too;
* ``--allow`` without ``--strict``, and either without a profile, is a usage
  error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.evidence.register import REGISTER_SCHEMA_URL_1_2
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SAMPLE = {"min": 950, "max": 1050}

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: ledger.shop
name: Ledger shop
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
        - { name: segment, type: "enum('A','B')", required: true }
        - { name: tier, type: "enum('gold','silver')", required: true }
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
        - { name: amount, type: INT, required: true }
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


def _metric(definition: str, value: Any = None, status: str = "F") -> Dict[str, Any]:
    metric: Dict[str, Any] = {
        "definition": definition,
        "status": status,
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
    }
    if value is not None:
        metric["value"] = value
    return metric


def _levels(levels: Any, counts: Any, group_by: Any = None, groups: Any = None) -> Dict[str, Any]:
    statistic: Dict[str, Any] = {
        "definition": "levelHistogram/1",
        "levels": levels,
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
    }
    if group_by is not None:
        statistic["groupBy"] = group_by
        statistic["groups"] = groups
    else:
        statistic["groups"] = [
            {"group": "", "k": sum(counts), "levelCounts": counts, "status": "F"}
        ]
    return statistic


def _profile() -> Dict[str, Any]:
    customer = {
        "customer_id": {
            "domain": "number",
            "metrics": {
                "count": _metric("count/1", 100),
                "min": _metric("min/1", "1"),
                "max": _metric("max/1", "100"),
                "nullCount": _metric("nullCount/1", status="A"),
            },
        },
        "segment": {
            "domain": "category",
            "metrics": {"count": _metric("count/1", 100)},
            "statistics": [_levels(["A", "B"], [60, 40])],
        },
        "tier": {
            "domain": "category",
            "metrics": {"count": _metric("count/1", 100)},
            "statistics": [
                _levels(["gold", "silver"], [50, 50]),
                _levels(
                    ["gold", "silver"],
                    None,
                    group_by="segment",
                    groups=[
                        {"group": "A", "k": 60, "levelCounts": [45, 15], "status": "F"},
                        {"group": "B", "k": 40, "levelCounts": [5, 35], "status": "F"},
                    ],
                ),
            ],
        },
    }
    orders = {
        "order_id": {
            "domain": "number",
            "metrics": {
                "count": _metric("count/1", 500),
                "min": _metric("min/1", "1"),
                "max": _metric("max/1", "500"),
            },
        },
        "customer_id": {"domain": "number", "metrics": {"count": _metric("count/1", 500)}},
        "amount": {
            "domain": "number",
            "metrics": {
                "count": _metric("count/1", 500),
                "min": _metric("min/1", "1"),
                "max": _metric("max/1", "99"),
            },
            "statistics": [
                {
                    "definition": "mean/1",
                    "sampleSize": SAMPLE,
                    "disclosure": "acro-default",
                    "groups": [{"group": "", "k": 500, "Sx": str(50 * 500 * 65536), "status": "F"}],
                }
            ],
        },
    }
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
            "customer": {"rowCount": 100, "columns": customer},
            "orders": {
                "rowCount": 500,
                "columns": orders,
                "columnPairs": [
                    {
                        "columns": ["order_id", "amount"],
                        "statistics": [
                            {
                                "definition": "correlation/1",
                                "sampleSize": SAMPLE,
                                "disclosure": "acro-default",
                                "groups": [
                                    {
                                        "group": "",
                                        "k": 500,
                                        "Sx": "0",
                                        "Sy": "0",
                                        "Sxx": "0",
                                        "Syy": "0",
                                        "Sxy": "0",
                                        "status": "F",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        },
        "relationships": [
            {
                "parent": {"table": "customer", "columns": ["customer_id"]},
                "child": {"table": "orders", "columns": ["customer_id"]},
                "statistics": [
                    {
                        "definition": "mean/1",
                        "sampleSize": SAMPLE,
                        "disclosure": "acro-default",
                        "groups": [
                            {"group": "", "k": 100, "Sx": str(5 * 100 * 65536), "status": "F"}
                        ],
                    }
                ],
            }
        ],
    }


GROUPED = "/tables/customer/columns/tier/statistics/1"
CHILDREN = "/relationships/0/statistics/0"
#: A correlation between two number columns: no declaration of a generator emit reads it.
PAIR = "/tables/orders/columnPairs/0/statistics/0"


@pytest.fixture
def inputs(tmp_path: Path) -> Dict[str, Path]:
    contract = tmp_path / "shop.fluid.yaml"
    contract.write_text(CONTRACT, encoding="utf-8")
    profile = tmp_path / "shop.profile.json"
    profile.write_text(json.dumps(_profile()), encoding="utf-8")
    return {"contract": contract, "profile": profile, "root": tmp_path}


def _init(inputs: Dict[str, Path], name: str, **options: Any) -> Any:
    return run_init(
        str(inputs["root"] / name),
        contract=str(inputs["contract"]),
        profile=str(inputs["profile"]),
        **options,
    )


def test_every_statistic_is_listed_once_with_its_state(inputs: Dict[str, Path]) -> None:
    result = _init(inputs, "bundle")
    assert result.exit_code == 0, result.stderr
    ledger = result.artifacts["ledger"]
    states = {entry["pointer"]: entry for entry in ledger["statistics"]}
    assert len(states) == len(ledger["statistics"])
    assert states[GROUPED]["state"] == "used"
    assert "weights of tier given segment" in states[GROUPED]["note"]
    assert states[PAIR]["state"] == "unused"
    assert "correlation" in states[PAIR]["note"]
    assert states[CHILDREN]["state"] == "used"
    assert "a mean alone" in states[CHILDREN]["note"]
    assert states["/tables/customer/columns/customer_id/metrics/nullCount"]["state"] == "withheld"
    assert states["/tables/customer/columns/segment/statistics/0"]["state"] == "used"
    assert states["/tables/customer/rowCount"]["state"] == "used"
    counts = ledger["counts"]
    assert sum(counts.values()) == len(states)
    assert f"profile: {len(states)} statistics -- {counts['used']} used" in result.stdout
    assert f"  - {PAIR} (correlation/1): " in result.stdout
    assert f"  - {CHILDREN} (mean/1): " not in result.stdout


def test_the_register_lists_them_and_the_evidence_names_them(inputs: Dict[str, Path]) -> None:
    result = _init(inputs, "bundle")
    assert result.exit_code == 0, result.stderr
    bundle = inputs["root"] / "bundle"
    simulation = yaml.safe_load((bundle / "simulation.flux.yml").read_text(encoding="utf-8"))
    listed = simulation["extensions"][NAMESPACE]["unused"]
    pointers = [item["statistic"]["pointer"] for item in listed]
    unused = [
        e["pointer"] for e in result.artifacts["ledger"]["statistics"] if e["state"] == "unused"
    ]
    assert pointers == unused and PAIR in pointers and CHILDREN not in pointers
    state = inputs["root"] / "state"
    out = inputs["root"] / "out"
    outcome = run(
        bundle, state_dir=state, profile=inputs["profile"], verify=True, ship=True, out_dir=out
    )
    assert outcome.success, outcome.stderr
    run_id = outcome.artifacts["plan"]["run_id"]
    record = json.loads(
        (state / "runs" / run_id / "parameter-register.json").read_text(encoding="utf-8")
    )
    assert record["_schemaURL"] == REGISTER_SCHEMA_URL_1_2
    assert [item["statistic"]["pointer"] for item in record["unused"]] == pointers
    evidence = (state / "runs" / run_id / "EVIDENCE.md").read_text(encoding="utf-8")
    assert "no number of this run rests on" in evidence
    assert f"`{PAIR}`" in evidence


def test_strict_writes_nothing_and_names_each_finding(inputs: Dict[str, Path]) -> None:
    result = _init(inputs, "strict", strict=True)
    assert result.exit_code == 1
    assert not (inputs["root"] / "strict").exists()
    assert "--strict refuses this bundle" in result.stderr
    assert f"  unused {PAIR} (correlation/1): " in result.stderr
    assert f"  unused {CHILDREN} (mean/1): " not in result.stderr
    assert "  gap /extensions/com.agentics.flux~11/emits/orders/fidelity/columns/customer_id" in (
        result.stderr
    )
    found = result.artifacts["strict"]
    assert PAIR in [item["pointer"] for item in found["unused"]]
    assert found["staleAllow"] == []


def test_allow_accepts_what_lies_at_or_below_a_pointer(inputs: Dict[str, Path]) -> None:
    every = _init(inputs, "every", strict=True)
    allow = sorted(
        {item["pointer"] for item in every.artifacts["strict"]["unused"]}
        | {item["pointer"] for item in every.artifacts["strict"]["gaps"]}
    )
    result = _init(inputs, "allowed", strict=True, allow=tuple(allow))
    assert result.exit_code == 0, result.stderr
    prefix = _init(
        inputs,
        "prefix",
        strict=True,
        allow=("/tables", "/extensions/com.agentics.flux~11/emits", "/spec"),
    )
    assert prefix.exit_code == 0, prefix.stderr


def test_an_allow_that_accepts_nothing_is_refused(inputs: Dict[str, Path]) -> None:
    result = _init(
        inputs,
        "stale",
        strict=True,
        allow=(
            "/tables",
            "/extensions/com.agentics.flux~11/emits",
            "/spec",
            "/queries",
        ),
    )
    assert result.exit_code == 1
    assert "  --allow /queries accepts nothing" in result.stderr
    assert result.artifacts["strict"]["staleAllow"] == ["/queries"]
    bad = _init(inputs, "bad", strict=True, allow=("tables",))
    assert bad.exit_code == 2
    assert "JSON Pointer" in bad.stderr


def test_allow_needs_strict_and_strict_needs_a_profile(
    inputs: Dict[str, Path], tmp_path: Path
) -> None:
    alone = _init(inputs, "alone", allow=("/tables",))
    assert alone.exit_code == 2
    assert "pass --strict too" in alone.stderr
    starter = run_init(str(tmp_path / "starter"), strict=True)
    assert starter.exit_code == 2
    assert "no profile to check" in starter.stderr
