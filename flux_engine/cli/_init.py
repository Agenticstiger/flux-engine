# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init``: write a small, working project into a new directory.

The files come from :mod:`flux_engine.starter`, or, given ``--contract`` and
``--profile``, from :mod:`flux_engine.author`; this module owns what the person
at the terminal sees. The point of the command is the first five minutes, so it
ends by printing the exact next command, spelled the way that works for however
this process was started (see :func:`command_prefix`).
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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


def run_init(
    directory: str, *, contract: Optional[str] = None, profile: Optional[str] = None
) -> StageResult:
    """Scaffold *directory* and report it as a :class:`StageResult`.

    With *contract* and *profile*, the project is a bundle for that data
    (:func:`run_author`); with neither, it is the starter. Exit ``0`` when the
    project was written. Exit ``1`` when it could not be: the directory holds
    something, is a file, or a write failed. The command line itself was well
    formed in those cases, which is what exit ``2`` is reserved for, so a
    wrapper can tell "pick another name" from "fix the invocation".
    """
    from flux_engine.starter import PURPOSES, ScaffoldError, scaffold

    if not directory.strip():
        return StageResult(
            stage="init",
            exit_code=2,
            stderr="init: say where to write the project, for example `fluxsim init demo`",
        )
    if (contract is None) != (profile is None):
        given, missing = (
            ("--contract", "--profile") if profile is None else ("--profile", "--contract")
        )
        return StageResult(
            stage="init",
            exit_code=2,
            stderr=(
                f"init: {given} needs {missing}: a bundle for your data is written from its "
                "FLUID contract and its profile together, for example `fluxsim init bundle "
                "--contract contract.orders.fluid.yaml --profile orders.profile.json`"
            ),
        )
    if contract is not None and profile is not None:
        return run_author(directory, contract, profile)
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


def _grouped(assumptions: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """One line per assumption, naming every column it is made for, in first-seen order."""
    from flux_engine.seam.coverage import printable

    subjects: Dict[str, List[str]] = {}
    for subject, sentence in assumptions:
        listed = subjects.setdefault(sentence, [])
        if subject not in listed:
            listed.append(subject)
    return [
        (", ".join(printable(name) for name in names), sentence)
        for sentence, names in subjects.items()
    ]


def _next_run(directory: str, profile: str) -> List[str]:
    """The command that runs the bundle: the profile goes with it, since the Simulation pins it."""
    return [f"{command_prefix()} run {shlex.quote(directory)} --profile {shlex.quote(profile)}"]


def _parameters_line(counts: Dict[str, int]) -> str:
    """The register's shares, worded as ``plan`` words them."""
    declared = sum(counts.values())
    parts = [f"{counts.get('profile', 0)} from the pinned profile"]
    if "contract" in counts:
        parts.append(f"{counts['contract']} from the contract")
    parts.append(f"{counts.get('assumption', 0)} assumed")
    parts.append(f"{counts.get('default', 0)} default")
    return f"parameters: {declared} declared -- {', '.join(parts)}"


def run_author(directory: str, contract: str, profile: str) -> StageResult:
    """Write a bundle for the data *contract* and *profile* describe into *directory*.

    Exit ``1`` when an input cannot be used (the message names it and, for a
    refusal the engine shares, its ``FLX`` code), when no expose can be written,
    or when the directory cannot take the files. Nothing is written then.
    """
    from flux_engine.author import AuthorError, author_bundle
    from flux_engine.core.errors import FluxError
    from flux_engine.seam.coverage import printable
    from flux_engine.starter import ScaffoldError, write_project

    try:
        bundle = author_bundle(Path(contract), Path(profile))
    except AuthorError as error:
        # A refusal can quote the contract (a column name, a FLUX message about it): no
        # control character of it reaches the terminal.
        return StageResult(stage="init", exit_code=1, stderr=f"init: {printable(str(error))}")
    except FluxError as error:
        if error.code.code == "FLX-3109":
            # The profile could not be read. FLX-3109's title is about a pin and a profile
            # that do not pair, which says nothing here, so its detail is the message.
            return StageResult(stage="init", exit_code=1, stderr=f"init: {printable(error.detail)}")
        return StageResult(
            stage="init",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=f"init: {printable(str(error))}",
            error_code=error.code.code,
        )
    except OSError as error:
        return StageResult(
            stage="init",
            exit_code=1,
            stderr=(
                f"init: cannot read {printable(str(error.filename or contract))}: "
                f"{error.strerror or error}"
            ),
        )
    try:
        names = write_project(Path(directory), lambda: bundle.files)
    except ScaffoldError as error:
        return StageResult(stage="init", exit_code=1, stderr=f"init: {error}")

    width = max(len(printable(name)) for name in names)
    purposes = {entry.name: entry.purpose for entry in bundle.files}
    lines = [
        f"created {printable(directory)} from {printable(Path(contract).name)} and "
        f"{printable(Path(profile).name)} "
        f"({len(names)} files):",
        "",
    ]
    lines += [f"  {printable(name):<{width}}  {purposes.get(name, '')}".rstrip() for name in names]
    exposes: List[Dict[str, Any]] = []
    assumptions: List[Tuple[str, str]] = []
    unscored = 0
    properties = 0
    for expose in bundle.exposes:
        if expose.skipped is not None:
            lines += ["", f"{printable(expose.expose_id)}: not written -- {expose.skipped}"]
            exposes.append({"exposeId": expose.expose_id, "skipped": expose.skipped})
            continue
        shown = [printable(column.name) for column in expose.columns]
        column_width = max((len(name) for name in shown), default=0)
        lines += [
            "",
            f"{printable(expose.expose_id)}: {expose.rows} rows, {len(expose.columns)} columns",
        ]
        lines += [
            f"  {name:<{column_width}}  {column.how}".rstrip()
            for name, column in zip(shown, expose.columns, strict=True)
        ]
        assumptions += expose.assumptions
        unscored += sum(1 for prop in expose.properties if not prop.scorable)
        properties += len(expose.properties)
        exposes.append(
            {
                "exposeId": expose.expose_id,
                "rows": expose.rows,
                "columns": [{"name": column.name, "how": column.how} for column in expose.columns],
                "properties": [dict(prop.document) for prop in expose.properties],
                "assumptions": [
                    {"subject": subject, "assumption": sentence}
                    for subject, sentence in expose.assumptions
                ],
            }
        )
    assumptions += list(bundle.notes)
    lines += ["", _parameters_line(dict(bundle.counts))]
    if assumptions:
        lines.append(
            "assumed (the register gives each assumed number its reason; replace what you "
            "know better):"
        )
        lines += [
            f"  - {subjects}: {printable(sentence)}" for subjects, sentence in _grouped(assumptions)
        ]
    if unscored:
        them, has = ("it", "has") if unscored == 1 else ("them", "have")
        noun = "property" if properties == 1 else "properties"
        lines.append(
            f"checks: {unscored} of {properties} {noun} {has} no statistic in the profile to "
            f"compare with, so `verify --profile` refuses {them} (FLX-5037) until the profile "
            "carries one"
        )
    steps = _next_run(directory, profile)
    lines += ["", "next:"] + [f"  {step}" for step in steps]
    return StageResult(
        stage="init",
        exit_code=0,
        _artifacts={
            "directory": directory,
            "files": list(names),
            "next": steps,
            "simulationId": bundle.simulation_id,
            "profileDigest": bundle.profile_digest,
            "parameters": {"declared": sum(bundle.counts.values()), "counts": dict(bundle.counts)},
            "exposes": exposes,
            "notes": [
                {"subject": subject, "assumption": sentence} for subject, sentence in bundle.notes
            ],
        },
        stdout="\n".join(lines),
    )
