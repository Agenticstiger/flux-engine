# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The tokenizer: EXPR-SPEC §1's lexical rules and the resolutions around them."""

from __future__ import annotations

import dataclasses
import unicodedata
from decimal import Decimal

import pytest

from flux_engine.expr.errors import ParseError, ReservedWordError
from flux_engine.expr.lexer import RESERVED_WORDS, Token, TokenKind, tokenize


def kinds(source: str) -> list:
    return [token.kind for token in tokenize(source)]


@pytest.mark.unit
def test_every_stream_ends_with_one_eof() -> None:
    tokens = tokenize("a == 1")
    assert tokens[-1].kind is TokenKind.EOF
    assert [token.kind for token in tokens].count(TokenKind.EOF) == 1


@pytest.mark.unit
def test_space_and_tab_are_the_whole_whitespace_set() -> None:
    """EXPR-SPEC §1: "Whitespace: space, tab."."""
    assert kinds("a\t==\t1") == kinds("a == 1")
    for hostile in ("a ==\n1", "a ==\r1", "a\x0b== 1", "a == 1"):
        with pytest.raises(ParseError) as raised:
            tokenize(hostile)
        assert raised.value.code == "FLX-6001"


@pytest.mark.unit
def test_the_two_character_operators_are_required_in_full() -> None:
    for broken in ("a & b", "a | b", "a = 1"):
        with pytest.raises(ParseError):
            tokenize(broken)
    assert kinds("a && b")[1] is TokenKind.AND
    assert kinds("a || b")[1] is TokenKind.OR
    assert kinds("a == b")[1] is TokenKind.EQ
    assert kinds("a != b")[1] is TokenKind.NE
    assert kinds("a <= b")[1] is TokenKind.LE
    assert kinds("a >= b")[1] is TokenKind.GE
    assert kinds("a < b")[1] is TokenKind.LT
    assert kinds("a > b")[1] is TokenKind.GT
    assert kinds("!a")[0] is TokenKind.BANG


@pytest.mark.unit
def test_keywords_are_their_own_kinds_not_identifiers() -> None:
    assert kinds("true") == [TokenKind.TRUE, TokenKind.EOF]
    assert kinds("false") == [TokenKind.FALSE, TokenKind.EOF]
    assert kinds("null") == [TokenKind.NULL, TokenKind.EOF]
    assert kinds("a in b") == [TokenKind.IDENT, TokenKind.IN, TokenKind.IDENT, TokenKind.EOF]


@pytest.mark.unit
def test_matches_is_reserved_in_every_position() -> None:
    """EXPR-SPEC §1: ``matches`` "parses to ``FLX-6002`` naming flux-expr/2"."""
    assert RESERVED_WORDS == frozenset({"matches"})
    for source in ("matches", "a.matches('x')", "matches == 1", "a == matches"):
        with pytest.raises(ReservedWordError) as raised:
            tokenize(source)
        assert raised.value.code == "FLX-6002"
        assert "flux-expr/2" in str(raised.value)


@pytest.mark.unit
def test_a_word_that_merely_contains_matches_is_an_identifier() -> None:
    assert kinds("matchesFoo == 1")[0] is TokenKind.IDENT
    assert kinds("no_matches == 1")[0] is TokenKind.IDENT


@pytest.mark.unit
def test_numbers_are_decimal_and_keep_their_declared_scale() -> None:
    """EXPR-SPEC §2: "Numeric literals are parsed as ``Decimal``, never ``float``."."""
    for text, expected in (
        ("0", "0"),
        ("42", "42"),
        ("1.50", "1.50"),
        ("-3", "-3"),
        ("-0.25", "-0.25"),
    ):
        token = tokenize(text)[0]
        assert token.kind is TokenKind.NUMBER
        assert isinstance(token.value, Decimal)
        assert str(token.value) == expected


@pytest.mark.unit
def test_the_number_grammar_has_no_bare_dot_and_no_arithmetic() -> None:
    for broken in ("1.", "-", "- 1", "1 + 1"):
        with pytest.raises(ParseError):
            tokenize(broken)


@pytest.mark.unit
def test_an_exponent_is_not_part_of_a_number() -> None:
    """``1e5`` lexes as a number beside an identifier; the parser then rejects it."""
    assert kinds("1e5") == [TokenKind.NUMBER, TokenKind.IDENT, TokenKind.EOF]
    assert tokenize("1e5")[0].value == Decimal("1")


@pytest.mark.unit
def test_non_ascii_digits_are_not_digits() -> None:
    """``digit`` is ASCII; U+0660 ARABIC-INDIC ZERO would otherwise reach Decimal."""
    with pytest.raises(ParseError):
        tokenize("١٢")


@pytest.mark.unit
def test_strings_are_single_quoted_only() -> None:
    assert tokenize("'abc'")[0].value == "abc"
    with pytest.raises(ParseError) as raised:
        tokenize('"abc"')
    assert "single quotes" in str(raised.value)


@pytest.mark.unit
def test_the_only_escape_is_an_escaped_quote() -> None:
    """Resolution of the EBNF's ambiguous ``{ char - "'" | "\\'" }``."""
    assert tokenize(r"'it\'s'")[0].value == "it's"
    for broken in (r"'a\b'", r"'a\\b'", r"'a\'"):
        with pytest.raises(ParseError) as raised:
            tokenize(broken)
        assert raised.value.code == "FLX-6001"


@pytest.mark.unit
def test_an_unterminated_string_is_a_parse_error() -> None:
    with pytest.raises(ParseError) as raised:
        tokenize("'abc")
    assert "unterminated" in str(raised.value)


@pytest.mark.unit
def test_a_control_character_inside_a_string_is_rejected() -> None:
    with pytest.raises(ParseError) as raised:
        tokenize("'a\nb'")
    assert raised.value.code == "FLX-6001"


@pytest.mark.unit
def test_source_is_normalised_to_nfc_before_a_character_is_read() -> None:
    """EXPR-SPEC §5: composed and decomposed identifiers name the same path."""
    composed = "café > 0"
    decomposed = "café > 0"
    assert composed != decomposed
    assert unicodedata.normalize("NFD", composed) == decomposed
    assert tokenize(composed)[0].value == tokenize(decomposed)[0].value == "café"


@pytest.mark.unit
def test_identifiers_accept_unicode_letters_digits_and_underscore() -> None:
    for name in ("a", "_a", "a1", "a_1", "été", "late_payment_count", "tenureMonths"):
        token = tokenize(name)[0]
        assert token.kind is TokenKind.IDENT
        assert token.value == name


@pytest.mark.unit
def test_an_identifier_cannot_start_with_a_digit() -> None:
    tokens = tokenize("1a")
    assert tokens[0].kind is TokenKind.NUMBER
    assert tokens[1].kind is TokenKind.IDENT


@pytest.mark.unit
def test_positions_index_the_normalised_source() -> None:
    tokens = tokenize("  a == 1")
    assert [token.position for token in tokens[:3]] == [2, 4, 7]
    with pytest.raises(ParseError) as raised:
        tokenize("a = 1")
    assert raised.value.position == 2


@pytest.mark.unit
def test_a_token_is_frozen() -> None:
    token = Token(TokenKind.IDENT, "a", 0, "a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        token.position = 1  # type: ignore[misc]
