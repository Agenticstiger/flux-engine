# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim describe --self``: describe this installation, as a document a host can read."""

from __future__ import annotations

import argparse
from typing import Any, Dict, Tuple

from flux_engine.cli._args import _add_json_flag
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "describe": (
        Example("fluxsim describe --self --json", "identity, capabilities and the command tree"),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim describe`` to *sub*, the commands of the ``fluxsim`` parser."""
    describe_p = sub.add_parser(
        "describe",
        help="self-describe this installation",
        description=(
            "Report this installation's identity, engine capabilities and full command "
            "tree, so a consumer can render UI without lagging the CLI."
        ),
    )
    describe_p.add_argument(
        "--self",
        dest="self_",
        action="store_true",
        help="describe this installation (currently the only mode)",
    )
    _add_json_flag(describe_p)

    describe_p.set_defaults(stage_name="describe")


def _describe_human_lines(result: Dict[str, Any]) -> str:
    """Render :func:`~flux_engine.describe.self_describe`'s result for a terminal.

    Hand-rendered, one line per top-level key -- never ``f"{key}: {value}"``
    over the raw dict, which for ``capabilities`` (a ``dict[str, bool]``)
    printed a Python ``repr`` (``{'agentic': False, ...}``) and for
    ``commands`` (the full argparse tree) would print an unreadable
    wall of nested dicts. camelCase keys throughout, matching every other
    JSON surface this CLI emits.
    """
    capabilities = result.get("capabilities", {})
    enabled = sorted(name for name, on in capabilities.items() if on)
    # The absent capabilities are the whole point of the README's "What it
    # is not" section, and printing only the true ones kept the engine's
    # say-what-is-absent posture for machines and dropped it for people: a
    # human could not see that `agentic`, `calibrate_abc` and
    # `lineage_transport` are off without re-running with --json.
    absent = sorted(name for name, on in capabilities.items() if not on)
    lines = [
        f"component: {result.get('component')}",
        f"version: {result.get('version')}",
        f"pythonVersion: {result.get('python_version')}",
        f"specSource: {result.get('spec_source')}",
        f"specVersion: {result.get('spec_version')}",
        f"capabilities: {', '.join(enabled) if enabled else '(none enabled)'}",
    ]
    if absent:
        lines.append(f"not available: {', '.join(absent)}")
    commands = result.get("commands", {})
    subcommands = sorted(commands.get("subcommands", {})) if isinstance(commands, dict) else []
    if subcommands:
        lines.append(f"commands: {', '.join(subcommands)} (pass --json for the full tree)")
    return "\n".join(lines)


def _cmd_describe(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.describe import self_describe

    result = self_describe()
    return _print_result(
        StageResult(
            stage="describe", exit_code=0, _artifacts=result, stdout=_describe_human_lines(result)
        ),
        as_json=args.json,
    )
