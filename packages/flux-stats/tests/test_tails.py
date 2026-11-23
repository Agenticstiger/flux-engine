# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The binomial-tail decisions, against the tails written out in full.

Every decision is checked against the exact rational it decides, formed
here the long way: ``base ** exponent`` as a :class:`~fractions.Fraction`,
the upper tail as the sum of its terms, Chernoff's bound as its two powers,
and the expected occupancy as its sum. The edges are pinned on their own: a
level equal to the value (decided on the exact fallback when the value has
no finite binary expansion), a level a hair below it, ``0 ** 0``, a count
at the mean, and the inputs refused. Large runs are decided from the bounds
alone, which is what lets a shape check read a run of millions of records.
"""

from __future__ import annotations

from fractions import Fraction
from math import comb

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_stats.tails import occupancy_compare, power_at_most, upper_tail_at_most

_SHARES = st.fractions(min_value=0, max_value=1, max_denominator=1000)
_LEVELS = st.fractions(min_value=0, max_value=1, max_denominator=10**6)


def _upper_tail(share: Fraction, records: int, count: int) -> Fraction:
    return sum(
        (
            comb(records, taken) * share**taken * (1 - share) ** (records - taken)
            for taken in range(count, records + 1)
        ),
        Fraction(0),
    )


def _chernoff(share: Fraction, records: int, count: int) -> Fraction:
    return (records * share / count) ** count * (records * (1 - share) / (records - count)) ** (
        records - count
    )


def _occupancy(cells, records: int) -> Fraction:  # type: ignore[no-untyped-def]
    return sum(
        (multiplicity * (1 - (1 - share) ** records) for multiplicity, share in cells),
        Fraction(0),
    )


# ---------------------------------------------------------------------------
# power_at_most: the empty cell's tail, (1 - p)^n
# ---------------------------------------------------------------------------


@settings(max_examples=300)
@given(base=_SHARES, exponent=st.integers(min_value=0, max_value=300), level=_LEVELS)
def test_a_power_is_decided_as_the_exact_value_decides_it(
    base: Fraction, exponent: int, level: Fraction
) -> None:
    assert power_at_most(base, exponent, level) is (base**exponent <= level)


@settings(max_examples=100)
@given(base=_SHARES, exponent=st.integers(min_value=1, max_value=200))
def test_a_level_equal_to_the_power_is_met_and_one_a_hair_below_it_is_not(
    base: Fraction, exponent: int
) -> None:
    value = base**exponent
    assert power_at_most(base, exponent, value) is True
    if value > 0:
        assert power_at_most(base, exponent, value - Fraction(1, 2**20000)) is False


def test_the_exact_fallback_decides_a_value_with_no_finite_binary_expansion() -> None:
    # 1/3**7 lies strictly between every pair of binary bounds, so only the
    # exact value can say a level of exactly 1/3**7 is met.
    assert power_at_most(Fraction(1, 3), 7, Fraction(1, 3**7)) is True
    assert power_at_most(Fraction(1, 3), 7, Fraction(1, 3**7) - Fraction(1, 10**4000)) is False


def test_zero_to_the_zero_is_one_and_zero_to_any_other_power_is_zero() -> None:
    assert power_at_most(Fraction(0), 0, Fraction(1)) is True
    assert power_at_most(Fraction(0), 0, Fraction(1, 2)) is False
    assert power_at_most(Fraction(0), 5, Fraction(0)) is True
    assert power_at_most(Fraction(1), 10**9, Fraction(1)) is True
    assert power_at_most(Fraction(1), 10**9, Fraction(999, 1000)) is False


def test_an_empty_cell_of_a_large_run_is_decided_from_the_bounds() -> None:
    # (1 - 10**-6) ** 10**8 is about e**-100; written out it would hold some
    # 600 million digits. The bounds decide it either way.
    share = Fraction(1, 10**6)
    assert power_at_most(1 - share, 10**8, Fraction(1, 10**40)) is True
    assert power_at_most(1 - share, 10**8, Fraction(1, 10**50)) is False
    assert power_at_most(1 - share, 10, Fraction(1, 2)) is False


# ---------------------------------------------------------------------------
# upper_tail_at_most: the point mass's tail, through Chernoff's bound
# ---------------------------------------------------------------------------


@settings(max_examples=300)
@given(
    share=_SHARES,
    records=st.integers(min_value=1, max_value=60),
    data=st.data(),
    level=_LEVELS,
)
def test_the_upper_tail_is_decided_on_chernoffs_bound_and_never_wrongly(
    share: Fraction, records: int, data: st.DataObject, level: Fraction
) -> None:
    count = data.draw(st.integers(min_value=0, max_value=records))
    decided = upper_tail_at_most(share, records, count, level)
    tail = _upper_tail(share, records, count)
    if decided:
        assert tail <= level
    if count == 0:
        assert decided is (1 <= level)
    elif share == 0:
        assert decided is True
    elif count == records:
        assert decided is (share**records <= level)
    elif count <= records * share:
        assert decided is (1 <= level)
    else:
        assert decided is (_chernoff(share, records, count) <= level)


@settings(max_examples=300)
@given(share=_SHARES, records=st.integers(min_value=2, max_value=60), data=st.data())
def test_chernoffs_bound_is_never_below_the_tail(
    share: Fraction, records: int, data: st.DataObject
) -> None:
    count = data.draw(st.integers(min_value=1, max_value=records - 1))
    if 0 < share and records * share < count:
        assert _chernoff(share, records, count) >= _upper_tail(share, records, count)


def test_a_count_at_the_mean_is_shown_nothing_below_one() -> None:
    assert upper_tail_at_most(Fraction(1, 2), 10, 5, Fraction(999, 1000)) is False
    assert upper_tail_at_most(Fraction(1, 2), 10, 5, Fraction(1)) is True


def test_a_point_mass_of_a_large_run_is_decided_from_the_bounds() -> None:
    # A tenth of ten million records on one value whose cell the profile
    # gives a hundredth: Chernoff's bound is far below any level in use.
    assert upper_tail_at_most(Fraction(1, 100), 10**7, 10**6, Fraction(1, 10**9)) is True
    # The same tenth where the profile gives the cell a tenth is the mean.
    assert upper_tail_at_most(Fraction(1, 10), 10**7, 10**6, Fraction(1, 10**9)) is False


# ---------------------------------------------------------------------------
# occupancy_compare: the distinct count a sample of n is expected to show
# ---------------------------------------------------------------------------

_CELLS = st.lists(st.tuples(st.integers(min_value=0, max_value=5), _SHARES), min_size=0, max_size=6)


@settings(max_examples=300)
@given(
    cells=_CELLS,
    records=st.integers(min_value=0, max_value=120),
    value=st.fractions(min_value=0, max_value=40, max_denominator=1000),
)
def test_the_occupancy_is_compared_as_the_exact_sum_compares(
    cells: list, records: int, value: Fraction  # type: ignore[type-arg]
) -> None:
    exact = _occupancy(cells, records)
    assert occupancy_compare(cells, records, value) == (exact > value) - (exact < value)


@settings(max_examples=50)
@given(cells=_CELLS, records=st.integers(min_value=0, max_value=60))
def test_the_occupancy_equals_itself(cells: list, records: int) -> None:  # type: ignore[type-arg]
    assert occupancy_compare(cells, records, _occupancy(cells, records)) == 0


def test_an_occupancy_a_hair_below_its_cells_is_decided_without_the_exact_sum() -> None:
    # Ten million draws over four cells leave an empty mass of about 2e-1249387:
    # the occupancy is that far below 4, and is decided against 4, against
    # 4 less a little, and against 3, from the bounds on that mass alone.
    cells = [(4, Fraction(1, 4))]
    assert occupancy_compare(cells, 10**7, Fraction(4)) == -1
    assert occupancy_compare(cells, 10**7, Fraction(4) - Fraction(1, 10**9)) == 1
    assert occupancy_compare(cells, 10**7, Fraction(3)) == 1
    assert occupancy_compare([(3, Fraction(1))], 10, Fraction(3)) == 0
    assert occupancy_compare([(3, Fraction(1))], 0, Fraction(0)) == 0
    many = [(1, Fraction(index + 1, 10**6)) for index in range(10**4)]
    assert occupancy_compare(many, 10**8, Fraction(10**4)) == -1


def test_a_distinct_count_of_a_large_run_is_decided_from_the_bounds() -> None:
    # A million equally likely values, ten million draws: about
    # 10**6 * (1 - e**-10), some 999,954.6 of them, are expected to be seen.
    cells = [(10**6, Fraction(1, 10**6))]
    assert occupancy_compare(cells, 10**7, Fraction(999_900)) == 1
    assert occupancy_compare(cells, 10**7, Fraction(999_960)) == -1


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: power_at_most(Fraction(-1, 2), 3, Fraction(1)),
        lambda: power_at_most(Fraction(1, 2), -1, Fraction(1)),
        lambda: power_at_most(Fraction(1, 2), True, Fraction(1)),
        lambda: power_at_most(Fraction(1, 2), 3, Fraction(-1)),
        lambda: upper_tail_at_most(Fraction(3, 2), 10, 1, Fraction(1)),
        lambda: upper_tail_at_most(Fraction(1, 2), 10, 11, Fraction(1)),
        lambda: upper_tail_at_most(Fraction(1, 2), -1, 0, Fraction(1)),
        lambda: occupancy_compare([(1, Fraction(2))], 10, Fraction(1)),
        lambda: occupancy_compare([(-1, Fraction(1, 2))], 10, Fraction(1)),
        lambda: occupancy_compare([(1, Fraction(1, 2))], -5, Fraction(1)),
    ],
)
def test_an_input_outside_the_domain_is_refused(call) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        call()
