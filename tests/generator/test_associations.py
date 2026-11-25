# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The pairing of an association table's two sides (ADR 0039), as pure functions.

* **Gale-Ryser.** :func:`gale_ryser_gap` is ``0`` exactly when a simple bipartite graph has the two
  degree sequences: a tight case (a complete graph), an infeasible one, zeros on either side.
* **Caps.** A degree above the other side's parents is cut to it, and the excess goes to the parents
  under their cap by the largest remainders; the sum is kept.
* **The layout.** Both sides hold exactly their degrees, no pair repeats when ``distinct``, the
  repair always ends (a dense table, a complete one, a whole Havel-Hakimi rebuild), and the same
  keys give the same pairs while another key gives others.
"""

from __future__ import annotations

import collections
from fractions import Fraction
from itertools import product
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import derive_root
from flux_engine.generator import associations
from flux_engine.generator.associations import (
    PairedSide,
    Pairing,
    PairKeys,
    capped,
    degree_distance,
    degree_overlap,
    gale_ryser_gap,
    pair_layout,
    realisable,
)
from flux_engine.seam.counts import ChildShape

pytestmark = pytest.mark.unit

KEYS = PairKeys(
    counts=(bytes(range(32)), bytes(range(32, 64))),
    stubs=bytes(range(64, 96)),
    switches=bytes(range(96, 128)),
)
OTHER = PairKeys(counts=KEYS.counts, stubs=bytes(range(128, 160)), switches=KEYS.switches)


def _simple(first: List[int], second: List[int]) -> bool:
    """Whether a simple bipartite graph has the two degree sequences, by brute force."""
    rows, columns = len(first), len(second)
    for cells in product((0, 1), repeat=rows * columns):
        matrix = np.array(cells).reshape(rows, columns)
        if matrix.sum(axis=1).tolist() == first and matrix.sum(axis=0).tolist() == second:
            return True
    return False


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ([3, 3], [2, 2, 2]),  # tight: the complete graph K(2,3)
        ([3, 3, 0], [3, 2, 1]),  # infeasible: three rows of one parent need three others
        ([2, 1, 1], [2, 1, 1]),
        ([0, 0, 0], [0, 0]),  # zero rows
        ([2, 0, 2], [1, 1, 2]),
        ([1, 1, 1, 1], [4, 0]),  # a degree above the other side's parents
        ([3, 1], [2, 2]),
    ],
)
def test_the_gap_is_zero_exactly_when_a_simple_graph_has_the_degrees(
    first: List[int], second: List[int]
) -> None:
    gap = gale_ryser_gap(np.array(first), np.array(second))
    assert (gap == 0) == _simple(first, second)


def test_the_tight_case_and_the_infeasible_case_by_value() -> None:
    assert gale_ryser_gap(np.array([3, 3]), np.array([2, 2, 2])) == 0
    assert gale_ryser_gap(np.array([3, 3, 0]), np.array([3, 2, 1])) == 1
    assert gale_ryser_gap(np.array([], dtype=np.int64), np.array([], dtype=np.int64)) == 0


def test_a_cap_moves_the_excess_by_the_largest_remainders_and_keeps_the_sum() -> None:
    found, excess = capped(np.array([9, 1, 2, 0, 3]), 4)
    assert excess == 5
    # The 5 rows go to the parents of 1, 2 and 3 rows by weight, round after round, none past 4;
    # the parent of none takes none.
    assert found.tolist() == [4, 3, 4, 0, 4]
    assert int(found.sum()) == 15
    even, moved = capped(np.array([0, 0, 6]), 2)
    assert (even.tolist(), moved) == ([2, 2, 2], 4)  # none under the cap has rows: all alike
    same, none = capped(np.array([1, 2]), 5)
    assert (same.tolist(), none) == ([1, 2], 0)


def test_an_infeasible_pair_of_sides_is_moved_until_it_holds() -> None:
    second, moved = realisable(np.array([3, 3, 0]), np.array([3, 2, 1]))
    assert gale_ryser_gap(np.array([3, 3, 0]), second) == 0
    assert (second.tolist(), moved) == ([2, 2, 2], 1)
    kept, none = realisable(np.array([3, 3]), np.array([2, 2, 2]))
    assert (kept.tolist(), none) == ([2, 2, 2], 0)


def _side(
    parents: Tuple[int, int],
    shapes: Tuple[Optional[ChildShape], Optional[ChildShape]] = (None, None),
    distinct: bool = True,
    minima: Tuple[int, int] = (0, 0),
) -> PairedSide:
    return PairedSide(("a", "b"), 0, shapes, minima, parents, distinct)


def _pairs(pairing: Pairing) -> List[Tuple[int, int]]:
    rows = np.arange(pairing.edges, dtype=np.int64)
    first = pairing.side(0).parents_of(rows).tolist()
    second = pairing.side(1).parents_of(rows).tolist()
    return list(zip(first, second, strict=True))


def _check_degrees(pairing: Pairing) -> None:
    pairs = _pairs(pairing)
    for index in (0, 1):
        held = collections.Counter(pair[index] for pair in pairs)
        expected = pairing.degrees[index].tolist()
        assert [held.get(parent, 0) for parent in range(len(expected))] == expected


SHAPE = ChildShape("bins", ("0", "1", "3", "6"), ("0.1", "0.5", "0.4"))


@pytest.mark.parametrize(
    ("parents", "edges"),
    [((40, 30), 120), ((12, 10), 100), ((6, 6), 36), ((50, 3), 140), ((7, 9), 0)],
)
def test_no_pair_repeats_and_both_sides_hold_their_degrees(
    parents: Tuple[int, int], edges: int
) -> None:
    pairing = pair_layout(KEYS, _side(parents, (SHAPE, None)), edges)
    pairs = _pairs(pairing)
    assert len(pairs) == edges and len(set(pairs)) == edges
    _check_degrees(pairing)
    if edges == parents[0] * parents[1]:
        assert set(pairs) == set(product(range(parents[0]), range(parents[1])))


def test_the_same_keys_give_the_same_pairs_and_another_key_others() -> None:
    side = _side((40, 30), (SHAPE, None))
    first, again = pair_layout(KEYS, side, 120), pair_layout(KEYS, side, 120)
    assert _pairs(first) == _pairs(again)
    assert first.report() == again.report()
    assert _pairs(pair_layout(OTHER, side, 120)) != _pairs(first)


def test_with_repeats_allowed_a_row_s_parents_come_from_its_index_alone() -> None:
    pairing = pair_layout(KEYS, _side((8, 5), distinct=False), 60)
    assert pairing.second is None and pairing.switches == 0
    rows = np.arange(60, dtype=np.int64)
    whole = pairing.side(1).parents_of(rows).tolist()
    assert [pairing.side(1).parent_of(row) for row in range(60)] == whole
    assert pairing.side(1).parents_of(rows[17:23]).tolist() == whole[17:23]
    _check_degrees(pairing)


def test_a_degree_above_the_other_side_s_parents_is_capped_and_recorded() -> None:
    heavy = ChildShape("levels", ("1", "20"), ("0.5", "0.5"))  # 20 rows each: above 6 parents
    pairing = pair_layout(KEYS, _side((10, 6), (heavy, None)), 50)
    assert int(pairing.degrees[0].max()) <= 6 and int(pairing.degrees[0].sum()) == 50
    assert pairing.moved[0] > 0 and pairing.report()["moved"]["caps"][0] == pairing.moved[0]
    assert len(set(_pairs(pairing))) == 50


def test_the_repair_switches_then_places_and_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    switched = pair_layout(KEYS, _side((40, 30), (SHAPE, None)), 120)
    assert switched.switches > 0 and switched.placed == 0 and not switched.rebuilt
    monkeypatch.setattr(associations, "SWITCH_ATTEMPTS", 0)
    placed = pair_layout(KEYS, _side((40, 30), (SHAPE, None)), 120)
    assert placed.switches == 0 and placed.placed > 0 and not placed.rebuilt
    assert len(set(_pairs(placed))) == 120
    _check_degrees(placed)


def test_a_dense_table_ends_with_no_pair_repeated() -> None:
    # 100 of the 120 pairs: the switches leave rows the Havel-Hakimi pass may not serve, and the
    # whole table is then laid out again; either way it ends with every pair once.
    dense = pair_layout(KEYS, _side((12, 10), (SHAPE, None)), 100)
    assert len(set(_pairs(dense))) == 100
    _check_degrees(dense)


def test_a_parent_the_havel_hakimi_pass_cannot_serve_rebuilds_the_whole_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(associations, "SWITCH_ATTEMPTS", 0)
    monkeypatch.setattr(associations, "_place", lambda repair: False)
    pairing = pair_layout(KEYS, _side((12, 10), (SHAPE, None)), 100)
    assert pairing.rebuilt and pairing.report()["repair"]["rebuilt"] is True
    assert len(set(_pairs(pairing))) == 100
    _check_degrees(pairing)


def test_more_rows_than_pairs_is_refused_and_allowed_with_repeats() -> None:
    with pytest.raises(FluxError) as many:
        pair_layout(KEYS, _side((4, 5)), 21)
    assert many.value.code.code == "FLX-3403" and "pairs" in str(many.value)
    assert len(_pairs(pair_layout(KEYS, _side((4, 5), distinct=False), 21))) == 21


def test_the_second_side_has_no_run_of_children() -> None:
    pairing = pair_layout(KEYS, _side((4, 5)), 10)
    with pytest.raises(FluxError) as caught:
        pairing.side(1).children_of(0)
    assert caught.value.code.code == "FLX-3403"
    assert len(pairing.side(0).children_of(0)) == int(pairing.degrees[0][0])


def test_the_distance_is_the_exact_total_variation_distance() -> None:
    shape = ChildShape("bins", ("0", "1", "3", "6"), ("0.1", "0.5", "0.4"))
    # Classes [0], [1, 2], [3, 6]: shares 0.2, 0.4, 0.3 against 0.1, 0.5, 0.4, and the 9, which
    # no class holds, a share of 0.1 of its own against nothing.
    degrees = [0, 0, 1, 2, 2, 2, 3, 6, 9, 4]
    assert degree_distance(degrees, shape) == Fraction(1, 5)
    assert degree_overlap(degrees, shape) == (80, 100)
    assert degree_distance(degrees, None) is None and degree_distance([], shape) is None


@pytest.mark.parametrize(
    ("degrees", "shape", "distance"),
    [
        ([20] * 10, ChildShape("bins", ("61", "70", "80"), ("0.5", "0.5")), Fraction(1)),
        ([5] * 4 + [50], ChildShape("levels", ("5",), ("1",)), Fraction(1, 5)),
        ([2, 4], ChildShape("levels", ("2", "5"), ("0.5", "0.5")), Fraction(1, 2)),
        ([0, 1], ChildShape("bins", ("1", "3"), ("1",)), Fraction(1, 2)),
    ],
)
def test_a_count_no_class_holds_adds_its_whole_share_to_the_distance(
    degrees: List[int], shape: ChildShape, distance: Fraction
) -> None:
    # Above the last class, a one-class shape, between two levels and below the first class.
    assert degree_distance(degrees, shape) == distance


def _neighbour_sets(pairing: Pairing) -> int:
    held: Dict[int, set] = {}
    for first, second in _pairs(pairing):
        held.setdefault(first, set()).add(second)
    return len({frozenset(found) for found in held.values()})


def test_a_rebuilt_dense_table_keeps_many_neighbour_sets() -> None:
    # 4,500 of the 6,000 pairs: the repair lays the whole table out again, and the keyed ties
    # give parents of as many rows different second-side parents instead of a few blocks.
    dense = pair_layout(KEYS, _side((200, 30)), 4500)
    assert dense.rebuilt and len(set(_pairs(dense))) == 4500
    assert _neighbour_sets(dense) >= 100
    _check_degrees(dense)


def test_a_rebuild_keeps_no_switch_or_placement_and_the_plan_says_so() -> None:
    dense = pair_layout(KEYS, _side((200, 30)), 4500)
    assert dense.report()["repair"] == {"switches": 0, "placed": 0, "rebuilt": True}
    side = PairedSide(("a", "b"), 0, (None, None), (0, 0), (200, 30), True)
    counted = SimpleNamespace(paired=side, child_rows=4500)
    plan = SimpleNamespace(
        relations=SimpleNamespace(drivers=(("a_id", SimpleNamespace(counted=counted)),)),
        sim_id="s",
        expose=SimpleNamespace(expose_id="link"),
        seam_digest="d",
    )
    root = derive_root(bytes(32), 7, seed_tree_version=kernel2.SEED_TREE_VERSION)
    (note,) = associations.association_notes([plan], root)
    assert note.startswith("link: association a, b: ") and "laid out again" in note
