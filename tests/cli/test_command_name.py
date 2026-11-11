# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The command is ``fluxsim``, and nothing in the repository still says ``flux``.

``flux`` is the command line of Flux CD, and the two collide on a shared
``PATH``. This package therefore installs exactly one console script,
``fluxsim``, and no alias for the old name. These tests keep it that way:
the packaging metadata, the parser, and every document a reader is told to
copy a command from.

``flux`` the *word* is still everywhere and still correct: ``flux_engine``,
``flux-spec``, the ``.flux/`` state directory, ``flux.lock``, ``flux://`` and
the ``FLUX_*`` variables name the specification and the package, not the
command. Only a ``flux`` followed by one of this CLI's subcommands or flags
is a stale command line.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Iterator, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A ``flux`` that is a command line: followed by a subcommand of this CLI, a
#: flag, a placeholder or a path, across a line wrap too, or the path of the
#: executable itself. ``-n flux flux-manifest.json`` (the ssh-keygen namespace)
#: and ``flux_engine`` do not match.
_STALE_COMMAND = re.compile(
    # a command line: `flux` then a subcommand, a flag, a placeholder or a path
    r"(?<![\w./-])flux(?=\s+(?:--?[A-Za-z]|\$|\./|<|(?:validate|plan|apply|run|verify|ship|"
    r"replay|calibrate|accuracy|gate|expr|describe|plugins|explain|init)\b))"
    # a path to the executable: `.venv/bin/flux`, `./venv/bin/flux`, `$(BIN)flux`
    r"|(?<![\w-])(?:bin/|\$\(BIN\))flux(?![\w.-])"
)

#: Not scanned: third-party and vendored bytes, the committed fixture bundles
#: (their bytes feed digests), the lock file, and ``docs/orchestration/``, the
#: internal build-review transcripts that quote the old command as history.
_NOT_SCANNED = (
    "uv.lock",
    "flux_engine/_vendor/",
    "tests/fixtures/",
    "docs/orchestration/",
    # This file spells the stale pattern out.
    "tests/cli/test_command_name.py",
)
#: Everything tracked is read as text unless it is one of these or fails to decode:
#: the Makefile and the shell scripts are where the executable is really invoked.
_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".gz", ".tgz", ".whl", ".zip", ".pdf"}


def _tracked_text_files() -> Iterator[Path]:
    try:
        listing = subprocess.run(
            ["git", "ls-files"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout, so there is no list of tracked files to scan")
    for relative in listing:
        if relative.startswith(_NOT_SCANNED):
            continue
        path = REPO_ROOT / relative
        if path.suffix not in _BINARY_SUFFIXES and path.is_file():
            yield path


@pytest.mark.unit
def test_the_only_console_script_is_fluxsim() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"fluxsim": "flux_engine.cli:main"}


@pytest.mark.unit
def test_the_installed_distribution_registers_no_flux_script() -> None:
    """What an install actually puts on ``PATH``, not what pyproject says it will.

    The distribution is found through the package it ships, not by its name, so
    renaming the distribution cannot turn this test into a quiet skip.
    """
    names = metadata.packages_distributions().get("flux_engine")
    if not names:
        pytest.skip("flux_engine is not installed as a distribution in this interpreter")
    distribution = metadata.distribution(names[0])
    scripts = {
        entry.name for entry in distribution.entry_points if entry.group == "console_scripts"
    }
    assert scripts == {"fluxsim"}


@pytest.mark.unit
def test_every_parser_in_the_tree_is_named_after_fluxsim() -> None:
    from flux_engine.cli import build_parser

    def walk(parser: argparse.ArgumentParser) -> List[str]:
        names = [parser.prog]
        for action in parser._actions:  # noqa: SLF001 - argparse introspection idiom
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for child in action.choices.values():
                    names.extend(walk(child))
        return names

    progs = walk(build_parser())
    assert progs[0] == "fluxsim"
    assert len(progs) > 15
    assert [p for p in progs if p != "fluxsim" and not p.startswith("fluxsim ")] == []


@pytest.mark.unit
def test_help_and_usage_errors_name_the_new_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from flux_engine.cli import main

    with pytest.raises(SystemExit) as raised:
        main(["--help"])
    assert raised.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: fluxsim ")
    assert "fluxsim run . --verify --ship -o out" in out

    with pytest.raises(SystemExit) as raised:
        main(["validate"])
    assert raised.value.code == 2
    assert "fluxsim validate:" in capsys.readouterr().err


@pytest.mark.unit
def test_a_top_level_usage_error_envelope_names_fluxsim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The one machine-readable value that carried the bare command name.

    ``stage`` is the discriminator a host routes on. A usage error that
    happens before any subcommand has no subparser to name it, so it reports
    the program itself; that moved from ``flux`` to ``fluxsim`` with the rename.
    """
    import json

    from flux_engine.cli import main

    with pytest.raises(SystemExit) as raised:
        main(["--json", "--bogus"])
    assert raised.value.code == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["stage"] == "fluxsim"
    assert envelope["exitCode"] == 2
    assert envelope["ok"] is False
    assert envelope["error"]["message"].startswith("fluxsim: ")


@pytest.mark.unit
def test_no_tracked_file_still_tells_a_reader_to_run_flux() -> None:
    stale = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in _STALE_COMMAND.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            stale.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    assert stale == [], (
        "these places still name the old command; it is `fluxsim` now, with no alias "
        "(Flux CD's own CLI is `flux`): " + ", ".join(stale)
    )
