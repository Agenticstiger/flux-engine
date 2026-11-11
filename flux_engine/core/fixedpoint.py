# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The fixed-point grid: quantiser, tolerance tightening, epsilon, comparisons.

Implements KERNEL-SPEC §12 and the integer comparison forms of
SEAM-FIDELITY-SPEC §5. Every statistic the fidelity check compares is
computed on a declared grid of ``fractionalBits`` (default 16): values
are integers on that grid, the quantiser rounds toward -inf, and every
comparison is an integer cross-multiplied inequality -- no division, no
square roots, no floating point anywhere on the path.

Why the grid exists at all: two sides of a fidelity check run on
different machines, on different data, possibly years apart. A comparison
that went through binary floating point would depend on the order the
implementation happened to accumulate in. On the grid it does not: the
inputs are integers, the arithmetic is exact, and the verdict is a
property of the numbers rather than of the run.

Three rules follow from that, and this module enforces all three:

* **a declared tolerance is rounded in the direction that tightens it**
  (:func:`tighten_onto_grid`) -- rounding a tolerance outward would make
  the grid itself a source of leniency;
* **an encoding-error bound epsilon is propagated by interval arithmetic**
  from the declared value ranges and minimum counts, and the compared
  margin is ``tau' = tau - epsilon`` (:func:`effective_tolerance`), with
  ``epsilon >= tau`` failing closed (``FLX-5002``) rather than comparing
  against a margin the encoding has already eaten;
* **intermediate magnitudes are bounded at compile time** from the
  declared ranges and the maximum record count
  (:func:`check_field_capacity`, ``FLX-5001``), so no comparison silently
  exceeds the capacity of the field the boundary evaluates it in.

Every function here is pure and total on ``int`` / ``Decimal`` /
``Fraction``: no I/O, no clock, no state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Dict, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.core.errors import FluxError, register

__all__ = [
    "Bound",
    "DEFAULT_FIELD_CAPACITY_BITS",
    "DEFAULT_FRACTIONAL_BITS",
    "Exact",
    "FLX_5001",
    "FLX_5002",
    "FLX_5003",
    "FLX_5004",
    "FLX_5005",
    "Grid",
    "Interval",
    "MAX_FRACTIONAL_BITS",
    "MagnitudeReport",
    "SecondMoments",
    "correlation_within",
    "correlation_within_bounds",
    "check_field_capacity",
    "effective_tolerance",
    "epsilon_correlation",
    "epsilon_marginal_histogram",
    "epsilon_mean_absolute",
    "epsilon_mean_relative",
    "epsilon_quantile",
    "epsilon_support",
    "magnitude_bits",
    "mean_within_absolute",
    "mean_within_relative",
    "order_statistic_index",
    "quantile_rank_ok",
    "quantile_within",
    "require_sample",
    "support_within",
    "tighten_onto_grid",
    "to_fraction",
    "total_variation_within",
]

FLX_5001 = register(
    "FLX-5001",
    "intermediate magnitude outside the declared field capacity",
    "A comparison formed from the declared value ranges and the maximum "
    "record count can produce an integer wider than the field it is "
    "evaluated in (KERNEL-SPEC §12). Narrow the declared range, lower "
    "fractionalBits, or lower records.max until the bound fits.",
)

FLX_5002 = register(
    "FLX-5002",
    "encoding-error bound is not smaller than the declared tolerance",
    "The tolerance a property declares is no larger than the error the "
    "fixed-point encoding can introduce, so a comparison against it would "
    "report agreement it cannot support. Widen the tolerance, raise "
    "fractionalBits, or narrow the declared value range.",
)

FLX_5003 = register(
    "FLX-5003",
    "value on the fixed-point grid is not exact",
    "The grid accepts int, decimal.Decimal and fractions.Fraction only. A "
    "float, a non-finite Decimal, or any other class would make the "
    "encoded value depend on the platform's binary rounding, which is the "
    "one thing the grid exists to remove.",
)

FLX_5004 = register(
    "FLX-5004",
    "declared grid parameter is invalid or not representable",
    "A grid parameter does not describe a usable grid: fractionalBits out "
    "of range, an interval whose bounds are the wrong way round, or "
    "histogram edges that are not strictly increasing grid points. Binning "
    "is exact only when every declared edge lands on the grid.",
)

FLX_5005 = register(
    "FLX-5005",
    "statistic refused: sample outside the declared bounds",
    "A fidelity comparison fails closed rather than reporting agreement "
    "over a sample it cannot stand behind: an empty sample, a record count "
    "outside the declared records range, a group below records.min, a "
    "variance below the declared floor, or bin counts that do not account "
    "for every record.",
)

#: KERNEL-SPEC §12's default grid.
DEFAULT_FRACTIONAL_BITS = 16

#: The widest grid this module will build. Well above any plausible
#: declaration, and low enough that a typo (``fractionalBits: 100000``)
#: is a diagnostic rather than an allocation.
MAX_FRACTIONAL_BITS = 128

#: Default capacity, in bits, of the integer field a comparison is
#: evaluated in. 253 is the safe width of a 254-bit prime scalar field --
#: the smallest target the boundary's digest and arithmetic ports may
#: later be evaluated in (SEAM-FIDELITY-SPEC §4 keeps the leaf function a
#: port "so it can become circuit-friendly later"). Callers evaluating in
#: plain Python integers can pass a larger capacity; the point of the
#: check is that the bound is computed and asserted, not that it is 253.
DEFAULT_FIELD_CAPACITY_BITS = 253

#: The exact numeric classes the grid accepts. Deliberately no ``float``:
#: see ``FLX-5003``.
Exact = Union[int, Decimal, Fraction]


def to_fraction(value: Exact, *, subject: str = "value") -> Fraction:
    """Return *value* as an exact :class:`~fractions.Fraction`.

    ``Decimal`` converts exactly (``Fraction(Decimal("0.1")) == 1/10``),
    which is the whole reason declared numbers are decimals and not
    floats. A ``float``, a non-finite ``Decimal`` or a ``bool`` is
    ``FLX-5003``.
    """
    if isinstance(value, bool):
        raise FluxError(FLX_5003, f"{subject}: bool is not a number on the grid")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise FluxError(FLX_5003, f"{subject}: Decimal is {value.to_eng_string()}")
        return Fraction(value)
    raise FluxError(FLX_5003, f"{subject}: value class {type(value).__name__!r}")


@dataclass(frozen=True)
class Grid:
    """A fixed-point grid of ``fractional_bits`` binary fractional digits.

    KERNEL-SPEC §12: "``fractionalBits`` (default 16), quantiser rounds
    toward -inf, values are ``int`` on the grid". Encoding is
    ``q = floor(v * 2**b)``, so the encoding error ``v - q/2**b`` lies in
    ``[0, 2**-b)`` for every value -- one-sided, which is what makes the
    epsilon bounds below tight rather than merely safe.
    """

    fractional_bits: int = DEFAULT_FRACTIONAL_BITS

    def __post_init__(self) -> None:
        bits = self.fractional_bits
        if isinstance(bits, bool) or not isinstance(bits, int):
            raise FluxError(FLX_5004, f"fractionalBits class {type(bits).__name__!r}")
        if not 0 <= bits <= MAX_FRACTIONAL_BITS:
            raise FluxError(FLX_5004, f"fractionalBits {bits} outside 0..{MAX_FRACTIONAL_BITS}")

    @property
    def scale(self) -> int:
        """``2**fractional_bits`` -- the number of grid points per unit."""
        return 1 << self.fractional_bits

    @property
    def resolution(self) -> Fraction:
        """The grid step ``2**-fractional_bits``; the encoding-error bound."""
        return Fraction(1, self.scale)

    def quantise(self, value: Exact, *, subject: str = "value") -> int:
        """Encode *value* onto the grid, rounding toward -inf."""
        exact = to_fraction(value, subject=subject)
        return (exact.numerator * self.scale) // exact.denominator

    def quantise_ceil(self, value: Exact, *, subject: str = "value") -> int:
        """Encode *value* onto the grid, rounding toward +inf.

        Not the encoding rule -- :meth:`quantise` is -- but the rounding a
        *lower* bound needs when it is placed on the grid without being
        loosened (see :func:`tighten_onto_grid`).
        """
        exact = to_fraction(value, subject=subject)
        return -((-exact.numerator * self.scale) // exact.denominator)

    def dequantise(self, units: int) -> Fraction:
        """Return the exact value of grid point *units*."""
        return Fraction(units, self.scale)

    def is_on_grid(self, value: Exact, *, subject: str = "value") -> bool:
        """Return whether *value* is exactly a grid point."""
        exact = to_fraction(value, subject=subject)
        return (exact * self.scale).denominator == 1


class Bound(Enum):
    """Which side of a comparison a declared number sits on.

    An ``UPPER`` bound (a tolerance, a maximum) is tightened by rounding
    **down** onto the grid; a ``LOWER`` bound (a minimum variance, a floor)
    is tightened by rounding **up**. Rounding either one the other way
    would make the grid a source of leniency the declaration never granted.
    """

    UPPER = "upper"
    LOWER = "lower"


def tighten_onto_grid(grid: Grid, value: Exact, bound: Bound) -> Fraction:
    """Round *value* onto *grid* in the direction that tightens *bound*.

    KERNEL-SPEC §12: "a declared tolerance is rounded in the direction
    that tightens it". The result is exactly representable on the grid, so
    every downstream comparison stays integral.
    """
    if bound is Bound.UPPER:
        return grid.dequantise(grid.quantise(value, subject="upper bound"))
    return grid.dequantise(grid.quantise_ceil(value, subject="lower bound"))


@dataclass(frozen=True)
class Interval:
    """A closed interval of exact rationals, for epsilon propagation.

    KERNEL-SPEC §12 propagates the encoding-error bound "by interval
    arithmetic from declared value ranges and minimum counts"; this is the
    interval those declarations become. Operations are the standard
    interval forms and stay exact -- the endpoints are ``Fraction``.
    """

    lo: Fraction
    hi: Fraction

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            raise FluxError(FLX_5004, f"interval [{self.lo}, {self.hi}] is inverted")

    @classmethod
    def of(cls, lo: Exact, hi: Exact, *, subject: str = "range") -> "Interval":
        """Build an interval from any two exact numbers."""
        return cls(
            lo=to_fraction(lo, subject=f"{subject}.min"),
            hi=to_fraction(hi, subject=f"{subject}.max"),
        )

    @property
    def width(self) -> Fraction:
        """``hi - lo``."""
        return self.hi - self.lo

    @property
    def contains_zero(self) -> bool:
        """Whether zero lies inside the interval (endpoints included)."""
        return self.lo <= 0 <= self.hi

    @property
    def magnitude(self) -> Fraction:
        """The largest ``|v|`` for ``v`` in the interval."""
        return max(abs(self.lo), abs(self.hi))

    @property
    def min_magnitude(self) -> Fraction:
        """The smallest ``|v|`` in the interval -- zero when it spans zero."""
        if self.contains_zero:
            return Fraction(0)
        return min(abs(self.lo), abs(self.hi))

    def contains(self, value: Exact) -> bool:
        """Whether *value* lies in the interval."""
        exact = to_fraction(value)
        return self.lo <= exact <= self.hi

    def __add__(self, other: "Interval") -> "Interval":
        return Interval(self.lo + other.lo, self.hi + other.hi)

    def __sub__(self, other: "Interval") -> "Interval":
        return Interval(self.lo - other.hi, self.hi - other.lo)

    def __neg__(self) -> "Interval":
        return Interval(-self.hi, -self.lo)

    def __mul__(self, other: "Interval") -> "Interval":
        corners = (
            self.lo * other.lo,
            self.lo * other.hi,
            self.hi * other.lo,
            self.hi * other.hi,
        )
        return Interval(min(corners), max(corners))


def _encoding_error(grid: Grid) -> Interval:
    """The interval the per-value encoding error lives in: ``[0, 2**-b)``.

    Represented closed at the top (``[0, delta]``) because an interval of
    exact rationals has no notion of an open endpoint; every bound derived
    from it is therefore conservative by at most one grid step.
    """
    return Interval(Fraction(0), grid.resolution)


def epsilon_mean_absolute(grid: Grid) -> Fraction:
    """Encoding-error bound for a mean compared with an absolute tolerance.

    Each side's encoded mean is its true mean minus the mean of that
    side's per-value encoding errors, which lies in ``[0, delta)`` by
    :func:`_encoding_error`. The difference of two such means therefore
    differs from the difference of the true means by less than ``delta``,
    whatever the record counts are -- the count cancels, which is why this
    bound does not improve with more records.
    """
    error = _encoding_error(grid)
    difference = error - error
    return max(abs(difference.lo), abs(difference.hi))


def epsilon_mean_relative(grid: Grid, value_range: Interval, tolerance: Exact) -> Fraction:
    """Encoding-error bound for a mean compared with a relative tolerance.

    The check is ``|m_a - m_b| <= tau * |m_b|`` and both sides are
    encoded. Writing ``delta`` for the grid resolution, the left side
    carries at most ``delta`` of encoding error and the right side's
    reference magnitude carries at most ``tau * delta``, so a bound of
    ``delta * (1 + tau) / M`` -- with ``M`` the smallest magnitude the
    reference mean can take -- keeps the encoded verdict sound in the
    true-valued domain.

    ``M`` comes from the declared column range: a mean of values in
    ``[lo, hi]`` lies in ``[lo, hi]``. When that range spans zero the
    reference mean can be zero, no relative bound exists, and the compile
    fails closed (``FLX-5002``).
    """
    tau = to_fraction(tolerance, subject="tolerance")
    if tau <= 0:
        raise FluxError(FLX_5002, f"tolerance {tau} is not positive")
    reference = value_range.min_magnitude
    if reference == 0:
        raise FluxError(
            FLX_5002,
            "a relative tolerance needs a reference magnitude, but the declared "
            "range spans zero",
        )
    return grid.resolution * (1 + tau) / reference


def epsilon_marginal_histogram(grid: Grid, edges: Sequence[Exact]) -> Fraction:
    """Encoding-error bound for a histogram / total-variation comparison.

    Zero -- but only because the edges are required to be grid points.
    With ``E * 2**b`` an integer, ``v >= E`` if and only if
    ``floor(v * 2**b) >= E * 2**b``, so encoding never moves a record
    across an edge and the bin counts on the grid are the bin counts of
    the true values. An off-grid edge would move an unbounded number of
    records (their count depends on the data's density near the edge,
    which no declaration bounds), so it is refused (``FLX-5004``) rather
    than absorbed into a bound that cannot be justified.
    """
    if len(edges) < 2:
        raise FluxError(FLX_5004, f"histogram needs at least two edges, got {len(edges)}")
    previous: Optional[Fraction] = None
    for index, edge in enumerate(edges):
        exact = to_fraction(edge, subject=f"bins[{index}]")
        if not grid.is_on_grid(exact):
            raise FluxError(
                FLX_5004,
                f"bins[{index}] is not a grid point at fractionalBits=" f"{grid.fractional_bits}",
            )
        if previous is not None and exact <= previous:
            raise FluxError(FLX_5004, f"bins[{index}] does not increase")
        previous = exact
    return Fraction(0)


def epsilon_support() -> Fraction:
    """Encoding-error bound for a support check: zero.

    Support is a comparison of sorted string members and a count of
    non-members. No value is encoded, so the grid contributes no error.
    """
    return Fraction(0)


def epsilon_quantile(grid: Grid) -> Fraction:
    """Encoding-error bound for a quantile compared in the value domain.

    Encoding is monotone non-decreasing, so it never reorders records and
    the order statistic keeps its index; only its *value* moves, by less
    than one grid step on each side. The bound is therefore ``delta``, the
    same cancellation as :func:`epsilon_mean_absolute`.
    """
    return epsilon_mean_absolute(grid)


def _sqrt_lower_bound(value: Fraction, *, bits: int = 64) -> Fraction:
    """Return an exact rational ``r`` with ``0 < r <= sqrt(value)``.

    ``floor(sqrt(value) * 2**bits) / 2**bits``, computed with
    :func:`math.isqrt` -- integer-only, so it is the same number on every
    platform. Used where a bound needs to divide by a square root and must
    stay conservative: a smaller root makes the resulting epsilon larger,
    which tightens the margin.
    """
    if value <= 0:
        raise FluxError(FLX_5004, f"cannot bound the square root of {value}")
    scale = 1 << bits
    root = math.isqrt((value.numerator * scale * scale) // value.denominator)
    if root == 0:
        raise FluxError(
            FLX_5004,
            f"variance floor {value} is too small to bound at {bits} bits",
        )
    return Fraction(root, scale)


def epsilon_correlation(grid: Grid, min_variance: Exact) -> Fraction:
    """Encoding-error bound for a correlation comparison.

    A sample correlation is the inner product of the two centred, unit-
    normalised data vectors. Encoding perturbs each column by a vector
    ``u`` with ``||u|| <= delta * sqrt(k)``; normalising has Lipschitz
    constant ``2 / ||x_c||`` and ``||x_c|| = sqrt(Qx) / sqrt(k)``, so one
    column moves the correlation by at most ``2 * delta * k / sqrt(Qx)``.
    With the declared variance floor ``Qx >= k**2 * v`` that is
    ``2 * delta / sqrt(v)`` per column, ``4 * delta / sqrt(v)`` per side,
    and ``8 * delta / sqrt(v)`` for the difference of the two sides being
    compared.

    ``sqrt(v)`` is replaced by the rational lower bound of
    :func:`_sqrt_lower_bound`, which can only enlarge the result.
    """
    variance = to_fraction(min_variance, subject="minVariance")
    if variance <= 0:
        raise FluxError(
            FLX_5004,
            "a correlation property needs a positive minVariance to bound its " "encoding error",
        )
    return 8 * grid.resolution / _sqrt_lower_bound(variance)


def effective_tolerance(grid: Grid, tolerance: Exact, epsilon: Exact) -> Fraction:
    """Return ``tau' = tau - epsilon``, rounded onto the grid, or fail closed.

    KERNEL-SPEC §12 and SEAM-FIDELITY-SPEC §3: the compared margin is the
    declared tolerance less the encoding-error bound, rounded in the
    tightening direction, and ``epsilon >= tau`` is ``FLX-5002`` at
    compile time. A margin that rounds down to zero is refused for the
    same reason: a comparison against zero margin reports agreement only
    on exact equality, which is not what the declaration asked for.
    """
    tau = to_fraction(tolerance, subject="tolerance")
    eps = to_fraction(epsilon, subject="epsilon")
    if tau <= 0:
        raise FluxError(FLX_5002, f"tolerance {tau} is not positive")
    if eps < 0:
        raise FluxError(FLX_5004, f"epsilon {eps} is negative")
    if eps >= tau:
        raise FluxError(
            FLX_5002,
            f"epsilon {eps} is not smaller than tolerance {tau}",
        )
    margin = tighten_onto_grid(grid, tau - eps, Bound.UPPER)
    if margin <= 0:
        raise FluxError(
            FLX_5002,
            f"tolerance {tau} less epsilon {eps} rounds to zero at "
            f"fractionalBits={grid.fractional_bits}",
        )
    return margin


@dataclass(frozen=True)
class MagnitudeReport:
    """The compile-time magnitude bound of each named intermediate."""

    bounds: Mapping[str, int]
    capacity_bits: int

    @property
    def widest(self) -> Tuple[str, int]:
        """The name and bit width of the widest intermediate."""
        name = max(sorted(self.bounds), key=lambda key: self.bounds[key])
        return name, self.bounds[name]

    @property
    def within_capacity(self) -> bool:
        """Whether every intermediate fits the declared field capacity."""
        return self.widest[1] <= self.capacity_bits


def magnitude_bits(
    *,
    grid: Grid,
    value_range: Interval,
    max_records: int,
    tolerance: Optional[Exact] = None,
) -> Dict[str, int]:
    """Bound the width, in bits, of every intermediate a comparison forms.

    KERNEL-SPEC §12: "Intermediate magnitudes are bounded at compile time
    from the declared ranges and maximum record count". The named
    intermediates are exactly the quantities the comparison helpers below
    build, with ``q`` the largest encoded magnitude the declared range
    admits, ``k`` the maximum record count and ``d`` the denominator of
    the compared margin:

    ``value``
        ``q`` -- one encoded value.
    ``sum``
        ``k * q`` -- ``Sx``.
    ``sumOfProducts``
        ``k * q**2`` -- ``Sxx``, ``Sxy``, ``Syy``.
    ``centredSecondMoment``
        ``k**2 * q**2`` -- ``Qx = k*Sxx - Sx**2``.
    ``correlationNumerator``
        ``2 * k**2 * q**2`` -- ``N = k*Sxy - Sx*Sy``.
    ``meanComparison``
        ``d * k**2 * q`` -- the cross-multiplied mean inequality.
    ``totalVariationComparison``
        ``2 * d * k**2`` -- the cross-multiplied total-variation inequality.
    ``correlationComparison``
        ``4 * d**2 * k**4 * q**4`` -- ``N**2`` against ``U**2 * Qx * Qy``
        with the margin's denominator cleared, the widest of the set.
    """
    if max_records <= 0:
        raise FluxError(FLX_5004, f"records.max {max_records} must be positive")
    q = max(
        abs(grid.quantise(value_range.lo, subject="range.min")),
        abs(grid.quantise(value_range.hi, subject="range.max")),
    )
    k = max_records
    margin = grid.scale if tolerance is None else to_fraction(tolerance).denominator
    magnitudes: Dict[str, int] = {
        "value": q,
        "sum": k * q,
        "sumOfProducts": k * q * q,
        "centredSecondMoment": k * k * q * q,
        "correlationNumerator": 2 * k * k * q * q,
        "meanComparison": margin * k * k * q,
        "totalVariationComparison": 2 * margin * k * k,
        "correlationComparison": 4 * margin * margin * k**4 * q**4,
    }
    return {name: int(value).bit_length() for name, value in sorted(magnitudes.items())}


def check_field_capacity(
    *,
    grid: Grid,
    value_range: Interval,
    max_records: int,
    tolerance: Optional[Exact] = None,
    capacity_bits: int = DEFAULT_FIELD_CAPACITY_BITS,
) -> MagnitudeReport:
    """Bound every intermediate and raise ``FLX-5001`` if one does not fit."""
    if capacity_bits <= 0:
        raise FluxError(FLX_5004, f"capacity {capacity_bits} bits must be positive")
    report = MagnitudeReport(
        bounds=magnitude_bits(
            grid=grid,
            value_range=value_range,
            max_records=max_records,
            tolerance=tolerance,
        ),
        capacity_bits=capacity_bits,
    )
    if not report.within_capacity:
        name, width = report.widest
        raise FluxError(
            FLX_5001,
            f"{name} needs {width} bits, capacity is {capacity_bits}",
        )
    return report


def require_sample(
    count: int,
    *,
    minimum: int = 1,
    maximum: Optional[int] = None,
    subject: str = "sample",
) -> None:
    """Fail closed (``FLX-5005``) unless *count* is inside the declared bounds.

    SEAM-FIDELITY-SPEC §5's fail-closed conditions: ``k = 0``, a count
    outside ``records``, a group below ``records.min``. Called before any
    comparison rather than after, so a refusal never reads as a verdict.
    """
    if count < max(minimum, 1):
        raise FluxError(FLX_5005, f"{subject}: {count} records, minimum is {minimum}")
    if maximum is not None and count > maximum:
        raise FluxError(FLX_5005, f"{subject}: {count} records, maximum is {maximum}")


def _margin(tolerance: Exact) -> Tuple[int, int]:
    tau = to_fraction(tolerance, subject="tolerance")
    if tau < 0:
        raise FluxError(FLX_5004, f"margin {tau} is negative")
    return tau.numerator, tau.denominator


def mean_within_absolute(
    *,
    sum_a: int,
    count_a: int,
    sum_b: int,
    count_b: int,
    tolerance_units: int,
) -> bool:
    """``|Sx*n' - Sx'*k| <= tau'*k*n'`` (SEAM-FIDELITY-SPEC §5).

    Both sides are sums of **encoded** values over their own record
    counts; cross-multiplying by the two counts removes the division, so
    the verdict is an integer comparison of exactly representable
    quantities.

    An absolute tolerance is the one margin in this module that carries
    the column's units rather than being a ratio, so it is passed as an
    integer count of grid steps -- ``grid.quantise(tau_prime)`` -- and not
    as a value-domain number that would silently be a scale factor out.
    The relative, total-variation and correlation comparisons take a
    dimensionless ``Fraction`` because their units cancel.
    """
    require_sample(count_a, subject="side a")
    require_sample(count_b, subject="side b")
    if tolerance_units < 0:
        raise FluxError(FLX_5004, f"margin {tolerance_units} grid steps is negative")
    left = abs(sum_a * count_b - sum_b * count_a)
    right = tolerance_units * count_a * count_b
    return left <= right


def mean_within_relative(
    *,
    sum_a: int,
    count_a: int,
    sum_b: int,
    count_b: int,
    tolerance: Exact,
) -> bool:
    """The homogenised relative form: ``|Sx*n' - Sx'*k| <= tau'*|Sx'|*k``.

    Side ``b`` is the reference the tolerance is relative to. A zero
    reference sum has no relative neighbourhood, so it fails closed
    (``FLX-5005``) rather than passing or failing by accident.
    """
    require_sample(count_a, subject="side a")
    require_sample(count_b, subject="side b")
    if sum_b == 0:
        raise FluxError(
            FLX_5005,
            "relative comparison against a reference sum of zero",
        )
    num, den = _margin(tolerance)
    left = abs(sum_a * count_b - sum_b * count_a) * den
    right = num * abs(sum_b) * count_a
    return left <= right


def total_variation_within(
    *,
    counts_a: Sequence[int],
    count_a: int,
    counts_b: Sequence[int],
    count_b: int,
    tolerance: Exact,
) -> bool:
    """``sum_bins |n'*c - k*c'| <= 2*tau'*k*n'`` (SEAM-FIDELITY-SPEC §5).

    The bins of the two sides must align one-for-one, and each side's bin
    counts must account for every one of its records: an unbinned record
    is a value outside the declared range, which is a fail-closed
    condition (``FLX-5005``), not a bin with less mass.
    """
    if len(counts_a) != len(counts_b):
        raise FluxError(
            FLX_5004,
            f"bin counts differ in length: {len(counts_a)} and {len(counts_b)}",
        )
    require_sample(count_a, subject="side a")
    require_sample(count_b, subject="side b")
    if sum(counts_a) != count_a:
        raise FluxError(FLX_5005, "side a: bin counts do not account for every record")
    if sum(counts_b) != count_b:
        raise FluxError(FLX_5005, "side b: bin counts do not account for every record")
    num, den = _margin(tolerance)
    left = (
        sum(abs(count_b * a - count_a * b) for a, b in zip(counts_a, counts_b, strict=True)) * den
    )
    right = 2 * num * count_a * count_b
    return left <= right


@dataclass(frozen=True)
class SecondMoments:
    """The aggregate quantities a correlation comparison needs.

    Exactly the derived statistic ``S`` of SEAM-FIDELITY-SPEC §4 for a
    correlation property: counts and sums of encoded values, never a
    record. ``numerator``, ``qx`` and ``qy`` are the scaled covariance and
    variances -- ``Qx = k*Sxx - Sx**2 = k**2 * Var(x)`` -- so the variance
    floor of the declaration reads as ``Qx >= k**2 * v``.
    """

    count: int
    sum_x: int
    sum_y: int
    sum_xx: int
    sum_yy: int
    sum_xy: int

    @property
    def numerator(self) -> int:
        """``N = k*Sxy - Sx*Sy``."""
        return self.count * self.sum_xy - self.sum_x * self.sum_y

    @property
    def qx(self) -> int:
        """``Qx = k*Sxx - Sx**2``; non-negative by Cauchy-Schwarz."""
        return self.count * self.sum_xx - self.sum_x * self.sum_x

    @property
    def qy(self) -> int:
        """``Qy = k*Syy - Sy**2``; non-negative by Cauchy-Schwarz."""
        return self.count * self.sum_yy - self.sum_y * self.sum_y

    def require_variance(self, min_variance: Exact, *, subject: str = "moments") -> None:
        """Fail closed unless both columns clear the declared variance floor.

        Below the floor a correlation is dominated by the encoding error
        (see :func:`epsilon_correlation`), so the comparison is refused
        rather than answered.
        """
        require_sample(self.count, minimum=2, subject=subject)
        floor = to_fraction(min_variance, subject="minVariance")
        if floor <= 0:
            raise FluxError(FLX_5004, f"minVariance {floor} must be positive")
        threshold = self.count * self.count * floor
        for name, value in (("x", self.qx), ("y", self.qy)):
            if value * threshold.denominator < threshold.numerator:
                raise FluxError(
                    FLX_5005,
                    f"{subject}: column {name} variance is below the declared floor",
                )


def _at_most_root(left: int, coefficient: Fraction, radicand: int) -> bool:
    """Return ``left <= coefficient * sqrt(radicand)`` without a square root.

    Split by sign, then squared: with ``radicand >= 0`` the right side has
    the sign of *coefficient*, so the four cases are decided by comparing
    ``left**2`` with ``coefficient**2 * radicand`` in the direction that
    squaring preserves.
    """
    if radicand < 0:  # pragma: no cover - Qx*Qy is a product of two non-negatives
        raise FluxError(FLX_5004, f"radicand {radicand} is negative")
    if radicand == 0:
        # The right side is exactly zero whatever the coefficient's sign,
        # so squaring would lose the comparison's direction.
        return left <= 0
    squared_left = left * left * coefficient.denominator * coefficient.denominator
    squared_right = coefficient.numerator * coefficient.numerator * radicand
    if coefficient >= 0:
        if left <= 0:
            return True
        return squared_left <= squared_right
    if left >= 0:
        return False
    return squared_left >= squared_right


def _at_least_root(left: int, coefficient: Fraction, radicand: int) -> bool:
    """Return ``left >= coefficient * sqrt(radicand)`` without a square root."""
    return _at_most_root(-left, -coefficient, radicand)


def correlation_within_bounds(
    *,
    moments: SecondMoments,
    lower: Exact,
    upper: Exact,
    min_variance: Exact,
) -> bool:
    """``L*R <= A <= U*R`` split by signs (SEAM-FIDELITY-SPEC §5).

    ``A`` is the covariance numerator ``N`` and ``R`` is
    ``sqrt(Qx * Qy)``; comparing ``N`` against a rational multiple of a
    square root is decided by sign analysis and one squaring, so no root
    is ever taken. Both columns must clear ``min_variance`` first.
    """
    moments.require_variance(min_variance)
    low = to_fraction(lower, subject="lower")
    high = to_fraction(upper, subject="upper")
    if low > high:
        raise FluxError(FLX_5004, f"correlation bounds [{low}, {high}] are inverted")
    radicand = moments.qx * moments.qy
    numerator = moments.numerator
    return _at_most_root(numerator, high, radicand) and _at_least_root(numerator, low, radicand)


def correlation_within(
    *,
    moments_a: SecondMoments,
    moments_b: SecondMoments,
    tolerance: Exact,
    min_variance: Exact,
) -> bool:
    """``|r_a - r_b| <= tau'`` for two sample correlations, root-free.

    Squaring the difference leaves one square root, in the cross term:
    with ``A = Na/sqrt(Ra)`` and ``B = Nb/sqrt(Rb)``, the test
    ``(A-B)**2 <= tau'**2`` rearranges to
    ``A**2 + B**2 - tau'**2 <= 2*Na*Nb / sqrt(Ra*Rb)``, whose left side is
    rational and whose right side is an integer over one square root --
    decided by :func:`_at_least_root`. Exact, and no root is taken.
    """
    moments_a.require_variance(min_variance, subject="side a")
    moments_b.require_variance(min_variance, subject="side b")
    tau = to_fraction(tolerance, subject="tolerance")
    if tau < 0:
        raise FluxError(FLX_5004, f"margin {tau} is negative")
    radicand_a = moments_a.qx * moments_a.qy
    radicand_b = moments_b.qx * moments_b.qy
    left = (
        Fraction(moments_a.numerator * moments_a.numerator, radicand_a)
        + Fraction(moments_b.numerator * moments_b.numerator, radicand_b)
        - tau * tau
    )
    cross = 2 * moments_a.numerator * moments_b.numerator
    return _at_least_root(cross, left, radicand_a * radicand_b)


def order_statistic_index(count: int, level: Exact) -> int:
    """The 1-based order-statistic index of quantile *level* over *count*.

    ``j = ceil(level * count)``, clamped to ``[1, count]`` -- the smallest
    index whose value has at least ``level`` of the sample at or below it.
    Integer-only, so both sides of a comparison pick the same record.
    """
    require_sample(count, subject="quantile sample")
    fraction = to_fraction(level, subject="quantile level")
    if not 0 <= fraction <= 1:
        raise FluxError(FLX_5004, f"quantile level {fraction} is outside [0, 1]")
    index = -((-fraction.numerator * count) // fraction.denominator)
    return min(max(index, 1), count)


def quantile_rank_ok(
    *,
    count: int,
    level: Exact,
    count_strictly_below: int,
    count_at_or_below: int,
) -> bool:
    """Whether a witness with these ranks really is the *level* quantile.

    The witness is the order statistic at index ``j`` exactly when fewer
    than ``j`` records lie strictly below it and at least ``j`` lie at or
    below it. Checking the rank on both sides is what stops a witness from
    being asserted rather than demonstrated.
    """
    index = order_statistic_index(count, level)
    if not 0 <= count_strictly_below <= count_at_or_below <= count:
        raise FluxError(
            FLX_5005,
            f"witness ranks {count_strictly_below}/{count_at_or_below} are not "
            f"a sub-count of {count}",
        )
    return count_strictly_below < index <= count_at_or_below


def quantile_within(*, witness_a: int, witness_b: int, tolerance_units: int) -> bool:
    """Whether two quantile witnesses agree within *tolerance_units* grid steps.

    Both witnesses are encoded values, so the comparison is a difference
    of integers against an integer margin -- put the margin on the grid
    with :meth:`Grid.quantise` on ``tau'`` first.
    """
    if tolerance_units < 0:
        raise FluxError(FLX_5004, f"margin {tolerance_units} grid steps is negative")
    return abs(witness_a - witness_b) <= tolerance_units


def support_within(*, non_member_count: int, max_non_members: int) -> bool:
    """Whether the count of values outside the committed support is allowed.

    SEAM-FIDELITY-SPEC §5's support rule: "count of non-members <= max".
    A negative count is a caller error, not a passing check.
    """
    if non_member_count < 0:
        raise FluxError(FLX_5005, f"non-member count {non_member_count} is negative")
    if max_non_members < 0:
        raise FluxError(FLX_5004, f"support maximum {max_non_members} is negative")
    return non_member_count <= max_non_members
