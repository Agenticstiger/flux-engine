# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Journeys: a per-subject state machine over a taxonomy of legal moves.

``Journey.spec`` is a trigger (``signalRef`` plus an optional ``flux-expr/1``
``condition`` over the signal's payload), an intervention, a
``JourneyTaxonomy``, and one reaction per Persona
(``successProbability`` / ``outcome`` / ``nextState``). One step is:

1. a trigger event arrives and its condition holds;
2. the subject's Persona has a reaction, and the taxonomy declares an edge from
   the subject's **current** state to that reaction's ``nextState``;
3. the reaction succeeds or fails;
4. success moves the subject to ``nextState`` and records ``outcome``; failure
   leaves the state unchanged and records the negated outcome.

**Where the draw lands.** KERNEL-SPEC §6 says the ``campaignResponse`` dial is
the dial of ``Journey.personaReactions[].successProbability``, and that in a
dial's stochastic share "each subject draws ``bernoulli(stream(temperature
path), p)``". So when the World declares the dial, the reaction is realised
through §6 and the draw is on
``("sim", id, "temperature", "campaignResponse", "subject", ordinal, step)``.
When the World declares no such dial there is no partition to make, and the
reaction is a plain Bernoulli on §3's own reaction path,
``("sim", id, "journey", journeyId, "subject", ordinal, step)`` -- the path
whose declared consumer is "reaction draw". Both paths are canonical; which one
a run uses is a property of the World, not of this module.

**The taxonomy is checked twice, for two different failures.** At compile time,
a reaction whose ``nextState`` is not a state of the taxonomy is ``FLX-2302`` --
a document that cannot be executed at all. At run time, a subject standing in a
state from which the taxonomy declares no edge to ``nextState`` is simply not
eligible for that step: no draw, no op, no transition. That is what a taxonomy
of legal moves *is*; raising there would turn a correctly-restrictive taxonomy
into a crash.

**The step budget** bounds how many times one subject may be triggered
(``FLX-2303`` above it), so a bundle whose transitions cycle cannot make the
length of a run depend on its data.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.clock import Phase, Scheduler
from flux_engine.core.errors import FluxError
from flux_engine.core.rng import DrawStream, bernoulli
from flux_engine.core.seedtree import SeedTree, journey_path, temperature_path
from flux_engine.core.trace import TraceOp
from flux_engine.expr.attrs import AttributeModel
from flux_engine.expr.compile import Predicate, compile_source
from flux_engine.expr.types import ScopeModel, nfc
from flux_engine.runtime.cast import Cast, strip_range
from flux_engine.runtime.errors import FLX_2302, FLX_2303
from flux_engine.runtime.signals import SignalEvent
from flux_engine.runtime.temperature import Dial
from flux_engine.world.population import PopulationSpec, SubjectStore
from flux_engine.world.tracing import TraceLog
from flux_engine.world.values import probability_of

__all__ = [
    "DEFAULT_STEP_BUDGET",
    "NEGATED_OUTCOME_PREFIX",
    "REACTION_DIAL",
    "CompiledJourney",
    "Reaction",
    "Transition",
    "compile_journeys",
    "negated_outcome",
    "run_journeys",
]

#: KERNEL-SPEC §6: "``campaignResponse`` scales
#: ``Journey.personaReactions[].successProbability``".
REACTION_DIAL = "campaignResponse"

#: Steps one subject may take in one journey before ``FLX-2303``. Engine-owned
#: and generous: a bundle needs a runaway cycle, not a busy subject, to hit it.
DEFAULT_STEP_BUDGET = 32

#: How a failed reaction's outcome is spelled. FLUX 0.5.0 declares only the
#: success outcome, and a failure must still be distinguishable in the trace
#: from "no step happened at all".
NEGATED_OUTCOME_PREFIX = "not_"


def negated_outcome(outcome: str) -> str:
    """The outcome a failed reaction records."""
    return f"{NEGATED_OUTCOME_PREFIX}{nfc(outcome)}"


@dataclass(frozen=True)
class Reaction:
    """One Persona's reaction to a journey trigger."""

    persona_id: str
    success_probability: Decimal
    outcome: str
    next_state: Optional[str]


@dataclass(frozen=True)
class CompiledJourney:
    """A ``Journey`` with its condition compiled and its taxonomy resolved."""

    journey_id: str
    signal_id: str
    condition: Optional[Predicate]
    condition_source: Optional[str]
    scope: Optional[ScopeModel]
    states: Tuple[str, ...]
    transitions: FrozenSet[Tuple[str, str]]
    reactions: Mapping[str, Reaction]
    stream_ordinal: int

    def triggered_by(self, event: SignalEvent) -> bool:
        """Whether *event* triggers this journey."""
        if event.signal_id != self.signal_id:
            return False
        if self.condition is None or self.scope is None:
            return True
        return self.condition(event.view(self.scope))

    def may_move(self, from_state: str, to_state: Optional[str]) -> bool:
        """Whether the taxonomy declares the edge this reaction would take.

        A reaction with no ``nextState`` moves nobody, and is legal from every
        state: it records an outcome and leaves the subject where it was.
        """
        if to_state is None:
            return True
        return (nfc(from_state), nfc(to_state)) in self.transitions


@dataclass(frozen=True)
class Transition:
    """One §10 ``transition``: what happened to one subject at one step."""

    journey_id: str
    ordinal: int
    step: int
    t_ms: int
    from_state: str
    to_state: str
    outcome: str
    success: bool


def compile_journeys(cast: Cast, model: AttributeModel) -> Tuple[CompiledJourney, ...]:
    """Compile every ``Journey`` in *cast*, NFC-sorted by id."""
    built: List[CompiledJourney] = []
    for journey_id, document in cast.of_kind("Journey"):
        spec = document.get("spec", {})
        trigger = spec.get("trigger", {})
        signal_id = strip_range(trigger.get("signalRef", ""))
        source = trigger.get("condition")
        scope = model.event_scope(signal_id) if source else None
        taxonomy_ref = spec.get("taxonomyRef")
        states, transitions = _taxonomy(cast, taxonomy_ref)
        reactions: Dict[str, Reaction] = {}
        for persona_ref in sorted(spec.get("personaReactions", {}) or {}, key=nfc):
            declared = spec["personaReactions"][persona_ref]
            persona_id = strip_range(persona_ref)
            next_state = declared.get("nextState")
            if next_state is not None and nfc(str(next_state)) not in states:
                raise FluxError(
                    FLX_2302,
                    f"Journey {journey_id!r} reaction for {persona_id!r} moves to "
                    f"{next_state!r}, which is not a state of {taxonomy_ref!r}",
                )
            reactions[persona_id] = Reaction(
                persona_id=persona_id,
                success_probability=probability_of(
                    declared.get("successProbability"),
                    what=f"Journey {journey_id!r} reaction {persona_id!r} successProbability",
                ),
                outcome=nfc(str(declared.get("outcome", journey_id))),
                next_state=nfc(str(next_state)) if next_state is not None else None,
            )
        built.append(
            CompiledJourney(
                journey_id=journey_id,
                signal_id=signal_id,
                condition=compile_source(str(source), scope) if source and scope else None,
                condition_source=nfc(str(source)) if source else None,
                scope=scope,
                states=states,
                transitions=transitions,
                reactions=reactions,
                stream_ordinal=cast.stream_ordinal(journey_id),
            )
        )
    return tuple(built)


def _taxonomy(
    cast: Cast, taxonomy_ref: Optional[str]
) -> Tuple[Tuple[str, ...], FrozenSet[Tuple[str, str]]]:
    """The referenced taxonomy's states and edges, NFC-normalised."""
    if taxonomy_ref is None:
        return (), frozenset()
    document = cast.document(str(taxonomy_ref))
    spec = document.get("spec", {})
    states = tuple(sorted({nfc(str(state)) for state in spec.get("states", []) or []}))
    edges = frozenset(
        (nfc(str(edge["from"])), nfc(str(edge["to"])))
        for edge in spec.get("transitions", []) or []
        if isinstance(edge, Mapping) and "from" in edge and "to" in edge
    )
    return states, edges


@dataclass(frozen=True)
class _Step:
    """A trigger that passed the condition, before its reaction is realised."""

    ordinal: int
    step: int
    t_ms: int
    reaction: Reaction


def run_journeys(
    *,
    cast: Cast,
    world: PopulationSpec,
    journeys: Sequence[CompiledJourney],
    events: Sequence[SignalEvent],
    store: SubjectStore,
    seed_tree: SeedTree,
    trace: TraceLog,
    step_budget: int = DEFAULT_STEP_BUDGET,
) -> Tuple[Transition, ...]:
    """Run every journey over every subject's events, tracing each transition."""
    sim_id = cast.simulation_id
    dial = Dial.declared(world.temperature, REACTION_DIAL)
    dialled = REACTION_DIAL in world.temperature
    scheduler: Scheduler[Transition] = Scheduler()

    for journey in journeys:
        steps = _steps(journey, events, store, step_budget)
        outcomes = _realise(
            journey=journey,
            steps=steps,
            dial=dial,
            dialled=dialled,
            sim_id=sim_id,
            seed_tree=seed_tree,
        )
        state: Dict[int, str] = {}
        for step in steps:
            current = state.get(step.ordinal, store.lifecycle(step.ordinal))
            if not journey.may_move(current, step.reaction.next_state):
                continue
            success = outcomes[(step.ordinal, step.step)]
            target = step.reaction.next_state
            to_state = target if (success and target is not None) else current
            if success:
                state[step.ordinal] = to_state
            scheduler.schedule(
                t_ms=step.t_ms,
                phase=Phase.JOURNEY,
                entity_ordinal=step.ordinal,
                stream_ordinal=journey.stream_ordinal,
                payload=Transition(
                    journey_id=journey.journey_id,
                    ordinal=step.ordinal,
                    step=step.step,
                    t_ms=step.t_ms,
                    from_state=current,
                    to_state=to_state,
                    outcome=(
                        step.reaction.outcome if success else negated_outcome(step.reaction.outcome)
                    ),
                    success=success,
                ),
            )

    made: List[Transition] = []
    for item in scheduler.drain():
        transition = item.payload
        made.append(transition)
        trace.record(
            TraceOp.TRANSITION,
            t=transition.t_ms,
            journeyId=transition.journey_id,
            ordinal=transition.ordinal,
            fromState=transition.from_state,
            toState=transition.to_state,
            outcome=transition.outcome,
            success=transition.success,
        )
    return tuple(made)


def _steps(
    journey: CompiledJourney,
    events: Sequence[SignalEvent],
    store: SubjectStore,
    step_budget: int,
) -> Tuple[_Step, ...]:
    """Every trigger of *journey*, in ``(ordinal, step)`` canonical order."""
    counters: Dict[int, int] = {}
    by_subject: Dict[int, List[_Step]] = {}
    for event in events:
        if not journey.triggered_by(event):
            continue
        reaction = journey.reactions.get(store.persona(event.ordinal))
        if reaction is None:
            continue
        step = counters.get(event.ordinal, 0)
        if step >= step_budget:
            raise FluxError(
                FLX_2303,
                f"Journey {journey.journey_id!r}: subject {event.ordinal} reached step "
                f"{step} of a budget of {step_budget}",
            )
        counters[event.ordinal] = step + 1
        by_subject.setdefault(event.ordinal, []).append(
            _Step(ordinal=event.ordinal, step=step, t_ms=event.t_ms, reaction=reaction)
        )
    return tuple(step for ordinal in sorted(by_subject) for step in by_subject[ordinal])


def _realise(
    *,
    journey: CompiledJourney,
    steps: Sequence[_Step],
    dial: Dial,
    dialled: bool,
    sim_id: str,
    seed_tree: SeedTree,
) -> Mapping[Tuple[int, int], bool]:
    """Decide every step's success (see this module's docstring for which path).

    §6 partitions "an eligible set of subjects", so the dial is applied once per
    ``(step index, Persona)``: at step *k* the eligible set is the subjects that
    reached step *k* with that Persona's reaction, in ordinal order, and the
    temperature path's ``occurrence`` is *k*. The split by Persona matters
    because §6's frozen share allocates ``LRM(count, p)`` for **one** ``p``, and
    two Personas may declare different ``successProbability``.
    """
    by_step: Dict[Tuple[int, str], List[_Step]] = {}
    for step in steps:
        by_step.setdefault((step.step, step.reaction.persona_id), []).append(step)

    decided: Dict[Tuple[int, int], bool] = {}
    for key in sorted(by_step):
        index, _persona_id = key
        group = by_step[key]
        rate = group[0].reaction.success_probability
        ordinals = tuple(step.ordinal for step in group)
        if dialled:
            realisation = dial.realise(
                ordinals,
                rate,
                partial(_dial_stream, seed_tree, sim_id, dial.name, index),
            )
            for ordinal in ordinals:
                decided[(ordinal, index)] = ordinal in realisation
        else:
            for ordinal in ordinals:
                stream = _journey_stream(seed_tree, sim_id, journey.journey_id, index, ordinal)
                decided[(ordinal, index)] = bernoulli(stream, rate)
    return decided


def _dial_stream(
    seed_tree: SeedTree, sim_id: str, dial: str, occurrence: int, ordinal: int
) -> DrawStream:
    return seed_tree.stream(temperature_path(sim_id, dial, ordinal, occurrence))


def _journey_stream(
    seed_tree: SeedTree, sim_id: str, journey_id: str, step: int, ordinal: int
) -> DrawStream:
    return seed_tree.stream(journey_path(sim_id, journey_id, ordinal, step))
