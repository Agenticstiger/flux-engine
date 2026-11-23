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
    candidates = _candidates(relationship)
    if not candidates:
        return None
    if found is None or not isinstance(shape, Mapping) or "bins" not in shape:
        why = "the entry's children are not a histogram of whole counts, which a grouping weighs"
        return Given(
            {},
            unused=tuple((tokens + ("statistics", str(i)), why) for i, _, _ in candidates),
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
    chosen, unused = _pick_grouping(
        candidates, tokens, edges, childless and none_class, parent, minimum_cell
    )
    if chosen is None:
        return Given({}, used=tuple(base), unused=tuple(unused))
    return replace(chosen, used=(*base, *chosen.used), unused=tuple(unused))


def _candidates(
    relationship: Mapping[str, Any],
) -> List[Tuple[int, Mapping[str, Any], Tuple[str, bool]]]:
    """The statistics of *relationship* that are a histogram grouped by a parent column, by index."""
    found = []
    for index, item in enumerate(relationship.get("statistics") or ()):
        if isinstance(item, Mapping) and item.get("definition") == _HISTOGRAM:
            grouping = _grouping(item)
            if grouping is not None:
                found.append((index, item, grouping))
    return found


def _pick_grouping(
    candidates: Sequence[Tuple[int, Mapping[str, Any], Tuple[str, bool]]],
    tokens: Tuple[str, ...],
    edges: Sequence[int],
    first_is_empty: bool,
    parent: GivenParent,
    minimum_cell: int,
) -> Tuple[Optional[Given], List[_Note]]:
    """The first grouping that can be the given column, and the reason for each that is passed over."""
    unused: List[_Note] = []
    chosen: Optional[Given] = None
    for index, statistic, (column, binned) in candidates:
        pointer = tokens + ("statistics", str(index))
        outcome = _one(
            statistic, pointer, column, binned, edges, first_is_empty, parent, minimum_cell
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
    return chosen, unused


_Classes = Tuple[Optional[Tuple[str, ...]], Optional[Dict[str, str]], Optional[str], bool]


def _no_classes(why: str) -> _Classes:
    return None, None, why, False


def _classes(
    column: str, binned: bool, statistic: Mapping[str, Any], parent: GivenParent
) -> _Classes:
    """The classes of the given column, how a group key names one, why not, and a null drop."""
    plan = parent.plans.get(column)
    kind = parent.kinds.get(column)
    if plan is None or kind is None:
        return _no_classes("the parent expose draws no column of this name")
    held = parent.held_back.get(column)
    if held is not None:
        return _no_classes(f"{printable(column)} is {held}, which is no class to weigh by")
    nulls = plan.declaration.get("nullFraction")
    drop = nulls is not None and kind in _NUMERIC and Decimal(str(nulls)) == 0
    if nulls is not None and not drop:
        return _no_classes(
            f"{printable(column)} declares a nullFraction, and a null has no class to weigh "
            "the children by"
        )
    found = (
        _binned_classes(column, kind, plan.declaration, statistic)
        if binned
        else _level_classes(column, kind, plan)
    )
    return (*found[:3], drop) if found[2] is None else found


def _level_classes(column: str, kind: LogicalKind, plan: ColumnPlan) -> _Classes:
    """The classes of a category column: its declared levels, each its own key."""
    if kind is not LogicalKind.STRING:
        return _no_classes(
            f"{printable(column)} is not a category column; a number column is grouped by its bins"
        )
    declaration = plan.declaration
    levels = declaration.get("levels")
    names = tuple(str(level) for level in levels) if isinstance(levels, Mapping) else None
    if names is None and "levelWeightsGiven" in declaration and plan.level_names is not None:
        names = tuple(plan.level_names)
    if not names:
        return _no_classes(f"{printable(column)} draws no declared levels")
    return names, {name: name for name in names}, None, False


def _binned_classes(
    column: str, kind: LogicalKind, declaration: Mapping[str, Any], statistic: Mapping[str, Any]
) -> _Classes:
    """The classes of a column drawn from bins: its bins, keyed by the profile's lower edges."""
    declared = declaration.get("bins")
    if kind not in _NUMERIC or not isinstance(declared, list) or len(declared) < 2:
        return _no_classes(f"{printable(column)} is not drawn from bins")
    spelled = [str(edge) for edge in declared]
    grouping = statistic.get("groupByBins")
    cut = grouping.get("bins") if isinstance(grouping, Mapping) else None
    try:
        theirs = [str(_spell(kind, Decimal(str(edge)))) for edge in cut or ()]
    except ArithmeticError:
        theirs = []
    if theirs != spelled:
        return _no_classes(
            f"its bins are not the bins {printable(column)} is drawn over, which a class is keyed by"
        )
    keys = {
        edge_key: spelled[position]
        for position, edge_key in enumerate(_edge_keys(grouping or {}))
        if position < len(spelled) - 1
    }
    return tuple(spelled[:-1]), keys, None, False


def _edge_keys(grouping: Mapping[str, Any]) -> List[str]:
    """The canonical text of each lower edge of a ``groupByBins``, as the profile keys its groups."""
    from flux_stats.cliques import edge_text

    bins = grouping.get("bins")
    return [edge_text(Fraction(Decimal(str(edge)))) for edge in (bins or ())][:-1]


def _group_shares(
    group: Mapping[str, Any],
    pointer: Tuple[str, ...],
    edges: Sequence[int],
    first_is_empty: bool,
    minimum_cell: int,
) -> "Optional[List[str]] | str":
    """The shares one published group gives its class, ``None`` when none rests on a count."""
    cells = count_cells(group, "binCounts")
    if len(cells) != len(edges) - 1:
        return "a group of it does not count the classes of the entry's children"
    if first_is_empty:
        cells[0] = replace(cells[0], count=0)
    weights, _ = cell_weights(
        minimum_cell, cells, pointer, "binCounts", _HISTOGRAM, total=_whole(group.get("k"))
    )
    return share_texts(weights) if sum(weights, Fraction(0)) > 0 else None


def _listed_groups(statistic: Mapping[str, Any]) -> List[Tuple[int, Mapping[str, Any]]]:
    """The groups of *statistic* that are mappings, with their positions."""
    listed = statistic.get("groups")
    items = listed if isinstance(listed, list) else ()
    return [(at, item) for at, item in enumerate(items) if isinstance(item, Mapping)]


def _group_weights(
    statistic: Mapping[str, Any],
    pointer: Tuple[str, ...],
    column: str,
    binned: bool,
    named: Tuple[Tuple[str, ...], Dict[str, str]],
    edges: Sequence[int],
    first_is_empty: bool,
    minimum_cell: int,
) -> "Dict[str, List[str]] | str":
    """The weights each published group of *statistic* gives its class, or why it cannot be read."""
    classes, keys = named
    noun = "bins" if binned else "levels"
    shown: Dict[str, List[str]] = {}
    for position, group in _listed_groups(statistic):
        key = keys.get(str(group.get("group")))
        if key is None or key not in classes:
            return f"a group of it is no class of {printable(column)}, whose classes are its {noun}"
        if group.get("status") != _FREE:
            continue
        at = pointer + ("groups", str(position))
        found = _group_shares(group, at, edges, first_is_empty, minimum_cell)
        if isinstance(found, str):
            return found
        if found is not None:
            shown[key] = found
    return shown


def _absent_note(column: str, absent: Sequence[str]) -> Tuple[str, ...]:
    """The assumption a class with no usable group makes, or nothing when every class has one."""
    if not absent:
        return ()
    return (
        f"The profile publishes no usable group of {printable(column)} for "
        f"{', '.join(printable(name) for name in absent)}, so the parents of "
        f"{'that class' if len(absent) == 1 else 'those classes'} have the children shape's own "
        "weights (the plan states them).",
    )


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
    shown = _group_weights(
        statistic, pointer, column, binned, (classes, keys), edges, first_is_empty, minimum_cell
    )
    if isinstance(shown, str):
        return shown
    if not shown:
        return "no group of it publishes a count the weights can rest on"
    absent = [name for name in classes if name not in shown]
    assumptions = _absent_note(column, absent)
    members = {
        "given": column,
        "childrenGiven": {name: shown[name] for name in classes if name in shown},
    }
    return Given(
        members,
        used=((pointer, f"the weights of each class of {printable(column)} the children follow"),),
        assumptions=assumptions,
        column=column,
        drop_null_fraction=drop,
    )
