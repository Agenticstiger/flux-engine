# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A process works each history of a run out once, however many the run holds (ADR 0017, the amendment of 2026-11-28).

A history's version layout reads every entity, so working it out twice is the cost the layout is shared
to avoid. The process that starts a pool works each one out and hands it to its workers, so it must
keep every history the run reads until the run's memos are cleared, not only the few it last used.
The store holds up to 64 layouts, past which it is cleared, so a run of up to 64 histories builds each once.
Pinned here on the store itself, with more histories than the process used to keep: each is built
once however the reads interleave, and clearing the run's memos forgets them all. What is handed to a
worker is what the plans' own histories returned, under the whole key they were worked out under.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from flux_engine.generator import history
from flux_engine.generator.history import plan_history
from flux_engine.generator.rows import clear_memos
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.history import Histogram

from .test_history_layout import SIM_ID, entry, root_for

#: More than the four layouts a process used to keep before it dropped them all.
HISTORIES = 7


def _plan(root: Any, emit: str) -> Any:
    return plan_history(
        root,
        SIM_ID,
        emit,
        entry(
            emit=emit,
            entities=40,
            parent=None,
            start=Histogram(("2019-01-01", "2021-12-31"), ("1",)),
            until="2021-12-31",
        ),
        kind=LogicalKind.DATE,
        rows=120,
        entities=40,
        time_bounds={"start": "2019-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"},
    )


@pytest.fixture(autouse=True)
def _fresh_store() -> Any:
    clear_memos()
    yield
    clear_memos()


def test_more_histories_than_the_old_bound_are_each_built_once_however_the_reads_interleave() -> (
    None
):
    root = root_for()
    emits: List[str] = [f"member_{number}_version" for number in range(HISTORIES)]
    before = history.chain_builds()
    first = {emit: _plan(root, emit) for emit in emits}
    assert history.chain_builds() - before == HISTORIES
    # Read them again in the order that evicted the earliest ones before: all are held.
    for emit in reversed(emits):
        again = _plan(root, emit)
        assert again.versions.counts.tolist() == first[emit].versions.counts.tolist()
    for emit in emits:
        _plan(root, emit)
    assert history.chain_builds() - before == HISTORIES


def test_the_layouts_handed_to_a_worker_are_the_ones_its_plans_returned() -> None:
    root = root_for()
    layouts = [_plan(root, f"member_{number}_version") for number in range(HISTORIES)]
    handed = history.hand_chains(layouts)
    assert len(handed) == HISTORIES
    assert all(layout.per_parent is None for layout in handed.values())
    assert set(handed) == set(history._PLANS)


def test_clearing_the_run_memos_forgets_every_history() -> None:
    root = root_for()
    for number in range(HISTORIES):
        _plan(root, f"member_{number}_version")
    clear_memos()
    assert not history._PLANS and not history._HANDED
    before = history.chain_builds()
    _plan(root, "member_0_version")
    assert history.chain_builds() - before == 1


def test_a_layout_handed_to_a_worker_is_the_one_its_own_plan_returned_not_one_the_store_holds() -> (
    None
):
    """A store holding a layout of another run, or of another key, hands nothing it was not asked for."""
    root = root_for()
    mine = _plan(root, "member_0_version")
    stale = _plan(root, "member_1_version")
    assert set(history.hand_chains([mine])) == {mine.key}
    assert stale.key in history._PLANS and stale.key not in history.hand_chains([mine])


def test_a_process_past_the_bound_clears_the_store_and_builds_again() -> None:
    root = root_for()
    emits = [f"member_{number}_version" for number in range(history._PLANS_BOUND + 1)]
    before = history.chain_builds()
    for emit in emits:
        _plan(root, emit)
    assert history.chain_builds() - before == len(emits)
    # The store was cleared when the bound was reached: only the last layout is held now.
    assert len(history._PLANS) == 1
    _plan(root, emits[0])
    assert history.chain_builds() - before == len(emits) + 1


def test_a_run_of_as_many_histories_as_the_bound_builds_each_once() -> None:
    root = root_for()
    emits = [f"member_{number}_version" for number in range(history._PLANS_BOUND)]
    before = history.chain_builds()
    for _ in range(2):
        for emit in emits:
            _plan(root, emit)
    assert history.chain_builds() - before == len(emits)
    assert len(history._PLANS) == len(emits)
