# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ``fluxsim`` command surface is the recorded one: every flag, default, help string and help text.

The snapshots under ``tests/cli/help-snapshots/`` were recorded from the parser before
``flux_engine/cli/`` was split into one module per command, and they are what keeps that
split, and any later move, honest: a refactor that changes a flag, a default, the order
of the commands or one character of ``--help`` fails here. A deliberate change to the
surface is made, then the snapshots are rewritten with ``make help-snapshots`` (see
``tests/cli/_generate_help_snapshots.py``) and the diff is reviewed with the change.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tomllib
from pathlib import Path

import pytest

from flux_engine.cli import build_parser

from . import _generate_help_snapshots as snapshots

pytestmark = pytest.mark.unit

_REWRITE = "if the change is deliberate, rewrite the snapshots with `make help-snapshots`"


def test_the_parser_structure_is_the_recorded_one() -> None:
    recorded = json.loads((snapshots.SNAPSHOTS / "structure.json").read_text(encoding="utf-8"))
    current = snapshots.structure(build_parser())
    assert list(current) == list(recorded), f"the commands changed; {_REWRITE}"
    changed = [path for path in current if current[path] != recorded[path]]
    assert not changed, f"the arguments of {changed} changed; {_REWRITE}"


def test_a_help_set_is_recorded_for_every_python_the_package_supports() -> None:
    """The rendered half skips on a Python with no set, so a supported one must have a set.

    The supported versions are the ones pyproject.toml's classifiers declare, which the
    CI matrix runs. Without this, adding a version to both and forgetting its set would
    leave that cell skipping the comparison, and passing.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    classifiers = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["classifiers"]
    supported = sorted(
        match.group(1)
        for match in map(
            re.compile(r"Programming Language :: Python :: (3\.\d+)").fullmatch, classifiers
        )
        if match
    )
    recorded = sorted(
        path.name.removeprefix("py") for path in snapshots.SNAPSHOTS.glob("py3.*") if path.is_dir()
    )
    assert recorded == supported, (
        f"pyproject.toml declares Python {supported}, and help is recorded for {recorded}: "
        "keep the help-snapshots target in the Makefile to the same versions and run "
        "`make help-snapshots`"
    )


def test_every_help_text_is_the_recorded_one() -> None:
    directory = snapshots.minor_version_directory()
    if not directory.is_dir():
        pytest.skip(f"no help snapshots recorded for this Python ({directory.name})")
    recorded = {path.stem: path.read_text(encoding="utf-8") for path in directory.glob("*.txt")}
    current = snapshots.help_texts(build_parser())
    assert sorted(current) == sorted(recorded), f"the commands changed; {_REWRITE}"
    for name, text in current.items():
        if text != recorded[name]:
            diff = "".join(
                difflib.unified_diff(
                    recorded[name].splitlines(keepends=True),
                    text.splitlines(keepends=True),
                    fromfile=f"recorded {name}",
                    tofile=f"current {name}",
                )
            )
            pytest.fail(f"`{name.replace('_', ' ')} --help` changed; {_REWRITE}\n{diff}")


def test_rendering_leaves_the_environment_as_it_found_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "132")
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    snapshots.help_texts(build_parser())
    assert os.environ["COLUMNS"] == "132"
    assert os.environ["FORCE_COLOR"] == "1"
    assert "NO_COLOR" not in os.environ
