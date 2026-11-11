# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init``: write a small, working project into a new directory.

The files come from :mod:`flux_engine.starter`; this module owns what the
person at the terminal sees. The point of the command is the first five
minutes, so it ends by printing the exact next command, spelled the way that
works for however this process was started (see :func:`command_prefix`).
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from flux_engine.api.stages import StageResult

__all__ = ["command_prefix", "run_init"]

_PROG = "fluxsim"

#: The name the package is published under. It is only the answer when this
#: process cannot say which distribution it came from (a source tree that was
#: never installed): see :func:`_distribution`.
_PUBLISHED_NAME = "flux-sim-engine"

#: The command that turns the scaffolded project into a first result: plan,
#: apply, verify, and an evidence pack written to ``out/``.
RUN_ARGS = "run . --verify --ship -o out"


def _distribution() -> str:
    """The name this installation carries, found through the package it ships.

    The name is asked for rather than written down here, so renaming the
    distribution changes nothing in this module.
    """
    names = metadata.packages_distributions().get("flux_engine") or []
    return names[0] if names else _PUBLISHED_NAME


def _installed_from() -> str:
    """What this installation came from, spelled for ``uvx --from``.

    A distribution installed from a wheel, a directory or a git URL records
    that (PEP 610, ``direct_url.json``), and that is what the hint must repeat:
    the package name alone makes ``uvx`` ask an index, and an index may hold a
    different package under that name, or none. Only an installation that
    really came from an index falls back to the name.
    """
    name = _distribution()
    try:
        text = metadata.distribution(name).read_text("direct_url.json")
        info = json.loads(text) if text else {}
    except (metadata.PackageNotFoundError, ValueError):
        return name
    url = info.get("url") if isinstance(info, dict) else None
    if not isinstance(url, str) or not url:
        return name
    vcs = info.get("vcs_info")
    if isinstance(vcs, dict) and vcs.get("vcs") and vcs.get("commit_id"):
        spec = f"{vcs['vcs']}+{url}@{vcs['commit_id']}"
        if isinstance(info.get("subdirectory"), str) and info["subdirectory"]:
            spec += f"#subdirectory={info['subdirectory']}"
        return shlex.quote(spec)
    if url.startswith("file://"):
        return shlex.quote(unquote(urlparse(url).path))
    return shlex.quote(url)


def command_prefix() -> str:
    """How to type ``fluxsim`` from here, so the printed next step is one that works.

    Four situations differ, and each is a first-run dead end if the hint
    ignores it:

    * Started through ``uvx``: the tool lives in a throwaway environment, so a
      bare ``fluxsim`` on the next line is "command not found". The hint is the
      ``uvx --from <source>`` spelling, with the source this installation was
      made from (see :func:`_installed_from`). uv unpacks those environments
      under its cache's ``archive-v0`` directory; a wrong guess prints a
      spelling that still runs.
    * An environment that is not on ``PATH`` (a virtualenv that was never
      activated): the hint is the absolute path of the script that is running.
    * ``python -m flux_engine.cli`` with no ``fluxsim`` on ``PATH``: the hint
      repeats that spelling.
    * Anything else: ``fluxsim``.
    """
    if "archive-v0" in Path(sys.prefix).parts:
        return f"uvx --from {_installed_from()} {_PROG}"
    script = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    found = shutil.which(_PROG)
    if script is not None and script.name == _PROG and script.is_file():
        if found is None or Path(found).resolve() != script.resolve():
            return shlex.quote(str(script.resolve()))
    elif found is None and script is not None and script.name == "__main__.py":
        return f"{shlex.quote(sys.executable)} -m flux_engine.cli"
    return _PROG


def _cd(directory: str) -> str:
    """``cd`` into *directory*; a name that starts with ``-`` would read as an option."""
    quoted = shlex.quote(directory)
    return f"cd -- {quoted}" if directory.startswith("-") else f"cd {quoted}"


def run_init(directory: str) -> StageResult:
    """Scaffold *directory* and report it as a :class:`StageResult`.

    Exit ``0`` when the project was written. Exit ``1`` when it could not be:
    the directory holds something, is a file, or a write failed. The command
    line itself was well formed in those cases, which is what exit ``2`` is
    reserved for, so a wrapper can tell "pick another name" from "fix the
    invocation".
    """
    from flux_engine.starter import PURPOSES, ScaffoldError, scaffold

    if not directory.strip():
        return StageResult(
            stage="init",
            exit_code=2,
            stderr="init: say where to write the project, for example `fluxsim init demo`",
        )
    try:
        names = scaffold(Path(directory))
    except ScaffoldError as error:
        return StageResult(stage="init", exit_code=1, stderr=f"init: {error}")

    width = max(len(name) for name in names)
    steps = [] if directory == "." else [_cd(directory)]
    steps.append(f"{command_prefix()} {RUN_ARGS}")
    lines = [
        f"created {directory} with a small FLUX bundle and its FLUID contract "
        f"({len(names)} files):",
        "",
    ]
    lines += [f"  {name:<{width}}  {PURPOSES.get(name, '')}".rstrip() for name in names]
    lines += ["", "next:"]
    lines += [f"  {step}" for step in steps]
    return StageResult(
        stage="init",
        exit_code=0,
        _artifacts={"directory": directory, "files": list(names), "next": steps},
        stdout="\n".join(lines),
    )
