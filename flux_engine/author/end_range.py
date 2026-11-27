# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The span a lifetime's end is held to, and the gap that keeps every end inside it (ADR 0024 section 4).

The generator cuts a lifetime's gap to the end column's declared range, so the gap ``init`` writes
is read against that range. :class:`Spans` holds what a table's date columns declare in the
bundle (``bounds``), what their contract's range rules give those the bundle declares no span for
(``declared``), and the run's window, and answers the three questions the lifetime asks of them:
which span the end is held to, where the start can lie, and how long the gap may be.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple, Union

__all__ = ["Spans"]

_DAY_MS = 86_400_000

Span = Tuple[int, int]


@dataclass(frozen=True)
class Spans:
    """The spans of one table's date columns, in milliseconds, and the run's window."""

    bounds: Mapping[str, Span]
    declared: Mapping[str, Span]
    window_start: int
    window_end: int

    def end(self, end: str) -> Tuple[Optional[Span], bool]:
        """The span an end column's draw is held to, and whether the bundle is to declare it.

        A span the bundle declares for the column (its bins' ends, or its ``range``) that lies
        inside what the contract's range rules give is the range the generator cuts a lifetime's
        gap to, and is left as it is. One that reaches past the contract's range is cut to it, and
        the lifetime then writes the cut as the column's ``range``; so does a column the bundle
        declares no span for, which takes the contract's. With neither there is no span.
        """
        found = self.bounds.get(end)
        declared = self.declared.get(end)
        if found is None:
            return declared, declared is not None
        if declared is None:
            return found, False
        low, high = max(found[0], declared[0]), min(found[1], declared[1])
        if (low, high) == found:
            return found, False
        # A draw wholly outside the contract's range keeps nothing of itself: the range is the contract's.
        return ((low, high) if low <= high else declared), True

    def reach(self, start: str) -> Optional[Span]:
        """The span a start column's draw reaches, where it is known.

        A start with no span of its own in the bundle is drawn within the run's window and its
        contract's range, so the two meet.
        """
        found = self.bounds.get(start)
        if found is not None:
            return found
        declared = self.declared.get(start)
        if declared is None:
            return None
        low, high = max(declared[0], self.window_start), min(declared[1], self.window_end)
        return (low, high) if low <= high else None

    def earliest(self, start: str) -> int:
        """The earliest instant a start can take: the window's start when none is known."""
        found = self.reach(start)
        return self.window_start if found is None else found[0]

    def gap(self, start: str, end: str, span: Span, shortest: int, longest: int) -> Union[int, str]:
        """The longest gap that keeps an end inside *span*, or why none is left.

        The latest end is the span's last instant, from the earliest start.
        """
        longest = min(longest, (span[1] - self.earliest(start)) // _DAY_MS)
        if longest <= shortest:
            return (
                f"the declared range of {end!r} leaves {longest} days from the earliest "
                f"{start!r}, too few to draw a gap in"
            )
        return longest

    def fits(self, start: str, end: str, shortest: int, longest: int) -> bool:
        """Whether a kept end can always be drawn inside its declared range (``FLX-6204``)."""
        reach, (span, _) = self.reach(start), self.end(end)
        if reach is None or span is None:
            return False
        return reach[1] + shortest * _DAY_MS <= span[1] and reach[0] + longest * _DAY_MS >= span[0]
