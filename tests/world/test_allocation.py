# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The two allocations population materialisation makes before any draw."""

from __future__ import annotations

from decimal import Decimal

import pytest

from flux_engine.core.lrm import WEIGHT_SUM_TOLERANCE, largest_remainder
from flux_engine.world.population import _blocks, _equal_share

pytestmark = pytest.mark.unit


def test_blocks_lay_labels_down_in_nfc_sorted_order_whatever_the_mapping_order() -> None:
    """Ordinal 0 is in the first cohort **by name**, never by declaration order.

    Declaration order is a YAML accident: two bundles that declare the same mix
    in a different order must produce the same subject in the same cohort, or
    the population digest depends on how the document was typed.
    """
    assert _blocks({"b": 2, "a": 1}) == ("a", "b", "b")
    assert _blocks({"a": 1, "b": 2}) == _blocks({"b": 2, "a": 1})


def test_blocks_are_contiguous_and_total() -> None:
    allocation = {"new": 3, "active": 5, "atrisk": 2}
    labels = _blocks(allocation)
    assert len(labels) == sum(allocation.values())
    boundaries = [labels[0]]
    for label in labels:
        if label != boundaries[-1]:
            boundaries.append(label)
    assert boundaries == sorted(allocation)


def test_an_empty_allocation_lays_down_nothing() -> None:
    assert _blocks({}) == ()


@pytest.mark.parametrize("count", [1, 2, 3, 7, 11])
def test_equal_shares_sum_to_one_within_the_allocators_tolerance(count: int) -> None:
    """``1/3`` is not representable; the last name absorbs the remainder.

    Exactly one is not achievable in decimal for every count -- ``1/11`` at 28
    significant digits does not re-sum to one -- and is not what
    :func:`~flux_engine.core.lrm.largest_remainder` asks for: KERNEL-SPEC §5
    requires the weights to sum to 1 "within 1e-9". The residual here is around
    ``1e-28``, nineteen orders of magnitude inside that.
    """
    names = tuple(f"p{index}" for index in range(count))
    weights = _equal_share(names, 0)
    assert abs(sum(weights.values()) - Decimal(1)) < WEIGHT_SUM_TOLERANCE


@pytest.mark.parametrize("count", [1, 2, 3, 7, 11])
def test_equal_shares_allocate_a_population_evenly(count: int) -> None:
    names = tuple(f"p{index}" for index in range(count))
    allocation = largest_remainder(100, dict(_equal_share(names, 100)))
    assert sum(allocation.values()) == 100
    assert max(allocation.values()) - min(allocation.values()) <= 1
