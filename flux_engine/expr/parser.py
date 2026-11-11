# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Pratt parser for ``flux-expr/1`` (EXPR-SPEC §1, §4).

The EBNF is reproduced here because the code is written against it clause by
clause::

    expr        = or_expr ;
    or_expr     = and_expr , { "||" , and_expr } ;
    and_expr    = not_expr , { "&&" , not_expr } ;
    not_expr    = [ "!" ] , primary ;
    primary     = "(" , expr , ")" | comparison | membership | method | bool_lit ;
    comparison  = operand , cmp_op , operand ;
    membership  = operand , "in" , list_lit | operand , "in" , path ;
    method      = path , "." , method_name , "(" , [ literal , { "," , literal } ] , ")" ;
    operand     = path | literal ;

The binary operators ``||`` and ``&&`` are driven by the binding-power table
:data:`_BINDING_POWER` in one loop (the Pratt shape EXPR-SPEC §4 names);
``!`` is the single prefix operator; everything tighter is a *primary*, which
this module parses with one token of lookahead.

Two consequences of taking the EBNF literally are worth stating, because both
are commonly assumed otherwise:

* **A bare path is not an expression.** ``primary`` offers ``bool_lit`` but
  not ``operand``, so a boolean attribute is written ``flag == true``, and
  ``products`` on its own is ``FLX-6001``.
* **``!`` applies to a primary, not to an operand.** ``!a > 1`` therefore
  derives as ``!(a > 1)``: ``not_expr = [ "!" ] , primary`` and ``primary``
  includes ``comparison``. The precedence line in EXPR-SPEC §1 lists ``!`` as
  tighter than comparison, but the grammar gives ``!`` nothing tighter to
  apply to, so the ordering is unobservable and the EBNF decides. ``!!x`` is
  not derivable and is ``FLX-6001``.
* **A method is not an operand.** ``a == b.has('x')`` is ``FLX-6001``; the
  method production stands alone as a primary.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Sequence, Tuple, Union

from flux_engine.expr.ast import (
    METHOD_NAMES,
    And,
    BoolLit,
    Compare,
    Expr,
    In,
    ListLit,
    Literal,
    Method,
    Not,
    NullLit,
    NumberLit,
    Operand,
    Or,
    Path,
    StringLit,
)
from flux_engine.expr.errors import ParseError
from flux_engine.expr.lexer import COMPARISON_KINDS, Token, TokenKind, tokenize

__all__ = ["parse", "parse_tokens"]

#: Left binding power of the two infix boolean operators. ``&&`` binds
#: tighter than ``||``; both are left-associative, which the parse loop gets
#: by recursing at ``bp + 1``.
_BINDING_POWER: Dict[TokenKind, int] = {
    TokenKind.OR: 1,
    TokenKind.AND: 2,
}

#: Token kinds that can begin an ``operand``.
_OPERAND_STARTS = (
    TokenKind.IDENT,
    TokenKind.STRING,
    TokenKind.NUMBER,
    TokenKind.TRUE,
    TokenKind.FALSE,
    TokenKind.NULL,
)


class _Parser:
    """One parse. Holds the token stream and the cursor; never backtracks."""

    def __init__(self, tokens: Sequence[Token]) -> None:
        self._tokens = tokens
        self._index = 0

    # --- cursor -----------------------------------------------------------

    def at_end(self) -> bool:
        """True when the whole token stream has been consumed."""
        return self._peek().kind is TokenKind.EOF

    def trailing(self) -> Token:
        """The token the parse stopped at (``EOF`` on a complete parse)."""
        return self._peek()

    def _peek(self, offset: int = 0) -> Token:
        index = min(self._index + offset, len(self._tokens) - 1)
        return self._tokens[index]

    def _advance(self) -> Token:
        token = self._tokens[self._index]
        if token.kind is not TokenKind.EOF:
            self._index += 1
        return token

    def _expect(self, kind: TokenKind, what: str) -> Token:
        token = self._peek()
        if token.kind is not kind:
            raise ParseError(f"expected {what}, found {token}", position=token.position)
        return self._advance()

    # --- expr / or_expr / and_expr ----------------------------------------

    def parse_expr(self, min_bp: int = 1) -> Expr:
        """``expr = or_expr`` -- the Pratt loop over ``||`` and ``&&``."""
        node: Expr = self.parse_not()
        while True:
            token = self._peek()
            bp = _BINDING_POWER.get(token.kind)
            if bp is None or bp < min_bp:
                return node
            self._advance()
            right = self.parse_expr(bp + 1)
            node = Or(node, right) if token.kind is TokenKind.OR else And(node, right)

    # --- not_expr ---------------------------------------------------------

    def parse_not(self) -> Expr:
        """``not_expr = [ "!" ] , primary``. One ``!`` only; ``!!`` is FLX-6001."""
        if self._peek().kind is TokenKind.BANG:
            self._advance()
            if self._peek().kind is TokenKind.BANG:
                raise ParseError(
                    "'!' applies to one primary; '!!' is not derivable",
                    position=self._peek().position,
                )
            return Not(self.parse_primary())
        return self.parse_primary()

    # --- primary ----------------------------------------------------------

    def parse_primary(self) -> Expr:
        """``primary = "(" expr ")" | comparison | membership | method | bool_lit``."""
        token = self._peek()
        if token.kind is TokenKind.LPAREN:
            self._advance()
            inner = self.parse_expr()
            self._expect(TokenKind.RPAREN, "')'")
            return inner
        if token.kind not in _OPERAND_STARTS:
            raise ParseError(f"expected a predicate, found {token}", position=token.position)

        left = self._parse_operand_or_method(allow_method=True)
        after = self._peek()

        if after.kind in COMPARISON_KINDS:
            if isinstance(left, Method):
                raise ParseError(
                    "a method call is not an operand of a comparison",
                    position=after.position,
                )
            self._advance()
            right = self._parse_operand_or_method(allow_method=False)
            assert not isinstance(right, Method)  # allow_method=False
            return Compare(after.text, left, right)

        if after.kind is TokenKind.IN:
            if isinstance(left, Method):
                raise ParseError(
                    "a method call is not an operand of 'in'",
                    position=after.position,
                )
            self._advance()
            return In(left, self._parse_membership_right())

        if isinstance(left, Method):
            return left
        if isinstance(left, BoolLit):
            return left
        raise ParseError(
            "a bare operand is not a predicate; primary is a comparison, a "
            "membership test, a method call, a boolean literal or a "
            "parenthesised expression",
            position=after.position,
        )

    # --- operand / path / method ------------------------------------------

    def _parse_operand_or_method(self, *, allow_method: bool) -> Union[Operand, Method]:
        """``operand = path | literal``, or -- when *allow_method* -- a ``method``."""
        token = self._peek()
        if token.kind is TokenKind.IDENT:
            return self._parse_path_or_method(allow_method=allow_method)
        return self._parse_literal()

    def _parse_literal(self) -> Literal:
        """``literal = string | number | bool_lit | null_lit``."""
        token = self._peek()
        if token.kind is TokenKind.STRING:
            self._advance()
            return StringLit(str(token.value))
        if token.kind is TokenKind.NUMBER:
            self._advance()
            value = token.value
            assert isinstance(value, Decimal)  # the lexer decodes NUMBER to Decimal
            return NumberLit(value)
        if token.kind in (TokenKind.TRUE, TokenKind.FALSE):
            self._advance()
            return BoolLit(token.kind is TokenKind.TRUE)
        if token.kind is TokenKind.NULL:
            self._advance()
            return NullLit()
        raise ParseError(f"expected a literal, found {token}", position=token.position)

    def _parse_path_or_method(self, *, allow_method: bool) -> Union[Path, Method]:
        """Read ``path`` and, on the method lookahead, the whole ``method``.

        ``path`` and ``method`` share their prefix, so the dotted segments are
        consumed greedily until a segment is both a declared ``method_name``
        and immediately followed by ``(`` -- the one-token lookahead that
        separates ``products.has`` (a path) from ``products.has('tv')`` (a
        method call).
        """
        parts: List[str] = [str(self._expect(TokenKind.IDENT, "an identifier").value)]
        while self._peek().kind is TokenKind.DOT:
            segment = self._peek(1)
            if segment.kind is not TokenKind.IDENT:
                raise ParseError(
                    f"expected an identifier after '.', found {segment}",
                    position=segment.position,
                )
            is_call = segment.text in METHOD_NAMES and self._peek(2).kind is TokenKind.LPAREN
            if is_call:
                if not allow_method:
                    raise ParseError(
                        "a method call is not an operand here",
                        position=segment.position,
                    )
                self._advance()  # '.'
                self._advance()  # method_name
                args = self._parse_arguments()
                return Method(Path(tuple(parts)), segment.text, args)
            self._advance()  # '.'
            self._advance()  # segment
            parts.append(str(segment.value))
        return Path(tuple(parts))

    def _parse_arguments(self) -> Tuple[Literal, ...]:
        """``"(" , [ literal , { "," , literal } ] , ")"`` -- literals only."""
        self._expect(TokenKind.LPAREN, "'('")
        args: List[Literal] = []
        if self._peek().kind is not TokenKind.RPAREN:
            args.append(self._parse_literal())
            while self._peek().kind is TokenKind.COMMA:
                self._advance()
                args.append(self._parse_literal())
        self._expect(TokenKind.RPAREN, "')'")
        return tuple(args)

    def _parse_membership_right(self) -> Union[ListLit, Path]:
        """The right side of ``in``: a list literal or a path (``set<string>``)."""
        token = self._peek()
        if token.kind is TokenKind.LBRACKET:
            self._advance()
            items: List[Literal] = []
            if self._peek().kind is not TokenKind.RBRACKET:
                items.append(self._parse_literal())
                while self._peek().kind is TokenKind.COMMA:
                    self._advance()
                    items.append(self._parse_literal())
            self._expect(TokenKind.RBRACKET, "']'")
            return ListLit(tuple(items))
        if token.kind is TokenKind.IDENT:
            right = self._parse_path_or_method(allow_method=False)
            assert isinstance(right, Path)  # allow_method=False
            return right
        raise ParseError(
            f"the right side of 'in' is a list literal or a path, found {token}",
            position=token.position,
        )


def parse_tokens(tokens: Sequence[Token]) -> Expr:
    """Parse an already-tokenized expression; the stream must end at ``EOF``."""
    parser = _Parser(tokens)
    node = parser.parse_expr()
    if not parser.at_end():
        rest = parser.trailing()
        raise ParseError(f"unexpected {rest} after the expression", position=rest.position)
    return node


def parse(source: str) -> Expr:
    """Tokenize and parse *source* into a frozen AST.

    Raises ``FLX-6001`` for anything the closed grammar cannot derive and
    ``FLX-6002`` for a word reserved for ``flux-expr/2``. No type checking
    happens here -- that is :func:`flux_engine.expr.check.check`, which needs
    the attribute model.
    """
    tokens = tokenize(source)
    if len(tokens) == 1:
        raise ParseError("an expression is not empty", position=0)
    return parse_tokens(tokens)
