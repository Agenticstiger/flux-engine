# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Children per parent become a foreign key's spread over its parent's keys.

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
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from flux_engine.author.basis import Label, assumed
from flux_engine.author.columns import (
    ColumnPlan,
    cell_weights,
    count_cells,
    drawn_span,
    share_texts,
)
from flux_engine.author.fill import drawn_mean
from flux_engine.core.lrm import largest_remainder
from flux_engine.seam.contract import LogicalKind
from flux_stats.definitions import MARGINAL_HISTOGRAM, MEAN

__all__ = ["CHILDREN_REASON", "Spread", "spread_children"]

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
    if "groupBy" in statistic or not isinstance(groups, list) or len(groups) != 1:
        return None
    group = groups[0]
    return group if isinstance(group, Mapping) and group.get("status") == _FREE else None


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
    """The foreign key's published mean, and the metrics or statistic it was read from."""
    found = entry.get("metrics") if isinstance(entry, Mapping) else None
    metrics: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    total, count = metrics.get("sum"), metrics.get("count")
    if isinstance(total, Mapping) and isinstance(count, Mapping):
        value = _whole(count.get("value"))
        if total.get("status") == _FREE and count.get("status") == _FREE and value is not None:
            mean = Fraction(Decimal(str(total.get("value")))) / value
            return mean, [("metrics", "sum"), ("metrics", "count")]
    statistics = entry.get("statistics") if isinstance(entry, Mapping) else None
    for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
        if isinstance(statistic, Mapping) and statistic.get("definition") == _MEAN:
            group = _published(statistic)
            k = _whole(group.get("k")) if group is not None else None
            if group is not None and k is not None:
                return Fraction(int(str(group.get("Sx"))), k * 2**bits), [
                    ("statistics", str(index))
                ]
    return None


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
    unused: List[Tuple[Tuple[str, ...], str]] = []
    statistics = relationship.get("statistics")
    histogram: Optional[Tuple[int, Mapping[str, Any], Mapping[str, Any]]] = None
    mean: Optional[Tuple[int, Mapping[str, Any]]] = None
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
    buckets = _own_buckets(child_entry, low, high, child_rows, minimum_cell, child_tokens) or [
        (low, high - low + 1, Fraction(child_rows))
    ]
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
    _declare(child, best[0], best[1])
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
    if buckets != [(low, high - low + 1, Fraction(child_rows))]:
        statistics_of = child_entry.get("statistics") if isinstance(child_entry, Mapping) else None
        for position, entry in enumerate(statistics_of if isinstance(statistics_of, list) else ()):
            if (
                isinstance(entry, Mapping)
                and entry.get("definition") == _HISTOGRAM
                and "groupBy" not in entry
            ):
                used.append(
                    (
                        child_tokens + ("statistics", str(position)),
                        "children per parent are laid out inside each of its bins",
                    )
                )
                break
    return Spread(tuple(used), tuple(unused))


def _all(
    tokens: Tuple[str, ...], statistics: Any, why: str
) -> Tuple[Tuple[Tuple[str, ...], str], ...]:
    return tuple(
        (tokens + ("statistics", str(index)), why)
        for index, _ in enumerate(statistics if isinstance(statistics, list) else ())
    )


def _declare(child: ColumnPlan, edges: Sequence[int], weights: Sequence[Fraction]) -> None:
    """Declare *child*'s spread: the laid-out bins and weights, each assumed with the method."""
    child.declaration.pop("levels", None)
    child.declaration["bins"] = [str(edge) for edge in edges]
    child.declaration["binWeights"] = share_texts(list(weights))
    child.labels = [
        label
        for label in child.labels
        if label.tokens[:1] not in (("bins",), ("binWeights",), ("levels",))
    ]
    child.labels.append(Label(("bins",), assumed(CHILDREN_REASON)))
    child.labels.append(Label(("binWeights",), assumed(CHILDREN_REASON)))
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
