# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 1, trees: a chain channel -> market -> amount, drawn three ways.

A synthetic committed record set (NumPy, stated parameters, never real data)
is drawn from a chain: channel, then market given channel, then amount given
market. Its two-way count tables, (channel, market), (market, amount bin)
and (channel, amount bin), are the aggregates the generator is declared
from. S holds each of them: a grouped ``levelHistogram`` gives the first and
grouped histograms the numeric ones. This test counts them from the records
directly; ``test_level_pair_draws.py`` reads the categorical one from S.

Three generators are compared on the same rows and seeds:

* independent: every column from its own one-way margin;
* star: amount given market, with channel and market independent (the first spike);
* tree: the Chow-Liu tree :func:`flux_engine.calibrate.structure.suggest_tree`
  picks from the tables, declared by :func:`~flux_engine.calibrate.structure.declarations`.

Pass criteria:

* the suggested tree is the true chain, channel -> market -> amount;
* for the tree, each table on a tree edge sits within three one-sample
  sampling distances of the committed table, because D' is drawn from that
  table exactly. The table off the tree and the three-way table sit within
  three two-sample distances, because the committed sample is a chain only up
  to its own sampling noise;
* the fixture discriminates: the star misses (channel, market) by more than
  three two-sample distances, and the independent draw misses
  (market, amount bin) by more.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import numpy as np
import pytest

from flux_engine.calibrate.structure import PairTable, TreeEdge, declarations, suggest_tree
from flux_engine.core.seedtree import derive_root, digest_bytes
from flux_engine.generator.rows import build_plan, generate_rows
from tests.accuracy.harness.record import ResultLog
from tests.accuracy.harness.tier import Tier
from tests.concordance.conftest import total_variation
from tests.generator.conftest import expose_from_schema

FAMILY = "fidelity-s"
SPREAD = 3.0
H = "sha256:" + "7" * 64
EDGES: Tuple[int, ...] = (1, 100, 200, 300, 400, 1000)
CHANNELS: Dict[str, float] = {"store": 0.4, "web": 0.6}
MARKET_GIVEN: Dict[str, Dict[str, float]] = {
    "store": {"DE": 0.1, "FR": 0.2, "GB": 0.3, "IE": 0.4},
    "web": {"DE": 0.5, "FR": 0.3, "GB": 0.15, "IE": 0.05},
}
AMOUNT_MEAN: Dict[str, float] = {"DE": 150.0, "FR": 250.0, "GB": 330.0, "IE": 520.0}
AMOUNT_SD = 70.0
SCHEMA: List[Dict[str, Any]] = [
    {"name": "amount", "type": "DECIMAL(18,4)", "required": True},
    {"name": "market", "type": "STRING", "required": True},
    {"name": "channel", "type": "STRING", "required": True},
]
BINS = len(EDGES) - 1

Row = Tuple[str, str, int]


def _bin(value: Decimal) -> int:
    """Half-open bins, the last closed at the top, as the fidelity statistic bins."""
    edges = [Decimal(edge) for edge in EDGES]
    if value >= edges[-1]:
        return BINS - 1
    return bisect_right(edges, value) - 1


def _committed(k: int, seed: int) -> List[Row]:
    rng = np.random.default_rng(seed)
    channels = sorted(CHANNELS)
    markets = sorted(AMOUNT_MEAN)
    drawn = rng.choice(channels, size=k, p=[CHANNELS[name] for name in channels])
    rows: List[Row] = []
    for channel in drawn:
        weights = MARKET_GIVEN[str(channel)]
        market = str(rng.choice(markets, p=[weights[name] for name in markets]))
        amount = float(np.clip(rng.normal(AMOUNT_MEAN[market], AMOUNT_SD), EDGES[0], EDGES[-1]))
        rows.append((str(channel), market, _bin(Decimal(f"{amount:.4f}"))))
    return rows


def _levels(index: int) -> List[str]:
    if index == 0:
        return sorted(CHANNELS)
    if index == 1:
        return sorted(AMOUNT_MEAN)
    return [str(position) for position in range(BINS)]


def _counts(rows: Sequence[Row], columns: Sequence[int]) -> Dict[Tuple[str, ...], int]:
    """A full grid of counts over *columns*, empty cells included."""
    grid: Dict[Tuple[str, ...], int] = {}
    keys: List[Tuple[str, ...]] = [()]
    for index in columns:
        keys = [key + (level,) for key in keys for level in _levels(index)]
    for key in keys:
        grid[key] = 0
    for row in rows:
        grid[tuple(str(row[index]) for index in columns)] += 1
    return grid


NAMES = ("channel", "market", "amount")


def _pair_tables(rows: Sequence[Row]) -> List[PairTable]:
    tables = []
    for a, b in ((0, 1), (1, 2), (0, 2)):
        counts = {(key[0], key[1]): count for key, count in _counts(rows, (a, b)).items()}
        tables.append(PairTable((NAMES[a], NAMES[b]), counts))
    return tables


def _tv(committed: Mapping[Any, int], synthetic: Mapping[Any, int]) -> float:
    return float(total_variation(committed, synthetic))


def _expected(committed: Mapping[Any, int], *inverse_sizes: float) -> float:
    total = sum(committed.values())
    spread = sum(inverse_sizes)
    return 0.5 * sum(
        math.sqrt(2 / math.pi) * math.sqrt((c / total) * (1 - c / total) * spread)
        for c in committed.values()
    )


def _generate(columns: Mapping[str, Mapping[str, Any]], n: int, seed: int) -> List[Row]:
    expose = expose_from_schema(SCHEMA, expose_id="tree_chain")
    declared = {name: dict(value) for name, value in columns.items()}
    declared.setdefault("amount", {})
    declared["amount"]["bins"] = [str(edge) for edge in EDGES]
    plan = build_plan(
        expose, sim_id="accuracy", seam_digest=H, rows=n, fidelity={"columns": declared}
    )
    root = derive_root(digest_bytes(H), seed)
    return [
        (channel, market, _bin(amount))
        for amount, market, channel in generate_rows(plan, root=root)
    ]


@pytest.fixture(scope="module")
def chain(tier: Tier) -> Dict[str, Any]:
    k = tier.pick(pr=5_000, nightly=50_000)
    n = tier.pick(pr=5_000, nightly=100_000)
    committed = _committed(k, tier.seed("fidelity-tree", "committed"))
    tables = _pair_tables(committed)
    kinds = {"categorical": ["channel", "market"], "numeric": ["amount"]}
    edges = suggest_tree(tables, **kinds)
    market_amount = next(edge for edge in edges if edge.child == "amount")
    star_edges = [TreeEdge("market", "amount", market_amount.mutual_information)]
    independent = declarations([], tables, **kinds)
    amount_margin = tables[1].margin("amount")
    independent["amount"] = {"binWeights": [str(amount_margin[str(b)]) for b in range(BINS)]}
    seed = tier.seed("fidelity-tree", "generator")
    configs = {
        "independent": independent,
        "star": declarations(star_edges, tables, **kinds),
        "tree": declarations(edges, tables, **kinds),
    }
    return {
        "k": k,
        "n": n,
        "edges": [(edge.parent, edge.child) for edge in edges],
        "committed": committed,
        "drawn": {name: _generate(config, n, seed) for name, config in configs.items()},
    }


TABLES: Dict[str, Tuple[int, ...]] = {
    "channel-market": (0, 1),
    "market-bin": (1, 2),
    "channel-bin": (0, 2),
    "channel-market-bin": (0, 1, 2),
}
ON_TREE = ("channel-market", "market-bin")


def test_the_suggested_tree_is_the_true_chain(chain: Dict[str, Any]) -> None:
    assert chain["edges"] == [("channel", "market"), ("market", "amount")]


def test_tree_draws_reproduce_every_table_of_the_chain(
    chain: Dict[str, Any], accuracy_log: ResultLog
) -> None:
    k, n = chain["k"], chain["n"]
    scores: Dict[str, Dict[str, float]] = {}
    bounds: Dict[str, float] = {}
    for table, columns in TABLES.items():
        committed = _counts(chain["committed"], columns)
        sizes = (1 / n,) if table in ON_TREE else (1 / k, 1 / n)
        bounds[table] = SPREAD * _expected(committed, *sizes)
        for name, rows in chain["drawn"].items():
            scores.setdefault(name, {})[table] = _tv(committed, _counts(rows, columns))
    two_sample = {
        table: SPREAD * _expected(_counts(chain["committed"], columns), 1 / k, 1 / n)
        for table, columns in TABLES.items()
    }
    discriminates = (
        scores["star"]["channel-market"] > two_sample["channel-market"]
        and scores["independent"]["market-bin"] > two_sample["market-bin"]
    )
    within = {table: scores["tree"][table] <= bounds[table] for table in TABLES}
    passed = discriminates and all(within.values())
    accuracy_log.record(
        family=FAMILY,
        case="engine-tree-chain",
        criterion=(
            "tree draws: TV <= 3 x one-sample distance on tree edges, "
            "3 x two-sample distance off them"
        ),
        passed=passed,
        n=n,
        metrics={
            "committedRecords": k,
            **{
                f"tv.{name}.{table}": score
                for name, row in scores.items()
                for table, score in row.items()
            },
            **{f"bound.{table}": bound for table, bound in bounds.items()},
        },
    )
    assert discriminates, (scores, two_sample)
    for table in TABLES:
        assert within[table], (table, scores["tree"][table], bounds[table])
