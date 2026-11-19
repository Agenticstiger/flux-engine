# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §9's INT rule, world half: what is always a whole number.

The seam lets a drawn value fill an ``INT`` column only when the plan can
show, from the declarations alone, that every value of it is a whole
number. This module pins the three answers the world gives the plan: which
families draw only whole numbers (:attr:`Family.integral`), which derived
attributes do in every cohort (:meth:`Derivation.integral_names`), and which
traits do for every Persona that declares them (:func:`integral_trait_names`).

Every positive case is also drawn, many times: a family that claims whole
numbers and then draws a fraction would turn a plan-time promise into a
per-row failure, which is the outcome the rule exists to rule out.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List

import pytest

from flux_engine.core.rng import DrawStream
from flux_engine.expr.types import NUMBER, STRING, Type
from flux_engine.world import integral_trait_names
from flux_engine.world.derived import DERIVATIONS, Derivation, derivation_for
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

pytestmark = pytest.mark.unit

ENTROPIES = range(1, 301)


def draw_on(family: Family, entropy: int) -> Any:
    """Draw one value on a fresh stream (a named helper, not a loop lambda: ruff B023)."""
    source = DrawStream.from_entropy(entropy)
    return family.draw(lambda: source)


def is_whole(value: Any) -> bool:
    """The test's own reading of "a whole number": an int or a whole Decimal, never a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return False
    return Decimal(value) == Decimal(value).to_integral_value()


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------

WHOLE_FAMILIES: List[Family] = [
    RateCount(mean=Decimal("0.40")),
    RateCount(mean=Decimal("1.20"), offset=1),
    BoundedNormal(
        mean=Decimal("54"), stddev=Decimal("30"), lo=Decimal("0"), hi=Decimal("180"), scale=0
    ),
    # Bounds that are not whole, and a spread wide enough that many draws are
    # clamped: the bounds are rounded inward onto scale 0, so a clamped draw
    # is whole too.
    BoundedNormal(
        mean=Decimal("5"), stddev=Decimal("40"), lo=Decimal("0.5"), hi=Decimal("9.5"), scale=0
    ),
    Constant(Decimal("3")),
    Constant(Decimal("1.000000")),
    Constant(7),
]

FRACTIONAL_FAMILIES: List[Family] = [
    BoundedNormal(
        mean=Decimal("42"), stddev=Decimal("12"), lo=Decimal("5"), hi=Decimal("250"), scale=2
    ),
    Constant(Decimal("0.6")),
    Constant(True),
    Constant(Decimal("NaN")),
    Constant(Decimal("Infinity")),
    Constant("7", declared_type=STRING),
    # A level spelled with digits is still a string.
    Categorical({"1": Decimal("0.5"), "2": Decimal("0.5")}),
    Bernoulli(Decimal("0.5")),
    SetOf({"a": Decimal("0.5")}),
]


@pytest.mark.parametrize("family", WHOLE_FAMILIES, ids=repr)
def test_a_family_that_declares_whole_numbers_draws_only_whole_numbers(family: Family) -> None:
    assert family.integral
    drawn = [draw_on(family, entropy) for entropy in ENTROPIES]
    assert all(is_whole(value) for value in drawn)


@pytest.mark.parametrize("family", FRACTIONAL_FAMILIES, ids=repr)
def test_every_other_family_is_not_integral(family: Family) -> None:
    assert not family.integral


def test_the_clamped_bounds_of_a_scale_0_normal_are_reached_and_whole() -> None:
    """The inward-rounded bounds are the values a clamp produces; both are drawn."""
    family = WHOLE_FAMILIES[3]
    drawn = {draw_on(family, entropy) for entropy in ENTROPIES}
    assert {Decimal(1), Decimal(9)} <= drawn


def test_integrality_is_read_off_the_declaration_never_off_a_draw() -> None:
    """A scale-2 normal that only ever draws ``5.00`` is still not integral.

    Its every draw is a whole number, so a per-value check would admit it;
    the declaration says it may carry cents, so the plan does not.
    """
    family = BoundedNormal(
        mean=Decimal("5"), stddev=Decimal("0"), lo=Decimal("0"), hi=Decimal("10"), scale=2
    )
    assert {draw_on(family, entropy) for entropy in range(1, 20)} == {Decimal("5.00")}
    assert not family.integral


def test_a_rate_count_with_a_fractional_offset_is_not_integral() -> None:
    assert not RateCount(mean=Decimal("1"), offset=Decimal("0.5")).integral
    assert RateCount(mean=Decimal("1"), offset=Decimal("2")).integral


class _Unstated(Family):
    """A family that only ever draws 1 and does not say so."""

    def draw(self, open_stream: StreamFactory) -> Decimal:
        return Decimal(1)

    @property
    def word_cost(self) -> int:
        return 0

    @property
    def expr_type(self) -> Type:
        return NUMBER


def test_a_family_that_does_not_state_whole_numbers_is_not_trusted_with_them() -> None:
    """The base answer is no: a new family is refused for INT until it says otherwise."""
    assert not _Unstated().integral


# ---------------------------------------------------------------------------
# Derived attributes
# ---------------------------------------------------------------------------


def test_the_shipped_tables_name_their_counts_and_nothing_else() -> None:
    assert derivation_for("telco").integral_names() == ("latePaymentCount", "tenureMonths")
    assert derivation_for("towers").integral_names() == (
        "chronicSlaBreaches",
        "monthsToLeaseExpiry",
        "tenancyCount",
    )


@pytest.mark.parametrize("vertical", sorted(DERIVATIONS))
def test_an_integral_attribute_is_whole_in_every_cohort(vertical: str) -> None:
    table = derivation_for(vertical)
    lifecycles = sorted(table.by_lifecycle) + ["no-such-lifecycle"]
    for lifecycle in lifecycles:
        families = table.families_for(lifecycle)
        for name in table.integral_names():
            assert all(is_whole(draw_on(families[name], entropy)) for entropy in range(1, 61))


def test_one_cohort_that_draws_a_fraction_disqualifies_the_attribute() -> None:
    """The cohort a subject lands in picks the family, so every cohort must hold."""
    cents = BoundedNormal(
        mean=Decimal("3"), stddev=Decimal("1"), lo=Decimal("0"), hi=Decimal("9"), scale=2
    )
    table = Derivation(
        vertical="test",
        attributes={"count": RateCount(mean=Decimal("1")), "other": RateCount(mean=Decimal("1"))},
        by_lifecycle={"atrisk": {"count": cents}},
    )
    assert table.integral_names() == ("other",)


def test_integral_names_keep_the_draw_order() -> None:
    table = derivation_for("towers")
    names = table.integral_names()
    assert list(names) == [name for name in table.names if name in names]


# ---------------------------------------------------------------------------
# Traits
# ---------------------------------------------------------------------------


def _persona(persona_id: str, traits: Dict[str, Any]) -> Dict[str, Any]:
    return {"kind": "Persona", "id": persona_id, "spec": {"traits": traits}}


def test_a_trait_is_whole_only_when_every_persona_declaring_it_draws_it_whole() -> None:
    personas = [
        _persona(
            "persona-a",
            {
                "committed": 1,
                "fluency": Decimal("0.6"),
                "spend": {"dist": "normal", "mean": 40, "stddev": 5},
                "tier": {
                    "dist": "categorical",
                    "levels": {"1": Decimal("0.5"), "2": Decimal("0.5")},
                },
            },
        ),
        _persona("persona-b", {"committed": 0, "fluency": 1}),
    ]
    assert integral_trait_names(personas) == ("committed",)


def test_a_persona_that_does_not_declare_a_trait_neither_qualifies_nor_disqualifies_it() -> None:
    """Its subjects carry no value for the trait, so they hold no fraction either."""
    personas = [_persona("persona-a", {"committed": 1}), _persona("persona-b", {})]
    assert integral_trait_names(personas) == ("committed",)
    personas = [_persona("persona-a", {"fluency": Decimal("0.6")}), _persona("persona-b", {})]
    assert integral_trait_names(personas) == ()


def test_the_order_personas_are_listed_in_does_not_matter() -> None:
    personas = [_persona("persona-b", {"committed": 0}), _persona("persona-a", {"loyal": 1})]
    assert integral_trait_names(personas) == integral_trait_names(list(reversed(personas)))
    assert integral_trait_names(personas) == ("committed", "loyal")
