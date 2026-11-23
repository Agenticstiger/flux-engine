# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The lexer's and the parser's limits and refusals, beyond what the vectors pin.

The vectors hold one boundary pair per kind of nesting. These tests hold
the rest of the depth rule (CASE, calls and unary minus count as levels; an
IN item, even ``-1``, is one literal), the guards that keep a hostile text
from reaching Python's own
recursion limit or paying for normalization, positions, and the parser's
side rule (a contract's text never parses an extension).
"""

from __future__ import annotations

import pytest

from flux_engine.expr2 import MAX_DEPTH, Expr2Error, Side, parse
from flux_engine.expr2.ast import And, Column, Neg, Not, Number, Path
from flux_engine.expr2.lexer import MAX_LENGTH, TokenKind, normalize, tokenize

pytestmark = pytest.mark.unit


def _code(text: str, side: Side = Side.CORE) -> str:
    with pytest.raises(Expr2Error) as raised:
        parse(text, side=side)
    return raised.value.code.code


def test_depth_counts_every_level_of_the_tree() -> None:
    assert parse("net", side=Side.CORE).depth == 1
    assert parse("(net)", side=Side.CORE).depth == 2
    assert parse("-1", side=Side.CORE).depth == 2
    assert parse("net = 1", side=Side.CORE).depth == 2
    # An IN item is one literal, a minus included (RFC §2.1's in_item).
    assert parse("net IN (-1, 2)", side=Side.CORE).depth == 2
    assert parse("ROUND(net, 2)", side=Side.CORE).depth == 2
    assert parse("CASE WHEN a THEN b END", side=Side.CORE).depth == 2
    assert parse("a + b + c", side=Side.CORE).depth == 3


@pytest.mark.parametrize(
    "build",
    [
        lambda n: "COALESCE(" * n + "net" + ", 0)" * n,
        lambda n: "CASE WHEN TRUE THEN " * n + "net" + " END" * n,
        lambda n: "ROUND(" * n + "net" + ", 2)" * n,
        lambda n: "-(" * n + "net" + ")" * n,
    ],
    ids=["coalesce", "case", "round", "minus"],
)
def test_nested_constructs_stop_at_64_levels(build) -> None:  # type: ignore[no-untyped-def]
    deepest = None
    for levels in range(1, 80):
        try:
            depth = parse(build(levels), side=Side.CORE).depth
        except Expr2Error as error:
            assert error.code.code == "FLX-6103"
            break
        deepest = depth
    assert deepest is not None and deepest <= MAX_DEPTH
    assert deepest > MAX_DEPTH - 3


def test_thousands_of_parentheses_are_refused_not_a_recursion_error() -> None:
    assert _code("(" * 2000 + "net" + ")" * 2000 + " = 1") == "FLX-6103"


def test_a_long_flat_chain_is_refused_by_depth_not_by_recursion() -> None:
    assert _code(" + ".join(["1"] * 1000) + " = 1") == "FLX-6103"


def test_a_text_four_times_too_long_is_refused_before_normalizing() -> None:
    with pytest.raises(Expr2Error) as raised:
        normalize("a" * (4 * MAX_LENGTH + 1))
    assert raised.value.code.code == "FLX-6103"


def test_a_text_that_normalizes_into_the_limit_is_accepted() -> None:
    text = "é" * (MAX_LENGTH // 2 + 1)
    assert len(text) > MAX_LENGTH
    assert len(normalize(text)) <= MAX_LENGTH


def test_positions_index_the_normalized_text() -> None:
    with pytest.raises(Expr2Error) as raised:
        parse("'é' = ;", side=Side.CORE)
    assert raised.value.position == 6


def test_quoted_names_and_strings_unescape() -> None:
    tokens = tokenize(normalize("\"a\"\"b\" = 'it''s'"))
    assert [(t.kind, t.text) for t in tokens[:3]] == [
        (TokenKind.QUOTED_NAME, 'a"b'),
        (TokenKind.OPERATOR, "="),
        (TokenKind.STRING, "it's"),
    ]


def test_keywords_fold_ascii_only() -> None:
    # U+0131 (dotless i) upper-cases to I, so "ınner" must never become a keyword:
    # it is not an ASCII letter at all, so the lexer refuses it.
    assert _code("n" + chr(0x131) + " = 1") == "FLX-6103"


@pytest.mark.parametrize(
    "text",
    [
        "COUNT(CHILDREN.x)",
        "previous.a.b.c",
        "a.b.c",
        "ROW_NUMBER() OVER (PARTITION BY a ORDER BY b)",
    ],
)
def test_a_contract_text_never_parses_an_extension(text: str) -> None:
    assert _code(text, Side.CORE) == "FLX-6103"


def test_roots_shape_the_flux_side_tree() -> None:
    assert parse("row.a", side=Side.FLUX).tree == Column("a")
    assert parse("link.r.a", side=Side.FLUX).tree == Column("a", "r")
    assert parse("link.r.s.a", side=Side.FLUX).tree == parse("r.s.a", side=Side.FLUX).tree
    assert parse("previous.a", side=Side.FLUX).tree == Path("previous", ("a",))
    assert parse("row.a", side=Side.CORE).tree == Column("a", "row")


def test_not_chains_and_minus_build_nested_nodes() -> None:
    tree = parse("NOT NOT a AND b", side=Side.CORE).tree
    assert tree == And(Not(Not(Column("a"))), Column("b"))
    assert parse("-1", side=Side.CORE).tree == Neg(Number("1"))


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("a = 1 OR", "FLX-6103"),
        ("CASE WHEN a THEN b", "FLX-6103"),
        ("COALESCE(a, b", "FLX-6103"),
        ("a IS DISTINCT b", "FLX-6103"),
        ("a NOT b", "FLX-6103"),
        ("x IN (1,)", "FLX-6103"),
        ("then = 1", "FLX-6104"),
        ("a.end = 1", "FLX-6104"),
        ('a."end" = 1', None),
    ],
)
def test_grammar_edges(text: str, code: object) -> None:
    if code is None:
        parse(text, side=Side.CORE)
    else:
        assert _code(text) == code


def test_a_negated_null_is_still_no_operand_of_a_comparison() -> None:
    assert _code("net = -NULL") == "FLX-6104"
    assert _code("net BETWEEN -(-NULL) AND 2") == "FLX-6104"


def test_a_caller_deep_in_its_own_stack_gets_a_refusal() -> None:
    import sys

    text = "COALESCE(" * 63 + "x" + ", 0)" * 63
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    try:
        assert _code(text) == "FLX-6103"
    finally:
        sys.setrecursionlimit(limit)
    parse(text, side=Side.CORE)
