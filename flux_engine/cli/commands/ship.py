# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim ship``: the fidelity gate, then the evidence pack."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine.cli._args import (
    _BUNDLE_HELP,
    _OUT_DIR_HELP,
    _RUN_ID_HELP,
    _SLICE_HELP,
    _TRUST_KEY_HELP,
    _add_json_flag,
    _add_profile,
    _add_state_dir,
    _kv_pair_type,
    _kv_pairs,
    _read_signing_key,
    _read_trust_key,
    _signing_key_type,
    _trust_key_type,
)
from flux_engine.cli._output import _finish
from flux_engine.cli._parser import Example
from flux_engine.cli._progress import stage_progress

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "ship": (
        Example(
            "fluxsim ship ./bundle --run-id de72fa4b22711c52 -o ./out",
            "pack the evidence archive",
        ),
        Example(
            "fluxsim ship ./bundle --run-id de72fa4b22711c52 -o ./out \\\n"
            "    --committed-slice payment_recovery_moment=file:///data/slice.ndjson",
            "a bundle that declares a fidelity block needs the slice it is measured against",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim ship`` to *sub*, the commands of the ``fluxsim`` parser."""
    ship_p = sub.add_parser(
        "ship",
        help="the fidelity gate, then the evidence pack",
        description=(
            "Run the fidelity gate over every emit that declares a fidelity block, and "
            "only if none is withheld, pack the bundle and the run into one evidence "
            "archive with its in-toto statements. A run whose plan pins a profile needs "
            "it with --profile: it is gated and scored against it, and its scorecard "
            "travels in the archive."
        ),
    )
    ship_p.add_argument("bundle", help=_BUNDLE_HELP)
    ship_p.add_argument("--run-id", required=True, help=_RUN_ID_HELP)
    ship_p.add_argument("-o", "--out-dir", required=True, help=_OUT_DIR_HELP)
    ship_p.add_argument(
        "--committed-slice",
        action="append",
        type=_kv_pair_type,
        metavar="EXPOSE_ID=file://PATH",
        help=_SLICE_HELP,
    )
    _add_profile(ship_p, scores=True)
    ship_p.add_argument(
        "--signing-key",
        default=None,
        type=_signing_key_type,
        help=(
            "file holding a 32-byte ed25519 seed (raw or hex) to sign fidelity records "
            "with; without it the record is signed by a per-invocation key and says so"
        ),
    )
    ship_p.add_argument(
        "--trust-key",
        default=None,
        type=_trust_key_type,
        metavar="PATH",
        help=_TRUST_KEY_HELP,
    )
    _add_state_dir(ship_p)
    _add_json_flag(ship_p)

    ship_p.set_defaults(stage_name="ship")


def _cmd_ship(args: argparse.Namespace) -> int:
    from flux_engine.stages.ship import ship

    with stage_progress(f"shipping {args.bundle}", as_json=args.json):
        result = ship(
            args.bundle,
            run_id=args.run_id,
            out_dir=args.out_dir,
            state_dir=args.state_dir,
            committed_slice=_kv_pairs(args.committed_slice),
            signing_key=_read_signing_key(args),
            trust_key=_read_trust_key(args),
            profile=args.profile,
        )
    return _finish(args, result)
