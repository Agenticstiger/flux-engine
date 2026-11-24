# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The table of ``start`` inside an interval, cut on demand, is the table the bins give (ADR 0017).

A history that keeps to its parent's lifetime draws each entity's first start inside the entity's
own interval, so the planner needs the table of ``start`` inside a different interval for almost
every entity. :func:`~flux_engine.generator.history._start_table` cuts it from the bins in whole
numbers, reading a run of them by bisection, and
:func:`~flux_engine.generator.history._table_over` is the table built bin by bin with exact
fractions. What is pinned, over random histograms (weights of zero, a bin that is out of order,
either ``closed``) and random intervals:

* **Value for value.** For every draw, the cut table picks the value the full table picks; the
  draws tried include each threshold, the one before it and the one after.
* **The per-row oracle.** The first start of an entity whose interval is cut to its parent equals
  :func:`~flux_engine.generator.history.first_start_at` for that entity.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from typing import List, Optional, Tuple

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.generator import history
from flux_engine.generator.history import (
    _start_table,
    _table_over,
    first_start_at,
    unit_of,
)
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.history import Histogram, HistoryEntry

from .test_history_layout import BOUNDS, root_for

DAY0 = date(2023, 1, 1)


def _histogram(rng: random.Random, *, ordered: bool = True) -> Histogram:
    """A histogram of 1 to 12 bins over days after 2023-01-01, some of weight zero."""
    bins = rng.randint(1, 12)
    steps = [rng.randint(1, 40) for _ in range(bins)]
    edges = [0]
    for step in steps:
        edges.append(edges[-1] + step)
    if not ordered and bins > 2:
        edges[2] = edges[1]  # a bin with no value
    texts = tuple((DAY0 + timedelta(days=edge)).isoformat() for edge in edges)
    weights = tuple(rng.choice(["0", "1", "2", "0.5", "0.25", "3"]) for _ in range(bins))
    closed = rng.choice([None, "lower", "upper"])
    return Histogram(texts, weights, closed)


def _reference(histogram: Histogram, first: int, last: int) -> Optional[history._StartTable]:
    edges = [unit_of(text, LogicalKind.DATE) for text in histogram.edges]
    return _table_over(histogram, edges, first, last, "test")


def _draws(rng: random.Random, reference: history._StartTable) -> List[int]:
    top = (1 << 53) - 1
    found = {0, top}
    for threshold in reference.thresholds:
        found.update(
            value for value in (threshold - 1, threshold, threshold + 1) if 0 <= value <= top
        )
    found.update(rng.randint(0, top) for _ in range(40))
    return sorted(found)


@pytest.mark.parametrize("seed", range(12))
def test_the_cut_table_picks_what_the_full_table_picks(seed: int) -> None:
    rng = random.Random(seed)
    base = unit_of(DAY0.isoformat(), LogicalKind.DATE)
    for _ in range(60):
        histogram = _histogram(rng, ordered=rng.random() < 0.8)
        first = base + rng.randint(-20, 200)
        last = first + rng.randint(0, 260)
        reference = _reference(histogram, first, last)
        found = _start_table(histogram, LogicalKind.DATE, first, last, "test")
        assert (found is None) == (reference is None)
        if reference is None or found is None:
            continue
        draws = _draws(rng, reference)
        for draw in draws:
            assert found.value(draw) == reference.value(draw)
        assert found.values(np.array(draws, dtype=np.uint64)).tolist() == [
            reference.value(draw) for draw in draws
        ]


def test_a_timestamp_chain_cuts_the_same_table() -> None:
    rng = random.Random(7)
    histogram = Histogram(
        ("2023-01-01T00:00:00Z", "2023-02-01T00:00:00Z", "2023-04-01T00:00:00Z"), ("1", "3")
    )
    edges = [unit_of(text, LogicalKind.TIMESTAMP) for text in histogram.edges]
    for _ in range(80):
        first = edges[0] + rng.randint(-(10**9), 6 * 10**9)
        last = first + rng.randint(1, 8 * 10**9)
        reference = _table_over(histogram, edges, first, last, "test")
        found = _start_table(histogram, LogicalKind.TIMESTAMP, first, last, "test")
        assert (found is None) == (reference is None)
        if reference is None or found is None:
            continue
        for draw in _draws(rng, reference):
            assert found.value(draw) == reference.value(draw)


def _entry(histogram: Optional[Histogram]) -> HistoryEntry:
    return HistoryEntry(
        emit="thing",
        entities=1,
        length=Histogram(("30", "90"), ("1",)),
        until="2030-12-31",
        start=histogram,
    )


@pytest.mark.parametrize("with_bins", [True, False])
@pytest.mark.parametrize("seed", range(4))
def test_each_entitys_first_start_is_the_per_row_oracles(seed: int, with_bins: bool) -> None:
    rng = random.Random(100 + seed)
    held = _entry(_histogram(rng) if with_bins else None)
    root = root_for(3)
    c = 1
    window = history.window_start(BOUNDS)
    first, last, _ = history._first_start_interval(held, LogicalKind.DATE, window, c)
    count = 120
    shared = [(first + rng.randint(0, 400), rng.randint(0, 300)) for _ in range(3)]
    low_list: List[int] = []
    width_list: List[int] = []
    for _ in range(count):
        if rng.random() < 0.5:  # entities that share an interval, in groups past the small size
            at, width = rng.choice(shared)
        else:
            at, width = first + rng.randint(0, 400), rng.randint(0, 300)
        low_list.append(at)
        width_list.append(width)
    low = np.array(low_list, dtype=np.int64)
    high = np.minimum(low + np.array(width_list, dtype=np.int64), last)
    found = history._bounded_first_starts(root, "sim", "thing", held, LogicalKind.DATE, low, high)
    for entity in range(count):
        # The entity's parent ends ``c + 1`` days after the last start it leaves room for.
        parent: Tuple[Optional[int], Optional[int]] = (int(low[entity]), int(high[entity]) + c + 1)
        expected = first_start_at(
            root,
            "sim",
            "thing",
            held,
            LogicalKind.DATE,
            entity,
            c=c,
            time_bounds=BOUNDS,
            parent=parent,
        )
        assert int(found[entity]) == expected


def test_each_row_of_a_small_group_draws_what_the_column_draw_gives() -> None:
    root = root_for(4)
    key = history._first_start_key(root, "sim", "thing")
    rows = np.arange(50, dtype=np.int64)
    words = kernel2.column_words(key, rows, 0).tolist()
    for n in (1, 2, 3, 1000, 86_400_000, (1 << 41) + 7, (1 << 62) + 1):
        assert history._bounded_each(key, rows[5:40], words, n).tolist() == (
            kernel2.column_bounded(key, rows[5:40], 0, n).tolist()
        )
