# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy of a test bundle with its declared row count raised.

The small telco fixture declares 64 rows. Tests that need a run large
enough to time, or to start worker processes, copy it and raise that one
number; this is the one place that knows how.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

#: The declared row count in the fixture's simulation document.
_DECLARED_ROWS = re.compile(r"(?m)^(\s+rows:) 64\b")


def bundle_with_rows(fixture: Path, target: Path, rows: int) -> Path:
    """Copy *fixture* to *target*, declare *rows* rows, and return *target*."""
    shutil.copytree(fixture, target)
    simulation = target / "simulation.flux.yml"
    text, count = _DECLARED_ROWS.subn(rf"\g<1> {rows}", simulation.read_text())
    assert count == 1, "the fixture's declared row count moved"
    simulation.write_text(text)
    return target
