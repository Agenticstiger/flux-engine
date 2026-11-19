# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Library-level self-description for the FLUX Simulation Engine.

Importable as: ``from flux_engine.describe import self_describe``.
Pattern adapted from ``pulumi about --json`` (flat object, top-level keys
per category) -- the same pattern ``fluid_build.describe.self_describe``
uses on the FLUID side, so a future host application's backend route can
treat both engines uniformly. ``commands`` follows
``fluid_build.describe._command_tree``'s own port of that same
introspection idiom: ``fluid describe --format=json`` returns the full
command tree, the flag list, the output schemas and the provider
capabilities, so a host application can generate its UI dynamically and
never lag the CLI. This module is that promise on the FLUX side.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from typing import Any, Dict, List, Tuple

from flux_engine import __version__
from flux_engine.spec import SOURCE, SOURCE_VERSION

__all__ = ["installed_spec_version", "self_describe"]

#: The distribution :mod:`flux_engine.spec` takes the specification from.
#: Its version, not a constant in this file, is what a host is told it is
#: talking to.
_SPEC_DISTRIBUTION = "flux-spec"


def installed_spec_version() -> str:
    """The FLUX spec version this installation actually runs against.

    Read from the installed ``flux-spec`` distribution's own metadata, so an
    environment holding a newer spec is reported as holding a newer spec.
    A ``flux_spec`` that is importable but declares no metadata this engine
    can read (a source tree on ``sys.path``) reports the ``__version__`` it
    carries (:data:`flux_engine.spec.SOURCE_VERSION`).
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(_SPEC_DISTRIBUTION)
    except PackageNotFoundError:
        return SOURCE_VERSION


#: Capabilities this engine implements itself. They are present in every
#: installation -- the code that provides them is inside ``flux_engine`` --
#: so probing for a third-party import to decide whether to report them
#: answered the wrong question. It reported ``lineage`` false on a build
#: whose every run writes OpenLineage RunEvents, and ``calibrate`` false on
#: a build that ships ``fluxsim calibrate``; a host application reading this
#: document would have hidden two working features.
_ENGINE_CAPABILITIES: Tuple[str, ...] = (
    "accuracy",
    "calibrate",
    "evidence",
    "expr",
    "fidelity",
    "gate",
    "generator",
    "lineage",
)

#: Optional *backends* -- distributions this engine can use when they are
#: installed and does without when they are not. These are the only names
#: an import probe can honestly answer, and each is named for the backend
#: rather than for the feature, so "the pyabc backend is absent" can never
#: again be read as "calibration is absent".
_CAPABILITY_MODULES: Dict[str, str] = {
    "agentic": "flux_engine_agentic",
    "calibrate_abc": "pyabc",
    "lineage_transport": "openlineage",
}


def _detect_capabilities() -> Dict[str, bool]:
    detected: Dict[str, bool] = {name: True for name in _ENGINE_CAPABILITIES}
    for name, module in _CAPABILITY_MODULES.items():
        try:
            detected[name] = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError, ModuleNotFoundError):
            detected[name] = False
    return detected


def _describe_action(action: "argparse.Action") -> Dict[str, Any]:
    """One argparse option/positional -> a JSON-safe flag descriptor.

    Ported from ``fluid_build.describe._describe_action`` verbatim (see
    the module docstring's borrow-before-build note): the two engines'
    downstream consumers read one shape.
    """
    default = action.default
    if default is argparse.SUPPRESS or not isinstance(default, (str, int, float, bool, type(None))):
        default = None if default is argparse.SUPPRESS else str(default)
    return {
        "names": list(action.option_strings),  # [] for a positional
        "dest": action.dest,
        "positional": not action.option_strings,
        "required": bool(getattr(action, "required", False)),
        "help": action.help or "",
        "choices": [str(c) for c in action.choices] if action.choices else None,
        "default": default,
        "metavar": action.metavar,
    }


def _command_tree(parser: "argparse.ArgumentParser", _depth: int = 0) -> Dict[str, Any]:
    """Introspect an argparse parser into a ``{options, subcommands}`` tree.

    Ported from ``fluid_build.describe._command_tree``. Walks
    ``parser._actions`` for flags and the ``_SubParsersAction`` for
    subcommands -- the standard argparse-introspection idiom argparse
    itself has no public API for, but that shtab and argparse-manpage
    read the same private attributes to get. Read-only: no parsing, no
    side effects. Aliases (several names -> one subparser) are
    de-duplicated under ``aliases``.
    """
    options: List[Dict[str, Any]] = []
    subcommands: Dict[str, Any] = {}
    if _depth > 8:  # defensive recursion guard
        return {"options": options, "subcommands": subcommands}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: (a.help or "") for a in (action._choices_actions or [])}
            seen: Dict[int, str] = {}
            for name, sub in action.choices.items():
                if id(sub) in seen:
                    subcommands[seen[id(sub)]].setdefault("aliases", []).append(name)
                    continue
                seen[id(sub)] = name
                subcommands[name] = {
                    "help": helps.get(name, ""),
                    **_command_tree(sub, _depth + 1),
                }
        elif isinstance(action, argparse._HelpAction):
            continue
        else:
            options.append(_describe_action(action))
    return {"options": options, "subcommands": subcommands}


def self_describe() -> Dict[str, Any]:
    """Return a machine-readable description of this flux_engine installation.

    Returns a dict with stable keys:
      - component: always "FLUX Simulation Engine" (the canon name)
      - version: flux_engine's own version
      - python_version: the running interpreter's version
      - spec_source: always "flux_spec", the distribution the specification
        comes from (see flux_engine.spec.SOURCE)
      - spec_version: the FLUX spec version this installation runs
        against -- the installed ``flux-spec`` distribution's own version
        (:func:`installed_spec_version`)
      - capabilities: dict[str, bool]. The engine's own capabilities
        (:data:`_ENGINE_CAPABILITIES`) are always true -- the code that
        provides them ships in this package. The optional backends
        (:data:`_CAPABILITY_MODULES`) are derived by import probing, so
        ``capabilities["agentic"]`` is False unless the separate
        ``flux-engine-agentic`` distribution is installed.
      - commands: dict -- the CLI's argparse command tree (per-command
        options plus nested subcommands), so a consumer can render
        command/flag UI dynamically and never lag the CLI. Never lets a
        parser-construction issue crash ``describe`` itself: an import or
        build failure degrades to ``{"options": [], "subcommands": {},
        "error": "..."}`` rather than propagating.
    """
    try:
        from flux_engine.cli import build_parser

        commands = _command_tree(build_parser())
    except Exception as exc:  # describe must never crash on a parser-construction issue
        commands = {"options": [], "subcommands": {}, "error": str(exc)[:160]}

    return {
        "component": "FLUX Simulation Engine",
        "version": __version__,
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "spec_source": SOURCE,
        "spec_version": installed_spec_version(),
        "capabilities": _detect_capabilities(),
        "commands": commands,
    }
