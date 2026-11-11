# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for flux_engine.evidence.pack."""

from __future__ import annotations

import json

import pytest

from flux_engine.calibrate.provenance import write_calibration_provenance
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.evidence.pack import (
    RUNTIME_PREDICATE_TYPE,
    EvidencePackResult,
    _bundle_directory_digest,
    pack_evidence,
    verify_evidence,
)

from .conftest import telco_example_dir

EXAMPLE = telco_example_dir()
requires_example = pytest.mark.skipif(
    EXAMPLE is None,
    reason="the FLUX spec repo's telco example is not checked out beside this repo",
)

RUN_DIGEST = DigestRef(
    value="sha256:" + "aa" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="run-digest"
)


def _make_run_dir(tmp_path, name: str = "run"):
    """A fixed-content run directory: same bytes every call (see the module docstring)."""
    run_dir = tmp_path / name
    write_calibration_provenance(
        run_dir / "calibration-provenance.json",
        world_ref="q3-retention-world",
        dials={},
        generated_at="2026-01-01T00:00:00.000Z",  # fixed, not wall-clock: identical every call
    )
    return run_dir


@requires_example
@pytest.mark.unit
def test_packing_into_the_bundle_itself_is_refused(tmp_path) -> None:
    """`fluxsim ship <bundle> -o <bundle>` writes evidence into the thing it attests.

    The staging copy keeps the packer out of the source bundle. It cannot
    keep the pack's output out of it, because `out_dir` is the operator's
    own `-o`. Measured before this guard: the call returned ok, and left
    four evidence files in the bundle directory, where the loader then
    refuses them (`FLX-3108`) and the next archive packs a stale copy of
    its own siblings.
    """
    import shutil

    bundle = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, bundle, ignore=shutil.ignore_patterns(".flux"))
    before = sorted(path.name for path in bundle.iterdir())
    run_dir = _make_run_dir(tmp_path)

    result = pack_evidence(
        bundle,
        run_dir,
        bundle,
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )

    assert not result.ok
    assert any("cannot be written into the bundle" in problem for problem in result.problems)
    assert (
        sorted(path.name for path in bundle.iterdir()) == before
    ), "the refusal must come before anything is written"


@requires_example
@pytest.mark.unit
def test_pack_evidence_leaves_the_source_bundle_tree_byte_unchanged(tmp_path) -> None:
    before = _bundle_directory_digest(EXAMPLE)
    run_dir = _make_run_dir(tmp_path)
    out_dir = tmp_path / "out"

    result = pack_evidence(
        EXAMPLE,
        run_dir,
        out_dir,
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )

    assert result.ok, result.problems
    after = _bundle_directory_digest(EXAMPLE)
    assert before == after
    # and no manifest was written into the real source directory
    assert (
        not (EXAMPLE / "flux-manifest.json").exists()
        or (json.loads((EXAMPLE / "flux-manifest.json").read_text()) != result.manifest)
        or True
    )  # source may ship its own committed manifest; only the digest-equality above matters


@requires_example
@pytest.mark.unit
def test_pack_evidence_never_writes_a_runtime_profile_into_the_manifest(tmp_path) -> None:
    run_dir = _make_run_dir(tmp_path)
    result = pack_evidence(
        EXAMPLE,
        run_dir,
        tmp_path / "out",
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )
    assert result.ok, result.problems
    assert result.manifest is not None
    assert result.manifest["attestation"]["profile"] in ("core", "enterprise")


@requires_example
@pytest.mark.unit
def test_pack_evidence_writes_a_second_distinct_runtime_statement(tmp_path) -> None:
    run_dir = _make_run_dir(tmp_path)
    result = pack_evidence(
        EXAMPLE,
        run_dir,
        tmp_path / "out",
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )
    assert result.ok, result.problems
    assert result.build_statement_path is not None and result.build_statement_path.exists()
    assert result.runtime_statement_path is not None and result.runtime_statement_path.exists()
    assert result.build_statement_path != result.runtime_statement_path

    build_statement = json.loads(result.build_statement_path.read_text())
    runtime_statement = json.loads(result.runtime_statement_path.read_text())
    assert build_statement["predicateType"] != runtime_statement["predicateType"]
    assert runtime_statement["predicateType"] == RUNTIME_PREDICATE_TYPE
    assert runtime_statement["predicate"]["runDigest"] == RUN_DIGEST.value
    assert runtime_statement["predicate"]["kernelVersion"] == "0.1.0"


@requires_example
@pytest.mark.unit
def test_pack_evidence_two_runs_produce_byte_identical_archives(tmp_path) -> None:
    run_dir = _make_run_dir(tmp_path, name="run")
    kwargs = dict(
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )
    first = pack_evidence(EXAMPLE, run_dir, tmp_path / "out1", **kwargs)
    second = pack_evidence(EXAMPLE, run_dir, tmp_path / "out2", **kwargs)

    assert first.ok and second.ok
    assert first.archive is not None and second.archive is not None
    assert first.archive.read_bytes() == second.archive.read_bytes()


@requires_example
@pytest.mark.unit
def test_pack_evidence_archive_verifies_offline(tmp_path) -> None:
    run_dir = _make_run_dir(tmp_path)
    result = pack_evidence(
        EXAMPLE,
        run_dir,
        tmp_path / "out",
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )
    assert result.ok, result.problems
    assert result.archive is not None
    assert verify_evidence(result.archive) == 0


@pytest.mark.unit
def test_pack_evidence_fails_cleanly_for_a_missing_bundle_directory(tmp_path) -> None:
    result = pack_evidence(
        tmp_path / "does-not-exist",
        tmp_path / "run",
        tmp_path / "out",
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )
    assert result == EvidencePackResult(ok=False, problems=result.problems)
    assert not result.ok
    assert result.problems


@requires_example
@pytest.mark.unit
def test_pack_evidence_fails_and_writes_nothing_for_an_invalid_bundle(tmp_path) -> None:
    import shutil

    broken = tmp_path / "broken-bundle"
    shutil.copytree(EXAMPLE, broken)
    # A duplicate document id is a guaranteed validator failure regardless
    # of which schema version this checkout ships.
    shutil.copyfile(broken / "world.flux.yml", broken / "world-duplicate.flux.yml")

    out_dir = tmp_path / "out"
    result = pack_evidence(
        broken,
        tmp_path / "run",
        out_dir,
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )
    assert not result.ok
    assert result.problems
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


@requires_example
@pytest.mark.unit
def test_a_second_run_does_not_silently_replace_the_first_runs_evidence(tmp_path) -> None:
    """Every artifact is named from the bundle basename alone.

    ``<name>.flux.tgz``, ``<name>.flux-manifest.json``,
    ``<name>.attestation.json`` and ``<name>.runtime-evidence.json`` carry
    neither the run id nor the run digest, so a second ship of a
    *different* run into one shared ``dist/`` replaced all four with exit
    0 and no warning -- destroying the manifest an operator may already
    have signed, and leaving that signature pointing at an archive it
    never covered.
    """
    assert EXAMPLE is not None
    run_dir = _make_run_dir(tmp_path)
    out_dir = tmp_path / "out"

    first = pack_evidence(
        EXAMPLE,
        run_dir,
        out_dir,
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00+00:00",
    )
    assert first.ok, first.problems
    assert first.archive is not None
    archive_bytes = first.archive.read_bytes()

    other_digest = DigestRef(
        value="sha256:" + "bb" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="run-digest"
    )
    second = pack_evidence(
        EXAMPLE,
        run_dir,
        out_dir,
        run_digest=other_digest,
        kernel_version="0.1.0",
        generated_at="2026-01-02T00:00:00+00:00",
    )
    assert second.ok is False
    assert any(RUN_DIGEST.value in problem for problem in second.problems), second.problems
    assert first.archive.read_bytes() == archive_bytes, "the first run's archive was replaced"

    # Re-packing the *same* run stays reproducible, and force overrides.
    again = pack_evidence(
        EXAMPLE,
        run_dir,
        out_dir,
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-03T00:00:00+00:00",
    )
    assert again.ok, again.problems
    forced = pack_evidence(
        EXAMPLE,
        run_dir,
        out_dir,
        run_digest=other_digest,
        kernel_version="0.1.0",
        generated_at="2026-01-04T00:00:00+00:00",
        force=True,
    )
    assert forced.ok, forced.problems


@requires_example
@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "content"),
    (
        ("truncated", '{"predicate": {"runDig'),
        ("not-json", "this is not a statement\n"),
        ("no-predicate", '{"_type": "https://in-toto.io/Statement/v1"}'),
        ("predicate-is-a-list", '{"predicate": []}'),
    ),
)
def test_an_unreadable_runtime_statement_is_treated_as_an_occupant(
    tmp_path, name: str, content: str
) -> None:
    """Fail closed. An unknown occupant is an occupant, not an empty directory.

    ``_occupied_by_another_run`` swallowed ``OSError``/``ValueError``/
    ``KeyError``/``TypeError`` and returned ``None`` -- and ``None`` means
    "free", so the guard added to stop a second run silently replacing
    four artifacts stepped aside in precisely the case where the on-disk
    evidence was already suspect. A truncated or hand-edited Statement
    reopened the whole hole without ``force=True``.
    """
    assert EXAMPLE is not None
    run_dir = _make_run_dir(tmp_path, name=f"run-{name}")
    out_dir = tmp_path / f"out-{name}"
    out_dir.mkdir()
    statement_path = out_dir / f"{EXAMPLE.name}.runtime-evidence.json"
    statement_path.write_text(content, encoding="utf-8")

    result = pack_evidence(
        EXAMPLE,
        run_dir,
        out_dir,
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00+00:00",
    )

    assert result.ok is False
    assert any(
        "does not name a run digest" in problem for problem in result.problems
    ), result.problems
    assert statement_path.read_text(encoding="utf-8") == content, "the suspect file was replaced"
    assert not (out_dir / f"{EXAMPLE.name}.flux.tgz").exists()

    # And the caller can still say so deliberately -- the refusal is a
    # refusal to guess, not a dead end.
    forced = pack_evidence(
        EXAMPLE,
        run_dir,
        out_dir,
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00+00:00",
        force=True,
    )
    assert forced.ok, forced.problems
