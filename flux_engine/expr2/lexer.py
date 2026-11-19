# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Normalize and tokenize a flux-expr/2 text (RFC-data-rules.md §2.1, EXPR-SPEC-2 §2).

The refusals that SQL tooling needs before any parser runs happen here, so
no text that breaks one ever reaches the grammar:

* the text is NFC-normalized first and is at most 4,096 code points after;
  a text more than four times that long is refused before normalizing,
  because no canonical decomposition is longer than four code points
  (U+1F82), so NFC can shrink a text by at most that factor;
* a ``;``, ``--`` or ``/*`` outside a string or a quoted name is
  ``FLX-6103`` (EXPR-SPEC-2 §2): SQL reads the last two as comments, and sqlglot
  reads ``net--1 > 0`` as ``net``;
* white space is space, tab and newline only, an unquoted name is ASCII
  ``[A-Za-z_][A-Za-z0-9_]*`` and a number is ASCII digits with an
  optional fraction: ``str.isdigit`` and ``str.isalpha`` are never used,
  because they accept digits and letters from other scripts.

Keywords are not decided here. A word is a :attr:`TokenKind.NAME` and the
parser compares it case-insensitively, on ASCII only, so no case folding
of another script can turn a name into a keyword.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from flux_engine.expr2.errors import FLX_6103, Expr2Error

__all__ = ["MAX_LENGTH", "Token", "TokenKind", "normalize", "tokenize"]

#: The longest text, in code points after NFC normalization.
MAX_LENGTH = 4096

#: No canonical decomposition is longer than this (U+1F82, Unicode 16), so a
#: raw text this many times longer than :data:`MAX_LENGTH` cannot normalize
#: into it, and is refused without paying for the normalization.
_MAX_NFC_SHRINK = 4

_WHITE_SPACE = frozenset(" \t\n")
_DIGITS = frozenset("0123456789")
_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_NAME_PART = _NAME_START | _DIGITS
_SINGLE = frozenset("+*(),.")


class TokenKind(str, Enum):
    NAME = "name"
    QUOTED_NAME = "quoted name"
    STRING = "string"
    NUMBER = "number"
    OPERATOR = "operator"
    END = "end of text"


@dataclass(frozen=True)
class Token:
    """One token: its kind, its text (unescaped for strings and quoted names) and its offset."""

    kind: TokenKind
    text: str
    position: int

    def describe(self) -> str:
        if self.kind is TokenKind.END:
            return "the end of the text"
        if self.kind is TokenKind.STRING:
            return "a string literal"
        if self.kind is TokenKind.QUOTED_NAME:
            return f'the name "{self.text}"'
        return f"{self.text!r}"


def normalize(text: str) -> str:
    """Return *text* in NFC, refusing one longer than :data:`MAX_LENGTH` (``FLX-6103``)."""
    if not isinstance(text, str):
        raise TypeError(f"a flux-expr/2 text is a str, got {type(text).__name__}")
    if len(text) > MAX_LENGTH * _MAX_NFC_SHRINK:
        raise Expr2Error(FLX_6103, f"the text is longer than {MAX_LENGTH} code points")
    normalized = unicodedata.normalize("NFC", text)
    if len(normalized) > MAX_LENGTH:
        raise Expr2Error(
            FLX_6103,
            f"the text is {len(normalized)} code points after NFC normalization; "
            f"the limit is {MAX_LENGTH}",
        )
    return normalized


def tokenize(text: str) -> List[Token]:
    """Split an NFC-normalized text into tokens, ending with one :attr:`TokenKind.END`."""
    tokens: List[Token] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in _WHITE_SPACE:
            index += 1
            continue
        start = index
        if char == ";":
            raise Expr2Error(FLX_6103, "a text holds no ';'", position=start)
        if char == "-" and text.startswith("--", index):
            raise Expr2Error(
                FLX_6103,
                "'--' starts a comment in SQL; write a double negation as 'x - -1'",
                position=start,
            )
        if char == "/" and text.startswith("/*", index):
            raise Expr2Error(FLX_6103, "'/*' starts a comment in SQL", position=start)
        if char == "'" or char == '"':
            value, index = _quoted(text, index, char)
            if char == '"' and not value:
                raise Expr2Error(FLX_6103, 'a quoted name is never empty: ""', position=start)
            kind = TokenKind.STRING if char == "'" else TokenKind.QUOTED_NAME
            tokens.append(Token(kind, value, start))
            continue
        if char in _DIGITS:
            index = _number_end(text, index)
            if index < length and (text[index] in _NAME_PART or text[index] in "'\""):
                raise Expr2Error(
                    FLX_6103,
                    "a number is digits with an optional fraction, with no exponent and "
                    "nothing joined to it",
                    position=start,
                )
            tokens.append(Token(TokenKind.NUMBER, text[start:index], start))
            continue
        if char in _NAME_START:
            index += 1
            while index < length and text[index] in _NAME_PART:
                index += 1
            tokens.append(Token(TokenKind.NAME, text[start:index], start))
            continue
        operator = _operator(text, index)
        if operator is None:
            raise Expr2Error(FLX_6103, f"unexpected character U+{ord(char):04X}", position=start)
        tokens.append(Token(TokenKind.OPERATOR, operator, start))
        index += len(operator)
    tokens.append(Token(TokenKind.END, "", length))
    return tokens


def _quoted(text: str, index: int, quote: str) -> Tuple[str, int]:
    """Read a ``'string'`` or ``"name"`` from *index*; a doubled quote is one quote."""
    parts: List[str] = []
    position = index + 1
    while True:
        end = text.find(quote, position)
        if end < 0:
            what = "string" if quote == "'" else "quoted name"
            raise Expr2Error(FLX_6103, f"the {what} is never closed", position=index)
        parts.append(text[position:end])
        if text.startswith(quote * 2, end):
            parts.append(quote)
            position = end + 2
            continue
        return "".join(parts), end + 1


def _number_end(text: str, index: int) -> int:
    """Return the end of the digits-with-optional-fraction number starting at *index*."""
    length = len(text)
    while index < length and text[index] in _DIGITS:
        index += 1
    if index + 1 < length and text[index] == "." and text[index + 1] in _DIGITS:
        index += 1
        while index < length and text[index] in _DIGITS:
            index += 1
    return index


def _operator(text: str, index: int) -> Optional[str]:
    char = text[index]
    if char in _SINGLE or char in "=-/":
        return char
    if char == "<":
        if text.startswith("<=", index) or text.startswith("<>", index):
            return text[index : index + 2]
        return "<"
    if char == ">":
        return ">=" if text.startswith(">=", index) else ">"
    if char == "!" and text.startswith("!=", index):
        return "!="
    if char == "|" and text.startswith("||", index):
        # The SQL standard's concatenation (EXPR-SPEC-3 §2). The parser refuses it
        # outside a flux-expr/3 FLUX-side text, so a flux-expr/2 text meets the same
        # FLX-6103 it met when this character was unknown.
        return "||"
    return None
