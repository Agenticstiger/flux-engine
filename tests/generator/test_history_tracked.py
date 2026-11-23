# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tracked columns, change reasons and carried columns (ADR 0017 section 6, KERNEL-SPEC-2 section 8.4).

A version after an entity's first is one ordinary change: exactly one tracked column moves, to a
level drawn from its previous level's row of ``transitions``, and every other column carries.
What is pinned, on kernel 0.2.0:

* **The reasons.** Each tracked column makes exactly its share of the changes, by the Largest
  Remainder Method over the ordinary changes, with no draw; one tracked column is the reason of
  every change. No change is a move to the same level, and an ending version is no change.
* **The values.** Version 0 draws each initial level at ``256 + c`` of its ``change`` path, and a
  large run's transitions follow the matrix.
* **The carried columns.** Every other column takes, on every version, the cell its entity's first
  version row draws, and the later rows' cells are never drawn; a foreign key other than the
  parent's is picked on the first row and carried. Rules then run on each version.
* **Two paths, one answer.** The column path's rows equal the per-row path's, in slices that cut
  entities in two.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

from flux_engine.core.lrm import largest_remainder
from flux_engine.generator import rows as rows_module
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.rows import generate_row_slice
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import HistoryEnd, HistoryEntry, HistoryTracked

from .test_history_end import (
    EMIT,
    PLAN,
    PRICE,
    STATUS,
    TIER,
    by_entity,
    entry,
    fresh_layouts,  # noqa: F401  (an autouse fixture)
    history_plan,
    plans_for,
    run_of,
)
from .test_history_layout import SIM_ID, root_for
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit

PLAN_MOVES = {
    "basic": {"pro": "0.8", "team": "0.2"},
    "pro": {"basic": "0.5", "team": "0.5"},
    "team": {"pro": "1"},
}
TIER_MOVES = {"low": {"high": "1"}, "high": {"low": "1"}}
BINS = ChildShape("bins", ("1", "2", "4", "9"), ("0.45", "0.35", "0.20"))
REASONS = ChildShape("levels", ("plan", "tier"), ("0.7", "0.3"))


def track(
    column: str, moves: Mapping[str, Mapping[str, str]], initial: Optional[ChildShape] = None
) -> HistoryTracked:
    return HistoryTracked(
        column=column,
        transitions=tuple((level, tuple(row.items())) for level, row in moves.items()),
        initial=initial,
    )


PLAN_ONLY = (track("plan", PLAN_MOVES),)
BOTH = (track("plan", PLAN_MOVES), track("tier", TIER_MOVES))
FINAL = HistoryEnd("finalVersion", "0.5", "status", "gone")
CLOSE = HistoryEnd("closeLast", "0.5", "status", "gone")


def tracked_entry(tracked: Sequence[HistoryTracked] = BOTH, **rest: Any) -> HistoryEntry:
    reasons = REASONS if len(tracked) > 1 else None
    return entry(tracked=tuple(tracked), reasons=reasons, **rest)


def moved(before: Mapping[str, Any], after: Mapping[str, Any], columns: Sequence[str]) -> List[str]:
    return [column for column in columns if before[column] != after[column]]


def changes_of(
    data: List[Dict[str, Any]], columns: Sequence[str], *, ending: Optional[str] = None
) -> List[Tuple[Dict[str, Any], Dict[str, Any], List[str]]]:
    """Every pair of consecutive versions of an entity, with the tracked columns that moved."""
    found = []
    for versions in by_entity(data).values():
        for before, after in zip(versions, versions[1:], strict=False):
            if ending is not None and after["status"] == ending:
                continue
            found.append((before, after, moved(before, after, columns)))
    return found


# -- the reasons ----------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("end", [None, FINAL, CLOSE])
def test_every_change_moves_exactly_one_tracked_column_to_another_level(
    seed: int, end: Optional[HistoryEnd]
) -> None:
    held = tracked_entry(BOTH, end=end, versions=BINS)
    data = run_of(plans_for(held, rows=80, entities=24), seed)
    pairs = changes_of(data, ["plan", "tier"], ending="gone")
    assert pairs
    for _before, _after, columns in pairs:
        assert len(columns) == 1
    # No move to the same level, and a move only along the matrix.
    for before, after, columns in pairs:
        (column,) = columns
        moves = PLAN_MOVES if column == "plan" else TIER_MOVES
        assert after[column] in moves[before[column]]


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_each_tracked_column_makes_exactly_its_share_of_the_changes(seed: int) -> None:
    held = tracked_entry(BOTH, versions=BINS)
    data = run_of(plans_for(held, rows=100, entities=30), seed)
    counts = Counter(
        column for _b, _a, columns in changes_of(data, ["plan", "tier"]) for column in columns
    )
    total = 100 - 30
    wanted = largest_remainder(total, {"0": Decimal("0.7"), "1": Decimal("0.3")})
    assert counts["plan"] == wanted["0"] == 49
    assert counts["tier"] == wanted["1"] == 21
    layout = history_plan(held, rows=100, entities=30, seed=seed)
    assert layout.reasons is not None and layout.reasons.total == total
    assert layout.reasons.cumulative == (49, 70)


def test_the_ending_version_is_no_change_so_the_reasons_count_the_ordinary_ones() -> None:
    held = tracked_entry(BOTH, end=FINAL, versions=BINS)
    layout = history_plan(held, rows=100, entities=30)
    assert layout.ended is not None
    ending = int(layout.ended.sum())
    assert ending > 0
    assert layout.reasons is not None and layout.reasons.total == 100 - 30 - ending
    data = run_of(plans_for(held, rows=100, entities=30))
    counts = Counter(
        column
        for _b, _a, columns in changes_of(data, ["plan", "tier"], ending="gone")
        for column in columns
    )
    assert sum(counts.values()) == layout.reasons.total
    assert (
        counts["plan"]
        == largest_remainder(layout.reasons.total, {"0": Decimal("0.7"), "1": Decimal("0.3")})["0"]
    )
    # The ending version moves no tracked column.
    for versions in by_entity(data).values():
        if versions[-1]["status"] == "gone":
            assert moved(versions[-2], versions[-1], ["plan", "tier"]) == []


def test_one_tracked_column_is_the_reason_of_every_change() -> None:
    held = tracked_entry(PLAN_ONLY, versions=BINS)
    layout = history_plan(held, rows=80, entities=24)
    assert layout.reasons is None and layout.changes is not None
    data = run_of(plans_for(held, rows=80, entities=24))
    pairs = changes_of(data, ["plan"])
    assert len(pairs) == 80 - 24 == int(layout.changes[-1])
    assert all(columns == ["plan"] for _b, _a, columns in pairs)
    # The other column is carried: one value per entity.
    for versions in by_entity(data).values():
        assert len({row["tier"] for row in versions}) == 1


def test_the_reasons_do_not_depend_on_a_draw() -> None:
    held = tracked_entry(BOTH, versions=BINS)
    first = history_plan(held, rows=100, entities=30, seed=1)
    second = history_plan(held, rows=100, entities=30, seed=2)
    assert first.reasons is not None and second.reasons is not None
    assert first.reasons.cumulative == second.reasons.cumulative
    assert first.reasons.key != second.reasons.key


# -- the values -----------------------------------------------------------------------------------


def test_version_zero_takes_the_initial_levels_by_default_the_columns_own() -> None:
    held = tracked_entry(PLAN_ONLY, versions=None, entities=3000)
    data = run_of(plans_for(held, rows=6000, entities=3000, customers=60))
    firsts = Counter(versions[0]["plan"] for versions in by_entity(data).values())
    for level, weight in PLAN.items():
        assert abs(firsts[level] / 3000 - float(weight)) < 0.03, level


def test_version_zero_takes_the_declared_initial_levels() -> None:
    initial = ChildShape("levels", ("basic", "pro"), ("0.75", "0.25"))
    held = tracked_entry((track("plan", PLAN_MOVES, initial),), versions=None, entities=3000)
    data = run_of(plans_for(held, rows=6000, entities=3000, customers=60))
    firsts = Counter(versions[0]["plan"] for versions in by_entity(data).values())
    assert firsts["team"] == 0
    assert abs(firsts["basic"] / 3000 - 0.75) < 0.03


def test_a_large_runs_transitions_follow_the_matrix() -> None:
    held = tracked_entry(PLAN_ONLY, versions=ChildShape("levels", ("5",), ("1",)), entities=2000)
    data = run_of(plans_for(held, rows=10000, entities=2000, customers=60))
    seen: Dict[str, Counter[str]] = {level: Counter() for level in PLAN_MOVES}
    for before, after, _ in changes_of(data, ["plan"]):
        seen[before["plan"]][after["plan"]] += 1
    for level, row in PLAN_MOVES.items():
        total = sum(seen[level].values())
        assert total > 500, level
        assert set(seen[level]) <= set(row)
        for target, weight in row.items():
            assert abs(seen[level][target] / total - float(weight)) < 0.04, (level, target)


def test_a_tracked_end_column_takes_the_end_value_on_the_ending_version_only() -> None:
    moves = {"active": {"paused": "1"}, "paused": {"active": "1"}}
    tracked = (track("status", moves, ChildShape("levels", ("active", "paused"), ("0.5", "0.5"))),)
    held = tracked_entry(tracked, end=FINAL, versions=BINS)
    data = run_of(plans_for(held, rows=100, entities=30))
    layout = history_plan(held, rows=100, entities=30)
    assert layout.ended is not None
    seen = 0
    for entity, versions in enumerate(by_entity(data).values()):
        statuses = [row["status"] for row in versions]
        assert "gone" not in statuses[:-1]
        if layout.ended[entity]:
            seen += 1
            assert statuses[-1] == "gone"
        else:
            assert statuses[-1] != "gone"
        # Every other version moves, so the chain alternates.
        for before, after in zip(statuses[:-1], statuses[1:-1], strict=False):
            assert before != after
    assert seen == int(layout.ended.sum())


# -- the carried columns --------------------------------------------------------------------------


CARRIED = ("region", "store_id", "customer_id", "sub_id", "activated")


def test_carried_values_are_equal_across_an_entitys_versions() -> None:
    held = tracked_entry(BOTH, versions=BINS, end=FINAL)
    data = run_of(plans_for(held, rows=100, entities=30))
    for versions in by_entity(data).values():
        for column in CARRIED:
            assert len({row[column] for row in versions}) == 1, column
    # The cells differ between entities: they are drawn, not constant.
    assert len({versions[0]["region"] for versions in by_entity(data).values()}) > 20


def test_a_carried_value_is_the_cell_its_first_version_row_draws() -> None:
    held = tracked_entry(BOTH, versions=BINS)
    plans = plans_for(held, rows=100, entities=30)
    plan = plans[EMIT]
    streams = CellStreams(root_for(), SIM_ID, EMIT)
    layout = history_plan(held, rows=100, entities=30)
    data = run_of(plans)
    for row, value in enumerate(data):
        first = int(layout.versions.offsets[layout.entity_of(row)])
        drawn = rows_module._cell(
            plan.column_plan("region"), streams, SIM_ID, EMIT, first, plan.seam_digest
        )
        assert value["region"] == drawn


def test_the_later_rows_cells_are_never_drawn(monkeypatch: pytest.MonkeyPatch) -> None:
    held = tracked_entry(BOTH, versions=BINS)
    plans = plans_for(held, rows=100, entities=30)
    plan = plans[EMIT]
    layout = history_plan(held, rows=100, entities=30)
    asked: List[int] = []
    real = rows_module._cell

    def spy(
        column_plan: Any, source: Any, sim_id: str, expose_id: str, row_index: int, *rest: Any
    ) -> Any:
        if column_plan.column.name == "region":
            asked.append(row_index)
        return real(column_plan, source, sim_id, expose_id, row_index, *rest)

    with monkeypatch.context() as patch:
        patch.setattr("flux_engine.generator.columns.column_rows", lambda *args: None)
        patch.setattr(rows_module, "_cell", spy)
        generate_row_slice(plan, 0, plan.rows, root=root_for())
    assert sorted(set(asked)) == sorted(int(first) for first in layout.versions.offsets)


def test_a_foreign_key_other_than_the_parents_is_picked_on_the_first_row() -> None:
    held = tracked_entry(BOTH, versions=BINS)
    data = run_of(plans_for(held, rows=100, entities=30))
    for versions in by_entity(data).values():
        assert len({row["store_id"] for row in versions}) == 1
        assert len({row["customer_id"] for row in versions}) == 1
    assert len({row["store_id"] for row in data}) > 3


def test_an_untracked_status_is_carried_but_the_ending_version() -> None:
    held = tracked_entry(BOTH, end=FINAL, versions=BINS)
    data = run_of(plans_for(held, rows=100, entities=30))
    for versions in by_entity(data).values():
        assert len({row["status"] for row in versions if row["status"] != "gone"}) == 1
        assert versions[0]["status"] in STATUS and versions[0]["status"] != "gone"


def test_an_untracked_column_the_history_does_not_set_keeps_its_levels() -> None:
    held = tracked_entry(PLAN_ONLY, versions=BINS)
    data = run_of(plans_for(held, rows=100, entities=30))
    assert {row["tier"] for row in data} <= set(TIER)


# -- rules after the values -----------------------------------------------------------------------


def test_a_rule_over_a_tracked_column_follows_the_version() -> None:
    held = tracked_entry(PLAN_ONLY, versions=BINS)
    data = run_of(plans_for(held, rows=80, entities=24, priced=True))
    price = {"basic": 10, "pro": 30, "team": 50}
    assert PRICE["derive"] == "price"
    assert all(row["price"] == price[row["plan"]] for row in data)
    # The price changes with the plan on a version, which no carried cell could do.
    changes = [pair for pair in changes_of(data, ["plan"])]
    assert changes and all(a["price"] != b["price"] for b, a, _ in changes)


# -- two paths, one answer ------------------------------------------------------------------------


def variants() -> List[Tuple[str, HistoryEntry]]:
    return [
        ("one column", tracked_entry(PLAN_ONLY, versions=BINS)),
        ("two columns", tracked_entry(BOTH, versions=BINS)),
        ("two columns, finalVersion", tracked_entry(BOTH, versions=BINS, end=FINAL)),
        ("two columns, closeLast", tracked_entry(BOTH, versions=BINS, end=CLOSE)),
        ("no parent, no shape", tracked_entry(BOTH, versions=None, parent=None)),
        (
            "dayBefore, activation",
            tracked_entry(BOTH, versions=BINS, chain="dayBefore", activation="activated"),
        ),
    ]


@pytest.mark.parametrize("name", [name for name, _ in variants()])
def test_the_column_path_writes_the_rows_the_per_row_path_writes(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    held = dict(variants())[name]
    plans = plans_for(held, rows=80, entities=24, priced=True)
    plan = plans[EMIT]
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken == [True]
    assert by_columns == by_rows
    # In chunks that cut entities in two, and slices that start in the middle of one.
    monkeypatch.setattr("flux_engine.generator.columns.CHUNK_ROWS", 7)
    assert generate_row_slice(plan, 0, plan.rows, root=root_for()) == by_rows
    assert generate_row_slice(plan, 13, 41, root=root_for()) == by_rows[13:41]
    assert generate_row_slice(plan, 79, 80, root=root_for()) == by_rows[79:80]


def test_a_slice_is_the_slice_of_the_whole_run() -> None:
    plans = plans_for(tracked_entry(BOTH, versions=BINS, end=FINAL), rows=80, entities=24)
    whole = run_of(plans)
    for start, stop in ((0, 1), (5, 31), (30, 80), (79, 80)):
        assert run_of(plans, start=start, stop=stop) == whole[start:stop]


def test_a_generated_column_is_the_columns_of_the_rows() -> None:
    held = tracked_entry(BOTH, versions=BINS, end=FINAL)
    plans = plans_for(held, rows=60, entities=18)
    plan = plans[EMIT]
    rows = generate_row_slice(plan, 0, plan.rows, root=root_for())
    names = [column_plan.column.name for column_plan in plan.columns]
    for column in ("plan", "tier", "region", "store_id", "status"):
        assert rows_module.generate_column(plan, column, root=root_for()) == tuple(
            row[names.index(column)] for row in rows
        ), column


def test_two_seeds_give_two_histories_and_one_seed_one() -> None:
    plans = plans_for(tracked_entry(BOTH, versions=BINS), rows=80, entities=24)
    assert run_of(plans, 1) == run_of(plans, 1)
    assert run_of(plans, 1) != run_of(plans, 2)
