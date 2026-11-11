# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The distribution is named ``flux-sim-engine``; the import package stays ``flux_engine``.

Three places have to agree, and a mismatch fails quietly:

- pyproject.toml's ``name``;
- the name ``flux_engine`` looks its own version up under. A wrong one does not raise: the
  ``except`` branch returns a hard-coded literal, so ``__version__`` can lie while the suite
  stays green;
- the file names the release workflow expects the build to produce (PEP 427 and PEP 625
  spell the distribution ``flux_sim_engine`` inside a file name).
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest

import flux_engine

ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = "flux-sim-engine"


@pytest.mark.unit
def test_pyproject_names_the_distribution_flux_sim_engine() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == DIST_NAME


@pytest.mark.unit
def test_flux_engine_looks_its_version_up_under_the_distribution_name() -> None:
    tree = ast.parse((ROOT / "flux_engine" / "__init__.py").read_text(encoding="utf-8"))
    lookups = [
        node.args[0].value  # type: ignore[attr-defined]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "version"
        and node.args
    ]
    assert lookups == [DIST_NAME]


@pytest.mark.unit
def test_the_installed_metadata_and_the_package_version_agree() -> None:
    assert importlib.metadata.version(DIST_NAME) == flux_engine.__version__


@pytest.mark.unit
def test_the_release_workflow_expects_the_files_the_build_produces() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    normalised = re.sub(r"[-_.]+", "_", DIST_NAME)
    version = r"(?:\$VERSION|\$\{\{ needs\.guard\.outputs\.version \}\}|\{version\})"
    stems = set(re.findall(rf"\b(flux_[a-z_]+?)-{version}", text))
    assert stems == {
        normalised
    }, f"release.yml names its files {stems}, the build makes {normalised}"
    assert "flux-simulation-engine" not in text and "flux_simulation_engine" not in text
