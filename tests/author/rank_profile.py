# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A contract, and profiles of format 1-4-0 formed from synthetic rows, for the tests of init's copula.

The rows are three correlated number columns and a category, drawn through a Gaussian copula by
Python's own generator, so a test does not depend on the engine's. A profile holds the exact
histograms of the columns (quantile bins) and, as asked, a ``kendallTau/1`` and a
``spearmanRho/1`` of each pair, exact or noisy, formed by :mod:`flux_stats.rank_correlation` on the
profile's grid. Nothing here is a statement about the engine: it makes inputs.
"""

from __future__ import annotations

import bisect
import math
import random
from decimal import Decimal
from fractions import Fraction
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flux_stats.profile import SCHEMA_URL_1_4_0
from flux_stats.rank_correlation import kendall_s, spearman_a4

GRID = 1 << 16
DAY = 86_400_000
START = 1_700_006_400_000  # 2023-11-15
SPAN = 300 * DAY

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: bank.ledger
name: Ledger
metadata:
  owner:
    team: finance
exposes:
  - exposeId: account
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/account.parquet }
    contract:
      schema:
        - { name: acct_id, type: INT, required: true }
        - { name: amount, type: "DECIMAL(12,2)", required: false }
        - { name: balance, type: INT, required: false }
        - { name: opened_at, type: TIMESTAMP, required: false }
        - { name: region, type: "enum('east','north','west')", required: true }
    semantics:
      entities:
        - { name: account, type: primary, expr: acct_id }
"""

NUMBERS = ("amount", "balance", "opened_at")
#: The latent correlations of (amount, balance, opened_at) the rows are drawn with.
LATENT = ((1.0, 0.8, -0.3), (0.8, 1.0, 0.2), (-0.3, 0.2, 1.0))

Row = Tuple[Any, ...]


def rows(count: int = 6000, seed: int = 7) -> List[Row]:
    """Rows of (amount, balance, opened_at, region): continuous margins, correlated latent normals."""
    generator = random.Random(seed)
    normal = NormalDist()
    low = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(i + 1):
            total = sum(low[i][k] * low[j][k] for k in range(j))
            low[i][j] = (
                math.sqrt(LATENT[i][i] - total) if i == j else (LATENT[i][j] - total) / low[j][j]
            )
    found: List[Row] = []
    for _ in range(count):
        e = [generator.gauss(0, 1) for _ in range(3)]
        z = [sum(low[i][j] * e[j] for j in range(i + 1)) for i in range(3)]
        found.append(
            (
                round(math.exp(4 + 0.8 * z[0]), 2),
                max(0, round(1000 + 400 * z[1])),
                START + int(normal.cdf(z[2]) * SPAN),
                ("east", "north", "west")[int(normal.cdf(e[0]) * 3)],
            )
        )
    return found


def _units(value: Any) -> int:
    return math.floor(Fraction(Decimal(str(value))) * GRID)


def _edges(values: Sequence[Any], step: Decimal) -> List[str]:
    """Quantile bin edges, multiples of *step* (a power of two, so each lies on the profile's grid).

    The first edge is at or below the minimum and the last at or above the maximum.
    """
    ordered = sorted(values)
    cuts = [ordered[0]] + [ordered[len(ordered) * q // 4] for q in (1, 2, 3)] + [ordered[-1]]
    edges: List[Decimal] = []
    for position, cut in enumerate(cuts):
        units = Decimal(str(cut)) / step
        edge = (units.to_integral_value("ROUND_FLOOR" if position < 4 else "ROUND_CEILING")) * step
        if edges and edge <= edges[-1]:
            edge = edges[-1] + step
        edges.append(edge)
    return [
        format(edge.normalize() if edge == edge.to_integral_value() else edge, "f")
        for edge in edges
    ]


def _counts(values: Sequence[Any], edges: Sequence[str]) -> List[int]:
    cuts = [Decimal(edge) for edge in edges]
    counts = [0] * (len(cuts) - 1)
    for value in values:
        position = bisect.bisect_right(cuts, Decimal(str(value))) - 1
        counts[min(position, len(counts) - 1)] += 1
    return counts


def _metric(definition: str, **fields: Any) -> Dict[str, Any]:
    return {
        "definition": definition,
        "disclosure": "none",
        "sampleSize": {"min": 1},
        "status": "F",
        **fields,
    }


def _number(values: Sequence[Any], domain: str, step: Decimal) -> Dict[str, Any]:
    edges = _edges(values, step)
    return {
        "domain": domain,
        "metrics": {
            "count": _metric("count/1", value=len(values)),
            "nullCount": _metric("nullCount/1", value=0),
            "min": _metric("min/1", value=edges[0]),
            "max": _metric("max/1", value=edges[-1]),
        },
        "statistics": [
            {
                "definition": "marginalHistogram/1",
                "bins": edges,
                "disclosure": "none",
                "sampleSize": {"min": 1},
                "groups": [
                    {
                        "group": "",
                        "k": len(values),
                        "binCounts": _counts(values, edges),
                        "status": "F",
                    }
                ],
            }
        ],
    }


def _region(values: Sequence[str]) -> Dict[str, Any]:
    levels = sorted(set(values))
    return {
        "domain": "category",
        "metrics": {
            "count": _metric("count/1", value=len(values)),
            "nullCount": _metric("nullCount/1", value=0),
            "distinctCount": _metric("distinctCount/1", value=len(levels)),
        },
        "statistics": [
            {
                "definition": "levelHistogram/1",
                "levels": levels,
                "disclosure": "none",
                "sampleSize": {"min": 1},
                "groups": [
                    {
                        "group": "",
                        "k": len(values),
                        "levelCounts": [values.count(level) for level in levels],
                        "status": "F",
                    }
                ],
            }
        ],
    }


def exact_pair(
    data: Sequence[Row], first: str, second: str, definition: str = "kendallTau/1"
) -> Dict[str, Any]:
    """An exact ``kendallTau/1`` or ``spearmanRho/1`` of a pair of the number columns of *data*."""
    at = {name: position for position, name in enumerate(NUMBERS)}
    pairs = [(_units(row[at[first]]), _units(row[at[second]])) for row in data]
    group: Dict[str, Any] = {"group": "", "k": len(pairs), "status": "F"}
    if definition == "kendallTau/1":
        group["S"] = str(kendall_s(pairs))
    else:
        group["A4"] = str(spearman_a4(pairs))
    return {
        "definition": definition,
        "disclosure": "none",
        "sampleSize": {"min": len(pairs)},
        "groups": [group],
    }


def noisy_pair(value: str, definition: str = "kendallTau/1", floor: int = 6000) -> Dict[str, Any]:
    """A noisy rank correlation that releases *value* (it is clamped by the reader)."""
    return {
        "definition": definition,
        "rowFloor": floor,
        "disclosure": "noisy",
        "sampleSize": {"min": 1},
        "noise": {"mechanism": "gaussian", "scale": "0.001", "privacyLoss": {"rho": "100"}},
        "groups": [{"group": "", "status": "F", "noisyValue": value}],
    }


def profile(
    data: Sequence[Row],
    pairs: Optional[Dict[Tuple[str, str], List[Dict[str, Any]]]] = None,
    *,
    noisy: bool = False,
    rows: Optional[int] = None,
) -> Dict[str, Any]:
    """A profile of *data*: exact histograms, and the *pairs* given or every pair's Kendall's tau."""
    if pairs is None:
        pairs = {
            (a, b): [exact_pair(data, a, b)]
            for a in NUMBERS
            for b in NUMBERS
            if NUMBERS.index(a) < NUMBERS.index(b)
        }
    at = {name: position for position, name in enumerate(NUMBERS)}
    columns: Dict[str, Any] = {
        "acct_id": {"domain": "number"},
        "amount": _number([row[at["amount"]] for row in data], "number", Decimal("0.25")),
        "balance": _number([row[at["balance"]] for row in data], "number", Decimal(1)),
        "opened_at": _number([row[at["opened_at"]] for row in data], "timestamp", Decimal(1)),
        "region": _region([row[3] for row in data]),
    }
    document: Dict[str, Any] = {
        "_producer": "urn:example:rank-profiler",
        "_schemaURL": SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}, "noisy": {}},
        "tables": {
            "account": {
                "rowCount": rows if rows is not None else len(data),
                "columns": columns,
                "columnPairs": [
                    {"columns": [a, b], "statistics": statistics}
                    for (a, b), statistics in pairs.items()
                ],
            }
        },
    }
    if noisy or any("noise" in item for found in pairs.values() for item in found):
        document["privacy"] = {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "1000"},
            "privacyUnit": {"expose": "account"},
            "publicStructure": True,
        }
    return document


def group_by_region(document: Dict[str, Any], data: Sequence[Row], column: str) -> None:
    """Add to *column* of *document* its marginal histogram grouped by ``region`` (a two-way table)."""
    at = {name: position for position, name in enumerate(NUMBERS)}
    entry = document["tables"]["account"]["columns"][column]
    edges = list(entry["statistics"][0]["bins"])
    levels = sorted({row[3] for row in data})
    groups = []
    for level in levels:
        values = [row[at[column]] for row in data if row[3] == level]
        groups.append(
            {
                "group": level,
                "k": len(values),
                "binCounts": _counts(values, edges),
                "status": "F",
            }
        )
    entry["statistics"].append(
        {
            "definition": "marginalHistogram/1",
            "bins": edges,
            "groupBy": "region",
            "disclosure": "none",
            "sampleSize": {"min": 1},
            "groups": groups,
        }
    )
