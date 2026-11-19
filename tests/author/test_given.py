# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Grouped statistics become ``given`` (ADR 0014 section 4).

Pinned here, through ``fluxsim init`` and a run:

* a level histogram grouped by a categorical column makes the column drawn
  given that one, its weights the group counts, a withheld cell filled from the
  group's ``k``, and a grouped property the run is scored on;
* of two groupings of one column, the forest keeps the one of more mutual
  information, and the ledger says why the other is unused;
* a column with nulls is never the column another is drawn given, and an
  optional categorical whose null count is withheld declares no ``nullFraction``,
  so it can be;
* a number column's grouped histogram becomes ``binWeightsGiven``;
* a level of the parent with no group draws the child's own shares, said so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.stages.run import run

pytestmark = pytest.mark.integration

NAMESPACE = "com.agentics.flux/1"
SAMPLE = {"min": 950, "max": 1050}


def _contract(extra: str = "") -> str:
    return f"""\
fluidVersion: "0.7.7"
kind: DataProduct
id: given.shop
name: Given shop
metadata:
  owner:
    team: shop-team
exposes:
  - exposeId: customer
    kind: table
    binding:
      platform: local
      format: parquet
      location: {{ path: ./out/customer.parquet }}
    contract:
      schema:
        - {{ name: customer_id, type: INT, required: true }}
        - {{ name: segment, type: "enum('A','B')", required: {extra or "true"} }}
        - {{ name: region, type: "enum('N','S')", required: true }}
        - {{ name: tier, type: "enum('gold','silver')", required: true }}
        - {{ name: spend, type: INT, required: true }}
    semantics:
      entities:
        - {{ name: customer, type: primary, expr: customer_id }}
"""


def _metric(definition: str, value: Any = None, status: str = "F") -> Dict[str, Any]:
    metric: Dict[str, Any] = {
        "definition": definition,
        "status": status,
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
    }
    if value is not None:
        metric["value"] = value
    return metric


def _statistic(definition: str, groups: List[Dict[str, Any]], **fields: Any) -> Dict[str, Any]:
    statistic: Dict[str, Any] = {
        "definition": definition,
        "sampleSize": SAMPLE,
        "disclosure": "acro-default",
        "groups": groups,
    }
    statistic.update(fields)
    return statistic


def _group(group: str, counts: List[Optional[int]], key: str = "levelCounts") -> Dict[str, Any]:
    statuses = ["F" if count is not None else "C" for count in counts]
    return {
        "group": group,
        "k": 0,
        key: counts,
        "cellStatus": statuses,
        "status": "F",
    }


def _with_k(group: Dict[str, Any], k: int) -> Dict[str, Any]:
    group["k"] = k
    return group


def _profile(
    *,
    tier_groups: Optional[Dict[str, Any]] = None,
    region_tier: bool = False,
    segment_nulls: Optional[Dict[str, Any]] = None,
    spend_by_segment: bool = False,
    spend_by_region: bool = False,
) -> Dict[str, Any]:
    """A profile of 1,000 customers: enough rows that a run's shares sit near the profile's."""
    segment_metrics: Dict[str, Any] = {"count": _metric("count/1", 1000)}
    if segment_nulls is not None:
        segment_metrics["nullCount"] = segment_nulls
    tier_statistics = [
        _statistic(
            "levelHistogram/1",
            [_with_k(_group("", [500, 500]), 1000)],
            levels=["gold", "silver"],
        )
    ]
    groups = tier_groups or {"A": ([540, 60], 600), "B": ([40, 360], 400)}
    tier_statistics.append(
        _statistic(
            "levelHistogram/1",
            [_with_k(_group(level, counts), k) for level, (counts, k) in groups.items()],
            levels=["gold", "silver"],
            groupBy="segment",
        )
    )
    if region_tier:
        tier_statistics.append(
            _statistic(
                "levelHistogram/1",
                [_with_k(_group("N", [260, 240]), 500), _with_k(_group("S", [240, 260]), 500)],
                levels=["gold", "silver"],
                groupBy="region",
            )
        )
    spend: Dict[str, Any] = {
        "domain": "number",
        "metrics": {"count": _metric("count/1", 1000)},
        "statistics": [
            _statistic(
                "marginalHistogram/1",
                [_with_k(_group("", [400, 600], "binCounts"), 1000)],
                bins=["0", "50", "100"],
            )
        ],
    }
    if spend_by_segment:
        spend["statistics"].append(
            _statistic(
                "marginalHistogram/1",
                [
                    _with_k(_group("A", [100, 500], "binCounts"), 600),
                    _with_k(_group("B", [300, 100], "binCounts"), 400),
                ],
                bins=["0", "50", "100"],
                groupBy="segment",
            )
        )
    if spend_by_region:
        spend["statistics"].append(
            _statistic(
                "marginalHistogram/1",
                [
                    _with_k(_group("N", [210, 290], "binCounts"), 500),
                    _with_k(_group("S", [190, 310], "binCounts"), 500),
                ],
                bins=["0", "50", "100"],
                groupBy="region",
            )
        )
    columns = {
        "customer_id": {
            "domain": "number",
            "metrics": {
                "count": _metric("count/1", 1000),
                "min": _metric("min/1", "1"),
                "max": _metric("max/1", "1000"),
            },
        },
        "segment": {
            "domain": "category",
            "metrics": segment_metrics,
            "statistics": [
                _statistic(
                    "levelHistogram/1", [_with_k(_group("", [600, 400]), 1000)], levels=["A", "B"]
                )
            ],
        },
        "region": {
            "domain": "category",
            "metrics": {"count": _metric("count/1", 1000)},
            "statistics": [
                _statistic(
                    "levelHistogram/1", [_with_k(_group("", [500, 500]), 1000)], levels=["N", "S"]
                )
            ],
        },
        "tier": {
            "domain": "category",
            "metrics": {"count": _metric("count/1", 1000)},
            "statistics": tier_statistics,
        },
        "spend": spend,
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
        "tables": {"customer": {"rowCount": 1000, "columns": columns}},
    }


def _init(tmp_path: Path, profile: Dict[str, Any], contract: str = "") -> Any:
    (tmp_path / "shop.fluid.yaml").write_text(contract or _contract(), encoding="utf-8")
    (tmp_path / "shop.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "shop.fluid.yaml"),
        profile=str(tmp_path / "shop.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _fidelity(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block["emits"]["customer"]["fidelity"]


def _states(result: Any) -> Dict[str, Dict[str, Any]]:
    return {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}


TIER_BY_SEGMENT = "/tables/customer/columns/tier/statistics/1"
TIER_BY_REGION = "/tables/customer/columns/tier/statistics/2"


def test_a_grouped_histogram_makes_the_column_drawn_given_its_group(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile())
    tier = _fidelity(tmp_path)["columns"]["tier"]
    assert tier["given"] == "segment"
    assert tier["levelWeightsGiven"] == {
        "A": {"gold": "0.9", "silver": "0.1"},
        "B": {"gold": "0.1", "silver": "0.9"},
    }
    assert "levels" not in tier
    assert _states(result)[TIER_BY_SEGMENT]["state"] == "used"
    properties = _fidelity(tmp_path)["properties"]
    grouped = [prop for prop in properties if prop.get("groupBy") == "segment"]
    assert grouped == [
        {
            "property": "levelHistogram",
            "column": "tier",
            "groupBy": "segment",
            "tolerance": {"type": "distance", "measure": "totalVariation", "max": "0.10"},
        }
    ]
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stderr
    assert "joint" in outcome.stdout


def test_a_withheld_cell_is_filled_from_its_group(tmp_path: Path) -> None:
    profile = _profile(tier_groups={"A": ([540, None], 600), "B": ([40, 360], 400)})
    _init(tmp_path, profile)
    tier = _fidelity(tmp_path)["columns"]["tier"]
    # Group A counts 600 records: the published 540 leave 60 for the withheld silver cell.
    assert tier["levelWeightsGiven"]["A"] == {"gold": "0.9", "silver": "0.1"}


def test_a_tree_keeps_every_link_that_makes_no_cycle(tmp_path: Path) -> None:
    """Chow and Liu: segment - tier - region is a tree, so both groupings of tier are read."""
    result = _init(tmp_path, _profile(region_tier=True))
    columns = _fidelity(tmp_path)["columns"]
    links = {name: column.get("given") for name, column in columns.items() if column.get("given")}
    assert sorted(links.items()) in (
        [("segment", "tier"), ("tier", "region")],
        [("region", "tier"), ("tier", "segment")],
    )
    assert _states(result)[TIER_BY_SEGMENT]["state"] == "used"
    assert _states(result)[TIER_BY_REGION]["state"] == "used"


def test_of_two_groupings_of_a_leaf_the_more_informative_one_is_kept(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile(spend_by_segment=True, spend_by_region=True))
    assert _fidelity(tmp_path)["columns"]["spend"]["given"] == "segment"
    region = _states(result)["/tables/customer/columns/spend/statistics/2"]
    assert region["state"] == "unused"
    assert "drawn given segment" in region["note"]


def test_a_column_with_nulls_is_never_the_one_another_is_given(tmp_path: Path) -> None:
    result = _init(
        tmp_path,
        _profile(segment_nulls=_metric("nullCount/1", 20)),
        contract=_contract("false"),
    )
    tier = _fidelity(tmp_path)["columns"]["tier"]
    assert "given" not in tier
    assert "FLX-3402" in _states(result)[TIER_BY_SEGMENT]["note"]


def test_an_optional_categorical_with_a_withheld_null_count_can_be_given(tmp_path: Path) -> None:
    _init(
        tmp_path,
        _profile(segment_nulls=_metric("nullCount/1", status="A")),
        contract=_contract("false"),
    )
    columns = _fidelity(tmp_path)["columns"]
    assert "nullFraction" not in columns["segment"]
    assert columns["tier"]["given"] == "segment"


def test_a_number_column_s_grouped_histogram_is_bin_weights_given(tmp_path: Path) -> None:
    _init(tmp_path, _profile(spend_by_segment=True))
    spend = _fidelity(tmp_path)["columns"]["spend"]
    assert spend["given"] == "segment"
    assert spend["bins"] == ["0", "50", "100"]
    assert spend["binWeightsGiven"] == {"A": ["0.166666667", "0.833333333"], "B": ["0.75", "0.25"]}
    assert "binWeights" not in spend


def test_a_parent_level_with_no_group_draws_the_child_s_own_shares(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile(tier_groups={"A": ([540, 60], 600)}))
    tier = _fidelity(tmp_path)["columns"]["tier"]
    assert tier["levelWeightsGiven"]["B"] == {"gold": "0.5", "silver": "0.5"}
    assert "whose levels B the profile publishes no group for" in result.stdout
    grouped = [prop for prop in _fidelity(tmp_path)["properties"] if prop.get("groupBy")]
    assert grouped == []  # a grouped property needs a group of every level


def _grouping(sizes: Dict[str, int]) -> Any:
    from flux_engine.author.given import _Table

    statistic = _statistic(
        "levelHistogram/1",
        [_with_k(_group(level, [1, 1]), size) for level, size in sizes.items()],
        levels=["gold", "silver"],
        groupBy="segment",
    )
    return _Table("segment", "tier", ("t",), statistic, False, ("gold", "silver"))


def _own_histogram(counts: List[int]) -> Dict[str, Any]:
    group = _with_k(_group("", list(counts)), sum(counts))
    return {
        "statistics": [_statistic("levelHistogram/1", [group], levels=["A", "B"])],
    }


def test_a_parent_histogram_the_group_sizes_hold_is_checked(tmp_path: Path) -> None:
    from flux_engine.author.columns import ColumnPlan
    from flux_engine.author.given import _check_marginal

    plan = ColumnPlan(name="segment")
    _check_marginal(plan, _own_histogram([600, 400]), _grouping({"A": 598, "B": 402}), ("c",), 6)
    assert [how for _, how in plan.reads] == [
        "checked: its counts are the sizes of the groups the column is drawn from"
    ]
    assert plan.reads[0][0] == ("c", "statistics", "0")
    assert plan.passed == []


def test_a_parent_histogram_the_group_sizes_contradict_stays_unused_with_the_reason(
    tmp_path: Path,
) -> None:
    from flux_engine.author.columns import ColumnPlan
    from flux_engine.author.given import _check_marginal

    plan = ColumnPlan(name="segment")
    _check_marginal(plan, _own_histogram([600, 400]), _grouping({"A": 500, "B": 500}), ("c",), 6)
    assert plan.reads == []
    assert "counts 600 of A" in plan.passed[0][1] and "has 500 records" in plan.passed[0][1]


SEGMENT_STATISTIC = "/tables/customer/columns/segment/statistics/0"


def test_a_column_drawn_from_a_table_grouped_by_it_checks_its_own_histogram(
    tmp_path: Path,
) -> None:
    # With the table of tier by region as well, the forest draws segment given tier from the
    # table of tier grouped by segment, and segment's own histogram is checked against the
    # sizes of that table's groups (600 and 400, as segment counts them).
    result = _init(tmp_path, _profile(region_tier=True))
    assert _fidelity(tmp_path)["columns"]["segment"]["given"] == "tier"
    state = _states(result)[SEGMENT_STATISTIC]
    assert state["state"] == "used"
    assert "checked: its counts are the sizes of the groups" in state["note"]


def test_a_column_s_histogram_the_groups_it_is_drawn_from_contradict_is_unused(
    tmp_path: Path,
) -> None:
    groups = {"A": ([450, 50], 500), "B": ([50, 450], 500)}
    result = _init(tmp_path, _profile(region_tier=True, tier_groups=groups))
    assert _fidelity(tmp_path)["columns"]["segment"]["given"] == "tier"
    state = _states(result)[SEGMENT_STATISTIC]
    assert state["state"] == "unused"
    assert "counts 600 of A" in state["note"] and "has 500 records" in state["note"]


def _table_of_counts(rows: Dict[str, List[int]]) -> Any:
    from fractions import Fraction

    from flux_engine.author.given import _Table

    table = _Table("segment", "tier", ("t",), {}, False, ("A", "B"))
    table.counts = {level: [Fraction(n) for n in row] for level, row in rows.items()}
    return table


def test_a_child_histogram_the_groups_add_up_to_is_checked() -> None:
    from flux_engine.author.columns import ColumnPlan
    from flux_engine.author.given import _check_implied

    plan = ColumnPlan(name="tier")
    table = _table_of_counts({"x": [300, 100], "y": [100, 500]})
    _check_implied(plan, _own_histogram([400, 600]), table, ("c",), 6)
    assert [how for _, how in plan.reads] == [
        "checked: its counts are what the groups the column is drawn from add up to"
    ]
    assert plan.passed == []


def test_a_child_histogram_the_groups_contradict_stays_unused_with_the_reason() -> None:
    from flux_engine.author.columns import ColumnPlan
    from flux_engine.author.given import _check_implied

    plan = ColumnPlan(name="tier")
    table = _table_of_counts({"x": [300, 100], "y": [100, 500]})
    _check_implied(plan, _own_histogram([700, 300]), table, ("c",), 6)
    assert plan.reads == []
    assert "counts 700 of A" in plan.passed[0][1] and "add up to 400" in plan.passed[0][1]
