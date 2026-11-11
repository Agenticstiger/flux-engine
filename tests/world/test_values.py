# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``Decimal`` boundary: KERNEL-SPEC §2.1 keeps ``float`` out of every hash."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError
from flux_engine.world.values import (
    decimal_of,
    int_of,
    nfc_sorted_items,
    probability_of,
    weight_map,
)

pytestmark = pytest.mark.unit


def test_a_float_becomes_the_decimal_its_author_wrote_not_its_binary_expansion() -> None:
    # The rule the module docstring states: Decimal(repr(x)), not Decimal(x).
    assert decimal_of(0.6, what="t") == Decimal("0.6")
    assert Decimal(0.6) != Decimal("0.6")


@pytest.mark.property
@given(st.floats(allow_nan=False, allow_infinity=False, width=64))
def test_the_conversion_round_trips_back_to_the_same_double(value: float) -> None:
    """``repr`` is the shortest string that round-trips, so no information is lost."""
    assert float(decimal_of(value, what="t")) == value


@pytest.mark.property
@given(st.floats(allow_nan=False, allow_infinity=False, width=64))
def test_every_converted_value_is_canonicalisable(value: float) -> None:
    """The point of the conversion: the result may enter a hashed document."""
    assert canonical_bytes({"v": decimal_of(value, what="t")}).startswith(b'{"v":"')


def test_an_int_and_a_decimal_and_a_decimal_string_all_convert() -> None:
    assert decimal_of(7, what="t") == Decimal(7)
    assert decimal_of(Decimal("1.5"), what="t") == Decimal("1.5")
    assert decimal_of("1.5", what="t") == Decimal("1.5")


@pytest.mark.parametrize("value", [True, False])
def test_a_bool_is_refused_ahead_of_the_int_branch(value: bool) -> None:
    with pytest.raises(FluxError) as raised:
        decimal_of(value, what="dial")
    assert "FLX-5041" in str(raised.value)
    assert "bool" in str(raised.value)


@pytest.mark.parametrize("value", [None, b"1", [1], "not-a-number"])
def test_a_value_with_no_decimal_spelling_is_refused(value: object) -> None:
    with pytest.raises(FluxError):
        decimal_of(value, what="dial")


def test_an_error_names_the_class_and_never_the_value() -> None:
    with pytest.raises(FluxError) as raised:
        decimal_of([1234567], what="Persona 'p' trait 'secret'")
    message = str(raised.value)
    assert "list" in message and "1234567" not in message


def test_int_of_refuses_a_bool_and_a_float() -> None:
    assert int_of(3, what="t") == 3
    for value in (True, 3.0):
        with pytest.raises(FluxError):
            int_of(value, what="t")


@pytest.mark.parametrize("value", [-0.001, 1.5, Decimal("-1")])
def test_a_probability_outside_the_unit_interval_is_refused(value: object) -> None:
    with pytest.raises(FluxError) as raised:
        probability_of(value, what="successProbability")
    assert "FLX-5041" in str(raised.value)


def test_probability_accepts_both_bounds() -> None:
    assert probability_of(0, what="t") == Decimal(0)
    assert probability_of(1, what="t") == Decimal(1)


def test_a_weight_map_is_nfc_keyed_and_sorted() -> None:
    built = weight_map({"b": 0.5, "a": 0.5}, what="mix")
    assert list(built) == ["a", "b"]


def test_two_keys_that_normalise_to_one_are_a_collision() -> None:
    with pytest.raises(FluxError) as raised:
        weight_map({"é": 0.5, "é": 0.5}, what="mix")
    assert "FLX-5040" in str(raised.value)


def test_an_empty_weight_map_is_refused() -> None:
    with pytest.raises(FluxError):
        weight_map({}, what="mix")


def test_nfc_sorted_items_normalises_and_orders() -> None:
    assert nfc_sorted_items({"b": 2, "a": 1}) == (("a", 1), ("b", 2))
