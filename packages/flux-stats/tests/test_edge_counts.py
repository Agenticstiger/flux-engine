# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""ADR 0012's rule for the counts a profile publishes at a column's bin edges."""

from __future__ import annotations

import itertools
import random
from fractions import Fraction
from typing import Dict, List, Optional, Sequence, Tuple

import pytest

from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM, QUANTILE, QUANTILE_BINNED
from flux_stats.edge_counts import (
    DIFFERENCING,
    OFF_PARTITION,
    PUBLISHED,
    ROUNDING,
    SMALL,
    WITHHELD,
    EdgeDecision,
    EdgeStatistic,
    check_edge_counts,
)
from flux_stats.fixedpoint import order_statistic_index
from flux_stats.registry import GroupStatistic

pytestmark = pytest.mark.unit

EDGES = (0, 10, 20, 30, 40)
MINIMUM = 10


def _round(count: int, base: int = 5) -> int:
    """A count to the nearest multiple of *base*, a tie to the even one (ACRO's rounding)."""
    quotient, remainder = divmod(count, base)
    if 2 * remainder > base or (2 * remainder == base and quotient % 2 == 1):
        quotient += 1
    return quotient * base


def _quantile(key: str, k: int, index: int, below: int, through: int) -> GroupStatistic:
    return GroupStatistic(group=key, k=k, quantile_bin=index, bin_below=below, bin_through=through)


def _histogram(key: str, counts: Sequence[int]) -> GroupStatistic:
    return GroupStatistic(group=key, k=sum(counts), bin_counts=tuple(counts))


def _binned(
    *groups: GroupStatistic,
    level: str = "0.5",
    edges: Tuple[int, ...] = EDGES,
    group_by: Optional[str] = None,
    status: Optional[Dict[str, str]] = None,
) -> EdgeStatistic:
    return EdgeStatistic(
        identifier=QUANTILE_BINNED.identifier,
        edges=edges,
        groups=tuple(groups),
        group_by=group_by,
        level=Fraction(level),
        group_status=status or {},
    )


def _histogram_statistic(
    *groups: GroupStatistic,
    edges: Tuple[int, ...] = EDGES,
    group_by: Optional[str] = None,
    status: Optional[Dict[str, str]] = None,
    cells: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> EdgeStatistic:
    return EdgeStatistic(
        identifier=MARGINAL_HISTOGRAM.identifier,
        edges=edges,
        groups=tuple(groups),
        group_by=group_by,
        group_status=status or {},
        cell_status=cells or {},
    )


LEVELS = ("DE", "FR", "GB", "IE")


def _witnessed(key: str, k: int, witness: int, below: int, at_or_below: int) -> GroupStatistic:
    return GroupStatistic(
        group=key,
        k=k,
        quantile_witness=witness,
        quantile_below=below,
        quantile_at_or_below=at_or_below,
    )


def _ranked(
    *groups: GroupStatistic,
    level: str = "0.5",
    group_by: Optional[str] = None,
    status: Optional[Dict[str, str]] = None,
) -> EdgeStatistic:
    return EdgeStatistic(
        identifier=QUANTILE.identifier,
        edges=(),
        groups=tuple(groups),
        group_by=group_by,
        level=Fraction(level),
        group_status=status or {},
    )


def _levels(key: str, counts: Sequence[int]) -> GroupStatistic:
    return GroupStatistic(group=key, k=sum(counts), level_counts=tuple(counts))


def _level_statistic(
    *groups: GroupStatistic,
    levels: Tuple[str, ...] = LEVELS,
    group_by: Optional[str] = None,
    status: Optional[Dict[str, str]] = None,
    cells: Optional[Dict[str, Tuple[str, ...]]] = None,
) -> EdgeStatistic:
    return EdgeStatistic(
        identifier=LEVEL_HISTOGRAM.identifier,
        edges=(),
        groups=tuple(groups),
        group_by=group_by,
        group_status=status or {},
        cell_status=cells or {},
        levels=levels,
    )


def _check(*statistics: EdgeStatistic, total: int, zeros: bool = False) -> Tuple[EdgeDecision, ...]:
    return check_edge_counts(
        statistics,
        total=total,
        minimum_cell=MINIMUM,
        zeros_disclosive=zeros,
        round_count=_round,
    )


def _groups(decision: EdgeDecision) -> Dict[str, str]:
    """Each group's status, read as ``F`` or the reason it is withheld."""
    return {
        key: PUBLISHED if status == PUBLISHED else decision.group_reason[key]
        for key, status in decision.group_status.items()
    }


def _cells(decision: EdgeDecision) -> Dict[str, Tuple[str, ...]]:
    """Each published group's cells, read as ``F`` or the reason each is withheld."""
    return {
        key: tuple(
            PUBLISHED if status == PUBLISHED else reason
            for status, reason in zip(statuses, decision.cell_reason[key], strict=True)
        )
        for key, statuses in decision.cell_status.items()
    }


def _again(
    statistics: Sequence[EdgeStatistic],
    decisions: Sequence[EdgeDecision],
    total: int,
    zeros: bool = False,
) -> None:
    """Feeding the decisions back withholds nothing more: no small cell is left to form."""
    replayed = [
        EdgeStatistic(
            identifier=statistic.identifier,
            edges=statistic.edges,
            groups=statistic.groups,
            group_by=statistic.group_by,
            level=statistic.level,
            group_status=dict(decision.group_status),
            cell_status={
                group.group: decision.cell_status.get(group.group, ())
                for group in statistic.groups
                if decision.cell_status.get(group.group)
            },
            levels=statistic.levels,
        )
        for statistic, decision in zip(statistics, decisions, strict=True)
    ]
    again = _check(*replayed, total=total, zeros=zeros)
    for before, after in zip(decisions, again, strict=True):
        assert after.group_status == before.group_status
        assert after.cell_status == before.cell_status


# ---------------------------------------------------------------------------
# Three cells
# ---------------------------------------------------------------------------


def test_a_group_whose_three_cells_each_hold_enough_is_published_rounded() -> None:
    (decision,) = _check(_binned(_quantile("", 61, 1, 21, 39)), total=61)
    assert _groups(decision) == {"": PUBLISHED}
    shown = decision.rounded[""]
    assert (shown.k, shown.quantile_bin, shown.bin_below, shown.bin_through) == (60, 1, 20, 40)
    assert shown.quantile_witness is None


@pytest.mark.parametrize(
    "group",
    [
        pytest.param(_quantile("", 60, 1, 5, 40), id="below holds 5"),
        pytest.param(_quantile("", 60, 1, 20, 29), id="the bin holds 9"),
        pytest.param(_quantile("", 45, 1, 20, 40), id="above holds 5"),
        pytest.param(_quantile("", 60, 2, 0, 40), id="below empty but not the first bin"),
        pytest.param(_quantile("", 40, 1, 20, 40), id="above empty but not the last bin"),
        pytest.param(GroupStatistic(group="", k=0), id="an empty group"),
    ],
)
def test_a_group_with_a_small_cell_is_withheld_as_small(group: GroupStatistic) -> None:
    (decision,) = _check(_binned(group), total=group.k)
    assert _groups(decision) == {"": SMALL}
    assert decision.rounded == {}


def test_the_ends_of_the_range_may_be_empty() -> None:
    first, last = _check(
        _binned(_quantile("", 40, 0, 0, 20), level="0.25"),
        _binned(_quantile("", 40, 3, 20, 40), level="0.75"),
        total=40,
    )
    assert _groups(first) == {"": PUBLISHED} and _groups(last) == {"": PUBLISHED}


# ---------------------------------------------------------------------------
# One partition
# ---------------------------------------------------------------------------


def test_a_quantile_on_the_histograms_edges_or_a_coarsening_of_them_is_on_the_partition() -> None:
    histogram = _histogram_statistic(_histogram("", [20, 20, 20, 20]))
    coarse = _binned(_quantile("", 80, 0, 0, 40), level="0.25", edges=(0, 20, 40))
    off = _binned(_quantile("", 80, 0, 0, 30), level="0.25", edges=(0, 15, 40))
    decisions = _check(histogram, coarse, off, total=80)
    assert _groups(decisions[1]) == {"": PUBLISHED}
    assert _groups(decisions[2]) == {"": OFF_PARTITION} and decisions[2].rounded == {}


def test_without_a_histogram_the_first_statistic_names_the_partition() -> None:
    first = _binned(_quantile("", 80, 1, 20, 40), level="0.5", edges=(0, 20, 40, 60))
    other = _binned(_quantile("", 80, 1, 20, 40), level="0.5", edges=(0, 25, 40, 60))
    decisions = _check(first, other, total=80)
    assert [_groups(d)[""] for d in decisions] == [PUBLISHED, OFF_PARTITION]


# ---------------------------------------------------------------------------
# What a reader can form
# ---------------------------------------------------------------------------


FIVE_BINS = (0, 10, 20, 30, 40, 50)


def test_two_levels_whose_edges_form_a_small_cell_withhold_the_later_one() -> None:
    # F(10) = 30, F(20) = 50 at level 0.4, and F(30) = 55, F(40) = 75 at level 0.7:
    # each group's own cells hold enough, but [20, 30) holds 55 - 50 = 5.
    statistics = (
        _binned(_quantile("", 100, 1, 30, 50), level="0.4", edges=FIVE_BINS),
        _binned(_quantile("", 100, 3, 55, 75), level="0.7", edges=FIVE_BINS),
    )
    decisions = _check(*statistics, total=100)
    assert [_groups(d)[""] for d in decisions] == [PUBLISHED, DIFFERENCING]
    _again(statistics, decisions, 100)


def test_a_histogram_cell_withheld_as_small_is_not_given_back_by_a_quantile() -> None:
    # The histogram withholds [10, 20), 3 records. Its first cell fixes F(10) = 12 and
    # the quantile's bin [20, 30) fixes F(20) = 15, which would give the 3 back. So
    # would the histogram's own cells, with the column's size: 100 - 12 - 45 - 40.
    histogram = _histogram_statistic(
        _histogram("", [12, 3, 45, 40]), cells={"": (PUBLISHED, SMALL, PUBLISHED, PUBLISHED)}
    )
    quantile = _binned(_quantile("", 100, 2, 15, 60), level="0.5")
    statistics = (histogram, quantile)
    decisions = _check(*statistics, total=100)
    assert _groups(decisions[1]) == {"": DIFFERENCING}
    # [20, 30), next to the withheld cell, is withheld rather than [30, 40]: a reader
    # then knows only [10, 30), 48 records.
    assert _cells(decisions[0]) == {"": (PUBLISHED, SMALL, DIFFERENCING, PUBLISHED)}
    _again(statistics, decisions, 100)


def test_the_whole_less_the_other_groups_gives_no_withheld_cell_back() -> None:
    # GB's first cell, 5 records, is withheld. GB's own second cell gives it back from
    # GB's size, and so does the whole's first cell less IE's: every count that would
    # is withheld in turn, until only the groups' sizes and the whole are left.
    whole = _histogram_statistic(_histogram("", [25, 30]), edges=(0, 10, 20))
    grouped = _histogram_statistic(
        _histogram("IE", [20, 20]),
        _histogram("GB", [5, 10]),
        edges=(0, 10, 20),
        group_by="market",
        cells={"GB": (SMALL, PUBLISHED)},
    )
    statistics = (whole, grouped)
    decisions = _check(*statistics, total=55)
    assert _cells(decisions[0]) == {"": (PUBLISHED, PUBLISHED)}
    assert _groups(decisions[1]) == {"IE": PUBLISHED, "GB": PUBLISHED}
    assert _cells(decisions[1]) == {"IE": (DIFFERENCING, DIFFERENCING), "GB": (SMALL, DIFFERENCING)}
    _again(statistics, decisions, 55)


def test_groups_withheld_beside_the_whole_withhold_the_rest_of_their_groupby() -> None:
    # A and B are withheld; the whole less C would give A and B together below each
    # edge, and [0, 10) of them holds 30 - 25 = 5.
    edges = (0, 10, 20, 30)
    whole = _histogram_statistic(_histogram("", [30, 30, 40]), edges=edges)
    grouped = _histogram_statistic(
        _histogram("A", [3, 10, 12]),
        _histogram("B", [2, 10, 13]),
        _histogram("C", [25, 10, 15]),
        edges=edges,
        group_by="market",
        status={"A": SMALL, "B": SMALL},
    )
    statistics = (whole, grouped)
    decisions = _check(*statistics, total=100)
    assert _groups(decisions[1]) == {"A": SMALL, "B": SMALL, "C": DIFFERENCING}
    _again(statistics, decisions, 100)


def test_a_groupby_alone_on_its_column_keeps_its_groups_that_show_nothing_small() -> None:
    # No whole count and no other groupBy to subtract from: GB's small cell is withheld,
    # and IE, known in full, is published.
    grouped = _histogram_statistic(
        _histogram("IE", [20, 20]),
        _histogram("GB", [5, 10]),
        edges=(0, 10, 20),
        group_by="market",
        cells={"GB": (SMALL, PUBLISHED)},
    )
    (decision,) = _check(grouped, total=55)
    assert _groups(decision) == {"IE": PUBLISHED, "GB": PUBLISHED}
    assert _cells(decision)["IE"] == (PUBLISHED, PUBLISHED)
    assert _cells(decision)["GB"] == (SMALL, DIFFERENCING)
    _again((grouped,), (decision,), 55)


def test_one_groupby_known_in_full_gives_another_no_withheld_cell_back() -> None:
    # The market's groups, known in full, add up to the whole at every edge, so the
    # whole less web would give shop's withheld cells back: web's cells are withheld.
    edges = (0, 10, 20)
    market = _histogram_statistic(
        _histogram("IE", [20, 20]), _histogram("GB", [15, 10]), edges=edges, group_by="market"
    )
    channel = _histogram_statistic(
        _histogram("web", [30, 24]),
        _histogram("shop", [5, 6]),
        edges=edges,
        group_by="channel",
        cells={"shop": (SMALL, SMALL)},
    )
    statistics = (market, channel)
    decisions = _check(*statistics, total=65)
    assert _groups(decisions[0]) == {"IE": PUBLISHED, "GB": PUBLISHED}
    assert _cells(decisions[0]) == {"IE": (PUBLISHED,) * 2, "GB": (PUBLISHED,) * 2}
    assert _cells(decisions[1]) == {"web": (DIFFERENCING,) * 2, "shop": (SMALL,) * 2}
    _again(statistics, decisions, 65)


def test_a_histogram_and_a_coarsening_of_it_give_no_withheld_cell_back() -> None:
    # The coarse cell [10, 30) less the fine cell [10, 20) is the fine cell [20, 30),
    # 4 records, withheld: a reader needs neither end of it to know that.
    fine = _histogram_statistic(_histogram("", [3, 20, 4, 3]))
    coarse = _histogram_statistic(_histogram("", [3, 24, 3]), edges=(0, 10, 30, 40))
    statistics = (fine, coarse)
    decisions = _check(*statistics, total=30)
    # Both published cells tie edges that nothing ties to either end of the range.
    assert _cells(decisions[0]) == {"": (SMALL, DIFFERENCING, SMALL, SMALL)}
    assert _cells(decisions[1]) == {"": (SMALL, DIFFERENCING, SMALL)}
    _again(statistics, decisions, 30)


def test_a_published_cell_between_withheld_ones_gives_their_union_back_unless_withheld() -> None:
    # [10, 1, 20, 1, 10]: the whole less the outer cells is 22, less the middle cell 2,
    # the two withheld cells together: so the middle cell, tied to neither end, goes.
    histogram = _histogram_statistic(_histogram("", [10, 1, 20, 1, 10]), edges=FIVE_BINS)
    (decision,) = _check(histogram, total=42)
    assert _cells(decision) == {"": (PUBLISHED, SMALL, DIFFERENCING, SMALL, PUBLISHED)}
    _again((histogram,), (decision,), 42)


def test_a_groupby_alone_is_not_withheld_for_the_whole_its_sums_give() -> None:
    # Only the market publishes; its two groups share their bin's edges, so their sums
    # give the whole there, which a reader could not use against anything else.
    quantile = _binned(
        _quantile("a", 100, 2, 40, 60), _quantile("b", 100, 2, 40, 60), group_by="market"
    )
    (decision,) = _check(quantile, total=200)
    assert _groups(decision) == {"a": PUBLISHED, "b": PUBLISHED}


def test_a_statistic_that_does_not_span_the_partition_is_off_it() -> None:
    histogram = _histogram_statistic(_histogram("", [20, 20, 15, 20]))
    short = _binned(_quantile("", 75, 1, 20, 40), level="0.5", edges=(0, 10, 20, 30))
    decisions = _check(histogram, short, total=75)
    assert _groups(decisions[1]) == {"": OFF_PARTITION}


def test_a_group_a_null_key_leaves_out_is_read_from_the_others_and_checked() -> None:
    # The records with no market are passed as one more group, never published. The
    # whole less IE and GB gives its cells, 10 and 12, which hold enough: all published.
    whole = _histogram_statistic(_histogram("", [40, 47]), edges=(0, 10, 20))
    grouped = _histogram_statistic(
        _histogram("IE", [15, 20]),
        _histogram("GB", [15, 15]),
        _histogram("(none)", [10, 12]),
        edges=(0, 10, 20),
        group_by="market",
        status={"(none)": OFF_PARTITION},
    )
    statistics = (whole, grouped)
    decisions = _check(*statistics, total=87)
    assert _groups(decisions[1]) == {"IE": PUBLISHED, "GB": PUBLISHED, "(none)": OFF_PARTITION}
    assert _cells(decisions[1]) == {"IE": (PUBLISHED,) * 2, "GB": (PUBLISHED,) * 2}
    _again(statistics, decisions, 87)


@pytest.mark.parametrize(
    "statistics, message",
    [
        pytest.param(
            (_histogram_statistic(_histogram("", [20, 20, 20, 20])),),
            "ungrouped statistic counts other records",
            id="an ungrouped statistic of another size",
        ),
        pytest.param(
            (_histogram_statistic(_histogram("IE", [20, 20, 20, 20]), group_by="market"),),
            "do not add up to the column's total",
            id="groups that leave records out",
        ),
        pytest.param(
            (
                _histogram_statistic(_histogram("IE", [30, 30, 0, 0]), group_by="market"),
                _histogram_statistic(_histogram("IE", [30, 0, 0, 0]), group_by="market"),
            ),
            "two sizes",
            id="one group with two sizes",
        ),
        pytest.param(
            (
                EdgeStatistic(
                    identifier=MARGINAL_HISTOGRAM.identifier,
                    edges=EDGES,
                    groups=(GroupStatistic(group="", k=60, bin_counts=(20, 20, 20, 20)),),
                ),
            ),
            "cells that do not add up",
            id="cells that are not the group",
        ),
        pytest.param(
            (_binned(_quantile("", 60, 1, 40, 20)),),
            "more records below a bin",
            id="a quantile's counts out of order",
        ),
        pytest.param(
            (
                _histogram_statistic(
                    _histogram("", [15, 15, 15, 15]), cells={"": (PUBLISHED, SMALL)}
                ),
            ),
            "a status for other cells",
            id="statuses for other cells",
        ),
        pytest.param(
            (
                _histogram_statistic(_histogram("", [15, 15, 15, 15])),
                _binned(_quantile("", 60, 1, 20, 30), level="0.4"),
            ),
            "disagree",
            id="a quantile and a histogram that disagree",
        ),
        pytest.param(
            (_level_statistic(_levels("", [20, 20, 20])),),
            "cells that do not add up",
            id="levels that are not the declared ones",
        ),
        pytest.param(
            (
                _level_statistic(_levels("", [15, 15, 15, 15])),
                _histogram_statistic(_histogram("", [15, 15, 15, 15])),
            ),
            "levels or over its bins",
            id="levels and bins on one column",
        ),
        pytest.param(
            (_ranked(_witnessed("", 60, 25, 40, 30)),),
            "ranks its witness outside",
            id="a quantile/1's ranks out of order",
        ),
        pytest.param(
            (
                EdgeStatistic(
                    identifier=QUANTILE.identifier,
                    edges=(),
                    groups=(_witnessed("", 60, 25, 20, 40),),
                ),
            ),
            "names its level",
            id="a quantile with no level",
        ),
        pytest.param(
            (
                EdgeStatistic(
                    identifier="mean/1", edges=(), groups=(GroupStatistic(group="", k=60),)
                ),
            ),
            "no edge-count rule",
            id="a statistic that counts at no edge",
        ),
    ],
)
def test_counts_that_cannot_describe_one_set_of_records_are_refused(
    statistics: Tuple[EdgeStatistic, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _check(*statistics, total=60)


def test_counts_that_form_no_small_cell_are_all_published() -> None:
    histogram = _histogram_statistic(_histogram("", [20, 20, 20, 20]))
    quantile = _binned(_quantile("", 80, 2, 40, 60), level="0.6")
    decisions = _check(histogram, quantile, total=80)
    assert _cells(decisions[0]) == {"": (PUBLISHED,) * 4}
    assert _groups(decisions[1]) == {"": PUBLISHED}


# ---------------------------------------------------------------------------
# Then rounding
# ---------------------------------------------------------------------------


def test_rounded_counts_that_no_longer_show_the_bin_withhold_the_group() -> None:
    # Exactly, j = 52 lies in (40, 52]; rounded, k is 105, j is 53, and through is 50.
    (decision,) = _check(_binned(_quantile("", 103, 1, 40, 52), level="0.5"), total=103)
    assert _groups(decision) == {"": ROUNDING}


def test_a_positive_count_that_rounds_to_zero_withholds_the_group() -> None:
    group = _quantile("", 50, 1, 10, 30)
    (decision,) = check_edge_counts(
        (_binned(group, level="0.4"),),
        total=50,
        minimum_cell=MINIMUM,
        zeros_disclosive=False,
        round_count=lambda count: _round(count, 20),
    )
    assert _groups(decision) == {"": ROUNDING}


def test_a_profile_spells_every_withheld_value_c_whatever_withheld_it() -> None:
    # One withheld code (ADR 0012): a status says nothing of what it withholds.
    histogram = _histogram_statistic(
        _histogram("", [12, 3, 45, 40]), cells={"": (PUBLISHED, SMALL, PUBLISHED, PUBLISHED)}
    )
    quantile = _binned(_quantile("", 100, 2, 15, 60), level="0.5")
    off = _binned(_quantile("", 100, 0, 0, 30), level="0.25", edges=(0, 15, 40))
    decisions = _check(histogram, quantile, off, total=100)
    spelled = {status for d in decisions for status in d.group_status.values()}
    spelled |= {status for d in decisions for cells in d.cell_status.values() for status in cells}
    assert spelled == {PUBLISHED, WITHHELD}
    assert _groups(decisions[1]) == {"": DIFFERENCING}
    assert _groups(decisions[2]) == {"": OFF_PARTITION}
    assert _cells(decisions[0])[""][1] == SMALL


def test_no_statistic_is_no_decision() -> None:
    assert _check(total=10) == ()


def test_the_threshold_is_a_positive_count() -> None:
    with pytest.raises(ValueError, match="positive count"):
        check_edge_counts((), total=10, minimum_cell=0, zeros_disclosive=False, round_count=_round)


# ---------------------------------------------------------------------------
# Against an oracle: no cell a reader can form is small
# ---------------------------------------------------------------------------


def _spans(functionals: Sequence[Sequence[Fraction]], target: Sequence[Fraction]) -> bool:
    """Whether *target* is a combination of *functionals*, by exact elimination."""
    width = len(target)
    basis: List[List[Fraction]] = []
    pivots: List[int] = []
    for vector in functionals:
        row = list(vector)
        for pivot, base in zip(pivots, basis, strict=True):
            if row[pivot]:
                factor = row[pivot] / base[pivot]
                row = [a - factor * b for a, b in zip(row, base, strict=True)]
        lead = next((i for i in range(width) if row[i]), None)
        if lead is not None:
            basis.append(row)
            pivots.append(lead)
    rest = list(target)
    for pivot, base in zip(pivots, basis, strict=True):
        if rest[pivot]:
            factor = rest[pivot] / base[pivot]
            rest = [a - factor * b for a, b in zip(rest, base, strict=True)]
    return not any(rest)


def _quantile_of(
    key: str,
    counts: Sequence[int],
    edges: Tuple[int, ...],
    level: Fraction,
    partition: Tuple[int, ...],
) -> GroupStatistic:
    """The quantile/2 group a profiler measures from a row's records per partition cell."""
    k = sum(counts)
    if k == 0:
        return GroupStatistic(group=key, k=0)
    index = order_statistic_index(k, level)
    cumulative = [sum(counts[: partition.index(edge)]) for edge in edges]
    cumulative[-1] = k
    for position in range(len(edges) - 1):
        if cumulative[position] < index <= cumulative[position + 1]:
            return _quantile(key, k, position, cumulative[position], cumulative[position + 1])
    raise AssertionError("an order statistic lies in some bin")


def _over(counts: Sequence[int], edges: Tuple[int, ...], partition: Tuple[int, ...]) -> List[int]:
    """A row's records per partition cell, added up into the cells of *edges*."""
    cuts = [partition.index(edge) for edge in edges]
    return [sum(counts[low:high]) for low, high in zip(cuts, cuts[1:], strict=False)]


NULL = "(none)"


def _oracle(seed: int, *, kind: str) -> None:
    """Draw a column and its statistics, check them, and look for a small cell a reader can form.

    The records are modelled jointly by market and channel, with an optional
    null market, per partition cell; a market or a channel may hold none. *kind*
    draws histograms and ``quantile/2`` statistics over bins, level histograms,
    or one ``quantile/1`` alone, whose witness is a cell's value. Odd seeds hold
    zeros disclosive. A groupBy's sizes are known to the reader unless one of its
    groups is small, when the size rule withholds them all.
    """
    chance = random.Random(seed)
    zeros = seed % 2 == 1
    levels = kind == "levels"

    def small(count: int) -> bool:
        return count < MINIMUM and (count > 0 or zeros)

    cells = chance.randint(2, 4)
    partition = tuple(range(cells + 1)) if levels else tuple(10 * i for i in range(cells + 1))
    markets = ["M%d" % index for index in range(chance.randint(2, 3))]
    if chance.random() < 0.3:
        markets.append(NULL)
    channels = ["web", "shop"]
    joint = {
        (market, channel): [chance.choice([0, 0, 1, 3, 6, 10, 12, 20]) for _ in range(cells)]
        for market in markets
        for channel in channels
    }
    if chance.random() < 0.25:
        # A market that holds no record, which a caller may still pass as a group.
        empty = chance.choice(markets)
        for channel in channels:
            joint[(empty, channel)] = [0] * cells
    rows: Dict[Tuple[str, str], List[int]] = {("", ""): [0] * cells}
    for (market, channel), counts in joint.items():
        for key in (("", ""), ("market", market), ("channel", channel)):
            row = rows.setdefault(key, [0] * cells)
            for cell, count in enumerate(counts):
                row[cell] += count
    total = sum(rows[("", "")])
    if total < MINIMUM:
        return  # the profile withholds a column of too few records

    def keys_of(owner: str) -> List[str]:
        return [""] if owner == "" else (markets if owner == "market" else channels)

    def coarsening() -> Tuple[int, ...]:
        inner = [edge for edge in partition[1:-1] if chance.random() < 0.6]
        return (partition[0], *inner, partition[-1])

    def histogram(owner: str, edges: Tuple[int, ...]) -> EdgeStatistic:
        keys = keys_of(owner)
        status = {NULL: OFF_PARTITION} if NULL in keys else None
        if levels:
            groups = [_levels(key, rows[(owner, key)]) for key in keys]
            names = tuple(LEVELS[:cells]) if cells <= len(LEVELS) else tuple(map(str, partition))
            return _level_statistic(*groups, levels=names, group_by=owner or None, status=status)
        groups = [_histogram(key, _over(rows[(owner, key)], edges, partition)) for key in keys]
        return _histogram_statistic(*groups, edges=edges, group_by=owner or None, status=status)

    def quantile(owner: str, edges: Tuple[int, ...], level: Fraction) -> EdgeStatistic:
        keys = keys_of(owner)
        groups = [_quantile_of(key, rows[(owner, key)], edges, level, partition) for key in keys]
        return _binned(
            *groups,
            level=str(level),
            edges=edges,
            group_by=owner or None,
            status={NULL: OFF_PARTITION} if NULL in keys else None,
        )

    def witnessed(owner: str, level: Fraction) -> Tuple[EdgeStatistic, Dict[str, int]]:
        groups: List[GroupStatistic] = []
        cell_of: Dict[str, int] = {}
        for key in keys_of(owner):
            counts = rows[(owner, key)]
            k = sum(counts)
            if k == 0:
                groups.append(GroupStatistic(group=key, k=0))
                continue
            index = order_statistic_index(k, level)
            below = [sum(counts[:cut]) for cut in range(cells + 1)]
            cell = next(c for c in range(cells) if below[c] < index <= below[c + 1])
            cell_of[key] = cell
            groups.append(_witnessed(key, k, cell, below[cell], below[cell + 1]))
        status = {NULL: OFF_PARTITION} if NULL in keys_of(owner) else None
        return _ranked(*groups, level=str(level), group_by=owner or None, status=status), cell_of

    statistics: List[EdgeStatistic] = []
    cell_of: Dict[str, int] = {}
    if kind == "witness":
        level = Fraction(chance.choice([1, 2, 3, 4, 5, 6, 7, 8, 9]), 10)
        lone, cell_of = witnessed(chance.choice(["", "market", "channel"]), level)
        statistics.append(lone)
    for owner in ("", "market", "channel") if kind != "witness" else ():
        if chance.random() < 0.55:
            coarse = not levels and chance.random() >= 0.6
            statistics.append(histogram(owner, coarsening() if coarse else partition))
    for _ in range(chance.randint(0, 3) if kind == "bins" else 0):
        owner = chance.choice(["", "market", "channel"])
        level = Fraction(chance.choice([1, 2, 3, 4, 5, 6, 7, 8, 9]), 10)
        statistics.append(quantile(owner, coarsening(), level))
    if not statistics:
        return
    chance.shuffle(statistics)
    decisions = _check(*statistics, total=total, zeros=zeros)

    # Unknowns: the records of each market and channel in each partition cell.
    variables = [(market, channel, cell) for market, channel in joint for cell in range(cells)]

    def vector(owner: str, keys: Sequence[str], low: int, high: int) -> List[Fraction]:
        def holds(market: str, channel: str) -> bool:
            if owner == "":
                return True
            return (market if owner == "market" else channel) in keys

        return [
            Fraction(1 if holds(market, channel) and low <= cell < high else 0)
            for market, channel, cell in variables
        ]

    known: List[List[Fraction]] = [vector("", [""], 0, cells)]
    for owner in ("market", "channel"):
        if not any(small(sum(rows[(owner, key)])) for key in keys_of(owner)):
            known += [vector(owner, [key], 0, cells) for key in keys_of(owner)]
    for statistic, decision in zip(statistics, decisions, strict=True):
        owner = statistic.group_by or ""
        edges = (
            partition if levels or statistic.identifier == QUANTILE.identifier else statistic.edges
        )
        cuts = [partition.index(edge) for edge in edges]
        for group in statistic.groups:
            if decision.group_status[group.group] != PUBLISHED:
                continue
            known.append(vector(owner, [group.group], 0, cells))  # its size, published with it
            if statistic.identifier == QUANTILE.identifier:
                cell = cell_of[group.group]
                known.append(vector(owner, [group.group], 0, cell))
                known.append(vector(owner, [group.group], 0, cell + 1))
            elif statistic.identifier == QUANTILE_BINNED.identifier:
                assert group.quantile_bin is not None
                known.append(vector(owner, [group.group], 0, cuts[group.quantile_bin]))
                known.append(vector(owner, [group.group], 0, cuts[group.quantile_bin + 1]))
            else:
                for cell, status in enumerate(decision.cell_status[group.group]):
                    if status == PUBLISHED:
                        known.append(vector(owner, [group.group], cuts[cell], cuts[cell + 1]))

    targets: List[Tuple[str, Tuple[str, ...]]] = [("", ("",))]
    for owner in ("market", "channel"):
        keys = keys_of(owner)
        for size in range(1, len(keys) + 1):
            targets += [(owner, combination) for combination in itertools.combinations(keys, size)]
    for owner, keys in targets:
        # Every set of cells; all of them is the row's size, public only for the whole.
        last = 2**cells - 1 if owner == "" else 2**cells
        for mask in range(1, last):
            chosen = [cell for cell in range(cells) if mask >> cell & 1]
            count = sum(
                joint[(market, channel)][cell]
                for market, channel in joint
                if owner == "" or (market if owner == "market" else channel) in keys
                for cell in chosen
            )
            if small(count):
                target = [
                    sum(vector(owner, keys, cell, cell + 1)[i] for cell in chosen)
                    for i in range(len(variables))
                ]
                assert not _spans(known, target), (
                    f"seed {seed}: {owner} {keys} in cells {chosen} holds {count} "
                    "and a reader can form it"
                )


@pytest.mark.parametrize("seed", range(400))
def test_no_cell_a_reader_can_form_from_what_is_published_is_small(seed: int) -> None:
    _oracle(seed, kind="bins")


@pytest.mark.parametrize("seed", range(200))
def test_no_level_a_reader_can_form_from_what_is_published_is_small(seed: int) -> None:
    _oracle(seed, kind="levels")


@pytest.mark.parametrize("seed", range(200))
def test_no_cell_a_lone_quantile_1_publishes_is_small(seed: int) -> None:
    _oracle(seed, kind="witness")


# ---------------------------------------------------------------------------
# A quantile/1 counts at its witness, an edge no declaration fixes
# ---------------------------------------------------------------------------


def test_a_quantile_1_alone_on_its_column_is_published_with_rounded_ranks() -> None:
    # 40 records, the median is the 20th: 17 below, 10 holding it and 13 above.
    (decision,) = _check(_ranked(_witnessed("", 40, 25, 17, 27)), total=40)
    assert _groups(decision) == {"": PUBLISHED}
    shown = decision.rounded[""]
    assert (shown.k, shown.quantile_below, shown.quantile_at_or_below) == (40, 15, 25)
    assert shown.quantile_witness == 25


@pytest.mark.parametrize(
    "group",
    [
        pytest.param(_witnessed("", 40, 25, 5, 27), id="five-below"),
        pytest.param(_witnessed("", 40, 25, 17, 37), id="three-above"),
        pytest.param(_witnessed("", 40, 25, 17, 26), id="nine-hold-it"),
    ],
)
def test_a_quantile_1_with_a_small_cell_is_withheld(group: GroupStatistic) -> None:
    (decision,) = _check(_ranked(group), total=40)
    assert _groups(decision) == {"": SMALL}
    assert decision.group_status == {"": WITHHELD}


def test_a_quantile_1_at_an_end_of_its_records_is_published() -> None:
    # None below: the witness is the least value, which 12 records hold.
    (decision,) = _check(_ranked(_witnessed("", 40, 0, 0, 12), level="0.25"), total=40)
    assert _groups(decision) == {"": PUBLISHED}


def test_rounded_ranks_that_no_longer_prove_the_witness_withhold_a_quantile_1() -> None:
    # 18 below and 28 at or below prove the 20th of 40; rounded, 20 and 30 do not.
    (decision,) = _check(_ranked(_witnessed("", 40, 25, 18, 28)), total=40)
    assert _groups(decision) == {"": ROUNDING}


@pytest.mark.parametrize(
    "beside",
    [
        pytest.param(_histogram_statistic(_histogram("", [10, 10, 10, 10])), id="a-histogram"),
        pytest.param(_binned(_quantile("", 40, 1, 10, 30)), id="a-quantile-2"),
        pytest.param(_ranked(_witnessed("", 40, 12, 10, 22), level="0.4"), id="another-level"),
    ],
)
def test_a_quantile_1_beside_another_count_at_edges_is_withheld(beside: EdgeStatistic) -> None:
    decisions = _check(_ranked(_witnessed("", 40, 25, 17, 27)), beside, total=40)
    assert _groups(decisions[0]) == {"": OFF_PARTITION}
    if beside.identifier == QUANTILE.identifier:
        assert _groups(decisions[1]) == {"": OFF_PARTITION}
    else:
        assert _groups(decisions[1]) == {"": PUBLISHED}


def test_a_quantile_1_of_the_whole_beside_one_by_a_groupby_withholds_both() -> None:
    whole = _ranked(_witnessed("", 40, 25, 17, 27))
    by_market = _ranked(
        _witnessed("IE", 20, 25, 0, 10), _witnessed("GB", 20, 30, 0, 12), group_by="market"
    )
    decisions = _check(whole, by_market, total=40)
    assert _groups(decisions[0]) == {"": OFF_PARTITION}
    assert _groups(decisions[1]) == {"IE": OFF_PARTITION, "GB": OFF_PARTITION}


# ---------------------------------------------------------------------------
# A group's size
# ---------------------------------------------------------------------------


def test_a_group_withheld_for_its_size_withholds_its_siblings_in_every_statistic() -> None:
    # LU holds 5 records: the column's 105 less IE's 100 would give them back.
    histogram = _histogram_statistic(
        _histogram("IE", [40, 30, 20, 10]), _histogram("LU", [2, 1, 1, 1]), group_by="market"
    )
    quantile = _binned(
        _quantile("IE", 100, 1, 40, 70), _quantile("LU", 5, 1, 2, 3), group_by="market"
    )
    whole = _histogram_statistic(_histogram("", [42, 31, 21, 11]))
    statistics = (histogram, quantile, whole)
    decisions = _check(*statistics, total=105)
    assert _groups(decisions[0]) == {"IE": DIFFERENCING, "LU": SMALL}
    assert _groups(decisions[1]) == {"IE": DIFFERENCING, "LU": SMALL}
    assert _groups(decisions[2]) == {"": PUBLISHED}
    assert _cells(decisions[2]) == {"": (PUBLISHED,) * 4}
    _again(statistics, decisions, 105)


def test_a_small_group_a_null_key_leaves_out_withholds_its_groupby() -> None:
    # The 4 records with no market are in no group, and still the column's 104 less IE's 100.
    histogram = _histogram_statistic(
        _histogram("IE", [40, 30, 20, 10]),
        _histogram(NULL, [1, 1, 1, 1]),
        group_by="market",
        status={NULL: OFF_PARTITION},
    )
    (decision,) = _check(histogram, total=104)
    assert _groups(decision) == {"IE": DIFFERENCING, NULL: OFF_PARTITION}


def test_a_small_group_leaves_another_groupby_alone() -> None:
    edges = (0, 10, 20)
    market = _histogram_statistic(
        _histogram("IE", [60, 40]), _histogram("LU", [3, 2]), edges=edges, group_by="market"
    )
    channel = _histogram_statistic(
        _histogram("web", [33, 22]), _histogram("shop", [30, 20]), edges=edges, group_by="channel"
    )
    decisions = _check(market, channel, total=105)
    assert _groups(decisions[0]) == {"IE": DIFFERENCING, "LU": SMALL}
    assert _groups(decisions[1]) == {"web": PUBLISHED, "shop": PUBLISHED}
    assert _cells(decisions[1]) == {"web": (PUBLISHED,) * 2, "shop": (PUBLISHED,) * 2}


def test_a_column_of_too_few_records_publishes_no_group() -> None:
    (decision,) = _check(_histogram_statistic(_histogram("", [3, 2, 2, 1])), total=8)
    assert _groups(decision) == {"": SMALL}


# ---------------------------------------------------------------------------
# A level histogram, checked over its declared levels as over bins
# ---------------------------------------------------------------------------


def test_one_withheld_level_is_not_given_back_by_the_others() -> None:
    # DE holds 3 records; the group's 123 less the other levels' 120 would give them back.
    # FR, beside it, is withheld with it, so a reader knows only DE and FR together.
    statistic = _level_statistic(_levels("", [3, 40, 40, 40]))
    (decision,) = _check(statistic, total=123)
    assert _groups(decision) == {"": PUBLISHED}
    assert _cells(decision) == {"": (SMALL, DIFFERENCING, PUBLISHED, PUBLISHED)}
    _again((statistic,), (decision,), 123)


def test_a_level_histogram_by_a_groupby_gives_no_withheld_level_back_with_the_whole() -> None:
    whole = _level_statistic(_levels("", [30, 25, 20, 45]))
    by_tier = _level_statistic(
        _levels("gold", [26, 20, 15, 40]), _levels("base", [4, 5, 5, 5]), group_by="tier"
    )
    statistics = (whole, by_tier)
    decisions = _check(*statistics, total=120)
    published = {
        (key, level)
        for key, statuses in decisions[1].cell_status.items()
        for level, status in enumerate(statuses)
        if status == PUBLISHED
    }
    # The whole less gold would be base, 4 or 5 records a level: no gold level is shown
    # while the whole's is.
    assert not {key for key, _ in published} & {"base"}
    assert not published or all(
        decisions[0].cell_status[""][level] != PUBLISHED for _, level in published
    )
    _again(statistics, decisions, 120)


def test_a_level_histogram_over_other_levels_is_off_the_partition() -> None:
    first = _level_statistic(_levels("", [10, 10, 10, 10]))
    other = _level_statistic(_levels("", [20, 20]), levels=("FR", "GB"))
    decisions = _check(first, other, total=40)
    assert _groups(decisions[0]) == {"": PUBLISHED}
    assert _groups(decisions[1]) == {"": OFF_PARTITION}


# ---------------------------------------------------------------------------
# Zeros
# ---------------------------------------------------------------------------


def test_a_zero_is_protected_when_zeros_are_disclosive() -> None:
    histogram = _histogram_statistic(_histogram("", [12, 0, 12]), edges=(0, 10, 20, 30))
    (decision,) = _check(histogram, total=24, zeros=True)
    # The size less the other cells would give the zero back: the cell beside it goes too.
    assert _cells(decision) == {"": (PUBLISHED, SMALL, DIFFERENCING)}
    _again((histogram,), (decision,), 24, zeros=True)
    (shown,) = _check(histogram, total=24)
    assert _cells(shown) == {"": (PUBLISHED,) * 3}


def test_a_group_of_no_records_is_withheld_with_its_siblings_when_zeros_are_disclosive() -> None:
    # LU holds no record: the column's 40 less IE's 40 would say so.
    histogram = _histogram_statistic(
        _histogram("IE", [10, 10, 10, 10]), _histogram("LU", [0, 0, 0, 0]), group_by="market"
    )
    (decision,) = _check(histogram, total=40, zeros=True)
    assert _groups(decision) == {"IE": DIFFERENCING, "LU": SMALL}
    (shown,) = _check(histogram, total=40)
    assert _groups(shown) == {"IE": PUBLISHED, "LU": PUBLISHED}


def test_no_record_below_a_quantile_1_witness_is_a_zero_when_zeros_are_disclosive() -> None:
    # None below: no record lies between the range's start and the witness.
    lone = _ranked(_witnessed("", 40, 0, 0, 12), level="0.25")
    (decision,) = _check(lone, total=40, zeros=True)
    assert _groups(decision) == {"": SMALL}
    (shown,) = _check(lone, total=40)
    assert _groups(shown) == {"": PUBLISHED}
