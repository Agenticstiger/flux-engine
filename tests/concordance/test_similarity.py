# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A property's similarity to its reference: one exact measure per statistic, in ``[0, 1]``.

The measures are SDMetrics' (TVComplement, StatisticSimilarity,
CategoryAdherence, CorrelationSimilarity) formed from the integer quantities
the comparison reads. What is pinned here: each formula on hand-worked
numbers, the floor at zero, the degenerate cases, a correlation's score being
an exact lower bound of the true one, and every registered statistic having
a measure.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from flux_engine.concordance.similarity import (
    CORRELATION_PLACES,
    similarity,
    similarity_identifiers,
    total_variation,
)
from flux_engine.core.errors import FluxError
from flux_stats import GroupStatistic, definition, definitions

pytestmark = pytest.mark.unit

MEAN = definition("mean/1")
HISTOGRAM = definition("marginalHistogram/1")
LEVELS = definition("levelHistogram/1")
SUPPORT = definition("support/1")
CORRELATION = definition("correlation/1")
QUANTILE = definition("quantile/1")


def test_every_registered_statistic_has_a_similarity() -> None:
    assert similarity_identifiers() == tuple(sorted(item.identifier for item in definitions()))


def test_a_count_table_scores_one_minus_its_total_variation() -> None:
    synthetic = GroupStatistic(group="", k=10, bin_counts=(5, 3, 2))
    reference = GroupStatistic(group="", k=20, bin_counts=(10, 10, 0))
    # |5/10 - 10/20| + |3/10 - 10/20| + |2/10 - 0| = 0 + 1/5 + 1/5, halved.
    assert total_variation((5, 3, 2), 10, (10, 10, 0), 20) == Fraction(1, 5)
    assert similarity(HISTOGRAM, synthetic, reference) == Fraction(4, 5)
    levelled = GroupStatistic(group="", k=4, level_counts=(1, 1, 2))
    same = GroupStatistic(group="", k=8, level_counts=(2, 2, 4))
    assert similarity(LEVELS, levelled, same) == 1


def test_a_count_table_with_no_record_on_a_side_is_refused() -> None:
    with pytest.raises(FluxError) as raised:
        similarity(
            HISTOGRAM,
            GroupStatistic(group="", k=0, bin_counts=(0, 0)),
            GroupStatistic(group="", k=3, bin_counts=(1, 2)),
        )
    assert raised.value.code.code == "FLX-5005"


def test_a_mean_scores_its_difference_over_the_declared_width() -> None:
    synthetic = GroupStatistic(group="", k=4, sum_x=40)  # mean 10 units
    reference = GroupStatistic(group="", k=2, sum_x=30)  # mean 15 units
    assert similarity(MEAN, synthetic, reference, width_units=100) == Fraction(19, 20)
    # Never below zero: a difference wider than the range scores 0.
    assert similarity(MEAN, synthetic, reference, width_units=4) == 0


def test_a_range_of_one_grid_point_scores_agreement_or_nothing() -> None:
    one = GroupStatistic(group="", k=3, sum_x=21)
    assert similarity(MEAN, one, GroupStatistic(group="", k=1, sum_x=7), width_units=0) == 1
    assert similarity(MEAN, one, GroupStatistic(group="", k=1, sum_x=8), width_units=0) == 0


def test_a_mean_or_a_quantile_needs_its_declared_width() -> None:
    side = GroupStatistic(group="", k=1, sum_x=1)
    with pytest.raises(FluxError) as raised:
        similarity(MEAN, side, side)
    assert raised.value.code.code == "FLX-5037"


def test_a_quantile_scores_its_witnesses_distance_over_the_width() -> None:
    synthetic = GroupStatistic(group="", k=9, quantile_witness=70)
    reference = GroupStatistic(group="", k=5, quantile_witness=40)
    assert similarity(QUANTILE, synthetic, reference, width_units=120) == Fraction(3, 4)


def test_support_scores_the_share_of_records_on_the_reference_support() -> None:
    synthetic = GroupStatistic(group="", k=10, support=("DE", "GB", "XX"), support_counts=(4, 4, 2))
    reference = GroupStatistic(group="", k=5, support=("DE", "FR", "GB"))
    assert similarity(SUPPORT, synthetic, reference) == Fraction(4, 5)
    assert similarity(SUPPORT, GroupStatistic(group="", k=0), reference) == 1


def _moments(xs: list, ys: list) -> GroupStatistic:
    return GroupStatistic(
        group="",
        k=len(xs),
        sum_x=sum(xs),
        sum_y=sum(ys),
        sum_xx=sum(x * x for x in xs),
        sum_yy=sum(y * y for y in ys),
        sum_xy=sum(x * y for x, y in zip(xs, ys, strict=True)),
    )


def _true_similarity(a: GroupStatistic, b: GroupStatistic) -> Decimal:
    """1 - |r_a - r_b| / 2 at 60 digits, as an oracle for the exact bound."""
    with localcontext() as context:
        context.prec = 60
        r = []
        for side in (a, b):
            m = side.moments
            r.append(Decimal(m.numerator) / (Decimal(m.qx) * Decimal(m.qy)).sqrt())
        return 1 - abs(r[0] - r[1]) / 2


@pytest.mark.parametrize(
    "xs, ys, xr, yr",
    [
        ([1, 2, 3, 4, 5], [2, 4, 5, 4, 5], [1, 2, 3, 4], [1, 3, 2, 4]),
        ([1, 5, 2, 8, 3], [9, 1, 7, 2, 6], [3, 1, 4, 1, 5], [9, 2, 6, 5, 3]),
        ([10, 20, 30], [30, 20, 11], [1, 2, 3], [1, 2, 3]),
    ],
)
def test_a_correlation_scores_an_exact_lower_bound_of_the_true_score(
    xs: list, ys: list, xr: list, yr: list
) -> None:
    synthetic, reference = _moments(xs, ys), _moments(xr, yr)
    bound = similarity(CORRELATION, synthetic, reference)
    truth = _true_similarity(synthetic, reference)
    places = Fraction(1, 10**CORRELATION_PLACES)
    assert Fraction(truth) - places <= bound <= Fraction(truth)
    # Terminating at the declared number of places, so it is written as a decimal.
    assert (bound / places).denominator == 1


def test_equal_correlations_score_exactly_one() -> None:
    a = _moments([1, 2, 3, 4], [2, 4, 6, 9])
    doubled = _moments([2, 4, 6, 8], [4, 8, 12, 18])  # the same correlation, scaled
    assert similarity(CORRELATION, a, doubled) == 1
    opposite = _moments([1, 2, 3, 4], [-2, -4, -6, -9])
    assert similarity(CORRELATION, a, opposite) < 1


def test_every_measure_lies_between_zero_and_one() -> None:
    cases = [
        (MEAN, GroupStatistic(group="", k=3, sum_x=-900), GroupStatistic(group="", k=3, sum_x=900)),
        (
            QUANTILE,
            GroupStatistic(group="", k=1, quantile_witness=0),
            GroupStatistic(group="", k=1, quantile_witness=10**9),
        ),
        (
            HISTOGRAM,
            GroupStatistic(group="", k=1, bin_counts=(1, 0)),
            GroupStatistic(group="", k=1, bin_counts=(0, 1)),
        ),
    ]
    for statistic, a, b in cases:
        score = similarity(statistic, a, b, width_units=600)
        assert 0 <= score <= 1
    assert similarity(HISTOGRAM, cases[2][1], cases[2][2]) == 0
    assert similarity(MEAN, cases[0][1], cases[0][2], width_units=600) == 0
