# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` reads a noisy statistic as a point estimate with a variance (PROFILE-SPEC 3a).

Pinned here, through ``fluxsim init`` and a run: the noisy counts of a statistic released under
differential privacy become the weights of a draw by the one rule of ``flux_stats.noise``
(negative cells projected away, normalised), the ledger records the variance each statistic
carries, a citation points at the profile's own ``noisyCounts``, and the bundle runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SAMPLE = {"min": 900, "max": 1100}
SCHEMA = "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json"

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: noisy.shop
name: Noisy shop
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
        - { name: region, type: "enum('east','north','west')", required: true }
        - { name: plan, type: "enum('basic','pro')", required: true }
    semantics:
      entities:
        - { name: customer, type: primary, expr: customer_id }
"""


def _noise(rho: str = "0.1", scale: str = "2.5", mechanism: str = "gaussian") -> Dict[str, Any]:
    return {"mechanism": mechanism, "scale": scale, "privacyLoss": {"rho": rho}}


def _noisy(levels: List[str], values: List[str], **noise: str) -> Dict[str, Any]:
    return {
        "definition": "levelHistogram/1",
        "sampleSize": SAMPLE,
        "disclosure": "noisy",
        "levels": levels,
        "noise": _noise(**noise),
        "groups": [{"group": "", "status": "F", "noisyCounts": values}],
    }


def _profile(region: List[str], plan: List[str]) -> Dict[str, Any]:
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": SCHEMA,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"noisy": {}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "0.5"},
            "privacyUnit": {"expose": "customer"},
        },
        "tables": {
            "customer": {
                "columns": {
                    "customer_id": {"domain": "number"},
                    "region": {
                        "domain": "category",
                        "statistics": [_noisy(["east", "north", "west"], region)],
                    },
                    "plan": {
                        "domain": "category",
                        "statistics": [
                            _noisy(["basic", "pro"], plan, mechanism="laplace", scale="3")
                        ],
                    },
                }
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


def _columns(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    columns: Dict[str, Any] = block["emits"]["customer"]["fidelity"]["columns"]
    return columns


def test_noisy_counts_are_the_weights_of_their_point_estimate(tmp_path: Path) -> None:
    # region: T = 1000 and the vector 520, 500, -20 projects to 510, 490, 0: the negative cell
    # takes what it lacks from the other two, evenly. plan: 700.4 + 299.6 = 1000 records, which
    # are whole counts by the largest remainder (700 and 300), so the shares are 0.7 and 0.3.
    _init(tmp_path, _profile(["520", "500", "-20"], ["700.4", "299.6"]))
    columns = _columns(tmp_path)
    assert columns["region"]["levels"] == {"east": "0.51", "north": "0.49", "west": "0"}
    assert columns["plan"]["levels"] == {"basic": "0.7", "pro": "0.3"}


def test_the_ledger_records_the_variance_each_noisy_statistic_carries(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile(["520", "500", "-20"], ["700", "300"]))
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    region = states["/tables/customer/columns/region/statistics/0"]
    plan = states["/tables/customer/columns/plan/statistics/0"]
    assert region["state"] == "used"
    assert region["noise"] == {"mechanism": "gaussian", "scale": "2.5", "variance": "6.25"}
    assert plan["noise"] == {"mechanism": "laplace", "scale": "3", "variance": "18"}
    assert "variance 6.25" in result.stdout and "variance 18" in result.stdout


def test_a_citation_names_the_noisy_counts_of_the_profile(tmp_path: Path) -> None:
    _init(tmp_path, _profile(["520", "500", "-20"], ["700", "300"]))
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    register = yaml.safe_load(text)["extensions"][NAMESPACE]["parameters"]
    pointers = [
        entry["statistic"]["pointer"]
        for entry in register
        if entry.get("source") == "profile" and "region" in entry["statistic"]["pointer"]
    ]
    assert any(pointer.endswith("/groups/0/noisyCounts/0") for pointer in pointers)
    assert not any("levelCounts" in pointer for pointer in pointers)


def test_the_bundle_runs_and_is_verified_against_the_noisy_profile(tmp_path: Path) -> None:
    _init(tmp_path, _profile(["520", "500", "-20"], ["700", "300"]))
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr


def test_the_rows_are_the_records_the_noisy_statistics_estimate(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile(["520", "500", "-20"], ["700", "301"]))
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    emit = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]["customer"]
    assert emit["rows"] == 1001  # the larger noisy total: 700 + 301
    (exposes,) = result.artifacts["exposes"]
    assert any(
        "a noisy statistic estimates" in item["assumption"] for item in exposes["assumptions"]
    )
