# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` declares no date that a key or a rule of the contract already sets.

Pinned here, from small hand-written profiles of format 1-3-0:

* a parent's start-only lifetime is not written from a relationship's date pair whose child column a
  contract derive sets, and the pair is recorded unused with the reason the ``within`` side gives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.stages.plan import plan as plan_stage

from .test_init_dates import (
    HOLDS,
    SCHEMA_URL,
    Written,
    _histogram,
    _levels,
    _link_pair,
    _pair,
    _stamp,
)

pytestmark = pytest.mark.integration

#: A gym's members and their visits. A visit's ``visited_on`` is a day after it was booked.
GYM = """\
fluidVersion: 0.7.7
kind: DataProduct
id: gym.silver.visits
name: Gym visits
metadata:
  owner:
    team: gym
exposes:
- exposeId: gym_member
  kind: table
  contract:
    schema:
    - name: member_id
      type: STRING
      required: true
    - name: joined_on
      type: DATE
      required: true
  semantics:
    entities:
    - name: member
      type: primary
      expr: member_id
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/gym_member.parquet
- exposeId: visit
  kind: table
  contract:
    schema:
    - name: visit_id
      type: STRING
      required: true
    - name: member_id
      type: STRING
      required: true
    - name: booked_on
      type: DATE
      required: true
    - name: visited_on
      type: DATE
    invariants:
    - id: visited_the_day_after
      derive: visited_on
      expr: ADD_DAYS(booked_on, 1)
  semantics:
    entities:
    - name: visit
      type: primary
      expr: visit_id
    relationships:
    - name: visit_to_member
      to: gym_member
      fromColumns:
      - member_id
      toColumns:
      - member_id
      cardinality: many_to_one
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/visit.parquet
"""


def _gym_profile(child_column: str) -> Dict[str, Any]:
    """A profile whose one relationship date pair runs from ``joined_on`` to *child_column*."""
    return {
        "_producer": "https://example.test/producer",
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "gym_member": {
                "rowCount": 100,
                "columns": {"member_id": {"domain": "category"}, "joined_on": _stamp()},
            },
            "visit": {
                "rowCount": 400,
                "columns": {
                    "visit_id": {"domain": "category"},
                    "member_id": {"domain": "category"},
                    "booked_on": _stamp(),
                    "visited_on": _stamp(),
                },
            },
        },
        "relationships": [
            {
                "parent": {"table": "gym_member", "columns": ["member_id"]},
                "child": {"table": "visit", "columns": ["member_id"]},
                "statistics": [_histogram((0, 2, 8), (60, 40))],
                "dates": [
                    _link_pair("joined_on", child_column, _levels(after=300, before=0, same=100))
                ],
            }
        ],
    }


def test_a_start_is_not_written_from_a_pair_whose_child_date_a_rule_sets(tmp_path: Path) -> None:
    written = Written(tmp_path, GYM, _gym_profile("visited_on"))
    assert written.lifetimes == {}
    assert written.within == []
    reasons = written.unused("/relationships/0/dates/0")
    assert reasons and all("already sets 'visited_on'" in reason for reason in reasons)
    planned = plan_stage(
        str(written.directory),
        state_dir=str(tmp_path / "state"),
        profile=str(written.profile_path),
    )
    assert planned.exit_code == 0, planned.stderr


def test_a_start_is_written_from_a_pair_whose_child_date_nothing_sets(tmp_path: Path) -> None:
    written = Written(tmp_path, GYM, _gym_profile("booked_on"))
    assert written.lifetimes == {"gym_member": {"emit": "gym_member", "start": "joined_on"}}
    assert [entry["date"] for entry in written.within] == ["booked_on"]
    assert written.unused("/relationships/0/dates/0") == []


def test_a_start_is_not_written_when_no_within_entry_reads_it(tmp_path: Path) -> None:
    # Visits have a lifetime of their own, from booking to visit, so a within entry cannot date
    # the visit: the start the relationship's pair offers the member is then read by nothing.
    contract = GYM.replace(
        "    invariants:\n"
        "    - id: visited_the_day_after\n"
        "      derive: visited_on\n"
        "      expr: ADD_DAYS(booked_on, 1)\n",
        "",
    )
    profile = _gym_profile("visited_on")
    profile["tables"]["visit"]["datePairs"] = [_pair("booked_on", "visited_on", HOLDS)]
    written = Written(tmp_path, contract, profile)
    assert sorted(written.lifetimes) == ["visit"]
    assert (written.lifetimes["visit"]["start"], written.lifetimes["visit"]["end"]) == (
        "booked_on",
        "visited_on",
    )
    assert written.within == []
    reasons = written.unused("/relationships/0/dates/0")
    assert reasons and all("is the end of the lifetime of 'visit'" in reason for reason in reasons)
    planned = plan_stage(
        str(written.directory),
        state_dir=str(tmp_path / "state"),
        profile=str(written.profile_path),
    )
    assert planned.exit_code == 0, planned.stderr
