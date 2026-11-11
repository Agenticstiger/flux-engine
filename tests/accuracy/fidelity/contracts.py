# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A synthetic contract, a synthetic population, and an independent reading of the gate's rules.

Everything here is synthetic. The "committed" record sets stand in for an
owner's data and are drawn by NumPy from stated parameters, so the
population a deviation is measured against is known exactly (ADR 0003: no
test fits anything to real records).

The contract is shaped like the published fidelity vectors
(``tests/vectors/fidelity-vectors.json``) and compiled by the engine's own
compiler, so the margins ``τ' = τ - ε`` the gate applies are the engine's,
not this module's. What this module computes independently is the
*deviation* a decision rests on: it re-encodes every value onto the declared
grid with ``Fraction`` arithmetic and evaluates each property's rule
(SEAM-FIDELITY-SPEC §5) itself, so a test can hold the gate to it.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from scipy import stats

from flux_engine.concordance.boundary.slice import CommittedState, SliceReference
from flux_engine.concordance.boundary.statistic import compute_statistic
from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.core.clock import parse_rfc3339_ms
from tests.concordance.conftest import build_compiled

__all__ = [
    "AMOUNT_BINS",
    "EXPOSE_ID",
    "FRACTIONAL_BITS",
    "MARKETS",
    "Population",
    "committed_state",
    "contract_document",
    "decide_independently",
    "draw_rows",
    "population_truth",
]

EXPOSE_ID = "accuracy_moment"
FRACTIONAL_BITS = 16
AMOUNT_RANGE = (1, 1000)
AMOUNT_BINS: Tuple[str, ...] = ("1", "100", "200", "300", "400", "1000")
MARKETS: Tuple[Tuple[str, float], ...] = (("DE", 0.4), ("FR", 0.3), ("GB", 0.2), ("IE", 0.1))
#: Off-support market injected by the support property's deviations.
STRANGER = "XX"
COMMITTED_AT = "2026-11-01T00:00:00Z"

#: The declared tolerances, one per property the gate is measured on.
MEAN_TOLERANCE = "0.02"
TV_TOLERANCE = "0.05"
QUANTILE_TOLERANCE = "10"


def contract_document(
    *,
    generator: Optional[Mapping[str, Any]] = None,
    grouped: bool = True,
    records_max: int = 1_000_000,
) -> Dict[str, Any]:
    """A vectors-shaped document: contract, emit, extension and expose.

    *generator* adds generator-side keys (``bins``, ``binWeights``,
    ``levels`` ...) to the ``amount`` and ``market`` column declarations.
    Those keys are part of ``G``, so a committed state must be formed under
    the same document the synthetic side is checked against. *grouped*
    keeps the grouped histogram property (family 1's contingency table);
    the gate measurements drop it, because it is the costliest property to
    form and none of them injects a deviation into it. *records_max* bounds
    ``C1``'s Merkle padding when a real commitment is formed.
    """
    columns: Dict[str, Any] = {
        "amount": {
            "range": {"min": str(AMOUNT_RANGE[0]), "max": str(AMOUNT_RANGE[1])},
            "null": "fail",
        },
        "market": {
            "support": {"values": [name for name, _ in MARKETS]},
            "maxCardinality": 8,
            "null": "fail",
        },
    }
    for name, extra in (generator or {}).items():
        columns[name] = {**columns[name], **copy.deepcopy(dict(extra))}
    document = {
        "contract": {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "accuracy.gold.moment",
            "exposes": [
                {
                    "exposeId": EXPOSE_ID,
                    "kind": "stream",
                    "contract": {
                        "schema": [
                            {"name": "amount", "type": "DECIMAL(18,4)", "required": True},
                            {"name": "market", "type": "STRING", "required": True},
                        ]
                    },
                }
            ],
        },
        "emit": {
            "productRef": "accuracy.gold.moment",
            "exposeId": EXPOSE_ID,
            "fluidVersion": "^0.7.3",
        },
        "extension": {
            "rows": 1000,
            "fidelity": {
                "encoding": {"fractionalBits": FRACTIONAL_BITS, "rounding": "floor"},
                "records": {"min": 100, "max": 1_000_000},
                "columns": columns,
                "properties": [
                    {
                        "property": "mean",
                        "column": "amount",
                        "tolerance": {"type": "relative", "value": MEAN_TOLERANCE},
                    },
                    {
                        "property": "marginalHistogram",
                        "column": "amount",
                        "bins": list(AMOUNT_BINS),
                        "tolerance": {
                            "type": "distance",
                            "measure": "totalVariation",
                            "max": TV_TOLERANCE,
                        },
                    },
                    {
                        "property": "support",
                        "column": "market",
                        "tolerance": {"type": "count", "max": 0},
                    },
                    {
                        "property": "quantile",
                        "column": "amount",
                        "level": "0.5",
                        "tolerance": {"type": "absolute", "value": QUANTILE_TOLERANCE},
                    },
                    {
                        "property": "marginalHistogram",
                        "column": "amount",
                        "groupBy": "market",
                        "bins": list(AMOUNT_BINS),
                        "tolerance": {
                            "type": "distance",
                            "measure": "totalVariation",
                            "max": "0.1",
                        },
                    },
                ],
                "budget": {"maxChecksPerState": 1_000_000, "maxStateAge": "P30D"},
                "failureAction": "withholdBundle",
            },
        },
        "expose": {"exposeId": EXPOSE_ID, "columns": ["amount", "market"]},
    }
    fidelity = document["extension"]["fidelity"]
    fidelity["records"]["max"] = records_max
    if not grouped:
        fidelity["properties"] = [
            entry for entry in fidelity["properties"] if "groupBy" not in entry
        ]
    return document


def metrics_document() -> Dict[str, Any]:
    """The contract with a second numeric column and a correlation property added.

    Family 1 needs a correlation property (and so two numeric columns); the
    gate measurements do not, and stay on :func:`contract_document`.
    """
    document = contract_document()
    schema = document["contract"]["exposes"][0]["contract"]["schema"]
    schema.append({"name": "fee", "type": "DECIMAL(18,4)", "required": True})
    document["expose"]["columns"].append("fee")
    fidelity = document["extension"]["fidelity"]
    fidelity["columns"]["fee"] = {"range": {"min": "0", "max": "200"}, "null": "fail"}
    fidelity["properties"].append(
        {
            "property": "correlation",
            "columns": ["amount", "fee"],
            "minVariance": "4",
            "tolerance": {"type": "absolute", "value": "0.05"},
        }
    )
    return document


def draw_metric_rows(
    population: Population, n: int, seed: int, *, slope: float = 0.1, noise: float = 6.0
) -> List[Dict[str, Any]]:
    """:func:`draw_rows` plus ``fee = slope * amount + noise``, clipped to [0, 200]."""
    rows = draw_rows(population, n, seed)
    rng = np.random.default_rng([seed, 1])
    amounts = np.array([float(row["amount"]) for row in rows])
    fees = np.clip(slope * amounts + rng.normal(0.0, noise, len(rows)), 0.0, 200.0)
    for row, fee in zip(rows, fees, strict=True):
        row["fee"] = f"{fee:.4f}"
    return rows


def compile_document(document: Mapping[str, Any]) -> CompiledFidelity:
    """Compile *document* with the engine's own compiler."""
    return build_compiled(document)


def committed_state(
    rows: Sequence[Mapping[str, Any]], compiled: CompiledFidelity
) -> CommittedState:
    """A committed state whose ``S`` the engine formed, without the commitments.

    ``ClearStatistic`` reads ``G``, the expose and ``S`` from a committed
    state and nothing else; ``C1`` and ``C2`` are what the *gate's* binding
    and record checks read. Forming ``C1`` pads a Merkle tree to
    ``records.max`` leaves per column, which would dominate a measurement
    that commits hundreds of states, so this builds the state with ``S``
    from :func:`compute_statistic` and empty commitments.
    ``test_gate_roc.py`` checks on one sample that the decision is the one
    the real :func:`~flux_engine.concordance.boundary.slice.commit_state`
    state produces.
    """
    return CommittedState(
        expose_id=EXPOSE_ID,
        ordinal=1,
        g=compiled.g.value,
        c1=(),
        c2="sha256:" + "0" * 64,
        statistic=compute_statistic(rows, compiled, subject="committed state"),
        record_count=len(rows),
        committed_at=COMMITTED_AT,
        committed_at_ms=parse_rfc3339_ms(COMMITTED_AT),
        slice_reference=SliceReference(
            uri="file:///synthetic.ndjson", first_line=1, last_line=len(rows)
        ),
    )


@dataclass(frozen=True)
class Population:
    """The distribution a record set is drawn from, with its deviations stated.

    ``amount`` is a normal clipped to the declared range, mixed with weight
    *mix* with a uniform on ``[mix_low, mix_high]`` (which moves mass between
    histogram bins), and shifted by *shift* (which moves the median).
    ``strangers`` off-support records are appended to the sample.
    """

    mean: float = 300.0
    sd: float = 90.0
    mix: float = 0.0
    mix_low: float = 400.0
    mix_high: float = 1000.0
    shift: float = 0.0
    strangers: int = 0

    def with_(self, **changes: Any) -> "Population":
        values = {**self.__dict__, **changes}
        return Population(**values)


REFERENCE = Population()


def draw_rows(population: Population, n: int, seed: int) -> List[Dict[str, Any]]:
    """*n* records from *population* (plus its strangers), amounts at four decimal places."""
    rng = np.random.default_rng(seed)
    amounts = rng.normal(population.mean + population.shift, population.sd, n)
    if population.mix > 0:
        mixed = rng.random(n) < population.mix
        amounts[mixed] = rng.uniform(population.mix_low, population.mix_high, int(mixed.sum()))
    amounts = np.clip(amounts, AMOUNT_RANGE[0], AMOUNT_RANGE[1])
    names = [name for name, _ in MARKETS]
    weights = np.array([weight for _, weight in MARKETS])
    markets = rng.choice(len(names), size=n, p=weights)
    rows = [
        {"amount": f"{amount:.4f}", "market": names[market]}
        for amount, market in zip(amounts, markets, strict=True)
    ]
    rows.extend({"amount": "300.0000", "market": STRANGER} for _ in range(population.strangers))
    return rows


def _clipped_normal_cdf(x: float, mean: float, sd: float) -> float:
    if x < AMOUNT_RANGE[0]:
        return 0.0
    if x >= AMOUNT_RANGE[1]:
        return 1.0
    return float(stats.norm.cdf(x, mean, sd))


def _amount_cdf(x: float, population: Population) -> float:
    centre = population.mean + population.shift
    normal = _clipped_normal_cdf(x, centre, population.sd)
    if population.mix <= 0:
        return normal
    uniform = float(
        stats.uniform.cdf(x, population.mix_low, population.mix_high - population.mix_low)
    )
    return (1 - population.mix) * normal + population.mix * uniform


def _bin_probabilities(population: Population) -> np.ndarray:
    edges = [float(edge) for edge in AMOUNT_BINS]
    cdf = [_amount_cdf(edge - 1e-12, population) for edge in edges[1:-1]]
    cumulative = np.array([0.0, *cdf, 1.0])
    return np.diff(cumulative)


def _amount_mean(population: Population) -> float:
    centre = population.mean + population.shift
    lo, hi = AMOUNT_RANGE
    a, b = (lo - centre) / population.sd, (hi - centre) / population.sd
    inside = stats.norm.cdf(b) - stats.norm.cdf(a)
    clipped = (
        lo * stats.norm.cdf(a)
        + hi * stats.norm.sf(b)
        + centre * inside
        + population.sd * (stats.norm.pdf(a) - stats.norm.pdf(b))
    )
    if population.mix <= 0:
        return float(clipped)
    return float(
        (1 - population.mix) * clipped
        + population.mix * (population.mix_low + population.mix_high) / 2
    )


def _amount_median(population: Population) -> float:
    from scipy.optimize import brentq

    return float(
        brentq(lambda x: _amount_cdf(x, population) - 0.5, AMOUNT_RANGE[0], AMOUNT_RANGE[1])
    )


def population_truth(population: Population, reference: Population = REFERENCE) -> Dict[str, float]:
    """The population-level deviation of *population* from *reference*, per property.

    These are the ground-truth labels: a deviation above the declared
    tolerance should be withheld, one at or below it should ship. They are
    computed from the distributions, never from a sample.
    """
    return {
        "mean": abs(_amount_mean(population) - _amount_mean(reference)) / _amount_mean(reference),
        "tv": 0.5
        * float(np.abs(_bin_probabilities(population) - _bin_probabilities(reference)).sum()),
        "quantile": abs(_amount_median(population) - _amount_median(reference)),
    }


# ---------------------------------------------------------------------------
# The rules, read independently
# ---------------------------------------------------------------------------

_SCALE = 1 << FRACTIONAL_BITS


def _units(value: str) -> int:
    """Encode a decimal string onto the declared grid: ``floor(value * 2**bits)``."""
    exact = Fraction(value)
    return math.floor(exact * _SCALE)


def _edges() -> List[int]:
    return [_units(edge) for edge in AMOUNT_BINS]


def _bin_of(units: int, edges: Sequence[int]) -> int:
    for index in range(len(edges) - 2):
        if units < edges[index + 1]:
            return index
    return len(edges) - 2


def _histogram(units: Sequence[int]) -> List[int]:
    edges = _edges()
    counts = [0] * (len(edges) - 1)
    for value in units:
        counts[_bin_of(value, edges)] += 1
    return counts


def _witness(units: Sequence[int], level: Fraction) -> int:
    ordered = sorted(units)
    index = min(max(math.ceil(level * len(ordered)), 1), len(ordered))
    return ordered[index - 1]


@dataclass(frozen=True)
class IndependentReading:
    """The sample deviations, and the decisions the declared rules give for them."""

    mean_deviation: Fraction
    tv_deviation: Fraction
    quantile_deviation_units: int
    strangers: int
    decisions: Mapping[str, bool]


def decide_independently(
    committed: Sequence[Mapping[str, Any]],
    synthetic: Sequence[Mapping[str, Any]],
    compiled: CompiledFidelity,
) -> IndependentReading:
    """Re-encode both sides and apply SEAM-FIDELITY-SPEC §5's rules, in rationals.

    Returns ``within`` per ungrouped property id. The margins come from
    *compiled* -- they are the contract's declaration -- and everything else
    is computed here.
    """
    margin: Dict[str, Any] = {entry.property_id: entry for entry in compiled.properties}
    by_kind = {
        (entry.declaration.kind.value, entry.declaration.group_by): entry.property_id
        for entry in compiled.properties
    }
    c_units = [_units(row["amount"]) for row in committed]
    s_units = [_units(row["amount"]) for row in synthetic]

    mean_c = Fraction(sum(c_units), len(c_units))
    mean_s = Fraction(sum(s_units), len(s_units))
    mean_deviation = abs(mean_s - mean_c) / abs(mean_c)

    hist_c, hist_s = _histogram(c_units), _histogram(s_units)
    tv_deviation = Fraction(1, 2) * sum(
        (
            abs(Fraction(a, len(s_units)) - Fraction(b, len(c_units)))
            for a, b in zip(hist_s, hist_c, strict=True)
        ),
        Fraction(0),
    )

    level = Fraction(1, 2)
    quantile_deviation = abs(_witness(s_units, level) - _witness(c_units, level))

    committed_support = {row["market"] for row in committed}
    strangers = sum(1 for row in synthetic if row["market"] not in committed_support)

    mean_id = by_kind[("mean", None)]
    tv_id = by_kind[("marginalHistogram", None)]
    quantile_id = by_kind[("quantile", None)]
    support_id = by_kind[("support", None)]
    decisions = {
        mean_id: mean_deviation <= margin[mean_id].margin,
        tv_id: tv_deviation <= margin[tv_id].margin,
        quantile_id: quantile_deviation <= margin[quantile_id].margin_units,
        support_id: strangers <= margin[support_id].max_non_members,
    }
    return IndependentReading(
        mean_deviation=mean_deviation,
        tv_deviation=tv_deviation,
        quantile_deviation_units=quantile_deviation,
        strangers=strangers,
        decisions=decisions,
    )
