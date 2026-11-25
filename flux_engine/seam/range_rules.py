# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The grammar of a contract column's ``range`` ``validationRules``, for a date or timestamp column.

:func:`declared_instants` reads a date or timestamp column's ``range`` ``validationRules`` with the
spelling the FLUID profiler bins a declared date range with: ``between 'A' and 'B'``, or ``>= 'A'
and <= 'B'`` (``>`` and ``<`` too), each value an ISO date (midnight UTC) or an RFC 3339 timestamp
(UTC when it has no offset), in single or double quotes or bare. Several rules tighten each side.
The range is read only when both ends are declared and the low end lies before the high end;
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


def instant_ms(text: Any) -> Optional[int]:
    """Milliseconds since the epoch of an ISO date or RFC 3339 timestamp, or ``None``.

    A date is midnight UTC and a timestamp with no offset is UTC. Text that is neither, and an
    instant outside the years 1 to 9999 in UTC, is not read.
    """
    if not isinstance(text, str):
        return None
    clean = text.strip()
    try:
        if len(clean) == 10:
            moment = datetime.combine(date.fromisoformat(clean), datetime.min.time())
        else:
            moment = datetime.fromisoformat(clean.replace("Z", "+00:00").replace("z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        milliseconds = (moment - _EPOCH) // timedelta(milliseconds=1)
    except (ValueError, OverflowError):
        return None
    if not _FIRST_MS <= milliseconds <= _LAST_MS:
        return None
    return milliseconds


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


def declared_range_bounds(column: Any) -> Optional[RangeBounds]:
    """The range a date or timestamp column's ``range`` rules declare, each end optional, or ``None``.

    The grammar is :func:`declared_instants`', but one end is enough (``>= '2024-01-01'``), and a
    high end in the year 9999 or later is no high end, so ``between '2024-01-01' and '9999-12-31'``
    is ``>= '2024-01-01'``. ``None`` for a rule that is not read, for a range with neither end, and
    for one whose low end is not before its high end. An end is strict when the rule that sets it is
    ``>`` or ``<``: the range holds the instants beyond it and not the instant itself.
    """
    rules = column.get("validationRules") if isinstance(column, Mapping) else None
    low: Optional[Tuple[int, bool]] = None
    high: Optional[Tuple[int, bool]] = None
    for rule in rules if isinstance(rules, list) else ():
        if not isinstance(rule, Mapping) or str(rule.get("type") or "").lower() != "range":
            continue
        constraint = rule.get("constraint")
        if not isinstance(constraint, str):
            continue
        pairs: List[Tuple[str, Optional[int]]] = []
        between = _BETWEEN.match(constraint)
        if between:
            first = next(text for text in between.groups()[:3] if text is not None)
            second = next(text for text in between.groups()[3:] if text is not None)
            pairs = [(">=", instant_ms(first)), ("<=", instant_ms(second))]
        else:
            for part in and_parts(constraint):
                bound = _BOUND.match(part)
                if not bound:
                    return None
                text = next(item for item in bound.groups()[1:] if item is not None)
                pairs.append((bound.group(1), instant_ms(text)))
        for operator, milliseconds in pairs:
            if milliseconds is None:
                return None
            strict = operator in (">", "<")
            if operator in (">", ">="):
                low = _tighter(low, milliseconds, strict, above=True)
            else:
                high = _tighter(high, milliseconds, strict, above=False)
    if low is not None and high is not None and low[0] >= high[0] and high[0] < WINDOW_END_LIMIT_MS:
        return None
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


def declared_instants(column: Any) -> Optional[Tuple[int, int]]:
    """The ``(low, high)`` milliseconds a date or timestamp column's ``range`` rules declare, or ``None``.

    ``None`` unless both ends are declared, each an instant that is read, and the low end lies
    before the high end; also ``None`` when the high end is at or after
    :data:`WINDOW_END_LIMIT_MS`, since such a range gives the window nothing. A strict end is read
    as the instant itself, as the profiler lays out bin edges; :func:`declared_range_bounds` says which
    ends are strict, and reads a range with one end as well.
    """
    bounds = declared_range_bounds(column)
    if bounds is None or bounds.low is None or bounds.high is None:
        return None
    return bounds.low, bounds.high
