# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 2, power: the battery catches a sampler whose mean has moved by 2%.

A goodness-of-fit battery that never rejects is easy to build. This checks
the battery can see a defect of a stated size: each case that declares a
``shifted`` sampler is drawn with its mean moved by 2% (the location of a
truncated normal, the mean of an exponential, a Poisson or a Bernoulli) and
tested against the *unshifted* reference with the battery's own statistics.

Pass criterion: the shifted sample is detected in at least 90% of the
repetitions. "Detected" means its smallest p-value is at or below the
battery's rank-one critical value (``q/m`` for BH, ``q/(m*c(m))`` for BY, with
``m`` the size of the battery the case belongs to) -- the condition under
which the real battery rejects it whatever the other cases did.

The exponential and Bernoulli cases need about 250 000 draws to reach that
power, so they run in the nightly tier only, at the battery's nightly size.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import pytest  # noqa: E402

from tests.accuracy.gof.kernel_battery import (  # noqa: E402
    add_continuous,
    add_discrete,
    case_size,
    draw_case,
    rank_one_threshold,
    tests_per_case,
)
from tests.accuracy.gof.kernel_cases import CONTINUOUS, DISCRETE, ContinuousCase  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q, clopper_pearson  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402

FAMILY = "gof-power"
REQUIRED_POWER = 0.90
CRITERION = (
    "a 2% mean shift is detected in at least 90% of repetitions at the battery's "
    "rank-one critical value"
)

#: Cases the pull-request sizes already power: the 2% shift is several
#: standard errors at n = 10 000 and 20 000.
POWERED_AT_PR_SIZE = frozenset({"truncated_normal-two-sided", "poisson-20"})

SHIFTED = [case for case in CONTINUOUS + DISCRETE if case.shifted is not None]


def _battery_size(case: object) -> int:
    family = CONTINUOUS if isinstance(case, ContinuousCase) else DISCRETE
    return sum(tests_per_case(member) for member in family)


@pytest.mark.parametrize("name", [case.name for case in SHIFTED])
def test_a_two_percent_mean_shift_is_caught(name: str, tier: Tier, accuracy_log: ResultLog) -> None:
    case = next(case for case in SHIFTED if case.name == name)
    if name not in POWERED_AT_PR_SIZE:
        tier.only_nightly(f"{name} needs its nightly size, {case.n_nightly} draws, for 90% power")
    continuous = isinstance(case, ContinuousCase)
    threshold = rank_one_threshold(Q, _battery_size(case), "by" if continuous else "bh")
    repetitions = tier.pick(pr=5, nightly=20)

    smallest = []
    for repetition in range(repetitions):
        values = draw_case(case, tier, shifted=True, repetition=repetition)
        battery = Battery(name=f"{name} shifted")
        if continuous:
            add_continuous(battery, case, values, sample="shifted")  # type: ignore[arg-type]
        else:
            add_discrete(battery, case, values, sample="shifted")  # type: ignore[arg-type]
        smallest.append(min(entry.pvalue for entry in battery.entries))

    detected = sum(1 for pvalue in smallest if pvalue <= threshold)
    rate = detected / repetitions
    low, high = clopper_pearson(detected, repetitions)
    passed = rate >= REQUIRED_POWER
    accuracy_log.record(
        family=FAMILY,
        case=name,
        criterion=CRITERION,
        passed=passed,
        seed=tier.seed("gof-kernel", f"{name}-shifted", 0),
        n=case_size(case, tier),
        metrics={
            "repetitions": repetitions,
            "detected": detected,
            "power": rate,
            "powerInterval95": [low, high],
            "threshold": threshold,
            "smallestPvalues": smallest,
        },
    )
    assert passed, (
        f"{name}: a 2% shift was detected in {detected} of {repetitions} repetitions "
        f"(threshold p <= {threshold:.3g}); the battery is too weak at n={case_size(case, tier)}"
    )
