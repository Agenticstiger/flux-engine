# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 6, journey transitions: the empirical transition matrix is the declared one.

A journey row is a (Persona, from-state) pair. The declared row says: with
probability ``p`` (``successProbability``) the subject moves to the reaction's
``nextState``, otherwise it stays. The stage is run over a large synthetic
population whose Personas and lifecycle states are laid out so that every row
the taxonomy allows has the same number of subjects, and each subject gets
three trigger events (three steps).

What is measured, and why each is a valid test:

* **Rows**, plain World (no ``campaignResponse`` dial, so the draw is a plain
  Bernoulli on the journey path). Every eligible step is one Bernoulli(p) trial.
  A subject stops being eligible after its first success (the new state has no
  edge to ``nextState``), so the trials of one row are not a fixed-size sample
  and counts pooled across steps would not be binomial. Each ``(row, step)``
  cell is: conditional on the set of subjects that reached step *k* still in
  the row's state, their step-*k* draws are independent Bernoulli(p). The exact
  binomial test is run per cell, all cells of both seeds in one Benjamini-
  Hochberg battery at ``q = 0.01`` (each cell reads fresh draws, so the
  p-values are independent). Total-variation and Jensen-Shannon distances
  between the empirical and declared two-point split are reported for step 0,
  the cell that holds every subject of the row.
* **The two seeds agree**: the step-0 2x2 tables (success/failure) of two
  independent seeds are compared per row with a two-sample chi-square.
* **Structure**, checked deterministically: no transition is recorded from a
  row the taxonomy does not allow, a success lands on ``nextState`` with the
  declared outcome, a failure stays put with the negated outcome.
* **The ``campaignResponse`` dial**, on a Persona with no ``nextState`` (so it
  is eligible at every step and every group is fully observable): the frozen
  share's success count in every (step, Persona) group is exactly
  ``LRM(count_frozen, p)`` and its successes are the first frozen ordinals; the
  stochastic share's successes are binomial. The frozen share is derived here
  from ``largest_remainder`` over ``{frozen: 1-t, stochastic: t}`` directly, not
  from the engine's own ``partition``.
* **Power**: tested against a declared probability moved by 0.1, every row's
  step-0 count must be rejected at the battery's rank-one critical value.

The rows' populations are synthetic (see ``tests/accuracy/journeys/__init__``):
the stage runs on the real documents, seed paths and samplers.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

from collections import defaultdict  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Dict, List, Sequence, Tuple  # noqa: E402

import pytest  # noqa: E402
from scipy import stats  # noqa: E402
from scipy.spatial.distance import jensenshannon  # noqa: E402

from flux_engine.core.lrm import largest_remainder  # noqa: E402
from flux_engine.runtime.journey import Transition, negated_outcome  # noqa: E402
from tests.accuracy.gof.kernel_battery import rank_one_threshold  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.journeys import (  # noqa: E402
    SyntheticStore,
    base,
    population_spec,
    reaction,
    requires_example,
    run,
    synthetic_event,
    with_reactions,
)

pytestmark = [pytest.mark.integration, requires_example]

FAMILY = "journeys"
STEPS = 3
LIFECYCLE = ("new", "atrisk", "active", "churned")

#: Persona -> (declared p, nextState, outcome). The shipped taxonomy's edges are
#: new->active, active->atrisk, atrisk->active, atrisk->churned.
MOVING = {
    "persona-a": ("0.42", "active", "recovered"),  # legal from new and atrisk
    "persona-b": ("0.65", "churned", "lost"),  # legal from atrisk only
    "persona-c": ("0.08", "atrisk", "slipped"),  # legal from active only
}
PERSONAS = tuple(MOVING)
LEGAL_ROWS = (
    ("persona-a", "new"),
    ("persona-a", "atrisk"),
    ("persona-b", "atrisk"),
    ("persona-c", "active"),
)
STEADY = "persona-d"  # no nextState: eligible at every step, for the dial test
STEADY_P = "0.37"

ROW_CRITERION = (
    "no (Persona, from-state, step) cell rejected by the exact binomial test under "
    "Benjamini-Hochberg at q=0.01, both seeds in one battery"
)


def _persona_of(ordinal: int) -> str:
    return PERSONAS[ordinal % 3]


def _lifecycle_of(ordinal: int) -> str:
    return LIFECYCLE[(ordinal // 3) % 4]


def _row_name(persona: str, state: str) -> str:
    return f"{persona}/{state}"


def _events(count: int) -> List:
    signal = with_reactions({}).signal_id
    start = base().clock.start_ms
    return [
        synthetic_event(signal, ordinal, step, start + 1000 * (step + 1))
        for ordinal in range(count)
        for step in range(STEPS)
    ]


def _journey():
    return with_reactions(
        {
            persona: reaction(persona, p, next_state=next_state, outcome=outcome)
            for persona, (p, next_state, outcome) in MOVING.items()
        }
    )


class _Run:
    """One seed's transitions, tallied per (Persona, from-state, step)."""

    def __init__(self, seed: int, transitions: Sequence[Transition]) -> None:
        self.seed = seed
        self.transitions = tuple(transitions)
        self.trials: Dict[Tuple[str, str, int], int] = defaultdict(int)
        self.wins: Dict[Tuple[str, str, int], int] = defaultdict(int)
        for item in self.transitions:
            key = (_persona_of(item.ordinal), item.from_state, item.step)
            self.trials[key] += 1
            self.wins[key] += 1 if item.success else 0


@pytest.fixture(scope="module")
def cell(tier: Tier) -> int:
    """Subjects per (Persona, lifecycle state) cell; the population is 12 cells."""
    return int(tier.pick(pr=1500, nightly=12000))


@pytest.fixture(scope="module")
def runs(tier: Tier, cell: int) -> Tuple[_Run, _Run]:
    population = 12 * cell
    store = SyntheticStore(population, _persona_of, _lifecycle_of)
    journey = _journey()
    events = _events(population)
    spec = population_spec({})
    seeds = tier.seeds(2, "journeys", "transitions", "plain")
    return tuple(  # type: ignore[return-value]
        _Run(seed, run(journey, events, store, spec, seed)) for seed in seeds
    )


def _declared(persona: str) -> float:
    return float(Decimal(MOVING[persona][0]))


def _row_battery(runs: Sequence[_Run]) -> Battery:
    battery = Battery(name="journey transition rows", q=Q)
    for one in runs:
        for (persona, state, step), trials in sorted(one.trials.items()):
            result = stats.binomtest(one.wins[(persona, state, step)], trials, _declared(persona))
            battery.add(
                case=_row_name(persona, state),
                test=f"binomial/step{step}/seed{one.seed}",
                statistic=one.wins[(persona, state, step)] / trials,
                pvalue=result.pvalue,
                sample=f"{one.seed}/{persona}/{state}/{step}",
            )
    return battery


def _distances(successes: int, trials: int, p: float) -> Tuple[float, float]:
    empirical = [successes / trials, 1 - successes / trials]
    declared = [p, 1 - p]
    total_variation = abs(empirical[0] - declared[0])
    return total_variation, float(jensenshannon(empirical, declared, base=2))


@pytest.mark.parametrize("row", [_row_name(*row) for row in LEGAL_ROWS])
def test_row_matches_its_declared_split(
    row: str, runs: Tuple[_Run, _Run], tier: Tier, cell: int, accuracy_log: ResultLog
) -> None:
    battery = _row_battery(runs)
    persona, state = row.split("/")
    decisions = [d for d in battery.decisions() if d.entry.case == row]
    rejected = [d for d in decisions if d.rejected]
    metrics = {"declared": MOVING[persona][0], "method": battery.method, "cells": len(decisions)}
    for one in runs:
        trials = one.trials[(persona, state, 0)]
        wins = one.wins[(persona, state, 0)]
        tv, js = _distances(wins, trials, _declared(persona))
        metrics[f"seed{one.seed}"] = {
            "n_step0": trials,
            "successes_step0": wins,
            "empirical": wins / trials,
            "total_variation": tv,
            "jensen_shannon": js,
        }
    accuracy_log.record(
        family=FAMILY,
        case=f"transitions/{row}",
        criterion=ROW_CRITERION,
        passed=not rejected,
        metrics=metrics,
        seed=runs[0].seed,
        n=12 * cell,
    )
    assert battery.method == "bh"
    assert decisions, f"no transition was recorded for the row {row}"
    assert not rejected, battery.summary()


def test_the_whole_row_battery_is_not_rejected(
    runs: Tuple[_Run, _Run], tier: Tier, cell: int, accuracy_log: ResultLog
) -> None:
    battery = _row_battery(runs)
    accuracy_log.record(
        family=FAMILY,
        case="transitions/battery",
        criterion=ROW_CRITERION,
        passed=not battery.rejected(),
        metrics={"tests": len(battery.entries), "method": battery.method},
        seed=runs[0].seed,
        n=12 * cell,
    )
    assert not battery.rejected(), battery.summary()


def test_the_taxonomy_is_respected_and_outcomes_are_recorded(
    runs: Tuple[_Run, _Run], cell: int, accuracy_log: ResultLog
) -> None:
    legal = set(LEGAL_ROWS)
    problems: List[str] = []
    for one in runs:
        for item in one.transitions:
            persona = _persona_of(item.ordinal)
            _p, next_state, outcome = MOVING[persona]
            if (persona, item.from_state) not in legal:
                problems.append(f"transition from the illegal row {persona}/{item.from_state}")
            elif item.success and (item.to_state, item.outcome) != (next_state, outcome):
                problems.append(f"success recorded {item.to_state}/{item.outcome}")
            elif not item.success and (
                item.to_state != item.from_state or item.outcome != negated_outcome(outcome)
            ):
                problems.append(f"failure recorded {item.to_state}/{item.outcome}")
    accuracy_log.record(
        family=FAMILY,
        case="transitions/structure",
        criterion="no transition from a row the taxonomy forbids; every success/failure recorded as declared",
        passed=not problems,
        metrics={
            "transitions": sum(len(one.transitions) for one in runs),
            "problems": problems[:5],
        },
        seed=runs[0].seed,
        n=12 * cell,
    )
    assert not problems, problems[:5]
    expected_rows = {(p, s) for (p, s, _step) in runs[0].trials}
    assert expected_rows == legal


@pytest.mark.parametrize("row", [_row_name(*row) for row in LEGAL_ROWS])
def test_two_seeds_give_the_same_row(
    row: str, runs: Tuple[_Run, _Run], tier: Tier, cell: int, accuracy_log: ResultLog
) -> None:
    """Step-0 2x2 tables of two independent seeds, two-sample chi-square, own battery (BH)."""
    battery = Battery(name="journey rows, seed vs seed", q=Q)
    tables: Dict[str, List[List[int]]] = {}
    for persona, state in LEGAL_ROWS:
        table = [
            [
                one.wins[(persona, state, 0)],
                one.trials[(persona, state, 0)] - one.wins[(persona, state, 0)],
            ]
            for one in runs
        ]
        result = stats.chi2_contingency(table, correction=False)
        battery.add(
            case=_row_name(persona, state),
            test="chi2-two-sample",
            statistic=result.statistic,
            pvalue=result.pvalue,
            sample=_row_name(persona, state),
        )
        tables[_row_name(persona, state)] = table
    decisions = [d for d in battery.decisions() if d.entry.case == row]
    accuracy_log.record(
        family=FAMILY,
        case=f"transitions/two-seeds/{row}",
        criterion="no row's seed-vs-seed 2x2 chi-square rejected by Benjamini-Hochberg at q=0.01",
        passed=not any(d.rejected for d in decisions),
        metrics={"table": tables[row], "pvalue": decisions[0].entry.pvalue},
        seed=runs[0].seed,
        n=2 * 12 * cell,
    )
    assert battery.method == "bh"
    assert not battery.rejected(), battery.summary()


def test_the_row_test_has_power_against_a_shift_of_a_tenth(
    runs: Tuple[_Run, _Run], cell: int, accuracy_log: ResultLog
) -> None:
    """Against p moved by 0.1, every row's step-0 count is rejected at the rank-one value."""
    battery = _row_battery(runs)
    threshold = rank_one_threshold(Q, len(battery.entries), battery.method)
    worst = 0.0
    caught = 0
    total = 0
    for one in runs:
        for persona, state in LEGAL_ROWS:
            p = _declared(persona)
            shifted = p - 0.1 if p > 0.5 else p + 0.1
            pvalue = stats.binomtest(
                one.wins[(persona, state, 0)], one.trials[(persona, state, 0)], shifted
            ).pvalue
            worst = max(worst, pvalue)
            caught += pvalue <= threshold
            total += 1
    accuracy_log.record(
        family=FAMILY,
        case="transitions/power",
        criterion="a 0.1 shift of the declared probability is caught in every row, both seeds",
        passed=caught == total,
        metrics={"caught": caught, "of": total, "rank_one_threshold": threshold, "worst_p": worst},
        seed=runs[0].seed,
        n=cell,
    )
    assert caught == total, (caught, total, worst, threshold)


# --- the campaignResponse dial -------------------------------------------------


@pytest.fixture(scope="module")
def steady_population(tier: Tier) -> int:
    return int(tier.pick(pr=12000, nightly=60000))


@pytest.mark.parametrize("dial", ["0", "0.5", "1"])
def test_the_dial_freezes_an_exact_share_and_draws_the_rest(
    dial: str, steady_population: int, tier: Tier, accuracy_log: ResultLog
) -> None:
    t = Decimal(dial)
    p = Decimal(STEADY_P)
    n = steady_population
    journey = with_reactions({STEADY: reaction(STEADY, STEADY_P, next_state=None)})
    store = SyntheticStore(n, lambda ordinal: STEADY)
    events = _events(n)
    spec = population_spec({"campaignResponse": t})
    frozen_count = largest_remainder(n, {"frozen": Decimal(1) - t, "stochastic": t})["frozen"]
    frozen_wins = (
        largest_remainder(frozen_count, {"positive": p, "negative": Decimal(1) - p})["positive"]
        if frozen_count
        else 0
    )
    battery = Battery(name=f"campaignResponse dial {dial}, stochastic share", q=Q)
    seeds = tier.seeds(2, "journeys", "dial", dial)
    frozen_sets = []
    violations: List[str] = []
    for seed in seeds:
        moved = run(journey, events, store, spec, seed)
        if len(moved) != n * STEPS:
            violations.append(f"seed {seed}: {len(moved)} transitions, expected {n * STEPS}")
        for step in range(STEPS):
            group = sorted((m.ordinal, m.success) for m in moved if m.step == step)
            if len(group) != n:
                violations.append(f"seed {seed} step {step}: group of {len(group)}, expected {n}")
                continue
            frozen = group[:frozen_count]
            stochastic = group[frozen_count:]
            got = [ordinal for ordinal, success in frozen if success]
            if len(got) != frozen_wins:
                violations.append(
                    f"seed {seed} step {step}: {len(got)} frozen wins, not {frozen_wins}"
                )
            if got != [ordinal for ordinal, _ in frozen[:frozen_wins]]:
                violations.append(
                    f"seed {seed} step {step}: frozen wins are not the first ordinals"
                )
            frozen_sets.append(tuple(got))
            if stochastic:
                wins = sum(1 for _, success in stochastic if success)
                battery.add(
                    case=f"dial {dial}",
                    test=f"binomial/step{step}/seed{seed}",
                    statistic=wins / len(stochastic),
                    pvalue=stats.binomtest(wins, len(stochastic), float(p)).pvalue,
                    sample=f"{seed}/{step}",
                )
    # the partition is invariant to the seed and to the step: the same subjects are positive
    if len(set(frozen_sets)) != 1:
        violations.append("the frozen successes differ between seeds or steps")
    rejected = battery.rejected()
    accuracy_log.record(
        family=FAMILY,
        case=f"transitions/dial/{dial}",
        criterion=(
            "frozen successes equal LRM(frozen count, p) and are the first frozen ordinals, in "
            "every (step, seed); no stochastic-share binomial rejected under Benjamini-Hochberg "
            "at q=0.01"
        ),
        passed=not rejected and not violations,
        metrics={
            "violations": violations[:5],
            "frozen": frozen_count,
            "frozen_successes": frozen_wins,
            "stochastic": n - frozen_count,
            "tests": len(battery.entries),
        },
        seed=seeds[0],
        n=n,
    )
    assert not violations, violations[:5]
    assert battery.method == "bh"
    assert not rejected, battery.summary()
