# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The rows a fact and a relationship make on the scorecard, and the dimensions they fill."""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from flux_engine.concordance.integrity import link_integrity
from flux_engine.concordance.module_scores import fact_row, link_row, module_rows
from flux_engine.concordance.rule_shares import RuleShare
from flux_engine.concordance.scorecard import (
    SCHEMA_URL,
    SCHEMA_URL_1_3_0,
    TableInput,
    build_scorecard,
)
from flux_engine.concordance.shares import Holds
from flux_engine.seam.facts import Fact
from flux_engine.seam.keys import Relationship
from flux_stats.profile import parse_profile

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
PROFILE = json.loads((SPEC / "profile-1-2-example.json").read_text(encoding="utf-8"))
SCORED = parse_profile(copy.deepcopy(PROFILE))
NAME = "payment.paid_after_due"  # the example profile measures 7960 of 8000


def _share(satisfied: int, total: int, name: str = NAME, enforced: bool = True) -> RuleShare:
    expose, _, rule = name.partition(".")
    return RuleShare(expose, rule, "check", "contract", Holds(satisfied, total), enforced)


def test_a_fact_scores_one_minus_the_gap_between_the_two_shares() -> None:
    row = fact_row(_share(7900, 8000), SCORED, Fraction(1, 100))
    assert row.score == 1 - (Fraction(7960, 8000) - Fraction(7900, 8000))
    assert row.outcome == "within" and row.dimension == "invariants"
    assert row.document is not None and row.document["gap"] == "0.0075"
    assert row.document["run"]["share"] == "0.9875" and row.document["profile"]["share"] == "0.995"


def test_a_fact_outside_its_tolerance_is_outside_and_the_default_tolerance_is_zero() -> None:
    assert fact_row(_share(7900, 8000), SCORED, Fraction(1, 200)).outcome == "outside"
    strict = fact_row(_share(7900, 8000), SCORED)
    assert strict.outcome == "outside" and strict.document is not None
    assert strict.document["tolerance"] == "0"
    assert fact_row(_share(7960, 8000), SCORED).outcome == "within"


def test_a_fact_is_censored_without_a_profile_share_or_without_a_covered_row() -> None:
    withheld = fact_row(_share(5, 10, "subscriber.tenure_within_term"), SCORED)
    unknown = fact_row(_share(5, 10, "payment.other"), SCORED)
    no_profile = fact_row(_share(5, 10), None)
    empty = fact_row(_share(0, 0), SCORED)
    for row in (withheld, unknown, no_profile, empty):
        assert row.outcome == "censored" and row.score is None
        assert row.document is not None and row.document["gap"] is None
    assert withheld.document is not None and withheld.document["profile"] is None


def _link(**kwargs: Any) -> Relationship:
    return Relationship(
        "owner", "child", "parent", ("fk",), ("pk",), "many_to_one", "left", **kwargs
    )


def test_a_relationship_is_within_only_when_every_check_holds() -> None:
    whole = link_row(link_integrity(_link(), ["fk"], [[1], [2]], ["pk"], [[1], [2]]))
    assert (whole.outcome, whole.score, whole.dimension) == ("within", 1, "crossTable")
    broken = link_row(link_integrity(_link(), ["fk"], [[1], [9]], ["pk"], [[1], [2]]))
    assert broken.outcome == "outside" and broken.score == Fraction(3, 4)
    empty = link_row(link_integrity(_link(), ["fk"], [], ["pk"], []))
    assert empty.outcome == "censored" and empty.score is None


def test_the_rows_sit_in_the_table_of_their_emit() -> None:
    rows = module_rows(
        [_share(7960, 8000)],
        [link_integrity(_link(), ["fk"], [[1]], ["pk"], [[1]])],
        [Fact("payment", "paid_after_due", Fraction(1, 100), "0.01")],
        SCORED,
    )
    assert sorted(rows) == ["child", "payment"]
    assert [row.property_id for row in rows["payment"]] == [f"fact:{NAME}"]
    assert [row.property_id for row in rows["child"]] == ["relationship:child.owner"]
    assert rows["payment"][0].document is not None
    assert rows["payment"][0].document["tolerance"] == "0.01"


def test_a_scorecard_without_these_rows_is_format_1_2_0_and_with_them_1_3_0() -> None:
    def build(rows: Any) -> Any:
        return build_scorecard(
            run_id="r",
            plan_digest="sha256:" + "0" * 64,
            run_digest="sha256:" + "1" * 64,
            profile_digest="sha256:" + "2" * 64,
            tables=[TableInput(expose_id="payment")],
            module_rows=rows,
        )

    plain = build(None)
    assert plain.to_document()["_schemaURL"] == SCHEMA_URL
    assert build({}).to_bytes() == plain.to_bytes()
    scored = build(module_rows([_share(7960, 8000)], [], [], SCORED))
    document = scored.to_document()
    assert document["_schemaURL"] == SCHEMA_URL_1_3_0
    invariants = next(d for d in document["dimensions"] if d["dimension"] == "invariants")
    assert invariants["status"] == "measured" and invariants["score"] == "1"
    assert document["overall"]["score"] == "1"
    assert scored.table("payment") is not None
