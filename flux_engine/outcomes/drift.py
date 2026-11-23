# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Drift between two states flags a stale calibration (ADR 0029 section 6).

When ``fluxsim forecast score`` scores, it compares state ``k`` (the pinned profile) with state
``k + 1`` over the statistics both hold with the same definition, levels or bins, and grouping:

* each ``levelHistogram/1`` and ``marginalHistogram/1`` of a column, one statistic for each group
  it publishes (a statistic with no grouping has the one group ``""``);
* each query cell the response families read: the cell of the treated and the control query for
  each segment of a family, a count of the records the query does not match and of those it
  matches.

A statistic with a withheld cell in either state, with no records in either state (smoothing would
give it a uniform share, which no record supports), or that the other state does not hold with the
same definition, is left out and listed with its reason. The share such a cell lost or gained still
shows in the statistic of the column that groups it.

* **Shares.** Each share is ``(count + 1/2) / (total + n/2)`` over the statistic's ``n`` cells, so
  none is zero and the shares of a statistic add to one. ``p`` is state ``k``'s share and ``q``
  state ``k + 1``'s.
* **The metric** is the one the Playback that ``forecasts.playback`` names declares: ``psi``, the
  sum of ``(q - p) ln(q / p)``; ``kl``, the sum of ``q ln(q / p)``; ``ks``, the largest gap between
  the cumulative shares in the order the profile lists the cells. ``ln`` is taken in ``Decimal``
  under the kernel's context (:func:`flux_engine.core.rng.kernel_context`); the shares and the
  ``ks`` gaps are exact fractions.
* **The flag.** A statistic whose value is above the Playback's ``threshold`` has drifted, and one
  drifted statistic marks the calibration on state ``k`` stale. ``onBreach`` is ``warn`` (the
  default) or ``fail``. The Playback's ``drift`` is read only when ``forecasts.playback`` names it,
  so a bundle whose Playback declares drift and no forecast reads as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError, register
from flux_engine.core.rng import kernel_context
from flux_engine.expr.types import nfc
from flux_engine.outcomes.responses import decimal_text
from flux_engine.seam.responses import ResponseFamily
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM
from flux_stats.profile import Profile, ProfileGroup, StatisticEntry

__all__ = [
    "FLX_7145",
    "FLX_7146",
    "METRICS",
    "DriftReport",
    "DriftRule",
    "Statistic",
    "compare_states",
    "kl",
    "ks",
    "psi",
    "read_drift_rule",
    "shares",
    "statistics_of",
]

METRICS: Tuple[str, ...] = ("psi", "kl", "ks")
_ON_BREACH: Tuple[str, ...] = ("warn", "fail")
_HISTOGRAMS = (LEVEL_HISTOGRAM.identifier, MARGINAL_HISTOGRAM.identifier)

FLX_7145 = register(
    "FLX-7145",
    "the Playback's drift declaration is not one the engine can read",
    "The Playback that forecasts.playback names declares drift with a metric (psi, kl or ks), "
    "a threshold that is a number above 0, and an onBreach of warn or fail. A drift block "
    "with another metric, a threshold that is not a positive number, or another onBreach "
    "cannot be compared with, so it is refused rather than read as no drift.",
)

FLX_7146 = register(
    "FLX-7146",
    "the calibration on the pinned state is stale",
    "The Playback that forecasts.playback names declares drift with onBreach fail, and at "
    "least one statistic both states hold has moved past its threshold between the pinned "
    "state and the next one. Every verdict was written before the command exited, so the "
    "scores are on record whichever way it falls. The message names the statistic with the "
    "largest value. Calibrate on the next state, or declare onBreach warn to report without "
    "failing.",
)


@dataclass(frozen=True)
class DriftRule:
    """What the Playback declares: the metric, the threshold and what a breach does."""

    metric: str
    threshold: Decimal
    on_breach: str = "warn"
    playback: str = ""


@dataclass(frozen=True)
class Statistic:
    """One statistic of a profile: its shape, its counts, or why it has none.

    ``shape`` is everything that makes two statistics the same one: the definition, the grouping,
    and the levels or the bins. ``counts`` is ``None`` for a withheld cell, and ``reason`` says so.
    """

    name: str
    shape: Tuple[Any, ...]
    counts: Optional[Tuple[int, ...]]
    reason: str = ""


@dataclass(frozen=True)
class DriftReport:
    """The comparison of the two states: each value, what was left out, and the verdict."""

    rule: DriftRule
    values: Tuple[Tuple[str, Decimal], ...]
    left_out: Tuple[Tuple[str, str], ...]
    largest: Optional[Tuple[str, Decimal]]
    stale: bool

    def as_member(self) -> Dict[str, Any]:
        """The ``drift`` member of a verdict: the metric, the threshold, the largest and stale."""
        return {
            "metric": self.rule.metric,
            "threshold": decimal_text(self.rule.threshold),
            "largest": decimal_text(self.largest[1]) if self.largest is not None else None,
            "statistic": self.largest[0] if self.largest is not None else None,
            "stale": self.stale,
        }

    def as_document(self) -> Dict[str, Any]:
        """The member, with the number of statistics compared and the ones left out."""
        return {
            **self.as_member(),
            "compared": len(self.values),
            "leftOut": [{"statistic": name, "reason": reason} for name, reason in self.left_out],
        }


def read_drift_rule(
    documents: Mapping[str, Mapping[str, Any]], playback: Optional[str], *, where: str
) -> Optional[DriftRule]:
    """The drift the Playback *playback* declares, or ``None`` when none is named or declared.

    Only a Playback that ``forecasts.playback`` names is read, so a Playback that declares drift
    and is named by nothing changes nothing. A declaration the engine cannot compare with is
    ``FLX-7145``.
    """
    if playback is None:
        return None
    document = documents.get(nfc(playback))
    spec = document.get("spec") if isinstance(document, Mapping) else None
    block = spec.get("drift") if isinstance(spec, Mapping) else None
    if block is None:
        return None
    at = f"{where}: the drift of the Playback {playback!r}"
    if not isinstance(block, Mapping):
        raise FluxError(FLX_7145, f"{at} is a mapping of metric, threshold and onBreach")
    metric = block.get("metric")
    if metric not in METRICS:
        raise FluxError(FLX_7145, f"{at} has the metric {metric!r}; it is one of {list(METRICS)}")
    raw = block.get("threshold")
    try:
        threshold = Decimal(str(raw))
    except ArithmeticError:
        threshold = Decimal(-1)
    if isinstance(raw, bool) or raw is None or not threshold.is_finite() or threshold <= 0:
        raise FluxError(FLX_7145, f"{at} has the threshold {raw!r}; it is a number above 0")
    on_breach = block.get("onBreach", "warn")
    if on_breach not in _ON_BREACH:
        raise FluxError(
            FLX_7145, f"{at} has the onBreach {on_breach!r}; it is one of {list(_ON_BREACH)}"
        )
    return DriftRule(metric=metric, threshold=threshold, on_breach=on_breach, playback=playback)


def shares(counts: Sequence[int]) -> Tuple[Fraction, ...]:
    """``(count + 1/2) / (total + n/2)`` for each of the ``n`` cells; they add up to one."""
    cells = len(counts)
    if cells == 0:
        raise ValueError("a statistic has at least one cell")
    total = Fraction(sum(counts)) + Fraction(cells, 2)
    return tuple((Fraction(count) + Fraction(1, 2)) / total for count in counts)


def _decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _log_ratio(q: Fraction, p: Fraction) -> Decimal:
    return _decimal(q / p).ln()


def psi(p: Sequence[Fraction], q: Sequence[Fraction]) -> Decimal:
    """The population stability index: the sum of ``(q - p) ln(q / p)`` over the cells."""
    with localcontext(kernel_context()):
        total = Decimal(0)
        for before, after in zip(p, q, strict=True):
            total += _decimal(after - before) * _log_ratio(after, before)
        return total


def kl(p: Sequence[Fraction], q: Sequence[Fraction]) -> Decimal:
    """The Kullback-Leibler divergence of ``q`` from ``p``: the sum of ``q ln(q / p)``."""
    with localcontext(kernel_context()):
        total = Decimal(0)
        for before, after in zip(p, q, strict=True):
            total += _decimal(after) * _log_ratio(after, before)
        return total


def ks(p: Sequence[Fraction], q: Sequence[Fraction]) -> Decimal:
    """The largest gap between the cumulative shares, in the order the cells are listed."""
    gap = Fraction(0)
    before = after = Fraction(0)
    for first, second in zip(p, q, strict=True):
        before += first
        after += second
        gap = max(gap, abs(before - after))
    with localcontext(kernel_context()):
        return _decimal(gap)


_METRIC = {"psi": psi, "kl": kl, "ks": ks}


def _group_counts(
    group: ProfileGroup, kind: str, cells: int
) -> Tuple[Optional[Tuple[int, ...]], str]:
    if not group.published or group.suppressed_cells or group.statistic is None:
        return None, "a cell is withheld"
    counts = group.statistic.cell_counts(kind)
    if len(counts) != cells:
        return None, "the counts do not fit the declared cells"
    return counts, ""


def _shape(entry: StatisticEntry) -> Tuple[Any, ...]:
    return (
        entry.definition.identifier,
        entry.group_by,
        entry.levels,
        entry.bins,
        entry.per,
        repr(entry.group_by_link),
    )


def _cells_of(entry: StatisticEntry) -> int:
    return len(entry.levels) if entry.definition.kind == "levelHistogram" else len(entry.bins) - 1


def _held(found: Dict[str, Statistic], statistic: Statistic) -> None:
    """Add *statistic* under its name; a repeated name is told apart by its position."""
    name, position = statistic.name, 1
    while name in found:
        position += 1
        name = f"{statistic.name}#{position}"
    found[name] = Statistic(name, statistic.shape, statistic.counts, statistic.reason)


def _entry_statistics(
    table_name: str, column_name: str, entry: StatisticEntry, found: Dict[str, Statistic]
) -> None:
    """Add each group of one histogram statistic of a column to *found*."""
    cells = _cells_of(entry)
    for group in entry.groups:
        name = f"{table_name}.{column_name}/{entry.definition.identifier}"
        if entry.group_by is not None:
            name += f"/by {entry.group_by}={group.key}"
        counts, reason = _group_counts(group, entry.definition.kind, cells)
        _held(found, Statistic(name, _shape(entry), counts, reason))


def _column_statistics(profile: Profile, found: Dict[str, Statistic]) -> None:
    """Add every histogram statistic of each column of *profile* to *found*."""
    for table_name, table in sorted(profile.tables.items()):
        for column_name, column in sorted(table.columns.items()):
            for entry in column.statistics:
                if entry.definition.identifier in _HISTOGRAMS:
                    _entry_statistics(table_name, column_name, entry, found)


def _family_cells(
    family: ResponseFamily, queries: Mapping[str, Any], found: Dict[str, Statistic]
) -> None:
    """Add the query cell *family* reads for each segment, treated and control, to *found*."""
    for query in (family.estimate.treated, family.estimate.control):
        entry_of = queries.get(query)
        for level, _segment in family.segments:
            name = f"query:{query}/{level}"
            if name in found:
                continue
            held = _query_cell(entry_of.statistics if entry_of else (), level)
            if held is not None:
                found[name] = Statistic(name, held[0], held[1], held[2])


def _query_statistics(
    profile: Profile, families: Sequence[ResponseFamily], found: Dict[str, Statistic]
) -> None:
    """Add the query cell each family reads, treated and control, to *found*."""
    queries = {nfc(query.name): query for query in profile.queries}
    for family in families:
        _family_cells(family, queries, found)


def statistics_of(profile: Profile, families: Sequence[ResponseFamily]) -> Dict[str, Statistic]:
    """Every statistic of *profile* the comparison reads, by name.

    A column statistic is ``<table>.<column>/<definition>`` (``/by <column>=<group>`` when
    grouped), a query cell ``query:<query id>/<segment>``. A cell that is withheld is held
    with no counts, so the comparison can list it.
    """
    found: Dict[str, Statistic] = {}
    _column_statistics(profile, found)
    _query_statistics(profile, families, found)
    return found


def _query_cell(
    statistics: Sequence[StatisticEntry], level: str
) -> Optional[Tuple[Tuple[Any, ...], Optional[Tuple[int, ...]], str]]:
    for entry in statistics:
        if entry.group_by is None or entry.definition.identifier != LEVEL_HISTOGRAM.identifier:
            continue
        for group in entry.groups:
            if group.key == level:
                counts, reason = _group_counts(group, entry.definition.kind, _cells_of(entry))
                return _shape(entry), counts, reason
    return None


def _left_out_reason(first: Statistic, second: Optional[Statistic]) -> Optional[str]:
    """Why the comparison leaves a statistic out, or ``None`` when both states hold its counts."""
    if second is None or second.shape != first.shape:
        return "the next state does not hold it with the same definition"
    if first.counts is None or second.counts is None:
        where = "the pinned state" if first.counts is None else "the next state"
        return f"{first.reason if first.counts is None else second.reason} " f"in {where}"
    for held, where in ((first, "the pinned state"), (second, "the next state")):
        if held.counts is not None and not any(held.counts):
            return f"it holds no records in {where}"
    return None


def _largest(values: Sequence[Tuple[str, Decimal]]) -> Optional[Tuple[str, Decimal]]:
    """The first of the largest drifts, or ``None`` when there are none."""
    largest: Optional[Tuple[str, Decimal]] = None
    for name, value in values:
        if largest is None or value > largest[1]:
            largest = (name, value)
    return largest


def compare_states(
    earlier: Profile,
    later: Profile,
    families: Sequence[ResponseFamily],
    rule: DriftRule,
) -> DriftReport:
    """The drift of each statistic both states hold, the ones left out, and whether it is stale."""
    before = statistics_of(earlier, families)
    after = statistics_of(later, families)
    metric = _METRIC[rule.metric]
    values: List[Tuple[str, Decimal]] = []
    left_out: List[Tuple[str, str]] = []
    for name in sorted(before):
        first, second = before[name], after.get(name)
        reason = _left_out_reason(first, second)
        if reason is not None:
            left_out.append((name, reason))
        else:
            assert second is not None and first.counts is not None and second.counts is not None
            values.append((name, metric(shares(first.counts), shares(second.counts))))
    largest = _largest(values)
    stale = largest is not None and largest[1] > rule.threshold
    return DriftReport(
        rule=rule,
        values=tuple(values),
        left_out=tuple(left_out),
        largest=largest,
        stale=stale,
    )
