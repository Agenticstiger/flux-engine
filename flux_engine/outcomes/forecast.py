# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Forecasts for the next state, sealed in the prediction registry (ADR 0029 sections 3 and 4).

``fluxsim forecast make`` runs the Simulation's ``spec.sweep`` as replicates, reads each forecast
item's value off every replicate, and registers the median and its interval in the outcome-accuracy
store, sealed and write-once, before the next state is seen.

* **Replicates.** Run ``r``, for ``r = 1..R``, is the sweep's run ``r - 1``, seeded as the sweep
  seeds its runs (:func:`flux_engine.outcomes.responses.replicate_seed`) and takes, for each family and
  segment, the effect ``L + (U - L)(2r - 1) / (2R)`` of the interval ``[L, U]`` the estimate gave
  (:func:`flux_engine.outcomes.responses.replicate_effects`, exact). Its plan records ``r``, ``R``
  and those effects, so ``fluxsim replay`` of the plan reproduces the replicate's run digest.
* **A value.** Among the subjects of the item's segment in its arm (treated: the variant names the
  family's treatment; control: any other variant of the experiment) that have a first reaction in
  the family's journey, the share whose first reaction succeeded, an exact fraction. A replicate
  with no such subject gives the item no value.
* **The forecast.** The median of the values (the mean of the two middle ones when there are an
  even number), and an interval from the value of rank ``a`` to the value of rank
  ``count + 1 - a``, with ``a`` the largest whole number not above ``count (1 - c) / 2``. Fewer
  values than give ``a`` of at least 1 refuse the item (``FLX-7140``). The arithmetic is exact; a
  value is written as a decimal in the kernel's context when it is registered.
* **Registration.** The record's id is the item's id, a dot and the first 16 hexadecimal digits of
  the pinned profile's digest; its run digest the digest of the replicates' run digests in sweep
  order; its metric ``query:<query id>/<segment>``. Beside the claim it seals ``stateDigest`` (the
  pinned profile) and ``inputDigest`` (the bundle). Registering the same forecast again changes
  nothing, and another forecast under a used id is ``FLX-7007``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.accuracy.store import AccuracyStore
from flux_engine.api.accuracy import PredictionRecord
from flux_engine.api.stages import StageResult
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.digest import digest_of_document
from flux_engine.core.errors import FluxError, register
from flux_engine.core.rng import kernel_context
from flux_engine.outcomes.responses import Replicate
from flux_engine.runtime.cast import resolve_cast, strip_range
from flux_engine.seam.resolve import declared_responses, load_bundle, profile_reference
from flux_engine.seam.responses import ForecastItem, ResponseFamily, least_sweep_runs
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages.apply import apply as run_apply
from flux_engine.stages.plan import plan_with_result

__all__ = [
    "FLX_7140",
    "FLX_7141",
    "FLX_7142",
    "ForecastOutcome",
    "ReplicateRun",
    "Summary",
    "forecast_make",
    "interval_rank",
    "item_value",
    "make_forecasts",
    "summarise",
]

FLX_7140 = register(
    "FLX-7140",
    "a forecast item has too few replicates with a value for its interval",
    "A forecast item's value in one replicate is the share of the subjects of its segment in its "
    "arm whose first reaction succeeded, and a replicate with no such subject gives none. The "
    "interval runs from the value of rank a to the value of rank count + 1 - a, with a the "
    "largest whole number not above count (1 - c) / 2, so it needs at least one rank on each "
    "side. Fewer values than that cannot give the interval at the confidence c. Nothing is "
    "registered for any item. Make the segment or the arm larger, or the sweep longer.",
)

FLX_7141 = register(
    "FLX-7141",
    "the Simulation declares no forecasts to make",
    "`fluxsim forecast make` registers the items of extensions['com.agentics.flux/1'].forecasts. "
    "The Simulation the command read declares none, so there is nothing to forecast.",
)

FLX_7142 = register(
    "FLX-7142",
    "a forecast's horizon ends before its registration",
    "--horizon-end is the instant after which the next state can be read, and --registered-at "
    "the instant the forecast is sealed at. A horizon that ends before the registration "
    "is not a forecast of anything still to come. Both are RFC 3339 instants the caller states, "
    "because the engine reads no clock.",
)


@dataclass(frozen=True)
class Summary:
    """A forecast and its interval, exact, and the number of values they rest on."""

    forecast: Fraction
    low: Fraction
    high: Fraction
    count: int
    rank: int


def interval_rank(count: int, confidence: Fraction) -> int:
    """``a``: the largest whole number not above ``count (1 - c) / 2``."""
    return int(Fraction(count) * (1 - confidence) / 2 // 1)


def summarise(values: Sequence[Fraction], confidence: Fraction, *, item: str = "") -> Summary:
    """The median of *values*, and the values of rank ``a`` and ``count + 1 - a`` around it.

    Ranks count from 1 in ascending order. The median of an even number of values is the mean of
    the two middle ones. Fewer values than give ``a`` of at least 1 are ``FLX-7140``.
    """
    ordered = sorted(values)
    count = len(ordered)
    rank = interval_rank(count, confidence)
    if rank < 1:
        raise FluxError(
            FLX_7140,
            f"forecast item {item!r}: {count} replicates have a value, and at confidence "
            f"{confidence} the interval needs a rank of at least 1",
        )
    middle = count // 2
    forecast = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return Summary(
        forecast=forecast,
        low=ordered[rank - 1],
        high=ordered[count - rank],
        count=count,
        rank=rank,
    )


def item_value(
    result: "_pipeline.PipelineResult",
    family_index: int,
    family: ResponseFamily,
    item: ForecastItem,
) -> Optional[Fraction]:
    """An item's value in one replicate, or ``None`` when no subject of its segment and arm reacted.

    A subject that has a first reaction in the family's journey is a record of the cell the item
    forecasts, as a subject the journey never reached is no record of the query; its value is the
    share of those whose first reaction succeeded.
    """
    runtime = result.responses
    assert runtime is not None  # a forecast reads a run that declared its families
    journey = strip_range(family.journey)
    first: Dict[int, Tuple[int, bool]] = {}
    for transition in result.transitions:
        if transition.journey_id != journey:
            continue
        known = first.get(transition.ordinal)
        if known is None or transition.step < known[0]:
            first[transition.ordinal] = (transition.step, transition.success)
    subjects = [
        ordinal
        for ordinal in result.population.ordinals
        if runtime.segment_of(family_index, ordinal) == item.segment
        and runtime.arm_of(family_index, ordinal) == item.arm
    ]
    reacted = [first[ordinal][1] for ordinal in subjects if ordinal in first]
    if not reacted:
        return None
    return Fraction(sum(reacted), len(reacted))


@dataclass(frozen=True)
class ReplicateRun:
    """One replicate: its run, and the value of each item in it."""

    replicate: int
    run_id: str
    run_digest: str
    input_digest: str
    values: Mapping[str, Optional[Fraction]]


@dataclass(frozen=True)
class ForecastOutcome:
    """One registered forecast."""

    prediction_id: str
    metric_id: str
    seal: str
    created: bool
    summary: Summary
    path: Path


def _decimal(value: Fraction) -> Decimal:
    with localcontext(kernel_context()):
        return Decimal(value.numerator) / Decimal(value.denominator)


def _instant(value: str, flag: str) -> int:
    from flux_engine.accuracy.records import FLX_7005

    try:
        return parse_rfc3339_ms(value)
    except (FluxError, ValueError) as error:
        raise FluxError(
            FLX_7005,
            f"{flag}={value!r} is not an RFC 3339 instant with an offset "
            "(e.g. 2026-01-01T00:00:00Z)",
        ) from error


def _replicates(
    bundle: Path,
    *,
    runs: int,
    simulation_id: Optional[str],
    state_dir: Union[str, Path, None],
    profile: Union[str, Path, None],
    zone: Optional[str],
    jobs: int,
    families: Sequence[ResponseFamily],
    items: Sequence[ForecastItem],
) -> List[ReplicateRun]:
    by_id = {family.id: (index, family) for index, family in enumerate(families)}
    made: List[ReplicateRun] = []
    for r in range(1, runs + 1):
        planned, result = plan_with_result(
            bundle,
            simulation_id=simulation_id,
            state_dir=state_dir,
            zone=zone,
            jobs=jobs,
            profile=profile,
            stage=False,
            replicate=Replicate(r=r, runs=runs),
        )
        if not planned.success or result is None:
            raise _Failed(planned)
        run_id = planned.artifacts["run_id"]
        applied = run_apply(
            bundle,
            run_id=run_id,
            simulation_id=simulation_id,
            state_dir=state_dir,
            zone=zone,
            auto_plan=False,
            precomputed=result,
            jobs=jobs,
            use_stage=False,
        )
        if not applied.success:
            raise _Failed(applied)
        values = {item.id: item_value(result, *by_id[item.response], item) for item in items}
        made.append(
            ReplicateRun(
                replicate=r,
                run_id=run_id,
                run_digest=applied.artifacts["run_digest"],
                input_digest=result.input_digest.ref.value,
                values=values,
            )
        )
    return made


class _Failed(Exception):
    """A stage a replicate ran refused: its result is the command's."""

    def __init__(self, result: StageResult) -> None:
        super().__init__(result.stderr)
        self.result = result


def make_forecasts(
    bundle: Union[str, Path],
    *,
    registered_at: str,
    horizon_end: str,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    profile: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    jobs: int = 1,
) -> Tuple[List[ForecastOutcome], List[ReplicateRun]]:
    """Run the sweep, make every item's forecast and register it, or refuse before registering any.

    Every item's summary is made before the first is registered, so an item that cannot be
    forecast (``FLX-7140``) leaves nothing registered. A stage a replicate ran that refused raises
    :class:`_Failed`; :func:`forecast_make` turns it into the command's result.
    """
    directory = Path(bundle)
    if _instant(horizon_end, "--horizon-end") < _instant(registered_at, "--registered-at"):
        raise FluxError(
            FLX_7142, f"--horizon-end {horizon_end!r} is before --registered-at {registered_at!r}"
        )
    loaded = load_bundle(directory)
    cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
    where = cast.simulation_id
    families, forecasts = declared_responses(cast.simulation, where=where)
    if forecasts is None or not forecasts.items:
        raise FluxError(FLX_7141, f"{where} declares no forecasts")
    least = least_sweep_runs(forecasts.confidence)
    sweep = cast.simulation.get("spec", {}).get("sweep")
    runs = sweep.get("runs") if isinstance(sweep, Mapping) else None
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < least:
        from flux_engine.seam.responses import FLX_3670

        raise FluxError(
            FLX_3670,
            f"{where}: spec.sweep.runs is {runs!r}; a forecast at confidence "
            f"{forecasts.declared_confidence} needs at least {least} runs",
        )
    pinned = profile_reference(cast.simulation, where=where)
    if pinned is None:
        from flux_engine.outcomes.responses import FLX_3671

        raise FluxError(FLX_3671, f"{where}: a forecast reads the profile the Simulation pins")
    state_hex = pinned.split(":", 1)[1][:16]
    made = _replicates(
        directory,
        runs=runs,
        simulation_id=simulation_id,
        state_dir=state_dir,
        profile=profile,
        zone=zone,
        jobs=jobs,
        families=families,
        items=forecasts.items,
    )
    run_digest = digest_of_document(
        [item.run_digest for item in made], subject="forecastRunDigests"
    )
    input_digest = made[0].input_digest
    by_id = {family.id: family for family in families}
    summaries: List[Tuple[ForecastItem, Summary]] = []
    for item in forecasts.items:
        values = [run.values[item.id] for run in made]
        summaries.append(
            (
                item,
                summarise(
                    [value for value in values if value is not None],
                    forecasts.confidence,
                    item=item.id,
                ),
            )
        )
    store = AccuracyStore.for_state_dir(state_dir)
    from flux_engine.accuracy.store import prediction_from_document

    # Every record is built and checked against the store before the first is written, so a
    # refusal that can be decided without writing (a used id with another claim, FLX-7007; a
    # reversed interval, FLX-7012; a record that does not verify) leaves nothing registered.
    pending: List[Tuple[PredictionRecord, Dict[str, Any], ForecastItem, Summary, str]] = []
    for item, summary in summaries:
        family = by_id[item.response]
        query = getattr(family.estimate, item.arm)
        metric = f"query:{query}/{item.segment}"
        prediction_id = f"{item.id}.{state_hex}"
        document: Dict[str, Any] = {
            "prediction_id": prediction_id,
            "run_digest": run_digest.value,
            "metric_id": metric,
            "statement": (
                f"The share of the {item.segment} subjects in the {item.arm} arm of "
                f"{family.experiment} whose first reaction in {family.journey} succeeds is "
                f"{_decimal(summary.forecast)}, as forecast by {item.id} from the profile "
                f"{pinned}, with an interval at confidence {forecasts.declared_confidence} "
                f"from the median of {summary.count} sweep runs."
            ),
            "predicted": str(_decimal(summary.forecast)),
            "interval_low": str(_decimal(summary.low)),
            "interval_high": str(_decimal(summary.high)),
            "horizon_end": horizon_end,
            "registered_at": registered_at,
            "stateDigest": pinned,
            "inputDigest": input_digest,
        }
        prediction: PredictionRecord = prediction_from_document(document)
        store.check_registrable(prediction, document)
        pending.append((prediction, document, item, summary, metric))
    registered: List[ForecastOutcome] = []
    try:
        for prediction, document, _item, summary, metric in pending:
            registration = store.register(prediction, document)
            registered.append(
                ForecastOutcome(
                    prediction_id=prediction.prediction_id,
                    metric_id=metric,
                    seal=registration.seal.value,
                    created=registration.created,
                    summary=summary,
                    path=registration.path,
                )
            )
    except OSError as error:
        # A write that fails part way cannot be undone: a sealed record is never removed.
        done = [item.prediction_id for item in registered if item.created]
        raise OSError(
            error.errno,
            f"{error.strerror or error}; the forecasts registered before it failed, which stay "
            f"registered, are {', '.join(done) if done else 'none'}; run the command again to "
            "register the rest",
        ) from error
    return registered, made


def forecast_make(
    bundle: Union[str, Path],
    *,
    registered_at: str,
    horizon_end: str,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    profile: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    jobs: int = 1,
) -> StageResult:
    """``fluxsim forecast make``: :func:`make_forecasts` as a stage result."""
    try:
        registered, made = make_forecasts(
            bundle,
            registered_at=registered_at,
            horizon_end=horizon_end,
            simulation_id=simulation_id,
            state_dir=state_dir,
            profile=profile,
            zone=zone,
            jobs=jobs,
        )
    except _Failed as failed:
        return failed.result
    except FluxError as error:
        return StageResult(
            stage="forecast-make",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )
    lines = [
        f"{'registered' if item.created else 'already registered'} {item.prediction_id} "
        f"({item.seal}): {_decimal(item.summary.forecast)} "
        f"[{_decimal(item.summary.low)}, {_decimal(item.summary.high)}]"
        for item in registered
    ]
    return StageResult(
        stage="forecast-make",
        exit_code=0,
        _artifacts={
            "replicates": len(made),
            "forecasts": [
                {
                    "predictionId": item.prediction_id,
                    "metricId": item.metric_id,
                    "sealDigest": item.seal,
                    "created": item.created,
                    "predicted": str(_decimal(item.summary.forecast)),
                    "intervalLow": str(_decimal(item.summary.low)),
                    "intervalHigh": str(_decimal(item.summary.high)),
                    "values": item.summary.count,
                    "path": str(item.path),
                }
                for item in registered
            ],
            "runs": [
                {"replicate": run.replicate, "runId": run.run_id, "runDigest": run.run_digest}
                for run in made
            ],
        },
        stdout="\n".join(lines),
    )
