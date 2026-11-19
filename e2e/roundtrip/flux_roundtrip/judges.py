# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The judges: the synthetic rows against the truth's, in plain Python.

This is not the engine. The judges read both sides' rows, which the engine never does, and
compute exact or float figures; none of it feeds back into generation.

* **k-marginal.** The mean total-variation distance over every pair (k = 2) of categorical or
  binned columns of a table, reported as ``1 - mean TV``. It is the k-marginal idea of NIST
  SDNist (https://github.com/usnistgov/SDNist), reimplemented here without the dependency.
  A numeric or date column is binned on the truth's quantile edges, so the truth fills every
  bin about evenly and a synthetic column that bunches up shows as distance.
* **Relationships.** The share of parents with no child, and the share of children held by
  the top 1 % of parents (by their number of children).
* **Grouped shares.** The share of one column's levels within each level of another, and
  Cramér's V of the two columns.
* **Derives.** The share of rows on which a contract ``derive`` holds.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import Counter
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from itertools import combinations
from typing import Any, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple

Row = Mapping[str, Any]
BINS = 10
TOP_FRACTION = Fraction(1, 100)
#: The contract's two derives on ``invoice``: ``vat = ROUND(net * 0.081, 2)``, ``gross = net + vat``.
VAT_RATE = Decimal("0.081")


# --- distributions -----------------------------------------------------------------------------


def distribution(cells: Sequence[Hashable]) -> Dict[Hashable, float]:
    """The share of each cell among *cells*."""
    counts = Counter(cells)
    total = sum(counts.values())
    return {cell: count / total for cell, count in counts.items()} if total else {}


def total_variation(p: Mapping[Hashable, float], q: Mapping[Hashable, float]) -> float:
    """Half the L1 distance between two distributions on the same cells (0 to 1)."""
    return 0.5 * sum(abs(p.get(cell, 0.0) - q.get(cell, 0.0)) for cell in set(p) | set(q))


def cramers_v(pairs: Sequence[Tuple[Hashable, Hashable]]) -> float:
    """Cramér's V of two categorical columns, from their paired values (0 to 1).

    The uncorrected statistic, ``sqrt(chi2 / (n * (min(rows, cols) - 1)))``. It is 0 when
    either column holds one level, or there are no pairs.
    """
    n = len(pairs)
    rows = Counter(a for a, _ in pairs)
    cols = Counter(b for _, b in pairs)
    if n == 0 or len(rows) < 2 or len(cols) < 2:
        return 0.0
    cells = Counter(pairs)
    chi2 = 0.0
    for a, row_total in rows.items():
        for b, col_total in cols.items():
            expected = row_total * col_total / n
            chi2 += (cells.get((a, b), 0) - expected) ** 2 / expected
    return math.sqrt(chi2 / (n * (min(len(rows), len(cols)) - 1)))


# --- columns -----------------------------------------------------------------------------------


def numeric_value(contract_type: str, value: Any) -> Optional[float]:
    """One cell as a number: INT and DECIMAL as they are, a DATE as its ordinal day."""
    if value is None:
        return None
    upper = contract_type.upper()
    if upper == "DATE":
        return float(date.fromisoformat(str(value)).toordinal())
    return float(Decimal(str(value)))


def quantile_edges(values: Sequence[float], bins: int = BINS) -> List[float]:
    """The inner edges of *bins* equal-count bins of *values*, without repeats.

    Edge ``i`` is the value at rank ``i / bins`` of the sorted values (the lower quantile);
    an edge at the column's smallest value is dropped (it would leave bin 0 empty), so a column
    with few distinct values gets fewer bins.
    """
    ordered = sorted(values)
    if not ordered:
        return []
    edges = []
    for i in range(1, bins):
        edge = ordered[min(len(ordered) - 1, (i * len(ordered)) // bins)]
        if edge > ordered[0] and (not edges or edge > edges[-1]):
            edges.append(edge)
    return edges


def bin_of(edges: Sequence[float], value: Optional[float]) -> int:
    """The bin a value falls in: ``-1`` for NULL, else its place among the inner edges."""
    return -1 if value is None else bisect_right(edges, value)


class Column:
    """One column, as categorical levels or as bins on the truth's quantile edges."""

    def __init__(
        self, name: str, contract_type: str, truth_values: Sequence[Any], bins: int = BINS
    ):
        self.name = name
        self.contract_type = contract_type
        self.categorical = contract_type.lower().startswith("enum(") or contract_type.upper() in (
            "STRING",
            "VARCHAR",
        )
        self.edges: List[float] = []
        if not self.categorical:
            numbers = [
                v for v in (numeric_value(contract_type, x) for x in truth_values) if v is not None
            ]
            self.edges = quantile_edges(numbers, bins)

    def cell(self, value: Any) -> Hashable:
        if self.categorical:
            return value
        return bin_of(self.edges, numeric_value(self.contract_type, value))


def k_marginal(
    truth: Sequence[Row],
    synthetic: Sequence[Row],
    columns: Sequence[Tuple[str, str]],
    *,
    k: int = 2,
    bins: int = BINS,
) -> Dict[str, Any]:
    """The k-marginal score of one table: ``1 - mean TV`` over every k-subset of *columns*.

    *columns* is ``(name, contract type)``. The bins are the truth's quantile edges.
    Returns the score, the mean distance and the distance of each subset.
    """
    prepared = {
        name: Column(name, kind, [row[name] for row in truth], bins) for name, kind in columns
    }
    distances: Dict[str, float] = {}
    for subset in combinations(prepared, k):
        t = distribution([tuple(prepared[c].cell(row[c]) for c in subset) for row in truth])
        s = distribution([tuple(prepared[c].cell(row[c]) for c in subset) for row in synthetic])
        distances["|".join(subset)] = total_variation(t, s)
    mean = sum(distances.values()) / len(distances) if distances else 0.0
    return {"score": 1.0 - mean, "meanTotalVariation": mean, "subsets": distances}


# --- relationships -----------------------------------------------------------------------------


def children_per_parent(parent_keys: Sequence[Any], child_keys: Sequence[Any]) -> List[int]:
    """How many children each parent has, one entry per parent, in parent order."""
    held = Counter(child_keys)
    return [held.get(key, 0) for key in parent_keys]


def childless_share(counts: Sequence[int]) -> float:
    """The share of parents with no child."""
    return sum(1 for c in counts if c == 0) / len(counts) if counts else 0.0


def top_share(counts: Sequence[int], fraction: Fraction = TOP_FRACTION) -> float:
    """The share of all children held by the top *fraction* of parents (at least one parent)."""
    total = sum(counts)
    if total == 0 or not counts:
        return 0.0
    top = max(1, math.ceil(len(counts) * fraction))
    return sum(sorted(counts, reverse=True)[:top]) / total


def relationship_figures(parent_keys: Sequence[Any], child_keys: Sequence[Any]) -> Dict[str, Any]:
    counts = children_per_parent(parent_keys, child_keys)
    return {
        "parents": len(counts),
        "children": sum(counts),
        "childlessShare": childless_share(counts),
        "topShare": top_share(counts),
        "mostChildren": max(counts) if counts else 0,
    }


# --- grouped shares ----------------------------------------------------------------------------


def grouped_shares(rows: Sequence[Row], group: str, value: str) -> Dict[Any, Dict[Any, float]]:
    """For each level of *group*, the share of each level of *value* among its rows."""
    by_group: Dict[Any, List[Any]] = {}
    for row in rows:
        by_group.setdefault(row[group], []).append(row[value])
    return {level: distribution(values) for level, values in sorted(by_group.items())}


def max_share_difference(
    truth: Mapping[Any, Mapping[Any, float]], synthetic: Mapping[Any, Mapping[Any, float]]
) -> float:
    """The largest absolute difference between a truth share and the same synthetic share."""
    worst = 0.0
    for level in set(truth) | set(synthetic):
        t, s = truth.get(level, {}), synthetic.get(level, {})
        for other in set(t) | set(s):
            worst = max(worst, abs(t.get(other, 0.0) - s.get(other, 0.0)))
    return worst


def grouped_figures(
    truth: Sequence[Row], synthetic: Sequence[Row], group: str, value: str
) -> Dict[str, Any]:
    t_shares = grouped_shares(truth, group, value)
    s_shares = grouped_shares(synthetic, group, value)
    t_v = cramers_v([(row[group], row[value]) for row in truth])
    s_v = cramers_v([(row[group], row[value]) for row in synthetic])
    return {
        "maxShareDifference": max_share_difference(t_shares, s_shares),
        "cramersVTruth": t_v,
        "cramersVSynthetic": s_v,
        "cramersVDifference": abs(t_v - s_v),
    }


# --- contract derives --------------------------------------------------------------------------


def derives_hold(rows: Sequence[Row]) -> Dict[str, Any]:
    """The share of invoice rows on which each contract derive, and both, hold.

    ``vat`` is ``ROUND(net * 0.081, 2)`` (half away from zero) and ``gross`` is ``net + vat``.
    """
    vat_holds = gross_holds = both = 0
    for row in rows:
        net, vat, gross = (Decimal(str(row[c])) for c in ("net", "vat", "gross"))
        v = (net * VAT_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == vat
        g = net + vat == gross
        vat_holds += v
        gross_holds += g
        both += v and g
    n = len(rows)
    return {
        "rows": n,
        "vatShare": vat_holds / n if n else 0.0,
        "grossShare": gross_holds / n if n else 0.0,
        "bothShare": both / n if n else 0.0,
    }


# --- what the engine said ----------------------------------------------------------------------


def _share(text: Any) -> Optional[float]:
    return None if text is None else float(Fraction(str(text)))


def scorecard_figures(verify: Mapping[str, Any]) -> Dict[str, Any]:
    """The overall score and the coverage of a scorecard, from ``verify``'s JSON."""
    document = (verify.get("scorecard") or {}).get("document") or {}
    overall = document.get("overall") or {}
    coverage = document.get("coverage")
    stated = (
        isinstance(coverage, Mapping)
        and _share((coverage.get("columns") or {}).get("share")) is not None
        and _share((coverage.get("relationships") or {}).get("share")) is not None
    )
    figures: Dict[str, Any] = {
        "overall": _share(overall.get("score")),
        "status": overall.get("status"),
        "coverageStated": bool(stated),
    }
    if stated:
        figures["coverage"] = {
            "columns": _share(coverage["columns"]["share"]),
            "columnsCovered": coverage["columns"]["covered"],
            "columnsOf": coverage["columns"]["of"],
            "relationships": _share(coverage["relationships"]["share"]),
            "relationshipsCovered": coverage["relationships"]["covered"],
            "relationshipsOf": coverage["relationships"]["of"],
        }
    return figures


def profile_levels(column: Mapping[str, Any]) -> int:
    """How many levels or bins a profiled column shows, else how many distinct values.

    The widest published histogram counts (``levelHistogram/1`` levels, ``marginalHistogram/1``
    bins); a profile with none shows its published ``distinctCount`` instead.
    """
    widest = 0
    for statistic in column.get("statistics", []):
        if statistic.get("definition") == "levelHistogram/1":
            widest = max(widest, len(statistic.get("levels", [])))
        elif statistic.get("definition") == "marginalHistogram/1":
            widest = max(widest, max(0, len(statistic.get("bins", [])) - 1))
    if widest:
        return widest
    metric = column.get("metrics", {}).get("distinctCount") or {}
    if metric.get("status") == "F" and metric.get("value") is not None:
        return int(metric["value"])
    return 0


def constant_flags(verify: Mapping[str, Any], profile: Mapping[str, Any]) -> List[str]:
    """``table.column`` of every CONSTANT flag on a column whose profile shows 2+ levels or bins."""
    document = (verify.get("scorecard") or {}).get("document") or {}
    found: List[str] = []
    for entry in (document.get("shapeFlags") or {}).get("tables", []):
        table = entry.get("exposeId")
        profiled = profile["tables"].get(table, {}).get("columns", {})
        for column in entry.get("columns", []):
            if any(flag.get("alert") == "CONSTANT" for flag in column.get("flags", [])):
                if profile_levels(profiled.get(column["column"], {})) >= 2:
                    found.append(f"{table}.{column['column']}")
    return sorted(found)


def unused_statistics(init: Mapping[str, Any]) -> Optional[int]:
    """The count of statistics init left unused, from its JSON ``ledger.counts.unused``."""
    counts = ((init.get("artifacts") or {}).get("ledger") or {}).get("counts") or {}
    return counts.get("unused")
