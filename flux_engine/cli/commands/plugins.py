# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim plugins``: list the installed vertical packs, sinks and generator plugins."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Dict, List, Sequence, Tuple

from flux_engine.cli._args import _add_json_flag
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example
from flux_engine.seam.coverage import printable

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from flux_engine.generators.registry import GeneratorInfo
    from flux_engine.sinks.registry import SinkInfo

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "plugins": (
        Example("fluxsim plugins", "list the vertical packs, the sinks and the generator plugins"),
        Example("fluxsim plugins --json", "the same, as JSON"),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim plugins`` to *sub*, the commands of the ``fluxsim`` parser."""
    plugins_p = sub.add_parser(
        "plugins",
        help="list installed vertical packs, sinks and generator plugins",
        description=(
            "List the vertical packs, the output sinks and the generator plugins available "
            "in this environment, with the extra each sink needs and whether it is "
            "installed, and for each generator plugin its distribution and whether it is "
            "an editable install or lies outside an installation directory. Nothing is "
            "imported to find them. A generator plugin is your own code: it runs with your "
            "rights, and FLUX does not sandbox its network or file access, so allow only "
            "plugins you trust (docs/explanation/offline.md)."
        ),
    )
    _add_json_flag(plugins_p)

    plugins_p.set_defaults(stage_name="plugins")


def _sink_lines(sinks: Sequence["SinkInfo"]) -> List[str]:
    """The ``sinks`` section: each name, its source, and the extra it needs or its target."""
    width = max((len(sink.name) for sink in sinks), default=0)
    lines = []
    for sink in sinks:
        if sink.extra:
            state = "installed" if sink.installed else "not installed"
            needs = f"needs flux-sim-engine[{sink.extra}] ({state})"
        else:
            needs = "shadowed by the built-in of that name" if sink.shadowed else sink.value
        lines.append(f"  {sink.name.ljust(width)}  {sink.source}  {needs}")
    return lines


def _generator_markers(generator: "GeneratorInfo") -> str:
    """``  (editable, outside an installation directory)`` for the markers that apply, else empty."""
    markers = []
    if generator.editable:
        markers.append("editable")
    if generator.outside:
        markers.append("outside an installation directory")
    return f"  ({', '.join(markers)})" if markers else ""


def _generator_lines(generators: Sequence["GeneratorInfo"]) -> List[str]:
    """The ``generators`` section.

    A name, a distribution and an entry-point value come from installed metadata, which a
    terminal must not be handed raw.
    """
    if not generators:
        return ["  none installed"]
    width = max(len(printable(generator.name)) for generator in generators)
    return [
        f"  {printable(generator.name).ljust(width)}  {printable(generator.source)}  "
        f"{printable(generator.value)}{_generator_markers(generator)}"
        for generator in generators
    ]


def _cmd_plugins(args: argparse.Namespace) -> int:
    from flux_engine.api.stages import StageResult
    from flux_engine.generators.registry import available as available_generators
    from flux_engine.sinks.registry import available
    from flux_engine.verticals import pack_names

    names = list(pack_names())
    sinks = available()
    generators = available_generators()
    lines = ["vertical packs:"]
    lines.extend(f"  {name}" for name in names)
    lines.append("sinks (fluxsim run --sink NAME:TARGET):")
    lines.extend(_sink_lines(sinks))
    lines.append("generators (entry points of flux_engine.generators, none imported):")
    lines.extend(_generator_lines(generators))
    return _print_result(
        StageResult(
            stage="plugins",
            exit_code=0,
            _artifacts={
                "verticalPacks": names,
                "sinks": [sink.as_document() for sink in sinks],
                "generators": [generator.as_document() for generator in generators],
            },
            stdout="\n".join(lines),
        ),
        as_json=args.json,
    )
