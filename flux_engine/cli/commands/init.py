# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init``: write a small working project, or one for your own data, into a new directory."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine.cli._args import _add_json_flag
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "init": (
        Example("fluxsim init demo", "write a project into a new directory"),
        Example("fluxsim init .", "or into the current directory, if it is empty"),
        Example(
            "fluxsim init bundle --contract contract.orders.fluid.yaml \\\n"
            "    --profile orders.profile.json",
            "write a bundle for your own data, from its contract and its profile",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim init`` to *sub*, the commands of the ``fluxsim`` parser."""
    # First in the list because it is first in time: the command a newcomer
    # types before any bundle of their own exists.
    init_p = sub.add_parser(
        "init",
        help="write a small working project into a new directory",
        description=(
            "Write a small FLUX bundle and its FLUID contract into a directory, so a first "
            "run needs nothing else checked out. With --contract and --profile, write a "
            "bundle for your own data instead: the contract, and a Simulation declared from "
            "the profile's published aggregates, with every number labelled in its parameter "
            "register (from the profile, from the contract, or assumed, with the reason). "
            "The directory must not exist yet, or must be empty: nothing is ever overwritten."
        ),
    )
    init_p.add_argument("directory", help="where to write the project (created if missing)")
    init_p.add_argument(
        "--contract",
        default=None,
        metavar="FILE",
        help=(
            "a FLUID contract: the bundle generates rows for each of its exposes the "
            "profile describes; copied into the bundle unchanged (needs --profile)"
        ),
    )
    init_p.add_argument(
        "--profile",
        default=None,
        metavar="FILE",
        help=(
            "a profile document of that data (docs/spec/PROFILE-SPEC.md), such as `fluid "
            "profile` writes: the Simulation pins its digest and is declared from its "
            "published aggregates; it stays outside the bundle (needs --contract)"
        ),
    )
    init_p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "with --contract and --profile: write nothing, and exit 1, when a statistic the "
            "profile publishes is unused or a number rests on a gap, an assumption standing "
            "in for what the profile did not publish (docs/adr/0014); each one is named"
        ),
    )
    init_p.add_argument(
        "--allow",
        action="append",
        default=None,
        metavar="POINTER",
        help=(
            "with --strict: accept the unused statistics of the profile, or the gaps of the "
            "Simulation, at or below this JSON Pointer; repeatable, and refused when it "
            "accepts nothing"
        ),
    )
    _add_json_flag(init_p)

    init_p.set_defaults(stage_name="init")


def _cmd_init(args: argparse.Namespace) -> int:
    from flux_engine.cli._init import run_init

    return _print_result(
        run_init(
            args.directory,
            contract=args.contract,
            profile=args.profile,
            strict=args.strict,
            allow=tuple(args.allow or ()),
        ),
        as_json=args.json,
    )
