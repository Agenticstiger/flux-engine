# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Turn a case's draws into test statistics, against its stated reference.

Shared by the battery tests and the power test, so that "the battery" means
the same statistics in both: a power check that ran a different test from
the one the nightly gate runs would measure the wrong thing.
"""

from __future__ import annotations

import itertools
import math
from decimal import Decimal
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from scipy import stats

from flux_engine.core.rng import poisson_cap
from tests.accuracy.gof.kernel_cases import ContinuousCase, DiscreteCase
from tests.accuracy.harness.draws import draw_many
from tests.accuracy.harness.references import (
    capped_poisson_pmf,
    quantised_pmf,
    truncated_normal,
)
from tests.accuracy.harness.stats import Battery, pooled_chisquare
from tests.accuracy.harness.tier import Tier

__all__ = [
    "add_continuous",
    "add_discrete",
    "case_size",
    "draw_case",
    "rank_one_threshold",
    "reference_cdf",
    "tests_per_case",
]


def case_size(case: Any, tier: Tier) -> int:
    """The case's sample size at *tier*."""
    return int(tier.pick(pr=case.n_pr, nightly=case.n_nightly))


def draw_case(case: Any, tier: Tier, *, shifted: bool = False, repetition: int = 0) -> List[Any]:
    """Draw the case's sample at *tier*, on sibling streams seeded for it."""
    sampler = case.shifted if shifted else case.draw
    if sampler is None:
        raise ValueError(f"{case.name} has no shifted sampler")
    label = f"{case.name}{'-shifted' if shifted else ''}"
    seed = tier.seed("gof-kernel", label, repetition)
    return draw_many(label, seed, case_size(case, tier), sampler)


def reference_cdf(reference: Tuple[Any, ...]) -> Any:
    """The fully specified CDF a continuous case is tested against."""
    kind = reference[0]
    if kind == "uniform":
        return stats.uniform(0.0, 1.0).cdf
    if kind == "truncnorm":
        _, mean, sd, lo, hi = reference
        return truncated_normal(mean, sd, lo, hi).cdf
    if kind == "expon":
        return stats.expon(scale=1.0 / reference[1]).cdf
    raise ValueError(f"unknown continuous reference {kind!r}")


def tests_per_case(case: Any) -> int:
    """How many statistics the battery computes for *case*."""
    if isinstance(case, ContinuousCase):
        return 3 if case.anderson else 2
    return 1


def add_continuous(
    battery: Battery, case: ContinuousCase, values: Sequence[Any], sample: str
) -> None:
    """KS and Cramer-von Mises against the reference; composite AD where it applies.

    scipy's composite Anderson-Darling test estimates the parameters and,
    with ``method="interpolate"``, reports a p-value interpolated from its
    published tables and clipped to [0.01, 0.15]. Clipping only ever
    overstates a p-value, so it enters the false-discovery control as a
    conservative bound: valid, but on its own it cannot raise an alarm at
    ``q = 0.01``. The family's power comes from KS and CvM.
    """
    draws = np.array([float(value) for value in values], dtype=float)
    cdf = reference_cdf(case.reference)
    ks = stats.kstest(draws, cdf)
    battery.add(case=case.name, test="ks", statistic=ks.statistic, pvalue=ks.pvalue, sample=sample)
    cvm = stats.cramervonmises(draws, cdf)
    battery.add(
        case=case.name, test="cvm", statistic=cvm.statistic, pvalue=cvm.pvalue, sample=sample
    )
    if case.anderson:
        ad = stats.anderson(draws, dist=case.anderson, method="interpolate")
        battery.add(
            case=case.name,
            test="ad",
            statistic=ad.statistic,
            pvalue=min(1.0, max(0.0, float(ad.pvalue))),
            sample=sample,
        )


def _permutation_rank(values: Sequence[Any], n: int) -> np.ndarray:
    index: Dict[Tuple[int, ...], int] = {
        perm: rank for rank, perm in enumerate(itertools.permutations(range(n)))
    }
    return np.array([index[tuple(value)] for value in values], dtype=np.int64)


def _discrete_counts(case: DiscreteCase, values: Sequence[Any]) -> Tuple[np.ndarray, np.ndarray]:
    """Observed counts and reference probabilities, cell for cell."""
    reference = case.reference
    kind = reference[0]
    if kind == "grid-truncnorm":
        _, mean, sd, lo, hi, step = reference
        pmf = quantised_pmf(truncated_normal(mean, sd, lo, hi), lo, hi, step)
        return pmf.counts([float(value) for value in values]), pmf.probabilities
    if kind == "poisson":
        mean = reference[1]
        cap = poisson_cap(Decimal(str(mean)))
        probabilities = capped_poisson_pmf(mean, cap)
        observed = np.bincount(np.asarray(values, dtype=np.int64), minlength=cap + 1)
        if len(observed) != cap + 1:
            raise AssertionError(f"{case.name}: a draw exceeded the documented cap {cap}")
        return observed, probabilities
    if kind == "categorical":
        levels = sorted(reference[1])
        position = {level: index for index, level in enumerate(levels)}
        observed = np.bincount(
            np.array([position[value] for value in values]), minlength=len(levels)
        )
        return observed, np.array([reference[1][level] for level in levels], dtype=float)
    if kind == "uniform-int":
        bound = reference[1]
        observed = np.bincount(np.asarray(values, dtype=np.int64), minlength=bound)
        if len(observed) != bound:
            raise AssertionError(f"{case.name}: a draw fell outside [0, {bound})")
        return observed, np.full(bound, 1.0 / bound)
    if kind == "residue":
        _, bound, modulus = reference
        if any(not 0 <= value < bound for value in values):
            raise AssertionError(f"{case.name}: a draw fell outside [0, {bound})")
        observed = np.bincount(
            np.array([value % modulus for value in values], dtype=np.int64), minlength=modulus
        )
        return observed, np.full(modulus, 1.0 / modulus)
    if kind == "permutation":
        n = reference[1]
        cells = math.factorial(n)
        observed = np.bincount(_permutation_rank(values, n), minlength=cells)
        return observed, np.full(cells, 1.0 / cells)
    raise ValueError(f"unknown discrete reference {kind!r}")


def add_discrete(battery: Battery, case: DiscreteCase, values: Sequence[Any], sample: str) -> None:
    """An exact binomial test for a Bernoulli; Pearson's chi-square, pooled, otherwise."""
    if case.reference[0] == "bernoulli":
        successes = sum(1 for value in values if value)
        result = stats.binomtest(successes, len(values), case.reference[1])
        battery.add(
            case=case.name,
            test="binomial",
            statistic=successes / len(values),
            pvalue=result.pvalue,
            sample=sample,
        )
        return
    observed, probabilities = _discrete_counts(case, values)
    statistic, pvalue, _cells = pooled_chisquare(observed, probabilities)
    battery.add(case=case.name, test="chi2", statistic=statistic, pvalue=pvalue, sample=sample)


def rank_one_threshold(q: float, tests: int, method: str) -> float:
    """The p-value at or below which the step-up procedure rejects whatever else is in the family.

    The smallest critical value is ``q / m`` for Benjamini-Hochberg and
    ``q / (m * c(m))``, with ``c(m) = 1 + 1/2 + ... + 1/m``, for
    Benjamini-Yekutieli. A p-value at or below it is rejected regardless of
    the other ``m - 1`` p-values, so a power check that uses it never credits
    the battery with a detection the real battery might not make.
    """
    if method == "bh":
        return q / tests
    if method == "by":
        harmonic = sum(1.0 / index for index in range(1, tests + 1))
        return q / (tests * harmonic)
    raise ValueError(f"unknown method {method!r}")
