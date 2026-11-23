# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 2, numerics: the decimal Φ and Φ⁻¹ every continuous draw passes through.

``truncated_normal`` is only as accurate as ``normal_cdf`` and ``normal_ppf``.
The kernel's own tests hold them to ``1e-9`` against ``statistics.NormalDist``;
this holds them to scipy's double-precision ``ndtr`` and ``ndtri`` across the
range a draw can reach, far tails included, and measures how many digits the
pair keeps against each other.

Pass criteria, deterministic (no sampling):

* ``|Φ_engine(x) − ndtr(x)| ≤ 1e-12 · ndtr(x)`` for x on a grid over [-37, 8];
* ``|Φ⁻¹_engine(p) − ndtri(p)| ≤ 1e-12 · max(1, |ndtri(p)|)`` for p on a
  log-spaced grid over [1e-300, 0.5] and a linear grid over [0.5, 1), each p
  handed to both sides as the same exact binary value;
* the round trip ``|Φ(Φ⁻¹(p)) − p| / p`` stays within the bound measured for
  its range, below.

A relative bound of ``1e-12`` is three orders of magnitude looser than
double precision, so the first two measure the engine, not scipy's rounding.

**What the round trip measured** (2026-11-11, recorded so the bounds are
not mistaken for the kernel's declared 28 digits): about ``5e-26`` for p in
[0.001, 0.999], ``2e-23`` down to p = 1e-16, the smallest p an untruncated
``uniform53`` draw reaches, and ``2e-19`` at p = 1e-300, reachable only
under a far-tail truncation. Acklam's estimate degrades in the far tail and
one Halley step does not fully recover it. Every value is quantised to its
declared scale, at most 8 places in the shipped bundles, long before those
digits matter.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

from decimal import Decimal, localcontext  # noqa: E402
from typing import Iterable, Tuple  # noqa: E402

import numpy as np  # noqa: E402
from scipy import special  # noqa: E402

from flux_engine.core.rng import kernel_context, normal_cdf, normal_ppf  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402

FAMILY = "gof-numerics"
RELATIVE = 1e-12

#: (lowest p, highest p, bound on the relative round-trip error).
ROUND_TRIP_BOUNDS: Tuple[Tuple[str, str, str], ...] = (
    ("0.001", "0.999", "1e-25"),
    ("1e-16", "0.001", "1e-21"),
    ("1e-300", "1e-16", "1e-17"),
)


def test_normal_cdf_matches_ndtr_across_its_range(accuracy_log: ResultLog) -> None:
    grid = np.concatenate([np.linspace(-37.0, -8.0, 59), np.linspace(-8.0, 8.0, 321)])
    worst = 0.0
    for x in grid:
        engine = float(normal_cdf(Decimal(float(x))))
        reference = float(special.ndtr(x))
        worst = max(worst, abs(engine - reference) / reference)
    accuracy_log.record(
        family=FAMILY,
        case="normal_cdf",
        criterion=f"relative error against scipy.special.ndtr <= {RELATIVE:g}",
        passed=worst <= RELATIVE,
        n=len(grid),
        metrics={"worstRelativeError": worst},
    )
    assert worst <= RELATIVE


def test_normal_ppf_matches_ndtri_across_its_range(accuracy_log: ResultLog) -> None:
    # Decimal(float) is the float's exact binary value, so both sides invert
    # the same p. Converting through repr() instead would hand the engine the
    # shortest round-tripping decimal, which near p = 1 differs from the
    # float by enough to move the quantile in the fourth digit.
    lower = np.logspace(-300, np.log10(0.5), 400)
    upper = np.linspace(0.5, 1.0, 401)[1:-1]
    worst = 0.0
    for p in np.concatenate([lower, upper]):
        engine = float(normal_ppf(Decimal(float(p))))
        reference = float(special.ndtri(p))
        worst = max(worst, abs(engine - reference) / max(1.0, abs(reference)))
    accuracy_log.record(
        family=FAMILY,
        case="normal_ppf",
        criterion=f"error against scipy.special.ndtri <= {RELATIVE:g} x max(1, |x|)",
        passed=worst <= RELATIVE,
        n=len(lower) + len(upper),
        metrics={"worstScaledError": worst},
    )
    assert worst <= RELATIVE


def _decades(lowest: Decimal, highest: Decimal) -> Iterable[Decimal]:
    for exponent in range(-300, 1):
        for mantissa in (1, 2, 5):
            p = Decimal(mantissa).scaleb(exponent)
            if lowest <= p <= highest:
                yield p


def test_cdf_of_ppf_returns_p_within_its_measured_bound(accuracy_log: ResultLog) -> None:
    failures = []
    measured = {}
    for low, high, bound in ROUND_TRIP_BOUNDS:
        lowest, highest = Decimal(low), Decimal(high)
        grid = list(_decades(lowest, highest))
        if lowest >= Decimal("0.001"):
            grid = [Decimal(k) / 1000 for k in range(1, 1000)]
        worst = Decimal(0)
        for p in grid:
            back = normal_cdf(normal_ppf(p))
            with localcontext(kernel_context()):
                worst = max(worst, abs(back - p) / p)
        measured[f"[{low}, {high}]"] = {"worst": str(worst), "bound": bound, "points": len(grid)}
        if worst > Decimal(bound):
            failures.append(f"[{low}, {high}]: {worst} > {bound}")
    accuracy_log.record(
        family=FAMILY,
        case="cdf-ppf-round-trip",
        criterion="relative round-trip error within the bound measured for each range of p",
        passed=not failures,
        metrics=measured,
    )
    assert not failures, failures
