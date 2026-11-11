# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``pack_evidence`` refuses a symlink instead of dereferencing it.

The unpack side (``spec.bundle._extract_safely``) has always refused
symlinks and special files. The pack side had no matching guard: a link
inside a bundle directory was silently dereferenced by
``shutil.copytree`` and by ``bundle_files``' ``is_file()`` walk, then
digested, manifested, Merkle-rooted, in-toto attested and written into
the shipped archive as an ordinary file -- so anything readable by the
packing process could be exfiltrated into a distributed, signed artifact
by a relative link that is invisible in a YAML review.

Modelled on ``tests/acceptance/test_public_verify.py``'s tamper test:
build a real bundle, put one thing wrong, assert the refusal.
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest

from flux_engine.core.digest import digest_of_document
from flux_engine.evidence.pack import pack_evidence

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

_RUN_DIGEST = digest_of_document({"runDigest": "test"}, subject="runDigest")


def _pack(bundle: Path, run_dir: Path, out_dir: Path):
    return pack_evidence(
        bundle,
        run_dir,
        out_dir,
        run_digest=_RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00+00:00",
    )


def test_pack_refuses_a_bundle_containing_a_symlink(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT-abc123\n", encoding="utf-8")

    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)
    (bundle / "notes.txt").symlink_to(secret)

    result = _pack(bundle, tmp_path / "run", tmp_path / "out")

    assert result.ok is False
    assert any("symlink" in problem for problem in result.problems), result.problems
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").is_dir() else True


def test_pack_refuses_a_symlink_in_the_run_directory(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT-abc123\n", encoding="utf-8")

    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "leak.ndjson").symlink_to(secret)

    result = _pack(bundle, run_dir, tmp_path / "out")
    assert result.ok is False
    assert any("symlink" in problem for problem in result.problems), result.problems


def test_a_clean_bundle_still_packs_and_names_a_manifest_that_exists(tmp_path: Path) -> None:
    """The positive control -- and the signing hint's path must survive.

    ``spec.bundle.pack`` names ``<directory>/flux-manifest.json`` in its
    "sign it:" line, and the directory it packs here is a staging copy
    deleted before ``pack_evidence`` returns. Every path the result names
    must still exist once it does.
    """
    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)
    result = _pack(bundle, tmp_path / "run", tmp_path / "out")

    assert result.ok, result.problems
    assert result.archive is not None and result.archive.is_file()
    assert result.manifest_path is not None and result.manifest_path.is_file()
    assert result.build_statement_path is not None and result.build_statement_path.is_file()
    assert result.runtime_statement_path is not None and result.runtime_statement_path.is_file()

    with tarfile.open(result.archive) as tar:
        names = tar.getnames()
    assert any(name.endswith("flux-manifest.json") for name in names)


def test_a_link_created_after_the_scan_is_caught_before_packing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window the first scan leaves open, closed by a scan of the staged tree.

    The comment above ``shutil.copytree`` used to assert that a link
    appearing between the scan and the copy "is staged as a link and then
    refused by the packer's own is_file() walk". That second guard does
    not exist: ``bundle_files`` selects with ``Path.is_file()``, which
    follows symlinks, so the staged link would be dereferenced, digested,
    Merkle-rooted and written into the archive as an ordinary file.

    This forces the race the comment describes -- a link that appears
    *during* the copy -- and holds the refusal to what the comment now
    claims.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT-abc123\n", encoding="utf-8")

    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out_dir = tmp_path / "out"

    real_copytree = shutil.copytree

    def racing_copytree(src, dst, *args, **kwargs):
        result = real_copytree(src, dst, *args, **kwargs)
        Path(dst).joinpath("leak.yml").symlink_to(secret)
        return result

    monkeypatch.setattr("flux_engine.evidence.pack.shutil.copytree", racing_copytree)

    result = _pack(bundle, run_dir, out_dir)
    assert result.ok is False
    assert any("leak.yml" in problem for problem in result.problems), result.problems
    assert not (out_dir / "bundle.flux.tgz").exists()
