# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Scoring every forecast against the next state (ADR 0029 section 5).

``fluxsim forecast score`` reads the next state's profile and gives a verdict to every forecast
registered for the bundle's ``inputDigest``, none skipped:

* **The observation** is the next state's cell for the forecast's query and segment: its ``true``
  count over its ``k``, with ``k`` as the sample size. A withheld cell, or a ``k`` of zero, leaves
  the forecast not scored, and its verdict says why.
* **Binding.** Every observation goes through :func:`flux_engine.accuracy.records.bind_readout`,
  so one dated before the registration is ``FLX-7001``. Every verdict is made before the first is
  written, so a refusal leaves the store as it was. The basis is the forecast item's ``basis``.
* **The scores.** Beside the record's containment verdict, two scores: the absolute error, and
  the interval score of Gneiting and Raftery (2007) at ``alpha = 1 - c``, ``c`` being the
  forecasts' confidence. For the interval ``[l, u]`` and the observation ``x``,
  ``u - l``, plus ``(2 / alpha)(l - x)`` when ``x < l``, plus ``(2 / alpha)(x - u)`` when
  ``x > u``. Lower is better. The formula is the one of scoringrules' ``interval_score``; it is
  evaluated over exact fractions of the recorded decimals and written as a decimal in the
  kernel's context.
* **Grades.** The pinned profile and the next one are each graded under ADR 0020 rule 2
  (:func:`flux_engine.evidence.grades.grade_profile`), and the verdict states both.

The verdict document gains optional members (``observed``, ``sampleSize``, ``observedAt``,
``nextStateDigest``, ``absoluteError``, ``intervalScore``, ``grades`` and, for a forecast not
scored, ``reason``). A verdict written before them reads as it did, and ``accuracy-record/1`` is
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from flux_engine.accuracy.records import bind_readout, score, seal_prediction
from flux_engine.accuracy.store import AccuracyStore, prediction_from_document
from flux_engine.api.accuracy import (
    AccuracyOutcome,
    AccuracyVerdict,
    ObservedReadout,
    PredictionRecord,
)
from flux_engine.api.calibration import Scorecard
from flux_engine.api.claims import ClaimBasis
from flux_engine.api.stages import StageResult
from flux_engine.core import kernels as _kernels
from flux_engine.core.errors import FluxError, register
from flux_engine.evidence.grades import grade_profile
from flux_engine.expr.types import nfc
from flux_engine.outcomes.drift import FLX_7146, DriftReport, compare_states, read_drift_rule
from flux_engine.outcomes.forecast import _decimal, _instant
from flux_engine.outcomes.responses import FLX_3671, decimal_text, query_cell
from flux_engine.runtime.cast import resolve_cast
from flux_engine.seam.profile import PinnedProfile, load_profile, require_profile
from flux_engine.seam.resolve import declared_responses, load_bundle, profile_reference
from flux_engine.seam.responses import DEFAULT_BASIS, Forecasts
from flux_engine.stages import pipeline as _pipeline

__all__ = [
    "FLX_7143",
    "FLX_7144",
    "ForecastScore",
    "ScoreReport",
    "absolute_error",
    "forecast_score",
    "interval_score",
    "score_forecasts",
]

FLX_7143 = register(
    "FLX-7143",
    "the next state is the state the forecasts were made on",
    "`fluxsim forecast score --next` names the profile of the state after the one the bundle "
    "pins. A forecast scored against the state it was made on would be scored against what it "
    "already read, so a next state whose digest is the pinned profile's digest is refused, "
    "and nothing is scored or written. Pass the later release of the profile.",
)

FLX_7144 = register(
    "FLX-7144",
    "no forecast is registered for the bundle",
    "`fluxsim forecast score` gives a verdict to every forecast registered for the bundle's "
    "inputDigest in the outcome-accuracy store under --state-dir. The store holds none, or the "
    "Simulation declares no forecasts: run `fluxsim forecast make` on this bundle first, with "
    "the same --state-dir, and score it once the next state exists.",
)

#: ``plan.json``-style names for the two states a verdict grades.
_PINNED = "pinned"
_NEXT = "next"


def _as_fraction(value: Decimal) -> Fraction:
    return Fraction(value)


def absolute_error(observed: Decimal, forecast: Decimal) -> Decimal:
    """``|observed - forecast|``, evaluated exactly and written in the kernel's context."""
    return _decimal(abs(_as_fraction(observed) - _as_fraction(forecast)))


def interval_score(low: Decimal, high: Decimal, observed: Decimal, alpha: Fraction) -> Decimal:
    """The interval score of ``[low, high]`` for *observed* at level *alpha* (lower is better).

    ``high - low``, plus ``(2 / alpha)(low - observed)`` when the observation falls below the
    interval, plus ``(2 / alpha)(observed - high)`` when it falls above. *alpha* is ``1 - c`` for
    the confidence ``c`` of the interval, in ``(0, 1)``.
    """
    if not 0 < alpha < 1:
        raise ValueError(f"alpha is in (0, 1), got {alpha}")
    lower, upper, x = _as_fraction(low), _as_fraction(high), _as_fraction(observed)
    total = upper - lower
    if x < lower:
        total += 2 / alpha * (lower - x)
    elif x > upper:
        total += 2 / alpha * (x - upper)
    return _decimal(total)


@dataclass(frozen=True)
class ForecastScore:
    """The verdict one forecast took, as it was written."""

    prediction_id: str
    metric_id: str
    outcome: AccuracyOutcome
    basis: ClaimBasis
    observed: Optional[Decimal]
    sample_size: Optional[int]
    absolute_error: Optional[Decimal]
    interval_score: Optional[Decimal]
    reason: str
    document: Mapping[str, Any]
    path: Optional[Path] = None


@dataclass(frozen=True)
class ScoreReport:
    """Every verdict of one pass, with the digests and grades of the two states."""

    scores: Tuple[ForecastScore, ...]
    state_digest: str
    next_state_digest: str
    grades: Mapping[str, str]
    input_digest: str
    #: The comparison of the two states, or ``None`` when no Playback is named for drift.
    drift: Optional[DriftReport] = None


def _split_metric(metric_id: str) -> Optional[Tuple[str, str]]:
    """``(query id, segment key)`` of a forecast's metric ``query:<query id>/<segment>``."""
    if not metric_id.startswith("query:") or "/" not in metric_id:
        return None
    query, _, level = metric_id[len("query:") :].partition("/")
    return query, level


def _item_basis(forecasts: Forecasts, prediction_id: str) -> ClaimBasis:
    item_id = prediction_id.rpartition(".")[0]
    for item in forecasts.items:
        if item.id == item_id:
            return ClaimBasis(item.basis)
    return ClaimBasis(DEFAULT_BASIS)


def _verdict(
    prediction: PredictionRecord,
    observed_at: str,
    basis: ClaimBasis,
    cell: Optional[Tuple[int, int]],
    reason: str,
    alpha: Fraction,
) -> Tuple[AccuracyVerdict, Dict[str, Any], Optional[ObservedReadout]]:
    """The verdict of one forecast, and the optional members its document carries."""
    members: Dict[str, Any] = {"observedAt": observed_at}
    if cell is None:
        # Nothing was observed, but a date before the registration is refused all the same.
        probe = ObservedReadout(
            prediction_id=prediction.prediction_id,
            metric_id=prediction.metric_id,
            observed=Decimal(0),
            sample_size=0,
            basis=basis,
            observed_at=observed_at,
        )
        bind_readout(prediction, probe)
        verdict = AccuracyVerdict(
            prediction_id=prediction.prediction_id,
            metric_id=prediction.metric_id,
            outcome=AccuracyOutcome.NOT_SCORED,
            basis=basis,
            scorecard=Scorecard(),
            evidence=(seal_prediction(prediction),),
        )
        members["reason"] = reason
        return verdict, members, None
    records, matches = cell
    observed = _decimal(Fraction(matches, records))
    readout = ObservedReadout(
        prediction_id=prediction.prediction_id,
        metric_id=prediction.metric_id,
        observed=observed,
        sample_size=records,
        basis=basis,
        observed_at=observed_at,
    )
    verdict = score(prediction, readout)
    members["observed"] = decimal_text(observed)
    members["sampleSize"] = records
    if prediction.interval_low is not None and prediction.interval_high is not None:
        members["absoluteError"] = decimal_text(absolute_error(observed, prediction.predicted))
        members["intervalScore"] = decimal_text(
            interval_score(prediction.interval_low, prediction.interval_high, observed, alpha)
        )
    return verdict, members, readout


def _read_states(
    profile: Union[str, Path, None],
    next_profile: Union[str, Path],
    where: str,
    pinned: str,
) -> Tuple[PinnedProfile, PinnedProfile]:
    state = require_profile(pinned, Path(profile) if profile is not None else None, where=where)
    assert state is not None  # a pin was given, so a profile or a refusal came back
    following = load_profile(Path(next_profile))
    if following.digest.value == state.digest.value:
        raise FluxError(
            FLX_7143,
            f"{where}: {Path(next_profile).name} has the digest of the pinned profile, "
            f"{state.digest.value}",
        )
    return state, following


def score_forecasts(
    bundle: Union[str, Path],
    *,
    profile: Union[str, Path, None],
    next_profile: Union[str, Path],
    observed_at: str,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
) -> ScoreReport:
    """Give every forecast registered for the bundle a verdict, and write them all.

    The pinned state's profile (*profile*, checked against the pin) and the next state's
    (*next_profile*) are read first; every verdict is made before the first is written, so a
    refusal (an observation before a registration, ``FLX-7001``) leaves the store as it was.
    """
    directory = Path(bundle)
    _instant(observed_at, "--observed-at")
    loaded = load_bundle(directory)
    cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
    where = cast.simulation_id
    families, forecasts = declared_responses(cast.simulation, where=where)
    if forecasts is None or not forecasts.items:
        raise FluxError(FLX_7144, f"{where} declares no forecasts")
    pinned = profile_reference(cast.simulation, where=where)
    if pinned is None:
        raise FluxError(FLX_3671, f"{where}: a forecast reads the profile the Simulation pins")
    state, following = _read_states(profile, next_profile, where, pinned)
    # Forecasts exist only under kernel 0.2.0 (kernel 0.1.0 refuses `forecasts`, FLX-3105), so the
    # inputDigest `forecast make` registered them under is kernel 0.2.0's, named here and not read
    # from the default a new plan takes.
    input_digest = _pipeline.input_digest_of(loaded, kernel=_kernels.KERNEL_V2).ref.value
    store = AccuracyStore.for_state_dir(state_dir)
    registered = [
        document for document in store.documents() if document.get("inputDigest") == input_digest
    ]
    if not registered:
        raise FluxError(FLX_7144, f"the store under {state_dir or '.flux'} holds none for {where}")
    queries = {nfc(query.name): query for query in following.profile.queries}
    alpha = 1 - forecasts.confidence
    grades = {
        _PINNED: grade_profile(state.document, pinned=True).pattern.value,
        _NEXT: grade_profile(following.document, pinned=True).pattern.value,
    }
    rule = read_drift_rule(loaded.flux_documents, forecasts.playback, where=where)
    drift = (
        compare_states(state.profile, following.profile, families, rule)
        if rule is not None
        else None
    )
    made: List[Tuple[PredictionRecord, AccuracyVerdict, Dict[str, Any], Optional[ObservedReadout]]]
    made = []
    for document in registered:
        prediction = prediction_from_document(document)
        split = _split_metric(prediction.metric_id)
        cell: Optional[Tuple[int, int]] = None
        reason = f"the metric {prediction.metric_id!r} names no query cell"
        if split is not None:
            cell, reason = query_cell(queries, *split)
        verdict, members, readout = _verdict(
            prediction,
            observed_at,
            _item_basis(forecasts, prediction.prediction_id),
            cell,
            reason,
            alpha,
        )
        members["nextStateDigest"] = following.digest.value
        members["grades"] = dict(grades)
        if drift is not None:
            members["drift"] = drift.as_member()
        made.append((prediction, verdict, members, readout))
    return ScoreReport(
        scores=_write(store, made),
        state_digest=state.digest.value,
        next_state_digest=following.digest.value,
        grades=grades,
        input_digest=input_digest,
        drift=drift,
    )


def _write(
    store: AccuracyStore,
    made: List[Tuple[PredictionRecord, AccuracyVerdict, Dict[str, Any], Optional[ObservedReadout]]],
) -> Tuple[ForecastScore, ...]:
    written: List[ForecastScore] = []
    for _prediction, verdict, members, readout in made:
        path = store.write_verdict(verdict, members)
        written.append(
            ForecastScore(
                prediction_id=verdict.prediction_id,
                metric_id=verdict.metric_id,
                outcome=verdict.outcome,
                basis=verdict.basis,
                observed=readout.observed if readout is not None else None,
                sample_size=readout.sample_size if readout is not None else None,
                absolute_error=_decimal_member(members, "absoluteError"),
                interval_score=_decimal_member(members, "intervalScore"),
                reason=str(members.get("reason", "")),
                document=members,
                path=path,
            )
        )
    return tuple(written)


def _decimal_member(members: Mapping[str, Any], key: str) -> Optional[Decimal]:
    return Decimal(members[key]) if key in members else None


def _row(item: ForecastScore) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "predictionId": item.prediction_id,
        "metricId": item.metric_id,
        "outcome": item.outcome.value,
        "basis": item.basis.value,
        "path": str(item.path),
    }
    for key in ("observed", "sampleSize", "absoluteError", "intervalScore", "reason", "drift"):
        if key in item.document:
            row[key] = item.document[key]
    return row


def _line(item: ForecastScore) -> str:
    if item.observed is None:
        return f"{item.prediction_id}: {item.outcome.value} ({item.reason})"
    return (
        f"{item.prediction_id}: {item.outcome.value}, observed {decimal_text(item.observed)} "
        f"of {item.sample_size}, absolute error {item.document.get('absoluteError')}, "
        f"interval score {item.document.get('intervalScore')}"
    )


def forecast_score(
    bundle: Union[str, Path],
    *,
    profile: Union[str, Path, None],
    next_profile: Union[str, Path],
    observed_at: str,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
) -> StageResult:
    """``fluxsim forecast score``: :func:`score_forecasts` as a stage result."""
    try:
        report = score_forecasts(
            bundle,
            profile=profile,
            next_profile=next_profile,
            observed_at=observed_at,
            simulation_id=simulation_id,
            state_dir=state_dir,
        )
    except FluxError as error:
        return StageResult(
            stage="forecast-score",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )
    breached = report.drift is not None and report.drift.stale
    fails = breached and report.drift is not None and report.drift.rule.on_breach == "fail"
    lines = [_line(item) for item in report.scores]
    lines.extend(_drift_lines(report.drift))
    return StageResult(
        stage="forecast-score",
        exit_code=1 if fails else 0,
        _artifacts={
            "stateDigest": report.state_digest,
            "nextStateDigest": report.next_state_digest,
            "inputDigest": report.input_digest,
            "grades": dict(report.grades),
            "scored": sum(1 for item in report.scores if item.observed is not None),
            "verdicts": [_row(item) for item in report.scores],
            "drift": report.drift.as_document() if report.drift is not None else None,
        },
        stdout="\n".join(lines),
        stderr=_breach(report.drift) if fails else "",
        error_code=FLX_7146.code if fails else None,
    )


def _drift_lines(drift: Optional[DriftReport]) -> List[str]:
    """What the pass says of drift: the verdict on the calibration and each statistic left out."""
    if drift is None:
        return ["drift: not reported, no Playback is named by forecasts.playback"]
    if drift.largest is None:
        head = f"drift ({drift.rule.metric}): no statistic is held by both states"
    else:
        name, value = drift.largest
        head = (
            f"drift ({drift.rule.metric}, threshold {decimal_text(drift.rule.threshold)}): "
            f"largest {decimal_text(value)} at {name}; the calibration is "
            f"{'stale' if drift.stale else 'not stale'}"
        )
    return [head, *(f"drift: left out {name}: {reason}" for name, reason in drift.left_out)]


def _breach(drift: Optional[DriftReport]) -> str:
    assert drift is not None and drift.largest is not None
    name, value = drift.largest
    return (
        f"the calibration on the pinned state is stale: {drift.rule.metric} is "
        f"{decimal_text(value)} at {name}, above the threshold {decimal_text(drift.rule.threshold)} "
        f"of the Playback {drift.rule.playback!r}, whose onBreach is fail. Every verdict was written"
    )
