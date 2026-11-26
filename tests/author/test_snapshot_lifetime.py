# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A snapshot whose two dates its history sets takes no lifetime of its own.

Pinned here, through ``fluxsim init`` on the plans of an account, with ``plan_latest`` the
snapshot of the plans' history:

* a date pair of the snapshot whose start and end are both columns the history sets writes no
  lifetime, and its statistics are unused because the history sets both dates;
* a pair one of whose columns a rule of the contract sets keeps its lifetime, as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.seam.projections import SNAPSHOTS_KEY
from tests.cli.test_init_dates import HOLDS, _pair

from .test_history_init import CONTRACT, _init, _metrics, _ms, _namespace, _profile, _states

pytestmark = pytest.mark.integration

LATEST = """\
  - exposeId: plan_latest
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/plan_latest.parquet }
    contract:
      schema:
        - { name: plan_id, type: INT, required: true }
        - { name: valid_from, type: DATE, required: true }
        - { name: signed_on, type: DATE, required: true }
{rules}    semantics:
      entities:
        - { name: plan, type: primary, expr: plan_id }
"""

#: A rule of the snapshot that sets its start date, so only the end is the history's.
SIGNED_BY_RULE = """\
      invariants:
        - id: signed_when_valid
          derive: signed_on
          expr: DATE '2024-06-01'
"""


def _written(tmp_path: Path, rules: str = "", pairs: bool = True) -> Any:
    contract = CONTRACT.read_text(encoding="utf-8") + LATEST.replace("{rules}", rules)
    profile = _profile()
    columns: Dict[str, Any] = {
        "plan_id": {"domain": "number", "metrics": _metrics("1", "1000", 1000)},
        "valid_from": {
            "domain": "timestamp",
            "metrics": _metrics(_ms(2025, 1, 1), _ms(2025, 9, 1), 1000),
        },
        "signed_on": {
            "domain": "timestamp",
            "metrics": _metrics(_ms(2024, 6, 1), _ms(2025, 6, 1), 1000),
        },
    }
    profile["disclosureRules"]["none"] = {}
    profile["tables"]["plan_latest"] = {
        "rowCount": 1000,
        "columns": columns,
    }
    if pairs:
        profile["tables"]["plan_latest"]["datePairs"] = [
            _pair("signed_on", "valid_from", HOLDS, edges=(214, 300, 400, 450))
        ]
    return _init(tmp_path, profile, contract)


def test_a_snapshot_pair_whose_dates_the_history_both_sets_writes_no_lifetime(
    tmp_path: Path,
) -> None:
    result = _written(tmp_path)
    namespace = _namespace(tmp_path)
    assert {"emit": "plan_latest", "of": "plan_version", "ended": "keep"} in namespace[
        SNAPSHOTS_KEY
    ]
    assert not any(entry["emit"] == "plan_latest" for entry in namespace.get("lifetimes", []))
    state = _states(result)["/tables/plan_latest/datePairs/0/statistics/0"]
    assert state["state"] == "unused"
    assert "the history sets both dates" in state["note"]


def test_a_snapshot_pair_one_of_whose_dates_a_rule_sets_keeps_its_lifetime(
    tmp_path: Path,
) -> None:
    _written(tmp_path, SIGNED_BY_RULE)
    namespace = _namespace(tmp_path)
    assert {"emit": "plan_latest", "of": "plan_version", "ended": "keep"} in namespace[
        SNAPSHOTS_KEY
    ]
    (entry,) = [item for item in namespace["lifetimes"] if item["emit"] == "plan_latest"]
    assert (entry["start"], entry["end"]) == ("signed_on", "valid_from")


#: A check of the snapshot that orders the two dates its history sets.
ORDERED_BY_CHECK = """\
      invariants:
        - id: signed_before_valid
          check: "valid_from >= signed_on"
"""


def test_a_check_that_orders_two_dates_the_history_both_sets_writes_no_lifetime(
    tmp_path: Path,
) -> None:
    result = _written(tmp_path, ORDERED_BY_CHECK, pairs=False)
    namespace = _namespace(tmp_path)
    assert not any(entry["emit"] == "plan_latest" for entry in namespace.get("lifetimes", []))
    notes = {item["subject"]: item["assumption"] for item in result.artifacts["notes"]}
    assert "the history sets both dates" in notes["plan_latest.signed_before_valid"]
