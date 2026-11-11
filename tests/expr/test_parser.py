# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The parser and the canonical printer: EXPR-SPEC §1's grammar and §4's fixed point."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.expr.ast import (
    METHOD_NAMES,
    And,
    BoolLit,
    Compare,
    In,
    ListLit,
    Method,
    Not,
    NullLit,
    NumberLit,
    Or,
    Path,
    StringLit,
    to_json,
    unparse,
)
from flux_engine.expr.errors import ParseError
from flux_engine.expr.parser import parse

# --- the grammar's shape ----------------------------------------------------


@pytest.mark.unit
def test_a_comparison_is_a_primary() -> None:
    assert parse("a == 1") == Compare("==", Path(("a",)), NumberLit(Decimal("1")))


@pytest.mark.unit
def test_a_dotted_path_is_one_operand() -> None:
    assert parse("traits.digitalFluency > 0").left == Path(("traits", "digitalFluency"))


@pytest.mark.unit
def test_a_method_call_is_split_from_a_path_by_one_token_of_lookahead() -> None:
    """``products.has`` is a path; ``products.has('tv')`` is a method call."""
    assert parse("products.has('tv')") == Method(Path(("products",)), "has", (StringLit("tv"),))
    assert parse("products.has == 1").left == Path(("products", "has"))


@pytest.mark.unit
def test_membership_takes_a_list_literal_or_a_path() -> None:
    assert parse("a in ['x']") == In(Path(("a",)), ListLit((StringLit("x"),)))
    assert parse("a in b") == In(Path(("a",)), Path(("b",)))


@pytest.mark.unit
def test_an_empty_list_literal_parses() -> None:
    assert parse("a in []") == In(Path(("a",)), ListLit(()))


@pytest.mark.unit
def test_bool_literal_is_the_only_bare_primary() -> None:
    assert parse("true") == BoolLit(True)
    assert parse("false") == BoolLit(False)
    with pytest.raises(ParseError) as raised:
        parse("flag")
    assert raised.value.code == "FLX-6001"


@pytest.mark.unit
def test_null_is_an_operand_not_a_primary() -> None:
    assert parse("a == null") == Compare("==", Path(("a",)), NullLit())
    with pytest.raises(ParseError):
        parse("null")


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "",
        "   ",
        "a",
        "a +",
        "a + 1 > 0",
        "a > 1 > 2",
        "!!true",
        "(a > 1",
        "a > 1)",
        "a > 1 b > 2",
        "a.toUpper()",
        "a.has('x'",
        "a.has('x') == true",
        "a == b.has('x')",
        "a in b.has('x')",
        "a.has('x') in b",
        "a in [b]",
        "a.has(b)",
        "a in ['x',]",
        "a in ",
        "a in 1",
        "[1] in a",
        "a.1 > 0",
        "a. > 0",
        "a..b > 0",
        "&& a > 1",
        "a > 1 &&",
        "matches('x')",
        "a ! b",
    ],
)
def test_text_outside_the_closed_grammar_is_flx_6001_or_6002(source: str) -> None:
    """EXPR-SPEC preamble: "anything not derivable is a parse error, never an extension point"."""
    with pytest.raises(Exception) as raised:
        parse(source)
    assert getattr(raised.value, "code", None) in ("FLX-6001", "FLX-6002"), source


@pytest.mark.unit
def test_a_method_is_not_an_operand() -> None:
    with pytest.raises(ParseError) as raised:
        parse("a == b.has('x')")
    assert "operand" in str(raised.value)


# --- precedence and associativity -------------------------------------------


@pytest.mark.unit
def test_and_binds_tighter_than_or() -> None:
    node = parse("a == 1 || b == 2 && c == 3")
    assert isinstance(node, Or)
    assert isinstance(node.right, And)


@pytest.mark.unit
def test_both_operators_are_left_associative() -> None:
    left_nested = parse("a == 1 || b == 2 || c == 3")
    assert isinstance(left_nested, Or) and isinstance(left_nested.left, Or)
    and_nested = parse("a == 1 && b == 2 && c == 3")
    assert isinstance(and_nested, And) and isinstance(and_nested.left, And)


@pytest.mark.unit
def test_parentheses_regroup() -> None:
    node = parse("(a == 1 || b == 2) && c == 3")
    assert isinstance(node, And)
    assert isinstance(node.left, Or)


@pytest.mark.unit
def test_not_applies_to_the_following_primary() -> None:
    """``not_expr = [ "!" ] , primary``; ``primary`` includes ``comparison``."""
    assert parse("!a > 1") == Not(Compare(">", Path(("a",)), NumberLit(Decimal("1"))))
    assert parse("!a > 1") == parse("!(a > 1)")


@pytest.mark.unit
def test_not_binds_tighter_than_and() -> None:
    node = parse("!a.isNull() && b == 1")
    assert isinstance(node, And)
    assert isinstance(node.left, Not)


# --- canonical text ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "source,canonical",
    [
        ("a==1", "a == 1"),
        ("a   ==   1", "a == 1"),
        ("((a == 1))", "a == 1"),
        ("a in ['x','y']", "a in ['x', 'y']"),
        ("a.startsWith('x')", "a.startsWith('x')"),
        ("a == 1 || b == 2 && c == 3", "a == 1 || b == 2 && c == 3"),
        ("(a == 1 || b == 2) && c == 3", "(a == 1 || b == 2) && c == 3"),
        ("a == 1 || (b == 2 || c == 3)", "a == 1 || (b == 2 || c == 3)"),
        ("!a > 1", "!(a > 1)"),
        ("!true", "!true"),
        ("!a.isNull()", "!a.isNull()"),
        ("a == 'it\\'s'", "a == 'it\\'s'"),
        ("a == 1.50", "a == 1.50"),
        ("a == -0.25", "a == -0.25"),
    ],
)
def test_unparse_prints_one_canonical_spelling(source: str, canonical: str) -> None:
    assert unparse(parse(source)) == canonical


@pytest.mark.unit
def test_the_ast_is_frozen_and_hashable() -> None:
    node = parse("a == 1")
    assert hash(node) == hash(parse("a == 1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.op = "!="  # type: ignore[misc]


# --- properties --------------------------------------------------------------

_NAMES = st.from_regex(r"\A[A-Za-z_][A-Za-z0-9_]{0,4}\Z").filter(
    lambda name: name not in ("true", "false", "null", "in", "matches")
)
_PATHS = st.builds(
    lambda parts: Path(tuple(parts)),
    st.lists(_NAMES, min_size=1, max_size=3),
)
_STRING_CHARS = st.sampled_from(list(" !#$%&()*+,-./0123456789:;<=>?@ABCXYZ[]^_`abcxyz{|}~'"))
_STRINGS = st.builds(StringLit, st.text(alphabet=_STRING_CHARS, max_size=6))
_NUMBERS = st.builds(
    lambda text: NumberLit(Decimal(text)),
    st.from_regex(r"\A-?[0-9]{1,4}(\.[0-9]{1,3})?\Z"),
)
_BOOLS = st.builds(BoolLit, st.booleans())
_LITERALS = st.one_of(_STRINGS, _NUMBERS, _BOOLS, st.just(NullLit()))
_OPERANDS = st.one_of(_PATHS, _LITERALS)

_COMPARISONS = st.builds(
    Compare,
    st.sampled_from(["==", "!=", "<", "<=", ">", ">="]),
    _OPERANDS,
    _OPERANDS,
)
_MEMBERSHIPS = st.builds(
    In,
    _OPERANDS,
    st.one_of(
        st.builds(lambda items: ListLit(tuple(items)), st.lists(_LITERALS, max_size=3)),
        _PATHS,
    ),
)
_METHODS = st.one_of(
    st.builds(lambda target: Method(target, "isNull", ()), _PATHS),
    st.builds(
        lambda target, name, arg: Method(target, name, (arg,)),
        _PATHS,
        st.sampled_from([name for name in METHOD_NAMES if name != "isNull"]),
        _STRINGS,
    ),
)
_PRIMARIES = st.one_of(_COMPARISONS, _MEMBERSHIPS, _METHODS, _BOOLS)


def _expressions() -> st.SearchStrategy[Any]:
    return st.recursive(
        _PRIMARIES,
        lambda children: st.one_of(
            st.builds(Not, _PRIMARIES),
            st.builds(And, children, children),
            st.builds(Or, children, children),
        ),
        max_leaves=6,
    )


@pytest.mark.property
@settings(max_examples=400, deadline=None)
@given(_expressions())
def test_unparse_round_trips_through_parse(node: Any) -> None:
    """EXPR-SPEC §4: ``unparse(parse(x))`` is the canonical form and a fixed point."""
    text = unparse(node)
    reparsed = parse(text)
    assert to_json(reparsed) == to_json(node)
    assert unparse(reparsed) == text


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(_expressions())
def test_leading_and_trailing_whitespace_never_changes_the_ast(node: Any) -> None:
    """Space and tab are the whole whitespace set, and they are insignificant."""
    text = unparse(node)
    assert to_json(parse("\t  " + text + "  \t")) == to_json(node)


@pytest.mark.unit
@pytest.mark.parametrize(
    "spaced",
    [
        "a==1&&b.has('x')",
        "a  ==  1  &&  b.has( 'x' )",
        "a\t==\t1\t&&\tb.has('x')",
        "a == 1 && b . has ( 'x' )",
        " a==1 && b.has('x') ",
    ],
)
def test_whitespace_between_tokens_is_insignificant(spaced: str) -> None:
    """Only whitespace *between* tokens: a space inside a string literal is data."""
    assert unparse(parse(spaced)) == "a == 1 && b.has('x')"


@pytest.mark.unit
def test_a_space_inside_a_string_literal_is_data_not_whitespace() -> None:
    assert parse("a == ' x '").right == StringLit(" x ")


@pytest.mark.unit
def test_a_method_call_with_no_arguments_parses_and_fails_the_type_check() -> None:
    """Arity is a §2 type rule, not a §1 grammar rule: ``has()`` parses, then FLX-6005."""
    assert parse("a.has()") == Method(Path(("a",)), "has", ())
