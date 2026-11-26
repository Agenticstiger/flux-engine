# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes a history's ``parent`` from its many-to-one relationship (ADR 0017 section 13).

Pinned here, through ``fluxsim init`` and the seam:

* the relationship, ``minChildren`` and the ``children`` classes written, and the profile's version
  rows per parent read as the shape of entities per parent, with the total the entities';
* the parent's foreign key declares no draw, and no ``counts`` entry lays the same relationship out;
* the rule that picks one relationship of several, and the refusal to guess;
* a contract without a to-one relationship into another expose writes the bundle it always did.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.seam.history import history_entries_of

from ._written import assert_state, bundle_digests, plan_written, resolved_emits
from .test_history_init import (
    CONTRACT,
    _bins,
    _entries,
    _init,
    _metrics,
    _namespace,
    _profile,
    _states,
)

pytestmark = pytest.mark.integration

RELATIONSHIP = "/relationships/0/statistics/0"


def _relationship(parent: str = "account", column: str = "account_id") -> Dict[str, Any]:
    """Version rows per account: 200 accounts hold one, 120 two, 80 three to five."""
    return {
        "parent": {"table": parent, "columns": [column]},
        "child": {"table": "plan_version", "columns": [column]},
        "statistics": [_bins(["0", "1", "2", "3", "6"], [0, 200, 120, 80], 400)],
    }


def _with_accounts(**more: Any) -> Dict[str, Any]:
    profile = _profile()
    profile["tables"]["account"]["rowCount"] = 400
    profile["tables"]["account"]["columns"]["account_id"]["metrics"] = _metrics("1", "400", 400)
    profile["relationships"] = [{**_relationship(), **more}]
    return profile


def _parent(tmp_path: Path) -> Dict[str, Any]:
    found: Dict[str, Any] = _entries(tmp_path)[0]["parent"]
    return found


def test_the_parent_is_the_relationship_with_its_children_per_parent(tmp_path: Path) -> None:
    result = _init(tmp_path, _with_accounts())
    assert _parent(tmp_path) == {
        "relationship": "account",
        "children": {
            "bins": ["0", "1", "2", "3", "9"],
            "binWeights": ["0", "0.5", "0.3", "0.2"],
        },
    }
    states = _states(result)
    assert states[RELATIONSHIP]["state"] == "used"
    assert "entities per parent" in states[RELATIONSHIP]["note"]
    # Version rows are not entities: the classes are kept and the total is the entities'.
    assert "version rows" in result.stdout


def test_no_counts_entry_lays_the_history_parent_out_again(tmp_path: Path) -> None:
    _init(tmp_path, _with_accounts())
    assert "counts" not in _namespace(tmp_path)
    columns = _namespace(tmp_path)["emits"]["plan_version"]["fidelity"]["columns"]
    assert not {"range", "bins", "levels"} & set(columns.get("account_id", {}))


def test_a_childless_class_is_written_as_the_minimum(tmp_path: Path) -> None:
    result = _init(tmp_path, _with_accounts(structural=["childless"]))
    assert _parent(tmp_path)["minChildren"] == 1
    assert_state(
        result, "/relationships/0/structural", "used", "minChildren 1 on the history's parent"
    )


def test_a_relationship_with_no_published_statistic_is_the_parent_alone(tmp_path: Path) -> None:
    _init(tmp_path, _profile())
    assert _parent(tmp_path) == {"relationship": "account"}


def test_a_histogram_the_layout_cannot_read_leaves_the_parent_without_children(
    tmp_path: Path,
) -> None:
    profile = _with_accounts()
    profile["relationships"][0]["statistics"] = [
        _bins(["0", "0.5", "2"], [100, 300], 400),
    ]
    result = _init(tmp_path, profile)
    assert _parent(tmp_path) == {"relationship": "account"}
    assert_state(result, RELATIONSHIP, "unused", "no children for the history's parent")


TWO_PARENTS = (
    CONTRACT.read_text(encoding="utf-8")
    .replace(
        "  - exposeId: plan_version",
        """  - exposeId: region
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/region.parquet }
    contract:
      schema:
        - { name: region_id, type: INT, required: true }
    semantics:
      entities:
        - { name: region, type: primary, expr: region_id }
  - exposeId: plan_version""",
    )
    .replace(
        "        - { name: signed_on, type: DATE, required: true }",
        "        - { name: signed_on, type: DATE, required: true }\n"
        "        - { name: region_id, type: INT, required: true }",
    )
    .replace(
        "          cardinality: many_to_one",
        "          cardinality: many_to_one\n"
        "        - name: region\n"
        "          to: region\n"
        "          fromColumns: [region_id]\n"
        "          toColumns: [region_id]\n"
        "          cardinality: many_to_one",
    )
)


def _two_parents() -> Dict[str, Any]:
    profile = _with_accounts()
    profile["tables"]["region"] = {
        "rowCount": 20,
        "columns": {"region_id": {"domain": "number", "metrics": _metrics("1", "20", 20)}},
    }
    profile["tables"]["plan_version"]["columns"]["region_id"] = {
        "domain": "number",
        "metrics": _metrics("1", "20", 1850),
    }
    return profile


def test_with_several_relationships_the_one_with_a_published_statistic_is_the_parent(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _two_parents(), TWO_PARENTS)
    assert _parent(tmp_path)["relationship"] == "account"
    assert "the only one the profile publishes children per parent for" in result.stdout


def test_with_several_relationships_and_no_rule_to_choose_no_parent_is_written(
    tmp_path: Path,
) -> None:
    profile = _two_parents()
    profile["relationships"].append(_relationship("region", "region_id"))
    result = _init(tmp_path, profile, TWO_PARENTS)
    assert "parent" not in _entries(tmp_path)[0]
    assert "choosing one would be a guess" in result.stdout

    folder = tmp_path / "none"
    folder.mkdir()
    bare = _two_parents()
    del bare["relationships"]
    _init(folder, bare, TWO_PARENTS)
    assert "parent" not in _entries(folder)[0]


def test_a_contract_without_a_to_one_relationship_writes_the_entry_it_always_did(
    tmp_path: Path,
) -> None:
    contract = CONTRACT.read_text(encoding="utf-8").replace(
        """      relationships:
        - name: account
          to: account
          fromColumns: [account_id]
          toColumns: [account_id]
          cardinality: many_to_one
""",
        "",
    )
    _init(tmp_path, _with_accounts(), contract)
    entry = _entries(tmp_path)[0]
    assert "parent" not in entry
    assert "counts" not in _namespace(tmp_path)
    # The statistics of a relationship the contract does not declare stay unused.
    assert entry["entities"] == 1000
    digests = bundle_digests(tmp_path)
    # Pinned from the files `fluxsim init` wrote for this contract and profile before a history
    # wrote its parent, but for the header, which no longer names the profile's file, and the
    # unused list, which names a history's tracked column whole.
    assert digests == WITHOUT_A_PARENT


WITHOUT_A_PARENT: Dict[str, str] = {
    "persona.flux.yml": "74f6062c0e15e70d2689583ad345103c7189b244886c3280c244573b49677755",
    "plans.fluid.yml": "6cb0c570c814e1d9fe3880b64b4f67e36b63428c9e8f6b93104d050e6803f40e",
    "simulation.flux.yml": "efee1fac226a89dfc0e2d0b08e4899652361e17abe915ac359aef7f115dc9a99",
    "world.flux.yml": "20a3e0be50c0216bb8ef8ac07cdc72b0100a3cf4bddccbb91b7841fcc96b88cc",
}


def test_a_written_parent_resolves_through_the_seam(tmp_path: Path) -> None:
    _init(tmp_path, _with_accounts())
    emits = resolved_emits(tmp_path)
    (entry,) = history_entries_of(emits["plan_version"].extension, "plan_version")
    assert entry.parent is not None and entry.parent.relationship == "account"
    assert entry.parent.children is not None


def test_the_published_statistics_are_not_changed_by_reading_them(tmp_path: Path) -> None:
    profile = _with_accounts()
    before = copy.deepcopy(profile)
    _init(tmp_path, profile)
    assert profile == before


def test_every_statistic_of_the_relationship_is_used_or_unused_with_a_reason(
    tmp_path: Path,
) -> None:
    profile = _with_accounts()
    profile["relationships"][0]["statistics"].append(
        {
            "definition": "mean/1",
            "sampleSize": {"min": 1000, "max": 9999},
            "disclosure": "acro-default",
            "groups": [{"group": "", "k": 400, "Sx": str(int(4.6 * 400 * 2**16)), "status": "F"}],
        }
    )
    result = _init(tmp_path, profile)
    states = _states(result)
    mean = states["/relationships/0/statistics/1"]
    assert mean["state"] == "unused"
    assert "version rows per parent, not of entities" in mean["note"]
    assert not any(
        state["state"] not in ("used", "unused")
        for pointer, state in states.items()
        if pointer.startswith("/relationships/")
    )


def _with_dependency(sources: str, targets: str) -> str:
    """The plans contract with one ``dependency`` of the plan versions, *sources* to *targets*."""
    return CONTRACT.read_text(encoding="utf-8").replace(
        "        - { name: signed_on, type: DATE, required: true }\n    semantics:",
        "        - { name: signed_on, type: DATE, required: true }\n"
        "      invariants:\n"
        "        - id: account_by_plan\n"
        f"          dependency: {{ from: [{sources}], to: [{targets}] }}\n"
        "    semantics:",
    )


def test_a_dependency_from_the_entity_to_the_parent_key_keeps_the_parent(tmp_path: Path) -> None:
    # The layout gives every version of a plan one account, so the dependency holds by it and
    # the plan draws nothing for it: the relationship is still the history's parent.
    result = _init(tmp_path, _with_accounts(), _with_dependency("plan_id", "account_id"))
    (entry,) = _entries(tmp_path)
    assert entry["parent"]["relationship"] == "account" and "children" in entry["parent"]
    assert "history parent account: not the parent" not in result.stdout
    plan_written(tmp_path)


@pytest.mark.parametrize(
    "sources, targets",
    [
        # From a column that is not the natural entity: the layout does not decide it.
        ("signed_on", "account_id"),
        # To a column besides the parent's key: the layout sets only the key.
        ("plan_id", "account_id, channel"),
    ],
)
def test_a_dependency_the_layout_does_not_hold_still_refuses_the_parent(
    tmp_path: Path, sources: str, targets: str
) -> None:
    result = _init(tmp_path, _with_accounts(), _with_dependency(sources, targets))
    assert "parent" not in _entries(tmp_path)[0]
    assert "a rule, a dependency or a copy sets its column" in result.stdout
