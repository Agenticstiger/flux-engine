# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""CONSUMER-QUERIES-SPEC.md names exactly the classes and functions the SQL check allows.

The spec marks each list with ``<!-- allowlist: NAME -->`` and ``<!-- end allowlist -->`` and writes
each member in backticks. A name added to or removed from ``flux_engine/consumers/_allow.py`` without
the page, or the page without the code, fails here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest

pytestmark = pytest.mark.unit

pytest.importorskip("sqlglot")

from flux_engine.consumers import _allow  # noqa: E402

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec" / "CONSUMER-QUERIES-SPEC.md"


def _listed(name: str) -> List[str]:
    text = SPEC.read_text(encoding="utf-8")
    match = re.search(
        rf"<!-- allowlist: {re.escape(name)} -->\n(.*?)\n<!-- end allowlist -->", text, re.DOTALL
    )
    assert match is not None, f"the spec has no allowlist section named {name}"
    return re.findall(r"`([A-Za-z0-9]+)`", match.group(1))


def _same(name: str, expected: List[str]) -> None:
    listed = _listed(name)
    assert len(listed) == len(set(listed)), f"the spec lists a name twice in {name}"
    assert sorted(listed) == sorted(expected)


def test_the_spec_names_the_expression_classes_of_the_allowlist() -> None:
    _same("expressions", [cls.__name__ for cls in _allow.EXPRESSIONS])


def test_the_spec_names_the_function_classes_of_the_allowlist() -> None:
    _same("functions", [cls.__name__ for cls in _allow.FUNCTIONS])


def test_the_spec_names_the_cast_types_of_the_allowlist() -> None:
    _same("cast-types", [kind.name for kind in _allow._CAST_TYPES])


def test_the_spec_names_the_date_parts_of_the_allowlist() -> None:
    _same("date-parts", sorted(_allow._DATE_PARTS))


def test_the_spec_is_listed_in_the_readme() -> None:
    readme = (SPEC.parent / "README.md").read_text(encoding="utf-8")
    assert "(./CONSUMER-QUERIES-SPEC.md)" in readme
