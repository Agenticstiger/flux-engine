# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The patterns a run's numbers rest on, and the order they are graded in (ADR 0020).

Kept in ``core`` because two layers that may not import each other both need it: the evidence
report (:mod:`flux_engine.evidence.grades`) states a run's grade, and the governance gate
(:mod:`flux_engine.governance.privacy`), which the seam's namespace reader calls, compares a grade
with a floor. It holds the five names, their order and two small functions, and reads nothing.

* ``P0`` **assumed**: no profile was pinned. Every parameter is declared by hand, and the engine
  read no data. It cannot confirm that no data shaped the numbers a person typed.
* ``P1`` **checked**: a profile of aggregates under the profiler's output checks, with no formal
  guarantee.
* ``P3`` **differentially private marginals**: a profile that states one release under a stated
  budget and holds only noisy statistics, on the profiler's statement.
* ``P4`` **differentially private model**: a learned model from a generator plugin whose bundle
  declares the account of its training under differential privacy (ADR 0032). The engine checks
  the account's form, not how the model was trained.
* ``P5`` **learned model, no privacy account**: a learned model from a generator plugin whose
  privacy rests on empirical checks alone. The weakest pattern.

The number 2 is not used: the numbers are stable, and P2 is unassigned.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Mapping, Tuple

__all__ = ["ORDER", "Pattern", "at_least", "weakest"]


class Pattern(str, Enum):
    """What a run's numbers rest on. The numbers are stable; 2 is unassigned."""

    P0 = "P0"
    P1 = "P1"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"

    @property
    def label(self) -> str:
        """The pattern's name: ``assumed``, ``checked``, and so on."""
        return _TITLES[self]


_TITLES: Mapping[Pattern, str] = {
    Pattern.P0: "assumed",
    Pattern.P1: "checked",
    Pattern.P3: "differentially private marginals",
    Pattern.P4: "differentially private model",
    Pattern.P5: "learned model, no privacy account",
}

#: Weakest first: by what the pattern lets a reader say about the data. ``P5`` is a learned model
#: with no account, so it says least. ``P1`` says nothing formal, ``P3`` and ``P4`` give a bound
#: (``P4`` ranks above ``P3``, as a bundle may raise a label's minimum to ``P4``), and ``P0`` reads
#: no data.
ORDER: Tuple[Pattern, ...] = (Pattern.P5, Pattern.P1, Pattern.P3, Pattern.P4, Pattern.P0)


def weakest(patterns: Iterable[Pattern]) -> Pattern:
    """The weakest of *patterns*; ``P0`` for none (no data informs the run)."""
    found = set(patterns)
    return next((pattern for pattern in ORDER if pattern in found), Pattern.P0)


def at_least(pattern: Pattern, floor: Pattern) -> bool:
    """Whether *pattern* is *floor* or stronger."""
    return ORDER.index(pattern) >= ORDER.index(floor)
