# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The starter project: what ships, that it is valid, and that its result is pinned.

``fluxsim init`` writes these files and the next command runs them, so they
are the first thing every new user executes. Three properties are held here
because a change to any of them is invisible in a diff of the YAML itself.
"""

from __future__ import annotations

import fnmatch
import json
import tomllib
from importlib import resources
from pathlib import Path

import pytest

from flux_engine.cli import main
from flux_engine.starter import PURPOSES, scaffold, starter_files

REPO_ROOT = Path(__file__).resolve().parents[2]

#: What the starter produces, byte for byte. A bundle's digests are taken over
#: its bytes, so ANY edit to a starter file, a comment included, moves all
#: three. That is deliberate: it is the determinism claim, held on the one
#: bundle every new user runs first. If you changed a starter file on purpose,
#: replace these with the values the failing test prints, in the same commit.
PINNED_INPUT_DIGEST = "sha256:e8c40bd3ec5da6733dd64b242cbce42f4e6a81d263456118a0382c7fdc1c033a"
PINNED_RUN_DIGEST = "sha256:c4bb981d99467e59f8a89c546d59a35fb35f973c3abe77d70b2af1174604915f"
PINNED_ROWS = 154

_DOCUMENT_SUFFIXES = (".flux.yml", ".flux.yaml", ".fluid.yml", ".fluid.yaml")


@pytest.mark.unit
def test_every_shipped_file_has_a_purpose_line_and_the_reverse() -> None:
    shipped = {f.name for f in starter_files()}
    assert shipped == set(PURPOSES)
    assert all(purpose.strip() for purpose in PURPOSES.values())


@pytest.mark.unit
def test_the_bundle_directory_holds_only_documents_the_loader_accepts() -> None:
    """FLX-3108: a bundle directory holds specification documents and nothing else.

    A README, a ``.gitignore`` or a stray ``__pycache__`` here would be copied
    into every new project and make its first command refuse.
    """
    directory = resources.files("flux_engine.starter").joinpath("bundle")
    names = [entry.name for entry in directory.iterdir()]
    assert names
    assert [n for n in names if not n.endswith(_DOCUMENT_SUFFIXES)] == []


@pytest.mark.unit
def test_pyproject_ships_every_starter_file_in_the_wheel() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = project["tool"]["setuptools"]["package-data"]["flux_engine.starter"]
    assert patterns, "no package-data entry: the wheel would install without the starter"
    for name in PURPOSES:
        relative = f"bundle/{name}"
        assert any(fnmatch.fnmatch(relative, pattern) for pattern in patterns), relative


@pytest.mark.integration
def test_the_starter_validates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    demo = tmp_path / "demo"
    scaffold(demo)
    assert main(["validate", str(demo)]) == 0
    assert "valid" in capsys.readouterr().out


@pytest.mark.integration
def test_the_starter_result_is_pinned_and_does_not_depend_on_where_it_lives(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = []
    # Two different parents AND two different names for the bundle directory
    # itself: a result that leaned on either would give two answers.
    for place, name in (("one", "project"), ("somewhere/else/entirely", "a-different-name")):
        location = tmp_path / place
        scaffold(location / name)
        code = main(
            ["run", str(location / name), "--verify", "--state-dir", str(location / "state")]
            + ["--json"]
        )
        envelope = json.loads(capsys.readouterr().out)
        assert code == 0, envelope
        artifacts = envelope["artifacts"]
        emitted = Path(artifacts["apply"]["run_dir"]) / "emits" / "payment_recovery_moment.ndjson"
        seen.append(
            (
                artifacts["plan"]["input_digest"],
                artifacts["apply"]["run_digest"],
                len(emitted.read_text(encoding="utf-8").splitlines()),
            )
        )
    assert seen[0] == seen[1], "the same bundle gave two results in two places"
    assert seen[0] == (PINNED_INPUT_DIGEST, PINNED_RUN_DIGEST, PINNED_ROWS), (
        "the starter's result moved. If you edited a starter file on purpose, pin these in "
        f"tests/starter/test_starter_bundle.py: {seen[0]}"
    )
