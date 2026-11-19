# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A maximum-entropy fill: bins over a span, tilted so the mean as drawn is the published one.

ADR 0014 section 3. Among the distributions on a span with a known mean, the one
of largest entropy is the truncated exponential, ``p(x)`` proportional to
``exp(λx)`` (K. Conrad, "Probability distributions and maximum entropy",
theorem 5.1); on the integers from a bound it is the geometric (theorem 5.8).
The generator draws a bin evenly over its grid points, so the fill lays
equal-width bins over the span, weights bin ``k`` by ``r**k`` with
``r = exp(λ·width)``, and finds ``r`` by bisection in exact fractions so that the
mean as the generator draws it (:func:`drawn_mean`) is the published one.

The far end of a span from one bound is where the geometric with that mean
leaves less than half a row beyond it, worked out under one explicit decimal
context (``Decimal.ln`` is correctly rounded), so the same inputs give the same
bins on every platform. Nothing here is a float.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from fractions import Fraction
from typing import List, Optional, Sequence, Tuple

__all__ = ["MOST_BINS", "drawn_mean", "part_mean", "tail_steps", "tilted_fill"]

#: The most bins a fill lays out: a span of fewer grid steps gets one bin per step.
MOST_BINS = 32

#: How many halvings find the tilt: it is then known to 2^-64 of its range.
_BISECTIONS = 64

#: The precision the tail is worked out to.
_PRECISION = 40


def part_mean(low: Fraction, high: Fraction, step: Fraction, last: bool) -> Fraction:
    """The mean of one bin as the generator draws it: evenly over its grid points.

    A bin is half-open, its top grid point one step below its upper edge, and
    the last bin is closed (``_bin_top`` in ``generator/rows.py``). A bin
    narrower than one step holds its lower edge alone.
    """
    top = high if last else high - step
    if top <= low:
        return low
    return low + step * ((top - low) // step) / 2


def drawn_mean(edges: Sequence[Fraction], weights: Sequence[Fraction], step: Fraction) -> Fraction:
    """A histogram's mean as the generator draws it: each bin's mean, by the bin's weight."""
    last = len(weights) - 1
    parts = zip(weights, edges[:-1], edges[1:], strict=True)
    total = sum(
        (weight * part_mean(low, high, step, position == last))
        for position, (weight, low, high) in enumerate(parts)
    )
    return Fraction(total) / sum(weights, Fraction(0))


def tail_steps(steps: Fraction, rows: int) -> int:
    """How many grid steps from its bound a geometric with mean *steps* leaves under half a row.

    The geometric with mean ``m`` on ``0, 1, 2, ...`` has ratio ``q = m / (1 + m)``,
    and ``P(X > K) = q^(K+1)``: the smallest ``K`` with ``q^(K+1) <= 1 / (2 rows)``.
    """
    with localcontext(Context(prec=_PRECISION, rounding=ROUND_HALF_EVEN)):
        mean = Decimal(steps.numerator) / Decimal(steps.denominator)
        ratio = ((mean + 1) / mean).ln()
        needed = Decimal(2 * max(rows, 1)).ln() / ratio
    return max(int(math.ceil(needed)) - 1, 0)


def _edges(near: Fraction, steps: int, step: Fraction, upward: bool) -> List[Fraction]:
    """Equal-width edges over *steps* grid steps from *near*, ascending, at most :data:`MOST_BINS` bins."""
    width = max(1, -(-steps // MOST_BINS))
    offsets = list(range(0, steps, width)) + [steps]
    if upward:
        return [near + offset * step for offset in offsets]
    return [near - offset * step for offset in reversed(offsets)]


def _weights(count: int, ratio: Fraction, upward: bool) -> List[Fraction]:
    """``ratio**k`` from the bound's bin out: rising from the bound when *upward* is ``False``."""
    powers = [ratio**index for index in range(count)]
    return powers if upward else powers[::-1]


def tilted_fill(
    bound: Fraction,
    mean: Fraction,
    step: Fraction,
    rows: int,
    *,
    distinct: Optional[int] = None,
    limit: Optional[Fraction] = None,
) -> Optional[Tuple[List[Fraction], List[Fraction]]]:
    """Bins from *bound* away toward *mean*, tilted so their mean as drawn is *mean*.

    The span starts at *bound* and holds at least *distinct* grid values and at
    least three, and reaches the point past which the geometric with that mean
    leaves under half a row (:func:`tail_steps`). *limit* caps how far from zero
    an end may lie. Returns ascending edges on the grid and a weight per bin, or
    ``None`` when *mean* is not on the far side of *bound* or the bins cannot
    reach it within *limit*.
    """
    if mean == bound or step <= 0:
        return None
    upward = mean > bound
    steps = max(tail_steps(abs(mean - bound) / step, rows), (distinct or 0) - 1, 2)
    if limit is not None:
        room = (limit - bound) / step if upward else (bound + limit) / step
        steps = min(steps, math.floor(room))
        if steps < 2:
            return None
    edges = _edges(bound, steps, step, upward)
    count = len(edges) - 1

    def toward_far(ratio: Fraction) -> bool:
        """Whether the bins tilted by *ratio* put the mean at or beyond the published one."""
        drawn = drawn_mean(edges, _weights(count, ratio, upward), step)
        return drawn >= mean if upward else drawn <= mean

    low, high = Fraction(0), Fraction(1)
    while not toward_far(high):
        low, high = high, high * 2
        if high > 2**64:
            return None
    for _ in range(_BISECTIONS):
        middle = (low + high) / 2
        if toward_far(middle):
            high = middle
        else:
            low = middle
    best = min(
        (low, high),
        key=lambda ratio: abs(drawn_mean(edges, _weights(count, ratio, upward), step) - mean),
    )
    if best == 0:
        best = high
    return edges, _weights(count, best, upward)
