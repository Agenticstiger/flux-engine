# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""flux-expr/2's codes: registered, explainable, raised, and apart from flux-expr/1's."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from flux_engine.core import errors as core_errors
from flux_engine.expr2 import errors
from flux_engine.expr2.errors import CATALOG

pytestmark = pytest.mark.unit

PACKAGE = Path(errors.__file__).resolve().parent


def test_every_code_is_in_the_engine_catalog() -> None:
    core_errors.ensure_core_codes_loaded()
    for code, entry in CATALOG.items():
        assert core_errors.get(code) == entry
        assert core_errors.explain(code).startswith(f"{code}  [expression language]")


def test_the_codes_are_the_specifications_and_only_those() -> None:
    assert set(CATALOG) == {f"FLX-61{n:02d}" for n in range(1, 14)} | {
        "FLX-6201",
        "FLX-6202",
        "FLX-6203",
        "FLX-6204",
        "FLX-6205",
        "FLX-6206",
        "FLX-6208",
        "FLX-6209",
        "FLX-6210",
    }


def test_every_code_is_raised_by_the_package() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in PACKAGE.glob("*.py")
        if path.name != "errors.py"
    )
    # Generation from rules raises the codes about what a rule determines.
    sources += (PACKAGE.parent / "generator" / "rules.py").read_text(encoding="utf-8")
    for code in CATALOG:
        constant = code.replace("-", "_")
        assert re.search(rf"\b{constant}\b", sources), f"{code} is registered but never raised"


def test_flux_expr_1_is_neither_imported_nor_raised_here() -> None:
    for path in PACKAGE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bFLX_600\d\b", text), path.name
        modules = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
            elif isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
        assert not {
            m for m in modules if m == "flux_engine.expr" or m.startswith("flux_engine.expr.")
        }, path.name
