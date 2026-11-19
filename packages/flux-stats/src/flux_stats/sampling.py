# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Estimates from a sample of whole blocks, with exact intervals that err wide.

A sampled check reads a few whole blocks of a dataset's records, each block
a cluster, and estimates a ratio of two totals from them, such as a mean
(the sum of a column over its record count), with the one-stage cluster
ratio estimator and its linearised variance, the form survey packages use
for a ratio (samplics' Taylor estimator, for one):

    R = sum(y) / sum(m)
    v = (1 - n/N) * n / (n - 1) * sum((y - R * m)^2) / sum(m)^2

over the ``n`` chosen blocks of ``N``, where ``y`` is a block's total and
``m`` its record count. ``1 - n/N`` is the finite-population correction: a
sample of every block has no sampling error. The interval is
``R +/- t * sqrt(v)``, with ``t`` the Student t value at 0.975 for ``n - 1``
degrees of freedom: a 95 % interval.

Every number is exact. The t values are tabulated rounded up at the fourth
decimal (computed from the closed form of the t distribution for whole
degrees of freedom, Abramowitz and Stegun 26.7.3 and 26.7.4), and a degree
of freedom between two entries reads the smaller one's larger value; the
square root is bounded above with :func:`math.isqrt`. Each makes the
interval a little wider than the formula, never narrower, so it covers at
least as often as the formula says. The module imports the standard library
alone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Sequence, Tuple

from flux_stats.integers import is_int_at_least

__all__ = [
    "LEVEL",
    "T_975",
    "RatioEstimate",
    "ratio_estimate",
    "sqrt_upper",
    "t_critical",
]

#: The coverage every interval here is formed for.
LEVEL = Fraction(95, 100)

#: Student t at 0.975, by degrees of freedom, rounded up at the fourth decimal.
T_975: Mapping[int, Fraction] = {
    degrees: Fraction(value)
    for degrees, value in (
        (1, "12.7063"),
        (2, "4.3027"),
        (3, "3.1825"),
        (4, "2.7765"),
        (5, "2.5706"),
        (6, "2.4470"),
        (7, "2.3647"),
        (8, "2.3061"),
        (9, "2.2622"),
        (10, "2.2282"),
        (11, "2.2010"),
        (12, "2.1789"),
        (13, "2.1604"),
        (14, "2.1448"),
        (15, "2.1315"),
        (16, "2.1200"),
        (17, "2.1099"),
        (18, "2.1010"),
        (19, "2.0931"),
        (20, "2.0860"),
        (21, "2.0797"),
        (22, "2.0739"),
        (23, "2.0687"),
        (24, "2.0639"),
        (25, "2.0596"),
        (26, "2.0556"),
        (27, "2.0519"),
        (28, "2.0485"),
        (29, "2.0453"),
        (30, "2.0423"),
        (40, "2.0211"),
        (60, "2.0003"),
        (120, "1.9800"),
    )
}

#: The precision, in fractional bits, a square root is bounded at.
SQRT_BITS = 64


def t_critical(degrees: int) -> Fraction:
    """Student t at 0.975 for *degrees* degrees of freedom, never below the true value.

    A degree of freedom between two table entries reads the smaller one's
    value, which is the larger t; one past 120 reads 120's.
    """
    if not is_int_at_least(degrees, 1):
        raise ValueError("degrees of freedom must be a whole number of at least 1")
    return T_975[max(entry for entry in T_975 if entry <= degrees)]


def sqrt_upper(value: Fraction, bits: int = SQRT_BITS) -> Fraction:
    """A binary fraction with *bits* fractional bits, never below ``sqrt(value)``."""
    value = Fraction(value)
    if value < 0:
        raise ValueError("cannot take the square root of a negative number")
    scale = 1 << bits
    quotient, remainder = divmod(value.numerator * scale * scale, value.denominator)
    root = math.isqrt(quotient)
    if root * root < quotient or remainder:
        root += 1
    return Fraction(root, scale)


@dataclass(frozen=True)
class RatioEstimate:
    """A ratio estimated from chosen blocks, with its 95 % interval ``[low, high]``."""

    estimate: Fraction
    low: Fraction
    high: Fraction
    #: The blocks chosen, of how many there are.
    blocks: int
    population: int
    #: The records in the chosen blocks: the ratio's denominator total.
    records: int

    @property
    def exact(self) -> bool:
        """Whether every block was read, so the estimate is the value itself."""
        return self.blocks == self.population


def ratio_estimate(blocks: Sequence[Tuple[int, int]], population: int) -> RatioEstimate:
    """Estimate ``sum(y) / sum(m)`` over *population* blocks from the chosen *blocks*.

    *blocks* holds one ``(y, m)`` pair per chosen block: its total and its
    record count. Two blocks are the fewest a variance can be estimated
    from, unless every block was chosen, when the estimate is exact. A
    sample whose blocks hold no record has no ratio, and is refused.
    """
    chosen = [(int(total), int(count)) for total, count in blocks]
    sampled = len(chosen)
    if isinstance(population, bool) or not isinstance(population, int):
        raise ValueError("the number of blocks must be a whole number")
    if not 1 <= sampled <= population:
        raise ValueError("the chosen blocks must be between 1 and all of them")
    if any(count < 0 for _, count in chosen):
        raise ValueError("a block's record count cannot be negative")
    records = sum(count for _, count in chosen)
    if records == 0:
        raise ValueError("the chosen blocks hold no record")
    estimate = Fraction(sum(total for total, _ in chosen), records)
    if sampled == population:
        return RatioEstimate(estimate, estimate, estimate, sampled, population, records)
    if sampled < 2:
        raise ValueError("a variance needs at least two chosen blocks")
    residual = sum(
        ((total - estimate * count) ** 2 for total, count in chosen),
        Fraction(0),
    )
    variance = (
        (1 - Fraction(sampled, population)) * Fraction(sampled, sampled - 1) * residual / records**2
    )
    half = t_critical(sampled - 1) * sqrt_upper(variance)
    return RatioEstimate(estimate, estimate - half, estimate + half, sampled, population, records)
