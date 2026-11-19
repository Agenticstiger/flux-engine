# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 2, kernel level: the continuous samplers fit their declared distributions.

Pass criterion: across the whole continuous battery (every case in
``kernel_cases.CONTINUOUS``, KS and Cramer-von Mises on each, composite
Anderson-Darling where it applies), no test is rejected after
Benjamini-Yekutieli adjustment at ``q = 0.01``. BY because the statistics of
one case share its sample.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import pytest  # noqa: E402

from tests.accuracy.gof.kernel_battery import add_continuous, case_size, draw_case  # noqa: E402
from tests.accuracy.gof.kernel_cases import CONTINUOUS  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402

FAMILY = "gof-kernel"
CRITERION = "no statistic rejected by Benjamini-Yekutieli at q=0.01 across the continuous battery"


@pytest.fixture(scope="module")
def battery(tier: Tier) -> Battery:
    battery = Battery(name="kernel samplers, continuous", q=Q)
    for case in CONTINUOUS:
        add_continuous(battery, case, draw_case(case, tier), sample=case.name)
    return battery


@pytest.mark.parametrize("name", [case.name for case in CONTINUOUS])
def test_continuous_sampler_fits_its_declared_distribution(
    name: str, battery: Battery, tier: Tier, accuracy_log: ResultLog
) -> None:
    case = next(case for case in CONTINUOUS if case.name == name)
    decisions = [d for d in battery.decisions() if d.entry.case == name]
    rejected = [d for d in decisions if d.rejected]
    accuracy_log.record(
        family=FAMILY,
        case=name,
        criterion=CRITERION,
        passed=not rejected,
        seed=tier.seed("gof-kernel", name, 0),
        n=case_size(case, tier),
        metrics={
            "method": battery.method,
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
    assert battery.method == "by"
    assert not rejected, battery.summary()
