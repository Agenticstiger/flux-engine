# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim replay``: re-execute a recorded plan and assert its run digest."""

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
    _add_state_dir,
    _digest_type,
)
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example
from flux_engine.cli._progress import stage_progress

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "replay": (
        Example(
            "fluxsim replay ./bundle --run-id de72fa4b22711c52 \\\n"
            "    --assert-digest sha256:42d923fd6f4b8c0a1e7d59b2c8a4f36e0d15927acb3e8f6410d2b7c95e8a3f01",
            "re-execute the plan; refuse unless the run digest is the one asserted",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim replay`` to *sub*, the commands of the ``fluxsim`` parser."""
    replay_p = sub.add_parser(
        "replay",
        help="re-execute a recorded plan; assert its digest",
        description=(
            "Re-execute a recorded plan and refuse unless the run digest it produces is "
            "the one asserted. The determinism claim, as a command."
        ),
    )
    replay_p.add_argument("bundle", help=_BUNDLE_HELP)
    replay_p.add_argument("--run-id", required=True, help=_RUN_ID_HELP)
    replay_p.add_argument(
        "--assert-digest",
        required=True,
        type=_digest_type,
        metavar="sha256:HEX",
        help=(
            "the runDigest this replay must reproduce, as sha256:<64 hex chars>; "
            "the bare hex is accepted and the prefix put back"
        ),
    )
    replay_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    replay_p.add_argument("--zone", default=None, help=_ZONE_HELP)
    _add_allow_generator(replay_p, when="while the replay generates the rows again")
    _add_jobs(replay_p)
    _add_state_dir(replay_p)
    _add_json_flag(replay_p)

    replay_p.set_defaults(stage_name="replay")


def _cmd_replay(args: argparse.Namespace) -> int:
    from flux_engine.stages.replay import replay

    with stage_progress(f"replaying {args.bundle}", as_json=args.json):
        result = replay(
            args.bundle,
            run_id=args.run_id,
            assert_digest=args.assert_digest,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
            zone=args.zone,
            jobs=args.jobs,
            allow_generators=tuple(args.allow_generator or ()),
        )
    return _print_result(result, as_json=args.json)
