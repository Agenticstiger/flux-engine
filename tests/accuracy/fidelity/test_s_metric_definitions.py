# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 1: the aggregate metrics equal SDMetrics' definitions evaluated on the records.

Two synthetic record sets (NumPy, stated parameters, never real data) are
reduced to S by the engine's own ``compute_statistic``. Each metric computed
from those two S values is compared with the SDMetrics formula evaluated
directly on the records, implemented here with NumPy and scipy.

Pass criteria:

* TVComplement and ContingencySimilarity equal the record-level values
  exactly, as rationals;
* KSComplement equals the record-level KS taken at the declared edges
  exactly, and the record-level two-sample KS (``scipy.stats.ks_2samp``, the
  statistic SDMetrics' KSComplement uses) is never smaller than the
  aggregate distance;
* Pearson's ``r`` from the moment sums equals NumPy's ``corrcoef`` on the
  grid-encoded values to ``1e-12``, and on the raw decimals to ``1e-6``
  (the declared grid is 2^-16).
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import math  # noqa: E402
from fractions import Fraction  # noqa: E402
from typing import Any, Dict, List, Sequence  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from scipy import stats  # noqa: E402

from flux_engine.concordance.boundary.statistic import compute_statistic  # noqa: E402
from tests.accuracy.fidelity.contracts import (  # noqa: E402
    AMOUNT_BINS,
    FRACTIONAL_BITS,
    REFERENCE,
    compile_document,
    draw_metric_rows,
    metrics_document,
)
from tests.accuracy.fidelity.s_metrics import (  # noqa: E402
    contingency_similarity,
    correlation_similarity,
    ks_complement,
    pearson_r,
    tv_complement,
)
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402

FAMILY = "fidelity-s"
EDGES = [Fraction(edge) for edge in AMOUNT_BINS]


COMPILED = compile_document(metrics_document())


def _property(statistic: Any, kind: str, group_by: Any = None) -> Any:
    """The statistic of the one declared property of *kind* grouped by *group_by*."""
    for entry in COMPILED.properties:
        declaration = entry.declaration
        if declaration.kind.value == kind and declaration.group_by == group_by:
            return statistic.properties[entry.property_id]
    raise KeyError((kind, group_by))


def _bin(value: Fraction) -> int:
    for index in range(len(EDGES) - 2):
        if value < EDGES[index + 1]:
            return index
    return len(EDGES) - 2


def _frequencies(values: Sequence[Fraction]) -> List[Fraction]:
    counts = [0] * (len(EDGES) - 1)
    for value in values:
        counts[_bin(value)] += 1
    return [Fraction(count, len(values)) for count in counts]


@pytest.fixture(scope="module")
def samples(tier: Tier) -> Dict[str, Any]:
    n = tier.pick(pr=5_000, nightly=50_000)
    reference = draw_metric_rows(REFERENCE, n, tier.seed("fidelity-s", "reference"))
    other = draw_metric_rows(
        REFERENCE.with_(mean=330.0, mix=0.05),
        n,
        tier.seed("fidelity-s", "other"),
        slope=0.07,
        noise=9.0,
    )
    return {
        "n": n,
        "rows": (reference, other),
        "statistics": tuple(compute_statistic(rows, COMPILED) for rows in (reference, other)),
    }


def _amounts(rows: Sequence[Dict[str, Any]]) -> List[Fraction]:
    return [Fraction(row["amount"]) for row in rows]


def test_tv_complement_equals_the_record_level_definition(
    samples: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    left, right = samples["statistics"]
    aggregate = tv_complement(
        _property(left, "marginalHistogram"), _property(right, "marginalHistogram")
    )
    a, b = (_frequencies(_amounts(rows)) for rows in samples["rows"])
    records = 1 - Fraction(1, 2) * sum((abs(x - y) for x, y in zip(a, b, strict=True)), Fraction(0))
    accuracy_log.record(
        family=FAMILY,
        case="TVComplement-definition",
        criterion="aggregate TVComplement equals the record-level value exactly",
        passed=aggregate == records,
        n=samples["n"],
        metrics={"aggregate": float(aggregate), "records": float(records)},
    )
    assert aggregate == records


def test_ks_complement_is_the_edge_ks_and_bounds_the_record_ks(
    samples: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    left, right = samples["statistics"]
    aggregate = ks_complement(
        _property(left, "marginalHistogram"), _property(right, "marginalHistogram")
    )
    a, b = (_amounts(rows) for rows in samples["rows"])
    at_edges = max(
        abs(
            Fraction(sum(1 for value in a if value < edge), len(a))
            - Fraction(sum(1 for value in b if value < edge), len(b))
        )
        for edge in EDGES[1:-1]
    )
    record_ks = float(stats.ks_2samp([float(v) for v in a], [float(v) for v in b]).statistic)
    passed = (1 - aggregate) == at_edges and float(1 - aggregate) <= record_ks + 1e-12
    accuracy_log.record(
        family=FAMILY,
        case="KSComplement-definition",
        criterion="aggregate KS equals the record KS at the edges; never exceeds the full record KS",
        passed=passed,
        n=samples["n"],
        metrics={
            "aggregateDistance": float(1 - aggregate),
            "edgeDistance": float(at_edges),
            "recordDistance": record_ks,
        },
    )
    assert 1 - aggregate == at_edges
    assert float(1 - aggregate) <= record_ks + 1e-12


def test_pearson_r_from_moments_matches_numpy(
    samples: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    scale = 1 << FRACTIONAL_BITS
    worst_units = worst_raw = 0.0
    for statistic, rows in zip(samples["statistics"], samples["rows"], strict=True):
        group = _property(statistic, "correlation").groups[0]
        from_moments = pearson_r(group)
        assert from_moments is not None
        x_units = np.array(
            [math.floor(Fraction(row["amount"]) * scale) for row in rows], dtype=float
        )
        y_units = np.array([math.floor(Fraction(row["fee"]) * scale) for row in rows], dtype=float)
        x_raw = np.array([float(row["amount"]) for row in rows])
        y_raw = np.array([float(row["fee"]) for row in rows])
        worst_units = max(
            worst_units, abs(float(from_moments) - np.corrcoef(x_units, y_units)[0, 1])
        )
        worst_raw = max(worst_raw, abs(float(from_moments) - np.corrcoef(x_raw, y_raw)[0, 1]))
    left, right = samples["statistics"]
    similarity = correlation_similarity(
        _property(left, "correlation"), _property(right, "correlation")
    )
    passed = worst_units <= 1e-12 and worst_raw <= 1e-6
    accuracy_log.record(
        family=FAMILY,
        case="CorrelationSimilarity-definition",
        criterion="r from moments equals numpy corrcoef to 1e-12 (encoded) and 1e-6 (raw)",
        passed=passed,
        n=samples["n"],
        metrics={
            "worstEncodedError": worst_units,
            "worstRawError": worst_raw,
            "similarity": float(similarity),
        },
    )
    assert worst_units <= 1e-12
    assert worst_raw <= 1e-6


def test_contingency_similarity_equals_the_crosstab_definition(
    samples: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    left, right = samples["statistics"]
    aggregate = contingency_similarity(
        _property(left, "marginalHistogram", "market"),
        _property(right, "marginalHistogram", "market"),
    )

    def crosstab(rows: Sequence[Dict[str, Any]]) -> Dict[Any, Fraction]:
        table: Dict[Any, int] = {}
        for row in rows:
            cell = (row["market"], _bin(Fraction(row["amount"])))
            table[cell] = table.get(cell, 0) + 1
        return {cell: Fraction(count, len(rows)) for cell, count in table.items()}

    a, b = (crosstab(rows) for rows in samples["rows"])
    cells = set(a) | set(b)
    records = 1 - Fraction(1, 2) * sum(
        (abs(a.get(cell, Fraction(0)) - b.get(cell, Fraction(0))) for cell in cells), Fraction(0)
    )
    accuracy_log.record(
        family=FAMILY,
        case="ContingencySimilarity-definition",
        criterion="aggregate ContingencySimilarity equals the record-level crosstab value exactly",
        passed=aggregate == records,
        n=samples["n"],
        metrics={"aggregate": float(aggregate), "records": float(records)},
    )
    assert aggregate == records
