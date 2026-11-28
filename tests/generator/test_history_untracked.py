# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A change that moves no tracked column (ADR 0017, amendment of 2026-11-27).

``history[].untracked`` is the share of the ordinary changes that move no tracked column. What is
pinned:

* **The count.** The untracked class takes exactly its share of the ordinary changes by the Largest
  Remainder Method, and each tracked column its share of what is left, with no draw.
* **What an untracked change keeps.** Every tracked level, and every carried cell: only the validity
  window moves.
* **Two paths, one answer.** The column path equals the per-row path, and slices equal the whole.
* **Nothing changes without the key.** A one-column history with no ``untracked`` still moves its
  column on every change.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from flux_engine.core.lrm import largest_remainder
from flux_engine.generator import columns

from .test_history_end import (
    by_entity,
    fresh_layouts,  # noqa: F401  (an autouse fixture)
    history_plan,
    plans_for,
    run_of,
)
from .test_history_tracked import (
    BINS,
    BOTH,
    FINAL,
    PLAN_ONLY,
    REASONS,
    changes_of,
    tracked_entry,
)

pytestmark = pytest.mark.unit

ROWS, ENTITIES = 100, 30
TOTAL = ROWS - ENTITIES


def test_one_tracked_column_moves_exactly_its_share_of_the_changes() -> None:
    held = tracked_entry(PLAN_ONLY, versions=BINS, untracked="0.6")
    layout = history_plan(held, rows=ROWS, entities=ENTITIES)
    assert layout.reasons is not None and layout.reasons.total == TOTAL
    wanted = largest_remainder(TOTAL, {"0": Decimal("0.4"), "1": Decimal("0.6")})
    assert layout.reasons.cumulative == (wanted["0"], TOTAL)
    for seed in (1, 2, 3):
        data = run_of(plans_for(held, rows=ROWS, entities=ENTITIES), seed)
        pairs = changes_of(data, ["plan"])
        assert len(pairs) == TOTAL
        assert sum(1 for _b, _a, moved in pairs if moved) == wanted["0"] == 28


def test_without_the_key_one_column_moves_on_every_change() -> None:
    held = tracked_entry(PLAN_ONLY, versions=BINS)
    data = run_of(plans_for(held, rows=ROWS, entities=ENTITIES))
    assert all(moved for _b, _a, moved in changes_of(data, ["plan"]))


def test_an_untracked_change_keeps_every_column_but_its_validity_window() -> None:
    held = tracked_entry(PLAN_ONLY, versions=BINS, untracked="0.6")
    data = run_of(plans_for(held, rows=ROWS, entities=ENTITIES, priced=True))
    kept = 0
    for before, after, moved in changes_of(data, ["plan"]):
        if moved:
            continue
        kept += 1
        for name in before:
            if name not in ("version_id", "valid_from", "valid_to"):
                assert before[name] == after[name], name
        assert before["valid_from"] != after["valid_from"]
    assert kept == TOTAL - 28


def test_several_tracked_columns_share_what_the_untracked_class_leaves() -> None:
    held = tracked_entry(BOTH, versions=BINS, untracked="0.25")
    data = run_of(plans_for(held, rows=ROWS, entities=ENTITIES))
    counts: Counter[str] = Counter()
    for _b, _a, moved in changes_of(data, ["plan", "tier"]):
        assert len(moved) <= 1
        counts[moved[0] if moved else "none"] += 1
    weights = {
        "0": Decimal("0.7") * Decimal("0.75"),
        "1": Decimal("0.3") * Decimal("0.75"),
        "2": Decimal("0.25"),
    }
    wanted = largest_remainder(TOTAL, weights)
    assert counts == {"plan": wanted["0"], "tier": wanted["1"], "none": wanted["2"]}
    assert REASONS.values == ("plan", "tier")


def test_the_ending_version_is_no_change_beside_the_key() -> None:
    held = tracked_entry(PLAN_ONLY, versions=BINS, untracked="0.5", end=FINAL)
    layout = history_plan(held, rows=ROWS, entities=ENTITIES)
    assert layout.reasons is not None and layout.reasons.total == int(layout.changes[-1])  # type: ignore[index]
    data = run_of(plans_for(held, rows=ROWS, entities=ENTITIES))
    pairs = changes_of(data, ["plan"], ending="gone")
    assert len(pairs) == layout.reasons.total


def test_the_column_path_is_the_row_path(monkeypatch: pytest.MonkeyPatch) -> None:
    held = tracked_entry(BOTH, versions=BINS, untracked="0.4", end=FINAL)
    plans = plans_for(held, rows=ROWS, entities=ENTITIES, priced=True)

    def make() -> List[Dict[str, Any]]:
        return run_of(plans)

    taken: List[bool] = []
    real = columns.column_rows

    def spy(*args: Any) -> Any:
        found = real(*args)
        taken.append(found is not None)
        return found

    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", spy)
        by_columns = make()
    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", lambda *args: None)
        by_rows = make()
    assert by_columns == by_rows and any(taken)


@pytest.mark.parametrize("cuts", [(0, 17, 100), (0, 1, 2, 63, 100), (0, 99, 100)])
def test_slices_make_the_whole_run(cuts: Any) -> None:
    held = tracked_entry(PLAN_ONLY, versions=BINS, untracked="0.6")
    plans = plans_for(held, rows=ROWS, entities=ENTITIES)
    whole = run_of(plans)
    joined: List[Dict[str, Any]] = []
    for start, stop in zip(cuts, cuts[1:], strict=False):
        joined += run_of(plans, start=start, stop=stop)
    assert joined == whole
    assert len(by_entity(whole)) == ENTITIES
