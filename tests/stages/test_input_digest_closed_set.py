# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §11's closed set: which files move ``inputDigest`` and which do not.

On 2026-09-07 the same authored bundle had two ``inputDigest`` values,
and with them two ``runDigest`` values, depending on whether the FLUX
bundle packer had been run in the directory. The packer writes
``flux-manifest.json`` into the directory it packs, the spec repository
gitignores it, and the engine hashed every regular file it found. Every
behavioural component digest was identical across the two runs. Only the
input digest moved.

These tests pin the rule that replaced that behaviour. A bundle's inputs
are the documents the loader matched plus ``flux.lock``. The files a pack
writes beside them are products, recognised and not hashed. Anything else
ends the command with ``FLX-3108`` before a digest is taken.

Every equality assertion here is paired with a control that must go red
under a rule that hashes nothing, or under a rule that excludes a whole
filename class rather than two exact names. A gate that cannot fail is
not a gate.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.seam.resolve import (
    BUNDLE_PACK_PRODUCTS,
    FLUID_DOCUMENT_GLOBS,
    FLUX_DOCUMENT_GLOBS,
    LOCK_FILE_NAME,
    load_bundle,
)
from flux_engine.spec.bundle import MANIFEST_NAME
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages.plan import plan_with_result
from flux_engine.stages.verify import recompute_input_digest

from .conftest import FIXTURE_BUNDLE

pytestmark = pytest.mark.integration

#: A stand-in for the manifest, written by hand so the assertion does not
#: depend on the packer having produced this exact content. The packer
#: itself is exercised end to end further down.
_MANIFEST_BYTES = b'{"manifestVersion": "1", "files": {}}\n'


def _copy_without_manifest(source: Path, target: Path) -> Path:
    """A copy of *source* at *target* with any packed manifest removed."""
    shutil.copytree(source, target)
    manifest = target / MANIFEST_NAME
    if manifest.exists():
        manifest.unlink()
    return target


def test_a_packed_manifest_does_not_move_the_input_digest(tmp_path: Path) -> None:
    """The reported incident, as an equality.

    Two copies of one authored bundle, one carrying the file the packer
    writes and one not. Under the rule this branch replaced these gave
    two digests, which is how a developer came to diff every component
    digest looking for a difference that was not there.
    """
    clean = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "clean")
    packed = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "packed")
    (packed / MANIFEST_NAME).write_bytes(_MANIFEST_BYTES)

    assert recompute_input_digest(packed) == recompute_input_digest(clean), (
        "a bundle's inputDigest moved because a pack had run in its directory; "
        f"{MANIFEST_NAME} is written from the digests of the files beside it, so "
        "hashing it folds a bundle's digest back into itself"
    )


def test_the_manifest_signature_is_also_a_product(tmp_path: Path) -> None:
    """``ssh-keygen -Y sign`` writes its signature beside the file it signs."""
    clean = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "clean")
    signed = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "signed")
    (signed / MANIFEST_NAME).write_bytes(_MANIFEST_BYTES)
    (signed / f"{MANIFEST_NAME}.sig").write_bytes(b"-----BEGIN SSH SIGNATURE-----\n")

    assert recompute_input_digest(signed) == recompute_input_digest(clean)


def test_a_byte_changed_in_a_document_still_moves_the_input_digest(tmp_path: Path) -> None:
    """The control for both equalities above.

    An implementation that hashed nothing at all would satisfy every
    stability assertion in this module. This is what it fails.
    """
    before = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "before")
    after = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "after")
    edited = after / "world.flux.yml"
    edited.write_bytes(edited.read_bytes() + b"\n# a comment the simulation never reads\n")

    assert recompute_input_digest(after) != recompute_input_digest(before), (
        "a byte changed in a *.flux.yml document did not move inputDigest, so the "
        "digest is covering less than it claims"
    )


def test_the_lockfile_still_moves_the_input_digest(tmp_path: Path) -> None:
    """``flux.lock`` travels as its own field, and it is still an input."""
    before = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "before")
    after = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "after")
    lock = after / LOCK_FILE_NAME
    assert lock.is_file(), lock
    lock.write_bytes(lock.read_bytes() + b"\n")

    assert recompute_input_digest(after) != recompute_input_digest(before)


def test_an_unrecognised_file_is_refused_by_name(tmp_path: Path) -> None:
    """The named reason. A file that is neither input nor product stops the command.

    The alternative the design panel weighed was to ignore it silently,
    which buys stability by letting a file the digest does not cover sit
    beside the documents unannounced.
    """
    dirty = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "dirty")
    (dirty / "catalog.flux.yml.orig").write_bytes(b"left behind by a merge\n")
    (dirty / "seed-data.csv").write_bytes(b"subscriber_id,amount\n")

    with pytest.raises(FluxError) as error:
        load_bundle(dirty)

    assert error.value.code.code == "FLX-3108"
    message = str(error.value)
    assert "catalog.flux.yml.orig" in message
    assert "seed-data.csv" in message, "one refusal must name every offending file"
    for pattern in (*FLUX_DOCUMENT_GLOBS, *FLUID_DOCUMENT_GLOBS, LOCK_FILE_NAME):
        assert pattern in message, (
            "the refusal must name the set this engine recognises: an operator "
            "meeting a document kind a newer FLUX version defines needs to know "
            "to upgrade rather than to delete the document"
        )


def test_an_arbitrary_sig_file_is_refused(tmp_path: Path) -> None:
    """The control for the signature case: two exact names, not a suffix class.

    The FLUX bundle format refuses an unmanifested file for this reason:
    a wildcard over ``*.sig`` leaves a place to put content that no digest
    covers.
    """
    dirty = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "dirty")
    (dirty / "evil.sig").write_bytes(b"not a manifest signature\n")

    with pytest.raises(FluxError) as error:
        load_bundle(dirty)

    assert error.value.code.code == "FLX-3108"
    assert "evil.sig" in str(error.value)
    assert BUNDLE_PACK_PRODUCTS == {MANIFEST_NAME, f"{MANIFEST_NAME}.sig"}


def test_a_run_state_directory_is_outside_the_digest(tmp_path: Path) -> None:
    """A subdirectory is not part of the bundle, and does not refuse it either.

    The public example carries a live ``.flux/`` state directory on disk,
    and an extracted evidence archive carries a ``run/`` directory. A
    refusal that fired on a subdirectory would break both at once.
    """
    clean = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "clean")
    with_state = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "with-state")
    runs = with_state / ".flux" / "runs" / "abcd1234"
    runs.mkdir(parents=True)
    (runs / "plan.json").write_text("{}\n", encoding="utf-8")

    assert recompute_input_digest(with_state) == recompute_input_digest(clean)


def test_apply_and_verify_are_one_code_path() -> None:
    """The two stages resolve to one function, not to two copies kept equal by review.

    ``pipeline.execute`` and ``verify.recompute_input_digest`` used to
    carry byte-identical filtering with no shared helper. A fix applied
    to one of them would have reported a moved input digest on every run.
    """
    for function in (recompute_input_digest, _pipeline.execute):
        called = function.__code__.co_names
        assert "input_digest_of" in called, (
            f"{function.__qualname__} no longer calls pipeline.input_digest_of, so "
            "apply time and verify time can hold two opinions about which files "
            "are inputs"
        )
        assert "build_input_digest" not in called, (
            f"{function.__qualname__} builds the digest itself again; the file "
            "selection belongs to one function, called by both"
        )


def test_the_loaders_read_is_what_the_digest_covers(tmp_path: Path) -> None:
    """Every document the loader matched is hashed, and nothing else is.

    The anti-under-coverage guard. An allow-list that dropped a real
    document would fail here rather than shipping a digest that covers
    less than it claims.
    """
    bundle = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "clean")
    (bundle / MANIFEST_NAME).write_bytes(_MANIFEST_BYTES)
    loaded = load_bundle(bundle)
    inputs = loaded.digest_inputs()

    on_disk = {path.name for path in bundle.iterdir() if path.is_file()}
    hashed = set(inputs.documents) | set(inputs.fluid_documents) | {LOCK_FILE_NAME}
    assert hashed | set(inputs.not_hashed) == on_disk, (
        "a file the loader read is in neither the hashed set nor the reported "
        "not-hashed set, so it is being dropped without a word"
    )
    assert inputs.not_hashed == (MANIFEST_NAME,)
    assert set(loaded.flux_file_names) == set(inputs.documents)
    assert set(loaded.fluid_file_names) == set(inputs.fluid_documents)


def test_the_pack_products_come_from_the_bundler_this_engine_links_against() -> None:
    """Anti-rot: the excluded names are read from the bundler, never copied.

    Both backends are checked against each other when the distribution is
    installed, because two backends that disagreed about the manifest's
    name would give two machines two digests for one bundle, which is the
    2026-09-07 incident wearing a different hat.
    """
    assert BUNDLE_PACK_PRODUCTS == {MANIFEST_NAME, f"{MANIFEST_NAME}.sig"}

    from flux_engine._vendor.flux_spec import bundle as vendored

    try:
        from flux_spec import bundle as installed
    except ImportError:  # pragma: no cover - exercised only without the distribution
        pytest.skip("the flux_spec distribution is not installed in this environment")
    assert installed.MANIFEST_NAME == vendored.MANIFEST_NAME


def test_the_recognised_globs_match_the_reference_validators(tmp_path: Path) -> None:
    """Anti-rot: this engine's idea of a bundle document is the validator's idea.

    Read out of the vendored validator's own source rather than restated,
    so a new document suffix upstream turns this red instead of turning
    into a file the digest quietly stops covering.
    """
    from flux_engine._vendor.flux_spec import validate as vendored

    source = Path(vendored.__file__).read_text(encoding="utf-8")
    for pattern in (*FLUX_DOCUMENT_GLOBS, *FLUID_DOCUMENT_GLOBS):
        assert f'"{pattern}"' in source, (
            f"{pattern} is not a glob the reference validator reads, so this "
            "engine and the validator disagree about what a bundle document is"
        )
    assert f'"{LOCK_FILE_NAME}"' in source


def test_running_the_packer_in_place_does_not_move_the_input_digest(tmp_path: Path) -> None:
    """The incident replayed through the real writer, not a hand-placed file.

    This is the sequence the developer performed: pack a bundle in the
    directory it lives in, then ask the engine what that bundle digests
    to. The packer writes its manifest into whatever directory it is
    handed.
    """
    from flux_engine.evidence.pack import default_flux_validator
    from flux_engine.spec.bundle import pack

    bundle = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "bundle")
    before = recompute_input_digest(bundle)

    assert pack(bundle, tmp_path / "archive", default_flux_validator()) == 0
    assert (bundle / MANIFEST_NAME).is_file(), "the packer did not write into the bundle"

    assert recompute_input_digest(bundle) == before


def test_a_file_that_is_not_hashed_is_named_in_the_plan_output(
    tmp_path: Path, state_dir: Path
) -> None:
    """The answer to "then the exclusion is silent" has to be a test, not a promise."""
    bundle = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "bundle")
    (bundle / MANIFEST_NAME).write_bytes(_MANIFEST_BYTES)

    result, execution = plan_with_result(bundle, state_dir=state_dir)

    assert result.exit_code == 0, result.stderr
    assert execution is not None
    assert execution.files_not_hashed == (MANIFEST_NAME,)
    assert result.artifacts["files_not_hashed"] == [MANIFEST_NAME]
    assert MANIFEST_NAME in result.stdout
    assert "not hashed into inputDigest" in result.stdout
    assert MANIFEST_NAME not in json.dumps(result.artifacts["plan_document"]), (
        "the not-hashed list must stay out of the digested plan document, or the "
        "digest depends on the ignored files again"
    )


def test_the_plan_says_nothing_when_every_file_is_an_input(tmp_path: Path, state_dir: Path) -> None:
    """The control for the report: a clean bundle gets no extra line."""
    bundle = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "bundle")

    result, execution = plan_with_result(bundle, state_dir=state_dir)

    assert result.exit_code == 0, result.stderr
    assert execution is not None
    assert execution.files_not_hashed == ()
    assert "not hashed into inputDigest" not in result.stdout


def test_apply_time_and_verify_time_agree_on_a_bundle_carrying_a_product(
    tmp_path: Path, state_dir: Path
) -> None:
    """One value, computed by the two stages that bind a run to it.

    A fix applied to only one of the two former copies would pass every
    stability test above and still report a moved input digest on the
    first ``fluxsim verify``.
    """
    bundle = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "bundle")
    (bundle / MANIFEST_NAME).write_bytes(_MANIFEST_BYTES)

    result, execution = plan_with_result(bundle, state_dir=state_dir)
    assert execution is not None
    planned = result.artifacts["plan_document"]["inputDigest"]

    assert planned == recompute_input_digest(bundle)
    assert planned == _pipeline.input_digest_of(load_bundle(bundle)).ref.value


def test_a_shipped_archive_extracts_to_the_input_digest_it_was_packed_from(
    tmp_path: Path,
) -> None:
    """The capability the product class buys, checked rather than claimed.

    The archive carries ``flux-manifest.json`` as a tar member. Under a
    rule that hashed it, extracting the archive and asking the engine what
    the extracted bundle digests to returned a value the receipt had never
    seen, so shipped evidence could not be re-verified from the evidence.
    """
    import tarfile

    from flux_engine.evidence.pack import default_flux_validator
    from flux_engine.spec.bundle import pack

    bundle = _copy_without_manifest(FIXTURE_BUNDLE, tmp_path / "telco")
    packed_from = recompute_input_digest(bundle)

    out = tmp_path / "out"
    out.mkdir()
    assert pack(bundle, out, default_flux_validator()) == 0
    archive = next(out.glob("*.flux.tgz"))

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(archive) as handle:
        handle.extractall(extracted, filter="data")
    inner = next(path for path in extracted.iterdir() if path.is_dir())
    assert (inner / MANIFEST_NAME).is_file(), "the archive should carry its own manifest"

    assert recompute_input_digest(inner) == packed_from
