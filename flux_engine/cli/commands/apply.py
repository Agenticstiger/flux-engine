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
    _add_allow_generator,
    _add_jobs,
    _add_json_flag,
    _add_max_rows,
    _add_profile,
    _add_scale,
    _add_sink,
    _add_state_dir,
    _add_strict_coverage,
    _add_timings,
    _kv_pairs,
    _shard_type,
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
        Example(
            "fluxsim apply ./bundle --run-id de72fa4b22711c52 --shard 1/4",
            "write the first of four shares of that run (no receipt; a share is not a run)",
        ),
        Example(
            "fluxsim apply ./bundle --run-id de72fa4b22711c52 --sink parquet:./out/parquet",
            "apply the run whole and write every stream as Parquet",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim apply`` to *sub*, the commands of the ``fluxsim`` parser."""
    apply_p = sub.add_parser(
        "apply",
        help="apply a recorded plan and write its run directory",
        description=(
            "Write every run artifact of a recorded plan: the population, "
            "the events, the emit streams and their seam attestations, the run digest "
            "and the receipt. With --run-id, commits what plan staged after checking "
            "the plan, the bundle and every staged file, and generates nothing; with no "
            "staged rows it executes the plan again. Refuses when the plan or the bundle "
            "has moved since the plan was recorded. A run whose bundle declares generator "
            "plugins, once committed, is kept as it stands: apply never asks the plugin for "
            "its rows again or writes the run directory over, and with --sink it delivers "
            "the run's files. "
            "A child table that a counts entry lays out comes out grouped by parent in row "
            "order, as a table loaded parent by parent does; a consumer that needs another order "
            "sorts it. The child's own key is laid out from the row index, so a number or date key "
            "in ascending order gives a parent's children consecutive keys, and a text or UUID key, "
            "permuted, does not."
        ),
    )
    apply_p.add_argument("bundle", help=_BUNDLE_HELP)
    apply_p.add_argument(
        "--run-id", default=None, help=f"{_RUN_ID_HELP} (default: take a fresh plan first)"
    )
    apply_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    apply_p.add_argument("--zone", default=None, help=_ZONE_HELP)
    apply_p.add_argument(
        "--shard",
        default=None,
        type=_shard_type,
        metavar="I/N",
        help=(
            "make only the I-th of N contiguous shares of every emit's rows and write them as "
            "emits/<exposeId>.shard-I-of-N.ndjson; the shares joined in order are the bytes of "
            "the run's emit streams. A share is part of a run: no run digest and no receipt"
        ),
    )
    _add_sink(apply_p, when="after the run is written")
    _add_allow_generator(
        apply_p, when="when apply generates the rows rather than committing a stage"
    )
    _add_scale(apply_p, fresh_plan_only=True)
    _add_max_rows(apply_p, fresh_plan_only=True)
    _add_timings(apply_p)
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
            scale=args.scale,
            shard=args.shard,
            timings=args.timings,
            sink=args.sink,
            sink_options=_kv_pairs(args.sink_opt),
            allow_generators=tuple(args.allow_generator or ()),
            max_rows=args.max_rows,
        )
    return _finish(args, result)
