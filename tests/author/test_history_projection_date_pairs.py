# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A date pair never gives a second source to a column a history's projection entry sets.

Pinned here, through ``run_init`` and the plan stage, on the tier history of a gym's members, with
a table of tier changes (an ``events`` entry) and a table of monthly tiers (a ``periods`` entry):

* a relationship's date pair whose child column is the date column of an ``events`` entry, or the
  period column of a ``periods`` entry, is recorded unused, with the entry named as the reason;
* the projection entry is written, no ``within`` entry or lifetime sets that column, and the
  written bundle plans;
* the same pair on a column the projection leaves alone is still a ``within`` entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY
from flux_engine.stages.plan import plan
from tests.cli.test_init_dates import HOLDS, _link_pair, _pair
from tests.cli.test_init_dates import _levels as _order

from .test_history_date_pairs import TIERS, _profile
from .test_history_init import NAMESPACE, _metrics, _ms
from .test_history_init import _levels as _categories

pytestmark = pytest.mark.integration

#: ``TIERS`` with a join date on the member.
MEMBERS = TIERS.replace(
    "        - { name: member_id, type: INT, required: true }\n"
    "    semantics:\n"
    "      entities:\n"
    "        - { name: member, type: primary, expr: member_id }\n",
    "        - { name: member_id, type: INT, required: true }\n"
    "        - { name: joined_on, type: DATE, required: false }\n"
    "    semantics:\n"
    "      entities:\n"
    "        - { name: member, type: primary, expr: member_id }\n",
)

#: A table of tier changes: each row changes the tier of a pass, and refers to its member by the holder column.
CHANGES = """\
  - exposeId: tier_change
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/tier_change.parquet }
    contract:
      schema:
        - { name: event_id, type: INT, required: true }
        - { name: pass_id, type: INT, required: true }
        - { name: holder_id, type: INT, required: true }
        - { name: op, type: "enum('c','u','d')", required: true }
        - { name: recorded_on, type: DATE, required: true }
        - { name: reviewed_on, type: DATE, required: false }
    semantics:
      defaultAggTimeDimension: recorded_on
      entities:
        - { name: event, type: primary, expr: event_id }
      dimensions:
        - { name: op, type: categorical }
        - { name: recorded_on, type: time }
        - { name: reviewed_on, type: time }
      relationships:
        - name: pass
          to: tier_version
          fromColumns: [pass_id]
          toColumns: [pass_id]
          cardinality: many_to_one
        - name: member
          to: gym_member
          fromColumns: [holder_id]
          toColumns: [member_id]
          cardinality: many_to_one
"""

#: A table of monthly tiers: each row holds the tier a pass had in a month, and refers to its member by the holder column.
MONTHS = """\
  - exposeId: tier_month
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/tier_month.parquet }
    contract:
      schema:
        - { name: period_id, type: INT, required: true }
        - { name: pass_id, type: INT, required: true }
        - { name: holder_id, type: INT, required: true }
        - { name: month, type: DATE, required: true }
        - { name: closed_on, type: DATE, required: false }
    semantics:
      entities:
        - { name: period, type: primary, expr: period_id }
      dimensions:
        - { name: month, type: time, typeParams: { timeGranularity: month } }
        - { name: closed_on, type: time }
      relationships:
        - name: pass
          to: tier_version
          fromColumns: [pass_id]
          toColumns: [pass_id]
          cardinality: many_to_one
        - name: member
          to: gym_member
          fromColumns: [holder_id]
          toColumns: [member_id]
          cardinality: many_to_one
"""

#: Every row of a projected table follows the member's join date.
AFTER = _order(after=300, before=0, same=100)


def _number(count: int) -> Dict[str, Any]:
    return {"domain": "number", "metrics": _metrics("1", str(count), count)}


def _stamp(low: str, high: str, count: int) -> Dict[str, Any]:
    return {"domain": "timestamp", "metrics": _metrics(low, high, count)}


def _table(count: int, **columns: Dict[str, Any]) -> Dict[str, Any]:
    return {"rowCount": count, "columns": columns}


def _with_tables(child: str, date: str, key: str = "holder_id") -> Dict[str, Any]:
    """The tier profile, a join date on the member, the table *child*, and a pair on its *date*.

    The child refers to its member by the column *key*.
    """
    found = _profile()
    found["tables"]["gym_member"]["columns"]["joined_on"] = _stamp(
        _ms(2024, 1, 1), _ms(2024, 12, 1), 1000
    )
    if child == "tier_change":
        found["tables"][child] = _table(
            3000,
            event_id=_number(3000),
            pass_id=_number(1000),
            **{key: _number(1000)},
            op={
                "domain": "category",
                "statistics": [_categories(["c", "d", "u"], [1000, 200, 1800], 3000)],
            },
            recorded_on=_stamp(_ms(2025, 1, 1), _ms(2025, 9, 1), 3000),
            reviewed_on=_stamp(_ms(2025, 2, 1), _ms(2025, 10, 1), 3000),
        )
    else:
        found["tables"][child] = _table(
            4000,
            period_id=_number(4000),
            pass_id=_number(1000),
            **{key: _number(1000)},
            month=_stamp(_ms(2025, 1, 1), _ms(2025, 9, 1), 4000),
            closed_on=_stamp(_ms(2025, 2, 1), _ms(2025, 10, 1), 4000),
        )
    found["relationships"] = [
        {
            "parent": {"table": "gym_member", "columns": ["member_id"]},
            "child": {"table": child, "columns": [key]},
            "statistics": [],
            "dates": [_link_pair("joined_on", date, AFTER)],
        }
    ]
    return found


def _written(tmp_path: Path, contract: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    (tmp_path / "tiers.fluid.yml").write_text(contract, encoding="utf-8")
    (tmp_path / "tiers.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "tiers.fluid.yml"),
        profile=str(tmp_path / "tiers.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block


def _reasons(block: Dict[str, Any], pointer: str) -> list[str]:
    return [
        item["reason"]
        for item in block["unused"]
        if item["statistic"]["pointer"].startswith(pointer)
    ]


def _planned(tmp_path: Path) -> None:
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "tiers.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr


def _sets_nothing(block: Dict[str, Any], emit: str, column: str) -> None:
    """No ``within`` entry and no lifetime of *emit* sets *column*."""
    for entry in block.get("within", []):
        assert (entry["emit"], entry["date"]) != (emit, column), entry
    for entry in block.get("lifetimes", []):
        assert (entry["emit"], entry.get("end")) != (emit, column), entry


def test_a_relationship_date_pair_on_the_date_column_of_an_events_entry_is_unused(
    tmp_path: Path,
) -> None:
    block = _written(tmp_path, MEMBERS + CHANGES, _with_tables("tier_change", "recorded_on"))
    _planned(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert [(entry["emit"], entry["date"]) for entry in block[EVENTS_KEY]] == [
        ("tier_change", "recorded_on")
    ]
    _sets_nothing(block, "tier_change", "recorded_on")
    reasons = _reasons(block, "/relationships/0/dates/0")
    assert reasons
    assert all("the history's events entry sets 'recorded_on'" in reason for reason in reasons)


def test_a_relationship_date_pair_on_the_period_column_of_a_periods_entry_is_unused(
    tmp_path: Path,
) -> None:
    block = _written(tmp_path, MEMBERS + MONTHS, _with_tables("tier_month", "month"))
    _planned(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert [(entry["emit"], entry["column"]) for entry in block[PERIODS_KEY]] == [
        ("tier_month", "month")
    ]
    _sets_nothing(block, "tier_month", "month")
    reasons = _reasons(block, "/relationships/0/dates/0")
    assert reasons
    assert all("the history's periods entry sets 'month'" in reason for reason in reasons)


def test_a_relationship_date_pair_on_a_date_the_projection_leaves_alone_is_still_declared(
    tmp_path: Path,
) -> None:
    block = _written(tmp_path, MEMBERS + CHANGES, _with_tables("tier_change", "reviewed_on"))
    assert [entry["emit"] for entry in block[EVENTS_KEY]] == ["tier_change"]
    assert [(entry["emit"], entry["relationship"], entry["date"]) for entry in block["within"]] == [
        ("tier_change", "member", "reviewed_on")
    ]
    assert _reasons(block, "/relationships/0/dates/0") == []
    _planned(tmp_path)


def test_a_table_date_pair_on_the_date_column_of_an_events_entry_is_unused(
    tmp_path: Path,
) -> None:
    profile = _with_tables("tier_change", "reviewed_on")
    profile["tables"]["tier_change"]["datePairs"] = [_pair("recorded_on", "reviewed_on", HOLDS)]
    profile["relationships"] = []
    block = _written(tmp_path, MEMBERS + CHANGES, profile)
    _planned(tmp_path)
    assert [entry["emit"] for entry in block[EVENTS_KEY]] == ["tier_change"]
    assert "lifetimes" not in block and "within" not in block
    reasons = _reasons(block, "/tables/tier_change/datePairs/0")
    assert reasons
    assert all("the history's events entry sets 'recorded_on'" in reason for reason in reasons)


# -- a relationship whose foreign key a projection entry sets ----------------------------------------

#: The same tables, each referring to its member by ``member_id``, which the history holds too: the
#: entry then sets the foreign key with the history's value. Each case is (the table, its contract,
#: its entry, a date of it the entry leaves alone).
SHARED_KEY = {
    "events": ("tier_change", CHANGES, "events", "reviewed_on"),
    "periods": ("tier_month", MONTHS, "periods", "closed_on"),
}


@pytest.mark.parametrize("case", SHARED_KEY)
def test_a_relationship_date_pair_through_a_foreign_key_a_projection_sets_is_unused(
    tmp_path: Path, case: str
) -> None:
    child, tables, entry, date = SHARED_KEY[case]
    contract = MEMBERS + tables.replace("holder_id", "member_id")
    block = _written(tmp_path, contract, _with_tables(child, date, "member_id"))
    _planned(tmp_path)
    assert [item["emit"] for item in block[EVENTS_KEY if entry == "events" else PERIODS_KEY]] == [
        child
    ]
    # No entry dates the child through the relationship, and the member holds no start-only lifetime.
    assert "within" not in block and "lifetimes" not in block
    reasons = _reasons(block, "/relationships/0/dates/0")
    assert reasons
    assert all(
        f"the history's {entry} entry sets its foreign key 'member_id'" in r for r in reasons
    )
