# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The facts over dates, scored on a run's written rows (ADR 0024 section 8).

Each :class:`~flux_engine.seam.date_lines.DateLine` is counted as the share of the rows it covers
that hold it, with the units the ADR fixes: a lifetime includes its last day, a DATE start bounds a
TIMESTAMP from its midnight UTC, a DATE end bounds it up to the end of that day, and a TIMESTAMP
bounds a DATE by the day that holds it.

* ``lifetime``: on the rows where the start and the end are both known, the end is not before the
  start.
* ``date``: with ``p`` the row the line's path reaches (each hop followed through the written rows'
  foreign keys), ``p.start <= c``, and ``c <= p.end`` unless ``p``'s lifetime is open. It covers the
  rows whose path reaches a row whose start is not NULL and whose own ``c`` is not NULL.
* ``period``: the emit's own start is within ``p``'s lifetime as above, and when ``p`` has ended the
  emit's end is not NULL and not after ``p.end``. It covers the rows whose path reaches a row whose
  start is not NULL and whose own start is not NULL.

``p``'s lifetime is open when its lifetime declares no end, when ``p.end`` is NULL, or when
``p.end < p.start``. Only covered rows count, in the numerator and the denominator.

A line with no target is enforced: a violating covered row fails the run. A line with a target is
checked against it: the violating share of the covered rows may not exceed the target by more than
the allowance, which is the fact's tolerance when a ``facts`` entry gives one and otherwise four
standard deviations of the share a draw at that target makes over that many rows
(:func:`sampling_allowance`), in exact integers and fractions. A share below the target is not a
miss: a violation that finds no value outside the interval keeps one that holds (ADR 0024 section
7), and the implied outer level of a chain, whose target is the sum of its levels', is an upper bound,
since a row that violates one level may still hold another. This module reads the rows a run
wrote and never generates any.
"""

from __future__ import annotations

from datetime import date, datetime
from fractions import Fraction
from math import isqrt
from typing import Any, List, Optional, Sequence, Tuple

from flux_engine.concordance.rule_shares import FORM_CHECK, ORIGIN_BUNDLE, RuleShare
from flux_engine.concordance.shares import Holds
from flux_engine.expr2.rows import Rows
from flux_engine.seam.date_lines import DateLine

__all__ = [
    "at_or_after_start",
    "at_or_before_end",
    "date_shares",
    "is_open",
    "sampling_allowance",
]

_SIGMAS_SQUARED = 16
_SCALE = 10**6
_DAY_MICROS = 86_400_000_000
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()


def _days(value: date) -> int:
    """The whole days from 1970-01-01 to the date *value* (its own day)."""
    return value.toordinal() - _EPOCH_ORDINAL


def _micros(value: datetime) -> int:
    """A timestamp as integer microseconds from the epoch in UTC; a timestamp with no zone is UTC.

    Integer arithmetic, so a timestamp at either end of the calendar range never overflows by
    being moved to UTC.
    """
    naive = _days(value) * _DAY_MICROS + (
        ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond
    )
    offset = value.utcoffset()
    if offset is None:
        return naive
    return naive - (offset.days * _DAY_MICROS + offset.seconds * 1_000_000 + offset.microseconds)


def _day(value: Any) -> int:
    """The day number that holds *value*: a date is its own day."""
    if isinstance(value, datetime):
        return _micros(value) // _DAY_MICROS
    return _days(value)


def _midnight(day: date) -> int:
    return _days(day) * _DAY_MICROS


def at_or_after_start(value: Any, start: Any) -> bool:
    """Whether *value* is not before the lifetime start *start*, in the units of ADR 0024 section 5.

    A DATE *value* is compared by days (a TIMESTAMP start is the day that holds it); a TIMESTAMP
    *value* is compared as an instant (a DATE start is its midnight UTC).
    """
    if isinstance(value, datetime):
        bound = _micros(start) if isinstance(start, datetime) else _midnight(start)
        return _micros(value) >= bound
    return _day(start) <= _days(value)


def at_or_before_end(value: Any, end: Any) -> bool:
    """Whether *value* is not after the lifetime end *end*: a DATE end holds up to the end of that day.

    The bounds are integer microseconds, so a DATE end of 9999-12-31 has a last instant and no
    day after it is built.
    """
    if isinstance(value, datetime):
        if isinstance(end, datetime):
            return _micros(value) <= _micros(end)
        return _micros(value) <= _midnight(end) + _DAY_MICROS - 1
    return _days(value) <= _day(end)


def is_open(start: Any, end: Any) -> bool:
    """Whether a lifetime is open: no end, or an end before its start (which reads as open)."""
    return end is None or not at_or_after_start(end, start)


def sampling_allowance(target: Fraction, covered: int) -> Fraction:
    """The gap between a violating share and its target that a draw at the target stays within.

    Four standard deviations of the share ``target/100`` makes over *covered* rows, taken as an
    integer square root rounded up to a millionth, so a target of zero allows no violating row once
    *covered* is under a million, and the allowance never rests on a float.
    """
    if covered <= 0:
        return Fraction(0)
    share = target / 100
    variance = share * (1 - share) / covered
    scaled = int(_SIGMAS_SQUARED * variance * _SCALE * _SCALE)
    return Fraction(isqrt(scaled) + 1, _SCALE)


def _follow(view: Any, path: Sequence[str]) -> Optional[Any]:
    here = view
    for name in path:
        here = here.follow(name)
        if here is None:
            return None
    return here


def _lifetime_holds(line: DateLine, rows: Rows) -> Holds:
    covered = satisfied = 0
    for view in rows.rows(line.emit):
        start, end = view.get(line.own_start), view.get(line.own_end or "")
        if start is None or end is None:
            continue
        covered += 1
        if at_or_after_start(end, start):
            satisfied += 1
    return Holds(satisfied, covered)


def _within_row(line: DateLine, view: Any) -> Optional[bool]:
    """Whether one row holds a ``within`` line, or ``None`` when the row is not covered.

    A row is covered when its own date, its parent along the path and the parent's start are all
    present. A date before the parent's start does not hold; an open parent holds; otherwise the
    date (or, for a lifetime, the row's own end) must not pass the parent's end.
    """
    own = view.get(line.own_start if line.column is None else line.column)
    if own is None:
        return None
    parent = _follow(view, line.path)
    if parent is None:
        return None
    start = parent.get(line.parent_start)
    if start is None:
        return None
    if not at_or_after_start(own, start):
        return False
    end = parent.get(line.parent_end) if line.parent_end is not None else None
    if is_open(start, end):
        return True
    if line.kind == "date":
        return bool(at_or_before_end(own, end))
    own_end = view.get(line.own_end) if line.own_end is not None else None
    return own_end is not None and bool(at_or_before_end(own_end, end))


def _within_holds(line: DateLine, rows: Rows) -> Holds:
    covered = satisfied = 0
    for view in rows.rows(line.emit):
        holds = _within_row(line, view)
        if holds is None:
            continue
        covered += 1
        satisfied += holds
    return Holds(satisfied, covered)


def date_shares(
    lines: Sequence[DateLine], rows: Rows, *, generated: bool = True
) -> Tuple[RuleShare, ...]:
    """The share of rows holding each line, in the order of *lines*.

    *rows* holds the stored rows of every expose the lines read (:attr:`DateLine.tables`).
    *generated* is whether the run generated the emit under its declarations, which is whether a
    line without a target is enforced.
    """
    found: List[RuleShare] = []
    for line in lines:
        holds = (
            _lifetime_holds(line, rows) if line.kind == "lifetime" else _within_holds(line, rows)
        )
        allowed: Optional[Fraction] = None
        if line.target is not None:
            allowed = (
                line.tolerance
                if line.tolerance is not None
                else sampling_allowance(line.target, holds.total)
            )
        found.append(
            RuleShare(
                expose_id=line.emit,
                rule_id=line.rule_id,
                form=FORM_CHECK,
                origin=ORIGIN_BUNDLE,
                holds=holds,
                enforced=generated,
                target=line.target,
                allowed=allowed,
            )
        )
    return tuple(found)
