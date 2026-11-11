# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Packing a bundle and a run into one evidence archive -- source tree untouched.

:func:`pack_evidence` is a thin, disk-only wrapper around
``flux_engine.spec.bundle.pack`` (the vendored-or-published FLUX bundler,
RFC-09). Everything this module adds is:

1. **Staging.** ``spec.bundle.pack`` writes ``flux-manifest.json`` *into
   the directory it packs* (see its own docstring). Packing the author's
   actual bundle directory in place would mutate the source tree every
   time evidence is produced. This module copies the bundle plus a run's
   output directory into a temporary staging root first, packs *that*, and
   discards it -- the caller's ``bundle_dir`` is read, never written.
2. **A second attestation.** ``spec.bundle.pack`` already writes one
   in-toto Statement describing what the *offline validator* could prove
   (``core``/``enterprise`` -- never ``runtime``: the manifest schema's
   ``attestation.profile`` enum has no ``"runtime"`` value, so a packer
   claiming it would fail schema validation before it could even be
   written). :func:`pack_evidence` writes a **second**, separate Statement
   under a distinct ``predicateType`` describing the runtime evidence --
   the run digest, the kernel version that produced it -- without ever
   touching ``manifest["attestation"]["profile"]``. The two Statements
   answer different questions and neither is a rephrasing of the other.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Tuple, Union

from jsonschema import Draft202012Validator, FormatChecker

from flux_engine.core.canonical import sha256_hex
from flux_engine.core.digest import DigestRef, digest_of_bytes
from flux_engine.spec.bundle import pack as _spec_pack

__all__ = [
    "RUNTIME_PREDICATE_TYPE",
    "RUNTIME_STATEMENT_SUFFIX",
    "EvidencePackResult",
    "default_flux_validator",
    "pack_evidence",
    "verify_evidence",
]

#: Distinct from ``flux_spec.bundle.PREDICATE_TYPE`` (the offline-validator
#: attestation) by construction -- a reader can tell the two Statements
#: apart without inspecting anything but this string.
RUNTIME_PREDICATE_TYPE = "https://agenticstiger.github.io/flux/attestation/runtime-evidence/v1"
RUNTIME_STATEMENT_SUFFIX = ".runtime-evidence.json"

_MANIFEST_NAME = "flux-manifest.json"


def _flux_schema_path() -> Path:
    """The FLUX schema of whichever backend ``flux_engine.spec`` selected.

    Delegates rather than re-deriving: the shim asks the active backend for
    the constant it already computed, so the validator's code and the schema
    it validates against always come from the same copy. The probe this
    replaced guessed ``flux_spec/schema/``; the published distribution keeps
    its data under ``flux_spec/data/``, so with the distribution installed
    this returned the *vendored* schema while ``describe`` reported
    ``spec_source: flux_spec``.
    """
    from flux_engine.spec import flux_schema_path

    return flux_schema_path()


def default_flux_validator() -> Draft202012Validator:
    """Build the offline FLUX validator :func:`pack_evidence` uses when none is supplied."""
    schema = json.loads(_flux_schema_path().read_text())
    return Draft202012Validator(schema, format_checker=FormatChecker())


@dataclass(frozen=True)
class EvidencePackResult:
    """What :func:`pack_evidence` produced, or why it did not.

    ``manifest`` is the parsed ``flux-manifest.json`` content; the staged
    copy it was written into is a temporary directory that no longer
    exists by the time this result is returned (see the module docstring's
    staging discussion), so ``manifest_path`` names a durable copy written
    beside the archive -- byte-identical to the one inside it, and the one
    an operator actually signs.
    """

    ok: bool
    archive: Optional[Path] = None
    manifest: Optional[Mapping[str, Any]] = None
    manifest_path: Optional[Path] = None
    build_statement_path: Optional[Path] = None
    runtime_statement_path: Optional[Path] = None
    problems: Tuple[str, ...] = field(default_factory=tuple)


def _sha256_file(path: Path) -> str:
    return digest_of_bytes(path.read_bytes(), subject=path.name).hex


def _runtime_statement(
    archive: Path,
    manifest: Mapping[str, Any],
    *,
    run_digest: DigestRef,
    kernel_version: str,
    generated_at: str,
) -> Mapping[str, Any]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": archive.name, "digest": {"sha256": _sha256_file(archive)}}],
        "predicateType": RUNTIME_PREDICATE_TYPE,
        "predicate": {
            "schemaId": manifest["schemaId"],
            "merkleRoot": manifest["merkleRoot"],
            "kernelVersion": kernel_version,
            "runDigest": run_digest.value,
            "generatedAt": generated_at,
        },
    }


#: The line ``spec.bundle.pack`` prints naming the manifest to sign.
_SIGN_HINT_MARKER = "sign it: ssh-keygen"


def _retarget_signing_hint(output: str, manifest_path: Path) -> str:
    """Rewrite the packer's signing hint to name a path that still exists."""
    lines = []
    for line in output.splitlines(keepends=True):
        if _SIGN_HINT_MARKER in line:
            indent = line[: len(line) - len(line.lstrip())]
            lines.append(f"{indent}sign it: ssh-keygen -Y sign -f <key> -n flux {manifest_path}\n")
        else:
            lines.append(line)
    return "".join(lines)


def _symlink_entries(directory: Path) -> Tuple[str, ...]:
    """Every symlink under *directory*, as refusal messages. Empty means clean.

    The unpack side (``spec.bundle._extract_safely``) refuses symlinks and
    special files outright; without the same guard on the way in, a link
    inside a bundle was silently dereferenced -- digested, manifested,
    Merkle-rooted, in-toto attested, and written into the shipped archive
    as a regular file. Anything readable by the packing process (an ssh
    key, a .env, the source dataset a fidelity record promises never
    leaves the boundary) could be exfiltrated into a distributed, signed
    artifact by a relative link that is invisible in a YAML review.

    Message shape mirrors ``_extract_safely``'s so the two halves of the
    pair read alike.
    """
    if not directory.is_dir():
        return ()
    problems = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            rel = path.relative_to(directory).as_posix()
            problems.append(f"unsupported bundle entry (symlink): {rel}")
    return tuple(problems)


def _skip_output_directory(out_dir: Path) -> Callable[[str, List[str]], List[str]]:
    """A ``shutil.copytree`` ignore callback that leaves the pack's own output out.

    ``-o`` may name a directory inside the bundle, and ``fluxsim run . --ship -o
    out`` run from the bundle directory does exactly that. That directory holds
    what earlier runs shipped. Staging it with the bundle packed the previous
    archive inside the new one, so every re-run grew the archive by one more
    pack and moved the Merkle root for a reason that has nothing to do with the
    bundle. The output directory is a product of packing, never an input to it.
    """
    resolved = out_dir.resolve()

    def ignore(directory: str, names: List[str]) -> List[str]:
        here = Path(directory)
        return [name for name in names if (here / name).resolve() == resolved]

    return ignore


def _occupied_by_another_run(
    out_dir: Path, bundle_name: str, run_digest: DigestRef
) -> Optional[str]:
    """Say why *out_dir* already holds a different run's evidence, or ``None``.

    Every file this function writes is named from the bundle directory's
    basename alone -- ``<name>.flux.tgz``, ``<name>.flux-manifest.json``,
    ``<name>.attestation.json``, ``<name>.runtime-evidence.json`` -- so a
    second ship of a *different* run into one shared ``dist/`` replaced
    all four with exit 0 and no warning, destroying the manifest an
    operator may already have signed and leaving that signature pointing
    at an archive it never covered. Re-packing the same run is unchanged
    (an evidence pack is meant to be reproducible); replacing a different
    one is refused unless the caller says to.

    The identity compared is the ``runDigest`` in the durable runtime
    Statement, not the archive's Merkle root: the root legitimately moves
    between two packs of one run, because ``EVIDENCE.md`` carries a
    generation timestamp.

    A Statement that is there but cannot be read is treated as an
    occupant, never as an empty directory -- see the comment on the
    handler below.
    """
    statement_path = out_dir / f"{bundle_name}{RUNTIME_STATEMENT_SUFFIX}"
    if not statement_path.is_file():
        return None
    try:
        existing = json.loads(statement_path.read_text())
        existing_digest = str(existing["predicate"]["runDigest"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        # Fail closed. An unreadable Statement is the one case where the
        # on-disk evidence is already suspect, and returning ``None`` here
        # meant "this directory is free" -- so the guard added to stop a
        # second run silently replacing four artifacts stepped aside
        # precisely when a truncated or hand-edited file was sitting in
        # them. An unknown occupant is an occupant.
        return (
            f"{statement_path} exists but does not name a run digest "
            f"({type(error).__name__}), so {out_dir} cannot be shown to be free. Packing "
            f"{run_digest.value} here would replace {bundle_name}.flux.tgz, its manifest "
            "and both in-toto Statements. Choose another output directory, or pass "
            "force=True to replace them deliberately."
        )
    if existing_digest == run_digest.value:
        return None
    return (
        f"{out_dir} already holds evidence for run digest {existing_digest}; packing "
        f"{run_digest.value} here would replace {bundle_name}.flux.tgz, its manifest and "
        "both in-toto Statements. Choose another output directory, or pass force=True to "
        "replace them deliberately."
    )


def pack_evidence(
    bundle_dir: Union[str, Path],
    run_dir: Union[str, Path],
    out_dir: Union[str, Path],
    *,
    run_digest: DigestRef,
    kernel_version: str,
    generated_at: str,
    validator: Optional[Draft202012Validator] = None,
    force: bool = False,
) -> EvidencePackResult:
    """Pack *bundle_dir* plus *run_dir* into one evidence archive under *out_dir*.

    *bundle_dir* is copied, never modified: everything ``spec.bundle.pack``
    writes (``flux-manifest.json``, the archive, its in-toto Statement)
    lands in a temporary staging copy that is discarded once this function
    returns. When *out_dir* lies inside *bundle_dir* it is left out of that
    copy: it holds the packs earlier runs wrote, which are products of packing
    and no part of the bundle. *run_dir*'s contents are staged alongside the bundle under a
    ``run/`` subdirectory, so the packed archive's manifest covers both the
    bundle documents and the run's own artifacts (lineage, fidelity
    records, the accuracy record) under one Merkle root.

    Returns ``ok=False`` with ``problems`` naming why when the staged
    bundle does not validate; nothing is written to *out_dir* in that case.
    The same is true when *out_dir* already holds evidence for a different
    run and *force* is not set (see :func:`_occupied_by_another_run`), and
    when *out_dir* is *bundle_dir* itself.
    """
    bundle_dir = Path(bundle_dir).resolve()
    run_dir = Path(run_dir).resolve()
    out_dir = Path(out_dir)
    validator = validator or default_flux_validator()

    if not bundle_dir.is_dir():
        return EvidencePackResult(ok=False, problems=(f"{bundle_dir}: not a directory",))

    # The staging copy above keeps the packer out of the source bundle. It
    # cannot keep the pack's *output* out of it: `out_dir` is the
    # operator's own `-o`, and pointing it at the bundle drops four
    # evidence files into the directory being attested, where the loader
    # then reads them (`FLX-3108`) and the next archive packs a stale copy
    # of its own siblings.
    if out_dir.resolve() == bundle_dir:
        return EvidencePackResult(
            ok=False,
            problems=(
                f"{out_dir}: an evidence pack cannot be written into the bundle it "
                "packs; choose an output directory outside the bundle",
            ),
        )

    unsafe = [*_symlink_entries(bundle_dir), *_symlink_entries(run_dir)]
    if unsafe:
        return EvidencePackResult(ok=False, problems=tuple(unsafe))

    occupied = _occupied_by_another_run(out_dir, bundle_dir.name, run_digest)
    if occupied is not None and not force:
        return EvidencePackResult(ok=False, problems=(occupied,))

    with tempfile.TemporaryDirectory(prefix="flux-evidence-") as tmp:
        staged = Path(tmp) / bundle_dir.name
        # symlinks=True: a link is staged as a link, never as a copy of
        # whatever it points at -- so a link that appeared between the
        # scan above and this copy reaches the staging tree *as a link*,
        # where the re-scan below finds it.
        #
        # The packer's own walk is not that second guard, whatever the
        # comment here used to claim: ``bundle_files`` selects with
        # ``Path.is_file()``, which follows symlinks, so a staged link is
        # dereferenced, digested, Merkle-rooted and written into the
        # archive as an ordinary file. Closing the window the first scan
        # leaves open therefore takes a scan of the tree that is actually
        # about to be packed, here, before ``_spec_pack`` ever sees it.
        shutil.copytree(bundle_dir, staged, symlinks=True, ignore=_skip_output_directory(out_dir))
        if run_dir.is_dir():
            shutil.copytree(run_dir, staged / "run", dirs_exist_ok=True, symlinks=True)

        staged_unsafe = _symlink_entries(staged)
        if staged_unsafe:
            return EvidencePackResult(ok=False, problems=tuple(staged_unsafe))

        # `spec.bundle.pack` prints a "sign it: ssh-keygen ... <dir>/
        # flux-manifest.json" hint naming the directory it packed -- which
        # here is this staging copy, deleted before the operator can read
        # the line. Capture its output and rewrite that one line to name
        # the durable copy written below, so the last instruction `fluxsim
        # ship` prints is one that can actually be run.
        manifest_path = out_dir / f"{staged.name}.{_MANIFEST_NAME}"
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            exit_code = _spec_pack(staged, out_dir, validator)
        print(_retarget_signing_hint(captured.getvalue(), manifest_path), end="")
        if exit_code != 0:
            return EvidencePackResult(
                ok=False, problems=("staged bundle failed validation; see the pack() output above",)
            )

        archive = out_dir / f"{staged.name}.flux.tgz"
        manifest = json.loads((staged / _MANIFEST_NAME).read_text())
        build_statement_path = out_dir / f"{staged.name}.attestation.json"

        runtime_statement = _runtime_statement(
            archive,
            manifest,
            run_digest=run_digest,
            kernel_version=kernel_version,
            generated_at=generated_at,
        )
        runtime_statement_path = out_dir / f"{staged.name}{RUNTIME_STATEMENT_SUFFIX}"
        runtime_statement_path.write_text(
            json.dumps(runtime_statement, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        # The manifest is what a signature covers, and until now the only
        # copy of it lived in the staging directory this block is about to
        # delete -- so `spec.bundle.pack`'s own "sign it: ssh-keygen -Y sign
        # ... <staging>/flux-manifest.json" named a path that no longer
        # existed by the time the operator read it. Copy it out beside the
        # archive, byte-identical to the one inside it.
        manifest_path.write_bytes((staged / _MANIFEST_NAME).read_bytes())

    return EvidencePackResult(
        ok=True,
        archive=archive,
        manifest=manifest,
        manifest_path=manifest_path,
        build_statement_path=build_statement_path,
        runtime_statement_path=runtime_statement_path,
    )


def verify_evidence(
    archive: Union[str, Path], *, validator: Optional[Draft202012Validator] = None
) -> int:
    """Re-verify a packed archive offline. Thin pass-through to ``spec.bundle.verify``.

    Exists so a caller of this module never needs to import
    ``flux_engine.spec.bundle`` directly for the read side of the pair
    :func:`pack_evidence` writes with.
    """
    from flux_engine.spec.bundle import verify as _spec_verify

    validator = validator or default_flux_validator()
    return _spec_verify(Path(archive), validator)


def _bundle_directory_digest(directory: Path) -> str:
    """Return a stable digest over every file's bytes under *directory*, sorted by path.

    Test-only helper: confirms a source bundle tree is byte-unchanged after
    :func:`pack_evidence` runs, without depending on filesystem mtimes.

    Built on :func:`flux_engine.core.canonical.sha256_hex`, not on a local
    ``hashlib`` import: KERNEL-SPEC §2 makes ``core.canonical`` the only
    module that hashes, and a second hashing site in shipped code is
    exactly what that containment rule exists to prevent -- it can drift
    from the canonicaliser on framing, ordering or encoding with no gate
    noticing.
    """
    material = b"".join(
        path.relative_to(directory).as_posix().encode("utf-8") + b"\x00" + path.read_bytes()
        for path in sorted(p for p in directory.rglob("*") if p.is_file())
    )
    return sha256_hex(material)
