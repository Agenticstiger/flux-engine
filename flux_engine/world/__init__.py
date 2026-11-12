# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Population materialisation: cohorts, markets, trait sampling, derived attributes.

The entry point is :func:`materialise_population`, which turns a schema-valid
``World`` plus the Simulation's ``Persona`` documents into a
:class:`SubjectStore` -- a columnar population iterated by ordinal -- while
appending the ``cohort`` / ``market`` / ``trait`` ops of KERNEL-SPEC §10 to a
:class:`~flux_engine.world.tracing.TraceLog`::

    from flux_engine.core.seedtree import SeedTree, derive_root
    from flux_engine.verticals import get_pack
    from flux_engine.world import TraceLog, materialise_population

    trace = TraceLog()
    tree = SeedTree(root=derive_root(seam_bytes, effective_seed))
    store = materialise_population(
        world=world_doc, personas=[persona_doc], pack=get_pack("telco"),
        seed_tree=tree, trace=trace,
    )
    store.cohort_counts()          # LRM of World.spec.population.lifecycleMix
    trace.component_digests()["population"]

The shapes the values are drawn from live in :mod:`flux_engine.world.derived`
(vertical-derived attributes) and :mod:`flux_engine.world.families` (the four
sampling families); the ``Decimal`` boundary is :mod:`flux_engine.world.values`.
"""

from __future__ import annotations

from flux_engine.world.derived import DERIVATIONS, Derivation, derivation_for
from flux_engine.world.errors import FLX_5040, FLX_5041
from flux_engine.world.families import (
    Bernoulli,
    BoundedNormal,
    Categorical,
    Constant,
    Family,
    RateCount,
    SetOf,
    StreamFactory,
)
from flux_engine.world.population import (
    NORMAL_BOUND_SIGMAS,
    SUBJECT_ID_LENGTH,
    SUBJECT_ID_PREFIX,
    TRAIT_SCALE,
    PersonaSpec,
    PopulationSpec,
    Subject,
    SubjectStore,
    integral_trait_names,
    materialise_population,
    subject_id,
)
from flux_engine.world.tracing import TraceLog
from flux_engine.world.values import decimal_of, int_of, probability_of, weight_map

__all__ = [
    "DERIVATIONS",
    "FLX_5040",
    "FLX_5041",
    "NORMAL_BOUND_SIGMAS",
    "SUBJECT_ID_LENGTH",
    "SUBJECT_ID_PREFIX",
    "TRAIT_SCALE",
    "Bernoulli",
    "BoundedNormal",
    "Categorical",
    "Constant",
    "Derivation",
    "Family",
    "PersonaSpec",
    "PopulationSpec",
    "RateCount",
    "SetOf",
    "StreamFactory",
    "Subject",
    "SubjectStore",
    "TraceLog",
    "decimal_of",
    "derivation_for",
    "int_of",
    "integral_trait_names",
    "materialise_population",
    "probability_of",
    "subject_id",
    "weight_map",
]
