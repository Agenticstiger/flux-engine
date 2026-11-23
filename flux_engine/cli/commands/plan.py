# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim plan``: execute the loop once and record ``plan.json``."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine.cli._args import (
    _BUNDLE_HELP,
    _SIMULATION_ID_HELP,
    _ZONE_HELP,
    _add_allow_generator,
    _add_jobs,
    _add_json_flag,
    _add_profile,
    _add_scale,
    _add_state_dir,
    _add_strict_coverage,
    _add_timings,
)
from flux_engine.cli._output import _finish
from flux_engine.cli._parser import Example
from flux_engine.cli._progress import stage_progress

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "plan": (
        Example("fluxsim plan ./bundle", "record what a run would do"),
        Example("fluxsim plan ./bundle --state-dir ./.flux", "keep the run state somewhere else"),
        Example("fluxsim plan ./bundle --kernel 0.1.0", "plan the run under kernel 0.1.0"),
        Example("fluxsim plan ./bundle --no-stage", "record the plan only, without staging rows"),
        Example(
            "fluxsim plan ./bundle --scale 10 --timings", "plan ten times the rows, and time it"
        ),
    ),
}

#: The kernels ``--kernel`` offers: :data:`flux_engine.core.kernels.KERNELS`'s versions,
#: written out so that building the parser imports no kernel code.
#: ``tests/cli/test_plan_kernel.py`` holds the two to each other.
KERNEL_CHOICES = ("0.1.0", "0.2.0")


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim plan`` to *sub*, the commands of the ``fluxsim`` parser."""
    plan_p = sub.add_parser(
        "plan",
        help="execute the loop once and record plan.json",
        description=(
            "Execute the whole pipeline once, in memory, and write down what it saw: "
            "inputDigest, the seed-tree paths it materialised, the per-emit seam "
            "digests, and each declared fidelity block as it compiled, every epsilon "
            "included. Writes plan.json and "
            "seed-paths.ndjson under the run directory, and stages the rows it generated "
            "there too, so that apply --run-id commits them instead of generating them "
            "again. A child table that a counts entry lays out comes out grouped by parent in row "
            "order, as a table loaded parent by parent does; a consumer that needs another order "
            "sorts it. The child's own key is laid out from the row index, so a number or date key "
            "in ascending order gives a parent's children consecutive keys, and a text or UUID key, "
            "permuted, does not."
        ),
    )
    plan_p.add_argument("bundle", help=_BUNDLE_HELP)
    plan_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    plan_p.add_argument("--zone", default=None, help=_ZONE_HELP)
    plan_p.add_argument(
        "--kernel",
        default=None,
        choices=KERNEL_CHOICES,
        metavar="VERSION",
        help=(
            "the kernel the new plan takes, one of "
            + ", ".join(KERNEL_CHOICES)
            + " (default 0.2.0). The plan records it, and apply, replay, verify and ship "
            "run the kernel their plan names; 0.2.0's streams are counter-based and its "
            "continuous draws binary64 (docs/spec/KERNEL-SPEC-2.md), so its digests differ "
            "from 0.1.0's"
        ),
    )
    plan_p.add_argument(
        "--no-stage",
        action="store_true",
        help=(
            "write only plan.json and the seed paths; apply --run-id then generates the "
            "rows itself, as a plan did before it staged them (refused for a bundle that "
            "declares generator plugins, whose bytes are stored and never generated again)"
        ),
    )
    _add_allow_generator(plan_p, when="when the plan generates the rows")
    _add_scale(plan_p)
    _add_timings(plan_p)
    _add_jobs(plan_p)
    _add_strict_coverage(plan_p)
    _add_profile(plan_p)
    _add_state_dir(plan_p)
    _add_json_flag(plan_p)

    plan_p.set_defaults(stage_name="plan")


def _cmd_plan(args: argparse.Namespace) -> int:
    from flux_engine.stages.plan import plan

    with stage_progress(f"planning {args.bundle}", as_json=args.json):
        result = plan(
            args.bundle,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
            zone=args.zone,
            jobs=args.jobs,
            strict_coverage=args.strict_coverage,
            profile=args.profile,
            kernel=args.kernel,
            stage=not args.no_stage,
            scale=args.scale,
            timings=args.timings,
            allow_generators=tuple(args.allow_generator or ()),
        )
    return _finish(args, result)
