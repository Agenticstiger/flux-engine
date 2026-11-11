# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The choke point for the enveloped artifacts flux_engine writes to disk.

Mirrors ``fluid_build.cli.artifact_writer`` / ``artifact_envelope``: one
place, one envelope shape, so every generated file -- plan output, run
attestation, fidelity record, evidence manifest -- is instantly
recognisable and carries a stable schema_version a future migration can
key off.

**Which kinds go through it today.** ``RunAttestation``
(:mod:`flux_engine.seam.attest`), ``AccuracyRecord``
(:mod:`flux_engine.accuracy.accuracy_record`) and ``GoldenSnapshot``
(:mod:`flux_engine.stages.verify`) are written by this function. The
remaining kinds in :data:`EnvelopeKind` are still written by their own
producers -- ``PlanArtifact`` in ``stages/plan.py``, the fidelity record in
``stages/ship.py``, the manifest in ``evidence/pack.py``, and
``calibrate/provenance.py``, which says in its own docstring that it
mirrors this envelope's byte shape and then re-implements it. That is a
real gap, not a design: a fifth envelope field or a different indent would
desynchronise those files from the rest. ``tests/stages`` pins the list of
producers that bypass this function, so it can shrink and cannot silently
grow.

Kernel modules (see the "kernel packages" import-linter contract in
pyproject.toml) may not call the wall clock, so ``generated_at`` is always
supplied by the caller -- the actual ``datetime.now()`` read happens at
the CLI / engine-facade layer, never inside ``flux_engine.core``. That
keeps this module itself free of non-deterministic calls while still
producing a real wall-clock timestamp in every artifact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Literal, Union

__all__ = ["ENVELOPE_NON_DIGEST_FIELDS", "EnvelopeKind", "write_artifact"]

#: Every artifact kind flux_engine writes. Kept closed (a Literal, not a
#: bare ``str``) so a typo in a caller is a type-check failure, not a
#: silently-accepted new kind nothing downstream recognises.
EnvelopeKind = Literal[
    "PlanArtifact",
    "RunAttestation",
    "FidelityRecord",
    "AccuracyRecord",
    "EvidenceManifest",
    "GoldenSnapshot",
    "CalibrationProvenance",
]

#: Fields present in every envelope that must be excluded from any
#: content-addressed digest computed over an artifact's bytes. Both are
#: wall-clock / identity fields that would otherwise make two runs over
#: identical inputs produce different digests -- this is the envelope-only
#: subset; ``flux_engine.core.canonical.NON_DIGEST_FIELDS`` is the
#: five-member superset the digest mask actually uses, and nothing outside
#: this module reads the constant below. Two constants under one name is
#: how a reader comes to believe the envelope mask is the digest mask.
ENVELOPE_NON_DIGEST_FIELDS = frozenset({"generated_at", "generated_by"})


def write_artifact(
    path: Union[str, Path],
    obj: Dict[str, Any],
    *,
    kind: EnvelopeKind,
    schema_version: str,
    generated_at: str,
    generated_by: str = "flux-engine",
) -> Path:
    """Write *obj* to *path* as sorted-key JSON with the standard envelope.

    The envelope has exactly four fields: ``schema_version``, ``kind``,
    ``generated_at``, ``generated_by``. Payload keys from *obj* follow.
    The file is written with ``sort_keys=True`` and a trailing newline so
    two runs over identical inputs (modulo
    :data:`ENVELOPE_NON_DIGEST_FIELDS`) produce
    byte-identical output.

    ``generated_at`` is a required keyword, not defaulted internally --
    see the module docstring for why the wall-clock read lives at the
    call site rather than in this kernel module.
    """
    envelope: Dict[str, Any] = {
        "schema_version": schema_version,
        "kind": kind,
        "generated_at": generated_at,
        "generated_by": generated_by,
    }
    merged: Dict[str, Any] = {**envelope, **obj}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(merged, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return p
