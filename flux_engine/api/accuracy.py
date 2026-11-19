# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The outcome-accuracy gate: register before, score after, record either way.

This is the surface that keeps a validity claim honest, and its order of
operations is the whole point.

1. :meth:`AccuracyGate.register` takes a :class:`PredictionRecord` **before**
   the outcome can be known. A prediction that can be edited after the
   readout arrives is not a prediction.
2. :meth:`AccuracyGate.score` takes the :class:`ObservedReadout` and returns
   an :class:`AccuracyVerdict` bound to the registered prediction.
3. :meth:`AccuracyGate.record` writes the verdict to the evidence pack and
   returns its digest -- including when the prediction missed. A gate that
   only records the hits is a gate that measures nothing.

A verdict whose basis is :attr:`~flux_engine.api.claims.ClaimBasis.NONE`
carries :attr:`AccuracyOutcome.NOT_SCORED`. That is the v0.1 state, and it
is written down rather than left to inference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

from flux_engine.api.calibration import Scorecard
from flux_engine.api.claims import ClaimBasis
from flux_engine.core.errors import FluxError, register

if TYPE_CHECKING:
    from flux_engine.api.digests import DigestRef

__all__ = [
    "AccuracyGate",
    "AccuracyOutcome",
    "AccuracyVerdict",
    "ObservedReadout",
    "PredictionRecord",
]


#: The FLUX ``$defs/identifier`` grammar. Every id the engine turns into a path
#: component (exposeId, simulationId) is held to it by the spec validator; the
#: accuracy records are authored outside any bundle, so they enforce it here.
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]|[A-Za-z0-9_]")

FLX_7003 = register(
    "FLX-7003",
    "prediction id is not a safe identifier",
    "A prediction_id names a file under the state directory, so it must be "
    "one path component drawn from the FLUX identifier grammar "
    "([A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]): no slashes, no leading dot, "
    "no empty value. A document carrying any other spelling is refused before "
    "anything is written.",
)


def require_identifier(value: object, *, field_name: str) -> str:
    """Return *value* when it is a safe single-component identifier, else raise ``FLX-7003``."""
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise FluxError(FLX_7003, f"{field_name}={value!r}")
    return value


class AccuracyOutcome(str, Enum):
    """How a registered prediction fared. Closed set."""

    WITHIN_INTERVAL = "within-interval"
    OUTSIDE_INTERVAL = "outside-interval"
    #: No comparison was made. The honest outcome when nothing was observed.
    NOT_SCORED = "not-scored"


@dataclass(frozen=True)
class PredictionRecord:
    """A statement about the future, fixed before the future arrives.

    Attributes
    ----------
    prediction_id
        Caller-supplied identity. Not generated here: KERNEL-SPEC forbids
        the kernel from reaching for a UUID or the wall clock, and a
        prediction's identity has to be reproducible from the run.
    run_digest
        The run this prediction came out of, so a reader can re-take it.
    metric_id
        What is being predicted.
    statement
        The prediction in words, for the reader who will not re-derive it.
    predicted
        The predicted value, exact.
    interval_low, interval_high
        The declared interval, exact. Both ``None`` means a point
        prediction with no interval -- and then
        :attr:`AccuracyOutcome.WITHIN_INTERVAL` is not available to it.
    horizon_end
        RFC 3339 instant after which the outcome can be read. Registering a
        prediction whose horizon has already passed is not a prediction.
    registered_at
        RFC 3339 instant of registration, supplied by the caller for the
        same reason as ``run_digest``: the engine's kernel does not read a
        wall clock.
    """

    prediction_id: str
    run_digest: "DigestRef"
    metric_id: str
    statement: str
    predicted: Decimal
    horizon_end: str
    registered_at: str
    interval_low: Optional[Decimal] = None
    interval_high: Optional[Decimal] = None

    def __post_init__(self) -> None:
        require_identifier(self.prediction_id, field_name="prediction_id")


@dataclass(frozen=True)
class ObservedReadout:
    """What the world did, read after the horizon.

    Attributes
    ----------
    prediction_id
        The prediction this answers. A readout that matches no registered
        prediction is refused rather than scored.
    metric_id
        Must match the prediction's metric. Scoring one metric against
        another is the failure mode this field exists to make impossible.
    observed
        The observed value, exact.
    sample_size
        How many units the observation rests on.
    basis
        How the observation was obtained.
    observed_at
        RFC 3339 instant of the reading, caller-supplied.
    source_digest
        The digest of the readout's own inputs, when there are any.
    """

    prediction_id: str
    metric_id: str
    observed: Decimal
    sample_size: int
    basis: ClaimBasis
    observed_at: str
    source_digest: Optional["DigestRef"] = None

    def __post_init__(self) -> None:
        require_identifier(self.prediction_id, field_name="prediction_id")


@dataclass(frozen=True)
class AccuracyVerdict:
    """The comparison of one prediction with one readout.

    :attr:`basis` travels with the verdict rather than being looked up
    elsewhere, so a verdict quoted on its own still says what it rests on.
    """

    prediction_id: str
    metric_id: str
    outcome: AccuracyOutcome
    basis: ClaimBasis
    scorecard: Scorecard = field(default_factory=Scorecard)
    evidence: tuple["DigestRef", ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        require_identifier(self.prediction_id, field_name="prediction_id")


@runtime_checkable
class AccuracyGate(Protocol):
    """Holds the register-then-score order that makes a validity claim mean something.

    **Reserved** (:data:`flux_engine.api.RESERVED_PORTS`): this build ships
    no implementation of this shape and no parameter through which a
    consumer could inject one. It is a shape to design against, not an
    extension point to ship against.
    """

    def register(self, prediction: PredictionRecord) -> "DigestRef":
        """Fix *prediction* before its horizon and return its digest.

        The digest is what later binds a readout to a prediction that was
        not edited in between.
        """

    def score(self, observed: ObservedReadout) -> AccuracyVerdict:
        """Compare *observed* with the prediction it names.

        Returns a verdict whose outcome is
        :attr:`AccuracyOutcome.NOT_SCORED` when the readout's basis is
        :attr:`~flux_engine.api.claims.ClaimBasis.NONE`.
        """

    def record(self, verdict: AccuracyVerdict) -> "DigestRef":
        """Write *verdict* to the evidence pack and return its digest.

        Called for misses as well as hits.
        """
