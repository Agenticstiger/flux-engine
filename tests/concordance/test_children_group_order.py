# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The groups of a children-per-parent row grouped by bins are named in the order of the bins.

SCORECARD-SPEC.md names such groups by ordinal in the order of the bins, as the rows of a property
are. The profile's keys are the bins' lower edges as text, which sort ``0``, ``200``, ``50``, and
the row used to number the groups in that order.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from flux_engine.concordance.integrity import LinkIntegrity, ParentGroups
from flux_engine.concordance.module_scores import grouped_children_row
from flux_engine.concordance.shares import Holds
from flux_stats.profile import SCHEMA_URL_1_4_0, parse_profile

pytestmark = pytest.mark.unit

EDGES = ["0", "50", "200", "500"]
BINS = ["0", "1", "3", "9"]


def _group(key: str, cells: List[int]) -> Dict[str, Any]:
    return {"group": key, "k": sum(cells), "binCounts": cells, "status": "F"}


def _profile(by_bins: bool) -> Any:
    keys = ["0", "50", "200"]
    cells = {"0": [5, 0, 0], "50": [0, 5, 0], "200": [0, 0, 5]}
    statistic: Dict[str, Any] = {
        "definition": "marginalHistogram/1",
        "bins": BINS,
        "sampleSize": {"min": 1, "max": 99},
        "disclosure": "none",
        # The order of the profile's own keys: text order, which is not the order of the bins.
        "groups": [_group(key, cells[key]) for key in sorted(keys)],
    }
    if by_bins:
        statistic["groupByBins"] = {"column": "balance", "bins": EDGES}
    else:
        statistic["groupBy"] = "balance"
    column = {"domain": "number"}
    balance = column if by_bins else {"domain": "category"}
    return parse_profile(
        {
            "_producer": "urn:example:test",
            "_schemaURL": SCHEMA_URL_1_4_0,
            "encoding": {"fractionalBits": 16, "rounding": "floor"},
            "disclosureRules": {"none": {}},
            "tables": {
                "account": {"columns": {"account_id": column, "balance": balance}},
                "invoice": {"columns": {"account_id": column}},
            },
            "relationships": [
                {
                    "parent": {"table": "account", "columns": ["account_id"]},
                    "child": {"table": "invoice", "columns": ["account_id"]},
                    "statistics": [statistic],
                }
            ],
        }
    )


def _link() -> LinkIntegrity:
    # Five accounts in each bin of balance, every one with the number of invoices the profile says.
    spreads = {10: {0: 5}, 60: {1: 5}, 300: {3: 5}}
    return LinkIntegrity(
        name="account",
        child="invoice",
        parent="account",
        from_columns=("account_id",),
        to_columns=("account_id",),
        cardinality="many_to_one",
        join_type="inner",
        found=Holds(1, 1),
        unique=None,
        nulls=None,
        one_to_one=None,
        parents_by_children={0: 5, 1: 5, 3: 5},
        parents_by_group=ParentGroups(column="balance", declared="INT", spreads=spreads),
    )


def test_groups_by_bins_are_numbered_in_the_order_of_the_bins() -> None:
    row = grouped_children_row(_link(), _profile(by_bins=True), None, "balance")
    assert row is not None and row.document is not None
    groups = row.document["groups"]
    assert [group["ordinal"] for group in groups] == [0, 1, 2]
    # Ordinal 1 is the bin starting at 50, whose parents all have one child.
    assert [group["run"]["binCounts"] for group in groups] == [[5, 0, 0], [0, 5, 0], [0, 0, 5]]
    assert [group["profile"]["binCounts"] for group in groups] == [[5, 0, 0], [0, 5, 0], [0, 0, 5]]


def test_the_outside_ordinal_follows_the_bin_order() -> None:
    link = _link()
    # The bin starting at 50 now holds parents with three children: it is the second bin, ordinal 1.
    moved = ParentGroups(
        column="balance", declared="INT", spreads={10: {0: 5}, 60: {3: 5}, 300: {3: 5}}
    )
    row = grouped_children_row(
        LinkIntegrity(**{**link.__dict__, "parents_by_group": moved}),
        _profile(by_bins=True),
        None,
        "balance",
    )
    assert row is not None
    assert row.groups_outside == (1,)
    assert [group["outcome"] for group in (row.document or {})["groups"]] == [
        "within",
        "outside",
        "within",
    ]


def test_groups_by_a_column_keep_the_profiles_order() -> None:
    link = _link()
    by_value = ParentGroups(
        column="balance", declared="INT", spreads={"0": {0: 5}, "200": {3: 5}, "50": {1: 5}}
    )
    row = grouped_children_row(
        LinkIntegrity(**{**link.__dict__, "parents_by_group": by_value}),
        _profile(by_bins=False),
        None,
        "balance",
    )
    assert row is not None and row.document is not None
    assert [group["profile"]["binCounts"] for group in row.document["groups"]] == [
        [5, 0, 0],
        [0, 0, 5],
        [0, 5, 0],
    ]
