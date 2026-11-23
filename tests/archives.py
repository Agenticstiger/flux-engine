# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Reading a member back out of a shipped evidence archive."""

from __future__ import annotations

import tarfile
from pathlib import Path


def packed_file(archive: Path, suffix: str) -> bytes:
    """The bytes of the member of *archive* whose name ends in *suffix*, as packed."""
    with tarfile.open(archive) as pack:
        member = next(m for m in pack.getmembers() if m.name.endswith(suffix))
        stream = pack.extractfile(member)
        assert stream is not None
        return stream.read()
