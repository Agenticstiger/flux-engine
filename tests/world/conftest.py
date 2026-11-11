# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Fixtures for the world suite.

Two sources of documents, deliberately:

* the **shipped telco bundle** from the FLUX spec repo, loaded through the real
  ``flux_engine.spec.validate.Bundle`` -- every count taken from it in a test is
  computed from the documents, never typed;
* a **hand-built miniature world** whose numbers are chosen by the test, used
  wherever a property needs a small population or an edge-case declaration the
  shipped example does not contain.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

import flux_engine._vendor.flux_spec.validate as vendored_validate
from flux_engine.core.seedtree import SeedTree, SeedTreeRoot, derive_root
from flux_engine.spec.validate import Bundle
from flux_engine.world import TraceLog
from tests.flux_examples import examples_are_required

REPO_ROOT = Path(__file__).resolve().parents[2]

_EXAMPLE_CANDIDATES = (
    REPO_ROOT.parent / "FLUX" / "examples" / "telco-payment-recovery",
    REPO_ROOT / "examples" / "telco-payment-recovery",
)


def telco_example_dir() -> Optional[Path]:
    """The shipped telco bundle directory, or ``None`` when it is not checked out."""
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


def load_bundle() -> Bundle:
    """Load and fully validate the shipped telco bundle."""
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
def telco_world(bundle: Bundle) -> Dict[str, Any]:
    """The shipped ``World`` document, deep-copied so a test may resize it."""
    return deepcopy(next(d for d in bundle.flux_docs.values() if d["kind"] == "World"))


@pytest.fixture()
def telco_persona(bundle: Bundle) -> Dict[str, Any]:
    return deepcopy(next(d for d in bundle.flux_docs.values() if d["kind"] == "Persona"))


def resized(world: Dict[str, Any], size: int) -> Dict[str, Any]:
    """A copy of *world* with a smaller population, for a fast test."""
    smaller = deepcopy(world)
    smaller["spec"]["population"]["size"] = size
    return smaller


def root(seed: int = 42, seam: bytes = b"H") -> SeedTreeRoot:
    """A seed-tree root over a fixed stand-in for the seam digest."""
    return derive_root(seam, seed)


def tree(seed: int = 42, seam: bytes = b"H") -> SeedTree:
    return SeedTree(root=root(seed, seam))


@pytest.fixture()
def log() -> TraceLog:
    return TraceLog()
