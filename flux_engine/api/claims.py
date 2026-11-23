# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What the engine is willing to say, and what it must show for saying it.

A claim is a sentence plus the reason it is allowed to be a sentence. The
kinds are closed on purpose: four of them, each answering a different
question, none of them collapsible into another.

* ``conformance`` -- the bundle obeys the FLUX schema and its cross-document
  rules.
* ``reproducibility`` -- the same inputs and the same seed produced the same
  bytes.
* ``fidelity`` -- the generated rows stand in the declared statistical
  relation to a committed state (SEAM-FIDELITY-SPEC section 6).
* ``validity`` -- an outcome the run predicted matched what was later
  observed in the world.

The last one is the one nothing else implies. A run can be perfectly
reproducible, perfectly conformant, and pass every fidelity check, and
still say nothing about whether the world behaves the way the scenario
said it would. That is why :class:`ClaimBasis` exists and why ``none`` is
one of its values: a claim with no basis is a claim the engine will render
as unsupported, never a claim it will quietly drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flux_engine.api.digests import DigestRef

__all__ = ["Claim", "ClaimBasis", "ClaimType"]


class ClaimType(str, Enum):
    """The four things the engine can be asked to say. Closed set."""

    CONFORMANCE = "conformance"
    REPRODUCIBILITY = "reproducibility"
    FIDELITY = "fidelity"
    VALIDITY = "validity"


class ClaimBasis(str, Enum):
    """What a claim rests on.

    ``NONE`` is not an absence of a field; it is a stated position. It is
    what a validity claim carries when nothing was measured against the
    world, and it is the value the record shows rather than leaving the
    reader to assume the strongest reading.
    """

    #: Measured against a live randomised comparison.
    LIVE_AB = "live-ab"
    #: Measured against a historical period held out of the inputs.
    HISTORICAL_HOLDOUT = "historical-holdout"
    #: Nothing was measured against the world.
    NONE = "none"


@dataclass(frozen=True)
class Claim:
    """One sentence the engine will stand behind, and its receipts.

    Attributes
    ----------
    claim_type
        Which of the four questions this sentence answers.
    statement
        The sentence itself, in the register the project uses: what was
        checked, against what, under which declarations.
    basis
        What the sentence rests on. A :class:`ClaimType.VALIDITY` claim
        whose basis is :attr:`ClaimBasis.NONE` is rendered as unsupported.
    evidence
        The digests a reader can re-take to arrive at the same sentence.
        A tuple rather than a list: a frozen record whose evidence could be
        appended to after the fact is not a record.
    verifier
        Who or what evaluated the claim -- a key id, a gate name, a tool
        identifier. Never a person's assurance in place of a check.
    """

    claim_type: ClaimType
    statement: str
    basis: ClaimBasis
    evidence: tuple["DigestRef", ...] = field(default_factory=tuple)
    verifier: str = ""
