# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A run's shape against its pinned profile, column by column.

docs/spec/SCORECARD-SPEC.md §6. The checks run over every column a pinned
profile describes, in every emit whose table it describes, whether or not a
fidelity property is declared on it. Each raises a flag named after
ydata-profiling's alerts where one fits (4.20.0, ``model/alerts.py``), and
the two ydata has no name for are this engine's:

- ``EMPTY_BINS``: cells the profile publishes a count above zero for (a
  ``marginalHistogram/1`` bin or a ``levelHistogram/1`` level) that the run
  leaves empty. A cell is flagged when the exact chance of its being empty
  under the profile, ``(1 - p)^n``, is at most the test level.
- ``IMBALANCE``: each value holding at least a tenth of the column's
  records, flagged when the profile's share of the cell that holds it makes
  that many records too unlikely: Chernoff's bound on the binomial upper
  tail is at most the test level. A value outside every cell of the profile
  is a mass the profile does not have at all.
- ``CONSTANT``: one distinct value in two or more records, where the profile
  shows more than one and makes a column of one value too unlikely: the
  chance of that, bounded from its count table or from its ``count`` and
  ``distinctCount``, is at most the test level.
- ``HIGH_CARDINALITY`` and ``LOW_CARDINALITY``: a distinct count above twice
  the highest, or below half the lowest, the profile allows a sample the size
  of the run's column: the expected occupancy of its levels, for a categorical
  column whose every level is published; otherwise the range its ``count``
  and ``distinctCount`` allow, for a run no larger than that ``count``.

The test level is ``1/1000`` for the whole run, split evenly (Bonferroni)
across every test the run takes: one per cell that can be flagged empty, one
per published cell for a point mass, since a value's test is a test of its
cell's count, and one per column for a constant column. A run drawn from the
profile itself therefore shows a false ``EMPTY_BINS``, ``IMBALANCE`` or
``CONSTANT`` flag in at most one run in a thousand.

What the profile withheld is unknown, so it is never tested: a withheld cell
is not a candidate, and the run's records in it are left out of ``n``, as
PROFILE-SPEC §7 compares a count table over its published cells only. A
record outside every cell stays in ``n``: the profile gives it no chance at
all, so it counts against every cell. A count a disclosure rule rounded
(ACRO's base) is read as the share least favourable to a flag: the smallest
share it allows for an empty cell, the largest for a point mass.

The flags inform. None of them withholds an emit. The document names cells
by ordinal and holds the run's own counts only: no value of the data, and no
count, share or distinct count of the profile.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from flux_engine.concordance.decoded import DecodedRows, bin_cells
from flux_engine.core.errors import FluxError
from flux_stats.column_metrics import CATEGORY, TIMESTAMP
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM
from flux_stats.fixedpoint import FLX_5005, Grid
from flux_stats.profile import FREE, ColumnEntry, Profile
from flux_stats.reading import (
    MS_PER_DAY,
    ValueDomain,
    exact,
    nfc,
    parse_date_days,
    parse_rfc3339_ms,
)
from flux_stats.tails import occupancy_compare, power_at_most, upper_tail_at_most

__all__ = [
    "ABSENT",
    "CARDINALITY_FACTOR",
    "CHECKED",
    "CONSTANT",
    "EMPTY_BINS",
    "HIGH_CARDINALITY",
    "IMBALANCE",
    "LEVEL",
    "LOW_CARDINALITY",
    "NOT_PROFILED",
    "POINT_MASS_SHARE",
    "REFUSED",
    "ColumnSummary",
    "EmitSummary",
    "ShapeFlag",
    "ShapeReport",
    "decide_shape",
    "refused_emit",
    "summarise_emit",
]

#: The test level of the whole run, split evenly across its tests.
LEVEL = Fraction(1, 1000)
#: The share of a column's records one value must hold to be tested as a point mass.
POINT_MASS_SHARE = Fraction(1, 10)
#: How far a distinct count may stray from the predicted one, either way.
CARDINALITY_FACTOR = 2

_NUMBER = ValueDomain.NUMBER

EMPTY_BINS = "EMPTY_BINS"
IMBALANCE = "IMBALANCE"
CONSTANT = "CONSTANT"
HIGH_CARDINALITY = "HIGH_CARDINALITY"
LOW_CARDINALITY = "LOW_CARDINALITY"

CHECKED = "checked"
REFUSED = "refused"
#: A column the profile describes that the run's rows do not carry.
ABSENT = "absent"
#: An emit whose table the profile does not describe.
NOT_PROFILED = "not profiled"


# ---------------------------------------------------------------------------
# What the profile says about one column
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cells:
    """A column's count table in the profile, as the checks read it."""

    edges: Tuple[int, ...]
    levels: Tuple[str, ...]
    counts: Tuple[int, ...]
    published: Tuple[bool, ...]
    #: The most a published count can differ from the count it was rounded from.
    slack: int
    #: A bin table's first and last edges, exactly: the profiled range.
    bounds: Tuple[Fraction, Fraction] = (Fraction(0), Fraction(0))
    #: The published counts each raised by the slack, and each lowered (not below 0).
    raised: int = 0
    lowered: int = 0

    def locator(self, grid: Grid) -> Callable[[Any], Optional[int]]:
        """A function from a value to the cell holding it, ``None`` when no cell does.

        A value is inside the profiled range when it lies between the first
        and the last edge exactly; inside it, it falls in the bin
        :func:`flux_stats.bin_index` gives its grid value, found by bisection.
        """
        if self.levels:
            return {level: index for index, level in enumerate(self.levels)}.get
        edges = self.edges
        low, high = self.bounds
        last = len(edges) - 2

        def cell_of(value: Any) -> Optional[int]:
            if value < low or value > high:
                return None
            units = grid.quantise(value, subject="value")
            return min(bisect_right(edges, units) - 1, last)

        return cell_of

    def candidates(self) -> Tuple[int, ...]:
        """The cells a run can be flagged for leaving empty: published, and above the slack."""
        return tuple(
            index
            for index, (count, published) in enumerate(
                zip(self.counts, self.published, strict=True)
            )
            if published and count > self.slack
        )

    def smallest_share(self, index: int) -> Fraction:
        """The smallest share of the published total published cell *index* can hold.

        Its count lowered by the slack, every other published count raised.
        """
        own = self.counts[index] - self.slack
        if own <= 0:
            return Fraction(0)
        return Fraction(own, own + self.raised - (self.counts[index] + self.slack))

    def largest_share(self, index: int) -> Fraction:
        """The largest share of the published total published cell *index* can hold.

        Its count raised by the slack, every other published count lowered
        (never below 0). A table of published zeros gives every cell share 0.
        """
        own = self.counts[index] + self.slack
        total = own + self.lowered - max(self.counts[index] - self.slack, 0)
        return Fraction(own, total) if own and total else Fraction(0)


def _slack(profile: Profile, disclosure: str) -> int:
    rule = profile.disclosure_rules.get(disclosure)
    base = rule.rounding_base if rule is not None else None
    return base - 1 if base else 0


def _cells(profile: Profile, entry: ColumnEntry, grid: Grid) -> Optional[_Cells]:
    """The column's ungrouped count table, when the profile published one."""
    wanted = LEVEL_HISTOGRAM if entry.domain == CATEGORY else MARGINAL_HISTOGRAM
    for statistic in entry.statistics:
        if (
            statistic.identifier != wanted.identifier
            or statistic.group_by is not None
            or statistic.group_by_link is not None
            or statistic.per
        ):
            continue
        group = next((group for group in statistic.groups if group.key == ""), None)
        if group is None or not group.published or group.statistic is None:
            continue
        bounds = (Fraction(0), Fraction(0))
        if wanted is LEVEL_HISTOGRAM:
            counts = group.statistic.level_counts
            edges: Tuple[int, ...] = ()
            levels = tuple(statistic.levels)
        else:
            counts = group.statistic.bin_counts
            edges = tuple(grid.quantise(edge, subject="bin edge") for edge in statistic.bins)
            levels = ()
            bounds = (Fraction(statistic.bins[0]), Fraction(statistic.bins[-1]))
        status = group.cell_status or (FREE,) * len(counts)
        published = tuple(cell == FREE for cell in status)
        slack = _slack(profile, statistic.disclosure)
        shown = [count for count, kept in zip(counts, published, strict=True) if kept]
        return _Cells(
            edges=edges,
            levels=levels,
            counts=tuple(counts),
            published=published,
            slack=slack,
            bounds=bounds,
            raised=sum(count + slack for count in shown),
            lowered=sum(max(count - slack, 0) for count in shown),
        )
    return None


def _metric(profile: Profile, entry: ColumnEntry, name: str) -> Optional[Tuple[int, int]]:
    """A published count metric of the column (``count``, ``distinctCount``) and its rounding slack."""
    metric = entry.metrics.get(name)
    if metric is None or metric.status != FREE or metric.value is None:
        return None
    return int(metric.value), _slack(profile, metric.disclosure)


# ---------------------------------------------------------------------------
# What the run holds in one column: counts only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSummary:
    """One profiled column of one emit, reduced to the counts the checks read.

    Nothing of the data is kept but counts: how many records hold a value,
    how many distinct values there are, how many records fall in each of the
    profile's cells and how many in none, and the size and cell of every value
    holding a tenth of the records or more.
    """

    column: str
    status: str
    records: int = 0
    distinct: int = 0
    cell_counts: Tuple[int, ...] = ()
    #: The records outside every cell of the profile.
    outside: int = 0
    #: ``(cell, records)`` of each value holding a tenth of the records or
    #: more, by cell (``None``, outside every cell, last), then most records.
    masses: Tuple[Tuple[Optional[int], int], ...] = ()
    code: Optional[str] = None
    reason: Optional[str] = None
    cells: Optional[_Cells] = None
    #: The profile's published ``distinctCount`` and ``count``, each with its rounding slack.
    profile_distinct: Optional[Tuple[int, int]] = None
    profile_count: Optional[Tuple[int, int]] = None


@dataclass(frozen=True)
class EmitSummary:
    """One emit of the run, column by column, or why it was not read."""

    expose_id: str
    status: str
    columns: Tuple[ColumnSummary, ...] = ()
    code: Optional[str] = None
    reason: Optional[str] = None


def _value(value: Any, domain: str, subject: str) -> Any:
    """A cell read as the profile reads its column: exact decimal, epoch ms, or NFC text.

    A profile reads a DATE column as a timestamp, so a ``YYYY-MM-DD`` cell of a
    timestamp column is the millisecond its day starts at (UTC), which is how
    the profile holds that column's values. A date-time is read as ever.
    """
    if domain == CATEGORY:
        if isinstance(value, bool):
            return "true" if value else "false"  # a BOOL column is a categorical one
        if isinstance(value, int):
            return str(value)  # an INT column drawn over a few levels is one too
        if not isinstance(value, str):
            raise FluxError(FLX_5005, f"{subject}: value class {type(value).__name__!r}")
        return nfc(value)
    if domain == TIMESTAMP:
        try:
            if isinstance(value, str) and len(value) == 10:
                return Fraction(parse_date_days(value) * MS_PER_DAY)
            return Fraction(parse_rfc3339_ms(value))
        except FluxError as error:
            raise FluxError(error.code, f"{subject}: {error.detail}") from None
    return exact(value, subject)


def _occurrences(
    rows: Sequence[Mapping[str, Any]], entry: ColumnEntry, expose_id: str
) -> Dict[Any, int]:
    """How many records hold each value of the column, read as the profile reads it."""
    name = entry.name
    occurrences: Dict[Any, int] = {}
    for index, row in enumerate(rows):
        if name not in row:
            raise FluxError(FLX_5005, f"column {name!r} is absent from row {index}")
        raw = row[name]
        if raw is None:
            continue
        value = _value(raw, entry.domain, f"{expose_id} column {name!r} row {index}")
        occurrences[value] = occurrences.get(value, 0) + 1
    return occurrences


def _placed(
    occurrences: Mapping[Any, int], cells: _Cells, grid: Grid
) -> Tuple[List[int], Dict[Any, Optional[int]]]:
    """The run's records per cell of the profile, and the cell holding each value."""
    counts = [0] * len(cells.counts)
    holding: Dict[Any, Optional[int]] = {}
    cell_of = cells.locator(grid)
    for value, occurring in occurrences.items():
        cell = holding[value] = cell_of(value)
        if cell is not None:
            counts[cell] += occurring
    return counts, holding


def _masses(
    occurrences: Mapping[Any, int], holding: Mapping[Any, Optional[int]], records: int
) -> Tuple[Tuple[Optional[int], int], ...]:
    """``(cell, records)`` of every value holding at least :data:`POINT_MASS_SHARE` of *records*."""
    found = [
        (holding.get(value), occurring)
        for value, occurring in occurrences.items()
        if Fraction(occurring, records) >= POINT_MASS_SHARE
    ]
    return tuple(sorted(found, key=lambda mass: (mass[0] is None, mass[0] or 0, -mass[1])))


def _summarise_column(
    profile: Profile,
    expose_id: str,
    entry: ColumnEntry,
    rows: Sequence[Mapping[str, Any]],
    grid: Grid,
) -> ColumnSummary:
    name = entry.name
    cells = _cells(profile, entry, grid)
    distinct = _metric(profile, entry, "distinctCount")
    if rows and name not in rows[0]:
        return ColumnSummary(column=name, status=ABSENT)
    if isinstance(rows, DecodedRows):
        counted = _summarise_decoded(rows, entry, cells, grid)
        if counted is not None:
            records, distinct_values, tallied, outside, heavy_masses = counted
            return ColumnSummary(
                column=name,
                status=CHECKED,
                records=records,
                distinct=distinct_values,
                cell_counts=tallied,
                outside=outside,
                masses=heavy_masses,
                cells=cells,
                profile_distinct=distinct,
                profile_count=_metric(profile, entry, "count"),
            )
    try:
        occurrences = _occurrences(rows, entry, expose_id)
        counts, holding = _placed(occurrences, cells, grid) if cells is not None else ([], {})
    except FluxError as error:
        return ColumnSummary(column=name, status=REFUSED, code=error.code.code, reason=error.detail)
    records = sum(occurrences.values())
    return ColumnSummary(
        column=name,
        status=CHECKED,
        records=records,
        distinct=len(occurrences),
        cell_counts=tuple(counts),
        outside=records - sum(counts) if cells is not None else 0,
        masses=_masses(occurrences, holding, records) if cells is not None and records else (),
        cells=cells,
        profile_distinct=distinct,
        profile_count=_metric(profile, entry, "count"),
    )


def _summarise_decoded(
    rows: DecodedRows, entry: ColumnEntry, cells: Optional[_Cells], grid: Grid
) -> Optional[Tuple[int, int, Tuple[int, ...], int, Tuple[Tuple[Optional[int], int], ...]]]:
    """What :func:`_summarise_column` counts, from the column decoded once, or ``None`` to read the rows.

    The records and the distinct values, the records in each cell of the profile and in none,
    and the values holding a tenth of the records or more: the same numbers
    :func:`_occurrences`, :func:`_placed` and :func:`_masses` count one value at a time, here from
    sorted integer values and their counts. ``None`` when a cell is not one the fast reading takes
    (an unusual spelling, a class the row reader refuses), so the rows' reader decides it.
    """
    name = entry.name
    if not len(rows) or not rows.has(name):
        return None
    if entry.domain == CATEGORY:
        found = rows.levels(name)
        if found is None:
            return None
        codes = found.codes[found.codes >= 0]
        counts = np.bincount(codes, minlength=len(found.levels))
        values = found.levels
        occurring = [int(c) for c in counts]
        records = int(codes.size)
        if cells is None:
            return records, len(values), (), 0, ()
        locate = cells.locator(grid)
        placed = [locate(level) for level in values]
        per_cell = [0] * len(cells.counts)
        for cell, count in zip(placed, occurring, strict=True):
            if cell is not None:
                per_cell[cell] += count
        masses = [
            (placed[i], count) for i, count in enumerate(occurring) if count * 10 >= records > 0
        ]
        return _counted(records, len(values), per_cell, masses)
    column = rows.instants(name) if entry.domain == TIMESTAMP else rows.exact(name, _NUMBER)
    if column is None:
        return None
    units = column.units if column.valid is None else column.units[column.valid]
    records = int(units.size)
    if cells is None:
        return records, int(np.unique(units).size), (), 0, ()
    unique, counts = np.unique(units, return_counts=True)
    placed_cells = bin_cells(unique, column.scale, cells.edges, cells.bounds, grid)
    if placed_cells is None:
        return None
    inside = placed_cells >= 0
    tallies = np.bincount(placed_cells[inside], weights=counts[inside], minlength=len(cells.counts))
    heavy_at: List[int] = np.flatnonzero(counts * 10 >= records).tolist() if records else []
    heavy = [
        (int(placed_cells[i]) if placed_cells[i] >= 0 else None, int(counts[i])) for i in heavy_at
    ]
    return _counted(records, int(unique.size), [int(c) for c in tallies], heavy)


def _counted(
    records: int,
    distinct: int,
    per_cell: Sequence[int],
    masses: Sequence[Tuple[Optional[int], int]],
) -> Tuple[int, int, Tuple[int, ...], int, Tuple[Tuple[Optional[int], int], ...]]:
    ordered = tuple(sorted(masses, key=lambda mass: (mass[0] is None, mass[0] or 0, -mass[1])))
    return records, distinct, tuple(per_cell), records - sum(per_cell), ordered


def summarise_emit(
    profile: Profile, expose_id: str, rows: Sequence[Mapping[str, Any]]
) -> EmitSummary:
    """Reduce one emit's rows to the counts the shape checks read, column by column.

    Every column the profile describes for the emit's table is read, in name
    order. A value that cannot be read as the profile reads its column
    refuses that column (``FLX-5005``, naming the row and the value's class,
    never the value) and no other.
    """
    table = profile.tables.get(expose_id)
    if table is None:
        return EmitSummary(expose_id=expose_id, status=NOT_PROFILED)
    grid = Grid(fractional_bits=profile.fractional_bits)
    columns = tuple(
        _summarise_column(profile, expose_id, table.columns[name], rows, grid)
        for name in sorted(table.columns)
    )
    return EmitSummary(expose_id=expose_id, status=CHECKED, columns=columns)


def refused_emit(expose_id: str, code: str, reason: str) -> EmitSummary:
    """An emit whose rows could not be read at all: its table is refused, and says why."""
    return EmitSummary(expose_id=expose_id, status=REFUSED, code=code, reason=reason)


# ---------------------------------------------------------------------------
# The flags
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShapeFlag:
    """One flag on one column."""

    alert: str
    #: ``EMPTY_BINS``: the empty cells, by ordinal.
    cells: Tuple[int, ...] = ()
    #: ``IMBALANCE``: the cell holding the value, ``None`` when no cell does.
    cell: Optional[int] = None
    #: ``IMBALANCE``: the records holding the value.
    count: Optional[int] = None

    def to_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {"alert": self.alert}
        if self.alert == EMPTY_BINS:
            document["cells"] = list(self.cells)
        elif self.alert == IMBALANCE:
            document["cell"] = self.cell
            document["count"] = self.count
        return document

    def text(self, distinct: int) -> str:
        """The flag as a person reads it; *distinct* is the column's distinct count."""
        if self.alert == EMPTY_BINS:
            return f"EMPTY_BINS (cells {', '.join(str(cell) for cell in self.cells)})"
        if self.alert == IMBALANCE:
            where = f"cell {self.cell}" if self.cell is not None else "outside every cell"
            return f"IMBALANCE ({self.count} records on one value, {where})"
        if self.alert == HIGH_CARDINALITY:
            return (
                f"HIGH_CARDINALITY ({distinct} distinct, above twice the most the profile allows)"
            )
        if self.alert == LOW_CARDINALITY:
            return (
                f"LOW_CARDINALITY ({distinct} distinct, below half the fewest the profile allows)"
            )
        return self.alert


@dataclass(frozen=True)
class ColumnShape:
    """One column's flags, or why it was not checked."""

    summary: ColumnSummary
    flags: Tuple[ShapeFlag, ...] = ()

    def to_document(self) -> Dict[str, Any]:
        summary = self.summary
        checked = {
            "records": summary.records,
            "distinct": summary.distinct,
            "flags": [flag.to_document() for flag in self.flags],
        }
        return _entry({"column": summary.column}, summary, checked)


@dataclass(frozen=True)
class TableShape:
    """One emit's columns, or why its table was not checked."""

    summary: EmitSummary
    columns: Tuple[ColumnShape, ...] = ()

    def to_document(self) -> Dict[str, Any]:
        checked = {"columns": [column.to_document() for column in self.columns]}
        return _entry({"exposeId": self.summary.expose_id}, self.summary, checked)


@dataclass(frozen=True)
class ShapeReport:
    """The run's shape flags: every profiled column of every emit, and the level they were tested at."""

    tables: Tuple[TableShape, ...]
    #: The number of tests the run took, which the level is split across.
    tests: int
    level: Fraction = LEVEL

    @property
    def test_level(self) -> Fraction:
        """The level each single test is taken at."""
        return self.level / self.tests if self.tests else self.level

    def flagged(self) -> List[Tuple[str, ColumnShape]]:
        """Every column with at least one flag, with its table's ``exposeId``."""
        return [
            (table.summary.expose_id, column)
            for table in self.tables
            for column in table.columns
            if column.flags
        ]

    def to_document(self) -> Dict[str, Any]:
        """The scorecard's ``shapeFlags`` (docs/spec/scorecard-1.schema.json)."""
        return {
            "status": CHECKED,
            "level": _spell(self.level),
            "tests": self.tests,
            "testLevel": _spell(self.test_level),
            "tables": [table.to_document() for table in self.tables],
        }

    def summary_lines(self, limit: Optional[int] = None) -> List[str]:
        """The flags as a person reads them: a count, then one line per flagged or unread column.

        Every line stands on its own, so ``verify`` can indent them under the
        scorecard and ``EVIDENCE.md`` can list them.
        """
        checked = [
            column
            for table in self.tables
            for column in table.columns
            if column.summary.status == CHECKED
        ]
        flagged = self.flagged()
        noun = "column" if len(checked) == 1 else "columns"
        where = (
            f"{self.tests} test{'' if self.tests == 1 else 's'} at {_spell(self.level)} per run"
            if self.tests
            else "no count table to test"
        )
        if not flagged:
            lines = [f"shape: no flags in {len(checked)} profiled {noun} ({where})"]
        else:
            total = sum(len(column.flags) for _, column in flagged)
            lines = [
                f"shape: {total} flag{'' if total == 1 else 's'} in {len(flagged)} of "
                f"{len(checked)} profiled {noun} ({where}); "
                + (
                    "they inform and withhold nothing"
                    if limit is None
                    else f"at most {limit} allowed"
                )
            ]
        for expose_id, column in flagged:
            lines.append(
                f"shape of {expose_id}.{column.summary.column}: "
                + "; ".join(flag.text(column.summary.distinct) for flag in column.flags)
            )
        return lines + self._unread_lines()

    def _unread_lines(self) -> List[str]:
        """One line per table or column that was refused, that the rows lack, or that holds no value."""
        lines: List[str] = []
        for table in self.tables:
            expose_id = table.summary.expose_id
            if table.summary.status == REFUSED:
                lines.append(f"shape of {expose_id}: not read ({table.summary.reason})")
            for column in table.columns:
                where = f"shape of {expose_id}.{column.summary.column}"
                if column.summary.status == REFUSED:
                    lines.append(f"{where}: not read ({column.summary.reason})")
                elif column.summary.status == ABSENT:
                    lines.append(f"{where}: absent from the rows")
                elif column.summary.records == 0:
                    lines.append(f"{where}: no non-null record to test")
        return lines


def _entry(document: Dict[str, Any], summary: Any, checked: Mapping[str, Any]) -> Dict[str, Any]:
    """*document* with its status, and what that status carries: *checked*, or the refusal."""
    document["status"] = summary.status
    if summary.status == CHECKED:
        document.update(checked)
    elif summary.status == REFUSED:
        document.update(code=summary.code, reason=summary.reason)
    return document


def _spell(value: Fraction) -> str:
    return (
        str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    )


#: ``(multiplicity, share)`` cells whose expected occupancy is a distinct count.
_Model = List[Tuple[int, Fraction]]


def _occupancy_models(summary: ColumnSummary) -> Tuple[Optional[_Model], Optional[_Model]]:
    """The fewest and the most distinct values the profile allows a sample of the run's size.

    Over a level table whose every cell is published the shares are known,
    up to the rounding: the fewest reads each level at its smallest share
    and the most at its largest, at any size. Otherwise, from the profile's
    ``count`` ``N`` and ``distinctCount`` ``D``: a sample of ``m`` records
    drawn from ``N`` holding ``D`` distinct values occupies, in expectation,
    no more than ``D`` equally likely values do, and no fewer than ``D - 1``
    values of one record and one holding the rest do (the sum is concave in
    the shares), with ``D`` read at its largest and its smallest under the
    rounding. A sample larger than ``N`` is not predicted: the profile says
    nothing of values its records never held.
    """
    cells = summary.cells
    if cells is not None and cells.levels and all(cells.published):
        if cells.raised == 0:
            return None, None
        low = [
            (1, share)
            for share in (cells.smallest_share(index) for index in range(len(cells.counts)))
            if share > 0
        ]
        high = [
            (1, share)
            for share in (cells.largest_share(index) for index in range(len(cells.counts)))
            if share > 0
        ]
        return low, high
    distinct, counted = summary.profile_distinct, summary.profile_count
    if distinct is None or counted is None:
        return None, None
    values, slack = distinct
    least, largest = max(counted[0] - counted[1], 1), counted[0] + counted[1]
    if not 0 < values or summary.records > least:
        return None, None
    # The fewest falls as N grows past D, so it is read at the largest N the
    # rounding allows; the most does not depend on N beyond its cap.
    fewest, most = max(values - slack, 1), min(values + slack, largest)
    if fewest > largest:
        return None, None
    low = [(fewest - 1, Fraction(1, largest)), (1, Fraction(largest - fewest + 1, largest))]
    return low, [(most, Fraction(1, most))]


def _empty_bins(
    summary: ColumnSummary, cells: _Cells, tested: int, test_level: Fraction
) -> Optional[ShapeFlag]:
    """``EMPTY_BINS``: every candidate cell the run leaves empty against the odds."""
    if tested == 0:
        return None
    empty = tuple(
        index
        for index in cells.candidates()
        if summary.cell_counts[index] == 0
        and power_at_most(1 - cells.smallest_share(index), tested, test_level)
    )
    return ShapeFlag(alert=EMPTY_BINS, cells=empty) if empty else None


def _point_masses(
    summary: ColumnSummary, cells: _Cells, tested: int, test_level: Fraction
) -> List[ShapeFlag]:
    """``IMBALANCE``: each value holding a tenth or more of the records against the odds.

    A value outside every cell is a mass the profile gives no chance at all.
    One in a withheld cell is not tested: its cell's share is unknown.
    """
    flags: List[ShapeFlag] = []
    for cell, count in summary.masses:
        if cell is None or (
            cells.published[cell]
            and upper_tail_at_most(cells.largest_share(cell), tested, count, test_level)
        ):
            flags.append(ShapeFlag(alert=IMBALANCE, cell=cell, count=count))
    return flags


def _constant_terms(summary: ColumnSummary) -> Optional[Tuple[_Model, int]]:
    """What bounds the chance that every record of a column holds one value, and its terms' sum.

    With a count table showing two cells above the slack: no more than the
    chance that every record falls in one published cell, ``sum of p^m`` over
    the cells at their largest shares. With no count table, from the
    profile's ``count`` ``N`` and ``distinctCount`` ``D`` showing two values
    or more: no more than one value holding all but ``D - 1`` of the
    ``N`` records and ``D - 1`` holding one each allow. ``None`` when the
    profile cannot show more than one value. The terms are given as
    ``(multiplicity, 1 - p)``, the shape :func:`occupancy_compare` reads.
    """
    cells = summary.cells
    if cells is not None:
        if len(cells.candidates()) < 2:
            return None
        shares = [
            cells.largest_share(index)
            for index, published in enumerate(cells.published)
            if published
        ]
        return [(1, 1 - share) for share in shares], len(shares)
    distinct, counted = summary.profile_distinct, summary.profile_count
    if distinct is None or counted is None:
        return None
    # The fewest values and the most records the rounding allows: both make
    # the bound larger, so no rounded count can raise the flag.
    values, records = distinct[0] - distinct[1], counted[0] + counted[1]
    if not 1 < values <= records:
        return None
    biggest = Fraction(records - values + 1, records)
    return [(1, 1 - biggest), (values - 1, 1 - Fraction(1, records))], values


def _is_constant(summary: ColumnSummary, test_level: Fraction) -> bool:
    """``CONSTANT``: one distinct value in two or more records, and the profile makes that unlikely.

    A value outside every cell is a record the profile gives no chance, so a
    column of one such value is constant against the odds. One in a withheld
    cell is not tested.
    """
    if summary.distinct != 1 or summary.records < 2:
        return False
    found = _constant_terms(summary)
    if found is None:
        return False
    if summary.cells is not None:
        ((cell, _),) = summary.masses
        if cell is None:
            return True
        if not summary.cells.published[cell]:
            return False
    terms, total = found
    # sum of p^m <= level, read as occupancy over the terms >= total - level.
    return occupancy_compare(terms, summary.records, total - test_level) >= 0


def _cardinality(summary: ColumnSummary) -> Optional[ShapeFlag]:
    """A distinct count above twice the highest, or below half the lowest, the profile allows."""
    if summary.records == 0:
        return None
    low, high = _occupancy_models(summary)
    records, distinct = summary.records, summary.distinct
    if high is not None and (
        occupancy_compare(high, records, Fraction(distinct, CARDINALITY_FACTOR)) < 0
    ):
        return ShapeFlag(alert=HIGH_CARDINALITY)
    # A column of one value is the constant test's to judge, at its level.
    if (
        low is not None
        and distinct > 1
        and occupancy_compare(low, records, Fraction(distinct * CARDINALITY_FACTOR)) > 0
    ):
        return ShapeFlag(alert=LOW_CARDINALITY)
    return None


def _column_flags(summary: ColumnSummary, test_level: Fraction) -> Tuple[ShapeFlag, ...]:
    """Every flag of one checked column, in the order SCORECARD-SPEC.md §6 lists them.

    The tests count every record outside the withheld cells: one in a
    published cell, and one outside every cell, which the profile gives no
    chance and which so counts against every cell.
    """
    found: List[Optional[ShapeFlag]] = []
    cells = summary.cells
    if cells is not None:
        tested = summary.outside + sum(
            count
            for count, published in zip(summary.cell_counts, cells.published, strict=True)
            if published
        )
        found.append(_empty_bins(summary, cells, tested, test_level))
        found.extend(_point_masses(summary, cells, tested, test_level))
    if _is_constant(summary, test_level):
        found.append(ShapeFlag(alert=CONSTANT))
    found.append(_cardinality(summary))
    return tuple(flag for flag in found if flag is not None)


def _tests(column: ColumnSummary) -> int:
    """The tests one column takes: none with no record to read.

    One per cell that can be flagged empty, one per published cell for a
    point mass, and one for a constant column where the profile can say
    how unlikely it is.
    """
    if column.records == 0:
        return 0
    cells = column.cells
    tests = len(cells.candidates()) + sum(cells.published) if cells is not None else 0
    return tests + (1 if _constant_terms(column) is not None else 0)


def decide_shape(summaries: Sequence[EmitSummary], *, level: Fraction = LEVEL) -> ShapeReport:
    """The run's flags, from one :class:`EmitSummary` per emit.

    The run's tests are counted first, across every emit, so that the level
    is split across all of them before any is taken.
    """
    ordered = sorted(summaries, key=lambda summary: summary.expose_id)
    tests = sum(
        _tests(column)
        for summary in ordered
        if summary.status == CHECKED
        for column in summary.columns
        if column.status == CHECKED
    )
    test_level = level / tests if tests else level
    tables = tuple(
        TableShape(
            summary=summary,
            columns=tuple(
                ColumnShape(
                    summary=column,
                    flags=(_column_flags(column, test_level) if column.status == CHECKED else ()),
                )
                for column in summary.columns
            ),
        )
        for summary in ordered
    )
    return ShapeReport(tables=tables, tests=tests, level=level)
