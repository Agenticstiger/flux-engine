# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim calibrate``: declared-scorecard calibration for a Playback."""

from __future__ import annotations

import argparse
from typing import Any, Dict, Tuple

from flux_engine.cli._args import _BUNDLE_HELP, _add_json_flag, _add_state_dir
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "calibrate": (
        Example("fluxsim calibrate ./bundle", "print the declared scorecard; nothing is written"),
        Example(
            "fluxsim calibrate ./bundle --write --run-id de72fa4b22711c52",
            "record it in the run directory",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim calibrate`` to *sub*, the commands of the ``fluxsim`` parser."""
    calibrate_p = sub.add_parser(
        "calibrate",
        help="declared-scorecard calibration for a Playback",
        description=(
            "Read a Playback's declared scorecard and report the credibility and grade "
            "it carries. Declared calibration only: no fitting is performed."
        ),
    )
    calibrate_p.add_argument("bundle", help=_BUNDLE_HELP)
    calibrate_p.add_argument(
        "--playback-id", default=None, help="default: the bundle's only Playback"
    )
    calibrate_p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="print the scorecard without writing calibration-provenance.json (default)",
    )
    calibrate_p.add_argument(
        "--write",
        dest="dry_run",
        action="store_false",
        help="write calibration-provenance.json into the run directory",
    )
    calibrate_p.add_argument("--run-id", default=None, help="required without --dry-run")
    _add_state_dir(calibrate_p)
    _add_json_flag(calibrate_p)

    calibrate_p.set_defaults(stage_name="calibrate")


def _scorecard_document(scorecard: Any) -> Dict[str, Any]:
    """A JSON-safe, camelCase view of a :class:`~flux_engine.api.calibration.Scorecard`.

    Not ``scorecard.__dict__``: a dataclass's internal attribute dict is an
    implementation detail (snake_case field names, ``Decimal``/``DigestRef``
    values that are not themselves JSON-safe), not a wire contract this CLI
    should let leak unreviewed.
    """
    return {
        "provenance": scorecard.provenance.value,
        "credibility": scorecard.credibility.value,
        "methodId": scorecard.method_id,
        "grade": str(scorecard.grade) if scorecard.grade is not None else None,
        "intervalCoverage": (
            str(scorecard.interval_coverage) if scorecard.interval_coverage is not None else None
        ),
        "backtestWindow": scorecard.backtest_window,
        "evidenceDigest": (
            scorecard.evidence_digest.value if scorecard.evidence_digest is not None else None
        ),
    }


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from pathlib import Path

    from flux_engine.api.stages import StageResult
    from flux_engine.calibrate.declared import DeclaredCalibrator
    from flux_engine.calibrate.provenance import write_calibration_provenance
    from flux_engine.seam.resolve import load_bundle

    loaded = load_bundle(args.bundle)
    playbacks = {
        doc_id: doc
        for doc_id, doc in loaded.flux_documents.items()
        if doc.get("kind") == "Playback"
    }
    if not playbacks:
        return _print_result(
            StageResult(
                stage="calibrate", exit_code=1, stderr=f"{args.bundle}: no Playback document"
            ),
            as_json=args.json,
        )
    playback_id = args.playback_id or sorted(playbacks)[0]
    if playback_id not in playbacks:
        return _print_result(
            StageResult(
                stage="calibrate",
                exit_code=1,
                stderr=f"{playback_id!r} is not a Playback in this bundle",
            ),
            as_json=args.json,
        )

    calibrator = DeclaredCalibrator(playback=playbacks[playback_id])
    _dials, scorecard = calibrator.calibrate({}, {})

    if args.dry_run:
        return _print_result(
            StageResult(
                stage="calibrate",
                exit_code=0,
                _artifacts={"playbackId": playback_id, "scorecard": _scorecard_document(scorecard)},
                stdout=(
                    # Not `key=value` for one half and an English phrase with a
                    # space in it for the other: one register per line. The JSON
                    # side carries the machine-readable spelling.
                    f"{playback_id}: credibility {scorecard.credibility.value}, "
                    f"{f'grade {scorecard.grade}' if scorecard.grade is not None else 'not graded'}"
                ),
            ),
            as_json=args.json,
        )

    if not args.run_id:
        return _print_result(
            StageResult(
                stage="calibrate", exit_code=2, stderr="calibrate --write requires --run-id"
            ),
            as_json=args.json,
        )
    from datetime import datetime, timezone

    from flux_engine.stages.layout import RunLayout

    world_ref = str(playbacks[playback_id].get("spec", {}).get("worldRef", playback_id))
    layout = RunLayout.for_state_dir(Path(args.state_dir or ".flux"), args.run_id).ensure()
    path = write_calibration_provenance(
        layout.run_dir / "calibration-provenance.json",
        world_ref=world_ref,
        dials={},
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return _print_result(
        StageResult(
            stage="calibrate", exit_code=0, _artifacts={"path": str(path)}, stdout=f"wrote {path}"
        ),
        as_json=args.json,
    )
