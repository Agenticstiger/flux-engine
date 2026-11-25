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

from typing import List, Tuple

from flux_engine.core.clock import format_rfc3339_ms
from flux_engine.seam.range_rules import WINDOW_END_LIMIT_MS, declared_instants, instant_ms

__all__ = ["WINDOW_END_LIMIT_MS", "WindowError", "declared_instants", "instant_ms", "parse_window"]


class WindowError(ValueError):
    """A ``--window`` value that cannot be read; the message names it."""


def parse_window(start: str, end: str) -> Tuple[str, str]:
    """The window ``init --window START END`` gives, as RFC 3339 millisecond texts.

    Each end is a date (``YYYY-MM-DD``, midnight UTC) or an RFC 3339 timestamp. Raises
    :class:`WindowError`, naming the value, for text that is neither, an instant outside the
    years 1 to 9999, or a START that is not before END.
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
    return format_rfc3339_ms(ends[0]), format_rfc3339_ms(ends[1])
