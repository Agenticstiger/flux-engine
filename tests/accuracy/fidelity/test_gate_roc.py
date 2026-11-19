# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 3: how often the fidelity gate is wrong, against deviations of known size.

``ship`` withholds with ``FLX-8001`` when the fidelity check finds a
property outside its tolerance. This module injects deviations of stated
size into a synthetic dataset, relative to a synthetic committed state, and
records what the check decides:

* **mean** -- the population mean moved by ``f * τ`` (relative, ``τ = 0.02``);
* **total variation** -- mass mixed into the top bin until the population
  histogram is ``f * τ`` away (``τ = 0.05``);
* **quantile** -- the population median moved by ``f * τ`` (absolute,
  ``τ = 10``);
* **support** -- ``m`` off-support records (``max = 0``).

The ground truth is the *population* deviation, computed from the
distributions (``contracts.population_truth``): above ``τ`` the bundle should
be withheld, at or below it shipped. A **false accept** ships a positive, a
**false reject** withholds a negative.

Pass criteria:

* **exactness** -- in every trial, every property's verdict equals the
  declared rule applied to the sample deviation in rational arithmetic
  (``contracts.decide_independently``), with the compiler's margin
  ``τ' = τ - ε``. One disagreement is a defect;
* **operating point** (nightly sizes) -- the false-reject rate at ``f = 0``
  and the false-accept rate at ``f = 2`` each have an exact 95% upper bound
  of at most 5%;
* **separation** -- the ROC area between ``f <= 0.5`` and ``f >= 1.5``, with
  the sample deviation as the score, is at least 0.99;
* **support** -- withheld exactly when ``m > max``.

The rates at ``f = 0.9`` and ``f = 1.1`` are reported and not gated: they
are the sampling noise of the stated sizes, which is a property of ``n``
and ``τ``, not of the gate.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

from dataclasses import dataclass, field  # noqa: E402
from typing import Any, Callable, Dict, List, Tuple  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from scipy import stats  # noqa: E402

from flux_engine.concordance.boundary.slice import SliceReference, commit_state  # noqa: E402
from flux_engine.concordance.verifier import ClearStatistic  # noqa: E402
from tests.accuracy.fidelity.contracts import (  # noqa: E402
    COMMITTED_AT,
    MEAN_TOLERANCE,
    QUANTILE_TOLERANCE,
    REFERENCE,
    TV_TOLERANCE,
    Population,
    committed_state,
    compile_document,
    contract_document,
    decide_independently,
    draw_rows,
    population_truth,
)
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.stats import clopper_pearson  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402

FAMILY = "gate-roc"
FACTORS = (0.0, 0.5, 0.9, 1.1, 1.5, 2.0)
SEPARATION_AUC = 0.99
OPERATING_BOUND = 0.05

#: The top bin's share of the reference population, which a mixture moves.
_TOP_BIN_SHARE = 1.0 - float(stats.norm.cdf(400.0, REFERENCE.mean, REFERENCE.sd))


def _mean_population(factor: float) -> Population:
    return REFERENCE.with_(mean=REFERENCE.mean * (1 + factor * float(MEAN_TOLERANCE)))


def _tv_population(factor: float) -> Population:
    # Mixing weight w into U[400, 1000] -- entirely the top bin -- moves the
    # binned distribution by w * (1 - top share) in total variation.
    return REFERENCE.with_(mix=factor * float(TV_TOLERANCE) / (1.0 - _TOP_BIN_SHARE))


def _quantile_population(factor: float) -> Population:
    return REFERENCE.with_(shift=factor * float(QUANTILE_TOLERANCE))


@dataclass(frozen=True)
class Series:
    name: str
    population: Callable[[float], Population]
    kind: str
    tolerance: float
    truth_key: str


SERIES: Tuple[Series, ...] = (
    Series("mean", _mean_population, "mean", float(MEAN_TOLERANCE), "mean"),
    Series("tv", _tv_population, "marginalHistogram", float(TV_TOLERANCE), "tv"),
    Series("quantile", _quantile_population, "quantile", float(QUANTILE_TOLERANCE), "quantile"),
)


@dataclass
class Trial:
    factor: float
    truth: float
    score: float
    shipped: bool


@dataclass
class SeriesResult:
    series: Series
    trials: List[Trial] = field(default_factory=list)
    disagreements: List[str] = field(default_factory=list)


def _sizes(tier: Tier) -> Dict[str, Any]:
    return {
        "k": tier.pick(pr=4_000, nightly=10_000),
        "n": tier.pick(pr=4_000, nightly=10_000),
        "repetitions": {
            factor: tier.pick(pr=12, nightly=150 if factor in (0.0, 2.0) else 50)
            for factor in FACTORS
        },
    }


def _score(series: Series, reading: Any) -> float:
    if series.name == "mean":
        return float(reading.mean_deviation)
    if series.name == "tv":
        return float(reading.tv_deviation)
    return reading.quantile_deviation_units / float(1 << 16)


@pytest.fixture(scope="module")
def measured(tier: Tier) -> Dict[str, Any]:
    sizes = _sizes(tier)
    compiled = compile_document(contract_document(grouped=False))
    ids = {entry.declaration.kind.value: entry.property_id for entry in compiled.properties}
    results = {series.name: SeriesResult(series) for series in SERIES}
    deepest = max(sizes["repetitions"].values())
    for repetition in range(deepest):
        committed_rows = draw_rows(
            REFERENCE, sizes["k"], tier.seed("gate-roc", "committed", repetition)
        )
        committed = committed_state(committed_rows, compiled)
        for series in SERIES:
            for factor in FACTORS:
                if repetition >= sizes["repetitions"][factor]:
                    continue
                population = series.population(factor)
                synthetic = draw_rows(
                    population, sizes["n"], tier.seed("gate-roc", series.name, factor, repetition)
                )
                evaluation = ClearStatistic(
                    compiled=compiled, committed=committed, synthetic_rows=synthetic
                ).evaluate()
                verdicts = {outcome.property_id: outcome.within for outcome in evaluation.outcomes}
                reading = decide_independently(committed_rows, synthetic, compiled)
                result = results[series.name]
                for property_id, expected in reading.decisions.items():
                    if verdicts.get(property_id) != expected:
                        result.disagreements.append(
                            f"{series.name} f={factor} rep={repetition} {property_id}: "
                            f"gate={verdicts.get(property_id)} rule={expected}"
                        )
                result.trials.append(
                    Trial(
                        factor=factor,
                        truth=population_truth(population)[series.truth_key],
                        score=_score(series, reading),
                        shipped=bool(verdicts[ids[series.kind]]),
                    )
                )
    return {"sizes": sizes, "results": results, "compiled": compiled}


def _rate(trials: List[Trial], factor: float, *, shipped: bool) -> Tuple[int, int]:
    chosen = [trial for trial in trials if trial.factor == factor]
    return sum(1 for trial in chosen if trial.shipped is shipped), len(chosen)


def _auc(trials: List[Trial]) -> float:
    negatives = [trial.score for trial in trials if trial.factor <= 0.5]
    positives = [trial.score for trial in trials if trial.factor >= 1.5]
    u = stats.mannwhitneyu(positives, negatives, alternative="greater").statistic
    return float(u) / (len(positives) * len(negatives))


def _roc_points(trials: List[Trial], tolerance: float) -> List[Tuple[float, float]]:
    labels = np.array([trial.truth > tolerance for trial in trials])
    scores = np.array([trial.score for trial in trials])
    points = []
    for threshold in np.quantile(scores, np.linspace(0.0, 1.0, 21)):
        flagged = scores > threshold
        tpr = float(flagged[labels].mean()) if labels.any() else 0.0
        fpr = float(flagged[~labels].mean()) if (~labels).any() else 0.0
        points.append((round(fpr, 4), round(tpr, 4)))
    return points


@pytest.mark.parametrize("name", [series.name for series in SERIES])
def test_every_verdict_is_the_declared_rule(
    name: str, measured: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    result = measured["results"][name]
    accuracy_log.record(
        family=FAMILY,
        case=f"{name}-exactness",
        criterion="every verdict equals the declared rule on the sample deviation, in rationals",
        passed=not result.disagreements,
        n=measured["sizes"]["n"],
        metrics={"trials": len(result.trials), "disagreements": result.disagreements[:10]},
    )
    assert not result.disagreements, result.disagreements[:10]


@pytest.mark.parametrize("name", [series.name for series in SERIES])
def test_operating_point_and_separation(
    name: str, measured: Dict[str, Any], tier: Tier, accuracy_log: ResultLog
) -> None:
    result = measured["results"][name]
    trials = result.trials
    false_rejects, at_zero = _rate(trials, 0.0, shipped=False)
    false_accepts, at_two = _rate(trials, 2.0, shipped=True)
    frr_high = clopper_pearson(false_rejects, at_zero)[1]
    far_high = clopper_pearson(false_accepts, at_two)[1]
    auc = _auc(trials)
    withhold = {
        str(factor): {
            "withheld": _rate(trials, factor, shipped=False)[0],
            "trials": _rate(trials, factor, shipped=False)[1],
            "truth": float(np.mean([t.truth for t in trials if t.factor == factor])),
        }
        for factor in FACTORS
    }
    operating = frr_high <= OPERATING_BOUND and far_high <= OPERATING_BOUND
    passed = auc >= SEPARATION_AUC and (operating or not tier.nightly)
    accuracy_log.record(
        family=FAMILY,
        case=f"{name}-roc",
        criterion=(
            "ROC area >= 0.99 between f <= 0.5 and f >= 1.5; nightly: FRR(f=0) and FAR(f=2) "
            "95% upper bounds <= 5%"
        ),
        passed=passed,
        n=measured["sizes"]["n"],
        metrics={
            "tolerance": result.series.tolerance,
            "committedRecords": measured["sizes"]["k"],
            "falseRejectRateAtZero": false_rejects / at_zero,
            "falseRejectUpper95": frr_high,
            "falseAcceptRateAtTwice": false_accepts / at_two,
            "falseAcceptUpper95": far_high,
            "auc": auc,
            "withholdByFactor": withhold,
            "roc": _roc_points(trials, result.series.tolerance),
        },
    )
    assert auc >= SEPARATION_AUC, auc
    if tier.nightly:
        assert frr_high <= OPERATING_BOUND, (false_rejects, at_zero)
        assert far_high <= OPERATING_BOUND, (false_accepts, at_two)


def test_support_is_withheld_exactly_when_strangers_exceed_the_maximum(
    tier: Tier, accuracy_log: ResultLog
) -> None:
    compiled = compile_document(contract_document(grouped=False))
    support_id = next(
        entry.property_id
        for entry in compiled.properties
        if entry.declaration.kind.value == "support"
    )
    maximum = next(
        entry.max_non_members for entry in compiled.properties if entry.property_id == support_id
    )
    n = tier.pick(pr=2_000, nightly=10_000)
    outcomes = {}
    for strangers in (0, 1, 3):
        committed_rows = draw_rows(
            REFERENCE, n, tier.seed("gate-roc", "support-committed", strangers)
        )
        synthetic = draw_rows(
            REFERENCE.with_(strangers=strangers), n, tier.seed("gate-roc", "support", strangers)
        )
        evaluation = ClearStatistic(
            compiled=compiled,
            committed=committed_state(committed_rows, compiled),
            synthetic_rows=synthetic,
        ).evaluate()
        within = next(o.within for o in evaluation.outcomes if o.property_id == support_id)
        outcomes[strangers] = within
    expected = {strangers: strangers <= maximum for strangers in outcomes}
    accuracy_log.record(
        family=FAMILY,
        case="support-exactness",
        criterion="withheld exactly when the off-support record count exceeds the declared max",
        passed=outcomes == expected,
        n=n,
        metrics={"within": {str(k): v for k, v in outcomes.items()}, "max": maximum},
    )
    assert outcomes == expected


def test_the_stand_in_committed_state_decides_as_a_real_commitment(tier: Tier) -> None:
    """``contracts.committed_state`` skips C1 and C2; the verdicts must not notice."""
    compiled = compile_document(contract_document(grouped=False, records_max=5_000))
    committed_rows = draw_rows(REFERENCE, 2_000, tier.seed("gate-roc", "real-commit"))
    synthetic = draw_rows(_mean_population(1.5), 2_000, tier.seed("gate-roc", "real-commit-synth"))
    real = commit_state(
        rows=committed_rows,
        compiled=compiled,
        ordinal=1,
        committed_at=COMMITTED_AT,
        blinding=bytes(range(32)),
        slice_reference=SliceReference(
            uri="file:///synthetic.ndjson", first_line=1, last_line=2_000
        ),
    )
    stand_in = committed_state(committed_rows, compiled)
    assert real.statistic.to_document() == stand_in.statistic.to_document()

    def verdicts(state: Any) -> Dict[str, bool]:
        evaluation = ClearStatistic(
            compiled=compiled, committed=state, synthetic_rows=synthetic
        ).evaluate()
        return {outcome.property_id: outcome.within for outcome in evaluation.outcomes}

    assert verdicts(real) == verdicts(stand_in)
