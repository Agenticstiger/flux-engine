# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 2 at kind level, runtime kinds: arrivals, reactions, delivery, assignment, dials.

Every case draws through the real runtime stage (its seed paths, its kernel
samplers, its dial), on synthetic populations (``tests/accuracy/journeys``),
and states its reference with scipy.

**Signal arrivals** (``emit_signals`` with the default arrival model, payload
fields off -- they are drawn after the gap on the same stream, so no
arrival changes):

* *Emitter share* under the ``engagement`` dial ``t``. The frozen share's
  emitters equal ``LRM(frozen, 0.35)`` exactly (checked deterministically, and
  at ``t = 0`` the whole population is frozen, so the emitter count is
  ``LRM(n, 0.35)``); the stochastic share's emitters are binomial at 0.35. The
  frozen share is derived here from ``largest_remainder`` over
  ``{frozen: 1-t, stochastic: t}``, not from the engine's ``partition``.
* *Occurrence count*: ``count - 1`` is Poisson(1.5) folded at the kernel's
  documented cap (chi-square, thin cells pooled). A burst whose later
  occurrences fall past the window end is cut short; a burst starts within
  ``~ 1.5 * 300 000 ms`` of the end of the 92-day (7.95e9 ms) window about once in
  17 000 times, and then loses at most a few occurrences, so the effect on a
  chi-square over ~4 000 emitters is far below its sampling noise.
* *Burst start*: ``t_0 - start`` is a discrete uniform over ``[0, window]``;
  KS and Cramer-von Mises against the continuous uniform on ``[0, window + 1)`` evaluated at the cell
  midpoint. The grid is 7.95e9 cells, so discretisation is irrelevant.
* *Gaps*: ``Exponential`` with mean ``gap_mean_ms`` (300 000 ms), rounded to
  the nearest whole ms (the kernel's quantisation is half-to-even) and floored
  at 1; KS and Cramer-von Mises against the continuous exponential. The
  rounding moves the CDF by at most ``1 / 300 000``, three orders under the
  tests' noise at these sizes.

**Journey reactions**: with no ``campaignResponse`` dial each reaction is a
Bernoulli(``successProbability``); three Personas (the shipped reaction and two
at 0.05 and 0.93) in one run, binomial each.

**Campaign delivery**: the draw ``run_campaign`` makes is the explicit-opt-in
Bernoulli(``opt_in_share``) on the subject's campaign stream; the other gates are
switched off (every subject in segment, no jurisdiction list, no rate limit),
so ``deliveries ~ Binomial(n, share)``. Run at the engine's default share and at
another.

**Experiment assignment**: variant sizes equal ``LRM(n, weights)`` exactly in
every assignment. Over many assignment seeds, the variant of a *fixed ordinal*
is multinomial with the declared weights (chi-square; ``n = 100`` and weights
0.5/0.3/0.2, so the realised sizes 50/30/20 equal the weights); within one
assignment the ordinals of one variant are uniform (KS and Cramer-von Mises, ``n = 4000``). Those
tests share assignments, hence Benjamini-Yekutieli (see below).

**Temperature dial** at ``t`` in {0, 0.5, 1}: positives are exactly
``LRM(frozen, p)`` in the frozen share plus Binomial(stochastic, p); the exact
part is checked per seed and the stochastic positives, pooled across seeds, by
the binomial test.

**Independence and the battery.** The engagement-1 run yields the emitter count,
the occurrence counts, the burst starts and the gaps. They are different random
quantities (count and start come from different words of an emitter's first
stream; each gap has its own stream and is independent of the count that
decides only *how many* gaps there are), but KS and Cramer-von Mises also share
samples, and the five quantities come from one run. So the main battery
is Benjamini-Yekutieli, labelling all of them as one sample, which holds under any
dependence. The experiment battery (fixed-ordinal chi-squares and per-variant
KS/CvM, all on shared assignments) is Benjamini-Yekutieli too.

Pass criterion: no test rejected after BY adjustment at ``q = 0.01`` in either
battery, and every exact check holds. Exact checks and structural invariants
are accumulated and recorded before anything is asserted, so a defect leaves a
failing row in the artefact instead of erroring the module at setup.

**Power controls** (``test_the_battery_detects_*``): each runs the module's own
statistic against a deliberately wrong reference or biased data and requires
the smallest p-value to be at or below the battery's rank-one critical value
``q / (m * c(m))``. Defect sizes are stated per tier, and are larger at the
pull-request tier because its samples are smaller. Pull request: emitter share
+0.03, Poisson extra-occurrence mean +10%, gap mean +15%, burst window cut by
10%, reaction/delivery/dial probabilities +0.04/+0.04/+0.03, ordinal-0 variant
probabilities (0.7, 0.2, 0.1) and a shuffle skewed by 0.4. Nightly: +0.01, +5%,
+5%, 3%, +0.01 each, (0.6, 0.25, 0.15) and skew 0.2. A 2% shift in a Poisson
mean or a gap mean is *not* detectable at these sizes (a simulation at the
nightly sizes gave p of 0.5 and 0.03); the controls say 5% because that is
what the sizes can see.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import dataclasses  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Any, Dict, List, Optional, Tuple  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from scipy import stats  # noqa: E402

import flux_engine.runtime.signals as signals_module  # noqa: E402
from flux_engine.core.lrm import largest_remainder  # noqa: E402
from flux_engine.core.rng import poisson_cap  # noqa: E402
from flux_engine.core.seedtree import SeedTree, derive_root, temperature_path  # noqa: E402
from flux_engine.runtime import emit_signals  # noqa: E402
from flux_engine.runtime.campaign import (  # noqa: E402
    DeliveryRequest,
    SuppressionReason,
    compile_campaign,
    run_campaign,
)
from flux_engine.runtime.experiment import (  # noqa: E402
    CompiledExperiment,
    Variant,
    assign_variants,
)
from flux_engine.runtime.journey import compile_journeys  # noqa: E402
from flux_engine.runtime.signals import DEFAULT_ARRIVAL_MODEL, SignalEvent  # noqa: E402
from flux_engine.runtime.temperature import Dial  # noqa: E402
from flux_engine.world import TraceLog  # noqa: E402
from tests.accuracy.gof.kernel_battery import rank_one_threshold  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.references import capped_poisson_pmf  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q, pooled_chisquare  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.journeys import (  # noqa: E402
    SEAM,
    SHIPPED_PERSONA,
    SyntheticStore,
    base,
    fresh_tree,
    population_spec,
    reaction,
    requires_example,
    run,
    synthetic_event,
    with_reactions,
)

pytestmark = [pytest.mark.integration, requires_example]

FAMILY = "gof-kind"
CRITERION = "no statistic rejected by Benjamini-Yekutieli at q=0.01 across the runtime-kind battery"
CRITERION_BY = (
    "no statistic rejected by Benjamini-Yekutieli at q=0.01 across the experiment-assignment "
    "battery"
)
WEIGHTS = {"alpha": Decimal("0.5"), "beta": Decimal("0.3"), "gamma": Decimal("0.2")}


@dataclass
class Measured:
    """Everything the module measured once, for the per-case tests to read."""

    main: Battery
    experiment: Battery
    meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    exact: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: The measured data the power controls re-test against a wrong reference.
    raw: Dict[str, Any] = field(default_factory=dict)

    def note(self, case: str, *, n: int, seed: int, **metrics: Any) -> None:
        self.meta[case] = {"n": n, "seed": seed, "metrics": metrics}

    def check(self, case: str, *, passed: bool, n: int, seed: int, **metrics: Any) -> None:
        self.exact[case] = {"passed": passed, "n": n, "seed": seed, "metrics": metrics}


# --- the module's statistics, as functions of data and reference -----------------
# Shared by the measurements and by the power controls, so that a control runs the
# same statistic the battery runs.


def binomial_p(successes: int, trials: int, p: float) -> float:
    return float(stats.binomtest(successes, trials, p).pvalue)


def occurrence_p(extra: np.ndarray, mean: float) -> float:
    """Pearson chi-square of ``count - 1`` against the Poisson(mean) folded at its cap."""
    cap = poisson_cap(Decimal(str(mean)))
    observed = np.bincount(np.minimum(extra, cap), minlength=cap + 1)
    return pooled_chisquare(observed, capped_poisson_pmf(mean, cap))[1]


def ks_cvm_p(values: np.ndarray, cdf: Any) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """``((KS D, p), (CvM statistic, p))`` against a fully specified CDF."""
    ks = stats.kstest(values, cdf)
    cvm = stats.cramervonmises(values, cdf)
    return (float(ks.statistic), float(ks.pvalue)), (float(cvm.statistic), float(cvm.pvalue))


def start_p(starts: np.ndarray, upper: float = 1.0) -> Tuple[Tuple[float, float], ...]:
    return ks_cvm_p(starts, stats.uniform(0.0, upper).cdf)


def gap_p(gaps: np.ndarray, mean_ms: float) -> Tuple[Tuple[float, float], ...]:
    return ks_cvm_p(gaps, stats.expon(scale=mean_ms).cdf)


def multinomial_p(counts: List[int], weights: List[float]) -> Tuple[float, float, int]:
    return pooled_chisquare(counts, weights)


def uniform_p(members: np.ndarray, n: int) -> Tuple[Tuple[float, float], ...]:
    return ks_cvm_p((members + 0.5) / n, stats.uniform(0.0, 1.0).cdf)


def _add_pair(m: "Measured", case: str, sample: str, pair: Tuple[Tuple[float, float], ...]) -> None:
    for test, (statistic, pvalue) in zip(("ks", "cvm"), pair, strict=True):
        m.main.add(case=case, test=test, statistic=statistic, pvalue=pvalue, sample=sample)


# --- signal arrivals ------------------------------------------------------------


def _emit(seed: int, population: int, engagement: str) -> Tuple[SignalEvent, ...]:
    staged = base()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(signals_module, "payload_families", lambda signal_id, pack: {})
        return emit_signals(
            cast=staged.cast,
            world=population_spec({"engagement": Decimal(engagement)}),
            store=SyntheticStore(population),  # type: ignore[arg-type]
            clock=staged.clock,
            seed_tree=fresh_tree(seed),
            trace=TraceLog(),
            pack=staged.pack,
        )


def _measure_arrivals(m: Measured, tier: Tier) -> None:
    share = DEFAULT_ARRIVAL_MODEL.emitter_share
    p = float(share)
    clock = base().clock

    # engagement 0: everything is frozen, no draw, the emitter set is exact
    n0 = int(tier.pick(pr=3000, nightly=20000))
    seed0 = tier.seed("kind-runtime", "arrivals", "engagement-0")
    emitters0 = sorted({e.ordinal for e in _emit(seed0, n0, "0")})
    exact0 = largest_remainder(n0, {"positive": share, "negative": Decimal(1) - share})["positive"]
    m.check(
        "arrivals/emitters/engagement-0",
        passed=emitters0 == list(range(exact0)),
        n=n0,
        seed=seed0,
        emitters=len(emitters0),
        expected=exact0,
    )

    # engagement 0.5: frozen share exact, stochastic share binomial
    half = Decimal("0.5")
    n5 = int(tier.pick(pr=6000, nightly=40000))
    seed5 = tier.seed("kind-runtime", "arrivals", "engagement-0.5")
    emitters5 = {e.ordinal for e in _emit(seed5, n5, "0.5")}
    frozen = largest_remainder(n5, {"frozen": Decimal(1) - half, "stochastic": half})["frozen"]
    frozen_exact = largest_remainder(frozen, {"positive": share, "negative": Decimal(1) - share})[
        "positive"
    ]
    got_frozen = sorted(o for o in emitters5 if o < frozen)
    m.check(
        "arrivals/emitters/engagement-0.5-frozen",
        passed=got_frozen == list(range(frozen_exact)),
        n=frozen,
        seed=seed5,
        emitters=len(got_frozen),
        expected=frozen_exact,
    )
    stochastic_hits = sum(1 for o in emitters5 if o >= frozen)
    m.main.add(
        case="arrivals/emitters/engagement-0.5-stochastic",
        test="binomial",
        statistic=stochastic_hits / (n5 - frozen),
        pvalue=binomial_p(stochastic_hits, n5 - frozen, p),
        sample="engagement-0.5",
    )
    m.note("arrivals/emitters/engagement-0.5-stochastic", n=n5 - frozen, seed=seed5, p=p)

    # engagement 1: everyone draws; the same events give count, start and gaps.
    # One run, one sample label: the main battery is then Benjamini-Yekutieli.
    n1 = int(tier.pick(pr=12000, nightly=100000))
    seed1 = tier.seed("kind-runtime", "arrivals", "engagement-1")
    shared = "engagement-1-run"
    events = _emit(seed1, n1, "1")
    per_subject: Dict[int, List[SignalEvent]] = {}
    for event in events:
        per_subject.setdefault(event.ordinal, []).append(event)
    m.main.add(
        case="arrivals/emitters/engagement-1",
        test="binomial",
        statistic=len(per_subject) / n1,
        pvalue=binomial_p(len(per_subject), n1, p),
        sample=shared,
    )
    m.note("arrivals/emitters/engagement-1", n=n1, seed=seed1, p=p, emitters=len(per_subject))
    m.raw["emitters"] = (len(per_subject), n1, p)

    mean = DEFAULT_ARRIVAL_MODEL.extra_occurrences_mean
    cap = poisson_cap(mean)
    extra = np.array([len(items) - 1 for items in per_subject.values()], dtype=np.int64)
    over_cap = int((extra > cap).sum())
    wrong_first = sum(1 for items in per_subject.values() if items[0].occurrence != 0)
    m.check(
        "arrivals/structure",
        passed=over_cap == 0 and wrong_first == 0,
        n=len(extra),
        seed=seed1,
        occurrences_over_the_cap=over_cap,
        emitters_whose_first_event_is_not_occurrence_0=wrong_first,
    )
    observed = np.bincount(np.minimum(extra, cap), minlength=cap + 1)
    statistic, pvalue, cells = pooled_chisquare(observed, capped_poisson_pmf(float(mean), cap))
    m.main.add(
        case="arrivals/occurrences", test="chi2", statistic=statistic, pvalue=pvalue, sample=shared
    )
    m.note("arrivals/occurrences", n=len(extra), seed=seed1, mean=float(mean), cells=cells)
    m.raw["extra"] = extra

    window = clock.duration_ms
    starts = np.array(
        [(items[0].t_ms - clock.start_ms + 0.5) / (window + 1) for items in per_subject.values()]
    )
    _add_pair(m, "arrivals/burst-start", shared, start_p(starts))
    m.note("arrivals/burst-start", n=len(starts), seed=seed1, window_ms=window)
    m.raw["starts"] = starts

    gap_mean = float(DEFAULT_ARRIVAL_MODEL.gap_mean_ms)
    gaps: List[int] = []
    for items in per_subject.values():
        items.sort(key=lambda event: event.occurrence)
        gaps.extend(b.t_ms - a.t_ms for a, b in zip(items, items[1:], strict=False))
    gap_values = np.array(gaps, dtype=float)
    _add_pair(m, "arrivals/gaps", shared, gap_p(gap_values, gap_mean))
    m.note(
        "arrivals/gaps",
        n=len(gaps),
        seed=seed1,
        mean_ms=gap_mean,
        observed_mean=float(np.mean(gaps)),
    )
    m.raw["gaps"] = gap_values


# --- journey reactions and campaign delivery ------------------------------------


def _measure_reactions(m: Measured, tier: Tier) -> None:
    shipped = compile_journeys(base().cast, base().model)[0].reactions[SHIPPED_PERSONA]
    reactions = {
        SHIPPED_PERSONA: shipped,
        "persona-low": reaction("persona-low", "0.05", next_state="active"),
        "persona-high": reaction("persona-high", "0.93", next_state="active"),
    }
    names = tuple(reactions)
    per_persona = int(tier.pick(pr=6000, nightly=60000))
    population = per_persona * len(names)
    journey = with_reactions(reactions)
    store = SyntheticStore(population, lambda ordinal: names[ordinal % 3])
    start = base().clock.start_ms
    events = [synthetic_event(journey.signal_id, o, 0, start + 1000) for o in range(population)]
    seed = tier.seed("kind-runtime", "journey-reaction")
    moved = run(journey, events, store, population_spec({}), seed)
    m.check(
        "journey-reaction/structure",
        passed=len(moved) == population,
        n=population,
        seed=seed,
        transitions=len(moved),
    )
    for index, name in enumerate(names):
        wins = sum(1 for t in moved if t.ordinal % 3 == index and t.success)
        p = float(reactions[name].success_probability)
        m.main.add(
            case=f"journey-reaction/{name}",
            test="binomial",
            statistic=wins / per_persona,
            pvalue=binomial_p(wins, per_persona, p),
            sample=f"journey-reaction-{name}",
        )
        m.note(f"journey-reaction/{name}", n=per_persona, seed=seed, p=p, successes=wins)
        if name == SHIPPED_PERSONA:
            m.raw["reaction"] = (wins, per_persona, p)


def _measure_campaign(m: Measured, tier: Tier) -> None:
    staged = base()
    campaign_id = staged.cast.ids("Campaign")[0]
    declared = compile_campaign(staged.cast, campaign_id, staged.model)
    n = int(tier.pick(pr=8000, nightly=80000))
    store = SyntheticStore(n)
    requests = [DeliveryRequest(ordinal=o, t_ms=staged.clock.start_ms) for o in range(n)]
    for label, share in (("default", declared.opt_in_share), ("0.3", Decimal("0.3"))):
        policy = dataclasses.replace(
            declared,
            segment=lambda view: True,
            allowed_jurisdictions=frozenset(),
            requires_explicit_opt_in=True,
            allowed_use_cases=frozenset(),
            denied_use_cases=frozenset(),
            rate_limit_per_second=None,
            opt_in_share=share,
        )
        seed = tier.seed("kind-runtime", "campaign", label)
        ledger = run_campaign(
            cast=staged.cast,
            policy=policy,
            requests=requests,
            store=store,  # type: ignore[arg-type]
            model=staged.model,
            pack=staged.pack,
            seed_tree=fresh_tree(seed),
            trace=TraceLog(),
        )
        delivered = len(ledger.deliveries)
        other_reasons = {row.reason for row in ledger.suppressions} - {
            SuppressionReason.NO_EXPLICIT_OPT_IN
        }
        m.check(
            f"campaign-delivery/{label}/ledger",
            passed=delivered + len(ledger.suppressions) == n and not other_reasons,
            n=n,
            seed=seed,
            delivered=delivered,
            suppressed=len(ledger.suppressions),
            unexpected_reasons=sorted(r.value for r in other_reasons),
        )
        if label == "default":
            m.raw["campaign"] = (delivered, n, float(share))
        m.main.add(
            case=f"campaign-delivery/{label}",
            test="binomial",
            statistic=delivered / n,
            pvalue=binomial_p(delivered, n, float(share)),
            sample=f"campaign-{label}",
        )
        m.note(
            f"campaign-delivery/{label}", n=n, seed=seed, share=float(share), delivered=delivered
        )


# --- experiment assignment -------------------------------------------------------


def _experiment(assignment_seed: int) -> CompiledExperiment:
    return CompiledExperiment(
        experiment_id="exp-accuracy",
        variants=tuple(
            Variant(name=name, weight=weight, treatment_id="tr", is_control=index == 0)
            for index, (name, weight) in enumerate(WEIGHTS.items())
        ),
        assignment_seed=assignment_seed,
        stream_ordinal=0,
    )


def _assign(seed: int, size: int) -> Dict[int, str]:
    return dict(
        assign_variants(
            experiment=_experiment(seed),
            eligible=list(range(size)),
            run_root=derive_root(SEAM, 1),
            trace=TraceLog(),
        )
    )


def _measure_experiment(m: Measured, tier: Tier) -> None:
    # sizes are the largest-remainder allocation, for sizes that leave a remainder too
    off = []
    for size in (97, 100, 101, 1003):
        expected = largest_remainder(size, dict(WEIGHTS))
        got: Dict[str, int] = {}
        for variant in _assign(
            tier.seed("kind-runtime", "experiment", "sizes", size), size
        ).values():
            got[variant] = got.get(variant, 0) + 1
        if got != dict(expected):
            off.append((size, got, dict(expected)))
    m.check(
        "experiment/sizes",
        passed=not off,
        n=4,
        seed=tier.seed("kind-runtime", "experiment", "sizes", 97),
        mismatches=off[:3],
    )

    # the variant of a fixed ordinal, over many assignments, is multinomial
    seeds = tier.seeds(int(tier.pick(pr=200, nightly=2000)), "kind-runtime", "experiment", "seeds")
    size = 100
    names = list(WEIGHTS)
    ordinals = (0, 49, 99)
    tally = {o: dict.fromkeys(names, 0) for o in ordinals}
    sizes_exact = True
    expected = dict(largest_remainder(size, dict(WEIGHTS)))
    for seed in seeds:
        assigned = _assign(seed, size)
        sizes_exact &= all(
            sum(1 for v in assigned.values() if v == name) == expected[name] for name in names
        )
        for o in ordinals:
            tally[o][assigned[o]] += 1
    m.check(
        "experiment/sizes-every-assignment",
        passed=sizes_exact,
        n=len(seeds),
        seed=seeds[0],
        expected=expected,
    )
    for o in ordinals:
        statistic, pvalue, cells = pooled_chisquare(
            [tally[o][name] for name in names], [float(WEIGHTS[name]) for name in names]
        )
        m.experiment.add(
            case=f"experiment/ordinal-{o}",
            test="chi2",
            statistic=statistic,
            pvalue=pvalue,
            sample="assignments",
        )
        m.note(f"experiment/ordinal-{o}", n=len(seeds), seed=seeds[0], counts=tally[o], cells=cells)

    # within one assignment the ordinals of a variant are uniform
    big = 4000
    reps = int(tier.pick(pr=3, nightly=10))
    for rep in range(reps):
        seed = tier.seed("kind-runtime", "experiment", "uniform", rep)
        assigned = _assign(seed, big)
        for name in names:
            members = np.array([o for o, v in assigned.items() if v == name], dtype=float)
            case = f"experiment/uniform-{name}-rep{rep}"
            for test, (statistic, pvalue) in zip(
                ("ks", "cvm"), uniform_p(members, big), strict=True
            ):
                m.experiment.add(
                    case=case,
                    test=test,
                    statistic=statistic,
                    pvalue=pvalue,
                    sample=f"assignment-{rep}",
                )
            m.note(case, n=len(members), seed=seed)


# --- temperature dial ------------------------------------------------------------


def _measure_dial(m: Measured, tier: Tier) -> None:
    sim_id = base().cast.simulation_id
    p = Decimal("0.3")
    n = int(tier.pick(pr=1500, nightly=3000))
    reps = int(tier.pick(pr=16, nightly=60))
    for dial in ("0", "0.5", "1"):
        t = Decimal(dial)
        frozen = largest_remainder(n, {"frozen": Decimal(1) - t, "stochastic": t})["frozen"]
        frozen_exact = (
            largest_remainder(frozen, {"positive": p, "negative": Decimal(1) - p})["positive"]
            if frozen
            else 0
        )
        seeds = tier.seeds(reps, "kind-runtime", "dial", dial)
        exact_ok = True
        stochastic_wins = 0
        for seed in seeds:
            tree = SeedTree(root=derive_root(SEAM, seed))
            realised = Dial("campaignResponse", t).realise(
                range(n),
                p,
                lambda ordinal, tree=tree: tree.stream(
                    temperature_path(sim_id, "campaignResponse", ordinal, 0)
                ),
            )
            in_frozen = sorted(o for o in realised.positive if o < frozen)
            exact_ok &= in_frozen == list(range(frozen_exact))
            stochastic_wins += sum(1 for o in realised.positive if o >= frozen)
        m.check(
            f"dial/{dial}/frozen-share",
            passed=exact_ok,
            n=reps,
            seed=seeds[0],
            frozen=frozen,
            frozen_positives=frozen_exact,
        )
        trials = (n - frozen) * reps
        if trials:
            m.main.add(
                case=f"dial/{dial}/stochastic-share",
                test="binomial",
                statistic=stochastic_wins / trials,
                pvalue=binomial_p(stochastic_wins, trials, float(p)),
                sample=f"dial-{dial}",
            )
            m.note(f"dial/{dial}/stochastic-share", n=trials, seed=seeds[0], p=float(p))
            if dial == "1":
                m.raw["dial"] = (stochastic_wins, trials, float(p))


@pytest.fixture(scope="module")
def measured(tier: Tier) -> Measured:
    m = Measured(
        main=Battery(name="runtime kinds", q=Q),
        experiment=Battery(name="experiment assignment", q=Q),
    )
    _measure_arrivals(m, tier)
    _measure_reactions(m, tier)
    _measure_campaign(m, tier)
    _measure_experiment(m, tier)
    _measure_dial(m, tier)
    return m


def _cases(battery_name: str) -> List[str]:
    names = {
        "main": [
            "arrivals/emitters/engagement-0.5-stochastic",
            "arrivals/emitters/engagement-1",
            "arrivals/occurrences",
            "arrivals/burst-start",
            "arrivals/gaps",
            "journey-reaction/persona-pragmatic-family",
            "journey-reaction/persona-low",
            "journey-reaction/persona-high",
            "campaign-delivery/default",
            "campaign-delivery/0.3",
            "dial/0.5/stochastic-share",
            "dial/1/stochastic-share",
        ],
        "experiment": ["experiment/ordinal-0", "experiment/ordinal-49", "experiment/ordinal-99"],
    }
    return names[battery_name]


def _tests_of(decisions: list) -> Dict[str, Any]:
    return {
        d.entry.test: {
            "statistic": d.entry.statistic,
            "pvalue": d.entry.pvalue,
            "adjusted": d.adjusted,
        }
        for d in decisions
    }


@pytest.mark.parametrize("case", _cases("main"))
def test_runtime_kind_fits_its_declared_distribution(
    case: str, measured: Measured, accuracy_log: ResultLog
) -> None:
    battery = measured.main
    decisions = [d for d in battery.decisions() if d.entry.case == case]
    rejected = [d for d in decisions if d.rejected]
    info = measured.meta.get(case, {"n": None, "seed": None, "metrics": {}})
    accuracy_log.record(
        family=FAMILY,
        case=case,
        criterion=CRITERION,
        passed=bool(decisions) and not rejected,
        seed=info["seed"],
        n=info["n"],
        metrics={**info["metrics"], "method": battery.method, "tests": _tests_of(decisions)},
    )
    assert decisions, f"{case} was not measured"
    assert battery.method == "by"
    assert not rejected, battery.summary()


def test_the_whole_runtime_battery_is_not_rejected_and_every_case_is_tested(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    battery = measured.main
    in_battery = {entry.case for entry in battery.entries}
    unparametrised = in_battery - set(_cases("main"))
    missing = set(_cases("main")) - in_battery
    accuracy_log.record(
        family=FAMILY,
        case="runtime-kinds/battery",
        criterion=CRITERION,
        passed=not battery.rejected() and not unparametrised and not missing,
        seed=tier.seed("kind-runtime", "arrivals", "engagement-1"),
        n=len(battery.entries),
        metrics={
            "method": battery.method,
            "tests": len(battery.entries),
            "unparametrised": sorted(unparametrised),
            "missing": sorted(missing),
        },
    )
    assert not unparametrised and not missing, (unparametrised, missing)
    assert not battery.rejected(), battery.summary()


@pytest.mark.parametrize("case", _cases("experiment"))
def test_experiment_assignment_is_a_uniform_random_permutation(
    case: str, measured: Measured, accuracy_log: ResultLog
) -> None:
    battery = measured.experiment
    decisions = [d for d in battery.decisions() if d.entry.case == case]
    info = measured.meta.get(case, {"n": None, "seed": None, "metrics": {}})
    accuracy_log.record(
        family=FAMILY,
        case=case,
        criterion=CRITERION_BY,
        passed=bool(decisions) and not battery.rejected(),
        seed=info["seed"],
        n=info["n"],
        metrics={**info["metrics"], "method": battery.method, "tests": _tests_of(decisions)},
    )
    assert decisions
    assert battery.method == "by"
    assert not battery.rejected(), battery.summary()


def test_every_within_assignment_uniformity_test_is_in_the_experiment_battery(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """The per-variant KS/CvM tests are adjusted together with the fixed-ordinal chi-squares."""
    battery = measured.experiment
    uniform = [d for d in battery.decisions() if "/uniform-" in d.entry.case]
    expected = 3 * 2 * int(tier.pick(pr=3, nightly=10))
    accuracy_log.record(
        family=FAMILY,
        case="experiment/uniform",
        criterion=CRITERION_BY,
        passed=len(uniform) == expected and not battery.rejected(),
        seed=tier.seed("kind-runtime", "experiment", "uniform", 0),
        n=4000,
        metrics={
            "method": battery.method,
            "tests": len(battery.entries),
            "smallest_adjusted": min(d.adjusted for d in battery.decisions()),
        },
    )
    assert len(uniform) == expected
    assert not battery.rejected(), battery.summary()


EXACT_CASES = (
    "arrivals/emitters/engagement-0",
    "arrivals/emitters/engagement-0.5-frozen",
    "arrivals/structure",
    "journey-reaction/structure",
    "campaign-delivery/default/ledger",
    "campaign-delivery/0.3/ledger",
    "experiment/sizes",
    "experiment/sizes-every-assignment",
    "dial/0/frozen-share",
    "dial/0.5/frozen-share",
    "dial/1/frozen-share",
)


@pytest.mark.parametrize("case", EXACT_CASES)
def test_the_exact_part_is_exact(case: str, measured: Measured, accuracy_log: ResultLog) -> None:
    result: Optional[Dict[str, Any]] = measured.exact.get(case)
    accuracy_log.record(
        family=FAMILY,
        case=case,
        criterion="the largest-remainder share is exact and the structural invariants hold",
        passed=result is not None and result["passed"],
        seed=result["seed"] if result else None,
        n=result["n"] if result else None,
        metrics=result["metrics"] if result else {},
    )
    assert result is not None, f"{case} was not measured"
    assert result["passed"], result["metrics"]
    assert set(measured.exact) == set(EXACT_CASES)


# --- power controls --------------------------------------------------------------
# Each re-runs the module's own statistic against a deliberately wrong reference
# (or biased data) and needs min p <= the battery's rank-one critical value.


def _record_control(
    log: ResultLog, name: str, battery: Battery, defect: str, smallest: float, tier: Tier
) -> float:
    threshold = rank_one_threshold(Q, len(battery.entries), battery.method)
    log.record(
        family=FAMILY,
        case=f"power/{name}",
        criterion=(
            "the injected defect is rejected: smallest p at or below the battery's rank-one "
            "critical value q/(m*c(m))"
        ),
        passed=smallest <= threshold,
        seed=tier.seed("kind-runtime", "power", name),
        n=len(battery.entries),
        metrics={
            "defect": defect,
            "smallest_p": smallest,
            "rank_one_threshold": threshold,
            "tier": tier.name,
        },
    )
    return threshold


def _binomial_control(
    name: str, key: str, delta: tuple, measured: Measured, tier: Tier, log: ResultLog
) -> None:
    successes, trials, declared = measured.raw[key]
    shift = tier.pick(pr=delta[0], nightly=delta[1])
    smallest = binomial_p(successes, trials, declared + shift)
    threshold = _record_control(
        log,
        name,
        measured.main,
        f"declared probability {declared} moved by +{shift}",
        smallest,
        tier,
    )
    assert smallest <= threshold, (smallest, threshold)


def test_the_battery_detects_a_shifted_emitter_share(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """PR: share +0.03 (about 6.8 sigma at 12 000); nightly: +0.01 at 100 000."""
    _binomial_control("emitter-share", "emitters", (0.03, 0.01), measured, tier, accuracy_log)


def test_the_battery_detects_a_shifted_reaction_probability(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """PR: p +0.04 at 6 000 subjects; nightly: +0.01 at 60 000."""
    _binomial_control("reaction", "reaction", (0.04, 0.01), measured, tier, accuracy_log)


def test_the_battery_detects_a_shifted_delivery_share(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """PR: share +0.04 at 8 000 subjects; nightly: +0.01 at 80 000."""
    _binomial_control("campaign", "campaign", (0.04, 0.01), measured, tier, accuracy_log)


def test_the_battery_detects_a_shifted_dial_stochastic_share(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """PR: p +0.03 over the pooled stochastic draws; nightly: +0.01."""
    _binomial_control("dial", "dial", (0.03, 0.01), measured, tier, accuracy_log)


def test_the_battery_detects_a_larger_poisson_mean(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """PR: mean +10% (about 4 100 emitters); nightly: +5% (about 35 000). +2% is not visible."""
    factor = 1 + tier.pick(pr=0.10, nightly=0.05)
    mean = float(DEFAULT_ARRIVAL_MODEL.extra_occurrences_mean) * factor
    smallest = occurrence_p(measured.raw["extra"], mean)
    threshold = _record_control(
        accuracy_log,
        "poisson-mean",
        measured.main,
        f"extra-occurrence mean x{factor:.2f}",
        smallest,
        tier,
    )
    assert smallest <= threshold, (smallest, threshold)


def test_the_battery_detects_a_longer_gap_mean(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """PR: gap mean +15% (about 6 000 gaps); nightly: +5% (about 50 000). +2% is not visible."""
    factor = 1 + tier.pick(pr=0.15, nightly=0.05)
    pair = gap_p(measured.raw["gaps"], float(DEFAULT_ARRIVAL_MODEL.gap_mean_ms) * factor)
    smallest = min(pvalue for _statistic, pvalue in pair)
    threshold = _record_control(
        accuracy_log, "gap-mean", measured.main, f"gap mean x{factor:.2f}", smallest, tier
    )
    assert smallest <= threshold, (smallest, threshold)


def test_the_battery_detects_a_truncated_burst_window(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """The start reference is uniform over only the first 90% (PR) / 97% (nightly) of the window."""
    cut = tier.pick(pr=0.10, nightly=0.03)
    pair = start_p(measured.raw["starts"], upper=1.0 - cut)
    smallest = min(pvalue for _statistic, pvalue in pair)
    threshold = _record_control(
        accuracy_log, "burst-window", measured.main, f"window cut by {cut}", smallest, tier
    )
    assert smallest <= threshold, (smallest, threshold)


def test_the_experiment_battery_detects_a_biased_first_ordinal(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """Ordinal 0 lands in the first arm too often: (0.7, 0.2, 0.1) PR, (0.6, 0.25, 0.15) nightly."""
    biased = tier.pick(pr=(0.7, 0.2, 0.1), nightly=(0.6, 0.25, 0.15))
    seeds = int(tier.pick(pr=200, nightly=2000))
    rng = np.random.default_rng(tier.seed("kind-runtime", "power", "biased-ordinal"))
    counts = [int(c) for c in rng.multinomial(seeds, biased)]
    smallest = multinomial_p(counts, [float(w) for w in WEIGHTS.values()])[1]
    threshold = _record_control(
        accuracy_log,
        "biased-ordinal",
        measured.experiment,
        f"ordinal 0 arm probabilities {biased}",
        smallest,
        tier,
    )
    assert smallest <= threshold, (smallest, threshold)


def test_the_experiment_battery_detects_a_skewed_shuffle(
    measured: Measured, tier: Tier, accuracy_log: ResultLog
) -> None:
    """A biased permutation (sort by uniform key + skew * i/n): low ordinals favour early arms.

    Skew 0.4 at the pull-request tier, 0.2 at nightly; the worst of ten simulated
    repetitions had p of about 2e-9 and 1e-8 respectively.
    """
    skew = tier.pick(pr=0.4, nightly=0.2)
    n = 4000
    rng = np.random.default_rng(tier.seed("kind-runtime", "power", "skewed-shuffle"))
    order = np.argsort(rng.random(n) + skew * np.arange(n) / n)
    sizes = [int(v) for v in largest_remainder(n, dict(WEIGHTS)).values()]
    smallest = 1.0
    start = 0
    for size in sizes:
        members = np.sort(order[start : start + size]).astype(float)
        start += size
        smallest = min(smallest, min(pv for _st, pv in uniform_p(members, n)))
    threshold = _record_control(
        accuracy_log,
        "skewed-shuffle",
        measured.experiment,
        f"shuffle key skew {skew}",
        smallest,
        tier,
    )
    assert smallest <= threshold, (smallest, threshold)
