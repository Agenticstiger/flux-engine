# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Kernel ``0.2.0``'s primitives (docs/spec/KERNEL-SPEC-2.md), beside kernel ``0.1.0``'s.

Kernel ``0.1.0`` lives in :mod:`flux_engine.core.rng`, and nothing here changes it. This
module holds what kernel ``0.2.0`` replaces:

- Philox4x64-10 (section 3), in Python integers;
- :class:`CounterStream`, one path's stream: a Philox key and the path's ordinals, whose
  every sampler call takes the next draw index (section 2);
- the draws that change (section 4): ``uniform53`` from a draw's block, Lemire's bounded
  draw on the draw's own attempts, and ``poisson`` as an integer search over thresholds;
- the binary64 transforms (section 5): the truncated normal and the exponential, every
  operation one correctly rounded IEEE operation in the order the specification writes,
  and the check that refuses a process that flushes subnormal results to zero
  (``FLX-5050``);
- the words of a generated identifier (section 6.1) and of ``permute/2``'s round
  function (section 7).

The samplers of :mod:`flux_engine.core.rng` hand a draw to the stream it was asked of, so a
consumer draws from either kernel with the same calls, and the stream the seed tree gives
it decides which (:meth:`flux_engine.core.seedtree.SeedTree.stream`). That is JAX's typed
PRNG keys (JEP 9263) in miniature: the implementation travels with the key, never in a
process-wide setting. ``bernoulli``, ``categorical`` and ``permutation`` keep kernel
``0.1.0``'s code, which reaches this module through ``uniform53`` and ``bounded_uint``.

``tests/vectors/kernel2-*-vectors.json`` pin every function here, and
``tests/kernel2/test_engine_conformance.py`` holds this module to them.

Sources: Philox4x64-10 is Random123's (Salmon, Moraes, Dror and Shaw, "Parallel random
numbers: as easy as 1, 2, 3", SC11). The bounded draw is Lemire's ("Fast random integer
generation in an interval", ACM TOMACS 29(1), 2019). Φ⁻¹ is Wichura's AS241 (Applied
Statistics 37(3), 1988) in the order of CPython's pure-Python ``statistics.NormalDist``,
and the logarithm follows the method of fdlibm's ``e_log.c`` (Sun Microsystems, 1993).
"""

from __future__ import annotations

import bisect
import math
import struct
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from functools import lru_cache
from typing import Iterator, List, Optional, Sequence, Tuple

import numpy as np

from flux_engine.core import rng as _v1
from flux_engine.core.canonical import sha256_hex
from flux_engine.core.errors import FluxError, get, register
from flux_engine.core.rng import FLX_5021, DrawStream

__all__ = [
    "BOUNDED_ATTEMPTS",
    "FLX_5050",
    "IDENTIFIER_LABEL",
    "KEY_LABEL",
    "PERMUTE_LABEL",
    "SEED_TREE_VERSION",
    "CounterStream",
    "Key",
    "NormalPlan",
    "clear_memos",
    "exponential_units",
    "identifier_digits",
    "key_of",
    "log_basic",
    "normal_plan",
    "permute_round",
    "permute_round_key",
    "philox_block",
    "poisson_of",
    "poisson_thresholds",
    "ppnd16",
    "require_subnormals",
    "truncated_normal_units",
    "units_at",
]

FLX_5050 = register(
    "FLX-5050",
    "this process flushes subnormal results to zero",
    "KERNEL-SPEC-2 section 5.1: kernel 0.2.0's continuous draws are binary64 arithmetic, "
    "and their bits are the same on every machine only when every operation is the IEEE "
    "754 one. A process whose floating-point environment flushes subnormal results to "
    "zero, which some compiled libraries switch on while they run (fast-math builds, some "
    "audio and machine-learning runtimes), computes other bits. Half of the smallest "
    "normal number, computed while the run starts, came out zero here, so the run is "
    "refused before it draws anything. Run in a process that has not loaded such a "
    "library, or plan the run under kernel 0.1.0, whose continuous draws are decimal.",
)

#: KERNEL-SPEC-2 section 1: the seed tree kernel ``0.2.0`` derives its streams with.
SEED_TREE_VERSION = "flux-seed/2"
#: Section 6.1's domain label for a generated identifier's key.
IDENTIFIER_LABEL = b"flux-gen/2"
#: Section 8's domain label for a key layout's or a relationship's key.
KEY_LABEL = b"flux-key/2"
#: Section 7's domain label for ``permute/2``'s round key.
PERMUTE_LABEL = b"flux-permute/2"

_NUL = b"\x00"
_WORD_MASK = (1 << 64) - 1
_MEMO_SIZE = 1024

#: A Philox key: two 64-bit words, ``(k0, k1)``.
Key = Tuple[int, int]
Block = Tuple[int, int, int, int]
#: A key's ten round keys, the Weyl sequence Random123 steps between rounds.
Schedule = Tuple[Tuple[int, int], ...]


# ---------------------------------------------------------------------------
# Philox4x64-10 (section 3)
# ---------------------------------------------------------------------------

_M0 = 0xD2E7470EE14C6C93
_M1 = 0xCA5A826395121157
_W0 = 0x9E3779B97F4A7C15
_W1 = 0xBB67AE8584CAA73B
_ROUNDS = 10


@lru_cache(maxsize=_MEMO_SIZE)
def _schedule(key: Key) -> Schedule:
    """The key of each of the ten rounds: ``k + i * W`` (mod 2**64) for round ``i``."""
    k0, k1 = key
    return tuple(((k0 + i * _W0) & _WORD_MASK, (k1 + i * _W1) & _WORD_MASK) for i in range(_ROUNDS))


def _block(c0: int, c1: int, c2: int, c3: int, schedule: Schedule) -> Block:
    """Ten Philox rounds on one counter. Every word is already in ``[0, 2**64)``."""
    for k0, k1 in schedule:
        product0 = _M0 * c0
        product1 = _M1 * c2
        c0, c1, c2, c3 = (
            (product1 >> 64) ^ c1 ^ k0,
            product1 & _WORD_MASK,
            (product0 >> 64) ^ c3 ^ k1,
            product0 & _WORD_MASK,
        )
    return c0, c1, c2, c3


def philox_block(counter: Sequence[int], key: Key) -> Block:
    """Philox4x64-10 of one counter under one key: Random123's function, word 0 first."""
    words = tuple(int(word) for word in counter)
    if len(words) != 4 or not all(0 <= word <= _WORD_MASK for word in (*words, *key)):
        raise FluxError(FLX_5021, "a Philox counter is four 64-bit words and a key two")
    return _block(words[0], words[1], words[2], words[3], _schedule(key))


def key_of(material: bytes) -> Key:
    """The first 16 bytes of ``sha256(material)`` as a key, each word big-endian."""
    digest = sha256_hex(material)
    return int(digest[0:16], 16), int(digest[16:32], 16)


# ---------------------------------------------------------------------------
# One path's stream (section 2)
# ---------------------------------------------------------------------------

#: A bounded draw's attempts, each with a counter of its own (section 4).
BOUNDED_ATTEMPTS = 64


class CounterStream(DrawStream):
    """One path's stream under kernel ``0.2.0``: its key and its two ordinals.

    The counter of draw ``d`` at attempt ``a`` is ``(o1, o2, d, a)``, and :attr:`position`
    is the number of draws taken so far, which is the next draw's index. A draw is taken by
    a sampler call, never by a word: there is no word sequence to read, so
    :meth:`next_raw` refuses. :func:`flux_engine.core.seedtree.counter_stream` builds one
    from a path, which is where the key label and the ordinals are worked out.
    """

    __slots__ = ("_schedule", "_first", "_second")

    #: The samplers of :mod:`flux_engine.core.rng` read this to hand a draw over.
    counter_based = True

    def __init__(self, key: Key, ordinals: Tuple[int, int], *, label: str = "") -> None:
        # DrawStream's numpy generator is not built: this stream has none.
        first, second = ordinals
        self._schedule = _schedule(key)
        self._first = first
        self._second = second
        self._position = 0
        self.label = label

    def block(self, draw: int, attempt: int = 0) -> Block:
        """The block of draw *draw* at attempt *attempt*, which takes no draw index."""
        return _block(self._first, self._second, draw, attempt, self._schedule)

    def _take(self) -> int:
        draw = self._position
        if draw > _WORD_MASK:
            # Section 2: a draw index that does not fit in 64 bits is refused. The seed
            # tree registers the code, and every counter stream comes from the seed tree.
            raise FluxError(get("FLX-2202"), f"{self.label}: a 2**64th draw on one path")
        self._position = draw + 1
        return draw

    def next_raw(self) -> int:
        raise TypeError(
            "a kernel 0.2.0 stream has no sequence of words: each sampler call takes one "
            "draw index (KERNEL-SPEC-2 section 2)"
        )

    def next_raw_words(self, count: int) -> List[int]:
        return [self.next_raw() for _ in range(count)]

    def uniform53(self) -> int:
        """Section 4: the top 53 bits of word 0 of the next draw's block, at attempt 0."""
        return self.block(self._take())[0] >> 11

    def bounded(self, n: int) -> int:
        """Section 4: ``[0, n)`` by Lemire's test on the next draw's attempts, at most 64.

        The first attempt whose low product clears ``2**64 mod n`` decides; if all 64 are
        rejected, the last attempt's word modulo *n* is the result, kernel ``0.1.0``'s
        documented fallback. No other draw's words depend on how many attempts it took.
        """
        if n > 1 << 64:
            raise FluxError(FLX_5021, f"a bounded draw needs n <= 2**64, got {n}")
        draw = self._take()
        threshold = (1 << 64) % n
        word = 0
        for attempt in range(BOUNDED_ATTEMPTS):
            word = self.block(draw, attempt)[0]
            product = word * n
            if product & _WORD_MASK >= threshold:
                return product >> 64
        return word % n

    # The three transforms take the uniform53 their sampler in flux_engine.core.rng drew,
    # after that sampler's own refusals (KERNEL-SPEC §4's, which hold under 0.2.0).

    def binary64_truncated_normal(
        self, u53: int, *, mean: Decimal, stddev: Decimal, lo: Decimal, hi: Decimal, scale: int
    ) -> Decimal:
        """Section 5.3 on *u53*, for a positive *stddev*, as a decimal at *scale*."""
        return units_at(
            truncated_normal_units(u53, normal_plan(mean, stddev, lo, hi, scale)), scale
        )

    def binary64_exponential(self, u53: int, *, rate: Decimal, scale: int) -> Decimal:
        """Section 5.4 on *u53*, as a decimal at *scale*."""
        return units_at(exponential_units(u53, rate, scale), scale)

    def threshold_poisson(self, u53: int, *, mean: Decimal) -> int:
        """Section 4's integer search on *u53*, for a positive *mean*."""
        return poisson_of(u53, mean)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CounterStream(label={self.label!r}, position={self._position})"


# ---------------------------------------------------------------------------
# poisson (section 4)
# ---------------------------------------------------------------------------


#: A Poisson table of a large mean holds about 17 square roots of the mean in thresholds;
#: a run declares few Poisson means, so few tables are kept.
_POISSON_TABLES = 64


def _require_mean(mean: Decimal) -> None:
    if not mean.is_finite() or mean < 0:
        raise FluxError(FLX_5021, f"poisson mean must be a finite number >= 0, got {mean}")


def _thresholds(mean: Decimal) -> Iterator[int]:
    """``t_0, t_1, ...`` up to ``t_{cap-1}``, one at a time, for a positive *mean*.

    Kernel ``0.1.0``'s sums, by its recurrence, in ``decimal`` at 50 digits rounding
    half-even; ``cap`` is its ``poisson_cap``. A sum holds at most 50 digits and ``2**53``
    sixteen, so ``c_k * 2**53`` is exact at 70 digits and its ceiling is ``t_k``. The
    arithmetic goes through context objects of its own, so nothing here changes the
    context of the code that reads the thresholds, and no sum is kept once its threshold
    is out.
    """
    cap = _v1.poisson_cap(mean)
    sums = _v1._pinned(_v1._WORKING_PRECISION).copy()
    ceiling = _v1._pinned(_v1._WORKING_PRECISION + 20).copy()
    ceiling.rounding = ROUND_CEILING
    term = sums.exp(sums.minus(mean))
    cumulative = term
    for k in range(cap):
        if k:
            term = sums.divide(sums.multiply(term, mean), k)
            cumulative = sums.add(cumulative, term)
        yield int(
            ceiling.to_integral_value(ceiling.multiply(cumulative, _v1.UNIFORM53_DENOMINATOR))
        )


def poisson_thresholds(mean: Decimal) -> Tuple[int, ...]:
    """Section 4's thresholds ``t_k = ceil(c_k * 2**53)`` for ``k = 0 .. cap - 1``, every one.

    For the vector files and the tests. A draw reads :func:`_poisson_table`, which holds
    the same thresholds without the runs at either end that no draw can tell apart.
    """
    _require_mean(mean)
    if mean == 0:
        return ()
    return tuple(_thresholds(mean))


@dataclass(frozen=True)
class _PoissonTable:
    """The thresholds of one mean, as much of them as a draw needs (section 4).

    Every ``t_k`` with ``k < start`` is 0 or 1, and ``first_positive`` is the first that is
    not 0. ``window`` holds ``t_start`` up to the last threshold below ``2**53``, and
    ``stop`` is the first ``k`` past it whose threshold is at least ``2**53``, or the cap
    when there is none. No uniform53 reaches ``2**53``, so the thresholds after ``stop``
    decide nothing; and for a large mean all but about 17 square roots of the mean lie
    outside the window, so the table's size follows the spread of the distribution, not
    its mean.
    """

    first_positive: int
    start: int
    window: Tuple[int, ...]
    stop: int


@lru_cache(maxsize=_POISSON_TABLES)
def _poisson_table(mean: Decimal) -> _PoissonTable:
    cap = _v1.poisson_cap(mean)
    first_positive: Optional[int] = None
    start: Optional[int] = None
    window: List[int] = []
    stop = cap
    for k, threshold in enumerate(_thresholds(mean)):
        if first_positive is None and threshold > 0:
            first_positive = k
        if start is None:
            if threshold <= 1:
                continue
            start = k
        if threshold >= _v1.UNIFORM53_DENOMINATOR:
            stop = k
            break
        window.append(threshold)
    return _PoissonTable(
        first_positive=cap if first_positive is None else first_positive,
        start=cap if start is None else start,
        window=tuple(window),
        stop=stop,
    )


def poisson_of(u53: int, mean: Decimal) -> int:
    """Section 4's draw: the smallest ``k`` with ``u53 < t_k``, or the cap when there is none."""
    _require_mean(mean)
    if mean == 0:
        return 0
    table = _poisson_table(mean)
    if u53 == 0:
        return table.first_positive
    # A uniform53 of at least 1 clears every threshold before the window, and none reaches
    # the threshold at stop.
    index = bisect.bisect_right(table.window, u53)
    if index < len(table.window):
        return table.start + index
    return table.stop


# ---------------------------------------------------------------------------
# binary64 (section 5)
# ---------------------------------------------------------------------------

_TWO_POW_MINUS_53 = 1.0 / (1 << 53)
_PRECISION = 50
_SMALLEST_NORMAL = Decimal(2) ** -1022
_SMALLEST_MASS = Decimal(2) ** -969
_LARGEST_UNITS = 1 << 52
#: The magnitudes a declaration's binary64 inputs may take (a mean may also be zero). With
#: these and the bounds above, every intermediate of section 5 is zero or a normal number.
_SMALLEST_INPUT = math.ldexp(1.0, -900)
_LARGEST_INPUT = math.ldexp(1.0, 900)
#: 10**22 is the largest power of ten binary64 holds exactly.
_LARGEST_SCALE = 22


def _bits(pattern: str) -> float:
    """The binary64 value whose IEEE 754 bit pattern is the 16 hex digits *pattern*."""
    value: float = struct.unpack(">d", bytes.fromhex(pattern))[0]
    return value


# fdlibm e_log.c: ln 2 split in two, and the coefficients of its polynomial in s.
_LN2_HI = _bits("3fe62e42fee00000")
_LN2_LO = _bits("3dea39ef35793c76")
_LG1 = _bits("3fe5555555555593")
_LG2 = _bits("3fd999999997fa04")
_LG3 = _bits("3fd2492494229359")
_LG4 = _bits("3fcc71c51d8e78af")
_LG5 = _bits("3fc7466496cb03de")
_LG6 = _bits("3fc39a09d078c69f")
_LG7 = _bits("3fc2f112df3e5244")
_SQRT_HALF = math.sqrt(0.5)

# AS241's PPND16 coefficients, highest power first, as CPython's statistics module holds them.
_CENTRAL_NUM = (
    2.50908_09287_30122_6727e3, 3.34305_75583_58812_8105e4, 6.72657_70927_00870_0853e4,
    4.59219_53931_54987_1457e4, 1.37316_93765_50946_1125e4, 1.97159_09503_06551_4427e3,
    1.33141_66789_17843_7745e2, 3.38713_28727_96366_6080e0,
)  # fmt: skip
_CENTRAL_DEN = (
    5.22649_52788_52854_5610e3, 2.87290_85735_72194_2674e4, 3.93078_95800_09271_0610e4,
    2.12137_94301_58659_5867e4, 5.39419_60214_24751_1077e3, 6.87187_00749_20579_0830e2,
    4.23133_30701_60091_1252e1, 1.0,
)  # fmt: skip
_NEAR_NUM = (
    7.74545_01427_83414_07640e-4, 2.27238_44989_26918_45833e-2, 2.41780_72517_74506_11770e-1,
    1.27045_82524_52368_38258e0, 3.64784_83247_63204_60504e0, 5.76949_72214_60691_40550e0,
    4.63033_78461_56545_29590e0, 1.42343_71107_49683_57734e0,
)  # fmt: skip
_NEAR_DEN = (
    1.05075_00716_44416_84324e-9, 5.47593_80849_95344_94600e-4, 1.51986_66563_61645_71966e-2,
    1.48103_97642_74800_74590e-1, 6.89767_33498_51000_04550e-1, 1.67638_48301_83803_84940e0,
    2.05319_16266_37758_82187e0, 1.0,
)  # fmt: skip
_FAR_NUM = (
    2.01033_43992_92288_13265e-7, 2.71155_55687_43487_57815e-5, 1.24266_09473_88078_43860e-3,
    2.65321_89526_57612_30930e-2, 2.96560_57182_85048_91230e-1, 1.78482_65399_17291_33580e0,
    5.46378_49111_64114_36990e0, 6.65790_46435_01103_77720e0,
)  # fmt: skip
_FAR_DEN = (
    2.04426_31033_89939_78564e-15, 1.42151_17583_16445_88870e-7, 1.84631_83175_10054_68180e-5,
    7.86869_13114_56132_59100e-4, 1.48753_61290_85061_48525e-2, 1.36929_88092_27358_05310e-1,
    5.99832_20655_58879_37690e-1, 1.0,
)  # fmt: skip


def _horner(coefficients: Sequence[float], x: float) -> float:
    total = coefficients[0]
    for coefficient in coefficients[1:]:
        total = total * x + coefficient
    return total


def log_basic(x: float) -> float:
    """``ln x`` for a positive normal *x*: fdlibm's reduction to ``[√2/2, √2)`` and polynomial.

    Section 5.2. Every step is one binary64 operation; CPython never fuses two of them.
    """
    mantissa, exponent = math.frexp(x)
    if mantissa < _SQRT_HALF:
        mantissa *= 2.0
        exponent -= 1
    k = float(exponent)
    f = mantissa - 1.0
    s = f / (2.0 + f)
    z = s * s
    w = z * z
    t1 = w * (_LG2 + w * (_LG4 + w * _LG6))
    t2 = z * (_LG1 + w * (_LG3 + w * (_LG5 + w * _LG7)))
    r = t2 + t1
    hfsq = 0.5 * f * f
    return k * _LN2_HI - ((hfsq - (s * (hfsq + r) + k * _LN2_LO)) - f)


def ppnd16(p: float) -> float:
    """Φ⁻¹(p) for ``0 < p < 1``: AS241's PPND16 in the order of CPython's pure-Python copy.

    Section 5.2. CPython's C copy of the same formula differs in the last bits, because its
    compiler fuses each multiply and add of the Horner steps into one instruction.
    """
    q = p - 0.5
    if abs(q) <= 0.425:
        r = 0.180625 - q * q
        return _horner(_CENTRAL_NUM, r) * q / _horner(_CENTRAL_DEN, r)
    t = math.sqrt(-log_basic(p if q <= 0.0 else 1.0 - p))
    if t <= 5.0:
        t -= 1.6
        x = _horner(_NEAR_NUM, t) / _horner(_NEAR_DEN, t)
    else:
        t -= 5.0
        x = _horner(_FAR_NUM, t) / _horner(_FAR_DEN, t)
    return -x if q < 0.0 else x


def require_subnormals() -> None:
    """Refuse (``FLX-5050``) a process that flushes subnormal results to zero.

    Section 5.1: half of ``2**-1022``, computed now rather than folded at compile time,
    must not be zero, in plain binary64 arithmetic and on numpy's array path. numpy takes
    its own subnormal constants from compiled tables, so only arithmetic shows a mode a
    library switched on while the process ran.
    """
    smallest_normal = math.ldexp(1.0, -1022)
    halved = np.array([smallest_normal]) / 2.0
    if smallest_normal / 2.0 == 0.0 or halved[0] == 0.0:
        raise FluxError(
            FLX_5050,
            "half of the smallest normal binary64 number is zero in this process",
        )


def _input_ok(value: float, *, zero_allowed: bool = False) -> bool:
    if value == 0.0:
        return zero_allowed
    return _SMALLEST_INPUT <= abs(value) <= _LARGEST_INPUT


def _refused(what: str) -> FluxError:
    return FluxError(FLX_5021, f"{what}, which kernel 0.2.0's binary64 draws cannot hold")


@dataclass(frozen=True)
class NormalPlan:
    """What a bounded-normal declaration fixes before any draw (section 5.3).

    The interval is mirrored when it lies above the mean, as kernel ``0.1.0`` mirrors it.
    The decimal values are kernel ``0.1.0``'s, at 50 digits; the binary64 ones are the same
    values rounded once, so no draw converts anything. ``lo_units`` and ``hi_units`` are
    the declared bounds rounded inward onto the declared scale.
    """

    mirror: bool
    degenerate: bool
    lo_units: int
    hi_units: int
    a: float
    b: float
    cdf_a: float
    sf_b: float
    mass: float
    mean: float
    stddev: float
    unit: float


def normal_plan(mean: Decimal, stddev: Decimal, lo: Decimal, hi: Decimal, scale: int) -> NormalPlan:
    """The plan of a bounded normal with a positive *stddev*, or its refusal (``FLX-5021``).

    Kernel ``0.1.0``'s refusals have already run (:func:`flux_engine.core.rng.truncated_normal`
    checks them first); these are the ones section 5.3 adds, stricter than kernel
    ``0.1.0``'s: a scale above 22, an interval holding less than ``2**-969`` of its
    distribution, bounds beyond ``2**52`` units, and a standard deviation, or a mean other
    than zero, whose magnitude lies outside ``[2**-900, 2**900]``.
    """
    if not all(value.is_finite() for value in (mean, stddev, lo, hi)):
        raise _refused(f"a declaration ({mean}, {stddev}, [{lo}, {hi}]) that is not finite")
    if not 0 <= scale <= _LARGEST_SCALE:
        raise _refused(f"a scale of {scale}, outside [0, 22]")
    return _normal_plan_of(mean, stddev, lo, hi, scale)


@lru_cache(maxsize=_MEMO_SIZE)
def _normal_plan_of(
    mean: Decimal, stddev: Decimal, lo: Decimal, hi: Decimal, scale: int
) -> NormalPlan:
    # Once per declaration and process, before its first draw (section 5.1).
    require_subnormals()
    try:
        return _normal_plan(mean, stddev, lo, hi, scale)
    except InvalidOperation:
        raise _refused(
            f"an interval [{lo}, {hi}] whose bounds do not fit at scale {scale}"
        ) from None


def _normal_plan(
    mean: Decimal, stddev: Decimal, lo: Decimal, hi: Decimal, scale: int
) -> NormalPlan:
    with localcontext(_v1._pinned(_PRECISION)):
        a = (lo - mean) / stddev
        b = (hi - mean) / stddev
        mirror = a > 0
        if mirror:
            a, b = -b, -a
        cdf_a = _v1.normal_cdf(a, precision=_PRECISION)
        sf_b = _v1.normal_cdf(-b, precision=_PRECISION)
        mass = _v1.normal_cdf(b, precision=_PRECISION) - cdf_a
        if 0 < mass < _SMALLEST_MASS:
            raise _refused(f"an interval [{lo}, {hi}] holding {mass:.3e} of its distribution")
        cdf_a = cdf_a if cdf_a >= _SMALLEST_NORMAL else Decimal(0)
        sf_b = sf_b if sf_b >= _SMALLEST_NORMAL else Decimal(0)
        step = Decimal(1).scaleb(-scale)
        lo_units = int(lo.quantize(step, rounding=ROUND_CEILING).scaleb(scale))
        hi_units = int(hi.quantize(step, rounding=ROUND_FLOOR).scaleb(scale))
    if lo_units > hi_units:
        raise _refused(f"an interval [{lo}, {hi}] holding no value at scale {scale}")
    if max(abs(lo_units), abs(hi_units)) > _LARGEST_UNITS:
        raise _refused(f"an interval [{lo}, {hi}] whose units at scale {scale} exceed 2**52")
    if not _input_ok(float(stddev)):
        raise _refused(f"a stddev of {stddev}, outside [2**-900, 2**900]")
    if not _input_ok(float(mean), zero_allowed=True):
        raise _refused(f"a mean of {mean}, not zero and outside [2**-900, 2**900]")
    return NormalPlan(
        mirror=mirror,
        degenerate=mass <= 0,
        lo_units=lo_units,
        hi_units=hi_units,
        a=float(a),
        b=float(b),
        cdf_a=float(cdf_a),
        sf_b=float(sf_b),
        mass=float(mass),
        mean=float(mean),
        stddev=float(stddev),
        unit=float(10**scale),
    )


def truncated_normal_units(u53: int, plan: NormalPlan) -> int:
    """Section 5.3: the draw on *u53*, in units of the declared scale.

    Each tail's probability is formed from that tail's own mass, so neither is 1 minus a
    number close to 1. A draw at an end of the interval (no mass, or a tail probability of
    zero) takes that end's units whole, with no binary64 arithmetic.
    """
    lower_end, upper_end = (
        (plan.hi_units, plan.lo_units) if plan.mirror else (plan.lo_units, plan.hi_units)
    )
    if plan.degenerate:
        return lower_end
    u = float(u53) * _TWO_POW_MINUS_53
    if plan.mirror:
        u = 1.0 - u
    lower = plan.cdf_a + u * plan.mass
    if lower <= 0.5:
        if lower <= 0.0:
            return lower_end
        z = ppnd16(lower)
    else:
        upper = plan.sf_b + (1.0 - u) * plan.mass
        if upper <= 0.0:
            return upper_end
        z = -ppnd16(upper)
    if plan.mirror:
        z = -z
    value = plan.mean + plan.stddev * z
    return min(max(round(value * plan.unit), plan.lo_units), plan.hi_units)


def _exponential_plan(rate: Decimal, scale: int) -> Tuple[float, float]:
    if not rate.is_finite() or rate <= 0:
        raise _refused(f"a rate of {rate}")
    if not 0 <= scale <= _LARGEST_SCALE:
        raise _refused(f"a scale of {scale}, outside [0, 22]")
    return _exponential_plan_of(rate, scale)


@lru_cache(maxsize=_MEMO_SIZE)
def _exponential_plan_of(rate: Decimal, scale: int) -> Tuple[float, float]:
    # Once per declaration and process, before its first draw (section 5.1).
    require_subnormals()
    rate_f = float(rate)
    unit_f = float(10**scale)
    if not _input_ok(rate_f):
        raise _refused(f"a rate of {rate}, outside [2**-900, 2**900]")
    if (37.0 / rate_f) * unit_f > _LARGEST_UNITS:
        raise _refused(f"a rate of {rate}, too small for scale {scale}")
    return rate_f, unit_f


def exponential_units(u53: int, rate: Decimal, scale: int) -> int:
    """Section 5.4: ``-log(1 - u) / rate`` in binary64, in units of the declared scale."""
    rate_f, unit_f = _exponential_plan(rate, scale)
    u = float(u53) * _TWO_POW_MINUS_53
    x = -log_basic(1.0 - u) / rate_f
    return round(x * unit_f)


def units_at(units: int, scale: int) -> Decimal:
    """*units* of ``10**-scale`` as a decimal at that scale, as a row holds a drawn value."""
    with localcontext(_v1._pinned(_PRECISION)):
        return Decimal(units).scaleb(-scale)


def column_units_at(units: Sequence[int], scale: int) -> List[Decimal]:
    """:func:`units_at` of each of *units*: the same operation, in one pinned context."""
    with localcontext(_v1._pinned(_PRECISION)):
        return [Decimal(value).scaleb(-scale) for value in units]


# ---------------------------------------------------------------------------
# Identifiers (section 6.1)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=_MEMO_SIZE)
def _identifier_key(seam: bytes, key_label: bytes) -> Key:
    return key_of(IDENTIFIER_LABEL + _NUL + seam + _NUL + key_label)


def identifier_digits(seam: bytes, key_label: bytes, row: int) -> str:
    """The 64 hex digits of row *row*'s identifier in the column whose key label is *key_label*.

    The key reads ``H`` and the column's path, never the seed, and the counter is
    ``(row, 0, 0, 0)``: the four words of the block, each as 16 lowercase hex digits.
    """
    words = _block(row, 0, 0, 0, _schedule(_identifier_key(seam, key_label)))
    return "".join(f"{word:016x}" for word in words)


# ---------------------------------------------------------------------------
# permute/2's round function (section 7)
# ---------------------------------------------------------------------------


def permute_round_key(key: bytes, domain: int) -> Key:
    """``roundKey = sha256("flux-permute/2" NUL key NUL domain)[0:16]``, the domain in 33 bytes."""
    return key_of(PERMUTE_LABEL + _NUL + key + _NUL + domain.to_bytes(33, "big"))


def permute_round(round_key: Key, round_index: int, half: int) -> int:
    """``F(i, y)``: words 0 and 1 of the block of ``(y mod 2**64, y >> 64, i, 0)``.

    A half of a domain of at most ``2**256`` holds at most 128 bits, so it fits the
    counter's first two words.
    """
    words = _block(half & _WORD_MASK, half >> 64, round_index, 0, _schedule(round_key))
    return words[0] | (words[1] << 64)


# ---------------------------------------------------------------------------
# A column at a time (ADR 0010, step 4)
# ---------------------------------------------------------------------------
#
# The functions above take one draw of one cell. These take the same draw for every row of a
# column at once, over numpy uint64 and float64 arrays: the counter of row ``r``'s draw is
# ``(r, 0, draw, attempt)`` under the column's key, so a slice's words are one Philox call.
# Every result is the scalar function's for each row, bit for bit
# (tests/kernel2/test_columns.py holds each to its scalar twin). The binary64 arithmetic is
# one numpy ufunc per IEEE operation, in the order section 5 writes, so nothing is fused.

_LOW32 = np.uint64(0xFFFFFFFF)
_SHIFT32 = np.uint64(32)


def _mulhilo(a: "np.ndarray", m: int) -> Tuple["np.ndarray", "np.ndarray"]:
    """The high and low words of ``a * m`` for uint64 *a* and a 64-bit *m*, by 32-bit halves."""
    m_lo = np.uint64(m & 0xFFFFFFFF)
    m_hi = np.uint64(m >> 32)
    a_lo = a & _LOW32
    a_hi = a >> _SHIFT32
    low_low = a_lo * m_lo
    high_low = a_hi * m_lo
    low_high = a_lo * m_hi
    cross = (low_low >> _SHIFT32) + (high_low & _LOW32) + (low_high & _LOW32)
    high = a_hi * m_hi + (high_low >> _SHIFT32) + (low_high >> _SHIFT32) + (cross >> _SHIFT32)
    return high, a * np.uint64(m)


def philox_arrays(
    c0: "np.ndarray", c1: "np.ndarray", c2: "np.ndarray", c3: "np.ndarray", key: Key
) -> Tuple["np.ndarray", "np.ndarray", "np.ndarray", "np.ndarray"]:
    """Philox4x64-10 of every counter ``(c0[i], c1[i], c2[i], c3[i])`` under *key*, as arrays."""
    for k0, k1 in _schedule(key):
        high0, low0 = _mulhilo(c0, _M0)
        high1, low1 = _mulhilo(c2, _M1)
        c0, c1, c2, c3 = high1 ^ c1 ^ np.uint64(k0), low1, high0 ^ c3 ^ np.uint64(k1), low0
    return c0, c1, c2, c3


def _counters(
    rows: "np.ndarray", second: int, draw: int, attempt: int
) -> Tuple["np.ndarray", "np.ndarray", "np.ndarray", "np.ndarray"]:
    size = rows.shape[0]
    return (
        rows.astype(np.uint64, copy=False),
        np.full(size, second, dtype=np.uint64),
        np.full(size, draw, dtype=np.uint64),
        np.full(size, attempt, dtype=np.uint64),
    )


def column_words(key: Key, rows: "np.ndarray", draw: int, attempt: int = 0) -> "np.ndarray":
    """Word 0 of draw *draw*'s block at *attempt*, for each row of a column keyed *key*."""
    return philox_arrays(*_counters(rows, 0, draw, attempt), key)[0]


def column_uniform53(key: Key, rows: "np.ndarray", draw: int) -> "np.ndarray":
    """Section 4's ``uniform53`` of draw *draw* for each row: word 0 at attempt 0, its top 53 bits."""
    return column_words(key, rows, draw) >> np.uint64(11)


def column_bounded(key: Key, rows: "np.ndarray", draw: int, n: int) -> "np.ndarray":
    """:meth:`CounterStream.bounded` of draw *draw* for each row, the 64 attempts as masks.

    Each row's first attempt whose low product clears ``2**64 mod n`` decides; a row whose
    64 attempts are all rejected takes its last word modulo *n*. Only the rows still
    pending are drawn again, so an attempt costs nothing for the rows it does not reach.
    """
    if not 1 <= n <= 1 << 64:
        raise FluxError(FLX_5021, f"a bounded draw needs 1 <= n <= 2**64, got {n}")
    if n == 1 << 64:
        # The threshold is 0 and the high word of word * 2**64 is the word itself.
        return column_words(key, rows, draw)
    threshold = np.uint64((1 << 64) % n)
    result = np.zeros(rows.shape[0], dtype=np.uint64)
    pending = np.arange(rows.shape[0])
    words = np.zeros(0, dtype=np.uint64)
    for attempt in range(BOUNDED_ATTEMPTS):
        words = column_words(key, rows[pending], draw, attempt)
        high, low = _mulhilo(words, n)
        accepted = low >= threshold
        result[pending[accepted]] = high[accepted]
        rejected = ~accepted
        pending, words = pending[rejected], words[rejected]
        if pending.shape[0] == 0:
            return result
    result[pending] = words % np.uint64(n)
    return result


def column_bernoulli(u53: "np.ndarray", p: Decimal) -> "np.ndarray":
    """Section 4's ``bernoulli`` on each uniform53: kernel 0.1.0's exact integer comparison."""
    _v1._require_probability(p, "bernoulli p")
    return u53 < np.uint64(_v1._probability_threshold(p))


def column_categorical(u53: "np.ndarray", levels: Sequence[Tuple[str, Decimal]]) -> List[str]:
    """Section 4's ``categorical`` on each uniform53, by one sorted search of v1's thresholds."""
    if not levels:
        raise FluxError(FLX_5021, "categorical levels must not be empty")
    thresholds, last = _v1._categorical_table(tuple(levels))
    bounds = np.array([threshold for threshold, _key in thresholds], dtype=np.uint64)
    names = [key for _threshold, key in thresholds] + [last]
    return [names[index] for index in np.searchsorted(bounds, u53, side="right").tolist()]


def column_poisson(u53: "np.ndarray", mean: Decimal) -> "np.ndarray":
    """:func:`poisson_of` for each uniform53, by one sorted search of the table's window."""
    _require_mean(mean)
    if mean == 0:
        return np.zeros(u53.shape[0], dtype=np.int64)
    table = _poisson_table(mean)
    window = np.array(table.window, dtype=np.uint64)
    index = np.searchsorted(window, u53, side="right").astype(np.int64)
    drawn = np.where(index < window.shape[0], table.start + index, table.stop)
    return np.where(u53 == 0, table.first_positive, drawn).astype(np.int64)


def _horner_array(coefficients: Sequence[float], x: "np.ndarray") -> "np.ndarray":
    total = np.full_like(x, coefficients[0])
    for coefficient in coefficients[1:]:
        total = total * x + coefficient
    return total


def log_basic_array(x: "np.ndarray") -> "np.ndarray":
    """:func:`log_basic` for each element: the same operations, one ufunc each."""
    mantissa, exponent = np.frexp(x)
    low = mantissa < _SQRT_HALF
    m = np.where(low, mantissa * 2.0, mantissa)
    k = (exponent - low).astype(np.float64)
    f = m - 1.0
    s = f / (2.0 + f)
    z = s * s
    w = z * z
    t1 = w * (_LG2 + w * (_LG4 + w * _LG6))
    t2 = z * (_LG1 + w * (_LG3 + w * (_LG5 + w * _LG7)))
    r = t2 + t1
    hfsq = 0.5 * f * f
    result: "np.ndarray" = k * _LN2_HI - ((hfsq - (s * (hfsq + r) + k * _LN2_LO)) - f)
    return result


def _ppnd16_array(p: "np.ndarray") -> "np.ndarray":
    """:func:`ppnd16` for each element in ``(0, 1)``: every branch computed, the right one kept."""
    q = p - 0.5
    r = 0.180625 - q * q
    central = _horner_array(_CENTRAL_NUM, r) * q / _horner_array(_CENTRAL_DEN, r)
    t = np.sqrt(-log_basic_array(np.where(q <= 0.0, p, 1.0 - p)))
    near = t - 1.6
    far = t - 5.0
    tail = np.where(
        t <= 5.0,
        _horner_array(_NEAR_NUM, near) / _horner_array(_NEAR_DEN, near),
        _horner_array(_FAR_NUM, far) / _horner_array(_FAR_DEN, far),
    )
    tail = np.where(q < 0.0, -tail, tail)
    result: "np.ndarray" = np.where(np.abs(q) <= 0.425, central, tail)
    return result


def column_truncated_normal_units(u53: "np.ndarray", plan: NormalPlan) -> "np.ndarray":
    """:func:`truncated_normal_units` for each uniform53, as int64 units of the declared scale."""
    lower_end, upper_end = (
        (plan.hi_units, plan.lo_units) if plan.mirror else (plan.lo_units, plan.hi_units)
    )
    if plan.degenerate:
        return np.full(u53.shape[0], lower_end, dtype=np.int64)
    u = u53.astype(np.float64) * _TWO_POW_MINUS_53
    if plan.mirror:
        u = 1.0 - u
    lower = plan.cdf_a + u * plan.mass
    upper = plan.sf_b + (1.0 - u) * plan.mass
    use_lower = lower <= 0.5
    p = np.where(use_lower, lower, upper)
    at_lower = use_lower & (p <= 0.0)
    at_upper = ~use_lower & (p <= 0.0)
    inside = ~(at_lower | at_upper)
    z = _ppnd16_array(np.where(inside, p, 0.5))
    z = np.where(use_lower, z, -z)
    z = np.where(inside, z, 0.0)
    if plan.mirror:
        z = -z
    value = plan.mean + plan.stddev * z
    units = np.clip(np.rint(value * plan.unit), plan.lo_units, plan.hi_units).astype(np.int64)
    result: "np.ndarray" = np.where(at_lower, lower_end, np.where(at_upper, upper_end, units))
    return result


def column_exponential_units(u53: "np.ndarray", rate: Decimal, scale: int) -> "np.ndarray":
    """:func:`exponential_units` for each uniform53, as int64 units of the declared scale."""
    rate_f, unit_f = _exponential_plan(rate, scale)
    u = u53.astype(np.float64) * _TWO_POW_MINUS_53
    x = -log_basic_array(1.0 - u) / rate_f
    result: "np.ndarray" = np.rint(x * unit_f).astype(np.int64)
    return result


def column_identifier_digits(seam: bytes, key_label: bytes, rows: "np.ndarray") -> List[str]:
    """:func:`identifier_digits` for each row of one column: one Philox call for all of them."""
    size = rows.shape[0]
    zeros = np.zeros(size, dtype=np.uint64)
    words = philox_arrays(
        rows.astype(np.uint64, copy=False), zeros, zeros, zeros, _identifier_key(seam, key_label)
    )
    return [
        f"{w0:016x}{w1:016x}{w2:016x}{w3:016x}"
        for w0, w1, w2, w3 in zip(*(word.tolist() for word in words), strict=True)
    ]


#: A domain whose two halves each fit one counter word: then only word 0 of a round's block
#: reaches the half it is added to (``(half + w0 + w1 * 2**64) mod 2**bits`` with ``bits <= 64``).
PERMUTE_ARRAY_MAX_DOMAIN = 1 << 128

#: :data:`flux_engine.core.permute.PERMUTE_KEY_BYTES` (permute imports this module).
_PERMUTE_KEY_BYTES = 32

#: The rows an array of rows holds: indexes below ``2**63``, as numpy's int64 ``arange``
#: makes them. A row at or past it is left to the per-row code.
ARRAY_ROWS = 1 << 63


def permute2_array(key: bytes, domain: int, indexes: "np.ndarray") -> List[int]:
    """``permute/2`` of every index in *indexes*, for a domain of at most ``2**128``.

    :func:`flux_engine.core.permute.permute2`'s rounds and cycle walking on uint64 arrays.
    A value is held as its two halves, each of which fits a word, so a round adds the
    block's word 0 to one half and masks it, and only the values that walked out of the
    domain take another pass. The images come back as Python integers, in order.
    """
    if not 1 <= domain <= PERMUTE_ARRAY_MAX_DOMAIN:
        raise FluxError(
            FLX_5021, f"an array permutation covers at most 2**128 values, got {domain}"
        )
    size = indexes.shape[0]
    # permute2's own refusals (flux_engine.core.permute._check), for every index at once,
    # so that an index it refuses is refused here too.
    if not isinstance(key, bytes) or len(key) != _PERMUTE_KEY_BYTES:
        raise FluxError(FLX_5021, f"a permutation key is {_PERMUTE_KEY_BYTES} bytes")
    if size and not (int(indexes.min()) >= 0 and int(indexes.max()) < domain):
        raise FluxError(FLX_5021, f"a permutation index lies in [0, {domain})")
    if domain == 1:
        return [0] * size
    bits = (domain - 1).bit_length()
    left_bits = bits // 2
    right_bits = bits - left_bits
    left_mask = np.uint64((1 << left_bits) - 1)
    right_mask = np.uint64((1 << right_bits) - 1)
    # Every value of `bits` bits lies inside a domain that is exactly 2**bits.
    whole = domain == 1 << bits
    domain_left = np.uint64(0 if whole else domain >> right_bits)
    domain_right = np.uint64(0 if whole else domain & ((1 << right_bits) - 1))
    round_key = permute_round_key(key, domain)
    values = [int(index) for index in indexes.tolist()]
    left = np.array([value >> right_bits for value in values], dtype=np.uint64)
    right = np.array([value & ((1 << right_bits) - 1) for value in values], dtype=np.uint64)
    done_left = np.zeros(size, dtype=np.uint64)
    done_right = np.zeros(size, dtype=np.uint64)
    pending = np.arange(size)
    while pending.shape[0]:
        count = pending.shape[0]
        zeros = np.zeros(count, dtype=np.uint64)
        for round_index in range(4):
            stage = np.full(count, round_index, dtype=np.uint64)
            if round_index % 2 == 0:
                word = philox_arrays(right, zeros, stage, zeros, round_key)[0]
                left = (left + word) & left_mask
            else:
                word = philox_arrays(left, zeros, stage, zeros, round_key)[0]
                right = (right + word) & right_mask
        if whole:
            inside = np.ones(count, dtype=bool)
        else:
            inside = (left < domain_left) | ((left == domain_left) & (right < domain_right))
        done_left[pending[inside]] = left[inside]
        done_right[pending[inside]] = right[inside]
        outside = ~inside
        pending, left, right = pending[outside], left[outside], right[outside]
    return [
        (high << right_bits) | low
        for high, low in zip(done_left.tolist(), done_right.tolist(), strict=True)
    ]


def clear_memos() -> None:
    """Forget every declaration and key the functions above remembered.

    No draw depends on what is remembered, only on its arguments, so clearing is always
    safe; :func:`flux_engine.stages.pipeline.execute` clears at the end of every run.
    """
    for memo in (
        _schedule,
        _poisson_table,
        _normal_plan_of,
        _exponential_plan_of,
        _identifier_key,
    ):
        memo.cache_clear()
