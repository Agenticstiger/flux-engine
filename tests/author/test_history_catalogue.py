# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` tracks a foreign key into a public reference table as levels (ADR 0017 section 13).

A history's column that points at a table the profile releases whole (``references``) is carried
when the emit declares no levels for it. ``init`` now declares the table's keys as the column's
levels, at equal weights, and writes its ``initial`` and ``transitions`` from the history's
statistic like any categorical column. Pinned here: the entry and the declaration written, the
ledger, a key that is not published, a transition to a value that is no key, and the bundle of a
contract without the shape.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.author.columns import ColumnPlan
from flux_engine.author.history import write_history

from ._written import bundle_digests, resolved_emits
from .test_history_init import (
    CONTRACT,
    HISTORY,
    _entries,
    _init,
    _levels,
    _metrics,
    _namespace,
    _profile,
    _source,
    _states,
    _transitions,
)

pytestmark = pytest.mark.integration

KEYS = ["101", "102", "103"]
PRODUCT = """\
  - exposeId: product
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/product.parquet }
    contract:
      schema:
        - { name: product_id, type: INT, required: true }
        - { name: label, type: STRING, required: true }
    semantics:
      entities:
        - { name: product, type: primary, expr: product_id }
"""
WITH_PRODUCT = (
    CONTRACT.read_text(encoding="utf-8")
    .replace("  - exposeId: plan_version", PRODUCT + "  - exposeId: plan_version", 1)
    .replace(
        "        - { name: signed_on, type: DATE, required: true }",
        "        - { name: signed_on, type: DATE, required: true }\n"
        "        - { name: product_id, type: INT, required: true }",
        1,
    )
    .replace(
        "          cardinality: many_to_one",
        "          cardinality: many_to_one\n"
        "        - name: product\n"
        "          to: product\n"
        "          fromColumns: [product_id]\n"
        "          toColumns: [product_id]\n"
        "          cardinality: many_to_one",
        1,
    )
)


def _with_product(keys: Any = None) -> Dict[str, Any]:
    profile = _profile()
    profile["disclosureRules"]["none"] = {}
    profile["references"] = {
        "product": {
            "disclosure": "none",
            "columns": ["product_id", "label"],
            "rows": [[key, f"p{key}"] for key in (keys if keys is not None else KEYS)],
        }
    }
    table = profile["tables"]["plan_version"]
    table["columns"]["product_id"] = {
        "domain": "category",
        "statistics": [_levels(KEYS, [900, 600, 350], 1850)],
    }
    table["history"]["columns"]["product_id"] = {
        "initial": _levels(KEYS, [500, 300, 200], 1000),
        "transitions": _transitions(
            "product_id",
            KEYS,
            {"101": [0, 70, 30], "102": [20, 0, 80], "103": [50, 50, 0]},
        ),
    }
    table["history"]["reasons"] = _levels(["product_id", "several", "tier"], [300, 50, 500], 850)
    return profile


def _tracked(tmp_path: Path) -> Dict[str, Dict[str, Any]]:
    return {item["column"]: item for item in _entries(tmp_path)[0].get("tracked", [])}


def test_a_foreign_key_into_a_reference_table_is_tracked_over_its_keys(tmp_path: Path) -> None:
    result = _init(tmp_path, _with_product(), WITH_PRODUCT)
    tracked = _tracked(tmp_path)
    assert set(tracked) == {"product_id", "tier"}
    assert tracked["product_id"]["initial"] == {
        "levels": KEYS,
        "levelWeights": ["0.5", "0.3", "0.2"],
    }
    assert tracked["product_id"]["transitions"] == {
        "101": {"102": "0.7", "103": "0.3"},
        "102": {"101": "0.2", "103": "0.8"},
        "103": {"101": "0.5", "102": "0.5"},
    }
    column = _namespace(tmp_path)["emits"]["plan_version"]["fidelity"]["columns"]["product_id"]
    assert column["levels"] == {"101": "0.333333334", "102": "0.333333333", "103": "0.333333333"}
    states = _states(result)
    for key in ("initial", "transitions"):
        assert states[f"{HISTORY}/columns/product_id/{key}"]["state"] == "used"
    assert "declares product's 3 keys as its levels, at equal weights" in result.stdout
    # The reference table is no parent of the entities, so no parent is guessed between two.
    assert "parent" not in _entries(tmp_path)[0] or "product" not in str(
        _entries(tmp_path)[0]["parent"]
    )


def test_a_key_the_profile_does_not_publish_leaves_the_column_carried() -> None:
    # A reference expose the engine accepts publishes every key, so this is the guard: with a
    # key missing, the levels would be a guess.
    table = _with_product()["tables"]["plan_version"]
    source = replace(
        _source(WITH_PRODUCT, table),
        plans={
            "tier": ColumnPlan("tier", {"levels": {"basic": "0.5", "plus": "0.3", "pro": "0.2"}}),
            "product_id": ColumnPlan("product_id", {}),
        },
        catalogues={"product_id": ("product", None)},
    )
    written = write_history(source)
    assert written.entry is not None
    assert [item["column"] for item in written.entry["tracked"]] == ["tier"]
    notes = dict(written.unused)
    why = notes[("tables", "plan_version", "history", "columns", "product_id", "transitions")]
    assert "whose keys are not all published" in why
    assert "levels" not in source.plans["product_id"].declaration


def test_a_transition_to_a_value_that_is_no_key_leaves_the_column_carried(tmp_path: Path) -> None:
    profile = _with_product(keys=["101", "102"])
    result = _init(tmp_path, profile, WITH_PRODUCT)
    assert "product_id" not in _tracked(tmp_path)
    state = _states(result)[f"{HISTORY}/columns/product_id/transitions"]
    assert "names a level the column does not declare" in state["note"]


def test_a_foreign_key_into_a_table_that_is_not_released_whole_is_carried(tmp_path: Path) -> None:
    profile = _with_product()
    del profile["references"]
    profile["tables"]["product"] = {
        "rowCount": 3,
        "columns": {"product_id": {"domain": "number", "metrics": _metrics("101", "103", 3)}},
    }
    result = _init(tmp_path, profile, WITH_PRODUCT)
    assert "product_id" not in _tracked(tmp_path)
    state = _states(result)[f"{HISTORY}/columns/product_id/transitions"]
    assert "the emit declares no levels for the column, so it is carried" in state["note"]
    digests = bundle_digests(tmp_path)
    # Pinned from the files `fluxsim init` wrote for this contract and profile before a foreign key
    # into a reference table was tracked.
    assert digests == WITHOUT_TRACKING


def test_the_written_bundle_validates_and_resolves(tmp_path: Path) -> None:
    _init(tmp_path, _with_product(), WITH_PRODUCT)
    emits = resolved_emits(tmp_path)
    assert "plan_version" in emits


#: sha256 of each file `fluxsim init` wrote for the profile that does not release `product` whole.
WITHOUT_TRACKING: Dict[str, str] = {
    "persona.flux.yml": "09abfa26eb21ac0667e7b872c394714fe05f17ae4a6b0a8b687b83d305023ab3",
    "plans.fluid.yml": "13f3fbf211036962d53bcfc096eafb36463f913e12d60b9f2cb8587d0e8e8882",
    "simulation.flux.yml": "f0717377bc2099f674a38f2a3de089c30f968824e30395192ec580090ed7ce4b",
    "world.flux.yml": "9b8d0e5a5cc58cb4743d82eaeb2696bb8cee479486e835def76fc7e93705a17c",
}


def test_the_profile_is_not_changed_by_reading_it(tmp_path: Path) -> None:
    profile = _with_product()
    before = copy.deepcopy(profile)
    _init(tmp_path, profile, WITH_PRODUCT)
    assert profile == before
