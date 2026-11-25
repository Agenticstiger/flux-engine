# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The grammar of a contract column's ``range`` ``validationRules``, for a date or timestamp column.

:func:`declared_instants` reads a date or timestamp column's ``range`` ``validationRules`` with the
spelling the FLUID profiler bins a declared date range with: ``between 'A' and 'B'``, or ``>= 'A'
and <= 'B'`` (``>`` and ``<`` too), each value an ISO date (midnight UTC) or an RFC 3339 timestamp
(UTC when it has no offset), in single or double quotes or bare. Several rules tighten each side.
The range is read only when both ends are declared and the low end lies before the high end (a
fraction of a millisecond is floored); a range that holds no instant declares nothing here;
anything else declares nothing. A range whose high end is at or after :data:`WINDOW_END_LIMIT_MS`
says only that the column holds a date, so it declares nothing either: a run's periods step past
the end of its window, and a date in the year 10000 is out of range. The range only feeds the
run's window: the column's own declaration does not change.

:func:`declared_range_bounds` is the sibling a run reads (ADR 0024, the amendment of 2026-11-25): each end
is optional (``>= '2024-01-01'`` is a range), a high end in the year 9999 or later is the open-end
spelling and so no high end, and each end says whether it is strict. :func:`declared_instants` is
built on it and keeps its rule, which ``init`` reads for its window.

This module is in the kernel's ``seam`` package so reading a contract's columns can read a range without
importing the authoring layer above it; :mod:`flux_engine.author.window` and
:mod:`flux_engine.author.ranges` re-export what they used to define.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Mapping, NamedTuple, Optional, Tuple

__all__ = [
    "WINDOW_END_LIMIT_MS",
    "RangeBounds",
    "and_parts",
    "declared_range_bounds",
    "declared_instants",
    "instant_ms",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FIRST_MS = (datetime(1, 1, 1, tzinfo=timezone.utc) - _EPOCH) // timedelta(milliseconds=1)
_LAST_MS = (datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc) - _EPOCH) // timedelta(
    milliseconds=1
)

#: The first instant (9999-01-01T00:00:00Z) a run's window may not reach. A run's periods step past
#: the window's end, and a date in the year 10000 is out of range.
WINDOW_END_LIMIT_MS = (datetime(9999, 1, 1, tzinfo=timezone.utc) - _EPOCH) // timedelta(
    milliseconds=1
)

_BOUND = re.compile(r"""^\s*(>=|<=|>|<)\s*(?:'([^']*)'|"([^"]*)"|(\S+))\s*$""")
_BETWEEN = re.compile(
    r"""^\s*between\s+(?:'([^']*)'|"([^"]*)"|(\S+))\s+and\s+(?:'([^']*)'|"([^"]*)"|(\S+))\s*$""",
    re.IGNORECASE,
)


def and_parts(text: str) -> List[str]:
    """*text* cut at each word ``and``, in any case, in one pass over its words.

    The same parts as splitting at whitespace, ``and``, whitespace; a regular
    expression for that backtracks over a long run of spaces, and a contract
    can hold one.
    """
    parts: List[str] = []
    words: List[str] = []
    for word in text.split():
        if word.lower() == "and":
            parts.append(" ".join(words))
            words = []
        else:
            words.append(word)
    parts.append(" ".join(words))
    return parts


def _moment(text: Any) -> Optional[datetime]:
    """The UTC instant of an ISO date or RFC 3339 timestamp, or ``None`` for text that is neither."""
    if not isinstance(text, str):
        return None
    clean = text.strip()
    try:
        if len(clean) == 10:
            moment = datetime.combine(date.fromisoformat(clean), datetime.min.time())
        else:
            moment = datetime.fromisoformat(clean.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def instant_ms(text: Any) -> Optional[int]:
    """Milliseconds since the epoch of an ISO date or RFC 3339 timestamp, or ``None``.

    A date is midnight UTC and a timestamp with no offset is UTC. Text that is neither, and an
    instant outside the years 1 to 9999 in UTC, is not read. A fraction of a millisecond is floored.
    """
    moment = _moment(text)
    if moment is None:
        return None
    try:
        milliseconds = (moment - _EPOCH) // timedelta(milliseconds=1)
    except OverflowError:
        return None
    if not _FIRST_MS <= milliseconds <= _LAST_MS:
        return None
    return milliseconds


def _between_milliseconds(text: Any) -> bool:
    """Whether *text* names an instant between two whole milliseconds, which :func:`instant_ms` floors."""
    moment = _moment(text)
    return moment is not None and moment.microsecond % 1000 != 0


class RangeBounds(NamedTuple):
    """A column's declared date range: each end is optional, with whether it is strict.

    ``>`` and ``<`` are strict ends. An end the contract does not declare, and a high end in the
    year 9999 or later (the open-end spelling, :data:`WINDOW_END_LIMIT_MS`), is ``None``.
    """

    low: Optional[int]
    high: Optional[int]
    low_strict: bool = False
    high_strict: bool = False


def _tighter(
    held: Optional[Tuple[int, bool]], milliseconds: int, strict: bool, *, above: bool
) -> Tuple[int, bool]:
    """The end that holds when *held* and a new bound both apply: the higher low end, the lower high end.

    At the same instant a strict bound is the tighter one.
    """
    if held is None:
        return milliseconds, strict
    kept, kept_strict = held
    if milliseconds == kept:
        return kept, kept_strict or strict
    return (milliseconds, strict) if (milliseconds > kept) == above else (kept, kept_strict)


class _End(NamedTuple):
    """One end a rule sets: which side, the millisecond, and whether it is strict."""

    low: bool
    milliseconds: int
    strict: bool


def _end_of(operator: str, text: str, *, round_low_up: bool) -> Optional[_End]:
    """The end one spelled bound sets, or ``None`` when its instant is not read."""
    milliseconds = instant_ms(text)
    if milliseconds is None:
        return None
    low, strict = operator in (">", ">="), operator in (">", "<")
    if round_low_up and _between_milliseconds(text):
        # A low end's next whole millisecond is already beyond the instant, strict or not; a
        # high end's millisecond at or below it is already short of the instant, strict or not.
        milliseconds, strict = (milliseconds + 1 if low else milliseconds), False
    return _End(low, milliseconds, strict)


def _rule_ends(constraint: str, *, round_low_up: bool) -> Optional[List[_End]]:
    """The ends one ``range`` rule's text sets, or ``None`` when the text is not read."""
    between = _BETWEEN.match(constraint)
    if between:
        first = next(text for text in between.groups()[:3] if text is not None)
        second = next(text for text in between.groups()[3:] if text is not None)
        spelled = [(">=", first), ("<=", second)]
    else:
        spelled = []
        for part in and_parts(constraint):
            bound = _BOUND.match(part)
            if not bound:
                return None
            spelled.append((bound.group(1), next(i for i in bound.groups()[1:] if i is not None)))
    ends: List[_End] = []
    for operator, text in spelled:
        end = _end_of(operator, text, round_low_up=round_low_up)
        if end is None:
            return None
        ends.append(end)
    return ends


def _range_constraints(column: Any) -> List[str]:
    """The text of each ``range`` rule of a schema column."""
    rules = column.get("validationRules") if isinstance(column, Mapping) else None
    return [
        rule["constraint"]
        for rule in (rules if isinstance(rules, list) else ())
        if isinstance(rule, Mapping)
        and str(rule.get("type") or "").lower() == "range"
        and isinstance(rule.get("constraint"), str)
    ]


def _column_ends(column: Any, *, round_low_up: bool) -> Optional[List[_End]]:
    """The ends every ``range`` rule of a column sets, or ``None`` when a rule's text is not read."""
    found: List[_End] = []
    for constraint in _range_constraints(column):
        ends = _rule_ends(constraint, round_low_up=round_low_up)
        if ends is None:
            return None
        found.extend(ends)
    return found


def _bounds_of(
    low: Optional[Tuple[int, bool]], high: Optional[Tuple[int, bool]]
) -> Optional[RangeBounds]:
    """The :class:`RangeBounds` of the tightest low and high ends, or ``None`` when neither is declared."""
    if high is not None and high[0] >= WINDOW_END_LIMIT_MS:
        high = None
    if low is None and high is None:
        return None
    return RangeBounds(
        None if low is None else low[0],
        None if high is None else high[0],
        low is not None and low[1],
        high is not None and high[1],
    )


def _read_bounds(column: Any, *, round_low_up: bool) -> Optional[RangeBounds]:
    """:func:`declared_range_bounds`, with the low end's sub-millisecond fraction rounded up or floored."""
    ends = _column_ends(column, round_low_up=round_low_up)
    if ends is None:
        return None
    low: Optional[Tuple[int, bool]] = None
    high: Optional[Tuple[int, bool]] = None
    for end in ends:
        if end.low:
            low = _tighter(low, end.milliseconds, end.strict, above=True)
        else:
            high = _tighter(high, end.milliseconds, end.strict, above=False)
    return _bounds_of(low, high)


def declared_range_bounds(column: Any) -> Optional[RangeBounds]:
    """The range a date or timestamp column's ``range`` rules declare, each end optional, or ``None``.

    The grammar is :func:`declared_instants`', but one end is enough (``>= '2024-01-01'``), and a
    high end in the year 9999 or later is no high end, so ``between '2024-01-01' and '9999-12-31'``
    is ``>= '2024-01-01'``. ``None`` for a rule that is not read and for a range with neither end. A
    range that holds no instant is still a range: its low end lies after its high end, or its ends
    meet and one is strict (the run refuses it, ``FLX-3403``); an inclusive range whose ends meet
    holds that one instant. An end is strict when the rule that sets it is ``>`` or ``<``: the range holds the instants beyond it and not the instant itself. An end
    between two milliseconds is read to the millisecond: a low end rounds up, so the range never holds
    an instant below it, and a high end rounds down, so it never holds one above it; a fraction
    makes either end inclusive.
    """
    return _read_bounds(column, round_low_up=True)


def declared_instants(column: Any) -> Optional[Tuple[int, int]]:
    """The ``(low, high)`` milliseconds a date or timestamp column's ``range`` rules declare, or ``None``.

    ``None`` unless both ends are declared, each an instant that is read, and the low end lies
    before the high end; also ``None`` when the high end is at or after
    :data:`WINDOW_END_LIMIT_MS`, since such a range gives the window nothing. A strict end is read
    as the instant itself, as the profiler lays out bin edges; :func:`declared_range_bounds` says which
    ends are strict, and reads a range with one end as well.
    """
    bounds = _read_bounds(column, round_low_up=False)
    if bounds is None or bounds.low is None or bounds.high is None or bounds.low >= bounds.high:
        return None
    return bounds.low, bounds.high
