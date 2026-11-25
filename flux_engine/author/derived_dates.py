# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A date the profile gives no distribution is drawn where the contract's derives over it hold.

ADR 0014, amendment of 2026-11-25. A DATE column whose profile entry withholds every order
statistic (or whose released histogram is noise everywhere) is drawn evenly within the run's
window. When a contract ``derive`` sets a column from it as a whole-unit difference from a constant
date (``DAYS_BETWEEN``, ``MONTHS_BETWEEN`` or ``YEARS_BETWEEN``, in either argument order), and the
derived column declares a ``range`` rule, the dates that keep the derived value inside that range
are the draw's bounds: an even draw over the window would write the derived column outside its
range on most rows.

The bounds are the preimage of the range under the difference. The difference is a monotone step
function of the date (non-decreasing when the constant comes first, non-increasing when it comes
last), so each end is a bisection over the day ordinals with the engine's own evaluator, and the
two ends are then checked with it: the difference lies inside the range at each end, and the day
beside each end does not. The engine's semantics for a month difference (the day of the month kept
or clamped to the month's last day) decide, never a formula written here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from typing import Any, Callable, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.author.columns import ColumnPlan, declare_one_bin
from flux_engine.author.ranges import RangeRule, column_range, grid_bounds
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.expr2 import Side, check, parse
from flux_engine.expr2.ast import Call, Column, Date
from flux_engine.expr2.check import VALUE
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.rows import Rows
from flux_engine.expr2.scope import model_from_json
from flux_engine.seam.contract import LogicalKind

__all__ = ["DateDifference", "bound_derived_dates", "date_difference", "preimage"]

_DAY_MS = 86_400_000
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_DIFFERENCES = ("DAYS_BETWEEN", "MONTHS_BETWEEN", "YEARS_BETWEEN")
_FIRST_DAY, _LAST_DAY = date.min.toordinal(), date.max.toordinal()
_NUMBERS = (LogicalKind.INT, LogicalKind.DECIMAL)

#: The days (ordinals) a bound holds, each end ``None`` when that side is open.
Span = Tuple[Optional[int], Optional[int]]


@dataclass(frozen=True)
class DateDifference:
    """A derive's text read as a whole-unit difference between a constant date and one column."""

    function: str
    constant: date
    column: str
    #: The constant is the first argument, so the difference grows with the column.
    constant_first: bool


def date_difference(text: str) -> Optional[DateDifference]:
    """*text* as ``<UNIT>_BETWEEN`` of a date literal and a column of the row, or ``None``.

    The engine's parser reads it, so a text it refuses, or one of any other shape (a sum, a
    rounding, a column of another row), is not a date difference.
    """
    try:
        tree = parse(text, side=Side.CORE).tree
    except Expr2Error:
        return None
    if not isinstance(tree, Call) or tree.name.upper() not in _DIFFERENCES or len(tree.args) != 2:
        return None
    first, second = tree.args
    for literal, named, constant_first in ((first, second, True), (second, first, False)):
        if isinstance(literal, Date) and isinstance(named, Column) and named.relationship is None:
            return DateDifference(
                tree.name.upper(), date.fromisoformat(literal.value), named.name, constant_first
            )
    return None


def _difference_at(text: str, column: str) -> Optional[Callable[[int], Optional[Fraction]]]:
    """The engine's evaluation of *text* for a row holding the day with a given ordinal."""
    model = model_from_json({"exposes": {"e": {"columns": {column: {"type": "date"}}}}})
    try:
        checked = check(parse(text, side=Side.CORE), model, "e", position=VALUE)
    except Expr2Error:
        return None

    def at(ordinal: int) -> Optional[Fraction]:
        try:
            row = Rows(model, {"e": [{column: date.fromordinal(ordinal)}]}).row("e", 0)
            value = checked.evaluate(row)
        except (ValueError, OverflowError):
            return None
        return None if value is None else Fraction(value)

    return at


def _first_true(holds: Callable[[int], bool]) -> Optional[int]:
    """The first day ordinal where a predicate that goes from false to true holds, or ``None``."""
    if not holds(_LAST_DAY):
        return None
    low, high = _FIRST_DAY, _LAST_DAY
    while low < high:
        middle = (low + high) // 2
        if holds(middle):
            high = middle
        else:
            low = middle + 1
    return low


def _last_true(holds: Callable[[int], bool]) -> Optional[int]:
    """The last day ordinal where a predicate that goes from true to false holds, or ``None``."""
    if not holds(_FIRST_DAY):
        return None
    low, high = _FIRST_DAY, _LAST_DAY
    while low < high:
        middle = (low + high + 1) // 2
        if holds(middle):
            low = middle
        else:
            high = middle - 1
    return low


def preimage(
    text: str, found: DateDifference, low: Optional[Fraction], high: Optional[Fraction]
) -> Optional[Span]:
    """The days whose difference lies in ``[low, high]`` (``None`` is an open end), or ``None``.

    ``None`` too when no day does, and when the evaluator disagrees with the ends found (the
    difference at an end is outside the range, or the day beside it inside): the caller then
    keeps the column's draw.
    """
    at = _difference_at(text, found.column)
    if at is None:
        return None

    def reaches(ordinal: int) -> bool:
        value = at(ordinal)
        return value is not None and (low is None or value >= low)

    def stays(ordinal: int) -> bool:
        value = at(ordinal)
        return value is not None and (high is None or value <= high)

    def inside(ordinal: int) -> bool:
        return reaches(ordinal) and stays(ordinal)

    first: Optional[int]
    last: Optional[int]
    if found.constant_first:
        # Non-decreasing in the date: at least ``low`` from some day on, at most ``high`` up to one.
        first = _FIRST_DAY if low is None else _first_true(reaches)
        last = _LAST_DAY if high is None else _last_true(stays)
    else:
        first = _FIRST_DAY if high is None else _first_true(stays)
        last = _LAST_DAY if low is None else _last_true(reaches)
    if first is None or last is None or first > last:
        return None
    if not (inside(first) and inside(last)):
        return None
    if (first > _FIRST_DAY and inside(first - 1)) or (last < _LAST_DAY and inside(last + 1)):
        return None
    return (None if first == _FIRST_DAY else first, None if last == _LAST_DAY else last)


def _intersect(spans: Sequence[Span]) -> Optional[Span]:
    """The days every span holds, or ``None`` when none is."""
    firsts = [span[0] for span in spans if span[0] is not None]
    lasts = [span[1] for span in spans if span[1] is not None]
    first = max(firsts) if firsts else None
    last = min(lasts) if lasts else None
    if first is not None and last is not None and first > last:
        return None
    return first, last


def _day_span(low_ms: int, high_ms: int) -> Span:
    """The whole days inside ``[low_ms, high_ms)``, as ordinals."""
    return (
        -(-low_ms // _DAY_MS) + _EPOCH_ORDINAL,
        -(-high_ms // _DAY_MS) - 1 + _EPOCH_ORDINAL,
    )


def _ms(ordinal: int) -> int:
    return (ordinal - _EPOCH_ORDINAL) * _DAY_MS


def _number(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else str(float(value))


def _range_words(low: Optional[Fraction], high: Optional[Fraction]) -> str:
    if low is not None and high is not None:
        return f"between {_number(low)} and {_number(high)}"
    if low is not None:
        return f"at least {_number(low)}"
    return f"at most {_number(high)}" if high is not None else "anywhere"


def _spelled(span: Span) -> str:
    def day(ordinal: Optional[int], open_word: str) -> str:
        return open_word if ordinal is None else date.fromordinal(ordinal).isoformat()

    return f"{day(span[0], 'the earliest date')} to {day(span[1], 'the latest date')}"


@dataclass(frozen=True)
class _Bound:
    """What one derive says of the column it reads."""

    #: The days the derived column's range allows, ``None`` when no day does.
    span: Optional[Span]
    #: The derive and its range, as the report says them.
    sentence: str
    #: Why no day holds the range, when ``span`` is ``None``.
    reason: str = ""


def _schema_entry(entry: Mapping[str, Any], name: str) -> Optional[Mapping[str, Any]]:
    contract = entry.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    for column in schema if isinstance(schema, list) else ():
        if isinstance(column, Mapping) and column.get("name") == name:
            return column
    return None


def _invariant_id(entry: Mapping[str, Any], index: int) -> str:
    contract = entry.get("contract")
    invariants = contract.get("invariants") if isinstance(contract, Mapping) else None
    found = invariants[index] if isinstance(invariants, list) and index < len(invariants) else None
    name = found.get("id") if isinstance(found, Mapping) else None
    return f"{name}: " if isinstance(name, str) else ""


def _bound_of(
    entry: Mapping[str, Any], derived: str, index: int, text: str, column: str
) -> Optional[_Bound]:
    """The days one derive of *column* allows, or ``None`` when the derive says nothing of it."""
    found = date_difference(text)
    if found is None or found.column != column:
        return None
    schema = _schema_entry(entry, derived)
    rule: Optional[RangeRule] = column_range(schema) if schema is not None else None
    if rule is None:
        return None
    grid = grid_bounds(rule, Fraction(1))
    if grid is None:
        return None
    low, high = grid
    sentence = f"{_invariant_id(entry, index)}{text}, with {derived} {_range_words(low, high)}"
    span = preimage(text, found, low, high)
    if span is None:
        return _Bound(None, sentence, f"no date keeps {derived} {_range_words(low, high)}")
    return _Bound(span, sentence)


def bound_derived_dates(
    exposes: Sequence[Any],
    contract: Mapping[str, Any],
    window: Tuple[str, str],
    ranges_of: Callable[[Any, ColumnPlan], List[Tuple[int, int]]],
) -> None:
    """Declare each date of *exposes* that the contract's derives bound, as one bin over the days.

    Only a DATE column drawn evenly within the window (no distribution from the profile, or a
    histogram that is noise everywhere) that no rule sets is read. *ranges_of* gives the ranges
    (milliseconds) the column declares, which cut the bound. The bin is the bound cut to the run's
    window; a bound that misses the window is drawn as it stands, since the contract's range
    decides. A bound the column's own ranges leave empty changes nothing but the report.
    """
    listed = contract.get("exposes") if isinstance(contract, Mapping) else None
    if not isinstance(listed, list):
        return
    span = tuple(parse_rfc3339_ms(text) for text in window)
    for expose in exposes:
        if expose.skipped is not None or expose.reference or not expose.derives:
            continue
        entry = listed[expose.index] if expose.index < len(listed) else None
        if not isinstance(entry, Mapping):
            continue
        set_by_rules: Set[str] = (
            set(expose.derives) | set(expose.copies) | expose.dependents | expose.aggregated
        ) | expose.guarded
        for plan in expose.columns:
            if expose.kinds.get(plan.name) is not LogicalKind.DATE or plan.name in set_by_rules:
                continue
            if not (plan.even_in_window or plan.drawn_in_window):
                continue
            bounds = [
                found
                for derived, (index, text) in expose.derives.items()
                if expose.kinds.get(derived) in _NUMBERS
                for found in [_bound_of(entry, derived, index, text, plan.name)]
                if found is not None
            ]
            if bounds:
                _declare(expose, plan, bounds, (span[0], span[1]), ranges_of(expose, plan))


def _declare(
    expose: Any,
    plan: ColumnPlan,
    bounds: Sequence[_Bound],
    window: Tuple[int, int],
    declared: Sequence[Tuple[int, int]],
) -> None:
    """Write the bin the bounds leave, or say why the column keeps its draw."""
    sentences = "; ".join(item.sentence for item in bounds)
    empty = next((item for item in bounds if item.span is None), None)
    if empty is not None:
        _keep(expose, plan, f"{empty.reason} ({empty.sentence})")
        return
    spans = [item.span for item in bounds if item.span is not None]
    spans += [_day_span(*held) for held in declared]
    if plan.published_range is not None:
        spans.append(_day_span(int(plan.published_range[0]), int(plan.published_range[1])))
    bound = _intersect(spans)
    if bound is None:
        _keep(
            expose,
            plan,
            f"no date is inside the range of every derive ({sentences}) and the column's own range; "
            "change a derive or a range",
        )
        return
    in_window = _intersect([bound, _day_span(*window)])
    met = in_window is not None
    where = in_window if in_window is not None else bound
    if where[0] is None or where[1] is None:
        _keep(
            expose,
            plan,
            f"({sentences}) reach dates from {_spelled(bound)}, which have no end to draw to "
            "and none inside the run's window; give the column a range or the run a window",
        )
        return
    if where[0] == where[1]:
        _keep(
            expose,
            plan,
            f"({sentences}) leave one date, {_spelled(where)[:10]}, and a bin needs a width",
        )
        return
    note = "" if met else " (outside the run's window: the contract's range decides)"
    reason = (
        f"The profile gives it no distribution, and the contract derives from it ({sentences}), "
        f"so it is drawn evenly over the dates that keep the derived column inside its range, "
        f"{_spelled(where)}{note}."
    )
    declare_one_bin(plan, LogicalKind.DATE, _ms(where[0]), _ms(where[1]), reason)
    plan.how = f"evenly over the dates its derives allow, {_spelled(where)}{note}"
    _say(expose, plan, reason)


def _say(expose: Any, plan: ColumnPlan, sentence: str) -> None:
    """Record an assumption of the column on its plan and on the expose's list the report reads."""
    plan.assumptions.append(sentence)
    expose.assumptions.append((plan.name, sentence))


def _keep(expose: Any, plan: ColumnPlan, reason: str) -> None:
    """The column keeps its even draw; the report says why its derives are not honoured."""
    _say(
        expose,
        plan,
        "The contract's derives over it are not honoured, so it keeps its even draw and a "
        f"derived column may fall outside its range on some rows: {reason}.",
    )
