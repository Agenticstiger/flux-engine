# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The release guard's own controls, on the pull-request path.

``scripts/release_guard.py --self-test`` builds a broken fixture for every
check it makes and requires each one to refuse. Running it here rather than
only from a pushed tag is the difference between a guard that was shown to
fire once and a guard that is shown to fire on every pull request.

``--check-tree`` runs the two checks that need no tag against the REAL
tree, so both close a drift class that has no other test:

*   ``pyproject.toml``'s ``[project] version`` and the hard-coded fallback
    in ``flux_engine/__init__.py``'s ``except PackageNotFoundError`` branch
    are two copies of one string. ``tests/test_cli.py`` compares
    ``fluxsim --version`` against the same imported ``__version__`` the CLI
    printed, and ``tests/cli/test_envelope_and_stage.py`` does the same, so
    every existing assertion over the version is a tautology that cannot
    see the two literals disagree.
*   ``SEED_TREE_VERSION`` and ``TRACE_VERSION`` are declared in
    ``flux_engine/stages/pipeline.py`` AND in ``flux_engine/core/seedtree.py``
    and ``flux_engine/core/trace.py``. Test modules that mention them
    declare their own local copies, which is the same tautology shape.

``KERNEL_VERSION`` is deliberately absent from all of this. It is the
semver of normative kernel behaviour, it is hashed into ``inputDigest``,
and it already has a far stronger guard: moving it moves every pinned
digest and turns ``make test-digests`` red.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GUARD = REPO_ROOT / "scripts" / "release_guard.py"


def _load_guard() -> ModuleType:
    """Import the guard by path: scripts/ is not an installed package."""
    spec = importlib.util.spec_from_file_location("release_guard", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """The guard through its real entry point, not through an import."""
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


def test_the_guard_script_exists() -> None:
    assert GUARD.is_file(), f"no release guard at {GUARD}"


def test_every_check_refuses_a_fixture_built_to_break_it() -> None:
    """The self-test is the control. A check that stopped firing fails here."""
    result = _run("--self-test")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "checks all fired" in result.stdout


def test_this_tree_passes_the_checks_that_need_no_tag() -> None:
    result = _run("--check-tree")
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "the package version reads" in result.stdout
    assert "SEED_TREE_VERSION" in result.stdout
    assert "TRACE_VERSION" in result.stdout


def test_a_drifted_version_fallback_is_refused_without_any_tag(tmp_path: Path) -> None:
    """The negative arm of the check the test above runs green.

    A green check is not evidence on its own. This bends exactly one thing,
    the hard-coded fallback literal, and requires the same command that
    passed on the real tree to refuse.
    """
    guard = _load_guard()
    fixture, _ = guard.build_fixture(tmp_path / "drifted", REPO_ROOT, fallback="9.9.9")
    result = _run("--check-tree", "--root", str(fixture))
    assert result.returncode == 1, f"the guard accepted a drifted fallback: {result.stdout}"
    assert "must move together" in result.stderr


@pytest.mark.parametrize("tag", ["v0.2.0.dev0", "v0.2.0.post1", "0.2.0", "release-0.2.0"])
def test_a_working_version_cannot_be_tagged_as_a_release(tag: str, tmp_path: Path) -> None:
    """The obvious way around a version mismatch: tag the dev string."""
    guard = _load_guard()
    fixture, head = guard.build_fixture(tmp_path / "shape", REPO_ROOT)
    result = _run("--tag", tag, "--commit", head, "--root", str(fixture))
    assert result.returncode == 1, f"the guard accepted {tag!r}: {result.stdout}"
    assert "is not a release tag" in result.stderr


def test_a_consistent_tree_and_tag_is_accepted(tmp_path: Path) -> None:
    """The green arm, so the guard is not one that only ever refuses."""
    guard = _load_guard()
    fixture, head = guard.build_fixture(tmp_path / "green", REPO_ROOT)
    output = tmp_path / "github-output.txt"
    result = _run(
        "--tag",
        "v0.2.0",
        "--commit",
        head,
        "--root",
        str(fixture),
        "--github-output",
        str(output),
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert output.read_text(encoding="utf-8") == "version=0.2.0\nprerelease=false\n"
