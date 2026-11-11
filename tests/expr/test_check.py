# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The static type checker: EXPR-SPEC §2's rules and their four error codes."""

from __future__ import annotations

import pytest

from flux_engine.expr.check import check
from flux_engine.expr.errors import FluxExprError
from flux_engine.expr.parser import parse
from flux_engine.expr.types import BOOL, NUMBER, SET_STRING, STRING, Scope, ScopeModel, enum_of


def code_for(source: str, model: ScopeModel) -> str:
    with pytest.raises(FluxExprError) as raised:
        check(parse(source), model)
    return raised.value.code


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "n == 1",
        "n != 1",
        "n < 1",
        "n <= 1",
        "n > 1",
        "n >= 1",
        "n == m",
        "s == 'a'",
        "'a' == s",
        "flag == true",
        "grade == 'gold'",
        "grade == s",
        "s in tags",
        "grade in tags",
        "s in ['a', 'b']",
        "n in [1, 2]",
        "grade in ['gold']",
        "tags.has('a')",
        "n.isNull()",
        "tags.isNull()",
        "s.startsWith('a')",
        "s.endsWith('a')",
        "s.contains('a')",
        "n == null",
        "n != null",
        "true",
        "!(n > 1)",
        "n > 1 && s == 'a'",
        "n > 1 || s == 'a'",
    ],
)
def test_well_typed_expressions_check_to_bool(source: str, tiny: ScopeModel) -> None:
    assert check(parse(source), tiny) == BOOL


@pytest.mark.unit
def test_an_unknown_path_is_flx_6004(tiny: ScopeModel) -> None:
    assert code_for("nope == 1", tiny) == "FLX-6004"
    assert code_for("n == nope", tiny) == "FLX-6004"
    assert code_for("nope.isNull()", tiny) == "FLX-6004"
    assert code_for("s in nope", tiny) == "FLX-6004"
    assert code_for("nope == null", tiny) == "FLX-6004"


@pytest.mark.unit
def test_an_enum_literal_outside_the_declared_levels_is_flx_6003(tiny: ScopeModel) -> None:
    """EXPR-SPEC §2: "a literal outside the set is ``FLX-6003`` at plan time"."""
    assert code_for("grade == 'platinum'", tiny) == "FLX-6003"
    assert code_for("'platinum' == grade", tiny) == "FLX-6003"
    assert code_for("grade != 'platinum'", tiny) == "FLX-6003"
    assert code_for("grade in ['gold', 'platinum']", tiny) == "FLX-6003"


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "'1' == 1",
        "s == 1",
        "n == 'a'",
        "flag == 1",
        "flag == 'a'",
        "grade == 1",
        "grade == flag",
        "tags == 'a'",
        "tags == tags",
        "s < 'b'",
        "grade > 'gold'",
        "flag > true",
        "tags > 1",
        "n > null",
        "'a' == null",
        "null == null",
        "s in n",
        "n in tags",
        "flag in tags",
        "s in [1]",
        "s in ['a', 1]",
        "grade in [1]",
        "grade in [true]",
        "n in ['a']",
        "s in [null]",
        "tags.startsWith('a')",
        "grade.startsWith('g')",
        "n.startsWith('a')",
        "s.has('a')",
        "n.has('a')",
        "s.isNull('a')",
        "tags.has()",
        "tags.has('a', 'b')",
        "tags.has(1)",
        "s.contains(1)",
    ],
)
def test_type_mismatches_are_flx_6005(source: str, tiny: ScopeModel) -> None:
    """EXPR-SPEC §2: "comparison between different types is an error (no coercion)"."""
    assert code_for(source, tiny) == "FLX-6005"


@pytest.mark.unit
def test_the_two_examples_the_spec_names_by_hand(telco_subject: ScopeModel) -> None:
    """EXPR-SPEC §2 names ``market > 3`` and ``traits.digitalFluency.has('x')``."""
    assert code_for("market > 3", telco_subject) == "FLX-6005"
    assert code_for("traits.digitalFluency.has('x')", telco_subject) == "FLX-6005"


@pytest.mark.unit
def test_an_enum_compares_as_a_string(tiny: ScopeModel) -> None:
    """EXPR-SPEC §2: an enum is "a string drawn from a declared closed set"."""
    assert check(parse("grade == s"), tiny) == BOOL
    assert check(parse("s == grade"), tiny) == BOOL


@pytest.mark.unit
def test_ordering_is_numeric_only(tiny: ScopeModel) -> None:
    """Resolution: ``<`` and friends are declared over ``number`` and nothing else."""
    assert check(parse("n < m"), tiny) == BOOL
    for source in ("s < 'a'", "grade < 'gold'", "flag < true", "tags < tags"):
        assert code_for(source, tiny) == "FLX-6005"


@pytest.mark.unit
def test_the_null_literal_only_tests_a_path_for_absence(tiny: ScopeModel) -> None:
    assert check(parse("n == null"), tiny) == BOOL
    assert check(parse("tags != null"), tiny) == BOOL
    for source in ("null == null", "'a' == null", "1 != null", "n > null", "null in tags"):
        assert code_for(source, tiny) == "FLX-6005"


@pytest.mark.unit
def test_sugar_resolves_before_the_type_is_read(telco_subject: ScopeModel) -> None:
    """Bare names and their canonical spellings are the same attribute."""
    assert check(parse("lifecycle == 'atrisk'"), telco_subject) == BOOL
    assert check(parse("subject.lifecycle == 'atrisk'"), telco_subject) == BOOL
    assert code_for("subject.subject.lifecycle == 'atrisk'", telco_subject) == "FLX-6004"


@pytest.mark.unit
def test_a_scope_only_declares_its_own_names(
    telco_subject: ScopeModel, telco_event: ScopeModel, telco_observed: ScopeModel
) -> None:
    """The three namespaces of EXPR-SPEC §3 do not leak into one another."""
    assert code_for("amount > 0", telco_subject) == "FLX-6004"
    assert code_for("lifecycle == 'atrisk'", telco_event) == "FLX-6004"
    assert code_for("lifecycle == 'atrisk'", telco_observed) == "FLX-6004"
    assert code_for("late_payment_count >= 2", telco_subject) == "FLX-6004"


@pytest.mark.unit
def test_an_ambiguous_bare_name_resolves_to_the_context_attribute() -> None:
    """A payload field colliding with a CloudEvents context name loses its sugar."""
    model = ScopeModel(
        Scope.EVENT,
        {"type": STRING, "data.type": NUMBER},
        {},
    )
    assert check(parse("type == 'a'"), model) == BOOL
    assert check(parse("data.type > 1"), model) == BOOL


@pytest.mark.unit
def test_a_set_has_no_equality_but_has_membership() -> None:
    model = ScopeModel(Scope.SUBJECT, {"tags": SET_STRING, "s": STRING, "grade": enum_of(["a"])})
    assert check(parse("s in tags"), model) == BOOL
    assert check(parse("grade in tags"), model) == BOOL
    assert check(parse("tags.has('a')"), model) == BOOL
    with pytest.raises(FluxExprError) as raised:
        check(parse("tags == tags"), model)
    assert raised.value.code == "FLX-6005"
