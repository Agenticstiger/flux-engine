# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The test a count, an ordinal or a size is read with, and each caller's own refusal.

:func:`flux_stats.integers.is_int_at_least` is the one test; the callers in
this package keep their messages, and these tests pin them, so moving the test
here changed no refusal.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

from flux_stats.integers import is_int_at_least
from flux_stats.sampling import t_critical
from flux_stats.tails import upper_tail_at_most

pytestmark = pytest.mark.unit


class _Level(enum.IntEnum):
    ONE = 1


@pytest.mark.parametrize("value", [0, 1, 2, 10**30, _Level.ONE])
def test_an_int_at_or_above_the_floor_passes(value: Any) -> None:
    assert is_int_at_least(value, 0)


@pytest.mark.parametrize(
    "value", [True, False, -1, -(10**30), 1.0, "1", None, Decimal(1), Fraction(1), b"1"]
)
def test_a_bool_a_negative_and_every_other_class_fail(value: Any) -> None:
    assert not is_int_at_least(value, 0)


def test_the_floor_is_inclusive() -> None:
    assert is_int_at_least(1, 1)
    assert not is_int_at_least(0, 1)
    assert is_int_at_least(-3, -3)


#: Each caller in this package, the values it refuses, and its own message.
REFUSALS = [
    (
        "tails",
        lambda value: upper_tail_at_most(Fraction(1, 2), value, 0, Fraction(1, 2)),
        (True, -1, 1.0),
        "records must be a whole number not below 0",
    ),
    (
        "sampling",
        t_critical,
        (True, 0, 1.0),
        "degrees of freedom must be a whole number of at least 1",
    ),
]


@pytest.mark.parametrize(
    ("refuse", "value", "message"),
    [
        pytest.param(refuse, value, message, id=f"{caller}-{value!r}")
        for caller, refuse, values, message in REFUSALS
        for value in values
    ],
)
def test_each_caller_keeps_its_own_refusal(refuse: Any, value: Any, message: str) -> None:
    with pytest.raises(ValueError) as raised:
        refuse(value)
    assert str(raised.value) == message
