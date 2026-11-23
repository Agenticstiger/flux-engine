# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A sample estimates a histogram and a quantile with bins, each with its intervals.

docs/spec/SAMPLE-SPEC.md. The full comparison (:class:`ClearProfile`) is the oracle: a sample
of every block gives each group the outcome it gives, over generated distributions that sit
inside, past and across the margin. A partial sample's shares and ranks are the cluster ratio
estimator's over the blocks' own counts, and the intervals cover the full shares at about
the rate they claim.
"""

from __future__ import annotations

import random
from fractions import Fraction
from typing import Any, Dict, List, Sequence

import pytest

from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.sampling import (
    OUTSIDE,
    UNDECIDED,
    WITHIN,
    SampleRequest,
    choose_blocks,
    evaluate_sample,
)
from flux_engine.concordance.verifier import ClearProfile
from flux_stats.profile import parse_profile
from flux_stats.sampling import ratio_estimate
from flux_stats.spelling import render_exact
from tests.concordance.conftest import build_compiled
from tests.stats.profiles import profile_from_rows

from .test_quantile_bins import BINS, EXPOSE_ID, _document

pytestmark = pytest.mark.unit

BLOCK = 20
TOLERANCE = {"type": "absolute", "value": "2"}
TOTAL_VARIATION = {"type": "distance", "measure": "totalVariation", "max": "0.2"}


def _compiled(level: str = "0.5") -> CompiledFidelity:
    properties: List[Dict[str, Any]] = [
        {
            "property": "quantile",
            "column": "amount",
            "level": level,
            "bins": list(BINS),
            "tolerance": dict(TOLERANCE),
        },
        {
            "property": "marginalHistogram",
            "column": "amount",
            "bins": list(BINS),
            "tolerance": dict(TOTAL_VARIATION),
        },
        {"property": "mean", "column": "amount", "tolerance": dict(TOLERANCE)},
        {"property": "quantile", "column": "amount", "level": level, "tolerance": dict(TOLERANCE)},
    ]
    document = _document(properties=properties)
    document["extension"]["fidelity"]["records"] = {"min": 2, "max": 10**6}
    return build_compiled(document)


def _rows(seed: int, count: int, centre: float, spread: float) -> List[Dict[str, Any]]:
    generator = random.Random(seed)
    rows = []
    for _ in range(count):
        value = min(100.0, max(0.0, generator.gauss(centre, spread)))
        rows.append({"amount": render_exact(Fraction(round(value * 16), 16))})
    return rows


def _blocks(rows: Sequence[Dict[str, Any]], size: int = BLOCK) -> Dict[int, List[Dict[str, Any]]]:
    return {start: list(rows[start : start + size]) for start in range(0, len(rows), size)}


def _full(
    compiled: CompiledFidelity, profile: Any, rows: Sequence[Dict[str, Any]]
) -> Dict[str, str]:
    evaluation = ClearProfile(compiled=compiled, profile=profile, synthetic_rows=rows).evaluate()
    assert not evaluation.refusals, evaluation.refusals
    return {entry.property_id: entry.outcome for entry in evaluation.outcomes}


@pytest.mark.property
@pytest.mark.parametrize("shift", [0, 1, 4, 10, 25])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_sample_of_every_block_gives_the_full_comparisons_answer(shift: int, seed: int) -> None:
    compiled = _compiled()
    reference = _rows(seed, 400, 45, 12)
    profile = parse_profile(profile_from_rows(reference, compiled))
    synthetic = _rows(seed + 100, 400, 45 + shift, 12)
    full = _full(compiled, profile, synthetic)
    every = _blocks(synthetic)
    found = evaluate_sample(compiled, profile, every, population=len(every), records=len(synthetic))
    assert not found.refusals
    estimated = {entry.property_id: entry for entry in found.properties}
    # The mean, the histogram and the binned quantile are estimated; the plain quantile is not.
    assert set(estimated) == {
        "000:quantile:amount",
        "001:marginalHistogram:amount",
        "002:mean:amount",
    }
    assert found.not_estimated == ("003:quantile:amount",)
    for property_id, entry in estimated.items():
        assert entry.outcome == full[property_id], (property_id, shift, seed)
    histogram = estimated["001:marginalHistogram:amount"].groups[0]
    assert all(share.exact for share in histogram.shares)
    assert histogram.distance is not None and histogram.distance[0] == histogram.distance[1]
    ranks = estimated["000:quantile:amount"].groups[0]
    assert all(rank.exact for rank in ranks.ranks)


def test_a_partial_samples_shares_are_the_estimators_over_the_blocks_counts() -> None:
    compiled = _compiled()
    reference = _rows(7, 600, 45, 12)
    profile = parse_profile(profile_from_rows(reference, compiled))
    synthetic = _rows(8, 600, 47, 12)
    every = _blocks(synthetic)
    chosen = sorted(every)[::3]
    found = evaluate_sample(
        compiled,
        profile,
        {start: every[start] for start in chosen},
        population=len(every),
        records=len(synthetic),
    )
    histogram = next(e for e in found.properties if e.property_id == "001:marginalHistogram:amount")
    (group,) = histogram.groups
    edges = (0, 400, 800, 1600)  # BINS on the 4-bit grid
    per_block = []
    for start in chosen:
        counts = [0, 0, 0]
        for row in every[start]:
            units = int(Fraction(row["amount"]) * 16)
            counts[next(i for i in range(3) if units < edges[i + 1] or i == 2)] += 1
        per_block.append((counts, len(every[start])))
    for index, share in enumerate(group.shares):
        expected = ratio_estimate([(c[index], m) for c, m in per_block], len(every))
        assert share == expected
    document = group.to_document()
    assert document["blocks"] == len(chosen) and document["level"] == "0.95"
    assert len(document["shares"]) == 3
    assert all(set(entry) == {"estimate", "interval"} for entry in document["shares"])
    ranks = next(e for e in found.properties if e.property_id == "000:quantile:amount").groups[0]
    assert set(ranks.to_document()["ranks"]) == {"below", "through"}
    assert found.status in ("passed", "failed")
    assert "histograms" in found.detail() and "quantile bins" in found.detail()


def test_a_distribution_far_from_the_profile_is_outside_and_one_close_is_within() -> None:
    compiled = _compiled()
    profile = parse_profile(profile_from_rows(_rows(1, 2000, 45, 12), compiled))
    far = _rows(2, 2000, 80, 8)
    near = _rows(3, 2000, 45, 12)
    for rows, wanted in ((far, OUTSIDE), (near, WITHIN)):
        every = _blocks(rows, 50)
        chosen = sorted(every)[::4]
        found = evaluate_sample(
            compiled,
            profile,
            {start: every[start] for start in chosen},
            population=len(every),
            records=len(rows),
        )
        histogram = next(
            e for e in found.properties if e.property_id == "001:marginalHistogram:amount"
        )
        quantile = next(e for e in found.properties if e.property_id == "000:quantile:amount")
        assert histogram.outcome == wanted
        assert quantile.outcome == wanted


def test_a_distribution_across_the_margin_is_undecided_on_a_small_sample() -> None:
    compiled = _compiled()
    profile = parse_profile(profile_from_rows(_rows(1, 4000, 45, 12), compiled))
    rows = _rows(4, 4000, 54, 12)  # the quantile sits at the bin's edge: the margin is crossed
    every = _blocks(rows, 100)
    chosen = sorted(every)[:3]
    found = evaluate_sample(
        compiled,
        profile,
        {start: every[start] for start in chosen},
        population=len(every),
        records=len(rows),
    )
    assert UNDECIDED in {entry.outcome for entry in found.properties}
    assert found.status == "failed"


def test_the_intervals_of_a_five_percent_sample_cover_the_full_shares() -> None:
    compiled = _compiled()
    rows = _rows(11, 4000, 45, 14)
    profile = parse_profile(profile_from_rows(rows, compiled))
    full = [0, 0, 0]
    for row in rows:
        units = int(Fraction(row["amount"]) * 16)
        full[0 if units < 400 else 1 if units < 800 else 2] += 1
    covered = [0, 0, 0]
    size = 40
    for seed in range(100):
        request = SampleRequest(fraction=Fraction(10, 100), block_records=size, seed=seed)
        total, chosen = choose_blocks(len(rows), request, f"run/{EXPOSE_ID}")
        blocks = {i * size: rows[i * size : (i + 1) * size] for i in chosen}
        found = evaluate_sample(compiled, profile, blocks, population=total, records=len(rows))
        assert not found.refusals
        histogram = next(
            e for e in found.properties if e.property_id == "001:marginalHistogram:amount"
        )
        for index, share in enumerate(histogram.groups[0].shares):
            covered[index] += share.low <= Fraction(full[index], len(rows)) <= share.high
    assert min(covered) >= 90, covered
