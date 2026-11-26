# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Children per parent become a ``counts`` entry, or else a foreign key's spread over its parent's keys.

A relationship the counted layout can place is written as a ``counts`` entry whose ``children``
are the histogram's classes (:func:`count_children`, ADR 0016 section 7); the layout holds the
counts exactly, so the foreign key declares no draw. Every other relationship keeps the spread
below, which is ADR 0014 section 5.

ADR 0014 section 5. A profile ``relationships`` entry counts how many child rows
each parent row has: a ``marginalHistogram/1`` over that count, whose cells are
classes of parents (none, one, two, three or four, ...), and a ``mean/1``. For a
one-column number foreign key the classes are laid out over the parent's key
range in order: each class takes a share of the keys equal to its share of the
parents, by the Largest Remainder Method (KERNEL-SPEC section 5), and the
foreign key's draw weights it by its parents times their mean number of
children. The class of parents with no child gets weight 0. The generator then
snaps each drawn value to the nearest parent key (ADR 0011).

When the foreign key's own column publishes a histogram over its values, the
classes are laid out inside each of its bins, so the children that fall in each
key range stay the published count, and both statistics hold.

This is an approximation, and said to be one: a parent in a class whose mean is
``m`` receives a Poisson-like number of children, so about ``e^-m`` of them
receive none. Exact counts need the parents drawn first, a later change.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from flux_engine.author.basis import Label, from_profile
from flux_engine.author.columns import (
    FOREIGN_OWN,
    ColumnPlan,
    binned_cuts,
    cell_weights,
    count_cells,
    drawn_span,
    share_texts,
)
from flux_engine.author.fill import drawn_mean
from flux_engine.author.forms import grouped
from flux_engine.core.errors import FluxError
from flux_engine.core.lrm import largest_remainder
from flux_engine.generator.counts import (
    CountClass,
    check_minimum,
    class_counts,
    classes_of,
)
from flux_engine.seam.contract import LogicalKind
from flux_stats.definitions import MARGINAL_HISTOGRAM, MEAN

__all__ = [
    "CHILDREN_REASON",
    "COUNTED_REASON",
    "Counted",
    "Spread",
    "count_children",
    "spread_children",
]

_HISTOGRAM = MARGINAL_HISTOGRAM.identifier

#: The most of the foreign key's own bins the classes are laid out inside: more are merged
#: in runs of neighbours, so a histogram of many narrow bins costs no more than this many.
MOST_BUCKETS = 32
_MEAN = MEAN.identifier
_FREE = "F"

#: Why a foreign key laid out from children per parent draws as it does.
CHILDREN_REASON = (
    "Children per parent, from the profile's relationship histogram: each class of parents "
    "takes a share of the parent's keys equal to its share of the parents, and is weighted "
    "by its parents times their mean number of children, the class with none at 0 (ADR "
    "0014). An approximation: a parent of a class whose mean is m receives about a Poisson "
    "number of children, so about e^-m of them receive none."
)


@dataclass(frozen=True)
class Spread:
    """What :func:`spread_children` made of one relationship of the profile."""

    #: Pointers into the profile, each with how it was used, or why it was not.
    used: Tuple[Tuple[Tuple[str, ...], str], ...]
    unused: Tuple[Tuple[Tuple[str, ...], str], ...]


def _published(statistic: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The one published group of an ungrouped statistic, or ``None``."""
    groups = statistic.get("groups")
    if grouped(statistic) or not isinstance(groups, list) or len(groups) != 1:
        return None
    group = groups[0]
    return group if isinstance(group, Mapping) and group.get("status") == _FREE else None


_Histogram = Tuple[int, Mapping[str, Any], Mapping[str, Any]]
_Mean = Tuple[int, Mapping[str, Any]]


def _read_statistics(
    relationship: Mapping[str, Any], tokens: Tuple[str, ...]
) -> Tuple[Optional[_Histogram], Optional[_Mean], List[Tuple[Tuple[str, ...], str]]]:
    """The relationship's histogram and mean of children per parent, and its other published statistics.

    Each is the first of its kind that publishes one ungrouped group. The third value lists the
    statistics that are neither, each with why it adds no bound.
    """
    unused: List[Tuple[Tuple[str, ...], str]] = []
    statistics = relationship.get("statistics")
    histogram: Optional[_Histogram] = None
    mean: Optional[_Mean] = None
    for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
        if not isinstance(statistic, Mapping):
            continue
        group = _published(statistic)
        if group is None:
            continue
        definition = statistic.get("definition")
        if definition == _HISTOGRAM and histogram is None and len(statistic.get("bins", ())) >= 2:
            histogram = (index, statistic, group)
        elif definition == _MEAN and mean is None:
            mean = (index, group)
        else:
            unused.append(
                (
                    tokens + ("statistics", str(index)),
                    "children per parent are laid out from the histogram; this statistic adds "
                    "no bound to it",
                )
            )
    return histogram, mean, unused


def _class_means(
    edges: Sequence[Fraction], parents: Sequence[Fraction], children: Optional[Fraction]
) -> List[Fraction]:
    """Each class's mean number of children: the middle of its whole counts.

    The last class is closed and often open-ended (top-coded): when the
    relationship's total of *children* is known, its mean is the one that makes
    the classes add up to it, kept within the class; when it is not, the class
    takes its lower bound, the one count it is known to reach (ADR 0014).
    """
    count = len(parents)
    means: List[Fraction] = []
    for position in range(count):
        low = Fraction(math.ceil(edges[position]))
        top = edges[position + 1] if position == count - 1 else edges[position + 1] - 1
        high = Fraction(math.floor(top))
        means.append((low + max(high, low)) / 2)
    if not count:
        return means
    low, high = Fraction(math.ceil(edges[-2])), Fraction(edges[-1])
    if children is not None and parents[-1] > 0:
        rest = sum((parents[i] * means[i] for i in range(count - 1)), Fraction(0))
        solved = (children - rest) / parents[-1]
        means[-1] = min(max(solved, low), max(high, low))
    else:
        means[-1] = low
    return means


def _merged_classes(
    parents: Sequence[Fraction], means: Sequence[Fraction]
) -> Tuple[List[Fraction], List[Fraction]]:
    """At most :data:`MOST_BUCKETS` classes, neighbours merged: parents added, means weighted.

    A merged class keeps its parents and its children, so the spread it gets is
    the classes' together; only the split between them inside the run is lost.
    The class of parents with no child, the first, is never merged: its keys
    take no child, so the childless share holds whatever the merge.
    """
    count = len(parents)
    if count <= MOST_BUCKETS:
        return list(parents), list(means)
    alone = 1 if means[0] == 0 else 0
    runs = MOST_BUCKETS - alone
    rest = count - alone
    merged_parents: List[Fraction] = list(parents[:alone])
    merged_means: List[Fraction] = list(means[:alone])
    for index in range(runs):
        run = range(alone + index * rest // runs, alone + (index + 1) * rest // runs)
        total = sum((parents[i] for i in run), Fraction(0))
        children = sum((parents[i] * means[i] for i in run), Fraction(0))
        merged_parents.append(total)
        merged_means.append(children / total if total > 0 else Fraction(0))
    return merged_parents, merged_means


def _bucket_layout(
    start: int,
    keys: int,
    children: Fraction,
    shares: Sequence[Fraction],
    weights: Sequence[Fraction],
    order: Sequence[int],
) -> List[Tuple[int, int, Fraction]]:
    """The classes laid out over *keys* keys from *start*: (first key, keys, weight) in *order*.

    The keys go to the classes by the Largest Remainder Method over their shares
    of the parents, and the bucket's *children* to the classes that got a key, by
    their weights. A bucket with no children is one part of weight 0, so a key
    range the foreign key's own histogram publishes empty stays empty; and a
    bucket whose keys all went to classes of no weight (too few keys for its
    classes) keeps its children evenly over its keys, rather than losing them.
    """
    if children <= 0:
        return [(start, keys, Fraction(0))]
    names = [str(position).zfill(4) for position in range(len(shares))]
    texts = share_texts(list(shares))
    allotted = largest_remainder(
        keys, {name: Decimal(text) for name, text in zip(names, texts, strict=True)}
    )
    live = [position for position in order if allotted[names[position]] > 0]
    total = sum((weights[position] for position in live), Fraction(0))
    if total <= 0:
        return [(start, keys, children)]
    layout: List[Tuple[int, int, Fraction]] = []
    first = start
    for position in order:
        width = allotted[names[position]]
        if width <= 0:
            continue
        weight = children * weights[position] / total if total > 0 else Fraction(0)
        layout.append((first, width, weight))
        first += width
    return layout


def _edges(
    layout: Sequence[Tuple[int, int, Fraction]], high: int
) -> Tuple[List[int], List[Fraction]]:
    """Bin edges and weights from consecutive (first key, keys, weight) parts.

    Bins are half-open and the last is closed, so a part's keys are exactly the
    grid points of its bin. A last part of one key, the top key, cannot be a bin
    of its own: it takes the key below it from the part before, so both keep
    their weights, and only when that part is one key too do the two join.
    """
    starts = [first for first, _, _ in layout]
    weights = [Fraction(weight) for _, _, weight in layout]
    while len(starts) > 1 and starts[-1] >= high:
        if starts[-2] < high - 1:
            starts[-1] = high - 1
            break
        starts.pop()
        last = weights.pop()
        weights[-1] += last
    return starts + [high], weights


def _own_buckets(
    entry: Optional[Mapping[str, Any]],
    low: int,
    high: int,
    rows: int,
    minimum_cell: int,
    tokens: Tuple[str, ...],
) -> Optional[List[Tuple[int, int, Fraction]]]:
    """The foreign key's own histogram as (first key, keys, children) buckets within the keys."""
    statistics = entry.get("statistics") if isinstance(entry, Mapping) else None
    for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
        if not isinstance(statistic, Mapping) or statistic.get("definition") != _HISTOGRAM:
            continue
        group = _published(statistic)
        bins = statistic.get("bins")
        if group is None or not isinstance(bins, list) or len(bins) < 2:
            continue
        edges = [Fraction(Decimal(str(edge))) for edge in bins]
        total = group.get("k")
        weights, _ = cell_weights(
            minimum_cell,
            count_cells(group, "binCounts"),
            tokens + ("statistics", str(index)),
            "binCounts",
            _HISTOGRAM,
            total=_whole(total),
        )
        buckets: List[Tuple[int, int, Fraction]] = []
        last = len(weights) - 1
        for position, weight in enumerate(weights):
            first = max(low, math.ceil(edges[position]))
            top = edges[position + 1] if position == last else edges[position + 1] - 1
            final = min(high, math.floor(top))
            if final >= first:
                buckets.append((first, final - first + 1, max(weight, Fraction(0))))
        scale = sum((bucket[2] for bucket in buckets), Fraction(0))
        if scale > 0:
            return [
                (first, keys, Fraction(rows) * weight / scale)
                for first, keys, weight in _merged(buckets)
            ]
    return None


def _quantile_buckets(
    entry: Optional[Mapping[str, Any]],
    low: int,
    high: int,
    rows: int,
    tokens: Tuple[str, ...],
) -> Optional[Tuple[List[Tuple[int, int, Fraction]], List[Tuple[str, ...]]]]:
    """The foreign key's binned quantiles as (first key, keys, children) buckets within the keys.

    Each cut says what share of the rows lies below a value, so the keys below it hold
    that share, as a histogram's bins would. Returns the buckets and the statistics whose
    cuts fall inside the parent's keys, or ``None`` when none does.
    """
    cuts = binned_cuts(entry, tokens)
    buckets: List[Tuple[int, int, Fraction]] = []
    used: List[Tuple[str, ...]] = []
    start, below = low, Fraction(0)
    for value, level, place in cuts:
        key = math.ceil(Fraction(value))
        if key <= start or key > high:
            continue
        buckets.append((start, key - start, Fraction(rows) * (level - below)))
        start, below = key, level
        if place[:-2] not in used:
            used.append(place[:-2])
    if not used:
        return None
    buckets.append((start, high - start + 1, Fraction(rows) * (1 - below)))
    return _merged(buckets), used


def _merged(buckets: Sequence[Tuple[int, int, Fraction]]) -> List[Tuple[int, int, Fraction]]:
    """*buckets* in at most :data:`MOST_BUCKETS` runs of neighbours, their keys and weights added.

    The buckets are consecutive key ranges, so a run of them is one range. Each
    run takes about as many buckets as every other, in order.
    """
    if len(buckets) <= MOST_BUCKETS:
        return list(buckets)
    runs: List[Tuple[int, int, Fraction]] = []
    count = len(buckets)
    for index in range(MOST_BUCKETS):
        part = buckets[index * count // MOST_BUCKETS : (index + 1) * count // MOST_BUCKETS]
        if part:
            first = part[0][0]
            keys = part[-1][0] + part[-1][1] - first
            runs.append((first, keys, sum((weight for _, _, weight in part), Fraction(0))))
    return runs


def _whole(value: Any) -> Optional[int]:
    """*value* when it is a whole number above zero; a boolean is not a count."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _foreign_mean(
    entry: Optional[Mapping[str, Any]], bits: int
) -> Optional[Tuple[Fraction, List[Tuple[str, ...]]]]:
    """The foreign key's published mean, and the metrics or statistics it was read from.

    ``sum`` over ``count`` first, with a ``mean/1`` statistic that gives the same mean on the
    profile's grid read beside it; else the ``mean/1`` statistic alone.
    """
    found = entry.get("metrics") if isinstance(entry, Mapping) else None
    metrics: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    mean: Optional[Fraction] = None
    sources: List[Tuple[str, ...]] = []
    total, count = metrics.get("sum"), metrics.get("count")
    if isinstance(total, Mapping) and isinstance(count, Mapping):
        value = _whole(count.get("value"))
        if total.get("status") == _FREE and count.get("status") == _FREE and value is not None:
            mean = Fraction(Decimal(str(total.get("value")))) / value
            sources = [("metrics", "sum"), ("metrics", "count")]
    statistics = entry.get("statistics") if isinstance(entry, Mapping) else None
    for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
        if isinstance(statistic, Mapping) and statistic.get("definition") == _MEAN:
            group = _published(statistic)
            k = _whole(group.get("k")) if group is not None else None
            if group is not None and k is not None:
                stated = Fraction(int(str(group.get("Sx"))), k * 2**bits)
                if mean is None:
                    return stated, [("statistics", str(index))]
                if abs(stated - mean) <= Fraction(1, 2**bits):
                    sources.append(("statistics", str(index)))
                break
    return (mean, sources) if mean is not None else None


def spread_children(
    relationship: Mapping[str, Any],
    tokens: Tuple[str, ...],
    child: ColumnPlan,
    child_kind: LogicalKind,
    child_rows: int,
    child_entry: Optional[Mapping[str, Any]],
    child_tokens: Tuple[str, ...],
    parent: ColumnPlan,
    parent_kind: LogicalKind,
    *,
    minimum_cell: int,
    fractional_bits: int,
) -> Spread:
    """Lay one relationship's children per parent out as *child*'s spread over *parent*'s keys.

    *relationship* is the profile's entry, at *tokens*; *child* is the plan of the
    one-column foreign key, and *parent* that of the key it references. Returns
    which of the entry's statistics were used, and why the others were not.
    """
    used: List[Tuple[Tuple[str, ...], str]] = []
    statistics = relationship.get("statistics")
    histogram, mean, others = _read_statistics(relationship, tokens)
    unused = list(others)
    if child_kind is not LogicalKind.INT or parent_kind is not LogicalKind.INT:
        why = "a key of other than whole numbers has no range of keys to lay children over"
        return Spread((), tuple(unused) + _all(tokens, statistics, why))
    span = drawn_span(parent)
    if not span:
        why = "the key it references has no span to lay children over"
        return Spread((), tuple(unused) + _all(tokens, statistics, why))
    low, high = math.ceil(min(span)), math.floor(max(span))
    checked = child.declaration.get("range")
    if isinstance(checked, Mapping):
        low = max(low, math.ceil(Fraction(Decimal(str(checked.get("min"))))))
        high = min(high, math.floor(Fraction(Decimal(str(checked.get("max"))))))
    if high <= low:
        why = "the parent's keys leave no span to lay children over"
        return Spread((), tuple(unused) + _all(tokens, statistics, why))
    children_total: Optional[Fraction] = None
    if mean is not None:
        index, group = mean
        children_total = Fraction(int(str(group.get("Sx"))), 2**fractional_bits)
    if histogram is None:
        if mean is not None and children_total is not None:
            parents_k = _whole(mean[1].get("k"))
            expected = Fraction(child_rows, parents_k) if parents_k is not None else None
            stated = children_total / parents_k if parents_k is not None else None
            pointer = tokens + ("statistics", str(mean[0]))
            if (
                expected is not None
                and stated is not None
                and abs(stated - expected) <= expected / 10
            ):
                used.append(
                    (
                        pointer,
                        "checked: the children the run draws evenly over the parent's keys "
                        "give this mean",
                    )
                )
            else:
                unused.append(
                    (
                        pointer,
                        "its mean is not the child rows over the parent rows the bundle "
                        "draws, and a mean alone does not shape the spread",
                    )
                )
        return Spread(tuple(used), tuple(unused))
    index, statistic, group = histogram
    edges = [Fraction(Decimal(str(edge))) for edge in statistic["bins"]]
    parents, _ = cell_weights(
        minimum_cell,
        count_cells(group, "binCounts"),
        tokens + ("statistics", str(index)),
        "binCounts",
        _HISTOGRAM,
        total=_whole(group.get("k")),
    )
    means = _class_means(edges, parents, children_total)
    parents, means = _merged_classes(parents, means)
    weights = [parents[i] * means[i] for i in range(len(parents))]
    if sum(weights, Fraction(0)) <= 0:
        why = "the histogram gives no parent a child"
        return Spread((), tuple(unused) + _all(tokens, statistics, why))
    whole = [(low, high - low + 1, Fraction(child_rows))]
    buckets = _own_buckets(child_entry, low, high, child_rows, minimum_cell, child_tokens)
    quantiles: Optional[Tuple[List[Tuple[int, int, Fraction]], List[Tuple[str, ...]]]] = None
    if buckets is None:
        quantiles = _quantile_buckets(child_entry, low, high, child_rows, child_tokens)
        buckets = quantiles[0] if quantiles is not None else None
    buckets = buckets or whole
    descending = sorted(range(len(parents)), key=lambda i: (-means[i], i))
    ascending = list(reversed(descending))
    target = _foreign_mean(child_entry, fractional_bits)
    best: Optional[Tuple[List[int], List[Fraction]]] = None
    for order in (descending, ascending):
        layout = [
            part
            for first, keys, children in buckets
            for part in _bucket_layout(first, keys, children, parents, weights, order)
        ]
        found = _edges(layout, high)
        if best is None:
            best = found
        elif target is not None:

            def gap(candidate: Tuple[List[int], List[Fraction]]) -> Fraction:
                spread = drawn_mean([Fraction(e) for e in candidate[0]], candidate[1], Fraction(1))
                return abs(spread - target[0])

            if gap(found) < gap(best):
                best = found
        if target is None:
            break
    assert best is not None
    _declare(child, best[0], best[1], tokens + ("statistics", str(index)))
    used.append((tokens + ("statistics", str(index)), "laid out over the parent's keys"))
    if mean is not None:
        used.append(
            (
                tokens + ("statistics", str(mean[0])),
                "the mean the classes' children add up to, which sets the open class's mean",
            )
        )
    if target is not None:
        for name in target[1]:
            used.append(
                (
                    child_tokens + name,
                    "decides which end of the parent's keys the parents with more children lie at",
                )
            )
    if quantiles is not None:
        for place in quantiles[1]:
            used.append(
                (place, "children per parent are laid out inside the key ranges its quantiles cut")
            )
    elif buckets != whole:
        statistics_of = child_entry.get("statistics") if isinstance(child_entry, Mapping) else None
        for position, entry in enumerate(statistics_of if isinstance(statistics_of, list) else ()):
            if (
                isinstance(entry, Mapping)
                and entry.get("definition") == _HISTOGRAM
                and not grouped(entry)
            ):
                used.append(
                    (
                        child_tokens + ("statistics", str(position)),
                        "children per parent are laid out inside each of its bins",
                    )
                )
                break
    used += _check_ends(child, child_entry, child_tokens, low, high)
    return Spread(tuple(used), tuple(unused))


#: Why a foreign key on the counted layout declares no draw of its own.
COUNTED_REASON = (
    "Children per parent, from the profile's relationship histogram: the bundle's counts entry "
    "gives each class of parents its share of the parent rows, and the counted layout gives "
    "every parent a whole number of children, so the foreign key declares no draw of its own "
    "(ADR 0016)."
)

_COUNTED_SETS = (
    "the counted layout sets the foreign key from the parent rows, so this statistic of its own "
    "values adds no bound to it"
)

#: The keys of a column's declaration that draw its values, which the counted layout sets instead.
_DRAWN = (
    "levels",
    "levelWeights",
    "bins",
    "binWeights",
    "range",
    "mean",
    "stddev",
    "rate",
    "support",
    "nullFraction",
)


#: The keys of a column's declaration that draw it given another column.
_DRAWN_GIVEN = ("given", "binWeightsGiven", "levelWeightsGiven")


def draws_itself(child: ColumnPlan) -> bool:
    """Whether *child* declares a draw of its own, which a layout that sets the column refuses (``FLX-3403``)."""
    return any(key in child.declaration for key in (*_DRAWN, *_DRAWN_GIVEN))


@dataclass(frozen=True)
class Counted:
    """What :func:`count_children` made of one relationship of the profile."""

    #: The ``children`` shape, and ``minChildren`` when the source enforces a child per parent.
    members: Mapping[str, Any]
    used: Tuple[Tuple[Tuple[str, ...], str], ...]
    unused: Tuple[Tuple[Tuple[str, ...], str], ...]
    #: What the entry assumes where the profile withheld a class of parents, else ``None``.
    assumption: Optional[str]
    #: Why the relationship is not laid out by counts although its histogram was read, else
    #: ``None``. The other members are then empty.
    why_not: Optional[str] = None


def _whole_edges(bins: Any) -> Optional[List[int]]:
    """The histogram's bin edges as whole numbers, or ``None`` when one is not."""
    edges: List[int] = []
    for edge in bins if isinstance(bins, list) else ():
        try:
            value = Fraction(Decimal(str(edge)))
        except ArithmeticError:
            return None
        if value.denominator != 1:
            return None
        edges.append(int(value))
    return edges if len(edges) >= 2 else None


def _open_edge(low: int, mean: Fraction, held: int, published: int) -> int:
    """The upper edge of the open last class whose even spread has the mean *mean*.

    The class holds *held* parents, and its ``j``-th parent takes
    ``low + floor(j * width / held)`` children, ``width`` being its counts from *low* to the
    edge (ADR 0016 section 2). The sum of those is ``held * low`` plus
    ``((width - 1) * (held - 1) + gcd(width, held) - 1) / 2``, which never falls as the edge
    rises, so the width whose total lies nearest ``held * mean`` is found by bisection. The class
    holds at least two counts, since a shape's edges ascend. A class with no parent keeps the
    published edge.
    """
    if held <= 0:
        return max(published, low + 1)
    target = mean * held

    def total(width: int) -> int:
        return held * low + ((width - 1) * (held - 1) + math.gcd(width, held) - 1) // 2

    if total(2) >= target:
        return low + 1
    above = 4
    while total(above) < target and above < 10**18:
        above *= 2
    below = 2
    while above - below > 1:
        middle = (above + below) // 2
        if total(middle) >= target:
            above = middle
        else:
            below = middle
    width = above if abs(total(above) - target) < abs(target - total(below)) else below
    return low + width - 1


def _span(low: int, high: int) -> str:
    return str(low) if low == high else f"{low} to {high}"


def count_children(
    relationship: Mapping[str, Any],
    tokens: Tuple[str, ...],
    child_rows: int,
    parent_rows: int,
    *,
    minimum_cell: int,
    fractional_bits: int,
    one_to_one: bool = False,
) -> Optional[Counted]:
    """The ``counts`` entry's members for one relationship's histogram of children per parent.

    ADR 0016 section 7. The entry's ``children`` are the histogram's classes. Each class's weight
    is its published count of parents, a zero included; a count the profile withheld is filled
    from the parents the published counts leave (ADR 0014 section 3), which never assumes a class
    is empty. Only a class the profile marks structural is known to be: with ``childless`` the
    class of none, the parents with no child, takes no weight and ``minChildren`` is 1. The open
    last class's upper edge is chosen so that its even spread has the mean :func:`_class_means`
    solves from the published total of children, or from the child rows when none is published
    (the layout gives every child a parent, so they are the total it holds).

    Returns ``None`` when the relationship publishes no histogram the layout can read: edges that
    are not whole ascending counts, no parent in any class, or a minimum the child rows cannot
    meet. The caller then lays the relationship out as before.

    A *one_to_one* relationship gives no parent a second child, so its entry takes
    ``levels: ["0", "1"]`` (ADR 0016 section 7): the weight of the class of none, and that of the
    class of one, which a last class closed at two or more is read as, since no parent can have
    more. A class that starts above one and holds parents is not laid out: the result then names
    that in ``why_not``. The mean is implied by the two weights, so it adds no bound.
    """
    histogram, mean, others = _read_statistics(relationship, tokens)
    if histogram is None:
        return None
    index, statistic, group = histogram
    pointer = tokens + ("statistics", str(index))
    read = _whole_cells(statistic, group)
    if read is None:
        return None
    edges, cells = read
    structural = relationship.get("structural")
    childless = isinstance(structural, list) and "childless" in structural
    # The first class is the parents with no child when it holds the one count 0.
    none_class = len(edges) > 2 and edges[0] == 0 and edges[1] == 1
    withheld = [position for position, cell in enumerate(cells) if cell.count is None]
    if childless and none_class:
        cells[0] = replace(cells[0], count=0)
        withheld = [position for position in withheld if position != 0]
    parents, _ = cell_weights(
        minimum_cell,
        cells,
        pointer,
        "binCounts",
        _HISTOGRAM,
        total=_whole(group.get("k")),
    )
    if sum(parents, Fraction(0)) <= 0:
        return None
    total: Optional[Fraction] = None
    if mean is not None:
        total = Fraction(int(str(mean[1].get("Sx"))), 2**fractional_bits)
    elif not one_to_one:
        # The layout gives every child a parent, so the child rows are the total it must hold.
        total = Fraction(child_rows)
    means = _class_means([Fraction(edge) for edge in edges], parents, total)
    shares = share_texts(parents)
    classes = [
        CountClass(
            edges[position],
            edges[position + 1] - (1 if position < len(parents) - 1 else 0),
            Decimal(shares[position]),
        )
        for position in range(len(parents))
    ]
    if one_to_one:
        return _count_one_to_one(
            parents, classes, withheld, none_class, childless, pointer, mean, others,
            child_rows, parent_rows,
        )  # fmt: skip
    return _count_open_classes(
        classes, edges, means, shares, withheld, childless, pointer, tokens, mean, others,
        child_rows, parent_rows,
    )  # fmt: skip


def _whole_cells(
    statistic: Mapping[str, Any], group: Mapping[str, Any]
) -> Optional[Tuple[List[int], List[Any]]]:
    """The whole ascending edges of a histogram and its cells, or ``None`` when it has no such."""
    edges = _whole_edges(statistic.get("bins"))
    if edges is None or any(low >= high for low, high in zip(edges, edges[1:], strict=False)):
        return None
    cells = count_cells(group, "binCounts")
    if len(cells) != len(edges) - 1:
        return None
    return edges, cells


def _count_open_classes(
    classes: Sequence[CountClass],
    edges: Sequence[int],
    means: Sequence[Fraction],
    shares: Sequence[str],
    withheld: Sequence[int],
    childless: bool,
    pointer: Tuple[str, ...],
    tokens: Tuple[str, ...],
    mean: Optional[Tuple[int, Mapping[str, Any]]],
    others: Sequence[Tuple[Tuple[str, ...], str]],
    child_rows: int,
    parent_rows: int,
) -> Optional[Counted]:
    """The members of an entry whose last class is open, with the classes' weights."""
    held = class_counts(parent_rows, classes)
    top = _open_edge(edges[-2], means[-1], held[-1], edges[-1])
    members: dict[str, Any] = {
        "children": {
            "bins": [str(edge) for edge in edges[:-1]] + [str(top)],
            "binWeights": shares,
        }
    }
    if childless:
        members["minChildren"] = 1
    try:
        shape = classes_of(members["children"])
        if childless:
            if shape[-1].high < 1:
                return None
            check_minimum(child_rows, parent_rows, 1)
    except FluxError:
        return None
    used = [(pointer, "the classes of the counts entry's children, with their weights")]
    if mean is not None:
        used.append(
            (
                tokens + ("statistics", str(mean[0])),
                "the total of children the classes add up to, which sets the open class's "
                "upper edge",
            )
        )
    return Counted(members, tuple(used), tuple(others), _withheld(classes, withheld))


def _withheld(classes: Sequence[CountClass], withheld: Sequence[int]) -> Optional[str]:
    """The assumption a filled class of parents is stated as, or ``None`` when none was."""
    if not withheld:
        return None
    named = "; ".join(
        f"{_span(classes[position].low, classes[position].high)} children" for position in withheld
    )
    return (
        f"The profile withheld the count of parents with {named}, so the counts entry fills "
        "each from the parents the published counts leave (ADR 0014 section 3), and never "
        "assumes a class is empty."
    )


def _count_one_to_one(
    parents: Sequence[Fraction],
    classes: Sequence[CountClass],
    withheld: Sequence[int],
    none_class: bool,
    childless: bool,
    pointer: Tuple[str, ...],
    mean: Optional[Tuple[int, Mapping[str, Any]]],
    others: Sequence[Tuple[Tuple[str, ...], str]],
    child_rows: int,
    parent_rows: int,
) -> Optional[Counted]:
    """The members of a ``one_to_one`` relationship's entry: the weights of none and of one."""
    if not none_class or child_rows > parent_rows:
        return None
    if any(weight > 0 for weight in parents[2:]):
        return Counted(
            {},
            (),
            (),
            None,
            "its histogram has a class of parents with more than one child, which a one_to_one "
            "relationship cannot hold",
        )
    members: dict[str, Any] = {
        "children": {"levels": ["0", "1"], "levelWeights": share_texts([parents[0], parents[1]])}
    }
    if childless:
        members["minChildren"] = 1
        try:
            check_minimum(child_rows, parent_rows, 1)
        except FluxError:
            return None
    used = [(pointer, "the weights of the counts entry's levels 0 and 1")]
    unused = list(others)
    if mean is not None:
        unused.append(
            (
                pointer[:-1] + (str(mean[0]),),
                "the mean of children per parent of a one_to_one relationship follows from its "
                "classes of none and of one, so it adds no bound",
            )
        )
    kept = [position for position in withheld if position < 2]
    return Counted(members, tuple(used), tuple(unused), _withheld(classes, kept))


def declare_counted(
    child: ColumnPlan,
    assumption: Optional[str],
    *,
    reason: str = COUNTED_REASON,
    how: str = "a foreign key, set by the counted layout from the profile's children per parent",
    sets: str = _COUNTED_SETS,
) -> None:
    """Take *child*, a foreign key on the counted layout, off every draw of its own.

    *reason*, *how* and *sets* say why, how the key reads and why its own statistics add no bound;
    a history's parent key (ADR 0017 section 3) says its own.

    The layout sets the key from the parent's rows (ADR 0016 section 1), so the foreign key
    declares no levels, bins, range, mean or null share (a share of none, which gives every child
    a parent as the layout does), and no property scores its values: they are the parents' keys,
    in the order the counts allot. What its own statistics said is passed over, with the reason.
    """
    for key in _DRAWN:
        child.declaration.pop(key, None)
    passed = [(pointer, sets if why == FOREIGN_OWN else why) for pointer, why in child.passed]
    cited: List[Tuple[str, ...]] = []
    kept: List[Label] = []
    for label in child.labels:
        if label.tokens[:1] in tuple((key,) for key in _DRAWN):
            if label.basis.statistic is not None:
                cited.append(label.basis.statistic[0])
        else:
            kept.append(label)
    child.labels = kept
    for prop in child.properties:
        cited.extend(read[0] for read in prop.reads)
        cited.extend(
            label.basis.statistic[0] for label in prop.labels if label.basis.statistic is not None
        )
    child.properties = []
    for place in cited:
        if all(item[0] != place for item in passed):
            passed.append((place, sets))
    child.passed = passed
    child.published_range = None
    child.assumptions = [reason] + ([assumption] if assumption else [])
    child.choices = [reason]
    child.placeholder = False
    child.how = how


def _all(
    tokens: Tuple[str, ...], statistics: Any, why: str
) -> Tuple[Tuple[Tuple[str, ...], str], ...]:
    return tuple(
        (tokens + ("statistics", str(index)), why)
        for index, _ in enumerate(statistics if isinstance(statistics, list) else ())
    )


def _declare(
    child: ColumnPlan,
    edges: Sequence[int],
    weights: Sequence[Fraction],
    cited: Tuple[str, ...],
) -> None:
    """Declare *child*'s spread: the laid-out bins and weights, which rest on a statistic of the profile.

    The edges and the weights are laid out from the relationship's children-per-parent histogram,
    at *cited*, by the method :data:`CHILDREN_REASON` says, so the register cites that statistic and
    not an assumption: a number that rests on the profile is cited as resting on it.
    """
    child.declaration.pop("levels", None)
    child.declaration["bins"] = [str(edge) for edge in edges]
    child.declaration["binWeights"] = share_texts(list(weights))
    child.labels = [
        label
        for label in child.labels
        if label.tokens[:1] not in (("bins",), ("binWeights",), ("levels",))
    ]
    child.labels.append(Label(("bins",), from_profile(cited, _HISTOGRAM)))
    child.labels.append(Label(("binWeights",), from_profile(cited, _HISTOGRAM)))
    child.assumptions = [
        sentence
        for sentence in child.assumptions
        if sentence not in child.weight_notes
        and "drawn evenly over the span" not in sentence
        and "placeholder" not in sentence
    ]
    child.assumptions.append(CHILDREN_REASON)
    child.choices.append(CHILDREN_REASON)
    child.placeholder = False
    child.how = "a foreign key, spread over its parent's keys by the profile's children per parent"


_RELATED = (
    "the relationship's children per parent shape the foreign key's draw; this statistic of "
    "its own values adds no bound to it"
)


def _check_ends(
    child: ColumnPlan,
    entry: Optional[Mapping[str, Any]],
    tokens: Tuple[str, ...],
    low: int,
    high: int,
) -> List[Tuple[Tuple[str, ...], str]]:
    """Check a foreign key's published ``min`` and ``max`` against its parent's key span.

    Returns the ones inside it, as used. The rest are left to the plan's unused passes, each
    with the key span it lies outside. A foreign key that now has a spread no longer says its
    own statistics are passed over for want of one (:data:`FOREIGN_OWN`).
    """
    used: List[Tuple[Tuple[str, ...], str]] = []
    metrics = entry.get("metrics") if isinstance(entry, Mapping) else None
    kept = [item for item in child.passed if item[1] != FOREIGN_OWN]
    for pointer, reason in child.passed:
        if reason == FOREIGN_OWN:
            kept.append((pointer, _RELATED))
    for name in ("min", "max"):
        metric = metrics.get(name) if isinstance(metrics, Mapping) else None
        if not isinstance(metric, Mapping) or metric.get("status") != _FREE:
            continue
        value = Fraction(Decimal(str(metric.get("value"))))
        place = tokens + ("metrics", name)
        if low <= value <= high:
            used.append((place, "checked: it lies within the parent's keys"))
            continue
        kept = [item for item in kept if item[0] != place]
        kept.append(
            (
                place,
                f"the published {name} is {value}, and the parent's keys span {low} to {high}: "
                "a foreign key is drawn over them only",
            )
        )
    child.passed = kept
    return used
