# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Two declared names that normalise onto one string are refused, not merged.

``inputDigest`` is taken over the raw file bytes (KERNEL-SPEC §11), so the
canonicaliser -- which refuses exactly this collision with ``FLX-1007`` --
never sees a ``World`` or ``Persona`` mapping. Every mapping these readers
build is therefore keyed by NFC without any canonicalisation pass behind
it, and a collision would drop one of the two declarations silently: the
run would stay deterministic, and would be a run of a document nobody
wrote.
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal
from typing import Any, Dict

import pytest

from flux_engine.core.canonical import canonicalise
from flux_engine.core.errors import FluxError
from flux_engine.world.population import PersonaSpec, PopulationSpec

pytestmark = pytest.mark.unit

#: One name in both spellings: distinct as written, one string after NFC.
DECOMPOSED = unicodedata.normalize("NFD", "âge")
COMPOSED = unicodedata.normalize("NFC", "âge")

_NORMAL: Dict[str, Any] = {"dist": "normal", "mean": "1", "stddev": "1", "min": "0", "max": "2"}


def test_the_canonicaliser_refuses_the_same_collision() -> None:
    """The doctrine these readers are being held to, stated by the kernel."""
    assert DECOMPOSED != COMPOSED
    with pytest.raises(FluxError) as caught:
        canonicalise({DECOMPOSED: 1, COMPOSED: 2})
    assert caught.value.code.code == "FLX-1007"


def _world(**population: Any) -> Dict[str, Any]:
    document: Dict[str, Any] = {
        "id": "w",
        "spec": {
            "vertical": "telco",
            "population": {
                "size": 10,
                "markets": ["ie"],
                "lifecycleMix": {"active": "1"},
                **population,
            },
        },
    }
    return document


def test_two_market_names_that_normalise_together_are_refused() -> None:
    with pytest.raises(FluxError) as caught:
        PopulationSpec.from_document(_world(markets=[DECOMPOSED, COMPOSED]))
    assert caught.value.code.code == "FLX-5040"
    assert "markets" in (caught.value.detail or "")


def test_two_temperature_dials_that_normalise_together_are_refused() -> None:
    document = _world()
    document["spec"]["temperature"] = {DECOMPOSED: "0.1", COMPOSED: "0.9"}
    with pytest.raises(FluxError) as caught:
        PopulationSpec.from_document(document)
    assert caught.value.code.code == "FLX-5040"
    assert "temperature" in (caught.value.detail or "")


def test_two_trait_names_that_normalise_together_are_refused() -> None:
    persona = {"id": "p", "spec": {"traits": {DECOMPOSED: _NORMAL, COMPOSED: _NORMAL}}}
    with pytest.raises(FluxError) as caught:
        PersonaSpec.from_document(persona)
    assert caught.value.code.code == "FLX-5040"
    assert "traits" in (caught.value.detail or "")


def test_two_worldview_names_that_normalise_together_are_refused() -> None:
    persona = {"id": "p", "spec": {"worldview": {DECOMPOSED: "1", COMPOSED: "2"}}}
    with pytest.raises(FluxError) as caught:
        PersonaSpec.from_document(persona)
    assert caught.value.code.code == "FLX-5040"
    assert "worldview" in (caught.value.detail or "")


def test_distinct_names_still_read() -> None:
    """The negative control: names that do not collide are all kept.

    A decomposed name is *normalised*, not refused -- the refusal is about
    two of them meeting, not about the spelling.
    """
    document = _world(markets=[DECOMPOSED, "uk"])
    document["spec"]["temperature"] = {DECOMPOSED: "0.1", "engagement": "0.9"}
    spec = PopulationSpec.from_document(document)
    assert spec.markets == tuple(sorted((COMPOSED, "uk")))
    assert spec.temperature[COMPOSED] == Decimal("0.1")

    persona = PersonaSpec.from_document(
        {"id": "p", "spec": {"traits": {DECOMPOSED: _NORMAL, "tenure": _NORMAL}}}
    )
    assert persona.trait_names == tuple(sorted((COMPOSED, "tenure")))
