# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 1: the engine's own output, scored against the S it was declared from.

This is ADR 0003's aggregate-only loop, end to end:

1. a synthetic committed record set (NumPy, stated parameters, never real
   data) is reduced to S by the engine;
2. the generator's declared statistics are read off S and nothing else --
   ``binWeights`` from the histogram's bin counts, the market ``levels``
   from the grouped histogram's group sizes;
3. the engine's generator produces D' from those declarations;
4. D' is reduced to S' on the same grid, and the four SDMetrics-style
   scores compare S' with S.

Pass criteria:

* **TVComplement** and **KSComplement**: the distance ``1 - score`` is at most
  three times the expected sampling distance of an ``n'``-row sample,
  ``E = 1/2 * sum sqrt(2/pi) * sqrt(p_i * (1 - p_i) / n')``, because D' is
  drawn from S's own bin weights;
* **ContingencySimilarity** with independent columns: at most three times
  the expected two-sample distance over the (market, bin) cells;
* **Agreement with the fidelity check**: the gate's ``within`` for the
  histogram and the mean property equals ``deviation <= margin`` computed
  from the two S values exactly.

A second case gives the committed markets different amount means. The
generator draws columns independently -- it has no joint structure yet --
so its contingency score is capped by the committed table's own distance
from independence. That cap is *measured*: the score must sit within three
sampling distances of it, so the shortfall is shown to be the independence
assumption and nothing else.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import math  # noqa: E402
from decimal import Decimal  # noqa: E402
from fractions import Fraction  # noqa: E402
from typing import Any, Dict, List, Mapping, Sequence  # noqa: E402

import pytest  # noqa: E402

from flux_engine.concordance.boundary.statistic import UNGROUPED, compute_statistic  # noqa: E402
from flux_engine.concordance.verifier import ClearStatistic  # noqa: E402
from flux_engine.core.seedtree import derive_root, digest_bytes  # noqa: E402
from flux_engine.generator.rows import build_plan, generate_rows  # noqa: E402
from tests.accuracy.fidelity.contracts import (  # noqa: E402
    AMOUNT_BINS,
    EXPOSE_ID,
    MARKETS,
    REFERENCE,
    committed_state,
    compile_document,
    contract_document,
    draw_rows,
)
from tests.accuracy.fidelity.s_metrics import (  # noqa: E402
    contingency_similarity,
    ks_complement,
    tv_complement,
)
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.generator.conftest import expose_from_schema  # noqa: E402

FAMILY = "fidelity-s"
SPREAD = 3.0


def _by(compiled: Any, kind: str, group_by: Any = None) -> str:
    for entry in compiled.properties:
        if entry.declaration.kind.value == kind and entry.declaration.group_by == group_by:
            return str(entry.property_id)
    raise KeyError((kind, group_by))


def _levels(statistic: Any, grouped_id: str) -> Dict[str, str]:
    """Market weights from the grouped histogram's group sizes -- an aggregate of S."""
    groups = statistic.properties[grouped_id].groups
    total = sum(group.k for group in groups)
    shares = {group.group: Fraction(group.k, total) for group in groups}
    names = sorted(shares)
    rendered = {
        name: Decimal(shares[name].numerator) / Decimal(shares[name].denominator) for name in names
    }
    rendered = {name: value.quantize(Decimal("1e-20")) for name, value in rendered.items()}
    rendered[names[-1]] = Decimal(1) - sum((rendered[name] for name in names[:-1]), Decimal(0))
    return {name: str(value) for name, value in rendered.items()}


def _expected_tv(frequencies: Sequence[Fraction], *inverse_sizes: float) -> float:
    spread = sum(inverse_sizes)
    return 0.5 * sum(
        math.sqrt(2 / math.pi) * math.sqrt(float(p) * (1 - float(p)) * spread) for p in frequencies
    )


def _loop(rows: List[Dict[str, Any]], n_synthetic: int, seed: int) -> Dict[str, Any]:
    first = compile_document(contract_document())
    statistic = compute_statistic(rows, first)
    hist_id = _by(first, "marginalHistogram")
    grouped_id = _by(first, "marginalHistogram", "market")
    generator = {
        "amount": {
            "bins": list(AMOUNT_BINS),
            "binWeights": [
                str(count) for count in statistic.properties[hist_id].groups[0].bin_counts
            ],
        },
        "market": {"levels": _levels(statistic, grouped_id)},
    }
    document = contract_document(generator=generator)
    compiled = compile_document(document)
    committed = committed_state(rows, compiled)
    assert (
        committed.statistic.to_document() == statistic.to_document()
    ), "S must not depend on the generator-side declarations"

    expose = expose_from_schema(
        document["contract"]["exposes"][0]["contract"]["schema"], expose_id=EXPOSE_ID
    )
    plan = build_plan(
        expose,
        sim_id="accuracy",
        seam_digest=compiled.h.value,
        rows=n_synthetic,
        fidelity=document["extension"]["fidelity"],
    )
    root = derive_root(digest_bytes(compiled.h.value), seed)
    names = [column.column.name for column in plan.columns]
    synthetic = [dict(zip(names, values, strict=True)) for values in generate_rows(plan, root=root)]
    synthetic_statistic = compute_statistic(synthetic, compiled)
    evaluation = ClearStatistic(
        compiled=compiled, committed=committed, synthetic_rows=synthetic
    ).evaluate()
    return {
        "compiled": compiled,
        "committed": committed.statistic,
        "synthetic": synthetic_statistic,
        "evaluation": evaluation,
        "ids": {"hist": hist_id, "grouped": grouped_id, "mean": _by(compiled, "mean")},
    }


@pytest.fixture(scope="module")
def independent(tier: Tier) -> Dict[str, Any]:
    k = tier.pick(pr=5_000, nightly=50_000)
    n = tier.pick(pr=5_000, nightly=100_000)
    rows = draw_rows(REFERENCE, k, tier.seed("fidelity-engine", "committed"))
    return {"k": k, "n": n, **_loop(rows, n, tier.seed("fidelity-engine", "generator"))}


def _frequencies(statistic: Any, property_id: str, group: str = UNGROUPED) -> List[Fraction]:
    entry = statistic.properties[property_id].group(group)
    return [Fraction(count, entry.k) for count in entry.bin_counts]


def test_histogram_scores_stay_within_the_sampling_distance(
    independent: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    ids = independent["ids"]
    committed, synthetic = independent["committed"], independent["synthetic"]
    tvc = tv_complement(synthetic.properties[ids["hist"]], committed.properties[ids["hist"]])
    ksc = ks_complement(synthetic.properties[ids["hist"]], committed.properties[ids["hist"]])
    expected = _expected_tv(_frequencies(committed, ids["hist"]), 1 / independent["n"])
    bound = SPREAD * expected
    passed = float(1 - tvc) <= bound and float(1 - ksc) <= bound
    accuracy_log.record(
        family=FAMILY,
        case="engine-histogram-scores",
        criterion="1 - TVComplement and 1 - KSComplement <= 3 x expected sampling distance",
        passed=passed,
        n=independent["n"],
        metrics={
            "TVComplement": float(tvc),
            "KSComplement": float(ksc),
            "expectedDistance": expected,
            "bound": bound,
            "committedRecords": independent["k"],
        },
    )
    assert float(1 - tvc) <= bound, (float(tvc), bound)
    assert float(1 - ksc) <= bound, (float(ksc), bound)


def test_contingency_score_with_independent_columns(
    independent: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    ids = independent["ids"]
    committed, synthetic = independent["committed"], independent["synthetic"]
    score = contingency_similarity(
        synthetic.properties[ids["grouped"]], committed.properties[ids["grouped"]]
    )
    groups = committed.properties[ids["grouped"]].groups
    total = sum(group.k for group in groups)
    cells = [Fraction(count, total) for group in groups for count in group.bin_counts]
    expected = _expected_tv(cells, 1 / independent["k"], 1 / independent["n"])
    passed = float(1 - score) <= SPREAD * expected
    accuracy_log.record(
        family=FAMILY,
        case="engine-contingency-independent",
        criterion="1 - ContingencySimilarity <= 3 x expected two-sample distance",
        passed=passed,
        n=independent["n"],
        metrics={"ContingencySimilarity": float(score), "expectedDistance": expected},
    )
    assert passed, (float(score), expected)


def test_the_fidelity_check_agrees_with_the_metrics(
    independent: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    ids = independent["ids"]
    compiled = independent["compiled"]
    committed, synthetic = independent["committed"], independent["synthetic"]
    margins = {entry.property_id: entry.margin for entry in compiled.properties}
    outcomes = {
        outcome.property_id: outcome.within for outcome in independent["evaluation"].outcomes
    }

    distance = 1 - tv_complement(
        synthetic.properties[ids["hist"]], committed.properties[ids["hist"]]
    )
    c_mean = committed.properties[ids["mean"]].groups[0]
    s_mean = synthetic.properties[ids["mean"]].groups[0]
    reference = Fraction(c_mean.sum_x, c_mean.k)
    relative = abs(Fraction(s_mean.sum_x, s_mean.k) - reference) / abs(reference)

    expected = {
        ids["hist"]: distance <= margins[ids["hist"]],
        ids["mean"]: relative <= margins[ids["mean"]],
    }
    actual = {key: outcomes[key] for key in expected}
    accuracy_log.record(
        family=FAMILY,
        case="engine-gate-agreement",
        criterion="the fidelity check's verdicts equal deviation <= margin computed from S",
        passed=actual == expected,
        n=independent["n"],
        metrics={
            "histogramDistance": float(distance),
            "meanRelativeDeviation": float(relative),
            "verdicts": actual,
        },
    )
    assert actual == expected


def _with_market_effect(
    rows: List[Dict[str, Any]], offsets: Mapping[str, float]
) -> List[Dict[str, Any]]:
    shifted = []
    for row in rows:
        amount = min(1000.0, max(1.0, float(row["amount"]) + offsets[row["market"]]))
        shifted.append({**row, "amount": f"{amount:.4f}"})
    return shifted


def test_contingency_shortfall_is_the_independence_assumption(
    tier: Tier, accuracy_log: ResultLog
) -> None:
    k = tier.pick(pr=5_000, nightly=50_000)
    n = tier.pick(pr=5_000, nightly=100_000)
    offsets = {"DE": -40.0, "FR": 0.0, "GB": 40.0, "IE": 120.0}
    assert set(offsets) == {name for name, _ in MARKETS}
    rows = _with_market_effect(
        draw_rows(REFERENCE, k, tier.seed("fidelity-engine", "dependent")), offsets
    )
    loop = _loop(rows, n, tier.seed("fidelity-engine", "dependent-generator"))
    grouped = loop["ids"]["grouped"]
    committed_groups = loop["committed"].properties[grouped].groups
    total = sum(group.k for group in committed_groups)
    joint = {
        (group.group, index): Fraction(count, total)
        for group in committed_groups
        for index, count in enumerate(group.bin_counts)
    }
    market_share = {group.group: Fraction(group.k, total) for group in committed_groups}
    bins = len(AMOUNT_BINS) - 1
    bin_share = [
        sum((joint[(group.group, index)] for group in committed_groups), Fraction(0))
        for index in range(bins)
    ]
    ceiling = 1 - Fraction(1, 2) * sum(
        (
            abs(joint[(market, index)] - market_share[market] * bin_share[index])
            for market, index in joint
        ),
        Fraction(0),
    )
    score = contingency_similarity(
        loop["synthetic"].properties[grouped], loop["committed"].properties[grouped]
    )
    noise = _expected_tv(list(joint.values()), 1 / k, 1 / n)
    passed = abs(float(score) - float(ceiling)) <= SPREAD * noise
    accuracy_log.record(
        family=FAMILY,
        case="engine-contingency-dependent",
        criterion="ContingencySimilarity within 3 x sampling distance of the independence ceiling",
        passed=passed,
        n=n,
        metrics={
            "ContingencySimilarity": float(score),
            "independenceCeiling": float(ceiling),
            "expectedDistance": noise,
        },
    )
    assert passed, (float(score), float(ceiling), noise)


def test_histogram_family_mean_is_the_within_bin_uniform_mean(
    independent: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    """The histogram family keeps bin masses, not the mean: it draws uniformly inside each bin.

    Declared from bins and weights alone, the generator's mean is
    ``sum w_i * (low_i + top_i) / 2``, where ``top_i`` is the last grid point
    below the next edge (the last bin is closed at the declared maximum).
    With the coarse bins here that moves the mean well past the mean
    property's 2% tolerance, and the fidelity check withholds on it. The
    criterion is that the synthetic mean sits within four standard errors of
    that prediction -- the whole shift is the declared within-bin shape.
    """
    ids = independent["ids"]
    committed, synthetic = independent["committed"], independent["synthetic"]
    weights = _frequencies(committed, ids["hist"])
    step = Fraction(1, 10_000)
    edges = [Fraction(edge) for edge in AMOUNT_BINS]
    tops = [edges[index + 1] - step for index in range(len(edges) - 2)] + [edges[-1]]
    lows = edges[:-1]
    mids = [(low + top) / 2 for low, top in zip(lows, tops, strict=True)]
    predicted = sum((w * m for w, m in zip(weights, mids, strict=True)), Fraction(0))
    second = sum(
        (
            w * (m * m + (top - low) ** 2 / 12)
            for w, m, low, top in zip(weights, mids, lows, tops, strict=True)
        ),
        Fraction(0),
    )
    standard_error = math.sqrt(float(second - predicted * predicted) / independent["n"])
    scale = 1 << 16
    s_mean = synthetic.properties[ids["mean"]].groups[0]
    c_mean = committed.properties[ids["mean"]].groups[0]
    observed = Fraction(s_mean.sum_x, s_mean.k * scale)
    committed_value = Fraction(c_mean.sum_x, c_mean.k * scale)
    passed = abs(float(observed - predicted)) <= 4 * standard_error
    accuracy_log.record(
        family=FAMILY,
        case="engine-histogram-family-mean",
        criterion="synthetic mean within 4 standard errors of the within-bin uniform prediction",
        passed=passed,
        n=independent["n"],
        metrics={
            "syntheticMean": float(observed),
            "predictedMean": float(predicted),
            "committedMean": float(committed_value),
            "relativeShiftFromCommitted": float(abs(observed - committed_value) / committed_value),
            "standardError": standard_error,
        },
    )
    assert passed, (float(observed), float(predicted), standard_error)
