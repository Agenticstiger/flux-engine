# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The statistics every accuracy family shares: false-discovery control, intervals, pooling.

A family runs many hypothesis tests at once. Read one at a time at a fixed
level, a battery of fifty tests on a correct sampler raises a false alarm
on most nights. So no family reads a single p-value against a threshold:
it collects them in a :class:`Battery`, which adjusts the whole family with
``scipy.stats.false_discovery_control`` at ``q = 0.01``:

* **Benjamini-Hochberg** when every test in the family ran on its own
  sample, so the p-values are independent;
* **Benjamini-Yekutieli** as soon as two tests share a sample (KS, CvM and
  AD on one set of draws are dependent), because BY holds under any
  dependence and BH needs independence or positive dependence.

Under the global null -- every sampler correct -- the false-discovery rate
is the family-wise error rate, so ``q = 0.01`` bounds the chance that a
correct engine turns a night red at 1% per family.

This module imports scipy. Callers guard that import with
:func:`tests.accuracy.harness.deps.require`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np
from scipy import stats

__all__ = [
    "Q",
    "Battery",
    "Decision",
    "PValue",
    "clopper_pearson",
    "ks_cvm",
    "pooled_chisquare",
]

#: The false-discovery level every family is controlled at.
Q = 0.01


@dataclass(frozen=True)
class PValue:
    """One test's outcome inside a battery."""

    case: str
    test: str
    statistic: float
    pvalue: float
    #: The sample the test ran on. Two tests naming one sample are dependent.
    sample: str


@dataclass(frozen=True)
class Decision:
    """One test after the family-wide adjustment."""

    entry: PValue
    adjusted: float
    rejected: bool


@dataclass
class Battery:
    """A family of tests whose false alarms are controlled together."""

    name: str
    q: float = Q
    entries: List[PValue] = field(default_factory=list)

    def add(self, *, case: str, test: str, statistic: float, pvalue: float, sample: str) -> None:
        """Add one test's statistic and p-value."""
        if not 0.0 <= pvalue <= 1.0 or pvalue != pvalue:
            raise ValueError(f"{case}/{test}: p-value {pvalue!r} is not in [0, 1]")
        self.entries.append(
            PValue(
                case=case,
                test=test,
                statistic=float(statistic),
                pvalue=float(pvalue),
                sample=sample,
            )
        )

    @property
    def method(self) -> str:
        """``by`` when any two tests share a sample, ``bh`` otherwise."""
        shared = Counter(entry.sample for entry in self.entries)
        return "by" if any(count > 1 for count in shared.values()) else "bh"

    def decisions(self) -> Tuple[Decision, ...]:
        """Every test with its adjusted p-value and verdict, in insertion order."""
        if not self.entries:
            return ()
        raw = np.array([entry.pvalue for entry in self.entries], dtype=float)
        adjusted = stats.false_discovery_control(raw, method=self.method)
        return tuple(
            Decision(entry=entry, adjusted=float(value), rejected=bool(value <= self.q))
            for entry, value in zip(self.entries, adjusted, strict=True)
        )

    def rejected(self) -> Tuple[Decision, ...]:
        """The tests the family-wide adjustment rejects."""
        return tuple(decision for decision in self.decisions() if decision.rejected)

    def case_rejected(self, case: str) -> bool:
        """Whether any test of *case* is rejected after the family-wide adjustment."""
        return any(decision.entry.case == case for decision in self.rejected())

    def summary(self) -> str:
        """A one-line account of the rejections, for an assertion message."""
        rejected = self.rejected()
        if not rejected:
            return f"{self.name}: no rejection among {len(self.entries)} tests ({self.method})"
        listed = ", ".join(
            f"{d.entry.case}/{d.entry.test} p={d.entry.pvalue:.3g} adj={d.adjusted:.3g}"
            for d in rejected
        )
        return f"{self.name}: {len(rejected)} of {len(self.entries)} rejected ({self.method}): {listed}"


def clopper_pearson(successes: int, trials: int, level: float = 0.95) -> Tuple[float, float]:
    """The exact (Clopper-Pearson) confidence interval for a binomial proportion."""
    if trials <= 0:
        raise ValueError("trials must be positive")
    interval = stats.binomtest(successes, trials).proportion_ci(
        confidence_level=level, method="exact"
    )
    return float(interval.low), float(interval.high)


def pooled_chisquare(
    observed: Sequence[int],
    probabilities: Sequence[float],
    *,
    min_expected: float = 5.0,
    ddof: int = 0,
) -> Tuple[float, float, int]:
    """Pearson's chi-square against declared cell probabilities, pooling thin cells.

    The chi-square approximation needs every expected count at or above about
    five. Cells below *min_expected* are merged into their neighbour, working
    inward from both tails, which keeps the cells ordered -- the right merge
    for a count or a binned continuous variable, and harmless for a
    categorical one. Returns ``(statistic, pvalue, cells)``.
    """
    counts = np.asarray(observed, dtype=float)
    probs = np.asarray(probabilities, dtype=float)
    if counts.shape != probs.shape or counts.ndim != 1:
        raise ValueError("observed and probabilities must be one-dimensional and aligned")
    total = counts.sum()
    if total <= 0:
        raise ValueError("no observations")
    probs = probs / probs.sum()
    cells: List[Tuple[float, float]] = [(c, p * total) for c, p in zip(counts, probs, strict=True)]
    cells = _merge_thin(cells, min_expected)
    cells = list(reversed(_merge_thin(list(reversed(cells)), min_expected)))
    if len(cells) < 2:
        raise ValueError("fewer than two cells survive pooling; raise n")
    f_obs = np.array([c for c, _ in cells])
    f_exp = np.array([e for _, e in cells])
    result = stats.chisquare(f_obs, f_exp=f_exp * (f_obs.sum() / f_exp.sum()), ddof=ddof)
    return float(result.statistic), float(result.pvalue), len(cells)


def _merge_thin(cells: List[Tuple[float, float]], min_expected: float) -> List[Tuple[float, float]]:
    merged: List[Tuple[float, float]] = []
    carry: Optional[Tuple[float, float]] = None
    for observed, expected in cells:
        if carry is not None:
            observed, expected = observed + carry[0], expected + carry[1]
            carry = None
        if expected < min_expected:
            carry = (observed, expected)
            continue
        merged.append((observed, expected))
    if carry is not None:
        if merged:
            last = merged.pop()
            merged.append((last[0] + carry[0], last[1] + carry[1]))
        else:
            merged.append(carry)
    return merged


def ks_cvm(
    battery: Battery, *, case: str, sample: str, draws: Iterable[float], cdf: object
) -> None:
    """Add a one-sample KS and Cramer-von Mises test against a fully specified CDF."""
    values = np.asarray(list(draws), dtype=float)
    ks = stats.kstest(values, cdf)
    battery.add(case=case, test="ks", statistic=ks.statistic, pvalue=ks.pvalue, sample=sample)
    cvm = stats.cramervonmises(values, cdf)
    battery.add(case=case, test="cvm", statistic=cvm.statistic, pvalue=cvm.pvalue, sample=sample)
