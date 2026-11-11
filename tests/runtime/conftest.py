# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the runtime suite: the shipped bundle, and one small staged run.

The loader is duplicated from ``tests/world/conftest.py`` rather than shared:
``tests/`` is not a package, so the two directories cannot import from each
other without adding a top-level ``__init__.py`` that is outside this work
item's ownership.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

import flux_engine._vendor.flux_spec.validate as vendored_validate
from flux_engine.core.clock import VirtualClock
from flux_engine.core.seedtree import SeedTree, derive_root
from flux_engine.expr.attrs import AttributeModel, VerticalPack, build_attribute_model
from flux_engine.runtime import Cast, SignalEvent, emit_signals, resolve_cast
from flux_engine.spec.validate import Bundle
from flux_engine.verticals import get_pack
from flux_engine.world import PopulationSpec, SubjectStore, TraceLog, materialise_population
from tests.flux_examples import examples_are_required

REPO_ROOT = Path(__file__).resolve().parents[2]

_EXAMPLE_CANDIDATES = (
    REPO_ROOT.parent / "FLUX" / "examples" / "telco-payment-recovery",
    REPO_ROOT / "examples" / "telco-payment-recovery",
)


def telco_example_dir() -> Optional[Path]:
    for candidate in _EXAMPLE_CANDIDATES:
        if (candidate / "world.flux.yml").is_file():
            return candidate
    return None


EXAMPLE = telco_example_dir()
# Not simply ``EXAMPLE is None``: under FLUX_EXAMPLES_REQUIRED the condition
# is never true, so these tests are collected and FAIL on the missing bundle
# rather than vanishing from a green run. CI sets that variable.
requires_example = pytest.mark.skipif(
    EXAMPLE is None and not examples_are_required(),
    reason=(
        "the FLUX spec repo's telco example is not checked out beside this repo "
        "(set FLUX_EXAMPLES_REQUIRED=1 to make this a failure instead)"
    ),
)

#: Small enough that the whole suite stays fast, large enough that the shipped
#: segment, detector, journey and campaign all have members.
STAGED_SIZE = 600


def load_bundle() -> Bundle:
    from jsonschema import Draft202012Validator, FormatChecker

    assert EXAMPLE is not None
    schema = json.loads(Path(vendored_validate.FLUX_SCHEMA_PATH).read_text())
    loaded = Bundle(EXAMPLE, Draft202012Validator(schema, format_checker=FormatChecker()))
    errors = loaded.run()
    assert errors == [], errors
    return loaded


@pytest.fixture(scope="session")
def bundle() -> Bundle:
    return load_bundle()


@pytest.fixture()
def docs(bundle: Bundle) -> Dict[str, Any]:
    """A deep copy of the bundle's FLUX documents, safe for a test to edit."""
    return deepcopy(dict(bundle.flux_docs))


@pytest.fixture()
def cast(bundle: Bundle) -> Cast:
    return resolve_cast(bundle.flux_docs)


@dataclass
class Staged:
    """One small run, materialised up to and including the signal stage."""

    bundle: Bundle
    cast: Cast
    pack: VerticalPack
    model: AttributeModel
    world: Dict[str, Any]
    spec: PopulationSpec
    store: SubjectStore
    clock: VirtualClock
    seed_tree: SeedTree
    trace: TraceLog
    events: Tuple[SignalEvent, ...]

    def personas(self) -> List[Dict[str, Any]]:
        return [self.bundle.flux_docs[doc_id] for doc_id in self.cast.ids("Persona")]

    def fresh_tree(self) -> SeedTree:
        """A seed tree at the same root, with nothing materialised yet.

        A ``SeedTree`` memoises its streams, so asking for one path twice
        continues that stream rather than restarting it -- which is what makes a
        run's draws advance. Re-running one *stage* therefore needs a tree that
        has not already been drawn from. That this works at all is the property
        under test in ``test_the_journey_stage_draws_on_paths_the_signal_stage_never_touches``:
        the stages' seed paths are disjoint, so a stage can be replayed on its
        own without replaying the ones before it.
        """
        return SeedTree(root=self.seed_tree.root)


def stage(size: int = STAGED_SIZE, *, seed: int = 42, seam: bytes = b"H") -> Staged:
    """Materialise a population and emit its signals -- the input to every executor."""
    bundle = load_bundle()
    cast = resolve_cast(bundle.flux_docs)
    pack = get_pack(cast.world["spec"]["vertical"])
    model = build_attribute_model(bundle, pack, simulation_id=cast.simulation_id)
    world = deepcopy(dict(cast.world))
    world["spec"]["population"]["size"] = size
    spec = PopulationSpec.from_document(world)
    seed_tree = SeedTree(root=derive_root(seam, seed))
    trace = TraceLog()
    store = materialise_population(
        world=world,
        personas=[bundle.flux_docs[doc_id] for doc_id in cast.ids("Persona")],
        pack=pack,
        seed_tree=seed_tree,
        trace=trace,
    )
    bounds = cast.simulation["spec"]["timeBounds"]
    clock = VirtualClock.from_time_bounds(bounds["start"], bounds["end"])
    events = emit_signals(
        cast=cast,
        world=spec,
        store=store,
        clock=clock,
        seed_tree=seed_tree,
        trace=trace,
        pack=pack,
    )
    return Staged(
        bundle=bundle,
        cast=cast,
        pack=pack,
        model=model,
        world=world,
        spec=spec,
        store=store,
        clock=clock,
        seed_tree=seed_tree,
        trace=trace,
        events=events,
    )


@pytest.fixture(scope="session")
def staged() -> Staged:
    return stage()
