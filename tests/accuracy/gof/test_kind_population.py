# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 2, by kind: a materialised population fits the World and Persona declarations.

``materialise_population`` is run on a large hand-built world (synthetic data,
no bundle needed) and every declared distribution is measured on the result:

* **markets** -- one ``bounded_uint`` per subject over the NFC-sorted market
  list, so each of five markets is equally likely (chi-square);
* **cohorts** -- ``largest_remainder(size, lifecycleMix)`` exactly, laid down
  as contiguous blocks in NFC-sorted order, and seed-independent. Deterministic,
  so asserted exactly, not tested;
* **personas** -- the same allocation over equal weights, exact;
* **categorical trait** -- its declared levels (chi-square);
* **normal traits** -- the truncated normal at ``TRAIT_SCALE`` (KS and
  Cramer-von Mises against the fully specified CDF), for the default bounds
  (mean +/- ``NORMAL_BOUND_SIGMAS`` sigma), for explicit tight bounds that
  truncate visibly, and for an interval wholly above the mean (the mirrored
  branch of the sampler);
* **scalar trait** -- a constant, carried on every subject, drawing nothing:
  no seed path is materialised for it and no ``trait`` op is traced.

The trait values are quantised to 1e-6 and the reference is continuous. With
a stddev of 0.1 to 0.2 that is a discreteness of at most about 5e-6 in the
ECDF, against a KS critical value of about 1e-2 at 15 000 draws and 3e-3 at
150 000, so the continuous reference is the right one and is conservative.

Pass criterion: no statistic rejected by Benjamini-Yekutieli at ``q = 0.01``
across the battery (BY because KS and Cramer-von Mises share a sample),
asserted per case and once for the whole battery. A value outside its declared
support is counted, logged and fails its case; it never aborts the fixture.

Power (``test_the_battery_catches_an_injected_defect``): the module's own
statistic, run on the real draws against a deliberately wrong reference, must
reach the battery's rank-one value (``q / (m c(m))``, 4.6e-4 for 8 tests).
Pull-request size (12 001 subjects) detects 0.02 of market mass moved between
two markets, 0.03 of segment mass moved, a trait mean 0.02 (4%) high, a tight
upper bound read 0.85 for 0.9, and a stddev 3% or 10% high on the mirrored
branch. Only nightly size (150 001) detects 0.01 of market mass moved, a trait
mean 1% high and a stddev 1% high on the mirrored branch.

The cohort and persona checks are exact, not statistical; the tie cases
exercise the largest-remainder tie-break (exact ties at an odd size,
declaration order unlike key order, a key whose NFC order differs from its
code-point order), which distinct fractional parts never reach.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import unicodedata  # noqa: E402
from collections import Counter  # noqa: E402
from decimal import Decimal  # noqa: E402
from fractions import Fraction  # noqa: E402
from typing import Any, Dict, Tuple  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from scipy import stats  # noqa: E402

from flux_engine.core.lrm import largest_remainder  # noqa: E402
from flux_engine.core.seedtree import SeedTree  # noqa: E402
from flux_engine.core.trace import TraceOp  # noqa: E402
from flux_engine.expr.attrs import VerticalPack  # noqa: E402
from flux_engine.world import SubjectStore, TraceLog, materialise_population  # noqa: E402
from flux_engine.world.derived import Derivation  # noqa: E402
from flux_engine.world.population import NORMAL_BOUND_SIGMAS, TRAIT_SCALE  # noqa: E402
from tests.accuracy.gof.kernel_battery import rank_one_threshold  # noqa: E402
from tests.accuracy.harness.draws import root_for  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.references import truncated_normal  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q, pooled_chisquare  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402

FAMILY = "gof-kind"
CRITERION = "no statistic rejected by Benjamini-Yekutieli at q=0.01 across the population battery"
LABEL = "gof-kind-population"

# A size that no declared share divides: the largest-remainder step has work to do.
SIZE_PR = 12_001
SIZE_NIGHTLY = 150_001

MARKETS = ["de", "fr", "ie", "it", "pl"]
LIFECYCLE_MIX = {"active": "0.505", "atrisk": "0.17", "churned": "0.125", "new": "0.2"}
PERSONA_IDS = ["p-alpha", "p-beta", "p-gamma"]
SEGMENT_LEVELS = {"core": "0.5", "edge": "0.3", "fringe": "0.15", "other": "0.05"}

#: trait -> (declaration, reference mean, sd, lo, hi)
NORMALS: Dict[str, Tuple[Dict[str, str], float, float, float, float]] = {
    "appetite": (
        {"dist": "normal", "mean": "0.5", "stddev": "0.2"},
        0.5,
        0.2,
        0.5 - float(NORMAL_BOUND_SIGMAS) * 0.2,
        0.5 + float(NORMAL_BOUND_SIGMAS) * 0.2,
    ),
    "tight": (
        {"dist": "normal", "mean": "0.5", "stddev": "0.2", "min": "0.4", "max": "0.9"},
        0.5,
        0.2,
        0.4,
        0.9,
    ),
    "above_mean": (
        {"dist": "normal", "mean": "0.2", "stddev": "0.1", "min": "0.45", "max": "0.9"},
        0.2,
        0.1,
        0.45,
        0.9,
    ),
}


def _persona(persona_id: str) -> Dict[str, Any]:
    traits: Dict[str, Any] = {
        "segment": {"dist": "categorical", "levels": dict(SEGMENT_LEVELS)},
        "fluency": "0.6",
    }
    traits.update({name: dict(spec[0]) for name, spec in NORMALS.items()})
    return {"id": persona_id, "spec": {"traits": traits}}


def _world(size: int, mix: Dict[str, str] | None = None) -> Dict[str, Any]:
    return {
        "id": "w-gof",
        "spec": {
            "vertical": "gof",
            "population": {
                "size": size,
                "markets": list(MARKETS),
                "lifecycleMix": dict(LIFECYCLE_MIX if mix is None else mix),
            },
        },
    }


def materialise(
    size: int, seed: int, mix: Dict[str, str] | None = None
) -> Tuple[SubjectStore, TraceLog, SeedTree]:
    """The population of the test world, with no vertical-derived attributes drawn."""
    seed_tree = SeedTree(root=root_for(LABEL, seed))
    trace = TraceLog()
    store = materialise_population(
        world=_world(size, mix),
        personas=[_persona(persona_id) for persona_id in PERSONA_IDS],
        pack=VerticalPack(name="gof"),
        seed_tree=seed_tree,
        trace=trace,
        derivation=Derivation(vertical="gof", attributes={}),
    )
    return store, trace, seed_tree


class Population:
    def __init__(self, tier: Tier) -> None:
        self.size = int(tier.pick(pr=SIZE_PR, nightly=SIZE_NIGHTLY))
        self.seed = tier.seed(LABEL, "population")
        self.violations: Counter = Counter()
        self.store, self.trace, self.seed_tree = materialise(self.size, self.seed)


@pytest.fixture(scope="module")
def population(tier: Tier) -> Population:
    return Population(tier)


def _chi(battery: Battery, case: str, observed: Any, probabilities: Any) -> None:
    statistic, pvalue, _cells = pooled_chisquare(observed, probabilities)
    battery.add(case=case, test="chi2", statistic=statistic, pvalue=pvalue, sample=case)


def _level_counts(
    values: Any, weights: Dict[str, float], viol: Counter, case: str
) -> Tuple[np.ndarray, np.ndarray]:
    """Counts per level and the declared probabilities; a value off the support is counted, not raised."""
    names = sorted(weights)
    index = {name: i for i, name in enumerate(names)}
    kept = [index[v] for v in values if v in index]
    viol[case] += len(values) - len(kept)
    return (
        np.bincount(np.array(kept, dtype=np.int64), minlength=len(names)),
        np.array([weights[name] for name in names], dtype=float),
    )


def _normal_tests(values: Any, mean: float, sd: float, lo: float, hi: float) -> Tuple[Any, Any]:
    sample = np.array([float(v) for v in values], dtype=float)
    cdf = truncated_normal(mean, sd, lo, hi).cdf
    return stats.kstest(sample, cdf), stats.cramervonmises(sample, cdf)


MARKET_WEIGHTS = {name: 1.0 / len(MARKETS) for name in MARKETS}
SEGMENT_WEIGHTS = {name: float(Decimal(w)) for name, w in SEGMENT_LEVELS.items()}


def build_battery(population: Population) -> Battery:
    store = population.store
    battery = Battery(name="population", q=Q)
    viol = population.violations

    _chi(battery, "market", *_level_counts(store.column("market"), MARKET_WEIGHTS, viol, "market"))
    _chi(
        battery,
        "trait-segment",
        *_level_counts(store.column("traits.segment"), SEGMENT_WEIGHTS, viol, "trait-segment"),
    )
    for name, (_, mean, sd, lo, hi) in NORMALS.items():
        ks, cvm = _normal_tests(store.column(f"traits.{name}"), mean, sd, lo, hi)
        case = f"trait-{name}"
        battery.add(case=case, test="ks", statistic=ks.statistic, pvalue=ks.pvalue, sample=case)
        battery.add(case=case, test="cvm", statistic=cvm.statistic, pvalue=cvm.pvalue, sample=case)
    return battery


@pytest.fixture(scope="module")
def battery(population: Population) -> Battery:
    return build_battery(population)


# Power: a defect injected into the reference, caught when the module's own
# statistic reaches the battery's rank-one critical value.
def _weights_moved(weights: Dict[str, float], source: str, target: str, mass: float):
    moved = dict(weights)
    moved[source] -= mass
    moved[target] += mass
    return moved


def _p_levels(column: str, case: str, weights: Dict[str, float]):
    return lambda store: float(
        pooled_chisquare(*_level_counts(store.column(column), weights, Counter(), case))[1]
    )


def _p_normal(name: str, mean: float, sd: float, lo: float, hi: float):
    return lambda store: min(
        t.pvalue for t in _normal_tests(store.column(f"traits.{name}"), mean, sd, lo, hi)
    )


#: name -> (defect, p-value function of the store, nightly only)
CONTROLS: Dict[str, Tuple[str, Any, bool]] = {
    "market-weight-0.02-moved": (
        "0.02 of the market probability moved from de to pl",
        _p_levels("market", "market", _weights_moved(MARKET_WEIGHTS, "de", "pl", 0.02)),
        False,
    ),
    "segment-3pt-moved": (
        "0.03 of probability moved from core to edge",
        _p_levels(
            "traits.segment", "trait-segment", _weights_moved(SEGMENT_WEIGHTS, "core", "edge", 0.03)
        ),
        False,
    ),
    "appetite-mean+0.02": (
        "the declared mean 0.5 read 0.02 high (4%)",
        _p_normal("appetite", 0.52, 0.2, -1.1, 2.1),
        False,
    ),
    "tight-upper-bound-0.9-read-0.85": (
        "the declared max 0.9 read as 0.85",
        _p_normal("tight", 0.5, 0.2, 0.4, 0.85),
        False,
    ),
    "above_mean-sd+10%": (
        "the declared stddev 0.1 read 10% high on the mirrored branch",
        _p_normal("above_mean", 0.2, 0.11, 0.45, 0.9),
        False,
    ),
    "above_mean-sd+3%": (
        "the declared stddev 0.1 read 3% high on the mirrored branch",
        _p_normal("above_mean", 0.2, 0.103, 0.45, 0.9),
        False,
    ),
    "market-weight-0.01-moved": (
        "0.01 of the market probability moved from de to pl",
        _p_levels("market", "market", _weights_moved(MARKET_WEIGHTS, "de", "pl", 0.01)),
        True,
    ),
    "appetite-mean+1%": (
        "the declared mean 0.5 read 1% high",
        _p_normal("appetite", 0.505, 0.2, -1.1, 2.1),
        True,
    ),
    "above_mean-sd+1%": (
        "the declared stddev 0.1 read 1% high on the mirrored branch",
        _p_normal("above_mean", 0.2, 0.101, 0.45, 0.9),
        True,
    ),
}


STATISTICAL_CASES = ["market", "trait-segment", *[f"trait-{name}" for name in NORMALS]]


@pytest.mark.parametrize("name", STATISTICAL_CASES)
def test_population_fits_its_declaration(
    name: str, battery: Battery, population: Population, tier: Tier, accuracy_log: ResultLog
) -> None:
    decisions = [d for d in battery.decisions() if d.entry.case == name]
    assert decisions
    rejected = [d for d in decisions if d.rejected]
    violations = int(population.violations[name])
    accuracy_log.record(
        family=FAMILY,
        case=f"population/{name}",
        criterion=CRITERION,
        passed=not rejected and violations == 0,
        seed=population.seed,
        n=population.size,
        metrics={
            "method": battery.method,
            "violations": violations,
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
    assert violations == 0, f"{name}: {violations} values fell outside the declared support"
    assert battery.method == "by"
    assert not rejected, battery.summary()


def test_the_whole_battery_is_clean_and_every_case_is_parametrised(
    battery: Battery, population: Population, accuracy_log: ResultLog
) -> None:
    in_battery = {entry.case for entry in battery.entries}
    rejected = battery.rejected()
    unparametrised = sorted(in_battery - set(STATISTICAL_CASES))
    unmeasured = sorted(set(STATISTICAL_CASES) - in_battery)
    violations = sum(population.violations.values())
    accuracy_log.record(
        family=FAMILY,
        case="population/battery",
        criterion=CRITERION,
        passed=not (rejected or unparametrised or unmeasured or violations),
        seed=population.seed,
        n=population.size,
        metrics={
            "tests": len(battery.entries),
            "rejected": [f"{d.entry.case}/{d.entry.test}" for d in rejected],
            "unparametrised": unparametrised,
            "unmeasured": unmeasured,
            "violations": violations,
        },
    )
    assert not unparametrised, f"in the battery but not parametrised: {unparametrised}"
    assert not unmeasured, f"parametrised but not measured: {unmeasured}"
    assert violations == 0
    assert not rejected, battery.summary()


@pytest.mark.parametrize("name", list(CONTROLS))
def test_the_battery_catches_an_injected_defect(
    name: str,
    battery: Battery,
    population: Population,
    tier: Tier,
    accuracy_log: ResultLog,
) -> None:
    defect, p_of, nightly_only = CONTROLS[name]
    if nightly_only:
        tier.only_nightly(f"the pull-request size cannot see: {defect}")
    threshold = rank_one_threshold(Q, len(battery.entries), battery.method)
    pvalue = p_of(population.store)
    caught = pvalue <= threshold
    accuracy_log.record(
        family=FAMILY,
        case=f"population/power/{name}",
        criterion="the module's own statistic rejects the defect at the battery's rank-one value",
        passed=caught,
        seed=population.seed,
        n=population.size,
        metrics={"defect": defect, "pvalue": pvalue, "threshold": threshold},
    )
    assert caught, f"{defect}: p={pvalue:.3g} above the rank-one value {threshold:.3g}"


@pytest.mark.parametrize("name", list(NORMALS))
def test_normal_trait_values_stay_on_the_declared_interval_and_scale(
    name: str, population: Population, accuracy_log: ResultLog
) -> None:
    """Exact: inside ``[min, max]`` (the default bounds are mean +/- 8 sigma), 6 places."""
    _, mean, sd, lo, hi = NORMALS[name]
    values = population.store.column(f"traits.{name}")
    spec = NORMALS[name][0]
    low = (
        Decimal(spec["min"])
        if "min" in spec
        else Decimal(spec["mean"]) - NORMAL_BOUND_SIGMAS * Decimal(spec["stddev"])
    )
    high = (
        Decimal(spec["max"])
        if "max" in spec
        else Decimal(spec["mean"]) + NORMAL_BOUND_SIGMAS * Decimal(spec["stddev"])
    )
    outside = sum(1 for v in values if not low <= v <= high)
    off_scale = sum(1 for v in values if v.as_tuple().exponent != -TRAIT_SCALE)
    accuracy_log.record(
        family=FAMILY,
        case=f"population/trait-{name}-bounds",
        criterion="every draw inside its declared interval, at the trait scale",
        passed=outside == 0 and off_scale == 0,
        seed=population.seed,
        n=population.size,
        metrics={"outside": outside, "offScale": off_scale, "lo": str(low), "hi": str(high)},
    )
    assert outside == 0, f"{outside} draws fell outside [{low}, {high}]"
    assert off_scale == 0, f"{off_scale} draws are not quantised to {TRAIT_SCALE} places"


def _exact_largest_remainder(size: int, weights: Dict[str, Fraction]) -> Dict[str, int]:
    """Largest remainder on exact rationals, ties to the NFC-sorted (here: ASCII) key.

    Written out independently of ``flux_engine.core.lrm``.
    """
    floors = {k: int(size * w) for k, w in weights.items()}
    remainders = {k: size * w - floors[k] for k, w in weights.items()}
    leftover = size - sum(floors.values())
    order = sorted(weights, key=lambda k: (-remainders[k], unicodedata.normalize("NFC", k)))
    for key in order[:leftover]:
        floors[key] += 1
    return floors


def test_cohort_sizes_are_the_largest_remainder_of_the_lifecycle_mix(
    population: Population, accuracy_log: ResultLog
) -> None:
    store = population.store
    declared = {k: Decimal(v) for k, v in LIFECYCLE_MIX.items()}
    expected = largest_remainder(population.size, declared)
    independent = _exact_largest_remainder(
        population.size, {k: Fraction(v) for k, v in LIFECYCLE_MIX.items()}
    )
    counts = store.cohort_counts()
    # Cohorts are contiguous blocks in sorted-lifecycle order.
    labels = [store.lifecycle(i) for i in store.ordinals]
    blocks = sorted(declared)
    contiguous = labels == [name for name in blocks for _ in range(expected[name])]
    # A different seed changes no cohort size.
    other, _, _ = materialise(997, population.seed + 1)
    same_rule = other.cohort_counts() == dict(sorted(largest_remainder(997, declared).items()))
    passed = (
        counts == dict(sorted(expected.items()))
        and expected == independent
        and sum(counts.values()) == population.size
        and contiguous
        and same_rule
    )
    accuracy_log.record(
        family=FAMILY,
        case="population/cohorts",
        criterion="cohort sizes equal largest_remainder(size, lifecycleMix) exactly",
        passed=passed,
        seed=population.seed,
        n=population.size,
        metrics={"counts": counts, "expected": dict(sorted(expected.items()))},
    )
    assert counts == dict(sorted(expected.items()))
    assert expected == independent, "the engine's LRM disagrees with an exact-rational LRM"
    assert contiguous
    assert same_rule


def test_persona_split_is_the_largest_remainder_over_equal_weights(
    population: Population, accuracy_log: ResultLog
) -> None:
    store = population.store
    size = population.size
    # The engine's equal weights: one third, three times, the last absorbing the residue
    # so the weights sum to exactly one (population._equal_share).
    share = Decimal(1) / Decimal(len(PERSONA_IDS))
    weights = {name: share for name in PERSONA_IDS}
    weights[PERSONA_IDS[-1]] = Decimal(1) - share * (len(PERSONA_IDS) - 1)
    expected = largest_remainder(size, weights)
    counts: Dict[str, int] = {}
    for ordinal in store.ordinals:
        counts[store.persona(ordinal)] = counts.get(store.persona(ordinal), 0) + 1
    labels = [store.persona(i) for i in store.ordinals]
    contiguous = labels == [name for name in sorted(expected) for _ in range(expected[name])]
    fair = max(counts.values()) - min(counts.values()) <= 1
    passed = counts == expected and contiguous and fair and sum(counts.values()) == size
    accuracy_log.record(
        family=FAMILY,
        case="population/personas",
        criterion="persona split equals largest_remainder over equal weights exactly",
        passed=passed,
        seed=population.seed,
        n=size,
        metrics={"counts": counts},
    )
    assert counts == expected
    assert contiguous
    assert fair, "personas differ by more than one subject"


def test_a_scalar_trait_is_constant_and_draws_nothing(
    population: Population, accuracy_log: ResultLog
) -> None:
    store = population.store
    column = store.column("traits.fluency")
    constant = set(column) == {Decimal("0.6")} and len(column) == population.size
    drawn_paths = [path for path in population.seed_tree.materialised_paths if "fluency" in path]
    traced = [r for r in population.trace.of(TraceOp.TRAIT) if r["name"] == "fluency"]
    # The control: a drawn trait does materialise one path per subject and trace one op each.
    control_paths = [path for path in population.seed_tree.materialised_paths if "appetite" in path]
    passed = constant and not drawn_paths and not traced and len(control_paths) == population.size
    accuracy_log.record(
        family=FAMILY,
        case="population/scalar-trait",
        criterion="a scalar trait is its declared constant and materialises no stream",
        passed=passed,
        seed=population.seed,
        n=population.size,
        metrics={
            "constant": constant,
            "pathsForScalar": len(drawn_paths),
            "pathsForDrawnTrait": len(control_paths),
        },
    )
    assert constant
    assert not drawn_paths, "a scalar trait materialised a seed path"
    assert not traced, "a scalar trait was traced"
    assert len(control_paths) == population.size, "the control (a drawn trait) did not draw"


def _nfc(key: str) -> str:
    return unicodedata.normalize("NFC", key)


#: (mix as declared, size, the key that must receive the residual unit)
TIE_CASES = {
    # two exact ties at an odd size: the single leftover unit goes to the NFC-first key, "a"
    "two-halves-odd-size": ({"b": "0.5", "a": "0.5"}, 1001, "a"),
    # declared zeta first; alpha and zeta tie on 0.5 and the one leftover unit goes to alpha
    "declaration-order-is-not-key-order": (
        {"zeta": "0.25", "alpha": "0.25", "mid": "0.5"},
        2,
        "alpha",
    ),
    # "e" + combining acute sorts before "f" by code point but after it once NFC-composed
    # ("\u00e9" > "f"): the unit must go to "f", the NFC-first key
    "key-order-differs-after-nfc": ({"e\u0301a": "0.5", "f": "0.5"}, 1, "f"),
    # a four-way exact tie at size 2: the two NFC-first keys, "a" and "b", get the units
    "four-way-ties": ({"c": "0.25", "b": "0.25", "a": "0.25", "d": "0.25"}, 2, "a"),
}


@pytest.mark.parametrize("name", list(TIE_CASES))
def test_cohort_ties_go_to_the_nfc_first_key_and_never_to_the_seed(
    name: str, population: Population, accuracy_log: ResultLog
) -> None:
    mix, size, winner = TIE_CASES[name]
    expected = _exact_largest_remainder(size, {_nfc(k): Fraction(v) for k, v in mix.items()})
    counts = []
    for seed_offset in (0, 1, 2):
        store, _, _ = materialise(size, population.seed + seed_offset, mix)
        counts.append(store.cohort_counts())
    engine = counts[0]
    independent = {k: v for k, v in sorted(expected.items()) if v}
    stable = all(c == engine for c in counts)
    won = engine.get(_nfc(winner), 0) == max(engine.values())
    passed = engine == independent and stable and won
    accuracy_log.record(
        family=FAMILY,
        case=f"population/cohort-ties/{name}",
        criterion="a tied remainder goes to the NFC-first key, whatever the seed",
        passed=passed,
        seed=population.seed,
        n=size,
        metrics={"engine": engine, "independent": independent},
    )
    assert engine == independent
    assert stable, "a cohort size changed with the seed"
    assert won, f"the residual unit did not go to {winner!r}"
