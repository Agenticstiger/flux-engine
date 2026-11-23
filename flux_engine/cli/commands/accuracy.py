# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim accuracy``: the outcome-accuracy gate (register, score, record)."""

from __future__ import annotations

import argparse
from typing import Any, Dict, Sequence, Tuple

from flux_engine.cli._args import _add_json_flag, _add_state_dir
from flux_engine.cli._output import _print_result
from flux_engine.cli._parser import Example

EXAMPLES: Dict[str, Tuple[Example, ...]] = {
    "accuracy": (
        Example("fluxsim accuracy register --file prediction.json", "seal a prediction first"),
    ),
    "accuracy register": (
        Example("fluxsim accuracy register --file prediction.json", "seal a prediction"),
    ),
    "accuracy score": (
        Example(
            "fluxsim accuracy score --prediction-id p-1 --readout readout.json",
            "score it once the outcome is known",
        ),
    ),
    "accuracy record": (
        Example(
            "fluxsim accuracy record --registered-at 2026-01-01T00:00:00Z --out accuracy.json",
            "publish the outcomes-only record",
        ),
    ),
}


def configure(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``fluxsim accuracy`` to *sub*, the commands of the ``fluxsim`` parser."""
    from flux_engine.cli._documents import PREDICTION_FIELDS, field_list_help, readout_fields

    accuracy_p = sub.add_parser(
        "accuracy",
        help="outcome-accuracy gate: register, score, record",
        description=(
            "Seal a prediction before its horizon, score it against an observed readout "
            "afterwards, and publish the outcomes-only accuracy record."
        ),
    )
    accuracy_sub = accuracy_p.add_subparsers(
        dest="accuracy_command", title="commands", metavar="<command>"
    )
    reg_p = accuracy_sub.add_parser(
        "register",
        help="seal a prediction before its horizon",
        description=(
            "Seal a PredictionRecord so it cannot be edited after the outcome is known. "
            "A prediction id is registered once: the same claim again writes nothing, and "
            "a different claim under that id is refused (FLX-7007)."
        ),
    )
    reg_p.add_argument(
        "--file",
        required=True,
        help=f"a PredictionRecord as JSON -- {field_list_help(PREDICTION_FIELDS)}",
    )
    _add_state_dir(reg_p)
    _add_json_flag(reg_p)
    score_p = accuracy_sub.add_parser(
        "score",
        help="score a registered prediction against a readout",
        description=(
            "Bind an ObservedReadout to a sealed prediction and record the verdict. A "
            "prediction whose record no longer matches its seal is refused (FLX-7008)."
        ),
    )
    score_p.add_argument(
        "--prediction-id", required=True, help="the id the prediction was registered under"
    )
    score_p.add_argument(
        "--readout",
        required=True,
        help=f"an ObservedReadout as JSON -- {field_list_help(readout_fields())}",
    )
    _add_state_dir(score_p)
    _add_json_flag(score_p)
    record_p = accuracy_sub.add_parser(
        "record",
        help="publish one registration's outcomes-only record",
        description=(
            "Publish the outcomes-only accuracy record for everything registered at one "
            "registration timestamp."
        ),
    )
    record_p.add_argument(
        "--registered-at",
        required=True,
        help=(
            "the registration timestamp to publish, as RFC 3339; only the "
            "predictions whose registered_at is exactly this are covered"
        ),
    )
    record_p.add_argument("--out", required=True, help="path the accuracy record is written to")
    _add_state_dir(record_p)
    _add_json_flag(record_p)

    reg_p.set_defaults(stage_name="accuracy-register")
    score_p.set_defaults(stage_name="accuracy-score")
    record_p.set_defaults(stage_name="accuracy-record")


def _require_rfc3339(value: str, *, flag: str) -> str:
    """Return *value* when it is an RFC 3339 instant, else raise ``FLX-7005``.

    An unchecked ``--registered-at`` was stamped into the published record
    verbatim, so a bare date -- or any string at all -- became the
    registration window an auditor reads. The instant is parsed here and
    the caller's own spelling is what travels, because that spelling is
    what the sealed predictions carry.
    """
    from flux_engine.accuracy.records import FLX_7005
    from flux_engine.core.clock import parse_rfc3339_ms
    from flux_engine.core.errors import FluxError

    try:
        parse_rfc3339_ms(value)
    except (FluxError, ValueError) as error:
        raise FluxError(
            FLX_7005,
            f"{flag}={value!r} is not an RFC 3339 instant with an offset "
            "(e.g. 2026-01-01T00:00:00Z)",
        ) from error
    return value


def _accuracy_document(
    path: str, *, what: str, fields: "Sequence[Any]", flag: str
) -> Dict[str, Any]:
    """Read one caller-supplied accuracy document, or raise the right code.

    Every problem the document has is reported in one message -- each
    missing field, each field of the wrong type, and the legal values of
    every closed set. What reached the terminal before this was one raw
    ``KeyError`` per run-and-crash cycle, and a ``ValueError`` that
    withheld the three values it had just checked against.

    A file that is not there is ``FLX-7006``, not ``FLX-7005``: it has no
    shape to be wrong about, and the code and the detail must not
    contradict each other in one line. The refusal names the flag that
    supplied the path, the way ``FLX-2109`` names the next command.
    """
    from flux_engine.accuracy.records import FLX_7005, FLX_7006
    from flux_engine.cli._documents import DocumentProblems, MissingDocument, read_json_object
    from flux_engine.core.errors import FluxError

    try:
        return read_json_object(path, what=what, fields=fields)
    except MissingDocument:
        raise FluxError(
            FLX_7006, f"no {what} at {path}; {flag} names the JSON document to read"
        ) from None
    except DocumentProblems as problems:
        raise FluxError(FLX_7005, str(problems)) from None


def _cmd_accuracy_register(args: argparse.Namespace) -> int:
    from flux_engine.accuracy.store import AccuracyStore, prediction_from_document
    from flux_engine.api.stages import StageResult
    from flux_engine.cli._documents import PREDICTION_FIELDS

    document = _accuracy_document(
        args.file, what="PredictionRecord", fields=PREDICTION_FIELDS, flag="--file"
    )
    prediction = prediction_from_document(document)
    registration = AccuracyStore.for_state_dir(args.state_dir).register(prediction, document)
    digest = registration.seal.value
    # Written once: the same claim registered again succeeds and writes
    # nothing, and the line says so rather than claiming a new registration.
    stdout = (
        f"registered {prediction.prediction_id} ({digest})"
        if registration.created
        else f"{prediction.prediction_id} is already registered with this seal ({digest}); "
        "nothing was written"
    )
    return _print_result(
        StageResult(
            stage="accuracy-register",
            exit_code=0,
            _artifacts={
                "predictionId": prediction.prediction_id,
                "sealDigest": digest,
                "path": str(registration.path),
                "created": registration.created,
            },
            stdout=stdout,
        ),
        as_json=args.json,
    )


def _cmd_accuracy_score(args: argparse.Namespace) -> int:
    from decimal import Decimal

    from flux_engine.accuracy.records import bind_readout, score
    from flux_engine.accuracy.store import AccuracyStore, prediction_from_document
    from flux_engine.api.accuracy import ObservedReadout
    from flux_engine.api.claims import ClaimBasis
    from flux_engine.api.stages import StageResult
    from flux_engine.cli._documents import readout_fields

    store = AccuracyStore.for_state_dir(args.state_dir)
    # Verified against its seal (FLX-7008), so an edit made after the
    # prediction was sealed is refused here rather than scored.
    document = store.read_prediction(args.prediction_id)
    if document is None:
        return _print_result(
            StageResult(
                stage="accuracy-score",
                exit_code=1,
                stderr=f"no registered prediction {args.prediction_id!r}",
            ),
            as_json=args.json,
        )
    prediction = prediction_from_document(document)
    readout_document = _accuracy_document(
        args.readout, what="ObservedReadout", fields=readout_fields(), flag="--readout"
    )
    readout = ObservedReadout(
        prediction_id=readout_document["prediction_id"],
        metric_id=readout_document["metric_id"],
        observed=Decimal(str(readout_document["observed"])),
        sample_size=int(readout_document["sample_size"]),
        basis=ClaimBasis(readout_document["basis"]),
        observed_at=readout_document["observed_at"],
    )
    bind_readout(prediction, readout)
    verdict = score(prediction, readout)
    verdict_path = store.write_verdict(verdict)
    return _print_result(
        StageResult(
            stage="accuracy-score",
            exit_code=0,
            _artifacts={
                "predictionId": verdict.prediction_id,
                "metricId": verdict.metric_id,
                "outcome": verdict.outcome.value,
                "basis": verdict.basis.value,
                "path": str(verdict_path),
            },
            stdout=f"{verdict.prediction_id}: {verdict.outcome.value}",
        ),
        as_json=args.json,
    )


def _cmd_accuracy_record(args: argparse.Namespace) -> int:
    import os

    from flux_engine.accuracy.store import AccuracyStore
    from flux_engine.api.stages import StageResult

    store = AccuracyStore.for_state_dir(args.state_dir)
    predictions_dir = store.predictions_dir
    # "Nothing registered" only when nothing is in place. Not is_dir(), which
    # follows a link: a link at accuracy or predictions goes to the store,
    # which refuses it (FLX-7009) instead of reading it as an empty store.
    if not os.path.islink(store.root) and not os.path.lexists(predictions_dir):
        return _print_result(
            StageResult(
                stage="accuracy-record",
                exit_code=1,
                stderr=f"no registered predictions under {predictions_dir}",
            ),
            as_json=args.json,
        )

    from datetime import datetime, timezone

    from flux_engine.accuracy.accuracy_record import write_accuracy_record
    from flux_engine.accuracy.records import FLX_7004
    from flux_engine.accuracy.store import prediction_from_document
    from flux_engine.api.accuracy import AccuracyOutcome, AccuracyVerdict
    from flux_engine.api.claims import ClaimBasis
    from flux_engine.core.errors import FluxError

    registered_at = _require_rfc3339(args.registered_at, flag="--registered-at")

    predictions = []
    verdicts = []
    # THE filter, now owned by the store (`documents_registered_at`).
    # Without it this command globbed every prediction ever registered,
    # stamped the caller's `--registered-at` on the result verbatim, and
    # published a record that made a false statement about which
    # predictions it covers. The store verifies every sealed record first
    # (FLX-7008), so the digests published below are the ones register
    # sealed, and the interval is read the way register read it.
    for document in store.documents_registered_at(registered_at):
        predictions.append(prediction_from_document(document))
        verdict_doc = store.read_verdict(document["prediction_id"])
        if verdict_doc is not None:
            verdicts.append(
                AccuracyVerdict(
                    prediction_id=verdict_doc["prediction_id"],
                    metric_id=verdict_doc["metric_id"],
                    outcome=AccuracyOutcome(verdict_doc["outcome"]),
                    basis=ClaimBasis(verdict_doc["basis"]),
                )
            )

    if not predictions:
        # An empty-but-stamped record is the false claim this refusal
        # exists to prevent: it would name a registration window and cover
        # nothing that was registered in it.
        raise FluxError(
            FLX_7004,
            f"no prediction under {predictions_dir} is registered at {registered_at!r}",
        )

    out_path = write_accuracy_record(
        args.out,
        registered_at=registered_at,
        targets=[p.metric_id for p in predictions],
        predictions=predictions,
        verdicts=verdicts,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return _print_result(
        StageResult(
            stage="accuracy-record",
            exit_code=0,
            _artifacts={
                "path": str(out_path),
                "registeredAt": registered_at,
                "predictions": len(predictions),
            },
            stdout=f"wrote {out_path}",
        ),
        as_json=args.json,
    )
