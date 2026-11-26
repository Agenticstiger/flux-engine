# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How often two date columns drawn apart would break the order a check declares between them.

``fluxsim init`` writes a lifetime for a check that orders two dates of a row only where the
histograms the two columns are drawn from would not keep the order on their own (ADR 0024
section 10). This module is that estimate: from the two histograms a bundle declares, each bin's
mass spread evenly over the days (or, for a timestamp, the milliseconds) it covers, the share of
rows in which the end would lie before the start (on or before it, for a strict check) when the
two are drawn independently. The share is exact under those two assumptions, in
:class:`~fractions.Fraction` arithmetic, and so deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional, Sequence, Tuple

from flux_engine.author.orders import DateOrder
from flux_engine.seam.contract import LogicalKind

__all__ = ["DateSpread", "independent_break_share", "spread_of"]

_DAY_MS = 86_400_000


@dataclass(frozen=True)
class DateSpread:
    """The histogram a date column is drawn from: its bins of mass, on a grid of days or milliseconds."""

    #: ``LogicalKind.DATE`` (a grid of days) or ``LogicalKind.TIMESTAMP`` (a grid of milliseconds).
    kind: LogicalKind
    #: Each bin that holds mass as ``(low, high, share)``: it holds the grid points
    #: ``low <= v < high``, and ``share`` is the share of the column's non-null rows it holds. The shares add up to 1.
    bins: Tuple[Tuple[int, int, Fraction], ...]
    #: The share of the column's rows that are NULL.
    nulls: Fraction = Fraction(0)


def spread_of(
    kind: LogicalKind,
    edges_ms: Sequence[int],
    weights: Sequence[Fraction],
    nulls: Fraction,
) -> Optional[DateSpread]:
    """The spread a declared histogram draws, or ``None`` when it holds no mass or a bin no unit.

    *edges_ms* are the bins' edges in milliseconds since the epoch. A DATE column's grid is the
    day each edge falls on, a TIMESTAMP's the millisecond.
    """
    if len(edges_ms) != len(weights) + 1 or any(weight < 0 for weight in weights):
        return None
    total = sum(weights, Fraction(0))
    if total <= 0 or not 0 <= nulls < 1:
        return None
    scale = _DAY_MS if kind is LogicalKind.DATE else 1
    grid = [edge // scale for edge in edges_ms]
    bins = []
    for low, high, weight in zip(grid[:-1], grid[1:], weights, strict=True):
        if weight == 0:
            continue
        if high <= low:
            return None
        bins.append((low, high, weight / total))
    return DateSpread(kind, tuple(bins), nulls)


def _count_before(first: Tuple[int, int], second: Tuple[int, int], tie: bool) -> int:
    """The pairs ``(x, y)`` with ``x`` in *first* and ``y`` in *second* and ``x < y`` (``x <= y`` if *tie*)."""
    low, high = first
    left, right = second
    if not tie:
        low, high = low + 1, high + 1
    # ``x <= y``: each x below *left* pairs with every y, and each x inside pairs with those from x on.
    below = max(0, min(high, left) - low) * (right - left)
    start, stop = max(low, left), min(high, right) - 1
    inside = 0
    if start <= stop:
        count = stop - start + 1
        inside = count * (2 * right - start - stop) // 2
    return below + inside


def independent_break_share(
    start: DateSpread, end: DateSpread, order: DateOrder
) -> Optional[Fraction]:
    """The share of rows that would break *order* if its two dates were drawn independently.

    A row breaks it when its end lies before its start (on or before it, for a strict check), and
    when a date is NULL where the check does not hold on a NULL one. ``None`` when the two columns
    are not of one kind, since they are then not on one grid.
    """
    if start.kind is not end.kind:
        return None
    breaking = Fraction(0)
    for end_low, end_high, end_weight in end.bins:
        for start_low, start_high, start_weight in start.bins:
            pairs = _count_before((end_low, end_high), (start_low, start_high), order.strict)
            breaking += (
                end_weight
                * start_weight
                * Fraction(pairs, (end_high - end_low) * (start_high - start_low))
            )
    dated = (1 - end.nulls) * (1 - start.nulls)
    share = dated * breaking
    # A NULL date breaks the check where it does not hold on one.
    share += _nulls_break(start.nulls, end.nulls, order)
    return share


def _nulls_break(start_nulls: Fraction, end_nulls: Fraction, order: DateOrder) -> Fraction:
    """The share of rows with a NULL date on which the check fails."""
    broken = Fraction(0)
    if not order.null_end_holds:
        broken += end_nulls * (1 - start_nulls)
    if not order.null_start_holds:
        broken += start_nulls * (1 - end_nulls)
    if not order.null_end_holds and not order.null_start_holds:
        broken += start_nulls * end_nulls
    return broken
