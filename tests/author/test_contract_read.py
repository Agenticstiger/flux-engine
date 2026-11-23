# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init --contract`` reads its contract as a bundle file is read.

The contract becomes the bundle's FLUID document, so it is held to a bundle
file's size cap, and a path that is not a regular file is refused before a
byte is read: ``/dev/zero`` behind the contract's name would be read without
end.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from flux_engine.author import bundle as author_bundle
from flux_engine.core.errors import FluxError
from flux_engine.seam import resolve as resolve_module

pytestmark = pytest.mark.unit


@pytest.mark.skipif(os.name != "posix", reason="a POSIX device")
def test_a_contract_that_is_not_a_regular_file_is_refused_unread(tmp_path: Path) -> None:
    path = tmp_path / "contract.fluid.yml"
    path.symlink_to(os.devnull)
    with pytest.raises(FluxError) as raised:
        author_bundle._read_contract(path)
    assert raised.value.code.code == "FLX-3105"
    assert raised.value.detail == "contract.fluid.yml: not a regular file, so it is not read"


def test_a_contract_past_the_size_cap_is_refused_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "contract.fluid.yml"
    path.write_bytes(b"x" * 9)
    monkeypatch.setattr(resolve_module, "MAX_FILE_BYTES", 8)
    with pytest.raises(FluxError) as raised:
        author_bundle._read_contract(path)
    assert raised.value.detail == (
        "contract.fluid.yml: 9 bytes, above the 8 bytes a bundle file may hold"
    )
