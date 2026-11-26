# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The run's window: what a contract's date ranges say of it, and the ``--window`` a person gives.

:func:`declared_instants` reads a date or timestamp column's ``range`` ``validationRules`` with the
spelling the FLUID profiler bins a declared date range with: ``between 'A' and 'B'``, or ``>= 'A'
and <= 'B'`` (``>`` and ``<`` too), each value an ISO date (midnight UTC) or an RFC 3339 timestamp
(UTC when it has no offset), in single or double quotes or bare. Several rules tighten each side.
The range is read only when both ends are declared and the low end lies before the high end;
anything else declares nothing. A range whose high end is at or after :data:`WINDOW_END_LIMIT_MS`
says only that the column holds a date, so it declares nothing either: a run's periods step past
the end of its window, and a date in the year 10000 is out of range. The range only feeds the
run's window: the column's own declaration does not change.

:func:`parse_window` reads the two texts of ``init --window``.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple

from flux_engine.core.clock import format_rfc3339_ms
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.range_rules import (
    WINDOW_END_LIMIT_MS,
    declared_instants,
    declared_range_bounds,
    instant_ms,
)

__all__ = [
    "WINDOW_END_LIMIT_MS",
    "WindowError",
    "declared_instants",
    "declared_days",
    "declared_window",
    "instant_ms",
    "parse_window",
]

_DAY_MS = 86_400_000


def declared_window(column: Any, kind: Optional[LogicalKind]) -> Optional[Tuple[int, int]]:
    """The ``(start, end)`` milliseconds a date or timestamp column's ``range`` rules give the window.

    :func:`declared_instants`' reading, and for an inclusive range whose ends meet, the one unit it
    holds as ``plan`` reads it: ``[D, D + 1 day)`` for a DATE, ``[T, T + 1 ms)`` for a TIMESTAMP. A
    range whose ends meet and one is strict holds nothing, and gives the window nothing. A low end
    between two milliseconds starts the window at the next one, where ``plan``'s range starts.
    """
    found = declared_instants(column)
    bounds = declared_range_bounds(column)
    if found is not None:
        # A low end between two milliseconds: `declared_instants` floors it, `plan` rounds it up,
        # and the window starts where plan's range does.
        low = found[0] if bounds is None or bounds.low is None else max(found[0], bounds.low)
        return (low, found[1]) if low < found[1] else None
    if (
        bounds is None
        or bounds.low is None
        or bounds.low != bounds.high
        or bounds.low_strict
        or bounds.high_strict
    ):
        return None
    if kind is LogicalKind.DATE:
        return (bounds.low, bounds.low + _DAY_MS) if bounds.low % _DAY_MS == 0 else None
    return bounds.low, bounds.low + 1


def declared_days(column: Any) -> Tuple[Optional[int], Optional[int]]:
    """The first and the last day (days since the epoch) a date column's declared ``range`` holds.

    Each end is ``None`` when the contract declares none (a high end in the year 9999 is none). A
    low end between two days is the next day; a strict end leaves out its own instant. A range
    with no day in it gives ``(None, None)``, since it describes nothing a projection can follow.
    """
    bounds = declared_range_bounds(column)
    if bounds is None:
        return None, None
    first = last = None
    if bounds.low is not None:
        low = bounds.low + 1 if bounds.low_strict else bounds.low
        first = -((-low) // _DAY_MS)
    if bounds.high is not None:
        high = bounds.high - 1 if bounds.high_strict else bounds.high
        last = high // _DAY_MS
    if first is not None and last is not None and last < first:
        return None, None
    return first, last


class WindowError(ValueError):
    """A ``--window`` value that cannot be read; the message names it."""


def parse_window(start: str, end: str) -> Tuple[str, str]:
    """The window ``init --window START END`` gives, as RFC 3339 millisecond texts.

    Each end is a date (``YYYY-MM-DD``, midnight UTC) or an RFC 3339 timestamp. Raises
    :class:`WindowError`, naming the value, for text that is neither, an instant outside the
    years 1 to 9999, a START that is not before END, or an END at or after
    :data:`WINDOW_END_LIMIT_MS`: the year 9999 spells an open end, which no window reads (as in a
    declared range), and a run's periods step past the end of its window.
    """
    ends: List[int] = []
    for name, text in (("START", start), ("END", end)):
        milliseconds = instant_ms(text) if isinstance(text, str) else None
        if milliseconds is None:
            raise WindowError(
                f"--window {name} {text!r} is not a date (YYYY-MM-DD) or an RFC 3339 timestamp "
                "in the years 1 to 9999"
            )
        ends.append(milliseconds)
    if ends[0] >= ends[1]:
        raise WindowError(f"--window START {start!r} is not before END {end!r}")
    if ends[1] >= WINDOW_END_LIMIT_MS:
        raise WindowError(
            f"--window END {end!r} is in the year 9999, which spells an open end rather than an "
            "instant the run reaches; give the instant your data runs to"
        )
    return format_rfc3339_ms(ends[0]), format_rfc3339_ms(ends[1])
