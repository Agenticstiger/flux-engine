# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The exact functions agree with independent oracles (RFC-data-rules.md §2.6).

``ROUND`` is compared with :mod:`decimal`'s ``ROUND_HALF_UP`` (ties away
from zero, despite the name) and ``ROUND_HALF_EVEN`` on every terminating
decimal Hypothesis draws, and checked to give the nearest multiple of the
unit on rationals that have no finite expansion. The date differences are
compared with DuckDB in ``test_duckdb_oracle.py``; here, with their own
definition and the calendar's leap years.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.expr2.functions import add_months, months_between, round_exact, years_between

pytestmark = pytest.mark.unit

DECIMALS = st.decimals(
    min_value=Decimal("-1000000"),
    max_value=Decimal("1000000"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


@given(DECIMALS, st.integers(0, 5))
def test_round_matches_decimal_quantize(value: Decimal, scale: int) -> None:
    exponent = Decimal(1).scaleb(-scale)
    away = value.quantize(exponent, rounding=ROUND_HALF_UP)
    even = value.quantize(exponent, rounding=ROUND_HALF_EVEN)
    assert round_exact(Fraction(value), scale) == Fraction(away)
    assert round_exact(Fraction(value), scale, "HALF_TO_EVEN") == Fraction(even)


@given(st.integers(-(10**9), 10**9), st.integers(1, 10**6), st.integers(0, 6))
def test_round_of_a_rational_is_the_nearest_multiple(
    numerator: int, denominator: int, scale: int
) -> None:
    value = Fraction(numerator, denominator)
    unit = Fraction(1, 10**scale)
    result = round_exact(value, scale)
    assert (result / unit).denominator == 1
    assert abs(result - value) <= unit / 2
    if abs(result - value) == unit / 2:
        assert abs(result) > abs(value)


def test_ties_go_away_from_zero_or_to_even() -> None:
    assert round_exact(Fraction("2.5"), 0) == 3
    assert round_exact(Fraction("-2.5"), 0) == -3
    assert round_exact(Fraction("2.5"), 0, "HALF_TO_EVEN") == 2
    assert round_exact(Fraction("-2.5"), 0, "HALF_TO_EVEN") == -2
    assert round_exact(Fraction(1, 3), 2) == Fraction("0.33")
    assert round_exact(Fraction(-2, 3), 2) == Fraction("-0.67")


def test_add_months_clamps_to_the_last_day() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2000, 2, 29), 12 * 26) == date(2026, 2, 28)
    assert add_months(date(1900, 1, 31), 1) == date(1900, 2, 28)
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)


@given(
    st.dates(min_value=date(1, 1, 1), max_value=date(9999, 12, 31)),
    st.dates(min_value=date(1, 1, 1), max_value=date(9999, 12, 31)),
)
def test_months_between_is_antisymmetric_and_whole(a: date, b: date) -> None:
    months = months_between(a, b)
    assert months == -months_between(b, a)
    if b >= a:
        assert add_months(a, months) <= b
        if (a.year * 12 + a.month - 1 + months + 1) // 12 <= 9999:
            assert add_months(a, months + 1) > b
        assert years_between(a, b) == months // 12
