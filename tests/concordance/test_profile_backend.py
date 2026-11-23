# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The profile path: a property's reference read from a profile, not a slice.

The anchor is equivalence. A profile formed over the vectors' committed
slice, under the vectors' block, must give every one of the vectors'
evaluations the outcomes and refusal codes the slice path gives, because
both sides are formed by one statistic and compared by one registered
definition. The rest pins what only a profile can do: withhold a group or a
cell (censored, never scored as zero), and fail to answer a property at all
(``FLX-5037``).
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.verifier import (
    CENSORED,
    OUTSIDE,
    WITHIN,
    ClearProfile,
    ClearStatistic,
    FidelityEvaluation,
    _published_cells,
)
from flux_stats import Check, GroupStatistic, Parameters, definition_for_kind
from flux_stats.fixedpoint import Grid, total_variation_within
from flux_stats.profile import FREE, ProfileGroup, parse_profile
from tests.stats.profiles import edited, profile_from_rows, statistic_of

pytestmark = pytest.mark.unit

EXPOSE = "recovery_moment"


def _shape(evaluation: FidelityEvaluation) -> Tuple[Any, ...]:
    """What two evaluations must share: outcomes, group detail and refusal codes."""
    return (
        evaluation.within,
        tuple(
            (
                outcome.property_id,
                outcome.outcome,
                tuple(
                    (group.group, group.committed_count, group.synthetic_count, group.outcome)
                    for group in outcome.groups
                ),
            )
            for outcome in evaluation.outcomes
        ),
        tuple((refusal.code, refusal.property_id) for refusal in evaluation.refusals),
    )


@pytest.fixture()
def profile_document(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> Dict[str, Any]:
    return profile_from_rows(committed_rows, compiled)


def _profiled(
    compiled: CompiledFidelity, document: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> FidelityEvaluation:
    return ClearProfile(
        compiled=compiled, profile=parse_profile(document), synthetic_rows=list(rows)
    ).evaluate()


def _outcome(evaluation: FidelityEvaluation, property_id: str) -> Any:
    return next(entry for entry in evaluation.outcomes if entry.property_id == property_id)


def test_a_profile_of_the_committed_slice_agrees_with_the_slice_path(
    vectors: Mapping[str, Any],
    compiled: CompiledFidelity,
    committed: Any,
    profile_document: Dict[str, Any],
) -> None:
    for case in vectors["evaluations"]:
        rows = case["syntheticRows"]
        sliced = ClearStatistic(compiled=compiled, committed=committed, synthetic_rows=rows)
        assert _shape(_profiled(compiled, profile_document, rows)) == _shape(
            sliced.evaluate()
        ), case["id"]


def test_a_suppressed_group_is_censored_not_scored(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    document = edited(profile_document)
    grouped = next(
        entry
        for entry in document["tables"][EXPOSE]["columns"]["amount"]["statistics"]
        if entry.get("groupBy") == "market"
    )
    grouped["groups"][1] = {"group": grouped["groups"][1]["group"], "status": "A"}
    evaluation = _profiled(compiled, document, committed_rows)
    outcome = _outcome(evaluation, "003:mean:amount@market")
    assert [group.outcome for group in outcome.groups] == [WITHIN, CENSORED]
    assert outcome.outcome == WITHIN and evaluation.within
    grouped["groups"] = [{"group": "ie", "status": "A"}, {"group": "uk", "status": "D"}]
    evaluation = _profiled(compiled, document, committed_rows)
    outcome = _outcome(evaluation, "003:mean:amount@market")
    assert outcome.outcome == CENSORED and outcome.censored
    assert outcome in evaluation.censored and outcome not in evaluation.outside
    assert evaluation.within, "the other properties were scored, and all are within"


def test_an_evaluation_that_scored_nothing_is_not_within(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    document = edited(profile_document)
    table = document["tables"][EXPOSE]
    for column in table["columns"].values():
        for entry in column["statistics"]:
            entry["groups"] = [
                {"group": group["group"], "status": "A"} for group in entry["groups"]
            ]
    for pair in table["columnPairs"]:
        for entry in pair["statistics"]:
            entry["groups"] = [
                {"group": group["group"], "status": "A"} for group in entry["groups"]
            ]
    evaluation = _profiled(compiled, document, committed_rows)
    assert all(outcome.censored for outcome in evaluation.outcomes)
    assert not evaluation.within and not evaluation.outside and not evaluation.refused


def test_a_suppressed_cell_is_censored_and_never_scored_as_zero() -> None:
    bins = definition_for_kind("marginalHistogram")
    reference = ProfileGroup(
        key="",
        status=FREE,
        statistic=GroupStatistic(group="", k=100, bin_counts=(50, 0, 47)),
        cell_status=(FREE, "A", FREE),
    )
    synthetic = GroupStatistic(group="", k=100, bin_counts=(50, 3, 47))
    compared = _published_cells(bins, reference, synthetic)
    assert compared is not None
    published_reference, published_synthetic = compared
    assert (published_reference.k, published_reference.bin_counts) == (97, (50, 47))
    assert (published_synthetic.k, published_synthetic.bin_counts) == (97, (50, 47))
    assert bins.compare(published_synthetic, published_reference, _check(margin="0")) is True
    as_zero = GroupStatistic(group="", k=97, bin_counts=(50, 0, 47))
    assert bins.compare(synthetic, as_zero, _check(margin="1/50")) is False, "scored as zero"


def test_rounding_and_suppression_together_compare_the_published_cells() -> None:
    # Twenty cells of 12 records and three of 2, under a rule that withholds
    # a cell below 10 and rounds the others, and the total, to a multiple of
    # 5. A synthetic side that is exactly the truth must be within.
    bins = definition_for_kind("marginalHistogram")
    truth = (12,) * 20 + (2,) * 3
    profiled = ProfileGroup(
        key="",
        status=FREE,
        statistic=GroupStatistic(group="", k=245, bin_counts=(10,) * 20 + (0,) * 3),
        cell_status=(FREE,) * 20 + ("A",) * 3,
    )
    synthetic = GroupStatistic(group="", k=sum(truth), bin_counts=truth)
    compared = _published_cells(bins, profiled, synthetic)
    assert compared is not None
    reference, published = compared
    assert (reference.k, published.k) == (200, 240)
    assert bins.compare(published, reference, _check(margin="1/20")) is True
    # Pooling what k leaves over the published cells (245 - 200 = 45 records,
    # where the truth holds 6) would report this very run outside.
    assert not total_variation_within(
        counts_a=(12,) * 20 + (6,),
        count_a=246,
        counts_b=(10,) * 20 + (45,),
        count_b=245,
        tolerance=Fraction(1, 20),
    )


def test_rounded_counts_compare_against_their_own_total() -> None:
    levels = definition_for_kind("levelHistogram")
    reference = ProfileGroup(
        key="",
        status=FREE,
        statistic=GroupStatistic(group="", k=100, level_counts=(55, 50)),
        cell_status=(FREE, FREE),
    )
    synthetic = GroupStatistic(group="", k=105, level_counts=(55, 50))
    compared = _published_cells(levels, reference, synthetic)
    assert compared is not None and compared[0].k == 105
    assert levels.compare(synthetic, compared[0], _check(margin="0")) is True
    rounded_away = ProfileGroup(
        key="",
        status=FREE,
        statistic=GroupStatistic(group="", k=5, level_counts=(10, 0)),
        cell_status=(FREE, "A"),
    )
    compared = _published_cells(levels, rounded_away, synthetic)
    assert compared is not None
    assert (compared[0].k, compared[0].level_counts) == (10, (10,)), "k counts no withheld cell"
    assert (compared[1].k, compared[1].level_counts) == (55, (55,))


def _histogram(document: Mapping[str, Any]) -> Dict[str, Any]:
    statistics = document["tables"][EXPOSE]["columns"]["amount"]["statistics"]
    return next(entry for entry in statistics if entry["definition"] == "marginalHistogram/1")


def test_a_group_whose_every_cell_was_withheld_is_censored(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    document = edited(profile_document)
    (group,) = _histogram(document)["groups"]
    group.update(binCounts=[None, None, None], cellStatus=["A", "D", "A"])
    evaluation = _profiled(compiled, document, committed_rows)
    outcome = _outcome(evaluation, "001:marginalHistogram:amount")
    assert outcome.outcome == CENSORED
    assert [(group.committed_count, group.outcome) for group in outcome.groups] == [(8, CENSORED)]
    assert evaluation.within and not evaluation.refused


@pytest.mark.parametrize(
    "published, remap, subject",
    [
        pytest.param(
            [0, None, None],
            {},
            "profile: 001:marginalHistogram:amount, in the cells it published",
            id="the-profile-publishes-only-an-empty-cell",
        ),
        pytest.param(
            [1, None, None],
            {"1": "2"},
            "synthetic dataset: 001:marginalHistogram:amount, in the cells the profile published",
            id="the-synthetic-side-misses-every-published-cell",
        ),
    ],
)
def test_a_side_with_no_record_in_the_published_cells_is_refused(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
    published: List[Any],
    remap: Dict[str, str],
    subject: str,
) -> None:
    document = edited(profile_document)
    (group,) = _histogram(document)["groups"]
    group.update(binCounts=published, cellStatus=["F", "A", "A"])
    rows = [dict(row, amount=remap.get(row["amount"], row["amount"])) for row in committed_rows]
    evaluation = _profiled(compiled, document, rows)
    (refusal,) = [
        refusal
        for refusal in evaluation.refusals
        if refusal.property_id == "001:marginalHistogram:amount"
    ]
    assert (refusal.code, refusal.reason) == ("FLX-5005", f"{subject}: 0 records, minimum is 1")
    assert _outcome(evaluation, "001:marginalHistogram:amount").outcome == OUTSIDE
    assert not evaluation.within


def _check(*, margin: str) -> Check:
    return Check(subject="p0", tolerance="distance", margin=Fraction(margin))


@pytest.mark.parametrize(
    "breaks, reason",
    [
        (lambda d: d["tables"].pop(EXPOSE), "the profile holds no table for this expose"),
        (
            lambda d: d["tables"][EXPOSE]["columns"]["amount"]["statistics"].clear(),
            "the profile holds no mean/1 statistic",
        ),
        (
            lambda d: d["tables"][EXPOSE]["columns"]["attempts"].update(domain="timestamp"),
            "the profile reads column 'attempts' as timestamp, the declaration as number",
        ),
        (
            lambda d: d["encoding"].update(fractionalBits=8),
            "the profile forms mean/1 on fractionalBits 8, the block declares 4",
        ),
    ],
)
def test_a_profile_that_cannot_answer_a_property_is_refused_with_flx_5037(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
    breaks: Any,
    reason: str,
) -> None:
    document = edited(profile_document)
    breaks(document)
    evaluation = _profiled(compiled, document, committed_rows)
    assert evaluation.refused and not evaluation.within
    assert any(
        refusal.code == "FLX-5037" and refusal.reason.startswith(reason)
        for refusal in evaluation.refusals
    ), evaluation.refusals


def test_a_histogram_needs_the_blocks_grid_and_a_member_list_does_not(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    document = edited(profile_document)
    document["encoding"]["fractionalBits"] = 8
    evaluation = _profiled(compiled, document, committed_rows)
    refused = {refusal.property_id: refusal for refusal in evaluation.refusals}
    histogram = refused["001:marginalHistogram:amount"]
    assert histogram.code == "FLX-5037"
    assert histogram.reason == (
        "the profile forms marginalHistogram/1 on fractionalBits 8, the block declares 4"
    )
    assert _outcome(evaluation, "002:support:market").outcome == WITHIN
    assert "002:support:market" not in refused
    assert refused["000:mean:amount"].code == "FLX-5037"


def test_a_coarser_grid_can_move_a_value_across_a_bin_edge() -> None:
    # Why a histogram is pinned to the block's grid: the edge 1/2 is a grid
    # point with one fractional bit and not with none, where it floors to 0,
    # so a value of 1/4, below the edge, is counted in the bin above it.
    bins = definition_for_kind("marginalHistogram")
    edges = (Fraction(0), Fraction(1, 2), Fraction(1))

    def binned(fractional_bits: int, value: Fraction) -> Tuple[int, ...]:
        grid = Grid(fractional_bits=fractional_bits)
        units = tuple(grid.quantise(edge, subject="edge") for edge in edges)
        values = [grid.quantise(value, subject="value")]
        return bins.measure("", values, Parameters(edges=units)).bin_counts

    assert binned(1, Fraction(1, 4)) == (1, 0)
    assert binned(0, Fraction(1, 4)) == (0, 1)
    assert binned(1, Fraction(3, 4)) == binned(0, Fraction(3, 4)) == (0, 1)


def test_the_profile_side_meets_the_fail_closed_conditions_of_section_5(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    few = edited(profile_document)
    few["tables"][EXPOSE]["rowCount"] = 3
    refused = _profiled(compiled, few, committed_rows)
    assert [(r.code, r.reason) for r in refused.refusals] == [
        ("FLX-5005", "profile: 3 records, minimum is 4")
    ]
    wide = edited(profile_document)
    wide["tables"][EXPOSE]["columns"]["amount"]["metrics"] = {
        "max": {
            "definition": "max/1",
            "status": "F",
            "value": "99",
            "sampleSize": {"min": 8, "max": 8},
            "disclosure": "none",
        }
    }
    outside = _profiled(compiled, wide, committed_rows)
    assert (
        "FLX-5005",
        "the profile's values of column 'amount' lie outside the declared range",
    ) in [(r.code, r.reason) for r in outside.refusals]
    small = edited(profile_document)
    grouped = next(
        entry
        for entry in small["tables"][EXPOSE]["columns"]["amount"]["statistics"]
        if entry.get("groupBy") == "market"
    )
    grouped["groups"][0]["k"] = 3
    below = _profiled(compiled, small, committed_rows)
    assert (
        "FLX-5005",
        "profile: 003:mean:amount@market group #0: 3 records, minimum is 4",
    ) in [(r.code, r.reason) for r in below.refusals]


def test_a_quantile_refusal_names_the_profile_side(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    document = edited(profile_document)
    quantile = statistic_of(document, EXPOSE, "amount", "quantile/1")
    quantile["groups"][0]["quantileBelow"] = quantile["groups"][0]["quantileAtOrBelow"]
    evaluation = _profiled(compiled, document, committed_rows)
    assert any(
        refusal.reason.startswith("the profile side's quantile witness for 005:quantile:amount")
        for refusal in evaluation.refusals
    ), evaluation.refusals


def test_a_refusal_names_which_of_groupby_bins_levels_or_level_the_profile_lacks(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    profile_document: Dict[str, Any],
) -> None:
    document = edited(profile_document)
    statistics = document["tables"][EXPOSE]["columns"]["amount"]["statistics"]
    histograms = [entry for entry in statistics if entry["definition"] == "marginalHistogram/1"]
    assert histograms, statistics
    for entry in histograms:
        entry["bins"] = [*entry["bins"][:-1], "999"]
    evaluation = _profiled(compiled, document, committed_rows)
    reason = next(
        refusal.reason
        for refusal in evaluation.refusals
        if refusal.property_id == "001:marginalHistogram:amount"
    )
    assert reason == (
        "the profile holds marginalHistogram/1 over this property's columns, but not with "
        "this property's bins"
    )
