# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The parents of a relationship counted by a column of the parent (ADR 0037 decision 7).

:func:`flux_engine.concordance.integrity.grouped_spread` counts, for each value of a parent column,
how many parents have each number of children, and
:func:`flux_engine.concordance.module_scores._run_groups` puts those counts into the groups of a
profile statistic: a category's own keys, or the bins of the profile's edges (a bin half open, the last
closed, a value outside every bin in none).
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict

import pytest

from flux_engine.concordance.integrity import ParentGroups, grouped_spread
from flux_engine.concordance.module_scores import _edge_units, _run_groups
from flux_engine.seam.keys import Relationship
from flux_stats.profile import BinGrouping

pytestmark = pytest.mark.unit

LINK = Relationship(
    name="account",
    source="invoice",
    target="account",
    from_columns=("account_id",),
    to_columns=("id",),
    cardinality="many_to_one",
    join_type="inner",
)


def test_parents_are_counted_by_children_within_each_value_of_the_column() -> None:
    parents = [[1, "a"], [2, "a"], [3, "b"], [4, "b"], [5, None], [None, "b"]]
    children = [[1], [1], [2], [3], [3], [3], [9], [None]]
    found = grouped_spread(
        LINK, ["account_id"], children, ["id", "segment"], parents, "segment", "STRING"
    )
    assert found.column == "segment" and found.declared == "STRING"
    assert found.spreads == {"a": {2: 1, 1: 1}, "b": {3: 1, 0: 1}}


def test_a_column_the_emit_lacks_is_refused_without_naming_a_value() -> None:
    with pytest.raises(ValueError) as caught:
        grouped_spread(LINK, ["account_id"], [[1]], ["id"], [[1]], "segment", "STRING")
    assert "account" in str(caught.value) and "1" not in str(caught.value)


class _Bins:
    group_by_bins = BinGrouping(column="n", bins=(Fraction(0), Fraction(10), Fraction(20)))


@pytest.mark.parametrize(
    "cell, bin_key",
    [
        ("0", "0"),
        ("9.99", "0"),
        ("10", "10"),
        ("19", "10"),
        ("20", "10"),
        ("-1", None),
        ("21", None),
    ],
)
def test_a_value_falls_in_the_profiles_bin_half_open_and_the_last_closed(
    cell: str, bin_key: Any
) -> None:
    parents = ParentGroups("n", "DECIMAL(10,2)", {cell: {1: 3}})
    found = _run_groups(parents, _Bins(), (Fraction(0), Fraction(2)))  # type: ignore[arg-type]
    assert (list(found) == [bin_key]) if bin_key is not None else found == {}


def test_values_of_one_bin_are_merged_and_counted_into_the_children_bins() -> None:
    parents = ParentGroups("n", "INT", {"1": {0: 2, 5: 1}, "2": {1: 4}, "15": {7: 1}})
    found: Dict[str, Any] = _run_groups(
        parents,
        _Bins(),  # type: ignore[arg-type]
        (Fraction(0), Fraction(2), Fraction(8)),
    )
    assert found == {"0": (6, 1), "10": (0, 1)}


@pytest.mark.parametrize(
    "cell, declared, expected",
    [
        ("12.50", "DECIMAL(10,2)", Fraction(25, 2)),
        (7, "INT", Fraction(7)),
        ("1970-01-02", "DATE", Fraction(86_400_000)),
        ("1970-01-01T00:00:01.5Z", "TIMESTAMP", Fraction(1500)),
        ("not a date", "DATE", None),
        ("x", "INT", None),
    ],
)
def test_a_cell_is_read_in_the_units_a_profile_spells_edges_in(
    cell: Any, declared: str, expected: Any
) -> None:
    assert _edge_units(cell, declared) == expected
