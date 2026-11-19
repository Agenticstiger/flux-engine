# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The fidelity check, as three ports.

SEAM-FIDELITY-SPEC section 5 is explicit about the v0.1 position: no
attestation artefact is generated, the :class:`ProofSystem` port has one
implementation whose name is ``"none"``, "and the record says so". The
port exists now so that the day a real backend arrives, the fidelity
record's shape does not change and every record already written stays
readable -- and so that today's record cannot be mistaken for tomorrow's.

The split is deliberate. A :class:`FidelityProver` runs where the source
data lives and never lets a record leave (SEAM-FIDELITY-SPEC section 4:
what crosses the boundary is aggregate quantities, commitments, and
counts -- never a record). A :class:`FidelityVerifier` runs anywhere and
takes only what crossed. Neither can do the other's job, which is what
makes the boundary a boundary rather than a convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol, runtime_checkable

from flux_engine.api.claims import Claim

if TYPE_CHECKING:
    from flux_engine.api.digests import DigestRef

__all__ = ["FidelityProver", "FidelityStatement", "FidelityVerifier", "ProofSystem"]

#: The only proof-system name v0.1 recognises. A record carrying this name
#: is a record saying that no attestation artefact backs it.
PROOF_SYSTEM_NONE = "none"


@dataclass(frozen=True)
class FidelityStatement:
    """What is being checked, and against what.

    Attributes
    ----------
    expose_id
        The emitted stream under check.
    contract_digest
        The referenced FLUID contract.
    committed_state
        The commitment the boundary published for its fixed record set --
        the per-column root, the derived-statistic commitment, the state
        ordinal, the count. Never records.
    properties
        The declared properties and their margins, as compiled. Read-only
        by construction: the statement is quoted in the record.
    """

    expose_id: str
    contract_digest: "DigestRef"
    committed_state: Mapping[str, Any] = field(default_factory=dict)
    properties: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


@runtime_checkable
class ProofSystem(Protocol):
    """Names the backend a fidelity record was produced under.

    In v0.1 the only value :attr:`name` takes is ``"none"``, and a record
    carrying it states that plainly rather than omitting the field. An
    omitted field reads as an unremarkable default; a stated ``"none"``
    reads as what it is.
    """

    name: str


@runtime_checkable
class FidelityProver(Protocol):
    """Runs inside the boundary; produces the artefact, if the backend has one.

    **Reserved** (:data:`flux_engine.api.RESERVED_PORTS`): this build ships
    no implementation of this shape and no parameter through which a
    consumer could inject one. It is a shape to design against, not an
    extension point to ship against.
    """

    proof_system: ProofSystem

    def attest(self, statement: FidelityStatement) -> Optional[bytes]:
        """Return the backend's artefact for *statement*, or ``None``.

        The ``"none"`` backend returns ``None``. That is a complete and
        correct answer for it, not a failure, and the record distinguishes
        the two.
        """


@runtime_checkable
class FidelityVerifier(Protocol):
    """Runs outside the boundary; re-evaluates the statement from what crossed."""

    proof_system: ProofSystem

    def check(self, statement: FidelityStatement, attestation: Optional[bytes]) -> Claim:
        """Re-evaluate *statement* and return the claim it supports.

        The returned claim's basis and evidence carry what the check
        actually rested on, so a caller reading the claim alone can tell a
        re-evaluated statement from an accepted assertion.
        """
