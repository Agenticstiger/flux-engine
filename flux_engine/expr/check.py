# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Static type checker for ``flux-expr/1`` against a scope model (EXPR-SPEC §2).

EXPR-SPEC §2 is the whole contract of this module:

    Static type check at plan time against the attribute model: unknown path
    ``FLX-6004``; type mismatch (``market > 3``,
    ``traits.digitalFluency.has('x')``) ``FLX-6005``; comparison between
    different types is an error (no coercion; ``'1' == 1`` is ``FLX-6005``).

Everything the checker accepts, :mod:`flux_engine.expr.compile` guarantees to
evaluate without raising ("Runtime totality: a type-checked expression never
raises"), so the rules below are exactly the ones the closures are allowed to
assume.

Ambiguities in EXPR-SPEC resolved here, each in the restrictive direction the
closed-grammar policy calls for:

* **Ordering is numeric only.** ``<``, ``<=``, ``>``, ``>=`` accept
  ``number`` on both sides and nothing else. The spec names ``market > 3``
  (an enum against a number) as the ``FLX-6005`` example and never defines an
  order over strings, enums or RFC 3339 ``time``; string ordering is a
  flux-expr/2 request.
* **``enum`` compares as string, but the string methods stay on ``string``.**
  EXPR-SPEC §2 says an enum "compares as string", which this checker honours
  for ``==`` / ``!=`` / ``in``; §1 declares ``startsWith`` / ``endsWith`` /
  ``contains`` "on ``string``", so an enum target is ``FLX-6005``.
* **``null`` is a null test, not a value comparison.** EXPR-SPEC §2 makes
  ``null`` "a value any path may carry, not a type", and §2's runtime rules
  give ``!= null`` a truth value of its own. The ``null`` literal is
  therefore accepted only with ``==`` / ``!=`` and only against a *path*:
  ``x == null`` means "x is null", ``x != null`` means "x is not null", and
  ``null`` in any other position is ``FLX-6005``.
"""

from __future__ import annotations

from typing import Optional, Union

from flux_engine.expr.ast import (
    And,
    BoolLit,
    Compare,
    Expr,
    In,
    ListLit,
    Method,
    Node,
    Not,
    NullLit,
    NumberLit,
    Operand,
    Or,
    Path,
    StringLit,
)
from flux_engine.expr.errors import EnumLiteralError, TypeMismatchError, UnknownPathError
from flux_engine.expr.types import BOOL, NUMBER, SET_STRING, STRING, ScopeModel, Type, TypeKind

__all__ = ["check", "type_of_path", "operand_type"]

#: The four ordering operators. ``==`` / ``!=`` are handled separately
#: because they admit every type, ordering admits only ``number``.
_ORDERING = ("<", "<=", ">", ">=")

#: Methods declared on ``string`` (EXPR-SPEC §1), each taking one string
#: literal.
_STRING_METHODS = ("startsWith", "endsWith", "contains")


def type_of_path(path: Path, model: ScopeModel) -> Type:
    """Return the declared type of *path*, resolving sugar; raise ``FLX-6004``."""
    declared = model.type_of(path.text)
    if declared is None:
        raise UnknownPathError(
            f"'{path.text}' is not an attribute of the {model.scope.value} scope"
        )
    return declared


def operand_type(operand: Operand, model: ScopeModel) -> Optional[Type]:
    """Return the type of an ``operand``; ``None`` for the ``null`` literal.

    ``null`` has no type ("a value any path may carry, not a type"), so it is
    the one operand that returns ``None`` and is handled by the caller.
    """
    if isinstance(operand, Path):
        return type_of_path(operand, model)
    if isinstance(operand, StringLit):
        return STRING
    if isinstance(operand, NumberLit):
        return NUMBER
    if isinstance(operand, BoolLit):
        return BOOL
    if isinstance(operand, NullLit):
        return None
    raise TypeMismatchError(f"not an operand: {type(operand).__name__}")


def _describe(operand: Operand, model: ScopeModel) -> str:
    declared = operand_type(operand, model)
    return "null" if declared is None else declared.name


def _check_enum_literal(declared: Type, literal: StringLit) -> None:
    """A literal compared against an enum must be one of its levels (FLX-6003)."""
    if literal.value not in declared.levels:
        raise EnumLiteralError(
            f"'{literal.value}' is not a level of {declared.name}; "
            f"declared levels are {', '.join(declared.levels)}"
        )


def _check_equality(node: Compare, model: ScopeModel) -> None:
    """``==`` / ``!=``: same type, no coercion, with the enum-as-string rule."""
    left, right = node.left, node.right

    if isinstance(left, NullLit) or isinstance(right, NullLit):
        other = right if isinstance(left, NullLit) else left
        if isinstance(other, NullLit) or not isinstance(other, Path):
            raise TypeMismatchError(
                "the null literal tests a path for absence; write `<path> == null`"
            )
        type_of_path(other, model)  # FLX-6004 for an unknown path
        return

    left_type = operand_type(left, model)
    right_type = operand_type(right, model)
    assert left_type is not None and right_type is not None  # NullLit handled above

    for declared in (left_type, right_type):
        if declared.kind is TypeKind.SET_STRING:
            raise TypeMismatchError(
                "set<string> has no equality in flux-expr/1; use `in` or `has()`"
            )

    if left_type.kind is TypeKind.ENUM or right_type.kind is TypeKind.ENUM:
        # "enum<...> (a string drawn from a declared closed set; compares as
        # string; a literal outside the set is FLX-6003 at plan time)".
        for declared, operand in ((left_type, right), (right_type, left)):
            if declared.kind is TypeKind.ENUM and isinstance(operand, StringLit):
                _check_enum_literal(declared, operand)
        if {left_type.kind, right_type.kind} <= {TypeKind.ENUM, TypeKind.STRING}:
            return
        raise TypeMismatchError(
            f"cannot compare {left_type.name} with {right_type.name}; there is no coercion"
        )

    if left_type != right_type:
        raise TypeMismatchError(
            f"cannot compare {left_type.name} with {right_type.name}; there is no coercion"
        )


def _check_ordering(node: Compare, model: ScopeModel) -> None:
    """``<`` ``<=`` ``>`` ``>=``: ``number`` on both sides, nothing else."""
    for operand in (node.left, node.right):
        declared = operand_type(operand, model)
        if declared != NUMBER:
            raise TypeMismatchError(
                f"'{node.op}' orders numbers; found {_describe(operand, model)}"
            )


def _check_membership(node: In, model: ScopeModel) -> None:
    """``operand in list_lit`` / ``operand in path`` (right side ``set<string>``)."""
    left_type = operand_type(node.left, model)
    if left_type is None:
        raise TypeMismatchError("the null literal is not a member test operand")

    if isinstance(node.right, Path):
        right_type = type_of_path(node.right, model)
        if right_type != SET_STRING:
            raise TypeMismatchError(
                f"the right side of 'in' must be set<string>; '{node.right.text}' is "
                f"{right_type.name}"
            )
        if left_type.kind not in (TypeKind.STRING, TypeKind.ENUM):
            raise TypeMismatchError(
                f"only a string or enum can be a member of set<string>; found {left_type.name}"
            )
        return

    assert isinstance(node.right, ListLit)
    for item in node.right.items:
        item_type = operand_type(item, model)
        if item_type is None:
            raise TypeMismatchError("a list literal does not carry null")
        if left_type.kind is TypeKind.ENUM:
            if not isinstance(item, StringLit):
                raise TypeMismatchError(
                    f"cannot test {left_type.name} against a {item_type.name} literal"
                )
            _check_enum_literal(left_type, item)
            continue
        if item_type != left_type:
            raise TypeMismatchError(
                f"list literal mixes {item_type.name} with {left_type.name}; there is no coercion"
            )


def _check_method(node: Method, model: ScopeModel) -> None:
    """The closed method set of EXPR-SPEC §1, each on its declared type."""
    target_type = type_of_path(node.target, model)

    if node.name == "isNull":
        # "isNull() on any path" -- the one method with no receiver type
        # constraint, because every path may carry null.
        if node.args:
            raise TypeMismatchError("isNull() takes no argument")
        return

    if len(node.args) != 1 or not isinstance(node.args[0], StringLit):
        raise TypeMismatchError(f"{node.name}() takes exactly one string literal")

    if node.name == "has":
        if target_type != SET_STRING:
            raise TypeMismatchError(
                f"has() is declared on set<string>; '{node.target.text}' is {target_type.name}"
            )
        return

    assert node.name in _STRING_METHODS
    if target_type != STRING:
        raise TypeMismatchError(
            f"{node.name}() is declared on string; '{node.target.text}' is {target_type.name}"
        )


def _check_expr(node: Union[Expr, Node], model: ScopeModel) -> Type:
    """Type an expression node.

    The return is always ``BOOL`` or an exception: the grammar gives ``&&``,
    ``||`` and ``!`` nothing but predicates to join, since ``primary`` offers
    no bare operand, and every other expression node is a predicate by
    construction. A node that is not an expression at all -- an operand handed
    in by a caller who built the AST itself -- falls through to the
    ``FLX-6005`` at the end.
    """
    if isinstance(node, (And, Or)):
        _check_expr(node.left, model)
        _check_expr(node.right, model)
        return BOOL
    if isinstance(node, Not):
        _check_expr(node.operand, model)
        return BOOL
    if isinstance(node, Compare):
        if node.op in _ORDERING:
            _check_ordering(node, model)
        else:
            _check_equality(node, model)
        return BOOL
    if isinstance(node, In):
        _check_membership(node, model)
        return BOOL
    if isinstance(node, Method):
        _check_method(node, model)
        return BOOL
    if isinstance(node, BoolLit):
        return BOOL
    raise TypeMismatchError(f"{type(node).__name__} is not a predicate")


def check(node: Expr, model: ScopeModel) -> Type:
    """Type-check *node* against *model*; return its type, which is always ``bool``.

    Every expression node of the grammar is a predicate -- ``primary`` offers
    no bare operand -- so the returned type is ``bool`` or the call raised:
    ``FLX-6003`` for a literal outside an enum's declared levels, ``FLX-6004``
    for a path the scope does not declare, ``FLX-6005`` for any type mismatch
    (an operand node passed in directly included). A node that survives this
    call can be compiled into a total closure (EXPR-SPEC §2, "Runtime
    totality").
    """
    return _check_expr(node, model)
