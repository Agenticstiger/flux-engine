# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim apply``: apply a recorded plan and write its run directory."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine.cli._args import (
    _BUNDLE_HELP,
    _RUN_ID_HELP,
    _SIMULATION_ID_HELP,
    _ZONE_HELP,
    _add_jobs,
    _add_json_flag,
    _add_profile,
    _add_state_dir,
    _add_strict_coverage,
)
from flux_engine.cli._output import _finish
from flux_engine.cli._parser import Example
from flux_engine.cli._progress import stage_progress

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "apply": (
        Example(
            "fluxsim apply ./bundle --run-id de72fa4b22711c52",
            "apply the plan recorded under that id (`plan` prints it)",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim apply`` to *sub*, the commands of the ``fluxsim`` parser."""
    apply_p = sub.add_parser(
        "apply",
        help="apply a recorded plan and write its run directory",
        description=(
            "Re-execute a recorded plan and write every run artifact: the population, "
            "the events, the emit streams and their seam attestations, the run digest "
            "and the receipt. Refuses when the plan or the bundle has moved since the "
            "plan was recorded."
        ),
    )
    apply_p.add_argument("bundle", help=_BUNDLE_HELP)
    apply_p.add_argument(
        "--run-id", default=None, help=f"{_RUN_ID_HELP} (default: take a fresh plan first)"
    )
    apply_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    apply_p.add_argument("--zone", default=None, help=_ZONE_HELP)
    _add_jobs(apply_p)
    _add_strict_coverage(apply_p)
    _add_profile(apply_p, fresh_plan_only=True)
    _add_state_dir(apply_p)
    _add_json_flag(apply_p)

    apply_p.set_defaults(stage_name="apply")


def _cmd_apply(args: argparse.Namespace) -> int:
    from flux_engine.stages.apply import apply

    with stage_progress(f"applying {args.bundle}", as_json=args.json):
        result = apply(
            args.bundle,
            run_id=args.run_id,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
            zone=args.zone,
            jobs=args.jobs,
            strict_coverage=args.strict_coverage,
            profile=args.profile,
        )
    return _finish(args, result)
