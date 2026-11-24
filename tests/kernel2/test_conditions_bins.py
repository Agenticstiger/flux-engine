# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Conditions over a column drawn from bins (ADR 0036 decision 5), exact and on both paths.

``score`` is a histogram, ``plan`` is given its bin, ``region`` is given ``plan`` and ``spend`` is
a histogram given ``region``. A group fixes categorical columns only; the bins of the columns it
leaves behind are variables whose conditionals the exact elimination sums, so a group that fixes
``plan`` changes the weights of ``score``'s bins to their posterior. The bin is chosen by the
cell's first word and the place inside it is the histogram's own draw, so the column path equals
the per-row path.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List

import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import derive_root, digest_bytes
from flux_engine.generator import columns
from flux_engine.generator.conditions import with_conditions
from flux_engine.generator.rows import (
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)
from tests.generator.conftest import SIM_ID, TIME_BOUNDS, H, expose_from_schema, fidelity
from tests.generator.test_columns import _both
from tests.generator.test_conditions import group

pytestmark = pytest.mark.unit

_SCHEMA: List[Dict[str, Any]] = [
    {"name": "score", "type": "INT"},
    {"name": "plan", "type": "STRING"},
    {"name": "region", "type": "STRING"},
    {"name": "spend", "type": "INT"},
    {"name": "visits", "type": "INT"},
]
_DECLARED: Dict[str, Any] = {
    "score": {
        "range": {"min": "0", "max": "100"},
        "bins": ["0", "10", "50", "100"],
        "binWeights": ["1", "2", "1"],
    },
    "plan": {
        "given": "score",
        "levelWeightsGiven": {
            "0": {"basic": "9", "premium": "1"},
            "10": {"basic": "1", "premium": "1"},
            "50": {"basic": "1", "premium": "9"},
        },
    },
    "region": {
        "given": "plan",
        "levelWeightsGiven": {
            "basic": {"EU": "3", "US": "1"},
            "premium": {"EU": "1", "US": "3"},
        },
    },
    "spend": {
        "range": {"min": "0", "max": "300"},
        "bins": ["0", "100", "300"],
        "given": "region",
        "binWeightsGiven": {"EU": ["3", "1"], "US": ["1", "3"]},
    },
    "visits": {"mean": "4", "range": {"min": "0", "max": "50"}, "rate": "0.25"},
}
_GROUPS = [
    group({"plan": "premium"}, 100),
    group({"region": "US"}, 80),
    group({"plan": "basic", "region": "EU"}, 70),
    group({}, 50),
]
_ROWS = 300


def _plan(groups: Any = None, declared: Any = None) -> GeneratorPlan:
    plan = build_plan(
        expose_from_schema(_SCHEMA),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=_ROWS,
        fidelity=fidelity(declared or _DECLARED),
        time_bounds=TIME_BOUNDS,
        generator_version=GENERATOR_VERSION_2,
    )
    return with_conditions(plan, groups or _GROUPS)


def _root(seed: int = 42) -> Any:
    return derive_root(digest_bytes(H), seed, seed_tree_version=kernel2.SEED_TREE_VERSION)


def test_the_weights_of_a_bin_are_its_posterior_given_what_a_group_fixes() -> None:
    plan = _plan()
    assert plan.conditions is not None
    scope, tables = plan.conditions.solution(0).tables["score"]
    assert scope == ()
    names, thresholds = tables[()]
    # P(bin) * P(premium | bin), normalised: Bayes over the bins of the parent.
    joint = {"0": Fraction(1, 4) * Fraction(1, 10), "10": Fraction(2, 4) * Fraction(1, 2)}
    joint["50"] = Fraction(1, 4) * Fraction(9, 10)
    total = sum(joint.values())
    shares = {name: weight / total for name, weight in joint.items()}
    assert names == tuple(sorted(shares))
    expected, running = [], Fraction(0)
    for name in names[:-1]:
        running += shares[name]
        expected.append(-((-running.numerator * 2**53) // running.denominator))
    assert list(thresholds[:-1]) == expected


@pytest.mark.parametrize("chunk", [1, 7, 64, columns.CHUNK_ROWS])
def test_the_column_path_holds_the_per_row_rows(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plan, root = _plan(), _root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]


@pytest.mark.parametrize("seed", [0, 1, 7, 99])
def test_it_agrees_under_any_seed(monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    plan, root = _plan(), _root(seed)
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows and taken == [True]


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (99, 101), (150, 260), (299, 300)])
def test_a_slice_across_group_boundaries_is_the_runs(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    plan, root = _plan(), _root()
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plan, start, stop, root=root)
    )
    assert by_columns == by_rows and taken == [True]
    assert (
        generate_row_slice(plan, start, stop, root=root)
        == generate_rows(plan, root=root)[start:stop]
    )


def test_the_groups_rows_hold_their_values_and_every_bin_value_is_inside_its_bin() -> None:
    rows = generate_rows(_plan(), root=_root())
    assert {row[1] for row in rows[:100]} == {"premium"}
    assert {row[2] for row in rows[100:180]} == {"US"}
    assert {(row[1], row[2]) for row in rows[180:250]} == {("basic", "EU")}
    assert all(0 <= row[0] <= 100 for row in rows)
    # Given premium, the first bin of score (weight 1/40 of the joint) is rare and the last common.
    premium = [row[0] for row in rows[:100]]
    assert sum(value >= 50 for value in premium) > sum(value < 10 for value in premium)
    # Given a score bin the class a row carries is the bin of its value: a row of the last bin
    # that is not fixed draws plan from the last bin's table.
    free = rows[250:]
    assert any(row[0] >= 50 for row in free) and any(row[0] < 10 for row in free)


def test_a_group_cannot_fix_a_column_drawn_from_bins() -> None:
    with pytest.raises(FluxError) as caught:
        _plan([group({"score": "0"}, _ROWS)])
    assert caught.value.code.code == "FLX-3643"
    assert "score" in str(caught.value)


def test_a_group_that_leaves_every_bin_no_weight_is_refused() -> None:
    declared = {**_DECLARED, "plan": {**_DECLARED["plan"]}}
    declared["plan"]["levelWeightsGiven"] = {
        "0": {"basic": "1", "premium": "0"},
        "10": {"basic": "1", "premium": "0"},
        "50": {"basic": "1", "premium": "0"},
    }
    with pytest.raises(FluxError) as caught:
        _plan([group({"plan": "premium"}, _ROWS)], declared)
    assert caught.value.code.code == "FLX-3646"
