# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Noisy clique marginals become columns drawn given their separators (PROFILE-SPEC 5a).

Pinned here, through ``fluxsim init`` and a run: the cliques of a table are arranged into a
junction tree rooted at the clique with the most columns (the first by name among equals), each
column is drawn given the columns it shares with the clique before it, its weights are the noisy
counts made a distribution row by row, cliques with no junction tree are refused naming them, and
the run is scored on the level histograms the cliques answer.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run
from tests.bundles import init_from_texts

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-2-private-example.json").read_text("utf-8"))

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: private.telco
name: Private telco
metadata:
  owner:
    team: telco-team
exposes:
  - exposeId: subscriber
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/subscriber.parquet }
    contract:
      schema:
        - { name: subscriber_id, type: INT, required: true }
        - { name: region, type: "enum('east','north','west')", required: true }
        - { name: plan, type: "enum('basic','pro')", required: true }
        - { name: channel, type: "enum('online','store')", required: true }
    semantics:
      entities:
        - { name: subscriber, type: primary, expr: subscriber_id }
"""


def _init(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    (tmp_path / "telco.fluid.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "telco.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "telco.fluid.yaml"),
        profile=str(tmp_path / "telco.profile.json"),
    )


def _fidelity(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    fidelity: Dict[str, Any] = block["emits"]["subscriber"]["fidelity"]
    return fidelity


def test_two_clique_marginals_become_a_chain_of_conditionals(tmp_path: Path) -> None:
    result = _init(tmp_path, copy.deepcopy(EXAMPLE))
    assert result.exit_code == 0, result.stderr
    columns = _fidelity(tmp_path)["columns"]
    # Both cliques have two columns, so the root is the first by name: (channel, plan). It
    # introduces plan and channel in declared order, and the other clique adds region given plan.
    assert "given" not in columns["plan"]
    assert float(columns["plan"]["levels"]["basic"]) == pytest.approx(9503.5 / 14000, abs=1e-9)
    assert columns["channel"]["given"] == "plan"
    assert columns["region"]["given"] == "plan"
    # P(channel | plan = basic) is the row (6100.25, 3403.25) of the second clique, normalised.
    row = columns["channel"]["levelWeightsGiven"]["basic"]
    assert float(row["online"]) == pytest.approx(6100.25 / 9503.5, abs=1e-9)
    row = columns["region"]["levelWeightsGiven"]["pro"]
    assert float(row["east"]) == pytest.approx(1987 / 4496.5, abs=1e-9)


def test_the_ledger_marks_the_cliques_used_with_their_noise(tmp_path: Path) -> None:
    result = _init(tmp_path, copy.deepcopy(EXAMPLE))
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    for index in (0, 1):
        clique = states[f"/tables/subscriber/cliques/{index}"]
        assert clique["state"] == "used"
        assert clique["definition"] == "cliqueMarginal/1"
        assert clique["noise"]["mechanism"] == "gaussian"
        assert clique["noise"]["variance"] == "5.0176"


def test_the_run_is_scored_on_the_histograms_the_cliques_answer(tmp_path: Path) -> None:
    _init(tmp_path, copy.deepcopy(EXAMPLE))
    properties = _fidelity(tmp_path)["properties"]
    pairs = {
        (p["column"], p.get("groupBy")) for p in properties if p["property"] == "levelHistogram"
    }
    assert {
        ("region", None),
        ("plan", None),
        ("channel", None),
        ("region", "plan"),
        ("plan", "region"),
        ("plan", "channel"),
        ("channel", "plan"),
    } <= pairs


def test_the_bundle_runs_and_verifies(tmp_path: Path) -> None:
    _init(tmp_path, copy.deepcopy(EXAMPLE))
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "telco.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr + outcome.stdout


def test_the_order_the_cliques_are_written_in_changes_nothing(tmp_path: Path) -> None:
    flipped = copy.deepcopy(EXAMPLE)
    flipped["tables"]["subscriber"]["cliques"].reverse()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _init(tmp_path / "a", copy.deepcopy(EXAMPLE))
    _init(tmp_path / "b", flipped)
    one = _fidelity(tmp_path / "a")["columns"]
    other = _fidelity(tmp_path / "b")["columns"]
    assert one == other


def test_cliques_with_no_junction_tree_are_refused_naming_them(tmp_path: Path) -> None:
    triangle = copy.deepcopy(EXAMPLE)
    table = triangle["tables"]["subscriber"]
    # a third clique over (channel, region) closes a cycle: region - plan - channel - region.
    # Its marginals agree with the other two, so the profile reads, and the cliques have no tree.
    table["cliques"].append(
        {
            "definition": "cliqueMarginal/1",
            "columns": ["channel", "region"],
            "levels": [["online", "store"], ["east", "north", "west"]],
            "noisyCounts": ["3500.25", "2900", "1800", "2499.25", "1600.5", "1700"],
            "noise": copy.deepcopy(table["cliques"][0]["noise"]),
        }
    )
    triangle["privacy"]["privacyLoss"]["rho"] = "1"
    result = _init(tmp_path, triangle)
    assert result.exit_code != 0
    assert "junction tree" in result.stderr or "junction tree" in result.stdout
    assert "/tables/subscriber/cliques/" in (result.stderr + result.stdout)


def test_levels_that_are_not_the_contracts_are_refused(tmp_path: Path) -> None:
    other = copy.deepcopy(EXAMPLE)
    clique = other["tables"]["subscriber"]["cliques"][1]
    clique["levels"][1] = ["online", "phone"]
    result = _init(tmp_path, other)
    assert result.exit_code != 0
    assert "not the levels the contract declares" in result.stderr


def test_a_column_the_contract_does_not_declare_is_refused(tmp_path: Path) -> None:
    other = copy.deepcopy(EXAMPLE)
    other["tables"]["subscriber"]["columns"]["tier"] = {"domain": "category"}
    other["tables"]["subscriber"]["cliques"][1]["columns"][1] = "tier"
    result = _init(tmp_path, other)
    assert result.exit_code != 0
    assert "is not a column of the contract's expose" in result.stderr


TRIPLE_CONTRACT = CONTRACT.replace(
    "        - { name: channel, type: \"enum('online','store')\", required: true }\n",
    "        - { name: channel, type: \"enum('online','store')\", required: true }\n"
    "        - { name: tier, type: \"enum('gold','silver')\", required: true }\n",
)


def _triple() -> Dict[str, Any]:
    """A clique of three columns and one of two that agrees with it on the channel."""
    profile = copy.deepcopy(EXAMPLE)
    table = profile["tables"]["subscriber"]
    del table["columns"]["region"]["statistics"]
    table["columns"]["tier"] = {"domain": "category"}
    noise = {"mechanism": "gaussian", "scale": "2.24", "privacyLoss": {"rho": "0.1"}}
    table["cliques"] = [
        {
            "definition": "cliqueMarginal/1",
            "columns": ["region", "plan", "channel"],
            "levels": [["east", "north", "west"], ["basic", "pro"], ["online", "store"]],
            # (region, plan, channel) row-major; one cell is negative, as noise can make it.
            "noisyCounts": [
                "2000", "700", "600", "400",
                "1500", "900", "500", "300",
                "1200", "800", "300", "-5",
            ],  # fmt: skip
            "noise": noise,
        },
        {
            "definition": "cliqueMarginal/1",
            "columns": ["channel", "tier"],
            "levels": [["online", "store"], ["gold", "silver"]],
            "noisyCounts": ["4000", "2100", "1000", "2095"],
            "noise": noise,
        },
    ]
    profile["privacy"]["privacyLoss"]["rho"] = "0.3"
    return profile


def _init_triple(tmp_path: Path) -> Any:
    return init_from_texts(
        tmp_path, TRIPLE_CONTRACT, _triple(), ("telco.fluid.yaml", "telco.profile.json")
    )


def test_a_clique_of_three_columns_draws_the_last_given_the_other_two(tmp_path: Path) -> None:
    result = _init_triple(tmp_path)
    assert result.exit_code == 0, result.stderr
    columns = _fidelity(tmp_path)["columns"]
    assert "given" not in columns["region"]
    assert columns["plan"]["given"] == "region"
    assert columns["channel"]["given"] == ["region", "plan"]
    assert columns["tier"]["given"] == "channel"
    # P(channel | region = west, plan = pro): the negative cell is projected away, so (300, -5)
    # takes the mass it lacks from the cell above it and the row is (1, 0).
    row = columns["channel"]["levelWeightsGiven"]["west"]["pro"]
    assert row == {"online": "1", "store": "0"}
    row = columns["channel"]["levelWeightsGiven"]["east"]["basic"]
    assert float(row["online"]) == pytest.approx(2000 / 2700, abs=1e-9)


def test_the_run_of_a_three_column_clique_verifies_and_is_the_same_at_any_jobs(
    tmp_path: Path,
) -> None:
    result = _init_triple(tmp_path)
    assert result.exit_code == 0, result.stderr
    outcomes = {}
    for jobs in (1, 3):
        outcome = run(
            tmp_path / "bundle",
            state_dir=tmp_path / f"state{jobs}",
            profile=tmp_path / "telco.profile.json",
            verify=True,
            jobs=jobs,
        )
        assert outcome.success, outcome.stderr + outcome.stdout
        outcomes[jobs] = outcome
    assert (
        outcomes[1].artifacts["apply"]["run_digest"] == outcomes[3].artifacts["apply"]["run_digest"]
    )
    emitted = {
        jobs: [p.read_bytes() for p in sorted((tmp_path / f"state{jobs}").glob("runs/*/emits/*"))]
        for jobs in (1, 3)
    }
    assert emitted[1] and emitted[1] == emitted[3]
