# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Sampling families: fixed word cost, sorted draw order, declared types."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, List

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core.rng import DrawStream
from flux_engine.expr.types import BOOL, NUMBER, SET_STRING, STRING, TypeKind, enum_of
from flux_engine.world.families import (
    Bernoulli,
    BoundedNormal,
    Categorical,
    Constant,
    Family,
    RateCount,
    SetOf,
    drawn_type_satisfies,
)

pytestmark = pytest.mark.unit


def stream(entropy: int = 12345) -> DrawStream:
    return DrawStream.from_entropy(entropy)


def draw_on(family: Family, source: DrawStream) -> Any:
    """Draw one value on *source*.

    A named helper rather than an inline ``lambda: source`` inside a loop: a
    lambda written in a loop closes over the loop variable rather than binding
    it (ruff B023), which is exactly the class of bug that would make a
    determinism test compare a family against the last stream instead of its own.
    """
    return family.draw(lambda: source)


ALL_FAMILIES: List[Family] = [
    Constant(Decimal("0.6")),
    Categorical({"a": Decimal("0.25"), "b": Decimal("0.75")}),
    BoundedNormal(mean=Decimal(10), stddev=Decimal(2), lo=Decimal(0), hi=Decimal(20), scale=3),
    RateCount(mean=Decimal("1.5")),
    RateCount(mean=Decimal("1.5"), offset=1),
    Bernoulli(Decimal("0.4")),
    SetOf({"x": Decimal("0.5"), "y": Decimal("0.5"), "z": Decimal("0.5")}),
]


@pytest.mark.parametrize("family", ALL_FAMILIES, ids=lambda f: type(f).__name__)
def test_a_draw_consumes_exactly_the_declared_word_cost(family: Family) -> None:
    source = stream()
    draw_on(family, source)
    assert source.position == family.word_cost


@pytest.mark.parametrize("family", ALL_FAMILIES, ids=lambda f: type(f).__name__)
def test_the_word_cost_does_not_depend_on_the_value_drawn(family: Family) -> None:
    """A data-dependent cost would make one stream's position depend on another's data."""
    positions = set()
    for entropy in range(1, 40):
        source = stream(entropy)
        draw_on(family, source)
        positions.add(source.position)
    assert positions == {family.word_cost}


class _Counting:
    """A stream factory that counts how many times a family asked for a stream."""

    def __init__(self, source: DrawStream) -> None:
        self._source = source
        self.opened = 0

    def __call__(self) -> DrawStream:
        self.opened += 1
        return self._source


def test_a_constant_never_opens_its_stream() -> None:
    """KERNEL-SPEC §3 insertion stability: a scalar trait materialises no path."""
    counter = _Counting(stream())
    assert Constant(Decimal("0.6")).draw(counter) == Decimal("0.6")
    assert counter.opened == 0


def test_every_other_family_opens_its_stream_exactly_once() -> None:
    for family in ALL_FAMILIES:
        if family.word_cost == 0:
            continue
        counter = _Counting(stream())
        family.draw(counter)
        assert counter.opened == 1, type(family).__name__


def test_a_set_draws_its_members_in_nfc_sorted_order_whatever_the_mapping_order() -> None:
    forward = SetOf({"x": Decimal("0.5"), "y": Decimal("0.9"), "z": Decimal("0.1")})
    backward = SetOf({"z": Decimal("0.1"), "y": Decimal("0.9"), "x": Decimal("0.5")})
    for entropy in range(1, 20):
        assert draw_on(forward, stream(entropy)) == draw_on(backward, stream(entropy))


def test_a_set_may_come_out_empty_and_that_is_a_legal_value() -> None:
    never = SetOf({"a": Decimal(0), "b": Decimal(0)})
    assert draw_on(never, stream()) == frozenset()
    assert never.member_names == ("a", "b")


def test_rate_count_offsets_rather_than_clamps() -> None:
    """An offset shifts the whole distribution; a clamp would spike the bound."""
    base = RateCount(mean=Decimal("1.2"))
    shifted = RateCount(mean=Decimal("1.2"), offset=1)
    for entropy in range(1, 30):
        assert draw_on(shifted, stream(entropy)) == draw_on(base, stream(entropy)) + 1


def test_a_bounded_normal_stays_inside_its_declared_interval() -> None:
    family = BoundedNormal(
        mean=Decimal("42"), stddev=Decimal("11"), lo=Decimal("0"), hi=Decimal("60"), scale=2
    )
    for entropy in range(1, 200):
        value = draw_on(family, stream(entropy))
        assert Decimal(0) <= value <= Decimal(60)


@pytest.mark.property
@settings(max_examples=50)
@given(st.integers(min_value=1, max_value=2**32))
def test_a_categorical_only_ever_returns_a_declared_level(entropy: int) -> None:
    family = Categorical({"low": Decimal("0.2"), "medium": Decimal("0.5"), "high": Decimal("0.3")})
    assert draw_on(family, stream(entropy)) in {"low", "medium", "high"}


def test_declared_expression_types() -> None:
    assert Constant(Decimal(1)).expr_type is NUMBER
    assert Constant("x", declared_type=STRING).expr_type is STRING
    assert BoundedNormal(Decimal(0), Decimal(1), Decimal(-1), Decimal(1)).expr_type is NUMBER
    assert RateCount(Decimal(1)).expr_type is NUMBER
    assert Bernoulli(Decimal("0.5")).expr_type is BOOL
    assert SetOf({"a": Decimal("0.5")}).expr_type is SET_STRING
    assert Categorical({"a": Decimal(1)}).expr_type.kind is TypeKind.ENUM


def test_an_enum_may_fill_a_string_but_a_string_may_not_fill_an_enum() -> None:
    closed = enum_of(["a", "b"])
    assert drawn_type_satisfies(STRING, closed)
    assert not drawn_type_satisfies(closed, STRING)


def test_an_enum_fills_an_enum_only_when_its_levels_are_a_subset() -> None:
    assert drawn_type_satisfies(enum_of(["a", "b"]), enum_of(["a"]))
    assert not drawn_type_satisfies(enum_of(["a"]), enum_of(["a", "b"]))


def test_unrelated_kinds_never_satisfy_each_other() -> None:
    assert not drawn_type_satisfies(NUMBER, BOOL)
    assert drawn_type_satisfies(NUMBER, NUMBER)
