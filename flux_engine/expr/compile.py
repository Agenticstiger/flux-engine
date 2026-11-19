# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Closure compiler for ``flux-expr/1`` (EXPR-SPEC §4).

    ``compile(expr, scope_model) -> Callable[[View], bool]`` (closure tree)

A type-checked AST compiles to a tree of closures over pre-resolved canonical
paths: sugar is resolved once, at compile time, so evaluation is a dictionary
lookup and a comparison per node with no name resolution and no branching on
the model.

**Runtime totality** (EXPR-SPEC §2) is the invariant this module owes the
rest of the engine: "a type-checked expression never raises. A ``null``
operand makes every comparison and ``in`` evaluate to ``false``, ``!= null``
to ``true``, and ``isNull()`` to ``true``. Methods on ``null`` return
``false``." A path missing from the view is null, so a view is never required
to be complete.

That sentence is ambiguous about ``x != null`` when ``x`` *is* null -- read
literally it would make ``null != null`` true while ``x.isNull()`` is also
true. Resolved here in the only self-consistent direction: the ``null``
literal makes the comparison a **null test**, so ``x == null`` is true when
``x`` is null, ``x != null`` is true when it is not, and ``x != null`` is
exactly ``!x.isNull()``. Every *other* comparison or ``in`` with a null
operand is false. The checker enforces the same reading by accepting the
``null`` literal only against a path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import AbstractSet, Any, Callable, Dict, FrozenSet, Mapping, Tuple, Union, cast

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
from flux_engine.expr.check import check
from flux_engine.expr.errors import TypeMismatchError, UnknownPathError
from flux_engine.expr.parser import parse
from flux_engine.expr.types import ScopeModel, Type, TypeKind, nfc

__all__ = ["Predicate", "View", "compile_expr", "compile_source", "make_view"]

#: One row presented to a compiled predicate: canonical path -> value.
#: ``number`` is ``Decimal``, ``string`` and ``enum`` are ``str``, ``bool`` is
#: ``bool``, ``set<string>`` is any set of ``str``, and a missing key or
#: ``None`` is null.
View = Mapping[str, Any]

#: What :func:`compile_expr` returns.
Predicate = Callable[[View], bool]

_Getter = Callable[[View], Any]


# --- views ------------------------------------------------------------------


def make_view(model: ScopeModel, values: Mapping[str, Any]) -> View:
    """Build a checked view: resolve sugar, NFC the strings, reject ``float``.

    This is a builder-side check, not part of evaluation: it runs once per row
    at most and raises ``FLX-6004`` for a path the model does not declare and
    ``FLX-6005`` for a value whose Python type does not match the declared
    one. ``float`` is refused outright -- KERNEL-SPEC §2.1 keeps ``float`` out
    of every hashed value, and a view feeds traced decisions. Errors name the
    path and the value's **class**, never the value.
    """
    built: Dict[str, Any] = {}
    for name in sorted(values, key=nfc):
        canonical = model.resolve(name)
        if canonical is None:
            raise UnknownPathError(f"'{name}' is not an attribute of the {model.scope.value} scope")
        built[canonical] = _coerce(canonical, model.attributes[canonical], values[name])
    return built


def _coerce(path: str, declared: Type, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        raise TypeMismatchError(f"'{path}': float is not a flux-expr/1 value; use Decimal")
    if declared.kind is TypeKind.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise TypeMismatchError(f"'{path}': number expected, got {type(value).__name__}")
        return value if isinstance(value, Decimal) else Decimal(value)
    if declared.kind is TypeKind.BOOL:
        if not isinstance(value, bool):
            raise TypeMismatchError(f"'{path}': bool expected, got {type(value).__name__}")
        return value
    if declared.kind in (TypeKind.STRING, TypeKind.ENUM):
        if not isinstance(value, str):
            raise TypeMismatchError(
                f"'{path}': {declared.name} expected, got {type(value).__name__}"
            )
        return nfc(value)
    if not isinstance(value, (set, frozenset)):
        raise TypeMismatchError(f"'{path}': set<string> expected, got {type(value).__name__}")
    members: AbstractSet[Any] = value
    for member in members:
        if not isinstance(member, str):
            raise TypeMismatchError(
                f"'{path}': set<string> member expected, got {type(member).__name__}"
            )
    return frozenset(nfc(member) for member in cast(AbstractSet[str], members))


# --- operand getters --------------------------------------------------------


def _getter(operand: Operand, model: ScopeModel) -> _Getter:
    """Compile an operand into a value getter over the view."""
    if isinstance(operand, Path):
        canonical = model.resolve(operand.text)
        if canonical is None:  # pragma: no cover - the checker ran first
            raise UnknownPathError(f"'{operand.text}' is not an attribute of this scope")
        return lambda view: view.get(canonical)
    if isinstance(operand, StringLit):
        literal_str = nfc(operand.value)
        return lambda view: literal_str
    if isinstance(operand, NumberLit):
        literal_num = operand.value
        return lambda view: literal_num
    if isinstance(operand, BoolLit):
        literal_bool = operand.value
        return lambda view: literal_bool
    # ``NullLit`` never reaches here: the checker accepts the null literal only
    # opposite a path, and _compile_compare turns that into a null test before
    # compiling either side. The guard stays so a future operand kind fails
    # loudly instead of compiling to something silently wrong.
    raise TypeMismatchError(  # pragma: no cover - unreachable after check()
        f"not a value operand: {type(operand).__name__}"
    )


def _literal_value(literal: Node) -> Any:
    """The constant a literal denotes, with strings NFC-normalised."""
    if isinstance(literal, StringLit):
        return nfc(literal.value)
    if isinstance(literal, NumberLit):
        return literal.value
    if isinstance(literal, BoolLit):
        return literal.value
    if isinstance(literal, NullLit):  # pragma: no cover - the checker rejects it here
        return None
    raise TypeMismatchError(  # pragma: no cover - unreachable after check()
        f"not a literal: {type(literal).__name__}"
    )


# --- comparison -------------------------------------------------------------

#: Total, null-free comparisons; the wrappers below own the null rules.
_OPERATIONS: Dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: bool(a == b),
    "!=": lambda a, b: bool(a != b),
    "<": lambda a, b: bool(a < b),
    "<=": lambda a, b: bool(a <= b),
    ">": lambda a, b: bool(a > b),
    ">=": lambda a, b: bool(a >= b),
}


def _compile_compare(node: Compare, model: ScopeModel) -> Predicate:
    if isinstance(node.left, NullLit) or isinstance(node.right, NullLit):
        path = node.right if isinstance(node.left, NullLit) else node.left
        assert isinstance(path, Path)  # the checker requires a path opposite null
        get = _getter(path, model)
        if node.op == "==":
            return lambda view: get(view) is None
        return lambda view: get(view) is not None

    left = _getter(node.left, model)
    right = _getter(node.right, model)
    operation = _OPERATIONS[node.op]

    def run(view: View) -> bool:
        a = left(view)
        b = right(view)
        if a is None or b is None:
            return False
        return operation(a, b)

    return run


def _compile_in(node: In, model: ScopeModel) -> Predicate:
    left = _getter(node.left, model)

    if isinstance(node.right, Path):
        right = _getter(node.right, model)

        def run_path(view: View) -> bool:
            value = left(view)
            container = right(view)
            if value is None or container is None:
                return False
            return value in cast(FrozenSet[str], container)

        return run_path

    assert isinstance(node.right, ListLit)
    members: Tuple[Any, ...] = tuple(_literal_value(item) for item in node.right.items)

    def run_list(view: View) -> bool:
        value = left(view)
        if value is None:
            return False
        return any(value == member for member in members)

    return run_list


def _starts_with(haystack: str, needle: str) -> bool:
    return haystack.startswith(needle)


def _ends_with(haystack: str, needle: str) -> bool:
    return haystack.endswith(needle)


def _contains(haystack: str, needle: str) -> bool:
    return needle in haystack


def _compile_method(node: Method, model: ScopeModel) -> Predicate:
    canonical = model.resolve(node.target.text)
    if canonical is None:  # pragma: no cover - the checker ran first
        raise UnknownPathError(f"'{node.target.text}' is not an attribute of this scope")

    if node.name == "isNull":
        return lambda view: view.get(canonical) is None

    argument = nfc(cast(StringLit, node.args[0]).value)

    if node.name == "has":

        def run_has(view: View) -> bool:
            container = view.get(canonical)
            if container is None:
                return False
            return argument in cast(FrozenSet[str], container)

        return run_has

    operation: Callable[[str, str], bool]
    if node.name == "startsWith":
        operation = _starts_with
    elif node.name == "endsWith":
        operation = _ends_with
    else:
        operation = _contains

    def run_string(view: View) -> bool:
        value = view.get(canonical)
        if value is None:
            return False
        return operation(cast(str, value), argument)

    return run_string


# --- the compiler -----------------------------------------------------------


def _compile_node(node: Union[Expr, Node], model: ScopeModel) -> Predicate:
    if isinstance(node, Or):
        left = _compile_node(node.left, model)
        right = _compile_node(node.right, model)
        return lambda view: left(view) or right(view)
    if isinstance(node, And):
        left = _compile_node(node.left, model)
        right = _compile_node(node.right, model)
        return lambda view: left(view) and right(view)
    if isinstance(node, Not):
        inner = _compile_node(node.operand, model)
        return lambda view: not inner(view)
    if isinstance(node, Compare):
        return _compile_compare(node, model)
    if isinstance(node, In):
        return _compile_in(node, model)
    if isinstance(node, Method):
        return _compile_method(node, model)
    if isinstance(node, BoolLit):
        constant = node.value
        return lambda view: constant
    raise TypeMismatchError(  # pragma: no cover - check() rejects this first
        f"{type(node).__name__} is not a predicate"
    )


def compile_expr(node: Expr, model: ScopeModel) -> Predicate:
    """Type-check *node* against *model* and return the compiled closure tree.

    This is the ``compile(expr, scope_model)`` of EXPR-SPEC §4; it is spelled
    ``compile_expr`` so it does not shadow the builtin at a call site. The
    check is not optional: compiling an unchecked AST would put the totality
    guarantee at the mercy of the caller.
    """
    check(node, model)
    return _compile_node(node, model)


def compile_source(source: str, model: ScopeModel) -> Predicate:
    """Parse, type-check and compile *source* in one call."""
    return compile_expr(parse(source), model)
