# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The market -> jurisdiction edge FLUX 0.5.0 does not declare."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.expr.attrs import VerticalPack
from flux_engine.runtime.jurisdiction import (
    MARKET_JURISDICTIONS,
    jurisdiction_map,
    jurisdiction_of,
)
from flux_engine.verticals import PACKS, get_pack

from .conftest import load_bundle, requires_example

pytestmark = pytest.mark.unit


def test_the_engine_table_is_used_when_the_pack_carries_none() -> None:
    pack = get_pack("telco")
    assert not hasattr(pack, "market_jurisdictions")
    assert jurisdiction_map(pack) == dict(MARKET_JURISDICTIONS["telco"])


@dataclass(frozen=True)
class _PackWithMap(VerticalPack):
    market_jurisdictions: Mapping[str, str] = ()  # type: ignore[assignment]


def test_a_pack_that_declares_the_map_supersedes_the_engine_table() -> None:
    """The stopgap is designed to be superseded without a change here."""
    pack = _PackWithMap(name="telco", market_jurisdictions={"mkt-a": "IE"})
    assert jurisdiction_map(pack) == {"mkt-a": "IE"}
    assert jurisdiction_of(pack, "mkt-a") == "IE"


def test_an_unmapped_market_is_refused_rather_than_guessed() -> None:
    with pytest.raises(FluxError) as raised:
        jurisdiction_of(get_pack("telco"), "mkt-unknown")
    assert "FLX-4110" in str(raised.value)


def test_a_subject_with_no_market_is_refused_too() -> None:
    with pytest.raises(FluxError):
        jurisdiction_of(get_pack("telco"), None)


def test_a_vertical_with_no_table_maps_nothing_rather_than_failing_at_import() -> None:
    assert jurisdiction_map(VerticalPack(name="unknown-vertical")) == {}


@requires_example
@pytest.mark.integration
def test_every_market_of_the_shipped_world_maps_into_the_shipped_consent_profile() -> None:
    """Both sides read from the bundle: no jurisdiction code is typed here."""
    bundle = load_bundle()
    world = next(d for d in bundle.flux_docs.values() if d["kind"] == "World")
    consent = next(d for d in bundle.flux_docs.values() if d["kind"] == "ConsentProfile")
    pack = get_pack(world["spec"]["vertical"])
    allowed = set(consent["spec"]["allowedJurisdictions"])
    for market in world["spec"]["population"]["markets"]:
        assert jurisdiction_of(pack, market) in allowed


def test_every_shipped_pack_has_a_table_or_deliberately_has_none() -> None:
    for name in sorted(PACKS):
        assert isinstance(jurisdiction_map(get_pack(name)), dict)
