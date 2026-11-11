# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Ports with no injection point are published as reserved, not as available.

``flux_engine.api`` invites a consumer to code against every Protocol it
exports. Four of them have no implementation anywhere in the engine and no
parameter through which one could be supplied -- the real expression
compiler, for instance, is a pair of module-level functions whose shape
differs from :class:`~flux_engine.api.expressions.ExpressionEvaluator`'s
method, and nothing type-checks one against the other.

This pins which ports are reserved and asserts the other side of the
claim: the ports that are *not* reserved have an in-tree implementation
that satisfies them structurally.
"""

from __future__ import annotations

import pytest

import flux_engine.api as api

pytestmark = pytest.mark.unit


def test_the_reserved_set_is_what_it_says_it_is() -> None:
    assert api.RESERVED_PORTS == frozenset(
        {"AccuracyGate", "ExpressionEvaluator", "FidelityProver", "SkillRunner"}
    )


@pytest.mark.parametrize("name", sorted(api.RESERVED_PORTS))
def test_every_reserved_port_says_so_in_its_docstring(name: str) -> None:
    documentation = getattr(api, name).__doc__ or ""
    assert "Reserved" in documentation, f"{name} does not say it is reserved"


def test_the_reserved_ports_are_all_real_exports() -> None:
    assert api.RESERVED_PORTS <= set(api.__all__)


def test_reserved_is_not_in_the_frozen_surface() -> None:
    """It is a fact about the surface, not a member of it."""
    assert "RESERVED_PORTS" not in api.__all__


def test_an_unreserved_port_has_an_implementation_that_satisfies_it() -> None:
    """The other half of the claim, so `reserved` cannot become a shrug."""
    from flux_engine.calibrate.declared import DeclaredCalibrator
    from flux_engine.governance.gate import _select_model_port
    from flux_engine.lineage.emitter import NdjsonLineageEmitter
    from flux_engine.seam.sinks import NdjsonSink, NullSink

    assert isinstance(NullSink(), api.EmitSink)
    assert isinstance(NdjsonSink(stream=None, path="x"), api.EmitSink)
    assert isinstance(DeclaredCalibrator(playback={}), api.Calibrator)
    assert isinstance(NdjsonLineageEmitter(path="x"), api.LineageEmitter)
    port = _select_model_port(env={"FLUX_MODEL_PORT": "stub"})
    assert isinstance(port, api.ModelPort)
