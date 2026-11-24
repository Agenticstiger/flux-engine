# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A labelled column of a table the unit of privacy does not reach counts as P1 (ADR 0037 decision 1).

The guarantee is about units: the rows of a table the unit does not reach are the same in every
neighbouring data set. A ``pii`` or ``restricted`` column of such a table can group a noisy
``groupByLink`` histogram, so that one fixed parent row moves all of its children, while the profile
grades P3. For the minimum-grade check any labelled column of such a table counts as P1, whether or
not a noisy statistic touches it. The rules for relationships, a statistic grouped by a link and the
unit's ``follows`` are in the grade, and the evidence states the unit with what it follows.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern
from flux_engine.evidence.grades import RunGrade, grade_profile
from flux_engine.evidence.report import render_privacy_section
from flux_engine.governance.privacy import (
    FLX_4005,
    GradeBelowMinimum,
    effective_floors,
    enforce_minimum_grades,
)
from flux_engine.stages._grades import column_floors, require_minimum_grade

pytestmark = pytest.mark.unit

SPEC = Path(__file__).resolve().parents[2] / "docs" / "spec"
NOISE = {"mechanism": "gaussian", "scale": "10", "privacyLoss": {"rho": "0.05"}}


def _load(name: str) -> Dict[str, Any]:
    document: Dict[str, Any] = json.loads((SPEC / name).read_text(encoding="utf-8"))
    return document


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

COLUMNS = [("plan", "tier"), ("subscriber", "region")]


def test_a_labelled_column_of_a_fixed_table_counts_as_p1_whatever_the_run_grade() -> None:
    labels = {("plan", "tier"): "pii"}
    floors = effective_floors()
    for grade in (Pattern.P3, Pattern.P4, Pattern.P0):
        enforce_minimum_grades(grade, labels, floors, COLUMNS)  # the check as it was
        with pytest.raises(GradeBelowMinimum) as refused:
            enforce_minimum_grades(grade, labels, floors, COLUMNS, fixed={"plan"})
        assert refused.value.code is FLX_4005
        (found,) = refused.value.shortfalls
        assert found.fixed and found.grade is Pattern.P1 and found.minimum is Pattern.P3
        assert "does not reach plan" in str(refused.value) and "count as P1" in str(refused.value)


def test_the_floor_does_not_refuse_a_label_that_p1_meets_or_a_column_of_a_reached_table() -> None:
    floors = effective_floors()
    enforce_minimum_grades(
        Pattern.P3, {("plan", "tier"): "confidential"}, floors, COLUMNS, fixed={"plan"}
    )
    enforce_minimum_grades(
        Pattern.P3, {("subscriber", "region"): "pii"}, floors, COLUMNS, fixed={"plan"}
    )
    # a grade below P1 stays where it is
    with pytest.raises(GradeBelowMinimum) as refused:
        enforce_minimum_grades(
            Pattern.P5, {("plan", "tier"): "confidential"}, floors, COLUMNS, fixed={"plan"}
        )
    (found,) = refused.value.shortfalls
    assert found.grade is Pattern.P5


# ---------------------------------------------------------------------------
# The leak, closed
# ---------------------------------------------------------------------------


@dataclass
class _Compiled:
    column_names: List[str]


@dataclass
class _Emit:
    """The parts of a resolved emit the minimum-grade check reads."""

    expose_id: str
    expose: Dict[str, Any]
    columns: List[str]
    is_reference: bool = False

    def compile(self) -> _Compiled:
        return _Compiled(self.columns)


def _emit(expose_id: str, column: str, label: str) -> _Emit:
    return _Emit(
        expose_id,
        {"contract": {"schema": [{"name": column, "type": "string", "sensitivity": label}]}},
        [column],
    )


def _leak() -> Dict[str, Any]:
    """A profile whose noisy histogram of subscribers is grouped by a column of a parent the unit
    does not reach: it grades P3."""
    document = _load("profile-1-4-example.json")
    columns = document["tables"]["subscriber"]["columns"]
    columns["plan"] = {"domain": "category"}
    columns["region"]["statistics"] = [
        {
            "definition": "levelHistogram/1",
            "levels": ["east", "north", "west"],
            "sampleSize": {"min": 13000, "max": 15000},
            "disclosure": "noisy",
            "groupByLink": {
                "parent": {"table": "plan", "columns": ["id"]},
                "columns": ["plan"],
                "column": "tier",
            },
            "noise": copy.deepcopy(NOISE),
            "groups": [
                {"group": tier, "status": "F", "noisyCounts": ["10", "20", "30"]}
                for tier in ("gold", "silver")
            ],
        }
    ]
    document["tables"]["plan"] = {
        "columns": {"id": {"domain": "category"}, "tier": {"domain": "category"}}
    }
    return document


def test_a_pii_parent_column_that_groups_a_noisy_link_histogram_does_not_pass_at_p3() -> None:
    document = _leak()
    graded = grade_profile(document, pinned=True)
    # the profile alone grades P3: every statistic in it is noisy
    assert graded.pattern is Pattern.P3 and graded.reasons == ()
    accounting = graded.accounting
    assert accounting is not None and accounting.from_1_4
    assert accounting.fixed == ("plan",)
    assert accounting.unreached({"plan", "subscriber"}) == {"plan"}

    emits = [_emit("plan", "tier", "pii"), _emit("subscriber", "region", "none")]
    # without the floor the gate lets it through
    require_minimum_grade(Pattern.P3, emits, {}, where="s")
    # with the account of the profile it is refused
    with pytest.raises(FluxError) as refused:
        require_minimum_grade(Pattern.P3, emits, {}, where="s", accounting=accounting)
    assert refused.value.code is FLX_4005
    assert "plan.tier is pii" in str(refused.value) and "does not reach plan" in str(refused.value)
    rows = {
        (row.expose_id, row.column): row
        for row in column_floors(Pattern.P3, emits, {}, where="s", accounting=accounting)
    }
    assert not rows[("plan", "tier")].met and rows[("subscriber", "region")].met


def test_a_labelled_column_of_the_expose_or_a_followed_table_is_not_held_to_p1() -> None:
    document = _load("profile-1-4-follows-example.json")
    graded = grade_profile(document, pinned=True)
    accounting = graded.accounting
    assert accounting is not None and graded.pattern is Pattern.P3
    assert dict(accounting.reached) == {"account": 1, "invoice": 3}
    assert accounting.unreached({"account", "invoice", "ledger"}) == {"ledger"}
    emits = [
        _emit("account", "name", "pii"),
        _emit("invoice", "payer", "pii"),
        _emit("ledger", "note", "confidential"),
    ]
    require_minimum_grade(Pattern.P3, emits, {}, where="s", accounting=accounting)
    emits.append(_emit("ledger", "holder", "pii"))
    with pytest.raises(FluxError) as refused:
        require_minimum_grade(Pattern.P3, emits, {}, where="s", accounting=accounting)
    assert "ledger.holder is pii" in str(refused.value)


def test_a_profile_of_an_earlier_format_keeps_the_check_it_had() -> None:
    document = _load("profile-1-2-private-example.json")
    accounting = grade_profile(document, pinned=True).accounting
    assert accounting is not None and not accounting.from_1_4
    assert accounting.unreached({"plan", "subscriber"}) == frozenset()
    emits = [_emit("plan", "tier", "pii")]
    require_minimum_grade(Pattern.P3, emits, {}, where="s", accounting=accounting)


# ---------------------------------------------------------------------------
# The grade of the new forms
# ---------------------------------------------------------------------------


def _relationship(**more: Any) -> Dict[str, Any]:
    return {
        "parent": {"table": "subscriber", "columns": ["id"]},
        "child": {"table": "usage_event", "columns": ["sub_id"]},
        "statistics": [
            {
                "definition": "marginalHistogram/1",
                "bins": ["0", "1", "5"],
                "sampleSize": {"min": 1000, "max": 9999},
                "disclosure": "noisy",
                "noise": copy.deepcopy(NOISE),
                "groups": [{"group": "", "status": "F", "noisyCounts": ["400", "600"]}],
            }
        ],
    } | more


def _with_relationship(relationship: Dict[str, Any]) -> Dict[str, Any]:
    document = _load("profile-1-4-example.json")
    document["tables"]["subscriber"]["columns"]["id"] = {"domain": "category"}
    document["tables"]["usage_event"] = {"columns": {"sub_id": {"domain": "category"}}}
    document["relationships"] = [relationship]
    document["privacy"]["privacyLoss"] = {"rho": "1"}
    return document


def test_a_relationship_of_noisy_histograms_is_noisy_and_keeps_a_run_at_p3() -> None:
    graded = grade_profile(_with_relationship(_relationship()), pinned=True)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()
    assert graded.accounting is not None and graded.accounting.parts == 4


def test_a_relationship_that_holds_no_statistic_releases_nothing() -> None:
    graded = grade_profile(_with_relationship(_relationship(statistics=[])), pinned=True)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["statistics"][0].pop("noise"),
        lambda r: r["statistics"].append({"definition": "mean/1"}),
        lambda r: r.update(dates=[{"parent": "a", "child": "b"}]),
        lambda r: r.update(exposure=[{"start": "a"}]),
    ],
    ids=["exact histogram", "a mean beside it", "dates", "exposure"],
)
def test_a_relationship_with_anything_released_without_noise_names_itself_and_is_p1(
    change: Any,
) -> None:
    relationship = _relationship()
    change(relationship)
    graded = grade_profile(_with_relationship(relationship), pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("relationships[0]" in reason for reason in graded.reasons), graded.reasons
    assert not any("relationship, query or fact" in reason for reason in graded.reasons)


def test_a_statistic_grouped_by_a_link_is_noisy_only_when_it_carries_noise() -> None:
    document = _leak()
    assert grade_profile(document, pinned=True).pattern is Pattern.P3
    statistic = document["tables"]["subscriber"]["columns"]["region"]["statistics"][0]
    del statistic["noise"]
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("a statistic released without noise" in reason for reason in graded.reasons)


def _exact_band(statistic: Dict[str, Any]) -> None:
    statistic["sampleSize"] = {"min": 4321, "max": 4321}


def test_a_noisy_rank_correlation_with_an_exact_band_is_p1_and_a_real_band_keeps_p3() -> None:
    document = _load("profile-1-4-example.json")
    statistic = document["tables"]["subscriber"]["columnPairs"][0]["statistics"][0]
    assert statistic["definition"] == "kendallTau/1" and "noise" in statistic
    assert grade_profile(document, pinned=True).pattern is Pattern.P3
    statistic["sampleSize"] = {"min": 4321, "max": 4322}
    assert grade_profile(document, pinned=True).pattern is Pattern.P3
    _exact_band(statistic)
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("min equals its max" in reason for reason in graded.reasons), graded.reasons
    assert "4321" not in "\n".join(graded.reasons)


def test_a_noisy_relationship_histogram_with_an_exact_band_is_p1_and_a_real_band_keeps_p3() -> None:
    relationship = _relationship()
    graded = grade_profile(_with_relationship(relationship), pinned=True)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()
    _exact_band(relationship["statistics"][0])
    graded = grade_profile(_with_relationship(relationship), pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("min equals its max" in reason for reason in graded.reasons), graded.reasons


def test_a_noisy_relationship_histogram_grouped_by_a_non_category_parent_column_is_p1() -> None:
    relationship = _relationship()
    relationship["statistics"][0]["groupBy"] = "id"
    relationship["statistics"][0]["groups"] = [
        {"group": key, "status": "F", "noisyCounts": ["400", "600"]} for key in ("a", "b")
    ]
    assert grade_profile(_with_relationship(relationship), pinned=True).pattern is Pattern.P3
    document = _with_relationship(relationship)
    document["tables"]["subscriber"]["columns"]["id"] = {"domain": "number"}
    graded = grade_profile(document, pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("keys are data values" in reason for reason in graded.reasons), graded.reasons


def _linked_by(column: str) -> Dict[str, Any]:
    """The follows example, with a noisy histogram of invoices grouped by *column* of its account."""
    document = _load("profile-1-4-follows-example.json")
    document["privacy"]["privacyLoss"] = {"rho": "1"}
    document["tables"]["account"]["columns"]["balance"] = {"domain": "number"}
    statistic = document["tables"]["invoice"]["columns"]["status"]["statistics"][0]
    statistic["groupByLink"] = {
        "parent": {"table": "account", "columns": ["id"]},
        "columns": ["account_id"],
        "column": column,
    }
    statistic["groups"] = [
        {"group": key, "status": "F", "noisyCounts": ["10", "20"]}
        for key in (("business", "personal") if column == "segment" else ("1234.56", "98765.43"))
    ]
    return document


def test_a_link_to_a_category_column_of_the_parent_keeps_a_1_4_0_profile_p3() -> None:
    graded = grade_profile(_linked_by("segment"), pinned=True)
    assert graded.pattern is Pattern.P3 and graded.reasons == ()


def test_a_link_to_a_number_column_of_the_parent_is_p1_in_1_4_0_and_names_no_value() -> None:
    graded = grade_profile(_linked_by("balance"), pinned=True)
    assert graded.pattern is Pattern.P1
    assert any("not a category column" in reason for reason in graded.reasons), graded.reasons
    assert not any("1234.56" in reason or "balance" in reason for reason in graded.reasons)


def test_a_link_to_a_column_the_profile_does_not_describe_is_p1_in_1_4_0() -> None:
    assert grade_profile(_linked_by("nowhere"), pinned=True).pattern is Pattern.P1


# ---------------------------------------------------------------------------
# What the declaration covers
# ---------------------------------------------------------------------------


def test_the_reason_for_an_undeclared_structure_lists_what_a_1_4_0_declaration_covers() -> None:
    document = _load("profile-1-4-example.json")
    del document["privacy"]["publicStructure"]
    (reason,) = grade_profile(document, pinned=True).reasons
    assert "the bins of every groupByBins" in reason and "group keys" in reason
    assert "every rowFloor and every maxChildren" in reason and "sampleSize bands" in reason


def test_the_reason_for_an_earlier_format_is_the_one_it_always_was() -> None:
    document = _load("profile-1-2-private-example.json")
    del document["privacy"]["publicStructure"]
    (reason,) = grade_profile(document, pinned=True).reasons
    assert reason == (
        "the profile does not declare that its levels, bin edges and sampleSize bands were "
        "chosen without the data"
    )


# ---------------------------------------------------------------------------
# The evidence
# ---------------------------------------------------------------------------


def test_the_evidence_states_the_unit_with_what_it_follows_and_the_tables_it_treats_as_fixed() -> (
    None
):
    document = _load("profile-1-4-follows-example.json")
    document["tables"]["ledger"] = {"columns": {"note": {"domain": "category"}}}
    text = render_privacy_section(RunGrade(grade_profile(document, pinned=True)))
    assert (
        "- Unit of privacy: one row of `account` together with the rows that follow it, at most 3 "
        "rows of `invoice` for each row of `account`; neighbouring data sets differ by adding or "
        "removing one such unit." in text
    )
    assert "`invoice` (3 rows)" in text
    assert "Tables the unit does not reach: `ledger`." in text
    assert "counts as P1 for its minimum grade" in text
    assert "the group keys of its grouped noisy statistics" in text
    assert "every rowFloor and every maxChildren were chosen without the data" in text


def test_the_evidence_of_a_1_4_0_profile_that_follows_nothing_names_what_it_does_not_reach() -> (
    None
):
    document = _leak()
    text = render_privacy_section(RunGrade(grade_profile(document, pinned=True)))
    assert (
        "- Unit of privacy: one row of `subscriber`; neighbouring data sets differ by adding or "
        "removing one row of it." in text
    )
    assert "Tables the unit does not reach: `plan`." in text
    assert "Tables the unit reaches besides" not in text


def test_the_evidence_of_an_earlier_profile_keeps_its_bytes() -> None:
    document = _load("profile-1-2-private-example.json")
    text = render_privacy_section(RunGrade(grade_profile(document, pinned=True)))
    assert "Tables the unit" not in text and "group keys" not in text
    assert "- Unit of privacy: one row of `subscriber`; neighbouring data sets differ" in text
    assert "and that the levels of its histograms, their bin edges and the sampleSize bands" in text
