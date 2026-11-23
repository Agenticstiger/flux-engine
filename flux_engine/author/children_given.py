# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Children per parent given a column of the parent (ADR 0037 decisions 4 and 6).

A profile of format 1-4-0 can publish a relationship's ``marginalHistogram/1`` of children per
parent grouped by a ``category`` column of the parent (``groupBy``) or by the bins of a ``number``
or ``timestamp`` one (``groupByBins``). ``fluxsim init`` writes such a grouping as the ``given`` and
``childrenGiven`` of the relationship's ``counts`` entry, beside the ``children`` that the
ungrouped histogram gives. Failing an ungrouped histogram, the sum of the groups is the
``children`` (:func:`with_summed_groups`).

**What a grouping must be.** The generator refuses a given column that is no class to read from a
final row of the parent: one with a null fraction, one a summary over this relationship's children
sets, a key, a column drawn given a column of a linked row, or a column no rule or draw of the
parent gives classes. Each is passed over here, in the ledger, with the reason, and the entry then
has no ``given``. A zero ``nullFraction`` that init wrote for a number column it knew nothing of is
dropped from a column that becomes the given one, since an absent share draws no null. The classes
of a category column are its declared levels; the classes of a binned one are its bins, keyed by the
text of each lower edge as the column's own ``bins`` write it, and the statistic's bins must be
those. Its ``bins`` must also be the ungrouped histogram's, so that a weight list has the classes of
the ``children`` shape.

A class the profile publishes no group for takes the shape's own weights, which the plan states. A
count a group withheld is filled as an ungrouped one is (ADR 0014 section 3). The entry has one
``given`` column: the first grouping that can be used, in the document's order.

Nothing here reads a record: it reads counts a profiler released, which a noisy one releases as
the point estimate of its noisy cells.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.author.children import Counted, _read_statistics, _whole, _whole_edges
from flux_engine.author.columns import (
    ColumnPlan,
    _spell,
    cell_weights,
    count_cells,
    share_texts,
)
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.coverage import printable
from flux_stats.definitions import MARGINAL_HISTOGRAM

__all__ = ["Given", "GivenParent", "given_children", "with_summed_groups"]

_HISTOGRAM = MARGINAL_HISTOGRAM.identifier
_FREE = "F"
_NUMERIC = (LogicalKind.INT, LogicalKind.DECIMAL, LogicalKind.DATE, LogicalKind.TIMESTAMP)

_Note = Tuple[Tuple[str, ...], str]


@dataclass(frozen=True)
class GivenParent:
    """The parent expose's columns, as far as a given column of a ``counts`` entry reads them."""

    plans: Mapping[str, ColumnPlan]
    kinds: Mapping[str, LogicalKind]
    #: Each column that is no class to read, with why: a key, a rule or a summary sets it, or it
    #: is drawn given a column of a linked row.
    held_back: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Given:
    """What the grouped statistics of a relationship came to: the members, and what was said."""

    #: ``given`` and ``childrenGiven``, or empty when no grouping could be used.
    members: Mapping[str, Any]
    used: Tuple[_Note, ...] = ()
    unused: Tuple[_Note, ...] = ()
    #: What the entry assumes, as sentences a person reads.
    assumptions: Tuple[str, ...] = ()
    #: The given column, and whether its zero ``nullFraction`` is to be dropped.
    column: Optional[str] = None
    drop_null_fraction: bool = False


def _grouping(statistic: Mapping[str, Any]) -> Optional[Tuple[str, bool]]:
    """The parent column a relationship statistic is grouped by, and whether by bins."""
    by = statistic.get("groupBy")
    if isinstance(by, str):
        return by, False
    bins = statistic.get("groupByBins")
    if isinstance(bins, Mapping) and isinstance(bins.get("column"), str):
        return str(bins["column"]), True
    return None


def with_summed_groups(relationship: Mapping[str, Any]) -> Mapping[str, Any]:
    """*relationship* with an ungrouped histogram formed from a grouping's groups when it has none.

    The groups of a grouped ``marginalHistogram/1`` hold every parent once, so their cells add to
    the ungrouped histogram. It is formed from the first grouped statistic whose every cell is
    published, and added after the entry's own statistics, where no pointer reaches it; it names the
    statistic it sums in ``summedFrom``. An entry that has an ungrouped histogram, or
    no such grouping, is returned as it is.
    """
    statistics = relationship.get("statistics")
    listed = statistics if isinstance(statistics, list) else []
    if any(
        isinstance(item, Mapping)
        and item.get("definition") == _HISTOGRAM
        and _grouping(item) is None
        for item in listed
    ):
        return relationship
    for index, item in enumerate(listed):
        if (
            not isinstance(item, Mapping)
            or item.get("definition") != _HISTOGRAM
            or _grouping(item) is None
        ):
            continue
        summed = _sum_groups(item, index)
        if summed is not None:
            return {**relationship, "statistics": [*listed, summed]}
    return relationship


def _sum_groups(statistic: Mapping[str, Any], source: int) -> Optional[Dict[str, Any]]:
    edges = _whole_edges(statistic.get("bins"))
    groups = statistic.get("groups")
    if edges is None or not isinstance(groups, list) or not groups:
        return None
    total = [0] * (len(edges) - 1)
    for group in groups:
        if not isinstance(group, Mapping) or group.get("status") != _FREE:
            return None
        cells = count_cells(group, "binCounts")
        if len(cells) != len(total) or any(cell.count is None for cell in cells):
            return None
        total = [now + (cell.count or 0) for now, cell in zip(total, cells, strict=True)]
    return {
        "summedFrom": source,
        "definition": _HISTOGRAM,
        "bins": list(statistic["bins"]),
        "sampleSize": statistic.get("sampleSize"),
        "disclosure": statistic.get("disclosure"),
        "groups": [{"group": "", "k": sum(total), "binCounts": total, "status": _FREE}],
    }


def given_children(
    relationship: Mapping[str, Any],
    tokens: Tuple[str, ...],
    counted: Counted,
    parent: GivenParent,
    *,
    minimum_cell: int,
) -> Optional[Given]:
    """The ``given`` and ``childrenGiven`` of the entry written for *counted*, or ``None``.

    ``None`` when the relationship publishes no statistic grouped by a parent column, so an entry
    with none is written as it was. Otherwise the first grouping that can be used gives the members,
    and each grouping that cannot is passed over with its reason.
    """
    found = _read_statistics(relationship, tokens)[0]
    shape = counted.members.get("children")
    candidates = [
        (index, item, grouping)
        for index, item in enumerate(relationship.get("statistics") or ())
        if isinstance(item, Mapping)
        and item.get("definition") == _HISTOGRAM
        and (grouping := _grouping(item)) is not None
    ]
    if not candidates:
        return None
    unused: List[_Note] = []
    if found is None or not isinstance(shape, Mapping) or "bins" not in shape:
        why = "the entry's children are not a histogram of whole counts, which a grouping weighs"
        return Given(
            {}, unused=tuple((tokens + ("statistics", str(i)), why) for i, _, _ in candidates)
        )
    edges = _whole_edges(found[1].get("bins")) or []
    structural = relationship.get("structural")
    childless = isinstance(structural, list) and "childless" in structural
    none_class = len(edges) > 2 and edges[0] == 0 and edges[1] == 1
    summed = found[1].get("summedFrom")
    base: List[_Note] = (
        [(tokens + ("statistics", str(summed)), "its groups add up to the entry's children")]
        if isinstance(summed, int)
        else []
    )
    chosen: Optional[Given] = None
    for index, statistic, (column, binned) in candidates:
        pointer = tokens + ("statistics", str(index))
        outcome = _one(
            statistic,
            pointer,
            column,
            binned,
            edges,
            childless and none_class,
            parent,
            minimum_cell,
        )
        if isinstance(outcome, str):
            unused.append((pointer, outcome))
        elif chosen is not None:
            unused.append(
                (
                    pointer,
                    f"the entry is drawn given one column, {printable(chosen.column or '')}, "
                    f"which came first; this grouping is by {printable(column)}",
                )
            )
        else:
            chosen = outcome
    if chosen is None:
        return Given({}, used=tuple(base), unused=tuple(unused))
    return replace(chosen, used=(*base, *chosen.used), unused=tuple(unused))


def _classes(
    column: str, binned: bool, statistic: Mapping[str, Any], parent: GivenParent
) -> Tuple[Optional[Tuple[str, ...]], Optional[Dict[str, str]], Optional[str], bool]:
    """The classes of the given column, how a group key names one, why not, and a null drop."""
    plan = parent.plans.get(column)
    kind = parent.kinds.get(column)
    if plan is None or kind is None:
        return None, None, "the parent expose draws no column of this name", False
    held = parent.held_back.get(column)
    if held is not None:
        return None, None, f"{printable(column)} is {held}, which is no class to weigh by", False
    declaration = plan.declaration
    drop = False
    nulls = declaration.get("nullFraction")
    if nulls is not None:
        if kind in _NUMERIC and Decimal(str(nulls)) == 0:
            drop = True
        else:
            return (
                None,
                None,
                (
                    f"{printable(column)} declares a nullFraction, and a null has no class to weigh "
                    "the children by"
                ),
                False,
            )
    if not binned:
        if kind is not LogicalKind.STRING:
            return (
                None,
                None,
                (
                    f"{printable(column)} is not a category column; a number column is grouped by "
                    "its bins"
                ),
                False,
            )
        levels = declaration.get("levels")
        names = tuple(str(level) for level in levels) if isinstance(levels, Mapping) else None
        if names is None and "levelWeightsGiven" in declaration and plan.level_names is not None:
            names = tuple(plan.level_names)
        if not names:
            return None, None, f"{printable(column)} draws no declared levels", False
        return names, {name: name for name in names}, None, drop
    declared = declaration.get("bins")
    if kind not in _NUMERIC or not isinstance(declared, list) or len(declared) < 2:
        return None, None, f"{printable(column)} is not drawn from bins", False
    spelled = [str(edge) for edge in declared]
    grouping = statistic.get("groupByBins")
    cut = grouping.get("bins") if isinstance(grouping, Mapping) else None
    try:
        theirs = [str(_spell(kind, Decimal(str(edge)))) for edge in cut or ()]
    except ArithmeticError:
        theirs = []
    if theirs != spelled:
        return (
            None,
            None,
            (
                f"its bins are not the bins {printable(column)} is drawn over, which a class is "
                "keyed by"
            ),
            False,
        )
    keys = {
        edge_key: spelled[position]
        for position, edge_key in enumerate(_edge_keys(grouping or {}))
        if position < len(spelled) - 1
    }
    return tuple(spelled[:-1]), keys, None, drop


def _edge_keys(grouping: Mapping[str, Any]) -> List[str]:
    """The canonical text of each lower edge of a ``groupByBins``, as the profile keys its groups."""
    from flux_stats.cliques import edge_text

    bins = grouping.get("bins")
    return [edge_text(Fraction(Decimal(str(edge)))) for edge in (bins or ())][:-1]


def _one(
    statistic: Mapping[str, Any],
    pointer: Tuple[str, ...],
    column: str,
    binned: bool,
    edges: Sequence[int],
    first_is_empty: bool,
    parent: GivenParent,
    minimum_cell: int,
) -> "Given | str":
    """The members one grouped statistic gives, or why it cannot be the given column."""
    classes, keys, why, drop = _classes(column, binned, statistic, parent)
    if why is not None or classes is None or keys is None:
        return why or "the classes of the given column are unknown"
    if _whole_edges(statistic.get("bins")) != list(edges):
        return "its bins are not the bins of the entry's children, so its counts are no weights"
    listed = statistic.get("groups")
    shown: Dict[str, List[str]] = {}
    absent: List[str] = []
    for position, group in enumerate(listed if isinstance(listed, list) else ()):
        if not isinstance(group, Mapping):
            continue
        key = keys.get(str(group.get("group")))
        if key is None or key not in classes:
            return (
                f"a group of it is no class of {printable(column)}, whose classes are its "
                f"{'bins' if binned else 'levels'}"
            )
        if group.get("status") != _FREE:
            continue
        cells = count_cells(group, "binCounts")
        if len(cells) != len(edges) - 1:
            return "a group of it does not count the classes of the entry's children"
        if first_is_empty:
            cells[0] = replace(cells[0], count=0)
        weights, _ = cell_weights(
            minimum_cell,
            cells,
            pointer + ("groups", str(position)),
            "binCounts",
            _HISTOGRAM,
            total=_whole(group.get("k")),
        )
        if sum(weights, Fraction(0)) > 0:
            shown[key] = share_texts(weights)
    for name in classes:
        if name not in shown:
            absent.append(name)
    if not shown:
        return "no group of it publishes a count the weights can rest on"
    assumptions: List[str] = []
    if absent:
        assumptions.append(
            f"The profile publishes no usable group of {printable(column)} for "
            f"{', '.join(printable(name) for name in absent)}, so the parents of "
            f"{'that class' if len(absent) == 1 else 'those classes'} have the children shape's own "
            "weights (the plan states them)."
        )
    members = {
        "given": column,
        "childrenGiven": {name: shown[name] for name in classes if name in shown},
    }
    return Given(
        members,
        used=((pointer, f"the weights of each class of {printable(column)} the children follow"),),
        assumptions=tuple(assumptions),
        column=column,
        drop_null_fraction=drop,
    )
