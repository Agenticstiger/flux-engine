# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A noisy statistic widens its tolerance by a bound derived from its noise (SCORECARD-SPEC 9).

The world here is a joint distribution over three categorical columns, and a run whose rows hold
exactly its shares. A profiler releases the world's counts as one clique under noise, a hundred
times, each time with another noise seed. The run is compared with each release: with the margin
the author declared it fails often, because the reference is off by the noise, and with the margin
widened by the noise's reach it passes at least 95 times in 100.
"""

from __future__ import annotations

import random
from fractions import Fraction
from itertools import product
from typing import Any, Dict, List, Mapping, Sequence

import pytest

from flux_engine.concordance import verifier
from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.verifier import ClearProfile
from flux_stats.noise import widening
from flux_stats.profile import SCHEMA_URL_1_2_0, parse_profile
from tests.concordance.conftest import build_compiled

pytestmark = pytest.mark.unit

EXPOSE = "plan_moment"
CHANNELS = ["store", "web"]
PLANS = ["basic", "plus", "pro"]
TIERS = ["gold", "silver"]

#: The true shares of (channel, plan, tier), row-major, as whole parts of 1000.
SHARES = [120, 40, 100, 60, 30, 20, 60, 20, 150, 150, 160, 90]
RECORDS = 20000
SCALE = 150  # the noise's scale: a cell's count is off by about 120 on 1,667 on average

TV = {"type": "distance", "measure": "totalVariation", "max": "0.03"}


def _document() -> Dict[str, Any]:
    columns = {
        "channel": {"support": {"values": CHANNELS}, "maxCardinality": 4, "null": "fail"},
        "plan": {"support": {"values": PLANS}, "maxCardinality": 4, "null": "fail"},
        "tier": {"support": {"values": TIERS}, "maxCardinality": 4, "null": "fail"},
    }
    properties = [
        {"property": "levelHistogram", "column": "channel", "tolerance": dict(TV)},
        {"property": "levelHistogram", "column": "plan", "tolerance": dict(TV)},
        {"property": "levelHistogram", "column": "tier", "tolerance": dict(TV)},
        {
            "property": "levelHistogram",
            "column": "plan",
            "groupBy": "channel",
            "tolerance": dict(TV),
        },
        {"property": "levelHistogram", "column": "tier", "groupBy": "plan", "tolerance": dict(TV)},
    ]
    return {
        "contract": {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "test.private.plan",
            "exposes": [
                {
                    "exposeId": EXPOSE,
                    "kind": "stream",
                    "contract": {
                        "schema": [
                            {"name": "channel", "type": "STRING", "required": True},
                            {"name": "plan", "type": "STRING", "required": True},
                            {"name": "tier", "type": "STRING", "required": True},
                        ]
                    },
                }
            ],
        },
        "emit": {"productRef": "test.private.plan", "exposeId": EXPOSE, "fluidVersion": "^0.7.3"},
        "extension": {
            "fidelity": {
                "encoding": {"fractionalBits": 16, "rounding": "floor"},
                "records": {"min": 2, "max": 1_000_000},
                "columns": columns,
                "properties": properties,
                "budget": {"maxChecksPerState": 20},
                "failureAction": "withholdBundle",
            }
        },
        "expose": {"exposeId": EXPOSE, "columns": ["channel", "plan", "tier"]},
    }


def _rows(shares: Sequence[int]) -> List[Dict[str, Any]]:
    """A run whose rows hold exactly the world's shares of RECORDS."""
    rows: List[Dict[str, Any]] = []
    for (channel, plan, tier), share in zip(product(CHANNELS, PLANS, TIERS), shares, strict=True):
        rows.extend(
            {"channel": channel, "plan": plan, "tier": tier}
            for _ in range(RECORDS * share // sum(shares))
        )
    return rows


def _noise(generator: random.Random) -> int:
    """A whole-number noise of variance about 896, from integers alone (the same on every platform)."""
    return sum(generator.randint(-7, 7) for _ in range(48)) * SCALE // 30


def _profile(seed: int) -> Dict[str, Any]:
    """The world's counts released as one clique under noise seed *seed*."""
    generator = random.Random(seed)
    counts = [RECORDS * share // sum(SHARES) + _noise(generator) for share in SHARES]
    return {
        "_producer": "urn:example:flux-test-profiler",
        "_schemaURL": SCHEMA_URL_1_2_0,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {"none": {}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "0.5"},
            "privacyUnit": {"expose": EXPOSE},
        },
        "tables": {
            EXPOSE: {
                "columns": {name: {"domain": "category"} for name in ("channel", "plan", "tier")},
                "cliques": [
                    {
                        "definition": "cliqueMarginal/1",
                        "columns": ["channel", "plan", "tier"],
                        "levels": [CHANNELS, PLANS, TIERS],
                        "noisyCounts": [str(count) for count in counts],
                        "noise": {
                            "mechanism": "discreteGaussian",
                            "scale": str(SCALE),
                            "privacyLoss": {"rho": "0.1"},
                        },
                    }
                ],
            }
        },
    }


def _evaluate(compiled: CompiledFidelity, document: Mapping[str, Any], rows: Any) -> Any:
    return ClearProfile(
        compiled=compiled, profile=parse_profile(dict(document)), synthetic_rows=rows
    ).evaluate()


@pytest.fixture(scope="module")
def compiled() -> CompiledFidelity:
    return build_compiled(_document())


def test_a_world_that_matches_the_profile_passes_at_least_95_of_100_noise_seeds(
    compiled: CompiledFidelity,
) -> None:
    rows = _rows(SHARES)
    passed = [seed for seed in range(100) if _evaluate(compiled, _profile(seed), rows).within]
    assert len(passed) >= 95, f"{len(passed)} of 100 passed"


def test_without_the_widening_the_same_runs_fail_often(
    compiled: CompiledFidelity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verifier, "widening", lambda *args: Fraction(0))
    rows = _rows(SHARES)
    passed = sum(1 for seed in range(100) if _evaluate(compiled, _profile(seed), rows).within)
    assert passed < 80, f"{passed} of 100 passed without the widening, so the test shows nothing"


def test_a_run_from_another_world_still_fails(compiled: CompiledFidelity) -> None:
    other = _rows(list(reversed(SHARES)))
    failed = [seed for seed in range(20) if not _evaluate(compiled, _profile(seed), other).within]
    assert len(failed) == 20


def test_each_group_of_a_noisy_property_carries_its_widening(compiled: CompiledFidelity) -> None:
    evaluation = _evaluate(compiled, _profile(3), _rows(SHARES))
    by_id = {outcome.property_id: outcome for outcome in evaluation.outcomes}
    plan = by_id["001:levelHistogram:plan"]
    # A one-way histogram of plan sums four clique cells onto each of its three cells.
    assert plan.cell_variance == Fraction(SCALE * SCALE * 4)
    group = plan.groups[0]
    total = sum(_plan_group(3)[0].noisy, Fraction(0))
    assert group.widening == widening(Fraction(SCALE * SCALE * 4), 3, total, group.committed_count)
    assert plan.widening == group.widening
    grouped = by_id["003:levelHistogram:plan@channel"]
    assert grouped.cell_variance == Fraction(SCALE * SCALE * 2)
    assert len({g.widening for g in grouped.groups if g.widening is not None}) >= 1


def _plan_group(seed: int) -> Any:
    """The groups of the one-way histogram of ``plan``, as the reader forms them from a release."""
    column = parse_profile(_profile(seed)).tables[EXPOSE].columns["plan"]
    (statistic,) = [s for s in column.statistics if s.group_by is None]
    return statistic.groups


def test_the_widening_formula() -> None:
    # m = 4 cells, variance 25 (scale 5), T = K = 1000 records:
    # sqrt(4) * (sqrt(100) + sqrt(250)) / 1000 + 4 / 2000, each root rounded up to 12 places.
    one = Fraction(1000)
    assert widening(Fraction(25), 4, one, 1000) == Fraction("0.053622776602")
    # No noise: only the spelling of the estimate in whole counts remains.
    assert widening(Fraction(0), 6, one, 1000) == Fraction(6, 2000)
    # It falls as the records grow and rises with the variance and the cells.
    assert widening(Fraction(25), 4, 2 * one, 2000) < widening(Fraction(25), 4, one, 1000)
    assert widening(Fraction(36), 4, one, 1000) > widening(Fraction(25), 4, one, 1000)
    assert widening(Fraction(25), 5, one, 1000) > widening(Fraction(25), 4, one, 1000)
    # The noise term reads the exact total, not the records the counts are spelled over.
    assert widening(Fraction(25), 4, Fraction(999), 1000) > widening(Fraction(25), 4, one, 1000)
    with pytest.raises(ValueError):
        widening(Fraction(25), 4, Fraction(0), 1000)
    with pytest.raises(ValueError):
        widening(Fraction(25), 4, one, 0)


def test_a_column_drawn_from_a_clique_and_compared_with_its_own_statistic_widens_by_both(
    compiled: CompiledFidelity,
) -> None:
    document = _profile(7)
    table = document["tables"][EXPOSE]
    generator = random.Random(99)
    counts = [
        str(sum(RECORDS * share // sum(SHARES) for share in SHARES[i::3]) + _noise(generator))
        for i in range(3)
    ]  # channel is the first column of the clique: its one-way marginal is a sum of 6 cells... as 3 of 12
    table["columns"]["plan"]["statistics"] = [
        {
            "definition": "levelHistogram/1",
            "sampleSize": {"min": 1, "max": RECORDS * 2},
            "disclosure": "none",
            "levels": PLANS,
            "noise": {
                "mechanism": "discreteGaussian",
                "scale": str(SCALE),
                "privacyLoss": {"rho": "0.1"},
            },
            "groups": [{"group": "", "status": "F", "noisyCounts": counts}],
        }
    ]
    profile = parse_profile(document)
    evaluation = ClearProfile(
        compiled=compiled, profile=profile, synthetic_rows=_rows(SHARES)
    ).evaluate()
    plan = {o.property_id: o for o in evaluation.outcomes}["001:levelHistogram:plan"]
    own = profile.tables[EXPOSE].columns["plan"].statistics[0]
    (clique,) = [
        s for s in profile.tables[EXPOSE].columns["plan"].clique_statistics if s.group_by is None
    ]
    group, drawn = own.groups[0], clique.groups[0]
    assert group.statistic is not None and drawn.statistic is not None
    expected = widening(
        own.cell_variance or Fraction(0), 3, sum(group.noisy), group.statistic.k
    ) + widening(clique.cell_variance or Fraction(0), 3, sum(drawn.noisy), drawn.statistic.k)
    assert plan.groups[0].widening == expected
    # the clique's own answer to the same question adds nothing
    again = {
        o.property_id: o
        for o in ClearProfile(
            compiled=compiled, profile=parse_profile(_profile(7)), synthetic_rows=_rows(SHARES)
        )
        .evaluate()
        .outcomes
    }["001:levelHistogram:plan"]
    assert again.groups[0].widening < expected


def test_the_scorecard_names_format_1_3_0_and_carries_the_noise_of_a_noisy_property(
    compiled: CompiledFidelity,
) -> None:
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator

    from flux_engine.concordance.scorecard import SCHEMA_URL_1_3_0, TableInput, build_scorecard

    spec = Path(__file__).resolve().parents[2] / "docs" / "spec"
    schema = json.loads((spec / "scorecard-1-3.schema.json").read_text(encoding="utf-8"))
    document = _profile(5)
    profile = parse_profile(dict(document))
    evaluation = ClearProfile(
        compiled=compiled, profile=profile, synthetic_rows=_rows(SHARES)
    ).evaluate()
    digest = "sha256:" + "a" * 64
    scorecard = build_scorecard(
        run_id="0123456789abcdef",
        plan_digest=digest,
        run_digest=digest,
        profile_digest=digest,
        tables=[TableInput(expose_id=EXPOSE, compiled=compiled, evaluation=evaluation)],
        profile=profile,
    )
    rendered = scorecard.to_document()
    Draft202012Validator(schema).validate(rendered)
    assert rendered["_schemaURL"] == SCHEMA_URL_1_3_0
    rows = {row["propertyId"]: row for row in rendered["tables"][0]["properties"]}
    noise = rows["001:levelHistogram:plan"]["noise"]
    assert noise["mechanism"] == "discreteGaussian"
    assert noise["cellVariance"] == str(SCALE * SCALE * 4)
    assert Fraction(noise["widening"]) > 0
    assert (
        scorecard.to_bytes()
        == build_scorecard(
            run_id="0123456789abcdef",
            plan_digest=digest,
            run_digest=digest,
            profile_digest=digest,
            tables=[TableInput(expose_id=EXPOSE, compiled=compiled, evaluation=evaluation)],
            profile=profile,
        ).to_bytes()
    )


def test_a_clique_whose_cells_add_up_to_nothing_is_refused_and_does_not_crash_verify(
    compiled: CompiledFidelity,
) -> None:
    document = _profile(7)
    table = document["tables"][EXPOSE]
    table["columns"]["plan"]["statistics"] = [
        {
            "definition": "levelHistogram/1",
            "sampleSize": {"min": 1, "max": RECORDS * 2},
            "disclosure": "none",
            "levels": PLANS,
            "noise": {
                "mechanism": "discreteGaussian",
                "scale": str(SCALE),
                "privacyLoss": {"rho": "0.1"},
            },
            "groups": [{"group": "", "status": "F", "noisyCounts": ["7000", "6000", "7000"]}],
        }
    ]
    # the clique the column is drawn from is all noise below zero: its records are 0
    table["cliques"][0]["noisyCounts"] = ["-10"] * len(table["cliques"][0]["noisyCounts"])
    evaluation = ClearProfile(
        compiled=compiled, profile=parse_profile(document), synthetic_rows=_rows(SHARES)
    ).evaluate()
    refused = [r for r in evaluation.refusals if r.property_id == "001:levelHistogram:plan"]
    assert [r.code for r in refused] == ["FLX-5005"]
    assert "no records above zero" in refused[0].reason
    assert not evaluation.within, "a refused comparison is not a pass"


def test_the_widening_of_a_group_that_adds_up_to_nothing_is_refused_by_the_helper() -> None:
    from flux_engine.concordance.verifier import _reach
    from flux_stats.profile import FREE, ProfileGroup
    from flux_stats.registry import GroupStatistic

    group = ProfileGroup(
        key="",
        status=FREE,
        statistic=GroupStatistic(group="", k=0, level_counts=(0, 0)),
        noisy=(Fraction(-3), Fraction(1)),
    )
    with pytest.raises(Exception, match="no records above zero"):
        _reach(Fraction(25), 2, group)
