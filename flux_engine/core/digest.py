# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Labelled digests: the three domains, and the plan-binding builders.

Implements KERNEL-SPEC §2.2-§2.3 (the digest string and its three
domains) and §11 (``inputDigest`` and ``planDigest``, with masking).

A digest in this engine is never a bare string. It is a
:class:`DigestRef` carrying the domain it was taken in and the subject it
was taken over, because the same 32 bytes mean different things depending
on what was hashed: the bytes of a file on disk, the canonical JSON of
the document those bytes parse to, and the newline-delimited emit stream
are three different claims. Comparing two refs from different domains
raises ``FLX-1002`` rather than returning ``False`` -- a comparison
across domains is a caller mistake, and a silent ``False`` would present
it as a digest mismatch.

``hashlib`` and ``rfc8785`` stay in :mod:`flux_engine.core.canonical`
(KERNEL-SPEC §2's heading names that module for both); everything here
routes through ``canonical.sha256_hex`` and ``canonical.canonical_bytes``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional, Union

from flux_engine.core.canonical import (
    SELF_REFERENTIAL_FIELDS,
    canonical_bytes,
    mask_document,
    sha256_hex,
)
from flux_engine.core.errors import FluxError, register

__all__ = [
    "DIGEST_PATTERN",
    "DigestDomain",
    "DigestRef",
    "DigestedDocument",
    "FLX_1002",
    "build_input_digest",
    "build_plan_digest",
    "digest_of_bytes",
    "digest_of_document",
    "digest_of_stream",
    "digests_match",
    "require_domain",
    "require_same_domain",
]

FLX_1002 = register(
    "FLX-1002",
    "digest comparison across two domains",
    "Two digest references were compared that were taken in different "
    "domains (KERNEL-SPEC §2.3): raw file bytes, the canonical JSON of a "
    "parsed document, and the emit stream are three separate claims and "
    "never equal one another. Compare like with like, or convert the "
    "subject first.",
)

#: A digest string: the algorithm, a colon, and lowercase hex.
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class DigestDomain(str, Enum):
    """The three digest domains of KERNEL-SPEC §2.3.

    ``RAW_BYTES``
        File bytes exactly as on disk -- the manifest's ``files`` entries
        and the shipped ``.fluid.yml``.
    ``JCS_DOCUMENT``
        Canonical JSON of a *parsed* document: ``contractDigest``,
        ``planDigest``, ``inputDigest``, the run digest.
    ``JCS_NDJSON_STREAM``
        The emit stream of KERNEL-SPEC §8.
    """

    RAW_BYTES = "raw-bytes"
    JCS_DOCUMENT = "jcs-document"
    JCS_NDJSON_STREAM = "jcs-ndjson-stream"


@dataclass(frozen=True, eq=False)
class DigestRef:
    """A digest, the domain it was taken in, and the subject it covers.

    ``subject`` is a human-readable label used in diagnostics ("the
    document that moved") and plays no part in equality: two references
    to the same bytes in the same domain are the same digest whatever
    they are called. Equality across domains raises ``FLX-1002``
    (KERNEL-SPEC §2.3).
    """

    value: str
    domain: DigestDomain
    subject: str

    def __post_init__(self) -> None:
        if not DIGEST_PATTERN.match(self.value):
            raise ValueError(
                f"{self.subject}: malformed digest {self.value!r} "
                "(expected 'sha256:' + 64 lowercase hex characters)"
            )
        if not isinstance(self.domain, DigestDomain):  # pragma: no cover - typed API
            raise ValueError(f"{self.subject}: {self.domain!r} is not a DigestDomain")

    @property
    def hex(self) -> str:
        """The hex digest without the ``sha256:`` prefix."""
        return self.value.split(":", 1)[1]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DigestRef):
            return NotImplemented
        require_same_domain(self, other)
        return self.value == other.value

    def __hash__(self) -> int:
        return hash((self.value, self.domain))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class DigestedDocument:
    """A document and the digest taken over it.

    Builders return both: the caller writes ``document`` into an artifact
    and quotes ``ref`` in the receipt, and neither can be recomputed from
    the other without repeating the masking rules.
    """

    document: Mapping[str, Any]
    ref: DigestRef


def require_same_domain(left: DigestRef, right: DigestRef) -> None:
    """Raise ``FLX-1002`` when *left* and *right* are in different domains."""
    if left.domain is not right.domain:
        raise FluxError(
            FLX_1002,
            f"{left.subject} is {left.domain.value}, {right.subject} is {right.domain.value}",
        )


def require_domain(ref: DigestRef, domain: DigestDomain) -> DigestRef:
    """Return *ref* if it is in *domain*, else raise ``FLX-1002``."""
    if ref.domain is not domain:
        raise FluxError(
            FLX_1002,
            f"{ref.subject} is {ref.domain.value} where {domain.value} is required",
        )
    return ref


def digests_match(left: DigestRef, right: DigestRef) -> bool:
    """Return whether two same-domain references carry the same digest.

    The explicit spelling of ``left == right``; it raises ``FLX-1002`` on
    a cross-domain comparison for the same reason the operator does.
    """
    return left == right


def _ref(data: bytes, domain: DigestDomain, subject: str) -> DigestRef:
    return DigestRef(value="sha256:" + sha256_hex(data), domain=domain, subject=subject)


def digest_of_bytes(data: bytes, *, subject: str) -> DigestRef:
    """Digest *data* in the ``raw-bytes`` domain (KERNEL-SPEC §2.3)."""
    return _ref(data, DigestDomain.RAW_BYTES, subject)


def digest_of_document(document: Any, *, subject: str) -> DigestRef:
    """Digest the canonical JSON of *document* in the ``jcs-document`` domain."""
    return _ref(canonical_bytes(document), DigestDomain.JCS_DOCUMENT, subject)


def digest_of_stream(data: bytes, *, subject: str) -> DigestRef:
    """Digest *data* in the ``jcs-ndjson-stream`` domain (KERNEL-SPEC §8).

    The emit stream's framing -- the ``flux-emit/1`` label, the header and
    the newline-delimited rows -- is assembled by the seam; this function
    labels the resulting bytes with the domain they belong to.
    """
    return _ref(data, DigestDomain.JCS_NDJSON_STREAM, subject)


#: Accepted spellings of an input to the §11 builders: the raw file bytes,
#: or a ``raw-bytes`` reference already taken over them.
FileInput = Union[bytes, DigestRef]


def _raw_bytes_ref(value: FileInput, *, subject: str) -> DigestRef:
    if isinstance(value, DigestRef):
        return require_domain(value, DigestDomain.RAW_BYTES)
    return digest_of_bytes(value, subject=subject)


def _digest_map(entries: Mapping[str, FileInput], *, label: str) -> Dict[str, str]:
    """Return ``{path: digest string}`` for *entries*, in sorted path order.

    KERNEL-SPEC §11 spells the maps as "sorted"; JCS sorts object keys
    anyway, so the sorting here is for the reader of ``plan.json``, not
    for the bytes.
    """
    return {
        path: _raw_bytes_ref(entries[path], subject=f"{label}:{path}").value
        for path in sorted(entries)
    }


def build_input_digest(
    *,
    kernel_version: str,
    seed_tree_version: str,
    trace_version: str,
    documents: Mapping[str, FileInput],
    fluid_documents: Mapping[str, FileInput],
    lock: Optional[FileInput] = None,
    attribute_model_digest: Optional[Union[str, DigestRef]] = None,
) -> DigestedDocument:
    """Build the ``inputDigest`` document and its digest (KERNEL-SPEC §11).

    ``documents`` and ``fluid_documents`` map a bundle-relative path to
    the file's bytes (or to a ``raw-bytes`` reference already taken over
    them). ``lock`` is the ``flux.lock`` bytes when the bundle ships one.
    ``attribute_model_digest`` is the ``jcs-document`` digest of the
    resolved attribute model.

    Absent optional inputs are written as JSON ``null`` rather than
    omitted, so the digested document always has the same field set --
    "no lockfile" is a fact about the bundle and is hashed as one.
    """
    document: Dict[str, Any] = {
        "kernelVersion": kernel_version,
        "seedTreeVersion": seed_tree_version,
        "traceVersion": trace_version,
        "documents": _digest_map(documents, label="documents"),
        "fluidDocuments": _digest_map(fluid_documents, label="fluidDocuments"),
        "lock": (None if lock is None else _raw_bytes_ref(lock, subject="flux.lock").value),
        "attributeModelDigest": _attribute_model_value(attribute_model_digest),
    }
    return DigestedDocument(
        document=document,
        ref=digest_of_document(document, subject="inputDigest"),
    )


def _attribute_model_value(value: Optional[Union[str, DigestRef]]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, DigestRef):
        return require_domain(value, DigestDomain.JCS_DOCUMENT).value
    if not DIGEST_PATTERN.match(value):
        raise ValueError(f"attributeModelDigest: malformed digest {value!r}")
    return value


def build_plan_digest(
    plan: Mapping[str, Any],
    *,
    self_referential: Iterable[str] = SELF_REFERENTIAL_FIELDS,
) -> DigestedDocument:
    """Build the ``planDigest`` over *plan* with the §11 mask applied.

    KERNEL-SPEC §11 defers to forge-cli's ``plan_digest`` rule: the plan's
    own ``planDigest`` and its wall-clock ``generated_at`` are masked out
    of the hash input. Hashing a document that carries its own hash has no
    fixed point, and hashing the timestamp would make two identical plan
    runs disagree -- which is exactly the divergence the digest exists to
    detect.

    ``document`` in the result is the **masked** form that was hashed, not
    the caller's plan: a reviewer can see precisely which bytes the digest
    covers.
    """
    masked = mask_document(plan, self_referential=self_referential)
    return DigestedDocument(
        document=masked,
        ref=digest_of_document(masked, subject="planDigest"),
    )
