# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The closure compiler: EXPR-SPEC §2's runtime totality and §4's evaluation."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.expr.compile import compile_source, make_view
from flux_engine.expr.errors import FluxExprError
from flux_engine.expr.types import ScopeModel


def evaluate(source: str, model: ScopeModel, row: Dict[str, Any]) -> bool:
    return compile_source(source, model)(make_view(model, row))


# --- evaluation --------------------------------------------------------------


@pytest.mark.unit
def test_numbers_compare_by_value_not_by_spelling(tiny: ScopeModel) -> None:
    assert evaluate("n == 1", tiny, {"n": Decimal("1.000")}) is True
    assert evaluate("n == 1.000", tiny, {"n": Decimal("1")}) is True
    assert evaluate("n > 1.5", tiny, {"n": Decimal("1.50001")}) is True


@pytest.mark.unit
def test_an_int_row_value_becomes_a_decimal(tiny: ScopeModel) -> None:
    """Views may carry ``int``; ``float`` is refused (KERNEL-SPEC §2.1)."""
    assert evaluate("n == 1", tiny, {"n": 1}) is True
    with pytest.raises(FluxExprError) as raised:
        make_view(tiny, {"n": 1.0})
    assert raised.value.code == "FLX-6005"
    assert "float" in str(raised.value)


@pytest.mark.unit
def test_and_or_short_circuit_and_stay_boolean(tiny: ScopeModel) -> None:
    assert evaluate("n > 0 && m > 0", tiny, {"n": Decimal("1"), "m": Decimal("1")}) is True
    assert evaluate("n > 0 && m > 0", tiny, {"n": Decimal("0")}) is False
    assert evaluate("n > 0 || m > 0", tiny, {"m": Decimal("1")}) is True
    assert evaluate("!(n > 0)", tiny, {"n": Decimal("0")}) is True


@pytest.mark.unit
def test_every_method_evaluates(tiny: ScopeModel) -> None:
    assert evaluate("tags.has('a')", tiny, {"tags": {"a", "b"}}) is True
    assert evaluate("tags.has('c')", tiny, {"tags": {"a", "b"}}) is False
    assert evaluate("s.startsWith('ab')", tiny, {"s": "abc"}) is True
    assert evaluate("s.endsWith('bc')", tiny, {"s": "abc"}) is True
    assert evaluate("s.contains('b')", tiny, {"s": "abc"}) is True
    assert evaluate("s.contains('z')", tiny, {"s": "abc"}) is False
    assert evaluate("s.isNull()", tiny, {"s": None}) is True
    assert evaluate("s.isNull()", tiny, {"s": "abc"}) is False


@pytest.mark.unit
def test_membership_over_a_list_and_over_a_set_path(tiny: ScopeModel) -> None:
    assert evaluate("s in ['a', 'b']", tiny, {"s": "b"}) is True
    assert evaluate("s in ['a', 'b']", tiny, {"s": "c"}) is False
    assert evaluate("s in tags", tiny, {"s": "a", "tags": {"a"}}) is True
    assert evaluate("n in [1, 2]", tiny, {"n": Decimal("2")}) is True


# --- null rules (EXPR-SPEC §2) ----------------------------------------------


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
        "n in [1]",
        "n in [1, 2]",
    ],
)
def test_a_null_operand_makes_every_comparison_and_in_false(source: str, tiny: ScopeModel) -> None:
    assert evaluate(source, tiny, {"n": None}) is False
    assert evaluate(source, tiny, {}) is False


@pytest.mark.unit
def test_methods_on_null_return_false_and_isnull_returns_true(tiny: ScopeModel) -> None:
    assert evaluate("tags.has('a')", tiny, {"tags": None}) is False
    assert evaluate("s.startsWith('a')", tiny, {"s": None}) is False
    assert evaluate("s.endsWith('a')", tiny, {"s": None}) is False
    assert evaluate("s.contains('a')", tiny, {"s": None}) is False
    assert evaluate("s.isNull()", tiny, {"s": None}) is True
    assert evaluate("s.isNull()", tiny, {}) is True


@pytest.mark.unit
def test_ne_null_is_exactly_the_negation_of_isnull(tiny: ScopeModel) -> None:
    """The resolution of §2's ambiguous "``!= null`` to true" sentence, pinned."""
    for row in ({"s": None}, {"s": "a"}, {}):
        assert evaluate("s != null", tiny, row) is not evaluate("s.isNull()", tiny, row)
        assert evaluate("s == null", tiny, row) is evaluate("s.isNull()", tiny, row)


@pytest.mark.unit
def test_a_missing_path_is_null_not_an_error(tiny: ScopeModel) -> None:
    """A view is never required to be complete."""
    predicate = compile_source("n > 0 || s.isNull()", tiny)
    assert predicate({}) is True


# --- make_view ---------------------------------------------------------------


@pytest.mark.unit
def test_make_view_resolves_sugar_to_the_canonical_path(telco_subject: ScopeModel) -> None:
    view = make_view(telco_subject, {"lifecycle": "atrisk"})
    assert view == {"subject.lifecycle": "atrisk"}


@pytest.mark.unit
def test_make_view_normalises_strings_and_set_members(tiny: ScopeModel) -> None:
    """Values are NFC-normalised on the way in, exactly as identifiers are."""
    decomposed = "cafe\u0301"
    composed = "caf\u00e9"
    assert decomposed != composed
    view = make_view(tiny, {"s": decomposed, "tags": {decomposed}})
    assert view["s"] == composed
    assert view["tags"] == frozenset({composed})


@pytest.mark.unit
def test_make_view_rejects_a_value_of_the_wrong_class(tiny: ScopeModel) -> None:
    for row, fragment in (
        ({"n": "1"}, "number expected"),
        ({"s": 1}, "string expected"),
        ({"flag": 1}, "bool expected"),
        ({"tags": ["a"]}, "set<string> expected"),
        ({"tags": {1}}, "member expected"),
        ({"n": True}, "number expected"),
    ):
        with pytest.raises(FluxExprError) as raised:
            make_view(tiny, row)
        assert raised.value.code == "FLX-6005"
        assert fragment in str(raised.value)


@pytest.mark.unit
def test_make_view_rejects_an_undeclared_path(tiny: ScopeModel) -> None:
    with pytest.raises(FluxExprError) as raised:
        make_view(tiny, {"nope": Decimal("1")})
    assert raised.value.code == "FLX-6004"


@pytest.mark.unit
def test_make_view_never_names_the_value_only_its_class(tiny: ScopeModel) -> None:
    """KERNEL-SPEC §9's error hygiene: name the column and the value class."""
    with pytest.raises(FluxExprError) as raised:
        make_view(tiny, {"s": 4815162342})
    assert "4815162342" not in str(raised.value)
    assert "int" in str(raised.value)


# --- properties ---------------------------------------------------------------

_VALUES = {
    "n": st.one_of(st.none(), st.integers(min_value=-5, max_value=5).map(Decimal)),
    "m": st.one_of(st.none(), st.integers(min_value=-5, max_value=5).map(Decimal)),
    "s": st.one_of(st.none(), st.sampled_from(["", "a", "ab", "abc", "z"])),
    "flag": st.one_of(st.none(), st.booleans()),
    "tags": st.one_of(st.none(), st.sets(st.sampled_from(["a", "b", "c"]), max_size=3)),
    "grade": st.one_of(st.none(), st.sampled_from(["gold", "silver", "bronze"])),
}

_EXPRESSIONS = [
    "n == 1",
    "n != 1",
    "n < m",
    "n >= 1 && s.startsWith('a')",
    "s in tags || grade == 'gold'",
    "!(n > 0) && tags.has('a')",
    "s == null || n != null",
    "s.isNull() || s.contains('b')",
    "grade in ['gold', 'silver'] && flag == true",
    "n in [1, 2, 3] || s in ['a', 'z']",
    "true",
    "!s.isNull()",
]


@pytest.mark.property
@settings(max_examples=300, deadline=None)
@given(
    source=st.sampled_from(_EXPRESSIONS),
    row=st.fixed_dictionaries(_VALUES),
)
def test_a_type_checked_expression_never_raises(
    source: str, row: Dict[str, Any], models: Any
) -> None:
    """EXPR-SPEC §2: "Runtime totality: a type-checked expression never raises"."""
    model = models["tiny"]
    result = compile_source(source, model)(make_view(model, row))
    assert result is True or result is False


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    source=st.sampled_from(_EXPRESSIONS),
    row=st.fixed_dictionaries(_VALUES),
)
def test_evaluation_is_deterministic(source: str, row: Dict[str, Any], models: Any) -> None:
    """Same expression, same row, same answer -- twice, and from two compilations."""
    model = models["tiny"]
    view = make_view(model, row)
    first = compile_source(source, model)
    second = compile_source(source, model)
    assert first(view) is first(view) is second(view)
