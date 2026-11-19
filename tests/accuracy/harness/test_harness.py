# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The harness the whole accuracy program stands on, tested on its own.

A false-discovery battery that picks the wrong method, a pooling rule that
drops a cell, or a seed that two workers derive differently would quietly
weaken every family at once. These tests pin each piece against an
independent computation.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

import json  # noqa: E402
import math  # noqa: E402
from decimal import Decimal  # noqa: E402
from fractions import Fraction  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from scipy import stats  # noqa: E402

from tests.accuracy.harness import deps, tier  # noqa: E402
from tests.accuracy.harness.record import RESULT_SCHEMA, ResultLog, jsonable  # noqa: E402
from tests.accuracy.harness.references import (  # noqa: E402
    capped_poisson_pmf,
    quantised_pmf,
    truncated_normal,
)
from tests.accuracy.harness.stats import Battery, clopper_pearson, pooled_chisquare  # noqa: E402


def test_seeds_are_a_pure_function_of_the_base_seed_and_the_labels() -> None:
    first = tier.Tier(name="nightly", base_seed=12345)
    again = tier.Tier(name="nightly", base_seed=12345)
    other = tier.Tier(name="nightly", base_seed=12346)
    assert first.seed("gof", "case", 0) == again.seed("gof", "case", 0)
    assert first.seed("gof", "case", 0) != first.seed("gof", "case", 1)
    assert first.seed("gof", "case", 0) != other.seed("gof", "case", 0)
    seeds = first.seeds(1000, "many")
    assert len(set(seeds)) == 1000
    assert all(0 <= seed < 1 << 63 for seed in seeds)


def test_the_tier_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        monkeypatch.delenv(tier.TIER_ENV, raising=False)
        tier.current_tier.cache_clear()
        assert tier.current_tier() == tier.Tier(name="pr", base_seed=tier.PR_BASE_SEED)
        monkeypatch.setenv(tier.TIER_ENV, "nightly")
        monkeypatch.setenv(tier.SEED_ENV, "777")
        tier.current_tier.cache_clear()
        assert tier.current_tier() == tier.Tier(name="nightly", base_seed=777)
        monkeypatch.setenv(tier.TIER_ENV, "weekly")
        tier.current_tier.cache_clear()
        with pytest.raises(ValueError):
            tier.current_tier()
    finally:
        tier.current_tier.cache_clear()


def test_pick_and_only_nightly() -> None:
    pr, nightly = tier.Tier("pr", 1), tier.Tier("nightly", 1)
    assert pr.pick(pr=1, nightly=2) == 1 and nightly.pick(pr=1, nightly=2) == 2
    nightly.only_nightly("never skips here")
    with pytest.raises(pytest.skip.Exception):
        pr.only_nightly("skips in the pull-request tier")


def test_the_battery_picks_bh_for_independent_samples_and_by_for_shared_ones() -> None:
    independent = Battery(name="independent")
    for index in range(5):
        independent.add(case=f"c{index}", test="t", statistic=0.0, pvalue=0.5, sample=f"s{index}")
    assert independent.method == "bh"
    shared = Battery(name="shared")
    shared.add(case="c", test="ks", statistic=0.0, pvalue=0.5, sample="s")
    shared.add(case="c", test="cvm", statistic=0.0, pvalue=0.5, sample="s")
    assert shared.method == "by"


def test_the_battery_adjusts_with_scipy() -> None:
    pvalues = [0.0001, 0.004, 0.03, 0.2, 0.9]
    battery = Battery(name="b", q=0.01)
    for index, pvalue in enumerate(pvalues):
        battery.add(case=f"c{index}", test="t", statistic=0.0, pvalue=pvalue, sample=f"s{index}")
    expected = stats.false_discovery_control(np.array(pvalues), method="bh")
    adjusted = [decision.adjusted for decision in battery.decisions()]
    assert np.allclose(adjusted, expected)
    assert [d.entry.case for d in battery.rejected()] == [
        f"c{i}" for i, value in enumerate(expected) if value <= 0.01
    ]
    assert battery.case_rejected("c0") and not battery.case_rejected("c4")
    assert "rejected" in battery.summary()


def test_the_battery_refuses_a_value_that_is_not_a_p_value() -> None:
    battery = Battery(name="b")
    for bad in (-0.1, 1.5, float("nan")):
        with pytest.raises(ValueError):
            battery.add(case="c", test="t", statistic=0.0, pvalue=bad, sample="s")


def test_pooling_merges_thin_tail_cells_and_keeps_every_count() -> None:
    observed = [2, 3, 28, 42, 22, 2, 1]
    probabilities = [0.01, 0.02, 0.3, 0.4, 0.25, 0.01, 0.01]
    statistic, pvalue, cells = pooled_chisquare(observed, probabilities)
    total = sum(observed)
    merged_obs = [sum(observed[:3]), observed[3], sum(observed[4:])]
    merged_exp = [total * (0.01 + 0.02 + 0.3), total * 0.4, total * (0.25 + 0.01 + 0.01)]
    reference = stats.chisquare(merged_obs, f_exp=merged_exp)
    assert cells == 3
    assert math.isclose(statistic, float(reference.statistic), rel_tol=1e-12)
    assert math.isclose(pvalue, float(reference.pvalue), rel_tol=1e-12)


def test_pooling_refuses_a_sample_too_small_to_test() -> None:
    with pytest.raises(ValueError):
        pooled_chisquare([1, 1], [0.5, 0.5])


def test_clopper_pearson_matches_the_closed_form_at_zero_successes() -> None:
    low, high = clopper_pearson(0, 20, 0.95)
    assert low == 0.0
    assert math.isclose(high, 1 - 0.025 ** (1 / 20), rel_tol=1e-9)


def test_the_references_are_distributions() -> None:
    grid = quantised_pmf(truncated_normal(10.0, 3.0, 0.0, 20.0), 0.0, 20.0, 1.0)
    assert math.isclose(float(grid.probabilities.sum()), 1.0, rel_tol=1e-12)
    assert np.all(grid.probabilities >= 0)
    with pytest.raises(AssertionError):
        grid.counts([20.0, 21.0])
    pmf = capped_poisson_pmf(1.5, 30)
    assert math.isclose(float(pmf.sum()), 1.0, rel_tol=1e-12)
    assert pmf[-1] == pytest.approx(stats.poisson.sf(29, 1.5))


def test_the_result_log_writes_one_schema_tagged_line_per_case(tmp_path: Path) -> None:
    log = ResultLog(node_id="t::x", tier=tier.Tier("pr", 1), directory=tmp_path)
    log.record(
        family="f",
        case="c",
        criterion="k",
        passed=True,
        metrics={"d": Decimal("0.1"), "r": Fraction(1, 3), "inf": float("inf"), "a": np.float64(2)},
        seed=5,
        n=10,
    )
    (line,) = [
        json.loads(text)
        for path in tmp_path.glob("*.jsonl")
        for text in path.read_text().splitlines()
    ]
    assert line["schema"] == RESULT_SCHEMA and line["case"] == "c" and line["passed"] is True
    assert line["metrics"] == {"d": "0.1", "r": "1/3", "inf": "inf", "a": 2.0}
    silent = ResultLog(node_id="t::y", tier=tier.Tier("pr", 1), directory=None)
    silent.record(family="f", case="c", criterion="k", passed=False)
    assert not silent.enabled
    assert jsonable({1: (Decimal("2"),)}) == {"1": ["2"]}


def test_a_missing_dependency_skips_or_fails_as_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(deps.REQUIRED_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception, match="accuracy dependency group"):
        deps.require("a_module_that_does_not_exist_anywhere")
    monkeypatch.setenv(deps.REQUIRED_ENV, "1")
    with pytest.raises(ImportError, match="FLUX_ACCURACY_REQUIRED=1"):
        deps.require("a_module_that_does_not_exist_anywhere")
    deps.require("json")
