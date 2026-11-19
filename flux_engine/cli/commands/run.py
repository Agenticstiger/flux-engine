# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim run``: plan and apply in one call, then optionally verify, sink and ship."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine.cli._args import (
    _BUNDLE_HELP,
    _OUT_DIR_HELP,
    _SIMULATION_ID_HELP,
    _SLICE_HELP,
    _TRUST_KEY_HELP,
    _ZONE_HELP,
    _add_jobs,
    _add_json_flag,
    _add_profile,
    _add_state_dir,
    _add_strict_coverage,
    _kv_pair_type,
    _kv_pairs,
    _OnceAction,
    _read_signing_key,
    _read_trust_key,
    _signing_key_type,
    _sink_opt_type,
    _trust_key_type,
)
from flux_engine.cli._output import _finish
from flux_engine.cli._parser import Example
from flux_engine.cli._progress import stage_progress

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "run": (
        Example(
            "fluxsim run ./bundle --verify --ship -o ./out",
            "plan, apply, verify and ship in one command",
        ),
        Example("fluxsim run ./bundle", "plan and apply only"),
        Example(
            "fluxsim run ./bundle --sink parquet:./out/parquet",
            "plan, apply and write every stream as Parquet",
        ),
        Example(
            "fluxsim run ./bundle --sink adbc:duckdb --sink-opt uri=./sim.duckdb",
            "plan, apply and load every stream into a DuckDB table",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim run`` to *sub*, the commands of the ``fluxsim`` parser."""
    run_p = sub.add_parser(
        "run",
        help="plan + apply, with --verify/--sink/--ship sugar",
        description=(
            "Plan and apply in one call, then optionally verify, write every stream to a "
            "sink, and ship. Stops at the first stage that exits non-zero; the artifacts "
            "of every stage that did run are still returned under that stage's own key."
        ),
    )
    run_p.add_argument("bundle", help=_BUNDLE_HELP)
    run_p.add_argument("--simulation-id", default=None, help=_SIMULATION_ID_HELP)
    run_p.add_argument("--zone", default=None, help=_ZONE_HELP)
    run_p.add_argument(
        "--verify", action="store_true", help="run `fluxsim verify` on the applied run"
    )
    run_p.add_argument(
        "--ship", action="store_true", help="run `fluxsim ship` on the applied run; requires -o"
    )
    run_p.add_argument(
        "-o", "--out-dir", default=None, help=f"{_OUT_DIR_HELP} (required with --ship)"
    )
    run_p.add_argument(
        "--sink",
        default=None,
        action=_OnceAction,
        metavar="NAME:TARGET",
        help=(
            "also write every stream to a sink, after verify and before ship: "
            "parquet:DIR, arrow-ipc:DIR or adbc:DRIVER (`fluxsim plugins` lists them)"
        ),
    )
    run_p.add_argument(
        "--sink-opt",
        action="append",
        type=_sink_opt_type,
        metavar="KEY=VALUE",
        help=(
            "a --sink setting, repeatable, for example row-group-size=100000 or "
            "uri=sim.duckdb; never a password: pass uri-env=VAR instead"
        ),
    )
    run_p.add_argument(
        "--committed-slice",
        action="append",
        type=_kv_pair_type,
        metavar="EXPOSE_ID=file://PATH",
        help=_SLICE_HELP,
    )
    run_p.add_argument(
        "--update-golden",
        action="store_true",
        help="record this run as the golden baseline (requires --reason)",
    )
    run_p.add_argument("--reason", default=None, help="why the golden baseline is being updated")
    run_p.add_argument(
        "--allow-declared-baseline-drift",
        action="store_true",
        help=(
            "exit 0 even when the bundle's declared golden.baselineDigest does not "
            "match this run's runDigest"
        ),
    )
    run_p.add_argument(
        "--signing-key",
        default=None,
        type=_signing_key_type,
        help=(
            "file holding a 32-byte ed25519 seed (raw or hex) to sign fidelity records "
            "with; without it ship signs with a per-invocation key attributable to nobody"
        ),
    )
    run_p.add_argument(
        "--trust-key",
        default=None,
        type=_trust_key_type,
        metavar="PATH",
        help=_TRUST_KEY_HELP,
    )
    _add_jobs(run_p)
    _add_strict_coverage(run_p)
    _add_profile(run_p, run=True)
    _add_state_dir(run_p)
    _add_json_flag(run_p)

    run_p.set_defaults(stage_name="run")


def _cmd_run(args: argparse.Namespace) -> int:
    from flux_engine.stages.run import run

    # No early return: `stages.run.run` already refuses this case with the
    # right StageResult (exit 2, the same message), and returning here
    # instead printed a bare stderr line and NO --json envelope at all --
    # the one shape this CLI promises every command emits.
    signing_key = _read_signing_key(args)
    with stage_progress(f"running {args.bundle}", as_json=args.json):
        result = run(
            args.bundle,
            simulation_id=args.simulation_id,
            state_dir=args.state_dir,
            zone=args.zone,
            verify=args.verify,
            ship=args.ship,
            out_dir=args.out_dir,
            committed_slice=_kv_pairs(args.committed_slice),
            update_golden=args.update_golden,
            reason=args.reason,
            allow_declared_baseline_drift=args.allow_declared_baseline_drift,
            signing_key=signing_key,
            trust_key=_read_trust_key(args),
            sink=args.sink,
            sink_options=_kv_pairs(args.sink_opt),
            jobs=args.jobs,
            strict_coverage=args.strict_coverage,
            profile=args.profile,
        )
    return _finish(args, result)
