# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Hand-written tokenizer for ``flux-expr/1`` (EXPR-SPEC §1, §4).

The grammar is closed: "anything not derivable is a parse error
(``FLX-6001``), never an extension point". This module is therefore written
to reject rather than to recover -- there is no error node, no skipped
character and no lookahead beyond one character.

Three rules resolved here that the EBNF leaves open, each in the restrictive
direction the closed-grammar policy calls for:

* **NFC first.** The source is normalised to NFC before a single character is
  read, so the composed and decomposed spellings of an identifier name the
  same path (EXPR-SPEC §5, "NFC/NFD identifier equivalence"). Every offset a
  token or an error reports indexes the *normalised* text.
* **Backslash.** ``string = "'" , { char - "'" | "\\'" } , "'"`` does not say
  what a bare backslash means. Resolved: a backslash is legal only as the
  two-character escape ``\\'``; any other backslash is ``FLX-6001``. A string
  value therefore never contains a backslash, which is what makes
  ``unparse`` total and a fixed point.
* **Control characters.** A character below U+0020 (a newline included) is
  ``FLX-6001`` anywhere in the source, including inside a string literal:
  EXPR-SPEC §1 lists space and tab as the whole of the whitespace set, and a
  canonical form that can carry a raw newline is not a single line of
  ``plan.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import List, Tuple

from flux_engine.expr.errors import ParseError, ReservedWordError
from flux_engine.expr.types import nfc

__all__ = ["Token", "TokenKind", "RESERVED_WORDS", "tokenize"]

#: Words held for ``flux-expr/2``. EXPR-SPEC §1: "``matches`` is a **reserved
#: word** that parses to ``FLX-6002`` naming flux-expr/2". Reserved in every
#: position -- as a bare name, as a path segment and as a method name -- so
#: that the word cannot acquire a meaning by accident in flux-expr/1.
RESERVED_WORDS: frozenset = frozenset({"matches"})

#: EXPR-SPEC §1: "Whitespace: space, tab."
_WHITESPACE = " \t"


class TokenKind(str, Enum):
    """Every token flux-expr/1 has. Closed."""

    IDENT = "ident"
    NUMBER = "number"
    STRING = "string"
    TRUE = "true"
    FALSE = "false"
    NULL = "null"
    IN = "in"
    LPAREN = "("
    RPAREN = ")"
    LBRACKET = "["
    RBRACKET = "]"
    COMMA = ","
    DOT = "."
    BANG = "!"
    AND = "&&"
    OR = "||"
    EQ = "=="
    NE = "!="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    EOF = "<end of expression>"


#: ``ident``s that are keywords rather than names. ``matches`` is handled
#: separately because it raises rather than producing a token.
_KEYWORDS = {
    "true": TokenKind.TRUE,
    "false": TokenKind.FALSE,
    "null": TokenKind.NULL,
    "in": TokenKind.IN,
}

#: Operators that are exactly one character and have no two-character form.
_SINGLES = {
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
    "[": TokenKind.LBRACKET,
    "]": TokenKind.RBRACKET,
    ",": TokenKind.COMMA,
    ".": TokenKind.DOT,
}

#: Comparison operator kinds, consumed by the parser as ``cmp_op``.
COMPARISON_KINDS = (
    TokenKind.EQ,
    TokenKind.NE,
    TokenKind.LT,
    TokenKind.LE,
    TokenKind.GT,
    TokenKind.GE,
)


@dataclass(frozen=True)
class Token:
    """One token: its kind, the exact text consumed, and its start offset.

    ``value`` carries the decoded payload for the three literal kinds -- a
    ``Decimal`` for ``NUMBER`` (never a ``float``; KERNEL-SPEC §2.1), the
    unescaped NFC string for ``STRING``, the identifier for ``IDENT`` -- and
    is the empty string for every operator.
    """

    kind: TokenKind
    text: str
    position: int
    value: object = ""

    def __str__(self) -> str:
        return self.text or self.kind.value


def _is_ident_start(char: str) -> bool:
    """``ident = ( letter | "_" ) , ...`` -- ``letter`` is any Unicode letter."""
    return char == "_" or char.isalpha()


def _is_ident_part(char: str) -> bool:
    """``... , { letter | digit | "_" }``."""
    return char == "_" or char.isalpha() or char.isdecimal()


def _read_string(source: str, start: int) -> Tuple[str, int]:
    """Read a single-quoted string starting at the quote; return (value, end)."""
    index = start + 1
    chars: List[str] = []
    while index < len(source):
        char = source[index]
        if char == "'":
            return "".join(chars), index + 1
        if char == "\\":
            if index + 1 < len(source) and source[index + 1] == "'":
                chars.append("'")
                index += 2
                continue
            raise ParseError(
                "a backslash is only legal as the escape \\' inside a string",
                position=index,
            )
        if char < " ":
            raise ParseError(
                f"control character U+{ord(char):04X} is not legal in a string literal",
                position=index,
            )
        chars.append(char)
        index += 1
    raise ParseError("unterminated string literal", position=start)


def _read_number(source: str, start: int) -> Tuple[Decimal, int]:
    """Read ``[ "-" ] , digit , { digit } , [ "." , digit , { digit } ]``.

    Parsed as ``Decimal`` with the literal's declared scale preserved
    (``0.50`` stays ``0.50``) -- EXPR-SPEC §2: "Numeric literals are parsed as
    ``Decimal``, never ``float``."
    """
    index = start
    if source[index] == "-":
        index += 1
    digits_start = index
    while index < len(source) and source[index].isdecimal():
        index += 1
    if index == digits_start:
        raise ParseError(
            "a '-' must be followed by a digit; there is no arithmetic", position=start
        )
    if index < len(source) and source[index] == ".":
        fraction_start = index + 1
        index = fraction_start
        while index < len(source) and source[index].isdecimal():
            index += 1
        if index == fraction_start:
            raise ParseError("a decimal point must be followed by a digit", position=start)
    text = source[start:index]
    # ``isdecimal`` accepts non-ASCII decimal digits, which ``Decimal`` would
    # then accept too; the grammar's ``digit`` is ASCII, so reject the rest
    # rather than silently admitting U+0660 and friends.
    if any(char not in "-.0123456789" for char in text):
        raise ParseError("a number is written with ASCII digits", position=start)
    return Decimal(text), index


def tokenize(source: str) -> Tuple[Token, ...]:
    """Tokenize *source*, ending with a single ``EOF`` token.

    Raises :class:`~flux_engine.expr.errors.ParseError` (``FLX-6001``) for any
    character or character sequence the grammar cannot derive, and
    :class:`~flux_engine.expr.errors.ReservedWordError` (``FLX-6002``) for a
    word held for ``flux-expr/2``.
    """
    text = nfc(source)
    tokens: List[Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in _WHITESPACE:
            index += 1
            continue
        if char < " " or char == "\x7f":
            raise ParseError(
                f"control character U+{ord(char):04X} is not whitespace in flux-expr/1",
                position=index,
            )
        if _is_ident_start(char):
            start = index
            index += 1
            while index < length and _is_ident_part(text[index]):
                index += 1
            word = text[start:index]
            if word in RESERVED_WORDS:
                raise ReservedWordError(
                    f"'{word}' is reserved for flux-expr/2 and has no meaning in flux-expr/1",
                    position=start,
                )
            keyword = _KEYWORDS.get(word)
            if keyword is not None:
                tokens.append(Token(keyword, word, start))
            else:
                tokens.append(Token(TokenKind.IDENT, word, start, word))
            continue
        if char.isdecimal() or char == "-":
            value, end = _read_number(text, index)
            tokens.append(Token(TokenKind.NUMBER, text[index:end], index, value))
            index = end
            continue
        if char == "'":
            value_str, end = _read_string(text, index)
            tokens.append(Token(TokenKind.STRING, text[index:end], index, value_str))
            index = end
            continue
        if char in _SINGLES:
            tokens.append(Token(_SINGLES[char], char, index))
            index += 1
            continue
        nxt = text[index + 1] if index + 1 < length else ""
        if char == "&":
            if nxt != "&":
                raise ParseError("'&' is not an operator; conjunction is '&&'", position=index)
            tokens.append(Token(TokenKind.AND, "&&", index))
            index += 2
            continue
        if char == "|":
            if nxt != "|":
                raise ParseError("'|' is not an operator; disjunction is '||'", position=index)
            tokens.append(Token(TokenKind.OR, "||", index))
            index += 2
            continue
        if char == "=":
            if nxt != "=":
                raise ParseError("'=' is not an operator; equality is '=='", position=index)
            tokens.append(Token(TokenKind.EQ, "==", index))
            index += 2
            continue
        if char == "!":
            if nxt == "=":
                tokens.append(Token(TokenKind.NE, "!=", index))
                index += 2
            else:
                tokens.append(Token(TokenKind.BANG, "!", index))
                index += 1
            continue
        if char == "<":
            kind, size = (TokenKind.LE, 2) if nxt == "=" else (TokenKind.LT, 1)
            tokens.append(Token(kind, text[index : index + size], index))
            index += size
            continue
        if char == ">":
            kind, size = (TokenKind.GE, 2) if nxt == "=" else (TokenKind.GT, 1)
            tokens.append(Token(kind, text[index : index + size], index))
            index += size
            continue
        if char == '"':
            raise ParseError(
                "flux-expr/1 has no double-quoted strings; use single quotes",
                position=index,
            )
        raise ParseError(f"unexpected character {char!r}", position=index)
    tokens.append(Token(TokenKind.EOF, "", length))
    return tuple(tokens)
