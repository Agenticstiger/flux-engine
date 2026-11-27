# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` never writes a counted layout beside a copy for one relationship (ADR 0022 section 4).

A ``sharedColumns`` entry of relationship ``R`` that lists the foreign key of another relationship
``S`` copies it into ``S``'s pick, and a copy sets the pick alone: the plan refuses a ``counts`` entry
over ``S``, and a draw its column declares (``FLX-3403``). The fixture is a loan that reaches its
branch directly and through its member, who holds the same branch, and copies the branch from the
member. The profile also publishes the loan's children per branch, which the counted layout
would lay out, were the branch's pick not the copy's.

Pinned here: the bundle init writes plans and runs; the copy stays declared; the relationship has
no counts entry and its foreign key no draw; its statistics are unused, with a reason that names no
value; the loans hold their member's branch; and a contract without the copy keeps its counts entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.stages.plan import plan
from flux_engine.stages.run import run
from tests.bundles import init_from_texts

from .test_children import _histogram, _mean, _metric

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
FILES = ("library.fluid.yaml", "library.profile.json")

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: library.lending
name: Lending
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
        - { name: branch_id, type: INT, required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
      relationships:
        - name: home_branch
          to: branch
          fromColumns: [branch_id]
          toColumns: [branch_id]
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
        - { name: branch_id, type: INT, required: true }
    semantics:
      entities:
        - { name: loan, type: primary, expr: loan_id }
      relationships:
        - name: borrower
          to: member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
          sharedColumns: [branch_id]
        - name: branch
          to: branch
          fromColumns: [branch_id]
          toColumns: [branch_id]
          cardinality: many_to_one
"""
WITHOUT_COPY = CONTRACT.replace("          sharedColumns: [branch_id]\n", "")

BRANCHES, MEMBERS, LOANS = 50, 400, 3000
BINS = ["0", "10", "40", "80", "200"]
PARENTS = [10, 15, 15, 10]


def _key(rows: int) -> Dict[str, Any]:
    metrics = {"count": _metric("count/1", rows), "min": _metric("min/1", "1")}
    metrics["max"] = _metric("max/1", str(rows))
    return {"domain": "number", "metrics": metrics}


def _profile() -> Dict[str, Any]:
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
            "branch": {"rowCount": BRANCHES, "columns": {"branch_id": _key(BRANCHES)}},
            "member": {
                "rowCount": MEMBERS,
                "columns": {"member_id": _key(MEMBERS), "branch_id": _key(BRANCHES)},
            },
            "loan": {
                "rowCount": LOANS,
                "columns": {
                    "loan_id": _key(LOANS),
                    "member_id": _key(MEMBERS),
                    "branch_id": _key(BRANCHES),
                },
            },
        },
        "relationships": [
            {
                "parent": {"table": "branch", "columns": ["branch_id"]},
                "child": {"table": "loan", "columns": ["branch_id"]},
                "statistics": [
                    _histogram(BINS, PARENTS, BRANCHES),
                    _mean(LOANS, BRANCHES),
                ],
            }
        ],
    }


def _init(tmp_path: Path, contract: str = CONTRACT) -> Any:
    result = init_from_texts(tmp_path, contract, _profile(), FILES)
    assert result.exit_code == 0, result.stderr
    return result


def _block(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block


def _states(result: Any) -> Dict[str, Dict[str, Any]]:
    return {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}


def test_a_relationship_a_copy_sets_is_not_laid_out_by_counts(tmp_path: Path) -> None:
    _init(tmp_path)
    block = _block(tmp_path)
    assert block["copies"] is True
    assert "counts" not in block
    # The copy sets the key, so the key declares no draw of its own.
    assert "branch_id" not in block["emits"]["loan"]["fidelity"]["columns"]


def test_its_statistics_are_unused_with_a_reason_that_names_no_value(tmp_path: Path) -> None:
    states = _states(_init(tmp_path))
    for pointer in ("/relationships/0/statistics/0", "/relationships/0/statistics/1"):
        assert states[pointer]["state"] == "unused"
        note = states[pointer]["note"]
        assert "sets the pick of this relationship" in note
        assert "relationship borrower" in note
        assert not any(character.isdigit() for character in note)


def test_the_report_says_the_copy_sets_the_key(tmp_path: Path) -> None:
    result = _init(tmp_path)
    assert "a foreign key, set by the copy through the relationship borrower" in result.stdout


def _rows(tmp_path: Path, emit: str) -> List[List[Any]]:
    outcome = run(
        tmp_path / "bundle", state_dir=tmp_path / "state", profile=tmp_path / FILES[1], verify=False
    )
    assert outcome.success, outcome.stderr
    run_id = outcome.artifacts["plan"]["run_id"]
    text = (tmp_path / "state" / "runs" / run_id / "emits" / f"{emit}.ndjson").read_text("utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_the_bundle_runs_and_a_loan_holds_its_member_s_branch(tmp_path: Path) -> None:
    _init(tmp_path)
    members = {row[0]: row[1] for row in _rows(tmp_path, "member")}
    loans = _rows(tmp_path, "loan")
    assert len(loans) == LOANS
    assert all(members[row[1]] == row[2] for row in loans)


def test_a_contract_without_the_copy_keeps_its_counts_entry(tmp_path: Path) -> None:
    result = _init(tmp_path, WITHOUT_COPY)
    block = _block(tmp_path)
    assert "copies" not in block
    assert [(entry["emit"], entry["relationship"]) for entry in block["counts"]] == [
        ("loan", "branch")
    ]
    assert _states(result)["/relationships/0/statistics/0"]["state"] == "used"


def test_only_a_copy_into_another_relationship_s_foreign_key_is_a_pick_copy() -> None:
    from flux_engine.author.derived import contract_pick_copies

    assert contract_pick_copies(yaml.safe_load(CONTRACT)) == {
        ("loan", "branch"): ("borrower", "branch_id")
    }
    assert contract_pick_copies(yaml.safe_load(WITHOUT_COPY)) == {}
    # A column that is the foreign key of the relationship it is copied through, or no foreign key.
    own = CONTRACT.replace("sharedColumns: [branch_id]", "sharedColumns: [member_id, loan_id]")
    assert contract_pick_copies(yaml.safe_load(own)) == {}
    # A target that does not hold the column has nothing to copy.
    bare = CONTRACT.replace(
        "        - { name: member_id, type: INT, required: true }\n"
        "        - { name: branch_id, type: INT, required: true }\n"
        "    semantics:\n      entities:\n        - { name: member,",
        "        - { name: member_id, type: INT, required: true }\n"
        "    semantics:\n      entities:\n        - { name: member,",
    ).replace(
        "      relationships:\n        - name: home_branch\n          to: branch\n"
        "          fromColumns: [branch_id]\n          toColumns: [branch_id]\n"
        "          cardinality: many_to_one\n",
        "",
    )
    assert bare != CONTRACT
    assert contract_pick_copies(yaml.safe_load(bare)) == {}


def test_the_plan_reads_the_same_relationship_as_the_pick_a_copy_sets() -> None:
    from flux_engine.core.canonical import nfc
    from flux_engine.seam.keys import compile_keys, copied_pick

    keys = compile_keys(yaml.safe_load(CONTRACT))
    assert keys is not None
    found = copied_pick(keys, nfc("loan"), "borrower", "branch_id")
    assert found is not None
    assert (found[0].name, found[1].name) == ("branch", "borrower")
    assert copied_pick(keys, nfc("loan"), "borrower", "member_id") is None
    assert copied_pick(keys, nfc("loan"), "branch", "branch_id") is None


def test_a_simulation_that_copies_nothing_has_no_pick_a_copy_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flux_engine.author.bundle import _copied_picks
    from flux_engine.core import kernels

    contract = yaml.safe_load(CONTRACT)
    assert _copied_picks(contract) == {("loan", "branch"): ("borrower", "branch_id")}
    # Kernel 0.1.0 copies nothing, so a plan of it reads no pick as a copy's.
    monkeypatch.setattr(kernels, "DEFAULT_KERNEL", kernels.KERNEL_V1)
    assert _copied_picks(contract) == {}


def _loan(contract: Dict[str, Any]) -> Dict[str, Any]:
    exposes: Dict[str, Dict[str, Any]] = {item["exposeId"]: item for item in contract["exposes"]}
    return exposes["loan"]


def test_a_pick_is_found_as_the_plan_finds_it() -> None:
    from flux_engine.author.derived import contract_pick_copies
    from flux_engine.seam.keys import compile_keys, copied_pick

    # A second relationship over the same foreign key, and a second copy into it: the first
    # declared of each wins, as the plan takes the first.
    contract = yaml.safe_load(CONTRACT)
    loan = _loan(contract)
    loan["contract"]["schema"].append({"name": "guarantor_id", "type": "INT", "required": True})
    loan["semantics"]["relationships"].extend(
        [
            {
                "name": "guarantor",
                "to": "member",
                "fromColumns": ["guarantor_id"],
                "toColumns": ["member_id"],
                "cardinality": "many_to_one",
                "sharedColumns": ["branch_id"],
            },
            {
                "name": "branch_again",
                "to": "branch",
                "fromColumns": ["branch_id"],
                "toColumns": ["branch_id"],
                "cardinality": "many_to_one",
            },
        ]
    )
    keys = compile_keys(contract)
    assert keys is not None
    found = copied_pick(keys, "loan", "guarantor", "branch_id")
    assert found is not None and found[0].name == "branch"
    assert copied_pick(keys, "loan", "no_such_relationship", "branch_id") is None
    assert contract_pick_copies(contract) == {("loan", "branch"): ("borrower", "branch_id")}


def test_a_column_the_relationship_copied_through_holds_in_a_key_is_not_a_pick() -> None:
    from flux_engine.seam.keys import compile_keys, copied_pick

    contract = yaml.safe_load(CONTRACT)
    borrower = _loan(contract)["semantics"]["relationships"][0]
    borrower["fromColumns"] = ["member_id", "branch_id"]
    borrower["toColumns"] = ["member_id", "branch_id"]
    keys = compile_keys(contract)
    assert keys is not None
    assert copied_pick(keys, "loan", "borrower", "branch_id") is None


def test_a_copy_through_a_relationship_to_the_same_expose_is_no_pick() -> None:
    from flux_engine.author.derived import contract_pick_copies

    contract = yaml.safe_load(CONTRACT)
    loan = _loan(contract)
    loan["contract"]["schema"].append(
        {"name": "previous_loan_id", "type": "INT", "required": False}
    )
    loan["semantics"]["relationships"][0]["sharedColumns"] = []
    loan["semantics"]["relationships"].append(
        {
            "name": "previous",
            "to": "loan",
            "fromColumns": ["previous_loan_id"],
            "toColumns": ["loan_id"],
            "cardinality": "many_to_one",
            "sharedColumns": ["branch_id"],
        }
    )
    assert contract_pick_copies(contract) == {}


def test_two_hold_backs_are_one_that_asks_either() -> None:
    from flux_engine.author.bundle import _either

    def first(emit: str, name: str) -> Any:
        return "first" if (emit, name) == ("a", "x") else None

    def second(emit: str, name: str) -> Any:
        return "second" if (emit, name) in (("a", "x"), ("b", "y")) else None

    both = _either(first, second)
    assert both is not None
    assert both("a", "x") == "first"
    assert both("b", "y") == "second"
    assert both("c", "z") is None
    assert _either(first, None) is first
    assert _either(None, second) is second
    assert _either(None, None) is None


def _grouped_by_the_borrower() -> Any:
    """The contract and profile with a statistic of the loan's branch grouped by the borrower's tier.

    A grouping by a column of a linked row is how a column comes to be drawn given another
    (``groupByLink``); here it is offered to the foreign key a copy sets.
    """
    from flux_stats.profile import SCHEMA_URL_1_4_0

    contract = CONTRACT.replace(
        "        - { name: branch_id, type: INT, required: true }\n    semantics:\n"
        "      entities:\n        - { name: member, type: primary, expr: member_id }",
        "        - { name: branch_id, type: INT, required: true }\n"
        "        - { name: tier, type: STRING, required: true }\n    semantics:\n"
        "      entities:\n        - { name: member, type: primary, expr: member_id }",
    )
    assert contract != CONTRACT
    profile = _profile()
    profile["_schemaURL"] = SCHEMA_URL_1_4_0
    sample = {"min": 950, "max": 1050}
    profile["tables"]["member"]["columns"]["tier"] = {
        "domain": "category",
        "statistics": [
            {
                "definition": "levelHistogram/1",
                "levels": ["gold", "plain"],
                "sampleSize": sample,
                "disclosure": "acro-default",
                "groups": [{"group": "", "k": MEMBERS, "levelCounts": [100, 300], "status": "F"}],
            }
        ],
    }
    profile["tables"]["loan"]["columns"]["branch_id"]["statistics"] = [
        {
            "definition": "marginalHistogram/1",
            "bins": ["1", "10", "30", "51"],
            "sampleSize": sample,
            "disclosure": "acro-default",
            "groupByLink": {
                "parent": {"table": "member", "columns": ["member_id"]},
                "columns": ["member_id"],
                "column": "tier",
            },
            "groups": [
                {"group": "gold", "k": 1000, "binCounts": [500, 300, 200], "status": "F"},
                {"group": "plain", "k": 2000, "binCounts": [100, 900, 1000], "status": "F"},
            ],
        }
    ]
    return contract, profile


def test_no_draw_given_another_column_reaches_a_foreign_key_a_copy_sets(tmp_path: Path) -> None:
    # `declare_counted` takes the copy's key off the draws it knows, and a draw given another
    # column is not among them. None needs to be: a foreign key is never drawn given another, so
    # a statistic that would make it so is passed over before the copy's key is read.
    contract, profile = _grouped_by_the_borrower()
    result = init_from_texts(tmp_path, contract, profile, FILES)
    assert result.exit_code == 0, result.stderr
    state = _states(result)["/tables/loan/columns/branch_id/statistics/0"]
    assert state["state"] == "unused"
    assert "a foreign key, which is never drawn given another" in state["note"]
    block = _block(tmp_path)
    assert "counts" not in block
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    assert "binWeightsGiven" not in text
    assert "levelWeightsGiven" not in text
    plan_outcome = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / FILES[1],
        kernel="0.2.0",
        stage=False,
    )
    assert plan_outcome.success, plan_outcome.stderr
