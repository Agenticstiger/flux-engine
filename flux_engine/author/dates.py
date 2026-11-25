# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Lifetimes and dates within a linked row's lifetime, declared from a profile's date pairs (ADR 0024).

``fluxsim init`` reads what profile format 1-3-0 publishes of the days between two date columns
of a table (``datePairs``) and between a parent's date and a child's (a relationship's ``dates``),
and writes the two namespace keys that generate them (``lifetimes`` and ``within``,
:mod:`flux_engine.seam.dates`). It declares an order only where the profile shows it holds on at
least :data:`ORDER_HOLDS_AT_LEAST` of the rows where both dates are known.

A lifetime's duration is the pair's histogram cut at day 0; when the pair also publishes a histogram
grouped by a column of the table, it is a dwell family given that column, one histogram per
published group and the ungrouped one as ``otherwise`` (ADR 0025 sections 3 and 7). A pair with no
usable duration gives a lifetime with its start only, never an end drawn apart from it, because
such an end holds its order only by chance.

A column a ``within`` entry or a duration sets, for which the profile publishes a ``calendar``,
also gets a ``rates`` entry (ADR 0025 sections 2 and 7): each published histogram is divided level
by level by the number of times the level occurs inside the window ``init`` writes, so a uniform
base reproduces the published counts.

Each published pair and calendar histogram is used or recorded as unused with its reason
(ADR 0014), so a bundle never drops one without saying so.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from math import gcd, lcm
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.author.columns import _share_text, cell_weights, count_cells
from flux_engine.author.forms import SUPERSEDED, grouped, number_grouped
from flux_engine.author.orders import DateOrder
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.dates import MOST_STATES
from flux_engine.seam.keys import ContractKeys
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM
from flux_stats.profile import CALENDAR_LEVELS
from flux_stats.spelling import MOST_DIGITS

__all__ = [
    "DATE_KINDS",
    "DatedExpose",
    "DateDeclarations",
    "ORDER_HOLDS_AT_LEAST",
    "declare_dates",
    "window_end_ms",
    "window_start_ms",
]

#: The share of the rows where both dates are known on which an order must hold for ``init`` to
#: declare it. Two independent date columns break an order about as often as they keep it, near
#: half of the rows, so a lower bar would turn noise into a declared fact with a large target.
ORDER_HOLDS_AT_LEAST = Fraction(9, 10)

DATE_KINDS = (LogicalKind.DATE, LogicalKind.TIMESTAMP)

#: The levels of a pair's ``order``, in the code point order the profile counts them in.
_AFTER, _BEFORE, _SAME = 0, 1, 2

_Note = Tuple[Tuple[str, ...], str]

_GROUPED_UNREAD = (
    "a statistic grouped by a column is read only as a histogram of the days grouped by a column "
    "of the table, for a dwell family (ADR 0025 section 3)"
)

_NO_DURATION = "no usable duration: an end drawn apart from its start would break the order"
#: Why a snapshot's date pair is no lifetime when the history it is projected from sets both dates.
_BOTH_CARRIED = "the history sets both dates"


@dataclass(frozen=True)
class DatedExpose:
    """What the date rules need of one written expose."""

    #: Its DATE and TIMESTAMP columns, with their kinds.
    kinds: Mapping[str, LogicalKind]
    #: Its columns that a key, a derive, a dependency or a copy already sets.
    blocked: FrozenSet[str]
    #: Every column the bundle writes for it, or ``None`` when that is not checked: a dwell
    #: family is given one of them.
    columns: Optional[FrozenSet[str]] = None
    #: Its columns that the history written for it, or the projection entry (``events`` or
    #: ``periods``) of a history written for it, sets, each with the sentence that says so: the
    #: validity start and end and the ``activation`` column of a history, the date column, the
    #: period column and the columns a projection entry takes from its history. A lifetime or a
    #: ``within`` entry would be a second source for them.
    history: Mapping[str, str] = field(default_factory=dict)
    #: Its columns that a projection entry sets, each with the sentence that says so. A ``within``
    #: entry reads through a relationship, and the plan refuses one whose foreign-key column the
    #: entry sets, since the cell then holds the history's value, not the row the relationship drew.
    projected_keys: Mapping[str, str] = field(default_factory=dict)
    #: Its columns a ``snapshots`` entry takes from the history and no rule of the contract sets.
    #: A lifetime over two of them would be a second source for both: the history sets both dates.
    carried: FrozenSet[str] = frozenset()

    #: The orders its contract's ``check`` rules declare between two of its dates.
    orders: Tuple[DateOrder, ...] = ()
    #: Its DATE and TIMESTAMP columns the contract makes ``required``.
    required: FrozenSet[str] = frozenset()
    #: The span each date column's declared draw reaches, in milliseconds, where it declares one.
    bounds: Mapping[str, Tuple[int, int]] = field(default_factory=dict)


@dataclass
class DateDeclarations:
    """The entries to write, and what each statistic of the profile came to."""

    lifetimes: List[Dict[str, Any]]
    within: List[Dict[str, Any]]
    used: List[_Note]
    unused: List[_Note]
    #: The ``rates`` entries, one per column a calendar shapes.
    rates: List[Dict[str, Any]] = field(default_factory=list)
    #: The checks of the contract that order two dates and were not written, as (subject, why).
    declined_orders: List[Tuple[str, str]] = field(default_factory=list)
    #: What each written number rests on: the entry, the member of it (tokens below the entry), the
    #: statistic of the profile it was read from (tokens into the profile) and the sentence that
    #: says how. The entry is one of ``lifetimes``, ``within`` or ``rates``, by identity.
    cited: List[Tuple[Dict[str, Any], Tuple[str, ...], Optional[Tuple[str, ...]], str]] = field(
        default_factory=list
    )


def window_end_ms(end: str) -> int:
    """The window's end, an RFC 3339 instant as ``init`` writes it, in milliseconds since the epoch."""
    instant = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(timezone.utc)
    return (instant - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)


def window_start_ms(start: str) -> int:
    """The window's start, an RFC 3339 instant as ``init`` writes it, in milliseconds since the epoch."""
    return window_end_ms(start)


def _percent(share: Fraction) -> str:
    """*share* as a percent to one place, for a sentence."""
    value = (Decimal(share.numerator) * 100 / Decimal(share.denominator)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_EVEN
    )
    return f"{value.normalize():f}%"


def _target(count: Fraction, total: int) -> Optional[str]:
    """*count* of *total* rows as a percent decimal string, or ``None`` when it is zero."""
    text = _share_text(count * 100 / total)
    return None if Fraction(text) == 0 else text


@dataclass(frozen=True)
class _Order:
    """A pair's ``order``: how many rows hold each level."""

    counts: Tuple[int, int, int]

    @property
    def total(self) -> int:
        return sum(self.counts)

    @property
    def after(self) -> int:
        return self.counts[_AFTER]

    @property
    def before(self) -> int:
        return self.counts[_BEFORE]

    @property
    def same(self) -> int:
        return self.counts[_SAME]

    def holds(self, count: int) -> bool:
        return Fraction(count, self.total) >= ORDER_HOLDS_AT_LEAST


def _read_order(statistic: Any) -> Tuple[Optional[_Order], str]:
    """The order a pair publishes, or why its share is unknown."""
    if not isinstance(statistic, Mapping):
        return None, "the profile publishes no order for it"
    if "noise" in statistic:
        return None, "its order is released with noise, which no rule reads for an order"
    groups = statistic.get("groups")
    group = groups[0] if isinstance(groups, list) and len(groups) == 1 else None
    if not isinstance(group, Mapping) or statistic.get("definition") != LEVEL_HISTOGRAM.identifier:
        return None, "its order is not one ungrouped level histogram"
    if group.get("status") != "F":
        return None, f"its order is withheld (status {group.get('status')})"
    cells = count_cells(group, "levelCounts")
    if len(cells) != 3 or any(cell.count is None for cell in cells):
        return None, "a cell of its order is suppressed, so the share that holds it is unknown"
    counts = (int(cells[0].count or 0), int(cells[1].count or 0), int(cells[2].count or 0))
    order = _Order(counts)
    if order.total <= 0:
        return None, "its order counts no row"
    return order, ""


@dataclass(frozen=True)
class _Histogram:
    """A published histogram of the days between two dates: whole-day edges and weights."""

    index: int
    edges: Tuple[int, ...]
    weights: Tuple[Fraction, ...]


def _read_histogram(
    statistics: Any, base: Tuple[str, ...], minimum_cell: int
) -> Optional[_Histogram]:
    """The first ungrouped, published ``marginalHistogram/1`` of a pair, over whole days."""
    for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
        if not isinstance(statistic, Mapping):
            continue
        read = _histogram_of(statistic, base + ("statistics", str(index)), minimum_cell)
        if read is not None:
            return _Histogram(index, read[0], read[1])
    return None


def _histogram_of(
    statistic: Mapping[str, Any], tokens: Tuple[str, ...], minimum_cell: int
) -> Optional[Tuple[Tuple[int, ...], Tuple[Fraction, ...]]]:
    """The whole-day edges and weights of one published, ungrouped histogram, or ``None``."""
    if (
        statistic.get("definition") != MARGINAL_HISTOGRAM.identifier
        or grouped(statistic)
        or "noise" in statistic
    ):
        return None
    groups = statistic.get("groups")
    group = groups[0] if isinstance(groups, list) and len(groups) == 1 else None
    if not isinstance(group, Mapping) or group.get("status") != "F":
        return None
    try:
        edges = tuple(_whole(edge) for edge in statistic.get("bins", ()))
    except ValueError:
        return None
    cells = count_cells(group, "binCounts")
    if len(edges) < 2 or len(cells) != len(edges) - 1:
        return None
    total = group.get("k")
    weights, _ = cell_weights(
        minimum_cell,
        cells,
        tokens,
        "binCounts",
        MARGINAL_HISTOGRAM.identifier,
        total=total if isinstance(total, int) and not isinstance(total, bool) else None,
    )
    if sum(weights, Fraction(0)) <= 0:
        return None
    return edges, tuple(weights)


def _whole(edge: Any) -> int:
    value = Decimal(str(edge))
    if value != value.to_integral_value():
        raise ValueError(edge)
    return int(value)


def _scaled(weights: Sequence[Fraction]) -> List[str]:
    """*weights* as whole numbers in the same proportions, as decimal text."""
    common = lcm(*(weight.denominator for weight in weights))
    whole = [int(weight * common) for weight in weights]
    divisor = gcd(*whole) or 1
    return [str(item // divisor) for item in whole]


def _histogram_member(
    edges: Sequence[int], weights: Sequence[Fraction], *, upper: bool
) -> Dict[str, Any]:
    member: Dict[str, Any] = {
        "bins": [str(edge) for edge in edges],
        "binWeights": _scaled(weights),
    }
    if upper:
        member["closed"] = "upper"
    return member


def _negated(histogram: _Histogram) -> Tuple[List[int], List[Fraction]]:
    """The histogram of the days from the other date: edges negated and reversed, weights reversed."""
    return [-edge for edge in reversed(histogram.edges)], list(reversed(histogram.weights))


def _state_text(key: Any, number: bool) -> str:
    """A group's key as the state's plain text the generator reads (NFC; a number as canonical decimal)."""
    text = key if isinstance(key, str) else ""
    if number and text:
        try:
            value = Decimal(text)
        except ArithmeticError:
            return ""
        text = "0" if value == 0 else format(value.normalize(), "f")
    return unicodedata.normalize("NFC", text)


def _duration(
    edges: List[int], weights: List[Fraction], *, upper: bool
) -> Optional[Dict[str, Any]]:
    """A duration from a histogram of days: nothing below day 0 (``FLX-3105`` refuses a negative one).

    Bins wholly below 0 are dropped, and a bin that straddles 0 starts at 0 with its weight times
    the share of its days from 0 up. A histogram closed at the upper edge is spelled with the
    edges one day later when it is cut, so that every day from 0 stays in a bin that starts at 0.
    """
    if edges[0] >= 0:
        return _histogram_member(edges, weights, upper=upper)
    lower = [edge + 1 for edge in edges] if upper else list(edges)
    kept_edges: List[int] = [0]
    kept_weights: List[Fraction] = []
    for position, weight in enumerate(weights):
        low, high = lower[position], lower[position + 1]
        if high <= 0:
            continue
        if low < 0:
            weight = weight * Fraction(high, high - low)
        kept_edges.append(high)
        kept_weights.append(weight)
    if not kept_weights or sum(kept_weights, Fraction(0)) <= 0:
        return None
    return _histogram_member(kept_edges, kept_weights, upper=False)


def _lifetime_citations(
    entry: Dict[str, Any],
    pair: _Pair,
    family: Optional["_Family"],
    how: Tuple[str, Optional[str]],
) -> List[Tuple[Dict[str, Any], Tuple[str, ...], Tuple[str, ...], str]]:
    """What each number of a lifetime *entry* rests on: its target, its duration and its states.

    *how* is the sentence for the target, and the one for the duration's histogram when it has one.
    """
    order_how, how_plain = how
    histogram = pair.histogram
    found: List[Tuple[Dict[str, Any], Tuple[str, ...], Tuple[str, ...], str]] = []
    if "target" in entry:
        found.append((entry, ("target",), pair.tokens + ("order",), order_how))
    if histogram is not None and how_plain is not None:
        member = ("duration", "otherwise") if family is not None else ("duration",)
        found.append((entry, member, pair.tokens + ("statistics", str(histogram.index)), how_plain))
    if family is not None:
        tokens = pair.tokens + ("statistics", str(family.index))
        found.append((entry, ("duration", "states"), tokens, family.note))
    return found


def _lifetime_duration(
    plain: Optional[Dict[str, Any]], family: Optional["_Family"]
) -> Optional[Dict[str, Any]]:
    """The ``duration`` of a lifetime: its dwell family over the *plain* histogram, or just that histogram."""
    if family is None:
        return plain
    duration: Dict[str, Any] = {"given": family.given, "states": family.states}
    if plain is not None:
        duration["otherwise"] = plain
    return duration


def _plain_how(plain: Optional[Dict[str, Any]], family: Optional["_Family"]) -> Optional[str]:
    """How the *plain* histogram of a lifetime is read, or ``None`` when it gives no duration."""
    if plain is None:
        return None
    if family is not None:
        return "the dwell of a row whose state is not listed, cut at day 0"
    return "the duration of the lifetime, cut at day 0"


def _cut(histogram: _Histogram, reversed_pair: bool) -> Optional[Dict[str, Any]]:
    """A histogram of a pair's days as a duration, oriented as the lifetime reads it and cut at day 0."""
    if reversed_pair:
        edges, weights = _negated(histogram)
    else:
        edges, weights = list(histogram.edges), list(histogram.weights)
    return _duration(edges, weights, upper=reversed_pair)


@dataclass
class _Family:
    """The states of a dwell family read from one grouped histogram."""

    index: int
    given: str
    states: Dict[str, Dict[str, Any]]
    note: str


@dataclass
class _Pair:
    """One published pair of date columns, read."""

    tokens: Tuple[str, ...]
    first: str
    second: str
    item: Mapping[str, Any]
    order: Optional[_Order]
    why_unknown: str
    histogram: Optional[_Histogram]

    def statistic_tokens(self) -> List[Tuple[str, ...]]:
        return statistic_tokens(self.item, self.tokens) + [self.tokens + ("order",)]


def statistic_tokens(item: Mapping[str, Any], tokens: Tuple[str, ...]) -> List[Tuple[str, ...]]:
    """The profile pointer of each statistic of a ``datePairs`` or ``relationships`` entry, as tokens."""
    statistics = item.get("statistics")
    return [
        tokens + ("statistics", str(index))
        for index, _ in enumerate(statistics if isinstance(statistics, list) else ())
    ]


def _pair(
    item: Any, tokens: Tuple[str, ...], first: str, second: str, minimum_cell: int
) -> Optional[_Pair]:
    if not isinstance(item, Mapping):
        return None
    order, why = _read_order(item.get("order"))
    return _Pair(
        tokens=tokens,
        first=first,
        second=second,
        item=item,
        order=order,
        why_unknown=why,
        histogram=_read_histogram(item.get("statistics"), tokens, minimum_cell),
    )


def _add_shaped(found: List[Tuple[str, str]], emit: str, column: Any) -> None:
    """Add (*emit*, *column*) to *found* once, when the column is named."""
    if column is not None and (emit, str(column)) not in found:
        found.append((emit, str(column)))


class _Declarer:
    """The state of one run of :func:`declare_dates`."""

    def __init__(
        self,
        profile: Mapping[str, Any],
        exposes: Mapping[str, DatedExpose],
        keys: Optional[ContractKeys],
        *,
        window_end: int,
        minimum_cell: int,
        window_start: Optional[int] = None,
        declined_exposures: Optional[Mapping[Tuple[str, str], str]] = None,
        declined_within: Optional[Mapping[Tuple[str, str], str]] = None,
        declined_rates: Optional[Mapping[Tuple[str, str], str]] = None,
    ) -> None:
        self.profile = profile
        self.declined_rates = declined_rates or {}
        self.declined_exposures = declined_exposures or {}
        self.declined_within = declined_within or {}
        self.exposes = exposes
        self.keys = keys
        self.window_end = window_end
        self.window_start = window_start
        self.minimum_cell = minimum_cell
        self.declined_orders: List[Tuple[str, str]] = []
        self.used: List[_Note] = []
        self.unused: List[_Note] = []
        self.cited: List[Tuple[Dict[str, Any], Tuple[str, ...], Optional[Tuple[str, ...]], str]] = (
            []
        )
        #: expose -> (start, end or None, the declaration)
        self.lifetimes: Dict[str, Dict[str, Any]] = {}
        #: The exposes whose lifetime is only a start a relationship's date pair offered: one is
        #: kept only while a within entry reads it.
        self.start_only: set[str] = set()
        #: A written within entry's id -> (the entry, its profile relationship's index, its parent).
        self.origin: Dict[int, Tuple[Dict[str, Any], int, str]] = {}

    # -- notes ---------------------------------------------------------------

    def reject(self, pair: _Pair, reason: str) -> None:
        self.unused.extend((tokens, reason) for tokens in pair.statistic_tokens())

    def keep(
        self,
        pair: _Pair,
        how_order: str,
        how_statistic: Optional[str],
        used_how: Optional[Mapping[int, str]] = None,
        unused_why: Optional[Mapping[int, str]] = None,
    ) -> None:
        """Mark a chosen pair: its order and the statistics it reads used, the others unused.

        The pair's ungrouped histogram is used when *how_statistic* says how; *used_how* and
        *unused_why* give the notes of other statistics by position.
        """
        self.used.append((pair.tokens + ("order",), how_order))
        used = dict(used_how or {})
        if pair.histogram is not None and how_statistic is not None:
            used[pair.histogram.index] = how_statistic
        statistics = pair.item.get("statistics")
        for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
            tokens = pair.tokens + ("statistics", str(index))
            if index in used:
                self.used.append((tokens, used[index]))
            elif unused_why and index in unused_why:
                self.unused.append((tokens, unused_why[index]))
            elif isinstance(statistic, Mapping) and grouped(statistic):
                self.unused.append((tokens, _GROUPED_UNREAD))
            else:
                self.unused.append(
                    (
                        tokens,
                        "the days between the dates are drawn from the pair's histogram, so "
                        "no rule reads this statistic of them",
                    )
                )

    def below(self, pair: _Pair, best: Fraction) -> None:
        self.reject(
            pair,
            f"its order holds on {_percent(best)} of the rows where both dates are known, below "
            f"the bar of {_percent(ORDER_HOLDS_AT_LEAST)}",
        )

    # -- lifetimes -----------------------------------------------------------

    def _usable(self, expose: str, *columns: str) -> Optional[str]:
        """Why a pair of an expose cannot be declared, or ``None`` when it can."""
        held = self.exposes.get(expose)
        if held is None:
            return "the bundle writes no generator emit of its table"
        missing = [name for name in columns if name not in held.kinds]
        if missing:
            return f"the contract declares no DATE or TIMESTAMP column {missing[0]!r} for it"
        return None

    def _history_sets(self, expose: str, column: str) -> Optional[str]:
        """Why a date of an expose is set by its history or a projection of it, or ``None``."""
        return self.exposes[expose].history.get(column)

    def _set_already(self, expose: str, column: str) -> Optional[str]:
        """Why a date of an expose is set by its history, a key or a rule of the contract, or ``None``."""
        if column in self.exposes[expose].blocked:
            return f"a key or a rule of the contract already sets {column!r}"
        return self._history_sets(expose, column)

    def lifetimes_of_tables(self) -> None:
        tables = self.profile.get("tables")
        for expose, table in (tables.items() if isinstance(tables, Mapping) else ()):
            held = self.exposes.get(str(expose))
            items = table.get("datePairs") if isinstance(table, Mapping) else None
            if held is None or not isinstance(items, list):
                continue
            self._table_lifetime(str(expose), held, items)

    def _table_lifetime(self, expose: str, held: DatedExpose, items: Sequence[Any]) -> None:
        candidates: List[Tuple[int, int, _Pair, str, str, bool, Fraction]] = []
        for index, item in enumerate(items):
            if isinstance(item, Mapping):
                candidate = self._lifetime_candidate(expose, held, index, item)
                if candidate is not None:
                    candidates.append(candidate)
        if not candidates:
            return
        best = max(candidates, key=lambda entry: (entry[0], -entry[1]))
        for candidate in candidates:
            if candidate is not best:
                self.reject(
                    candidate[2],
                    f"the table declares one lifetime, and the pair ({best[3]}, {best[4]}) "
                    "holds on more rows",
                )
        self._declare_lifetime(expose, best)

    def _lifetime_candidate(
        self, expose: str, held: DatedExpose, index: int, item: Mapping[str, Any]
    ) -> Optional[Tuple[int, int, _Pair, str, str, bool, Fraction]]:
        """One date pair of a table as a lifetime candidate, or ``None`` with the pair rejected."""
        tokens = ("tables", expose, "datePairs", str(index))
        pair = _pair(item, tokens, str(item.get("start")), str(item.get("end")), self.minimum_cell)
        assert pair is not None
        if pair.order is None:
            self.reject(pair, pair.why_unknown + ": the pair is not declared")
            return None
        order = pair.order
        forward = Fraction(order.same + order.after, order.total)
        reverse = Fraction(order.same + order.before, order.total)
        if forward < ORDER_HOLDS_AT_LEAST and reverse < ORDER_HOLDS_AT_LEAST:
            self.below(pair, max(forward, reverse))
            return None
        # The pair as published, unless only the other way round holds, or holds on more rows.
        reversed_pair = forward < ORDER_HOLDS_AT_LEAST or reverse > forward
        start, end = (pair.second, pair.first) if reversed_pair else (pair.first, pair.second)
        reason = self._usable(expose, pair.first, pair.second)
        if reason is None:
            reason = self._history_sets(expose, pair.first) or self._history_sets(
                expose, pair.second
            )
        if reason is None and {pair.first, pair.second} <= held.carried:
            reason = _BOTH_CARRIED
        if reason is None and held.kinds[pair.first] is not held.kinds[pair.second]:
            reason = "a lifetime's end is the same kind of column as its start"
        if reason is None and end in held.blocked:
            reason = f"a key or a rule of the contract already sets {end!r}"
        if reason is not None:
            self.reject(pair, reason)
            return None
        rows = order.same + (order.before if reversed_pair else order.after)
        return (rows, index, pair, start, end, reversed_pair, forward)

    def _declare_lifetime(
        self, expose: str, best: Tuple[int, int, _Pair, str, str, bool, Fraction]
    ) -> None:
        _, _, pair, start, end, reversed_pair, _ = best
        order = pair.order
        assert order is not None
        entry: Dict[str, Any] = {"emit": expose, "start": start, "end": end}
        plain = _cut(pair.histogram, reversed_pair) if pair.histogram is not None else None
        family, family_unused = self._family(expose, pair, start, end, reversed_pair)
        duration = _lifetime_duration(plain, family)
        if duration is None:
            # An end drawn from its own statistics is ordered after its start only by chance, so
            # the lifetime keeps its start, which children can still be dated after.
            self.lifetimes[expose] = {"emit": expose, "start": start}
            self.reject(pair, _NO_DURATION)
            return
        entry["duration"] = duration
        if self._max_after_window(expose, end):
            entry["endPastWindow"] = "kept"
        breaking = order.after if reversed_pair else order.before
        target = _target(Fraction(breaking), order.total)
        if target is not None:
            entry["target"] = target
        self.lifetimes[expose] = entry
        orientation = (
            f"the pair is read the other way round: {end!r} starts the lifetime"
            if reversed_pair
            else f"{start!r} starts the lifetime"
        )
        used_how: Dict[int, str] = {}
        unused_why: Dict[int, str] = dict(family_unused)
        if family is not None:
            used_how[family.index] = family.note
        how_plain = _plain_how(plain, family)
        if plain is None and pair.histogram is not None:
            unused_why[pair.histogram.index] = (
                "the histogram holds no days from 0 up, so no duration is drawn from it"
            )
        order_how = (
            f"{orientation}, and the share of rows that break its order is the target "
            f"({target or '0'}%)"
        )
        self.cited.extend(_lifetime_citations(entry, pair, family, (order_how, how_plain)))
        self.keep(pair, order_how, how_plain, used_how, unused_why)

    def _family(
        self, expose: str, pair: _Pair, start: str, end: str, reversed_pair: bool
    ) -> Tuple[Optional["_Family"], Dict[int, str]]:
        """The dwell family a pair's grouped histograms give (ADR 0025 section 3), or ``None``, and why each other is unused.

        The first ``marginalHistogram/1`` grouped by a column of the table that keeps a dwell for
        at least one group is read; each other grouped statistic is recorded unused with its reason.
        """
        statistics = pair.item.get("statistics")
        held = self.exposes[expose]
        columns = self._table_columns(expose)
        unused: Dict[int, str] = {}
        chosen: Optional[_Family] = None
        for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
            if (
                not isinstance(statistic, Mapping)
                or "groupBy" not in statistic
                or statistic.get("definition") != MARGINAL_HISTOGRAM.identifier
                or "groupByLink" in statistic
                or "per" in statistic
                or SUPERSEDED in statistic
            ):
                continue
            given = str(statistic["groupBy"])
            if chosen is not None:
                unused[index] = (
                    "a dwell family is read from one grouped statistic, and statistic "
                    f"{chosen.index} of the pair gives it"
                )
                continue
            reason = self._family_refusal(statistic, given, start, end, held)
            if reason is not None:
                unused[index] = reason
                continue
            read = self._family_states(
                pair, index, statistic, given, reversed_pair, number_grouped(statistic, columns)
            )
            if isinstance(read, str):
                unused[index] = read
            else:
                chosen = read
        return chosen, unused

    def _table_columns(self, expose: str) -> Optional[Mapping[str, Any]]:
        tables = self.profile.get("tables")
        table = tables.get(expose) if isinstance(tables, Mapping) else None
        columns = table.get("columns") if isinstance(table, Mapping) else None
        return columns if isinstance(columns, Mapping) else None

    @staticmethod
    def _family_refusal(
        statistic: Mapping[str, Any], given: str, start: str, end: str, held: DatedExpose
    ) -> Optional[str]:
        """Why a grouped histogram cannot give a dwell family, or ``None`` when it can."""
        if "noise" in statistic:
            return "its counts are released with noise, which no rule reads for a dwell"
        if given in (start, end):
            return (
                f"it is grouped by {given!r}, the lifetime's "
                f"{'start' if given == start else 'end'}, which a dwell cannot be given"
            )
        if held.columns is not None and given not in held.columns:
            return f"it is grouped by {given!r}, a column the bundle writes no column for"
        return None

    def _family_states(
        self,
        pair: _Pair,
        index: int,
        statistic: Mapping[str, Any],
        given: str,
        reversed_pair: bool,
        number: bool,
    ) -> Union["_Family", str]:
        """The states of one grouped histogram, or why it gives none."""
        try:
            edges = tuple(_whole(edge) for edge in statistic.get("bins", ()))
        except ValueError:
            return "its bins are not whole days"
        groups = statistic.get("groups")
        if len(edges) < 2 or not isinstance(groups, list):
            return "it lists no bins or no groups"
        if len(groups) > MOST_STATES:
            return f"it holds more than {MOST_STATES} groups, the most a dwell family lists"
        tokens = pair.tokens + ("statistics", str(index))
        states: Dict[str, Dict[str, Any]] = {}
        left_out: List[str] = []
        for position, group in enumerate(groups):
            if not isinstance(group, Mapping):
                continue
            left = self._add_group(
                group, position, edges, tokens, index, reversed_pair, number, states
            )
            if left is not None:
                left_out.append(left)
        if not states:
            detail = "; ".join(left_out)
            return "no group of it keeps a dwell" + (f": {detail}" if detail else "")
        note = f"the dwell of each of its {len(states)} groups, cut at day 0, for the state of {given!r}"
        if left_out:
            note += "; left out: " + "; ".join(left_out)
        return _Family(index, given, dict(sorted(states.items())), note)

    def _add_group(
        self,
        group: Mapping[str, Any],
        position: int,
        edges: Tuple[int, ...],
        tokens: Tuple[str, ...],
        index: int,
        reversed_pair: bool,
        number: bool,
        states: Dict[str, Dict[str, Any]],
    ) -> Optional[str]:
        """Add one group's dwell, cut at day 0, to *states*, or say why it is left out."""
        text = _state_text(group.get("group"), number)
        label = repr(text) if text else repr(group.get("group"))
        if group.get("status") != "F":
            return f"{label} (withheld, status {group.get('status')})"
        cells = count_cells(group, "binCounts")
        if len(cells) != len(edges) - 1:
            return f"{label} (its counts do not match the bins)"
        total = group.get("k")
        weights, _ = cell_weights(
            self.minimum_cell,
            cells,
            tokens,
            "binCounts",
            MARGINAL_HISTOGRAM.identifier,
            total=total if isinstance(total, int) and not isinstance(total, bool) else None,
            group=str(position),
        )
        member = (
            _cut(_Histogram(index, edges, tuple(weights)), reversed_pair)
            if sum(weights, Fraction(0)) > 0
            else None
        )
        if member is None:
            return f"{label} (it holds no day from 0 up)"
        if not text:
            return f"{label} (its text is empty)"
        if text in states:
            return f"{label} (the same text as another group after NFC)"
        states[text] = member
        return None

    def _max_after_window(self, expose: str, column: str) -> bool:
        """Whether the end column's published maximum lies after the window."""
        tables = self.profile.get("tables")
        table = tables.get(expose) if isinstance(tables, Mapping) else None
        columns = table.get("columns") if isinstance(table, Mapping) else None
        entry = columns.get(column) if isinstance(columns, Mapping) else None
        metrics = entry.get("metrics") if isinstance(entry, Mapping) else None
        maximum = metrics.get("max") if isinstance(metrics, Mapping) else None
        if not isinstance(maximum, Mapping) or maximum.get("status") != "F":
            return False
        try:
            return int(Decimal(str(maximum.get("value")))) > self.window_end
        except ArithmeticError:
            return False

    # -- rates ---------------------------------------------------------------

    @staticmethod
    def _shaped(
        lifetimes: Sequence[Mapping[str, Any]], within: Sequence[Mapping[str, Any]]
    ) -> List[Tuple[str, str]]:
        """Each (emit, column) a ``within`` entry or a duration sets, in the order written."""
        found: List[Tuple[str, str]] = []
        for life in lifetimes:
            if life.get("duration") is not None:
                _add_shaped(found, life["emit"], life.get("end"))
        for entry in within:
            if entry.get("date") is not None:
                _add_shaped(found, entry["emit"], entry["date"])
            elif entry.get("lifetime") is True:
                for life in lifetimes:
                    if life["emit"] == entry["emit"]:
                        _add_shaped(found, life["emit"], life["start"])
        return found

    def rates_entries(
        self, lifetimes: Sequence[Mapping[str, Any]], within: Sequence[Mapping[str, Any]]
    ) -> List[Dict[str, Any]]:
        """The ``rates`` entries the published calendars give, and the note of each histogram."""
        shaped = self._shaped(lifetimes, within)
        occurrences = (
            _Occurrences(self.window_start, self.window_end)
            if self.window_start is not None and self.window_start <= self.window_end
            else None
        )
        tables = self.profile.get("tables")
        entries: List[Dict[str, Any]] = []
        for expose, table in (tables.items() if isinstance(tables, Mapping) else ()):
            entries.extend(self._table_rates(str(expose), table, shaped, occurrences))
        order = {key: position for position, key in enumerate(shaped)}
        entries.sort(key=lambda entry: order[(entry["emit"], entry["column"])])
        return entries

    def _table_rates(
        self,
        expose: str,
        table: Any,
        shaped: Sequence[Tuple[str, str]],
        occurrences: Optional[_Occurrences],
    ) -> List[Dict[str, Any]]:
        """The ``rates`` entries of one table's calendars, in the order they are published."""
        held = self.exposes.get(expose)
        items = table.get("calendar") if isinstance(table, Mapping) else None
        if held is None or not isinstance(items, list):
            return []
        entries: List[Dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                continue
            entry = self._rates_entry(
                expose,
                index,
                item,
                held,
                (expose, str(item.get("column"))) in shaped,
                occurrences,
            )
            if entry is not None:
                entries.append(entry)
        return entries

    @staticmethod
    def _rates_factor(
        item: Mapping[str, Any],
        key: str,
        column: str,
        held: DatedExpose,
        shaped: bool,
        occurrences: Optional[_Occurrences],
    ) -> Any:
        """The factor of one calendar histogram, or the reason it gives none (a string)."""
        if not shaped:
            return _RATES_NO_COLUMN
        if occurrences is None:
            return "the window the bundle writes is not known, so no level can be counted in it"
        if key == "hourOfWeek" and held.kinds.get(column) is LogicalKind.DATE:
            return _RATES_NO_HOUR
        counts = _calendar_counts(item[key], len(CALENDAR_LEVELS[key]))
        if isinstance(counts, str):
            return counts
        return _factor_of(counts, occurrences.of(key))

    def _rates_entry(
        self,
        expose: str,
        index: int,
        item: Mapping[str, Any],
        held: DatedExpose,
        shaped: bool,
        occurrences: Optional[_Occurrences],
    ) -> Optional[Dict[str, Any]]:
        column = str(item.get("column"))
        base = ("tables", expose, "calendar", str(index))
        entry: Dict[str, Any] = {"emit": expose, "column": column}
        declined = self.declined_rates.get((expose, column))
        factors = {
            key: (
                declined
                if declined is not None
                else self._rates_factor(item, key, column, held, shaped, occurrences)
            )
            for key in _RATES_KEYS
            if key in item
        }
        for key, factor in factors.items():
            tokens = base + (key,)
            if key == "dayOfWeek" and not isinstance(factors.get("hourOfWeek", ""), str):
                # Each rate multiplies the others, so the weekday would count twice.
                self.unused.append((tokens, _RATES_WEEKDAY_TWICE))
                continue
            if isinstance(factor, str):
                self.unused.append((tokens, factor))
                continue
            entry[key] = factor
            how = (
                f"the weight of each {key} level of {column!r}: its count over the times the "
                "level occurs in the window, scaled so the largest is 1; a suppressed cell takes "
                "the smallest positive weight published, and a level the window does not hold "
                "takes 1"
            )
            self.used.append((tokens, how))
            self.cited.append((entry, (key,), tokens, how))
        return entry if len(entry) > 2 else None

    # -- orders the contract declares ---------------------------------------

    def lifetimes_of_orders(self) -> None:
        """A lifetime for each table whose contract orders two of its dates and whose profile gives no pair of them.

        The profile's date pair, when it publishes one for the two columns, is the statistic and
        wins: this reads nothing of the profile. The days between the dates are an assumption. An
        order that cannot be written is listed in ``declined_orders`` with the reason.
        """
        for expose, held in self.exposes.items():
            written = False
            for order in held.orders:
                if self._published_pair(expose, order):
                    continue
                subject = f"{expose}.{order.rule}"
                if written or expose in self.lifetimes:
                    self.declined_orders.append(
                        (subject, "the table declares one lifetime, and another one already does")
                    )
                    continue
                entry = self._order_lifetime(expose, held, order)
                if isinstance(entry, str):
                    self.declined_orders.append(
                        (
                            subject,
                            f"the check orders {order.start!r} and {order.end!r}, and no lifetime "
                            f"is written for it: {entry}",
                        )
                    )
                else:
                    self.lifetimes[expose] = entry
                    written = True

    def _published_pair(self, expose: str, order: DateOrder) -> bool:
        tables = self.profile.get("tables")
        table = tables.get(expose) if isinstance(tables, Mapping) else None
        items = table.get("datePairs") if isinstance(table, Mapping) else None
        names = {order.start, order.end}
        return any(
            isinstance(item, Mapping) and {str(item.get("start")), str(item.get("end"))} == names
            for item in (items if isinstance(items, list) else ())
        )

    def _order_lifetime(
        self, expose: str, held: DatedExpose, order: DateOrder
    ) -> Union[Dict[str, Any], str]:
        """The lifetime that keeps *order*, or why the bundle cannot hold it."""
        start, end = order.start, order.end
        refused = self._usable(expose, start, end)
        if refused is None:
            refused = self._set_already(expose, start) or self._set_already(expose, end)
        if refused is None and held.kinds[start] is not held.kinds[end]:
            refused = "a lifetime's end is the same kind of column as its start"
        shortest = 1 if order.strict else 0
        if refused is None and self.window_start is None:
            refused = "the run's window is not known"
        longest = (
            0 if self.window_start is None else (self.window_end - self.window_start) // _DAY_MS
        )
        if refused is None and longest <= shortest:
            refused = f"the run's window holds {longest} days, too few to draw a gap in"
        if refused is not None:
            return refused
        entry: Dict[str, Any] = {
            "emit": expose,
            "start": start,
            "end": end,
            "duration": {"bins": [str(shortest), str(longest)], "binWeights": ["1"]},
        }
        nullable = end not in held.required
        open_end = nullable and order.null_end_holds
        if nullable and not open_end:
            # A NULL end would fail the check: the end keeps a date past the window.
            entry["endPastWindow"] = "kept"
        if not open_end and not self._fits(held, start, end, shortest, longest):
            return (
                f"the declared range of {end!r} cannot be shown to hold every end the gap can "
                f"draw; widen it, or let the check hold on a NULL {end!r}"
            )
        self.cited.append(
            (
                entry,
                ("duration",),
                None,
                f"the days from {start!r} to {end!r}, which the contract's check "
                f"{order.rule!r} orders and the profile gives no pair for: drawn evenly from "
                f"{shortest} to {longest} days, the most the run's window allows",
            )
        )
        return entry

    @staticmethod
    def _fits(held: DatedExpose, start: str, end: str, shortest: int, longest: int) -> bool:
        """Whether a kept end can always be drawn inside its declared range (``FLX-6204``)."""
        reach, span = held.bounds.get(start), held.bounds.get(end)
        if reach is None or span is None:
            return False
        return reach[1] + shortest * _DAY_MS <= span[1] and reach[0] + longest * _DAY_MS >= span[0]

    # -- relationships -------------------------------------------------------

    def _relationships(self) -> List[Tuple[int, Any, Any, List[_Pair], Optional[str]]]:
        """Each profile relationship that has date pairs: its contract relationship and its pairs."""
        found = self.profile.get("relationships")
        listed: List[Tuple[int, Any, Any, List[_Pair], Optional[str]]] = []
        for index, entry in enumerate(found if isinstance(found, list) else ()):
            sides = self._dated_sides(entry)
            if sides is None:
                continue
            dates, parent, child = sides
            parent_table, child_table = str(parent.get("table")), str(child.get("table"))
            relationship = self._contract_relationship(parent, child)
            pairs = self._date_pairs(index, dates)
            reason = None
            if relationship is None:
                reason = "the contract declares no to-one relationship of these two exposes"
            elif parent_table == child_table:
                reason = "a table's rows within the lifetime of its own rows are not declared"
            else:
                reason = self._key_set_by_projection(child_table, relationship)
            listed.append((index, parent_table, (child_table, relationship), pairs, reason))
        return listed

    def _key_set_by_projection(self, child_table: str, relationship: Any) -> Optional[str]:
        """Why a relationship cannot be read, when a projection entry sets a foreign key of its child, or ``None``."""
        held = self.exposes[child_table].projected_keys
        for column in relationship.from_columns:
            if column in held:
                return held[column]
        return None

    def _dated_sides(
        self, entry: Any
    ) -> Optional[Tuple[List[Any], Mapping[str, Any], Mapping[str, Any]]]:
        """A relationship's date pairs and its two sides, when it has pairs and both tables are exposes."""
        dates = entry.get("dates") if isinstance(entry, Mapping) else None
        if not isinstance(dates, list) or not dates:
            return None
        parent, child = entry.get("parent"), entry.get("child")
        if not isinstance(parent, Mapping) or not isinstance(child, Mapping):
            return None
        if (
            str(parent.get("table")) not in self.exposes
            or str(child.get("table")) not in self.exposes
        ):
            return None
        return dates, parent, child

    def _date_pairs(self, index: int, dates: Sequence[Any]) -> List[_Pair]:
        """The date pairs a profile relationship publishes, in the order published."""
        pairs: List[_Pair] = []
        for position, item in enumerate(dates):
            if not isinstance(item, Mapping):
                continue
            tokens = ("relationships", str(index), "dates", str(position))
            read = _pair(
                item, tokens, str(item.get("parent")), str(item.get("child")), self.minimum_cell
            )
            assert read is not None
            pairs.append(read)
        return pairs

    def _contract_relationship(self, parent: Mapping[str, Any], child: Mapping[str, Any]) -> Any:
        if self.keys is None:
            return None
        try:
            relationships = self.keys.expose(str(child.get("table"))).relationships
        except (KeyError, LookupError):
            return None
        wanted_parent = tuple(str(name) for name in parent.get("columns") or ())
        wanted_child = tuple(str(name) for name in child.get("columns") or ())
        for relationship in relationships:
            if (
                relationship.target == str(parent.get("table"))
                and tuple(relationship.to_columns) == wanted_parent
                and tuple(relationship.from_columns) == wanted_child
                and relationship.cardinality in ("many_to_one", "one_to_one")
            ):
                return relationship
        return None

    def start_only_lifetimes(self) -> None:
        """A start for a parent's date that a relationship orders a child after, when it has no lifetime."""
        best: Dict[str, Tuple[int, str]] = {}
        for _, parent_table, (child_table, _relationship), pairs, reason in self._relationships():
            if reason is not None or parent_table in self.lifetimes:
                continue
            self._offer_starts(best, parent_table, child_table, pairs)
        for parent_table, (_, start) in best.items():
            self.lifetimes[parent_table] = {"emit": parent_table, "start": start}
            self.start_only.add(parent_table)

    def _offer_starts(
        self,
        best: Dict[str, Tuple[int, str]],
        parent_table: str,
        child_table: str,
        pairs: Sequence[_Pair],
    ) -> None:
        """Record in *best* the start of a parent's date that most rows of a child are ordered after."""
        for pair in pairs:
            order = pair.order
            if order is None or not order.holds(order.same + order.after):
                continue
            if self._usable(parent_table, pair.first) or self._usable(child_table, pair.second):
                continue
            if self._history_sets(parent_table, pair.first) is not None:
                continue
            if self._set_already(child_table, pair.second) is not None:
                continue
            rows = order.same + order.after
            if parent_table not in best or rows > best[parent_table][0]:
                best[parent_table] = (rows, pair.first)

    def within_entries(self) -> List[Dict[str, Any]]:
        chosen, out_of_order, ends = self._choose_within()
        for key, pair in out_of_order:
            self.reject(
                pair,
                f"a second relationship for {key[1]!r} of {key[0]!r}: one entry, the one that "
                "holds on more rows, declares it",
            )
        entries: List[Dict[str, Any]] = []
        read_ends = set()
        for _, entry, pair, end_pair in chosen.values():
            entries.append(entry)
            self._keep_within(entry, pair, end_pair)
            if end_pair is not None:
                read_ends.add(id(end_pair))
        for pair in ends:
            if id(pair) not in read_ends:
                self._reject_end(pair)
        # A start a pair offered dates nothing when no within entry reads it: the pair is unused
        # (the entry it would date was refused, with the reason), so the start goes with it.
        read = {self.origin[id(entry)][2] for entry in entries}
        for parent_table in sorted(self.start_only - read):
            del self.lifetimes[parent_table]
        self._declare_exposures(entries)
        return entries

    def _reject_end(self, pair: _Pair) -> None:
        """Reject a pair that ends the parent's lifetime and that no entry read."""
        self.reject(
            pair,
            (
                pair.why_unknown
                if pair.order is None
                else "the parent's lifetime ends here, and its order is read only to add to the "
                "target of an entry for the same date of the child, which none declares"
            ),
        )

    def _choose_within(
        self,
    ) -> Tuple[
        Dict[Tuple[str, str], Tuple[int, Dict[str, Any], _Pair, Optional[_Pair]]],
        List[Tuple[Tuple[str, str], _Pair]],
        List[_Pair],
    ]:
        """The within entry chosen for each (emit, date), the pairs it beat, and the end pairs."""
        chosen: Dict[Tuple[str, str], Tuple[int, Dict[str, Any], _Pair, Optional[_Pair]]] = {}
        out_of_order: List[Tuple[Tuple[str, str], _Pair]] = []
        ends: List[_Pair] = []
        for (
            index,
            parent_table,
            (child_table, relationship),
            pairs,
            reason,
        ) in self._relationships():
            by_columns = {(pair.first, pair.second): pair for pair in pairs}
            for pair in pairs:
                if reason is not None:
                    self.reject(pair, reason)
                    continue
                if self._ends_lifetime(parent_table, pair):
                    ends.append(pair)
                    continue
                made = self._within_for(parent_table, child_table, relationship, pair, by_columns)
                if made is None:
                    continue
                self.origin[id(made[2])] = (made[2], index, parent_table)
                key, rows, entry, end_pair = made
                self._prefer(chosen, out_of_order, key, (rows, entry, pair, end_pair))
        return chosen, out_of_order, ends

    @staticmethod
    def _prefer(
        chosen: Dict[Tuple[str, str], Tuple[int, Dict[str, Any], _Pair, Optional[_Pair]]],
        out_of_order: List[Tuple[Tuple[str, str], _Pair]],
        key: Tuple[str, str],
        made: Tuple[int, Dict[str, Any], _Pair, Optional[_Pair]],
    ) -> None:
        """Keep the entry that holds on more rows for *key*, and list the pair of the other."""
        if key in chosen and chosen[key][0] >= made[0]:
            out_of_order.append((key, made[2]))
            return
        if key in chosen:
            out_of_order.append((key, chosen[key][2]))
        chosen[key] = made

    # -- exposure (ADR 0025 sections 4 and 7) ---------------------------------

    def _published_exposures(self, index: int) -> List[Mapping[str, Any]]:
        relationships = self.profile.get("relationships")
        entry = relationships[index] if isinstance(relationships, list) else None
        found = entry.get("exposure") if isinstance(entry, Mapping) else None
        return [
            item for item in (found if isinstance(found, list) else ()) if isinstance(item, Mapping)
        ]

    @staticmethod
    def _published_count(metric: Any) -> Optional[Decimal]:
        """A published ``count/1`` metric's value, or ``None`` when it is withheld."""
        if not isinstance(metric, Mapping) or metric.get("status") != "F":
            return None
        try:
            return Decimal(str(metric.get("value")))
        except ArithmeticError:
            return None

    def _exposure_refusal(self, item: Mapping[str, Any], life: Mapping[str, Any]) -> Optional[str]:
        """Why a published exposure cannot be declared on a within entry, or ``None`` when it can."""
        if item.get("start") != life["start"] or item.get("end") != life.get("end"):
            return (
                "its lifetime columns are not the lifetime the bundle declares for the parent "
                f"({life['start']!r} to {life.get('end')!r})"
            )
        children = self._published_count(item.get("children"))
        active = self._published_count(item.get("activeDays"))
        if children is None or active is None:
            return "a count of it is withheld, so the children per active day is not known"
        if active <= 0:
            return "it counts no active day, so the children per active day is not known"
        return None

    def _declare_exposures(self, entries: Sequence[Dict[str, Any]]) -> None:
        """Mark ``exposure: true`` on the first written entry over a relationship that publishes one that fits.

        An exposure fits when its start and end are the start and end of the parent lifetime the
        bundle writes and both its counts are published, with an active day. A relationship of an
        emit carries one exposure entry, so a second entry over it never does.
        """
        written: Dict[int, List[Dict[str, Any]]] = {}
        for entry in entries:
            held = self.origin.get(id(entry))
            if held is not None and held[0] is entry:
                written.setdefault(held[1], []).append(entry)
        relationships = self.profile.get("relationships")
        taken: set[Tuple[str, str]] = set()
        for index in range(len(relationships) if isinstance(relationships, list) else 0):
            self._declare_exposure_of(index, written.get(index, []), taken)

    def _declare_exposure_of(
        self, index: int, over: Sequence[Dict[str, Any]], taken: set[Tuple[str, str]]
    ) -> None:
        """Declare the exposure of one relationship on its first written entry, or say why not."""
        items = self._published_exposures(index)
        if not items:
            return
        base = ("relationships", str(index), "exposure")
        life = self.lifetimes.get(self.origin[id(over[0])][2]) if over else None
        first = over[0] if over else None
        declared = False
        for position, item in enumerate(items):
            spots = [base + (str(position), key) for key in ("children", "activeDays")]
            why = self._exposure_why(item, life, first, declared, taken)
            if why:
                self.unused.extend((tokens, why) for tokens in spots)
                continue
            assert first is not None
            first["exposure"] = True
            taken.add((str(first["emit"]), str(first["relationship"])))
            declared = True
            how = (
                "the children per active day of the parent's lifetime, which a run is "
                "checked against, and the exposure the entry draws its children by"
            )
            self.used.extend((tokens, how) for tokens in spots)

    def _exposure_why(
        self,
        item: Mapping[str, Any],
        life: Optional[Mapping[str, Any]],
        first: Optional[Dict[str, Any]],
        declared: bool,
        taken: set[Tuple[str, str]],
    ) -> str:
        """Why a published exposure is not declared on the first written entry, or ``""`` when it is."""
        if life is None or first is None:
            return (
                "no within entry over this relationship is written, so no entry "
                "carries its exposure"
            )
        why = self._exposure_refusal(item, life) or ""
        if not why:
            why = self.declined_exposures.get((str(first["emit"]), str(first["relationship"])), "")
        if not why and (declared or (str(first["emit"]), str(first["relationship"])) in taken):
            why = (
                "a second entry over the relationship: one entry of an emit "
                "carries the exposure of a relationship"
            )
        return why

    def _ends_lifetime(self, parent_table: str, pair: _Pair) -> bool:
        life = self.lifetimes.get(parent_table)
        return life is not None and life.get("end") == pair.first

    def _within_for(
        self,
        parent_table: str,
        child_table: str,
        relationship: Any,
        pair: _Pair,
        by_columns: Mapping[Tuple[str, str], _Pair],
    ) -> Optional[Tuple[Tuple[str, str], int, Dict[str, Any], Optional[_Pair]]]:
        order = pair.order
        if order is None:
            self.reject(pair, pair.why_unknown + ": the entry is not declared")
            return None
        parent_life = self.lifetimes.get(parent_table)
        if parent_life is None or parent_life["start"] != pair.first:
            self.reject(
                pair,
                self._history_sets(parent_table, pair.first)
                or self._set_already(child_table, pair.second)
                or f"{pair.first!r} does not start a lifetime the bundle declares for "
                f"{parent_table!r}",
            )
            return None
        after = order.same + order.after
        if not order.holds(after):
            self.below(pair, Fraction(after, order.total))
            return None
        child_life = self.lifetimes.get(child_table)
        reason = self._child_refusal(child_table, pair.second, child_life)
        if reason is not None:
            self.reject(pair, reason)
            return None
        entry: Dict[str, Any] = {"emit": child_table, "relationship": relationship.name}
        period = (
            child_life is not None
            and child_life["start"] == pair.second
            and child_life.get("end") is not None
        )
        if period:
            entry["lifetime"] = True
        else:
            entry["date"] = pair.second
        if pair.histogram is not None:
            entry["offset"] = _histogram_member(
                pair.histogram.edges, pair.histogram.weights, upper=False
            )
        broken, end_pair = self._broken_share(order, parent_life, pair, by_columns)
        target = _target(broken, 1)
        if target is not None and Fraction(target) >= 100:
            self.reject(
                pair,
                f"the share that breaks it, {_percent(broken)}, is not below 100%, so it is "
                "no target",
            )
            return None
        if target is not None:
            entry["target"] = target
        key = (child_table, "lifetime" if period else pair.second)
        return key, after, entry, end_pair

    def _child_refusal(
        self, child_table: str, column: str, child_life: Optional[Mapping[str, Any]]
    ) -> Optional[str]:
        """Why a within entry cannot date *column* of the child, or ``None`` when it can."""
        reason = self._usable(child_table, column)
        if reason is None:
            reason = self.declined_within.get((child_table, column))
        if reason is None:
            reason = self._set_already(child_table, column)
        if reason is None and child_life is not None and child_life.get("end") == column:
            reason = f"{column!r} is the end of the lifetime of {child_table!r}"
        return reason

    @staticmethod
    def _broken_share(
        order: _Order,
        parent_life: Mapping[str, Any],
        pair: _Pair,
        by_columns: Mapping[Tuple[str, str], _Pair],
    ) -> Tuple[Fraction, Optional[_Pair]]:
        """The share of rows that break a within entry, and the pair of the parent's end it adds."""
        broken = Fraction(order.before) / order.total
        if parent_life.get("end") is None:
            return broken, None
        end_pair = by_columns.get((str(parent_life["end"]), pair.second))
        if end_pair is None or end_pair.order is None:
            return broken, None
        return broken + Fraction(end_pair.order.after, end_pair.order.total), end_pair

    def _keep_within(self, entry: Dict[str, Any], pair: _Pair, end_pair: Optional[_Pair]) -> None:
        offset = "offset" in entry
        order_how = (
            "the share of rows before the parent's start, with the share after its end, is the "
            "target of the entry"
        )
        offset_how = "the offset of the date from the parent's start"
        if "target" in entry:
            self.cited.append((entry, ("target",), pair.tokens + ("order",), order_how))
        if offset and pair.histogram is not None:
            tokens = pair.tokens + ("statistics", str(pair.histogram.index))
            self.cited.append((entry, ("offset",), tokens, offset_how))
        self.keep(pair, order_how, offset_how if offset else None)
        if end_pair is not None and end_pair.order is not None:
            self.used.append(
                (
                    end_pair.tokens + ("order",),
                    "its share after the parent's end is added to the target of the entry",
                )
            )
            statistics = end_pair.item.get("statistics")
            for index, _ in enumerate(statistics if isinstance(statistics, list) else ()):
                self.unused.append(
                    (
                        end_pair.tokens + ("statistics", str(index)),
                        "only the order of this pair is read, for the target of an entry",
                    )
                )


# -- seasonal rates (ADR 0025 sections 2 and 7) -----------------------------------------------------

_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000

#: The calendar histograms in the order a ``rates`` entry lists their factors.
_RATES_KEYS = ("month", "dayOfWeek", "hourOfWeek")

_RATES_NO_COLUMN = (
    "rates shape only a date a within entry or a duration sets, and none of the bundle's does "
    "for this column"
)
_RATES_NO_HOUR = "a DATE has no hour, so an hour of the week shapes nothing of it"
_RATES_WEEKDAY_TWICE = "hourOfWeek already carries the weekday"
_RATES_NOISE = "its counts are released with noise, which no rule reads for a rate"


def _residue_count(low: int, high: int, residue: int, modulus: int) -> int:
    """How many integers ``n`` in ``low..high`` have ``n % modulus == residue``."""
    if high < low:
        return 0
    return (high - residue) // modulus - (low - 1 - residue) // modulus


def _days_from_civil(year: int, month: int) -> int:
    """The epoch day of the first of *month* in *year*, proleptic Gregorian, on integers.

    No ``date`` is built, so a year past 9999 (the month after December 9999) is as good as any other.
    """
    shifted_year = year - (month <= 2)
    era = shifted_year // 400
    year_of_era = shifted_year - era * 400
    day_of_year = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5
    day_of_era = year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year
    return era * 146_097 + day_of_era - 719_468


def _year_month_of(day: int) -> Tuple[int, int]:
    """The ``(year, month)`` of epoch day *day*, proleptic Gregorian, on integers."""
    shifted = day + 719_468
    era = shifted // 146_097
    day_of_era = shifted - era * 146_097
    year_of_era = (
        day_of_era - day_of_era // 1_460 + day_of_era // 36_524 - day_of_era // 146_096
    ) // 365
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    shifted_month = (5 * day_of_year + 2) // 153
    month = shifted_month + 3 if shifted_month < 10 else shifted_month - 9
    return year_of_era + era * 400 + (month <= 2), month


def _window_occurrences(start_ms: int, end_ms: int) -> Dict[str, List[int]]:
    """How many times each level of each calendar key occurs inside a window.

    The window holds every UTC day and every UTC hour it touches, so its first and last day and
    hour count whole. Days of the month are counted month by month from the days each month
    holds; days of the week and hours of the week from the epoch's weekday, a Thursday. All of it
    is integer arithmetic on epoch days and hours, so a window that ends in December 9999 counts
    like any other.
    """
    first_day, last_day = start_ms // _DAY_MS, end_ms // _DAY_MS
    first_hour, last_hour = start_ms // _HOUR_MS, end_ms // _HOUR_MS
    months = [0] * 12
    if first_day <= last_day:
        year, month = _year_month_of(first_day)
        last_year, last_month = _year_month_of(last_day)
        while (year, month) <= (last_year, last_month):
            begin = _days_from_civil(year, month)
            following_year, following_month = (year + 1, 1) if month == 12 else (year, month + 1)
            following = _days_from_civil(following_year, following_month)
            months[month - 1] += min(last_day, following - 1) - max(first_day, begin) + 1
            year, month = following_year, following_month
    # Epoch day 0 is a Thursday: Monday-first index 3, so hour 0 is hour of the week 72.
    weekdays = [_residue_count(first_day, last_day, (day - 3) % 7, 7) for day in range(7)]
    hours = [_residue_count(first_hour, last_hour, (level - 72) % 168, 168) for level in range(168)]
    return {"month": months, "dayOfWeek": weekdays, "hourOfWeek": hours}


class _Occurrences:
    """The window's level occurrences, counted the first time a calendar histogram needs them."""

    def __init__(self, start_ms: int, end_ms: int) -> None:
        self._window = (start_ms, end_ms)
        self._held: Optional[Dict[str, List[int]]] = None

    def of(self, key: str) -> List[int]:
        """The occurrences of each level of calendar key *key*."""
        if self._held is None:
            self._held = _window_occurrences(*self._window)
        return self._held[key]


def _six_digits(value: Fraction) -> str:
    """*value* as a decimal string of six significant digits, with no exponent and no trailing zero."""
    with localcontext() as context:
        context.prec = 6
        context.rounding = ROUND_HALF_EVEN
        rounded = Decimal(value.numerator) / Decimal(value.denominator)
    text = format(rounded, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _calendar_counts(statistic: Any, size: int) -> Union[List[Optional[int]], str]:
    """The counts of one published calendar histogram (``None`` for a suppressed cell), or why it is not read."""
    if not isinstance(statistic, Mapping):
        return "the profile publishes no histogram here"
    if "noise" in statistic:
        return _RATES_NOISE
    groups = statistic.get("groups")
    group = groups[0] if isinstance(groups, list) and len(groups) == 1 else None
    if not isinstance(group, Mapping) or statistic.get("definition") != LEVEL_HISTOGRAM.identifier:
        return "it is not one ungrouped level histogram"
    if group.get("status") != "F":
        return f"it is withheld (status {group.get('status')})"
    cells = count_cells(group, "levelCounts")
    if len(cells) != size:
        return "its counts do not match the levels of its key"
    return [cell.count for cell in cells]


def _factor_of(
    counts: Sequence[Optional[int]], occurrences: Sequence[int]
) -> Union[List[str], str]:
    """The factor a calendar histogram gives, or why it gives none.

    A level's weight is its count over the times it occurs inside the window, exact. A suppressed
    cell is a small count by definition, so a suppressed level that occurs in the window takes the
    smallest positive weight the histogram publishes, before scaling. The weights are scaled so the
    largest is 1 and spelled to six significant digits; a level that does not occur in the window
    never draws, so it takes the weight 1.
    """
    weights: List[Optional[Fraction]] = [
        Fraction(count, held) if count is not None and held > 0 else None
        for count, held in zip(counts, occurrences, strict=True)
    ]
    usable = [weight for weight in weights if weight is not None]
    if not usable:
        return "no level has both a published count and a place in the window"
    largest = max(usable)
    if largest <= 0:
        return "every level that has a count and a place in the window counts none"
    smallest = min(weight for weight in usable if weight > 0)
    spelled = [
        "1" if held <= 0 else _six_digits((smallest if weight is None else weight) / largest)
        for weight, held in zip(weights, occurrences, strict=True)
    ]
    if any(len(text) - text.count(".") > MOST_DIGITS for text in spelled):
        return f"a weight needs more than {MOST_DIGITS} digits at six significant digits"
    return spelled


def declare_dates(
    profile: Mapping[str, Any],
    exposes: Mapping[str, DatedExpose],
    keys: Optional[ContractKeys],
    *,
    window_end: int,
    minimum_cell: int,
    window_start: Optional[int] = None,
    declined_exposures: Optional[Mapping[Tuple[str, str], str]] = None,
    declined_within: Optional[Mapping[Tuple[str, str], str]] = None,
    declined_rates: Optional[Mapping[Tuple[str, str], str]] = None,
) -> DateDeclarations:
    """The ``lifetimes``, ``within`` and ``rates`` entries a profile supports, with what each came to.

    *exposes* are the written, generated exposes. Nothing is declared of a profile with no
    ``datePairs``, no relationship ``dates`` and no ``calendar``. A pair or a calendar histogram
    that is not used is listed in ``unused`` with its reason; one of a table the bundle does not
    write is left to the ledger's reason for the table. An exposure of *declined_exposures* (per
    ``(emit, relationship)``, with its reason) is not declared and is listed as unused with that
    reason. A date of *declined_within* (per ``(emit, column)``, with its reason) is not dated by a
    ``within`` entry, and the pairs that would date it are listed as unused with that reason. A
    column of *declined_rates* (per ``(emit, column)``, with its reason) has no ``rates`` entry, and
    each calendar histogram the profile publishes for it is listed as unused with that reason, so
    its calendar line is printed and does not gate. A ``rates`` entry counts the levels the
    window ``window_start`` to ``window_end`` (milliseconds since the epoch, both inclusive of
    their day or hour) holds, so none is written when *window_start* is not given.
    """
    declarer = _Declarer(
        profile,
        exposes,
        keys,
        window_end=window_end,
        minimum_cell=minimum_cell,
        window_start=window_start,
        declined_exposures=declined_exposures,
        declined_within=declined_within,
        declined_rates=declined_rates,
    )
    declarer.lifetimes_of_tables()
    declarer.lifetimes_of_orders()
    declarer.start_only_lifetimes()
    within = declarer.within_entries()
    lifetimes = list(declarer.lifetimes.values())
    rates = declarer.rates_entries(lifetimes, within)
    return DateDeclarations(
        lifetimes=lifetimes,
        within=within,
        used=declarer.used,
        unused=declarer.unused,
        rates=rates,
        cited=declarer.cited,
        declined_orders=declarer.declined_orders,
    )
