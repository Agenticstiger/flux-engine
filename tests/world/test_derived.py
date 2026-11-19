# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The vertical derivation table: the shapes behind the pack's types."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.rng import DrawStream
from flux_engine.expr.attrs import VerticalPack
from flux_engine.expr.types import NUMBER, SET_STRING, STRING
from flux_engine.verticals import PACKS, get_pack
from flux_engine.world.derived import (
    DERIVATIONS,
    TELCO_DERIVATION,
    Derivation,
    derivation_for,
)
from flux_engine.world.families import BoundedNormal, Constant, RateCount

pytestmark = pytest.mark.unit


def stream(entropy: int = 7) -> DrawStream:
    return DrawStream.from_entropy(entropy)


def draw_all(derivation: Derivation, source: DrawStream, lifecycle: str) -> Any:
    """Draw one subject's derived attributes on *source* (see ruff B023)."""
    return derivation.draw(lambda: source, lifecycle=lifecycle)


def draw_one(family: Any, source: DrawStream) -> Any:
    """Draw one value on *source* (see ruff B023)."""
    return family.draw(lambda: source)


def test_every_registered_vertical_pack_has_a_derivation_that_matches_it() -> None:
    """The two v0.1 stopgap tables must agree: types from the pack, shapes from here."""
    for name in sorted(PACKS):
        derivation_for(name).check_against(get_pack(name))


def test_a_vertical_with_no_table_fails_by_name() -> None:
    with pytest.raises(FluxError) as raised:
        derivation_for("no-such-vertical")
    assert "FLX-5040" in str(raised.value)
    assert sorted(DERIVATIONS)[0] in str(raised.value)


def test_the_pack_and_the_derivation_declare_the_same_attribute_names() -> None:
    pack = get_pack("telco")
    assert set(TELCO_DERIVATION.names) == set(pack.subject_attributes)


def test_a_derivation_that_draws_an_attribute_the_pack_does_not_declare_is_refused() -> None:
    rogue = Derivation(vertical="telco", attributes={"unknown": RateCount(Decimal(1))})
    with pytest.raises(FluxError) as raised:
        rogue.check_against(get_pack("telco"))
    assert "unknown" in str(raised.value)


def test_a_derivation_whose_type_contradicts_the_pack_is_refused() -> None:
    pack = VerticalPack(name="tiny", subject_attributes={"x": SET_STRING})
    rogue = Derivation(vertical="tiny", attributes={"x": RateCount(Decimal(1))})
    with pytest.raises(FluxError) as raised:
        rogue.check_against(pack)
    assert "set<string>" in str(raised.value)


def test_an_enum_shaped_attribute_satisfies_a_string_declaration() -> None:
    pack = VerticalPack(name="tiny", subject_attributes={"x": STRING})
    Derivation(
        vertical="tiny", attributes={"x": Constant("a", declared_type=STRING)}
    ).check_against(pack)


def test_names_are_nfc_sorted_and_that_is_the_draw_order() -> None:
    assert TELCO_DERIVATION.names == tuple(sorted(TELCO_DERIVATION.names))


def test_the_word_cost_of_a_subject_does_not_depend_on_its_lifecycle() -> None:
    """A lifecycle-dependent cost would let one cohort move another's stream."""
    costs = set()
    for lifecycle in sorted(TELCO_DERIVATION.by_lifecycle):
        source = stream()
        draw_all(TELCO_DERIVATION, source, lifecycle)
        costs.add(source.position)
    assert costs == {TELCO_DERIVATION.word_cost()}


def test_an_unknown_lifecycle_falls_back_to_the_base_families() -> None:
    families = TELCO_DERIVATION.families_for("no-such-lifecycle")
    assert families == dict(TELCO_DERIVATION.attributes)


def test_a_lifecycle_override_changes_the_value_but_not_the_position() -> None:
    drawn: Dict[str, Any] = {}
    for lifecycle in ("new", "atrisk"):
        source = stream(99)
        drawn[lifecycle] = draw_all(TELCO_DERIVATION, source, lifecycle)
    # Only the overridden attribute may differ between two lifecycles.
    differing = {
        name for name in TELCO_DERIVATION.names if drawn["new"][name] != drawn["atrisk"][name]
    }
    assert differing <= set(TELCO_DERIVATION.by_lifecycle["new"])


def test_at_risk_subscribers_draw_more_late_payments_than_new_ones() -> None:
    """The reason ``latePaymentCount`` is lifecycle-conditioned at all."""
    totals = {}
    for lifecycle in ("new", "atrisk"):
        totals[lifecycle] = sum(
            draw_one(TELCO_DERIVATION.families_for(lifecycle)["latePaymentCount"], stream(entropy))
            for entropy in range(1, 300)
        )
    assert totals["atrisk"] > totals["new"]


def test_an_override_of_an_unknown_attribute_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown attribute"):
        Derivation(
            vertical="t",
            attributes={"a": RateCount(Decimal(1))},
            by_lifecycle={"x": {"b": RateCount(Decimal(1))}},
        )


def test_an_override_that_moves_the_stream_position_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="must not move the stream position"):
        Derivation(
            vertical="t",
            attributes={"a": Constant(Decimal(1))},
            by_lifecycle={"x": {"a": RateCount(Decimal(1))}},
        )


def test_the_declared_types_are_the_families_types() -> None:
    assert TELCO_DERIVATION.types()["arpu"] is NUMBER
    assert isinstance(TELCO_DERIVATION.attributes["arpu"], BoundedNormal)


def _leaves(value: Any) -> Any:
    if isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif hasattr(value, "__dict__"):
        for item in vars(value).values():
            yield from _leaves(item)
    else:
        yield value


def test_no_number_anywhere_in_the_table_is_a_float() -> None:
    """Every table entry is an exact decimal, so nothing here can taint a digest."""
    families = list(TELCO_DERIVATION.attributes.values())
    for overrides in TELCO_DERIVATION.by_lifecycle.values():
        families.extend(overrides.values())
    leaves = [leaf for family in families for leaf in _leaves(family)]
    assert leaves, "the table has no parameters at all; the check would pass vacuously"
    assert not [leaf for leaf in leaves if isinstance(leaf, float)]
