# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The rows behind ``EVIDENCE.md``'s "Forecast Accuracy" section (ADR 0029 section 8).

``fluxsim ship`` reads the outcome-accuracy store under its state directory. When the store holds
at least one forecast registered for the run's ``inputDigest``, scored or not, the run gains this
section and ``forecast-accuracy.json``, which holds the same rows. A run with no such forecast gains
neither, so its report and its pack are what they were.

* **Every forecast, ordered by id.** Each row names the forecast's metric, the digest of the state it
  was made on, its registration time, the forecast and its interval, and, when a verdict exists, the
  observation, inside or outside, the interval score, the absolute error and the drift. A forecast
  with no verdict is ``pending``; one whose verdict could not score it is ``not scored`` with the
  reason. A miss is listed as a hit is, and nothing filters either.
* **Same-state counts.** For each state digest listed, the number of forecasts the store holds on that
  state across all bundles, so a forecast made from another bundle on the same state is in sight.
* **Totals.** Forecasts scored, inside their interval, the measured coverage (inside over scored), and
  the Playback's declared ``scorecard.intervalCoverage``.
* **Grades.** The grade of each profile the forecasts read, the pinned state's and each next state's
  (ADR 0020 rule 2). A verdict states both of its states' grades, and the run's own grade stands for
  the state it pins.

This module reads documents and holds no runtime: it is the evidence layer's reading of the store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Tuple

from flux_engine.accuracy.store import AccuracyStore
from flux_engine.core.grades import Pattern, weakest
from flux_engine.core.rng import kernel_context

__all__ = [
    "FORECAST_ACCURACY_FILE",
    "ForecastAccuracy",
    "ForecastRow",
    "build_forecast_accuracy",
    "forecast_accuracy_bytes",
    "forecast_accuracy_document",
]

#: The file in the run directory that holds the rows, and that is packed with the run.
FORECAST_ACCURACY_FILE = "forecast-accuracy.json"

PENDING = "pending"
SCORED = "scored"
NOT_SCORED = "not-scored"

_INSIDE = "within-interval"
_OUTSIDE = "outside-interval"


@dataclass(frozen=True)
class ForecastRow:
    """One registered forecast, with its verdict when it has one."""

    prediction_id: str
    metric: str
    state_digest: Optional[str]
    registered_at: str
    forecast: str
    interval_low: Optional[str]
    interval_high: Optional[str]
    status: str
    observed: Optional[str] = None
    sample_size: Optional[int] = None
    inside: Optional[bool] = None
    interval_score: Optional[str] = None
    absolute_error: Optional[str] = None
    drift: Optional[Mapping[str, Any]] = None
    reason: str = ""
    next_state_digest: Optional[str] = None
    basis: Optional[str] = None

    def as_document(self) -> Dict[str, Any]:
        return {
            "predictionId": self.prediction_id,
            "metric": self.metric,
            "stateDigest": self.state_digest,
            "registeredAt": self.registered_at,
            "forecast": self.forecast,
            "intervalLow": self.interval_low,
            "intervalHigh": self.interval_high,
            "status": self.status,
            "observed": self.observed,
            "sampleSize": self.sample_size,
            "inside": self.inside,
            "intervalScore": self.interval_score,
            "absoluteError": self.absolute_error,
            "drift": dict(self.drift) if self.drift is not None else None,
            "reason": self.reason,
            "nextStateDigest": self.next_state_digest,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class ForecastAccuracy:
    """Every forecast registered for one run's ``inputDigest``, and what the store says about them."""

    input_digest: str
    rows: Tuple[ForecastRow, ...]
    #: For each state digest the rows name, the forecasts the store holds on it across all bundles.
    state_counts: Mapping[str, int]
    scored: int
    inside: int
    #: ``inside / scored`` as a decimal, or ``None`` when no forecast was scored.
    coverage: Optional[str]
    #: The Playback's declared ``scorecard.intervalCoverage``, as written, or ``None``.
    declared_coverage: Optional[str]
    playback: Optional[str]
    #: The grade of each profile the forecasts read, by digest: the pinned states and the next states.
    pinned_grades: Mapping[str, Pattern]
    next_grades: Mapping[str, Pattern]

    @property
    def all_grades(self) -> Tuple[Pattern, ...]:
        return tuple(self.pinned_grades.values()) + tuple(self.next_grades.values())

    @property
    def weakest_grade(self) -> Pattern:
        """The weakest grade of every profile the forecasts read."""
        return weakest(self.all_grades)


def _decimal_text(value: Fraction) -> str:
    with localcontext(kernel_context()):
        text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _pattern(value: Any) -> Optional[Pattern]:
    try:
        return Pattern(value)
    except ValueError:
        return None


def _merge(into: Dict[str, Pattern], digest: Any, value: Any) -> None:
    """Record the grade *value* of the profile *digest*; two accounts of one profile give the weaker."""
    pattern = _pattern(value)
    if not isinstance(digest, str) or pattern is None:
        return
    into[digest] = weakest([into[digest], pattern]) if digest in into else pattern


def _row(document: Mapping[str, Any], verdict: Optional[Mapping[str, Any]]) -> ForecastRow:
    base: Dict[str, Any] = {
        "prediction_id": str(document["prediction_id"]),
        "metric": str(document["metric_id"]),
        "state_digest": document.get("stateDigest"),
        "registered_at": str(document["registered_at"]),
        "forecast": str(document["predicted"]),
        "interval_low": document.get("interval_low"),
        "interval_high": document.get("interval_high"),
    }
    if verdict is None:
        return ForecastRow(status=PENDING, **base)
    outcome = verdict.get("outcome")
    scored = outcome in (_INSIDE, _OUTSIDE)
    return ForecastRow(
        status=SCORED if scored else NOT_SCORED,
        observed=verdict.get("observed") if scored else None,
        sample_size=verdict.get("sampleSize") if scored else None,
        inside=(outcome == _INSIDE) if scored else None,
        interval_score=verdict.get("intervalScore") if scored else None,
        absolute_error=verdict.get("absoluteError") if scored else None,
        drift=verdict.get("drift"),
        reason="" if scored else str(verdict.get("reason", "")),
        next_state_digest=verdict.get("nextStateDigest"),
        basis=verdict.get("basis"),
        **base,
    )


def build_forecast_accuracy(
    store: AccuracyStore,
    input_digest: str,
    *,
    declared_coverage: Optional[str] = None,
    playback: Optional[str] = None,
    pinned: Optional[Tuple[str, Pattern]] = None,
) -> Optional[ForecastAccuracy]:
    """The forecast accuracy of the run whose bundle has *input_digest*, or ``None`` when none.

    ``None`` is a store that holds no forecast registered for *input_digest*: the run is reported as
    it was. *pinned* is the digest and grade of the profile the run itself pins, which stands for
    that state wherever no verdict states its grade. Every sealed record of the store is verified
    when it is read (``FLX-7008``, ``FLX-7009``), so a record edited after it was sealed refuses the
    ship rather than appearing in the report.
    """
    documents = store.documents()
    mine = sorted(
        (item for item in documents if item.get("inputDigest") == input_digest),
        key=lambda item: str(item["prediction_id"]),
    )
    if not mine:
        return None
    on_state: Dict[str, int] = {}
    for item in documents:
        digest = item.get("stateDigest")
        if isinstance(digest, str):
            on_state[digest] = on_state.get(digest, 0) + 1
    rows: List[ForecastRow] = []
    pinned_grades: Dict[str, Pattern] = {}
    next_grades: Dict[str, Pattern] = {}
    for item in mine:
        verdict = store.read_verdict(str(item["prediction_id"]))
        rows.append(_row(item, verdict))
        if verdict is not None:
            grades = verdict.get("grades")
            if isinstance(grades, Mapping):
                _merge(pinned_grades, item.get("stateDigest"), grades.get("pinned"))
                _merge(next_grades, verdict.get("nextStateDigest"), grades.get("next"))
    if pinned is not None:
        # The run's own grade of the profile it pins is the one the report stands behind.
        pinned_grades[pinned[0]] = pinned[1]
    scored = sum(1 for row in rows if row.status == SCORED)
    inside = sum(1 for row in rows if row.inside)
    states = sorted({row.state_digest for row in rows if row.state_digest is not None})
    return ForecastAccuracy(
        input_digest=input_digest,
        rows=tuple(rows),
        state_counts={digest: on_state.get(digest, 0) for digest in states},
        scored=scored,
        inside=inside,
        coverage=_decimal_text(Fraction(inside, scored)) if scored else None,
        declared_coverage=declared_coverage,
        playback=playback,
        pinned_grades=pinned_grades,
        next_grades=next_grades,
    )


def forecast_accuracy_document(accuracy: ForecastAccuracy) -> Dict[str, Any]:
    """The document ``forecast-accuracy.json`` holds: the rows of the section, and its totals."""
    return {
        "inputDigest": accuracy.input_digest,
        "rows": [row.as_document() for row in accuracy.rows],
        "stateCounts": dict(accuracy.state_counts),
        "totals": {
            "registered": len(accuracy.rows),
            "scored": accuracy.scored,
            "inside": accuracy.inside,
            "measuredCoverage": accuracy.coverage,
            "declaredCoverage": accuracy.declared_coverage,
            "playback": accuracy.playback,
        },
        "grades": {
            "pinned": {digest: grade.value for digest, grade in accuracy.pinned_grades.items()},
            "next": {digest: grade.value for digest, grade in accuracy.next_grades.items()},
            "weakest": accuracy.weakest_grade.value,
        },
    }


def forecast_accuracy_bytes(accuracy: ForecastAccuracy) -> bytes:
    """The bytes of ``forecast-accuracy.json``: sorted keys, two-space indent, a final newline."""
    return (
        json.dumps(forecast_accuracy_document(accuracy), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
