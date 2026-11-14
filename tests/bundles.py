# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy of a test bundle with one declaration changed.

The small telco fixture declares 64 rows and two fidelity properties. Tests
that need a run large enough to time, or to start worker processes, copy it
and raise that one number; tests of how a property is checked copy it and
declare other properties; tests of what an unreadable declaration is told
copy it and write one line otherwise. This is the one place that knows how.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Callable, Sequence, Union

#: The declared row count in the fixture's simulation document.
_DECLARED_ROWS = re.compile(r"(?m)^(\s+rows:) 64\b")

#: The fixture's fidelity property list: everything between its
#: ``properties:`` line and the ``budget:`` line that follows it.
_DECLARED_PROPERTIES = re.compile(r"(?ms)^(          properties:\n).*?^(          budget:)")

#: The fixture's whole fidelity block: its ``fidelity:`` line and every line
#: indented under it, up to the ``agentPolicy:`` section that follows.
_DECLARED_FIDELITY = re.compile(r"(?ms)^(        fidelity:).*?^(agentPolicy:)")

_SIMULATION = "simulation.flux.yml"


def _rewritten(
    fixture: Path,
    target: Path,
    pattern: "re.Pattern[str]",
    replacement: Union[str, Callable[["re.Match[str]"], str]],
    what: str,
    *,
    file_name: str = _SIMULATION,
) -> Path:
    """Copy *fixture* to *target*, replace the one match of *pattern* in *file_name*, return *target*."""
    shutil.copytree(fixture, target)
    document = target / file_name
    text, count = pattern.subn(replacement, document.read_text())
    assert count == 1, f"the fixture's {what} moved"
    document.write_text(text)
    return target


def bundle_with_rows(fixture: Path, target: Path, rows: int) -> Path:
    """Copy *fixture* to *target*, declare *rows* rows, and return *target*."""
    return _rewritten(fixture, target, _DECLARED_ROWS, rf"\g<1> {rows}", "declared row count")


def bundle_with_properties(fixture: Path, target: Path, properties: Sequence[str]) -> Path:
    """Copy *fixture* to *target*, declare *properties*, and return *target*.

    Each entry is one property as a YAML flow mapping, such as
    ``{ property: support, column: market, tolerance: { type: count, max: 0 } }``;
    together they replace the fixture's whole list, in the order given.
    """
    listed = "".join(f"            - {entry}\n" for entry in properties)
    return _rewritten(
        fixture,
        target,
        _DECLARED_PROPERTIES,
        lambda found: found.group(1) + listed + found.group(2),
        "fidelity property list",
    )


def bundle_with_fidelity(fixture: Path, target: Path, value: str) -> Path:
    """Copy *fixture* to *target*, declare its fidelity block as *value*, and return *target*.

    *value* is the YAML written after ``fidelity:``, on that one line, such
    as ``oops`` or ``~``; it replaces the fixture's whole block.
    """
    return _rewritten(
        fixture,
        target,
        _DECLARED_FIDELITY,
        lambda found: f"{found.group(1)} {value}\n{found.group(2)}",
        "fidelity block",
    )


def bundle_with_text(fixture: Path, target: Path, file_name: str, old: str, new: str) -> Path:
    """Copy *fixture* to *target*, write *new* for the one *old* in *file_name*, return *target*."""
    return _rewritten(
        fixture, target, re.compile(re.escape(old)), lambda _: new, repr(old), file_name=file_name
    )
