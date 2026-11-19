# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The third-party licence inventory cannot drift from the dependency list.

``NOTICE`` names ``THIRD_PARTY_LICENSES.md`` as the authoritative list of
third-party licences, and the
generator's own comment claimed the table was "kept in sync with
pyproject.toml's [project.dependencies]". Nothing enforced that, and
``cryptography`` -- a core runtime dependency, the ed25519 signature over
the fidelity record -- was missing from it.

The table is now derived from pyproject rather than transcribed. These
tests hold the two ends together: every declared distribution has a
licence entry, no entry names something that is no longer declared, and
the generated file lists them all.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.unit


def _load_generator() -> Any:
    path = REPO_ROOT / "scripts" / "third_party_licenses.py"
    spec = importlib.util.spec_from_file_location("third_party_licenses", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("third_party_licenses", module)
    spec.loader.exec_module(module)
    return module


def _declared_from_pyproject() -> set[str]:
    generator = _load_generator()
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    names = {generator.requirement_name(r) for r in project.get("dependencies", [])}
    for requirements in project.get("optional-dependencies", {}).values():
        names |= {generator.requirement_name(r) for r in requirements}
    return names


def test_every_declared_dependency_has_a_licence_entry() -> None:
    generator = _load_generator()
    missing = generator.missing_entries()
    assert missing == [], (
        "pyproject declares "
        + ", ".join(missing)
        + " with no row in LICENSE_NOTES -- add the licence and the reason, "
        "then regenerate THIRD_PARTY_LICENSES.md."
    )


def test_no_licence_entry_outlives_its_dependency() -> None:
    generator = _load_generator()
    assert generator.stale_entries() == []


def test_the_derived_list_is_exactly_the_declared_list() -> None:
    generator = _load_generator()
    assert set(generator.declared_requirements()) == _declared_from_pyproject()


def test_the_generated_file_lists_every_declared_dependency() -> None:
    generator = _load_generator()
    published = (REPO_ROOT / "THIRD_PARTY_LICENSES.md").read_text()
    rows = {
        line.split("|")[1].strip()
        for line in published.splitlines()
        if line.startswith("|") and not line.startswith("|---")
    }
    rows.discard("Package")
    listed = {generator.normalize(name) for name in rows}
    assert _declared_from_pyproject() <= listed, (
        "THIRD_PARTY_LICENSES.md is stale -- run `python scripts/third_party_licenses.py`. "
        f"Missing: {sorted(_declared_from_pyproject() - listed)}"
    )


@pytest.mark.parametrize("distribution", ["cryptography", "flux-spec"])
def test_the_core_dependencies_the_inventory_used_to_omit_are_listed(distribution: str) -> None:
    generator = _load_generator()
    assert generator.normalize(distribution) in set(generator.declared_requirements())
    assert distribution in (REPO_ROOT / "THIRD_PARTY_LICENSES.md").read_text()


def test_a_requirement_name_is_read_from_every_pep508_form() -> None:
    generator = _load_generator()
    assert generator.requirement_name("numpy>=1.26,<3") == "numpy"
    assert generator.requirement_name("PyYAML>=6") == "pyyaml"
    assert generator.requirement_name("foo[bar]>=1") == "foo"
    assert generator.requirement_name("flux-spec @ git+https://example.invalid/x.git@abc") == (
        "flux-spec"
    )
