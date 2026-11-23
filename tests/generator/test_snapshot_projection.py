# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A snapshot is one row per entity, its last version's final values (ADR 0027 sections 2 and 3).

Built on a history's plans through :func:`flux_engine.generator.projections.project_plan`, as the
pipeline builds them. What is pinned, on kernel 0.2.0:

* **The rows.** One row per entity, in entity order; each column the entity's last version's
  value, a rule's included; ``ended: drop`` leaves out the ended entities, so the count is the
  entities less the ended ones; the count follows from the plan alone.
* **Nothing is drawn.** The snapshot reads the history's cells and draws none of its own, and the
  history's own rows do not move.
* **The slices.** A slice is the slice of the whole, and a process works out what its slices need
  once.
* **Two paths, one answer.** The column path's rows equal the per-row path's.
* **The sources.** Each row names the history row it is read from.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator.projections import (
    KINDS,
    sources_of,
)
from flux_engine.generator.rows import (
    GeneratorPlan,
    generate_row_slice,
    generate_rows,
)
from flux_engine.seam.history import HistoryEnd

from .test_history_end import (
    CLOSE_WITH,
    FINAL,
    by_entity,
    entry,
    fresh_layouts,  # noqa: F401  (an autouse fixture)
    history_plan,
    run_of,
)
from .test_history_layout import root_for, table
from .test_projection_plan import (
    COLUMNS,
    fresh_sources,  # noqa: F401  (an autouse fixture)
    held,
    snapshot_plan,
)
from .test_rules_over_links import _by_both_paths

pytestmark = pytest.mark.unit


def snapshot_rows(plan: GeneratorPlan, seed: int = 5) -> List[Dict[str, Any]]:
    return table(plan, generate_rows(plan, root=root_for(seed)))


# -- the rows -------------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
def test_each_row_is_its_entitys_last_version_in_entity_order(end: Optional[HistoryEnd]) -> None:
    plans = held(end)
    plan = snapshot_plan(plans)
    versions = by_entity(run_of(plans))
    rows = snapshot_rows(plan)
    assert plan.rows == len(rows) == len(versions) == 24
    assert [row["sub_id"] for row in rows] == list(versions)
    for row, chain in zip(rows, versions.values(), strict=True):
        last = chain[-1]
        assert row == {name: last[name] for name in COLUMNS}


def test_a_rule_s_value_is_the_last_versions_final_value() -> None:
    plans = held()
    rows = snapshot_rows(snapshot_plan(plans))
    prices = {"basic": 10, "pro": 30, "team": 50}
    assert rows and all(row["price"] == prices[row["plan"]] for row in rows)


@pytest.mark.parametrize("end", [FINAL, CLOSE_WITH])
def test_the_end_value_is_on_the_snapshot_of_an_ended_entity(end: HistoryEnd) -> None:
    plans = held(end)
    rows = snapshot_rows(snapshot_plan(plans))
    ended = [row for row in rows if row["status"] == "gone"]
    chain = history_plan(entry(end=end), rows=80, entities=24)
    assert chain.ended is not None
    assert len(ended) == int(chain.ended.sum()) > 0


@pytest.mark.parametrize("end", [FINAL, CLOSE_WITH])
def test_ended_drop_leaves_out_the_ended_entities(end: HistoryEnd) -> None:
    plans = held(end)
    chain = history_plan(entry(end=end), rows=80, entities=24)
    assert chain.ended is not None
    ended_count = int(chain.ended.sum())
    assert 0 < ended_count < 24
    kept = snapshot_plan(plans, ended="keep")
    dropped = snapshot_plan(plans, ended="drop")
    assert kept.rows == 24 and dropped.rows == 24 - ended_count
    everyone = snapshot_rows(kept)
    live = snapshot_rows(dropped)
    assert live == [
        row for row, gone in zip(everyone, chain.ended.tolist(), strict=True) if not gone
    ]
    assert all(row["status"] != "gone" for row in live)


def test_a_snapshot_draws_nothing_and_moves_no_history_row() -> None:
    plans = held(FINAL)
    before = run_of(plans)
    snapshot_rows(snapshot_plan(plans, ended="drop"))
    assert run_of(plans) == before


# -- the slices -----------------------------------------------------------------------------------


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
def test_a_slice_is_the_slice_of_the_whole(end: Optional[HistoryEnd]) -> None:
    plan = snapshot_plan(held(end), ended="drop")
    whole = generate_rows(plan, root=root_for())
    for start, stop in ((0, 1), (3, 9), (plan.rows - 1, plan.rows), (5, 5), (0, plan.rows)):
        assert generate_row_slice(plan, start, stop, root=root_for()) == whole[start:stop]


def test_a_process_works_out_its_sources_once(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = snapshot_plan(held(FINAL), ended="drop")
    made: List[int] = []
    real = KINDS["snapshots"].sources

    def spy(chain: Any, projection: Any) -> "np.ndarray":
        made.append(1)
        return real(chain, projection)

    monkeypatch.setitem(KINDS, "snapshots", type(KINDS["snapshots"])(KINDS["snapshots"].count, spy))
    for start in range(0, plan.rows, 3):
        generate_row_slice(plan, start, min(start + 3, plan.rows), root=root_for())
    assert made == [1]


@pytest.mark.parametrize("end", [None, CLOSE_WITH])
def test_the_column_path_writes_the_rows_the_per_row_path_writes(
    monkeypatch: pytest.MonkeyPatch, end: Optional[HistoryEnd]
) -> None:
    plan = snapshot_plan(held(end))
    by_columns, by_rows, taken = _by_both_paths(
        monkeypatch, lambda: generate_row_slice(plan, 0, plan.rows, root=root_for())
    )
    assert taken and all(taken)
    assert by_columns == by_rows and len(by_rows) == plan.rows
    # In slices that start in the middle of a chain, under small column chunks.
    monkeypatch.setattr("flux_engine.generator.columns.CHUNK_ROWS", 7)
    assert generate_row_slice(plan, 0, plan.rows, root=root_for()) == by_rows
    assert generate_row_slice(plan, 5, 17, root=root_for()) == by_rows[5:17]


# -- the sources ----------------------------------------------------------------------------------


def test_the_sources_name_each_entitys_last_row() -> None:
    plan = snapshot_plan(held(CLOSE_WITH))
    chain = history_plan(entry(end=CLOSE_WITH), rows=80, entities=24)
    ends = chain.versions.offsets + chain.versions.counts - 1
    assert sources_of(plan, root_for()).tolist() == ends.tolist()
    assert ends[-1] == 79


def test_a_snapshot_without_a_root_is_refused() -> None:
    plan = snapshot_plan(held())
    with pytest.raises(FluxError) as caught:
        generate_row_slice(plan, 0, 1)
    assert caught.value.code.code == "FLX-3403"
