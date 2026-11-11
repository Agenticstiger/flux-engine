# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §12 and SEAM-FIDELITY-SPEC §5: the grid and the comparisons.

The load-bearing tests here are the *soundness* properties: a comparison
that passes on the encoded grid, against the tightened margin
``tau' = tau - epsilon``, must imply the claim the declaration actually
made about the true (unencoded) values. Those are the tests that would
catch an epsilon derived optimistically -- a tolerance check that passes
on the grid and is false in the world is exactly the failure the epsilon
exists to rule out.

Every expected number is computed from the declaration in the test, with
``Fraction`` arithmetic, rather than typed as a literal.
"""

from __future__ import annotations

import math
from decimal import Decimal
from fractions import Fraction
from typing import List, Sequence, Tuple

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from flux_engine.core.errors import FluxError
from flux_engine.core.fixedpoint import (
    DEFAULT_FIELD_CAPACITY_BITS,
    DEFAULT_FRACTIONAL_BITS,
    MAX_FRACTIONAL_BITS,
    Bound,
    Grid,
    Interval,
    SecondMoments,
    check_field_capacity,
    correlation_within,
    correlation_within_bounds,
    effective_tolerance,
    epsilon_correlation,
    epsilon_marginal_histogram,
    epsilon_mean_absolute,
    epsilon_mean_relative,
    epsilon_quantile,
    epsilon_support,
    magnitude_bits,
    mean_within_absolute,
    mean_within_relative,
    order_statistic_index,
    quantile_rank_ok,
    quantile_within,
    require_sample,
    support_within,
    to_fraction,
    total_variation_within,
)

GRID = Grid(DEFAULT_FRACTIONAL_BITS)

#: The declaration in SEAM-FIDELITY-SPEC §2's worked fidelity block: the
#: ``amount`` column of the telco expose, its declared range, its record
#: bounds, its histogram edges and its relative tolerance. Used as the
#: shape a real declaration has -- no count from the example bundle is
#: typed anywhere in this file.
AMOUNT_RANGE = Interval.of(Decimal("0"), Decimal("10000"))
AMOUNT_BINS = [Decimal(edge) for edge in ("0", "50", "100", "250", "1000", "10000")]
RECORDS_MIN = 1000
RECORDS_MAX = 100000
RELATIVE_TOLERANCE = Decimal("0.02")


# --- the quantiser ------------------------------------------------------


@pytest.mark.unit
def test_the_default_grid_is_the_one_the_spec_declares() -> None:
    assert DEFAULT_FRACTIONAL_BITS == 16
    assert GRID.scale == 2**16
    assert GRID.resolution == Fraction(1, 2**16)


@pytest.mark.unit
@pytest.mark.parametrize("bits", [-1, MAX_FRACTIONAL_BITS + 1])
def test_an_impossible_grid_is_refused(bits: int) -> None:
    with pytest.raises(FluxError) as excinfo:
        Grid(bits)
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_the_grid_takes_no_float() -> None:
    with pytest.raises(FluxError) as excinfo:
        GRID.quantise(1.5)  # type: ignore[arg-type]
    assert excinfo.value.code.code == "FLX-5003"


@pytest.mark.unit
def test_a_non_finite_decimal_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        to_fraction(Decimal("NaN"))
    assert excinfo.value.code.code == "FLX-5003"


@pytest.mark.unit
def test_quantising_rounds_toward_negative_infinity() -> None:
    """Not toward zero: a negative value rounds down, like every other."""
    grid = Grid(0)
    assert grid.quantise(Decimal("1.5")) == 1
    assert grid.quantise(Decimal("-1.5")) == -2
    assert grid.quantise_ceil(Decimal("-1.5")) == -1


_GRID_VALUES = st.decimals(
    min_value=Decimal("-1000"), max_value=Decimal("1000"), places=6, allow_nan=False
)


@pytest.mark.property
@given(value=_GRID_VALUES)
def test_the_encoded_value_brackets_the_true_value(value: Decimal) -> None:
    """``q/2**b <= v < (q+1)/2**b`` -- the one-sided error the epsilons use."""
    units = GRID.quantise(value)
    exact = to_fraction(value)
    assert GRID.dequantise(units) <= exact < GRID.dequantise(units + 1)


@pytest.mark.property
@given(left=_GRID_VALUES, right=_GRID_VALUES)
def test_quantising_is_monotone(left: Decimal, right: Decimal) -> None:
    """Why a quantile keeps its index: encoding never reorders records."""
    if to_fraction(left) <= to_fraction(right):
        assert GRID.quantise(left) <= GRID.quantise(right)


@pytest.mark.property
@given(value=_GRID_VALUES)
def test_tightening_never_loosens_a_bound(value: Decimal) -> None:
    exact = to_fraction(value)
    upper = GRID.dequantise(GRID.quantise(value))
    lower = GRID.dequantise(GRID.quantise_ceil(value))
    assert upper <= exact <= lower
    assert GRID.is_on_grid(upper) and GRID.is_on_grid(lower)


@pytest.mark.unit
def test_tighten_onto_grid_picks_the_direction_from_the_bound() -> None:
    from flux_engine.core.fixedpoint import tighten_onto_grid

    value = Fraction(1, 3)
    assert tighten_onto_grid(GRID, value, Bound.UPPER) < value
    assert tighten_onto_grid(GRID, value, Bound.LOWER) > value


# --- intervals ----------------------------------------------------------


@pytest.mark.unit
def test_an_inverted_interval_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        Interval.of(Decimal("10"), Decimal("0"))
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_interval_arithmetic_is_the_standard_form() -> None:
    a = Interval.of(1, 2)
    b = Interval.of(-3, 4)
    assert (a + b) == Interval(Fraction(-2), Fraction(6))
    assert (a - b) == Interval(Fraction(-3), Fraction(5))
    assert (a * b) == Interval(Fraction(-6), Fraction(8))
    assert (-b) == Interval(Fraction(-4), Fraction(3))


@pytest.mark.unit
def test_min_magnitude_is_zero_exactly_when_the_interval_spans_zero() -> None:
    assert AMOUNT_RANGE.min_magnitude == 0
    assert AMOUNT_RANGE.contains_zero
    positive = Interval.of(Decimal("1"), Decimal("10000"))
    assert positive.min_magnitude == 1
    assert Interval.of(-10, -2).min_magnitude == 2


# --- epsilon ------------------------------------------------------------


@pytest.mark.unit
def test_epsilon_for_an_absolute_mean_is_one_grid_step() -> None:
    assert epsilon_mean_absolute(GRID) == GRID.resolution
    assert epsilon_quantile(GRID) == GRID.resolution


@pytest.mark.unit
def test_epsilon_for_support_is_zero_because_nothing_is_encoded() -> None:
    assert epsilon_support() == 0


@pytest.mark.unit
def test_epsilon_for_a_relative_mean_uses_the_declared_reference_magnitude() -> None:
    value_range = Interval.of(Decimal("1"), Decimal("10000"))
    tau = to_fraction(RELATIVE_TOLERANCE)
    expected = GRID.resolution * (1 + tau) / value_range.min_magnitude
    assert epsilon_mean_relative(GRID, value_range, RELATIVE_TOLERANCE) == expected


@pytest.mark.unit
def test_a_relative_tolerance_over_a_range_spanning_zero_fails_closed() -> None:
    with pytest.raises(FluxError) as excinfo:
        epsilon_mean_relative(GRID, AMOUNT_RANGE, RELATIVE_TOLERANCE)
    assert excinfo.value.code.code == "FLX-5002"


@pytest.mark.unit
def test_on_grid_bin_edges_make_binning_exact() -> None:
    assert epsilon_marginal_histogram(GRID, AMOUNT_BINS) == 0


@pytest.mark.unit
def test_an_off_grid_bin_edge_is_refused() -> None:
    edges = [Decimal("0"), Decimal("0.1"), Decimal("1")]
    assert not GRID.is_on_grid(Decimal("0.1"))
    with pytest.raises(FluxError) as excinfo:
        epsilon_marginal_histogram(GRID, edges)
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
@pytest.mark.parametrize(
    "edges",
    [[Decimal("0")], [Decimal("0"), Decimal("0")], [Decimal("1"), Decimal("0")]],
)
def test_bin_edges_must_be_at_least_two_and_strictly_increasing(
    edges: List[Decimal],
) -> None:
    with pytest.raises(FluxError) as excinfo:
        epsilon_marginal_histogram(GRID, edges)
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_epsilon_for_a_correlation_scales_with_the_variance_floor() -> None:
    """``8 * delta / sqrt(v)``, exact when ``sqrt(v)`` is representable."""
    assert epsilon_correlation(GRID, Fraction(1, 4)) == 8 * GRID.resolution / Fraction(1, 2)
    tighter = epsilon_correlation(GRID, Fraction(1, 100))
    looser = epsilon_correlation(GRID, Fraction(1, 4))
    assert tighter > looser


@pytest.mark.unit
def test_a_correlation_without_a_variance_floor_cannot_be_bounded() -> None:
    with pytest.raises(FluxError) as excinfo:
        epsilon_correlation(GRID, Fraction(0))
    assert excinfo.value.code.code == "FLX-5004"


# --- the effective margin -----------------------------------------------


@pytest.mark.unit
def test_the_effective_margin_is_tau_minus_epsilon_on_the_grid() -> None:
    tau = to_fraction(RELATIVE_TOLERANCE)
    epsilon = epsilon_mean_absolute(GRID)
    margin = effective_tolerance(GRID, RELATIVE_TOLERANCE, epsilon)
    assert margin == GRID.dequantise(GRID.quantise(tau - epsilon))
    assert margin <= tau - epsilon
    assert GRID.is_on_grid(margin)


@pytest.mark.unit
def test_epsilon_at_or_above_the_tolerance_fails_closed() -> None:
    epsilon = epsilon_mean_absolute(GRID)
    for tolerance in (epsilon, epsilon / 2):
        with pytest.raises(FluxError) as excinfo:
            effective_tolerance(GRID, tolerance, epsilon)
        assert excinfo.value.code.code == "FLX-5002"


@pytest.mark.unit
def test_a_margin_that_rounds_to_zero_fails_closed() -> None:
    coarse = Grid(2)
    with pytest.raises(FluxError) as excinfo:
        effective_tolerance(coarse, Decimal("0.2"), Fraction(0))
    assert excinfo.value.code.code == "FLX-5002"


# --- mean: the soundness properties -------------------------------------

_SAMPLE = st.lists(
    st.decimals(min_value=Decimal("1"), max_value=Decimal("10000"), places=4),
    min_size=1,
    max_size=25,
)


def _encode(values: Sequence[Decimal]) -> Tuple[int, int]:
    return sum(GRID.quantise(value) for value in values), len(values)


def _true_mean(values: Sequence[Decimal]) -> Fraction:
    return sum((to_fraction(value) for value in values), Fraction(0)) / len(values)


@pytest.mark.property
@settings(max_examples=200)
@given(left=_SAMPLE, right=_SAMPLE)
def test_an_absolute_mean_check_that_passes_is_true_of_the_real_values(
    left: List[Decimal], right: List[Decimal]
) -> None:
    """The reason epsilon exists, stated as a property.

    If the encoded comparison passes against ``tau'``, the declared
    absolute claim holds for the unencoded means.
    """
    tau = to_fraction(RELATIVE_TOLERANCE)
    margin = effective_tolerance(GRID, tau, epsilon_mean_absolute(GRID))
    sum_a, count_a = _encode(left)
    sum_b, count_b = _encode(right)
    passed = mean_within_absolute(
        sum_a=sum_a,
        count_a=count_a,
        sum_b=sum_b,
        count_b=count_b,
        tolerance_units=GRID.quantise(margin),
    )
    if passed:
        assert abs(_true_mean(left) - _true_mean(right)) <= tau


@pytest.mark.property
@settings(max_examples=200)
@given(left=_SAMPLE, right=_SAMPLE)
def test_a_relative_mean_check_that_passes_is_true_of_the_real_values(
    left: List[Decimal], right: List[Decimal]
) -> None:
    value_range = Interval.of(Decimal("1"), Decimal("10000"))
    tau = to_fraction(RELATIVE_TOLERANCE)
    epsilon = epsilon_mean_relative(GRID, value_range, tau)
    margin = effective_tolerance(GRID, tau, epsilon)
    sum_a, count_a = _encode(left)
    sum_b, count_b = _encode(right)
    passed = mean_within_relative(
        sum_a=sum_a, count_a=count_a, sum_b=sum_b, count_b=count_b, tolerance=margin
    )
    if passed:
        mean_a, mean_b = _true_mean(left), _true_mean(right)
        assert abs(mean_a - mean_b) <= tau * abs(mean_b)


@pytest.mark.property
@given(values=_SAMPLE)
def test_a_sample_against_itself_always_passes(values: List[Decimal]) -> None:
    """The negative control: the soundness properties are not vacuous."""
    margin = effective_tolerance(GRID, RELATIVE_TOLERANCE, epsilon_mean_absolute(GRID))
    total, count = _encode(values)
    assert mean_within_absolute(
        sum_a=total,
        count_a=count,
        sum_b=total,
        count_b=count,
        tolerance_units=GRID.quantise(margin),
    )


@pytest.mark.unit
def test_a_mean_far_outside_the_margin_does_not_pass() -> None:
    margin = effective_tolerance(GRID, RELATIVE_TOLERANCE, epsilon_mean_absolute(GRID))
    left = [Decimal("100.0000")] * 4
    right = [Decimal("101.0000")] * 4
    sum_a, count_a = _encode(left)
    sum_b, count_b = _encode(right)
    assert not mean_within_absolute(
        sum_a=sum_a,
        count_a=count_a,
        sum_b=sum_b,
        count_b=count_b,
        tolerance_units=GRID.quantise(margin),
    )


@pytest.mark.unit
def test_the_worst_case_the_epsilon_exists_for_is_rejected() -> None:
    """A hand-built adversarial pair, at exactly the boundary epsilon covers.

    Side A carries the largest encoding error a single value can (it sits
    just under a grid point, so ``floor`` drops almost a whole step) and
    side B carries none (it is a grid point). Their true means differ by
    more than the declared tolerance, but only by half a grid step -- the
    case a comparison against an un-tightened ``tau`` would wave through.
    With ``tau' = tau - epsilon`` it is rejected.
    """
    tau = Fraction(1, 100)
    delta = GRID.resolution
    value_a = tau + delta / 2
    value_b = Fraction(0)
    assert abs(value_a - value_b) > tau

    margin = effective_tolerance(GRID, tau, epsilon_mean_absolute(GRID))
    assert not mean_within_absolute(
        sum_a=GRID.quantise(value_a),
        count_a=1,
        sum_b=GRID.quantise(value_b),
        count_b=1,
        tolerance_units=GRID.quantise(margin),
    )
    # ...and the same pair does pass against the un-tightened tolerance,
    # which is precisely why the tightening is not decorative.
    assert mean_within_absolute(
        sum_a=GRID.quantise(value_a),
        count_a=1,
        sum_b=GRID.quantise(value_b),
        count_b=1,
        tolerance_units=GRID.quantise(tau),
    )


@pytest.mark.unit
def test_a_relative_comparison_against_a_zero_reference_fails_closed() -> None:
    with pytest.raises(FluxError) as excinfo:
        mean_within_relative(sum_a=1, count_a=1, sum_b=0, count_b=1, tolerance=Fraction(1, 10))
    assert excinfo.value.code.code == "FLX-5005"


@pytest.mark.unit
def test_an_empty_sample_fails_closed() -> None:
    with pytest.raises(FluxError) as excinfo:
        mean_within_absolute(sum_a=0, count_a=0, sum_b=1, count_b=1, tolerance_units=1)
    assert excinfo.value.code.code == "FLX-5005"


@pytest.mark.unit
def test_require_sample_enforces_the_declared_record_bounds() -> None:
    require_sample(RECORDS_MIN, minimum=RECORDS_MIN, maximum=RECORDS_MAX)
    for count in (RECORDS_MIN - 1, RECORDS_MAX + 1, 0):
        with pytest.raises(FluxError) as excinfo:
            require_sample(count, minimum=RECORDS_MIN, maximum=RECORDS_MAX)
        assert excinfo.value.code.code == "FLX-5005"


# --- total variation ----------------------------------------------------


def _bin_counts(values: Sequence[Decimal], edges: Sequence[Decimal]) -> List[int]:
    """Count *values* into the half-open bins between *edges*.

    Computed from the data, never typed: the expected counts in the tests
    below are whatever this function returns for the sample constructed
    there.
    """
    counts = [0] * (len(edges) - 1)
    for value in values:
        exact = to_fraction(value)
        for index in range(len(edges) - 1):
            low, high = to_fraction(edges[index]), to_fraction(edges[index + 1])
            last = index == len(edges) - 2
            if low <= exact < high or (last and exact == high):
                counts[index] += 1
                break
    return counts


@pytest.mark.unit
def test_identical_histograms_agree_at_any_margin() -> None:
    values = [Decimal(str(v)) for v in (10, 60, 120, 300, 5000, 9999)]
    counts = _bin_counts(values, AMOUNT_BINS)
    assert sum(counts) == len(values)
    assert total_variation_within(
        counts_a=counts,
        count_a=len(values),
        counts_b=counts,
        count_b=len(values),
        tolerance=Fraction(0),
    )


@pytest.mark.unit
def test_total_variation_matches_the_arithmetic_definition() -> None:
    left = [Decimal(str(v)) for v in (10, 20, 60, 120)]
    right = [Decimal(str(v)) for v in (10, 60, 120, 300)]
    counts_a = _bin_counts(left, AMOUNT_BINS)
    counts_b = _bin_counts(right, AMOUNT_BINS)
    distance = (
        sum(
            abs(Fraction(a, len(left)) - Fraction(b, len(right)))
            for a, b in zip(counts_a, counts_b, strict=True)
        )
        / 2
    )
    for margin in (distance, distance + Fraction(1, 100), distance - Fraction(1, 100)):
        expected = distance <= margin
        assert (
            total_variation_within(
                counts_a=counts_a,
                count_a=len(left),
                counts_b=counts_b,
                count_b=len(right),
                tolerance=margin,
            )
            is expected
        )


@pytest.mark.unit
def test_unbinned_records_fail_closed() -> None:
    with pytest.raises(FluxError) as excinfo:
        total_variation_within(
            counts_a=[1, 1], count_a=3, counts_b=[1, 1], count_b=2, tolerance=Fraction(1)
        )
    assert excinfo.value.code.code == "FLX-5005"


@pytest.mark.unit
def test_misaligned_bins_are_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        total_variation_within(
            counts_a=[1, 1], count_a=2, counts_b=[2], count_b=2, tolerance=Fraction(1)
        )
    assert excinfo.value.code.code == "FLX-5004"


# --- correlation --------------------------------------------------------


def _moments(xs: Sequence[int], ys: Sequence[int]) -> SecondMoments:
    return SecondMoments(
        count=len(xs),
        sum_x=sum(xs),
        sum_y=sum(ys),
        sum_xx=sum(x * x for x in xs),
        sum_yy=sum(y * y for y in ys),
        sum_xy=sum(x * y for x, y in zip(xs, ys, strict=True)),
    )


def _r_bracket(moments: SecondMoments) -> Tuple[Fraction, Fraction]:
    """An exact rational bracket ``lo <= r <= hi`` for the correlation.

    Computed with :func:`math.isqrt` on the radicand, entirely
    independently of the module under test -- so agreement between the
    two is evidence, not a tautology.
    """
    radicand = moments.qx * moments.qy
    root = math.isqrt(radicand)
    numerator = moments.numerator
    if numerator >= 0:
        return Fraction(numerator, root + 1), Fraction(numerator, root) if root else Fraction(1)
    return Fraction(numerator, root), Fraction(numerator, root + 1)


@pytest.mark.unit
def test_an_exactly_linear_pair_has_correlation_one() -> None:
    xs = list(range(1, 11))
    ys = [2 * x + 3 for x in xs]
    moments = _moments(xs, ys)
    assert moments.numerator > 0
    # y = 2x + 3 gives Qy = 4*Qx exactly, so sqrt(Qx*Qy) = 2*Qx = N.
    assert moments.qy == 4 * moments.qx
    assert moments.numerator == 2 * moments.qx
    assert correlation_within_bounds(
        moments=moments, lower=Fraction(1), upper=Fraction(1), min_variance=Fraction(1)
    )
    assert not correlation_within_bounds(
        moments=moments,
        lower=Fraction(0),
        upper=Fraction(9, 10),
        min_variance=Fraction(1),
    )


@pytest.mark.unit
def test_an_exactly_anti_linear_pair_has_correlation_minus_one() -> None:
    xs = list(range(1, 11))
    ys = [-3 * x + 7 for x in xs]
    moments = _moments(xs, ys)
    assert correlation_within_bounds(
        moments=moments, lower=Fraction(-1), upper=Fraction(-1), min_variance=Fraction(1)
    )
    assert not correlation_within_bounds(
        moments=moments,
        lower=Fraction(-9, 10),
        upper=Fraction(1),
        min_variance=Fraction(1),
    )


@pytest.mark.property
@given(
    left=st.integers(min_value=-40, max_value=40),
    numerator=st.integers(min_value=-9, max_value=9),
    denominator=st.integers(min_value=1, max_value=7),
    radicand=st.integers(min_value=0, max_value=400),
)
def test_the_root_free_primitive_matches_an_exact_reference(
    left: int, numerator: int, denominator: int, radicand: int
) -> None:
    """The four sign branches of the private helper, against exact arithmetic.

    ``Fraction`` cannot hold a square root, so the reference is squared by
    hand only where squaring is valid; the degenerate ``radicand == 0``
    case is included because the variance floor makes it unreachable
    through the public API and a defensive branch nothing exercises is a
    branch nothing checks.
    """
    from flux_engine.core.fixedpoint import _at_least_root, _at_most_root

    coefficient = Fraction(numerator, denominator)
    right_is_zero = radicand == 0 or coefficient == 0
    if right_is_zero:
        expected_at_most = left <= 0
        expected_at_least = left >= 0
    else:
        squared_left = Fraction(left * left)
        squared_right = coefficient * coefficient * radicand
        if coefficient > 0:
            expected_at_most = left <= 0 or squared_left <= squared_right
        else:
            expected_at_most = left < 0 and squared_left >= squared_right
        if -coefficient > 0:
            expected_at_least = -left <= 0 or squared_left <= squared_right
        else:
            expected_at_least = -left < 0 and squared_left >= squared_right
    assert _at_most_root(left, coefficient, radicand) is expected_at_most
    assert _at_least_root(left, coefficient, radicand) is expected_at_least


@pytest.mark.unit
def test_an_uncorrelated_pair_has_correlation_exactly_zero() -> None:
    """The sign-split branches, exercised where the numerator is zero.

    ``r = 0`` is the case a bound of the opposite sign has to get right:
    the comparison ``N <= U * sqrt(R)`` with ``N = 0`` and ``U >= 0`` is
    true without any squaring, and the mirror case on the lower side is
    true for the same reason. Both are decided by a branch that never
    fires for a strictly-signed correlation.
    """
    xs = [0, 1, 0, 1]
    ys = [0, 0, 1, 1]
    moments = _moments(xs, ys)
    assert moments.numerator == 0
    assert moments.qx > 0 and moments.qy > 0
    floor = Fraction(1, 100)
    assert correlation_within_bounds(
        moments=moments, lower=Fraction(0), upper=Fraction(1), min_variance=floor
    )
    assert correlation_within_bounds(
        moments=moments, lower=Fraction(-1), upper=Fraction(0), min_variance=floor
    )
    assert not correlation_within_bounds(
        moments=moments, lower=Fraction(1, 10), upper=Fraction(1), min_variance=floor
    )
    assert not correlation_within_bounds(
        moments=moments, lower=Fraction(-1), upper=Fraction(-1, 10), min_variance=floor
    )


@pytest.mark.unit
def test_a_bound_at_zero_admits_a_correlation_of_the_opposite_sign() -> None:
    """``r = -1`` against an upper bound of 0, and ``r = 1`` against a lower one."""
    xs = list(range(1, 11))
    negative = _moments(xs, [-3 * x + 7 for x in xs])
    positive = _moments(xs, [2 * x + 3 for x in xs])
    floor = Fraction(1, 100)
    assert correlation_within_bounds(
        moments=negative, lower=Fraction(-1), upper=Fraction(0), min_variance=floor
    )
    assert correlation_within_bounds(
        moments=positive, lower=Fraction(0), upper=Fraction(1), min_variance=floor
    )
    assert not correlation_within_bounds(
        moments=negative, lower=Fraction(0), upper=Fraction(1), min_variance=floor
    )
    assert not correlation_within_bounds(
        moments=positive, lower=Fraction(-1), upper=Fraction(0), min_variance=floor
    )


_INT_COLUMN = st.lists(st.integers(min_value=-50, max_value=50), min_size=4, max_size=12)


@pytest.mark.property
@settings(max_examples=150)
@given(data=st.data())
def test_the_root_free_comparison_agrees_with_an_independent_bracket(
    data: st.DataObject,
) -> None:
    xs = data.draw(_INT_COLUMN)
    ys = data.draw(
        st.lists(st.integers(min_value=-50, max_value=50), min_size=len(xs), max_size=len(xs))
    )
    moments = _moments(xs, ys)
    assume(moments.qx > 0 and moments.qy > 0)
    low, high = _r_bracket(moments)
    gap = Fraction(1, 1000)
    # A bracket that certainly contains r admits it...
    assert correlation_within_bounds(
        moments=moments,
        lower=low - gap,
        upper=high + gap,
        min_variance=Fraction(1, 10**9),
    )
    # ...and one that certainly lies above or below it does not.
    assert not correlation_within_bounds(
        moments=moments,
        lower=high + gap,
        upper=high + 2 * gap,
        min_variance=Fraction(1, 10**9),
    )
    assert not correlation_within_bounds(
        moments=moments,
        lower=low - 2 * gap,
        upper=low - gap,
        min_variance=Fraction(1, 10**9),
    )


@pytest.mark.unit
def test_two_identical_correlations_agree_at_zero_margin() -> None:
    xs = list(range(1, 9))
    ys = [x * x for x in xs]
    moments = _moments(xs, ys)
    assert correlation_within(
        moments_a=moments,
        moments_b=moments,
        tolerance=Fraction(0),
        min_variance=Fraction(1),
    )


@pytest.mark.property
@settings(max_examples=100)
@given(data=st.data())
def test_comparing_two_correlations_agrees_with_the_bracketed_difference(
    data: st.DataObject,
) -> None:
    xs = data.draw(_INT_COLUMN)
    ys = data.draw(
        st.lists(st.integers(min_value=-50, max_value=50), min_size=len(xs), max_size=len(xs))
    )
    zs = data.draw(
        st.lists(st.integers(min_value=-50, max_value=50), min_size=len(xs), max_size=len(xs))
    )
    left = _moments(xs, ys)
    right = _moments(xs, zs)
    assume(left.qx > 0 and left.qy > 0 and right.qx > 0 and right.qy > 0)
    left_low, left_high = _r_bracket(left)
    right_low, right_high = _r_bracket(right)
    difference_low = max(Fraction(0), max(left_low - right_high, right_low - left_high))
    difference_high = max(abs(left_high - right_low), abs(right_high - left_low))
    gap = Fraction(1, 1000)
    floor_variance = Fraction(1, 10**9)
    assert correlation_within(
        moments_a=left,
        moments_b=right,
        tolerance=difference_high + gap,
        min_variance=floor_variance,
    )
    if difference_low > gap:
        assert not correlation_within(
            moments_a=left,
            moments_b=right,
            tolerance=difference_low - gap,
            min_variance=floor_variance,
        )


@pytest.mark.unit
def test_a_column_below_the_variance_floor_fails_closed() -> None:
    xs = [1, 1, 1, 1]
    ys = [1, 2, 3, 4]
    moments = _moments(xs, ys)
    assert moments.qx == 0
    with pytest.raises(FluxError) as excinfo:
        correlation_within_bounds(
            moments=moments, lower=Fraction(-1), upper=Fraction(1), min_variance=Fraction(1)
        )
    assert excinfo.value.code.code == "FLX-5005"


@pytest.mark.unit
def test_a_correlation_needs_at_least_two_records() -> None:
    with pytest.raises(FluxError) as excinfo:
        _moments([1], [1]).require_variance(Fraction(1, 100))
    assert excinfo.value.code.code == "FLX-5005"


@pytest.mark.unit
def test_inverted_correlation_bounds_are_refused() -> None:
    moments = _moments([1, 2, 3], [3, 2, 1])
    with pytest.raises(FluxError) as excinfo:
        correlation_within_bounds(
            moments=moments,
            lower=Fraction(1),
            upper=Fraction(-1),
            min_variance=Fraction(1, 100),
        )
    assert excinfo.value.code.code == "FLX-5004"


# --- quantile and support ----------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("count", "level", "expected"),
    [
        (10, Fraction(1, 2), 5),
        (10, Fraction(9, 10), 9),
        (10, Fraction(0), 1),
        (10, Fraction(1), 10),
        (7, Fraction(1, 3), 3),
    ],
)
def test_the_order_statistic_index_is_ceil_of_level_times_count(
    count: int, level: Fraction, expected: int
) -> None:
    assert order_statistic_index(count, level) == expected


@pytest.mark.unit
def test_a_level_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        order_statistic_index(10, Fraction(3, 2))
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_a_witness_is_the_quantile_only_when_its_ranks_say_so() -> None:
    # A 10-record sample at the median: index 5.
    assert order_statistic_index(10, Fraction(1, 2)) == 5
    assert quantile_rank_ok(
        count=10, level=Fraction(1, 2), count_strictly_below=4, count_at_or_below=5
    )
    assert quantile_rank_ok(
        count=10, level=Fraction(1, 2), count_strictly_below=2, count_at_or_below=7
    )
    assert not quantile_rank_ok(
        count=10, level=Fraction(1, 2), count_strictly_below=5, count_at_or_below=6
    )
    assert not quantile_rank_ok(
        count=10, level=Fraction(1, 2), count_strictly_below=0, count_at_or_below=4
    )


@pytest.mark.unit
def test_impossible_witness_ranks_fail_closed() -> None:
    with pytest.raises(FluxError) as excinfo:
        quantile_rank_ok(
            count=10, level=Fraction(1, 2), count_strictly_below=7, count_at_or_below=5
        )
    assert excinfo.value.code.code == "FLX-5005"


@pytest.mark.unit
def test_two_quantile_witnesses_are_compared_in_grid_units() -> None:
    margin = effective_tolerance(GRID, Decimal("0.5"), epsilon_quantile(GRID))
    units = GRID.quantise(margin)
    witness = GRID.quantise(Decimal("100.0000"))
    near = GRID.quantise(Decimal("100.2500"))
    far = GRID.quantise(Decimal("100.7500"))
    assert quantile_within(witness_a=witness, witness_b=near, tolerance_units=units)
    assert not quantile_within(witness_a=witness, witness_b=far, tolerance_units=units)


@pytest.mark.unit
def test_a_negative_quantile_margin_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        quantile_within(witness_a=0, witness_b=0, tolerance_units=-1)
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_support_counts_non_members_against_the_declared_maximum() -> None:
    assert support_within(non_member_count=0, max_non_members=0)
    assert not support_within(non_member_count=1, max_non_members=0)
    assert support_within(non_member_count=2, max_non_members=2)


@pytest.mark.unit
def test_a_negative_non_member_count_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        support_within(non_member_count=-1, max_non_members=0)
    assert excinfo.value.code.code == "FLX-5005"


# --- field capacity -----------------------------------------------------


@pytest.mark.unit
def test_the_declared_shape_fits_the_default_field() -> None:
    """The SEAM-FIDELITY-SPEC §2 declaration, bounded at compile time."""
    margin = effective_tolerance(GRID, RELATIVE_TOLERANCE, epsilon_mean_absolute(GRID))
    report = check_field_capacity(
        grid=GRID,
        value_range=AMOUNT_RANGE,
        max_records=RECORDS_MAX,
        tolerance=margin,
    )
    assert report.within_capacity
    assert report.capacity_bits == DEFAULT_FIELD_CAPACITY_BITS
    name, width = report.widest
    assert name == "correlationComparison"
    assert width <= DEFAULT_FIELD_CAPACITY_BITS


@pytest.mark.unit
def test_a_declaration_too_wide_for_the_field_is_refused() -> None:
    margin = effective_tolerance(GRID, RELATIVE_TOLERANCE, epsilon_mean_absolute(GRID))
    bounds = magnitude_bits(
        grid=GRID, value_range=AMOUNT_RANGE, max_records=10**9, tolerance=margin
    )
    assert max(bounds.values()) > DEFAULT_FIELD_CAPACITY_BITS
    with pytest.raises(FluxError) as excinfo:
        check_field_capacity(
            grid=GRID, value_range=AMOUNT_RANGE, max_records=10**9, tolerance=margin
        )
    assert excinfo.value.code.code == "FLX-5001"


@pytest.mark.unit
def test_the_magnitude_bound_grows_with_every_declared_dimension() -> None:
    margin = effective_tolerance(GRID, RELATIVE_TOLERANCE, epsilon_mean_absolute(GRID))
    baseline = magnitude_bits(
        grid=GRID, value_range=AMOUNT_RANGE, max_records=RECORDS_MAX, tolerance=margin
    )
    wider_range = magnitude_bits(
        grid=GRID,
        value_range=Interval.of(Decimal("0"), Decimal("1000000")),
        max_records=RECORDS_MAX,
        tolerance=margin,
    )
    finer_grid = magnitude_bits(
        grid=Grid(32), value_range=AMOUNT_RANGE, max_records=RECORDS_MAX, tolerance=margin
    )
    more_records = magnitude_bits(
        grid=GRID, value_range=AMOUNT_RANGE, max_records=RECORDS_MAX * 10, tolerance=margin
    )
    for widened in (wider_range, finer_grid, more_records):
        assert widened["correlationComparison"] > baseline["correlationComparison"]


@pytest.mark.unit
def test_a_zero_record_declaration_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        magnitude_bits(grid=GRID, value_range=AMOUNT_RANGE, max_records=0)
    assert excinfo.value.code.code == "FLX-5004"


# --- guards -------------------------------------------------------------


@pytest.mark.unit
def test_a_bool_is_not_a_number_on_the_grid() -> None:
    with pytest.raises(FluxError) as excinfo:
        to_fraction(True)  # type: ignore[arg-type]
    assert excinfo.value.code.code == "FLX-5003"


@pytest.mark.unit
def test_fractional_bits_must_be_an_integer() -> None:
    for bits in (True, Decimal("16")):
        with pytest.raises(FluxError) as excinfo:
            Grid(bits)  # type: ignore[arg-type]
        assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_interval_width_magnitude_and_membership() -> None:
    interval = Interval.of(Decimal("-2"), Decimal("5"))
    assert interval.width == 7
    assert interval.magnitude == 5
    assert interval.contains(Decimal("0")) and not interval.contains(Decimal("6"))


@pytest.mark.unit
@pytest.mark.parametrize("tolerance", [Fraction(0), Fraction(-1, 10)])
def test_a_non_positive_tolerance_is_refused(tolerance: Fraction) -> None:
    with pytest.raises(FluxError) as excinfo:
        effective_tolerance(GRID, tolerance, Fraction(0))
    assert excinfo.value.code.code == "FLX-5002"
    with pytest.raises(FluxError) as second:
        epsilon_mean_relative(GRID, Interval.of(1, 2), tolerance)
    assert second.value.code.code == "FLX-5002"


@pytest.mark.unit
def test_a_negative_epsilon_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        effective_tolerance(GRID, Decimal("0.1"), Fraction(-1, 100))
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_a_variance_floor_that_cannot_be_bounded_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        epsilon_correlation(GRID, Fraction(1, 10**60))
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_a_non_positive_field_capacity_is_refused() -> None:
    with pytest.raises(FluxError) as excinfo:
        check_field_capacity(grid=GRID, value_range=AMOUNT_RANGE, max_records=10, capacity_bits=0)
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_negative_margins_are_refused_everywhere() -> None:
    with pytest.raises(FluxError) as excinfo:
        mean_within_absolute(sum_a=0, count_a=1, sum_b=0, count_b=1, tolerance_units=-1)
    assert excinfo.value.code.code == "FLX-5004"
    with pytest.raises(FluxError) as relative:
        mean_within_relative(sum_a=0, count_a=1, sum_b=1, count_b=1, tolerance=Fraction(-1, 10))
    assert relative.value.code.code == "FLX-5004"
    with pytest.raises(FluxError) as support:
        support_within(non_member_count=0, max_non_members=-1)
    assert support.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_unbinned_records_on_the_reference_side_also_fail_closed() -> None:
    with pytest.raises(FluxError) as excinfo:
        total_variation_within(
            counts_a=[1, 1], count_a=2, counts_b=[1, 1], count_b=3, tolerance=Fraction(1)
        )
    assert excinfo.value.code.code == "FLX-5005"


@pytest.mark.unit
def test_a_non_positive_variance_floor_is_refused() -> None:
    moments = _moments([1, 2, 3], [3, 2, 1])
    with pytest.raises(FluxError) as excinfo:
        moments.require_variance(Fraction(0))
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
def test_a_negative_correlation_margin_is_refused() -> None:
    moments = _moments([1, 2, 3], [3, 2, 1])
    with pytest.raises(FluxError) as excinfo:
        correlation_within(
            moments_a=moments,
            moments_b=moments,
            tolerance=Fraction(-1, 10),
            min_variance=Fraction(1, 100),
        )
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.unit
@pytest.mark.parametrize("value", [Fraction(0), Fraction(-1, 4)])
def test_the_square_root_bound_refuses_a_non_positive_radicand(value: Fraction) -> None:
    """A defensive branch the public API's variance floor keeps unreachable."""
    from flux_engine.core.fixedpoint import _sqrt_lower_bound

    with pytest.raises(FluxError) as excinfo:
        _sqrt_lower_bound(value)
    assert excinfo.value.code.code == "FLX-5004"


@pytest.mark.property
@given(
    numerator=st.integers(min_value=1, max_value=10**6),
    denominator=st.integers(min_value=1, max_value=10**6),
)
def test_the_square_root_bound_is_a_lower_bound(numerator: int, denominator: int) -> None:
    """``0 < r <= sqrt(v)``, checked by squaring -- no float involved."""
    from flux_engine.core.fixedpoint import _sqrt_lower_bound

    value = Fraction(numerator, denominator)
    root = _sqrt_lower_bound(value)
    assert root > 0
    assert root * root <= value
