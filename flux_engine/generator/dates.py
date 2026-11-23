# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The arithmetic of a lifetime's end: a duration drawn on integer thresholds (ADR 0024 section 4).

Everything here is integers and exact rationals, so a draw is the same in every implementation.
A date column holds epoch days (``DATE``) or epoch milliseconds (``TIMESTAMP``); a *unit* is one
day in those units, ``1`` or ``86 400 000``.

**The window** is ``spec.timeBounds`` in the column's units. A ``DATE`` window runs from the day
that holds the start to the day that holds the end.

**The duration** is a histogram over whole days from the row's start to its end, as EXPR-SPEC-2
section 6 writes a ``distribution``. Its bins are edges, ``n + 1`` of them for ``n`` weights.
With the default ``closed: lower`` a bin holds the days from its lower edge up to the day before
its upper edge, the last bin also its upper edge; with ``upper`` a bin holds the days after its
lower edge up to its upper edge, the first bin also its lower edge. A bin stands for the values of
the end column that fall on those days, counted at the column's grain: one value per day for a
``DATE`` end, a millisecond each for a ``TIMESTAMP`` end, whose day offset is the floor of
``(end - start) / day``. The optional ``open`` weight is one more slot after the bins: a row that
draws it has no end.

With ``W_i`` the weights in slot order and ``W`` their sum, the thresholds are
``T_i = floor(2^53 * (W_1 + ... + W_i) / W)``. One ``uniform53``, *u*, picks the first slot ``i``
with ``u < T_i``, and inside a bin of ``n`` values, taken in ascending order, the value at index
``(u - T_(i-1)) * n // (T_i - T_(i-1))``.

**Past the window** an end after the window's end is NULL under ``endPastWindow: open``, and as
drawn under ``kept``.

**A date within a linked row's lifetime** (ADR 0024 section 5) is drawn in :func:`interval_of`'s
``J``, a parent's closed lifetime cut at the window, from the cut table of an ``offset`` histogram
or, where the row has none or the table is empty, uniformly. A lifetime is ``(start, end)`` in the
column's units, ``end`` ``None`` when it is open. A DATE start bounds a TIMESTAMP column from its
midnight, a DATE end up to the last millisecond of its day, and a TIMESTAMP bounds a DATE column by
the day that holds it (:func:`bound_in_units`). A bin of an offset keeps the part of its weight that
its values inside ``J`` hold, exactly (EXPR-SPEC-2 section 6), and the draw is one ``uniform53`` on
its thresholds; the fallback is one ``bounded`` over the values of ``J``.

**A period** (``lifetime: true``) draws its start as a date within the parent's lifetime, and its end
as the lifetime draws it without the window rule, cut at the end of each parent that has ended
(:func:`cut_end`), and under the window rule only where no parent has ended. The cut draws nothing.

**The column's own range** (ADR 0024 sections 4 and 5). A date column whose declared statistics bound it
has a range ``R`` (:func:`declared_bounds`). A date within a linked lifetime is drawn in ``J`` cut to ``R``
(:func:`fit_in_bounds`), and a row where the two do not meet keeps the value its own cell drew and counts as
``infeasible``. A duration's end is cut so that the start plus the duration stays inside the end column's
range (``Duration.end_range``), by the cut arithmetic of :func:`cut_table` for a histogram and exactly for
an exponential (:mod:`flux_engine.generator.dwell`). A column with no ``R`` is drawn as before.

**Violating rows** (ADR 0024 section 7). On a covered row whose own value is not NULL, position 0 of
the column's dates stream decides whether the row holds: ``bernoulli(1 - t / 100)`` for a ``target``
*t* above zero (:func:`holding_probability`, exact), and nothing is drawn for a target of 0 or none.
A row that does not hold takes the first of 16 candidates, each drawn from the column's own
statistics at position ``257 + j * m``, that lies outside its fact's interval and inside every other
entry's lifetime (:func:`inside`); the counts of what each row came to are kept in a
:class:`DatesTally`.

**Seasonal rates** (ADR 0025 section 2) thin the value a step draws at position 1 of the column's dates
stream, and take the positions ``1`` to ``256`` of that stream (:mod:`flux_engine.generator.rates`); a
violating row's candidates are never thinned.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Sequence, Tuple

from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL, ColumnStatistics
from flux_engine.seam.dates import Histogram

if TYPE_CHECKING:  # dwell.py imports this module, so the family is named for the checker only
    from flux_engine.generator.dwell import DwellFamily

__all__ = [
    "DAY_MS",
    "KIND_DATE",
    "KIND_TIMESTAMP",
    "UNIFORM53",
    "CANDIDATES",
    "CANDIDATE_POSITION",
    "HOLD_POSITION",
    "TIMESTAMP_MAX_MS",
    "TIMESTAMP_MIN_MS",
    "OUTCOMES",
    "RATES_OUTCOMES",
    "CutTable",
    "DatesTally",
    "Duration",
    "DurationTable",
    "Lifetime",
    "Period",
    "WithinLevel",
    "bound_in_units",
    "declared_bounds",
    "fit_in_bounds",
    "cut_table",
    "cut_end",
    "draw_within",
    "holding_probability",
    "inside",
    "interval_of",
    "intersect",
    "lifetime_in_units",
    "lifetimes_meet",
    "offset_table",
    "offsets_reach_forward",
    "past_window",
    "positive_target",
    "date_of_days",
    "days_of_date",
    "duration_table",
    "unit_of",
    "window_in_units",
]

#: One day in milliseconds, the unit of a ``TIMESTAMP`` column.
DAY_MS = 86_400_000
#: ``2**53``: the thresholds of a draw run up to it, and ``u`` is below it.
UNIFORM53 = 1 << 53

KIND_DATE = "DATE"
KIND_TIMESTAMP = "TIMESTAMP"

#: The milliseconds a ``TIMESTAMP`` holds: years 1 to 9999, the range RFC 3339 rendering takes
#: (:func:`flux_engine.core.clock.format_rfc3339_ms`).
TIMESTAMP_MIN_MS = (date(1, 1, 1) - date(1970, 1, 1)).days * DAY_MS
TIMESTAMP_MAX_MS = (date(9999, 12, 31) - date(1970, 1, 1)).days * DAY_MS + DAY_MS - 1

#: A date's hold draw, ``bernoulli(1 - t / 100)``, on its dates stream (ADR 0024 section 6).
HOLD_POSITION = 0
#: Where violation candidate 0 sits: ``1 + v + j * m`` with ``v = 256`` (EXPR-SPEC-2 section 7).
CANDIDATE_POSITION = 257
#: How many candidates a violating row tries.
CANDIDATES = 16


def unit_of(kind: str) -> int:
    """One day in the units of a column of *kind*: ``1`` for ``DATE``, ``86 400 000`` for ``TIMESTAMP``."""
    return 1 if kind == KIND_DATE else DAY_MS


def window_in_units(kind: str, bounds_ms: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    """The run's window in a column's units, or ``None`` when the run has none.

    A ``DATE`` column's window is the days that hold the two bounds, so a window that ends at
    23:59:59 on a day includes that whole day.
    """
    if bounds_ms is None:
        return None
    low, high = bounds_ms
    if kind == KIND_DATE:
        return low // DAY_MS, high // DAY_MS
    return low, high


def days_of_date(value: date) -> int:
    """A ``DATE`` value as epoch days."""
    return value.toordinal() - DATE_EPOCH_ORDINAL


def date_of_days(days: int) -> date:
    """Epoch days as a ``DATE`` value; ``ValueError`` or ``OverflowError`` when the year is out of range."""
    return date.fromordinal(days + DATE_EPOCH_ORDINAL)


@dataclass(frozen=True)
class DurationTable:
    """A duration histogram worked out once: its thresholds and the days each bin holds.

    ``cuts[i]`` is ``T_(i+1)`` of slot ``i``, one slot per bin and a last one for ``open`` when the
    histogram declares it. ``days[i]`` is the first and the last whole day offset bin ``i`` holds.
    """

    cuts: Tuple[int, ...]
    days: Tuple[Tuple[int, int], ...]


def _bin_days(edges: Tuple[int, ...], index: int, upper: bool) -> Tuple[int, int]:
    low, high = edges[index], edges[index + 1]
    if upper:
        return (low if index == 0 else low + 1), high
    return low, (high if index == len(edges) - 2 else high - 1)


@lru_cache(maxsize=256)
def duration_table(histogram: Histogram) -> DurationTable:
    """The thresholds and day ranges of *histogram*, on exact rationals."""
    edges = tuple(int(edge) for edge in histogram.bins)
    weights = [Fraction(weight) for weight in histogram.bin_weights]
    if histogram.open is not None:
        weights.append(Fraction(histogram.open))
    total = sum(weights, Fraction(0))
    if total == 0:
        raise ValueError("a duration histogram holds no weight")
    cuts = []
    running = Fraction(0)
    for weight in weights:
        running += weight
        cuts.append((running * UNIFORM53) // total)
    upper = histogram.closed_edge == "upper"
    days = tuple(_bin_days(edges, index, upper) for index in range(len(edges) - 1))
    return DurationTable(tuple(cuts), days)


def draw_end(table: DurationTable, start: int, u: int, unit: int) -> Optional[int]:
    """The end *u* picks for a row that starts at *start*, in the column's units; ``None`` for ``open``.

    *u* is one ``uniform53``, below ``2**53``.
    """
    slot = bisect_right(table.cuts, u)
    if slot >= len(table.days):
        return None  # the open slot
    before = table.cuts[slot - 1] if slot else 0
    first, last = table.days[slot]
    count = (last - first + 1) * unit
    return start + first * unit + (u - before) * count // (table.cuts[slot] - before)


@dataclass(frozen=True)
class Duration:
    """A lifetime whose end is drawn as a duration from its start, as the plan holds it.

    ``kind`` is the grain of both columns, ``DATE`` or ``TIMESTAMP``. ``end_past_window`` is
    ``open`` or ``kept`` once the contract's default is applied.
    """

    start: str
    end: str
    kind: str
    #: A plain histogram duration (ADR 0024 section 4); ``None`` when ``family`` holds the dwell.
    histogram: Optional[Histogram]
    end_past_window: str
    #: The percent of rows whose end is before their start, as a decimal string, when declared.
    target: Optional[str] = None
    #: An exponential or a dwell family (ADR 0025 section 3), in place of a plain histogram.
    family: Optional["DwellFamily"] = None
    #: The end column's own declared range ``Re`` in the column's units (ADR 0024 section 4): the
    #: end is cut so that it stays inside it. ``None`` for an end column that declares none. Read
    #: off the declared statistics, so it is not part of the plan's document.
    end_range: Optional[Tuple[int, int]] = None

    def __post_init__(self) -> None:
        if (self.histogram is None) == (self.family is None):
            raise ValueError("a duration holds a histogram or a dwell family, and not both")

    @property
    def id(self) -> str:
        """The rule id, as the ``lifetime`` line of ``verify`` names the fact."""
        return f"lifetime:{self.start}.{self.end}"

    @property
    def dwell(self) -> "DwellFamily":
        """The dwell family that draws the end: a plain histogram is one dwell for every row."""
        from flux_engine.generator.dwell import DwellFamily, HistogramDwell

        if self.family is not None:
            return self.family
        assert self.histogram is not None
        return DwellFamily(given=None, states=(), otherwise=HistogramDwell(self.histogram))

    @property
    def given(self) -> Optional[str]:
        """The state column the dwell is chosen by, or ``None`` when every row draws the same dwell."""
        return None if self.family is None else self.family.given

    def as_document(self) -> Dict[str, Any]:
        """The duration as canonical-ready JSON, for ``generatorDigest``.

        A plain histogram writes the document it always has; an exponential or a family writes the
        family's.
        """
        document: Dict[str, Any] = {
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "duration": (
                self.histogram.as_member()
                if self.histogram is not None
                else self.dwell.as_document()
            ),
            "endPastWindow": self.end_past_window,
        }
        if self.target is not None:
            # Only an entry with a target lists one, so a plan with none keeps every byte it had.
            document["target"] = self.target
        return document

    def draw(self, start: int, u: int, state: Optional[str] = None) -> Tuple[bool, Optional[int]]:
        """``(applies, end)`` for a row that starts at *start* in *state* (the text of its ``given``).

        ``applies`` is ``False`` when the family has no dwell for the state and no ``otherwise``: the
        row keeps the end its own cell drew. Otherwise *end* is the drawn end, ``None`` for no end.

        With an ``end_range`` the dwell is cut so that the end stays inside it (ADR 0024 section
        4). A row where no value of the dwell fits has no end under ``endPastWindow: open``, where
        the end is nullable; anywhere else the plan has refused such a row, and one that is left
        (a start with no declared bounds) keeps the end its own cell drew, as a row with no dwell does.
        """
        unit = unit_of(self.kind)
        if self.end_range is None:
            if self.histogram is not None:
                return True, draw_end(duration_table(self.histogram), start, u, unit)
            from flux_engine.generator.dwell import draw_family

            applies, units = draw_family(self.dwell, state, u, unit)
            return applies, (None if units is None else start + units)
        from flux_engine.generator.dwell import draw_family_within

        low = max(0, self.end_range[0] - start)
        high = self.end_range[1] - start
        applies, fits, units = draw_family_within(self.dwell, state, u, unit, low, high)
        if not applies:
            return False, None
        if not fits:
            return self.end_past_window == "open", None
        return True, (None if units is None else start + units)

    def drawn_end(self, start: int, u: int, state: Optional[str] = None) -> Optional[int]:
        """The end *u* picks for a row that starts at *start*, before the window rule; ``None`` for ``open``."""
        return self.draw(start, u, state)[1]

    def past_window(self, end: Optional[int], window: Optional[Tuple[int, int]]) -> Optional[int]:
        """*end* under the window rule: an end after the window's end is ``None`` under ``open``."""
        return past_window(end, window, self.end_past_window)

    def end_of(
        self, start: int, u: int, window: Optional[Tuple[int, int]], state: Optional[str] = None
    ) -> Optional[int]:
        """The end of a row that starts at *start*, in the column's units, or ``None`` for no end.

        *u* is the row's ``uniform53``. An end after the window's end is ``None`` under ``open``.
        """
        return self.past_window(self.drawn_end(start, u, state), window)


def past_window(
    end: Optional[int], window: Optional[Tuple[int, int]], end_past_window: str
) -> Optional[int]:
    """*end* under ``endPastWindow``: after the window's end it is ``None`` under ``open``, as drawn under ``kept``."""
    if end is not None and window is not None and end > window[1] and end_past_window == "open":
        return None
    return end


# ---------------------------------------------------------------------------
# A date within a linked row's lifetime (ADR 0024 section 5)
# ---------------------------------------------------------------------------

#: A closed lifetime ``(start, end)`` in a column's units; ``end`` is ``None`` when it is open.
Lifetime = Tuple[int, Optional[int]]


def bound_in_units(value: int, from_kind: str, to_kind: str, *, end: bool) -> int:
    """A lifetime's start or end, held in *from_kind*'s units, in *to_kind*'s units.

    A lifetime includes its last day: a ``DATE`` start bounds a ``TIMESTAMP`` column from its
    midnight UTC, and a ``DATE`` end up to the last millisecond of that day. A ``TIMESTAMP``
    bounds a ``DATE`` column by the day that holds it.
    """
    if from_kind == to_kind:
        return value
    if from_kind == KIND_DATE:
        return (value + 1) * DAY_MS - 1 if end else value * DAY_MS
    return value // DAY_MS


def lifetime_in_units(
    start: Optional[int], end: Optional[int], from_kind: str, to_kind: str
) -> Optional[Lifetime]:
    """The lifetime of a parent row, in the units of a column of *to_kind*; ``None`` when it has no start.

    The parent's *start* and *end* are in *from_kind*'s units. The lifetime is open when the end
    is ``None`` and when the end is before the start: a parent that violates its own lifetime
    reads as open.
    """
    if start is None:
        return None
    low = bound_in_units(start, from_kind, to_kind, end=False)
    if end is None or end < start:
        return low, None
    return low, bound_in_units(end, from_kind, to_kind, end=True)


def intersect(lifetimes: Sequence[Lifetime]) -> Lifetime:
    """The latest start and the earliest end of *lifetimes*; an open end is no bound.

    The plan's ties (``FLX-6204``) make the lifetimes of a generated row meet, so the fallback
    below is the defensive path for violating rows a ``target`` will generate: where they do not
    meet, the first lifetime stands, the nearest link's (ADR 0024 section 5).
    """
    start = max(low for low, _ in lifetimes)
    ends = [high for _, high in lifetimes if high is not None]
    end = min(ends) if ends else None
    if end is not None and end < start:
        return lifetimes[0]
    return start, end


def lifetimes_meet(lifetimes: Sequence[Lifetime]) -> bool:
    """Whether *lifetimes* have a day in common: the latest start is not after the earliest end."""
    start = max(low for low, _ in lifetimes)
    return all(high is None or high >= start for _, high in lifetimes)


def inside(lifetime: Lifetime, value: int) -> bool:
    """Whether *value* lies in the closed *lifetime* (an open end is no bound)."""
    low, high = lifetime
    return low <= value and (high is None or value <= high)


def interval_of(lifetime: Lifetime, window: Optional[Tuple[int, int]]) -> Lifetime:
    """``J``: *lifetime* cut at *window*, in the column's units (ADR 0024 section 5).

    The part of the lifetime inside the window when they meet; the whole lifetime when it ended
    before the window starts; its start alone when it starts after the window ends. Without a
    window, the lifetime. The lifetime always wins over the window, and ``J`` is never empty.
    """
    if window is None:
        return lifetime
    start, end = lifetime
    low, high = window
    if end is not None and end < low:
        return lifetime
    if start > high:
        return start, start
    return max(start, low), (high if end is None else min(end, high))


def declared_bounds(statistics: ColumnStatistics) -> Optional[Tuple[int, int]]:
    """A column's own declared range ``R`` in its units, or ``None`` when its statistics bound it not.

    The support the declared statistics give it: its ``range`` (``min``, ``max``), the one
    ``FLX-5005`` checks a written value against; for a ``marginalHistogram`` that has no range
    of its own, its first and last edges. Epoch days for a ``DATE``, milliseconds for a ``TIMESTAMP``.
    """
    if statistics.has_range:
        assert statistics.range_min is not None and statistics.range_max is not None
        return int(statistics.range_min), int(statistics.range_max)
    if statistics.bins:
        return int(statistics.bins[0]), int(statistics.bins[-1])
    return None


def fit_in_bounds(interval: Lifetime, bounds: Optional[Tuple[int, int]]) -> Optional[Lifetime]:
    """``J`` cut to the column's own range ``R``: ``J ∩ R``; ``None`` when it is empty (ADR 0024 section 5).

    A column with no declared range (*bounds* ``None``) keeps *interval*. An interval with no
    upper end is cut at the range's maximum.
    """
    if bounds is None:
        return interval
    low = max(interval[0], bounds[0])
    high = bounds[1] if interval[1] is None else min(interval[1], bounds[1])
    return (low, high) if low <= high else None


@dataclass(frozen=True)
class OffsetTable:
    """An offset histogram worked out once: its weights and the whole days each bin holds."""

    weights: Tuple[Fraction, ...]
    days: Tuple[Tuple[int, int], ...]


@lru_cache(maxsize=256)
def offset_table(histogram: Histogram) -> OffsetTable:
    """The weights and day ranges of an offset *histogram* (the day ranges of a duration's bins)."""
    edges = tuple(int(edge) for edge in histogram.bins)
    upper = histogram.closed_edge == "upper"
    return OffsetTable(
        tuple(Fraction(weight) for weight in histogram.bin_weights),
        tuple(_bin_days(edges, index, upper) for index in range(len(edges) - 1)),
    )


def offsets_reach_forward(histogram: Histogram) -> bool:
    """Whether *histogram* puts a positive weight on a day from 0 up: a draw it makes can lie at or after the start."""
    table = offset_table(histogram)
    return any(
        weight > 0 and last >= 0
        for weight, (_, last) in zip(table.weights, table.days, strict=True)
    )


@dataclass(frozen=True)
class CutTable:
    """An offset cut to an interval: the thresholds of the bins that keep a value, and those values.

    ``cuts[i]`` is ``T_(i+1)``, and ``spans[i]`` the first and the last value of the column that
    bin ``i`` keeps inside the interval. A duration's ``open`` weight is one more slot after the
    bins (``opened``): a draw that lands in it has no end.
    """

    cuts: Tuple[int, ...]
    spans: Tuple[Tuple[int, int], ...]
    opened: bool = False

    def end(self, u: int) -> Optional[int]:
        """The value one ``uniform53`` *u* picks: the first slot with ``u < T_i``, then its ``n`` values in order.

        ``None`` for the ``open`` slot of a duration.
        """
        slot = bisect_right(self.cuts, u)
        if self.opened and slot == len(self.cuts) - 1:
            return None
        before = self.cuts[slot - 1] if slot else 0
        first, last = self.spans[slot]
        return first + (u - before) * (last - first + 1) // (self.cuts[slot] - before)

    def value(self, u: int) -> int:
        """The value one ``uniform53`` *u* picks, for a table with no ``open`` slot."""
        found = self.end(u)
        assert found is not None
        return found


def cut_table(
    table: OffsetTable,
    start: int,
    interval: Lifetime,
    unit: int,
    open_weight: Fraction = Fraction(0),
) -> Optional[CutTable]:
    """*table* cut to *interval*, for a parent whose start is *start*; ``None`` when no bin keeps a value.

    All of it in the column's units, *unit* one day. A bin's day ``d`` holds the values of the
    column whose day offset from *start* is ``d``, so bin ``i`` holds ``start + first * unit`` to
    ``start + (last + 1) * unit - 1``. Inside the interval it keeps the part of its weight its
    values inside hold over all its values, as an exact rational; a bin with no value inside, and
    a bin with no weight, drop out. With ``W_i`` the kept weights and ``W`` their sum,
    ``T_i = floor(2^53 * (W_1 + ... + W_i) / W)`` (EXPR-SPEC-2 section 6). A duration's *open_weight*
    is one more slot after the bins, kept whole: it stands for no value, so the interval does not
    cut it, and a table with it always keeps a slot.
    """
    low, high = interval
    kept: List[Tuple[Fraction, Tuple[int, int]]] = []
    for weight, (first, last) in zip(table.weights, table.days, strict=True):
        if weight == 0:
            continue
        bin_low, bin_high = start + first * unit, start + (last + 1) * unit - 1
        inside_low = max(bin_low, low)
        inside_high = bin_high if high is None else min(bin_high, high)
        if inside_low > inside_high:
            continue
        share = Fraction(inside_high - inside_low + 1, bin_high - bin_low + 1)
        kept.append((weight * share, (inside_low, inside_high)))
    if open_weight > 0:
        kept.append((open_weight, (0, 0)))
    if not kept:
        return None
    total = sum((weight for weight, _ in kept), Fraction(0))
    cuts: List[int] = []
    running = Fraction(0)
    for weight, _ in kept:
        running += weight
        cuts.append((running * UNIFORM53) // total)
    return CutTable(tuple(cuts), tuple(span for _, span in kept), open_weight > 0)


class WithinStream(Protocol):
    """The two samplers a within draw takes, one call each, on the dates stream at position 1."""

    def uniform53(self) -> int:
        """One ``uniform53``."""

    def bounded(self, n: int) -> int:
        """One ``bounded`` draw in ``[0, n)``."""


def draw_within(
    interval: Lifetime, cut: Optional[CutTable], has_offset: bool, stream: WithinStream
) -> Tuple[int, bool]:
    """The value of a date drawn inside *interval*, and whether the row fell back from its offset.

    With a cut table the value is one ``uniform53`` over its thresholds. Without one (the entry
    has no offset, or no bin keeps a value inside the interval) it is uniform over the interval
    at the column's grain, one ``bounded`` over its values, which needs both ends. Which sampler
    a row takes depends on the interval and the bins, never on a draw. A row that has an offset
    and no cut table has fallen back from it.
    """
    if cut is not None:
        return cut.value(stream.uniform53()), False
    low, high = interval
    if high is None:
        raise ValueError("the interval has no upper end")
    return low + stream.bounded(high - low + 1), has_offset


@dataclass(frozen=True)
class WithinLevel:
    """One ``within`` entry for a date, as the plan holds it.

    ``relationship`` is the chain from the row to the parent whose lifetime bounds it. ``start``
    and ``end`` are that parent's lifetime columns and ``parent_kind`` their grain, ``kind`` the
    grain of the date column that is set.
    """

    column: str
    kind: str
    relationship: Tuple[str, ...]
    start: str
    end: Optional[str]
    parent_kind: str
    offset: Optional[Histogram] = None
    #: ``True`` for a period (``lifetime: true``): ``column`` is then the emit's lifetime start.
    period: bool = False
    #: The percent of covered rows that violate the entry, as a decimal string, when declared.
    target: Optional[str] = None
    #: The date column's own declared range ``R`` in its units (ADR 0024 section 5), ``None`` when
    #: it declares none. Read off the declared statistics, so it is not part of the plan's document.
    bounds: Optional[Tuple[int, int]] = None

    @property
    def id(self) -> str:
        """The rule id, as a ``within`` line of ``verify`` names the fact."""
        return f"within:{'.'.join(self.relationship)}.{'lifetime' if self.period else self.column}"

    def as_document(self) -> Dict[str, Any]:
        """The level as canonical-ready JSON, for ``generatorDigest``."""
        document: Dict[str, Any] = {
            "column": self.column,
            "kind": self.kind,
            "relationship": list(self.relationship),
            "start": self.start,
            "end": self.end,
            "parentKind": self.parent_kind,
        }
        if self.offset is not None:
            document["offset"] = self.offset.as_member()
        if self.period:
            document["period"] = True
        if self.target is not None:
            # Only an entry with a target lists one, so a plan with none keeps every byte it had.
            document["target"] = self.target
        return document


def cut_end(
    end: Optional[int],
    parent_ends: Sequence[Optional[int]],
    window: Optional[Tuple[int, int]],
    end_past_window: str,
) -> Optional[int]:
    """The end of a period: the drawn *end* cut at the parents' ends, then the window rule (ADR 0024 section 5).

    *end* is the end as the lifetime draws it, before the window rule, and *parent_ends* the ends
    of the covered parents' lifetimes in the column's units, ``None`` for a parent that has not
    ended. Where a parent has ended, an end that is ``None`` or after the earliest such end becomes
    that end, so the child ends with its parent. The window rule then applies only where no parent
    has ended: a child whose parent ended after the window keeps an end, which a ``None`` would
    break, and a child whose parents are all still active follows its own ``endPastWindow``.
    """
    ended = [bound for bound in parent_ends if bound is not None]
    if ended:
        bound = min(ended)
        return bound if end is None or end > bound else end
    return past_window(end, window, end_past_window)


@dataclass(frozen=True)
class Period:
    """The end of an emit's lifetime where its period (``lifetime: true``) is within a linked row's.

    ``start`` and ``end`` are the lifetime's columns and ``kind`` the grain of the end column.
    ``duration`` is the lifetime's duration, when it has one; without it the end is the end
    column's own cell value. ``levels`` are the period entries, whose parents' ends cut it.
    """

    start: str
    end: str
    kind: str
    end_past_window: str
    levels: Tuple[WithinLevel, ...]
    duration: Optional[Duration] = None
    #: Whether an entry for the lifetime's start column carries a target above zero: a row whose
    #: start lies outside its parents' lifetimes is then a violating row, and its end is not cut.
    #: Read off the levels' targets, so it is not part of the plan's document.
    start_targeted: bool = False
    #: The end column's own declared range ``Re`` in its units (ADR 0024 section 5), ``None`` when
    #: it declares none; read off the declared statistics, so it is not part of the plan's document.
    end_range: Optional[Tuple[int, int]] = None

    def as_document(self) -> Dict[str, Any]:
        """The period's end as canonical-ready JSON, for ``generatorDigest``."""
        return {
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "endPastWindow": self.end_past_window,
            "levels": [level.as_document() for level in self.levels],
        }


# ---------------------------------------------------------------------------
# Violating rows (ADR 0024 section 7)
# ---------------------------------------------------------------------------


def positive_target(target: Optional[str]) -> bool:
    """Whether *target*, a percent as a decimal string, asks for violating rows: it is above zero."""
    return target is not None and Decimal(target) > 0


@lru_cache(maxsize=256)
def holding_probability(target: str) -> Decimal:
    """``1 - t / 100`` for the percent *target* ``t``, exactly, for the ``bernoulli`` at position 0.

    The division by 100 only moves the decimal point, so a context wide enough for the digits the
    target is written in loses none of them, whatever context the caller runs in.
    """
    with localcontext() as context:
        context.prec = max(60, 2 * len(target) + 8)
        return (Decimal(100) - Decimal(target)) / Decimal(100)


#: The outcomes a row of a fact can come to, in the order a receipt lists them.
OUTCOMES = ("held", "drawn", "infeasible", "notViolated", "offsetFallbacks")
#: What a plan with seasonal rates adds (ADR 0025 section 2): rows whose 128 proposals were all rejected.
RATES_OUTCOMES = OUTCOMES + ("ratesFallbacks",)


class DatesTally:
    """Per fact id, how many rows held, were violated on purpose, or fell back (ADR 0024 section 7).

    ``held``: rows the fact covers that hold it. ``drawn``: rows violated by a candidate (or an
    end of the declared range). ``infeasible``: rows whose levels' lifetimes did not meet, counted
    for the outer entry. ``notViolated``: rows that drew a violation and found no value outside the
    interval, so they hold. ``offsetFallbacks``: rows that fell back from their offset to a uniform
    draw. A plan with seasonal rates (``rates=True``) also counts ``ratesFallbacks``: rows whose value
    is the one a thinning fell back to after every proposal was rejected (ADR 0025 section 2). A tally
    adds up across slices of rows, so processes count apart and merge.
    """

    __slots__ = ("counts", "outcomes")

    def __init__(self, facts: Sequence[str] = (), *, rates: bool = False) -> None:
        self.outcomes: Tuple[str, ...] = RATES_OUTCOMES if rates else OUTCOMES
        self.counts: Dict[str, Dict[str, int]] = {
            fact: dict.fromkeys(self.outcomes, 0) for fact in facts
        }

    def add(self, fact: str, outcome: str) -> None:
        """Count one row of *fact* as *outcome*."""
        self.counts.setdefault(fact, dict.fromkeys(self.outcomes, 0))[outcome] += 1

    def merge(self, other: "DatesTally") -> None:
        """Add *other*'s counts to these."""
        for fact, found in other.counts.items():
            mine = self.counts.setdefault(fact, dict.fromkeys(found, 0))
            for outcome, count in found.items():
                mine[outcome] = mine.get(outcome, 0) + count

    def as_document(self) -> Dict[str, Dict[str, int]]:
        """The counts as the receipt's ``dates`` member holds them: per fact id, every outcome."""
        return {fact: dict(found) for fact, found in sorted(self.counts.items())}
