# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The failure line of a rank correlation or a property grouped by bins says what it measured.

Both used to print only ``outside the declared margin``: a rank correlation named neither the run's
value, the profile's, nor how far apart they may be, and a property grouped by the bins of a column
named no group, because only ``groupBy`` was read. Aggregates only, as everywhere in a failure line:
never a value of the data, and a group by its ordinal.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict

import pytest

from flux_engine.concordance.rank import compare_rank
from flux_engine.concordance.verifier import GroupOutcome, PropertyOutcome
from flux_engine.stages.verify import _failure_lines, _outside_document
from flux_stats.fidelity import (
    BinsGrouping,
    PropertyDeclaration,
    PropertyKind,
    Tolerance,
    ToleranceKind,
)

pytestmark = pytest.mark.unit


def _group(outcome: str) -> GroupOutcome:
    return GroupOutcome(group="", committed_count=5, synthetic_count=5, outcome=outcome)


def _rank_outcome() -> PropertyOutcome:
    declaration = PropertyDeclaration(
        index=0,
        kind=PropertyKind.RANK_CORRELATION,
        columns=("a", "b"),
        tolerance=Tolerance(ToleranceKind.ABSOLUTE, Fraction(1, 20)),
        measure="kendall",
    )
    found = compare_rank(
        declaration,
        Fraction(1, 20),
        run=Fraction(3, 10),
        records=400,
        reference=Fraction(4, 5),
        variance=None,
    )
    return PropertyOutcome(
        property_id=declaration.property_id,
        kind=PropertyKind.RANK_CORRELATION,
        columns=("a", "b"),
        group_by=None,
        tolerance={"type": "absolute", "max": "0.05"},
        margin="0.05",
        epsilon="0",
        outcome="outside",
        groups=(_group("outside"),),
        rank=found,
    )


def _bins_outcome() -> PropertyOutcome:
    return PropertyOutcome(
        property_id="000:levelHistogram:market@amount#bins",
        kind=PropertyKind.LEVEL_HISTOGRAM,
        columns=("market",),
        group_by=None,
        tolerance={"type": "distance", "max": "0.1"},
        margin="0.1",
        epsilon="0",
        outcome="outside",
        groups=(_group("within"), _group("outside"), _group("outside")),
        group_by_bins=BinsGrouping(
            column="amount", bins=(Fraction(0), Fraction(5), Fraction(9), Fraction(20))
        ),
    )


def _lines(outcome: PropertyOutcome) -> list[str]:
    document: Dict[str, Any] = {
        "status": "failed",
        "refusals": [],
        "outside": [_outside_document(outcome, refused=False)],
    }
    return _failure_lines(document)


def test_a_rank_correlation_names_both_values_their_distance_the_margin_and_the_reach() -> None:
    (line,) = _lines(_rank_outcome())
    assert line.startswith(
        "property 000:rankCorrelation:a+b (rankCorrelation of a, b): outside the declared margin ("
    )
    assert "run 0.3, profile 0.8, distance 0.5, margin 0.05, reach " in line


def test_a_property_grouped_by_bins_names_the_bins_column_the_margin_and_the_failing_groups() -> (
    None
):
    (line,) = _lines(_bins_outcome())
    assert line == (
        "property 000:levelHistogram:market@amount#bins "
        "(levelHistogram of market, grouped by the bins of amount): "
        "outside the declared margin (margin 0.1) in groups #1, #2"
    )


def test_a_refused_property_keeps_its_line_without_measurements() -> None:
    document: Dict[str, Any] = {
        "status": "failed",
        "refusals": [],
        "outside": [_outside_document(_rank_outcome(), refused=True)],
    }
    (line,) = _failure_lines(document)
    assert line.endswith("refused, not compared")


def test_an_outcome_that_lost_its_grouping_still_names_the_bins_column_from_its_id() -> None:
    # The comparison with a committed slice builds its outcome without the grouping.
    from dataclasses import replace

    (line,) = _lines(replace(_bins_outcome(), group_by_bins=None))
    assert "grouped by the bins of amount" in line and line.endswith("in groups #1, #2")
