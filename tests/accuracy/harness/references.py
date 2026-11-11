# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Reference distributions for the kernel samplers, built from scipy, not from the engine.

Every reference states the engine's *declared* behaviour (KERNEL-SPEC §4)
with an independent implementation, including the parts that make a sampler
differ from the textbook distribution: quantisation onto a declared scale,
clamping onto the inward-rounded bounds, and the Poisson search cap. A
goodness-of-fit test against a reference that ignored those would either
fail a correct sampler or be too blunt to see a wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence, Tuple

import numpy as np
from scipy import stats

__all__ = [
    "GridPmf",
    "as_floats",
    "capped_poisson_pmf",
    "quantised_pmf",
    "truncated_normal",
    "unit_interval",
]


def truncated_normal(mean: float, stddev: float, lo: float, hi: float) -> stats.rv_continuous:
    """The normal truncated to ``[lo, hi]``, as a frozen scipy distribution."""
    return stats.truncnorm((lo - mean) / stddev, (hi - mean) / stddev, loc=mean, scale=stddev)


@dataclass(frozen=True)
class GridPmf:
    """A distribution over evenly spaced grid points ``lo + k * step``."""

    lo: float
    step: float
    probabilities: np.ndarray

    @property
    def points(self) -> np.ndarray:
        return self.lo + self.step * np.arange(len(self.probabilities))

    def counts(self, values: Sequence[float]) -> np.ndarray:
        """Histogram *values* onto the grid. A value off the grid is a failure, not a bin."""
        index = np.rint((np.asarray(values, dtype=float) - self.lo) / self.step).astype(np.int64)
        if index.min() < 0 or index.max() >= len(self.probabilities):
            raise AssertionError("a draw fell outside the declared grid")
        return np.bincount(index, minlength=len(self.probabilities))


def quantised_pmf(
    distribution: stats.rv_continuous, lo_q: float, hi_q: float, step: float
) -> GridPmf:
    """Round a continuous draw to the nearest grid point, then clamp to ``[lo_q, hi_q]``.

    That is ``truncated_normal``'s and ``exponential``'s declared
    post-processing (``_quantise`` then ``_clamp`` in ``core/rng.py``). Ties
    at an exact half-step have probability zero for a continuous draw, so
    the rounding mode does not enter. Everything below ``lo_q + step/2``
    lands on ``lo_q``, everything above ``hi_q - step/2`` on ``hi_q``.
    """
    count = int(round((hi_q - lo_q) / step)) + 1
    points = lo_q + step * np.arange(count)
    upper = distribution.cdf(points + step / 2)
    lower = distribution.cdf(points - step / 2)
    probabilities = upper - lower
    probabilities[0] = upper[0]
    probabilities[-1] = 1.0 - lower[-1]
    return GridPmf(lo=lo_q, step=step, probabilities=np.clip(probabilities, 0.0, 1.0))


def capped_poisson_pmf(mean: float, cap: int) -> np.ndarray:
    """The Poisson distribution with all mass at or above *cap* placed on *cap*.

    ``core.rng.poisson`` searches the CDF up to ``poisson_cap(mean)`` and
    returns the cap when it gets there, so the tail beyond it is folded onto
    the cap rather than lost.
    """
    support = np.arange(cap + 1)
    pmf = stats.poisson.pmf(support, mean)
    pmf[-1] = stats.poisson.sf(cap - 1, mean)
    return pmf


def as_floats(values: Sequence[Decimal]) -> np.ndarray:
    """Exact decimals as floats, for scipy. Analysis only: nothing here is hashed."""
    return np.array([float(value) for value in values], dtype=float)


def unit_interval(words: Sequence[int]) -> Tuple[float, ...]:
    """``uniform53`` integers as points of ``[0, 1)``."""
    return tuple(word / float(1 << 53) for word in words)
