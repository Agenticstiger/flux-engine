# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Family 1: the aggregate metrics behave like the similarity scores they are named after.

Properties over random statistics, built directly as the engine's own
``GroupStatistic``/``PropertyStatistic`` values:

* every score lies in [0, 1];
* a statistic scores exactly 1 against itself;
* scores are symmetric;
* KSComplement is never below TVComplement, because on ordered bins the
  largest cumulative difference is at most the total variation;
* two histograms with disjoint occupied bins score TVComplement 0.
"""

from __future__ import annotations

from tests.accuracy.harness.deps import require

require("scipy")

from typing import List, Tuple  # noqa: E402

from hypothesis import assume, given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from flux_engine.concordance.boundary.statistic import (  # noqa: E402
    UNGROUPED,
    GroupStatistic,
    PropertyStatistic,
)
from flux_engine.concordance.predicate import PropertyKind  # noqa: E402
from tests.accuracy.fidelity.s_metrics import (  # noqa: E402
    contingency_similarity,
    correlation_similarity,
    ks_complement,
    tv_complement,
)
from tests.accuracy.harness.tier import current_tier  # noqa: E402

TIER = current_tier()
BINS = 6

counts = st.lists(st.integers(min_value=0, max_value=10_000), min_size=BINS, max_size=BINS).filter(
    lambda values: sum(values) > 0
)


def histogram(values: List[int], groups: Tuple[str, ...] = (UNGROUPED,)) -> PropertyStatistic:
    return PropertyStatistic(
        property_id="p",
        kind=PropertyKind.MARGINAL_HISTOGRAM,
        n=sum(values) * len(groups),
        groups=tuple(
            GroupStatistic(group=group, k=sum(values), bin_counts=tuple(values)) for group in groups
        ),
    )


@TIER.property_settings(pr=200, nightly=5_000, label="s-metric-histogram-axioms")
@given(left=counts, right=counts)
def test_histogram_scores_are_bounded_symmetric_and_ordered(
    left: List[int], right: List[int]
) -> None:
    a, b = histogram(left), histogram(right)
    tvc, ksc = tv_complement(a, b), ks_complement(a, b)
    assert 0 <= tvc <= 1 and 0 <= ksc <= 1
    assert tv_complement(a, a) == 1 and ks_complement(a, a) == 1
    assert tvc == tv_complement(b, a) and ksc == ks_complement(b, a)
    assert ksc >= tvc


@TIER.property_settings(pr=100, nightly=2_000, label="s-metric-disjoint")
@given(split=st.integers(min_value=1, max_value=BINS - 1), mass=st.integers(1, 1000))
def test_disjoint_histograms_score_zero(split: int, mass: int) -> None:
    left = [mass if index < split else 0 for index in range(BINS)]
    right = [0 if index < split else mass for index in range(BINS)]
    assert tv_complement(histogram(left), histogram(right)) == 0


@TIER.property_settings(pr=100, nightly=2_000, label="s-metric-contingency-axioms")
@given(left=counts, right=counts)
def test_contingency_scores_are_bounded_and_symmetric(left: List[int], right: List[int]) -> None:
    a = histogram(left, groups=("DE", "FR"))
    b = histogram(right, groups=("DE", "GB"))
    score = contingency_similarity(a, b)
    assert 0 <= score <= 1
    assert contingency_similarity(a, a) == 1
    assert score == contingency_similarity(b, a)


def moments(xs: List[int], ys: List[int]) -> PropertyStatistic:
    group = GroupStatistic(
        group=UNGROUPED,
        k=len(xs),
        sum_x=sum(xs),
        sum_y=sum(ys),
        sum_xx=sum(x * x for x in xs),
        sum_yy=sum(y * y for y in ys),
        sum_xy=sum(x * y for x, y in zip(xs, ys, strict=True)),
    )
    return PropertyStatistic(
        property_id="c", kind=PropertyKind.CORRELATION, n=len(xs), groups=(group,)
    )


pairs = st.lists(
    st.tuples(st.integers(-(10**9), 10**9), st.integers(-(10**9), 10**9)), min_size=3, max_size=40
)


@TIER.property_settings(pr=200, nightly=5_000, label="s-metric-correlation-axioms")
@given(left=pairs, right=pairs)
def test_correlation_similarity_is_bounded_and_symmetric(
    left: List[Tuple[int, int]], right: List[Tuple[int, int]]
) -> None:
    def varied(points: List[Tuple[int, int]]) -> bool:
        return len({x for x, _ in points}) > 1 and len({y for _, y in points}) > 1

    assume(varied(left) and varied(right))
    a = moments([x for x, _ in left], [y for _, y in left])
    b = moments([x for x, _ in right], [y for _, y in right])
    score = correlation_similarity(a, b)
    assert 0 <= score <= 1
    assert correlation_similarity(a, a) == 1
    assert score == correlation_similarity(b, a)
