# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The fidelity record -- SEAM-FIDELITY-SPEC §6.

One record per verified emit, in the evidence pack under
``run/fidelity-record-<exposeId>.json``. §6 fixes the shape; this module
builds it, signs it, and reads it back.

    The scope statement is generated from a fixed template, never
    hand-edited; its ``doesNotCertify`` list is the register-discipline
    guard. The record is signed by the verifier key (ed25519 via
    ``cryptography``) [...].

Three choices worth stating.

**The scope statement is a constant, not a parameter.** :data:`SCOPE` is
built from :data:`SCOPE_CERTIFIES` and :data:`SCOPE_DOES_NOT_CERTIFY`,
which no function takes an argument for and no caller can override.
:func:`load_record` refuses a record whose scope statement is not that
one, so a record that quietly narrowed its own limits is unreadable rather
than persuasive.

**The signature covers everything except itself -- and that includes who
signed.** The signed bytes are the canonical JSON of the record with the
``signature`` field removed, under a domain tag, so a field added to the
record after signing invalidates the signature and the signed bytes
cannot be replayed as the preimage of anything else. The signer's own
identity -- key id, public key, whether the key was a per-invocation one
-- therefore lives in a top-level ``signer`` block (:func:`signer_block`)
*inside* the signed body, not in the excluded ``signature`` object. With
it outside, anyone could rewrite a record's body, sign it with a key of
their own, drop that key's public half in beside the signature, and
produce a document that verified against the only key travelling with it.

**A verifier checks against keys its caller trusts.** :func:`verify_record`
takes the trust anchors as an argument and reports a record that verifies
under none of them as unverified. It never takes the key out of the
record: a key a document carries about itself answers "is this document
internally consistent", not "who signed it".

**An unsigned record says it is unsigned.** ``cryptography`` is a declared
dependency, so :class:`Ed25519Signer` is the ordinary path. The fallback
exists for an environment without it, and it writes
``"alg": "unsigned-fallback"`` and an empty signature rather than omitting
the field: a reader must be able to tell an unsigned record from a signed
one at a glance, and an absent field would not tell them.

Nothing here reads a clock. The commitment time comes from the committed
state and the record carries no generation timestamp of its own -- the
evidence pack's envelope (``flux_engine.core.artifact``) is where a
wall-clock field belongs, and it is masked out of every digest.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from types import MappingProxyType
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
    runtime_checkable,
)

from flux_engine.concordance.boundary.slice import CommittedState
from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.errors import FLX_8002
from flux_engine.concordance.verifier import FidelityEvaluation
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError

__all__ = [
    "Ed25519Signer",
    "FidelityRecord",
    "RECORD_VERSION",
    "SCOPE",
    "SCOPE_CERTIFIES",
    "SCOPE_DOES_NOT_CERTIFY",
    "SIGNATURE_PREFIX",
    "TrustedKeys",
    "RecordSigner",
    "SyntheticReference",
    "UnsignedFallbackSigner",
    "build_record",
    "load_record",
    "record_signer_public_key",
    "signer_block",
    "signing_payload",
    "verify_record",
]

#: What :func:`verify_record` accepts as a trust anchor: one raw ed25519
#: public key, several of them, or ``None`` for "the caller has none" --
#: which is a reason a record is unverified, never a reason to fall back
#: to the key the record names about itself.
TrustedKeys = Union[bytes, Sequence[bytes], None]

#: §6's ``recordVersion``.
RECORD_VERSION = 1

#: Domain tag on the signed bytes, so the preimage of a record signature
#: cannot be the preimage of anything else this engine signs.
SIGNATURE_PREFIX = b"flux-fidelity-record/1\x00"

#: §6's scope statement, verbatim. Not a template with slots: the whole
#: point of the field is that it says the same thing on every record.
SCOPE_CERTIFIES = (
    "statistical relation between the synthetic dataset and the committed state "
    "under the registered properties"
)

#: §6's ``doesNotCertify`` list, verbatim and in order. It is the
#: register-discipline guard: every limit a reader might otherwise assume
#: away is written down where they will read it.
SCOPE_DOES_NOT_CERTIFY: Tuple[str, ...] = (
    "privacy or re-identification resistance of the synthetic dataset",
    "fairness or any property of the source dataset itself",
    "any property of a system trained on or tested with the synthetic dataset",
    "that the synthetic dataset was generated independently of the source",
    "that the committed state is the whole source dataset",
    "predictive validity of any simulation outcome",
)

#: The generated scope statement, as it appears in every record.
SCOPE: Mapping[str, Any] = MappingProxyType(
    {
        "certifies": SCOPE_CERTIFIES,
        "doesNotCertify": list(SCOPE_DOES_NOT_CERTIFY),
    }
)


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


@runtime_checkable
class RecordSigner(Protocol):
    """The signature port. One implementation signs; one states it did not."""

    @property
    def alg(self) -> str:
        """The signature algorithm's wire name, written into the record."""

    @property
    def key_id(self) -> str:
        """The id of the key the signature was taken with."""

    def sign(self, payload: bytes) -> str:
        """Return the base64 signature over *payload*."""


@dataclass(frozen=True)
class Ed25519Signer:
    """§6's signer: ed25519 over the record's canonical bytes.

    Takes the 32-byte raw private key rather than a key object so the
    caller decides where key material comes from and this module never
    touches a file, an environment variable or a key store.
    """

    private_key: bytes
    key_id: str
    alg: str = "ed25519"

    def __post_init__(self) -> None:
        if len(self.private_key) != 32:
            raise FluxError(FLX_8002, "an ed25519 private key is 32 raw bytes")
        if not self.key_id.strip():
            raise FluxError(FLX_8002, "a signature names the key it was taken with")

    def sign(self, payload: bytes) -> str:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(self.private_key)
        return base64.b64encode(key.sign(payload)).decode("ascii")

    @property
    def public_key(self) -> bytes:
        """The 32-byte raw public key a verifier checks the signature with."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(self.private_key)
        return key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )


@dataclass(frozen=True)
class UnsignedFallbackSigner:
    """States that a record was not signed, rather than omitting the field.

    Used where ``cryptography`` is unavailable. :func:`verify_record`
    never accepts a record carrying this algorithm as verified -- it
    reports it as unsigned -- so the fallback cannot be mistaken for a
    weaker signature. It is a stated absence.
    """

    key_id: str = "unsigned"
    alg: str = "unsigned-fallback"

    def sign(self, payload: bytes) -> str:
        return ""


def signer_block(signer: RecordSigner, *, ephemeral: bool) -> Dict[str, Any]:
    """The record's top-level ``signer`` block: who took the signature.

    Written into the document **before** it is signed, so it is covered by
    the signature. ``publicKey`` is the signer's own 32-byte raw key as
    hex when it has one (``None`` for the unsigned fallback), and
    ``ephemeral`` says whether that key was generated for one invocation
    and belongs to nobody -- the difference between "this record is
    attributable" and "these bytes are internally consistent".
    """
    public_key = getattr(signer, "public_key", None)
    return {
        "alg": signer.alg,
        "keyId": signer.key_id,
        "publicKey": None if public_key is None else bytes(public_key).hex(),
        "ephemeral": bool(ephemeral),
    }


def record_signer_public_key(document: Mapping[str, Any]) -> Optional[bytes]:
    """The raw public key the record's ``signer`` block names, or ``None``.

    A convenience for a reader that wants to check a record's internal
    consistency. It is **not** a trust anchor: a record naming its own key
    verifies against itself no matter who wrote it, which is exactly what
    :func:`verify_record` refuses to accept as an answer.
    """
    signer = document.get("signer")
    if not isinstance(signer, Mapping):
        return None
    declared = signer.get("publicKey")
    if not isinstance(declared, str):
        return None
    try:
        return bytes.fromhex(declared)
    except ValueError:
        return None


def signing_payload(document: Mapping[str, Any]) -> bytes:
    """The bytes a record's signature is taken over.

    The record's canonical JSON with ``signature`` removed, under
    :data:`SIGNATURE_PREFIX`. Removing the field rather than blanking it
    keeps the payload identical whether the record has been signed yet or
    not, so signing and verifying hash the same bytes.
    """
    body = {key: value for key, value in document.items() if key != "signature"}
    return SIGNATURE_PREFIX + canonical_bytes(body)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyntheticReference:
    """§6's ``synthetic`` block: what was checked, and what produced it."""

    output_digest: str
    rows: int
    generator_digest: str

    def to_document(self) -> Dict[str, Any]:
        """The canonical form."""
        return {
            "outputDigest": self.output_digest,
            "rows": self.rows,
            "generatorDigest": self.generator_digest,
        }


@dataclass(frozen=True)
class FidelityRecord:
    """One fidelity record: §6's document, and the signer that closed it."""

    document: Mapping[str, Any]

    @property
    def h(self) -> str:
        """The seam digest the record is bound to."""
        return str(self.document["H"])

    @property
    def g(self) -> str:
        """The digest of the declarations the committed statistic was formed under."""
        return str(self.document["G"])

    @property
    def expose_id(self) -> str:
        """The emit this record answers for."""
        return str(self.document["reference"]["exposeId"])

    @property
    def signed(self) -> bool:
        """Whether the record carries a real signature rather than a stated absence."""
        signature = self.document.get("signature", {})
        return bool(signature.get("sig")) and signature.get("alg") != UnsignedFallbackSigner.alg

    @property
    def outcomes(self) -> Tuple[str, ...]:
        """Each declared property's outcome, in declaration order."""
        return tuple(str(row["outcome"]) for row in self.document["properties"])

    @property
    def within(self) -> bool:
        """Whether every property this record reports is within its margin."""
        return all(outcome == "within" for outcome in self.outcomes)

    def to_document(self) -> Dict[str, Any]:
        """A mutable copy of the record document."""
        return dict(self.document)


def build_record(
    *,
    compiled: CompiledFidelity,
    committed: CommittedState,
    evaluation: FidelityEvaluation,
    synthetic: SyntheticReference,
    product_ref: str,
    fluid_version: str,
    contract_digest: str,
    check_ordinal: int,
    signer: RecordSigner,
    ephemeral_key: bool = False,
    proof_system: str = "none",
    record_range: Optional[Sequence[int]] = None,
) -> FidelityRecord:
    """Build and sign one fidelity record (§6).

    Refuses (``FLX-8002``) to write a record for an evaluation that met a
    fail-closed condition: §5 says the check declines in those cases, and
    a record is the artefact that says a check *ran*. An evaluation whose
    properties are outside their margins does get a record -- that is a
    result, and the gate is what withholds the bundle over it.

    ``recordCount`` and ``recordRange`` are §6's alternatives. The count
    is written by default; pass *record_range* (a two-element ``[min,
    max]``) when the owner would rather publish the declared range than
    the exact number, which §4 allows.
    """
    if evaluation.refused:
        raise FluxError(
            FLX_8002,
            f"the fidelity check met {len(evaluation.refusals)} fail-closed conditions; "
            "there is no record to write",
        )
    if evaluation.expose_id != compiled.block.expose_id:
        raise FluxError(FLX_8002, "the evaluation answers for a different expose")
    if check_ordinal < 1:
        raise FluxError(FLX_8002, "a checkOrdinal counts from one")

    committed_block: Dict[str, Any] = {
        "ordinal": committed.ordinal,
        "C1": [entry.to_document() for entry in committed.c1],
        "C2": committed.c2,
        "committedAt": committed.committed_at,
        "checkOrdinal": check_ordinal,
    }
    if record_range is None:
        committed_block["recordCount"] = committed.record_count
    else:
        bounds = list(record_range)
        if len(bounds) != 2 or bounds[0] > bounds[1]:
            raise FluxError(FLX_8002, "a recordRange is an ascending pair")
        committed_block["recordRange"] = bounds

    document: Dict[str, Any] = {
        "recordVersion": RECORD_VERSION,
        "reference": {
            "productRef": product_ref,
            "exposeId": compiled.block.expose_id,
            "fluidVersion": fluid_version,
            "contractDigest": contract_digest,
        },
        "H": compiled.h.value,
        "G": compiled.g.value,
        "committedState": committed_block,
        "synthetic": synthetic.to_document(),
        "properties": [outcome.to_record_row() for outcome in evaluation.outcomes],
        "proofSystem": proof_system,
        "scope": {
            "certifies": SCOPE_CERTIFIES,
            "doesNotCertify": list(SCOPE_DOES_NOT_CERTIFY),
        },
        "columnsCovered": list(compiled.block.columns_covered),
        # Inside the signed body, never beside the signature: see this
        # module's docstring. *ephemeral_key* is the caller's statement
        # about where the key material came from -- this module never
        # generates one, so it cannot work that out for itself.
        "signer": signer_block(signer, ephemeral=ephemeral_key),
    }
    document["signature"] = {
        "alg": signer.alg,
        "keyId": signer.key_id,
        "sig": signer.sign(signing_payload(document)),
    }
    return FidelityRecord(document=MappingProxyType(document))


#: Every key §6 fixes at the top level of a record.
_REQUIRED_KEYS = (
    "H",
    "G",
    "columnsCovered",
    "committedState",
    "properties",
    "proofSystem",
    "recordVersion",
    "reference",
    "scope",
    "signature",
    "signer",
    "synthetic",
)


def load_record(document: Mapping[str, Any]) -> FidelityRecord:
    """Read a record back, refusing (``FLX-8002``) one that is not §6's shape.

    Checks the version, the top-level keys, and -- the one check that is
    not merely structural -- that the scope statement is the generated
    one. A record whose ``doesNotCertify`` list has been shortened is not
    a record with a smaller list; it is a different document wearing this
    one's name.
    """
    if not isinstance(document, Mapping):
        raise FluxError(FLX_8002, f"a record is a mapping, got {type(document).__name__}")
    missing = [key for key in _REQUIRED_KEYS if key not in document]
    if missing:
        raise FluxError(FLX_8002, f"the record is missing {', '.join(missing)}")
    if document["recordVersion"] != RECORD_VERSION:
        raise FluxError(
            FLX_8002,
            f"recordVersion {document['recordVersion']!r} is not {RECORD_VERSION}",
        )
    scope = document["scope"]
    if not isinstance(scope, Mapping):
        raise FluxError(FLX_8002, "the record's scope statement is not a mapping")
    if scope.get("certifies") != SCOPE_CERTIFIES:
        raise FluxError(FLX_8002, "the record's scope statement is not the generated one")
    if tuple(scope.get("doesNotCertify", ())) != SCOPE_DOES_NOT_CERTIFY:
        raise FluxError(
            FLX_8002,
            "the record's scope statement has a different list of what it does not cover",
        )
    properties = document["properties"]
    if not isinstance(properties, Sequence) or isinstance(properties, (str, bytes)):
        raise FluxError(FLX_8002, "the record's properties are not a list")
    for index, row in enumerate(properties):
        if not isinstance(row, Mapping) or row.get("outcome") not in ("within", "outside"):
            raise FluxError(FLX_8002, f"the record's property row {index} has no outcome")
    return FidelityRecord(document=MappingProxyType(dict(document)))


def _trust_anchors(trusted_keys: TrustedKeys) -> Tuple[Tuple[bytes, ...], List[str]]:
    """Normalise *trusted_keys* to raw 32-byte keys, plus the reasons any were refused."""
    if trusted_keys is None:
        return (), []
    candidates: Sequence[bytes]
    if isinstance(trusted_keys, (bytes, bytearray)):
        candidates = (bytes(trusted_keys),)
    else:
        candidates = [bytes(key) for key in trusted_keys]
    anchors = tuple(key for key in candidates if len(key) == 32)
    reasons = ["an ed25519 public key is 32 raw bytes"] if len(anchors) != len(candidates) else []
    return anchors, reasons


def verify_record(document: Mapping[str, Any], trusted_keys: TrustedKeys) -> List[str]:
    """Return the reasons *document* is not a verified record; empty means it is.

    *trusted_keys* is the caller's trust anchor -- one raw ed25519 public
    key or a sequence of them -- and the signature must verify under one
    of them. It is deliberately not read out of the record: a key a
    document carries about itself makes every self-consistent document
    verify, including one an attacker rewrote and re-signed with a key of
    their own. The record's own ``signer`` block is covered by the
    signature, so it says who the *signer claimed* to be; whether that
    signer is anyone the reader trusts is this argument's question.

    A list rather than a boolean, for the same reason
    :class:`~flux_engine.concordance.verifier.FidelityEvaluation` keeps
    its refusals: the gate reports every reason it withheld a bundle, not
    the first one it found. An unsigned-fallback record and a record whose
    signature does not verify are two different reasons and say so.
    """
    reasons: List[str] = []
    try:
        record = load_record(document)
    except FluxError as error:
        return [error.detail or error.code.title]

    signature = record.document.get("signature", {})
    if not isinstance(signature, Mapping):
        return ["the record's signature block is not a mapping"]
    signer = record.document.get("signer", {})
    if not isinstance(signer, Mapping):
        return ["the record's signer block is not a mapping"]
    alg = signature.get("alg")
    if alg == UnsignedFallbackSigner.alg:
        return ["the record states that it was not signed"]
    if alg != "ed25519":
        return [f"the record's signature algorithm {alg!r} is not one this verifier reads"]
    if signer.get("alg") != alg or signer.get("keyId") != signature.get("keyId"):
        return ["the record's signer block and its signature name different keys"]

    anchors, anchor_reasons = _trust_anchors(trusted_keys)
    reasons.extend(anchor_reasons)
    if not anchors:
        return [
            *reasons,
            "no trusted key was supplied to check the record's signature against",
        ]

    try:
        raw = base64.b64decode(str(signature.get("sig", "")), validate=True)
    except (ValueError, TypeError):
        return [*reasons, "the record's signature is not base64"]

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # pragma: no cover - cryptography is a declared dependency
        return [*reasons, "cryptography is not installed, so the signature cannot be checked"]

    payload = signing_payload(record.document)
    for anchor in anchors:
        try:
            Ed25519PublicKey.from_public_bytes(anchor).verify(raw, payload)
        except InvalidSignature:
            continue
        return reasons
    reasons.append("the record's signature does not verify against any trusted key")
    return reasons
