# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The seam attestation sidecar: what crossed the seam, and under what bytes.

One artifact per run, at ``run/seam-attestation.json``: for every emit,
the contract it was resolved against, the ordered column set it emitted,
the digests that bind the two, and the ``outputDigest`` the stream carries
(KERNEL-SPEC §8, SEAM-FIDELITY-SPEC §3).

**Nothing here writes into the bundle.** A bundle is the input a run is
answerable to; an engine that edited it could not be replayed against the
thing it was given. :func:`write_seam_attestation` and
:func:`copy_shipped_contract` both refuse a destination inside the bundle
directory (``FLX-3106``), and the refusal is a path comparison after
``resolve()``, so a symlink or a ``..`` cannot walk back in.

**The shipped contract is copied byte-for-byte.** SEAM-FIDELITY-SPEC §7:
"pack (staged copy; the ``.fluid.yml`` copied byte-for-byte)". The copy
here re-reads nothing and re-serialises nothing -- it writes the bytes the
resolution captured and then re-reads the destination and compares
``raw-bytes`` digests, so "byte-identical" is verified rather than
asserted.

**Wall-clock time arrives from the caller.** :mod:`flux_engine.seam` is a
kernel package and reads no clock; ``generated_at`` is a parameter, the
way :func:`flux_engine.core.artifact.write_artifact` requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flux_engine.core.artifact import write_artifact
from flux_engine.core.digest import DigestRef, digest_of_bytes, digest_of_document
from flux_engine.core.errors import FluxError, register
from flux_engine.seam.contract import CompiledExpose
from flux_engine.seam.emit import EmitResult, SeamDigests, seam_digests
from flux_engine.seam.resolve import ResolvedEmit

__all__ = [
    "ATTESTATION_FILENAME",
    "ATTESTATION_SCHEMA_VERSION",
    "FLX_3106",
    "SeamAttestation",
    "build_seam_attestation",
    "copy_shipped_contract",
    "seam_attestation_document",
    "shipped_contract_matches",
    "write_seam_attestation",
]

FLX_3106 = register(
    "FLX-3106",
    "run artifact would be written inside the bundle",
    "A bundle is the input a run is answerable to. Writing a run artifact "
    "into it would mean the next replay reads a directory the previous run "
    "changed, and the inputDigest of KERNEL-SPEC §11 would no longer name "
    "the bytes the run was given. Choose a destination outside the bundle "
    "directory.",
)

#: Where the sidecar lands, relative to the run directory.
ATTESTATION_FILENAME = "seam-attestation.json"

#: Bumped when the document's shape changes, so a consumer can key off it.
ATTESTATION_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class SeamAttestation:
    """One emit's crossing of the seam, ready to serialise.

    Every field is either a digest or a declaration -- there is no summary
    statistic here and no verdict. What this artifact says is "these bytes
    met these bytes"; whether that was good enough is the fidelity record's
    question, not this one's.
    """

    simulation_id: str
    emit_index: int
    product_ref: str
    expose_id: str
    fluid_version: str
    columns: Tuple[str, ...]
    types: Tuple[str, ...]
    contract_digest: DigestRef
    contract_file_digest: DigestRef
    seam: SeamDigests
    rows: int
    output_digest: DigestRef
    generator_digest: Optional[DigestRef] = None
    shipped_contract: Optional[str] = None

    def as_document(self) -> Dict[str, Any]:
        """The artifact payload, as canonical-ready JSON."""
        document: Dict[str, Any] = {
            "simulationId": self.simulation_id,
            "emitIndex": self.emit_index,
            "reference": {
                "productRef": self.product_ref,
                "exposeId": self.expose_id,
                "fluidVersion": self.fluid_version,
                "contractDigest": self.contract_digest.value,
                "contractFileDigest": self.contract_file_digest.value,
            },
            "columns": list(self.columns),
            "types": list(self.types),
            "H": self.seam.h.value,
            "G": self.seam.g.value,
            "synthetic": {
                "rows": self.rows,
                "outputDigest": self.output_digest.value,
                "generatorDigest": (
                    None if self.generator_digest is None else self.generator_digest.value
                ),
            },
            "shippedContract": self.shipped_contract,
        }
        return document


def build_seam_attestation(
    resolved: ResolvedEmit,
    expose: CompiledExpose,
    result: EmitResult,
    *,
    generator_digest: Optional[DigestRef] = None,
    shipped_contract: Optional[str] = None,
) -> SeamAttestation:
    """Assemble the attestation for one resolved emit and its closed stream.

    ``H`` and ``G`` are recomputed here from the resolution's own contract
    document, emit entry, extension entry and compiled column order,
    rather than taken from a caller: an attestation that quoted digests
    somebody else computed would attest to their arithmetic, not to the
    seam.

    **The extension entry is part of the document ``H`` covers.**
    SEAM-FIDELITY-SPEC §3 defines exactly one ``H``, over ``{"contract",
    "emit", "extension", "columns"}``, where ``extension`` is the emit's
    entry in ``extensions["com.agentics.flux/1"].emits``. Recomputing it
    without that argument produced a second, different ``H`` that no part
    of the engine ever used -- it matched neither ``plan.json``, nor the
    fidelity record, nor the seed root -- and it collapsed ``G`` onto
    ``H``, because with the extension dropped the emit entry no longer
    carries the ``fidelity`` block ``G``'s excluded-set strip operates on.
    Two digests that can never disagree make §8's reuse rule undecidable
    from this artifact.
    """
    if result.expose_id != expose.expose_id:
        raise FluxError(
            FLX_3106,
            f"emit result is for expose {result.expose_id!r}, the compiled expose is "
            f"{expose.expose_id!r}",
        )
    digests = seam_digests(
        resolved.contract_document, resolved.emit, expose.column_names, resolved.extension
    )
    return SeamAttestation(
        simulation_id=resolved.simulation_id,
        emit_index=resolved.emit_index,
        product_ref=resolved.product_ref,
        expose_id=expose.expose_id,
        fluid_version=resolved.fluid_version,
        columns=expose.column_names,
        types=expose.type_names,
        contract_digest=resolved.contract_digest,
        contract_file_digest=resolved.contract_file_digest,
        seam=digests,
        rows=result.rows,
        output_digest=result.output_digest,
        generator_digest=generator_digest,
        shipped_contract=shipped_contract,
    )


def seam_attestation_document(
    attestations: Sequence[SeamAttestation],
    *,
    kernel_version: str,
    input_digest: Optional[DigestRef] = None,
    plan_digest: Optional[DigestRef] = None,
) -> Dict[str, Any]:
    """Build the sidecar payload for every emit of one run.

    ``emits`` is ordered by ``emitIndex`` -- KERNEL-SPEC §3 forms the run's
    seed material from the per-emit digests "in ``emits[]`` order", so a
    reader of this document sees them in the order that mattered.
    """
    ordered: List[SeamAttestation] = sorted(attestations, key=lambda item: item.emit_index)
    return {
        "attestationVersion": int(ATTESTATION_SCHEMA_VERSION),
        "kernelVersion": kernel_version,
        "inputDigest": None if input_digest is None else input_digest.value,
        "planDigest": None if plan_digest is None else plan_digest.value,
        "emits": [item.as_document() for item in ordered],
    }


def _refuse_inside_bundle(destination: Path, bundle_directory: Path) -> Path:
    """Return *destination* resolved, or raise ``FLX-3106`` if it is in the bundle."""
    resolved_destination = Path(destination).resolve()
    resolved_bundle = Path(bundle_directory).resolve()
    if resolved_destination == resolved_bundle or resolved_destination.is_relative_to(
        resolved_bundle
    ):
        raise FluxError(
            FLX_3106,
            f"{resolved_destination} is inside the bundle at {resolved_bundle}",
        )
    return resolved_destination


def write_seam_attestation(
    run_directory: Path,
    attestations: Sequence[SeamAttestation],
    *,
    bundle_directory: Path,
    kernel_version: str,
    generated_at: str,
    input_digest: Optional[DigestRef] = None,
    plan_digest: Optional[DigestRef] = None,
    generated_by: str = "flux-engine",
) -> Tuple[Path, DigestRef]:
    """Write ``run/seam-attestation.json`` and return its path and digest.

    The returned digest is a ``jcs-document`` digest over the **payload**,
    not over the file: the file carries the wall-clock envelope fields
    :data:`flux_engine.core.artifact.ENVELOPE_NON_DIGEST_FIELDS` masks, and two runs
    over identical inputs must agree on this digest.
    """
    target = _refuse_inside_bundle(Path(run_directory), bundle_directory)
    target.mkdir(parents=True, exist_ok=True)
    payload = seam_attestation_document(
        attestations,
        kernel_version=kernel_version,
        input_digest=input_digest,
        plan_digest=plan_digest,
    )
    path = write_artifact(
        target / ATTESTATION_FILENAME,
        dict(payload),
        kind="RunAttestation",
        schema_version=ATTESTATION_SCHEMA_VERSION,
        generated_at=generated_at,
        generated_by=generated_by,
    )
    return path, digest_of_document(payload, subject=ATTESTATION_FILENAME)


def copy_shipped_contract(
    resolved: ResolvedEmit,
    destination_directory: Path,
    *,
    bundle_directory: Path,
    filename: Optional[str] = None,
) -> Tuple[Path, DigestRef]:
    """Stage the referenced ``.fluid.yml`` byte-for-byte (SEAM-FIDELITY-SPEC §7).

    Writes the bytes the resolution captured, re-reads the destination, and
    compares ``raw-bytes`` digests before returning: if the two differ the
    copy raises rather than reporting a success nobody checked. Refuses a
    destination inside the bundle (``FLX-3106``).
    """
    target_directory = _refuse_inside_bundle(Path(destination_directory), bundle_directory)
    target_directory.mkdir(parents=True, exist_ok=True)
    target = target_directory / (filename or resolved.contract_path.name)
    target.write_bytes(resolved.contract_bytes)

    written = digest_of_bytes(target.read_bytes(), subject=target.name)
    if written != resolved.contract_file_digest:
        raise FluxError(
            FLX_3106,
            f"staged copy of {resolved.contract_path.name} digests to {written.value}; "
            f"the bundle's copy is {resolved.contract_file_digest.value}",
        )
    return target, written


def shipped_contract_matches(resolved: ResolvedEmit, staged: Path) -> bool:
    """Whether *staged* still holds the exact bytes the bundle shipped."""
    if not staged.is_file():
        return False
    return digest_of_bytes(staged.read_bytes(), subject=staged.name) == (
        resolved.contract_file_digest
    )
