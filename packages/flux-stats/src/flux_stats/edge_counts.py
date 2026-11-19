# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The output checks for the counts a profile publishes at a column's edges.

A ``marginalHistogram/1`` group publishes one count per bin, a
``levelHistogram/1`` group one per declared level, a ``quantile/2`` group the
records below and through the bin that holds its order statistic, and a
``quantile/1`` group the records below its witness and at or below it: all are
counts between edges of the column, so a reader can add and subtract them. A
level histogram's levels are read as bins, one per level in declared order.
ADR 0012 (decided on 2026-11-18) sets the rule a profiler follows
for every such statistic of one column, or of one relationship's count of
children, and :func:`check_edge_counts` is that rule, so no profiler implements
one of its own:

1. **One partition.** Every statistic of the column that publishes counts at
   edges, at every level and in every group, uses one declared partition: the
   bins of the column's first histogram, or of its first statistic when it has
   no histogram, or the column's declared levels. A statistic whose edges are
   another's, or join adjacent ones of it, is on the partition; one with an
   edge the partition lacks is withheld whole. A ``quantile/1`` counts at its
   witness, an edge no declaration fixes, so it is published only as the one
   statistic of its column that counts at edges, and withheld beside any
   other: a histogram, a ``quantile/2``, another level, or the whole beside a
   groupBy.
2. **Three cells.** A ``quantile/2`` group states three cells: the records
   below its bin, in it and above it. On the unrounded counts, each holds at
   least ``minimum_cell`` records, or none at the ends of the range (below the
   first bin, above the last), which are outside the declared range. A
   ``quantile/1`` group's records below its witness, holding it and above it
   are checked the same way, but none below or above, which says the witness is
   the group's least or greatest value and so that no record lies between it
   and the end of the range, is a cell of no records, shown only where zeros
   are not disclosive. Otherwise the group is withheld. A histogram's published
   cell that is small is withheld too: one of 1 to ``minimum_cell - 1``
   records, or one of none when zeros are disclosive.
3. **A group's size.** Every statistic that holds a group publishes its size,
   so the column's count less the other groups' sizes gives it back. A group of
   1 to ``minimum_cell - 1`` records, or of none when zeros are disclosive, is
   withheld, and so is every group of every statistic of its groupBy.
4. **What a reader can form.** Every published count says how many records of
   one row (the whole column, or one group) lie between two edges: a cell
   between its two edges, a ``quantile/2`` group's counts between the first
   edge and each edge of its bin, and a row's size between the first edge and
   the last. A reader who links edges through such counts knows how many records
   lie between any two edges they link, whether or not either is tied to the
   first edge, so a published count that ties none of a row's edges to the
   first is withheld: subtracted from what is known outright, it would
   count cells that are not next to each other. The groups of a groupBy add up
   to the whole column at every edge, which gives a group's count at an edge
   where the whole and every other group are known. Any small cell a reader can
   form so (of 1 to ``minimum_cell - 1`` records, or of none when zeros are
   disclosive) is protected by withholding one more of the counts it rests on,
   until none is left. Several groups a reader knows only together could still
   be subtracted from the whole, so when the column publishes any other count
   at its edges, a groupBy's counts are published only if a reader can then
   tell every group's count at every edge, and are otherwise withheld whole: a
   group known in full gives back only cells the check has let through, and
   their sums.
5. **Then rounding.** Only then are a published quantile's counts rounded, by
   the profiler's own rounding, and the group is published only if the rounded
   counts still show the order statistic in its bin
   (``binBelow < ceil(level * k) <= binThrough``) or, for a ``quantile/1``, the
   witness by its ranks, and no positive count rounds to zero. Otherwise it is
   withheld.

Whatever withholds a value, the profile spells it ``C`` (:data:`WITHHELD`), so a
status tells a reader nothing of its size; the decision keeps the reason for
the profiler's own report.

The check takes the sizes of the column's groups as known, since the column's
other statistics, its means and supports, publish them too; those withhold a
small group's siblings themselves. Everything here is a pure function of its
arguments: counts in, statuses and rounded counts out. It imports the standard
library alone, as the package does.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM, QUANTILE, QUANTILE_BINNED
from flux_stats.fixedpoint import quantile_rank_ok
from flux_stats.registry import GroupStatistic

__all__ = [
    "DIFFERENCING",
    "OFF_PARTITION",
    "PUBLISHED",
    "ROUNDING",
    "SMALL",
    "WITHHELD",
    "EdgeDecision",
    "EdgeStatistic",
    "check_edge_counts",
]

#: The two statuses a count table publishes (PROFILE-SPEC §3): ``F``, and ``C``
#: for whatever an output check withholds, whatever the check, so that a status
#: says nothing of the size of what it withholds (ADR 0012; one withheld code,
#: decided on 2026-11-18). An ``A`` would tell a reader that a cell holds 1
#: to ``minimum_cell - 1`` records, which a known total can turn into the count.
PUBLISHED = "F"
WITHHELD = "C"

#: Why a value is withheld, for the profiler's own report. Never published.
SMALL = "small"
OFF_PARTITION = "off the partition"
DIFFERENCING = "differencing"
ROUNDING = "rounding"

#: The row of the records a statistic with no ``groupBy`` counts.
_WHOLE = ("", "")

_Row = Tuple[str, str]
#: One published count a reader reads: (kind, statistic, group key, cell), where
#: kind is "cell" for a histogram's cell, over bins or levels, and "quantile" for
#: a quantile/2 group, whose cell is -1. Ordered, so the greatest of a set is the one withheld first:
#: a quantile/2 group before a histogram's cell, a later statistic first.
_Item = Tuple[str, int, str, int]


@dataclass(frozen=True)
class EdgeStatistic:
    """One statistic of a column that publishes counts at edges, as measured.

    ``identifier`` is ``marginalHistogram/1``, ``levelHistogram/1``,
    ``quantile/2`` or ``quantile/1``. ``edges`` are a ``marginalHistogram/1``'s
    or a ``quantile/2``'s encoded bin edges, the grid the profile is formed on,
    and ``levels`` a ``levelHistogram/1``'s declared levels, in level order; a
    ``quantile/1`` has neither. ``groups`` are its measured groups, unrounded: a
    histogram's carry ``bin_counts`` or ``level_counts``, a ``quantile/2``'s its
    bin and the two counts, a ``quantile/1``'s its witness and two ranks.
    ``level`` is a quantile's level. ``group_status`` and ``cell_status`` hold
    what the profiler's other checks already withheld, by group key (absent
    means published), and a histogram's per cell: ``F``, or the reason a check
    withheld it, which the decision hands back.
    """

    identifier: str
    edges: Tuple[int, ...]
    groups: Tuple[GroupStatistic, ...]
    group_by: Optional[str] = None
    level: Optional[Fraction] = None
    group_status: Mapping[str, str] = field(default_factory=dict)
    cell_status: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    levels: Tuple[str, ...] = ()


@dataclass(frozen=True)
class EdgeDecision:
    """What a profiler publishes of one :class:`EdgeStatistic`.

    ``group_status`` holds every group's status, ``F`` or ``C``, and
    ``cell_status`` a histogram's cells' per published group: what the profile
    spells. ``group_reason`` and ``cell_reason`` hold why each withheld value is
    withheld (:data:`SMALL`, :data:`OFF_PARTITION`, :data:`DIFFERENCING`,
    :data:`ROUNDING`, or a reason the caller gave), for the profiler's report,
    and an empty string for a published one. ``rounded`` holds each published
    quantile group with its rounded counts, ready to spell; a histogram's cells
    are rounded by the profiler, by the same rounding.
    """

    group_status: Mapping[str, str]
    cell_status: Mapping[str, Tuple[str, ...]]
    rounded: Mapping[str, GroupStatistic]
    group_reason: Mapping[str, str] = field(default_factory=dict)
    cell_reason: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Fact:
    """What a reader knows of one row: ``F(high) - F(low) == value``, and what it rests on."""

    low: int
    high: int
    value: int
    rests: FrozenSet[_Item]
    #: Given by the groups' sum to the whole, rather than published for this row.
    derived: bool = False


@dataclass(frozen=True)
class _Rule:
    """The threshold a cell is checked against, and whether a cell of no records is small."""

    minimum_cell: int
    zeros_disclosive: bool

    def small(self, count: int) -> bool:
        """Whether a cell of *count* records may not be shown on its own."""
        return count < self.minimum_cell and (count > 0 or self.zeros_disclosive)


#: The statistics the check reads, and those of them that count over bins.
_BINNED = (MARGINAL_HISTOGRAM.identifier, QUANTILE_BINNED.identifier, QUANTILE.identifier)
_KINDS = (*_BINNED, LEVEL_HISTOGRAM.identifier)
_HISTOGRAMS = (MARGINAL_HISTOGRAM.identifier, LEVEL_HISTOGRAM.identifier)

#: A row as a reader sees it: for each edge, the edge its knowledge is tied to
#: (the first edge when the count below it is known outright), the count between
#: the two, and the facts on the way.
_View = Dict[int, Tuple[int, int, FrozenSet[int]]]


def check_edge_counts(
    statistics: Sequence[EdgeStatistic],
    *,
    total: int,
    minimum_cell: int,
    zeros_disclosive: bool,
    round_count: Callable[[int], int],
) -> Tuple[EdgeDecision, ...]:
    """Apply ADR 0012's rule to every statistic of one column that counts at edges.

    *statistics* are the column's histograms and quantiles, in the order the
    declaration names them, and the result holds one decision for each, in that
    order. A profiler runs it once per column, and once per relationship's count
    of children, with every such statistic of the column in the one call: a
    statistic checked in a call of its own is judged alone, so a ``quantile/1``
    would pass beside a histogram and no count would be checked against another. *total* is the number of records the column's statistics count,
    which a reader knows from its other metrics. *minimum_cell* is the rule's
    threshold, *zeros_disclosive* whether the rule holds a cell of no records
    disclosive, and *round_count* the profiler's rounding, the one it rounds
    every other count with.

    The counts must describe one set of records: an ungrouped statistic counts
    *total* records, a group has one size in every statistic that holds it, and
    a groupBy's groups add up to *total* (pass the records a null key leaves out
    as one more group, whose status is never ``F``; a group of no records is
    withheld with its siblings where zeros are disclosive).
    A column counts over its levels or over its bins, never both, and a
    quantile names its level. Anything else is refused with
    :class:`ValueError`, since the check could not see what a reader subtracts.
    """
    if minimum_cell < 1:
        raise ValueError(f"minimum_cell is a positive count, not {minimum_cell}")
    if not statistics:
        return ()
    rule = _Rule(minimum_cell, zeros_disclosive)
    sizes = _sizes(statistics, total)
    partition, levels = _partition(statistics)
    statuses, cells = _first_checks(statistics, partition, levels, rule)
    _withhold_beside_small_groups(statistics, statuses, sizes, rule)
    if partition:
        index = {edge: position for position, edge in enumerate(partition)}
        _withhold_until_safe(statistics, statuses, cells, sizes, index, rule)
    return _decisions(statistics, statuses, cells, round_count)


def _sizes(statistics: Sequence[EdgeStatistic], total: int) -> Dict[_Row, int]:
    """Every row a reader can reason about, with the records it holds, or refuse."""
    kinds = {statistic.identifier for statistic in statistics}
    if not kinds <= set(_KINDS):
        raise ValueError(f"no edge-count rule for {', '.join(sorted(kinds - set(_KINDS)))}")
    if LEVEL_HISTOGRAM.identifier in kinds and len(kinds) > 1:
        raise ValueError("a column's statistics count over its levels or over its bins, not both")
    sizes: Dict[_Row, int] = {_WHOLE: total}
    for statistic in statistics:
        for group in statistic.groups:
            _require_consistent(statistic, group)
            row = _row_of(statistic, group.group)
            if row == _WHOLE and group.k != total:
                raise ValueError(
                    "an ungrouped statistic counts other records than the column's total"
                )
            if sizes.setdefault(row, group.k) != group.k:
                raise ValueError(
                    f"group {group.group!r} of {row[0]!r} has two sizes in two statistics"
                )
    for name in _groupings_in_order(statistics):
        held = sum(size for row, size in sizes.items() if row != _WHOLE and row[0] == name)
        if held != total:
            raise ValueError(
                f"the groups of {name!r} do not add up to the column's total: pass the "
                "records a null key leaves out as one more group"
            )
    return sizes


def _require_consistent(statistic: EdgeStatistic, group: GroupStatistic) -> None:
    """Refuse a group whose counts cannot all be true of one set of records."""
    if statistic.identifier in (QUANTILE.identifier, QUANTILE_BINNED.identifier):
        if statistic.level is None:
            raise ValueError(f"a {statistic.identifier} statistic names its level")
    if statistic.identifier == QUANTILE.identifier:
        if group.k == 0:
            return
        if group.quantile_witness is None or not (
            0 <= group.quantile_below <= group.quantile_at_or_below <= group.k
        ):
            raise ValueError(f"group {group.group!r} ranks its witness outside its records")
        return
    if statistic.identifier == QUANTILE_BINNED.identifier:
        bins = len(statistic.edges) - 1
        if group.k == 0:
            return
        if group.quantile_bin is None or not 0 <= group.quantile_bin < bins:
            raise ValueError(f"group {group.group!r} names no bin of its statistic")
        if not 0 <= group.bin_below <= group.bin_through <= group.k:
            raise ValueError(f"group {group.group!r} counts more records below a bin than in all")
        return
    counts = _cell_counts(statistic, group)
    if len(counts) != len(_edges(statistic)) - 1 or sum(counts) != group.k:
        raise ValueError(f"group {group.group!r} has cells that do not add up to its size")


def _partition(statistics: Sequence[EdgeStatistic]) -> Tuple[Tuple[int, ...], Tuple[str, ...]]:
    """The column's one partition, and the levels it stands for when it counts over levels.

    Its first histogram's edges, or its first ``quantile/2``'s, or its first
    level histogram's levels; nothing when its only statistics are
    ``quantile/1``, which counts at no declared edge.
    """
    for wanted in (MARGINAL_HISTOGRAM, QUANTILE_BINNED, LEVEL_HISTOGRAM):
        for statistic in statistics:
            if statistic.identifier == wanted.identifier:
                return _edges(statistic), statistic.levels
    return (), ()


def _edges(statistic: EdgeStatistic) -> Tuple[int, ...]:
    """A statistic's edges: its bins', one per level in order, or none for a ``quantile/1``."""
    if statistic.identifier == LEVEL_HISTOGRAM.identifier:
        return tuple(range(len(statistic.levels) + 1))
    if statistic.identifier == QUANTILE.identifier:
        return ()
    return statistic.edges


def _cell_counts(statistic: EdgeStatistic, group: GroupStatistic) -> Tuple[int, ...]:
    """A histogram group's records per cell, per bin or per level."""
    if statistic.identifier == LEVEL_HISTOGRAM.identifier:
        return group.level_counts
    return group.bin_counts


def _first_checks(
    statistics: Sequence[EdgeStatistic],
    partition: Tuple[int, ...],
    levels: Tuple[str, ...],
    rule: _Rule,
) -> Tuple[List[Dict[str, str]], List[Dict[str, List[str]]]]:
    """Each group's and cell's status after the partition and the three-cell checks."""
    statuses: List[Dict[str, str]] = []
    cells: List[Dict[str, List[str]]] = []
    for statistic in statistics:
        on_partition = _on_partition(statistic, partition, levels, alone=len(statistics) == 1)
        group_status: Dict[str, str] = {}
        cell_status: Dict[str, List[str]] = {}
        for group in statistic.groups:
            status = statistic.group_status.get(group.group, PUBLISHED)
            if not on_partition:
                status = OFF_PARTITION
            elif status == PUBLISHED and not _shows_its_cells(statistic, group, rule):
                status = SMALL
            group_status[group.group] = status
            if statistic.identifier in _HISTOGRAMS:
                cell_status[group.group] = _cell_checks(statistic, group, rule)
        statuses.append(group_status)
        cells.append(cell_status)
    return statuses, cells


def _on_partition(
    statistic: EdgeStatistic, partition: Tuple[int, ...], levels: Tuple[str, ...], *, alone: bool
) -> bool:
    """Whether a statistic counts on the column's one partition."""
    if statistic.identifier == QUANTILE.identifier:
        return alone
    if statistic.identifier == LEVEL_HISTOGRAM.identifier:
        return statistic.levels == levels
    edges = statistic.edges
    return (
        set(edges) <= set(partition) and edges[:1] == partition[:1] and edges[-1:] == partition[-1:]
    )


def _shows_its_cells(statistic: EdgeStatistic, group: GroupStatistic, rule: _Rule) -> bool:
    """Whether a quantile group's three cells may be shown; a histogram's are checked one by one."""
    if statistic.identifier == QUANTILE_BINNED.identifier:
        return _three_cells(group, len(statistic.edges) - 1, rule.minimum_cell)
    if statistic.identifier == QUANTILE.identifier:
        return _witness_cells(group, rule)
    return True


def _cell_checks(statistic: EdgeStatistic, group: GroupStatistic, rule: _Rule) -> List[str]:
    counts = _cell_counts(statistic, group)
    given = statistic.cell_status.get(group.group, ())
    if given and len(given) != len(counts):
        raise ValueError(f"group {group.group!r} has a status for other cells than it has")
    shown = list(given) if given else [PUBLISHED] * len(counts)
    for cell, count in enumerate(counts):
        if shown[cell] == PUBLISHED and rule.small(count):
            shown[cell] = SMALL
    return shown


def _three_cells(group: GroupStatistic, bins: int, minimum_cell: int) -> bool:
    """Whether a ``quantile/2`` group's cells below, in and above its bin may be shown."""
    if group.k == 0 or group.quantile_bin is None:
        return False
    below = group.bin_below
    inside = group.bin_through - group.bin_below
    above = group.k - group.bin_through
    first = group.quantile_bin == 0
    last = group.quantile_bin == bins - 1
    return (
        inside >= minimum_cell
        and (below >= minimum_cell or (first and below == 0))
        and (above >= minimum_cell or (last and above == 0))
    )


def _witness_cells(group: GroupStatistic, rule: _Rule) -> bool:
    """Whether a ``quantile/1`` group's records below, holding and above its witness may be shown.

    None below or above says that the witness is the group's least or greatest
    value, and so that no record lies between it and the end of the range: a
    cell of no records, which may be shown only where zeros are not disclosive.
    """
    if group.k == 0 or group.quantile_witness is None:
        return False
    below = group.quantile_below
    holding = group.quantile_at_or_below - group.quantile_below
    above = group.k - group.quantile_at_or_below
    return holding >= rule.minimum_cell and not rule.small(below) and not rule.small(above)


def _withhold_beside_small_groups(
    statistics: Sequence[EdgeStatistic],
    statuses: List[Dict[str, str]],
    sizes: Mapping[_Row, int],
    rule: _Rule,
) -> None:
    """Withhold a group of too few records, and every group of every statistic of its groupBy.

    Every statistic that holds a group publishes its size, so the column's count
    less the other groups' sizes gives a withheld group's size back. A group of no
    records is small where zeros are disclosive.
    """
    small = {row[0] for row, size in sizes.items() if row != _WHOLE and rule.small(size)}
    for position, statistic in enumerate(statistics):
        for group in statistic.groups:
            if statuses[position][group.group] != PUBLISHED:
                continue
            if rule.small(sizes[_row_of(statistic, group.group)]):
                statuses[position][group.group] = SMALL
            elif statistic.group_by in small:
                statuses[position][group.group] = DIFFERENCING


def _withhold_until_safe(
    statistics: Sequence[EdgeStatistic],
    statuses: List[Dict[str, str]],
    cells: List[Dict[str, List[str]]],
    sizes: Mapping[_Row, int],
    index: Mapping[int, int],
    rule: _Rule,
) -> None:
    """Withhold one more count at a time until a reader can form no small cell."""
    edges = len(index)
    while True:
        facts = _facts(statistics, statuses, cells, sizes, index)
        views = _reader_views(facts, statistics, edges)
        exposed = _first_floating_count(facts, views) or _first_small_cell(facts, views, rule)
        if exposed is not None:
            _withhold_item(max(exposed), statuses, cells)
            continue
        grouping = _grouping_to_withhold(statistics, facts, views, edges)
        if grouping is None:
            return
        _withhold_grouping(statistics, statuses, grouping)


def _withhold_item(
    item: _Item, statuses: List[Dict[str, str]], cells: List[Dict[str, List[str]]]
) -> None:
    """Withhold one published count: a ``quantile/2`` group, or one histogram cell."""
    kind, position, key, cell = item
    if kind == "quantile":
        statuses[position][key] = DIFFERENCING
    else:
        cells[position][key][cell] = DIFFERENCING


def _withhold_grouping(
    statistics: Sequence[EdgeStatistic], statuses: List[Dict[str, str]], grouping: str
) -> None:
    """Withhold every published group of every statistic of one groupBy."""
    for position, statistic in enumerate(statistics):
        if statistic.group_by != grouping:
            continue
        for key, status in statuses[position].items():
            if status == PUBLISHED:
                statuses[position][key] = DIFFERENCING


def _facts(
    statistics: Sequence[EdgeStatistic],
    statuses: Sequence[Mapping[str, str]],
    cells: Sequence[Mapping[str, Sequence[str]]],
    sizes: Mapping[_Row, int],
    index: Mapping[int, int],
) -> Dict[_Row, List[_Fact]]:
    """What the published counts tell a reader, row by row; each row's size among them."""
    last = len(index) - 1
    facts: Dict[_Row, List[_Fact]] = {
        row: [_Fact(0, last, size, frozenset())] for row, size in sizes.items()
    }
    for position, statistic in enumerate(statistics):
        if statistic.identifier == QUANTILE.identifier:
            continue  # alone on its column, or withheld beside the others
        edges_of = _edges(statistic)
        for group in statistic.groups:
            if statuses[position][group.group] != PUBLISHED:
                continue
            row = facts[_row_of(statistic, group.group)]
            if statistic.identifier == QUANTILE_BINNED.identifier:
                assert group.quantile_bin is not None
                rests = frozenset({("quantile", position, group.group, -1)})
                for edge, value in (
                    (statistic.edges[group.quantile_bin], group.bin_below),
                    (statistic.edges[group.quantile_bin + 1], group.bin_through),
                ):
                    row.append(_Fact(0, index[edge], value, rests))
                continue
            counts = _cell_counts(statistic, group)
            for cell, status in enumerate(cells[position][group.group]):
                if status == PUBLISHED:
                    row.append(
                        _Fact(
                            index[edges_of[cell]],
                            index[edges_of[cell + 1]],
                            counts[cell],
                            frozenset({("cell", position, group.group, cell)}),
                        )
                    )
    return facts


def _reader_views(
    facts: Dict[_Row, List[_Fact]], statistics: Sequence[EdgeStatistic], edges: int
) -> Dict[_Row, _View]:
    """Each row as a reader sees it, once the groups' sums have told them what they can.

    The groups of each groupBy add up to the whole at every edge, so where all
    but one of them and the whole are known outright, that one is too: it is
    added to the row's facts, resting on everything the others rest on, and the
    rows are seen again until no sum tells a reader more.
    """
    groupings: Dict[str, List[_Row]] = {}
    for row in facts:
        if row != _WHOLE:
            groupings.setdefault(row[0], []).append(row)
    sums = [[_WHOLE, *groupings.get(name, [])] for name in _groupings_in_order(statistics)]
    while True:
        views = {row: _view(row_facts, edges) for row, row_facts in facts.items()}
        learned = [
            found
            for found in (
                _summed(facts, views, terms, edge) for terms in sums for edge in range(edges)
            )
            if found is not None
        ]
        if not learned:
            return views
        for row, fact in learned:
            facts[row].append(fact)


def _summed(
    facts: Mapping[_Row, Sequence[_Fact]],
    views: Mapping[_Row, _View],
    terms: Sequence[_Row],
    edge: int,
) -> Optional[Tuple[_Row, _Fact]]:
    """The count below *edge* of the one term of a sum a reader does not know, if one.

    *terms* are the whole and the groups of one groupBy, which add up to it. When
    the reader knows every term at *edge* but one, that one is the others' sum (or
    the whole less them), resting on everything they rest on.
    """
    missing = [row for row in terms if views[row][edge][0] != 0]
    if len(missing) != 1:
        return None
    (row,) = missing
    value = 0
    rests: FrozenSet[_Item] = frozenset()
    for term in terms:
        if term == row:
            continue
        known, basis = _outright(facts[term], views[term], edge)
        value += known if term == _WHOLE else -known
        rests |= basis
    return row, _Fact(0, edge, value if row != _WHOLE else -value, rests, derived=True)


def _view(facts: Sequence[_Fact], edges: int) -> _View:
    """Every edge of a row tied to the edge its component starts from, by breadth first.

    An edge reached from the first edge holds the count below it outright. The
    path to each edge is kept as the facts it crosses, so the facts behind the
    count between two edges are those on one path and not on both.
    """
    neighbours: Dict[int, List[Tuple[int, int, int]]] = {edge: [] for edge in range(edges)}
    for number, fact in enumerate(facts):
        neighbours[fact.low].append((fact.high, fact.value, number))
        neighbours[fact.high].append((fact.low, -fact.value, number))
    view: _View = {}
    for start in range(edges):
        if start in view:
            continue
        view[start] = (start, 0, frozenset())
        queue: List[int] = [start]
        for edge in queue:
            _, count, path = view[edge]
            for other, step, number in neighbours[edge]:
                if other not in view:
                    view[other] = (start, count + step, path | {number})
                    queue.append(other)
                elif view[other][1] != count + step:
                    raise ValueError("the counts of one group disagree about one of its edges")
    return view


def _outright(facts: Sequence[_Fact], view: _View, edge: int) -> Tuple[int, FrozenSet[_Item]]:
    """The count below *edge* that a row's reader knows outright, and what it rests on."""
    _, count, path = view[edge]
    return count, _rests(facts, path)


def _rests(facts: Sequence[_Fact], path: FrozenSet[int]) -> FrozenSet[_Item]:
    rests: FrozenSet[_Item] = frozenset()
    for number in path:
        rests |= facts[number].rests
    return rests


def _first_floating_count(
    facts: Mapping[_Row, Sequence[_Fact]], views: Mapping[_Row, _View]
) -> Optional[FrozenSet[_Item]]:
    """The published counts that tie edges of a row none of which is tied to its first.

    A reader who knows the records between two such edges, and the records
    below other edges outright, can subtract the one from the other and count
    cells that are not next to each other, such as two withheld cells either
    side of a published one. So a published count is kept only where it ties
    edges a reader also knows outright: then a reader knows the records below
    some edges of the row, every cell they can form is made of the cells
    between those edges that follow each other, and each of those is checked.
    """
    for row in sorted(views):
        floating: FrozenSet[_Item] = frozenset()
        for fact in facts[row]:
            if fact.rests and not fact.derived and views[row][fact.low][0] != 0:
                floating |= fact.rests
        if floating:
            return floating
    return None


def _first_small_cell(
    facts: Mapping[_Row, Sequence[_Fact]], views: Mapping[_Row, _View], rule: _Rule
) -> Optional[FrozenSet[_Item]]:
    """The published counts behind the first small cell a reader can form, if any.

    A reader knows the records between any two edges of one row that are tied
    together, so it suffices to look between edges that follow each other
    within each tied set: every other cell they know is a sum of those, and
    counts below an edge never fall. A cell is small when it holds 1 to
    ``minimum_cell - 1`` records, or none when zeros are disclosive. One that
    rests on no published count is a row's size, which the size rule has
    checked, so it is passed over. Of the counts the cell rests on, those that
    share one of its edges are returned when there are any: withholding one
    joins the cell to its neighbour, where another would leave published counts
    between withheld ones, which the floating rule would then withhold too.
    """
    for row in sorted(views):
        view = views[row]
        tied: Dict[int, List[int]] = {}
        for edge, (start, _, _) in view.items():
            tied.setdefault(start, []).append(edge)
        for members in tied.values():
            members.sort()
            for low, high in zip(members, members[1:], strict=False):
                count = view[high][1] - view[low][1]
                path = view[low][2] ^ view[high][2]
                rests = _rests(facts[row], path)
                if rule.small(count) and rests:
                    beside = frozenset(
                        number
                        for number in path
                        if {facts[row][number].low, facts[row][number].high} & {low, high}
                    )
                    return _rests(facts[row], beside) or rests
    return None


def _grouping_to_withhold(
    statistics: Sequence[EdgeStatistic],
    facts: Mapping[_Row, Sequence[_Fact]],
    views: Mapping[_Row, _View],
    edges: int,
) -> Optional[str]:
    """The last groupBy whose counts leave a group partly unknown beside other counts.

    A groupBy's groups add up to the whole column. When the column publishes
    any other count at its edges, the whole's or another groupBy's, a reader
    can subtract and form cells of groups they know only together, so such a
    groupBy is withheld whole. ``None`` when there is none.
    """
    publishing = {
        row[0]
        for row, row_facts in facts.items()
        if any(fact.rests and not fact.derived for fact in row_facts)
    }
    if len(publishing) < 2:
        return None
    for name in reversed(_groupings_in_order(statistics)):
        if name not in publishing:
            continue
        members = [row for row in views if row != _WHOLE and row[0] == name]
        if any(views[row][edge][0] != 0 for row in members for edge in range(edges)):
            return name
    return None


def _decisions(
    statistics: Sequence[EdgeStatistic],
    statuses: List[Dict[str, str]],
    cells: Sequence[Mapping[str, Sequence[str]]],
    round_count: Callable[[int], int],
) -> Tuple[EdgeDecision, ...]:
    """Round each published quantile group, and spell what is published."""
    decisions: List[EdgeDecision] = []
    for position, statistic in enumerate(statistics):
        rounded: Dict[str, GroupStatistic] = {}
        if statistic.identifier in (QUANTILE_BINNED.identifier, QUANTILE.identifier):
            assert statistic.level is not None, "a quantile names its level"
            for group in statistic.groups:
                if statuses[position][group.group] != PUBLISHED:
                    continue
                if statistic.identifier == QUANTILE.identifier:
                    spelled = _rounded_ranks(group, statistic.level, round_count)
                else:
                    bins = len(statistic.edges) - 1
                    spelled = _rounded(group, statistic.level, bins, round_count)
                if spelled is None:
                    statuses[position][group.group] = ROUNDING
                else:
                    rounded[group.group] = spelled
        published = [key for key, value in statuses[position].items() if value == PUBLISHED]
        decisions.append(
            EdgeDecision(
                group_status={key: _shown(value) for key, value in statuses[position].items()},
                cell_status={
                    key: tuple(_shown(value) for value in cells[position][key])
                    for key in published
                    if key in cells[position]
                },
                rounded=rounded,
                group_reason={key: _why(value) for key, value in statuses[position].items()},
                cell_reason={
                    key: tuple(_why(value) for value in cells[position][key])
                    for key in published
                    if key in cells[position]
                },
            )
        )
    return tuple(decisions)


def _shown(status: str) -> str:
    """The status a profile spells: ``F``, or ``C`` for anything withheld."""
    return PUBLISHED if status == PUBLISHED else WITHHELD


def _why(status: str) -> str:
    """Why a value is withheld, or the empty string for a published one."""
    return "" if status == PUBLISHED else status


def _row_of(statistic: EdgeStatistic, key: str) -> _Row:
    return _WHOLE if statistic.group_by is None else (statistic.group_by, key)


def _groupings_in_order(statistics: Sequence[EdgeStatistic]) -> List[str]:
    names: List[str] = []
    for statistic in statistics:
        if statistic.group_by is not None and statistic.group_by not in names:
            names.append(statistic.group_by)
    return names


def _rounded(
    group: GroupStatistic,
    level: Fraction,
    bins: int,
    round_count: Callable[[int], int],
) -> Optional[GroupStatistic]:
    """*group* with its counts rounded, or ``None`` when the rounded counts cannot be shown.

    The rounded counts must still show the order statistic in its bin, which
    is how a reader checks the group, keep the ends of the range empty where
    they are, and turn no positive count into zero.
    """
    k, below, through = (
        round_count(count) for count in (group.k, group.bin_below, group.bin_through)
    )
    for exact, shown in ((group.k, k), (group.bin_below, below), (group.bin_through, through)):
        if exact > 0 and shown == 0:
            return None
    if not 0 <= below <= through <= k:
        return None
    if group.quantile_bin == 0 and below != 0:
        return None
    if group.quantile_bin == bins - 1 and through != k:
        return None
    if not quantile_rank_ok(
        count=k, level=level, count_strictly_below=below, count_at_or_below=through
    ):
        return None
    return replace(
        group,
        k=k,
        bin_below=below,
        bin_through=through,
        quantile_witness=None,
        quantile_below=0,
        quantile_at_or_below=0,
    )


def _rounded_ranks(
    group: GroupStatistic, level: Fraction, round_count: Callable[[int], int]
) -> Optional[GroupStatistic]:
    """A ``quantile/1`` *group* with its size and ranks rounded, or ``None`` if they cannot be shown.

    The rounded ranks must still prove the witness, as a reader checks it, and
    no positive count may round to zero.
    """
    exact = (group.k, group.quantile_below, group.quantile_at_or_below)
    k, below, at_or_below = (round_count(count) for count in exact)
    if any(
        count > 0 and shown == 0
        for count, shown in zip(exact, (k, below, at_or_below), strict=True)
    ):
        return None
    if not 0 <= below <= at_or_below <= k:
        return None
    if not quantile_rank_ok(
        count=k, level=level, count_strictly_below=below, count_at_or_below=at_or_below
    ):
        return None
    return replace(group, k=k, quantile_below=below, quantile_at_or_below=at_or_below)
