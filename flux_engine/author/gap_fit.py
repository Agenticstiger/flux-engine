# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The days between two dates, read from the two histograms the release publishes for them.

A table whose rows each carry a start and an end, with the end on or after the start, publishes
the histogram of each column and may withhold the histogram of the days between them. The days are
then not unknown: a row's end is its start plus its gap, and the generator draws the gap apart
from the start, so the histogram of the ends is the histogram of the starts convolved with the
histogram of the gaps. :func:`fit_gap` finds the gaps' histogram that makes that convolution come
closest to the ends' histogram.

* **The model.** Each bin of starts and each bin of gaps spreads its mass evenly over the days it
  covers, and the share of the ends that land in each end bin follows exactly, in
  :class:`~fractions.Fraction` arithmetic (the same even spread as :mod:`flux_engine.author.date_spread`).
* **The update.** The gaps' weights start even, which is the assumption ``init`` made before this
  module, and take a fixed number of multiplicative updates, :data:`GAP_FIT_ITERATIONS`. That is
  the Richardson-Lucy update for a mixture of known components, the maximum-likelihood step for the
  published ends under the model: each update raises their likelihood, and none can give a gap
  below the shortest or a weight below zero. The count is fixed and small on purpose: the
  published cells carry noise, and the update, run to convergence, fits that noise (early
  stopping is how the method is regularised).
* **Determinism.** The updates run on whole numbers scaled by :data:`SCALE`, with floor division, so
  the same inputs give the same bytes on every machine. No float is read.
* **Whether it can hold.** A coupling with every end on or after its start that reproduces both
  histograms exists only if the ends' cumulative share lies at or below the starts' everywhere
  (Strassen's theorem). :attr:`GapFit.gap` is the largest amount by which it does not; where it is
  above zero no gap can match both histograms, and the fit is the closest one.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import List, Sequence, Tuple

__all__ = [
    "GAP_FIT_ITERATIONS",
    "GAP_FIT_MAX_BINS",
    "GapFit",
    "Interval",
    "dominance_gap",
    "fit_gap",
    "longest_without_leak",
    "total_variation",
]

#: One bin of a histogram on a grid of days: ``(low, high, share)``, holding the days ``low <= d < high``.
Interval = Tuple[Fraction, Fraction, Fraction]

#: The number of multiplicative updates. The likelihood of the published ends rises with each one
#: and flattens within a few tens; later updates fit the noise of the cells and not the shape, so
#: the count stops early (the regularisation of Richardson-Lucy and of expectation-maximisation).
GAP_FIT_ITERATIONS = 40

#: The most bins the gaps' histogram has, so the work is bounded whatever a release publishes.
GAP_FIT_MAX_BINS = 48

#: The scale whole numbers carry in the updates: a weight is ``SCALE`` times its share.
SCALE = 1 << 48


@dataclass(frozen=True)
class GapFit:
    """The gaps' histogram, and how well it reproduces the published ends."""

    #: The bin edges of the gap, in whole days, increasing.
    edges: Tuple[int, ...]
    #: The share of each bin; they add up to 1.
    weights: Tuple[Fraction, ...]
    #: The total variation between the ends the fit gives and the published ends (0 to 1).
    distance: Fraction
    #: The largest amount by which the ends' cumulative share exceeds the starts' (0 when a coupling exists).
    gap: Fraction
    #: ``True`` when the weights are the even ones: the histogram could not be fitted.
    even: bool


#: The grain a published weight is written to: nine decimals, as every histogram init writes is.
GRAIN = 10**9


def _grained(weights: Sequence[int]) -> Tuple[Fraction, ...]:
    """Whole-number *weights* (adding up to :data:`SCALE`) as shares of nine decimals that add up to 1.

    Each is rounded down, and what is left over goes to the largest remainders, earliest first.
    """
    floors = [weight * GRAIN // SCALE for weight in weights]
    left = GRAIN - sum(floors)
    order = sorted(range(len(weights)), key=lambda at: (-(weights[at] * GRAIN % SCALE), at))
    for at in order[:left]:
        floors[at] += 1
    return tuple(Fraction(value, GRAIN) for value in floors)


def _ramp(value: Fraction) -> Fraction:
    """The second antiderivative of a step: ``max(value, 0) ** 2 / 2``."""
    return value * value / 2 if value > 0 else Fraction(0)


def _cdf_of_sum(
    start: Tuple[Fraction, Fraction], gap: Tuple[Fraction, Fraction], at: Fraction
) -> Fraction:
    """``P(S + G <= at)`` for ``S`` uniform on *start* and ``G`` uniform on *gap*, exactly."""
    (a, b), (c, d) = start, gap
    total = _ramp(at - a - c) - _ramp(at - a - d) - _ramp(at - b - c) + _ramp(at - b - d)
    return total / ((b - a) * (d - c))


def _cumulative(bins: Sequence[Interval], at: Fraction) -> Fraction:
    """The share of *bins* below *at*, each bin spread evenly."""
    found = Fraction(0)
    for low, high, share in bins:
        if at >= high:
            found += share
        elif at > low:
            found += share * (at - low) / (high - low)
    return found


def dominance_gap(starts: Sequence[Interval], ends: Sequence[Interval]) -> Fraction:
    """How far the ends' cumulative share rises above the starts' anywhere, 0 when it never does."""
    points = sorted({edge for low, high, _ in (*starts, *ends) for edge in (low, high)})
    worst = Fraction(0)
    for point in points:
        worst = max(worst, _cumulative(ends, point) - _cumulative(starts, point))
    return worst


def total_variation(left: Sequence[Fraction], right: Sequence[Fraction]) -> Fraction:
    """Half the summed absolute difference of two shares that each add up to 1."""
    return sum((abs(a - b) for a, b in zip(left, right, strict=True)), Fraction(0)) / 2


def longest_without_leak(starts: Sequence[Interval], ends: Sequence[Interval]) -> int:
    """The longest gap that keeps every start's end inside the ends' histogram: last end edge less last start edge.

    A longer gap would put the latest starts' ends past every end bin, mass the histogram of the
    ends cannot hold. A caller whose ends are cut short of where a gap may take them (the closed
    part of a history, whose lengths the generator compresses to its horizon) does not use it.
    """
    return int(max(high for _, high, _ in ends) - max(high for _, high, _ in starts))


def _grid(
    starts: Sequence[Interval], ends: Sequence[Interval], shortest: int, longest: int
) -> List[int]:
    """The edges of the gaps' bins: the distances from a start edge to an end edge, thinned.

    A gap that lands a start's edge on an end's edge is where the convolution changes slope, so
    these are the places the gaps' histogram can differ most. The grid is whole days from
    *shortest* to *longest*, at most :data:`GAP_FIT_MAX_BINS` bins, thinned by rank.
    """
    found = {shortest, longest}
    start_edges = {edge for low, high, _ in starts for edge in (low, high)}
    end_edges = {edge for low, high, _ in ends for edge in (low, high)}
    for end in end_edges:
        for start in start_edges:
            if shortest < end - start < longest:
                found.add(int(end - start))
    edges = sorted(found)
    if len(edges) - 1 > GAP_FIT_MAX_BINS:
        keep = {
            edges[round(position * (len(edges) - 1) / GAP_FIT_MAX_BINS)]
            for position in range(GAP_FIT_MAX_BINS + 1)
        }
        edges = sorted(keep)
    return edges


def _even(
    shortest: int, longest: int, ends: Sequence[Interval], starts: Sequence[Interval]
) -> GapFit:
    return GapFit(
        edges=(shortest, longest),
        weights=(Fraction(1),),
        distance=Fraction(1),
        gap=dominance_gap(starts, ends),
        even=True,
    )


def _reach(
    starts: Sequence[Interval], ends: Sequence[Interval], bins: Sequence[Tuple[Fraction, Fraction]]
) -> List[List[int]]:
    """``matrix[j][k]``: the share of the ends in bin *k* when every gap is in bin *j*, scaled by :data:`SCALE`."""
    reach = [[Fraction(0)] * len(ends) for _ in bins]
    for start_low, start_high, start_share in starts:
        for j, span in enumerate(bins):
            row = reach[j]
            for k, (end_low, end_high, _) in enumerate(ends):
                row[k] += start_share * (
                    _cdf_of_sum((start_low, start_high), span, end_high)
                    - _cdf_of_sum((start_low, start_high), span, end_low)
                )
    return [[int(cell * SCALE) for cell in row] for row in reach]


def _update(
    matrix: Sequence[Sequence[int]],
    mass: Sequence[int],
    target: Sequence[int],
    live: Sequence[int],
    iterations: int,
) -> List[int]:
    """The gaps' weights after *iterations* multiplicative updates from an even start, in whole numbers."""
    size, cells = len(matrix), len(target)
    weights = [SCALE // len(live) if j in live else 0 for j in range(size)]
    weights[live[0]] += SCALE - sum(weights)
    for _ in range(iterations):
        predicted = [sum(weights[j] * matrix[j][k] for j in live) // SCALE for k in range(cells)]
        ratio = [target[k] * SCALE // predicted[k] if predicted[k] else 0 for k in range(cells)]
        updated = [0] * size
        for j in live:
            pull = sum(ratio[k] * matrix[j][k] for k in range(cells)) // SCALE
            updated[j] = weights[j] * pull // mass[j]
        total = sum(updated)
        if total <= 0:
            break
        weights = [value * SCALE // total for value in updated]
        weights[weights.index(max(weights))] += SCALE - sum(weights)
    return weights


def fit_gap(
    starts: Sequence[Interval],
    ends: Sequence[Interval],
    shortest: int,
    longest: int,
    iterations: int = GAP_FIT_ITERATIONS,
) -> GapFit:
    """The histogram of gaps, from *shortest* to *longest* days, that turns *starts* into *ends*.

    Both histograms are on a grid of days and each add up to 1. With nothing to fit (no mass, or
    no room for a gap) the gaps are even over the whole range and :attr:`GapFit.even` is set; its
    :attr:`GapFit.distance` is then 1, the distance of no claim.
    """
    gap = dominance_gap(starts, ends)
    if not starts or not ends or longest <= shortest:
        return _even(shortest, longest, ends, starts)
    edges = _grid(starts, ends, shortest, longest)
    bins = [
        (Fraction(low), Fraction(high)) for low, high in zip(edges[:-1], edges[1:], strict=True)
    ]
    matrix = _reach(starts, ends, bins)
    mass = [sum(row) for row in matrix]
    target = [int(share * SCALE) for _, _, share in ends]
    live = [j for j, total in enumerate(mass) if total > 0]
    if not live or not any(target):
        return _even(shortest, longest, ends, starts)
    weights = _update(matrix, mass, target, live, iterations)
    predicted = [sum(weights[j] * matrix[j][k] for j in live) for k in range(len(ends))]
    whole = sum(predicted)
    if whole <= 0:
        return _even(shortest, longest, ends, starts)
    fitted = [Fraction(value, whole) for value in predicted]
    distance = total_variation(fitted, [share for _, _, share in ends])
    return GapFit(
        edges=tuple(edges),
        weights=_grained(weights),
        distance=distance,
        gap=gap,
        even=False,
    )
