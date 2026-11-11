# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 6, dwell times: the time a subject spends in a state, and how it scales.

**Definition.** Dwell is the time a subject spends in one state between two
consecutive journey steps: for one subject and one journey, take its transitions
in step order; when step *k* failed (so the subject is still in the same state
after it), ``dwell = t_ms(step k+1) - t_ms(step k)``. Every such pair is one
dwell observation, measured from ``Transition.t_ms`` alone. A subject whose step
succeeded leaves the state, and its next step (if any) starts a dwell in another
state, so it is not paired.

With no payload condition every signal arrival is a trigger, so a dwell is one
inter-arrival gap of the signal's burst: ``max(round(Exponential(mean)), 1)``
milliseconds (the kernel quantises to the nearest whole ms, half to even) with ``mean = ArrivalModel.gap_mean_ms``. Whether a subject's step
succeeded is drawn on its own stream and is independent of the gap, so pairing
only after failures does not bias the sample. The sample is made through the
real chain: ``emit_signals`` (seed tree -> exponential -> quantisation), then
``run_journeys`` (condition-free trigger, ``successProbability`` 0.1). Payload
fields are switched off for speed: they are drawn after the gap on the same
stream, so no gap changes.

**Not-rejected criteria**, one Benjamini-Yekutieli battery (the KS and
Anderson-Darling statistics of a pair share samples) at ``q = 0.01``:

* two independent seeds at the base gap mean: two-sample KS and k-sample
  Anderson-Darling;
* the gap mean scaled by ``c = 2``: that sample divided by 2 against the base
  sample, the same two tests. The sample is in whole milliseconds, so dividing
  by 2 puts it on the half-millisecond grid; against a mean of 300 000 ms
  that is a CDF offset of order 1e-6, far under the KS statistic's noise
  (about 1e-2).

Window truncation (an occurrence past the end of the 92-day window is dropped)
touches a burst only when it starts within a few million ms of the end, about
one burst in 10 000, and affects both compared samples alike.

**Power**: the unscaled comparison at ``c = 1.1`` (gap mean 330 000 ms against
300 000 ms, one sample per repetition, each against the base sample) must be
rejected in at least 90% of repetitions. "Rejected" is the DESIGN's power
criterion: the smallest of the pair's KS and Anderson-Darling p-values is at or
below the battery's rank-one critical value ``q / (m * c(m))``, ``m = 4``. At
about 9 000 gaps per sample the Anderson-Darling test alone rejects a 10% scale
shift essentially always (a simulation of the same two-sample setting gave a
rejection rate of 100% at 8 000 per sample at ``alpha = 0.01``); nightly samples
are three times that size and the repetitions are 20, not 3.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import warnings  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Dict, List, Tuple  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from scipy import stats  # noqa: E402

import flux_engine.runtime.signals as signals_module  # noqa: E402
from flux_engine.runtime import emit_signals  # noqa: E402
from flux_engine.runtime.signals import DEFAULT_ARRIVAL_MODEL, ArrivalModel  # noqa: E402
from flux_engine.world import TraceLog  # noqa: E402
from tests.accuracy.gof.kernel_battery import rank_one_threshold  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.journeys import (  # noqa: E402
    SHIPPED_PERSONA,
    SyntheticStore,
    base,
    fresh_tree,
    population_spec,
    reaction,
    requires_example,
    run,
    with_reactions,
)

pytestmark = [pytest.mark.integration, requires_example]

FAMILY = "journeys"
#: Poisson mean of the occurrences beyond the first. Larger than the default
#: (1.5) so that each emitter yields about three dwell observations.
EXTRA_OCCURRENCES = Decimal("4")
BASE_GAP_MS = DEFAULT_ARRIVAL_MODEL.gap_mean_ms
SUCCESS_PROBABILITY = "0.1"
BATTERY_TESTS = 4  # two pairs, each KS + Anderson-Darling
CRITERION = (
    "no two-sample KS or k-sample Anderson-Darling statistic rejected by Benjamini-Yekutieli "
    "at q=0.01"
)


def dwell_sample(seed: int, scale: float, emitters: int) -> np.ndarray:
    """Dwell times (ms) of *emitters* subjects, with the gap mean at ``BASE_GAP_MS * scale``."""
    journey = with_reactions(
        {SHIPPED_PERSONA: reaction(SHIPPED_PERSONA, SUCCESS_PROBABILITY, next_state="active")}
    )
    store = SyntheticStore(emitters)
    arrival = ArrivalModel(
        emitter_share=Decimal(1),
        extra_occurrences_mean=EXTRA_OCCURRENCES,
        gap_mean_ms=BASE_GAP_MS * Decimal(str(scale)),
    )
    staged = base()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(signals_module, "payload_families", lambda signal_id, pack: {})
        events = emit_signals(
            cast=staged.cast,
            world=population_spec({"engagement": 0}),
            store=store,  # type: ignore[arg-type]
            clock=staged.clock,
            seed_tree=fresh_tree(seed),
            trace=TraceLog(),
            pack=staged.pack,
            arrival=arrival,
        )
    moved = run(journey, events, store, population_spec({}), seed)
    by_subject: Dict[int, List] = {}
    for item in moved:
        by_subject.setdefault(item.ordinal, []).append(item)
    dwell: List[int] = []
    for steps in by_subject.values():
        steps.sort(key=lambda item: item.step)
        for earlier, later in zip(steps, steps[1:], strict=False):
            if not earlier.success:
                dwell.append(later.t_ms - earlier.t_ms)
    return np.asarray(dwell, dtype=float)


def compare(a: np.ndarray, b: np.ndarray) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Two-sample KS and k-sample Anderson-Darling: ``((D, p), (A, p))``.

    The Anderson-Darling p-value is interpolated from tables and clipped to
    [0.001, 0.25]; the clip only overstates it, so it is a conservative bound.
    """
    ks = stats.ks_2samp(a, b)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ad = stats.anderson_ksamp([a, b], variant="midrank")
    return (float(ks.statistic), float(ks.pvalue)), (
        float(ad.statistic),
        min(1.0, max(0.0, float(ad.pvalue))),
    )


@pytest.fixture(scope="module")
def emitters(tier: Tier) -> int:
    return int(tier.pick(pr=3000, nightly=10000))


@pytest.fixture(scope="module")
def samples(tier: Tier, emitters: int) -> Dict[str, np.ndarray]:
    return {
        "A": dwell_sample(tier.seed("journeys", "dwell", "A"), 1.0, emitters),
        "B": dwell_sample(tier.seed("journeys", "dwell", "B"), 1.0, emitters),
        "C": dwell_sample(tier.seed("journeys", "dwell", "scaled-2"), 2.0, emitters),
    }


@pytest.fixture(scope="module")
def battery(samples: Dict[str, np.ndarray]) -> Battery:
    battery = Battery(name="journey dwell", q=Q)
    pairs = {"seeds": (samples["A"], samples["B"]), "scaled-2": (samples["C"] / 2.0, samples["A"])}
    for case, (x, y) in pairs.items():
        (d, ks_p), (a, ad_p) = compare(x, y)
        label = f"{case}-pair"
        battery.add(case=case, test="ks_2samp", statistic=d, pvalue=ks_p, sample=label)
        battery.add(case=case, test="anderson_ksamp", statistic=a, pvalue=ad_p, sample=label)
    assert len(battery.entries) == BATTERY_TESTS
    return battery


def _record(
    log: ResultLog, battery: Battery, case: str, tier: Tier, samples: Dict[str, np.ndarray], n: int
) -> list:
    decisions = [d for d in battery.decisions() if d.entry.case == case]
    log.record(
        family=FAMILY,
        case=f"dwell/{case}",
        criterion=CRITERION,
        passed=not any(d.rejected for d in decisions),
        seed=tier.seed("journeys", "dwell", "A"),
        n=n,
        metrics={
            "method": battery.method,
            "means_ms": {name: float(values.mean()) for name, values in samples.items()},
            "sizes": {name: int(len(values)) for name, values in samples.items()},
            "tests": {
                d.entry.test: {
                    "statistic": d.entry.statistic,
                    "pvalue": d.entry.pvalue,
                    "adjusted": d.adjusted,
                }
                for d in decisions
            },
        },
    )
    return decisions


def test_the_sample_is_large_enough_and_on_the_declared_scale(
    samples: Dict[str, np.ndarray], emitters: int, tier: Tier, accuracy_log: ResultLog
) -> None:
    """A dwell sample that was nearly empty or mis-scaled would make every test below vacuous."""
    problems: List[str] = []
    mean = float(BASE_GAP_MS)
    for name, factor in (("A", 1.0), ("B", 1.0), ("C", 2.0)):
        values = samples[name]
        if len(values) < emitters:
            problems.append(f"{name}: only {len(values)} dwell values")
        if (values < 1).any():
            problems.append(f"{name}: a dwell below 1 ms")
        standard_error = factor * mean / np.sqrt(len(values))
        if abs(values.mean() - factor * mean) >= 5 * standard_error:
            problems.append(f"{name}: mean {values.mean():.0f} ms is off the declared scale")
    accuracy_log.record(
        family=FAMILY,
        case="dwell/sample-sanity",
        criterion="each sample has at least one dwell per emitter, all >= 1 ms, mean within 5 SE",
        passed=not problems,
        seed=tier.seed("journeys", "dwell", "A"),
        n=emitters,
        metrics={"problems": problems, "sizes": {k: int(len(v)) for k, v in samples.items()}},
    )
    assert not problems, problems


def test_two_seeds_give_the_same_dwell_distribution(
    samples: Dict[str, np.ndarray], battery: Battery, tier: Tier, accuracy_log: ResultLog
) -> None:
    decisions = _record(
        accuracy_log, battery, "seeds", tier, samples, len(samples["A"]) + len(samples["B"])
    )
    assert battery.method == "by"
    assert not any(d.rejected for d in decisions), battery.summary()


def test_scaling_the_gap_mean_by_two_scales_the_dwell_by_two(
    samples: Dict[str, np.ndarray], battery: Battery, tier: Tier, accuracy_log: ResultLog
) -> None:
    decisions = _record(
        accuracy_log, battery, "scaled-2", tier, samples, len(samples["C"]) + len(samples["A"])
    )
    assert not any(d.rejected for d in decisions), battery.summary()
    assert not battery.rejected(), battery.summary()


def test_the_unscaled_comparison_at_c_1_1_is_rejected(
    samples: Dict[str, np.ndarray],
    battery: Battery,
    emitters: int,
    tier: Tier,
    accuracy_log: ResultLog,
) -> None:
    repetitions = int(tier.pick(pr=3, nightly=20))
    threshold = rank_one_threshold(Q, BATTERY_TESTS, "by")
    smallest: List[float] = []
    for repetition in range(repetitions):
        seed = tier.seed("journeys", "dwell", "power", repetition)
        shifted = dwell_sample(seed, 1.1, emitters)
        (_d, ks_p), (_a, ad_p) = compare(shifted, samples["A"])
        smallest.append(min(ks_p, ad_p))
    caught = sum(1 for p in smallest if p <= threshold)
    rate = caught / repetitions
    accuracy_log.record(
        family=FAMILY,
        case="dwell/power-c1.1",
        criterion=(
            "the unscaled c=1.1 comparison has min(KS, AD) p at or below the battery's "
            "rank-one critical value in >= 90% of repetitions"
        ),
        passed=rate >= 0.9,
        seed=tier.seed("journeys", "dwell", "power", 0),
        n=emitters,
        metrics={
            "repetitions": repetitions,
            "caught": caught,
            "rate": rate,
            "rank_one_threshold": threshold,
            "largest_smallest_p": max(smallest),
        },
    )
    assert rate >= 0.9, (caught, repetitions, smallest, threshold)
