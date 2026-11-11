# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Signal payload shapes: the table, the fallback, and the type agreement."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.rng import DrawStream
from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.types import BOOL, NUMBER, SET_STRING, STRING
from flux_engine.runtime.payloads import PAYLOAD_FAMILIES, default_family, payload_families
from flux_engine.verticals import PACKS, get_pack
from flux_engine.world.families import Bernoulli, BoundedNormal

pytestmark = pytest.mark.unit


def draw_on(family: Any, source: DrawStream) -> Any:
    """Draw one value on *source* (a named helper, not a loop lambda: ruff B023)."""
    return family.draw(lambda: source)


def test_every_table_entry_belongs_to_a_signal_the_shipped_pack_declares() -> None:
    declared = {
        signal_id
        for pack_name in sorted(PACKS)
        for signal_id in get_pack(pack_name).signal_attributes
    }
    assert set(PAYLOAD_FAMILIES) <= declared


def test_the_shipped_signal_draws_every_field_the_pack_declares() -> None:
    pack = get_pack("telco")
    signal_id = next(iter(pack.signal_attributes))
    families = payload_families(signal_id, pack)
    assert set(families) == set(pack.signal_attributes[signal_id])
    assert list(families) == sorted(families)


def test_a_field_absent_from_the_table_still_gets_drawn() -> None:
    pack = VerticalPack(name="tiny", signal_attributes={"sig": {"count": NUMBER, "flag": BOOL}})
    families = payload_families("sig", pack)
    assert isinstance(families["count"], BoundedNormal)
    assert isinstance(families["flag"], Bernoulli)


def test_a_string_field_absent_from_the_table_is_refused() -> None:
    """A string's levels are its whole content; there is no generic shape."""
    pack = VerticalPack(name="tiny", signal_attributes={"sig": {"name": STRING}})
    with pytest.raises(FluxError) as raised:
        payload_families("sig", pack)
    assert "FLX-5040" in str(raised.value)


def test_a_set_field_has_no_generic_shape_either() -> None:
    with pytest.raises(FluxError):
        default_family(SET_STRING)


def test_a_table_entry_the_pack_does_not_declare_is_refused() -> None:
    pack = VerticalPack(name="telco", signal_attributes={"sig-payment-aborted": {"amount": NUMBER}})
    with pytest.raises(FluxError) as raised:
        payload_families("sig-payment-aborted", pack)
    assert "attempts" in str(raised.value)


def test_an_unknown_signal_draws_nothing() -> None:
    assert payload_families("sig-unknown", get_pack("telco")) == {}


def test_the_attempts_field_is_at_least_one() -> None:
    """``RateCount(offset=1)``: an aborted payment has been attempted once."""
    family = payload_families("sig-payment-aborted", get_pack("telco"))["attempts"]
    for entropy in range(1, 60):
        stream = DrawStream.from_entropy(entropy)
        assert draw_on(family, stream) >= Decimal(1)


def test_the_amount_field_stays_inside_its_declared_interval() -> None:
    family = payload_families("sig-payment-aborted", get_pack("telco"))["amount"]
    assert isinstance(family, BoundedNormal)
    for entropy in range(1, 120):
        stream = DrawStream.from_entropy(entropy)
        value = draw_on(family, stream)
        assert family.lo <= value <= family.hi


def test_the_channel_field_only_takes_declared_levels() -> None:
    family = payload_families("sig-payment-aborted", get_pack("telco"))["channel"]
    levels = set(family.expr_type.levels)
    for entropy in range(1, 60):
        stream = DrawStream.from_entropy(entropy)
        assert draw_on(family, stream) in levels


def test_no_number_in_the_table_is_a_float() -> None:
    values = [
        leaf
        for fields in PAYLOAD_FAMILIES.values()
        for family in fields.values()
        for leaf in vars(family).values()
    ]
    assert values
    assert not [leaf for leaf in values if isinstance(leaf, float)]


def test_a_table_family_that_contradicts_the_declared_type_is_refused() -> None:
    """The pack owns the type; a table that draws another one is a plan-time error."""
    pack = VerticalPack(
        name="telco",
        signal_attributes={
            "sig-payment-aborted": {"amount": STRING, "attempts": NUMBER, "channel": STRING}
        },
    )
    with pytest.raises(FluxError) as raised:
        payload_families("sig-payment-aborted", pack)
    assert "'amount'" in str(raised.value)
    assert "declared string" in str(raised.value)
