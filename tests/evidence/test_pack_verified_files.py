# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``pack_evidence`` packs the bytes the caller checked, not whatever the name holds by then.

``ship`` reads the column provenance record, checks it against the digest in the
plan, renders the report from it and only then packs the run directory. A writer
racing that window could swap the file, so the archive would carry a record no check
had seen. The caller hands over the bytes it checked (``verified_run_files``) and the
pack writes those into its staging tree without opening the name on disk.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Mapping, Optional

import pytest

from flux_engine.core.digest import digest_of_document
from flux_engine.evidence.pack import EvidencePackResult, pack_evidence
from tests.archives import packed_file

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"

_RUN_DIGEST = digest_of_document({"runDigest": "test"}, subject="runDigest")
_NAME = "column-provenance.json"
_CHECKED = b'{"the bytes": "the caller checked"}\n'
_CHANGED = b'{"the bytes": "a writer put there after"}\n'


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return run_dir


def _pack(
    tmp_path: Path, run_dir: Path, verified: Optional[Mapping[str, bytes]]
) -> EvidencePackResult:
    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)
    return pack_evidence(
        bundle,
        run_dir,
        tmp_path / "out",
        run_digest=_RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00+00:00",
        verified_run_files=verified,
    )


def _packed(result: EvidencePackResult) -> bytes:
    assert result.ok, result.problems
    assert result.archive is not None
    return packed_file(result.archive, f"/run/{_NAME}")


def test_the_archive_holds_the_checked_bytes_and_not_what_the_file_holds_now(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    (run_dir / _NAME).write_bytes(_CHANGED)

    result = _pack(tmp_path, run_dir, {_NAME: _CHECKED})

    assert _packed(result) == _CHECKED
    assert (run_dir / _NAME).read_bytes() == _CHANGED, "the run directory is read, never written"


def test_without_checked_bytes_the_file_on_disk_is_packed_as_it_always_was(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    (run_dir / _NAME).write_bytes(_CHANGED)

    assert _packed(_pack(tmp_path, run_dir, None)) == _CHANGED


def test_a_file_the_caller_did_not_name_is_packed_from_disk(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    (run_dir / _NAME).write_bytes(_CHANGED)
    (run_dir / "EVIDENCE.md").write_text("the report\n", encoding="utf-8")

    result = _pack(tmp_path, run_dir, {_NAME: _CHECKED})

    assert result.archive is not None
    assert packed_file(result.archive, "/run/EVIDENCE.md") == b"the report\n"


@pytest.mark.timeout(60)
def test_a_pipe_in_the_place_of_a_checked_file_is_never_opened(tmp_path: Path) -> None:
    """Opening a pipe for reading waits for a writer; the copy must not open this name."""
    run_dir = _run_dir(tmp_path)
    os.mkfifo(run_dir / _NAME)

    assert _packed(_pack(tmp_path, run_dir, {_NAME: _CHECKED})) == _CHECKED


def test_a_link_in_the_place_of_a_checked_file_is_still_refused(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    run_dir = _run_dir(tmp_path)
    (run_dir / _NAME).symlink_to(victim)

    result = _pack(tmp_path, run_dir, {_NAME: _CHECKED})

    assert not result.ok
    assert any("symlink" in problem for problem in result.problems), result.problems
    assert victim.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / "out").exists()


def test_a_directory_in_the_bundle_where_a_checked_file_goes_is_refused_not_a_crash(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    (run_dir / _NAME).write_bytes(_CHANGED)
    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)
    (bundle / "run" / _NAME).mkdir(parents=True)
    (bundle / "run" / _NAME / "inner.txt").write_text("in the way", encoding="utf-8")

    result = pack_evidence(
        bundle,
        run_dir,
        tmp_path / "out",
        run_digest=_RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00+00:00",
        verified_run_files={_NAME: _CHECKED},
    )

    assert not result.ok
    assert any(f"run/{_NAME}" in problem for problem in result.problems), result.problems
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "name", ["", ".", "..", "../outside.json", "emits/rows.ndjson", "/etc/hosts"]
)
def test_a_name_that_is_not_a_file_directly_in_the_run_is_refused_before_anything_is_staged(
    tmp_path: Path, name: str
) -> None:
    run_dir = _run_dir(tmp_path)
    (run_dir / _NAME).write_bytes(_CHANGED)

    with pytest.raises(ValueError, match="directly inside the run"):
        _pack(tmp_path, run_dir, {name: _CHECKED})

    assert not (tmp_path / "out").exists()
