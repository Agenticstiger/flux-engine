# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Parse a flux-expr/2 text into its syntax tree (RFC-data-rules.md §2.1, EXPR-SPEC-2 §2).

A hand-written recursive-descent parser with one function per level of the
RFC's grammar (``or_expr``, ``and_expr``, ``not_expr``, ``predicate``,
``value``, ``term``, ``factor``, ``primary``), the shape sqlglot's parser and
flux-expr/1's take. No parser library: the core gains no dependency, and
the CLI's cold path stays small (ADR 0008).

A text is read for one of two sides. A **core** text is a FLUID contract's
FLUID SQL/1: a bare name is always a column and ``a.b`` always a
relationship's column, and the extensions do not parse. A **FLUX-side**
text is a bundle rule's, where ``row``, ``link``, ``previous`` and
``CHILDREN`` are roots and the four extensions parse (EXPR-SPEC-2 §3).

Depth is the syntax tree's depth, and a text deeper than 64 is
``FLX-6103`` (maintainers' decision, 2026-11-16): a literal or a name is 1,
and every operator, ``NOT``, minus, call, ``CASE`` and parenthesis adds a
level to the deepest part it holds. The parser counts the constructs it is
inside while it descends as well, so a text of thousands of nested
parentheses is refused before Python's own recursion limit is near.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Callable, List, Optional, Tuple, Union

from flux_engine.expr2.ast import (
    AGGREGATES,
    CORE_FUNCTIONS,
    CORE_KEYWORDS,
    DATE_ADD_FUNCTIONS,
    DEFAULT_ROUND_MODE,
    EXTRACT_FIELDS,
    LANGUAGES,
    ROOTS,
    ROUND_MODES,
    WINDOWS,
    Aggregate,
    And,
    Arith,
    Between,
    Bool,
    Call,
    Case,
    Children,
    Column,
    Compare,
    Concat,
    Date,
    DateAdd,
    Extract,
    In,
    IsDistinctFrom,
    IsNull,
    Neg,
    Node,
    Not,
    Null,
    Number,
    Or,
    Path,
    SortKey,
    String,
    Window,
)
from flux_engine.expr2.errors import (
    FLX_6103,
    FLX_6104,
    FLX_6106,
    FLX_6107,
    FLX_6109,
    Expr2Error,
)
from flux_engine.expr2.lexer import Token, TokenKind, normalize, tokenize
from flux_engine.expr2.types import MAX_ROUND_SCALE

__all__ = ["DEFAULT_LANGUAGE", "MAX_DEPTH", "Parsed", "Side", "parse"]

#: The language a text is read as when none is named.
DEFAULT_LANGUAGE = "flux-expr/2"

#: The deepest syntax tree a text may have.
MAX_DEPTH = 64

_COMPARISONS = frozenset({"=", "<>", "!=", "<", "<=", ">", ">="})
_DATE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

#: Value arguments each window takes: (fewest, most).
_WINDOW_ARITY = {
    "ROW_NUMBER": (0, 0),
    "RANK": (0, 0),
    "DENSE_RANK": (0, 0),
    "FIRST_VALUE": (1, 1),
    "LAST_VALUE": (1, 1),
    "LAG": (1, 3),
    "LEAD": (1, 3),
}


class Side(str, Enum):
    """Whose text it is: a FLUID contract's (core) or a FLUX bundle rule's."""

    CORE = "core"
    FLUX = "flux"


@dataclass(frozen=True)
class Parsed:
    """A parsed text: its NFC-normalized source, its tree, its side, its depth and its language."""

    text: str
    tree: Node
    side: Side
    depth: int
    language: str = DEFAULT_LANGUAGE


def parse(text: str, *, side: Union[Side, str], language: str = DEFAULT_LANGUAGE) -> Parsed:
    """Parse *text* as a core or a FLUX-side flux-expr/2 or flux-expr/3 text.

    *language* is the one a FLUX-side rule declares (``flux-expr/2``, the
    default, or ``flux-expr/3``). flux-expr/3 adds ``EXTRACT``, ``ADD_DAYS``,
    ``ADD_MONTHS``, ``ADD_YEARS`` and ``||`` (EXPR-SPEC-3 §2), which a core
    text and a flux-expr/2 text refuse with ``FLX-6103``. A flux-expr/2 text
    parses to the same tree under either language.

    Raises :class:`~flux_engine.expr2.errors.Expr2Error` with ``FLX-6103``
    (outside the grammar or past a limit), ``FLX-6104`` (a reserved word
    where it has no meaning), ``FLX-6106`` (a root path that cannot resolve
    as written) or ``FLX-6109`` (a literal argument of the wrong form).
    Everything that needs the expose's names is the checker's.
    """
    which = Side(side)
    if language not in LANGUAGES:
        raise ValueError(f"language is one of {', '.join(LANGUAGES)}, not {language!r}")
    normalized = normalize(text)
    tokens = tokenize(normalized)
    if not (which is Side.FLUX and language == LANGUAGES[1]):
        # Refused before the grammar is read, as an unknown character was before ||
        # was a token, so no earlier error in the text can take its place.
        for token in tokens:
            if token.kind is TokenKind.OPERATOR and token.text == "||":
                raise Expr2Error(
                    FLX_6103,
                    "'||' is a flux-expr/3 extension of a FLUX-side rule; a contract's texts "
                    "are FLUID SQL/1",
                    position=token.position,
                )
    parser = _Parser(tokens, which, language)
    try:
        tree, depth = parser.expr()
    except RecursionError:
        # The depth limit keeps an accepted text to about 700 frames; a caller
        # already deep in its own stack gets a refusal, never the exception.
        raise Expr2Error(
            FLX_6103, "the text nests deeper than this process's stack can parse"
        ) from None
    parser.expect_end()
    return Parsed(normalized, tree, which, depth, language)


def _places(text: str) -> str:
    """ROUND's places as the integer they name: ``02`` is ``2`` (RFC §2.1's integer).

    Every other number literal keeps its digits as written, as forge-cli's
    ``fluid-sql-vectors.json`` writes it, so ``007.50`` stays ``007.50``.
    """
    return text.lstrip("0") or "0"


class _Parser:
    def __init__(self, tokens: List[Token], side: Side, language: str) -> None:
        self.tokens = tokens
        self.index = 0
        self.side = side
        self.language = language
        self.open = 0

    def refuse_unless_flux_expr_3(self, what: str, position: int) -> None:
        """A flux-expr/3 form in a core text or a flux-expr/2 text is ``FLX-6103`` (EXPR-SPEC-3 §1)."""
        if self.side is Side.CORE:
            raise Expr2Error(
                FLX_6103,
                f"{what} is a flux-expr/3 extension, a FLUX-side rule's; a contract's texts are "
                "FLUID SQL/1",
                position=position,
            )
        if self.language != "flux-expr/3":
            raise Expr2Error(
                FLX_6103,
                f"{what} is a flux-expr/3 extension; declare language: flux-expr/3",
                position=position,
            )

    # ---------------------------------------------------------- tokens

    def peek(self, ahead: int = 0) -> Token:
        return self.tokens[min(self.index + ahead, len(self.tokens) - 1)]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        if token.kind is not TokenKind.END:
            self.index += 1
        return token

    def at_keyword(self, word: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token.kind is TokenKind.NAME and token.text.upper() == word

    def at_op(self, op: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token.kind is TokenKind.OPERATOR and token.text == op

    def expect_keyword(self, word: str) -> Token:
        if not self.at_keyword(word):
            token = self.peek()
            raise Expr2Error(
                FLX_6103, f"expected {word}, found {token.describe()}", position=token.position
            )
        return self.advance()

    def expect_op(self, op: str) -> Token:
        if not self.at_op(op):
            token = self.peek()
            raise Expr2Error(
                FLX_6103, f"expected '{op}', found {token.describe()}", position=token.position
            )
        return self.advance()

    def expect_end(self) -> None:
        token = self.peek()
        if token.kind is not TokenKind.END:
            raise Expr2Error(
                FLX_6103,
                f"unexpected {token.describe()} after a complete expression",
                position=token.position,
            )

    # ----------------------------------------------------------- depth

    def level(self, depth: int, position: int) -> int:
        if depth > MAX_DEPTH:
            raise Expr2Error(
                FLX_6103,
                f"the syntax tree is deeper than {MAX_DEPTH} levels; every operator, NOT, "
                "minus, call, CASE and parenthesis is a level",
                position=position,
            )
        return depth

    def enter(self, position: int) -> None:
        self.open += 1
        self.level(self.open + 1, position)

    def leave(self) -> None:
        self.open -= 1

    # --------------------------------------------------------- grammar

    def expr(self) -> Tuple[Node, int]:
        node, depth = self.and_expr()
        while self.at_keyword("OR"):
            position = self.advance().position
            right, right_depth = self.and_expr()
            node = Or(node, right, position=_start(node))
            depth = self.level(1 + max(depth, right_depth), position)
        return node, depth

    def and_expr(self) -> Tuple[Node, int]:
        node, depth = self.not_expr()
        while self.at_keyword("AND"):
            position = self.advance().position
            right, right_depth = self.not_expr()
            node = And(node, right, position=_start(node))
            depth = self.level(1 + max(depth, right_depth), position)
        return node, depth

    def not_expr(self) -> Tuple[Node, int]:
        nots: List[int] = []
        while self.at_keyword("NOT"):
            nots.append(self.advance().position)
        node, depth = self.predicate()
        for position in reversed(nots):
            node = Not(node, position=position)
            depth = self.level(depth + 1, position)
        return node, depth

    def predicate(self) -> Tuple[Node, int]:
        left, depth = self.concat()
        token = self.peek()
        node: Node
        if token.kind is TokenKind.OPERATOR and token.text in _COMPARISONS:
            self.advance()
            if token.text == "=" and self.at_op("="):
                raise Expr2Error(
                    FLX_6103, "SQL writes equality as a single '='", position=token.position
                )
            right, right_depth = self.concat()
            for operand in (left, right):
                self.refuse_null(operand, f"an operand of '{token.text}'")
            op = "<>" if token.text == "!=" else token.text
            node = Compare(op, left, right, position=_start(left))
            depth = max(depth, right_depth)
        elif self.at_keyword("NOT") and (self.at_keyword("IN", 1) or self.at_keyword("BETWEEN", 1)):
            self.advance()
            node, depth = self.in_or_between(left, depth, negated=True)
        elif self.at_keyword("IN") or self.at_keyword("BETWEEN"):
            node, depth = self.in_or_between(left, depth, negated=False)
        elif self.at_keyword("IS"):
            node, depth = self.is_test(left, depth)
        else:
            return left, depth
        depth = self.level(depth + 1, token.position)
        self.refuse_chain()
        return node, depth

    def refuse_chain(self) -> None:
        token = self.peek()
        chained = (
            (token.kind is TokenKind.OPERATOR and token.text in _COMPARISONS)
            or self.at_keyword("IN")
            or self.at_keyword("BETWEEN")
            or self.at_keyword("IS")
            or (
                self.at_keyword("NOT")
                and (self.at_keyword("IN", 1) or self.at_keyword("BETWEEN", 1))
            )
        )
        if chained:
            raise Expr2Error(
                FLX_6103,
                "comparisons do not chain: join them with AND, or put one in parentheses",
                position=token.position,
            )

    def refuse_null(self, node: Node, where: str) -> None:
        # A minus in front changes nothing: -NULL is NULL, and the test is as empty.
        while isinstance(node, Neg):
            node = node.operand
        if isinstance(node, Null):
            raise Expr2Error(
                FLX_6104,
                f"NULL is never {where}: such a test is always FALSE; write 'x IS NULL', "
                "'x IS NOT NULL' or 'a IS NOT DISTINCT FROM b'",
                position=node.position,
            )

    def in_or_between(self, left: Node, depth: int, *, negated: bool) -> Tuple[Node, int]:
        keyword = self.advance()
        if keyword.text.upper() == "BETWEEN":
            low, low_depth = self.concat()
            self.expect_keyword("AND")
            high, high_depth = self.concat()
            for operand in (left, low, high):
                self.refuse_null(operand, "an operand of BETWEEN")
            node = Between(left, low, high, negated, position=_start(left))
            return node, max(depth, low_depth, high_depth)
        self.refuse_null(left, "the left side of IN")
        self.expect_op("(")
        items: List[Node] = []
        item_depth = 1
        while True:
            item, this_depth = self.in_item()
            items.append(item)
            item_depth = max(item_depth, this_depth)
            if self.at_op(","):
                self.advance()
                continue
            self.expect_op(")")
            break
        return In(left, tuple(items), negated, position=_start(left)), max(depth, item_depth)

    def in_item(self) -> Tuple[Node, int]:
        token = self.peek()
        if self.at_op("-") and self.peek(1).kind is TokenKind.NUMBER:
            self.advance()
            number = self.advance()
            node = Number(number.text, position=number.position)
            # RFC §2.1's in_item is ``[ "-" ] number``: one literal, one level.
            return Neg(node, position=token.position), 1
        if token.kind is TokenKind.NUMBER:
            self.advance()
            return Number(token.text, position=token.position), 1
        if token.kind is TokenKind.STRING:
            self.advance()
            return String(token.text, position=token.position), 1
        if self.at_keyword("TRUE") or self.at_keyword("FALSE"):
            self.advance()
            return Bool(token.text.upper() == "TRUE", position=token.position), 1
        if self.at_keyword("DATE") and self.peek(1).kind is TokenKind.STRING:
            return self.date_literal(), 1
        if self.at_keyword("NULL"):
            raise Expr2Error(
                FLX_6104,
                "NULL is never an IN item: such a test is always FALSE; write 'x IS NULL'",
                position=token.position,
            )
        raise Expr2Error(
            FLX_6103,
            f"an IN list holds literals only, found {token.describe()}",
            position=token.position,
        )

    def is_test(self, left: Node, depth: int) -> Tuple[Node, int]:
        self.advance()
        negated = False
        if self.at_keyword("NOT"):
            self.advance()
            negated = True
        if self.at_keyword("NULL"):
            self.advance()
            return IsNull(left, negated, position=_start(left)), depth
        if self.at_keyword("DISTINCT"):
            self.advance()
            self.expect_keyword("FROM")
            right, right_depth = self.concat()
            return IsDistinctFrom(left, right, negated, position=_start(left)), max(
                depth, right_depth
            )
        token = self.peek()
        raise Expr2Error(
            FLX_6103,
            f"IS is followed by [NOT] NULL or [NOT] DISTINCT FROM, found {token.describe()}",
            position=token.position,
        )

    def concat(self) -> Tuple[Node, int]:
        """``value { "||" value }``: looser than ``+`` and ``-``, tighter than a comparison (EXPR-SPEC-3 §2)."""
        node, depth = self.value()
        while self.at_op("||"):
            operator = self.advance()
            self.refuse_unless_flux_expr_3("'||'", operator.position)
            right, right_depth = self.value()
            node = Concat(node, right, position=_start(node))
            depth = self.level(1 + max(depth, right_depth), operator.position)
        return node, depth

    def value(self) -> Tuple[Node, int]:
        node, depth = self.term()
        while self.at_op("+") or self.at_op("-"):
            operator = self.advance()
            right, right_depth = self.term()
            node = Arith(operator.text, node, right, position=_start(node))
            depth = self.level(1 + max(depth, right_depth), operator.position)
        return node, depth

    def term(self) -> Tuple[Node, int]:
        node, depth = self.factor()
        while self.at_op("*") or self.at_op("/"):
            operator = self.advance()
            right, right_depth = self.factor()
            node = Arith(operator.text, node, right, position=_start(node))
            depth = self.level(1 + max(depth, right_depth), operator.position)
        return node, depth

    def factor(self) -> Tuple[Node, int]:
        if not self.at_op("-"):
            return self.primary()
        minus = self.advance()
        if self.at_op("-"):
            raise Expr2Error(
                FLX_6103,
                "a minus applies to one value: write a double negation as -(-x)",
                position=self.peek().position,
            )
        operand, depth = self.primary()
        return Neg(operand, position=minus.position), self.level(depth + 1, minus.position)

    def primary(self) -> Tuple[Node, int]:
        token = self.peek()
        if token.kind is TokenKind.NUMBER:
            self.advance()
            return Number(token.text, position=token.position), 1
        if token.kind is TokenKind.STRING:
            self.advance()
            return String(token.text, position=token.position), 1
        if self.at_op("("):
            self.advance()
            self.enter(token.position)
            inner, depth = self.expr()
            self.expect_op(")")
            self.leave()
            return inner, self.level(depth + 1, token.position)
        if token.kind is TokenKind.NAME:
            word = token.text.upper()
            if word in ("TRUE", "FALSE"):
                self.advance()
                return Bool(word == "TRUE", position=token.position), 1
            if word == "NULL":
                self.advance()
                return Null(position=token.position), 1
            if word == "DATE" and self.peek(1).kind is TokenKind.STRING:
                return self.date_literal(), 1
            if word == "CASE":
                return self.case()
            if word == "NOT":
                raise Expr2Error(
                    FLX_6103,
                    "NOT applies to a predicate: put it in parentheses, as in a = (NOT b)",
                    position=token.position,
                )
            if word in CORE_KEYWORDS:
                raise _reserved(token)
            if self.at_op("(", 1):
                return self.call()
            return self.path(), 1
        if token.kind is TokenKind.QUOTED_NAME:
            return self.path(), 1
        if self.at_op("-"):
            raise Expr2Error(
                FLX_6103,
                "a minus applies to one value: write a double negation as -(-x)",
                position=token.position,
            )
        raise Expr2Error(
            FLX_6103, f"expected a value, found {token.describe()}", position=token.position
        )

    def date_literal(self) -> Date:
        keyword = self.advance()
        text = self.advance()
        if not _DATE_TEXT.fullmatch(text.text):
            raise Expr2Error(
                FLX_6109, "a DATE literal is written DATE 'YYYY-MM-DD'", position=text.position
            )
        try:
            date(int(text.text[:4]), int(text.text[5:7]), int(text.text[8:]))
        except ValueError:
            raise Expr2Error(
                FLX_6109,
                "the DATE literal is not a calendar date from 0001-01-01 to 9999-12-31",
                position=text.position,
            ) from None
        return Date(text.text, position=keyword.position)

    def case(self) -> Tuple[Node, int]:
        start = self.advance()
        self.enter(start.position)
        if not self.at_keyword("WHEN"):
            raise Expr2Error(
                FLX_6103,
                "CASE is followed by WHEN; FLUID SQL/1 has no CASE <value> WHEN form",
                position=self.peek().position,
            )
        whens: List[Tuple[Node, Node]] = []
        depth = 1
        while self.at_keyword("WHEN"):
            self.advance()
            condition, condition_depth = self.expr()
            self.expect_keyword("THEN")
            result, result_depth = self.expr()
            whens.append((condition, result))
            depth = max(depth, condition_depth, result_depth)
        otherwise: Optional[Node] = None
        if self.at_keyword("ELSE"):
            self.advance()
            otherwise, else_depth = self.expr()
            depth = max(depth, else_depth)
        self.expect_keyword("END")
        self.leave()
        node = Case(tuple(whens), otherwise, position=start.position)
        return node, self.level(depth + 1, start.position)

    # ----------------------------------------------------------- names

    def name_after_dot(self) -> str:
        token = self.peek()
        if token.kind is TokenKind.QUOTED_NAME:
            return self.advance().text
        if token.kind is TokenKind.NAME:
            if token.text.upper() in CORE_KEYWORDS:
                raise _reserved(token)
            return self.advance().text
        raise Expr2Error(
            FLX_6103, f"a name follows '.', found {token.describe()}", position=token.position
        )

    def path(self) -> Node:
        first = self.advance()
        parts = [first.text]
        while self.at_op("."):
            self.advance()
            parts.append(self.name_after_dot())
        position = first.position
        if self.side is Side.CORE:
            if len(parts) == 1:
                return Column(parts[0], position=position)
            if len(parts) == 2:
                return Column(parts[1], parts[0], position=position)
            raise Expr2Error(
                FLX_6103,
                "a contract text crosses one link (<relationship>.<column>); a path over "
                "several links is a FLUX-side extension",
                position=position,
            )
        root = None
        if first.kind is TokenKind.NAME and first.text.lower() in ROOTS:
            root = first.text.lower()
        if root is None or len(parts) == 1:
            note = first.text if root is not None else None
            if len(parts) == 1:
                return Column(parts[0], root_note=note, position=position)
            if len(parts) == 2:
                return Column(parts[1], parts[0], position=position)
            return Path("link", tuple(parts), position=position)
        if root == "row":
            if len(parts) != 2:
                raise Expr2Error(
                    FLX_6106,
                    "row.<column> names one column of this row; a path through a link "
                    "starts with link.",
                    position=position,
                )
            return Column(parts[1], position=position)
        if root == "link":
            if len(parts) == 2:
                raise Expr2Error(
                    FLX_6106,
                    "a relationship is not a value: write link.<relationship>.<column>",
                    position=position,
                )
            if len(parts) == 3:
                return Column(parts[2], parts[1], position=position)
            return Path("link", tuple(parts[1:]), position=position)
        if root == "previous":
            if len(parts) > 3:
                raise Expr2Error(
                    FLX_6103,
                    "previous reaches a column or one relationship's column: "
                    "previous.<column> or previous.<relationship>.<column>",
                    position=position,
                )
            return Path("previous", tuple(parts[1:]), root_note=first.text, position=position)
        raise Expr2Error(
            FLX_6106,
            "a children collection is not a value: it is an aggregate's first argument, "
            "as in COUNT(CHILDREN.<expose>)",
            position=position,
        )

    # ----------------------------------------------------------- calls

    def call(self) -> Tuple[Node, int]:
        name = self.advance()
        word = name.text.upper()
        self.advance()  # "("
        self.enter(name.position)
        if word in CORE_FUNCTIONS:
            node, depth = self.core_call(word, name.position)
        elif word in AGGREGATES or word in WINDOWS:
            if self.side is Side.CORE:
                raise Expr2Error(
                    FLX_6103,
                    f"{word} is a FLUX-side extension; a contract writes a summary over "
                    "children as derive with aggregate",
                    position=name.position,
                )
            if word in AGGREGATES:
                node, depth = self.aggregate(word, name.position)
            else:
                node, depth = self.window(word, name.position)
        elif word == "EXTRACT" or word in DATE_ADD_FUNCTIONS:
            self.refuse_unless_flux_expr_3(word, name.position)
            if word == "EXTRACT":
                node, depth = self.extract(name.position)
            else:
                node, depth = self.date_add(word, name.position)
        else:
            raise Expr2Error(
                FLX_6103, f"FLUID SQL/1 has no function {name.text}", position=name.position
            )
        self.leave()
        return node, self.level(depth + 1, name.position)

    def core_call(self, word: str, position: int) -> Tuple[Node, int]:
        if word == "ROUND":
            value, depth = self.value()
            self.expect_op(",")
            scale = self.peek()
            if scale.kind is not TokenKind.NUMBER or "." in scale.text:
                raise Expr2Error(
                    FLX_6103,
                    "ROUND's scale is a whole number literal, as in ROUND(x, 2)",
                    position=scale.position,
                )
            self.advance()
            if int(scale.text) > MAX_ROUND_SCALE:
                raise Expr2Error(
                    FLX_6103, f"ROUND's scale is at most {MAX_ROUND_SCALE}", position=scale.position
                )
            mode = DEFAULT_ROUND_MODE
            if self.at_op(","):
                self.advance()
                mode_token = self.peek()
                if mode_token.kind is not TokenKind.STRING or mode_token.text not in ROUND_MODES:
                    # RFC §2.1 spells round_mode as two terminals, upper case.
                    raise Expr2Error(
                        FLX_6103,
                        "ROUND's mode is 'HALF_AWAY_FROM_ZERO' or 'HALF_TO_EVEN', in upper case",
                        position=mode_token.position,
                    )
                mode = self.advance().text
            self.expect_op(")")
            digits = Number(_places(scale.text), position=scale.position)
            return Call("ROUND", (value, digits), mode, position=position), depth
        if word == "COALESCE":
            args, depth = self.arguments(self.expr)
            if len(args) < 2:
                raise Expr2Error(
                    FLX_6103, "COALESCE takes two arguments or more", position=position
                )
            return Call(word, tuple(args), position=position), depth
        args, depth = self.arguments(self.value)
        if len(args) != 2:
            raise Expr2Error(
                FLX_6103, f"{word} takes two dates, the earlier first", position=position
            )
        return Call(word, tuple(args), position=position), depth

    def extract(self, position: int) -> Tuple[Node, int]:
        """``EXTRACT(YEAR|MONTH|DAY FROM expr)``; the field words are keywords only here."""
        field = self.peek()
        if field.kind is not TokenKind.NAME or field.text.upper() not in EXTRACT_FIELDS:
            raise Expr2Error(
                FLX_6103,
                "EXTRACT's field is YEAR, MONTH or DAY, as in EXTRACT(DAY FROM d)",
                position=field.position,
            )
        self.advance()
        self.expect_keyword("FROM")
        arg, depth = self.expr()
        self.expect_op(")")
        return Extract(field.text.lower(), arg, position=position), depth

    def date_add(self, word: str, position: int) -> Tuple[Node, int]:
        """``ADD_DAYS|ADD_MONTHS|ADD_YEARS(date, count)``."""
        args, depth = self.arguments(self.expr)
        if len(args) != 2:
            raise Expr2Error(
                FLX_6103,
                f"{word} takes a date and a count of {DATE_ADD_FUNCTIONS[word]}s",
                position=position,
            )
        return DateAdd(DATE_ADD_FUNCTIONS[word], args[0], args[1], position=position), depth

    def arguments(self, read: Callable[[], Tuple[Node, int]]) -> Tuple[List[Node], int]:
        args: List[Node] = []
        depth = 1
        while True:
            arg, arg_depth = read()
            args.append(arg)
            depth = max(depth, arg_depth)
            if self.at_op(","):
                self.advance()
                continue
            self.expect_op(")")
            return args, depth

    def aggregate(self, word: str, position: int) -> Tuple[Node, int]:
        first = self.peek()
        if not (
            first.kind is TokenKind.NAME and first.text.lower() == "children" and self.at_op(".", 1)
        ):
            raise Expr2Error(
                FLX_6107,
                f"{word}'s first argument is a children collection, CHILDREN.<expose>",
                position=first.position,
            )
        children = self.children()
        arg: Optional[Node] = None
        depth = 1
        if self.at_op(","):
            self.advance()
            arg, depth = self.expr()
        self.expect_op(")")
        if self.at_keyword("OVER"):
            raise Expr2Error(
                FLX_6103,
                f"{word} over children is not written as a window: flux-expr/2 has no "
                "aggregates over windows",
                position=self.peek().position,
            )
        return Aggregate(word, children, arg, position=position), depth

    def children(self) -> Children:
        root = self.advance()
        self.advance()  # "."
        expose = self.name_after_dot()
        relationship: Optional[str] = None
        if self.at_op("."):
            self.advance()
            relationship = self.name_after_dot()
        if self.at_op("."):
            raise Expr2Error(
                FLX_6103,
                "a children collection is CHILDREN.<expose> or CHILDREN.<expose>.<relationship>",
                position=root.position,
            )
        return Children(expose, relationship, root_note=root.text, position=root.position)

    def window(self, word: str, position: int) -> Tuple[Node, int]:
        args: List[Node] = []
        depth = 1
        if not self.at_op(")"):
            args, depth = self.arguments(self.expr)
        else:
            self.advance()
        if not self.at_keyword("OVER"):
            raise Expr2Error(
                FLX_6103,
                f"{word} is a window function: write {word}(...) OVER (PARTITION BY ... "
                "ORDER BY ...)",
                position=self.peek().position,
            )
        self.advance()
        self.expect_op("(")
        self.expect_keyword("PARTITION")
        self.expect_keyword("BY")
        partition, bare = self.partition()
        self.expect_keyword("ORDER")
        self.expect_keyword("BY")
        keys = [self.sort_key()]
        while self.at_op(","):
            self.advance()
            keys.append(self.sort_key())
        self.expect_op(")")
        fewest, most = _WINDOW_ARITY[word]
        if not fewest <= len(args) <= most:
            wanted = str(fewest) if fewest == most else f"{fewest} to {most}"
            raise Expr2Error(
                FLX_6109,
                f"{word} takes {wanted} value arguments, not {len(args)}",
                position=position,
            )
        if word in ("LAG", "LEAD"):
            _check_offset_arguments(word, args)
        node = Window(word, tuple(args), partition, tuple(keys), bare, position=position)
        return node, depth

    def partition(self) -> Tuple[str, bool]:
        token = self.peek()
        if token.kind not in (TokenKind.NAME, TokenKind.QUOTED_NAME):
            raise Expr2Error(
                FLX_6106,
                f"PARTITION BY names a relationship of the row, found {token.describe()}",
                position=token.position,
            )
        if token.kind is TokenKind.NAME and token.text.upper() in CORE_KEYWORDS:
            raise _reserved(token)
        self.advance()
        if not self.at_op("."):
            return token.text, True
        if token.kind is TokenKind.NAME and token.text.lower() == "link":
            self.advance()
            name = self.name_after_dot()
            if not self.at_op("."):
                return name, False
        raise Expr2Error(
            FLX_6106,
            "PARTITION BY names a relationship of the row: write its name, or link.<name>",
            position=token.position,
        )

    def sort_key(self) -> SortKey:
        token = self.peek()
        if token.kind not in (TokenKind.NAME, TokenKind.QUOTED_NAME) or (
            token.kind is TokenKind.NAME and token.text.upper() in CORE_KEYWORDS
        ):
            raise Expr2Error(
                FLX_6103,
                f"an ORDER BY key is a column or a path, found {token.describe()}",
                position=token.position,
            )
        key = self.path()
        desc = False
        if self.at_keyword("ASC") or self.at_keyword("DESC"):
            desc = self.advance().text.upper() == "DESC"
        nulls_first = False
        if self.at_keyword("NULLS"):
            self.advance()
            if self.at_keyword("FIRST") or self.at_keyword("LAST"):
                nulls_first = self.advance().text.upper() == "FIRST"
            else:
                token = self.peek()
                raise Expr2Error(
                    FLX_6103,
                    f"NULLS is followed by FIRST or LAST, found {token.describe()}",
                    position=token.position,
                )
        return SortKey(key, desc, nulls_first)


def _start(node: Node) -> int:
    return int(getattr(node, "position", 0))


def _reserved(token: Token) -> Expr2Error:
    return Expr2Error(
        FLX_6104,
        f"{token.text.upper()} is a reserved word; to name a column or relationship "
        f'called {token.text}, quote it: "{token.text}"',
        position=token.position,
    )


def _check_offset_arguments(word: str, args: List[Node]) -> None:
    """LAG and LEAD: ``k`` is a positive whole number literal, ``d`` a literal or NULL."""
    if len(args) >= 2:
        offset = args[1]
        if not isinstance(offset, Number) or offset.scale != 0 or offset.value < 1:
            raise Expr2Error(
                FLX_6109,
                f"{word}'s offset is a positive whole number literal",
                position=_start(offset),
            )
    if len(args) == 3:
        default = args[2]
        literal = isinstance(default, (Number, String, Bool, Date, Null)) or (
            isinstance(default, Neg) and isinstance(default.operand, Number)
        )
        if not literal:
            raise Expr2Error(
                FLX_6109,
                f"{word}'s default is a literal or NULL",
                position=_start(default),
            )
