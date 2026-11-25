# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A contract's range for a column: the cut it makes of declared statistics, and the check ``verify`` makes of rows.

A contract column may carry ``validationRules: [{type: range, constraint: ...}]`` (ADR 0024, the
amendment of 2026-11-25). A DATE or TIMESTAMP column's range is read once, where the contract is
compiled, as the FLUID profiler's spelling reads it (:attr:`CompiledColumn.declared_range`, by
:func:`flux_engine.seam.range_rules.declared_instants`); a number column's range is read by
:func:`flux_engine.author.ranges.column_range`. Nothing here parses a range.

**The cut** (:func:`cut_statistics`), kernel 0.2.0 only. A DATE or TIMESTAMP column that is drawn from
declared statistics is drawn inside its range, so the statistics are cut before the draw, in the
column's own units (days or milliseconds):

* a declared ``range`` is intersected with it;
* a bin is cut at the range's ends and keeps weight in proportion to the width it keeps, so a draw
  inside a kept part is as likely as it was; a bin wholly outside is dropped. The last bin of a
  histogram is closed above, so a bin cut at the top ends on the range's last value. Weights stay
  exact decimals: every weight is multiplied by the same whole number, which a draw does not read
  (it divides by their sum);
* a ``binWeightsGiven`` table is cut the same way in every level.

A column whose statistics already lie inside the range is returned as it is, and keeps every byte.
A column with no statistics of its own is drawn within the run's ``timeBounds``, and that range is
intersected with the contract's too. Where nothing of what the column is drawn from lies inside the
range, the plan is refused (``FLX-3403``), naming the column, its range and what to do. A column that
another column is drawn ``given`` cannot lose or move a bin, since the other's weight tables name
the bins; that is refused too.

**The check** (:func:`range_problems`): the rows of an applied run that lie outside a column's
range, counted, for ``fluxsim verify``. A DATE or TIMESTAMP range is the one the generator cuts at;
a number range is the contract's, for an INT or DECIMAL column. NULL is never outside.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, Inexact
from fractions import Fraction
from math import lcm
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.clock import format_rfc3339_ms, parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.generator.statistics import DATE_EPOCH_ORDINAL, ColumnStatistics
from flux_engine.seam.contract import CompiledColumn, CompiledExpose, LogicalKind

__all__ = [
    "ColumnRange",
    "RangeCut",
    "cut_statistics",
    "range_text",
    "declared_ranges_of",
    "range_cut_notes",
    "range_phrase",
    "scale_widened_notes",
    "range_problems",
    "refuse_cut_parents",
]

_DAY_MS = 86_400_000


@dataclass(frozen=True)
class RangeCut:
    """That a column's statistics were cut at the contract's range, for the note ``plan`` prints.

    *low* and *high* spell the range as the column's own values are written (the empty text for an
    end the contract does not declare); *what* names what was cut: ``"bins"`` or ``"range"``.
    """

    low: str
    high: str
    what: str


#: What stands for an end the contract does not declare, in either unit: beyond any date.
_UNBOUNDED = 10**18


def range_phrase(low: str, high: str) -> str:
    """The ends of a range as a phrase: ``A to B``, ``from A`` or ``up to B`` (an end that is not declared is empty)."""
    if low and high:
        return f"{low} to {high}"
    return f"from {low}" if low else f"up to {high}"


def _units(column: CompiledColumn) -> Optional[Tuple[Optional[int], Optional[int]]]:
    """The column's declared range in its own units (epoch days, or milliseconds), or ``None``.

    A DATE holds whole days, so the range is the days it holds: the first day at or after the low
    end, the last at or before the high end. That can be empty (``low > high``). An end the contract
    does not declare is ``None``.
    """
    declared = column.declared_range
    if declared is None:
        return None
    low, high = declared
    if column.logical_type.kind is LogicalKind.DATE:
        return (
            None if low is None else -(-low // _DAY_MS),
            None if high is None else high // _DAY_MS,
        )
    return low, high


def _bounded(column: CompiledColumn) -> Optional[Tuple[int, int]]:
    """:func:`_units` with an end that is not declared at :data:`_UNBOUNDED`, for the cut."""
    span = _units(column)
    if span is None:
        return None
    return (-_UNBOUNDED if span[0] is None else span[0], _UNBOUNDED if span[1] is None else span[1])


def _spelled(column: CompiledColumn, value: int) -> str:
    """*value*, in the column's units, as a date or an RFC 3339 timestamp."""
    if column.logical_type.kind is LogicalKind.DATE:
        return date.fromordinal(value + DATE_EPOCH_ORDINAL).isoformat()
    return format_rfc3339_ms(value)


def _refused(
    expose_id: str, column: CompiledColumn, text: Tuple[str, str], source: str
) -> FluxError:
    from flux_engine.generator.relations import FLX_3403

    return FluxError(
        FLX_3403,
        f"{expose_id}.{column.name}: the contract's range {range_phrase(*text)} holds none of "
        f"the dates this column is drawn from ({source}); widen the contract's range, or declare "
        "statistics (or timeBounds) that reach it",
    )


def _scaled(weight: Decimal, factor: int) -> Decimal:
    """*weight* times a whole *factor*, exactly, whatever the ambient decimal context."""
    if factor == 1:
        return weight
    sign, digits, exponent = weight.as_tuple()
    coefficient = int("".join(str(digit) for digit in digits)) * factor
    return Decimal((sign, tuple(int(digit) for digit in str(coefficient)), exponent))  # type: ignore[arg-type]


def _draws_from_bins(statistics: ColumnStatistics) -> bool:
    """Whether the column is drawn from its bins: it declares them and no rate takes the draw instead."""
    if statistics.bins is None or statistics.rate is not None:
        return False
    return (statistics.family or "").lower() not in ("rate", "poisson", "exponential")


def _range_text(column: CompiledColumn) -> Tuple[str, str]:
    """The ends of the column's declared range, spelled as its values are (empty for an end it lacks)."""
    span = _units(column)
    assert span is not None and column.declared_range is not None
    low, high = span
    if low is not None and high is not None and low > high:
        raw = column.declared_range
        assert raw[0] is not None and raw[1] is not None
        return format_rfc3339_ms(raw[0]), format_rfc3339_ms(raw[1])
    return (
        "" if low is None else _spelled(column, low),
        "" if high is None else _spelled(column, high),
    )


def range_text(column: CompiledColumn) -> Optional[Tuple[str, str]]:
    """The ends of *column*'s declared range as its values are spelled, or ``None`` for none."""
    return None if column.declared_range is None else _range_text(column)


def cut_statistics(
    expose_id: str,
    column: CompiledColumn,
    statistics: ColumnStatistics,
    *,
    window: bool = False,
) -> Tuple[ColumnStatistics, Optional[RangeCut]]:
    """*statistics* cut at *column*'s declared range, and what was cut; *statistics* itself when nothing is.

    *window* says the range in *statistics* is the run's time bounds, which the plan filled in for a
    column that declares none: only the message of a refusal differs. A column drawn from bins has
    its bins cut and its ``range`` left as declared, since the draw never reads it; any other column
    has its ``range`` intersected. Raises ``FLX-3403`` when nothing the column is drawn from lies
    inside the contract's range.
    """
    span = _bounded(column)
    if span is None:
        return statistics, None
    low, high = span
    text = _range_text(column)
    if _draws_from_bins(statistics):
        if statistics.bin_weights is None and statistics.bin_weights_given is None:
            return statistics, None  # the plan refuses bins with no weights, in its own words
        cut = _cut_bins(expose_id, column, statistics, low, high)
        return (statistics, None) if cut is None else (cut, RangeCut(*text, "bins"))
    if not statistics.has_range:
        return statistics, None
    assert statistics.range_min is not None and statistics.range_max is not None
    new_low, new_high = max(int(statistics.range_min), low), min(int(statistics.range_max), high)
    if new_low > new_high:
        source = "the run's timeBounds" if window else "its declared range"
        raise _refused(expose_id, column, text, source)
    if (new_low, new_high) == (int(statistics.range_min), int(statistics.range_max)):
        return statistics, None
    narrowed = replace(
        statistics,
        range_min=Decimal(new_low),
        range_max=Decimal(new_high),
        range_min_text=_spelled(column, new_low),
        range_max_text=_spelled(column, new_high),
    )
    return narrowed, RangeCut(*text, "range")


def _cut_bins(
    expose_id: str, column: CompiledColumn, statistics: ColumnStatistics, low: int, high: int
) -> Optional[ColumnStatistics]:
    """*statistics* with its bins cut at ``[low, high]``, or ``None`` when every bin lies inside it."""
    assert statistics.bins is not None
    edges = [int(edge) for edge in statistics.bins]
    count = len(edges) - 1
    # The values a bin holds: from its lower edge to the day before its upper edge, the last bin
    # also its upper edge (it is closed above).
    firsts = edges[:-1]
    lasts = [edges[index + 1] - 1 for index in range(count - 1)] + [edges[-1]]
    kept: List[Tuple[int, int, int]] = []
    for index in range(count):
        first, last = max(firsts[index], low), min(lasts[index], high)
        if first <= last:
            kept.append((index, first, last))
    if not kept:
        raise _refused(expose_id, column, _range_text(column), "its declared bins")
    if len(kept) == count and all(
        (first, last) == (firsts[index], lasts[index]) for index, first, last in kept
    ):
        return None
    _require_weight_kept(expose_id, column, statistics, kept)
    widths = {index: lasts[index] - firsts[index] + 1 for index, _, _ in kept}
    cut = {index: last - first + 1 for index, first, last in kept}
    common = lcm(*(widths[index] for index in widths if cut[index] != widths[index]))
    # A bin that keeps its whole width keeps its weight times *common*; a cut one, in proportion.
    factors = {
        index: common if cut[index] == widths[index] else cut[index] * (common // widths[index])
        for index in widths
    }
    return replace(statistics, **_cut_changes(column, statistics, edges, kept, factors))


def _require_weight_kept(
    expose_id: str,
    column: CompiledColumn,
    statistics: ColumnStatistics,
    kept: Sequence[Tuple[int, int, int]],
) -> None:
    """Refuse (``FLX-3403``) a cut that leaves a weight table with no weight in the bins it keeps."""
    given = statistics.bin_weights_given or {}
    tables: List[Sequence[Decimal]] = (
        [statistics.bin_weights]
        if statistics.bin_weights is not None
        else [given[level] for level in sorted(given)]
    )
    for table in tables:
        if not any(table[index] > 0 for index, _, _ in kept):
            raise _refused(
                expose_id,
                column,
                _range_text(column),
                "its declared bins inside it carry no weight",
            )


def _cut_changes(
    column: CompiledColumn,
    statistics: ColumnStatistics,
    edges: Sequence[int],
    kept: Sequence[Tuple[int, int, int]],
    factors: Mapping[int, int],
) -> Dict[str, Any]:
    """The statistics fields a cut changes: the kept bins' edges and texts, and their weights."""
    new_edges = [kept[0][1], *(edges[index] for index, _, _ in kept[1:]), kept[-1][2]]
    texts = statistics.bin_texts or tuple(str(edge) for edge in statistics.bins or ())
    new_texts = [
        texts[kept[0][0]] if kept[0][1] == edges[kept[0][0]] else _spelled(column, kept[0][1]),
        *(texts[index] for index, _, _ in kept[1:]),
        texts[-1] if kept[-1][2] == edges[-1] else _spelled(column, kept[-1][2]),
    ]
    changes: Dict[str, Any] = {
        "bins": tuple(Decimal(edge) for edge in new_edges),
        "bin_texts": tuple(new_texts),
    }
    if statistics.bin_weights is not None:
        changes["bin_weights"] = tuple(
            _scaled(statistics.bin_weights[index], factors[index]) for index, _, _ in kept
        )
    if statistics.bin_weights_given is not None:
        changes["bin_weights_given"] = {
            level: tuple(_scaled(table[index], factors[index]) for index, _, _ in kept)
            for level, table in statistics.bin_weights_given.items()
        }
    return changes


def refuse_cut_parents(
    expose_id: str,
    plans: Sequence[Any],
) -> None:
    """Refuse a cut of the bins of a column that another column is drawn ``given`` (``FLX-3403``).

    Each class of a column drawn from bins is one of its bins, and the other column's weight tables
    name those classes (ADR 0036 decision 3), so a bin that is dropped or moved would leave the
    tables naming a bin the column cannot draw. *plans* are the column plans of the expose.
    """
    from flux_engine.generator.relations import FLX_3403

    cut_bins = {
        item.column.name: item.range_cut
        for item in plans
        if item.range_cut is not None and item.range_cut.what == "bins"
    }
    if not cut_bins:
        return
    for item in plans:
        for parent in item.statistics.parents:
            if parent in cut_bins:
                held = cut_bins[parent]
                raise FluxError(
                    FLX_3403,
                    f"{expose_id}.{parent}: its declared bins reach beyond the contract's range "
                    f"{range_phrase(held.low, held.high)}, and {item.column.name} is drawn given it, so "
                    "cutting its bins would leave that column's weights naming bins it cannot "
                    "draw; declare bins inside the range, or draw "
                    f"{item.column.name} without it",
                )


def range_cut_notes(expose_id: str, plans: Sequence[Any]) -> Tuple[str, ...]:
    """One sentence for each column of an expose whose statistics were cut at the contract's range.

    Advice only: the cut statistics are in the plan, and the note is in no plan document, digest
    or receipt.
    """
    notes: List[str] = []
    for item in plans:
        held = item.range_cut
        if held is None:
            continue
        reach = "its declared bins reach" if held.what == "bins" else "its declared range reaches"
        how = (
            "so each bin is cut at the range and keeps weight in proportion to the width it keeps"
            if held.what == "bins"
            else "so the range is cut at it"
        )
        notes.append(
            f"note: {expose_id}.{item.column.name} is drawn inside the contract's range "
            f"{range_phrase(held.low, held.high)}: {reach} beyond it, {how}"
        )
    return tuple(notes)


def scale_widened_notes(
    expose_id: str, ranges: Sequence["ColumnRange"], widened: Iterable[str], scale_text: str
) -> Tuple[str, ...]:
    """One sentence for each widened key column of an expose that declares a contract range.

    ``--scale`` above 1 grows a key column's keys past the range the contract declares, so ``verify``
    does not count that range (:func:`flux_engine.stages.scaling.widened_columns`). Advice only.
    """
    held = {item.column: item for item in ranges}
    return tuple(
        f"note: {expose_id}.{name}'s contract range ({held[name].text}) is not checked at "
        f"--scale {scale_text}: the scale widens the column's keys"
        for name in sorted(widened)
        if name in held
    )


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnRange:
    """A column's contract range, as ``verify`` checks the rows of a run against it.

    *kind* is ``"date"``, ``"timestamp"`` or ``"number"``. A date's *low* and *high* are the first and
    last day as ``YYYY-MM-DD`` texts, which compare in date order; a timestamp's are milliseconds
    since the epoch; a number's are exact fractions (``None`` for an end it does not declare), each
    with its strictness. *text* is the range as ``verify`` words it.
    """

    column: str
    kind: str
    text: str
    low: Any = None
    high: Any = None
    low_strict: bool = False
    high_strict: bool = False


def _decimal_text(value: Fraction) -> str:
    """*value*, a fraction a decimal number parsed into, as a plain decimal."""
    places = 0
    while (value * 10**places).denominator != 1:
        places += 1
    units = int(abs(value) * 10**places)
    digits = str(units).zfill(places + 1)
    body = f"{digits[:-places]}.{digits[-places:]}" if places else digits
    return f"-{body}" if value < 0 else body


def declared_ranges_of(
    expose: Mapping[str, Any], compiled: CompiledExpose
) -> Tuple[ColumnRange, ...]:
    """The ranges the contract declares for the columns of *expose*, in column order.

    *expose* is the contract's ``exposes[]`` entry and *compiled* its compiled columns. A DATE or
    TIMESTAMP column's range is the one compile read; an INT or DECIMAL column's is
    :func:`flux_engine.author.ranges.column_range`'s.
    """
    schema = (expose.get("contract") or {}).get("schema") or []
    # Compile names a column in NFC, so the entries are looked up in it too.
    entries = {nfc(str(entry.get("name"))): entry for entry in schema if isinstance(entry, Mapping)}
    found: List[ColumnRange] = []
    for column in compiled.columns:
        kind = column.logical_type.kind
        held: Optional[ColumnRange] = None
        if kind in (LogicalKind.DATE, LogicalKind.TIMESTAMP):
            held = _temporal_range(column, kind)
        elif kind in (LogicalKind.INT, LogicalKind.DECIMAL):
            held = _number_range(column, entries.get(column.name))
        if held is not None:
            found.append(held)
    return tuple(found)


def _temporal_range(column: CompiledColumn, kind: LogicalKind) -> Optional[ColumnRange]:
    """The range compile read for a DATE or TIMESTAMP column, or ``None`` when it declares none.

    Only the ends the contract declares are named, and a strict end is quoted as the contract
    spells it.
    """
    if column.declared_range is None:
        return None
    low_ms, high_ms = column.declared_range
    low_strict, high_strict = column.declared_range_strict
    span = _units(column)
    assert span is not None
    low = None if low_ms is None else _end_of(column, low_ms, span[0] or 0, low_strict, ">")
    high = None if high_ms is None else _end_of(column, high_ms, span[1] or 0, high_strict, "<")
    return ColumnRange(
        column.name,
        "date" if kind is LogicalKind.DATE else "timestamp",
        " and ".join(end[1] for end in (low, high) if end is not None),
        None if low is None else low[0],
        None if high is None else high[0],
    )


def _end_of(
    column: CompiledColumn, instant: int, unit: int, strict: bool, symbol: str
) -> Tuple[Any, str]:
    """One declared end of a date or timestamp range: what ``verify`` compares, and its words.

    *instant* is the end drawn, in milliseconds (a strict end is held one millisecond inward),
    *unit* the same end in the column's units, and *symbol* ``">"`` for the low end or ``"<"`` for
    the high one. A DATE's end compares as its ``YYYY-MM-DD`` text, a TIMESTAMP's as milliseconds;
    a strict end is worded as the contract spells it, one millisecond outward of the end drawn.
    """
    value: Any = _spelled(column, unit) if column.logical_type.kind is LogicalKind.DATE else instant
    if strict:
        return value, f"{symbol} {_named(column, instant + (1 if symbol == '<' else -1))}"
    return value, f"{symbol}= {_spelled(column, unit)}"


def _named(column: CompiledColumn, instant: int) -> str:
    """*instant*, a strict end of a range as the contract names it, spelled as the column's values are.

    A DATE's end that falls on a midnight is a date; any other instant is an RFC 3339 timestamp.
    """
    if column.logical_type.kind is LogicalKind.DATE and instant % _DAY_MS == 0:
        return _spelled(column, instant // _DAY_MS)
    return format_rfc3339_ms(instant)


def _number_range(column: CompiledColumn, entry: Any) -> Optional[ColumnRange]:
    """The range an INT or DECIMAL column's ``range`` rule declares, or ``None`` when it has none."""
    from flux_engine.author.ranges import column_range

    rule = column_range(entry)
    if rule is None or (rule.low is None and rule.high is None):
        return None
    parts: List[str] = []
    if rule.low is not None:
        parts.append(f"{'>' if rule.low_strict else '>='} {_decimal_text(rule.low)}")
    if rule.high is not None:
        parts.append(f"{'<' if rule.high_strict else '<='} {_decimal_text(rule.high)}")
    return ColumnRange(
        column.name,
        "number",
        " and ".join(parts),
        rule.low,
        rule.high,
        rule.low_strict,
        rule.high_strict,
    )


def _outside_dates(values: Sequence[Any], held: ColumnRange) -> int:
    """How many of *values*, ``YYYY-MM-DD`` texts, lie outside a date range (text compares in date order)."""
    return sum(
        1
        for value in values
        if isinstance(value, str) and len(value) == 10 and _beyond(value, held.low, held.high)
    )


def _outside_timestamps(values: Sequence[Any], held: ColumnRange) -> int:
    """How many of *values*, RFC 3339 texts, lie outside a timestamp range.

    A canonical ``YYYY-MM-DDTHH:MM:SS.mmmZ`` text compares in time order as text, so only another
    spelling is parsed; a value that is not a timestamp text is not counted here.
    """
    low = None if held.low is None else int(held.low)
    high = None if held.high is None else int(held.high)
    low_text = None if low is None else format_rfc3339_ms(low)
    high_text = None if high is None else format_rfc3339_ms(high)
    outside = 0
    for value in values:
        if not isinstance(value, str):
            continue
        if len(value) == 24 and value.endswith("Z"):
            beyond = _beyond(value, low_text, high_text)
        else:
            moment = _moment(value)
            beyond = moment is not None and _beyond(moment, low, high)
        outside += 1 if beyond else 0
    return outside


def _beyond(value: Any, low: Any, high: Any) -> bool:
    """Whether *value* lies below *low* or above *high*; an end that is ``None`` bounds nothing."""
    return (low is not None and value < low) or (high is not None and value > high)


def _moment(value: str) -> Optional[int]:
    """*value*, an RFC 3339 text, in milliseconds since the epoch, or ``None`` when it is not one."""
    try:
        return parse_rfc3339_ms(value)
    except FluxError:
        return None


def _cell(value: Any) -> Optional[Decimal]:
    """*value*, a cell of a number column, as a finite ``Decimal``, or ``None`` when it is not a number.

    ``Decimal`` keeps the text's own exponent, so a cell such as ``1e999999999`` costs nothing to
    read or to compare. A cell that is NaN, infinite or not a number at all reads as ``None``.
    """
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float, str)):
            number = Decimal(value)
        else:
            return None
    except (ArithmeticError, ValueError):
        return None
    return number if number.is_finite() else None


def _bound(value: Optional[Fraction]) -> Optional[Decimal]:
    """A range bound, a decimal of at most ``LONGEST_NUMBER`` characters, as the ``Decimal`` it equals."""
    if value is None:
        return None
    from flux_engine.author.ranges import LONGEST_NUMBER

    # Twice the longest bound's digits leaves room for any decimal the grammar reads; an inexact
    # quotient is a defect, so it raises instead of rounding a bound.
    context = Context(prec=2 * LONGEST_NUMBER + 50, Emax=MAX_EMAX, Emin=MIN_EMIN, traps=[Inexact])
    return context.divide(Decimal(value.numerator), Decimal(value.denominator))


def _outside_numbers(values: Sequence[Any], held: ColumnRange) -> int:
    """How many of *values* lie outside a number range; a cell that is not a number is outside.

    Cells are compared as ``Decimal`` against the bounds as ``Decimal``: a comparison orders two
    values by their exponents first and never expands one, so a tampered cell cannot cost more than
    reading it. A null cell (``None``) is no value and is skipped.
    """
    low, high = _bound(held.low), _bound(held.high)
    outside = 0
    for value in values:
        if value is None:
            continue
        number = _cell(value)
        if number is None:
            outside += 1
        elif low is not None and (number < low or (held.low_strict and number == low)):
            outside += 1
        elif high is not None and (number > high or (held.high_strict and number == high)):
            outside += 1
    return outside


def range_problems(
    expose_id: str,
    names: Sequence[str],
    rows: Sequence[Sequence[Any]],
    ranges: Sequence[ColumnRange],
) -> List[str]:
    """One sentence for each column of *ranges* that holds rows outside its range, in column order.

    *names* are the columns of *rows*, in order; a range on a column the rows do not hold (a hidden
    column) is not checked. The sentence names the expose, the column, how many of the rows lie
    outside and the range.
    """
    position = {name: index for index, name in enumerate(names)}
    problems: List[str] = []
    for held in ranges:
        index = position.get(held.column)
        if index is None:
            continue
        values = [row[index] for row in rows if len(row) > index]
        if held.kind == "date":
            outside = _outside_dates(values, held)
        elif held.kind == "timestamp":
            outside = _outside_timestamps(values, held)
        else:
            outside = _outside_numbers(values, held)
        if outside:
            problems.append(
                f"{expose_id}.{held.column}: {outside} of {len(rows)} rows are outside the "
                f"declared range ({held.text})"
            )
    return problems
