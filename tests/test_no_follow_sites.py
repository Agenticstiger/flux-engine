# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The writers outside the stages hold the no-follow rule too.

The boundary vault (``<state>/boundary/``), the evidence pack's four outputs and the
file sinks each write under a name the engine owns. A link planted at one of those
names, or in place of the directory that holds them, is refused or replaced, and the
file it points at is never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from flux_engine.concordance.boundary.vault import (
    BOUNDARY_DIRNAME,
    LOCK_FILENAME,
    BlindingVault,
)
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError
from flux_engine.evidence.pack import pack_evidence

SYMLINKS = pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symbolic links")

REPO_ROOT = Path(__file__).resolve().parents[1]
SMALL_BUNDLE = REPO_ROOT / "tests" / "fixtures" / "telco-fidelity-small"
STAMPED = "2026-09-01T00:00:00.000Z"
SLICE = "sha256:" + "ab" * 32


def _victim(root: Path, name: str = "victim.txt") -> Path:
    outside = root / "outside"
    outside.mkdir(exist_ok=True)
    victim = outside / name
    victim.write_bytes(b"original")
    return victim


# --- the boundary vault ------------------------------------------------------


def _draw(vault: BlindingVault) -> Any:
    return vault.resolve(expose_id="e", slice_digest=SLICE, committed_at=STAMPED)


@SYMLINKS
def test_a_boundary_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / BOUNDARY_DIRNAME).symlink_to(real, target_is_directory=True)

    with pytest.raises(FluxError) as refused:
        _draw(BlindingVault.for_state_dir(tmp_path))

    assert refused.value.code.code == "FLX-2118"
    assert not list(real.iterdir())


@SYMLINKS
def test_a_lock_that_is_a_link_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    vault = BlindingVault.for_state_dir(tmp_path)
    vault.directory.mkdir()
    victim = _victim(tmp_path)
    (vault.directory / LOCK_FILENAME).symlink_to(victim)

    with pytest.raises(FluxError) as refused:
        _draw(vault)

    assert refused.value.code.code == "FLX-5013"
    assert victim.read_bytes() == b"original"


@SYMLINKS
def test_a_store_that_is_a_link_is_not_read_through(tmp_path: Path) -> None:
    vault = BlindingVault.for_state_dir(tmp_path)
    _draw(vault)
    copy = tmp_path / "copy.json"
    copy.write_bytes(vault.path.read_bytes())
    vault.path.unlink()
    vault.path.symlink_to(copy)
    before = copy.read_bytes()

    with pytest.raises(FluxError) as refused:
        vault.entries()
    with pytest.raises(FluxError):
        _draw(vault)

    assert refused.value.code.code == "FLX-5013"
    assert "FLX-2117" in str(refused.value)
    assert copy.read_bytes() == before


@SYMLINKS
def test_a_dangling_link_at_the_store_name_is_refused_and_nothing_is_created(
    tmp_path: Path,
) -> None:
    vault = BlindingVault.for_state_dir(tmp_path)
    vault.directory.mkdir()
    target = tmp_path / "outside" / "never-created.json"
    target.parent.mkdir()
    vault.path.symlink_to(target)

    with pytest.raises(FluxError) as refused:
        _draw(vault)

    assert refused.value.code.code == "FLX-5013"
    assert not target.exists()


# --- the evidence pack -------------------------------------------------------

_RUN_DIGEST = digest_of_document({"runDigest": "test"}, subject="runDigest")


def _pack(bundle: Path, run_dir: Path, out_dir: Path, **kwargs: Any) -> Any:
    return pack_evidence(
        bundle,
        run_dir,
        out_dir,
        run_digest=_RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00+00:00",
        **kwargs,
    )


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(SMALL_BUNDLE, bundle)
    return bundle


@SYMLINKS
def test_an_output_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    out = tmp_path / "out"
    out.symlink_to(real, target_is_directory=True)

    with pytest.raises(FluxError) as refused:
        _pack(_bundle(tmp_path), tmp_path / "run", out)

    assert refused.value.code.code == "FLX-2118"
    assert not list(real.iterdir())


@SYMLINKS
def test_a_link_at_any_of_the_four_output_names_is_replaced(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    out = tmp_path / "out"
    first = _pack(bundle, tmp_path / "run", out)
    assert first.ok, first.problems
    names = [
        first.archive,
        first.manifest_path,
        first.build_statement_path,
        first.runtime_statement_path,
    ]
    planted = {}
    for index, name in enumerate(names):
        victim = _victim(tmp_path, f"victim-{index}.txt")
        name.unlink()
        name.symlink_to(victim)
        planted[name] = victim

    second = _pack(bundle, tmp_path / "run", out, force=True)

    assert second.ok, second.problems
    for name, victim in planted.items():
        assert victim.read_bytes() == b"original", name
        assert name.is_file() and not name.is_symlink(), name


@SYMLINKS
def test_a_runtime_statement_that_is_a_link_is_not_read_through(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    out = tmp_path / "out"
    first = _pack(bundle, tmp_path / "run", out)
    statement = first.runtime_statement_path
    copy = tmp_path / "copy.json"
    copy.write_bytes(statement.read_bytes())
    statement.unlink()
    statement.symlink_to(copy)

    refused = _pack(bundle, tmp_path / "run", out)

    assert refused.ok is False
    assert "does not name a run digest" in refused.problems[0]


def test_a_staged_leftover_in_the_run_directory_is_not_packed(tmp_path: Path) -> None:
    import tarfile

    run = tmp_path / "run"
    run.mkdir()
    (run / "receipt.json").write_text("{}\n", encoding="utf-8")
    (run / ".receipt.json-abcd1234.tmp").write_text("half", encoding="utf-8")

    result = _pack(_bundle(tmp_path), run, tmp_path / "out")

    assert result.ok, result.problems
    with tarfile.open(result.archive) as tar:
        names = tar.getnames()
    assert any(name.endswith("/run/receipt.json") for name in names)
    assert not [name for name in names if name.endswith(".tmp")]


# --- the file sinks ----------------------------------------------------------


@SYMLINKS
def test_a_stream_directory_that_is_a_link_is_refused(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow.parquet")
    from flux_engine.sinks import parquet
    from tests.sinks.test_files import Stream

    stream = Stream(2)
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / stream.context.expose_id).symlink_to(real, target_is_directory=True)
    sink = parquet.ParquetSink(tmp_path, stream.context)

    with pytest.raises(FluxError) as refused:
        sink.open(stream.header)

    assert refused.value.code.code == "FLX-2118"
    assert not list(real.iterdir())


@SYMLINKS
def test_a_link_at_the_manifest_name_is_replaced(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow.parquet")
    from flux_engine.sinks import parquet
    from flux_engine.sinks._files import manifest_name
    from tests.sinks.test_files import Stream, _deliver

    stream = Stream(3)
    stream_dir = tmp_path / stream.context.expose_id
    stream_dir.mkdir()
    victim = _victim(tmp_path)
    link = stream_dir / manifest_name(stream.context, "parquet")
    link.symlink_to(victim)

    receipt = _deliver(parquet.ParquetSink(tmp_path, stream.context), stream.header, stream.rows)

    assert receipt is not None
    assert victim.read_bytes() == b"original"
    assert link.is_file() and not link.is_symlink()


# --- the file mode honours the umask -----------------------------------------

_MODE_SCRIPT = """
import os, stat, sys
os.umask(int(sys.argv[1], 8))
from flux_engine.safeio import write_text_atomic
write_text_atomic(sys.argv[2], "x")
print(oct(stat.S_IMODE(os.stat(sys.argv[2]).st_mode)))
"""


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
@pytest.mark.parametrize(("umask", "mode"), [("077", "0o600"), ("022", "0o644")])
def test_an_atomic_write_has_the_mode_the_umask_leaves(
    tmp_path: Path, umask: str, mode: str
) -> None:
    done = subprocess.run(
        [sys.executable, "-c", _MODE_SCRIPT, umask, str(tmp_path / "f")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert done.stdout.strip() == mode


def test_a_pack_into_the_current_directory_prints_the_names_the_packer_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path)
    out = tmp_path / "here"
    out.mkdir()
    monkeypatch.chdir(out)

    result = _pack(bundle, tmp_path / "run", Path("."))

    assert result.ok, result.problems
    printed = capsys.readouterr().out
    assert "packed bundle.flux.tgz" in printed
    assert "./bundle" not in printed and "flux-evidence-" not in printed


def test_a_staged_manifest_a_killed_write_left_behind_is_removed(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow.parquet")
    from flux_engine.sinks import parquet
    from flux_engine.sinks._files import manifest_name
    from tests.sinks.test_files import Stream

    stream = Stream(2)
    stream_dir = tmp_path / stream.context.expose_id
    stream_dir.mkdir()
    leftover = stream_dir / f".{manifest_name(stream.context, 'parquet')}-0123abcd.tmp"
    leftover.write_text("half", encoding="utf-8")

    parquet.ParquetSink(tmp_path, stream.context).open(stream.header)

    assert not leftover.exists()
