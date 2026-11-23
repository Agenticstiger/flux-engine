# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The share that holds: ``(satisfied, total)``, with an exact fraction.

One declaration gives both a generator and a checker that returns how many of the rows it covers
satisfy it, as Deequ's ``satisfies`` does (Deequ 2.1.0: a constraint's result is the fraction of
rows that satisfy a predicate, ``Compliance``). A check here never says only "yes" or "no":
a broken rule or a broken key shows as a share under 100 %.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Optional

from flux_stats.spelling import render_exact

__all__ = ["Holds", "render_share"]


@dataclass(frozen=True)
class Holds:
    """``satisfied`` of ``total``: how many of the rows a check covers satisfy it."""

    satisfied: int
    total: int

    @property
    def share(self) -> Optional[Fraction]:
        """``satisfied / total``, or ``None`` when the check covers no row: there is no share to state."""
        return Fraction(self.satisfied, self.total) if self.total else None

    @property
    def violating(self) -> int:
        """The rows the check covers that do not satisfy it."""
        return self.total - self.satisfied

    @property
    def whole(self) -> bool:
        """Whether every covered row satisfies it (vacuously true when it covers none)."""
        return self.satisfied == self.total

    def to_document(self) -> Dict[str, Any]:
        """The share as a record spells it: the two counts, and the share as a decimal or ``None``."""
        return {
            "satisfied": self.satisfied,
            "total": self.total,
            "share": render_share(self.share),
        }

    def text(self) -> str:
        """``7960 of 8000 (99.5%)``, or ``0 of 0`` when the check covers no row."""
        if not self.total:
            return "0 of 0"
        return f"{self.satisfied} of {self.total} ({_percent(self.satisfied, self.total)})"


def _percent(satisfied: int, total: int) -> str:
    """A share as a percentage rounded down to two places, so a share under 100 % never reads as 100 %."""
    hundredths = satisfied * 10_000 // total
    return f"{hundredths // 100}.{hundredths % 100:02d}%"


def render_share(share: Optional[Fraction]) -> Optional[str]:
    """An exact share as the scorecard spells it: a terminating decimal, else ``n/d``."""
    return None if share is None else render_exact(share)
