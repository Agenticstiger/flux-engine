# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Periodic rows of a history: one row per held version and period (ADR 0027 section 5).

The periods run from the day of ``timeBounds.start`` (or the entry's ``from``) by the grain, each
counted from that anchor and never from the period before (a month added to a day the month lacks
lands on the month's last day, as ``ADD_MONTHS`` does), while they start before
``timeBounds.end``, which is excluded, and on or before the entry's ``through``, which is
included. Period ``p`` is ``[b_p, e_p)`` with ``e_p`` the next period's start or the window's end. The
period column holds the period's first day; each column that names a column of the history's emit
holds the final value of the pair's version, so a periodic row carries the version valid in its
period; the other columns are the emit's own cells at its row.

A version holds a period under the entry's ``match``: ``overlap`` every version valid at some
instant of it, ``start`` the version valid at its first instant, ``end`` the version valid at
its last instant inside the window. An open version is valid up to the window's end, so a version
holds a run of consecutive periods, ``first`` to ``last``, which a bisection over the periods'
instants gives. A held (version, period) is a pair.

The rows are period by period, then entity, then version, so a period's pairs are the versions
whose run holds it, in version-row order. The count follows from one pass over the versions, each
giving its first and last period, and a process that writes a slice finds its first period by
bisection over the periods' cumulative counts and walks only the periods the slice covers.
Periods after the old window's end are new rows appended after the rows already made, and
nothing before them moves (ADR 0027 section 7); none comes after ``through``.

:data:`KIND` is the dispatch entry of :mod:`flux_engine.generator.projections`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

from flux_engine.core import kernel2
from flux_engine.core.clock import format_rfc3339_ms, parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import SeedTreeRoot
from flux_engine.core.sizing import MOST_ROWS
from flux_engine.expr2.check import Checked
from flux_engine.expr2.functions import add_months
from flux_engine.generator import rows as _rows
from flux_engine.generator.counts import CountClass, classes_of, digest_key, slot_values
from flux_engine.generator.history import HistoryPlan
from flux_engine.generator.paths import KIND_PATH_SHAPE, kind_path
from flux_engine.generator.rows import GeneratorPlan, cells_before_rules, generate_row_slice
from flux_engine.generator.versionview import DAY_MS, VersionView, unit_value, view_of
from flux_engine.seam.contract import CompiledColumn, LogicalKind
from flux_engine.seam.projections import PeriodKind, PeriodsEntry

if TYPE_CHECKING:
    from flux_engine.generator.projections import ProjectionPlan

__all__ = [
    "DEFAULT_PERIODIC_ROWS",
    "PeriodLayout",
    "clear_layouts",
    "count",
    "estimated_pairs",
    "period_range",
    "layout_of",
    "period_grid",
    "periods_rows",
    "sources",
]

#: Months in a grain, for the grains counted in months.
_MONTHS = {"month": 1, "quarter": 3, "year": 12}
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
#: How many layouts a process keeps (each holds arrays over the versions and the periods).
_LAYOUTS_KEPT = 4
_LAYOUTS: Dict[Tuple[Any, ...], "PeriodLayout"] = {}
#: The most periods an entry may have; more is refused before any period is worked out.
MOST_PERIODS = 1_000_000
#: The rows a periodic emit may be estimated to derive before its periods are laid out, unless the
#: plan sets another budget (``fluxsim plan --max-rows``, which ``plan.json`` records as ``maxRows``).
DEFAULT_PERIODIC_ROWS = 50_000_000
#: The fewest days a period of each grain spans: a month from the 31st of January ends on the 28th
#: of February, and a quarter from the 1st of February spans 89 days.
_SHORTEST_DAYS = {"day": 1, "week": 7, "month": 28, "quarter": 89, "year": 365}


def _refuse(message: str) -> FluxError:
    from flux_engine.generator.projections import refuse

    return refuse(message)


def refuse_rows(label: str, rows: int) -> None:
    from flux_engine.generator.projections import refuse_rows as _refuse_rows

    _refuse_rows(label, rows)


def clear_layouts() -> None:
    """Drop the period layouts this process keeps."""
    _LAYOUTS.clear()


# -- the periods --------------------------------------------------------------------------------


def _period_start(anchor: date, grain: str, position: int) -> date:
    """Period *position* starts here: counted from *anchor*, never from the period before."""
    if grain == "day":
        return anchor + timedelta(days=position)
    if grain == "week":
        return anchor + timedelta(days=7 * position)
    return add_months(anchor, _MONTHS[grain] * position)


def _begins_before(anchor: date, grain: str, position: int, stop: int) -> bool:
    """Whether period *position* starts before *stop*; a period past the calendar does not."""
    try:
        begins = _period_start(anchor, grain, position)
    except (ValueError, OverflowError):
        return False
    return (begins.toordinal() - _EPOCH_ORDINAL) * DAY_MS < stop


def _day_of(text: str) -> date:
    return date.fromisoformat(text)


def _span(
    window: Tuple[int, int], from_day: Optional[str], through_day: Optional[str]
) -> Tuple[date, int]:
    """The day period 0 starts on, and the instant before which every period starts.

    A period's first day is at most *through_day*, so the bound is the day after it; with no
    ``through`` it is the window's end, which is excluded.
    """
    start, stop = window
    anchor = date.fromordinal(start // DAY_MS + _EPOCH_ORDINAL)
    if from_day is not None:
        anchor = _day_of(from_day)
    if through_day is not None:
        stop = min(stop, (_day_of(through_day).toordinal() + 1 - _EPOCH_ORDINAL) * DAY_MS)
    return anchor, stop


def period_count(
    grain: str,
    window: Tuple[int, int],
    from_day: Optional[str] = None,
    through_day: Optional[str] = None,
) -> int:
    """The number of periods :func:`period_grid` lists, found by bisection without listing them."""
    anchor, stop = _span(window, from_day, through_day)
    if not _begins_before(anchor, grain, 0, stop):
        return 0
    low, high = 0, 1
    while _begins_before(anchor, grain, high, stop):
        low, high = high, high * 2
    while high - low > 1:
        middle = (low + high) // 2
        if _begins_before(anchor, grain, middle, stop):
            low = middle
        else:
            high = middle
    return high


def _day_text(milliseconds: int, *, up: bool) -> Optional[str]:
    """The day of *milliseconds*, the first at or after it when *up*, else the last at or before it."""
    days = -(-milliseconds // DAY_MS) if up else milliseconds // DAY_MS
    try:
        return date.fromordinal(days + _EPOCH_ORDINAL).isoformat()
    except (ValueError, OverflowError):
        return None


def _day_before(text: str) -> Optional[str]:
    found = _day_of(text)
    return None if found == date.min else (found - timedelta(days=1)).isoformat()


def _earlier(first: Optional[str], second: Optional[str]) -> Optional[str]:
    """The earlier of two days, ``None`` being no bound."""
    return second if first is None else first if second is None else min(first, second)


def period_range(
    entry: PeriodsEntry, window: Tuple[int, int], declared: Tuple[Optional[int], Optional[int]]
) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """The days of the period column's range that cut the periods (ADR 0024, the amendment of 2026-11-25).

    *declared* is the column's range in milliseconds. A period is kept when its first day lies in
    it: ``(low, high)`` are the range's first and last days, each ``None`` where it drops no period,
    and the whole is ``None`` when it drops none. A range no period of the entry starts in is
    ``FLX-3403``.
    """
    grain = str(entry.grain)
    low = None if declared[0] is None else _day_text(declared[0], up=True)
    high = None if declared[1] is None else _day_text(declared[1], up=False)
    total = period_count(grain, window, entry.from_day, entry.through_day)
    through = _earlier(entry.through_day, high)
    kept = period_count(grain, window, entry.from_day, through)
    before = _before(entry, window, low, through)
    if total and kept <= before:
        raise _refuse(
            f"periods ({entry.emit}): the contract's range on the period column "
            f"{entry.column!r} ({low or ''} to {high or ''}) holds the first day of none of the "
            f"{total} periods of the window; widen the range, or move the window or the entry's "
            "from and through to meet it"
        )
    cut = (low if before else None, high if kept < total else None)
    return None if cut == (None, None) else cut


def _before(
    entry: PeriodsEntry, window: Tuple[int, int], low: Optional[str], through: Optional[str]
) -> int:
    """The periods that start before the day *low*, of those that start on or before *through*."""
    if low is None:
        return 0
    last = _day_before(low)
    if last is None:
        return 0
    return period_count(str(entry.grain), window, entry.from_day, _earlier(through, last))


def _clipped(entry: PeriodsEntry, projection: "ProjectionPlan") -> Tuple[PeriodsEntry, int]:
    """*entry* with its ``through`` at the period column's range, and the periods before its low end."""
    if projection.clip is None:
        return entry, 0
    assert projection.window is not None
    low, high = projection.clip
    clipped = replace(entry, through_day=_earlier(entry.through_day, high))
    return clipped, _before(clipped, projection.window, low, clipped.through_day)


def _refuse_many_periods(entry: PeriodsEntry, grain: str, window: Tuple[int, int]) -> None:
    """Refuse (``FLX-3403``) an entry whose range holds more than :data:`MOST_PERIODS` periods."""
    number = period_count(grain, window, entry.from_day, entry.through_day)
    if number > MOST_PERIODS:
        raise _refuse(
            f"periods ({entry.emit}): grain {grain} over the range makes {number} periods; "
            f"at most {MOST_PERIODS}"
        )


def _most_per_pair(entry: PeriodsEntry) -> int:
    """The most rows one held (version, period) pair takes: over the kinds, a count or a class's high."""
    most = 0
    for index, kind in enumerate(entry.kinds or (PeriodKind(),)):
        if isinstance(kind.count, int):
            most += kind.count
        else:
            where = f"periods ({entry.emit}).kinds[{index}].count"
            most += max(found.high for found in classes_of(kind.count.as_member(), where))
    return most


def estimated_pairs(
    entry: PeriodsEntry, window: Tuple[int, int], valid: Tuple["np.ndarray", "np.ndarray"]
) -> int:
    """An upper bound on the held pairs, worked out from the versions alone, before any period is listed.

    Spark's ``sequence`` sizes its array as ``1 + (stop - start) / step`` in exact arithmetic before
    it allocates; here a version valid over ``[a, b)`` inside the periods' span meets at most
    ``(b - a) // shortest + 2`` periods, ``shortest`` the fewest days a period of the grain spans,
    and never more than there are periods.
    """
    grain = str(entry.grain)
    periods = period_count(grain, window, entry.from_day, entry.through_day)
    if periods == 0:
        return 0
    anchor, _stop = _span(window, entry.from_day, entry.through_day)
    low = np.maximum(valid[0], (anchor.toordinal() - _EPOCH_ORDINAL) * DAY_MS)
    high = np.minimum(valid[1], window[1])
    met = (high - low) // (_SHORTEST_DAYS[grain] * DAY_MS) + 2
    return int(np.minimum(np.where(high >= low, met, 0), periods).sum())


def _refuse_above_budget(
    entry: PeriodsEntry,
    projection: "ProjectionPlan",
    valid: Tuple["np.ndarray", "np.ndarray"],
    entities: int,
) -> None:
    """Refuse (``FLX-3403``) an entry estimated to derive more rows than the plan's budget.

    The estimate is the pairs :func:`estimated_pairs` bounds times the most rows a pair takes, so
    the refusal comes before the periods, the layout or a row is made.
    """
    assert projection.window is not None
    budget = DEFAULT_PERIODIC_ROWS if projection.budget is None else projection.budget
    pairs = estimated_pairs(entry, projection.window, valid)
    estimate = pairs * _most_per_pair(entry)
    if estimate <= budget:
        return
    grain = str(entry.grain)
    start, end = (format_rfc3339_ms(instant) for instant in projection.window)
    periods = period_count(grain, projection.window, entry.from_day, entry.through_day)
    ranged = "".join(
        f", {word} {day}"
        for word, day in (("from", entry.from_day), ("through", entry.through_day))
        if day is not None
    )
    raise _refuse(
        f"periods ({entry.emit}): grain {grain} over the window {start} to {end}{ranged} makes "
        f"{periods} periods, and the {entities} entities' versions are estimated to hold {pairs} "
        f"(version, period) pairs, {estimate} rows, above the budget of {budget} rows a periodic "
        "emit; narrow the window (timeBounds, or the entry's from and through) or take a coarser "
        f"grain, or raise the budget with fluxsim plan --max-rows N (at most {MOST_ROWS})"
    )


def period_grid(
    grain: str,
    window: Tuple[int, int],
    from_day: Optional[str] = None,
    through_day: Optional[str] = None,
) -> Tuple[Tuple[date, ...], "np.ndarray", "np.ndarray"]:
    """Each period's first day, and its first instant and its end in milliseconds.

    The periods start from *from_day* (the day of the window's start when none) while they
    start before the window's end and on or before *through_day*; a period's end is the next
    period's start, and the window's end cuts it.
    """
    anchor, bound = _span(window, from_day, through_day)
    stop = window[1]
    days: List[date] = []
    # A period past the calendar (year 9999) starts after any bound, so it ends the list.
    while _begins_before(anchor, grain, len(days), bound):
        days.append(_period_start(anchor, grain, len(days)))
    begin = np.array([(day.toordinal() - _EPOCH_ORDINAL) * DAY_MS for day in days], dtype=np.int64)
    if not days:
        return (), begin, begin
    try:
        after = _period_start(anchor, grain, len(days))
        last_end = (after.toordinal() - _EPOCH_ORDINAL) * DAY_MS
    except (ValueError, OverflowError):
        last_end = stop
    following = np.append(begin[1:], np.int64(last_end))
    return tuple(days), begin, np.minimum(following, stop).astype(np.int64)


def _runs(
    match: str,
    begins: "np.ndarray",
    ends: "np.ndarray",
    valid_from: "np.ndarray",
    valid_to: "np.ndarray",
) -> Tuple["np.ndarray", "np.ndarray"]:
    """The first and the last period each version holds under *match*; empty where ``first > last``."""
    if match == "start":
        return (
            np.searchsorted(begins, valid_from, side="left"),
            np.searchsorted(begins, valid_to, side="left") - 1,
        )
    first = np.searchsorted(ends, valid_from, side="right")
    if match == "end":
        return first, np.searchsorted(ends, valid_to, side="right") - 1
    return first, np.searchsorted(begins, valid_to, side="left") - 1


# -- the kinds ----------------------------------------------------------------------------------


def kind_key(root: SeedTreeRoot, sim_id: str, expose_id: str, kind: int, period: int) -> bytes:
    """``K``: the 32-byte key of ``("sim", sim_id, "gen", exposeId, "kind", k, p)``, the positions in decimal."""
    return digest_key(root, kind_path(sim_id, expose_id, kind, period), KIND_PATH_SHAPE)


@dataclass(frozen=True, eq=False)
class _Kind:
    """One kind of row: how many rows a pair takes, which pairs it holds and what it overrides.

    ``mask`` says, per version row, whether the kind holds the version's pairs, for a ``when``
    that reads no period column; a ``when`` that does is ``checked`` per pair instead.
    """

    count: Union[int, Tuple[CountClass, ...]]
    mask: Optional["np.ndarray"]
    checked: Optional[Checked]
    overrides: Tuple[Tuple[str, str], ...]
    where: str = "when"


@lru_cache(maxsize=1024)
def _slots(classes: Tuple[CountClass, ...], pairs: int) -> "np.ndarray":
    """The rows each of *pairs* slots takes, level by level (ADR 0016 section 6's allotment)."""
    return np.asarray(slot_values(pairs, classes), dtype=np.int64)


def _sum(rows: "np.ndarray") -> int:
    """The exact sum of *rows*, which int64 would wrap where the rows are many and large."""
    if rows.shape[0] and int(rows.max()) * rows.shape[0] >= 1 << 62:
        return sum(int(row) for row in rows.tolist())
    return int(rows.sum())


def _kind_of(kind: PeriodKind, entry: PeriodsEntry, view: VersionView, where: str) -> _Kind:
    count: Union[int, Tuple[CountClass, ...]]
    if isinstance(kind.count, int):
        count = kind.count
    else:
        count = classes_of(kind.count.as_member(), f"{where}.count")
    checked = mask = None
    if kind.when is not None:
        checked = view.compile_when(kind.when, {entry.column: "DATE"})
        if all(read.column != entry.column for read in checked.reads):
            mask = view.holds(
                checked, np.arange(view.chain.rows, dtype=np.int64), where=f"{where}.when"
            )
            checked = None
    return _Kind(count, mask, checked, kind.overrides, f"{where}.when")


def _rows_of(kind: _Kind, held: int) -> int:
    """The rows a kind takes in a period where it holds *held* pairs: the sum of its slots."""
    if isinstance(kind.count, int):
        return kind.count * held
    return _sum(_slots(kind.count, held)) if held else 0


# -- the layout ---------------------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class PeriodLayout:
    """The periods and the pairs: each version's run of periods, and the rows before each period.

    ``first`` and ``last`` are per version row (a run is empty where ``first > last``); ``offsets``
    holds ``P + 1`` entries, the rows before each period. ``kinds`` are the entry's kinds, or the
    one kind of a row per pair.
    """

    days: Tuple[date, ...]
    first: "np.ndarray"
    last: "np.ndarray"
    offsets: "np.ndarray"
    kinds: Tuple[_Kind, ...]
    entry: PeriodsEntry
    view: VersionView
    root: SeedTreeRoot
    sim_id: str
    expose_id: str

    @property
    def count(self) -> int:
        """The number of rows."""
        return int(self.offsets[-1])

    def pairs(self, period: int) -> "np.ndarray":
        """The versions that hold *period*, in version-row order."""
        return np.flatnonzero((self.first <= period) & (self.last >= period))

    def held(self, kind: int, period: int, pairs: "np.ndarray") -> "np.ndarray":
        """Whether kind *kind* holds each of the *pairs* of *period*."""
        found = self.kinds[kind]
        if found.checked is not None:
            extra = {self.entry.column: self.days[period]}
            return self.view.holds(found.checked, pairs, extra, where=found.where)
        if found.mask is None:
            return np.ones(pairs.shape[0], dtype=bool)
        return found.mask[pairs]


def period_matrix(layout: PeriodLayout, period: int) -> Tuple["np.ndarray", "np.ndarray"]:
    """The pairs of *period* and, for each, the rows each kind gives it: ``(pairs, pairs x kinds)``.

    The ``M`` pairs a kind holds take ``LRM(M, weights)`` per level, and the pair of rank ``r``
    among them takes the level of slot ``permute/2(K, M, r)``, the slots laid out level by level.
    """
    pairs = layout.pairs(period)
    matrix = np.zeros((pairs.shape[0], len(layout.kinds)), dtype=np.int64)
    for index, kind in enumerate(layout.kinds):
        held = layout.held(index, period, pairs)
        number = int(held.sum())
        if not number:
            continue
        if isinstance(kind.count, int):
            matrix[held, index] = kind.count
            continue
        key = kind_key(layout.root, layout.sim_id, layout.expose_id, index, period)
        slot = np.array(
            kernel2.permute2_array(key, number, np.arange(number, dtype=np.int64)), dtype=np.int64
        )
        matrix[held, index] = _slots(kind.count, number)[slot]
    return pairs, matrix


def _entry_of(projection: "ProjectionPlan") -> PeriodsEntry:
    entry = projection.entry
    assert isinstance(entry, PeriodsEntry)
    if entry.grain is None:
        raise _refuse(f"periods ({entry.emit}): a grain is required; no timeGranularity gives one")
    if projection.window is None:
        raise _refuse(f"periods ({entry.emit}): periodic rows need the Simulation's timeBounds")
    return entry


def _per_period(layout_parts: Dict[str, Any], kinds: Tuple[_Kind, ...]) -> "np.ndarray":
    """The rows of each period: the sum over kinds of what each takes where it holds ``M`` pairs."""
    first, last, periods = layout_parts["first"], layout_parts["last"], layout_parts["periods"]
    runs = first <= last
    totals = np.zeros(periods, dtype=object)  # Python ints: a sum over kinds never wraps
    for index, kind in enumerate(kinds):
        if kind.checked is not None:
            totals += layout_parts["dynamic"](index)
            continue
        chosen = runs if kind.mask is None else runs & kind.mask
        edges = np.zeros(periods + 1, dtype=np.int64)
        np.add.at(edges, first[chosen], 1)
        np.add.at(edges, last[chosen] + 1, -1)
        held = np.cumsum(edges)[:periods]
        for number in np.unique(held).tolist():
            totals[held == number] += _rows_of(kind, int(number))
    return totals


def _build(chain: HistoryPlan, projection: "ProjectionPlan") -> PeriodLayout:
    entry, skipped = _clipped(_entry_of(projection), projection)
    assert projection.root is not None and projection.window is not None
    view = view_of(projection.history, chain, projection.root)
    _refuse_many_periods(entry, str(entry.grain), projection.window)
    valid_from, valid_to = view.valid_ms(projection.window[1])
    _refuse_above_budget(entry, projection, (valid_from, valid_to), chain.entities)
    days, begins, ends = period_grid(
        str(entry.grain), projection.window, entry.from_day, entry.through_day
    )
    days, begins, ends = days[skipped:], begins[skipped:], ends[skipped:]
    first, last = _runs(entry.match, begins, ends, valid_from, valid_to)
    where = f"periods ({entry.emit})"
    declared = entry.kinds or (PeriodKind(),)
    kinds = tuple(
        _kind_of(kind, entry, view, f"{where}.kinds[{index}]")
        for index, kind in enumerate(declared)
    )
    layout = PeriodLayout(
        days,
        first,
        last,
        np.zeros(len(days) + 1, dtype=np.int64),
        kinds,
        entry,
        view,
        projection.root,
        projection.history.sim_id,
        entry.emit,
    )

    def dynamic(index: int) -> "np.ndarray":
        rows = np.zeros(len(days), dtype=object)
        for period in range(len(days)):
            pairs = layout.pairs(period)
            rows[period] = _rows_of(kinds[index], int(layout.held(index, period, pairs).sum()))
        return rows

    per_period = _per_period(
        {"first": first, "last": last, "periods": len(days), "dynamic": dynamic}, kinds
    )
    totals = np.cumsum(per_period)
    refuse_rows(f"periods ({entry.emit})", int(totals[-1]) if len(totals) else 0)
    offsets = np.concatenate((np.zeros(1, dtype=np.int64), totals.astype(np.int64)))
    return replace(layout, offsets=offsets)


def layout_of(chain: HistoryPlan, projection: "ProjectionPlan") -> PeriodLayout:
    """The periods and pairs of the history, worked out once per process."""
    entry = _entry_of(projection)
    assert projection.root is not None
    root = projection.root
    held = (
        root.digest,
        root.seed_tree_version,
        projection.history.digest().value,
        repr(entry),
        projection.window,
        projection.budget,
    )
    found = _LAYOUTS.get(held)
    if found is None:
        found = _build(chain, projection)
        if len(_LAYOUTS) >= _LAYOUTS_KEPT:
            _LAYOUTS.clear()
        _LAYOUTS[held] = found
    return found


def count(chain: HistoryPlan, projection: "ProjectionPlan") -> int:
    """The number of periodic rows (ADR 0027 section 5), from the plan alone."""
    return layout_of(chain, projection).count


def sources(chain: HistoryPlan, projection: "ProjectionPlan") -> PeriodLayout:
    """The periods and pairs the rows are laid out by."""
    return layout_of(chain, projection)


# -- the rows -----------------------------------------------------------------------------------


def _period_range(layout: PeriodLayout, start: int, stop: int) -> range:
    """The periods whose rows meet rows *start* to *stop* - 1, found by bisection."""
    low = int(np.searchsorted(layout.offsets, start, side="right")) - 1
    high = int(np.searchsorted(layout.offsets, stop, side="left"))
    return range(low, high)


def _row_plan(
    layout: PeriodLayout, start: int, stop: int
) -> Tuple[List[int], List[int], List[int]]:
    """For rows *start* to *stop* - 1, each row's period, version row and kind, in row order."""
    periods: List[int] = []
    versions: List[int] = []
    kinds: List[int] = []
    width = len(layout.kinds)
    for period in _period_range(layout, start, stop):
        pairs, matrix = period_matrix(layout, period)
        flat = np.repeat(np.arange(pairs.shape[0] * width), matrix.ravel())
        periods.extend([period] * flat.shape[0])
        versions.extend(pairs[flat // width].tolist())
        kinds.extend((flat % width).tolist())
    first = int(layout.offsets[_period_range(layout, start, stop).start])
    cut = slice(start - first, stop - first)
    return periods[cut], versions[cut], kinds[cut]


def _override(text: str, column: CompiledColumn) -> Any:
    """*text* read as a value of *column*'s type."""
    kind = column.logical_type.kind
    if kind is LogicalKind.DATE:
        return date.fromisoformat(text)
    if kind is LogicalKind.TIMESTAMP:
        return parse_rfc3339_ms(text)
    return _rows._level_to_value(text, column)


def periods_rows(
    plan: GeneratorPlan, layout: PeriodLayout, start: int, stop: int, root: SeedTreeRoot
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* - 1 of the periodic emit *plan*, before the emit's rules run."""
    projection = plan.projection
    assert projection is not None and isinstance(projection.entry, PeriodsEntry)
    entry = projection.entry
    if start == stop:
        return ()
    held, versions, kinds = _row_plan(layout, start, stop)
    # The emit's own cells, before its rules: they run over the row once the period, the
    # version's columns and the kind's overrides are set (projections.projection_rows).
    own = cells_before_rules(replace(plan, projection=None), start, stop, root=root)
    names = [column.column.name for column in plan.columns]
    position = names.index(entry.column)
    kind = plan.column_plan(entry.column).column.logical_type.kind
    assert kind is LogicalKind.DATE
    overrides = []
    for declared in layout.kinds:
        overrides.append(
            {
                names.index(name): _override(text, plan.column_plan(name).column)
                for name, text in declared.overrides
            }
        )
    low, history_rows, named = _history_cells(projection, names, entry, versions, root)
    found = []
    for row, period, which, version in zip(own, held, kinds, versions, strict=True):
        cells = list(row)
        for index, source in named:
            cells[index] = history_rows[version - low][source]
        cells[position] = unit_value(kind, layout.days[period].toordinal() - _EPOCH_ORDINAL)
        for index, value in overrides[which].items():
            cells[index] = value
        found.append(tuple(cells))
    return tuple(found)


def _history_cells(
    projection: "ProjectionPlan",
    names: List[str],
    entry: PeriodsEntry,
    versions: List[int],
    root: SeedTreeRoot,
) -> Tuple[int, Sequence[Tuple[Any, ...]], List[Tuple[int, int]]]:
    """The history rows the pairs' versions span, and which columns of the emit read which of theirs.

    A column that names a column of the history's emit holds the pair's version's final value, so
    a periodic row carries the version valid in its period (the period column apart).
    """
    place = {column.column.name: index for index, column in enumerate(projection.history.columns)}
    named = [
        (index, place[name])
        for index, name in enumerate(names)
        if name in place and name != entry.column
    ]
    if not named:
        return 0, (), []
    low = min(versions)
    return low, generate_row_slice(projection.history, low, max(versions) + 1, root=root), named
