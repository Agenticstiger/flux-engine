# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The run's window: what a contract's date ranges say of it, and the ``--window`` a person gives.

:func:`declared_instants` reads a date or timestamp column's ``range`` ``validationRules`` with the
spelling the FLUID profiler bins a declared date range with: ``between 'A' and 'B'``, or ``>= 'A'
and <= 'B'`` (``>`` and ``<`` too), each value an ISO date (midnight UTC) or an RFC 3339 timestamp
(UTC when it has no offset), in single or double quotes or bare. Several rules tighten each side.
The range is read only when both ends are declared and the low end lies before the high end;
anything else declares nothing. The range only feeds the run's window: the column's own
declaration does not change.

:func:`parse_window` reads the two texts of ``init --window``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Mapping, Optional, Tuple

from flux_engine.core.clock import format_rfc3339_ms

__all__ = ["WindowError", "declared_instants", "instant_ms", "parse_window"]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FIRST_MS = (datetime(1, 1, 1, tzinfo=timezone.utc) - _EPOCH) // timedelta(milliseconds=1)
_LAST_MS = (datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc) - _EPOCH) // timedelta(
    milliseconds=1
)

_BOUND = re.compile(r"""^\s*(>=|<=|>|<)\s*(?:'([^']*)'|"([^"]*)"|(\S+))\s*$""")
_BETWEEN = re.compile(
    r"""^\s*between\s+(?:'([^']*)'|"([^"]*)"|(\S+))\s+and\s+(?:'([^']*)'|"([^"]*)"|(\S+))\s*$""",
    re.IGNORECASE,
)


class WindowError(ValueError):
    """A ``--window`` value that cannot be read; the message names it."""


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


def declared_instants(column: Any) -> Optional[Tuple[int, int]]:
    """The ``(low, high)`` milliseconds a date or timestamp column's ``range`` rules declare, or ``None``.

    ``None`` unless both ends are declared, each an instant that is read, and the low end lies
    before the high end.
    """
    rules = column.get("validationRules") if isinstance(column, Mapping) else None
    low: Optional[int] = None
    high: Optional[int] = None
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
            for part in re.split(r"\s+and\s+", constraint.strip(), flags=re.IGNORECASE):
                bound = _BOUND.match(part)
                if not bound:
                    return None
                text = next(item for item in bound.groups()[1:] if item is not None)
                pairs.append((bound.group(1), instant_ms(text)))
        for operator, milliseconds in pairs:
            if milliseconds is None:
                return None
            if operator in (">", ">="):
                low = milliseconds if low is None or milliseconds > low else low
            else:
                high = milliseconds if high is None or milliseconds < high else high
    if low is None or high is None or low >= high:
        return None
    return low, high


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
