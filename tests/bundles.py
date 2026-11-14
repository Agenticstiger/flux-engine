# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy of a test bundle with one declaration changed.

The small telco fixture declares 64 rows and two fidelity properties. Tests
that need a run large enough to time, or to start worker processes, copy it
and raise that one number; tests of how a property is checked copy it and
declare other properties. This is the one place that knows how.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Sequence

#: The declared row count in the fixture's simulation document.
_DECLARED_ROWS = re.compile(r"(?m)^(\s+rows:) 64\b")

#: The fixture's fidelity property list: everything between its
#: ``properties:`` line and the ``budget:`` line that follows it.
_DECLARED_PROPERTIES = re.compile(r"(?ms)^(          properties:\n).*?^(          budget:)")


def bundle_with_rows(fixture: Path, target: Path, rows: int) -> Path:
    """Copy *fixture* to *target*, declare *rows* rows, and return *target*."""
    shutil.copytree(fixture, target)
    simulation = target / "simulation.flux.yml"
    text, count = _DECLARED_ROWS.subn(rf"\g<1> {rows}", simulation.read_text())
    assert count == 1, "the fixture's declared row count moved"
    simulation.write_text(text)
    return target


def bundle_with_properties(fixture: Path, target: Path, properties: Sequence[str]) -> Path:
    """Copy *fixture* to *target*, declare *properties*, and return *target*.

    Each entry is one property as a YAML flow mapping, such as
    ``{ property: support, column: market, tolerance: { type: count, max: 0 } }``;
    together they replace the fixture's whole list, in the order given.
    """
    shutil.copytree(fixture, target)
    simulation = target / "simulation.flux.yml"
    listed = "".join(f"            - {entry}\n" for entry in properties)
    text, count = _DECLARED_PROPERTIES.subn(
        lambda found: found.group(1) + listed + found.group(2), simulation.read_text()
    )
    assert count == 1, "the fixture's fidelity property list moved"
    simulation.write_text(text)
    return target
