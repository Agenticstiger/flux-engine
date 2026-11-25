# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes no ``within`` entry whose date no wider range lets the plan accept.

Pinned here, from a small hand-written profile of format 1-3-0:

* a child date's entry is enforced against its parent's lifetime, and the plan checks that the
  declared ranges leave no row without a value. When the parent's start column declares no range, no
  widening of the child's range makes that check hold, so the entry is left out and the pair is
  recorded unused with the reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.stages.plan import plan as plan_stage

from .test_init_dates import SCHEMA_URL, Written, _histogram, _levels, _link_pair, _size, _stamp

pytestmark = pytest.mark.integration

#: A library's members and their loans. A member's ``joined_on`` is the start of a lifetime.
LIBRARY = """\
fluidVersion: 0.7.7
kind: DataProduct
id: library.silver.loans
name: Library loans
metadata:
  owner:
    team: library
exposes:
- exposeId: library_member
  kind: table
  contract:
    schema:
    - name: member_id
      type: STRING
      required: true
    - name: joined_on
      type: DATE
      required: true
    - name: renewed_on
      type: DATE
  semantics:
    entities:
    - name: member
      type: primary
      expr: member_id
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/library_member.parquet
- exposeId: loan
  kind: table
  contract:
    schema:
    - name: loan_id
      type: STRING
      required: true
    - name: member_id
      type: STRING
      required: true
    - name: borrowed_on
      type: DATE
      required: true
  semantics:
    entities:
    - name: loan
      type: primary
      expr: loan_id
    relationships:
    - name: loan_to_member
      to: library_member
      fromColumns:
      - member_id
      toColumns:
      - member_id
      cardinality: many_to_one
  binding:
    platform: local
    format: parquet
    location:
      path: ./out/loan.parquet
"""


def _withheld() -> Dict[str, Any]:
    """A date column whose range the profile withholds: it publishes its null count and no more."""
    withheld = {
        "definition": "min/1",
        "status": "A",
        "sampleSize": _size(100),
        "disclosure": "none",
    }
    return {
        "domain": "timestamp",
        "metrics": {
            "min": withheld,
            "max": {**withheld, "definition": "max/1"},
            "nullCount": {
                "definition": "nullCount/1",
                "status": "F",
                "value": 0,
                "sampleSize": _size(100),
                "disclosure": "none",
            },
        },
    }


def _library_profile(joined_on: Dict[str, Any]) -> Dict[str, Any]:
    """A profile whose one relationship date pair runs from ``joined_on`` to ``borrowed_on``."""
    return {
        "_producer": "https://example.test/producer",
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "tables": {
            "library_member": {
                "rowCount": 100,
                "columns": {
                    "member_id": {"domain": "category"},
                    "joined_on": joined_on,
                    "renewed_on": _stamp(),
                },
            },
            "loan": {
                "rowCount": 400,
                "columns": {
                    "loan_id": {"domain": "category"},
                    "member_id": {"domain": "category"},
                    "borrowed_on": _stamp(),
                },
            },
        },
        "relationships": [
            {
                "parent": {"table": "library_member", "columns": ["member_id"]},
                "child": {"table": "loan", "columns": ["member_id"]},
                "statistics": [_histogram((0, 2, 8), (60, 40))],
                "dates": [
                    _link_pair("joined_on", "borrowed_on", _levels(after=300, before=0, same=100))
                ],
            }
        ],
    }


def test_a_date_no_wider_range_can_hold_is_left_out_and_recorded_unused(tmp_path: Path) -> None:
    # The members' joining date publishes no range, so the latest start of a member's lifetime is
    # not declared and no range for the loan's date makes the plan's check hold.
    written = Written(tmp_path, LIBRARY, _library_profile(_withheld()))
    assert written.within == []
    reasons = written.unused("/relationships/0/dates/0")
    assert reasons
    assert all(
        "latest start of the parent's lifetime is not declared" in reason
        and "'joined_on'" in reason
        for reason in reasons
    )
    planned = plan_stage(
        str(written.directory),
        state_dir=str(tmp_path / "state"),
        profile=str(written.profile_path),
    )
    assert planned.exit_code == 0, planned.stderr


def test_the_same_date_is_written_when_the_start_declares_its_range(tmp_path: Path) -> None:
    written = Written(tmp_path, LIBRARY, _library_profile(_stamp()))
    assert [entry["date"] for entry in written.within] == ["borrowed_on"]
    assert written.unused("/relationships/0/dates/0") == []
