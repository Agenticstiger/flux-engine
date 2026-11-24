# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected table that is also a counted child takes the history's layout (ADR 0027 section 9).

A snapshot, events or periodic emit takes its rows from the history, and a ``counts`` entry over one
of its relationships is refused. ``fluxsim init`` therefore writes the projection, drops the emit's
own ``counts`` entry and gives that statistic to the history's ``parent`` when the history has none
of its own. Pinned here: the entries written, the ledger's account of where each statistic went,
the cases where it is kept and where it is unused, and the bundle of a contract without the shape.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.seam.projections import SNAPSHOTS_KEY
from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.validate import validate
from flux_engine.stages.verify import verify

from .test_history_init import _bins, _entries, _init, _metrics, _namespace, _states
from .test_history_parent import _relationship
from .test_projection_init import CONTRACT, _profile_with

pytestmark = pytest.mark.integration

#: ``plan_current`` is a snapshot that also names the account it belongs to.
COUNTED = CONTRACT.read_text(encoding="utf-8").replace(
    """        - { name: plan_id, type: INT, required: true }
        - { name: tier, type: STRING, required: true }
        - { name: channel, type: STRING, required: true }
    semantics:
      entities:
        - { name: plan, type: primary, expr: plan_id }
      dimensions:
        - { name: tier, type: categorical }
        - { name: channel, type: categorical }
""",
    """        - { name: plan_id, type: INT, required: true }
        - { name: account_id, type: INT, required: true }
        - { name: tier, type: STRING, required: true }
        - { name: channel, type: STRING, required: true }
    semantics:
      entities:
        - { name: plan, type: primary, expr: plan_id }
      dimensions:
        - { name: tier, type: categorical }
        - { name: channel, type: categorical }
      relationships:
        - name: account
          to: account
          fromColumns: [account_id]
          toColumns: [account_id]
          cardinality: many_to_one
""",
    1,
)

RELATIONSHIPS = "/relationships"


def _profile(history_children: bool = False, *, snapshot_children: bool = True) -> Dict[str, Any]:
    profile = _profile_with("plan_current")
    profile["tables"]["account"]["rowCount"] = 400
    profile["tables"]["account"]["columns"]["account_id"]["metrics"] = _metrics("1", "400", 400)
    current = profile["tables"]["plan_current"]
    current["columns"]["account_id"] = {
        "domain": "number",
        "metrics": _metrics("1", "400", 1000),
    }
    profile["relationships"] = []
    if history_children:
        profile["relationships"].append(_relationship())
    if snapshot_children:
        # One row per plan, so these are plans per account: 100 accounts hold one, 200 two, 100 three to five.
        profile["relationships"].append(
            {
                "parent": {"table": "account", "columns": ["account_id"]},
                "child": {"table": "plan_current", "columns": ["account_id"]},
                "statistics": [_bins(["0", "1", "2", "3", "6"], [0, 100, 200, 100], 400)],
                "structural": ["childless"],
            }
        )
    return profile


def _written(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    return _init(tmp_path, profile, COUNTED)


def test_the_snapshot_stays_a_projection_and_its_counts_entry_goes_to_the_history_parent(
    tmp_path: Path,
) -> None:
    _written(tmp_path, _profile())
    namespace = _namespace(tmp_path)
    assert namespace[SNAPSHOTS_KEY] == [
        {"emit": "plan_current", "of": "plan_version", "ended": "keep"}
    ]
    assert "counts" not in namespace
    assert "rows" not in namespace["emits"]["plan_current"]
    parent = _entries(tmp_path)[0]["parent"]
    assert parent["relationship"] == "account"
    assert parent["minChildren"] == 1
    assert parent["children"]["binWeights"] == ["0", "0.25", "0.5", "0.25"]


def test_the_ledger_says_which_statistic_moved_where(tmp_path: Path) -> None:
    result = _written(tmp_path, _profile())
    states = _states(result)
    state = states["/relationships/0/statistics/0"]
    assert state["state"] == "used"
    assert "projected from a history" in state["note"]
    assert "plan_version's parent account takes it as its children" in state["note"]
    assert "minChildren 1 on the history's parent" in states["/relationships/0/structural"]["note"]
    assert "plan_current: counts" in result.stdout


def test_the_history_keeps_its_own_children_and_the_statistic_is_unused(tmp_path: Path) -> None:
    result = _written(tmp_path, _profile(history_children=True))
    parent = _entries(tmp_path)[0]["parent"]
    assert parent["children"]["bins"] == ["0", "1", "2", "3", "9"]
    assert _namespace(tmp_path)[SNAPSHOTS_KEY]
    assert "counts" not in _namespace(tmp_path)
    state = _states(result)["/relationships/1/statistics/0"]
    assert state["state"] == "unused"
    assert "own children per parent are written and are kept" in state["note"]


#: ``COUNTED`` with the history's relationship to the account removed: the history writes no
#: ``parent``, though it holds the ``account_id`` column the counted table would copy.
NO_HISTORY_PARENT = COUNTED.replace(
    """      relationships:
        - name: account
          to: account
          fromColumns: [account_id]
          toColumns: [account_id]
          cardinality: many_to_one
  - exposeId: plan_invoice""",
    "  - exposeId: plan_invoice",
    1,
)


def test_with_no_history_parent_the_table_is_not_projected_and_says_why(tmp_path: Path) -> None:
    assert NO_HISTORY_PARENT != COUNTED
    result = _init(tmp_path, _profile(), NO_HISTORY_PARENT)
    namespace = _namespace(tmp_path)
    assert "parent" not in _entries(tmp_path)[0]
    # The table would copy the history's drawn account_id values, which are no account keys, so it
    # keeps its own rows and its own counts entry.
    assert SNAPSHOTS_KEY not in namespace
    assert [item["emit"] for item in namespace["counts"]] == ["plan_current"]
    assert "rows" in namespace["emits"]["plan_current"]
    assert (
        "plan_current: snapshots: unused, the expose meets only part of the rule: its column "
        "account_id is set by a counts entry, which a projected emit refuses, and plan_version "
        "writes no parent over account, so the values it holds in account_id are no keys of "
        "account"
    ) in result.stdout


def test_a_history_parent_over_another_expose_is_no_parent_over_the_counted_one(
    tmp_path: Path,
) -> None:
    # The history's parent is laid out under another expose than the counted table's account.
    other = COUNTED.replace(
        "        - name: account\n          to: account\n          fromColumns: [account_id]\n"
        "          toColumns: [account_id]\n          cardinality: many_to_one\n  - exposeId: plan_invoice",
        "        - name: account\n          to: plan_current\n          fromColumns: [account_id]\n"
        "          toColumns: [plan_id]\n          cardinality: many_to_one\n  - exposeId: plan_invoice",
        1,
    )
    assert other != COUNTED
    result = _init(tmp_path, _profile(), other)
    assert SNAPSHOTS_KEY not in _namespace(tmp_path)
    assert "plan_version writes its parent over plan_current, not over account" in result.stdout


@pytest.mark.parametrize("contract", [COUNTED, NO_HISTORY_PARENT], ids=["parent", "no-parent"])
def test_the_written_bundle_validates_plans_applies_and_verifies(
    tmp_path: Path, contract: str
) -> None:
    _init(tmp_path, _profile(), contract)
    bundle, state = tmp_path / "bundle", tmp_path / "state"
    assert validate(bundle).exit_code == 0
    planned = plan(
        bundle,
        state_dir=state,
        profile=tmp_path / "plans.profile.json",
        kernel="0.2.0",
        stage=False,
    )
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    applied = apply(bundle, state_dir=state, run_id=run_id)
    assert applied.success, applied.stderr
    checked = verify(bundle, state_dir=state, run_id=run_id)
    assert checked.success, checked.stdout + checked.stderr


def test_the_written_bundle_validates_and_plans(tmp_path: Path) -> None:
    _written(tmp_path, _profile())
    assert validate(tmp_path / "bundle").exit_code == 0
    outcome = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "plans.profile.json",
        kernel="0.2.0",
        stage=False,
    )
    assert outcome.success, outcome.stderr


def test_a_snapshot_that_is_no_counted_child_writes_the_bundle_it_always_did(
    tmp_path: Path,
) -> None:
    profile = _profile_with("plan_current")
    _init(tmp_path, profile, CONTRACT.read_text(encoding="utf-8"))
    digests = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((tmp_path / "bundle").iterdir())
        if path.is_file()
    }
    assert digests == WITHOUT_COUNTS


#: sha256 of each file `fluxsim init` wrote for this contract and profile before a counted
#: projection was written.
WITHOUT_COUNTS: Dict[str, str] = {
    "persona.flux.yml": "09abfa26eb21ac0667e7b872c394714fe05f17ae4a6b0a8b687b83d305023ab3",
    "plans.fluid.yml": "f50c1adff7eeae2f5dca996a48fa329ca9704765f408013097d796d5cee73e53",
    "simulation.flux.yml": "8e7d0acc907fcfbfd26cc9e298a97acd5185fc39ae6968195fcba2c81a19e74b",
    "world.flux.yml": "9b8d0e5a5cc58cb4743d82eaeb2696bb8cee479486e835def76fc7e93705a17c",
}
