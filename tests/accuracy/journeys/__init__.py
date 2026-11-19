# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 6, journey conformance -- and the staging the runtime-kind tests share.

The journey, signal, campaign and experiment stages are measured on populations
far larger than the shipped example's 25 000 subjects materialises quickly
(materialising a subject's traits costs about 2 ms, and none of that is under
test here). So the tests keep the real *documents* -- the cast, the compiled
journey, the clock, the World's temperature block -- and replace only the subject
store with :class:`SyntheticStore`, which answers the five questions the stages
ask of a store (``ordinals``, ``persona``, ``lifecycle``, ``identifier``,
``market``/``view``) from a formula. Every draw still goes through the real
runtime stage, its real seed paths and the real kernel samplers.

Importing this package needs neither scipy nor the sibling FLUX checkout;
only :func:`base` needs the latter, and it fails (or skips, as the runtime
suite does) when the shipped example is not there.
"""

from __future__ import annotations

import dataclasses
from copy import deepcopy
from decimal import Decimal
from functools import lru_cache
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from flux_engine.core.seedtree import SeedTree, derive_root
from flux_engine.runtime.journey import (
    CompiledJourney,
    Reaction,
    Transition,
    compile_journeys,
    run_journeys,
)
from flux_engine.runtime.signals import SignalEvent
from flux_engine.world import PopulationSpec, TraceLog
from tests.runtime.conftest import Staged, requires_example, stage

__all__ = [
    "SEAM",
    "SHIPPED_PERSONA",
    "SyntheticStore",
    "base",
    "fresh_tree",
    "population_spec",
    "reaction",
    "requires_example",
    "run",
    "synthetic_event",
    "with_reactions",
]

#: The ``H_bytes`` seam of every run here. Any non-empty value would do.
SEAM = b"H"

#: The one Persona the shipped ``Journey`` declares a reaction for.
SHIPPED_PERSONA = "persona-pragmatic-family"


@lru_cache(maxsize=1)
def base() -> Staged:
    """The shipped example, staged at 40 subjects: its cast, pack, clock and World."""
    return stage(40)


def fresh_tree(seed: int) -> SeedTree:
    """A seed tree at the root the run for *seed* would have, nothing drawn yet."""
    return SeedTree(root=derive_root(SEAM, seed))


def population_spec(temperature: Mapping[str, Any]) -> PopulationSpec:
    """The shipped World with its ``temperature`` block replaced by *temperature*."""
    world = deepcopy(base().world)
    world["spec"]["temperature"] = dict(temperature)
    return PopulationSpec.from_document(world)


class SyntheticStore:
    """A subject store answering from formulas instead of a materialised population."""

    def __init__(
        self,
        count: int,
        persona_of: Callable[[int], str] = lambda ordinal: SHIPPED_PERSONA,
        lifecycle_of: Callable[[int], str] = lambda ordinal: "new",
    ) -> None:
        self.ordinals = range(count)
        self._persona_of = persona_of
        self._lifecycle_of = lifecycle_of

    def persona(self, ordinal: int) -> str:
        return self._persona_of(ordinal)

    def lifecycle(self, ordinal: int) -> str:
        return self._lifecycle_of(ordinal)

    def identifier(self, ordinal: int) -> str:
        return f"subject-{ordinal}"

    def market(self, ordinal: int) -> Optional[str]:
        return None

    def view(self, ordinal: int, scope: Any) -> None:
        return None


def with_reactions(
    reactions: Mapping[str, Reaction],
    *,
    states: Optional[Sequence[str]] = None,
    transitions: Optional[Sequence[Tuple[str, str]]] = None,
) -> CompiledJourney:
    """The shipped journey with its condition removed and its reactions replaced.

    Without the condition every event of the signal is a trigger, so the number
    of steps a subject takes is the number of events it is given. The taxonomy
    stays the shipped one unless *states* and *transitions* replace it.
    """
    shipped = compile_journeys(base().cast, base().model)[0]
    return dataclasses.replace(
        shipped,
        condition=None,
        condition_source=None,
        scope=None,
        reactions=dict(reactions),
        states=tuple(states) if states is not None else shipped.states,
        transitions=frozenset(transitions) if transitions is not None else shipped.transitions,
    )


def reaction(
    persona: str, probability: str, *, next_state: Optional[str], outcome: str = "recovered"
) -> Reaction:
    """One Persona's reaction, at an exact decimal probability."""
    return Reaction(
        persona_id=persona,
        success_probability=Decimal(probability),
        outcome=outcome,
        next_state=next_state,
    )


def synthetic_event(signal_id: str, ordinal: int, step: int, t_ms: int) -> SignalEvent:
    """A trigger event with no payload and no envelope.

    A journey with no condition reads only ``signal_id``, ``ordinal`` and
    ``t_ms``, so the CloudEvent (which costs more to build than the journey
    stage costs to run) is left out.
    """
    return SignalEvent(
        signal_id=signal_id,
        ordinal=ordinal,
        occurrence=step,
        t_ms=t_ms,
        payload={},
        payload_digest="",
        event=None,  # type: ignore[arg-type]
    )


def run(
    journey: CompiledJourney,
    events: Sequence[SignalEvent],
    store: SyntheticStore,
    spec: PopulationSpec,
    seed: int,
) -> Tuple[Transition, ...]:
    """Run the real journey stage on a fresh tree rooted at *seed*."""
    return run_journeys(
        cast=base().cast,
        world=spec,
        journeys=(journey,),
        events=events,
        store=store,  # type: ignore[arg-type]
        seed_tree=fresh_tree(seed),
        trace=TraceLog(),
    )
