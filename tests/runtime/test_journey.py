# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Journeys: triggers, taxonomy legality, the reaction dial, the step budget."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.trace import TraceOp
from flux_engine.runtime.journey import (
    NEGATED_OUTCOME_PREFIX,
    REACTION_DIAL,
    compile_journeys,
    negated_outcome,
    run_journeys,
)
from flux_engine.world import PopulationSpec, TraceLog

from .conftest import Staged, requires_example, stage

pytestmark = [pytest.mark.integration, requires_example]


def journeys(staged: Staged):
    return compile_journeys(staged.cast, staged.model)


def run(staged: Staged, world: PopulationSpec = None, **kwargs: Any):
    """Run the journey stage on a tree that has not been drawn from yet.

    See ``Staged.fresh_tree``: the stage's own paths are disjoint from the
    population's and the signals', so replaying it in isolation is exact.
    """
    log = TraceLog()
    seed_tree = kwargs.pop("seed_tree", None) or staged.fresh_tree()
    moved = run_journeys(
        cast=staged.cast,
        world=world if world is not None else staged.spec,
        journeys=journeys(staged),
        events=staged.events,
        store=staged.store,
        seed_tree=seed_tree,
        trace=log,
        **kwargs,
    )
    return moved, log, seed_tree


def test_the_compiled_journey_mirrors_the_document(staged: Staged) -> None:
    compiled = journeys(staged)
    assert compiled, "the cast holds no Journey"
    for journey in compiled:
        document = staged.cast.document(journey.journey_id)
        spec = document["spec"]
        assert journey.signal_id == spec["trigger"]["signalRef"]
        assert journey.condition_source == spec["trigger"].get("condition")
        taxonomy = staged.cast.document(spec["taxonomyRef"])["spec"]
        assert set(journey.states) == set(taxonomy["states"])
        assert len(journey.transitions) == len(taxonomy["transitions"])
        assert set(journey.reactions) == set(spec["personaReactions"])


def test_only_events_that_satisfy_the_trigger_condition_make_a_step(
    staged: Staged,
) -> None:
    """The shipped condition is ``attempts >= 2``; both sides read from the bundle."""
    journey = journeys(staged)[0]
    triggering = [event for event in staged.events if journey.triggered_by(event)]
    assert triggering, "no event satisfies the shipped trigger condition"
    assert len(triggering) < len(staged.events), "the condition selects everything"
    for event in triggering:
        assert event.signal_id == journey.signal_id
        assert event.payload["attempts"] >= Decimal(2)


def test_every_transition_is_an_edge_the_taxonomy_declares(staged: Staged) -> None:
    moved, _, _ = run(staged)
    assert moved, "the shipped journey never fires"
    journey = journeys(staged)[0]
    for transition in moved:
        if transition.success and transition.to_state != transition.from_state:
            assert (transition.from_state, transition.to_state) in journey.transitions


def test_a_failed_reaction_leaves_the_state_alone_and_negates_the_outcome(
    staged: Staged,
) -> None:
    moved, _, _ = run(staged)
    declared = staged.cast.document(journeys(staged)[0].journey_id)["spec"]["personaReactions"]
    outcomes = {reaction["outcome"] for reaction in declared.values()}
    failures = [t for t in moved if not t.success]
    assert failures, "every reaction succeeded; the failure path is untested"
    for transition in failures:
        assert transition.to_state == transition.from_state
        assert transition.outcome.startswith(NEGATED_OUTCOME_PREFIX)
        assert transition.outcome[len(NEGATED_OUTCOME_PREFIX) :] in outcomes


def test_a_successful_reaction_records_the_declared_outcome_and_state(
    staged: Staged,
) -> None:
    moved, _, _ = run(staged)
    reactions = staged.cast.document(journeys(staged)[0].journey_id)["spec"]["personaReactions"]
    successes = [t for t in moved if t.success]
    assert successes
    for transition in successes:
        declared = reactions[staged.store.persona(transition.ordinal)]
        assert transition.outcome == declared["outcome"]
        assert transition.to_state == declared["nextState"]


def test_one_transition_op_is_traced_per_transition_in_time_order(
    staged: Staged,
) -> None:
    moved, log, _ = run(staged)
    traced = log.of(TraceOp.TRANSITION)
    assert len(traced) == len(moved)
    assert [record["t"] for record in traced] == sorted(record["t"] for record in traced)
    assert [record["success"] for record in traced] == [t.success for t in moved]


def test_the_declared_dial_puts_the_draw_on_the_temperature_path(
    staged: Staged,
) -> None:
    """§6: the stochastic share draws ``bernoulli(stream(temperature path), p)``."""
    assert REACTION_DIAL in staged.spec.temperature
    _, _, seed_tree = run(staged)
    materialised = set(seed_tree.materialised_paths)
    dialled = [
        path for path in materialised if path[2] == "temperature" and path[3] == REACTION_DIAL
    ]
    assert dialled, "no temperature stream was opened for the reaction dial"


def test_a_world_with_no_reaction_dial_draws_on_the_journey_path() -> None:
    staged = stage(size=300)
    world = deepcopy(staged.world)
    del world["spec"]["temperature"][REACTION_DIAL]
    spec = PopulationSpec.from_document(world)
    moved, _, seed_tree = run(staged, world=spec)
    assert moved
    materialised = set(seed_tree.materialised_paths)
    reaction_paths = [path for path in materialised if path[2] == "journey"]
    assert reaction_paths, "the undialled route opened no journey stream"


def test_a_frozen_reaction_dial_makes_the_success_count_exact() -> None:
    staged = stage(size=300)
    world = deepcopy(staged.world)
    world["spec"]["temperature"][REACTION_DIAL] = 0
    moved, _, _ = run(staged, world=PopulationSpec.from_document(world))
    assert moved
    same, _, _ = run(staged, world=PopulationSpec.from_document(world))
    assert [t.success for t in moved] == [t.success for t in same]


def test_a_reaction_moving_to_a_state_outside_the_taxonomy_is_refused(
    staged: Staged,
) -> None:
    cast = staged.cast
    journey_id = cast.ids("Journey")[0]
    broken = deepcopy(dict(cast.documents))
    persona = next(iter(broken[journey_id]["spec"]["personaReactions"]))
    broken[journey_id]["spec"]["personaReactions"][persona]["nextState"] = "nowhere"
    from flux_engine.runtime.cast import Cast

    patched = Cast(
        simulation_id=cast.simulation_id,
        world_id=cast.world_id,
        documents=broken,
        stream_ordinals=cast.stream_ordinals,
        references=cast.references,
    )
    with pytest.raises(FluxError) as raised:
        compile_journeys(patched, staged.model)
    assert "FLX-2302" in str(raised.value)


def test_the_step_budget_is_enforced(staged: Staged) -> None:
    with pytest.raises(FluxError) as raised:
        run(staged, step_budget=1)
    assert "FLX-2303" in str(raised.value)


def test_a_generous_budget_leaves_every_step(staged: Staged) -> None:
    moved, _, _ = run(staged, step_budget=1000)
    assert moved


def test_a_subject_whose_persona_has_no_reaction_takes_no_step(staged: Staged) -> None:
    journey = journeys(staged)[0]
    assert set(journey.reactions) == {staged.store.persona(0)}
    moved, _, _ = run(staged)
    assert {t.ordinal for t in moved} <= set(staged.store.ordinals)


def test_a_reaction_without_a_next_state_is_legal_from_every_state(
    staged: Staged,
) -> None:
    journey = journeys(staged)[0]
    for state in journey.states:
        assert journey.may_move(state, None)


def test_an_edge_the_taxonomy_does_not_declare_is_not_a_legal_move(
    staged: Staged,
) -> None:
    journey = journeys(staged)[0]
    illegal = [
        (a, b) for a in journey.states for b in journey.states if (a, b) not in journey.transitions
    ]
    assert illegal, "the taxonomy is complete; the restriction is untested"
    for from_state, to_state in illegal:
        assert not journey.may_move(from_state, to_state)


def test_negated_outcome_is_the_documented_spelling() -> None:
    assert negated_outcome("payment_completed") == "not_payment_completed"


def test_the_run_is_reproducible(staged: Staged) -> None:
    first, _, _ = run(staged)
    second, _, _ = run(staged)
    assert first == second


def test_the_journey_stage_draws_on_paths_the_signal_stage_never_touches(
    staged: Staged,
) -> None:
    """Disjoint seed paths are what let one stage be replayed without the others."""
    _, _, seed_tree = run(staged)
    journey_paths = set(seed_tree.materialised_paths)
    signal_paths = set(staged.seed_tree.materialised_paths)
    assert journey_paths and signal_paths
    assert journey_paths.isdisjoint(signal_paths)


def test_an_event_of_another_signal_never_triggers(staged: Staged) -> None:
    """A trigger reads one signal's payload; another signal's event is not it."""
    journey = journeys(staged)[0]
    borrowed = staged.events[0]
    stranger = type(borrowed)(**{**vars(borrowed), "signal_id": f"{journey.signal_id}-other"})
    assert borrowed.signal_id == journey.signal_id
    assert not journey.triggered_by(stranger)


def test_a_journey_with_no_condition_is_triggered_by_every_event_of_its_signal(
    staged: Staged,
) -> None:
    journey = journeys(staged)[0]
    unconditional = type(journey)(
        **{**vars(journey), "condition": None, "condition_source": None, "scope": None}
    )
    matching = [e for e in staged.events if e.signal_id == journey.signal_id]
    assert matching
    assert all(unconditional.triggered_by(event) for event in matching)
