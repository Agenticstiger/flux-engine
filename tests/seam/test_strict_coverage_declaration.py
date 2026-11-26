# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A Simulation declares strict coverage under the namespace's top-level ``strictCoverage`` key.

The key is a YAML boolean. ``true`` asks for what ``--strict-coverage`` asks for, and
the flag cannot turn it off; ``false`` and a null declare nothing. Anything else is
``FLX-3105``, because a declaration read as something it is not would be strictness
that silently is not on.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam import resolve as resolve_module
from flux_engine.seam.coverage import (
    STRICT_BY_DECLARATION,
    STRICT_BY_FLAG,
    strictness_asked_by,
)
from flux_engine.seam.resolve import load_bundle, resolve_emit, strict_coverage_declared

from .conftest import minimal_simulation

pytestmark = pytest.mark.unit


def _simulation(namespace: Any) -> Dict[str, Any]:
    return {"extensions": {resolve_module.EXTENSION_NAMESPACE: namespace}}


def _resolved(write_bundle: Any, namespace: Any) -> Any:
    """Resolve the one emit of a bundle whose Simulation declares *namespace*."""
    documents = minimal_simulation()
    documents[1]["extensions"] = {resolve_module.EXTENSION_NAMESPACE: namespace}
    return resolve_emit(
        load_bundle(write_bundle(flux_documents=documents)), simulation_id="sim-widget"
    )


@pytest.mark.parametrize(
    ("simulation", "declared"),
    [
        (_simulation({"strictCoverage": True}), True),
        (_simulation({"strictCoverage": False}), False),
        (_simulation({"strictCoverage": None}), False),
        (_simulation({"emits": {}}), False),
        (_simulation(None), False),
        ({"extensions": {}}, False),
        ({"extensions": None}, False),
        ({}, False),
    ],
)
def test_a_simulation_declares_strict_coverage_with_a_boolean(
    simulation: Dict[str, Any], declared: bool
) -> None:
    assert strict_coverage_declared(simulation) is declared


@pytest.mark.parametrize("where", ["sim-x", ""])
@pytest.mark.parametrize("value", ["true", "false", "yes", 1, 0, 1.0, [], [True], {}, {"on": True}])
def test_anything_but_a_boolean_is_refused_and_the_refusal_names_the_key(
    value: Any, where: str
) -> None:
    prefix = r"sim-x: " if where else ""
    named = rf"{prefix}extensions\['com.agentics.flux/1'\]\.strictCoverage is a boolean"
    with pytest.raises(FluxError, match=named) as raised:
        strict_coverage_declared(_simulation({"strictCoverage": value}), where=where)
    assert raised.value.code.code == "FLX-3105"
    assert ("sim-x" in raised.value.detail) is bool(where)


def test_the_namespace_carries_twenty_seven_keys_and_nothing_else(write_bundle: Any) -> None:
    with pytest.raises(FluxError) as raised:
        _resolved(write_bundle, {"strictCoverage": True, "strictCoverages": True})
    assert raised.value.code.code == "FLX-3105"
    assert (
        "declares ['strictCoverages']; this namespace carries "
        "['asOf', 'associations', 'bridges', 'conditions', 'consumers', 'copies', 'counts', 'emits', 'events', 'facts', 'forecasts', 'generators', 'history', 'lifetimes', 'links', 'overlay', 'parameters', 'periods', 'privacy', 'profile', 'rates', 'responses', 'scorecard', 'snapshots', 'strictCoverage', 'unused', 'within']"
        in raised.value.detail
    )


def test_a_malformed_declaration_is_refused_when_the_bundle_resolves(write_bundle: Any) -> None:
    with pytest.raises(FluxError, match="sim-widget: extensions.*strictCoverage is a boolean"):
        _resolved(write_bundle, {"strictCoverage": "true"})


def test_a_declaration_alone_does_not_change_what_an_emit_resolves_to(write_bundle: Any) -> None:
    assert _resolved(write_bundle, {"strictCoverage": True}).extension is None


@pytest.mark.parametrize(
    ("flag", "declared", "asked_by"),
    [
        (False, False, ()),
        (True, False, (STRICT_BY_FLAG,)),
        (False, True, (STRICT_BY_DECLARATION,)),
        (True, True, (STRICT_BY_FLAG, STRICT_BY_DECLARATION)),
    ],
)
def test_strictness_is_asked_for_by_the_flag_or_the_declaration_or_both(
    flag: bool, declared: bool, asked_by: Any
) -> None:
    assert strictness_asked_by(flag=flag, declared=declared) == asked_by
