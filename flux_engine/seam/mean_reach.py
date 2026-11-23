# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a number column's mean can reach, read from the statistics a profile publishes.

A column drawn to a published mean takes its values from the range and the quantiles the
profile publishes around it. A mean beyond what those allow cannot be drawn: every value
the profile leaves possible averages to less (or more) than the mean says, so a run drawn
to the profile fails its own ``mean`` check. An overlay that restates a ``mean/1`` is a
what-if on the mean, and one the column cannot reach is refused where it is read, naming
the bound it breaks, rather than written into a bundle that cannot pass.

The bound is the mean of the most extreme column the published statistics allow: each
slice of rows between two published quantile levels takes the value at its upper edge for
the highest mean and at its lower edge for the lowest, and the slices beyond the outermost
levels take the published minimum and maximum. A bound that rests on a withheld end is
absent, and a column with no bound on a side is never refused on that side.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, List, Mapping, Optional, Tuple

from flux_engine.core.errors import ErrorCode, FluxError
from flux_engine.seam.coverage import printable
from flux_engine.seam.overlay import Overlay
from flux_stats.definitions import MARGINAL_HISTOGRAM, MEAN

__all__ = ["check_restated_means", "mean_bounds"]

_MEAN = MEAN.identifier
_HISTOGRAM = MARGINAL_HISTOGRAM.identifier


def _metric(column: Any, name: str) -> Any:
    entry = column.metrics.get(name)
    return entry.value if entry is not None and entry.status == "F" else None


def _slices(column: Any) -> List[Tuple[Fraction, Optional[Fraction], Optional[Fraction]]]:
    """The published points as ``(share, lowest value, highest value)`` slices of the rows.

    From the minimum and maximum, the quantiles and an ungrouped histogram of the column;
    a share with no published edge on a side has ``None`` there.
    """
    low, high = _metric(column, "min"), _metric(column, "max")
    levels = {
        Fraction(level): Fraction(value)
        for level, value in (_metric(column, "quantiles") or {}).items()
    }
    points: List[Tuple[Fraction, Fraction]] = sorted(levels.items())
    slices: List[Tuple[Fraction, Optional[Fraction], Optional[Fraction]]] = []
    edge: Optional[Fraction] = Fraction(low) if low is not None else None
    taken = Fraction(0)
    for level, value in points:
        slices.append((level - taken, edge, value))
        edge, taken = value, level
    slices.append((1 - taken, edge, Fraction(high) if high is not None else None))
    return slices


def _histogram_slices(
    statistic: Any,
) -> List[Tuple[Fraction, Optional[Fraction], Optional[Fraction]]]:
    counts = statistic.groups[0].statistic.bin_counts
    total = sum(counts)
    if total <= 0 or len(counts) + 1 != len(statistic.bins):
        return []
    return [
        (
            Fraction(count, total),
            Fraction(statistic.bins[position]),
            Fraction(statistic.bins[position + 1]),
        )
        for position, count in enumerate(counts)
    ]


def _sum_of(
    slices: List[Tuple[Fraction, Optional[Fraction], Optional[Fraction]]], side: int
) -> Optional[Fraction]:
    total = Fraction(0)
    for share in slices:
        edge = share[side]
        if edge is None:
            return None
        total += share[0] * edge
    return total


def mean_bounds(column: Any) -> Tuple[Optional[Fraction], Optional[Fraction]]:
    """The lowest and highest mean the column's published statistics allow, or ``None`` for a side none bounds."""
    low_bounds: List[Fraction] = []
    high_bounds: List[Fraction] = []
    sources = [_slices(column)]
    for statistic in column.statistics:
        if (
            statistic.identifier == _HISTOGRAM
            and statistic.group_by is None
            and statistic.groups
            and len(statistic.groups) == 1
            and statistic.groups[0].published
            and statistic.groups[0].statistic is not None
        ):
            sources.append(_histogram_slices(statistic))
    for found in sources:
        if not found:
            continue
        low, high = _sum_of(found, 1), _sum_of(found, 2)
        if low is not None:
            low_bounds.append(low)
        if high is not None:
            high_bounds.append(high)
    return (max(low_bounds) if low_bounds else None, min(high_bounds) if high_bounds else None)


def _stated_mean(profile: Any, column: Any) -> Optional[Fraction]:
    for statistic in column.statistics:
        if (
            statistic.identifier != _MEAN
            or statistic.group_by is not None
            or len(statistic.groups) != 1
        ):
            continue
        group = statistic.groups[0]
        found = group.statistic
        if group.published and found is not None and found.k > 0:
            return Fraction(found.sum_x, found.k * 2**profile.fractional_bits)
    return None


def _spell(value: Fraction) -> str:
    """*value* as a decimal to six places, without trailing zeros: ``51.25``."""
    text = f"{Decimal(value.numerator) / Decimal(value.denominator):.6f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _patched_columns(patch: Mapping[str, Any]) -> List[Tuple[str, str]]:
    tables = patch.get("tables")
    found: List[Tuple[str, str]] = []
    for table_name, table in (tables.items() if isinstance(tables, Mapping) else ()):
        columns = table.get("columns") if isinstance(table, Mapping) else None
        found.extend(
            (table_name, name) for name in (columns if isinstance(columns, Mapping) else ())
        )
    return found


def check_restated_means(profile: Any, overlay: Overlay, *, code: ErrorCode) -> None:
    """Refuse (*code*) an overlay that leaves a patched column a mean its statistics cannot reach.

    *profile* is the overlaid profile. The message names the column, the mean, and the bound
    the published statistics put on it.
    """
    for table_name, name in _patched_columns(overlay.patch):
        table = profile.tables.get(table_name)
        column = table.columns.get(name) if table is not None else None
        if column is None:
            continue
        mean = _stated_mean(profile, column)
        if mean is None:
            continue
        low, high = mean_bounds(column)
        where = f"{printable(table_name)}.{printable(name)}"
        if high is not None and mean > high:
            side, bound, relation = "highest", high, "above"
        elif low is not None and mean < low:
            side, bound, relation = "lowest", low, "below"
        else:
            continue
        raise FluxError(
            code,
            f"the overlay {printable(overlay.name)!r} restates the mean of {where} as "
            f"{_spell(mean)}, {relation} the {side} mean its published range and quantiles "
            f"allow, {_spell(bound)}: no column those statistics describe has that mean",
        )
