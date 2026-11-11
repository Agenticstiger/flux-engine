# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Metamorphic relation 3: scaling a dwell distribution scales the output.

Three statements, each at the level where it holds and each with its own bound.

1. **Exponential scale.** On one stream (same raw word), ``exponential(rate=r/c)``
   equals ``c * exponential(rate=r)``. Level: the kernel sampler. Bound: both draws
   are rounded to ``10**-scale``, so ``|x_slow - c * x_fast| <= (1 + c) * 0.5 *
   10**-scale`` (plus 1e-30 relative, for the working precision).
2. **Truncated-normal location-scale.** ``truncated_normal(mean * b + a, sd * b, lo * b +
   a, hi * b + a)`` equals ``b * truncated_normal(mean, sd, lo, hi) + a``. Level: the
   kernel sampler. Bound: ``(1 + b) * 10**-scale``, one unit per side rather than half,
   because the declared bounds are rounded inward onto the grid and a clamped value can
   move by up to a unit. Vacuous (not counted) when exactly one side refuses because
   the inward-rounded interval is empty.
3. **Arrival gaps.** ``emit_signals`` with ``ArrivalModel(gap_mean_ms * 2)``: each
   within-burst gap ``max(int(exponential(...)), 1)`` is within 1 ms of twice the
   original, the emitters, burst starts and payloads are identical, and a burst only
   gets shorter. Level: the signal stage on ``tests/fixtures/telco-fidelity-small``.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy", "hypothesis_jsonschema")

from decimal import Decimal  # noqa: E402

import pytest  # noqa: E402
from hypothesis import assume, given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.properties.checks import (  # noqa: E402
    check_arrival_gap_scaling,
    check_exponential_scaling,
    check_truncated_normal_equivariance,
)
from tests.accuracy.properties.counterexamples import saving_counterexample  # noqa: E402

FAMILY = "properties"


def _decimal(units: st.SearchStrategy, places: int) -> st.SearchStrategy:
    """A decimal string ``units / 10**places``, drawn exactly."""
    return st.builds(lambda u, p: str(Decimal(u).scaleb(-p)), units, st.integers(0, places))


ENTROPY = st.integers(min_value=0, max_value=2**256 - 1)
POSITIVE = _decimal(st.integers(1, 10**6), 6)
SCALE = st.integers(min_value=0, max_value=8)
FACTOR = st.one_of(
    st.just("2"),
    st.builds(lambda n, d: str(Decimal(n) / Decimal(d)), st.integers(1, 64), st.integers(1, 16)),
)


def test_exponential_rate_scaling(tier: Tier, accuracy_log: ResultLog) -> None:
    examples = {"n": 0}

    @tier.property_settings(pr=150, nightly=5000, label="mr3-exponential")
    @given(entropy=ENTROPY, rate=POSITIVE, c=FACTOR, scale=SCALE)
    def prop(entropy: int, rate: str, c: str, scale: int) -> None:
        payload = {"entropy": entropy, "rate": rate, "c": c, "scale": scale}
        with saving_counterexample("mr3_exponential_scaling", payload):
            check_exponential_scaling(payload)
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr3-exponential-scaling",
        criterion="|exp(rate/c) - c*exp(rate)| <= (1+c)*0.5*10^-scale on one stream",
        passed=True,
        metrics={"examples": examples["n"]},
    )


def test_truncated_normal_location_scale(tier: Tier, accuracy_log: ResultLog) -> None:
    counted = {"checked": 0, "vacuous": 0}

    @tier.property_settings(pr=150, nightly=5000, label="mr3-truncated-normal")
    @given(
        entropy=ENTROPY,
        mean=_decimal(st.integers(-10_000, 10_000), 4),
        stddev=_decimal(st.integers(0, 10_000), 4),
        lo=_decimal(st.integers(-10_000, 10_000), 4),
        width=_decimal(st.integers(1, 20_000), 4),
        a=_decimal(st.integers(-10_000, 10_000), 4),
        b=_decimal(st.integers(1, 1_000), 3),
        scale=st.integers(min_value=0, max_value=6),
    )
    def prop(
        entropy: int, mean: str, stddev: str, lo: str, width: str, a: str, b: str, scale: int
    ) -> None:
        low = Decimal(lo)
        payload = {
            "entropy": entropy,
            "mean": mean,
            "stddev": stddev,
            "lo": lo,
            "hi": str(low + Decimal(width)),
            "a": a,
            "b": b,
            "scale": scale,
        }
        with saving_counterexample("mr3_truncated_normal_equivariance", payload):
            checked = check_truncated_normal_equivariance(payload)
        counted["checked" if checked else "vacuous"] += 1
        assume(checked)

    prop()
    assert counted["checked"] > 0
    accuracy_log.record(
        family=FAMILY,
        case="mr3-truncated-normal-location-scale",
        criterion="|tn(mean*b+a, sd*b, lo*b+a, hi*b+a) - (b*tn(mean, sd, lo, hi) + a)| <= (1+b)*10^-scale",
        passed=True,
        metrics=counted,
    )


@pytest.mark.parametrize("gap_mean_ms", ["60000", "300000", "1800000"])
def test_doubling_the_gap_mean_doubles_every_gap(
    gap_mean_ms: str, tier: Tier, accuracy_log: ResultLog
) -> None:
    if gap_mean_ms == "1800000":
        tier.only_nightly("a third arrival model adds two more runs of the signal stage")
    size = tier.pick(pr=200, nightly=600)
    seed = tier.seed("mr3-arrival", gap_mean_ms)
    payload = {"seed": seed, "size": size, "gap_mean_ms": gap_mean_ms, "factor": 2}
    with saving_counterexample("mr3_arrival_gap_scaling", payload):
        compared = check_arrival_gap_scaling(payload)
    assert compared, "no burst had two occurrences, so no gap was compared"
    accuracy_log.record(
        family=FAMILY,
        case=f"mr3-arrival-gaps-{gap_mean_ms}",
        criterion="each gap within 1 ms of twice the original; emitters, starts, payloads equal",
        passed=True,
        seed=seed,
        n=size,
    )
