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
from dataclasses import replace
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


def _needs_contract(
    profile: Optional[str], consumers: Optional[str], contract: Optional[str]
) -> Optional[StageResult]:
    """The refusal of ``--profile`` or ``--consumers`` given without ``--contract``, or ``None``."""
    if contract is not None:
        return None
    if profile is not None:
        return StageResult(
            stage="init",
            exit_code=2,
            stderr=(
                "init: --profile needs --contract: a bundle for your data is written from its "
                "FLUID contract and its profile together, for example `fluxsim init bundle "
                "--contract contract.orders.fluid.yaml --profile orders.profile.json`"
            ),
        )
    if consumers is not None:
        return StageResult(
            stage="init",
            exit_code=2,
            stderr=(
                "init: --consumers needs --contract: the consumers' questions are read against "
                "the contract's exposes"
            ),
        )
    return None


def run_init(
    directory: str,
    *,
    contract: Optional[str] = None,
    profile: Optional[str] = None,
    strict: bool = False,
    allow: Tuple[str, ...] = (),
    request: Optional[str] = None,
    overlays: Tuple[str, ...] = (),
    consumers: Optional[str] = None,
    window: Optional[Tuple[str, str]] = None,
) -> StageResult:
    """Scaffold *directory* and report it as a :class:`StageResult`.

    With *contract* and *profile*, the project is a bundle for that data
    (:func:`run_author`), or, given *overlays* (``--overlay``, ADR 0030 section 7), one bundle
    per overlay file in a directory of *directory* named after it (:func:`run_author_overlays`); with a contract and no profile, a profile request is written
    instead (:func:`~flux_engine.cli._request.run_request`, ADR 0018); with neither, it
    is the starter. Exit ``0`` when the
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
    needs_contract = _needs_contract(profile, consumers, contract)
    if needs_contract is not None:
        return needs_contract
    if request is not None and profile is not None:
        return StageResult(
            stage="init",
            exit_code=2,
            stderr=(
                "init: --request names where the profile request is written, and none is "
                "written once a profile is given; drop --request or --profile"
            ),
        )
    if request is not None and contract is None:
        return StageResult(
            stage="init",
            exit_code=2,
            stderr="init: --request needs --contract, which the profile request is made from",
        )
    if (strict or allow) and contract is None:
        flag = "--strict" if strict else "--allow"
        return StageResult(
            stage="init",
            exit_code=2,
            stderr=(
                f"init: {flag} checks a bundle written from --contract and --profile; the "
                "starter project has no profile to check"
            ),
        )
    if (strict or allow) and profile is None:
        flag = "--strict" if strict else "--allow"
        return StageResult(
            stage="init",
            exit_code=2,
            stderr=(
                f"init: {flag} checks a bundle written from --contract and --profile; "
                "with no profile, init writes the profile request, and there is no bundle to check"
            ),
        )
    if allow and not strict:
        return StageResult(
            stage="init",
            exit_code=2,
            stderr=(
                "init: --allow accepts what --strict would refuse; pass --strict too, or drop "
                "--allow"
            ),
        )
    refused = _overlays_need_inputs(overlays, contract, profile)
    if refused is not None:
        return refused
    given = _window_given(window, contract, profile)
    if isinstance(given, StageResult):
        return given
    if contract is not None and profile is not None:
        if overlays:
            return run_author_overlays(
                directory,
                contract,
                profile,
                overlays,
                strict=strict,
                allow=allow,
                consumers=consumers,
                window=given,
            )
        return run_author(
            directory,
            contract,
            profile,
            strict=strict,
            allow=allow,
            consumers=consumers,
            window=given,
        )
    if contract is not None:
        from flux_engine.cli._request import run_request

        return run_request(directory, contract, request, consumers=consumers)
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


def _window_given(
    window: Optional[Tuple[str, str]], contract: Optional[str], profile: Optional[str]
) -> Any:
    """The window ``--window`` gives as RFC 3339 texts, ``None`` without it, or the refusal (exit ``2``)."""
    from flux_engine.author.window import WindowError, parse_window
    from flux_engine.seam.coverage import printable

    if window is None:
        return None
    if contract is None or profile is None:
        return StageResult(
            stage="init",
            exit_code=2,
            stderr=(
                "init: --window sets the run window of a bundle written from --contract and "
                "--profile; pass both, for example `fluxsim init bundle --contract "
                "contract.orders.fluid.yaml --profile orders.profile.json --window 2025-01-01 "
                "2025-12-31`"
            ),
        )
    try:
        return parse_window(window[0], window[1])
    except WindowError as error:
        return StageResult(stage="init", exit_code=2, stderr=f"init: {printable(str(error))}")


def _overlays_need_inputs(
    overlays: Tuple[str, ...], contract: Optional[str], profile: Optional[str]
) -> Optional[StageResult]:
    """The refusal (exit ``2``) when ``--overlay`` comes without the contract and profile it patches."""
    if not overlays or (contract is not None and profile is not None):
        return None
    return StageResult(
        stage="init",
        exit_code=2,
        stderr=(
            "init: --overlay patches the profile a bundle is written from; pass --contract "
            "and --profile too, for example `fluxsim init bundles --contract "
            "contract.orders.fluid.yaml --profile orders.profile.json --overlay double.yml`"
        ),
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


_DATES_PAGE = "docs/how-to/keep-dates-inside-their-parents-lifetime.md"


def _dates_pointer(bundle: Any) -> List[str]:
    """One line that points at the dates how-to when the bundle declares ``lifetimes`` or ``within``.

    A bundle that declares neither gets no line, so its output is what it was.
    """
    from flux_engine.author.bundle import _simulation_of
    from flux_engine.seam.resolve import EXTENSION_NAMESPACE

    extensions = _simulation_of(bundle.files).get("extensions")
    block = extensions.get(EXTENSION_NAMESPACE) if isinstance(extensions, dict) else None
    if not isinstance(block, dict) or not (block.get("lifetimes") or block.get("within")):
        return []
    return [
        f"dates: the Simulation declares lifetimes or within entries; {_DATES_PAGE} reads "
        "them, and says how to change one"
    ]


def _next_run(directory: str, profile: str) -> List[str]:
    """The command that runs the bundle: the profile goes with it, since the Simulation pins it."""
    return [f"{command_prefix()} run {shlex.quote(directory)} --profile {shlex.quote(profile)}"]


def _grade_shortfalls(
    contract: str, profile: str, bundle: Any
) -> Tuple[str, List[Tuple[str, str, str, str]]]:
    """The grade of the run this profile gives, and each labelled column whose floor it is below.

    The grade is read from the profile as ``plan`` reads it (ADR 0020), and the floors are the
    built-in ones, which are all a freshly written Simulation carries. A column is
    ``(exposeId, column, sensitivity, minimum)``; none is listed for an expose that was not
    written. Nothing is raised from what ``author_bundle`` already read: a file that cannot be
    read here lists nothing.
    """
    from flux_engine.author.bundle import _read_contract
    from flux_engine.core.errors import FluxError
    from flux_engine.core.grades import at_least
    from flux_engine.evidence.grades import grade_profile
    from flux_engine.governance.privacy import effective_floors, sensitivities_of
    from flux_engine.seam.keys import declared_exposes
    from flux_engine.seam.profile import load_profile

    try:
        document, _ = _read_contract(Path(contract))
        pinned = load_profile(Path(profile))
    except (FluxError, OSError, ValueError):
        return "", []
    written = {
        expose.expose_id: {column.name for column in expose.columns}
        for expose in bundle.exposes
        if expose.skipped is None
    }
    exposes = declared_exposes(document)
    labels = sensitivities_of(
        [(name, exposes[name]) for name in sorted(written) if name in exposes]
    )
    grade = grade_profile(pinned.document, pinned=True).pattern
    floors = effective_floors(None)
    below = [
        (expose_id, column, label, floors[label].value)
        for (expose_id, column), label in sorted(labels.items())
        if column in written[expose_id] and label in floors and not at_least(grade, floors[label])
    ]
    return grade.value, below


def _parameters_line(counts: Dict[str, int]) -> str:
    """The register's shares, worded as ``plan`` words them."""
    declared = sum(counts.values())
    parts = [f"{counts.get('profile', 0)} from the pinned profile"]
    if "contract" in counts:
        parts.append(f"{counts['contract']} from the contract")
    if counts.get("overlay"):
        parts.append(f"{counts['overlay']} from the overlay")
    parts.append(f"{counts.get('assumption', 0)} assumed")
    parts.append(f"{counts.get('default', 0)} default")
    return f"parameters: {declared} declared -- {', '.join(parts)}"


def _ledger_lines(bundle: Any, request: Optional[Path] = None) -> List[str]:
    """The ledger's counts, each unused statistic with its reason (ADR 0014), and what would replace an assumption.

    The last is the statistics a profile of the contract would publish in place of the bundle's
    assumptions (ADR 0030 section 8), with the profile request *request* that asks for them.
    """
    counts = bundle.ledger.counts()
    total = sum(counts.values())
    noun = "statistic" if total == 1 else "statistics"
    lines = [
        f"profile: {total} {noun} -- {counts['used']} used, {counts['unused']} unused, "
        f"{counts['withheld']} withheld"
    ]
    return (
        lines + _noisy_lines(bundle) + _unused_lines(bundle) + _replaceable_lines(bundle, request)
    )


def _noisy_lines(bundle: Any) -> List[str]:
    """Each statistic released under noise, with its mechanism, scale and variance."""
    from flux_engine.seam.coverage import printable

    noisy = [entry for entry in bundle.ledger if entry.noise is not None]
    if not noisy:
        return []
    lines = [
        "noisy (released under differential privacy, and read as point estimates: negative "
        "cells projected away, normalised; each cell's variance is in the ledger):"
    ]
    for entry in noisy:
        assert entry.noise is not None
        lines.append(
            f"  - {printable(entry.pointer)}: {entry.noise['mechanism']}, scale "
            f"{entry.noise['scale']}, variance {entry.noise['variance']}"
        )
    return lines


def _unused_lines(bundle: Any) -> List[str]:
    """Each unused statistic with the reason no rule reads it."""
    from flux_engine.seam.coverage import printable

    unused = bundle.ledger.unused_entries()
    if not unused:
        return []
    lines = ["unused (no number rests on these; the register lists them):"]
    for entry in unused:
        definition = f" ({printable(entry.definition)})" if entry.definition else ""
        lines.append(f"  - {printable(entry.pointer)}{definition}: {printable(entry.note)}")
    return lines


def _replaceable_lines(bundle: Any, request: Optional[Path]) -> List[str]:
    """Each statistic a profile of the contract would publish in place of an assumption."""
    from flux_engine.seam.coverage import printable

    replaceable = bundle.ledger.replacements
    if not replaceable:
        return []
    asked = f" ({printable(str(request))} asks a profiler for them)" if request else ""
    lines = [f"a profile of the contract would replace assumptions{asked}:"]
    for pointer, definition, count in replaceable:
        named = f" ({printable(definition)})" if definition else ""
        noun = "number" if count == 1 else "numbers"
        lines.append(f"  - {printable(pointer)}{named}: {count} assumed {noun}")
    return lines


def _strict_refusal(findings: Any) -> str:
    """What ``--strict`` refused, one line per finding, and how to accept one."""
    from flux_engine.seam.coverage import printable

    parts = []
    if findings.unused:
        parts.append(
            f"{len(findings.unused)} statistic{'s' if len(findings.unused) != 1 else ''} of "
            "the profile unused"
        )
    if findings.gaps:
        many = len(findings.gaps) != 1
        parts.append(
            f"{len(findings.gaps)} assumption{'s' if many else ''} standing in for what the "
            "profile did not publish"
        )
    if findings.stale:
        parts.append(
            f"{len(findings.stale)} --allow pointer{'s' if len(findings.stale) != 1 else ''} "
            "accepting nothing"
        )
    lines = [f"init: --strict refuses this bundle ({', '.join(parts)}); nothing was written"]
    for pointer, definition, reason in findings.unused:
        named = f" ({printable(definition)})" if definition else ""
        lines.append(f"  unused {printable(pointer)}{named}: {printable(reason)}")
    for gap in findings.gaps:
        lines.append(f"  gap {printable(gap.pointer)}: {printable(gap.reason)}")
    for pointer in findings.stale:
        lines.append(f"  --allow {printable(pointer)} accepts nothing")
    lines.append(
        "accept one with --allow POINTER (a profile pointer for a statistic, a Simulation "
        "pointer for a gap), or publish what the profile left out"
    )
    return "\n".join(lines)


def _maybe_path(text: Optional[str]) -> Optional[Path]:
    return Path(text) if text is not None else None


def _consumer_report_lines(bundle: Any) -> List[str]:
    """A blank line, then the consumers' report; nothing when no consumers were given."""
    if bundle.consumers is None:
        return []
    return [""] + bundle.consumers.lines()


def _consumer_report_document(bundle: Any) -> Dict[str, Any]:
    """The consumers' report as one ``consumers`` entry; nothing when no consumers were given."""
    if bundle.consumers is None:
        return {}
    return {"consumers": bundle.consumers.as_document()}


def _authored(
    contract: str,
    profile: str,
    overlay: Optional[Any],
    *,
    strict: bool,
    allow: Tuple[str, ...],
    consumers: Optional[str] = None,
    window: Optional[Tuple[str, str]] = None,
) -> Any:
    """The checked bundle for *contract* and *profile* (with *overlay*), or the refusal to report.

    Exit ``1`` when an input cannot be used (the message names it and, for a refusal the engine
    shares, its ``FLX`` code), when no expose can be written, or when *strict* refuses an unused
    statistic or a gap that no *allow* pointer accepts (ADR 0014). Nothing is written here.
    """
    from flux_engine.author import AuthorError, author_bundle, strict_findings
    from flux_engine.core.errors import FluxError
    from flux_engine.seam.coverage import printable

    try:
        bundle = author_bundle(
            Path(contract),
            Path(profile),
            overlay,
            consumers_path=_maybe_path(consumers),
            window=window,
        )
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
    if strict:
        try:
            findings = strict_findings(bundle, allow)
        except ValueError as error:
            return StageResult(
                stage="init",
                exit_code=2,
                stderr=f"init: --allow takes a JSON Pointer (RFC 6901): {printable(str(error))}",
            )
        if findings.refused:
            return StageResult(
                stage="init",
                exit_code=1,
                _artifacts={
                    "strict": {
                        "unused": [
                            {"pointer": pointer, "definition": definition, "reason": reason}
                            for pointer, definition, reason in findings.unused
                        ],
                        "gaps": [
                            {"pointer": gap.pointer, "reason": gap.reason} for gap in findings.gaps
                        ],
                        "staleAllow": list(findings.stale),
                    }
                },
                stderr=_strict_refusal(findings),
            )
    return bundle


def run_author(
    directory: str,
    contract: str,
    profile: str,
    *,
    strict: bool = False,
    allow: Tuple[str, ...] = (),
    consumers: Optional[str] = None,
    window: Optional[Tuple[str, str]] = None,
) -> StageResult:
    """Write a bundle for the data *contract* and *profile* describe into *directory*.

    Exit ``1`` when an input cannot be used (the message names it and, for a
    refusal the engine shares, its ``FLX`` code), when no expose can be written,
    when *strict* refuses an unused statistic or a gap that no *allow* pointer
    accepts (ADR 0014), or when the directory cannot take the files. Nothing is
    written then.
    """
    from flux_engine.starter import ScaffoldError, write_project

    bundle = _authored(
        contract, profile, None, strict=strict, allow=allow, consumers=consumers, window=window
    )
    if isinstance(bundle, StageResult):
        return bundle
    asked = _request_path(directory, bundle)
    if isinstance(asked, StageResult):
        return asked
    try:
        names = write_project(Path(directory), lambda: bundle.files)
    except ScaffoldError as error:
        return StageResult(stage="init", exit_code=1, stderr=f"init: {error}")
    written = _write_request(asked, bundle)
    if isinstance(written, StageResult):
        _remove(Path(directory), names)
        return written
    return _described(directory, bundle, names, contract, profile, written)


def _request_path(directory: str, bundle: Any) -> Any:
    """Where the profile request for the bundle's assumptions goes, or the refusal when it is taken.

    Beside *directory*, named after it, with the suffix ``.assumptions.profile-request.yml`` (ADR
    0030 section 8): ``None`` when no assumption has a statistic to replace it. A file that exists
    is never overwritten, so it is refused before the bundle is written.
    """
    from flux_engine.author.replace import ASSUMPTIONS_REQUEST_SUFFIX
    from flux_engine.cli._request import default_request_path
    from flux_engine.seam.coverage import printable

    if bundle.assumptions_request is None:
        return None
    target = default_request_path(directory, ASSUMPTIONS_REQUEST_SUFFIX)
    if target.exists() or target.is_symlink():
        return StageResult(
            stage="init",
            exit_code=1,
            stderr=(
                f"init: {printable(str(target))} exists, and init never overwrites; move it away "
                "or write the bundle elsewhere"
            ),
        )
    return target


def _write_request(target: Optional[Path], bundle: Any) -> Any:
    """Write the profile request for the bundle's assumptions to *target*; the path, or the refusal."""
    from flux_engine.cli._request import _write_new
    from flux_engine.seam.coverage import printable
    from flux_stats.request import render_request

    if target is None:
        return None
    try:
        _write_new(target, render_request(bundle.assumptions_request).encode("utf-8"))
    except OSError as error:
        return StageResult(
            stage="init",
            exit_code=1,
            stderr=f"init: cannot write {printable(str(target))}: {error.strerror or error}",
        )
    return target


def _remove(directory: Path, names: Tuple[str, ...]) -> None:
    """Remove the files *names* ``init`` wrote into *directory*, and the directory when it is empty."""
    for name in names:
        (directory / name).unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        pass


def _read_overlays(files: Tuple[str, ...]) -> Any:
    """The overlays of *files*, in the order given, or the refusal to report (exit ``1``).

    Each file is read with the namespace's own reader (:func:`flux_engine.seam.overlay.read_overlay_file`),
    and two files that name one overlay are refused: each overlay is a directory named after it.
    """
    from flux_engine.core.errors import FluxError
    from flux_engine.seam.coverage import printable
    from flux_engine.seam.overlay import read_overlay_file
    from flux_engine.seam.resolve import FLX_3105

    overlays: List[Any] = []
    named: Dict[str, str] = {}
    for file in files:
        try:
            overlay = read_overlay_file(Path(file), code=FLX_3105)
        except FluxError as error:
            return StageResult(
                stage="init",
                exit_code=1,
                _artifacts={"code": error.code.code},
                stderr=f"init: --overlay {printable(error.detail or str(error))}",
                error_code=error.code.code,
            )
        if overlay.name in named:
            return StageResult(
                stage="init",
                exit_code=1,
                stderr=(
                    f"init: {printable(Path(file).name)} and {printable(Path(named[overlay.name]).name)} "
                    f"both name the overlay {overlay.name}; each overlay is a directory named after it"
                ),
            )
        named[overlay.name] = file
        overlays.append(overlay)
    return overlays


def _authored_each(
    contract: str,
    profile: str,
    overlays: List[Any],
    *,
    strict: bool,
    allow: Tuple[str, ...],
    consumers: Optional[str] = None,
    window: Optional[Tuple[str, str]] = None,
) -> Any:
    """The checked bundle of each overlay, or the first refusal, which names the overlay."""
    bundles: List[Any] = []
    for overlay in overlays:
        bundle = _authored(
            contract,
            profile,
            overlay,
            strict=strict,
            allow=allow,
            consumers=consumers,
            window=window,
        )
        if isinstance(bundle, StageResult):
            if bundle.stderr:
                bundle = replace(
                    bundle, stderr=f"{bundle.stderr}\n(written with the overlay {overlay.name})"
                )
            return bundle
        bundles.append(bundle)
    return bundles


def _written_each(root: Path, bundles: List[Any], contract: str, profile: str) -> StageResult:
    """Write each bundle into *root*, named after its overlay, with the request for its assumptions.

    Every request path is checked before the first bundle is written, and when a directory or a
    file cannot be written, everything this call wrote is removed.
    """
    from flux_engine.starter import ScaffoldError, write_project

    asked = [_request_path(str(root / bundle.overlay.name), bundle) for bundle in bundles]
    for refused in asked:
        if isinstance(refused, StageResult):
            return refused
    written: List[Tuple[Path, bool, Tuple[str, ...], Optional[Path]]] = []
    results: List[StageResult] = []

    def undo() -> None:
        for path, was_there, files, request in written:
            for name in files:
                (path / name).unlink(missing_ok=True)
            if not was_there:
                shutil.rmtree(path, ignore_errors=True)
            if request is not None:
                request.unlink(missing_ok=True)

    for bundle, request in zip(bundles, asked, strict=True):
        target = root / bundle.overlay.name
        existed = target.exists()
        try:
            names = write_project(target, lambda bundle=bundle: bundle.files)  # type: ignore[misc]
        except ScaffoldError as error:
            undo()
            return StageResult(stage="init", exit_code=1, stderr=f"init: {error}")
        placed = _write_request(request, bundle)
        written.append((target, existed, names, placed if isinstance(placed, Path) else None))
        if isinstance(placed, StageResult):
            undo()
            return placed
        results.append(_described(str(target), bundle, names, contract, profile, placed))
    return StageResult(
        stage="init",
        exit_code=0,
        _artifacts={
            "directory": str(root),
            "overlays": [result.artifacts for result in results],
            "next": [step for result in results for step in result.artifacts.get("next", [])],
        },
        stdout="\n\n".join(result.stdout for result in results),
    )


def run_author_overlays(
    directory: str,
    contract: str,
    profile: str,
    overlay_files: Tuple[str, ...],
    *,
    strict: bool = False,
    allow: Tuple[str, ...] = (),
    consumers: Optional[str] = None,
    window: Optional[Tuple[str, str]] = None,
) -> StageResult:
    """Write one bundle per overlay of *overlay_files* into *directory*, each in a directory named after it.

    Each bundle is :func:`run_author`'s, written from the overlaid profile, with the overlay in its
    Simulation, the profile pinned as published, and its register citing the overlay for the
    numbers it changed (ADR 0030 sections 7 and 8). Every overlay is read and every bundle is
    checked before anything is written, and when a directory cannot take its bundle, the
    bundles written before it are removed.
    """
    from flux_engine.seam.coverage import printable

    overlays = _read_overlays(overlay_files)
    if isinstance(overlays, StageResult):
        return overlays
    bundles = _authored_each(
        contract,
        profile,
        overlays,
        strict=strict,
        allow=allow,
        consumers=consumers,
        window=window,
    )
    if isinstance(bundles, StageResult):
        return bundles
    done = _written_each(Path(directory), bundles, contract, profile)
    if done.exit_code != 0:
        return done
    heading = f"created {len(bundles)} bundles in {printable(directory)}, one for each overlay:"
    return replace(done, stdout=f"{heading}\n\n{done.stdout}")


#: Why a property the profile cannot score is left out of the fidelity block.
_LEFT_OUT_REASON = (
    "the profile holds no statistic of this property's shape to compare with, so `verify "
    "--profile` would refuse it (FLX-5037)"
)


def _left_out_name(prop: Any) -> str:
    """What a left out property is called in a line: its kind and its column."""
    from flux_engine.seam.coverage import printable

    document = prop.document
    columns = document.get("columns") or [document.get("column")]
    return f"{document['property']} of {' and '.join(printable(str(name)) for name in columns)}"


def _described(
    directory: str,
    bundle: Any,
    names: Tuple[str, ...],
    contract: str,
    profile: str,
    request: Optional[Path] = None,
) -> StageResult:
    """What ``init`` says of a bundle it wrote into *directory*: files, columns, ledger, next step."""
    from flux_engine.seam.coverage import printable

    width = max(len(printable(name)) for name in names)
    purposes = {entry.name: entry.purpose for entry in bundle.files}
    with_overlay = f" with the overlay {bundle.overlay.name}" if bundle.overlay is not None else ""
    lines = [
        f"created {printable(directory)} from {printable(Path(contract).name)} and "
        f"{printable(Path(profile).name)}{with_overlay} "
        f"({len(names)} files):",
        "",
    ]
    lines += [f"  {printable(name):<{width}}  {purposes.get(name, '')}".rstrip() for name in names]
    exposes: List[Dict[str, Any]] = []
    assumptions: List[Tuple[str, str]] = []
    nothing_to_verify: List[str] = []
    carried: List[str] = []
    left_out: List[Tuple[str, str]] = []
    for expose in bundle.exposes:
        if expose.skipped is not None:
            lines += ["", f"{printable(expose.expose_id)}: not written -- {expose.skipped}"]
            exposes.append({"exposeId": expose.expose_id, "skipped": expose.skipped})
            continue
        if expose.reference:
            lines += [
                "",
                f"{printable(expose.expose_id)}: released whole, {expose.rows} published rows "
                "(reference: true)",
            ]
            exposes.append({"exposeId": expose.expose_id, "reference": True, "rows": expose.rows})
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
        if expose.copula is not None:
            lines += [f"  copula: {printable(sentence)}" for sentence in expose.copula.notes]
        assumptions += expose.assumptions
        if not expose.properties:
            (carried if expose.carries else nothing_to_verify).append(printable(expose.expose_id))
        left_out += [
            (printable(expose.expose_id), _left_out_name(prop)) for prop in expose.left_out
        ]
        exposes.append(
            {
                "exposeId": expose.expose_id,
                "rows": expose.rows,
                "columns": [{"name": column.name, "how": column.how} for column in expose.columns],
                "properties": [dict(prop.document) for prop in expose.properties],
                "leftOut": [
                    {"property": dict(prop.document), "reason": _LEFT_OUT_REASON}
                    for prop in expose.left_out
                ],
                "assumptions": [
                    {"subject": subject, "assumption": sentence}
                    for subject, sentence in expose.assumptions
                ],
                **(
                    {
                        "copula": {
                            "columns": list(expose.copula.columns),
                            "measure": expose.copula.measure or None,
                            "notes": list(expose.copula.notes),
                        }
                    }
                    if expose.copula is not None
                    else {}
                ),
            }
        )
    assumptions += list(bundle.notes)
    lines += [""] + _ledger_lines(bundle, request)
    lines += [_parameters_line(dict(bundle.counts))]
    lines += _dates_pointer(bundle)
    lines += _consumer_report_lines(bundle)
    if assumptions:
        lines.append(
            "assumed (the register gives each assumed number its reason; replace what you "
            "know better):"
        )
        lines += [
            f"  - {subjects}: {printable(sentence)}" for subjects, sentence in _grouped(assumptions)
        ]
    if left_out:
        gone = ", ".join(f"{name} ({table})" for table, name in left_out)
        noun = "property" if len(left_out) == 1 else "properties"
        lines.append(
            f"checks: left out {len(left_out)} {noun}, which the profile has no statistic to "
            f"compare with (FLX-5037): {gone}; the fidelity block lists no such property, "
            "and a profile that carries the statistic lets you declare it"
        )
    if nothing_to_verify:
        tables = ", ".join(nothing_to_verify)
        noun, has = ("table", "has") if len(nothing_to_verify) == 1 else ("tables", "have")
        lines.append(
            f"checks: {noun} {tables} {has} nothing to verify: the profile publishes no statistic "
            "a property of it could be scored against, so its fidelity block lists no property "
            "and `verify --profile` reports it as not scored"
        )
    if carried:
        tables = ", ".join(carried)
        noun, has = ("table", "has") if len(carried) == 1 else ("tables", "have")
        lines.append(
            f"checks: {noun} {tables} {has} nothing to verify against the profile: every column "
            "it could score holds the values of the history it is projected from, which the "
            "projection's own checks hold it to, so its fidelity block lists no property"
        )
    grade, below = _grade_shortfalls(contract, profile, bundle)
    steps = [] if below else _next_run(directory, profile)
    if below:
        # `plan` refuses this bundle (FLX-4005), so no run command is offered for it.
        floor_columns = ", ".join(
            f"{printable(short_expose)}.{printable(short_column)} is {short_label} (needs {short_minimum} or stronger)"
            for short_expose, short_column, short_label, short_minimum in below[:5]
        )
        more = f", and {len(below) - 5} more" if len(below) > 5 else ""
        lines += [
            "",
            f"grade: this profile gives the run {grade}, below what a column's sensitivity sets: "
            f"{floor_columns}{more}. `plan` refuses the bundle (FLX-4005) until the profile releases "
            "every statistic it publishes under noise (docs/spec/PROFILE-SPEC.md section 3a), "
            "or the label is corrected; no run command is suggested",
        ]
    else:
        lines += ["", "next:"] + [f"  {step}" for step in steps]
    return StageResult(
        stage="init",
        exit_code=0,
        _artifacts={
            "directory": directory,
            "files": list(names),
            "next": steps,
            "grade": {
                "grade": grade,
                "belowMinimum": [
                    {
                        "exposeId": short_expose,
                        "column": short_column,
                        "sensitivity": short_label,
                        "minimum": short_minimum,
                    }
                    for short_expose, short_column, short_label, short_minimum in below
                ],
            },
            "simulationId": bundle.simulation_id,
            "profileDigest": bundle.profile_digest,
            **({"assumptionsRequest": str(request)} if request is not None else {}),
            **(
                {"overlay": {"name": bundle.overlay.name, "digest": bundle.overlay.digest.value}}
                if bundle.overlay is not None
                else {}
            ),
            "parameters": {"declared": sum(bundle.counts.values()), "counts": dict(bundle.counts)},
            "exposes": exposes,
            "notes": [
                {"subject": subject, "assumption": sentence} for subject, sentence in bundle.notes
            ],
            "ledger": {
                "counts": bundle.ledger.counts(),
                "statistics": [entry.as_document() for entry in bundle.ledger],
                "replacedBy": [
                    {"pointer": pointer, "definition": definition, "assumptions": count}
                    for pointer, definition, count in bundle.ledger.replacements
                ],
            },
            "gaps": [{"pointer": gap.pointer, "reason": gap.reason} for gap in bundle.gaps],
            **_consumer_report_document(bundle),
        },
        stdout="\n".join(lines),
    )
