# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Kernel draws -- KERNEL-SPEC §4 ("Draws: fixed draw count, no rejection, no float branch").

Every sampler here obeys four normative rules from §4:

1. **The bit source is raw words only.** numpy is used *only* as the bit source
   (``bit_generator.random_raw()``); no numpy distribution function is called on any
   digest path, and ``PCG64`` is named explicitly (never ``default_rng``). The single
   numpy touch point is :class:`DrawStream`, which converts each 64-bit word to a Python
   ``int`` at the boundary -- no numpy scalar, and in particular no numpy float, is ever
   handed to a caller.
2. **Fixed draw count.** Each sampler consumes a number of raw words that is a function of
   its *shape* arguments only, never of the values drawn -- see :data:`RAW_WORDS_PER_DRAW`
   and :func:`permutation_word_cost`. There are no rejection loops: ``bounded_uint``
   consumes its 64-word budget in full whatever it draws.
3. **No float anywhere.** Probabilities and continuous results are ``Decimal``; thresholds
   and comparisons are exact integer arithmetic over ``Fraction``; the inverse-CDF work is
   done in the ``decimal`` module, whose General Decimal Arithmetic operations are
   correctly rounded and therefore give the same digits on every libm and architecture.
4. **Mappings iterate by sorted key.** ``categorical`` orders its levels by NFC key
   (:func:`nfc_sorted_items`), never by insertion order.

Never used here, per §4's closing sentence: ``random``, a numpy ``Generator`` distribution
method, ``math.exp``/``log``/``erf`` on a digest path, or a rejection loop.
"""

from __future__ import annotations

from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    getcontext,
    localcontext,
)
from fractions import Fraction
from functools import lru_cache
from typing import Mapping, Tuple, TypeVar

from numpy.random import PCG64, Generator, SeedSequence

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError, register
from flux_engine.core.lrm import FLX_5020, WEIGHT_SUM_TOLERANCE

__all__ = [
    "BOUNDED_UINT_DRAW_BUDGET",
    "FLX_5021",
    "KERNEL_DECIMAL_PRECISION",
    "RAW_WORDS_PER_DRAW",
    "UNIFORM53_DENOMINATOR",
    "WORD_BITS",
    "DrawStream",
    "bernoulli",
    "bounded_uint",
    "categorical",
    "exponential",
    "kernel_context",
    "nfc_sorted_items",
    "normal_cdf",
    "normal_ppf",
    "permutation",
    "permutation_word_cost",
    "poisson",
    "poisson_cap",
    "truncated_normal",
    "uniform53",
]

_T = TypeVar("_T")

FLX_5021 = register(
    "FLX-5021",
    "draw parameter outside its declared domain",
    "A kernel sampler was asked for a draw it cannot form -- a probability outside "
    "[0, 1], a non-positive rate, a bound below 1, an interval whose lower end is "
    "above its upper end, or a scale on which no value of the declared interval is "
    "representable. The kernel refuses rather than substituting a default, because "
    "the substituted value would be hashed into the run digest and no reader could "
    "tell it apart from an authored one (KERNEL-SPEC §4).",
)

#: Width of one raw word from the bit generator. PCG64's ``random_raw`` is 64-bit.
WORD_BITS = 64
_WORD_MASK = (1 << WORD_BITS) - 1

#: ``uniform53`` returns an integer in ``[0, 2**53)``; this is the denominator that turns
#: it into an exact fraction of the unit interval.
UNIFORM53_DENOMINATOR = 1 << 53

#: §4: ``bounded_uint`` is "debiased by a fixed 64-draw budget ... the bound is consumed in
#: full (all 64 words are always drawn) so the stream position is data-independent".
BOUNDED_UINT_DRAW_BUDGET = 64

#: The declared precision of §4's decimal evaluation ("evaluated in the ``decimal`` module
#: at 28 digits"). :func:`normal_cdf` and :func:`normal_ppf` return values at this
#: precision, and every continuous draw is rounded onto it before quantisation.
KERNEL_DECIMAL_PRECISION = 28

#: Guard digits used *inside* the series and continued-fraction evaluations, so that the
#: cancellation in ``1 - erf(z)`` and in the Halley residual cannot eat into the declared
#: 28 digits. An implementation detail of the same correctly-rounded decimal arithmetic:
#: every intermediate is still exact-to-the-digit and identical on every platform.
_WORKING_PRECISION = 50

#: Argument at which Φ's evaluation switches from the positive-term error-function series
#: to the continued fraction for the complementary error function. Below it the series has
#: no cancellation at all; above it ``1 - erf`` starts losing digits faster than the guard
#: digits above can absorb.
_ERFC_SWITCH = Decimal(4)

#: Depth of the backward recurrence for the complementary-error-function continued
#: fraction. Fixed rather than convergence-tested, so the evaluation is straight-line: no
#: iteration count, and therefore no result, depends on how close two intermediate values
#: happened to land.
_ERFC_CF_DEPTH = 200

#: Defensive bound on the error-function series. Unreachable for any argument that reaches
#: it: the series is entered only for ``z <= 4``, where it settles in well under 200 terms.
_SERIES_MAX_TERMS = 10_000

#: Raw 64-bit words consumed by each sampler, keyed by function name. Pinned by the "fixed
#: draw count per sample" property test: the stream position must advance by exactly this
#: much, whatever the parameters and whatever the values drawn. ``permutation`` is
#: shape-dependent and has its own :func:`permutation_word_cost`.
RAW_WORDS_PER_DRAW: Mapping[str, int] = {
    "uniform53": 1,
    "bernoulli": 1,
    "categorical": 1,
    "bounded_uint": BOUNDED_UINT_DRAW_BUDGET,
    "truncated_normal": 1,
    "exponential": 1,
    "poisson": 1,
}


def nfc_sorted_items(mapping: Mapping[str, _T]) -> Tuple[Tuple[str, _T], ...]:
    """Return *mapping*'s items ordered by NFC key.

    KERNEL-SPEC §7: "iteration over any mapping is by sorted key"; §4's ``categorical``
    names the same order for its levels. The raw key is the secondary sort term so that two
    distinct keys sharing one NFC form still have a total, reproducible order rather than
    one that depends on the mapping's insertion history.
    """
    return tuple(sorted(mapping.items(), key=lambda item: (nfc(item[0]), item[0])))


# ---------------------------------------------------------------------------
# The bit source
# ---------------------------------------------------------------------------


class DrawStream:
    """One path-addressed stream of raw 64-bit words (KERNEL-SPEC §3, §4).

    Wraps the numpy bit generator so that no numpy object -- and in particular no numpy
    float -- escapes into the kernel: :meth:`next_raw` returns a Python ``int``.
    :attr:`position` counts the words consumed so far, which is what the "fixed draw count"
    property test asserts against :data:`RAW_WORDS_PER_DRAW`.

    A stream is addressed by its seed-tree path (:mod:`flux_engine.core.seedtree`) and is
    never spawned from a parent, so no stream's values depend on the order in which the
    run happened to reach it.
    """

    __slots__ = ("_bit_generator", "_generator", "_position", "label")

    def __init__(self, generator: Generator, *, label: str = "") -> None:
        self._generator = generator
        self._bit_generator = generator.bit_generator
        self._position = 0
        self.label = label

    @classmethod
    def from_entropy(cls, entropy: int, *, label: str = "") -> "DrawStream":
        """Build the stream for one seed-tree entropy value.

        §3 names the construction exactly: ``Generator(PCG64(SeedSequence(<entropy>)))``.
        ``PCG64`` is named here rather than reached through ``default_rng``, so a change to
        numpy's default generator can never move a digest.
        """
        if entropy < 0:
            raise FluxError(FLX_5021, f"stream entropy must be non-negative, got {entropy}")
        return cls(Generator(PCG64(SeedSequence(entropy))), label=label)

    @property
    def position(self) -> int:
        """Number of raw words consumed from this stream so far."""
        return self._position

    def next_raw(self) -> int:
        """Consume and return one raw 64-bit word as a Python ``int``."""
        raw = self._bit_generator.random_raw()
        self._position += 1
        return int(raw) & _WORD_MASK

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DrawStream(label={self.label!r}, position={self._position})"


# ---------------------------------------------------------------------------
# Integer draws
# ---------------------------------------------------------------------------


def uniform53(stream: DrawStream) -> int:
    """Return an integer in ``[0, 2**53)`` -- §4: ``random_raw() >> 11``.

    The unit interval is carried as this integer numerator over
    :data:`UNIFORM53_DENOMINATOR` and is never converted to a binary float, so the value a
    downstream comparison sees is exactly the value that was drawn.
    """
    return stream.next_raw() >> (WORD_BITS - 53)


def _probability_threshold(p: Decimal) -> int:
    """``floor(p · 2**53)`` on exact rationals -- the integer form of §4's comparison."""
    return int(Fraction(p) * UNIFORM53_DENOMINATOR)


def _require_probability(p: Decimal, what: str) -> None:
    if not Decimal(0) <= p <= Decimal(1):
        raise FluxError(FLX_5021, f"{what} must lie in [0, 1], got {p}")


def bernoulli(stream: DrawStream, p: Decimal) -> bool:
    """Return ``uniform53 < floor(p · 2**53)`` (§4). The comparison is integer.

    ``p`` is a ``Decimal`` converted to an exact ``Fraction``, so the threshold is the true
    floor of ``p·2**53`` rather than the floor of a rounded binary approximation -- the
    difference is one subject in about nine quadrillion, but it is the difference between a
    reproducible cohort and a nearly reproducible one.
    """
    _require_probability(p, "bernoulli p")
    return uniform53(stream) < _probability_threshold(p)


def categorical(stream: DrawStream, levels: Mapping[str, Decimal]) -> str:
    """Draw one level -- §4: "cumulative integer thresholds over levels sorted by NFC key".

    One raw word. The thresholds are exact integers derived from the exact rational
    weights, so no level's share depends on the order in which the weights were
    accumulated. The last level absorbs the rounding remainder, which makes the sampler
    total: every value ``uniform53`` can return maps to a declared level.
    """
    if not levels:
        raise FluxError(FLX_5021, "categorical levels must not be empty")
    ordered = nfc_sorted_items(levels)
    total = Fraction(0)
    for key, weight in ordered:
        if weight < 0:
            raise FluxError(FLX_5021, f"categorical weight for level {key!r} is negative")
        total += Fraction(weight)
    if abs(total - 1) > Fraction(WEIGHT_SUM_TOLERANCE):
        raise FluxError(
            FLX_5020,
            f"categorical weights sum to {total.numerator}/{total.denominator}, "
            f"which is further than {WEIGHT_SUM_TOLERANCE} from 1",
        )

    draw = uniform53(stream)
    cumulative = Fraction(0)
    for key, weight in ordered[:-1]:
        cumulative += Fraction(weight)
        if draw < int(cumulative * UNIFORM53_DENOMINATOR):
            return key
    return ordered[-1][0]


def bounded_uint(stream: DrawStream, n: int) -> int:
    """Return an integer in ``[0, n)`` -- Lemire's nearly-divisionless method (§4).

    Debiased by a **fixed 64-draw budget**: all :data:`BOUNDED_UINT_DRAW_BUDGET` words are
    drawn every time, and the first one that passes Lemire's rejection test decides the
    result. Consuming the budget in full is what makes the stream position
    data-independent -- a replay of the same path lands on the same word whatever the
    values happened to be. If every word in the budget is rejected (probability below
    ``64 · 2**-64``, effectively unreachable), the last word is taken modulo ``n``; that is
    a documented, bounded bias, chosen over an unbounded rejection loop because an
    unbounded loop would make the stream position depend on the data.
    """
    if n < 1:
        raise FluxError(FLX_5021, f"bounded_uint bound must be >= 1, got {n}")

    words = [stream.next_raw() for _ in range(BOUNDED_UINT_DRAW_BUDGET)]
    # Lemire's rejection threshold, 2**64 mod n. Written out rather than spelled with the
    # C idiom ``(-n) % n``, which relies on unsigned wraparound and evaluates to 0 in
    # Python's arbitrary-precision integers.
    threshold = (1 << WORD_BITS) % n
    for word in words:
        product = word * n
        if (product & _WORD_MASK) >= threshold:
            return product >> WORD_BITS
    return words[-1] % n


def permutation_word_cost(n: int) -> int:
    """Raw words consumed by :func:`permutation` for a sequence of length *n*."""
    if n < 0:
        raise FluxError(FLX_5021, f"permutation length must be >= 0, got {n}")
    return max(n - 1, 0) * BOUNDED_UINT_DRAW_BUDGET


def permutation(stream: DrawStream, n: int) -> Tuple[int, ...]:
    """Return a permutation of ``range(n)`` -- §4: "Fisher-Yates over ``bounded_uint``".

    The downward Fisher-Yates sweep makes exactly ``n - 1`` bounded draws, so the word cost
    is :func:`permutation_word_cost` and depends on the length alone.
    """
    if n < 0:
        raise FluxError(FLX_5021, f"permutation length must be >= 0, got {n}")
    items = list(range(n))
    for i in range(n - 1, 0, -1):
        j = bounded_uint(stream, i + 1)
        items[i], items[j] = items[j], items[i]
    return tuple(items)


# ---------------------------------------------------------------------------
# Decimal Φ and Φ⁻¹
# ---------------------------------------------------------------------------


def kernel_context(precision: int = KERNEL_DECIMAL_PRECISION) -> Context:
    """The decimal context every continuous draw is evaluated in.

    Every field is pinned -- precision, rounding, exponent range and traps -- so a caller's
    ambient ``decimal`` context cannot move a hashed value. The traps are the default set:
    an invalid operation, a division by zero or an overflow raises, rather than producing
    the NaN or infinity that would otherwise travel silently into a digest.
    """
    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def _current_precision() -> int:
    """The precision of the context the caller is currently evaluating in."""
    return getcontext().prec


@lru_cache(maxsize=None)
def _pi(precision: int) -> Decimal:
    """π to *precision* digits.

    Borrowed, not derived: the ``pi()`` recipe published in CPython's ``decimal`` module
    documentation, transcribed onto an explicit context instead of the ambient one so the
    value cannot depend on a caller's settings.
    """
    with localcontext(kernel_context(precision + 4)):
        three = Decimal(3)
        lasts, t, s, n, na, d, da = Decimal(0), three, Decimal(3), 1, 0, 0, 24
        while s != lasts:
            lasts = s
            n, na = n + na, na + 8
            d, da = d + da, da + 32
            t = (t * n) / d
            s += t
    with localcontext(kernel_context(precision)):
        return +s


@lru_cache(maxsize=None)
def _sqrt_pi(precision: int) -> Decimal:
    with localcontext(kernel_context(precision + 4)):
        value = _pi(precision + 4).sqrt()
    with localcontext(kernel_context(precision)):
        return +value


@lru_cache(maxsize=None)
def _sqrt_two(precision: int) -> Decimal:
    with localcontext(kernel_context(precision + 4)):
        value = Decimal(2).sqrt()
    with localcontext(kernel_context(precision)):
        return +value


@lru_cache(maxsize=None)
def _sqrt_two_pi(precision: int) -> Decimal:
    with localcontext(kernel_context(precision + 4)):
        value = (2 * _pi(precision + 4)).sqrt()
    with localcontext(kernel_context(precision)):
        return +value


def _erf_series(z: Decimal) -> Decimal:
    """``erf(z)`` for ``0 <= z <= 4`` by the all-positive-term confluent series.

    ``erf(z) = (2z/√π)·e^{-z²}·Σ (2z²)^n / (2n+1)!!``. Every term is positive, so unlike the
    alternating Taylor series there is no cancellation and the accumulated sum keeps the
    full working precision. Must be called inside a working-precision context.
    """
    z_squared = z * z
    two_z_squared = 2 * z_squared
    term = Decimal(1)
    total = Decimal(1)
    for k in range(1, _SERIES_MAX_TERMS):
        term = term * two_z_squared / (2 * k + 1)
        updated = total + term
        if updated == total:
            break
        total = updated
    else:  # pragma: no cover - unreachable for the z <= 4 this branch is entered for
        raise FluxError(FLX_5021, "error-function series did not settle within its term bound")
    return 2 * z * (-z_squared).exp() * total / _sqrt_pi(_current_precision())


def _erfc_continued_fraction(z: Decimal) -> Decimal:
    """``erfc(z)`` for ``z > 4`` by the backward recurrence of its continued fraction.

    ``erfc(z) = (e^{-z²}/√π) · 1/(z + (1/2)/(z + 1/(z + (3/2)/(z + ...))))``. The recurrence
    runs a fixed :data:`_ERFC_CF_DEPTH` levels rather than stopping on a convergence test,
    so the evaluation is straight-line. Must be called inside a working-precision context.
    """
    tail = Decimal(0)
    for k in range(_ERFC_CF_DEPTH, 0, -1):
        tail = (Decimal(k) / 2) / (z + tail)
    return (-z * z).exp() / _sqrt_pi(_current_precision()) / (z + tail)


def _erfc_nonnegative(z: Decimal) -> Decimal:
    """``erfc(z)`` for ``z >= 0``. Must be called inside a working-precision context."""
    if z <= _ERFC_SWITCH:
        return 1 - _erf_series(z)
    return _erfc_continued_fraction(z)


def normal_cdf(x: Decimal, *, precision: int = KERNEL_DECIMAL_PRECISION) -> Decimal:
    """Φ(x), the standard normal CDF, evaluated in ``decimal`` (KERNEL-SPEC §4).

    Both branches route through the complementary error function of a **non-negative**
    argument, so a lower-tail value keeps its significant digits: ``Φ(-8)`` comes out as a
    number with full precision rather than as the difference of two values that both round
    to 1.
    """
    with localcontext(kernel_context(_WORKING_PRECISION)):
        scaled = x / _sqrt_two(_WORKING_PRECISION)
        if scaled <= 0:
            value = _erfc_nonnegative(-scaled) / 2
        else:
            value = 1 - _erfc_nonnegative(scaled) / 2
    with localcontext(kernel_context(precision)):
        return +value


# Acklam's rational approximation to Φ⁻¹ (relative error below 1.15e-9 across the open unit
# interval). Borrowed, not derived: Peter Acklam's published coefficient set, transcribed as
# exact decimal literals so the approximation -- and therefore the starting point of the
# refinement below -- is identical on every platform.
_ACKLAM_A = (
    Decimal("-3.969683028665376e+01"),
    Decimal("2.209460984245205e+02"),
    Decimal("-2.759285104469687e+02"),
    Decimal("1.383577518672690e+02"),
    Decimal("-3.066479806614716e+01"),
    Decimal("2.506628277459239e+00"),
)
_ACKLAM_B = (
    Decimal("-5.447609879822406e+01"),
    Decimal("1.615858368580409e+02"),
    Decimal("-1.556989798598866e+02"),
    Decimal("6.680131188771972e+01"),
    Decimal("-1.328068155288572e+01"),
)
_ACKLAM_C = (
    Decimal("-7.784894002430293e-03"),
    Decimal("-3.223964580411365e-01"),
    Decimal("-2.400758277161838e+00"),
    Decimal("-2.549732539343734e+00"),
    Decimal("4.374664141464968e+00"),
    Decimal("2.938163982698783e+00"),
)
_ACKLAM_D = (
    Decimal("7.784695709041462e-03"),
    Decimal("3.224671290700398e-01"),
    Decimal("2.445134137142996e+00"),
    Decimal("3.754408661907416e+00"),
)
_ACKLAM_P_LOW = Decimal("0.02425")


def _acklam_tail(q: Decimal) -> Decimal:
    """The shared tail branch of Acklam's approximation. Inside a working context."""
    c0, c1, c2, c3, c4, c5 = _ACKLAM_C
    d0, d1, d2, d3 = _ACKLAM_D
    numerator = ((((c0 * q + c1) * q + c2) * q + c3) * q + c4) * q + c5
    denominator = (((d0 * q + d1) * q + d2) * q + d3) * q + 1
    return numerator / denominator


def _acklam(p: Decimal) -> Decimal:
    """Acklam's initial estimate of Φ⁻¹(p). Inside a working-precision context."""
    if p < _ACKLAM_P_LOW:
        return _acklam_tail((-2 * p.ln()).sqrt())
    if p > 1 - _ACKLAM_P_LOW:
        return -_acklam_tail((-2 * (1 - p).ln()).sqrt())
    a0, a1, a2, a3, a4, a5 = _ACKLAM_A
    b0, b1, b2, b3, b4 = _ACKLAM_B
    q = p - Decimal("0.5")
    r = q * q
    numerator = (((((a0 * r + a1) * r + a2) * r + a3) * r + a4) * r + a5) * q
    denominator = ((((b0 * r + b1) * r + b2) * r + b3) * r + b4) * r + 1
    return numerator / denominator


def normal_ppf(p: Decimal, *, precision: int = KERNEL_DECIMAL_PRECISION) -> Decimal:
    """Φ⁻¹(p) -- §4: "Acklam's rational approximation with one Halley refinement step".

    The refinement is Halley's third-order step on the residual ``Φ(x) − p``::

        e = Φ(x) − p ;  u = e·√(2π)·e^{x²/2} ;  x ← x − u/(1 + x·u/2)

    One step suffices: Acklam's estimate is already accurate to about ``1.15e-9`` and a
    third-order step cubes that error, landing far below the declared precision. The step
    is unconditional -- there is no "iterate until converged" loop whose trip count could
    differ between two evaluations of the same input.
    """
    if not Decimal(0) < p < Decimal(1):
        raise FluxError(FLX_5021, f"normal_ppf requires 0 < p < 1, got {p}")
    with localcontext(kernel_context(_WORKING_PRECISION)):
        x = _acklam(p)
        residual = normal_cdf(x, precision=_WORKING_PRECISION) - p
        u = residual * _sqrt_two_pi(_WORKING_PRECISION) * (x * x / 2).exp()
        refined = x - u / (1 + x * u / 2)
    with localcontext(kernel_context(precision)):
        return +refined


# ---------------------------------------------------------------------------
# Continuous draws
# ---------------------------------------------------------------------------


def _unit_interval(draw: int) -> Decimal:
    """The ``uniform53`` draw as an exact ``Decimal`` in ``[0, 1)``.

    ``2**53`` is a power of two and its reciprocal has 53 significant decimal digits, which
    is inside the working precision, so this quotient is exact: the conversion introduces
    no rounding of its own.
    """
    with localcontext(kernel_context(_WORKING_PRECISION)):
        return Decimal(draw) / Decimal(UNIFORM53_DENOMINATOR)


def _unrepresentable(scale: int) -> FluxError:
    """``FLX-5021`` for a value too large to hold at *scale* in the working precision.

    ``Decimal.quantize`` signals ``InvalidOperation`` when the quantised
    coefficient would need more digits than the context's precision. The
    working context's traps make that an exception, and letting it escape
    bare would turn a declaration the kernel cannot honour into a crash
    instead of the refusal ``FLX-5021`` names.
    """
    return FluxError(
        FLX_5021,
        f"a value of this interval cannot be held at scale {scale} within the "
        f"kernel's {_WORKING_PRECISION}-digit working precision; declare a "
        "narrower interval or a coarser scale",
    )


def _quantise(value: Decimal, scale: int) -> Decimal:
    if scale < 0:
        raise FluxError(FLX_5021, f"declared scale must be >= 0, got {scale}")
    with localcontext(kernel_context(_WORKING_PRECISION)):
        try:
            return value.quantize(Decimal(1).scaleb(-scale))
        except InvalidOperation:
            raise _unrepresentable(scale) from None


def _inward_bounds(lo: Decimal, hi: Decimal, scale: int) -> Tuple[Decimal, Decimal]:
    """The declared bounds rounded *inward* onto the declared scale."""
    if scale < 0:
        raise FluxError(FLX_5021, f"declared scale must be >= 0, got {scale}")
    with localcontext(kernel_context(_WORKING_PRECISION)):
        step = Decimal(1).scaleb(-scale)
        try:
            lo_q = lo.quantize(step, rounding=ROUND_CEILING)
            hi_q = hi.quantize(step, rounding=ROUND_FLOOR)
        except InvalidOperation:
            raise _unrepresentable(scale) from None
    if lo_q > hi_q:
        raise FluxError(
            FLX_5021,
            f"no value at scale {scale} lies inside [{lo}, {hi}]; widen the interval "
            "or declare a finer scale",
        )
    return lo_q, hi_q


def _clamp(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def truncated_normal(
    stream: DrawStream,
    *,
    mean: Decimal,
    stddev: Decimal,
    lo: Decimal,
    hi: Decimal,
    scale: int = 6,
) -> Decimal:
    """One draw from the normal truncated to ``[lo, hi]`` (KERNEL-SPEC §4).

    **One** ``uniform53``, mapped through the inverse CDF on the standardised bounds:
    ``u' = Φ(a) + u·(Φ(b) − Φ(a))``, ``x = mean + stddev·Φ⁻¹(u')``. The result is quantised
    to *scale* decimal places (§4: "the trait's declared scale (default 6 dp)").

    Two decisions the spec leaves to the implementation, both taken for accuracy and both
    order-preserving in ``u``:

    * When the interval lies entirely above the mean it is **mirrored** about the mean, so
      that Φ is always evaluated where it keeps its significant digits (the lower tail)
      rather than where it rounds to 1. Mirroring reverses the draw's direction twice, so
      the result is still monotone in ``u`` -- the property the "monotone in u" test pins.
    * The declared bounds are rounded *inward* onto the declared scale and the quantised
      result is clamped to them, so rounding can never push a value outside the interval
      the caller declared. The ``[lo, hi]`` invariant therefore holds of the returned
      value, not merely of the value before rounding.
    """
    if stddev < 0:
        raise FluxError(FLX_5021, f"truncated_normal stddev must be >= 0, got {stddev}")
    if lo > hi:
        raise FluxError(FLX_5021, f"truncated_normal requires lo <= hi, got [{lo}, {hi}]")

    lo_q, hi_q = _inward_bounds(lo, hi, scale)
    draw = uniform53(stream)

    if stddev == 0:
        return _clamp(_quantise(mean, scale), lo_q, hi_q)

    with localcontext(kernel_context(_WORKING_PRECISION)):
        u = _unit_interval(draw)
        a = (lo - mean) / stddev
        b = (hi - mean) / stddev
        if a > 0:
            z = -_standard_truncated(1 - u, -b, -a)
        else:
            z = _standard_truncated(u, a, b)
        value = mean + stddev * z
    return _clamp(_quantise(value, scale), lo_q, hi_q)


def _standard_truncated(u: Decimal, a: Decimal, b: Decimal) -> Decimal:
    """Φ⁻¹ of *u* mapped into ``[Φ(a), Φ(b)]``. Inside a working-precision context."""
    cdf_a = normal_cdf(a, precision=_WORKING_PRECISION)
    cdf_b = normal_cdf(b, precision=_WORKING_PRECISION)
    mass = cdf_b - cdf_a
    if mass <= 0:
        # A degenerate interval: equal bounds, or one whose mass is below the working
        # precision. The lower bound is the only defensible answer -- inventing an interior
        # value would put a number into the run digest that the declared interval does not
        # contain.
        return a
    target = cdf_a + u * mass
    if target <= 0:
        return a
    if target >= 1:
        return b
    return normal_ppf(target, precision=_WORKING_PRECISION)


def exponential(stream: DrawStream, *, rate: Decimal, scale: int = 6) -> Decimal:
    """One draw from the exponential distribution -- §4: "inverse CDF in ``decimal``".

    ``x = −ln(1 − u)/rate`` on one ``uniform53``. ``1 − u`` lies in ``(0, 1]`` because ``u``
    is at most ``(2**53 − 1)/2**53``, so the logarithm's argument is never zero and the draw
    needs no resampling.
    """
    if rate <= 0:
        raise FluxError(FLX_5021, f"exponential rate must be > 0, got {rate}")
    draw = uniform53(stream)
    with localcontext(kernel_context(_WORKING_PRECISION)):
        value = -((1 - _unit_interval(draw)).ln()) / rate
    return _quantise(value, scale)


def poisson_cap(mean: Decimal) -> int:
    """§4's fixed cap on the Poisson search: ``floor(mean + 10·sqrt(mean) + 10)``.

    Fixed and documented rather than adaptive: the bound is a function of the declared rate
    alone, so two runs of the same declaration search the same distance.
    """
    if mean < 0:
        raise FluxError(FLX_5021, f"poisson mean must be >= 0, got {mean}")
    with localcontext(kernel_context(_WORKING_PRECISION)):
        bound = mean + 10 * mean.sqrt() + 10
        return int(bound.to_integral_value(rounding=ROUND_FLOOR))


def poisson(stream: DrawStream, *, mean: Decimal) -> int:
    """One draw from the Poisson distribution -- §4: inverse CDF, "one uniform per draw".

    Sequential search over the CDF with a hard cap at :func:`poisson_cap`. The search
    consumes no further randomness, so the word cost is 1 whatever the value returned; a
    draw that reaches the cap returns the cap, which is a documented truncation of the far
    tail rather than a resample.
    """
    if mean < 0:
        raise FluxError(FLX_5021, f"poisson mean must be >= 0, got {mean}")
    draw = uniform53(stream)
    if mean == 0:
        return 0
    cap = poisson_cap(mean)
    with localcontext(kernel_context(_WORKING_PRECISION)):
        u = _unit_interval(draw)
        term = (-mean).exp()
        cumulative = term
        k = 0
        while u >= cumulative and k < cap:
            k += 1
            term = term * mean / k
            cumulative += term
        return k
