# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 1, a categorical pair: market given channel, declared from S and checked by the gate.

A synthetic committed record set (NumPy, stated parameters, never real data)
draws market given channel. The engine reduces it to S, whose two properties
are a ``levelHistogram`` of market grouped by channel (the two-way count
table) and a ``levelHistogram`` of channel. Everything the generator is then
declared with is read off that S:

* the (channel, market) table, by ``pair_table_from_level_groups``;
* the link, by the Chow-Liu tree ``suggest_tree`` picks from that table;
* the declarations, by ``declarations``: ``levels`` for channel and, for the
  conditional draw, ``levelWeightsGiven`` for market.

Two generators are compared on the same rows and seeds: independent, with
each column drawn from its own one-way margin, and conditional, with market
drawn given channel.

Pass criteria:

* the suggested tree is the true link, channel -> market;
* conditional: the fidelity check finds both properties within, and
  ``1 - ContingencySimilarity`` is at most three one-sample sampling
  distances, because D' is drawn from S's own table;
* independent: the check withholds the emit, because the grouped property is
  outside while the channel property is within, and ContingencySimilarity
  sits within three two-sample distances of the table's independence ceiling;
* the fixture discriminates: the ceiling lies further below 1 than three
  one-sample distances;
* for both, every group's verdict equals ``TV <= margin`` computed from the two
  S values exactly, and ContingencySimilarity computed from S equals the
  record-level crosstab value exactly.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import copy  # noqa: E402
from collections import Counter  # noqa: E402
from fractions import Fraction  # noqa: E402
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from flux_engine.calibrate.structure import (  # noqa: E402
    declarations,
    pair_table_from_level_groups,
    suggest_tree,
)
from flux_engine.concordance.boundary.statistic import compute_statistic  # noqa: E402
from flux_engine.concordance.verifier import ClearStatistic  # noqa: E402
from flux_engine.core.seedtree import derive_root, digest_bytes  # noqa: E402
from flux_engine.generator.rows import build_plan, generate_rows  # noqa: E402
from tests.accuracy.fidelity.contracts import (  # noqa: E402
    EXPOSE_ID,
    committed_state,
    compile_document,
)
from tests.accuracy.fidelity.contracts import expected_total_variation as _expected_tv  # noqa: E402
from tests.accuracy.fidelity.s_metrics import contingency_similarity  # noqa: E402
from tests.accuracy.harness.record import ResultLog  # noqa: E402
from tests.accuracy.harness.tier import Tier  # noqa: E402
from tests.generator.conftest import expose_from_schema  # noqa: E402

FAMILY = "fidelity-s"
SPREAD = 3.0
CHANNELS: Dict[str, float] = {"store": 0.4, "web": 0.6}
MARKET_GIVEN: Dict[str, Dict[str, float]] = {
    "store": {"DE": 0.1, "FR": 0.2, "GB": 0.3, "IE": 0.4},
    "web": {"DE": 0.5, "FR": 0.3, "GB": 0.15, "IE": 0.05},
}
MARKETS: Tuple[str, ...] = tuple(sorted(MARKET_GIVEN["store"]))
GROUPED = "000:levelHistogram:market@channel"
CHANNEL = "001:levelHistogram:channel"
SCHEMA: List[Dict[str, Any]] = [
    {"name": "channel", "type": "STRING", "required": True},
    {"name": "market", "type": "STRING", "required": True},
]


def _tolerance(value: str) -> Dict[str, str]:
    return {"type": "distance", "measure": "totalVariation", "max": value}


def _document(generator: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """The pair's contract; *generator* adds the generator-side column keys, which sit in ``G``."""
    columns: Dict[str, Any] = {
        "channel": {"support": {"values": sorted(CHANNELS)}, "maxCardinality": 4, "null": "fail"},
        "market": {"support": {"values": list(MARKETS)}, "maxCardinality": 8, "null": "fail"},
    }
    for name, extra in (generator or {}).items():
        columns[name] = {**columns[name], **copy.deepcopy(dict(extra))}
    return {
        "contract": {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "accuracy.gold.pair",
            "exposes": [{"exposeId": EXPOSE_ID, "kind": "stream", "contract": {"schema": SCHEMA}}],
        },
        "emit": {
            "productRef": "accuracy.gold.pair",
            "exposeId": EXPOSE_ID,
            "fluidVersion": "^0.7.3",
        },
        "extension": {
            "rows": 1000,
            "fidelity": {
                "encoding": {"fractionalBits": 16, "rounding": "floor"},
                "records": {"min": 100, "max": 1_000_000},
                "columns": columns,
                "properties": [
                    {
                        "property": "levelHistogram",
                        "column": "market",
                        "groupBy": "channel",
                        "tolerance": _tolerance("0.1"),
                    },
                    {
                        "property": "levelHistogram",
                        "column": "channel",
                        "tolerance": _tolerance("0.05"),
                    },
                ],
                "budget": {"maxChecksPerState": 1_000_000, "maxStateAge": "P30D"},
                "failureAction": "withholdBundle",
            },
        },
        "expose": {"exposeId": EXPOSE_ID, "columns": ["channel", "market"]},
    }


def _committed(k: int, seed: int) -> List[Dict[str, str]]:
    rng = np.random.default_rng(seed)
    channels = sorted(CHANNELS)
    drawn = rng.choice(channels, size=k, p=[CHANNELS[name] for name in channels])
    rows: List[Dict[str, str]] = []
    for channel in channels:
        count = int(np.count_nonzero(drawn == channel))
        weights = [MARKET_GIVEN[channel][market] for market in MARKETS]
        for market in rng.choice(MARKETS, size=count, p=weights):
            rows.append({"channel": channel, "market": str(market)})
    return rows


def _arm(
    rows: Sequence[Mapping[str, str]], n: int, seed: int, declared: Mapping[str, Any]
) -> Dict[str, Any]:
    document = _document(declared)
    compiled = compile_document(document)
    committed = committed_state(rows, compiled)
    plan = build_plan(
        expose_from_schema(SCHEMA, expose_id=EXPOSE_ID),
        sim_id="accuracy",
        seam_digest=compiled.h.value,
        rows=n,
        fidelity=document["extension"]["fidelity"],
    )
    root = derive_root(digest_bytes(compiled.h.value), seed)
    names = [column.column.name for column in plan.columns]
    synthetic = [dict(zip(names, values, strict=True)) for values in generate_rows(plan, root=root)]
    return {
        "compiled": compiled,
        "committed": committed.statistic,
        "synthetic": compute_statistic(synthetic, compiled),
        "rows": synthetic,
        "evaluation": ClearStatistic(
            compiled=compiled, committed=committed, synthetic_rows=synthetic
        ).evaluate(),
    }


@pytest.fixture(scope="module")
def pair(tier: Tier) -> Dict[str, Any]:
    k = tier.pick(pr=5_000, nightly=50_000)
    n = tier.pick(pr=5_000, nightly=100_000)
    rows = _committed(k, tier.seed("fidelity-level-pair", "committed"))
    first = compile_document(_document())
    statistic = compute_statistic(rows, first)
    table = pair_table_from_level_groups(
        "channel",
        "market",
        first.block.column("market").support_values,
        statistic.properties[GROUPED].groups,
    )
    kinds = {"categorical": ["channel", "market"]}
    edges = suggest_tree([table], **kinds)
    seed = tier.seed("fidelity-level-pair", "generator")
    arms = {
        "independent": _arm(rows, n, seed, declarations([], [table], **kinds)),
        "conditional": _arm(rows, n, seed, declarations(edges, [table], **kinds)),
    }
    for arm in arms.values():
        assert (
            arm["committed"].to_document() == statistic.to_document()
        ), "S must not depend on the generator-side declarations"
    return {
        "k": k,
        "n": n,
        "rows": rows,
        "edges": [(edge.parent, edge.child) for edge in edges],
        "arms": arms,
    }


def _joint(groups: Sequence[Any]) -> Dict[Tuple[str, str], Fraction]:
    total = sum(group.k for group in groups)
    return {
        (group.group, market): Fraction(count, total)
        for group in groups
        for market, count in zip(MARKETS, group.level_counts, strict=True)
    }


def _ceiling(joint: Mapping[Tuple[str, str], Fraction]) -> Fraction:
    """The ContingencySimilarity the product of the table's own margins scores against it."""
    channel = Counter[str]()
    market = Counter[str]()
    for (c, m), share in joint.items():
        channel[c] += share
        market[m] += share
    return 1 - Fraction(1, 2) * sum(
        (abs(share - channel[c] * market[m]) for (c, m), share in joint.items()), Fraction(0)
    )


def _records_score(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]]) -> Fraction:
    """ContingencySimilarity from the records: SDMetrics' crosstab definition."""

    def crosstab(rows: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], Fraction]:
        counts = Counter((str(row["channel"]), str(row["market"])) for row in rows)
        return {cell: Fraction(count, len(rows)) for cell, count in counts.items()}

    left, right = crosstab(a), crosstab(b)
    cells = set(left) | set(right)
    return 1 - Fraction(1, 2) * sum(
        (abs(left.get(cell, Fraction(0)) - right.get(cell, Fraction(0))) for cell in cells),
        Fraction(0),
    )


def _verdicts_agree(arm: Mapping[str, Any]) -> bool:
    """Every group's verdict is ``TV <= margin``, from the two S values in exact arithmetic."""
    margin = arm["compiled"].property_by_id(GROUPED).margin
    committed = {g.group: g for g in arm["committed"].properties[GROUPED].groups}
    synthetic = {g.group: g for g in arm["synthetic"].properties[GROUPED].groups}
    outcome = next(o for o in arm["evaluation"].outcomes if o.property_id == GROUPED)
    expected = []
    for key in sorted(committed):
        tv = Fraction(1, 2) * sum(
            (
                abs(Fraction(c, committed[key].k) - Fraction(s, synthetic[key].k))
                for c, s in zip(
                    committed[key].level_counts, synthetic[key].level_counts, strict=True
                )
            ),
            Fraction(0),
        )
        expected.append(tv <= margin)
    return [group.within for group in outcome.groups] == expected


def test_the_suggested_tree_is_the_true_link(pair: Dict[str, Any]) -> None:
    assert pair["edges"] == [("channel", "market")]


@pytest.mark.parametrize("name", ["independent", "conditional"])
def test_the_gate_keeps_the_link_only_when_it_is_drawn(
    name: str, pair: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    k, n = pair["k"], pair["n"]
    arm = pair["arms"][name]
    committed, synthetic = arm["committed"], arm["synthetic"]
    evaluation = arm["evaluation"]
    outcomes = {outcome.property_id: outcome.outcome for outcome in evaluation.outcomes}
    joint = _joint(committed.properties[GROUPED].groups)
    ceiling = _ceiling(joint)
    one_sample = _expected_tv(list(joint.values()), 1 / n)
    two_sample = _expected_tv(list(joint.values()), 1 / k, 1 / n)
    score = contingency_similarity(synthetic.properties[GROUPED], committed.properties[GROUPED])
    definition = score == _records_score(arm["rows"], pair["rows"])
    discriminates = float(ceiling) < 1 - SPREAD * one_sample
    agrees = _verdicts_agree(arm)
    if name == "conditional":
        decided = evaluation.within
        scored = float(1 - score) <= SPREAD * one_sample
        criterion = (
            "market given channel, declared from S: the fidelity check passes and "
            "1 - ContingencySimilarity <= 3 x one-sample distance"
        )
    else:
        decided = (
            not evaluation.refused
            and outcomes == {GROUPED: "outside", CHANNEL: "within"}
            and not evaluation.within
        )
        scored = abs(float(score) - float(ceiling)) <= SPREAD * two_sample
        criterion = (
            "independent draws: the fidelity check withholds on the grouped level "
            "histogram and ContingencySimilarity sits at the independence ceiling"
        )
    passed = decided and scored and definition and discriminates and agrees
    accuracy_log.record(
        family=FAMILY,
        case=f"engine-level-pair-{name}",
        criterion=criterion,
        passed=passed,
        n=n,
        metrics={
            "committedRecords": k,
            "ContingencySimilarity": float(score),
            "independenceCeiling": float(ceiling),
            "oneSampleDistance": one_sample,
            "twoSampleDistance": two_sample,
            "verdicts": outcomes,
        },
    )
    assert discriminates, (float(ceiling), one_sample)
    assert definition, "S's ContingencySimilarity must equal the record-level crosstab value"
    assert agrees, "every group's verdict must equal TV <= margin from the two S values"
    assert decided, (outcomes, [r.to_document() for r in evaluation.refusals])
    assert scored, (float(score), float(ceiling), one_sample, two_sample)
