# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column a ``sharedColumns`` copies is written by ``fluxsim init`` with no draw of its own.

ADR 0022 section 4: on kernel 0.2.0 a column that a relationship's ``sharedColumns`` lists is
copied from the row the relationship reaches, on every row, so the bundle declares no draw for it, as
it declares none for a column a contract ``derive`` without ``when`` sets (ADR 0014 section 6). The
choice is recorded for the column, and the bundle that is written runs and verifies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.author.derived import contract_copies
from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

from .test_derived import NAMESPACE, _histogram, _mean, _metric

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: shared.shop
name: Shared shop
metadata:
  owner:
    team: shop-team
exposes:
  - exposeId: customers
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/customers.parquet }
    contract:
      schema:
        - { name: customer_id, type: INT, required: true }
        - { name: score, type: INT, required: true }
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
        - { name: score, type: INT, required: true }
    semantics:
      entities:
        - { name: order, type: primary, expr: order_id }
      relationships:
        - name: customer
          to: customers
          fromColumns: [customer_id]
          toColumns: [customer_id]
          cardinality: many_to_one
          sharedColumns: [score, customer_id]
"""


ATTRIBUTE_CONTRACT = CONTRACT.replace(
    "          sharedColumns: [score, customer_id]\n", ""
).replace(
    "{ name: score, type: INT, required: true }",
    "{ name: score, type: INT, required: true, sharedAttribute: score }",
)


def _column(rows: int, low: str, high: str, mean: str) -> Dict[str, Any]:
    return {
        "domain": "number",
        "metrics": {
            "count": _metric("count/1", rows),
            "min": _metric("min/1", low),
            "max": _metric("max/1", high),
        },
        "statistics": [_histogram([low, high], [rows]), _mean(mean, rows)],
    }


def _profile() -> Dict[str, Any]:
    def key(rows: int) -> Dict[str, Any]:
        return {
            "domain": "number",
            "metrics": {
                "count": _metric("count/1", rows),
                "min": _metric("min/1", "1"),
                "max": _metric("max/1", str(rows)),
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
            "customers": {
                "rowCount": 400,
                "columns": {"customer_id": key(400), "score": _column(400, "10", "90", "50")},
            },
            "orders": {
                "rowCount": 5000,
                "columns": {
                    "order_id": key(5000),
                    "customer_id": {
                        **key(400),
                        "statistics": [_histogram(["1", "400"], [5000]), _mean("200", 5000)],
                    },
                    "score": _column(5000, "10", "90", "50"),
                },
            },
        },
    }


def _init(tmp_path: Path, contract: str = CONTRACT) -> Any:
    (tmp_path / "shop.fluid.yaml").write_text(contract, encoding="utf-8")
    (tmp_path / "shop.profile.json").write_text(json.dumps(_profile()), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "shop.fluid.yaml"),
        profile=str(tmp_path / "shop.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _emits(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    emits: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]
    return emits


@pytest.mark.unit
def test_the_columns_a_shared_columns_copies_leave_out_what_a_copy_cannot_set() -> None:
    contract = yaml.safe_load(CONTRACT)
    keyed = {"order_id", "customer_id"}
    found = contract_copies(contract, "orders", 1, keyed, set())
    assert list(found) == ["score"]
    tokens, how, reason = found["score"]
    assert tokens == ("exposes", "1", "semantics", "relationships", "0", "sharedColumns", "0")
    assert how == "copied from the row customer reaches (sharedColumns), not drawn"
    assert "copies this column" in reason
    # A column another rule sets is left to the plan, which refuses two sources.
    assert contract_copies(contract, "orders", 1, keyed, {"score"}) == {}
    assert contract_copies(contract, "customers", 0, {"customer_id"}, set()) == {}


@pytest.mark.integration
def test_init_writes_no_draw_for_a_column_a_shared_columns_copies(tmp_path: Path) -> None:
    result = _init(tmp_path)
    orders = _emits(tmp_path)["orders"]["fidelity"]["columns"]
    customers = _emits(tmp_path)["customers"]["fidelity"]["columns"]
    # The parent draws its own; the copy declares nothing: no range, no statistic, no nullFraction.
    assert "range" in customers["score"] or "bins" in customers["score"]
    assert not orders.get("score")
    assert not any(
        prop["column"] == "score"
        for prop in _emits(tmp_path)["orders"]["fidelity"].get("properties", ())
    )
    exposes = {entry["exposeId"]: entry for entry in result.artifacts["exposes"]}
    how = {column["name"]: column["how"] for column in exposes["orders"]["columns"]}
    assert how["score"] == "copied from the row customer reaches (sharedColumns), not drawn"
    assert "customer_id" in orders  # the foreign key is the relationship's, not a copy


@pytest.mark.integration
def test_the_bundle_init_writes_runs_and_verifies_with_the_copy_scored(tmp_path: Path) -> None:
    _init(tmp_path)
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr
    rules = outcome.artifacts["verify"]["rules"]
    lines = (rules.get("emits") or {}).get("orders", ())
    scored = [entry["rule"] for entry in lines if entry["enforced"]]
    assert scored == ["customer.score"], scored
    # The foreign key the relationship lists is a copy the run does not make: reported only.
    reported = [entry["rule"] for entry in lines if not entry["enforced"]]
    assert reported == ["customer.customer_id"], reported


@pytest.mark.unit
def test_a_shared_attribute_member_that_is_not_the_owner_is_a_copy() -> None:
    contract = yaml.safe_load(ATTRIBUTE_CONTRACT)
    keyed = {"order_id", "customer_id"}
    found = contract_copies(contract, "orders", 1, keyed, set())
    assert list(found) == ["score"]
    tokens, how, reason = found["score"]
    assert tokens == ("exposes", "1", "contract", "schema", "2", "sharedAttribute")
    assert (
        how == "copied from the owner of the shared attribute 'score' (sharedAttribute), not drawn"
    )
    assert "generated once, on its owner" in reason
    # The owner is no copy.
    assert contract_copies(contract, "customers", 0, {"customer_id"}, set()) == {}


@pytest.mark.integration
def test_init_writes_no_draw_for_a_shared_attribute_member_that_copies(tmp_path: Path) -> None:
    result = _init(tmp_path, ATTRIBUTE_CONTRACT)
    emits = _emits(tmp_path)
    assert not emits["orders"]["fidelity"]["columns"].get("score")
    assert emits["customers"]["fidelity"]["columns"]["score"]
    exposes = {entry["exposeId"]: entry for entry in result.artifacts["exposes"]}
    how = {column["name"]: column["how"] for column in exposes["orders"]["columns"]}
    assert how["score"].startswith("copied from the owner of the shared attribute 'score'")
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr
    scored = [entry["rule"] for entry in outcome.artifacts["verify"]["rules"]["emits"]["orders"]]
    assert scored == ["score"], scored


def _namespace(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    namespace: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return namespace


PLAIN_CONTRACT = CONTRACT.replace("          sharedColumns: [score, customer_id]\n", "")


@pytest.mark.integration
@pytest.mark.parametrize("contract", [CONTRACT, ATTRIBUTE_CONTRACT], ids=["columns", "attribute"])
def test_init_writes_copies_true_for_a_contract_that_declares_something_to_copy(
    tmp_path: Path, contract: str
) -> None:
    """ADR 0022 section 4: a copy generates only where the Simulation declares ``copies: true``."""
    _init(tmp_path, contract)
    assert _namespace(tmp_path)["copies"] is True


@pytest.mark.integration
def test_init_writes_no_copies_key_and_the_same_files_for_a_contract_with_nothing_to_copy(
    tmp_path: Path,
) -> None:
    _init(tmp_path, PLAIN_CONTRACT)
    assert "copies" not in _namespace(tmp_path)
    # The columns a copy would have set are drawn, as they were before copies existed.
    assert _emits(tmp_path)["orders"]["fidelity"]["columns"]["score"]
