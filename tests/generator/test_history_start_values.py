# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A start table's array form gives the values its scalar form gives, on both of its paths.

:meth:`~flux_engine.generator.history._StartTable.values` is every history's first start and, under the
fit, every length. It does the integer arithmetic in int64 when no product of a position in a bin and
the bin's values reaches ``2**63``, and in Python integers otherwise; each must be
:meth:`~flux_engine.generator.history._StartTable.value` for every draw.
"""

from __future__ import annotations

import random
from typing import List

import numpy as np
import pytest

from flux_engine.generator.history import _StartTable

pytestmark = pytest.mark.unit

TOP = 1 << 53
CHOOSER = random.Random(2026_11_28)


def _table(bins: int, widest_bin: int, values: int) -> _StartTable:
    """A table of *bins* bins that tile ``[0, 2**53)``, none wider than *widest_bin*, of up to *values* values."""
    cuts = sorted(CHOOSER.sample(range(1, TOP), bins - 1)) if bins > 1 else []
    thresholds = [*cuts, TOP]
    # No bin wider than the limit: split any that is.
    held: List[int] = []
    before = 0
    for edge in thresholds:
        while edge - before > widest_bin:
            before += widest_bin
            held.append(before)
        held.append(edge)
        before = edge
    counts = tuple(CHOOSER.randrange(1, values + 1) for _ in held)
    firsts = tuple(CHOOSER.randrange(-(1 << 30), 1 << 30) for _ in held)
    return _StartTable(tuple(held), firsts, counts)


def _draws(table: _StartTable, many: int) -> "np.ndarray":
    """Random draws and the ones at every bin's edges, where an index is chosen."""
    top = table.thresholds[-1]
    edges = {0, top - 1}
    for edge in table.thresholds:
        edges |= {max(edge - 1, 0), min(edge, top - 1), min(edge + 1, top - 1)}
    drawn = {CHOOSER.randrange(top) for _ in range(many)} | edges
    return np.array(sorted(drawn), dtype=np.uint64)


def _fast(table: _StartTable) -> bool:
    """Whether :meth:`_StartTable.values` takes its int64 path for this table."""
    gaps = np.diff(np.concatenate((np.zeros(1, dtype=np.int64), np.array(table.thresholds))))
    return int(gaps.max()) * max(table.counts) < 1 << 63


@pytest.mark.parametrize("seed", range(12))
def test_the_int64_path_gives_the_scalar_values(seed: int) -> None:
    CHOOSER.seed(seed)
    table = _table(CHOOSER.randrange(1, 40), TOP, 1 << CHOOSER.randrange(1, 9))
    assert _fast(table), "the int64 path is the one taken"
    draws = _draws(table, 600)
    assert table.values(draws).tolist() == [table.value(int(draw)) for draw in draws]


@pytest.mark.parametrize("seed", range(12))
def test_the_integer_path_gives_the_scalar_values_where_a_product_passes_two_to_the_63(
    seed: int,
) -> None:
    CHOOSER.seed(100 + seed)
    # Bins about 2**50 wide and counts of 2**20 or more: a product passes 2**63.
    table = _table(CHOOSER.randrange(1, 12), 1 << 51, (1 << 30) + CHOOSER.randrange(1 << 20))
    assert not _fast(table), "the Python-integer path is the one taken"
    draws = _draws(table, 600)
    assert table.values(draws).tolist() == [table.value(int(draw)) for draw in draws]


def test_the_two_paths_meet_at_two_to_the_63() -> None:
    """One bin of ``2**43`` positions: counts under ``2**20`` stay in int64, and ``2**20`` does not."""
    width = 1 << 43
    for count, fast in ((1 << 20) - 1, True), (1 << 20, False):
        table = _StartTable((width, 2 * width), (7, -3), (count, 5))
        assert _fast(table) is fast
        draws = _draws(table, 400)
        assert table.values(draws).tolist() == [table.value(int(draw)) for draw in draws]


def test_a_table_of_one_bin_and_a_table_of_one_value() -> None:
    for table in (_StartTable((TOP,), (11,), (1,)), _StartTable((TOP,), (-5,), (1000,))):
        draws = _draws(table, 100)
        assert table.values(draws).tolist() == [table.value(int(draw)) for draw in draws]
