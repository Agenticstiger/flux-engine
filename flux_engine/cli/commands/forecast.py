# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim forecast``: forecasts for the next state, sealed in the prediction registry."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine.cli._args import (
    _BUNDLE_HELP,
    _SIMULATION_ID_HELP,
    _add_jobs,
    _add_json_flag,
    _add_profile,
    _add_state_dir,
)
from flux_engine.cli._output import _finish
from flux_engine.cli._parser import Example
from flux_engine.cli._progress import stage_progress

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "forecast": (
        Example(
            "fluxsim forecast make ./bundle --profile state.json "
            "--registered-at 2026-07-01T00:00:00Z --horizon-end 2026-09-30T23:59:59Z",
            "forecast the next state and seal every forecast",
        ),
        Example(
            "fluxsim forecast score ./bundle --profile state.json --next next-state.json "
            "--observed-at 2026-10-01T00:00:00Z",
            "score every forecast once the next state exists",
        ),
    ),
    "forecast score": (
        Example(
            "fluxsim forecast score ./bundle --profile state.json --next next-state.json "
            "--observed-at 2026-10-01T00:00:00Z",
            "score every forecast against the next state's cells",
        ),
    ),
    "forecast make": (
        Example(
            "fluxsim forecast make ./bundle --profile state.json "
            "--registered-at 2026-07-01T00:00:00Z --horizon-end 2026-09-30T23:59:59Z",
            "run the sweep, then register each item's forecast",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim forecast`` to *sub*, the commands of the ``fluxsim`` parser."""
    forecast_p = sub.add_parser(
        "forecast",
        help="forecast the next state and seal the forecasts",
        description=(
            "Forecast what the next profile will show, before it is seen, and seal each "
            "forecast in the outcome-accuracy store."
        ),
    )
    forecast_sub = forecast_p.add_subparsers(
        dest="forecast_command", title="commands", metavar="<command>"
    )
    make_p = forecast_sub.add_parser(
        "make",
        help="run the sweep and register each forecast",
        description=(
            "Run the Simulation's spec.sweep as replicates, each seeded as the sweep seeds "
            "it and each taking the effect at the midpoint of its part of the response "
            "estimate's interval. For each forecast item, take the median of its value over "
            "the replicates and an interval around it, and register that, sealed and "
            "write-once, under the item's id and the pinned profile's digest. The same "
            "forecast again changes nothing; another forecast under that id is refused "
            "(FLX-7007), and the refusal says when the id belongs to another bundle."
        ),
    )
    make_p.add_argument("bundle", help=_BUNDLE_HELP)
    make_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    make_p.add_argument(
        "--registered-at",
        required=True,
        metavar="T",
        help=(
            "the instant the forecasts are sealed at, as RFC 3339; the engine reads no clock, "
            "and a later score may not observe anything before it (FLX-7001)"
        ),
    )
    make_p.add_argument(
        "--horizon-end",
        required=True,
        metavar="T",
        help="the instant after which the next state can be read, as RFC 3339",
    )
    _add_jobs(make_p)
    _add_profile(make_p)
    _add_state_dir(make_p)
    _add_json_flag(make_p)
    make_p.set_defaults(stage_name="forecast-make")

    score_p = forecast_sub.add_parser(
        "score",
        help="score every forecast against the next state",
        description=(
            "Read the next state's profile and give a verdict to every forecast registered "
            "for the bundle, none skipped. The observation is the next state's cell for the "
            "forecast's query and segment; a withheld cell, or one that holds no records, "
            "leaves the forecast not scored, with the reason. Beside inside or outside the "
            "interval, each verdict holds the absolute error and the interval score, and "
            "the grade of each state's profile. A next state that is the pinned state is "
            "refused (FLX-7143). When forecasts.playback names a Playback that declares drift, "
            "the two states' histograms and the query cells the families read are compared "
            "with its metric (psi, kl or ks), each verdict records the largest value, and a "
            "statistic past the threshold marks the calibration stale; a cell withheld or with "
            "no records in either state is left out and listed. With onBreach fail the "
            "command then exits 1 (FLX-7146), after every verdict is written."
        ),
    )
    score_p.add_argument("bundle", help=_BUNDLE_HELP)
    score_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    score_p.add_argument(
        "--profile",
        required=True,
        metavar="STATE_K",
        help=(
            "the profile the Simulation pins, the state the forecasts were made on; refused "
            "when its digest is not the pinned one"
        ),
    )
    score_p.add_argument(
        "--next",
        required=True,
        dest="next_profile",
        metavar="STATE_K1",
        help=(
            "the profile of the next state, a later release answering the same request; "
            "refused when it is the pinned state (FLX-7143)"
        ),
    )
    score_p.add_argument(
        "--observed-at",
        required=True,
        metavar="T",
        help=(
            "the instant the next state was read, as RFC 3339; it may not predate a "
            "forecast's registration (FLX-7001) or its horizon end, the instant after which "
            "the next state can be read (FLX-7013)"
        ),
    )
    _add_state_dir(score_p)
    _add_json_flag(score_p)
    score_p.set_defaults(stage_name="forecast-score")


def _cmd_forecast_make(args: argparse.Namespace) -> int:
    from flux_engine.outcomes.forecast import forecast_make

    with stage_progress(f"forecasting {args.bundle}", as_json=args.json):
        result = forecast_make(
            args.bundle,
            registered_at=args.registered_at,
            horizon_end=args.horizon_end,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
            profile=args.profile,
            jobs=args.jobs,
        )
    return _finish(args, result)


def _cmd_forecast_score(args: argparse.Namespace) -> int:
    from flux_engine.outcomes.scoring import forecast_score

    with stage_progress(f"scoring {args.bundle}", as_json=args.json):
        result = forecast_score(
            args.bundle,
            profile=args.profile,
            next_profile=args.next_profile,
            observed_at=args.observed_at,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
        )
    return _finish(args, result)
