# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The evidence packer and verifier hold a bundle's documents to the read's bounds first.

flux_spec's validator reads with a plain SafeLoader, which bounds nothing: a short
document of aliases stands for far more values than its text, and the schema check
walks them all. ``pack_evidence`` and ``verify_evidence`` hand a bundle to it, so
each holds the documents to the exact read's bounds before flux_spec reads one.
"""

from __future__ import annotations

import gzip
import io
import shutil
import tarfile
from pathlib import Path

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.evidence import pack as evidence_pack
from flux_engine.evidence.pack import pack_evidence, verify_evidence
from flux_engine.seam import resolve as seam_resolve
from flux_engine.seam.resolve import refuse_out_of_bounds_documents
from flux_engine.spec import bundle as spec_bundle

from .conftest import telco_example_dir
from .test_pack import RUN_DIGEST, _make_run_dir

pytestmark = pytest.mark.unit

EXAMPLE = telco_example_dir()


def _bomb(levels: int = 6, width: int = 10) -> str:
    """A World document of a few hundred bytes that writes out ``width ** levels`` values."""
    lines = ["fluxVersion: 0.5.0", "kind: World", "a0: &a0 [" + ", ".join(["x"] * width) + "]"]
    for level in range(1, levels + 1):
        aliases = ", ".join([f"*a{level - 1}"] * width)
        lines.append(f"a{level}: &a{level} [{aliases}]")
    return "\n".join(lines) + "\n"


def _archive(path: Path, files: dict) -> Path:
    """A gzip tar of one bundle directory, ``bundle/``, holding *files* (name to bytes)."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            member = tarfile.TarInfo(f"bundle/{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return path


def _never(*args, **kwargs):
    raise AssertionError("flux_spec read a document past the bounds")


@pytest.mark.parametrize("name", ["world.flux.yml", "contract.fluid.yaml", "flux.lock"])
def test_a_directory_document_past_the_bounds_is_refused_by_name(tmp_path, name) -> None:
    (tmp_path / name).write_text(_bomb(), encoding="utf-8")
    with pytest.raises(FluxError) as refused:
        refuse_out_of_bounds_documents(tmp_path)
    assert refused.value.code.code == "FLX-3105"
    assert name in str(refused.value)


def test_a_directory_within_the_bounds_passes(tmp_path) -> None:
    (tmp_path / "world.flux.yml").write_text("fluxVersion: 0.5.0\nkind: World\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text(_bomb(), encoding="utf-8")  # no document of the bundle
    refuse_out_of_bounds_documents(tmp_path)


WORLD = b"fluxVersion: 0.5.0\nkind: World\n"


def _never_opened(*args, **kwargs):
    raise AssertionError("a file past the size caps was opened")


def test_a_directory_file_past_the_size_cap_is_refused_before_any_is_opened(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "world.flux.yml").write_bytes(WORLD)
    monkeypatch.setattr(seam_resolve, "MAX_FILE_BYTES", len(WORLD) - 1)
    monkeypatch.setattr(Path, "open", _never_opened)
    with pytest.raises(FluxError) as refused:
        refuse_out_of_bounds_documents(tmp_path)
    assert refused.value.code.code == "FLX-3105"
    assert refused.value.detail == (
        f"world.flux.yml: {len(WORLD)} bytes, above the {len(WORLD) - 1} bytes a bundle file may hold"
    )


def test_a_directory_past_the_bundle_cap_is_refused_before_any_file_is_opened(
    tmp_path, monkeypatch
) -> None:
    for name in ("a.flux.yml", "b.fluid.yml", "flux.lock"):
        (tmp_path / name).write_bytes(WORLD)
    (tmp_path / "notes.txt").write_bytes(WORLD)  # no document of the bundle, so not counted
    monkeypatch.setattr(seam_resolve, "MAX_BUNDLE_READ_BYTES", 3 * len(WORLD) - 1)
    monkeypatch.setattr(Path, "open", _never_opened)
    with pytest.raises(FluxError) as refused:
        refuse_out_of_bounds_documents(tmp_path)
    assert f"documents and lockfile: {3 * len(WORLD)} bytes" in (refused.value.detail or "")


def test_verify_refuses_a_document_past_the_size_cap_by_its_declared_size(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(spec_bundle, "verify", _never)
    monkeypatch.setattr(seam_resolve, "MAX_FILE_BYTES", len(WORLD) - 1)
    archive = _archive(tmp_path / "x.flux.tgz", {"world.flux.yml": WORLD})
    opened = _recording_extractfile(monkeypatch)
    assert verify_evidence(archive) == 1
    out = capsys.readouterr().out
    assert "FLX-3105" in out
    assert f"world.flux.yml: {len(WORLD)} bytes, above the {len(WORLD) - 1} bytes" in out
    assert opened == []


def test_verify_stops_at_the_document_that_takes_the_bundle_past_its_cap(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(spec_bundle, "verify", _never)
    monkeypatch.setattr(seam_resolve, "MAX_BUNDLE_READ_BYTES", 2 * len(WORLD) - 1)
    files = {"a.flux.yml": WORLD, "b.flux.yml": WORLD, "c.flux.yml": WORLD}
    archive = _archive(tmp_path / "x.flux.tgz", files)
    opened = _recording_extractfile(monkeypatch)
    assert verify_evidence(archive) == 1
    assert f"documents and lockfile: {2 * len(WORLD)} bytes" in capsys.readouterr().out
    assert opened == ["bundle/a.flux.yml"]


@pytest.mark.parametrize(
    "product", ["flux-manifest.json", "FLUX-MANIFEST.JSON", "flux-manifest.json.sig"]
)
def test_verify_holds_a_pack_product_to_the_size_cap_unread(
    tmp_path, monkeypatch, capsys, product
) -> None:
    # verify reads the manifest whole before its schema check, so it is held
    # to the cap by its header's size, as caselessly as the lockfile, and
    # never opened here.
    monkeypatch.setattr(spec_bundle, "verify", _never)
    monkeypatch.setattr(seam_resolve, "MAX_FILE_BYTES", len(WORLD))
    archive = _archive(tmp_path / "x.flux.tgz", {"world.flux.yml": WORLD, product: b"[{}]" * 40})
    opened = _recording_extractfile(monkeypatch)
    assert verify_evidence(archive) == 1
    assert f"{product}: 160 bytes, above the {len(WORLD)} bytes" in capsys.readouterr().out
    assert opened == ["bundle/world.flux.yml"]


def test_verify_holds_a_caseless_lockfile_to_the_size_cap_unread(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(spec_bundle, "verify", _never)
    monkeypatch.setattr(seam_resolve, "MAX_FILE_BYTES", len(WORLD) - 1)
    archive = _archive(tmp_path / "x.flux.tgz", {"FLUX.LOCK": WORLD})
    opened = _recording_extractfile(monkeypatch)
    assert verify_evidence(archive) == 1
    assert f"FLUX.LOCK: {len(WORLD)} bytes, above" in capsys.readouterr().out
    assert opened == []


def test_verify_reads_a_document_at_the_size_cap(tmp_path, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(spec_bundle, "verify", lambda *args: called.append(args) or 0)
    monkeypatch.setattr(seam_resolve, "MAX_FILE_BYTES", len(WORLD))
    files = {"world.flux.yml": WORLD, "flux-manifest.json": b"{}"}
    assert verify_evidence(_archive(tmp_path / "x.flux.tgz", files)) == 0
    assert called


def test_verify_refuses_an_archive_past_the_bounds_before_flux_spec_reads_it(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(spec_bundle, "verify", _never)
    archive = _archive(tmp_path / "x.flux.tgz", {"world.flux.yml": _bomb().encode("utf-8")})
    assert verify_evidence(archive) == 1
    out = capsys.readouterr().out
    assert out.startswith(f"[FAIL] {archive}") and "FLX-3105" in out and "world.flux.yml" in out


def test_verify_refuses_an_archive_whose_lockfile_is_past_the_bounds(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(spec_bundle, "verify", _never)
    files = {
        "world.flux.yml": b"fluxVersion: 0.5.0\nkind: World\n",
        "flux.lock": _bomb().encode("utf-8"),
    }
    assert verify_evidence(_archive(tmp_path / "x.flux.tgz", files)) == 1
    assert "flux.lock" in capsys.readouterr().out


KELVIN_SIGN = chr(0x212A)


@pytest.mark.parametrize("lock", ["FLUX.LOCK", "Flux.Lock", "flux.loc" + KELVIN_SIGN])
def test_verify_refuses_a_lockfile_under_a_name_a_caseless_filesystem_reads_as_it(
    tmp_path, monkeypatch, capsys, lock
) -> None:
    # The validator opens the lockfile by its path, flux.lock, which a
    # case-insensitive filesystem (macOS's default) also resolves to these names.
    monkeypatch.setattr(spec_bundle, "verify", _never)
    files = {"world.flux.yml": b"fluxVersion: 0.5.0\nkind: World\n", lock: _bomb().encode("utf-8")}
    assert verify_evidence(_archive(tmp_path / "x.flux.tgz", files)) == 1
    assert lock in capsys.readouterr().out


def test_verify_reads_the_archive_once_front_to_back(tmp_path, monkeypatch) -> None:
    # Listing every member first, then going back for the documents, would
    # decompress the archive a second time before flux_spec decompresses it again.
    files = {
        "a.flux.yml": b"fluxVersion: 0.5.0\nkind: World\n",
        "notes.txt": b"x" * 2000,
        "world.flux.yml": _bomb().encode("utf-8"),
        "z.fluid.yaml": b"fluidVersion: 0.7.3\n",
    }
    archive = _archive(tmp_path / "x.flux.tgz", files)
    seeks = []
    real_seek = gzip.GzipFile.seek

    def recording_seek(self, offset, whence=io.SEEK_SET):
        # GzipFile.tell seeks by 0 from the current position (Python 3.13), so
        # only an absolute seek is recorded, beside where it starts from.
        if whence == io.SEEK_SET:
            seeks.append((real_seek(self, 0, io.SEEK_CUR), offset))
        return real_seek(self, offset, whence)

    def listing(*args, **kwargs):
        raise AssertionError("the archive's members were listed before its documents were read")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", listing)
    monkeypatch.setattr(gzip.GzipFile, "seek", recording_seek)
    problems = evidence_pack._archive_out_of_bounds(archive)
    assert any("world.flux.yml" in problem for problem in problems)
    assert seeks and all(offset >= before for before, offset in seeks)


def test_verify_refuses_a_document_that_does_not_decode(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(spec_bundle, "verify", _never)
    archive = _archive(tmp_path / "x.flux.tgz", {"world.flux.yml": b"kind: World\nname: caf\xe9\n"})
    if io.TextIOWrapper(io.BytesIO(b"")).encoding.lower().replace("-", "") != "utf8":
        pytest.skip("a byte that is not UTF-8 decodes under this host's locale")
    assert verify_evidence(archive) == 1
    out = capsys.readouterr().out
    assert "FLX-3105" in out and "unreadable YAML" in out


def test_verify_reads_only_what_flux_spec_reads(tmp_path, monkeypatch) -> None:
    # A file below the bundle directory, or one of another name, is no document of it,
    # so it is left to verify, which digests it like any other file.
    called = []
    monkeypatch.setattr(spec_bundle, "verify", lambda *args: called.append(args) or 0)
    files = {
        "world.flux.yml": b"fluxVersion: 0.5.0\nkind: World\n",
        "run/state.flux.yml": _bomb().encode("utf-8"),
        "notes.yml": _bomb().encode("utf-8"),
    }
    assert verify_evidence(_archive(tmp_path / "x.flux.tgz", files)) == 0
    assert called


def test_an_archive_over_flux_specs_caps_is_left_to_it(tmp_path, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(spec_bundle, "verify", lambda *args: called.append(args) or 1)
    monkeypatch.setattr(spec_bundle, "MAX_MEMBERS", 1)
    files = {"world.flux.yml": _bomb().encode("utf-8"), "b.flux.yml": b"kind: World\n"}
    assert verify_evidence(_archive(tmp_path / "x.flux.tgz", files)) == 1
    assert called  # verify refuses an archive over its caps before it extracts a member


def _recording_extractfile(monkeypatch) -> list:
    """Patch ``TarFile.extractfile`` to record the name of every member it opens."""
    opened: list = []
    real = tarfile.TarFile.extractfile

    def extractfile(self, member):
        opened.append(member.name if isinstance(member, tarfile.TarInfo) else member)
        return real(self, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", extractfile)
    return opened


def test_verify_reads_no_member_that_takes_the_archive_past_the_caps(tmp_path, monkeypatch) -> None:
    # The caps are counted before a member's data is read, so the member that
    # takes the archive past them is never read, and the archive goes to verify.
    first, bomb = b"fluxVersion: 0.5.0\nkind: World\n", _bomb().encode("utf-8")
    archive = _archive(tmp_path / "x.flux.tgz", {"a.flux.yml": first, "world.flux.yml": bomb})
    called = []
    monkeypatch.setattr(spec_bundle, "verify", lambda *args: called.append(args) or 1)
    monkeypatch.setattr(spec_bundle, "MAX_BUNDLE_BYTES", len(first) + len(bomb) - 1)
    opened = _recording_extractfile(monkeypatch)
    assert verify_evidence(archive) == 1
    assert opened == ["bundle/a.flux.yml"] and called


def test_verify_stops_at_a_member_flux_spec_does_not_extract(tmp_path, monkeypatch) -> None:
    # flux_spec refuses an archive that holds a link before it reads a
    # document, so nothing after the link is read, and the archive goes to it.
    archive = tmp_path / "x.flux.tgz"
    bomb = _bomb().encode("utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        link = tarfile.TarInfo("bundle/elsewhere")
        link.type, link.linkname = tarfile.SYMTYPE, "world.flux.yml"
        tar.addfile(link)
        member = tarfile.TarInfo("bundle/world.flux.yml")
        member.size = len(bomb)
        tar.addfile(member, io.BytesIO(bomb))
    called = []
    monkeypatch.setattr(spec_bundle, "verify", lambda *args: called.append(args) or 1)
    opened = _recording_extractfile(monkeypatch)
    assert verify_evidence(archive) == 1
    assert opened == [] and called


def test_an_archive_this_read_cannot_open_is_left_to_verify(tmp_path, monkeypatch) -> None:
    import zlib

    def broken(self, member):
        raise zlib.error("invalid distance too far back")

    called = []
    monkeypatch.setattr(spec_bundle, "verify", lambda *args: called.append(args) or 1)
    monkeypatch.setattr(tarfile.TarFile, "extractfile", broken)
    archive = _archive(tmp_path / "x.flux.tgz", {"world.flux.yml": _bomb().encode("utf-8")})
    assert verify_evidence(archive) == 1
    assert called


@pytest.mark.skipif(EXAMPLE is None, reason="the FLUX spec repo's telco example is not beside")
def test_pack_refuses_a_bundle_past_the_bounds_before_flux_spec_reads_it(
    tmp_path, monkeypatch
) -> None:
    assert EXAMPLE is not None
    monkeypatch.setattr(evidence_pack, "_spec_pack", _never)
    bundle = tmp_path / "bundle"
    shutil.copytree(EXAMPLE, bundle)
    (bundle / "world.flux.yml").write_text(_bomb(), encoding="utf-8")
    result = pack_evidence(
        bundle,
        _make_run_dir(tmp_path),
        tmp_path / "out",
        run_digest=RUN_DIGEST,
        kernel_version="0.1.0",
        generated_at="2026-01-01T00:00:00.000Z",
    )
    assert not result.ok
    assert any("FLX-3105" in problem and "world.flux.yml" in problem for problem in result.problems)
