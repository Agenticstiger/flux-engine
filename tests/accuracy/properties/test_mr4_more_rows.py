# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Metamorphic relation 4: more rows keep the marginals.

Two statements, at the generator level under one fixed root:

1. **Prefix stability (exact).** The first ``n`` rows of a ``2n``-row generation are
   the ``n``-row generation, row by row and column by column. Each cell has its own
   stream addressed by its row index, so asking for more rows cannot move the rows
   already asked for. Pass criterion: identical ``repr`` for every column.
2. **Marginals (statistical).** Two-sample Kolmogorov-Smirnov between the ``n``-row
   marginal of a numeric column and the marginal of the rows the larger generation
   *added* (rows ``n`` to ``2n - 1``). Not the whole ``2n`` sample: by (1) the ``n``
   rows are the first half of it, so ``ks_2samp`` would compare a sample with itself
   plus more, which breaks the independence the test's p-value assumes and can only
   push it towards "not rejected". The added rows are an independent draw from the
   same declaration.

   Pass criterion: across the five numeric columns, no KS test is rejected after
   Benjamini-Hochberg adjustment at ``q = 0.01`` (every column is its own sample, so
   the p-values are independent). A negative control confirms the test can reject:
   the ``amount`` marginal against the ``wait`` marginal must reject at p < 1e-6.
   Discrete columns have ties, which makes ``ks_2samp`` conservative, so a pass is
   weaker evidence there.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy", "hypothesis_jsonschema")

from typing import Dict, List  # noqa: E402

from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from scipy import stats  # noqa: E402

from flux_engine.generator.rows import generate_column  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.stats import Battery, Q  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.properties import _support as support  # noqa: E402
from tests.accuracy.properties.checks import check_prefix_stable  # noqa: E402
from tests.accuracy.properties.counterexamples import saving_counterexample  # noqa: E402

FAMILY = "properties"
NUMERIC_COLUMNS = ("count", "level", "amount", "wait", "seen_at")


def test_first_n_rows_are_the_n_row_generation(tier: Tier, accuracy_log: ResultLog) -> None:
    examples = {"n": 0}

    @tier.property_settings(pr=25, nightly=200, label="mr4-prefix")
    @given(
        seed=st.integers(min_value=0, max_value=2**62),
        n=st.integers(min_value=1, max_value=tier.pick(pr=24, nightly=120)),
    )
    def prop(seed: int, n: int) -> None:
        payload = {"seed": seed, "n": n}
        with saving_counterexample("mr4_prefix_stable", payload):
            check_prefix_stable(payload)
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr4-prefix-stable",
        criterion="the first n rows of a 2n-row generation equal the n-row generation",
        passed=True,
        metrics={"examples": examples["n"]},
    )


def test_marginals_survive_adding_rows(tier: Tier, accuracy_log: ResultLog) -> None:
    n = tier.pick(pr=1500, nightly=10_000)
    seed = tier.seed("mr4", "marginals")
    root = support.root_of(seed)
    plan = support.base_plan(2 * n)

    original: Dict[str, List[float]] = {}
    added: Dict[str, List[float]] = {}
    for column in NUMERIC_COLUMNS:
        # Nulls are dropped, so "the first n rows" is taken before the filter.
        full = generate_column(plan, column, root=root)
        original[column] = [float(v) for v in full[:n] if v is not None]
        added[column] = [float(v) for v in full[n:] if v is not None]

    battery = Battery(name="marginals of n vs added rows", q=Q)
    for column in NUMERIC_COLUMNS:
        result = stats.ks_2samp(original[column], added[column])
        battery.add(
            case=column,
            test="ks_2samp",
            statistic=float(result.statistic),
            pvalue=float(result.pvalue),
            sample=column,
        )
    control = stats.ks_2samp(original["amount"], added["wait"])

    accuracy_log.record(
        family=FAMILY,
        case="mr4-marginals-ks",
        criterion="no two-sample KS rejected by Benjamini-Hochberg at q=0.01 across the numeric columns",
        passed=not battery.rejected(),
        seed=seed,
        n=n,
        metrics={
            "method": battery.method,
            "tests": {
                d.entry.case: {"D": d.entry.statistic, "p": d.entry.pvalue, "adjusted": d.adjusted}
                for d in battery.decisions()
            },
            "negativeControlP": float(control.pvalue),
        },
    )
    assert battery.method == "bh"
    assert float(control.pvalue) < 1e-6, "the KS test cannot tell two different marginals apart"
    assert not battery.rejected(), battery.summary()
