# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An entity takes a version count its room fits (ADR 0017, the amendment of 2026-11-28).

The assignment is a pure function of the counts, each entity's capacity and a priority, and the
capacity is the versions an entity's own drawn lengths fit before its limit. Pinned here on small
hand-built cases, with exact integers:

* **The greedy.** The counts go out from the largest down, each to the entity with room for it
  that comes first in the permutation; a count nobody has room for goes to the entity with the
  most room left, a tie to the earlier in the permutation.
* **The capacity.** The most versions the drawn lengths fit before the limit, one under
  ``closeLast``, and one version for an entity with no room at all.
* **The layout.** The counts as a multiset are the counted layout's, whatever the rule, and the
  rank coupling is what an entry of the earlier era gives.
"""

from __future__ import annotations

import json
from collections import Counter
from heapq import heappop, heappush
from pathlib import Path
from typing import Any, List

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.generator import history
from flux_engine.generator.history_fit import assign_counts, capacities, priorities
from flux_engine.generator.paths import VERSIONS_FIT_PATH_SHAPE, VERSIONS_PATH_SHAPE
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import (
    VERSION_COUNTS_FIT,
    VERSION_COUNTS_RANK,
    Histogram,
    HistoryEntry,
)

from .test_history_layout import EMIT, SIM_ID, entry, layout_of, plans_of, root_for

VECTOR_FILE = Path(__file__).resolve().parents[1] / "vectors" / "kernel2-history-vectors.json"


def _assign(multiset: List[int], capacity: List[int], priority: List[int]) -> List[int]:
    return assign_counts(
        np.array(multiset, dtype=np.int64),
        np.array(capacity, dtype=np.int64),
        np.array(priority, dtype=np.int64),
    ).tolist()


def test_the_largest_count_goes_to_the_entity_with_room_whose_priority_is_first() -> None:
    # Entity 2 has the most room but comes last; 0 and 1 both fit a 3 and 0 comes first.
    assert _assign([3, 2, 1], [3, 3, 1], [0, 1, 2]) == [3, 2, 1]
    assert _assign([3, 2, 1], [3, 3, 1], [1, 0, 2]) == [2, 3, 1]


def test_a_count_goes_to_a_smaller_capacity_than_the_rank_coupling_would_give_it() -> None:
    # Capacities 5, 1, 4, 2 (entity 0 has the most room). The counts 4, 2, 2, 1 need no more
    # than 4: entity 0 and 2 can take the 4 and the first by priority does.
    assert _assign([4, 2, 2, 1], [5, 1, 4, 2], [3, 0, 1, 2]) == [2, 1, 4, 2]


def test_a_tie_in_capacity_goes_to_the_entity_earlier_in_the_permutation() -> None:
    assert _assign([2, 1, 1], [2, 2, 2], [2, 0, 1]) == [1, 2, 1]
    assert _assign([2, 1, 1], [2, 2, 2], [0, 2, 1]) == [2, 1, 1]


def test_a_count_no_entity_has_room_for_goes_to_the_most_capacity_left() -> None:
    # Every capacity is below the 5: it goes to capacity 3, then the 4 to capacity 2, and so on.
    assert _assign([5, 4, 1], [2, 3, 1], [0, 1, 2]) == [4, 5, 1]


def test_a_tie_among_those_with_the_most_capacity_left_goes_to_the_earlier_priority() -> None:
    assert _assign([5, 1], [2, 2], [1, 0]) == [1, 5]
    assert _assign([5, 1], [2, 2], [0, 1]) == [5, 1]


def test_a_count_above_every_capacity_and_ties_in_a_run() -> None:
    counts = _assign([9, 9, 1, 1, 1], [1, 1, 1, 1, 1], [4, 3, 2, 1, 0])
    # The two 9s go to the two earliest priorities, the entities that come first in the order.
    assert counts == [1, 1, 1, 9, 9]


def test_the_multiset_is_kept_and_no_entity_is_left_without_a_count() -> None:
    rng = np.random.default_rng(7)
    entities = 400
    multiset = rng.integers(1, 12, size=entities)
    capacity = rng.integers(1, 12, size=entities)
    priority = rng.permutation(entities)
    counts = np.array(_assign(multiset.tolist(), capacity.tolist(), priority.tolist()))
    assert Counter(counts.tolist()) == Counter(multiset.tolist())
    assert (counts >= 1).all()
    # An entity holds a count above its capacity only where no entity with room was left for it:
    # so no entity that fits its count is passed over for a count it also fits, in the order.
    over = counts > capacity
    assert int(over.sum()) < entities


def test_an_assignment_is_the_same_every_time_and_reads_no_float() -> None:
    capacity = np.array([3, 1, 2, 3, 2, 1, 3], dtype=np.int64)
    priority = np.array([6, 5, 4, 3, 2, 1, 0], dtype=np.int64)
    multiset = np.array([3, 3, 2, 2, 1, 1, 1], dtype=np.int64)
    first = assign_counts(multiset, capacity, priority)
    assert first.dtype == np.int64
    assert assign_counts(multiset[::-1], capacity, priority).tolist() == first.tolist()
    assert sorted(first.tolist()) == sorted(multiset.tolist())
    assert first.tolist() == [1, 1, 2, 3, 2, 1, 3]


def _heap_assign(multiset: List[int], capacity: List[int], priority: List[int]) -> List[int]:
    """The deal one count at a time, as the rule is worded: the reference the array version must equal."""
    entities = len(capacity)
    given = [0] * entities
    order = sorted(range(entities), key=lambda e: (-capacity[e], priority[e]))
    by_priority = {priority[e]: e for e in range(entities)}
    eligible: List[int] = []
    ahead = 0
    for value in sorted(multiset, reverse=True):
        while ahead < entities and capacity[order[ahead]] >= value:
            heappush(eligible, priority[order[ahead]])
            ahead += 1
        if eligible:
            entity = by_priority[heappop(eligible)]
        else:
            entity = order[ahead]
            ahead += 1
        given[entity] = value
    return given


def _same_as_the_heap(multiset: List[int], capacity: List[int], priority: List[int]) -> None:
    assert _assign(multiset, capacity, priority) == _heap_assign(multiset, capacity, priority)


@pytest.mark.parametrize("seed", range(300))
def test_the_array_assignment_equals_the_one_at_a_time_deal_on_random_cases(seed: int) -> None:
    rng = np.random.default_rng(seed)
    entities = int(rng.integers(1, 60))
    top = int(rng.integers(1, 14))
    # Counts that reach above every capacity in some seeds, ties in capacity in all of them.
    capacity = rng.integers(1, max(2, top - int(rng.integers(0, 3))), size=entities).tolist()
    multiset = rng.integers(1, top + 1, size=entities).tolist()
    priority = rng.permutation(entities).tolist()
    _same_as_the_heap(multiset, capacity, priority)


def test_the_array_assignment_equals_the_deal_on_the_edges() -> None:
    _same_as_the_heap([5], [1], [0])
    _same_as_the_heap([1], [9], [0])
    _same_as_the_heap([3, 3, 3, 3], [1, 1, 1, 1], [2, 0, 3, 1])
    _same_as_the_heap([4, 4, 1, 1], [4, 4, 4, 4], [3, 2, 1, 0])
    _same_as_the_heap([9, 9, 9, 2, 2, 1], [2, 2, 2, 2, 1, 1], [5, 4, 3, 2, 1, 0])
    _same_as_the_heap([2] * 6, [3, 1, 2, 2, 1, 3], [0, 5, 4, 3, 2, 1])


@pytest.mark.parametrize(
    "vector",
    [v for v in json.loads(VECTOR_FILE.read_text(encoding="utf-8"))["layouts"] if "fitKey" in v],
    ids=lambda vector: vector["name"],
)
def test_the_array_assignment_equals_the_deal_on_every_vector(vector: Any) -> None:
    counts = vector["versionCounts"]
    capacity = vector["fitCapacity"]
    priority = vector["fitPriority"]
    assert _assign(counts, capacity, priority) == counts
    assert _heap_assign(counts, capacity, priority) == counts


def test_no_entities_assign_nothing() -> None:
    empty = np.zeros(0, dtype=np.int64)
    assert assign_counts(empty, empty, empty).tolist() == []


class _Table:
    """A length table that reads the draw as the length itself: ``u`` lengths of one unit and more."""

    def values(self, draws: Any) -> Any:
        return (draws.astype(np.int64) % 5) + 1


def _lengths(key: Any, entities: int, steps: int) -> Any:
    zero = np.zeros(entities * steps, dtype=np.uint64)
    words = kernel2.philox_arrays(
        np.repeat(np.arange(entities, dtype=np.uint64), steps),
        np.tile(np.arange(steps, dtype=np.uint64), entities),
        zero,
        zero,
        key,
    )[0]
    return _Table().values(words >> np.uint64(11)).reshape(entities, steps)


@pytest.fixture(name="key")
def _key() -> Any:
    from flux_engine.generator.history import _change_key

    return _change_key(root_for(7), "sim", "tier_version")


def test_the_capacity_is_the_versions_the_drawn_lengths_fit_before_the_limit(key: Any) -> None:
    entities, largest = 50, 8
    lengths = _lengths(key, entities, largest - 1)
    rooms = np.arange(entities, dtype=np.int64) % 40
    found = capacities(key, _Table(), rooms, 0, largest)
    for entity in range(entities):
        fitted = 0
        for versions in range(1, largest):
            if int(lengths[entity, :versions].sum()) <= int(rooms[entity]) - 1:
                fitted = versions
        assert found[entity] == 1 + fitted


def test_a_closed_last_version_takes_one_unit_of_the_room(key: Any) -> None:
    entities, largest = 50, 8
    lengths = _lengths(key, entities, largest - 1)
    rooms = np.full(entities, 20, dtype=np.int64)
    found = capacities(key, _Table(), rooms, 1, largest)
    for entity in range(entities):
        fitted = sum(
            1
            for versions in range(1, largest)
            if int(lengths[entity, :versions].sum()) <= 20 - 1 - 1
        )
        assert found[entity] == 1 + fitted


def test_an_entity_with_no_room_and_a_chain_of_one_have_a_capacity_of_one(key: Any) -> None:
    assert capacities(key, _Table(), np.array([0, 1, -3], dtype=np.int64), 0, 6).tolist() == [
        1,
        1,
        1,
    ]
    assert capacities(key, _Table(), np.array([9, 9], dtype=np.int64), 0, 1).tolist() == [1, 1]
    assert capacities(key, _Table(), np.zeros(0, dtype=np.int64), 0, 6).tolist() == []


def test_a_huge_length_does_not_overflow_the_sums(key: Any) -> None:
    class Huge:
        def values(self, draws: Any) -> Any:
            return np.full(draws.shape[0], (1 << 62) - 1, dtype=np.int64)

    found = capacities(key, Huge(), np.full(4, 10, dtype=np.int64), 0, 9)
    assert found.tolist() == [1, 1, 1, 1]


def test_the_priority_is_a_permutation_under_its_own_key() -> None:
    root = root_for(3)
    fit, versions = history.fit_key(root, "sim", "x"), history.versions_key(root, "sim", "x")
    assert fit != versions and len(fit) == 32
    found = priorities(fit, 37)
    assert sorted(found.tolist()) == list(range(37))
    assert found.tolist() != priorities(versions, 37).tolist()


def test_the_fit_path_matches_no_other_shape_of_a_history() -> None:
    from flux_engine.generator import paths

    path = paths.versions_fit_path("sim", "x")
    assert path == ("sim", "sim", "gen", "x", "versions", "fit")
    assert len(path) == len(VERSIONS_FIT_PATH_SHAPE.pattern) == 6
    assert paths.versions_path("sim", "x") == path[:5]
    others = [
        shape
        for name, shape in vars(paths).items()
        if name.endswith("_PATH_SHAPE") and shape is not VERSIONS_FIT_PATH_SHAPE
    ]
    # No other shape has the literals "versions" and "fit" at the fifth and sixth parts.
    assert not [shape for shape in others if tuple(shape.pattern[4:6]) == ("versions", "fit")]
    assert len(VERSIONS_PATH_SHAPE.pattern) == 5


def _overrun(layout: Any, history_entry: HistoryEntry) -> int:
    """How many entities have drawn lengths that overrun their room, so that the chain is compressed."""
    from flux_engine.generator.history import drawn_length
    from flux_engine.seam.contract import LogicalKind

    found = 0
    for entity in range(layout.entities):
        count = int(layout.versions.counts[entity])
        lengths = sum(
            drawn_length(root_for(), SIM_ID, EMIT, history_entry, LogicalKind.DATE, entity, version)
            for version in range(count - 1)
        )
        found += lengths > int(layout.rooms[entity]) - 1
    return found


def test_the_fit_keeps_the_multiset_and_compresses_fewer_chains_than_the_rank_coupling() -> None:
    # Long lengths against a three-year window: the entities that start late have room for a
    # short chain only, and the rank coupling gives the longest chains to the earliest starts.
    bins = ChildShape("bins", ("1", "2", "4", "9"), ("0.45", "0.35", "0.20"))
    length = Histogram(("30", "120", "400"), ("0.6", "0.4"))
    fit = entry(versions=bins, length=length)
    rank = entry(versions=bins, length=length, version_counts=VERSION_COUNTS_RANK)
    found = {}
    for name, held in (("fit", fit), ("rank", rank)):
        plans = plans_of(history_entry=held, rows=400, entities=100)
        found[name], _, _ = layout_of(plans)
    assert sorted(found["fit"].versions.counts.tolist()) == sorted(
        found["rank"].versions.counts.tolist()
    )
    assert found["fit"].versions.counts.tolist() != found["rank"].versions.counts.tolist()
    assert found["fit"].c == found["rank"].c
    assert int(found["fit"].versions.counts.sum()) == 400
    assert _overrun(found["fit"], fit) < _overrun(found["rank"], rank)


def test_the_fit_does_not_move_the_first_starts() -> None:
    bins = ChildShape("bins", ("1", "2", "4", "9"), ("0.45", "0.35", "0.20"))
    fit = entry(versions=bins)
    rank = entry(versions=bins, version_counts=VERSION_COUNTS_RANK)
    fitted, _, _ = layout_of(plans_of(history_entry=fit, rows=40, entities=10))
    ranked, _, _ = layout_of(plans_of(history_entry=rank, rows=40, entities=10))
    assert fitted.first_starts.tolist() == ranked.first_starts.tolist()


def test_an_entry_says_which_era_it_is_of() -> None:
    from dataclasses import replace

    from flux_engine.generator.history import HistoryPick
    from flux_engine.seam.contract import LogicalKind

    fitted: HistoryEntry = entry()
    assert fitted.version_counts == VERSION_COUNTS_FIT
    ranked = replace(fitted, version_counts=VERSION_COUNTS_RANK)
    assert ranked != fitted and hash(ranked) != hash(fitted)

    def document(of: HistoryEntry) -> Any:
        return HistoryPick(of, LogicalKind.DATE, 24, 8, 0).as_document()

    # The document a digest is taken over says it only for a shaped entry of the fit.
    assert document(fitted)["versionCounts"] == VERSION_COUNTS_FIT
    assert "versionCounts" not in document(ranked)
    assert "versionCounts" not in document(replace(fitted, versions=None))
    assert document(fitted) != document(ranked)


# -- counts that follow the room ----------------------------------------------------------------

#: The days a version lasts in the source below, drawn evenly inside these bins.
SOURCE_LENGTH = Histogram(("30", "60", "90", "121"), ("1", "1", "1"))


def _spearman(first: Any, second: Any) -> float:
    def ranks(values: Any) -> Any:
        _, inverse, counts = np.unique(np.asarray(values), return_inverse=True, return_counts=True)
        return (np.cumsum(counts) - (counts - 1) / 2.0)[inverse]

    one, two = ranks(first), ranks(second)
    one, two = one - one.mean(), two - two.mean()
    return float((one * two).sum() / np.sqrt((one * one).sum() * (two * two).sum()))


def _censored_source(entities: int, span: int, seed: int) -> Any:
    """Each entity's first start and version count in a table cut at a date: long chains stop at it."""
    draw = np.random.default_rng(seed)
    lengths = np.concatenate([np.arange(30, 60), np.arange(60, 90), np.arange(90, 121)])
    starts = draw.integers(0, span - 12, entities)
    counts = []
    for start in starts.tolist():
        at, held = start, 1
        while draw.random() > 0.02:
            at += int(draw.choice(lengths))
            if at >= span:
                break
            held += 1
        counts.append(held)
    return starts, counts


def test_counts_that_follow_the_room_keep_a_strongly_negative_correlation_near_the_sources() -> (
    None
):
    from flux_engine.generator.history import plan_history
    from flux_engine.seam.contract import LogicalKind

    entities, span = 1500, 1096
    starts, counts = _censored_source(entities, span, 5)
    source = _spearman(starts, counts)
    assert source < -0.5
    levels = sorted(set(counts))
    shape = ChildShape(
        "levels",
        tuple(str(level) for level in levels),
        tuple(f"{counts.count(level) / entities:.6f}" for level in levels[:-1])
        + (f"{1 - sum(round(counts.count(level) / entities, 6) for level in levels[:-1]):.6f}",),
    )
    found = {}
    for name, held in (("fit", VERSION_COUNTS_FIT), ("rank", VERSION_COUNTS_RANK)):
        planned = plan_history(
            root_for(),
            SIM_ID,
            EMIT,
            entry(
                entities=entities,
                parent=None,
                versions=shape,
                start=Histogram(("2019-01-01", "2021-12-31"), ("1",)),
                length=SOURCE_LENGTH,
                until="2021-12-31",
                version_counts=held,
            ),
            kind=LogicalKind.DATE,
            rows=sum(counts),
            entities=entities,
            time_bounds={"start": "2019-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"},
        )
        found[name] = _spearman(planned.first_starts, planned.versions.counts)
    assert found["fit"] < -0.5
    assert abs(found["fit"] - source) < 0.15
    assert abs(found["fit"] - source) <= abs(found["rank"] - source)
