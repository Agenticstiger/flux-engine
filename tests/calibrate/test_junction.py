# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A junction tree over clique marginals: the order columns are drawn in, and what is refused."""

from __future__ import annotations

import itertools
from decimal import Decimal
from typing import List, Sequence, Tuple

import pytest

from flux_engine.calibrate.junction import JunctionStep, junction_tree
from flux_engine.calibrate.structure import spanning_forest
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit

ORDER = ["a", "b", "c", "d", "e", "f"]


def _drawn(steps: Sequence[JunctionStep]) -> List[str]:
    return [name for step in steps for name in step.new]


def test_a_chain_of_pairs_is_the_chow_liu_forest() -> None:
    cliques = [("c", "d"), ("a", "b"), ("b", "c")]
    steps = junction_tree(cliques, order=ORDER)
    # Every clique has two columns, so the root is the first by name: (a, b).
    assert [(s.clique, s.parent, s.separator, s.new) for s in steps] == [
        (1, None, (), ("a", "b")),
        (2, 1, ("b",), ("c",)),
        (0, 2, ("c",), ("d",)),
    ]
    # The same edges the Chow-Liu forest keeps over these pairs, whatever it weighs them by.
    forest = spanning_forest(
        [(Decimal(1), x, y) for x, y in cliques], categorical={"a", "b", "c", "d"}
    )
    assert {frozenset((e.parent, e.child)) for e in forest} == {frozenset(c) for c in cliques}


def test_the_root_is_the_largest_clique_and_columns_are_drawn_given_their_separator() -> None:
    steps = junction_tree([("d", "e"), ("b", "c", "d"), ("a", "b", "c")], order=ORDER)
    assert [(s.clique, s.parent, s.separator, s.new) for s in steps] == [
        (2, None, (), ("a", "b", "c")),
        (1, 2, ("b", "c"), ("d",)),
        (0, 1, ("d",), ("e",)),
    ]
    assert steps[1].parents_of(0, ORDER) == ("b", "c")
    assert steps[0].parents_of(2, ORDER) == ("a", "b")
    assert steps[0].parents_of(0, ORDER) == ()


def test_new_columns_follow_the_declared_order_not_the_order_a_clique_lists_them() -> None:
    steps = junction_tree([("c", "a", "b")], order=ORDER)
    assert steps[0].new == ("a", "b", "c")
    steps = junction_tree([("c", "a", "b")], order=["c", "b", "a"])
    assert steps[0].new == ("c", "b", "a")


def test_equal_cliques_are_told_apart_by_their_columns_and_not_by_where_they_were_written() -> None:
    cliques: List[Tuple[str, ...]] = [("a", "b"), ("b", "c"), ("c", "d"), ("b", "e")]
    reference = None
    for permutation in itertools.permutations(range(len(cliques))):
        written = [cliques[i] for i in permutation]
        steps = junction_tree(written, order=ORDER)
        shape = [
            (written[s.clique], None if s.parent is None else written[s.parent], s.new)
            for s in steps
        ]
        if reference is None:
            reference = shape
        assert shape == reference


def test_cliques_that_share_nothing_are_two_trees_each_with_its_own_root() -> None:
    steps = junction_tree([("c", "d"), ("a", "b")], order=ORDER)
    assert [(s.clique, s.parent, s.new) for s in steps] == [
        (1, None, ("a", "b")),
        (0, None, ("c", "d")),
    ]


def test_a_clique_inside_another_hangs_off_it() -> None:
    steps = junction_tree([("a", "b"), ("a", "b", "c")], order=ORDER)
    assert [(s.clique, s.parent, s.new) for s in steps] == [(1, None, ("a", "b", "c")), (0, 1, ())]


def test_cliques_around_a_cycle_have_no_junction_tree_and_the_refusal_names_them() -> None:
    with pytest.raises(FluxError) as refused:
        junction_tree(
            [("a", "b"), ("b", "c"), ("a", "c")],
            order=ORDER,
            names=["/tables/t/cliques/0", "/tables/t/cliques/1", "/tables/t/cliques/2"],
        )
    assert refused.value.code.code == "FLX-3402"
    message = str(refused.value)
    assert "running intersection property" in message
    assert "/tables/t/cliques/" in message


def test_a_column_that_two_cliques_hold_through_a_clique_that_does_not_is_refused() -> None:
    # a-b, b-c, c-d and a-d around a square: the tree is a path, and a sits at both ends.
    with pytest.raises(FluxError, match="column '[a-d]'"):
        junction_tree([("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")], order=ORDER)
