# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A layout handed to a worker is found under everything its build reads (ADR 0017, the amendment of 2026-11-28).

A history bound by its parent's lifetime reads the parents' lifetimes, so two layouts of one history
that differ only in them are two layouts. The process that starts a pool hands each of its plans'
layouts to the workers under the key it was worked out under, so a worker that asks for one
gets that one and never the other's.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

import numpy as np
import pytest

from flux_engine.generator import history
from flux_engine.generator.history import HistoryPlan, OuterInputs, ParentBound, plan_history
from flux_engine.generator.rows import clear_memos
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import HistoryEnd, HistoryParent

from .test_history_layout import BOUNDS, SIM_ID, entry, root_for

pytestmark = pytest.mark.unit

EMIT = "sub_version"
CUSTOMERS = 12
ENTITIES = 40
BOUND_PARENT = HistoryParent("customer", min_children=1, lifetime=True)
#: The first day of 2023 and of 2026, in days.
JAN_2023, JAN_2026 = 19358, 20454


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    clear_memos()
    yield
    clear_memos()


def _layout(identity: str, closes: Optional[int]) -> HistoryPlan:
    """The layout of one history under parents that all open in 2023 and close on day *closes*."""
    lifetimes: List[Optional[Tuple[int, Optional[int]]]] = [
        (JAN_2023 + 7 * parent, closes) for parent in range(CUSTOMERS)
    ]
    return plan_history(
        root_for(),
        SIM_ID,
        EMIT,
        entry(
            entities=ENTITIES,
            parent=BOUND_PARENT,
            versions=ChildShape("levels", ("1", "3"), ("0.5", "0.5")),
            end=HistoryEnd("closeLast", "0.25"),
        ),
        kind=LogicalKind.DATE,
        rows=120,
        entities=ENTITIES,
        time_bounds=BOUNDS,
        outer=OuterInputs("customer", CUSTOMERS, minimum=1),
        bound=ParentBound.of(lifetimes, identity),
    )


def test_two_layouts_that_differ_only_in_the_parents_lifetimes_are_each_found_by_their_own_key() -> (
    None
):
    early = _layout("early", JAN_2023 + 400)
    late = _layout("late", JAN_2026 - 5)
    assert early.key != late.key
    assert early.key[:-1] == late.key[:-1], "they differ in the bound alone"
    assert not np.array_equal(early.first_starts, late.first_starts) or not np.array_equal(
        early.rooms, late.rooms
    )
    handed = history.hand_chains([early, late])
    assert set(handed) == {early.key, late.key}

    # A worker: it holds what it was handed and nothing it worked out.
    clear_memos()
    history.seed_chains(handed)
    builds = history.chain_builds()
    for mine, identity, closes in ((early, "early", JAN_2023 + 400), (late, "late", JAN_2026 - 5)):
        found = _layout(identity, closes)
        assert np.array_equal(found.first_starts, mine.first_starts), identity
        assert np.array_equal(found.versions.counts, mine.versions.counts), identity
        assert np.array_equal(found.rooms, mine.rooms), identity
    assert history.chain_builds() == builds, "a worker works no layout out that it was handed"
