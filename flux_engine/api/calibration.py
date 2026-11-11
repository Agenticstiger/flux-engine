# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Calibration and the scorecard that says how much a dial is worth trusting.

A calibrated dial is one whose setting came from somewhere. The scorecard
records *where* -- and, when the answer is "the owner declared it", says
that plainly instead of dressing a declaration up as a measurement.

Two rules shape this module.

**Aggregate-only** (ADR-0003): the only thing that crosses into a
calibrator is aggregate statistics. Never a stream of individual records.
The signature enforces the shape; the rule is the reason for the shape.

**Never invent a grade**: the v0.1 path declares rather than fits, and a
declared scorecard carries ``provenance = DECLARED``, ``grade = None``,
and ``credibility = DECLARED``. A missing grade is rendered as missing.
Nothing here supplies a default number in place of one that was never
computed.

Every number is a :class:`~decimal.Decimal`, never a ``float``:
KERNEL-SPEC section 2.1 refuses a float anywhere in a hashed value, and a
scorecard is quoted in the evidence pack.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from flux_engine.api.digests import DigestRef

__all__ = ["Calibrator", "Credibility", "Provenance", "Scorecard"]


class Provenance(str, Enum):
    """Where a dial's setting came from. Closed set, ordered weakest first."""

    #: The owner stated the value. The engine checked nothing.
    DECLARED = "declared"
    #: The value was fitted to aggregate statistics.
    FITTED = "fitted"
    #: The value was fitted and then graded by a scoring rule.
    SCORED = "scored"


class Credibility(str, Enum):
    """How much weight the scorecard supports. Closed set, weakest first."""

    #: Nothing was established. The default for an empty scorecard.
    NOT_ESTABLISHED = "not-established"
    #: The owner's statement, recorded as such.
    DECLARED = "declared"
    #: Graded against a historical period held out of the inputs.
    BACKTESTED = "backtested"
    #: Graded against a live randomised comparison.
    LIVE_VALIDATED = "live-validated"


@dataclass(frozen=True)
class Scorecard:
    """What is known about a calibration, including what is not known.

    Attributes
    ----------
    grade
        The value of a proper scoring rule, exact. ``None`` when nothing
        was scored -- which is what a declared calibration returns, and is
        never replaced by a placeholder number.
    interval_coverage
        The share of declared intervals that contained the observed value,
        exact. ``None`` when no interval was ever compared.
    credibility
        The band the evidence supports, not the band the author would like.
    backtest_window
        The period the grade was taken over, as an RFC 3339 interval
        (``"<start>/<end>"``). ``None`` when there was no backtest. A grade
        without its window is a number without a question.
    provenance
        Declared, fitted, or scored.
    method_id
        Identifies the method that produced the grade, so a reader can find
        out what the number means before deciding what it is worth.
    evidence_digest
        The digest of the aggregate inputs the calibration ran against.
        ``None`` for a declared scorecard, which ran against nothing.
    """

    provenance: Provenance = Provenance.DECLARED
    credibility: Credibility = Credibility.NOT_ESTABLISHED
    method_id: str = ""
    grade: Optional[Decimal] = None
    interval_coverage: Optional[Decimal] = None
    backtest_window: Optional[str] = None
    evidence_digest: Optional["DigestRef"] = None


@runtime_checkable
class Calibrator(Protocol):
    """Turns aggregate statistics into dial settings, with a scorecard.

    Implementations receive aggregates and declared dials and return the
    settings plus the :class:`Scorecard` that says how much those settings
    are worth. A calibrator that cannot grade its own output returns a
    scorecard saying so; it does not return a grade it did not compute.
    """

    method_id: str

    def calibrate(
        self,
        aggregates: Mapping[str, Any],
        dials: Mapping[str, Decimal],
    ) -> tuple[Mapping[str, Decimal], Scorecard]:
        """Fit *dials* to *aggregates* and report the result.

        *aggregates* carries summary quantities only (ADR-0003) -- counts,
        sums, bucket occupancies, declared supports. Individual records
        never appear here, in any implementation, under any option.
        """
