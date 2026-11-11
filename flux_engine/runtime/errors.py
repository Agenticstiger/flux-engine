# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Error codes raised by the executors.

KERNEL-SPEC §13 assigns 2000 to "plan/replay binding" and 4000 to
"governance/sovereignty". The three ``FLX-23nn`` codes below are binding
failures -- the run's cast, its taxonomy, or its step budget does not bind to
what the documents declare -- and ``FLX-4110`` is a governance failure: a
delivery decision the engine has no basis to make.

Registered at import time so ``fluxsim explain`` works without a run.
"""

from __future__ import annotations

from flux_engine.core.errors import ErrorCode, register

__all__ = ["FLX_2301", "FLX_2302", "FLX_2303", "FLX_4110"]

FLX_2301: ErrorCode = register(
    "FLX-2301",
    "the run's cast does not resolve",
    "A reference in the Simulation's closure names a document the bundle does not carry, "
    "of a kind that reference may not name, or -- for a Journey or a Detector -- one "
    "reached by a route other than moduleRefs -> Module.spec.resources. A cast that does "
    "not resolve cannot be planned, so the run stops rather than executing a subset of "
    "what the documents describe.",
)

FLX_2302: ErrorCode = register(
    "FLX-2302",
    "journey transition is not in the taxonomy",
    "A Journey's personaReactions[].nextState is not a state of the referenced "
    "JourneyTaxonomy, or the edge from the subject's current state to it is not one of "
    "the taxonomy's transitions. The offline validator checks the state's membership; the "
    "engine additionally checks the edge, because a legal state reached by an illegal "
    "edge would put a transition in the run digest that the taxonomy forbids.",
)

FLX_2303: ErrorCode = register(
    "FLX-2303",
    "journey step budget exhausted",
    "One subject's journey took more steps than the run's declared budget. The budget "
    "exists so a state machine whose transitions cycle cannot make a run's length depend "
    "on its data; exhausting it means the Journey and the JourneyTaxonomy describe a "
    "cycle the engine will not silently truncate.",
)

FLX_4110: ErrorCode = register(
    "FLX-4110",
    "no jurisdiction is known for a market",
    "A Campaign's consent gate needs the jurisdiction a subject's market sits in, and the "
    "vertical declares none for that market. The delivery is not suppressed and not "
    "allowed: an unknown jurisdiction is an unanswerable consent question, and answering "
    "it either way would be a governance claim the documents do not support.",
)
