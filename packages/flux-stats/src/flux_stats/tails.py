# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Exact decisions about binomial tails, without writing the tails out.

A shape check (flux-sim-engine's SCORECARD-SPEC.md) asks how
unlikely a run's count in one cell is under a profile's share ``p`` of that
cell, for ``X ~ Binomial(n, p)``:

- an empty cell: whether ``P(X = 0) = (1 - p)^n`` is at most a level;
- a point mass: whether ``P(X >= k)`` is at most a level;
- a distinct count: whether the number of cells a sample of ``n`` records
  is expected to occupy, ``sum(m * (1 - (1 - p)^n))``, is above or below a
  value.

Each answer turns on a rational number, and for a run of millions of
records on one whose numerator and denominator run to millions of digits.
Nothing here forms it unless it has to. Each question is first put to two
binary bounds of a fixed precision, one rounded down and one rounded up at
every product (directed rounding), which hold the exact value between them.
When the level lies outside the bounds, they decide; when it lies between
them, the precision doubles; and past the last precision the exact value is
formed as a :class:`~fractions.Fraction`. Every answer is the exact one.

The upper tail is decided through Chernoff's bound, in Hoeffding's form
(Hoeffding 1963, Theorem 1): ``P(X >= k) <= (np/k)^k (n(1-p)/(n-k))^(n-k)``
for ``np < k < n``, and ``P(X = n) = p^n`` exactly. The bound is never below
the tail, so a count it shows to be too unlikely is too unlikely; summing
the tail itself would take ``n - k`` products of growing size. The empty
cell's tail, ``(1 - p)^n``, is decided as it is, with nothing in between.

The module imports the standard library alone.
"""

from __future__ import annotations

from fractions import Fraction
from typing import List, Sequence, Tuple

from flux_stats.integers import is_int_at_least

__all__ = [
    "occupancy_compare",
    "power_at_most",
    "upper_tail_at_most",
]

#: The mantissa precision, in bits, the bounds are first formed at.
FIRST_BITS = 96
#: The largest precision tried before the exact value is formed instead.
LAST_BITS = 1 << 13

_ONE = Fraction(1)

#: ``(mantissa, exponent)``: the binary number ``mantissa * 2**exponent``,
#: with ``mantissa >= 0``.
_Binary = Tuple[int, int]


# ---------------------------------------------------------------------------
# Binary bounds, rounded outwards
# ---------------------------------------------------------------------------


def _rounded(mantissa: int, exponent: int, bits: int, up: bool) -> _Binary:
    """*mantissa* ``* 2**`` *exponent* cut to *bits* bits, toward the bound's side."""
    excess = mantissa.bit_length() - bits
    if excess <= 0:
        return mantissa, exponent
    kept = mantissa >> excess
    if up and kept << excess != mantissa:
        kept += 1
    return kept, exponent + excess


def _bound(value: Fraction, bits: int, up: bool) -> _Binary:
    """A binary number of *bits* bits not above *value*, or with *up* not below it."""
    numerator, denominator = value.numerator, value.denominator
    if numerator == 0:
        return 0, 0
    shift = bits + denominator.bit_length() - numerator.bit_length() + 1
    if shift >= 0:
        quotient, remainder = divmod(numerator << shift, denominator)
    else:
        quotient, remainder = divmod(numerator, denominator << -shift)
    if up and remainder:
        quotient += 1
    return _rounded(quotient, -shift, bits, up)


def _times(left: _Binary, right: _Binary, bits: int, up: bool) -> _Binary:
    return _rounded(left[0] * right[0], left[1] + right[1], bits, up)


def _power(base: _Binary, exponent: int, bits: int, up: bool) -> _Binary:
    """``base ** exponent`` by repeated squaring, every product rounded toward *up*'s side.

    The values are never negative, so a product of bounds on one side is a
    bound on the same side of the product.
    """
    result: _Binary = (1, 0)
    square = base
    while exponent:
        if exponent & 1:
            result = _times(result, square, bits, up)
        exponent >>= 1
        if exponent:
            square = _times(square, square, bits, up)
    return result


def _compare(value: _Binary, level: Fraction) -> int:
    """-1, 0 or 1 as the binary *value* is below, equal to or above *level*, exactly."""
    mantissa, exponent = value
    numerator, denominator = level.numerator, level.denominator
    if mantissa == 0 or numerator == 0:
        return (mantissa > 0) - (numerator > 0)
    # value lies in [2**(m - 1), 2**m) and level in (2**(n - 1), 2**(n + 1)).
    magnitude = mantissa.bit_length() + exponent
    level_magnitude = numerator.bit_length() - denominator.bit_length()
    if magnitude <= level_magnitude - 1:
        return -1
    if magnitude >= level_magnitude + 2:
        return 1
    left, right = mantissa * denominator, numerator
    if exponent >= 0:
        left <<= exponent
    else:
        right <<= -exponent
    return (left > right) - (left < right)


def _plus(left: _Binary, right: _Binary, bits: int, up: bool) -> _Binary:
    """``left + right`` cut to *bits* bits, toward *up*'s side.

    A term below the other's last bit is dropped from a lower bound and
    stands in as one unit of that bit in an upper one, so no shift ever runs
    past twice the precision.
    """
    if left[0] == 0:
        return right
    if right[0] == 0:
        return left
    if left[0].bit_length() + left[1] < right[0].bit_length() + right[1]:
        left, right = right, left
    gap = left[0].bit_length() + left[1] - right[0].bit_length() - right[1]
    if gap > bits + 2:
        return _rounded(left[0] + 1, left[1], bits, up) if up else left
    shift = left[1] - right[1]
    if shift >= 0:
        return _rounded((left[0] << shift) + right[0], right[1], bits, up)
    return _rounded(left[0] + (right[0] << -shift), left[1], bits, up)


def _empty_mass(
    terms: Sequence[Tuple[int, Fraction]], records: int, bits: int, up: bool
) -> _Binary:
    """``sum(multiplicity * base ** records)`` over *terms*, as a bound on *up*'s side."""
    total: _Binary = (0, 0)
    for multiplicity, base in terms:
        power = _power(_bound(base, bits, up), records, bits, up)
        total = _plus(total, _times(power, (multiplicity, 0), bits, up), bits, up)
    return total


# ---------------------------------------------------------------------------
# The decisions
# ---------------------------------------------------------------------------


def _require_share(share: Fraction, name: str) -> None:
    if not 0 <= share <= 1:
        raise ValueError(f"{name} must lie in [0, 1]")


def _require_count(value: int, name: str) -> None:
    if not is_int_at_least(value, 0):
        raise ValueError(f"{name} must be a whole number not below 0")


def _require_level(level: Fraction) -> None:
    if level < 0:
        raise ValueError("level must not be below 0")


def _product_at_most(factors: Sequence[Tuple[Fraction, int]], level: Fraction) -> bool:
    """Whether the product of ``base ** exponent`` over *factors* is at most *level*."""
    bits = FIRST_BITS
    while bits <= LAST_BITS:
        upper: _Binary = (1, 0)
        for base, exponent in factors:
            upper = _times(
                upper, _power(_bound(base, bits, True), exponent, bits, True), bits, True
            )
        if _compare(upper, level) <= 0:
            return True
        lower: _Binary = (1, 0)
        for base, exponent in factors:
            lower = _times(
                lower, _power(_bound(base, bits, False), exponent, bits, False), bits, False
            )
        if _compare(lower, level) > 0:
            return False
        bits *= 2
    exact = _ONE
    for base, exponent in factors:
        exact *= base**exponent
    return exact <= level


def power_at_most(base: Fraction, exponent: int, level: Fraction) -> bool:
    """Whether ``base ** exponent`` is at most *level*, decided exactly.

    With *base* ``1 - p`` and *exponent* ``n``, this is whether an empty cell
    is at most that likely: ``P(X = 0) = (1 - p)^n``. ``0 ** 0`` is 1.
    """
    base = Fraction(base)
    level = Fraction(level)
    if base < 0:
        raise ValueError("base must not be below 0")
    _require_count(exponent, "exponent")
    _require_level(level)
    if exponent == 0 or base == 1:
        return _ONE <= level
    if base == 0:
        return True
    return _product_at_most(((base, exponent),), level)


def upper_tail_at_most(share: Fraction, records: int, count: int, level: Fraction) -> bool:
    """Whether ``P(X >= count)`` for ``X ~ Binomial(records, share)`` is shown to be at most *level*.

    True when Chernoff's bound, which is never below the tail, is at most
    *level*; a ``False`` therefore says the bound is above it, and the tail
    itself may still be lower. ``count == records`` is decided on the tail
    itself, ``share ** records``. A count at or below the mean
    ``records * share`` is shown nothing below 1: the bound is 1 there.
    """
    share = Fraction(share)
    level = Fraction(level)
    _require_share(share, "share")
    _require_count(records, "records")
    _require_count(count, "count")
    _require_level(level)
    if count > records:
        raise ValueError("count must not exceed records")
    if count == 0:
        return _ONE <= level
    if share == 0:
        return True
    if count == records:
        return power_at_most(share, records, level)
    if count <= records * share:
        return _ONE <= level
    below = records * share / count
    above = records * (1 - share) / (records - count)
    return _product_at_most(((below, count), (above, records - count)), level)


def occupancy_compare(cells: Sequence[Tuple[int, Fraction]], records: int, value: Fraction) -> int:
    """-1, 0 or 1 as the expected number of occupied cells is below, equal to or above *value*.

    *cells* holds ``(multiplicity, share)`` pairs: ``multiplicity`` cells of
    share ``share`` each. A sample of *records* independent draws is
    expected to occupy ``sum(multiplicity * (1 - (1 - share) ** records))``
    of them: the expected distinct count of a sample, given the shares
    (``[(1, p1), (1, p2), ...]``) or, when only a distinct count ``D`` is
    known, ``D`` cells of share ``1/D`` (``[(D, 1/D)]``).
    """
    value = Fraction(value)
    _require_count(records, "records")
    pairs: List[Tuple[int, Fraction]] = []
    for multiplicity, share in cells:
        share = Fraction(share)
        _require_count(multiplicity, "multiplicity")
        _require_share(share, "share")
        pairs.append((multiplicity, share))
    # The expected occupancy is the cells less their expected empty mass,
    # total - Z with Z = sum(multiplicity * (1 - share) ** records), so it is
    # compared with *value* through Z against total - value. Z is decided in
    # relative precision: a run of millions leaves it vanishingly small, and
    # a bound on a fixed grid could then never tell total - Z from total.
    total = sum(multiplicity for multiplicity, _ in pairs)
    rest = total - value
    terms = [(multiplicity, 1 - share) for multiplicity, share in pairs if multiplicity]
    if rest < 0:
        return -1
    if rest == 0:
        empty = all(base == 0 and records > 0 for _, base in terms)
        return 0 if empty else -1
    bits = FIRST_BITS
    while bits <= LAST_BITS:
        if _compare(_empty_mass(terms, records, bits, True), rest) < 0:
            return 1
        if _compare(_empty_mass(terms, records, bits, False), rest) > 0:
            return -1
        bits *= 2
    exact = sum((multiplicity * base**records for multiplicity, base in terms), Fraction(0))
    return (rest > exact) - (rest < exact)
