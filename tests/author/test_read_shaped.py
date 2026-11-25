# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A clip of a column's bins records as read the statistics that placed them (ADR 0014 section 1).

A contract range clips the bins a profile gave, and the clip replaces the labels that cited the
statistics which placed their edges and weights. Those statistics still shape what is drawn, so
the clip records each as read, with the reason ``_RANGE_SHAPED``, before the labels go.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Dict, List, Optional, Tuple

import pytest

from flux_engine.author import columns
from flux_engine.author.basis import Basis, Label, assumed, from_contract, from_profile
from flux_engine.author.columns import ColumnPlan, _clip_bins, _draw_clipped
from flux_engine.seam.contract import LogicalKind

pytestmark = pytest.mark.unit

_COLUMN = ("tables", "orders", "columns", "c")
_MIN = _COLUMN + ("metrics", "min")
_MAX = _COLUMN + ("metrics", "max")
_QUANTILES = _COLUMN + ("metrics", "quantiles")
_HISTOGRAM = _COLUMN + ("statistics", "0")

_CONTRACT = from_contract("shop", ("exposes", "0", "contract", "schema", "2", "validationRules"))
_BINS = ["0", "10", "20", "30"]
_WEIGHTS = ["0.2", "0.5", "0.3"]


def _plan(weights: Optional[List[str]] = None, extra: Tuple[Label, ...] = ()) -> ColumnPlan:
    """Three bins: the minimum places the first edge, the quantiles the two between, the maximum
    the last, and the histogram every weight."""
    bases = [
        from_profile(_MIN, "min/1"),
        from_profile(_QUANTILES, "quantiles/1"),
        from_profile(_QUANTILES, "quantiles/1"),
        from_profile(_MAX, "max/1"),
    ]
    labels = [Label(("bins", str(position)), basis) for position, basis in enumerate(bases)]
    labels += [
        Label(("binWeights", str(position)), from_profile(_HISTOGRAM, "marginalHistogram/1"))
        for position in range(3)
    ]
    return ColumnPlan(
        name="c",
        declaration={"bins": list(_BINS), "binWeights": list(weights or _WEIGHTS)},
        labels=[*labels, *extra],
    )


def _bases(plan: ColumnPlan, head: str) -> Dict[str, Basis]:
    return {
        label.tokens[1]: label.basis
        for label in plan.labels
        if label.tokens[0] == head and len(label.tokens) == 2
    }


def _cited(basis: Basis) -> Optional[Tuple[str, ...]]:
    return None if basis.statistic is None else basis.statistic[0]


_ALL_FOUR = {_MIN, _QUANTILES, _MAX, _HISTOGRAM}


def _reads(plan: ColumnPlan) -> Dict[Tuple[str, ...], str]:
    return dict(plan.reads)


def test_a_clip_that_drops_a_bin_reads_every_statistic_the_bins_cited() -> None:
    plan = _plan()
    dropped = _clip_bins(plan, Fraction(10), Fraction(30), _CONTRACT, Fraction(1))
    assert dropped == 1
    assert plan.declaration["bins"] == ["10", "20", "30"]
    assert plan.declaration["binWeights"] == ["0.625", "0.375"]
    # The minimum cited only the edge that went; it is read all the same.
    assert _reads(plan) == {tokens: columns._RANGE_SHAPED for tokens in _ALL_FOUR}
    assert len(plan.reads) == 4
    # The edges the range leaves keep what placed them; every weight is now assumed.
    edges = _bases(plan, "bins")
    assert [_cited(edges[key]) for key in sorted(edges)] == [_QUANTILES, _QUANTILES, _MAX]
    weights = _bases(plan, "binWeights")
    assert sorted(weights) == ["0", "1"]
    assert all(basis.statistic is None and basis.reason for basis in weights.values())


def test_a_clip_that_moves_an_edge_reads_the_statistic_that_placed_it() -> None:
    plan = _plan()
    assert _clip_bins(plan, Fraction(5), None, _CONTRACT, Fraction(1)) == 0
    assert plan.declaration["bins"] == ["5", "10", "20", "30"]
    assert plan.declaration["binWeights"] == _WEIGHTS
    assert _reads(plan) == {tokens: columns._RANGE_SHAPED for tokens in _ALL_FOUR}
    edges = _bases(plan, "bins")
    assert edges["0"] == _CONTRACT
    assert [_cited(edges[key]) for key in ("1", "2", "3")] == [_QUANTILES, _QUANTILES, _MAX]
    # No bin went, so each weight keeps its citation.
    assert {_cited(basis) for basis in _bases(plan, "binWeights").values()} == {_HISTOGRAM}


def test_a_clip_that_leaves_one_grid_value_reads_what_the_bins_cited() -> None:
    plan = _plan()
    assert _clip_bins(plan, Fraction(15), Fraction(15), _CONTRACT, Fraction(1)) == 2
    assert plan.declaration["levels"] == {"15": "1"}
    assert "bins" not in plan.declaration and "binWeights" not in plan.declaration
    assert _reads(plan) == {tokens: columns._RANGE_SHAPED for tokens in _ALL_FOUR}
    assert [(label.tokens, label.basis) for label in plan.labels] == [(("levels",), _CONTRACT)]


def test_a_clip_that_leaves_no_weight_reads_what_the_bins_cited() -> None:
    plan = _plan(weights=["0", "0.7", "0.3"])
    assert _clip_bins(plan, Fraction(0), Fraction(9), _CONTRACT, Fraction(1)) == 0
    assert plan.declaration["bins"] == ["0", "9"]
    assert plan.declaration["binWeights"] == ["1"]
    assert _reads(plan) == {tokens: columns._RANGE_SHAPED for tokens in _ALL_FOUR}
    assert {label.tokens: label.basis for label in plan.labels}[("bins",)] == _CONTRACT
    assert columns._RANGE_NO_ROW in plan.assumptions


def test_a_clip_that_changes_nothing_reads_nothing_more() -> None:
    plan = _plan()
    assert _clip_bins(plan, Fraction(0), Fraction(30), _CONTRACT, Fraction(1)) == 0
    assert plan.reads == []
    assert len(plan.labels) == 7


def test_drawing_the_clip_evenly_reads_what_the_bins_cited() -> None:
    plan = _plan()
    plan.declaration["levels"] = {"1": "1"}
    _draw_clipped(plan, Fraction(2), Fraction(7), _CONTRACT, Fraction(1))
    assert plan.declaration == {"bins": ["2", "7"], "binWeights": ["1"]}
    assert _reads(plan) == {tokens: columns._RANGE_SHAPED for tokens in _ALL_FOUR}
    labels = {label.tokens: label.basis for label in plan.labels}
    assert labels[("bins",)] == _CONTRACT
    assert labels[("binWeights",)].statistic is None


def test_drawing_the_clip_as_one_value_reads_what_the_bins_cited() -> None:
    plan = _plan()
    _draw_clipped(plan, Fraction(4), Fraction(4), _CONTRACT, Fraction(1))
    assert plan.declaration == {"levels": {"4": "1"}}
    assert _reads(plan) == {tokens: columns._RANGE_SHAPED for tokens in _ALL_FOUR}


def test_a_statistic_already_read_keeps_its_first_reason() -> None:
    plan = _plan()
    plan.reads.append((_MAX, "checked: within the bounds the rule gives"))
    _draw_clipped(plan, Fraction(2), Fraction(7), _CONTRACT, Fraction(1))
    assert [tokens for tokens, _ in plan.reads].count(_MAX) == 1
    assert _reads(plan)[_MAX] == "checked: within the bounds the rule gives"
    assert _reads(plan)[_MIN] == columns._RANGE_SHAPED


def test_only_the_labels_of_bins_and_weights_are_read() -> None:
    other = from_profile(_COLUMN + ("metrics", "nullCount"), "nullCount/1")
    plan = _plan(extra=(Label(("nullFraction",), other), Label(("bins", "x", "y"), assumed("r"))))
    _draw_clipped(plan, Fraction(2), Fraction(7), _CONTRACT, Fraction(1))
    assert set(_reads(plan)) == _ALL_FOUR


def test_a_cut_at_the_window_reads_every_statistic_the_bins_cited() -> None:
    plan = ColumnPlan(
        name="d",
        declaration={
            "bins": ["2022-01-01", "2024-01-01", "2026-01-01"],
            "binWeights": ["0.5", "0.5"],
        },
        labels=[
            Label(("bins", "0"), from_profile(_MIN, "min/1")),
            Label(("bins", "1"), from_profile(_QUANTILES, "quantiles/1")),
            Label(("bins", "2"), from_profile(_MAX, "max/1")),
            Label(("binWeights", "0"), from_profile(_HISTOGRAM, "marginalHistogram/1")),
        ],
    )
    columns.bound_to_window(
        plan, LogicalKind.DATE, ("2023-01-01T00:00:00Z", "2025-01-01T00:00:00Z")
    )
    assert plan.declaration["bins"] == ["2023-01-01", "2024-01-01", "2025-01-01"]
    assert _reads(plan) == {tokens: columns._WINDOW_SHAPED for tokens in _ALL_FOUR}
    assert {label.tokens for label in plan.labels} == {("bins",), ("binWeights",)}
    assert all(label.basis.statistic is None for label in plan.labels)


def test_a_cut_at_the_window_keeps_the_reasons_the_whole_bins_had_before_its_own() -> None:
    # The register holds one entry per pointer: a whole-array label the bins already had gives its
    # reason to the new label, which comes first, and the cut's reason comes last. A reason a
    # label already holds is not said twice.
    plan = ColumnPlan(
        name="d",
        declaration={
            "bins": ["2022-01-01", "2024-01-01", "2026-01-01"],
            "binWeights": ["0.5", "0.5"],
        },
        labels=[
            Label(("bins",), assumed("the first reason of the bins")),
            Label(("bins",), assumed("the second reason of the bins")),
            Label(("bins",), assumed("the first reason of the bins")),
            Label(("binWeights",), assumed("the reason of the weights")),
            Label(("bins", "0"), from_profile(_MIN, "min/1")),
        ],
    )
    columns.bound_to_window(
        plan, LogicalKind.DATE, ("2023-01-01T00:00:00Z", "2025-01-01T00:00:00Z")
    )
    reasons = {label.tokens: label.basis.reason for label in plan.labels}
    assert reasons == {
        ("bins",): (
            "the first reason of the bins the second reason of the bins " + columns._WINDOW_CLIPPED
        ),
        ("binWeights",): "the reason of the weights " + columns._WINDOW_CLIPPED,
    }
    assert all(
        (label.basis.reason or "").endswith(columns._WINDOW_CLIPPED) for label in plan.labels
    )
