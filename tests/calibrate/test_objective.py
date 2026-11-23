# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The objective of a candidate: the sum of the squared gaps to the pinned profile's targets (ADR 0028 section 8).

A measured query adds the square of its gap, each group's for a query with ``groupBy``. A query the profile
states a rate for and that was not measured adds 1, so that a candidate that cannot be counted never beats
one that can. A query with no target adds nothing. Everything is an exact fraction.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Optional, Tuple

import pytest

from flux_engine.calibrate.objective import score, stated_queries
from flux_engine.concordance.queries import CALIBRATION, GroupRow, QueryRow
from flux_engine.consumers.outcome import QueryTarget, Target
from flux_engine.seam.consumers import ConsumerQuery

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "0" * 64


def row(
    name: str,
    *,
    gap: Optional[Fraction] = None,
    status: str = "measured",
    groups: Tuple[GroupRow, ...] = (),
) -> QueryRow:
    return QueryRow(
        name, CALIBRATION, DIGEST, None, None, None, None, gap, None, status, None, groups=groups
    )


def group(ordinal: int, gap: Optional[Fraction], status: str = "measured") -> GroupRow:
    return GroupRow(ordinal, False, 1, 2, None, None, gap, status, None)


def stated(*rates: Optional[str]) -> Tuple[QueryTarget, ...]:
    return (
        QueryTarget(
            None,
            {
                str(index): Target(Fraction(rate) if rate is not None else None)
                for index, rate in enumerate(rates)
            },
        ),
    )


def test_a_measured_query_adds_the_square_of_its_gap() -> None:
    evaluation = score(
        [row("a", gap=Fraction(-1, 10)), row("b", gap=Fraction(1, 5))],
        {"a", "b"},
    )
    assert evaluation.objective == Fraction(1, 100) + Fraction(1, 25)
    assert evaluation.gaps == (("a", Fraction(-1, 10)), ("b", Fraction(1, 5)))


def test_a_grouped_query_adds_the_square_of_each_group_s_gap_and_reports_the_largest() -> None:
    grouped = row(
        "g", groups=(group(0, Fraction(1, 10)), group(1, Fraction(-3, 10)), group(2, None))
    )
    evaluation = score([grouped], {"g"})
    assert evaluation.objective == Fraction(1, 100) + Fraction(9, 100)
    assert evaluation.gaps == (("g", Fraction(-3, 10)),)


@pytest.mark.parametrize(
    "status", ["timedOut", "outOfMemory", "crashed", "error", "notARate", "noRows"]
)
def test_a_query_with_a_target_that_was_not_measured_adds_the_largest_square_a_gap_can_have(
    status: str,
) -> None:
    evaluation = score([row("a", status=status)], {"a"})
    assert evaluation.objective == 1 and evaluation.gaps == (("a", None),)


@pytest.mark.parametrize("status", ["noTarget", "withheld"])
def test_a_query_with_no_target_adds_nothing(status: str) -> None:
    assert score([row("a", status=status)], set()).objective == 0
    assert score([row("a", status="measured")], set()).objective == 0


def test_a_candidate_that_cannot_be_counted_never_beats_one_that_can() -> None:
    worst_measured = score([row("a", gap=Fraction(1))], {"a"}).objective
    assert score([row("a", status="timedOut")], {"a"}).objective >= worst_measured


def test_the_objective_is_exact() -> None:
    gaps = [Fraction(1, 3), Fraction(-1, 7)]
    rows = [row(f"q{i}", gap=gap) for i, gap in enumerate(gaps)]
    assert score(rows, {"q0", "q1"}).objective == sum((gap * gap for gap in gaps), Fraction(0))


def query(name: str, group_by: Optional[str] = None) -> ConsumerQuery:
    return ConsumerQuery(id=name, sql="SELECT 1", unit="t", group_by=group_by)


def test_a_query_is_stated_when_a_statistic_grouped_as_it_is_gives_a_rate() -> None:
    by_plan = (QueryTarget("plan", {"basic": Target(Fraction(1, 5)), "pro": Target(None)}),)
    whole = (QueryTarget(None, {"": Target(Fraction(1, 2))}),)
    nothing = (QueryTarget(None, {"": Target(None)}),)
    targets = {"plain": whole, "grouped": by_plan, "empty": nothing, "mismatch": whole}
    found = stated_queries(
        [
            query("plain"),
            query("grouped", "plan"),
            query("empty"),
            query("mismatch", "plan"),
            query("absent"),
        ],
        targets,
    )
    assert found == {"plain", "grouped"}
