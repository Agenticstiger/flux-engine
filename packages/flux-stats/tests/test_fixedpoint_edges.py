# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The grid's boundaries, its exact bounds and the place each refusal names.

``test_fixedpoint.py`` pins the soundness properties. This file pins what a
caller reads off the module besides a verdict: the ``FLX`` code of each
refusal and the subject it names (``side a``, ``tolerance``, ``bins[2]``),
because a refusal that does not say which input it is about cannot be acted
on; the value a refusal quotes; the exact bound a computation returns
(``magnitude_bits``, the square-root lower bound behind the correlation
epsilon); and each ``<`` against ``<=`` at the edge of a declared range.

A refusal is pinned by its code, the subject it opens with and the value it
quotes. Where two refusals share a code and a subject, a few words of the
message tell them apart, and only those words are pinned. Expected figures
are computed from the inputs, and the exact bounds are also checked against
values worked out by hand, so that a formula copied wrong into a test and
into the module cannot pass both.
"""

from __future__ import annotations

import math
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Dict, List

import pytest

from flux_stats.errors import FluxError
from flux_stats.fixedpoint import (
    DEFAULT_FIELD_CAPACITY_BITS,
    DEFAULT_FRACTIONAL_BITS,
    MAX_FRACTIONAL_BITS,
    Bound,
    Grid,
    Interval,
    SecondMoments,
    _sqrt_lower_bound,
    check_field_capacity,
    correlation_within,
    correlation_within_bounds,
    effective_tolerance,
    epsilon_correlation,
    epsilon_marginal_histogram,
    epsilon_mean_relative,
    magnitude_bits,
    mean_within_absolute,
    mean_within_relative,
    order_statistic_index,
    quantile_rank_ok,
    quantile_within,
    require_sample,
    support_within,
    tighten_onto_grid,
    to_fraction,
    total_variation_within,
)

pytestmark = pytest.mark.unit

GRID = Grid(DEFAULT_FRACTIONAL_BITS)

#: A float is not on the grid: handing one to anything that reads a number
#: is how a test reaches the ``FLX-5003`` refusal and the subject it names.
A_FLOAT = 1.5


def _refused(code: str, call: Callable[[], object]) -> str:
    """The detail of the refusal *call* raises, after checking its ``FLX`` code."""
    with pytest.raises(FluxError) as raised:
        call()
    assert raised.value.code.code == code, raised.value.detail
    assert raised.value.detail
    return raised.value.detail


def _moments(xs: List[int], ys: List[int]) -> SecondMoments:
    return SecondMoments(
        count=len(xs),
        sum_x=sum(xs),
        sum_y=sum(ys),
        sum_xx=sum(x * x for x in xs),
        sum_yy=sum(y * y for y in ys),
        sum_xy=sum(x * y for x, y in zip(xs, ys, strict=True)),
    )


#: Two columns that clear a variance floor of one, with room.
SPREAD = _moments([0, 3, 6, 9], [1, 5, 4, 9])


# ---------------------------------------------------------------------------
# What is not a number on the grid, and which input said so
# ---------------------------------------------------------------------------


def test_a_value_that_is_not_exact_is_refused_naming_its_subject_and_class() -> None:
    assert _refused("FLX-5003", lambda: to_fraction(True)).startswith("value: ")
    assert "bool" in _refused("FLX-5003", lambda: to_fraction(True))
    detail = _refused("FLX-5003", lambda: to_fraction(A_FLOAT))
    assert detail.startswith("value: ") and "'float'" in detail
    assert _refused("FLX-5003", lambda: to_fraction(A_FLOAT, subject="lo")).startswith("lo: ")
    for text in ("NaN", "Infinity", "-Infinity"):
        detail = _refused("FLX-5003", lambda text=text: to_fraction(Decimal(text)))
        assert detail.startswith("value: ") and text.lstrip("-") in detail


def test_the_quantisers_name_the_subject_of_a_value_they_refuse() -> None:
    for call in (GRID.quantise, GRID.quantise_ceil, GRID.is_on_grid):
        assert _refused("FLX-5003", lambda call=call: call(A_FLOAT)).startswith("value: ")
        named = _refused("FLX-5003", lambda call=call: call(A_FLOAT, subject="edge"))
        assert named.startswith("edge: ")


def test_a_tightened_bound_names_which_side_it_was_tightening() -> None:
    upper = _refused("FLX-5003", lambda: tighten_onto_grid(GRID, A_FLOAT, Bound.UPPER))
    lower = _refused("FLX-5003", lambda: tighten_onto_grid(GRID, A_FLOAT, Bound.LOWER))
    assert upper.startswith("upper bound: ")
    assert lower.startswith("lower bound: ")


def test_the_widest_grid_is_accepted_and_one_past_it_is_not() -> None:
    assert Grid(MAX_FRACTIONAL_BITS).scale == 1 << MAX_FRACTIONAL_BITS
    assert Grid(0).scale == 1
    too_wide = _refused("FLX-5004", lambda: Grid(MAX_FRACTIONAL_BITS + 1))
    assert str(MAX_FRACTIONAL_BITS + 1) in too_wide and f"0..{MAX_FRACTIONAL_BITS}" in too_wide
    assert "'float'" in _refused("FLX-5004", lambda: Grid(A_FLOAT))  # type: ignore[arg-type]
    assert "'bool'" in _refused("FLX-5004", lambda: Grid(True))


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------


def test_an_interval_may_be_a_single_point_but_not_inverted() -> None:
    point = Interval(Fraction(2), Fraction(2))
    assert point.width == 0 and point.contains(2)
    detail = _refused("FLX-5004", lambda: Interval(Fraction(3), Fraction(2)))
    assert "[3, 2]" in detail


def test_an_interval_names_the_bound_of_a_number_it_refuses() -> None:
    assert _refused("FLX-5003", lambda: Interval.of(A_FLOAT, 2)).startswith("range.min: ")
    assert _refused("FLX-5003", lambda: Interval.of(1, A_FLOAT)).startswith("range.max: ")
    named = _refused("FLX-5003", lambda: Interval.of(A_FLOAT, 2, subject="amount"))
    assert named.startswith("amount.min: ")


def test_an_interval_contains_both_of_its_endpoints_and_nothing_beyond() -> None:
    interval = Interval.of(-2, 3)
    tiny = Fraction(1, 10**9)
    assert interval.contains(-2) and interval.contains(3) and interval.contains(0)
    assert not interval.contains(-2 - tiny) and not interval.contains(3 + tiny)


@pytest.mark.parametrize(
    "left, right",
    [
        # Every corner takes a turn as the minimum or the maximum: the
        # product of the two lows, then of the low and the high.
        ((-3, -2), (-5, -4)),
        ((-3, -2), (4, 5)),
        ((2, 3), (-5, -4)),
        ((2, 3), (4, 5)),
        ((-3, 2), (-5, 4)),
    ],
)
def test_an_interval_product_is_the_hull_of_its_four_corner_products(
    left: tuple[int, int], right: tuple[int, int]
) -> None:
    a, b = Interval.of(*left), Interval.of(*right)
    corners = [x * y for x in left for y in right]
    product = a * b
    assert (product.lo, product.hi) == (min(corners), max(corners))


# ---------------------------------------------------------------------------
# The epsilons
# ---------------------------------------------------------------------------


def test_a_relative_mean_epsilon_divides_by_the_smallest_reference_magnitude() -> None:
    tolerance = Fraction(1, 10)
    reference = Interval.of(2, 10)
    expected = GRID.resolution * (1 + tolerance) / reference.min_magnitude
    assert epsilon_mean_relative(GRID, reference, tolerance) == expected
    negative = Interval.of(-10, -4)
    assert epsilon_mean_relative(GRID, negative, tolerance) == (
        GRID.resolution * (1 + tolerance) / 4
    )


def test_a_relative_mean_epsilon_refuses_what_it_cannot_bound() -> None:
    interval = Interval.of(2, 10)
    assert _refused("FLX-5003", lambda: epsilon_mean_relative(GRID, interval, A_FLOAT)).startswith(
        "tolerance: "
    )
    assert _refused("FLX-5002", lambda: epsilon_mean_relative(GRID, interval, 0)).startswith(
        "tolerance 0 "
    )
    _refused("FLX-5002", lambda: epsilon_mean_relative(GRID, Interval.of(-1, 1), Fraction(1, 10)))


def test_a_histogram_needs_two_edges_and_two_are_enough() -> None:
    assert epsilon_marginal_histogram(GRID, [0, 1]) == 0
    assert _refused("FLX-5004", lambda: epsilon_marginal_histogram(GRID, [0])).endswith("got 1")
    _refused("FLX-5004", lambda: epsilon_marginal_histogram(GRID, []))


def test_a_histogram_edge_is_refused_naming_its_position() -> None:
    off_grid = _refused(
        "FLX-5004", lambda: epsilon_marginal_histogram(Grid(0), [0, 1, Fraction(5, 2)])
    )
    assert off_grid.startswith("bins[2] ") and "fractionalBits=0" in off_grid
    flat = _refused("FLX-5004", lambda: epsilon_marginal_histogram(GRID, [0, 1, 1]))
    assert flat.startswith("bins[2] ")
    backwards = _refused("FLX-5004", lambda: epsilon_marginal_histogram(GRID, [0, 2, 1]))
    assert backwards.startswith("bins[2] ")
    not_exact = _refused("FLX-5003", lambda: epsilon_marginal_histogram(GRID, [0, A_FLOAT]))
    assert not_exact.startswith("bins[1]: ")


def test_the_correlation_epsilon_is_eight_steps_over_the_lower_square_root() -> None:
    # By hand: the root of 4 is exactly 2, so 8 steps of 2**-16 over 2 is 2**-14.
    assert epsilon_correlation(GRID, Fraction(4)) == Fraction(1, 2**14)
    for variance in (Fraction(2), Fraction(1, 3), Fraction(10**6), Fraction(7, 5)):
        scaled = (variance.numerator << 128) // variance.denominator
        root = Fraction(math.isqrt(scaled), 1 << 64)
        assert epsilon_correlation(GRID, variance) == 8 * GRID.resolution / root


def test_the_square_root_lower_bound_is_the_sixty_four_bit_floor() -> None:
    for value in (Fraction(2), Fraction(1, 3), Fraction(5, 7), Fraction(10**9 + 7)):
        scaled = (value.numerator << 128) // value.denominator
        assert _sqrt_lower_bound(value) == Fraction(math.isqrt(scaled), 1 << 64)
        assert _sqrt_lower_bound(value, bits=10) == Fraction(
            math.isqrt((value.numerator << 20) // value.denominator), 1 << 10
        )
    assert _sqrt_lower_bound(Fraction(4)) == 2
    assert _sqrt_lower_bound(Fraction(9, 4)) == Fraction(3, 2)


def test_the_square_root_lower_bound_refuses_zero_and_what_is_too_small_to_bound() -> None:
    assert _refused("FLX-5004", lambda: _sqrt_lower_bound(Fraction(0))).startswith(
        "cannot bound the square root"
    )
    assert _refused("FLX-5004", lambda: _sqrt_lower_bound(Fraction(-1))).startswith(
        "cannot bound the square root"
    )
    too_small = _refused("FLX-5004", lambda: _sqrt_lower_bound(Fraction(1, 2**129)))
    assert "64 bits" in too_small and not too_small.startswith("cannot bound")
    assert _sqrt_lower_bound(Fraction(1, 2**128)) == Fraction(1, 2**64)


def test_the_correlation_epsilon_refuses_a_floor_that_is_not_positive() -> None:
    assert _refused("FLX-5003", lambda: epsilon_correlation(GRID, A_FLOAT)).startswith(
        "minVariance: "
    )
    for floor in (0, Fraction(-1, 2)):
        detail = _refused("FLX-5004", lambda floor=floor: epsilon_correlation(GRID, floor))
        assert "minVariance" in detail and "square root" not in detail


# ---------------------------------------------------------------------------
# The margin, tau' = tau - epsilon
# ---------------------------------------------------------------------------


def test_the_margin_refuses_an_epsilon_that_equals_the_tolerance() -> None:
    tolerance = Fraction(1, 8)
    detail = _refused("FLX-5002", lambda: effective_tolerance(GRID, tolerance, tolerance))
    assert str(tolerance) in detail and "fractionalBits" not in detail
    assert effective_tolerance(GRID, tolerance, tolerance - GRID.resolution) == GRID.resolution


def test_the_margin_refuses_a_tolerance_that_is_not_positive_before_comparing_it() -> None:
    zero = _refused("FLX-5002", lambda: effective_tolerance(GRID, 0, 0))
    negative = _refused("FLX-5002", lambda: effective_tolerance(GRID, Fraction(-1, 4), 0))
    assert "positive" in zero and "positive" in negative
    assert "-1/4" in negative


def test_the_margin_names_the_number_it_refuses() -> None:
    assert _refused("FLX-5003", lambda: effective_tolerance(GRID, A_FLOAT, 0)).startswith(
        "tolerance: "
    )
    assert _refused("FLX-5003", lambda: effective_tolerance(GRID, 1, A_FLOAT)).startswith(
        "epsilon: "
    )
    negative = _refused("FLX-5004", lambda: effective_tolerance(GRID, 1, Fraction(-1, 3)))
    assert "-1/3" in negative


def test_a_margin_that_rounds_to_zero_is_refused_naming_the_grid() -> None:
    # Half a grid step above the epsilon, so the difference floors to zero.
    tolerance = GRID.resolution / 2
    detail = _refused("FLX-5002", lambda: effective_tolerance(GRID, tolerance, 0))
    assert f"fractionalBits={GRID.fractional_bits}" in detail


# ---------------------------------------------------------------------------
# The compile-time magnitude bound
# ---------------------------------------------------------------------------


def _expected_bits(grid: Grid, interval: Interval, records: int, margin: int) -> Dict[str, int]:
    k = records
    q = max(abs(grid.quantise(interval.lo)), abs(grid.quantise(interval.hi)))
    widths = {
        "value": q,
        "sum": k * q,
        "sumOfProducts": k * q**2,
        "centredSecondMoment": k**2 * q**2,
        "correlationNumerator": 2 * k**2 * q**2,
        "meanComparison": margin * k**2 * q,
        "totalVariationComparison": 2 * margin * k**2,
        "correlationComparison": 4 * margin**2 * k**4 * q**4,
    }
    return {name: value.bit_length() for name, value in sorted(widths.items())}


def test_the_magnitude_bound_for_one_declaration_worked_out_by_hand() -> None:
    # Grid(4) has scale 16, so the range [-2, 3] encodes to a widest value of
    # q = 48, and with no tolerance the margin is the scale, 16. With k = 10:
    # 48 -> 6 bits, 480 -> 9, 23_040 -> 15, 230_400 -> 18, 460_800 -> 19,
    # 76_800 -> 17, 3_200 -> 12, and 4 * 16**2 * 10**4 * 48**4 -> 46.
    assert magnitude_bits(grid=Grid(4), value_range=Interval.of(-2, 3), max_records=10) == {
        "centredSecondMoment": 18,
        "correlationComparison": 46,
        "correlationNumerator": 19,
        "meanComparison": 17,
        "sum": 9,
        "sumOfProducts": 15,
        "totalVariationComparison": 12,
        "value": 6,
    }


def test_the_magnitude_bound_is_each_formula_s_width_with_the_grid_scale_as_margin() -> None:
    grid = Grid(4)
    interval = Interval.of(-2, 3)
    for records in (1, 2, 10, 1000):
        bounds = magnitude_bits(grid=grid, value_range=interval, max_records=records)
        assert bounds == _expected_bits(grid, interval, records, grid.scale)
        assert list(bounds) == sorted(bounds)


def test_the_magnitude_bound_takes_the_margin_from_the_tolerance_s_denominator() -> None:
    grid = Grid(4)
    interval = Interval.of(-2, 3)
    for tolerance, margin in ((Fraction(1, 7), 7), (Decimal("0.25"), 4), (3, 1)):
        bounds = magnitude_bits(
            grid=grid, value_range=interval, max_records=10, tolerance=tolerance
        )
        assert bounds == _expected_bits(grid, interval, 10, margin)
    assert _refused(
        "FLX-5003",
        lambda: magnitude_bits(grid=grid, value_range=interval, max_records=10, tolerance=A_FLOAT),
    ).startswith("value: ")


def test_the_correlation_comparison_is_four_times_the_squared_margin_and_the_fourth_powers() -> (
    None
):
    # k = 1, q = 3 and a margin of 5 put 4 * x just under a power of two and
    # 5 * x just over it, so a wrong leading factor changes the width.
    grid = Grid(0)
    interval = Interval.of(0, 3)
    x = 5**2 * 1**4 * 3**4
    assert (4 * x).bit_length() != (5 * x).bit_length()
    bounds = magnitude_bits(
        grid=grid, value_range=interval, max_records=1, tolerance=Fraction(1, 5)
    )
    assert bounds["correlationComparison"] == (4 * x).bit_length()
    assert bounds == _expected_bits(grid, interval, 1, 5)


def test_the_magnitude_bound_uses_the_wider_end_of_the_range_in_encoded_units() -> None:
    grid = Grid(4)
    for lo, hi in ((-5, 2), (-2, 5), (0, 0), (3, 3)):
        interval = Interval.of(lo, hi)
        bounds = magnitude_bits(grid=grid, value_range=interval, max_records=3)
        assert bounds["value"] == (max(abs(lo), abs(hi)) * grid.scale).bit_length()


def test_the_magnitude_bound_names_a_range_end_it_cannot_encode() -> None:
    # Built without Interval.of, so a float can reach the quantiser.
    low = Interval(A_FLOAT, 2)  # type: ignore[arg-type]
    high = Interval(1, A_FLOAT)  # type: ignore[arg-type]
    assert _refused(
        "FLX-5003", lambda: magnitude_bits(grid=GRID, value_range=low, max_records=2)
    ).startswith("range.min: ")
    assert _refused(
        "FLX-5003", lambda: magnitude_bits(grid=GRID, value_range=high, max_records=2)
    ).startswith("range.max: ")


def test_a_single_record_is_a_valid_maximum_and_none_is_not() -> None:
    interval = Interval.of(0, 1)
    assert magnitude_bits(grid=GRID, value_range=interval, max_records=1)["sum"] == (
        GRID.scale.bit_length()
    )
    detail = _refused(
        "FLX-5004", lambda: magnitude_bits(grid=GRID, value_range=interval, max_records=0)
    )
    assert "records.max" in detail


def test_the_field_capacity_check_passes_the_tolerance_through() -> None:
    interval = Interval.of(0, 100)
    tolerance = Fraction(1, 3)
    with_tolerance = check_field_capacity(
        grid=GRID, value_range=interval, max_records=50, tolerance=tolerance
    )
    without = check_field_capacity(grid=GRID, value_range=interval, max_records=50)
    assert dict(with_tolerance.bounds) == magnitude_bits(
        grid=GRID, value_range=interval, max_records=50, tolerance=tolerance
    )
    assert dict(with_tolerance.bounds) != dict(without.bounds)
    assert with_tolerance.capacity_bits == DEFAULT_FIELD_CAPACITY_BITS


def test_the_field_capacity_check_names_the_widest_intermediate_it_cannot_fit() -> None:
    interval = Interval.of(0, 100)
    bounds = magnitude_bits(grid=GRID, value_range=interval, max_records=50)
    name, width = max(sorted(bounds.items()), key=lambda item: item[1])
    detail = _refused(
        "FLX-5001",
        lambda: check_field_capacity(
            grid=GRID, value_range=interval, max_records=50, capacity_bits=width - 1
        ),
    )
    assert name in detail and str(width) in detail and str(width - 1) in detail
    fits = check_field_capacity(
        grid=GRID, value_range=interval, max_records=50, capacity_bits=width
    )
    assert fits.within_capacity


def test_a_capacity_of_one_bit_is_a_capacity_that_nothing_fits_in() -> None:
    interval = Interval.of(0, 1)
    _refused(
        "FLX-5001",
        lambda: check_field_capacity(
            grid=GRID, value_range=interval, max_records=1, capacity_bits=1
        ),
    )
    detail = _refused(
        "FLX-5004",
        lambda: check_field_capacity(
            grid=GRID, value_range=interval, max_records=1, capacity_bits=0
        ),
    )
    assert "0 bits" in detail


# ---------------------------------------------------------------------------
# The sample bounds and the comparisons that read them
# ---------------------------------------------------------------------------


def test_a_sample_is_named_sample_unless_the_caller_says_otherwise() -> None:
    assert _refused("FLX-5005", lambda: require_sample(0)).startswith("sample: 0 records")
    assert _refused("FLX-5005", lambda: require_sample(0, subject="side b")).startswith("side b: ")
    short = _refused("FLX-5005", lambda: require_sample(4, minimum=5))
    assert short.startswith("sample: 4 records") and "5" in short


def test_a_sample_at_the_maximum_is_inside_it_and_one_past_is_not() -> None:
    require_sample(7, maximum=7)
    over = _refused("FLX-5005", lambda: require_sample(8, maximum=7))
    assert "8 records" in over and "7" in over


def test_the_mean_comparisons_name_the_side_whose_sample_is_empty() -> None:
    absolute: Dict[str, Any] = {"tolerance_units": 1}
    relative: Dict[str, Any] = {"tolerance": Fraction(1, 10)}
    for compare, margin in ((mean_within_absolute, absolute), (mean_within_relative, relative)):
        first = _refused(
            "FLX-5005",
            lambda compare=compare, margin=margin: compare(
                sum_a=1, count_a=0, sum_b=1, count_b=1, **margin
            ),
        )
        second = _refused(
            "FLX-5005",
            lambda compare=compare, margin=margin: compare(
                sum_a=1, count_a=1, sum_b=1, count_b=0, **margin
            ),
        )
        assert first.startswith("side a: ") and second.startswith("side b: ")


def test_the_absolute_mean_refuses_a_negative_margin_and_accepts_a_zero_one() -> None:
    detail = _refused(
        "FLX-5004",
        lambda: mean_within_absolute(sum_a=1, count_a=1, sum_b=1, count_b=1, tolerance_units=-3),
    )
    assert "-3" in detail
    assert mean_within_absolute(sum_a=1, count_a=1, sum_b=1, count_b=1, tolerance_units=0)


def test_the_relative_mean_refuses_a_reference_of_zero_and_a_margin_it_cannot_read() -> None:
    _refused(
        "FLX-5005",
        lambda: mean_within_relative(
            sum_a=1, count_a=1, sum_b=0, count_b=1, tolerance=Fraction(1, 10)
        ),
    )
    named = _refused(
        "FLX-5003",
        lambda: mean_within_relative(sum_a=1, count_a=1, sum_b=1, count_b=1, tolerance=A_FLOAT),
    )
    assert named.startswith("tolerance: ")
    negative = _refused(
        "FLX-5004",
        lambda: mean_within_relative(
            sum_a=1, count_a=1, sum_b=1, count_b=1, tolerance=Fraction(-1, 5)
        ),
    )
    assert "-1/5" in negative


def test_total_variation_refuses_cells_that_do_not_line_up_naming_both_lengths() -> None:
    detail = _refused(
        "FLX-5004",
        lambda: total_variation_within(
            counts_a=[1, 1], count_a=2, counts_b=[1, 1, 1], count_b=3, tolerance=Fraction(1, 2)
        ),
    )
    assert "2 and 3" in detail


def test_total_variation_names_the_side_whose_sample_or_cells_are_wrong() -> None:
    half = Fraction(1, 2)
    empty_a = _refused(
        "FLX-5005",
        lambda: total_variation_within(
            counts_a=[0, 0], count_a=0, counts_b=[1, 1], count_b=2, tolerance=half
        ),
    )
    empty_b = _refused(
        "FLX-5005",
        lambda: total_variation_within(
            counts_a=[1, 1], count_a=2, counts_b=[0, 0], count_b=0, tolerance=half
        ),
    )
    assert empty_a.startswith("side a: ") and empty_b.startswith("side b: ")
    short_a = _refused(
        "FLX-5005",
        lambda: total_variation_within(
            counts_a=[1, 0], count_a=2, counts_b=[1, 1], count_b=2, tolerance=half
        ),
    )
    short_b = _refused(
        "FLX-5005",
        lambda: total_variation_within(
            counts_a=[1, 1], count_a=2, counts_b=[1, 0], count_b=2, tolerance=half
        ),
    )
    assert short_a.startswith("side a: ") and short_b.startswith("side b: ")


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def test_a_variance_floor_is_met_exactly_when_the_scaled_variance_equals_it() -> None:
    # Qx = k*Sxx - Sx**2 = 2*4 - 2**2 = 4, and the floor needs k**2 * v = 4 * 1.
    flat = _moments([0, 2], [0, 2])
    assert (flat.qx, flat.qy) == (4, 4)
    flat.require_variance(1)
    detail = _refused("FLX-5005", lambda: flat.require_variance(Fraction(11, 10)))
    assert "column x" in detail


def test_a_variance_floor_names_the_column_that_misses_it() -> None:
    only_y = _moments([0, 4], [0, 2])
    assert (only_y.qx, only_y.qy) == (16, 4)
    assert "column y" in _refused("FLX-5005", lambda: only_y.require_variance(2))
    only_x = _moments([0, 2], [0, 4])
    assert "column x" in _refused("FLX-5005", lambda: only_x.require_variance(2))


def test_a_variance_floor_needs_two_records_and_names_its_subject() -> None:
    one = _moments([5], [5])
    assert _refused("FLX-5005", lambda: one.require_variance(1)).startswith("moments: 1 records")
    named = _refused("FLX-5005", lambda: one.require_variance(1, subject="side a"))
    assert named.startswith("side a: 1 records") and "minimum is 2" in named
    two = _moments([0, 2], [0, 2])
    two.require_variance(1)
    _refused("FLX-5005", lambda: _moments([5, 5, 5], [1, 2, 3]).require_variance(1))
    assert _refused("FLX-5003", lambda: two.require_variance(A_FLOAT)).startswith("minVariance: ")
    assert "-1" in _refused("FLX-5004", lambda: two.require_variance(-1))


def test_correlation_bounds_name_the_bound_they_cannot_read_and_refuse_inverted_ones() -> None:
    assert _refused(
        "FLX-5003",
        lambda: correlation_within_bounds(moments=SPREAD, lower=A_FLOAT, upper=1, min_variance=1),
    ).startswith("lower: ")
    assert _refused(
        "FLX-5003",
        lambda: correlation_within_bounds(moments=SPREAD, lower=0, upper=A_FLOAT, min_variance=1),
    ).startswith("upper: ")
    inverted = _refused(
        "FLX-5004",
        lambda: correlation_within_bounds(
            moments=SPREAD, lower=Fraction(1, 2), upper=Fraction(-1, 2), min_variance=1
        ),
    )
    assert "[1/2, -1/2]" in inverted
    assert correlation_within_bounds(moments=SPREAD, lower=-1, upper=1, min_variance=1)


def test_a_correlation_comparison_names_the_side_below_the_floor() -> None:
    thin = _moments([5], [5])
    first = _refused(
        "FLX-5005",
        lambda: correlation_within(
            moments_a=thin, moments_b=SPREAD, tolerance=Fraction(1, 10), min_variance=1
        ),
    )
    second = _refused(
        "FLX-5005",
        lambda: correlation_within(
            moments_a=SPREAD, moments_b=thin, tolerance=Fraction(1, 10), min_variance=1
        ),
    )
    assert first.startswith("side a: ") and second.startswith("side b: ")


def test_a_correlation_comparison_names_the_margin_it_cannot_read() -> None:
    named = _refused(
        "FLX-5003",
        lambda: correlation_within(
            moments_a=SPREAD, moments_b=SPREAD, tolerance=A_FLOAT, min_variance=1
        ),
    )
    assert named.startswith("tolerance: ")
    negative = _refused(
        "FLX-5004",
        lambda: correlation_within(
            moments_a=SPREAD, moments_b=SPREAD, tolerance=Fraction(-1, 7), min_variance=1
        ),
    )
    assert "-1/7" in negative
    assert correlation_within(moments_a=SPREAD, moments_b=SPREAD, tolerance=0, min_variance=1)


# ---------------------------------------------------------------------------
# Quantiles and support
# ---------------------------------------------------------------------------


def test_an_order_statistic_names_the_input_it_refuses() -> None:
    empty = _refused("FLX-5005", lambda: order_statistic_index(0, Fraction(1, 2)))
    assert empty.startswith("quantile sample: 0 records")
    assert _refused("FLX-5003", lambda: order_statistic_index(5, A_FLOAT)).startswith(
        "quantile level: "
    )
    outside = _refused("FLX-5004", lambda: order_statistic_index(5, Fraction(3, 2)))
    assert "3/2" in outside


def test_a_witness_may_have_every_record_at_or_below_it() -> None:
    assert quantile_rank_ok(count=4, level=1, count_strictly_below=3, count_at_or_below=4) is True
    assert quantile_rank_ok(count=4, level=1, count_strictly_below=4, count_at_or_below=4) is False
    detail = _refused(
        "FLX-5005",
        lambda: quantile_rank_ok(count=4, level=1, count_strictly_below=3, count_at_or_below=5),
    )
    assert "3/5" in detail


def test_a_negative_quantile_margin_is_refused_with_its_value() -> None:
    assert "-2" in _refused(
        "FLX-5004", lambda: quantile_within(witness_a=1, witness_b=1, tolerance_units=-2)
    )
    assert quantile_within(witness_a=1, witness_b=3, tolerance_units=2) is True
    assert quantile_within(witness_a=1, witness_b=4, tolerance_units=2) is False


def test_the_support_count_and_maximum_are_refused_with_their_values() -> None:
    assert "-4" in _refused(
        "FLX-5005", lambda: support_within(non_member_count=-4, max_non_members=1)
    )
    assert "-5" in _refused(
        "FLX-5004", lambda: support_within(non_member_count=0, max_non_members=-5)
    )
    assert support_within(non_member_count=0, max_non_members=0) is True
