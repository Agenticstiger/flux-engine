# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim calibrate``: declared-scorecard calibration for a Playback."""

from __future__ import annotations

import argparse
from typing import Any, Dict, Tuple

from flux_engine.cli._args import (
    _BUNDLE_HELP,
    _add_json_flag,
    _add_query_flags,
    _add_state_dir,
    _whole_number_type,
)
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "calibrate": (
        Example("fluxsim calibrate ./bundle", "print the declared scorecard; nothing is written"),
        Example(
            "fluxsim calibrate ./bundle --write --run-id de72fa4b22711c52",
            "record it in the run directory",
        ),
        Example(
            "fluxsim calibrate --fit ./bundle --profile pinned.profile.json "
            "--held-out held-out.profile.json --out ./calibrated",
            "fit the declared parameters to the calibration queries and write ./calibrated.patch.json",
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
            "it carries; no fitting is performed. With --fit, fit the parameters a bundle's "
            "consumers calibration block declares to the rates of its calibration queries "
            "(CMA-ES, from the [calibrate] extra), and write the result as a calibration patch, "
            "DIR.patch.json, beside the output. A bundle that declares no held-out query is fitted "
            "without a held-out check, and the output says so."
        ),
    )
    calibrate_p.add_argument("bundle", nargs="?", default=None, help=_BUNDLE_HELP)
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
    calibrate_p.add_argument(
        "--fit",
        default=None,
        metavar="BUNDLE",
        help=(
            "fit the declared parameters of this bundle to its calibration queries instead of "
            "reading a declared scorecard; needs --profile and --out, and --held-out when the "
            "bundle declares a held-out query"
        ),
    )
    calibrate_p.add_argument(
        "--profile",
        default=None,
        metavar="PATH",
        help=(
            "with --fit: the profile the Simulation pins, whose queries section holds the targets "
            "the fit reads; refused when its digest is not the pinned one"
        ),
    )
    calibrate_p.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help=(
            "with --fit: where the calibration patch (DIR.patch.json) and, only when the "
            "held-out error holds, the calibrated bundle (DIR) are written; neither may exist"
        ),
    )
    calibrate_p.add_argument(
        "--max-evaluations",
        default=None,
        type=_whole_number_type(1),
        metavar="N",
        help=(
            "with --fit: raise the evaluations a fit may run above the engine's 64, up to 4096, "
            "for a bundle whose calibration block declares no evaluations; a bundle's own value "
            "is kept and may only lower the ceiling"
        ),
    )
    _add_query_flags(calibrate_p)
    _add_state_dir(calibrate_p)
    _add_json_flag(calibrate_p)

    calibrate_p.set_defaults(stage_name="calibrate")


#: The options only ``--fit`` reads.
_FIT_ONLY = (
    ("profile", "--profile"),
    ("out", "--out"),
    ("held_out", "--held-out"),
    ("max_evaluations", "--max-evaluations"),
    ("max_query_seconds", "--max-query-seconds"),
    ("max_query_memory", "--max-query-memory"),
)


def check_fit_options(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """``--fit`` takes the bundle itself and needs ``--profile`` and ``--out``; the rest belong to it.

    ``--held-out`` is needed only by a bundle that declares a held-out query; the fit says so once it has read the bundle.
    """
    if args.fit is None:
        stray = [flag for name, flag in _FIT_ONLY if getattr(args, name) is not None]
        if stray:
            parser.error(f"{', '.join(stray)} need --fit")
        if args.bundle is None:
            parser.error("name a bundle (fluxsim calibrate BUNDLE) or fit one (--fit BUNDLE)")
        return
    if args.bundle is not None:
        parser.error("--fit takes the bundle itself (--fit BUNDLE); drop the positional bundle")
    missing = [
        flag
        for name, flag in (("profile", "--profile"), ("out", "--out"))
        if getattr(args, name) is None
    ]
    if missing:
        parser.error(f"--fit needs {' and '.join(missing)}")
    if args.run_id is not None or not args.dry_run:
        parser.error(
            "--fit writes a patch beside --out; --write and --run-id are for the declared mode"
        )


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


def _display(value: Any) -> str:
    """A gap or a tolerance for a person: its exact text when short, else four decimals."""
    from flux_stats.spelling import render_exact

    if value is None:
        return "none"
    text = render_exact(value)
    return text if len(text) <= 8 and "/" not in text else f"{float(value):.4f}"


def _gate_document(outcome: Any) -> Any:
    """The held-out check as ``--json`` carries it; ``None`` when no held-out profile was given."""
    from flux_stats.spelling import render_exact

    gate = outcome.gate
    if gate is None:
        return None
    return {
        "profile": gate.profile,
        "grade": gate.grade,
        "pinnedGrade": gate.pinned_grade,
        "queries": [
            {
                "id": query.id,
                "status": query.status,
                "gap": render_exact(query.gap) if query.gap is not None else None,
                "tolerance": render_exact(query.tolerance) if query.tolerance is not None else None,
                "within": query.within,
                "why": query.why or None,
            }
            for query in gate.queries
        ],
        "untargeted": list(gate.untargeted),
    }


def _fit_lines(outcome: Any) -> Any:
    moved = len(outcome.operations)
    lines = [
        f"fit: {outcome.result.evaluations} evaluations, {moved} "
        f"{'value' if moved == 1 else 'values'} moved",
        f"wrote {outcome.patch_path} ({outcome.patch_digest})",
    ]
    gate = outcome.gate
    if outcome.held_out_note is not None:
        lines.append(outcome.held_out_note)
    if gate is not None:
        lines.append(f"grade: pinned profile {gate.pinned_grade}, held-out profile {gate.grade}")
        for query in gate.queries:
            verdict = "within" if query.within else f"missed ({query.why})"
            lines.append(
                f"held-out query {query.id}: gap {_display(query.gap)}, "
                f"tolerance {_display(query.tolerance)}: {verdict}"
            )
        lines += [
            f"held-out query {name}: the profile states no target" for name in gate.untargeted
        ]
    return lines


def _not_calibrated(outcome: Any) -> Any:
    from flux_engine.calibrate.errors import FLX_3712
    from flux_engine.core.errors import FluxError

    gate = outcome.gate
    if not outcome.operations:
        reason = "no candidate beat the start point, so no value moved"
    elif gate is None:
        reason = outcome.held_out_note or "no held-out profile was given"
    elif not gate.queries:
        reason = "no held-out query has a target"
    else:
        reason = "; ".join(
            f"held-out query {query.id} missed with gap {_display(query.gap)} against "
            f"tolerance {_display(query.tolerance)} ({query.why})"
            for query in gate.missed
        )
    return FluxError(
        FLX_3712, f"{reason}; the patch is at {outcome.patch_path} and no bundle was written"
    )


def _cmd_fit(args: argparse.Namespace) -> int:
    from pathlib import Path

    from flux_engine.api.stages import StageResult
    from flux_engine.calibrate.fit import FitRequest, HeldOutNeeded, run_fit
    from flux_engine.core.errors import FluxError

    request = FitRequest(
        bundle=Path(args.fit),
        profile=Path(args.profile),
        out=Path(args.out),
        held_out=Path(args.held_out) if args.held_out is not None else None,
        state_dir=Path(args.state_dir) if args.state_dir is not None else None,
        max_evaluations=args.max_evaluations,
        max_query_seconds=args.max_query_seconds,
        max_query_memory=args.max_query_memory,
    )
    try:
        outcome = run_fit(request)
    except HeldOutNeeded as error:
        return _print_result(
            StageResult(
                stage="calibrate", exit_code=2, stderr=f"fluxsim calibrate: error: {error}"
            ),
            as_json=args.json,
        )
    except FluxError as error:
        return _print_result(
            StageResult(
                stage="calibrate", exit_code=1, stderr=str(error), error_code=error.code.code
            ),
            as_json=args.json,
        )
    lines = _fit_lines(outcome)
    artifacts = {
        "method": outcome.patch["method"],
        "patch": {
            "path": str(outcome.patch_path),
            "digest": outcome.patch_digest,
            "operations": len(outcome.operations),
        },
        "evaluations": outcome.result.evaluations,
        "gaps": outcome.patch["gaps"],
        "heldOut": _gate_document(outcome),
        "heldOutNote": outcome.held_out_note,
        "calibrated": outcome.calibrated,
        "bundle": str(outcome.bundle.path) if outcome.bundle is not None else None,
    }
    if outcome.calibrated:
        assert outcome.bundle is not None
        lines.append(f"calibrated: wrote {outcome.bundle.path}")
        return _print_result(
            StageResult(
                stage="calibrate", exit_code=0, _artifacts=artifacts, stdout="\n".join(lines)
            ),
            as_json=args.json,
        )
    refusal = _not_calibrated(outcome)
    return _print_result(
        StageResult(
            stage="calibrate",
            exit_code=1,
            _artifacts=artifacts,
            stdout="\n".join(lines),
            stderr=str(refusal),
            error_code=refusal.code.code,
        ),
        as_json=args.json,
    )


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from pathlib import Path

    if args.fit is not None:
        return _cmd_fit(args)

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
