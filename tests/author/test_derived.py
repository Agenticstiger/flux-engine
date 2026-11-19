# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Columns a contract ``derive`` sets (ADR 0014 section 6).

Pinned here:

* the exact bounds of a rule over the ranges of the columns it reads, for every
  form the interval follows, and none for a form it does not;
* through ``fluxsim init`` and a run: a determined column declares no draw and
  never its own profiled range, its range is the rule's bounds, its published
  statistics are checked through the rule, and ``verify`` passes where 0.7.0
  refused the run (``FLX-5005``);
* bounds pass down a chain of rules, and a guarded ``derive`` leaves its column
  drawn.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
import yaml

from flux_engine.author.derived import bounds, contract_derives
from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

NAMESPACE = "com.agentics.flux/1"
SAMPLE = {"min": 950, "max": 1050}
F = Fraction
RANGES: Dict[str, Tuple[Fraction, Fraction]] = {"net": (F(10), F(100)), "qty": (F(-2), F(3))}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ROUND(net * 0.081, 2)", (F("0.81"), F("8.1"))),
        ("net + qty", (F(8), F(103))),
        ("net - qty", (F(7), F(102))),
        ("qty * qty", (F(-6), F(9))),
        ("net / 2", (F(5), F(50))),
        ("-net", (F(-100), F(-10))),
        ("COALESCE(net, qty)", (F(-2), F(100))),
        ("CASE WHEN qty > 0 THEN net ELSE 0 END", (F(0), F(100))),
        ("ROUND(net / 3, 0)", (F(3), F(33))),
    ],
)
def test_the_bounds_of_a_rule(text: str, expected: Tuple[Fraction, Fraction]) -> None:
    assert bounds(text, RANGES) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    ["net / qty", "missing + 1", "customer.limit * 2", "net +", "ABS(qty)"],
)
def test_a_form_the_interval_does_not_follow_has_no_bounds(text: str) -> None:
    assert bounds(text, RANGES) is None


@pytest.mark.unit
def test_only_an_unguarded_derive_determines_its_column() -> None:
    expose = {
        "contract": {
            "invariants": [
                {"id": "a", "derive": "vat", "expr": "net * 2"},
                {"id": "b", "derive": "tip", "expr": "net", "when": "net > 5"},
                {"id": "c", "check": "net > 0"},
            ]
        }
    }
    assert contract_derives(expose) == {"vat": (0, "net * 2")}


CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: derive.shop
name: Derive shop
metadata:
  owner:
    team: shop-team
exposes:
  - exposeId: orders
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/orders.parquet }
    contract:
      schema:
        - { name: order_id, type: INT, required: true }
        - { name: net, type: "DECIMAL(10,2)", required: true }
        - { name: vat, type: "DECIMAL(10,2)", required: true }
        - { name: gross, type: "DECIMAL(10,2)", required: true }
      invariants:
        - id: vat_of_net
          derive: vat
          expr: "ROUND(net * 0.081, 2)"
        - id: gross_of_net
          derive: gross
          expr: "net + vat"
    semantics:
      entities:
        - { name: order, type: primary, expr: order_id }
"""


def _metric(definition: str, value: Any) -> Dict[str, Any]:
    return {
        "definition": definition,
        "status": "F",
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "value": value,
    }


def _mean(mean: str, k: int = 5000) -> Dict[str, Any]:
    return {
        "definition": "mean/1",
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "groups": [
            {"group": "", "k": k, "Sx": str(int(Fraction(mean) * k * 2**16)), "status": "F"}
        ],
    }


def _histogram(bins: Any, counts: Any) -> Dict[str, Any]:
    return {
        "definition": "marginalHistogram/1",
        "bins": bins,
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "groups": [{"group": "", "k": sum(counts), "binCounts": counts, "status": "F"}],
    }


def _profile(vat_histogram: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    vat: Dict[str, Any] = {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", 5000),
            "min": _metric("min/1", "3.00"),
            "max": _metric("max/1", "5.00"),
        },
        "statistics": [_mean("4.05")],
    }
    if vat_histogram is not None:
        vat["statistics"].append(vat_histogram)
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
            "orders": {
                "rowCount": 5000,
                "columns": {
                    "order_id": {
                        "domain": "number",
                        "metrics": {
                            "count": _metric("count/1", 5000),
                            "min": _metric("min/1", "1"),
                            "max": _metric("max/1", "5000"),
                        },
                    },
                    "net": {
                        "domain": "number",
                        "metrics": {
                            "count": _metric("count/1", 5000),
                            "min": _metric("min/1", "10.00"),
                            "max": _metric("max/1", "100.00"),
                        },
                        "statistics": [_histogram(["10.00", "100.00"], [5000]), _mean("50")],
                    },
                    "vat": vat,
                    "gross": {"domain": "number", "metrics": {"count": _metric("count/1", 5000)}},
                },
            }
        },
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


def _fidelity(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]["orders"]["fidelity"]


@pytest.mark.integration
def test_a_derived_column_is_checked_through_its_rule(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile())
    fidelity = _fidelity(tmp_path)
    vat = fidelity["columns"]["vat"]
    assert vat == {"range": {"min": "0.81", "max": "8.10"}}  # the rule's bounds, no draw
    assert {"property": "mean", "column": "vat"}.items() <= next(
        prop for prop in fidelity["properties"] if prop["column"] == "vat"
    ).items()
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    assert states["/tables/orders/columns/vat/metrics/min"]["note"].startswith("checked: within")
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr
    assert "FLX-5005" not in outcome.stdout


@pytest.mark.integration
def test_a_published_end_beyond_the_rule_s_bounds_widens_the_range(tmp_path: Path) -> None:
    # The rule's bounds come from the span the bundle has for net; the profile's own vat
    # minimum lies below them, and the check reads it against the range (FLX-5005).
    profile = _profile()
    profile["tables"]["orders"]["columns"]["vat"]["metrics"]["min"] = _metric("min/1", "0.41")
    result = _init(tmp_path, profile)
    assert _fidelity(tmp_path)["columns"]["vat"] == {"range": {"min": "0.41", "max": "8.10"}}
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    assert "so the range holds it too" in states["/tables/orders/columns/vat/metrics/min"]["note"]
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr


@pytest.mark.integration
def test_bounds_pass_down_a_chain_of_rules(tmp_path: Path) -> None:
    _init(tmp_path, _profile())
    gross = _fidelity(tmp_path)["columns"]["gross"]
    assert gross == {"range": {"min": "10.81", "max": "108.10"}}


@pytest.mark.integration
def test_a_histogram_that_holds_the_bounds_gives_the_range_and_a_property(tmp_path: Path) -> None:
    profile = _profile(_histogram(["0.00", "4.00", "10.00"], [2500, 2500]))
    _init(tmp_path, profile)
    fidelity = _fidelity(tmp_path)
    assert fidelity["columns"]["vat"]["range"] == {"min": "0.00", "max": "10.00"}
    assert "bins" not in fidelity["columns"]["vat"]
    kinds = {(prop["property"], prop["column"]) for prop in fidelity["properties"]}
    assert ("mean", "vat") in kinds
    assert ("marginalHistogram", "vat") not in kinds  # its bins would be a draw
