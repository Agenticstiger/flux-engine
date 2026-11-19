# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The engine's pre-reads decode a bundle's files as flux-spec does: as UTF-8, whatever the locale.

flux-spec reads UTF-8 from 0.5.4. The seam's pre-check, the evidence packer's archive pre-read
and the position a broken document's message names each mirror that read, so each decodes
UTF-8 too. Under a locale whose encoding is not UTF-8 they would otherwise refuse, or misplace,
a file flux-spec reads as written. The fixture below makes every read that names no encoding
decode as a cp1252 locale would.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any

import pytest

from flux_engine.evidence import pack
from flux_engine.seam import resolve

#: "Á" is C3 81 in UTF-8, and 0x81 is no character in cp1252, so these bytes read one way only.
DOCUMENT = 'kind: Taxonomy\nname: "Á"\n'.encode("utf-8")


class _LocaleIO:
    """``io`` as a cp1252 locale has it: a text wrapper that names no encoding decodes cp1252."""

    def __getattr__(self, name: str) -> Any:
        return getattr(io, name)

    @staticmethod
    def TextIOWrapper(buffer: Any, encoding: Any = None, *args: Any, **kwargs: Any) -> Any:
        return io.TextIOWrapper(buffer, encoding or "cp1252", *args, **kwargs)


@pytest.fixture
def cp1252_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    real_open = Path.open

    def open_in_cp1252(
        self: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: Any = None,
        errors: Any = None,
        newline: Any = None,
    ) -> Any:
        if "b" not in mode and encoding is None:
            encoding = "cp1252"
        return real_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", open_in_cp1252)
    monkeypatch.setattr(pack, "io", _LocaleIO())


def test_the_cp1252_fixture_cannot_read_the_document(tmp_path: Path, cp1252_locale: None) -> None:
    # The guard on the tests below: a read that names no encoding fails on these bytes.
    path = tmp_path / "taxonomy.flux.yml"
    path.write_bytes(DOCUMENT)
    with pytest.raises(UnicodeDecodeError):
        with path.open() as handle:
            handle.read()


def test_the_seam_pre_check_reads_a_document_as_utf8(tmp_path: Path, cp1252_locale: None) -> None:
    (tmp_path / "taxonomy.flux.yml").write_bytes(DOCUMENT)
    resolve.refuse_out_of_bounds_documents(tmp_path)


def test_the_archive_pre_read_reads_a_member_as_utf8(tmp_path: Path, cp1252_locale: None) -> None:
    archive = tmp_path / "evidence.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("bundle/taxonomy.flux.yml")
        member.size = len(DOCUMENT)
        tar.addfile(member, io.BytesIO(DOCUMENT))
    assert pack._archive_out_of_bounds(archive) == []


def test_a_broken_documents_position_is_read_from_its_utf8_text(
    tmp_path: Path, cp1252_locale: None
) -> None:
    broken = tmp_path / "broken.flux.yml"
    broken.write_bytes('name: "Á"\nkey: [unclosed\n'.encode("utf-8"))
    assert "(line " in resolve._with_yaml_mark(broken, "unreadable YAML")
