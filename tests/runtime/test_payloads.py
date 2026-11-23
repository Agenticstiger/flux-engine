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
from flux_engine.runtime import payloads
from flux_engine.runtime.payloads import (
    PAYLOAD_FAMILIES,
    default_family,
    integral_payload_fields,
    payload_families,
)
from flux_engine.verticals import PACKS, get_pack
from flux_engine.world.families import Bernoulli, BoundedNormal, RateCount

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


# ---------------------------------------------------------------------------
# Which payload fields are always a whole number (KERNEL-SPEC §9's INT rule)
# ---------------------------------------------------------------------------


def test_the_shipped_tables_name_their_counts_and_nothing_else() -> None:
    assert integral_payload_fields(["sig-payment-aborted"], get_pack("telco")) == ("attempts",)
    towers = get_pack("towers")
    assert integral_payload_fields(sorted(towers.signal_attributes), towers) == (
        "outageMinutes",
        "sitesAffected",
    )


def test_a_field_two_signals_share_is_whole_only_when_both_draw_it_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both Signals fill one ``event.data.`` path, so the field is one source."""
    pack = VerticalPack(
        name="tiny",
        signal_attributes={
            "sig-a": {"count": NUMBER},
            "sig-b": {"count": NUMBER},
            "sig-c": {"other": NUMBER},
        },
    )
    count = RateCount(mean=Decimal("1"))
    cents = BoundedNormal(
        mean=Decimal("3"), stddev=Decimal("1"), lo=Decimal("0"), hi=Decimal("9"), scale=2
    )
    monkeypatch.setattr(
        payloads, "PAYLOAD_FAMILIES", {"sig-a": {"count": count}, "sig-b": {"count": count}}
    )
    assert integral_payload_fields(["sig-a", "sig-b"], pack) == ("count",)
    monkeypatch.setattr(
        payloads, "PAYLOAD_FAMILIES", {"sig-a": {"count": count}, "sig-b": {"count": cents}}
    )
    assert integral_payload_fields(["sig-a", "sig-b"], pack) == ()


def test_a_signal_that_does_not_declare_a_field_neither_qualifies_nor_disqualifies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pack = VerticalPack(
        name="tiny", signal_attributes={"sig-a": {"count": NUMBER}, "sig-c": {"other": NUMBER}}
    )
    monkeypatch.setattr(payloads, "PAYLOAD_FAMILIES", {"sig-a": {"count": RateCount(Decimal(1))}})
    # ``other`` falls back to the generic number family, which draws six decimal places.
    assert integral_payload_fields(["sig-c", "sig-a"], pack) == ("count",)


def test_a_field_absent_from_the_table_is_not_whole() -> None:
    """The fallback number family draws at scale 6, so it never qualifies."""
    pack = VerticalPack(name="tiny", signal_attributes={"sig": {"count": NUMBER}})
    assert not default_family(NUMBER).integral
    assert integral_payload_fields(["sig"], pack) == ()
