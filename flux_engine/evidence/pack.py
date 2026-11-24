# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Packing a bundle and a run into one evidence archive -- source tree untouched.

:func:`pack_evidence` is a thin, disk-only wrapper around
``flux_engine.spec.bundle.pack`` (the FLUX bundler ``flux_spec`` publishes,
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
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Tuple, Union

from jsonschema import Draft202012Validator, FormatChecker

from flux_engine.core.canonical import sha256_hex
from flux_engine.core.digest import DigestRef, digest_of_bytes
from flux_engine.safeio import (
    read_bytes_nofollow,
    read_text_nofollow,
    require_real_directory,
    write_bytes_atomic,
    write_text_atomic,
)
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
    """The FLUX schema the ``flux_spec`` distribution ships.

    Delegates rather than re-deriving: the shim asks the distribution for
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


#: The directory ``plan`` stages a run's execution in (:mod:`flux_engine.stages.staging`): the
#: engine's working copy of what ``apply`` commits, never a record of a run, and never packed.
STAGE_DIRNAME = ".stage"


def _is_staged_leftover(name: str) -> bool:
    """A ``.<name>-XXXX.tmp`` file a killed write left beside the file it was staging.

    :func:`flux_engine.safeio.write_bytes_atomic` stages every file under such a
    name and renames it. A crash between the two leaves the staged file behind;
    it is the engine's own litter, not a record of the run, and it never goes
    into a pack. A ``.DS_Store`` file, which a file manager writes into any folder it
    opens, is left out the same way.
    """
    return (name.startswith(".") and name.endswith(".tmp")) or name == ".DS_Store"


def _skip_verified_files(
    run_dir: Path, names: Collection[str]
) -> Callable[[str, List[str]], List[str]]:
    """A ``shutil.copytree`` ignore callback that leaves out the files the caller already holds.

    The caller read each of them, checked it and handed over the bytes
    (``verified_run_files``), which are written into the staging tree after the
    copy. The copy never opens those names, so one that was swapped for a link or a
    pipe after the check is neither followed nor waited on.
    """
    held = frozenset(names)

    def ignore(directory: str, entries: List[str]) -> List[str]:
        here = Path(directory)
        return [
            entry
            for entry in entries
            if (here == run_dir and (entry in held or entry == STAGE_DIRNAME))
            or _is_staged_leftover(entry)
        ]

    return ignore


def _verified_files(verified_run_files: Optional[Mapping[str, bytes]]) -> Dict[str, bytes]:
    """The caller's checked files; ``ValueError`` for a name that is not a file in the run directory."""
    verified = dict(verified_run_files or {})
    for name in verified:
        if name in ("", ".", "..") or Path(name).name != name:
            raise ValueError(
                f"verified_run_files names {name!r}, which is not a file directly inside the run"
            )
    return verified


def _copy_run_directory(run_dir: Path, staged: Path, verified: Mapping[str, bytes]) -> None:
    """Stage *run_dir* under ``run/``, without the files the caller holds checked bytes of."""
    if run_dir.is_dir():
        skip = _skip_verified_files(run_dir, verified)
        shutil.copytree(run_dir, staged / "run", dirs_exist_ok=True, symlinks=True, ignore=skip)


def _stage_verified_files(staged: Path, verified: Mapping[str, bytes]) -> Tuple[str, ...]:
    """Write *verified* into the staged run directory; messages when the bundle is in the way.

    Called after the scan that refused every link in the staged tree, so each name is
    a regular file or absent, and replacing it writes into the staging tree only.
    """
    for name, data in sorted(verified.items()):
        target = staged / "run" / name
        if (target.parent.exists() and not target.parent.is_dir()) or (
            target.exists() and not target.is_file()
        ):
            return (f"run/{name}: the bundle holds something at this path that is in the way",)
        write_bytes_atomic(target, data)
    return ()


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
    if not statement_path.is_file() and not statement_path.is_symlink():
        return None
    try:
        existing = json.loads(read_text_nofollow(statement_path))
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
    verified_run_files: Optional[Mapping[str, bytes]] = None,
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

    *verified_run_files* maps a file name directly inside *run_dir* to the bytes the caller
    checked. The staged copy holds those bytes and never opens the name on disk, so a file
    changed after the check cannot reach the archive. Any other name is a ``ValueError``.

    Returns ``ok=False`` with ``problems`` naming why when the staged
    bundle does not validate; nothing is written to *out_dir* in that case.
    The same is true when *out_dir* already holds evidence for a different
    run and *force* is not set (see :func:`_occupied_by_another_run`), and
    when *out_dir* is *bundle_dir* itself.
    """
    verified = _verified_files(verified_run_files)
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

    # A link in the place the four outputs go would send them wherever it points
    # (FLX-2118). Checked before anything is read or staged.
    require_real_directory(out_dir)

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
        _copy_run_directory(run_dir, staged, verified)

        # The checked bytes go in only once the scan has found no link in the staged tree.
        staged_unsafe = _symlink_entries(staged) or _stage_verified_files(staged, verified)
        if staged_unsafe:
            return EvidencePackResult(ok=False, problems=tuple(staged_unsafe))

        # `spec.bundle.pack` prints a "sign it: ssh-keygen ... <dir>/
        # flux-manifest.json" hint naming the directory it packed -- which
        # here is this staging copy, deleted before the operator can read
        # the line. Capture its output and rewrite that one line to name
        # the durable copy written below, so the last instruction `fluxsim
        # ship` prints is one that can actually be run.
        manifest_path = out_dir / f"{staged.name}.{_MANIFEST_NAME}"
        # The packer writes the archive and the build statement itself, with plain
        # opens that follow a link at either name. It is pointed at a private
        # directory instead, and each output is moved into the real one below
        # through a staged rename, which replaces a link rather than following it.
        private_out = Path(tmp) / "out"
        # The packer validates the staged tree with flux_spec's own read, which
        # bounds nothing, so the tree it is about to read is held to the read's
        # bounds first: the same tree, after the scan, so nothing can change
        # between the check and the pack.
        bounded = _refuse_out_of_bounds(staged)
        if bounded:
            return EvidencePackResult(ok=False, problems=bounded)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            exit_code = _spec_pack(staged, private_out, validator)
        # Each printed path is rebuilt from the real directory the way the packer
        # builds it from its own, so `-o .` prints `name.flux.tgz`, not `./name.flux.tgz`.
        printed = captured.getvalue().replace(str(private_out) + os.sep, str(out_dir / "x")[:-1])
        print(_retarget_signing_hint(printed, manifest_path), end="")
        if exit_code != 0:
            return EvidencePackResult(
                ok=False, problems=("staged bundle failed validation; see the pack() output above",)
            )

        archive = out_dir / f"{staged.name}.flux.tgz"
        private_archive = private_out / archive.name
        manifest = json.loads((staged / _MANIFEST_NAME).read_text())
        build_statement_path = out_dir / f"{staged.name}.attestation.json"

        runtime_statement = _runtime_statement(
            private_archive,
            manifest,
            run_digest=run_digest,
            kernel_version=kernel_version,
            generated_at=generated_at,
        )
        runtime_statement_path = out_dir / f"{staged.name}{RUNTIME_STATEMENT_SUFFIX}"
        require_real_directory(out_dir, create=True)
        write_bytes_atomic(archive, read_bytes_nofollow(private_archive))
        write_bytes_atomic(
            build_statement_path, read_bytes_nofollow(private_out / build_statement_path.name)
        )
        write_text_atomic(
            runtime_statement_path, json.dumps(runtime_statement, indent=2, sort_keys=True) + "\n"
        )

        # The manifest is what a signature covers, and until now the only
        # copy of it lived in the staging directory this block is about to
        # delete -- so `spec.bundle.pack`'s own "sign it: ssh-keygen -Y sign
        # ... <staging>/flux-manifest.json" named a path that no longer
        # existed by the time the operator read it. Copy it out beside the
        # archive, byte-identical to the one inside it.
        write_bytes_atomic(manifest_path, (staged / _MANIFEST_NAME).read_bytes())

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
    """Re-verify a packed archive offline, through ``spec.bundle.verify``.

    Exists so a caller of this module never needs to import
    ``flux_engine.spec.bundle`` directly for the read side of the pair
    :func:`pack_evidence` writes with. ``verify`` re-validates the archive's
    documents with flux_spec's own read, which bounds nothing, and an archive
    can come from anywhere: its documents are held to the read's bounds first
    (:func:`_archive_out_of_bounds`), and an archive that holds one past them
    is refused, in ``verify``'s own words, before flux_spec reads a document.
    """
    from flux_engine.spec.bundle import verify as _spec_verify

    problems = _archive_out_of_bounds(Path(archive))
    if problems:
        print(f"[FAIL] {archive}")
        for problem in problems[:10]:
            print(f"       {problem}")
        return 1
    validator = validator or default_flux_validator()
    return _spec_verify(Path(archive), validator)


def _refuse_out_of_bounds(directory: Path) -> Tuple[str, ...]:
    """Why *directory*'s documents may not go to flux_spec's read, or nothing."""
    from flux_engine.core.errors import FluxError
    from flux_engine.seam.resolve import refuse_out_of_bounds_documents

    try:
        refuse_out_of_bounds_documents(directory)
    except FluxError as error:
        return (str(error),)
    return ()


def _archive_out_of_bounds(archive: Path) -> List[str]:
    """What an archive's documents hold past the read's bounds, read as the validator reads them.

    Only what ``spec.bundle.verify`` goes on to read is read, and only inside
    its own caps: the archive is read once, front to back, and one that passes
    its member or byte caps is left to it, since it refuses those before it
    extracts a member. The documents are the bundle directory's own FLUX and
    FLUID documents and its lockfile (regular members one level below the
    archive's directory). The validator finds a document by its pattern, which
    Python's glob matches with case on POSIX, macOS included, and the lockfile
    by its path, which a case-insensitive filesystem (macOS's default) also
    resolves to ``FLUX.LOCK``: a member is the lockfile when its name is
    ``flux.lock`` under :func:`_caseless`. Each is decoded as the validator
    decodes the files ``verify`` extracts, as UTF-8 with universal newlines
    (flux-spec reads UTF-8 from 0.5.4, whatever the locale), and all are held
    to one read budget. A document or lockfile past the seam's size caps is refused
    by the size its header declares, before it is read
    (:func:`flux_engine.seam.resolve.refuse_oversized_file`), and so is a pack
    product (the manifest, which ``verify`` reads whole, and its signature),
    matched as caselessly as the lockfile and never read here. The read stops
    at a member ``verify`` does not extract (a link, a device, a type tarfile
    does not know) and leaves the archive to it, since it refuses one before
    it reads a document. An archive this read cannot open is left to
    ``verify`` too, which meets it the same way.
    """
    import fnmatch
    import tarfile
    import zlib

    from flux_engine.core.errors import FluxError
    from flux_engine.seam.resolve import (
        BUNDLE_PACK_PRODUCTS,
        FLUID_DOCUMENT_GLOBS,
        FLUX_DOCUMENT_GLOBS,
        FLX_3105,
        LOCK_FILE_NAME,
        refuse_oversized_file,
        refuse_oversized_read,
    )
    from flux_engine.spec.bundle import MAX_BUNDLE_BYTES, MAX_MEMBERS
    from flux_stats.exact_yaml import ReadBudget, check_documents

    patterns = FLUX_DOCUMENT_GLOBS + FLUID_DOCUMENT_GLOBS
    lock = _caseless(LOCK_FILE_NAME)
    products = {_caseless(product) for product in BUNDLE_PACK_PRODUCTS}
    budget = ReadBudget()
    problems: List[str] = []
    count = declared = read = 0
    try:
        with tarfile.open(archive, "r:gz") as tar:
            # Iterating reads each header as it comes, and a document's data is
            # read right after its header, so the archive is decompressed once.
            # Listing every member first and going back for the documents would
            # decompress it a second time, before flux_spec does so again.
            for member in tar:
                count += 1
                declared += member.size if member.isfile() else 0
                if count > MAX_MEMBERS or declared > MAX_BUNDLE_BYTES:
                    return []
                if not (member.isfile() or member.isdir()):
                    return []
                parts = Path(member.name).parts
                if not member.isfile() or len(parts) != 2:
                    continue
                name = parts[1]
                if _caseless(name) in products:
                    try:
                        refuse_oversized_file(name, member.size)
                    except FluxError as error:
                        problems.append(str(error))
                    continue
                if _caseless(name) != lock and not any(
                    fnmatch.fnmatchcase(name, pattern) for pattern in patterns
                ):
                    continue
                read += member.size
                try:
                    refuse_oversized_read(read)
                except FluxError as error:
                    problems.append(str(error))
                    return problems
                try:
                    refuse_oversized_file(name, member.size)
                except FluxError as error:
                    problems.append(str(error))
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                reader = io.TextIOWrapper(io.BytesIO(handle.read()), encoding="utf-8")
                try:
                    text = reader.read()
                except UnicodeDecodeError:
                    unreadable = f"{name}: unreadable YAML (not {reader.encoding} text)"
                    problems.append(str(FluxError(FLX_3105, unreadable)))
                    continue
                try:
                    check_documents(text, name=name, budget=budget)
                except FluxError as error:
                    problems.append(str(error))
    except (tarfile.TarError, OSError, EOFError, zlib.error):
        return []
    return problems


def _caseless(name: str) -> str:
    """*name* as a caseless filesystem compares it: Unicode's canonical caseless match.

    ``NFD(casefold(NFD(name)))`` (Unicode D145). A case-insensitive filesystem
    folds case, and macOS's also treats canonically equivalent names as one, so
    ``FLUX.LOCK``, or ``flux.lock`` spelled with a KELVIN SIGN for its ``k``,
    is ``flux.lock`` there. Matching more names than one filesystem does only
    reads more members, which only refuses more.
    """
    import unicodedata

    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


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
