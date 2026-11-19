# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim plugins``: list the installed vertical packs and sinks."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine.cli._args import _add_json_flag
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "plugins": (
        Example("fluxsim plugins", "list the vertical packs and the sinks"),
        Example("fluxsim plugins --json", "the same, as JSON"),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim plugins`` to *sub*, the commands of the ``fluxsim`` parser."""
    plugins_p = sub.add_parser(
        "plugins",
        help="list installed vertical packs and sinks",
        description=(
            "List the vertical packs and the output sinks available in this environment, "
            "with the extra each sink needs and whether it is installed. Nothing is "
            "imported to find them."
        ),
    )
    _add_json_flag(plugins_p)

    plugins_p.set_defaults(stage_name="plugins")


def _cmd_plugins(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.sinks.registry import available
    from flux_engine.verticals import pack_names

    names = list(pack_names())
    sinks = available()
    lines = ["vertical packs:"]
    lines.extend(f"  {name}" for name in names)
    lines.append("sinks (fluxsim run --sink NAME:TARGET):")
    width = max((len(sink.name) for sink in sinks), default=0)
    for sink in sinks:
        if sink.extra:
            state = "installed" if sink.installed else "not installed"
            needs = f"needs flux-sim-engine[{sink.extra}] ({state})"
        else:
            needs = "shadowed by the built-in of that name" if sink.shadowed else sink.value
        lines.append(f"  {sink.name.ljust(width)}  {sink.source}  {needs}")
    return _print_result(
        StageResult(
            stage="plugins",
            exit_code=0,
            _artifacts={
                "verticalPacks": names,
                "sinks": [sink.as_document() for sink in sinks],
            },
            stdout="\n".join(lines),
        ),
        as_json=args.json,
    )
