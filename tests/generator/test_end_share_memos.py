# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The memos keyed by a plan digest hold what an ``end`` share is a share of.

Two plans of one history that differ only in it have one digest, and their rows differ. Each memo
that reads those rows keeps one entry per plan, so one run's rows are never served to the other.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.generator import events as events_module
from flux_engine.generator import history, parent_bounds, projections, relations
from flux_engine.generator import rows as generator_rows
from flux_engine.generator import rules as rules_module
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.relations import RelationReader, run_set_key
from flux_engine.generator.rows import GeneratorPlan
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.history import (
    END_SHARE_OF_ELIGIBLE,
    END_SHARE_OF_ENTITIES,
    HistoryEnd,
    HistoryParent,
)

from .test_event_projection import EMIT, events_plan, held
from .test_history_layout import root_for
from .test_projection_plan import fresh_sources  # noqa: F401  (an autouse fixture)

pytestmark = pytest.mark.unit


def _pair() -> Dict[str, Dict[str, GeneratorPlan]]:
    """The plans of one history that end a share of the entities and of the eligible ones."""
    return {
        name: held(HistoryEnd("closeLast", "0.9", "status", "gone", share_of=name), rows=40)
        for name in (END_SHARE_OF_ENTITIES, END_SHARE_OF_ELIGIBLE)
    }


def test_the_run_set_key_tells_two_plans_of_one_digest_apart() -> None:
    first, second = (plans[EMIT] for plans in _pair().values())
    assert first.digest() == second.digest()
    assert run_set_key(first) != run_set_key(second)


def test_the_weighted_pick_thresholds_are_kept_per_parent_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(relations, "_THRESHOLDS", {})
    root = root_for()
    for plans in _pair().values():
        plan = plans[EMIT]
        reader = RelationReader(root)
        reader._plans = {plan.expose.expose_id: plan}  # type: ignore[assignment]
        driver = SimpleNamespace(
            weight="price",
            weight_values=None,
            parent=SimpleNamespace(expose_id=plan.expose.expose_id),
            relationship="r",
        )
        reader._thresholds(driver)  # type: ignore[arg-type]
    assert len(relations._THRESHOLDS) == 2


def test_the_whole_rows_of_a_windowed_plan_are_kept_per_end_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generator_rows, "_WHOLE", [])
    seen: List[Any] = []
    monkeypatch.setattr(
        rules_module, "apply_rules_whole", lambda plan, rows, root: seen.append(plan) or rows
    )
    root = root_for()
    for plans in _pair().values():
        plan = plans[EMIT]
        generator_rows._whole_rows(plan, CellStreams(root, plan.sim_id, plan.expose.expose_id))
    assert len(seen) == 2 and len(generator_rows._WHOLE) == 2


def test_the_whole_projected_emit_is_kept_per_end_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(projections, "_WHOLE", {})
    root = root_for()
    made: List[Any] = []

    def build(plan: Any, sources: Any, start: int, stop: int, run: Any) -> Any:
        made.append(plan)
        return ()

    first, second = _pair().values()
    # Two projected plans of one digest, each over the history of its own end share.
    base = events_plan(first)
    other = replace(base, projection=replace(base.projection, history=second[EMIT]))
    assert base.digest() == other.digest()
    for plan in (base, other):
        projections._whole(plan, plan, build, root)
    assert len(made) == 2 and len(projections._WHOLE) == 2


def _sums(parent: GeneratorPlan, child: GeneratorPlan) -> GeneratorPlan:
    """*parent* summarising *child*, as a pass attaches a child's plan to the plan it counts them by."""
    return replace(parent, sums=((child.expose.expose_id, "r", child),))


def test_two_plans_that_sum_a_history_under_two_end_shares_have_two_run_set_keys() -> None:
    """The share is read through the child plans a plan sums, as through the plans it reads."""
    pair = _pair()
    parents = [_sums(plans["customer"], plans[EMIT]) for plans in pair.values()]
    first, second = parents
    assert first.digest() == second.digest()
    assert first.reads == () and not first.projection
    assert run_set_key(first) != run_set_key(second)
    assert dict(run_set_key(first)[0]) == {EMIT: END_SHARE_OF_ENTITIES}
    assert dict(run_set_key(second)[0]) == {EMIT: END_SHARE_OF_ELIGIBLE}


def test_a_plan_reached_by_two_paths_is_walked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A diamond of reads and sums costs one visit per plan, not one per path to it."""
    plans = _pair()[END_SHARE_OF_ENTITIES]
    plan = plans[EMIT]
    for _ in range(8):
        plan = replace(
            plans["customer"],
            reads=(("a", plan), ("b", plan)),
            sums=(("c", "r", plan), ("d", "r", plan)),
        )
    visits: List[int] = []
    real = relations.history_pick_of
    monkeypatch.setattr(relations, "history_pick_of", lambda here: visits.append(1) or real(here))
    assert dict(relations.end_shares_of(plan)) == {EMIT: END_SHARE_OF_ENTITIES}
    assert len(visits) == 9


def _bounds_of(parent: GeneratorPlan, root: Any) -> Tuple[Any, ...]:
    held = parent_bounds.deferred_bound(root, parent, ("valid_from", "valid_to"), LogicalKind.DATE)
    found = held.get()
    return tuple(
        array.tolist() for array in (found.starts, found.has_start, found.ends, found.has_end)
    )


def test_the_lifetimes_of_a_parents_rows_are_kept_per_end_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two plans of one digest, each ending its entities its own way, keep the bounds of their own rows."""
    monkeypatch.setattr(parent_bounds, "_BOUNDS", {})
    root = root_for()
    parents = {name: plans[EMIT] for name, plans in _pair().items()}
    assert len({plan.digest() for plan in parents.values()}) == 1

    def alone(name: str) -> Any:
        parent_bounds._BOUNDS.clear()
        return _bounds_of(parents[name], root)

    expected = {name: alone(name) for name in parents}
    assert expected[END_SHARE_OF_ENTITIES] != expected[END_SHARE_OF_ELIGIBLE]
    parent_bounds._BOUNDS.clear()
    for name in (*parents, END_SHARE_OF_ENTITIES):
        assert _bounds_of(parents[name], root) == expected[name], name


def _about(plan: GeneratorPlan, root: Any) -> List[Any]:
    layout = projections.sources_of(plan, root)
    return [array.tolist() for array in (layout.about, layout.op, layout.before, layout.closing)]


def test_the_sources_of_a_projected_plan_are_kept_per_end_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two projected plans of one digest over histories that end differently keep their own sources."""
    monkeypatch.setattr(projections, "_SOURCES", {})
    root = root_for()
    first, second = _pair().values()
    base = events_plan(first)
    other = replace(base, projection=replace(base.projection, history=second[EMIT]))
    assert base.digest() == other.digest()
    plans = {END_SHARE_OF_ENTITIES: base, END_SHARE_OF_ELIGIBLE: other}
    # The events layout is kept apart from the sources, so what is held back here is the sources'.
    sources = {}
    for name, plan in plans.items():
        events_module.clear_layouts()
        projections._SOURCES.clear()
        sources[name] = _about(plan, root)
    assert sources[END_SHARE_OF_ENTITIES] != sources[END_SHARE_OF_ELIGIBLE]
    projections._SOURCES.clear()
    for name in (*plans, END_SHARE_OF_ENTITIES):
        events_module.clear_layouts()
        assert _about(plans[name], root) == sources[name], name


def _counted_under(share_of: str) -> GeneratorPlan:
    """A counted child whose parent reaches a history that ends a share of *share_of*.

    The parent is the account of the counted-picks plans with that history among the child plans it
    sums, so its digest holds the history's and is the same for either share. The child reads it
    as a pass attaches it.
    """
    from .test_counted_picks import SHAPE, when_counts
    from .test_counted_picks import plans_of as counted_plans

    plans = counted_plans(counts=when_counts(shape=SHAPE))
    history_plan = _pair()[share_of][EMIT]
    account = _sums(plans["account"], history_plan)
    return replace(
        plans["invoice"],
        reads=tuple(
            (expose_id, account if expose_id == "account" else read)
            for expose_id, read in plans["invoice"].reads
        ),
    )


def test_the_counted_layout_is_kept_per_end_share_of_the_parents_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A counted layout reads which parent rows are eligible; two parents of one digest keep two layouts."""
    monkeypatch.setattr(relations, "_LAYOUTS", {})
    read: List[Any] = []
    original = RelationReader._read_parent

    def spy(self: Any, parent: Any, *args: Any) -> Any:
        read.append(parent)
        return original(self, parent, *args)

    monkeypatch.setattr(RelationReader, "_read_parent", spy)
    root = root_for()
    children = {
        name: _counted_under(name) for name in (END_SHARE_OF_ENTITIES, END_SHARE_OF_ELIGIBLE)
    }
    first, second = children.values()
    assert first.digest() == second.digest()
    for _ in range(2):
        for plan in children.values():
            generator_rows.generate_rows(plan, root=root)
    assert len(read) == 2 and len(relations._LAYOUTS) == 2


def test_the_entity_layout_of_a_history_is_kept_per_end_share_of_its_parents_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entity layout of a history reads its parent's rows, and keeps them under the parent's key."""
    from .test_history_layout import entry, layout_of, plans_of

    monkeypatch.setattr(history, "_OUTERS", {})
    monkeypatch.setattr(history, "_PLANS", {})
    monkeypatch.setattr(relations, "_LAYOUTS", {})
    held_entry = entry(parent=HistoryParent("customer", parent_when="c_score > 4"))
    base = plans_of(history_entry=held_entry, entities=4, rows=16)
    made: Dict[str, GeneratorPlan] = {}
    for share_of, pair in _pair().items():
        parent = _sums(base["customer"], pair[EMIT])
        reads = tuple(
            (name, parent if name == "customer" else read) for name, read in base[EMIT].reads
        )
        made[share_of] = replace(base[EMIT], reads=reads)
    first, second = made.values()
    assert first.digest() == second.digest()
    for _ in range(2):
        for plan in made.values():
            layout_of({**base, EMIT: plan})
    assert len(history._OUTERS) == 2
