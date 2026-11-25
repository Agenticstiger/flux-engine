# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink's audience gate: who may read which grade (ADR 0038 section 2)."""

from __future__ import annotations

from typing import Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern
from flux_engine.sinks._kafka_gate import AUDIENCES, MINIMUM_GRADE, parse_audience, require_grade
from flux_engine.sinks.receipts import RecordedGrade

#: grade -> (internal reads it, external reads it)
READS = {
    "P5": (False, False),
    "P1": (True, False),
    "P3": (True, True),
    "P4": (True, True),
    "P0": (True, True),
}


def test_the_audiences_and_their_minimums() -> None:
    assert AUDIENCES == ("internal", "external")
    assert MINIMUM_GRADE == {"internal": Pattern.P1, "external": Pattern.P3}


@pytest.mark.parametrize("value", ["internal", "external"])
def test_an_audience_is_taken_as_named(value: str) -> None:
    assert parse_audience(value) == value


@pytest.mark.parametrize("value", [None, ""])
def test_no_audience_is_refused_with_the_option_named(value: Optional[str]) -> None:
    with pytest.raises(FluxError) as caught:
        parse_audience(value)
    assert caught.value.code.code == "FLX-3504"
    assert "audience=internal" in str(caught.value) and "audience=external" in str(caught.value)


@pytest.mark.parametrize("value", ["public", "Internal", "internal,external", " external"])
def test_another_audience_is_refused_naming_the_allowed_ones(value: str) -> None:
    with pytest.raises(FluxError) as caught:
        parse_audience(value)
    assert caught.value.code.code == "FLX-3504"
    assert "audience" in str(caught.value) and "internal, external" in str(caught.value)


def graded(grade: Optional[str], problem: Optional[str] = None) -> RecordedGrade:
    return RecordedGrade(grade=grade, problem=problem, profile_grade=grade)


@pytest.mark.parametrize("grade", sorted(READS))
@pytest.mark.parametrize("audience", AUDIENCES)
def test_every_grade_against_every_audience(grade: str, audience: str) -> None:
    allowed = READS[grade][AUDIENCES.index(audience)]
    if allowed:
        assert require_grade(audience, graded(grade), expose_id="site") is Pattern(grade)
        return
    with pytest.raises(FluxError) as caught:
        require_grade(audience, graded(grade), expose_id="site")
    text = str(caught.value)
    assert caught.value.code.code == "FLX-3512"
    minimum = MINIMUM_GRADE[audience]
    for needle in (
        "site",
        grade,
        Pattern(grade).label,
        audience,
        minimum.value,
        minimum.label,
    ):
        assert needle in text


def test_an_external_refusal_names_the_remedy() -> None:
    with pytest.raises(FluxError) as caught:
        require_grade("external", graded("P1"), expose_id="site")
    assert "differential privacy" in str(caught.value)
    assert "internal" in str(caught.value)


@pytest.mark.parametrize("audience", AUDIENCES)
def test_a_sink_handed_no_record_is_refused_for_every_audience(audience: str) -> None:
    with pytest.raises(FluxError) as caught:
        require_grade(audience, None, expose_id="reading")
    assert caught.value.code.code == "FLX-3512"
    assert "reading" in str(caught.value)
    assert "did not hand the sink the grade" in str(caught.value)


@pytest.mark.parametrize("audience", AUDIENCES)
def test_a_record_without_a_grade_is_refused_naming_its_problem(audience: str) -> None:
    problem = "the run's plan records no privacy grade"
    with pytest.raises(FluxError) as caught:
        require_grade(audience, graded(None, problem), expose_id="reading")
    assert caught.value.code.code == "FLX-3512"
    assert problem in str(caught.value) and "reading" in str(caught.value)


def test_a_record_without_a_grade_or_a_problem_is_still_refused() -> None:
    with pytest.raises(FluxError) as caught:
        require_grade("internal", graded(None), expose_id="reading")
    assert caught.value.code.code == "FLX-3512"


@pytest.mark.parametrize("grade", ["", "P2", "p1", "secret-value", "5"])
def test_a_text_that_is_not_a_grade_is_refused_without_echoing_it(grade: str) -> None:
    with pytest.raises(FluxError) as caught:
        require_grade("external", graded(grade), expose_id="reading")
    assert caught.value.code.code == "FLX-3512"
    if grade:
        assert repr(grade) not in str(caught.value)


def test_an_unknown_audience_is_not_guessed() -> None:
    with pytest.raises(FluxError) as caught:
        require_grade("everyone", graded("P0"), expose_id="site")
    assert caught.value.code.code == "FLX-3504"
