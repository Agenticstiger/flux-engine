# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` keeps a snapshot whose foreign key a ``counts`` entry would set (ADR 0027 section 9).

A snapshot takes every column from its history, and a ``counts`` entry over one of its
relationships is refused, so ``init`` drops that entry. The foreign key then holds the history's
value, which is a key of the parent expose when the history declares the same relationship over the
same column and requires the column, whether or not that relationship is the history's own
``parent``.

The fixture is a library: a loan history laid out under its member, a shelf the loans stand on, and
a table of the current loans that the profile counts per shelf. Pinned here: the snapshot is kept
and the count is unused with a reason that names the table and no value; the loans and their
versions name one another and every shelf key is a shelf's; a history that does not declare the
relationship still leaves the table unprojected; and a bundle no count touches keeps its bytes.
An events table counted per shelf is read by the same rule.
"""

from __future__ import annotations

import json
from functools import reduce
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest
import yaml

from flux_engine.seam.projections import SNAPSHOTS_KEY
from flux_engine.stages.run import run

from ._written import applied_and_verified, bundle_digests
from .test_history_init import (
    CONTRACT as HISTORY_CONTRACT,
)
from .test_history_init import (
    _bins,
    _entries,
    _init,
    _levels,
    _metrics,
    _namespace,
    _profile,
    _states,
)
from .test_history_parent import _relationship as _published_relationship

pytestmark = pytest.mark.integration

PROFILE_FILE = "plans.profile.json"
SHELVES, MEMBERS, LOANS = 40, 400, 1000

#: Library names for the fixture the history tests share: a loan history under its member.
_NAMES = (
    ("plan_version", "loan_version"),
    ("plan_current", "loan_current"),
    ("plan_id", "loan_id"),
    ("account", "member"),
)


def _renamed(text: str) -> str:
    return reduce(lambda found, pair: found.replace(*pair), _NAMES, text)


#: A shelf, and the columns and relationship of it that the loan history and its snapshot declare.
_SHELF = """\
  - exposeId: shelf
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/shelf.parquet }
    contract:
      schema:
        - { name: shelf_id, type: INT, required: true }
    semantics:
      entities:
        - { name: shelf, type: primary, expr: shelf_id }
"""
_RELATIONSHIP = """\
        - name: shelf
          to: shelf
          fromColumns: [shelf_id]
          toColumns: [shelf_id]
          cardinality: many_to_one
"""
_CURRENT = """\
  - exposeId: loan_current
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/loan_current.parquet }
    contract:
      schema:
        - { name: loan_id, type: INT, required: true }
        - { name: shelf_id, type: INT, required: true }
        - { name: tier, type: STRING, required: true }
        - { name: channel, type: STRING, required: true }
    semantics:
      entities:
        - { name: loan, type: primary, expr: loan_id }
      dimensions:
        - { name: tier, type: categorical }
        - { name: channel, type: categorical }
      relationships:
""" + _RELATIONSHIP


#: The relationship the history declares over its shelf column, when it does not reach the shelf.
_ELSEWHERE = """\
        - name: shelf
          to: member
          fromColumns: [shelf_id]
          toColumns: [member_id]
          cardinality: many_to_one
"""

_CHANGE = """\
  - exposeId: loan_change
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/loan_change.parquet }
    contract:
      schema:
        - { name: event_id, type: INT, required: true }
        - { name: loan_id, type: INT, required: true }
        - { name: shelf_id, type: INT, required: true }
        - { name: op, type: "enum('c','u','d')", required: true }
        - { name: recorded_on, type: DATE, required: true }
        - { name: before_tier, type: STRING, required: false }
        - { name: after_tier, type: STRING, required: false }
    semantics:
      defaultAggTimeDimension: recorded_on
      entities:
        - { name: event, type: primary, expr: event_id }
      dimensions:
        - { name: op, type: categorical }
        - { name: recorded_on, type: time }
        - { name: before_tier, type: categorical }
        - { name: after_tier, type: categorical }
      relationships:
        - name: loan
          to: loan_version
          fromColumns: [loan_id]
          toColumns: [loan_id]
          cardinality: many_to_one
""" + _RELATIONSHIP


def _library(
    *,
    relationship: Optional[str] = _RELATIONSHIP,
    required: bool = True,
    changes: bool = False,
) -> str:
    """The loan history, its member, a shelf and the table of the current loans.

    The history holds the shelf's key, declares *relationship* over it (none when ``None``), and
    requires it unless *required* is false. With *changes* the loans' change events follow.
    """
    text = _renamed(HISTORY_CONTRACT.read_text(encoding="utf-8"))
    column = f"        - {{ name: shelf_id, type: INT, required: {str(required).lower()} }}\n"
    anchor = "        - { name: valid_from, type: DATE, required: true }\n"
    text = text.replace(anchor, column + anchor, 1)
    if relationship is not None:
        text = text.replace(
            "          cardinality: many_to_one\n",
            "          cardinality: many_to_one\n" + relationship,
            1,
        )
    text = text.replace("  - exposeId: loan_version\n", _SHELF + "  - exposeId: loan_version\n", 1)
    return text + _CURRENT + (_CHANGE if changes else "")


def _shelf_counts() -> Dict[str, Any]:
    """The current loans per shelf: 10 shelves hold none, 20 hold a few, 10 hold many."""
    found = json.loads(_renamed(json.dumps(_published_relationship("shelf", "shelf_id"))))
    found["child"]["table"] = "loan_current"
    found["statistics"] = [_bins(["0", "1", "20", "60", "100"], [10, 10, 10, 10], SHELVES)]
    found["structural"] = ["childless"]
    return found


def _published(*, counted: bool = True) -> Dict[str, Any]:
    """The profile of the library: the history and its member, the shelf and the current loans."""
    profile = json.loads(_renamed(json.dumps(_profile())))
    tables = profile["tables"]
    tables["shelf"] = {
        "rowCount": SHELVES,
        "columns": {
            "shelf_id": {"domain": "number", "metrics": _metrics("1", str(SHELVES), SHELVES)}
        },
    }
    tables["loan_version"]["columns"]["shelf_id"] = {
        "domain": "number",
        "metrics": _metrics("1", str(SHELVES), 1850),
    }
    tables["loan_current"] = {
        "rowCount": LOANS,
        "columns": {
            # The table counts its loans from another number than the history does.
            "loan_id": {
                "domain": "number",
                "metrics": _metrics("5001", str(5000 + LOANS), LOANS),
            },
            "shelf_id": {"domain": "number", "metrics": _metrics("1", str(SHELVES), LOANS)},
            "tier": {
                "domain": "category",
                "statistics": [_levels(["basic", "plus"], [600, 400], LOANS)],
            },
            "channel": {
                "domain": "category",
                "statistics": [_levels(["app", "web"], [600, 400], LOANS)],
            },
        },
    }
    tables["member"]["rowCount"] = MEMBERS
    tables["member"]["columns"]["member_id"]["metrics"] = _metrics("1", str(MEMBERS), MEMBERS)
    profile["relationships"] = [
        {
            "parent": {"table": "member", "columns": ["member_id"]},
            "child": {"table": "loan_version", "columns": ["member_id"]},
            "statistics": [_bins(["0", "1", "2", "3", "6"], [200, 120, 50, 30], MEMBERS)],
        }
    ]
    if counted:
        profile["relationships"].append(_shelf_counts())
    return profile


def _written(tmp_path: Path, contract: str, profile: Dict[str, Any]) -> Any:
    return _init(tmp_path, json.loads(json.dumps(profile)), contract)


def test_the_snapshot_is_kept_and_the_count_over_its_shelf_is_unused(tmp_path: Path) -> None:
    result = _written(tmp_path, _library(), _published())
    namespace = _namespace(tmp_path)
    assert namespace[SNAPSHOTS_KEY] == [
        {"emit": "loan_current", "of": "loan_version", "ended": "keep"}
    ]
    assert "counts" not in namespace
    assert "rows" not in namespace["emits"]["loan_current"]
    # The history is laid out under its member, which the count over the shelf does not touch.
    assert _entries(tmp_path)[0]["parent"]["relationship"] == "member"
    state = _states(result)["/relationships/1/statistics/0"]
    assert state["state"] == "unused"
    assert "loan_current" in state["note"]
    assert "loan_version" in state["note"]
    assert "shelf" in state["note"]
    assert not any(character.isdigit() for character in state["note"])


def test_a_snapshot_with_no_count_over_the_shelf_is_the_snapshot_it_was(tmp_path: Path) -> None:
    profile = _published(counted=False)
    _written(tmp_path, _library(), profile)
    assert _namespace(tmp_path)[SNAPSHOTS_KEY]
    assert "counts" not in _namespace(tmp_path)


@pytest.mark.parametrize(
    "contract",
    [
        _library(relationship=None),
        _library(relationship=_ELSEWHERE),
        _library(required=False),
    ],
    ids=["no-relationship", "relationship-elsewhere", "column-not-required"],
)
def test_a_history_whose_column_is_no_shelf_key_leaves_the_table_unprojected(
    tmp_path: Path, contract: str
) -> None:
    result = _written(tmp_path, contract, _published())
    namespace = _namespace(tmp_path)
    assert SNAPSHOTS_KEY not in namespace
    assert [item["emit"] for item in namespace["counts"]] == ["loan_current"]
    assert (
        "loan_current: snapshots: unused, the expose meets only part of the rule: its column "
        "shelf_id is set by a counts entry, which a projected emit refuses, and loan_version "
        "writes its parent over member, not over shelf, so the values it holds in shelf_id "
        "are no keys of shelf"
    ) in result.stdout


def test_an_events_table_counted_per_shelf_is_projected_by_the_same_rule(tmp_path: Path) -> None:
    from .test_projection_init import _tables

    profile = _published(counted=False)
    changes = json.loads(_renamed(json.dumps(_tables()["plan_change"])))
    changes["columns"]["shelf_id"] = {
        "domain": "number",
        "metrics": _metrics("1", str(SHELVES), changes["rowCount"]),
    }
    profile["tables"]["loan_change"] = changes
    profile["relationships"].append(
        {
            "parent": {"table": "shelf", "columns": ["shelf_id"]},
            "child": {"table": "loan_change", "columns": ["shelf_id"]},
            "statistics": [_bins(["0", "1", "20", "60", "200"], [10, 10, 10, 10], SHELVES)],
            "structural": ["childless"],
        }
    )
    _written(tmp_path, _library(changes=True), profile)
    namespace = _namespace(tmp_path)
    assert [item["emit"] for item in namespace["events"]] == ["loan_change"]
    assert "counts" not in namespace
    # With no relationship of the history over the column, the table keeps its count.
    other = tmp_path / "other"
    other.mkdir()
    _written(other, _library(relationship=None, changes=True), profile)
    assert [item["emit"] for item in _namespace(other)["counts"]] == ["loan_change"]


def _rows(tmp_path: Path, emit: str) -> List[List[Any]]:
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / PROFILE_FILE,
        verify=False,
    )
    assert outcome.success, outcome.stderr
    run_id = outcome.artifacts["plan"]["run_id"]
    text = (tmp_path / "state" / "runs" / run_id / "emits" / f"{emit}.ndjson").read_text("utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _column(tmp_path: Path, emit: str, name: str) -> Set[Any]:
    block = yaml.safe_load((tmp_path / "plans.fluid.yml").read_text("utf-8"))
    schema = next(item for item in block["exposes"] if item["exposeId"] == emit)["contract"][
        "schema"
    ]
    position = [column["name"] for column in schema].index(name)
    return {row[position] for row in _rows(tmp_path, emit)}


def test_every_loan_has_its_versions_and_every_shelf_key_is_a_shelf(tmp_path: Path) -> None:
    _written(tmp_path, _library(), _published())
    loans = _column(tmp_path, "loan_current", "loan_id")
    versioned = _column(tmp_path, "loan_version", "loan_id")
    assert loans == versioned
    shelves = _column(tmp_path, "shelf", "shelf_id")
    assert _column(tmp_path, "loan_current", "shelf_id") <= shelves
    assert _column(tmp_path, "loan_version", "shelf_id") <= shelves


def test_the_written_bundle_validates_plans_applies_and_verifies(tmp_path: Path) -> None:
    _written(tmp_path, _library(), _published())
    applied_and_verified(tmp_path, PROFILE_FILE)


def test_a_bundle_no_count_touches_keeps_its_bytes(tmp_path: Path) -> None:
    _written(tmp_path, _library(), _published(counted=False))
    assert bundle_digests(tmp_path) == WITHOUT_A_COUNT


#: sha256 of each file `fluxsim init` wrote for the library without a count over its current
#: loans, before a counted snapshot beside another parent was kept.
WITHOUT_A_COUNT: Dict[str, str] = {
    "persona.flux.yml": "74f6062c0e15e70d2689583ad345103c7189b244886c3280c244573b49677755",
    "plans.fluid.yml": "0cb17aad95364afc94f84e5026700e035685307d9436bc275d618424a0e51d14",
    "simulation.flux.yml": "771d0b807ae00835cc1e69e84d8a78f969adc28c91f222b8e50d98fae670e041",
    "world.flux.yml": "20a3e0be50c0216bb8ef8ac07cdc72b0100a3cf4bddccbb91b7841fcc96b88cc",
}
