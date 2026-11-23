# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Metamorphic relation 2: the order of an expose's columns does not change any column's values.

Level: the generator, under one fixed seam digest ``H`` and one fixed seed-tree root.
Every cell has its own stream on ``("sim", sim_id, "gen", exposeId, "row", i,
column)``, a path that names the column and never its position, so permuting the
declared order must leave each column's value sequence unchanged. The check compares
by column name, row by row and column-first (``generate_column``), and also asserts
that ``generatorDigest`` moves with the order.

Pass criterion: for every Hypothesis-drawn permutation of the nine base columns,
every column's ``repr`` sequence is identical.

End to end this does not hold, by design: the seed-tree root derives from ``H``, ``H``
covers the column names *in order* (KERNEL-SPEC §3), and so reordering the contract
moves every stream. This relation is only about what the generator does once the
root is fixed.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy", "hypothesis_jsonschema")

from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.accuracy.properties import _support as support  # noqa: E402
from tests.accuracy.properties.checks import check_column_order  # noqa: E402
from tests.accuracy.properties.counterexamples import saving_counterexample  # noqa: E402

FAMILY = "properties"


def test_permuting_columns_leaves_each_column_unchanged(
    tier: Tier, accuracy_log: ResultLog
) -> None:
    rows = tier.pick(pr=6, nightly=24)
    examples = {"n": 0}

    @tier.property_settings(pr=40, nightly=400, label="mr2-column-order")
    @given(
        order=st.permutations(list(support.base_names())),
        seed=st.integers(min_value=0, max_value=2**62),
    )
    def prop(order: list, seed: int) -> None:
        payload = {"seed": seed, "rows": rows, "order": list(order)}
        with saving_counterexample("mr2_column_order", payload):
            check_column_order(payload)
        examples["n"] += 1

    prop()
    accuracy_log.record(
        family=FAMILY,
        case="mr2-column-order",
        criterion="every column's values identical under any permutation of the column order",
        passed=True,
        n=rows,
        metrics={"examples": examples["n"], "columns": len(support.base_names())},
    )
