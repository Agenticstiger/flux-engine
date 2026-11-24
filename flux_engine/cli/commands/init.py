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
            "fluxsim init bundle --contract contract.orders.fluid.yaml",
            "no profile yet: write the profile request a profiler measures, then run again",
        ),
        Example(
            "fluxsim init bundle --contract contract.orders.fluid.yaml \\\n"
            "    --profile orders.profile.json",
            "write a bundle for your own data, from its contract and its profile",
        ),
        Example(
            "fluxsim init bundles --contract contract.orders.fluid.yaml \\\n"
            "    --profile orders.profile.json --overlay double-orders.yml",
            "write one bundle for each profile overlay, such as what if the table doubles",
        ),
        Example(
            "fluxsim init bundle --contract contract.orders.fluid.yaml \\\n"
            "    --profile orders.profile.json --consumers consumers.yml",
            "declare what your consumers' SQL needs: its levels, and report the rest",
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
            "Each assumed number a profile statistic would answer names it, and the profile "
            "request for exactly those statistics is written inside the directory, as "
            "requests/assumptions.profile-request.yml. "
            "With --contract and no profile, write the profile request instead: the list of "
            "statistics and fact shares a profiler must measure for that contract, to run "
            "init again with. A profile with date pairs, calendars or exposure also gives "
            "the Simulation lifetimes, within entries, rates and exposure: "
            "docs/how-to/keep-dates-inside-their-parents-lifetime.md reads them. "
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
            "profile describes; copied into the bundle unchanged. Without --profile, writes "
            "the profile request for this contract"
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
        "--request",
        default=None,
        metavar="FILE",
        help=(
            "with --contract and no --profile: where to write the profile request "
            "(docs/spec/PROFILE-REQUEST-SPEC.md); default: beside the directory, named "
            "after it, with the suffix .profile-request.yml"
        ),
    )
    init_p.add_argument(
        "--overlay",
        action="append",
        default=None,
        metavar="FILE",
        help=(
            "with --contract and --profile: a profile overlay (name and patch, docs/adr/0030), "
            "a named RFC 7396 merge patch of the numbers the profile publishes; repeatable. "
            "Writes one bundle per overlay, each in a directory of the one given named after "
            "the overlay, written from the overlaid profile, with the overlay in its Simulation "
            "and its register citing the overlay for the numbers it changed"
        ),
    )
    init_p.add_argument(
        "--consumers",
        default=None,
        metavar="FILE",
        help=(
            "a YAML file that holds a consumers block: the SQL your consumers run, in the "
            "dialect they write (docs/adr/0028). The bundle embeds it, declares the levels "
            "its queries filter on, and init reports which literals, patterns, nulls and "
            "ranges the bundle meets. With no --profile, the request also asks for the rate of "
            "each query and lists the columns they read together, and a held-out request "
            "(<directory>.held-out-request.yml) holds the queries you hold back. Needs "
            "--contract and the sql extra"
        ),
    )
    init_p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "with --contract and --profile: write nothing, and exit 1, when a statistic the "
            "profile publishes is unused or an assumption stands in for something the profile "
            "did not publish, a gap (docs/adr/0014); each one is named"
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
    init_p.add_argument(
        "--window",
        nargs=2,
        default=None,
        metavar=("START", "END"),
        help=(
            "the run's window, written as the Simulation's spec.timeBounds: two dates "
            "(YYYY-MM-DD, read as midnight UTC) or RFC 3339 timestamps, START before END; "
            "without it the window spans every date column's published range, the contract's "
            "declared date ranges and each date column's own bins; a date column's bins that "
            "leave a window given here are cut at it (needs --contract and --profile)"
        ),
    )
    init_p.add_argument(
        "--history",
        default=None,
        metavar="FILE",
        help=(
            "with --contract and --profile: a YAML file of a declared history and its "
            "projections (the namespace's history, snapshots, events, periods and asOf, and an "
            "optional timeBounds), merged into the bundle in place of the ones init would infer; "
            "counted layouts, exact allotments and the ended status's weight are written to fit it"
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
            request=args.request,
            overlays=tuple(args.overlay or ()),
            consumers=args.consumers,
            window=tuple(args.window) if args.window else None,
            history=args.history,
        ),
        as_json=args.json,
    )
