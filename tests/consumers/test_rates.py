# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A query's rate (ADR 0028 section 4): exact fractions and three statuses."""

from __future__ import annotations

from fractions import Fraction

import pytest

from flux_engine.consumers.rates import (
    MEASURED,
    NO_ROWS,
    NOT_A_RATE,
    group_rates,
    ordered_counts,
    rate_of,
)


def test_a_measured_rate_is_an_exact_fraction() -> None:
    rate = rate_of(1, 3)
    assert rate.status == MEASURED
    assert rate.value == Fraction(1, 3)
    assert isinstance(rate.value, Fraction)  # never a float: 1/3 has no exact one


def test_a_rate_is_reduced_and_zero_is_a_rate() -> None:
    assert rate_of(2, 4).value == Fraction(1, 2)
    zero = rate_of(0, 7)
    assert (zero.status, zero.value) == (MEASURED, Fraction(0))
    whole = rate_of(7, 7)
    assert (whole.status, whole.value) == (MEASURED, Fraction(1))


def test_rows_past_the_unit_are_not_a_rate() -> None:
    rate = rate_of(5, 4)
    assert (rate.status, rate.value) == (NOT_A_RATE, None)
    assert (rate.rows, rate.unit_rows) == (5, 4)


def test_an_empty_unit_has_no_rows_and_rows_from_it_are_not_a_rate() -> None:
    empty = rate_of(0, 0)
    assert (empty.status, empty.value) == (NO_ROWS, None)
    assert rate_of(1, 0).status == NOT_A_RATE


@pytest.mark.parametrize("bad", [-1, True, 1.5, "1"])
def test_a_count_is_a_whole_number_from_zero(bad: object) -> None:
    with pytest.raises(ValueError):
        rate_of(bad, 4)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        rate_of(1, bad)  # type: ignore[arg-type]


def test_group_rates_put_the_null_group_first_and_cover_either_side() -> None:
    rates = group_rates(
        [("b", 1), (None, 1), ("a", 0)],
        [("a", 2), ("b", 1), (None, 3), ("c", 4)],
    )
    assert [group.key for group in rates] == [None, "a", "b", "c"]
    by_key = {group.key: group.rate for group in rates}
    assert by_key[None].value == Fraction(1, 3)
    assert by_key["a"].value == Fraction(0)
    assert by_key["c"].status == MEASURED and by_key["c"].rows == 0


def test_a_group_only_the_query_holds_is_not_a_rate() -> None:
    (only,) = group_rates([("x", 2)], [])
    assert (only.key, only.rate.status) == ("x", NOT_A_RATE)


def test_a_null_group_is_its_own_key_never_a_level() -> None:
    rates = group_rates([(None, 1), ("None", 1)], [(None, 2), ("None", 2)])
    assert [group.key for group in rates] == [None, "None"]


def test_a_group_named_twice_is_refused() -> None:
    with pytest.raises(ValueError):
        ordered_counts([("a", 1), ("a", 2)])


def test_the_order_is_by_code_point_whatever_the_input_order() -> None:
    assert ordered_counts([("b", 1), ("B", 2), (None, 3), ("a", 4)]) == (
        (None, 3),
        ("B", 2),
        ("a", 4),
        ("b", 1),
    )
