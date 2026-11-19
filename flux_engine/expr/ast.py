# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Frozen dataclass AST for ``flux-expr/1``, its canonical JSON, and ``unparse``.

EXPR-SPEC §4: "Hand-written tokenizer + Pratt parser -> frozen dataclass AST
... ``unparse(parse(x))`` is a fixed point and is the canonical form stored
in ``plan.json``. The AST JSON (canonical) is golden-pinned per expression."

Two consequences are load-bearing and tested as properties:

* every node is a frozen dataclass over tuples, so an AST is hashable,
  structurally comparable and cannot be mutated after the parse;
* :func:`unparse` prints one spelling per AST -- single spaces around binary
  operators, ``, `` between list items and arguments, parentheses only where
  a re-parse would otherwise regroup -- so ``unparse(parse(unparse(parse(x))))
  == unparse(parse(x))`` for every parseable ``x``.

Numbers live as :class:`decimal.Decimal` and are rendered with their declared
scale (``0.50`` stays ``0.50``), never as ``float``: KERNEL-SPEC §2.1 rejects
``float`` anywhere in a hashed object, and the canonical AST JSON is inside
``inputDigest`` by way of the attribute model and ``plan.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Tuple, Union

__all__ = [
    "And",
    "BoolLit",
    "Compare",
    "Expr",
    "In",
    "ListLit",
    "Literal",
    "Method",
    "Node",
    "Not",
    "NullLit",
    "NumberLit",
    "Operand",
    "Or",
    "Path",
    "StringLit",
    "to_json",
    "unparse",
]

#: Comparison operators of ``cmp_op`` (EXPR-SPEC §1). Closed.
COMPARISON_OPERATORS: Tuple[str, ...] = ("==", "!=", "<", "<=", ">", ">=")

#: The closed method set of EXPR-SPEC §1: ``has(string)`` on ``set<string>``;
#: ``isNull()`` on any path; ``startsWith`` / ``endsWith`` / ``contains``
#: (one string literal) on ``string``.
METHOD_NAMES: Tuple[str, ...] = ("has", "isNull", "startsWith", "endsWith", "contains")


@dataclass(frozen=True)
class Node:
    """Base of every AST node. Frozen, so an AST is immutable and hashable."""


# --- leaves -----------------------------------------------------------------


@dataclass(frozen=True)
class BoolLit(Node):
    """``true`` / ``false``."""

    value: bool


@dataclass(frozen=True)
class NumberLit(Node):
    """A numeric literal, held as ``Decimal`` -- never ``float`` (KERNEL-SPEC §2.1)."""

    value: Decimal


@dataclass(frozen=True)
class StringLit(Node):
    """A single-quoted string literal, NFC-normalised by the lexer."""

    value: str


@dataclass(frozen=True)
class NullLit(Node):
    """``null`` -- a value any path may carry, not a type (EXPR-SPEC §2)."""


@dataclass(frozen=True)
class Path(Node):
    """A dotted attribute path, e.g. ``traits.digitalFluency``."""

    parts: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValueError("a path has at least one part")

    @property
    def text(self) -> str:
        """The dotted spelling, as written."""
        return ".".join(self.parts)


@dataclass(frozen=True)
class ListLit(Node):
    """``[ literal , ... ]`` -- the right side of a list membership test."""

    items: Tuple["Literal", ...]


#: ``literal = string | number | bool_lit | null_lit`` (EXPR-SPEC §1).
Literal = Union[StringLit, NumberLit, BoolLit, NullLit]
#: ``operand = path | literal``.
Operand = Union[Path, StringLit, NumberLit, BoolLit, NullLit]


# --- composites -------------------------------------------------------------


@dataclass(frozen=True)
class Compare(Node):
    """``operand cmp_op operand``."""

    op: str
    left: Operand
    right: Operand

    def __post_init__(self) -> None:
        if self.op not in COMPARISON_OPERATORS:
            raise ValueError(f"{self.op!r} is not a flux-expr/1 comparison operator")


@dataclass(frozen=True)
class In(Node):
    """``operand in list_lit`` or ``operand in path`` (right side ``set<string>``)."""

    left: Operand
    right: Union[ListLit, Path]


@dataclass(frozen=True)
class Method(Node):
    """``path . method_name ( [literal, ...] )`` -- the closed method set."""

    target: Path
    name: str
    args: Tuple["Literal", ...] = ()

    def __post_init__(self) -> None:
        if self.name not in METHOD_NAMES:
            raise ValueError(f"{self.name!r} is not a flux-expr/1 method")


@dataclass(frozen=True)
class Not(Node):
    """``! primary``."""

    operand: "Expr"


@dataclass(frozen=True)
class And(Node):
    """``and_expr && not_expr``, left-associative."""

    left: "Expr"
    right: "Expr"


@dataclass(frozen=True)
class Or(Node):
    """``or_expr || and_expr``, left-associative."""

    left: "Expr"
    right: "Expr"


#: Anything the parser can return for a whole expression.
Expr = Union[Or, And, Not, Compare, In, Method, BoolLit]


# --- canonical JSON ---------------------------------------------------------


def to_json(node: Node) -> Dict[str, Any]:
    """Return the canonical JSON object for *node*.

    ``Decimal`` is serialised as a string carrying its declared scale, per
    KERNEL-SPEC §2.1 -- the document is hashed, and a JSON number would both
    reintroduce ``float`` and erase the scale. The result is golden-pinned
    per expression by ``tests/vectors/expr-vectors.json``.
    """
    if isinstance(node, BoolLit):
        return {"node": "bool", "value": node.value}
    if isinstance(node, NumberLit):
        return {"node": "number", "value": str(node.value)}
    if isinstance(node, StringLit):
        return {"node": "string", "value": node.value}
    if isinstance(node, NullLit):
        return {"node": "null"}
    if isinstance(node, Path):
        return {"node": "path", "parts": list(node.parts)}
    if isinstance(node, ListLit):
        return {"node": "list", "items": [to_json(item) for item in node.items]}
    if isinstance(node, Compare):
        return {
            "node": "compare",
            "op": node.op,
            "left": to_json(node.left),
            "right": to_json(node.right),
        }
    if isinstance(node, In):
        return {"node": "in", "left": to_json(node.left), "right": to_json(node.right)}
    if isinstance(node, Method):
        return {
            "node": "method",
            "target": to_json(node.target),
            "name": node.name,
            "args": [to_json(arg) for arg in node.args],
        }
    if isinstance(node, Not):
        return {"node": "not", "operand": to_json(node.operand)}
    if isinstance(node, And):
        return {"node": "and", "left": to_json(node.left), "right": to_json(node.right)}
    if isinstance(node, Or):
        return {"node": "or", "left": to_json(node.left), "right": to_json(node.right)}
    raise TypeError(f"not a flux-expr/1 AST node: {type(node).__name__}")


# --- canonical text ---------------------------------------------------------

#: Binding powers, loosest first. EXPR-SPEC §1: "Precedence, tightest first:
#: method/path, ``!``, comparison and ``in``, ``&&``, ``||``. All
#: left-associative."
_PREC_OR = 1
_PREC_AND = 2
_PREC_NOT = 3
_PREC_PRIMARY = 4


def _quote(value: str) -> str:
    """Render a string literal. ``'`` is the only escapable character.

    EXPR-SPEC §1 spells the production ``string = "'" , { char - "'" | "\\'" }
    , "'"``, which leaves a bare backslash ambiguous. Resolved here in the
    restrictive direction: a backslash is legal **only** as the two-character
    escape ``\\'`` (the lexer rejects any other backslash with ``FLX-6001``),
    so a string value never contains one and this printer is total and a
    fixed point.
    """
    return "'" + value.replace("'", "\\'") + "'"


def _render_literal(node: Node) -> str:
    if isinstance(node, StringLit):
        return _quote(node.value)
    if isinstance(node, NumberLit):
        return str(node.value)
    if isinstance(node, BoolLit):
        return "true" if node.value else "false"
    if isinstance(node, NullLit):
        return "null"
    if isinstance(node, Path):
        return node.text
    if isinstance(node, ListLit):
        return "[" + ", ".join(_render_literal(item) for item in node.items) + "]"
    raise TypeError(f"not an operand: {type(node).__name__}")


def _render(node: Node, min_prec: int) -> str:
    if isinstance(node, Or):
        text = f"{_render(node.left, _PREC_OR)} || {_render(node.right, _PREC_OR + 1)}"
        return f"({text})" if min_prec > _PREC_OR else text
    if isinstance(node, And):
        text = f"{_render(node.left, _PREC_AND)} && {_render(node.right, _PREC_AND + 1)}"
        return f"({text})" if min_prec > _PREC_AND else text
    if isinstance(node, Not):
        inner = node.operand
        # ``not_expr = [ "!" ] , primary``: the operand is always a primary, and
        # a primary that is not already atomic is printed parenthesised so the
        # printed form regroups the same way on a re-parse.
        atomic = isinstance(inner, (Method, BoolLit))
        rendered = _render(inner, _PREC_PRIMARY) if atomic else f"({_render(inner, _PREC_OR)})"
        text = f"!{rendered}"
        return f"({text})" if min_prec > _PREC_NOT else text
    if isinstance(node, Compare):
        text = f"{_render_literal(node.left)} {node.op} {_render_literal(node.right)}"
        return f"({text})" if min_prec > _PREC_PRIMARY else text
    if isinstance(node, In):
        text = f"{_render_literal(node.left)} in {_render_literal(node.right)}"
        return f"({text})" if min_prec > _PREC_PRIMARY else text
    if isinstance(node, Method):
        args = ", ".join(_render_literal(arg) for arg in node.args)
        return f"{node.target.text}.{node.name}({args})"
    return _render_literal(node)


def unparse(node: Node) -> str:
    """Return the canonical text of *node*.

    This is the form stored in ``plan.json`` (EXPR-SPEC §4). It is a fixed
    point: parsing the output yields an AST equal to *node*, whose own output
    is byte-identical.
    """
    return _render(node, _PREC_OR)
