# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The digest reference every governed extension point speaks in.

KERNEL-SPEC section 2.3 -- "a digest is always a ``DigestRef(value, domain,
subject)``; comparing two refs of different domains raises ``FLX-1002``" --
makes the labelled digest, not a bare hex string, the unit of exchange
between the kernel and anything outside it. The concrete class is owned by
``flux_engine.core.digest``, layered over the one module allowed to import
``rfc8785`` and ``hashlib``.

This package must not take a runtime import on a kernel module: the API
surface is what third-party implementations target, and it has to be
importable and type-checkable on its own. So the contract carried here is
the *structural* one -- a ``Protocol`` naming the three attributes
KERNEL-SPEC fixes. The kernel's concrete class satisfies it by shape, with
no registration, no subclassing, and no import edge from the extension
contract into the kernel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

__all__ = ["DIGEST_DOMAINS", "DigestRef"]

#: The three labelled digest domains of KERNEL-SPEC section 2.3, as the exact
#: wire strings. A closed tuple rather than an enum: an implementation can
#: check a domain against the spec without importing the kernel's own
#: ``DigestDomain`` enum, which is the type the kernel actually carries.
DIGEST_DOMAINS = ("raw-bytes", "jcs-document", "jcs-ndjson-stream")


@runtime_checkable
class DigestRef(Protocol):
    """A digest carrying the domain it was taken in.

    Structural contract for the kernel's digest reference (KERNEL-SPEC
    section 2.2-2.3): ``value`` is ``"sha256:" + hex``; ``domain`` is one of
    :data:`DIGEST_DOMAINS`; ``subject`` names what was digested. Two refs of
    different domains are never comparable -- the kernel refuses the
    comparison rather than returning a misleading answer.
    """

    @property
    def value(self) -> str:
        """The digest itself, rendered ``"sha256:" + lowercase hex``."""

    @property
    def domain(self) -> str:
        """The domain the digest was taken in; one of :data:`DIGEST_DOMAINS`."""

    @property
    def subject(self) -> str:
        """What was digested -- a document path, an expose id, a stream name."""


if TYPE_CHECKING:  # pragma: no cover - type-check-time only, never imported at runtime
    from flux_engine.core.digest import DigestRef as _KernelDigestRef

    def _kernel_digest_ref_satisfies_this_contract(ref: _KernelDigestRef) -> DigestRef:
        """Static assertion, evaluated by the type checker and never executed.

        The kernel's concrete digest reference satisfies the Protocol above
        by shape alone -- no registration, no subclassing, no runtime import
        from this package into the kernel. If the kernel's class ever drifts
        from the structural contract this package publishes, that drift shows
        up here as a type error rather than as a third-party implementation
        that no longer fits.
        """
        return ref
