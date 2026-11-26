# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's layout keeps to its validity columns' range (ADR 0024, the amendment of 2026-11-25).

The range is a bound every entity shares: each first start is at or after its low end, each chain
ends by its limit, the per-row path draws the first starts the planner draws, and no entity is made
to end by it.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator.history import (
    HistoryBinding,
    HistoryPlan,
    Ranged,
    entity_chain,
    first_start_at,
    unit_of,
    validity_bound,
)
from flux_engine.seam.contract import LogicalKind

from .test_history_end import history_plan
from .test_history_layout import BOUNDS, EMIT, ENTITIES, SIM_ID, VERSIONS, entry, root_for

pytestmark = pytest.mark.unit

DAY = 86_400_000
LOW = unit_of("2024-01-01", LogicalKind.DATE)
LIMIT = unit_of("2025-01-01", LogicalKind.DATE)


def _layout(ranged: Optional[Ranged]) -> HistoryPlan:
    return history_plan(entry(parent=None), rows=VERSIONS, entities=ENTITIES, ranged=ranged)


@pytest.mark.parametrize("ranged", [(LOW, LIMIT), (LOW, None), (None, LIMIT)])
def test_every_version_keeps_to_the_range_and_the_last_stays_open(ranged: Ranged) -> None:
    layout = _layout(ranged)
    low, limit = ranged
    for entity in range(layout.entities):
        chain = entity_chain(
            root_for(), SIM_ID, EMIT, entry(parent=None), LogicalKind.DATE, layout, entity
        )
        assert low is None or min(chain.starts) >= low
        assert limit is None or max(chain.starts) < limit
        assert all(end is None or limit is None or end < limit for end in chain.ends)
        assert chain.ends[-1] is None
    assert layout.ended is None


def test_the_per_row_path_draws_the_first_starts_the_planner_draws() -> None:
    layout = _layout((LOW, LIMIT))
    for entity in range(layout.entities):
        found = first_start_at(
            root_for(),
            SIM_ID,
            EMIT,
            entry(parent=None),
            LogicalKind.DATE,
            entity,
            c=layout.c,
            time_bounds=BOUNDS,
            ranged=(LOW, LIMIT),
        )
        assert found == int(layout.first_starts[entity])


def test_no_range_is_the_layout_without_one() -> None:
    plain, ranged = _layout(None), _layout((None, None))
    assert np.array_equal(plain.first_starts, ranged.first_starts)
    assert np.array_equal(plain.versions.counts, ranged.versions.counts)


def _binding(low: Optional[int], high: Optional[int]) -> HistoryBinding:
    return HistoryBinding(entry(parent=None), ENTITIES, "valid_from", validity_range=(low, high))


def test_the_bound_is_in_the_chains_unit_and_only_where_it_cuts() -> None:
    window = unit_of("2023-01-01", LogicalKind.DATE) * DAY
    assert validity_bound(_binding(LOW * DAY, LIMIT * DAY - 1), LogicalKind.DATE, window) == (
        LOW,
        LIMIT,
    )
    # A low end inside a day starts on the next day; a range wider than the window cuts nothing.
    assert validity_bound(_binding(LOW * DAY + 1, None), LogicalKind.DATE, window) == (
        LOW + 1,
        None,
    )
    assert validity_bound(_binding(0, None), LogicalKind.DATE, window) is None


def test_a_range_that_misses_the_window_is_refused() -> None:
    window = unit_of("2023-01-01", LogicalKind.DATE) * DAY
    with pytest.raises(FluxError) as caught:
        validity_bound(_binding(None, window - DAY), LogicalKind.DATE, window)
    assert caught.value.code.code == "FLX-3403" and "does not meet the window" in str(caught.value)
