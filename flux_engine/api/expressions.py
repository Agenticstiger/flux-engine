# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The expression seam: ``flux-expr/1`` compilation, as a port.

EXPR-SPEC section 4 fixes the shape: ``compile(expr, scope_model) ->
Callable[[View], bool]``, and ``unparse(parse(x))`` is a fixed point whose
output is the canonical form stored in ``plan.json``. This module carries
that shape and nothing else -- the grammar is closed (EXPR-SPEC section 1),
so this is a port for *evaluating* the one language, never an extension
point for adding to it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

__all__ = ["ExpressionEvaluator", "ExpressionScope"]


class ExpressionScope(str, Enum):
    """The three attribute scopes of EXPR-SPEC section 3.

    An expression is type-checked against exactly one scope's model; the
    scope decides which paths exist and what type each carries.
    """

    SUBJECT = "subject"
    EVENT = "event"
    OBSERVED = "observed"


@runtime_checkable
class ExpressionEvaluator(Protocol):
    """Compiles a ``flux-expr/1`` expression against an attribute model.

    **Reserved** (:data:`flux_engine.api.RESERVED_PORTS`): this build ships
    no implementation of this shape and no parameter to inject one. The
    compiler it actually uses is ``flux_engine.expr.compile``'s
    module-level ``compile_source(source, model)``. Design against this
    port; do not expect the engine to call one yet.

    Implementations are total in the sense EXPR-SPEC section 2 requires: a
    compiled expression never raises at evaluation time. Every way an
    expression can be wrong -- unknown path, type mismatch, a literal
    outside a declared enum -- is reported by :meth:`compile`, at plan
    time, as an ``FLX-6xxx`` failure.
    """

    def compile(
        self,
        expression: str,
        scope: ExpressionScope,
        scope_model: Mapping[str, Any],
    ) -> Callable[[Mapping[str, Any]], bool]:
        """Type-check *expression* against *scope_model* and return a predicate.

        *scope_model* is the ``attrmodel/1`` fragment for *scope* (EXPR-SPEC
        section 3). The returned predicate takes one view of an entity and
        answers true or false; it never raises and never mutates the view.
        """

    def unparse(self, expression: str) -> str:
        """Return the canonical form of *expression*.

        EXPR-SPEC section 4: this is a fixed point -- feeding the result
        back in returns the same string -- and it is the form stored in
        ``plan.json``, so two spellings of one predicate reach the same
        digest.
        """
