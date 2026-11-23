# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim plugins`` lists generator plugins, text and JSON, and imports none of them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from flux_engine.cli import main

from .conftest import PACKAGE, make_distribution

pytestmark = pytest.mark.unit


def run_plugins(capsys: pytest.CaptureFixture[str], *args: str) -> str:
    assert main([*args, "plugins"]) == 0
    return capsys.readouterr().out


def generator_lines(text: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("generators"))
    return lines[start + 1 :]


def test_the_text_lists_a_generator_with_its_source_and_imports_nothing(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_distribution(site)
    text = run_plugins(capsys)
    assert "generators (entry points of flux_engine.generators, none imported):" in text
    assert text.index("sinks (fluxsim run") < text.index("generators (")
    (line,) = generator_lines(text)
    assert line == f"  learned  fxgen-plugin 1.0  {PACKAGE}.plugin:factory"
    assert PACKAGE not in sys.modules


def test_the_text_marks_editable_and_outside(
    site: Path, elsewhere: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_distribution(site, dist="fxgen-edit", entry="edit", editable=True)
    make_distribution(elsewhere, dist="fxgen-stray", entry="stray")
    lines = {line.split()[0]: line for line in generator_lines(run_plugins(capsys))}
    assert lines["edit"].endswith("(editable)")
    assert lines["stray"].endswith("(outside an installation directory)")


def test_the_text_says_so_when_there_is_no_generator(capsys: pytest.CaptureFixture[str]) -> None:
    assert generator_lines(run_plugins(capsys)) == ["  none installed"]


def test_json_gains_a_generators_list(site: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_distribution(site)
    artifacts: Dict[str, Any] = json.loads(run_plugins(capsys, "--json"))["artifacts"]
    assert artifacts["generators"] == [
        {
            "name": "learned",
            "value": f"{PACKAGE}.plugin:factory",
            "distribution": "fxgen-plugin",
            "version": "1.0",
            "editable": False,
            "outside": False,
        }
    ]
    assert "sinks" in artifacts and "verticalPacks" in artifacts
    assert PACKAGE not in sys.modules


def test_json_is_an_empty_list_without_generators(capsys: pytest.CaptureFixture[str]) -> None:
    artifacts = json.loads(run_plugins(capsys, "--json"))["artifacts"]
    assert artifacts["generators"] == []


def test_a_distribution_found_on_pythonpath_is_listed_as_outside(tmp_path: Path) -> None:
    """In a real interpreter, a ``.dist-info`` on PYTHONPATH is not under an installation directory."""
    stray = tmp_path / "stray"
    stray.mkdir()
    make_distribution(stray, dist="fxgen-stray", entry="stray")
    env = {**os.environ, "PYTHONPATH": str(stray)}
    result = subprocess.run(
        [sys.executable, "-m", "flux_engine.cli", "--json", "plugins"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    generators = json.loads(result.stdout)["artifacts"]["generators"]
    assert [(g["name"], g["outside"]) for g in generators] == [("stray", True)]


def test_the_text_never_prints_metadata_text_raw(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_distribution(site, dist="fxgen-odd\x1b[2J", entry="odd\u202e")
    text = generator_lines(run_plugins(capsys))
    assert len(text) == 1
    assert "\x1b" not in text[0] and "\u202e" not in text[0]
