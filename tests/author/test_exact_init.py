# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A small table takes exact allocation (ADR 0016 section 7, "Small tables").

Pinned here, through ``fluxsim init`` and a run:

* a table whose sampling widening at its rows exceeds the default tolerance gives every column
  ``allocation`` can read ``allocation: exact``: levels, bins with weights, and a column given an
  exact column;
* a large table takes none;
* a column ``allocation`` cannot read stays as it is: a key, a column given a column that is not
  exact, a column that is given another;
* a run of a 26-row table passes its own profile.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.author.columns import ColumnPlan, PropertyPlan
from flux_engine.author.exact import allot_small_tables, is_small
from flux_engine.seam.contract import LogicalKind
from flux_engine.stages.run import run

from .test_given import NAMESPACE, _init, _profile

pytestmark = pytest.mark.integration

_DISTANCE = {"type": "distance", "measure": "totalVariation", "max": "0.10", "sampling": True}


def _scaled_counts(counts: List[Any], total: int) -> List[Any]:
    """*counts* rescaled to sum to *total* by largest remainder."""
    old = sum(count for count in counts if count is not None)
    exact = [0 if count is None else count * total / old for count in counts]
    scaled = [math.floor(value) for value in exact]
    order = sorted(range(len(counts)), key=lambda i: exact[i] - scaled[i], reverse=True)
    for position in order[: total - sum(scaled)]:
        scaled[position] += 1
    return [None if count is None else scaled[i] for i, count in enumerate(counts)]


def _of_rows(rows: int, *, grouped_spend: bool = True) -> Dict[str, Any]:
    """The 1,000-customer profile of ``test_given``, rescaled to *rows* customers."""
    profile = copy.deepcopy(
        _profile(region_tier=True, spend_by_segment=grouped_spend, spend_by_region=grouped_spend)
    )
    profile["disclosureRules"]["acro-default"]["minimumCell"] = 1
    profile["disclosureRules"]["acro-default"]["zerosDisclosive"] = False
    table = profile["tables"]["customer"]
    table["rowCount"] = rows
    for column in table["columns"].values():
        count = column["metrics"].get("count")
        if count is not None:
            count["value"] = rows
        if "max" in column["metrics"]:
            column["metrics"]["max"]["value"] = str(rows)
        for statistic in column.get("statistics", ()):
            for group in statistic["groups"]:
                key = "levelCounts" if "levelCounts" in group else "binCounts"
                group["k"] = max(1, round(group["k"] * rows / 1000))
                group[key] = _scaled_counts(group[key], group["k"])
    return profile


def _columns(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    emits = yaml.safe_load(text)["extensions"][NAMESPACE]["emits"]
    columns: Dict[str, Any] = emits["customer"]["fidelity"]["columns"]
    return columns


def test_a_26_row_table_gives_every_column_allocation_can_read_exact_allocation(
    tmp_path: Path,
) -> None:
    _init(tmp_path, _of_rows(26))
    columns = _columns(tmp_path)
    exact = {name for name, declared in columns.items() if declared.get("allocation") == "exact"}
    # The profile draws segment given tier, tier given region and spend given segment: only
    # a column given a column that is given nothing is read, so segment and spend stay as they are.
    assert exact == {"region", "tier"}
    assert columns["tier"]["given"] == "region"  # a column given an exact column
    assert "allocation" not in columns["customer_id"]  # a key


def test_a_histogram_column_with_bin_weights_takes_exact_allocation_and_passes(
    tmp_path: Path,
) -> None:
    _init(tmp_path, _of_rows(26, grouped_spend=False))
    spend = _columns(tmp_path)["spend"]
    assert spend["allocation"] == "exact" and "binWeights" in spend
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stdout + outcome.stderr


def test_a_large_table_takes_no_exact_allocation(tmp_path: Path) -> None:
    _init(tmp_path, _of_rows(100000))
    assert all("allocation" not in declared for declared in _columns(tmp_path).values())


def test_init_plan_apply_verify_a_26_row_table_that_passes_its_own_profile(
    tmp_path: Path,
) -> None:
    _init(tmp_path, _of_rows(26))
    outcome = run(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "shop.profile.json",
        verify=True,
    )
    assert outcome.success, outcome.stdout + outcome.stderr


def _plan(name: str, declaration: Dict[str, Any], fixed: Any = None) -> ColumnPlan:
    plan = ColumnPlan(name=name, declaration=declaration, fixed=fixed)
    plan.properties = [
        PropertyPlan({"property": "levelHistogram", "tolerance": dict(_DISTANCE)}, [], True)
    ]
    return plan


def _expose(rows: int, *plans: ColumnPlan) -> Any:
    return SimpleNamespace(
        rows=rows,
        columns=list(plans),
        kinds={plan.name: LogicalKind.STRING for plan in plans},
        derives={},
        dependents=set(),
        copies={},
        aggregated=set(),
        guarded=set(),
    )


LEVELS = {"levels": ["a", "b"], "levelWeights": ["0.5", "0.5"]}


def test_the_widening_at_the_rows_decides_whether_a_table_is_small() -> None:
    columns = [_plan("c", dict(LEVELS))]
    assert is_small(26, columns)
    assert not is_small(100000, columns)


def test_a_column_given_a_column_that_is_not_exact_stays_as_it_is() -> None:
    parent = _plan("p", dict(LEVELS), fixed="set by a rule of the contract")
    child = _plan(
        "c", {**LEVELS, "given": "p", "levelWeightsGiven": {"a": ["0.5", "0.5"], "b": ["1", "0"]}}
    )
    expose = _expose(26, parent, child)
    allot_small_tables([expose])
    assert "allocation" not in parent.declaration
    assert "allocation" not in child.declaration


def test_a_column_given_a_column_that_is_given_another_stays_as_it_is() -> None:
    top = _plan("t", dict(LEVELS))
    middle = _plan(
        "m", {**LEVELS, "given": "t", "levelWeightsGiven": {"a": ["1", "0"], "b": ["0", "1"]}}
    )
    bottom = _plan(
        "b", {**LEVELS, "given": "m", "levelWeightsGiven": {"a": ["1", "0"], "b": ["0", "1"]}}
    )
    allot_small_tables([_expose(26, top, middle, bottom)])
    assert top.declaration["allocation"] == "exact"
    assert middle.declaration["allocation"] == "exact"
    assert "allocation" not in bottom.declaration


def test_a_column_a_rule_sets_or_with_no_weights_for_its_bins_stays_as_it_is() -> None:
    ruled = _plan("r", dict(LEVELS))
    unweighted = _plan("u", {"bins": ["0", "5", "10"]})
    expose = _expose(26, ruled, unweighted, _plan("ok", dict(LEVELS)))
    expose.derives["r"] = (0, "x")
    allot_small_tables([expose])
    assert "allocation" not in ruled.declaration
    assert "allocation" not in unweighted.declaration
    assert expose.columns[2].declaration["allocation"] == "exact"
