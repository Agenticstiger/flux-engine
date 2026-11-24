# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim verify``: golden regression, row conformance, RFC-04 and fidelity for a run."""

from __future__ import annotations

import argparse
from typing import Dict, Tuple

from flux_engine.cli._args import (
    _BUNDLE_HELP,
    _RUN_ID_HELP,
    _SLICE_HELP,
    _add_json_flag,
    _add_profile,
    _add_query_flags,
    _add_state_dir,
    _jobs_type,
    _kv_pair_type,
    _kv_pairs,
    _read_signing_key,
    _read_trust_key,
    _sample_fraction_type,
    _signing_key_type,
    _trust_key_type,
    _whole_number_type,
)
from flux_engine.cli._output import _finish
from flux_engine.cli._parser import Example
from flux_engine.cli._progress import stage_progress

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "verify": (
        Example(
            "fluxsim verify ./bundle --run-id de72fa4b22711c52",
            "re-check an applied run on five independent legs",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim verify`` to *sub*, the commands of the ``fluxsim`` parser."""
    verify_p = sub.add_parser(
        "verify",
        help="golden regression + row conformance + RFC-04 + fidelity",
        description=(
            "Re-check an applied run on five independent legs: the run's own artifacts "
            "against its recorded digest, the golden regression, row conformance "
            "against the referenced contract, the RFC-04 enforcement vectors, and "
            "every declared fidelity check. One line per leg, with the flag that turns "
            "a `not checked` leg into a result. A run whose plan pins a profile is also "
            "scored against it, per property, table, dimension and overall, when the "
            "profile is passed with --profile. A bundle that declares consumer queries "
            "has every query counted on the run, the calibration queries apart from the "
            "held-out ones (--held-out), in the scorecard's queries dimension. Each counts "
            "entry of the bundle adds the rule lines counts:<relationship>.minChildren and "
            "counts:<relationship>.parentWhen to the child's rules, both enforced, and a "
            "profile that publishes the children per parent of a relationship adds a "
            "childrenPerParent row to the scorecard; a row of a relationship whose counts "
            "entry has given fails the stage when it is outside the entry's tolerance."
        ),
    )
    verify_p.add_argument("bundle", help=_BUNDLE_HELP)
    verify_p.add_argument("--run-id", required=True, help=_RUN_ID_HELP)
    verify_p.add_argument(
        "--update-golden",
        action="store_true",
        help="record this run as the golden baseline (requires --reason)",
    )
    verify_p.add_argument("--reason", default=None, help="why the golden baseline is being updated")
    verify_p.add_argument(
        "--committed-slice",
        action="append",
        type=_kv_pair_type,
        metavar="EXPOSE_ID=file://PATH",
        help=_SLICE_HELP,
    )
    _add_profile(verify_p, scores=True)
    verify_p.add_argument(
        "--sample",
        default=None,
        type=_sample_fraction_type,
        metavar="FRACTION",
        help=(
            "compare each emit's means, histograms and quantile bins with the pinned profile "
            "on a sample: FRACTION of its blocks of records, each estimate decided by its "
            "95 per cent interval (docs/spec/SAMPLE-SPEC.md)"
        ),
    )
    verify_p.add_argument(
        "--sample-block",
        default=None,
        type=_whole_number_type(1),
        metavar="N",
        help="records in a block of the sample (default: 1000); needs --sample",
    )
    verify_p.add_argument(
        "--sample-seed",
        default=None,
        type=_whole_number_type(0),
        metavar="N",
        help="seed of the choice of blocks (default: 0); needs --sample",
    )
    verify_p.add_argument("--vectors", default=None, help="path to an RFC-04 vectors file")
    verify_p.add_argument(
        "--allow-declared-baseline-drift",
        action="store_true",
        help=(
            "exit 0 even when the bundle's declared golden.baselineDigest does not "
            "match this run's runDigest"
        ),
    )
    verify_p.add_argument(
        "--signing-key",
        default=None,
        type=_signing_key_type,
        help=(
            "file holding the 32-byte ed25519 seed a prior `fluxsim ship` signed with; "
            "its public half becomes the trust anchor, and the check says it is "
            "self-attested"
        ),
    )
    verify_p.add_argument(
        "--trust-key",
        default=None,
        type=_trust_key_type,
        metavar="PATH",
        # Not `_TRUST_KEY_HELP`: that text ends "without it the record is
        # checked against the public half of the key that signed it",
        # which is true of `ship` (it holds the key it just signed with)
        # and false here. `verify` signs nothing, so with no key there is
        # no anchor, and the check says so.
        help=(
            "file holding the 32-byte raw ed25519 PUBLIC key (raw or hex -- the "
            "`publicKey` a fidelity record prints) any fidelity record this run "
            "already holds is checked against; without it, and without "
            "--signing-key, the signature is reported as not checked"
        ),
    )
    verify_p.add_argument(
        "--jobs",
        default=None,
        type=_jobs_type,
        metavar="N",
        help=(
            "the most processes that may read the run's emits at once, one emit to a "
            "process: a number or auto for one per CPU; the verdicts and every byte of the "
            "scorecard are the same whatever the number (default: FLUX_JOBS, else 1)"
        ),
    )
    _add_query_flags(verify_p)
    _add_state_dir(verify_p)
    _add_json_flag(verify_p)

    verify_p.set_defaults(stage_name="verify")


def check_sample_options(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """``--sample-block`` and ``--sample-seed`` shape a sample, so they need ``--sample``."""
    if args.sample is None and (args.sample_block is not None or args.sample_seed is not None):
        parser.error("--sample-block and --sample-seed need --sample")


def _cmd_verify(args: argparse.Namespace) -> int:
    from flux_engine.concordance.sampling import DEFAULT_BLOCK_RECORDS, SampleRequest
    from flux_engine.stages.verify import verify

    sample = (
        SampleRequest(
            fraction=args.sample,
            block_records=args.sample_block or DEFAULT_BLOCK_RECORDS,
            seed=args.sample_seed or 0,
        )
        if args.sample is not None
        else None
    )
    with stage_progress(f"verifying {args.bundle}", as_json=args.json):
        result = verify(
            args.bundle,
            run_id=args.run_id,
            state_dir=args.state_dir,
            update_golden=args.update_golden,
            reason=args.reason,
            committed_slice=_kv_pairs(args.committed_slice),
            vectors_path=args.vectors,
            allow_declared_baseline_drift=args.allow_declared_baseline_drift,
            signing_key=_read_signing_key(args),
            trust_key=_read_trust_key(args),
            profile=args.profile,
            sample=sample,
            jobs=args.jobs,
            held_out=args.held_out,
            max_query_seconds=args.max_query_seconds,
            max_query_memory=args.max_query_memory,
        )
    return _finish(args, result)
