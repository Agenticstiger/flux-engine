# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A rule that reads ``previous`` on a tracking history replays each entity's chain once.

Row ``i``'s previous version is read as the expose's own pass writes it, one row at a time, and the
tracked levels of that row are the draws of every version of its entity before it. Reading them
afresh for each row replayed the chain once per row, so a plan cost the rows times the versions
per entity. What is pinned:

* **The work.** The chain replays a pass makes number at most the entities it reaches, however
  many versions each has and however many rows it reads.
* **The rows.** They equal the rows the replay of every read gives, and a digest taken before
  the levels were kept.
* **The scope.** What is kept belongs to one run's root, never to another run's.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Callable, List, Sequence

import pytest

from flux_engine.generator import history, relations
from flux_engine.generator import rows as rows_module
from flux_engine.generator.rows import generate_row_slice, generate_rows
from flux_engine.seam.counts import ChildShape
from flux_engine.seam.history import VERSION_COUNTS_RANK

from .test_history_end import EMIT, fresh_layouts  # noqa: F401  (an autouse fixture)
from .test_history_layout import root_for
from .test_history_previous import PRIOR_PLAN, plans_with
from .test_history_tracked import BOTH, tracked_entry

pytestmark = pytest.mark.unit

ENTITIES = 16
SHALLOW = (ChildShape("bins", ("1", "2", "4", "9"), ("0.45", "0.35", "0.20")), 60)
DEEP = (ChildShape("bins", ("4", "9", "17", "25"), ("0.3", "0.3", "0.4")), 220)
#: The SHA-256 of the rows of ``SHALLOW`` and of ``DEEP``, taken from the replay of every read
#: before the levels were kept.
PINNED = {
    "shallow": "sha256:a5fa3edf1df3c5fd3fdf1a1b7e4e0209f244d3592fbb60dcc13dd1f4f90281d7",
    "deep": "sha256:92afee47a0f0ff6593e6b997e87eaa458f04472aa73d6df007d8973f6827b3ed",
}


def _plan_of(shape: Any) -> Any:
    versions, rows = shape
    # The rank coupling, as a plan recorded before the fit has it: the digests are those it had.
    held = tracked_entry(
        BOTH, entities=ENTITIES, versions=versions, version_counts=VERSION_COUNTS_RANK
    )
    return plans_with(held, [PRIOR_PLAN], rows=rows, entities=ENTITIES)[EMIT]


def _count(
    monkeypatch: pytest.MonkeyPatch, owners: Sequence[Any], name: str, size: Callable[..., int]
) -> List[int]:
    """The size of each call of *name*, on every module of *owners* that has it."""
    seen: List[int] = []
    original = getattr(owners[0], name)

    def counting(*args: Any, **kwargs: Any) -> Any:
        seen.append(size(*args))
        return original(*args, **kwargs)

    for owner in owners:
        if hasattr(owner, name):
            monkeypatch.setattr(owner, name, counting)
    return seen


def _count_replays(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    """The rows of each replay of chains, whichever module the reader reaches it by."""
    return _count(monkeypatch, (history, relations), "tracked_levels", lambda *a: a[-1].shape[0])


def _count_slices(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    """Each slice a reader asks the generator for, as a final row is read."""
    return _count(monkeypatch, (rows_module,), "generate_row_slice", lambda *a: a[2] - a[1])


def _singly(plan: Any, rows: Sequence[int]) -> List[Any]:
    """Each of *rows* as a slice of its own, which is how a rule reads a previous version."""
    root = root_for()
    return [generate_row_slice(plan, row, row + 1, root=root)[0] for row in rows]


def _digest(rows: Any) -> str:
    text = json.dumps([list(row) for row in rows], default=str)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize(
    ("shape", "rows", "replays"),
    [(SHALLOW, tuple(range(12)), ENTITIES), (DEEP, (40, 41, 42), 3)],
)
def test_single_rows_replay_each_chain_at_most_once_whatever_their_depth(
    monkeypatch: pytest.MonkeyPatch, shape: Any, rows: Sequence[int], replays: int
) -> None:
    plan = _plan_of(shape)
    replayed = _count_replays(monkeypatch)
    asked = _count_slices(monkeypatch)
    _singly(plan, rows)
    assert replayed
    assert len(replayed) <= replays, (len(replayed), plan.rows)
    assert len(asked) <= 3 * len(rows), (len(asked), len(rows))


def test_the_whole_table_replays_no_more_chains_than_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for shape in (SHALLOW, DEEP):
        replayed = _count_replays(monkeypatch)
        generate_rows(_plan_of(shape), root=root_for())
        assert 0 < len(replayed) <= ENTITIES


@pytest.mark.parametrize(("name", "shape"), [("shallow", SHALLOW), ("deep", DEEP)])
def test_the_rows_keep_the_digest_they_had(name: str, shape: Any) -> None:
    assert _digest(generate_rows(_plan_of(shape), root=root_for())) == PINNED[name]


@pytest.mark.parametrize("shape", [SHALLOW, DEEP])
def test_the_rows_are_the_rows_of_the_replay_of_every_read(
    monkeypatch: pytest.MonkeyPatch, shape: Any
) -> None:
    plan = _plan_of(shape)
    picked = (0, 1, 2, 3, 40, 41, 42, plan.rows - 1)
    kept = (generate_rows(plan, root=root_for()), _singly(plan, picked))
    monkeypatch.setattr(
        relations,
        "tracked_levels_kept",
        lambda root, emit, specs, plan, rows: history.tracked_levels(
            root, *emit, specs, plan, rows
        ),
    )
    assert (generate_rows(plan, root=root_for()), _singly(plan, picked)) == kept


def test_what_is_kept_belongs_to_one_run_and_never_to_another() -> None:
    plan = _plan_of(SHALLOW)
    first = generate_rows(plan, root=root_for(5))
    other = generate_rows(plan, root=root_for(6))
    assert other != first
    assert generate_rows(plan, root=root_for(5)) == first
    assert generate_rows(plan, root=root_for(6)) == other


def _kept_inputs() -> Any:
    """The root, the emit, the tracked specs and the layout a reader hands to the kept levels."""
    plan = _plan_of(SHALLOW)
    root = root_for()
    reader = relations.RelationReader(root)
    reader.know(plan)
    owner = relations.owner_of(plan)
    pick = relations.history_pick_of(plan)
    drivers = [plan.relations.driver(column.column.name) for column in plan.columns]
    specs = next(d for d in drivers if isinstance(d, relations.TrackedColumn)).specs
    return root, (owner.sim_id, owner.expose_id), specs, reader._chain_of(owner, pick)


def test_a_slice_does_not_depend_on_the_store_starting_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, emit, specs, layout = _kept_inputs()
    monkeypatch.setattr(history, "_KEPT_ENTITIES", 3)
    first = layout.versions.offsets[[0, 1, 2]].astype("int64")
    # Entity 0 is held, entity 3 is not, and keeping both is over the cap: the store starts over.
    mixed = layout.versions.offsets[[0, 3]].astype("int64")
    for rows in (first, mixed, first):
        kept = history.tracked_levels_kept(root, emit, specs, layout, rows)
        plain = history.tracked_levels(root, *emit, specs, layout, rows)
        assert [column.tolist() for column in kept] == [column.tolist() for column in plain]


def test_a_slice_with_no_tracked_columns_is_answered() -> None:
    root, emit, _, layout = _kept_inputs()
    rows = layout.versions.offsets[[0, 1]].astype("int64")
    assert history.tracked_levels_kept(root, emit, (), layout, rows) == []


def test_two_threads_each_get_the_reference_rows() -> None:
    plan = _plan_of(SHALLOW)
    reference = generate_rows(plan, root=root_for())
    picked = (0, 1, 2, 3, 40, 41, 42)
    expected = _singly(plan, picked)
    results: List[Any] = []

    def work() -> None:
        results.append((generate_rows(plan, root=root_for()), _singly(plan, picked)))

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [(reference, expected)] * 2
