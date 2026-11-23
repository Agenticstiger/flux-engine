# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The held-out gate, on rows (ADR 0028 section 9).

A calibration is calibrated only when at least one held-out query has a target and every one that has a
target is within its tolerance. A query is within when it was measured, it has a tolerance, and its gap (each
group's, for a query with ``groupBy``) is no larger than the tolerance in size. The register records the
largest gap and the tolerance it met, as exact decimal text that never understates the gap.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional, Tuple

import pytest

from flux_engine.calibrate.gate import GateReport, HeldOutQuery, decimal_bound, judge
from flux_engine.concordance.queries import HELD_OUT, GroupRow, QueryRow

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64
PROFILE = "sha256:" + "3" * 64


def row(
    name: str,
    *,
    gap: Optional[str] = None,
    tolerance: Optional[str] = "0.02",
    status: str = "measured",
    groups: Tuple[GroupRow, ...] = (),
) -> QueryRow:
    return QueryRow(
        name,
        HELD_OUT,
        DIGEST,
        None,
        None,
        None,
        None,
        Fraction(gap) if gap is not None else None,
        Fraction(tolerance) if tolerance is not None else None,
        status,
        None,
        groups=groups,
    )


def group(ordinal: int, gap: Optional[str]) -> GroupRow:
    return GroupRow(
        ordinal,
        False,
        1,
        2,
        None,
        None,
        Fraction(gap) if gap is not None else None,
        "measured",
        None,
    )


def report(*queries: HeldOutQuery) -> GateReport:
    return GateReport(PROFILE, "P1", "P1", queries)


def test_a_query_within_its_tolerance_is_within_and_the_boundary_counts() -> None:
    judged, untargeted = judge(
        [row("a", gap="0.01"), row("b", gap="-0.02"), row("c", gap="0.021")],
        {"a", "b", "c"},
    )
    assert [(item.id, item.within) for item in judged] == [("a", True), ("b", True), ("c", False)]
    assert untargeted == ()
    assert judged[2].why == "its gap is larger than its tolerance"


def test_the_size_of_the_gap_is_judged_whatever_its_sign() -> None:
    judged, _ = judge([row("a", gap="-0.5")], {"a"})
    assert not judged[0].within and judged[0].gap == Fraction(-1, 2)


def test_a_grouped_query_is_within_only_when_each_group_is() -> None:
    inside = row("g", groups=(group(0, "0.01"), group(1, "-0.02")))
    outside = row("h", groups=(group(0, "0.01"), group(1, "0.03")))
    judged, _ = judge([inside, outside], {"g", "h"})
    assert [item.within for item in judged] == [True, False]
    assert judged[1].gap == Fraction(3, 100)


@pytest.mark.parametrize(
    "status", ["timedOut", "outOfMemory", "crashed", "error", "notARate", "noRows"]
)
def test_a_query_with_a_target_that_was_not_measured_is_a_miss(status: str) -> None:
    judged, _ = judge([row("a", status=status)], {"a"})
    assert not judged[0].within and status in judged[0].why


def test_a_query_with_no_tolerance_cannot_be_within_and_says_so() -> None:
    judged, _ = judge([row("a", gap="0", tolerance=None)], {"a"})
    assert not judged[0].within and "no tolerance" in judged[0].why


def test_a_query_the_profile_states_no_rate_for_is_set_aside_not_judged() -> None:
    judged, untargeted = judge([row("a", status="noTarget"), row("b", gap="0.01")], {"b"})
    assert [item.id for item in judged] == ["b"] and untargeted == ("a",)


def test_calibrated_needs_one_target_and_every_target_within() -> None:
    within = HeldOutQuery("a", "measured", Fraction(1, 100), Fraction(1, 50), True)
    missed = HeldOutQuery("b", "measured", Fraction(1, 10), Fraction(1, 50), False, "why")
    assert report(within).calibrated
    assert report(within, within).calibrated
    assert not report(within, missed).calibrated
    assert report(within, missed).missed == (missed,)
    assert not report().calibrated  # a fit with no held-out target is never called calibrated


def test_the_evidence_is_the_largest_gap_and_the_tolerance_it_met() -> None:
    small = HeldOutQuery("a", "measured", Fraction(1, 100), Fraction(1, 10), True)
    large = HeldOutQuery("b", "measured", Fraction(-3, 100), Fraction(1, 20), True)
    evidence = report(small, large).evidence()
    assert (evidence.profile, evidence.largest_gap, evidence.tolerance) == (PROFILE, "0.03", "0.05")


def test_a_miss_has_no_evidence_to_record() -> None:
    missed = HeldOutQuery("b", "measured", Fraction(1, 10), Fraction(1, 50), False, "why")
    with pytest.raises(AssertionError):
        report(missed).evidence()


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Fraction(1, 50), "0.02"),
        (Fraction(0), "0"),
        (Fraction(3, 8), "0.375"),
        (Fraction(1, 3), "0.333333333334"),
        (Fraction(2, 3), "0.666666666667"),
        (Fraction(1, 150), "0.006666666667"),
    ],
)
def test_a_gap_that_does_not_terminate_is_rounded_up_and_never_down(
    value: Fraction, text: str
) -> None:
    assert decimal_bound(value) == text
    assert Fraction(decimal_bound(value)) >= value
