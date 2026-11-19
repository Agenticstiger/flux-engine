# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``flux-expr/1`` -- the frozen, closed expression grammar (diverges from CEL).

``Segment.spec.queryLanguage`` is ``const: flux-expr/1`` in FLUX 0.5.0, and
this package is its whole implementation: tokenizer, Pratt parser, frozen
dataclass AST, canonical ``unparse``, static type checker, closure compiler
and the ``attrmodel/1`` attribute-model builder (EXPR-SPEC §1-§5;
ADR-0002 records why the language is FLUX's own rather than CEL's).

The plan-time pipeline is four calls::

    from flux_engine.expr import build_attribute_model, compile_expr, parse, unparse
    from flux_engine.verticals import get_pack

    model = build_attribute_model(bundle, get_pack("telco"))
    node = parse(segment["spec"]["query"])       # FLX-6001 / FLX-6002
    canonical = unparse(node)                    # what plan.json stores
    predicate = compile_expr(node, model.subject)  # FLX-6003 / FLX-6004 / FLX-6005

``predicate(view)`` is then total: EXPR-SPEC §2 guarantees a type-checked
expression never raises, whatever nulls the view carries.

The grammar is closed. Anything not derivable is ``FLX-6001``, never an
extension point; growth requests go to ``flux-expr/2`` by policy.
"""

from __future__ import annotations

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
    Or,
    Path,
    StringLit,
    to_json,
    unparse,
)
from flux_engine.expr.attrs import (
    ATTR_MODEL_VERSION,
    AttributeModel,
    BundleLike,
    VerticalPack,
    build_attribute_model,
)
from flux_engine.expr.check import check
from flux_engine.expr.compile import Predicate, View, compile_expr, compile_source, make_view
from flux_engine.expr.errors import (
    CATALOG,
    EnumLiteralError,
    FluxExprError,
    ParseError,
    ReservedWordError,
    TypeMismatchError,
    UnknownPathError,
)
from flux_engine.expr.lexer import tokenize
from flux_engine.expr.parser import parse
from flux_engine.expr.types import (
    BOOL,
    NUMBER,
    SET_STRING,
    STRING,
    Scope,
    ScopeModel,
    Type,
    TypeKind,
    enum_of,
)

#: The grammar version this package implements. Frozen: a change is
#: ``flux-expr/2``, not a patch (EXPR-SPEC preamble).
EXPR_LANGUAGE = "flux-expr/1"

__all__ = [
    "ATTR_MODEL_VERSION",
    "BOOL",
    "CATALOG",
    "EXPR_LANGUAGE",
    "NUMBER",
    "SET_STRING",
    "STRING",
    "And",
    "AttributeModel",
    "BoolLit",
    "BundleLike",
    "Compare",
    "EnumLiteralError",
    "Expr",
    "FluxExprError",
    "In",
    "ListLit",
    "Method",
    "Node",
    "Not",
    "NullLit",
    "NumberLit",
    "Or",
    "ParseError",
    "Path",
    "Predicate",
    "ReservedWordError",
    "Scope",
    "ScopeModel",
    "StringLit",
    "Type",
    "TypeKind",
    "TypeMismatchError",
    "UnknownPathError",
    "VerticalPack",
    "View",
    "build_attribute_model",
    "check",
    "compile_expr",
    "compile_source",
    "enum_of",
    "make_view",
    "parse",
    "to_json",
    "tokenize",
    "unparse",
]
