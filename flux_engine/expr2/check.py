# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Check a parsed flux-expr/2 text against its row scope, and build its evaluator.

One pass over the tree does three things, the way flux-expr/1's ``check``
and ``compile`` do in two:

* **resolves names** against the :class:`~flux_engine.expr2.scope.Model`
  (``FLX-6105``, ``FLX-6106``, ``FLX-6111``, ``FLX-6113``) and places
  aggregates and windows (``FLX-6107``, ``FLX-6108``);
* **types** every node with RFC-data-rules.md §2.3's six kinds and §2.4's
  static scale (``FLX-6109``, ``FLX-6110``, ``FLX-6104`` for a NULL literal
  that nothing beside it types, and ``FLX-6206`` for a predicate position);
* **builds** a closure per node that evaluates it on a
  :class:`~flux_engine.expr2.evaluate.RowView`, exactly and totally: given
  rows of the declared types it returns a value or ``None`` and never
  raises.

Truth is two-valued (RFC §2.5): a comparison, ``IN``, ``BETWEEN`` and their
negations are ``FALSE`` when an operand is NULL, a NULL bool counts as
``FALSE`` under ``NOT``, ``AND``, ``OR``, ``WHEN`` and a predicate position,
and ``IS [NOT] NULL`` and ``IS [NOT] DISTINCT FROM`` treat NULL as a value.
Arithmetic follows SQL: a NULL operand gives NULL, and ``x / 0`` is NULL.

The check also reports what the text reads (EXPR-SPEC-2 §10): each column,
the expose that holds it, and how the row reaches it.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction
from math import lcm
from typing import Any, Callable, Dict, Hashable, Iterable, List, Optional, Sequence, Set, Tuple

from flux_engine.expr2.ast import (
    DATE_ADD_FUNCTIONS,
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
    String,
    Window,
    unparse,
)
from flux_engine.expr2.errors import (
    FLX_6103,
    FLX_6104,
    FLX_6105,
    FLX_6106,
    FLX_6107,
    FLX_6108,
    FLX_6109,
    FLX_6110,
    FLX_6111,
    FLX_6113,
    FLX_6206,
    Expr2Error,
)
from flux_engine.expr2.evaluate import RowView, Session, decoder, peers, sort_rows
from flux_engine.expr2.functions import (
    add_days,
    add_years,
    days_between,
    months_between,
    round_exact,
    try_add_months,
    years_between,
)
from flux_engine.expr2.lexer import MAX_LENGTH
from flux_engine.expr2.parser import Parsed, Side
from flux_engine.expr2.scope import Expose, Link, Model
from flux_engine.expr2.types import NULL_TYPE, ColumnType, Kind, SqlType

__all__ = ["Checked", "PREDICATE", "Read", "VALUE", "check"]

PREDICATE = "predicate"
VALUE = "value"

Fn = Callable[[RowView, Session], Any]

#: Window functions whose value depends on how tied rows are ordered.
_ORDER_SENSITIVE = frozenset({"ROW_NUMBER", "FIRST_VALUE", "LAST_VALUE", "LAG", "LEAD"})
_BOOL = SqlType(Kind.BOOL)
_ROUNDS_AND_COUNTS = SqlType(Kind.NUMBER, 0)


@dataclass(frozen=True, order=True)
class Read:
    """One column a text reads: how the row reaches it, the expose that holds it, the column.

    ``reach`` is ``row`` (the row itself), ``link`` (through relationships),
    ``previous`` (the entity's previous version), ``children`` (inside an
    aggregate's argument) or ``siblings`` (inside a window). ``path`` is the
    relationships crossed, starting with the children collection's expose
    and relationship for ``children``. The key columns that relationships,
    partitions and versions join on are not listed: they are the key graph's,
    which the planner reads beside this report (EXPR-SPEC-2 §10).
    """

    reach: str
    expose: str
    column: str
    path: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Checked:
    """A text that resolved and typed: its type, what it reads, its canonical text, its evaluator.

    ``canonical`` is read on the text's side: a core text's ``previous.x`` is a
    relationship's column and a FLUX-side one's is the previous version, so
    whoever stores the text stores where it came from with it (EXPR-SPEC-2 §1).
    """

    parsed: Parsed
    expose: str
    position: str
    target: Optional[str]
    type: SqlType
    reads: Tuple[Read, ...]
    canonical: str
    function: Fn = field(repr=False, compare=False)

    def evaluate(self, row: RowView, session: Optional[Session] = None) -> Any:
        """The text's value on *row*: ``True``/``False`` for a predicate, else a value or ``None``."""
        value = self.function(row, session if session is not None else Session())
        if self.position == PREDICATE:
            return value is True
        return value


@dataclass(frozen=True)
class _Typed:
    type: SqlType
    fn: Fn
    column: Optional[ColumnType] = None
    literal: Optional[str] = None
    #: Whether the value may be concatenated text (EXPR-SPEC-3 §3): it is then
    #: no level of an enum column, however CASE, COALESCE, an aggregate or a
    #: window value function carries it.
    concat: bool = False


@dataclass(frozen=True)
class _Ctx:
    expose: Expose
    reach: str = "row"
    prefix: Tuple[str, ...] = ()
    collections: Tuple[Tuple[str, str], ...] = ()
    windows_allowed: bool = True


def check(
    parsed: Parsed,
    model: Model,
    expose: str,
    *,
    position: str = PREDICATE,
    target: Optional[str] = None,
) -> Checked:
    """Resolve and type *parsed* for a row of *expose*, and build its evaluator.

    *position* is ``"predicate"`` for a ``check``, a ``when`` or an
    aggregate filter, where the text must be bool (``FLX-6206``), or
    ``"value"`` for a ``derive``'s ``expr``. *target* names the column a
    ``derive`` writes: its value must have that column's type and fit its
    scale (``FLX-6110``).
    """
    if position not in (PREDICATE, VALUE):
        raise ValueError(f"position is {PREDICATE!r} or {VALUE!r}, not {position!r}")
    if expose not in model.exposes:
        raise ValueError(f"the model holds no expose named {expose!r}")
    scope = model.exposes[expose]
    checker = _Checker(parsed, model)
    typed = checker.typed(parsed.tree, _Ctx(scope))
    # NULL takes its type from a typed operand beside it, never from its
    # position (EXPR-SPEC-2 §4), so a text that is only NULL has none.
    _refuse_untyped([typed.type], "the text", 0)
    _check_position(typed, scope, position, target, parsed)
    canonical = unparse(
        parsed.tree, flux_side=parsed.side is Side.FLUX, columns=frozenset(scope.columns)
    )
    if len(canonical) > MAX_LENGTH:
        # plan.json stores the canonical text, and it must parse back (EXPR-SPEC-2 §11).
        raise Expr2Error(
            FLX_6103,
            f"the canonical text, which the plan stores, would be {len(canonical)} code points; "
            f"the limit is {MAX_LENGTH}",
            position=0,
        )
    return Checked(
        parsed=parsed,
        expose=expose,
        position=position,
        target=target,
        type=typed.type,
        reads=tuple(sorted(checker.reads)),
        canonical=canonical,
        function=typed.fn,
    )


def _check_position(
    typed: _Typed, scope: Expose, position: str, target: Optional[str], parsed: Parsed
) -> None:
    found = typed.type.describe()
    if position == PREDICATE:
        if typed.type.kind is not Kind.BOOL:
            raise Expr2Error(FLX_6206, f"a predicate is bool, and this text is {found}", position=0)
        return
    if target is None:
        return
    column = scope.columns.get(target)
    if column is None:
        raise Expr2Error(FLX_6113, f"{scope.name} has no column {target!r} to derive", position=0)
    problem = column.derive_target_problem
    if problem is not None:
        raise Expr2Error(FLX_6110, f"{target!r} cannot be derived: {problem}", position=0)
    assert column.kind is not None  # derive_target_problem refuses an untyped column
    if column.kind is Kind.BOOL and typed.type.kind is not Kind.BOOL:
        raise Expr2Error(
            FLX_6206, f"{target!r} is a bool column, and this text is {found}", position=0
        )
    if typed.type.kind is not column.kind:
        raise Expr2Error(
            FLX_6110, f"{target!r} is {column.kind.value}, and this text is {found}", position=0
        )
    if column.enum is not None and typed.concat:
        raise Expr2Error(
            FLX_6110,
            f"{target!r} is an enum column, and a concatenation is not known to be one of its "
            "levels",
            position=0,
        )
    if column.kind is Kind.NUMBER:
        assert column.scale is not None  # derive_target_problem refuses an unbounded column
        if typed.type.scale is None or typed.type.scale > column.scale:
            raise Expr2Error(
                FLX_6110,
                f"{target!r} has scale {column.scale}, and this text is {found}: wrap it in "
                f"ROUND(..., {column.scale})",
                position=0,
            )


def _truthy(value: Any) -> bool:
    return value is True


def _refuse_untyped(types: Sequence[SqlType], what: str, position: int) -> None:
    """NULL takes its type from a typed operand beside it (EXPR-SPEC-2 §4): never all NULL."""
    if all(t.is_null for t in types):
        raise Expr2Error(
            FLX_6104,
            f"{what} has no typed operand: NULL takes its type from a typed value beside it, "
            "and everything here is NULL",
            position=position,
        )


def _unify(types: Sequence[SqlType], what: str, position: int) -> SqlType:
    _refuse_untyped(types, what, position)
    kinds = {t.kind for t in types if t.kind is not None}
    if len(kinds) > 1:
        names = ", ".join(sorted(kind.value for kind in kinds if kind is not None))
        raise Expr2Error(FLX_6109, f"{what} mixes types: {names}", position=position)
    kind = kinds.pop()
    if kind is not Kind.NUMBER:
        return SqlType(kind)
    numbers = [t for t in types if t.kind is Kind.NUMBER]
    if any(t.scale is None for t in numbers):
        return SqlType(Kind.NUMBER, None)
    return SqlType(Kind.NUMBER, max(t.scale for t in numbers if t.scale is not None))


def _constant(value: Any) -> Fn:
    return lambda row, session: value


class _Checker:
    def __init__(self, parsed: Parsed, model: Model) -> None:
        self.parsed = parsed
        self.model = model
        self.reads: Set[Read] = set()

    # --------------------------------------------------------- dispatch

    def typed(self, node: Node, ctx: _Ctx) -> _Typed:
        method = getattr(self, "_" + type(node).__name__.lower())
        result: _Typed = method(node, ctx)
        return result

    def _number(self, node: Number, ctx: _Ctx) -> _Typed:
        return _Typed(SqlType(Kind.NUMBER, node.scale), _constant(node.value))

    def _string(self, node: String, ctx: _Ctx) -> _Typed:
        return _Typed(SqlType(Kind.STRING), _constant(node.value), literal=node.value)

    def _bool(self, node: Bool, ctx: _Ctx) -> _Typed:
        return _Typed(_BOOL, _constant(node.value))

    def _null(self, node: Null, ctx: _Ctx) -> _Typed:
        return _Typed(NULL_TYPE, _constant(None))

    def _date(self, node: Date, ctx: _Ctx) -> _Typed:
        value = date.fromisoformat(node.value)
        return _Typed(SqlType(Kind.DATE), _constant(value))

    def _children(self, node: Children, ctx: _Ctx) -> _Typed:  # pragma: no cover - parser
        raise Expr2Error(FLX_6106, "a children collection is not a value", position=node.position)

    # ------------------------------------------------------------ names

    def _column(self, node: Column, ctx: _Ctx) -> _Typed:
        scope = ctx.expose
        if node.relationship is None:
            if node.root_note is not None:
                self._bare_root(node.root_note, node.name, scope, node.position)
            column = self._column_of(scope, node.name, node.position)
            self._read(ctx, "row", scope.name, node.name, ())
            return _Typed(column.sql_type, _getter((), False, node.name, decoder(column)), column)
        link = self._link_of(scope, node.relationship, node.position)
        target = self._hop(link, node.position)
        column = self._column_of(target, node.name, node.position)
        self._read(ctx, "link", target.name, node.name, (link.name,))
        fn = _getter((link.name,), False, node.name, decoder(column))
        return _Typed(column.sql_type, fn, column)

    def _path(self, node: Path, ctx: _Ctx) -> _Typed:
        scope = ctx.expose
        previous = node.root == "previous"
        if previous:
            if node.root_note is not None:
                self._root_conflict(node.root_note, scope, node.position)
            if scope.versions is None or not scope.versions.entity:
                raise Expr2Error(
                    FLX_6106,
                    f"previous needs an expose whose entity has versions, and {scope.name} "
                    "declares no validity window",
                    position=node.position,
                )
        current = scope
        steps: List[str] = []
        for name in node.parts[:-1]:
            link = self._link_of(current, name, node.position)
            current = self._hop(link, node.position)
            steps.append(link.name)
        column = self._column_of(current, node.parts[-1], node.position)
        reach = "previous" if previous else "link"
        path = (("previous",) if previous else ()) + tuple(steps)
        self._read(ctx, reach, current.name, node.parts[-1], path)
        fn = _getter(tuple(steps), previous, node.parts[-1], decoder(column))
        return _Typed(column.sql_type, fn, column)

    def _read(self, ctx: _Ctx, reach: str, expose: str, column: str, path: Tuple[str, ...]) -> None:
        if ctx.reach != "row":
            reach = ctx.reach
            path = ctx.prefix + path
        self.reads.add(Read(reach, expose, column, path))

    def _bare_root(self, written: str, name: str, scope: Expose, position: int) -> None:
        if _shadows_a_name(written, scope):
            raise Expr2Error(
                FLX_6105,
                f"{written} is a root and also a column or relationship of {scope.name}: write "
                f"row.{written} for the column, or link.{written}.<column> for the relationship",
                position=position,
            )
        root = written.lower()
        usage = {
            "row": "row.<column>",
            "link": "link.<relationship>.<column>",
            "previous": "previous.<column>",
            "children": "an aggregate's CHILDREN.<expose>",
        }[root]
        raise Expr2Error(
            FLX_6106, f"{written} is a root, not a value: write {usage}", position=position
        )

    def _root_conflict(self, written: str, scope: Expose, position: int) -> None:
        if _shadows_a_name(written, scope):
            raise Expr2Error(
                FLX_6105,
                f"{written} is a root and also a column or relationship of {scope.name}: write "
                f"row.{written} for the column, or link.{written}.<column> for the relationship",
                position=position,
            )

    def _column_of(self, scope: Expose, name: str, position: int) -> ColumnType:
        column = scope.columns.get(name)
        if column is None:
            raise Expr2Error(
                FLX_6113,
                f"{scope.name} has no column {name!r}{_suggest(name, scope.columns)}",
                position=position,
            )
        if column.kind is None:
            raise Expr2Error(
                FLX_6111,
                f"{scope.name}.{name} is declared {column.declared!r}, which has no FLUID SQL/1 type",
                position=position,
            )
        return column

    def _link_of(self, scope: Expose, name: str, position: int) -> Link:
        link = scope.links.get(name)
        if link is None:
            raise Expr2Error(
                FLX_6113,
                f"{scope.name} has no relationship {name!r}{_suggest(name, scope.links)}",
                position=position,
            )
        return link

    def _hop(self, link: Link, position: int) -> Expose:
        target = self.model.exposes[link.target]
        if target.declares_validity_window:
            raise Expr2Error(
                FLX_6106,
                f"relationship {link.name!r} reaches {target.name}, which declares a validity "
                "window; no path crosses such a relationship until the declaring row's time "
                "column is named (RFC-data-rules.md §2.2)",
                position=position,
            )
        return target

    # ------------------------------------------------------- arithmetic

    def _neg(self, node: Neg, ctx: _Ctx) -> _Typed:
        operand = self.typed(node.operand, ctx)
        _refuse_untyped([operand.type], "a minus", node.position)
        _require_number(operand.type, "a minus", node.position)
        inner = operand.fn
        scale = operand.type.scale

        def negate(row: RowView, session: Session) -> Any:
            value = inner(row, session)
            return None if value is None else -value

        return _Typed(SqlType(Kind.NUMBER, scale), negate)

    def _arith(self, node: Arith, ctx: _Ctx) -> _Typed:
        left = self.typed(node.left, ctx)
        right = self.typed(node.right, ctx)
        _refuse_untyped([left.type, right.type], f"'{node.op}'", node.position)
        for side in (left, right):
            _require_number(side.type, f"'{node.op}'", node.position)
        scales = [0 if t.type.is_null else t.type.scale for t in (left, right)]
        scale: Optional[int]
        if node.op == "/" or None in scales:
            scale = None
        elif node.op == "*":
            scale = int(scales[0] or 0) + int(scales[1] or 0)
        else:
            scale = max(int(s or 0) for s in scales)
        lf, rf, op = left.fn, right.fn, node.op

        def arith(row: RowView, session: Session) -> Any:
            a = lf(row, session)
            if a is None:
                return None
            b = rf(row, session)
            if b is None:
                return None
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            return None if b == 0 else Fraction(a) / b

        return _Typed(SqlType(Kind.NUMBER, scale), arith)

    # ------------------------------------------------------- predicates

    def _compare(self, node: Compare, ctx: _Ctx) -> _Typed:
        left = self.typed(node.left, ctx)
        right = self.typed(node.right, ctx)
        kind = _unify([left.type, right.type], f"'{node.op}'", node.position).kind
        if node.op not in ("=", "<>") and kind is Kind.BOOL:
            raise Expr2Error(
                FLX_6109, f"'{node.op}' takes an orderable type, not bool", position=node.position
            )
        _check_enum(left, right, node.position)
        lf, rf, op = left.fn, right.fn, node.op

        def compare(row: RowView, session: Session) -> bool:
            a = lf(row, session)
            if a is None:
                return False
            b = rf(row, session)
            if b is None:
                return False
            if op == "=":
                return bool(a == b)
            if op == "<>":
                return bool(a != b)
            if op == "<":
                return bool(a < b)
            if op == "<=":
                return bool(a <= b)
            if op == ">":
                return bool(a > b)
            return bool(a >= b)

        return _Typed(_BOOL, compare)

    def _in(self, node: In, ctx: _Ctx) -> _Typed:
        value = self.typed(node.value, ctx)
        items = [self.typed(item, ctx) for item in node.items]
        _unify([value.type] + [item.type for item in items], "IN", node.position)
        for item in items:
            _check_enum(value, item, node.position)
        members = frozenset(item.fn(_NO_ROW, Session()) for item in items)
        vf, negated = value.fn, node.negated

        def member(row: RowView, session: Session) -> bool:
            v = vf(row, session)
            if v is None:
                return False
            return (v in members) is not negated

        return _Typed(_BOOL, member)

    def _between(self, node: Between, ctx: _Ctx) -> _Typed:
        value = self.typed(node.value, ctx)
        low = self.typed(node.low, ctx)
        high = self.typed(node.high, ctx)
        kind = _unify([value.type, low.type, high.type], "BETWEEN", node.position).kind
        if kind is Kind.BOOL:
            raise Expr2Error(
                FLX_6109, "BETWEEN takes an orderable type, not bool", position=node.position
            )
        _check_enum(value, low, node.position)
        _check_enum(value, high, node.position)
        vf, lf, hf, negated = value.fn, low.fn, high.fn, node.negated

        def between(row: RowView, session: Session) -> bool:
            v = vf(row, session)
            lo = lf(row, session)
            hi = hf(row, session)
            if negated:
                below = v is not None and lo is not None and v < lo
                above = v is not None and hi is not None and v > hi
                return bool(below or above)
            if v is None or lo is None or hi is None:
                return False
            return bool(lo <= v <= hi)

        return _Typed(_BOOL, between)

    def _isnull(self, node: IsNull, ctx: _Ctx) -> _Typed:
        value = self.typed(node.value, ctx)
        test = "IS NOT NULL" if node.negated else "IS NULL"
        _refuse_untyped([value.type], test, node.position)
        vf, negated = value.fn, node.negated
        return _Typed(_BOOL, lambda row, session: (vf(row, session) is None) is not negated)

    def _isdistinctfrom(self, node: IsDistinctFrom, ctx: _Ctx) -> _Typed:
        left = self.typed(node.left, ctx)
        right = self.typed(node.right, ctx)
        test = "IS NOT DISTINCT FROM" if node.negated else "IS DISTINCT FROM"
        _unify([left.type, right.type], test, node.position)
        _check_enum(left, right, node.position)
        lf, rf, negated = left.fn, right.fn, node.negated

        def distinct(row: RowView, session: Session) -> bool:
            a = lf(row, session)
            b = rf(row, session)
            if a is None or b is None:
                different = (a is None) is not (b is None)
            else:
                different = bool(a != b)
            return different is not negated

        return _Typed(_BOOL, distinct)

    def _not(self, node: Not, ctx: _Ctx) -> _Typed:
        operand = self.typed(node.operand, ctx)
        _require_bool(operand.type, "NOT", node.position)
        inner = operand.fn
        return _Typed(_BOOL, lambda row, session: not _truthy(inner(row, session)))

    def _and(self, node: And, ctx: _Ctx) -> _Typed:
        left = self.typed(node.left, ctx)
        right = self.typed(node.right, ctx)
        for side in (left, right):
            _require_bool(side.type, "AND", node.position)
        lf, rf = left.fn, right.fn
        return _Typed(
            _BOOL, lambda row, session: _truthy(lf(row, session)) and _truthy(rf(row, session))
        )

    def _or(self, node: Or, ctx: _Ctx) -> _Typed:
        left = self.typed(node.left, ctx)
        right = self.typed(node.right, ctx)
        for side in (left, right):
            _require_bool(side.type, "OR", node.position)
        lf, rf = left.fn, right.fn
        return _Typed(
            _BOOL, lambda row, session: _truthy(lf(row, session)) or _truthy(rf(row, session))
        )

    # ------------------------------------------------- CASE and calls

    def _case(self, node: Case, ctx: _Ctx) -> _Typed:
        branches: List[Tuple[Fn, Fn]] = []
        types: List[SqlType] = []
        carried: List[_Typed] = []
        for condition, value in node.whens:
            typed_condition = self.typed(condition, ctx)
            _require_bool(typed_condition.type, "a WHEN condition", node.position)
            typed_value = self.typed(value, ctx)
            branches.append((typed_condition.fn, typed_value.fn))
            types.append(typed_value.type)
            carried.append(typed_value)
        otherwise: Fn = _constant(None)
        if node.otherwise is not None:
            typed_else = self.typed(node.otherwise, ctx)
            otherwise = typed_else.fn
            carried.append(typed_else)
            types.append(typed_else.type)
        result = _unify(types, "CASE", node.position)

        def case(row: RowView, session: Session) -> Any:
            for when, then in branches:
                if _truthy(when(row, session)):
                    return then(row, session)
            return otherwise(row, session)

        return _Typed(result, case, concat=any(t.concat for t in carried))

    def _call(self, node: Call, ctx: _Ctx) -> _Typed:
        args = [self.typed(arg, ctx) for arg in node.args]
        if node.name == "COALESCE":
            result = _unify([a.type for a in args], "COALESCE", node.position)
            fns = [a.fn for a in args]

            def coalesce(row: RowView, session: Session) -> Any:
                for fn in fns:
                    value = fn(row, session)
                    if value is not None:
                        return value
                return None

            return _Typed(result, coalesce, concat=any(a.concat for a in args))
        if node.name == "ROUND":
            _refuse_untyped([args[0].type], "ROUND", node.position)
            _require_number(args[0].type, "ROUND", node.position)
            scale_node = node.args[1]
            assert isinstance(scale_node, Number)
            scale = int(scale_node.text)
            inner, mode = args[0].fn, node.mode or "HALF_AWAY_FROM_ZERO"

            def rounded(row: RowView, session: Session) -> Any:
                value = inner(row, session)
                return None if value is None else round_exact(Fraction(value), scale, mode)

            return _Typed(SqlType(Kind.NUMBER, scale), rounded)
        function = {
            "DAYS_BETWEEN": days_between,
            "MONTHS_BETWEEN": months_between,
            "YEARS_BETWEEN": years_between,
        }[node.name]
        _refuse_untyped([arg.type for arg in args], node.name, node.position)
        for arg in args:
            if arg.type.kind not in (Kind.DATE, None):
                raise Expr2Error(
                    FLX_6109,
                    f"{node.name} takes two dates, and an argument is {arg.type.describe()}",
                    position=node.position,
                )
        af, bf = args[0].fn, args[1].fn

        def difference(row: RowView, session: Session) -> Any:
            a = af(row, session)
            b = bf(row, session)
            if a is None or b is None:
                return None
            return Fraction(function(a, b))

        return _Typed(_ROUNDS_AND_COUNTS, difference)

    # ------------------------------------------- flux-expr/3 extensions

    def _require_flux_expr_3(self, what: str, position: int) -> None:
        # The parser is what refuses these forms; a tree built by hand and checked
        # under flux-expr/2 gets the same refusal rather than a language it never declared.
        if self.parsed.language != "flux-expr/3" or self.parsed.side is Side.CORE:
            raise Expr2Error(
                FLX_6103,
                f"{what} is a flux-expr/3 extension of a FLUX-side rule",
                position=position,
            )

    def _extract(self, node: Extract, ctx: _Ctx) -> _Typed:
        self._require_flux_expr_3("EXTRACT", node.position)
        arg = self.typed(node.arg, ctx)
        _refuse_untyped([arg.type], "EXTRACT", node.position)
        _require_date(arg.type, "EXTRACT", node.position)
        inner, part = arg.fn, node.part

        def extract(row: RowView, session: Session) -> Any:
            value = inner(row, session)
            return None if value is None else Fraction(getattr(value, part))

        return _Typed(_ROUNDS_AND_COUNTS, extract)

    def _dateadd(self, node: DateAdd, ctx: _Ctx) -> _Typed:
        name = next(word for word, unit in DATE_ADD_FUNCTIONS.items() if unit == node.unit)
        if self.parsed.side is not Side.CORE:
            self._require_flux_expr_3(name, node.position)
        start = self.typed(node.date, ctx)
        count = self.typed(node.count, ctx)
        for operand in (start, count):
            _refuse_untyped([operand.type], name, node.position)
        _require_date(start.type, name, node.position)
        _require_integer(count.type, name, node.position)
        move = {"day": add_days, "month": try_add_months, "year": add_years}[node.unit]
        sf, nf = start.fn, count.fn

        def shift(row: RowView, session: Session) -> Any:
            a = sf(row, session)
            if a is None:
                return None
            n = nf(row, session)
            if n is None:
                return None
            return move(a, int(n))

        return _Typed(SqlType(Kind.DATE), shift)

    def _concat(self, node: Concat, ctx: _Ctx) -> _Typed:
        self._require_flux_expr_3("'||'", node.position)
        left = self.typed(node.left, ctx)
        right = self.typed(node.right, ctx)
        _refuse_untyped([left.type, right.type], "'||'", node.position)
        for side in (left, right):
            if side.type.kind is Kind.NUMBER:
                _require_integer(side.type, "'||'", node.position)
            elif side.type.kind not in (Kind.STRING, None):
                raise Expr2Error(
                    FLX_6109,
                    f"'||' takes strings and integers, and an operand is {side.type.describe()}",
                    position=node.position,
                )
        lf, rf = left.fn, right.fn

        def concat(row: RowView, session: Session) -> Any:
            a = lf(row, session)
            if a is None:
                return None
            b = rf(row, session)
            if b is None:
                return None
            return _text(a) + _text(b)

        return _Typed(SqlType(Kind.STRING), concat, concat=True)

    # ------------------------------------------------------ extensions

    def _aggregate(self, node: Aggregate, ctx: _Ctx) -> _Typed:
        scope = ctx.expose
        children = node.children
        if children.root_note is not None:
            self._root_conflict(children.root_note, scope, children.position)
        child = self.model.exposes.get(children.expose)
        if child is None:
            raise Expr2Error(
                FLX_6113,
                f"the model has no expose {children.expose!r}"
                f"{_suggest(children.expose, self.model.exposes)}",
                position=children.position,
            )
        links = self.model.children_links(scope.name, child.name)
        if children.relationship is not None:
            chosen = child.links.get(children.relationship)
            if chosen is None:
                raise Expr2Error(
                    FLX_6113,
                    f"{child.name} has no relationship {children.relationship!r}"
                    f"{_suggest(children.relationship, child.links)}",
                    position=children.position,
                )
            if chosen.target != scope.name:
                raise Expr2Error(
                    FLX_6106,
                    f"{child.name}.{chosen.name} points to {chosen.target}, not to {scope.name}",
                    position=children.position,
                )
        elif len(links) == 1:
            chosen = links[0]
        else:
            what = "no relationship" if not links else "several relationships"
            raise Expr2Error(
                FLX_6106,
                f"{child.name} has {what} to {scope.name}: name one, "
                f"CHILDREN.{child.name}.<relationship>",
                position=children.position,
            )
        if scope.declares_validity_window:
            raise Expr2Error(
                FLX_6106,
                f"{child.name}.{chosen.name} reaches {scope.name}, which declares a validity "
                "window; no summary crosses such a relationship (RFC-data-rules.md §2.2)",
                position=children.position,
            )
        collection = (child.name, chosen.name)
        if collection in ctx.collections:
            raise Expr2Error(
                FLX_6107,
                f"{node.name} is nested over the same collection, {child.name}.{chosen.name}",
                position=node.position,
            )
        inner = _Ctx(
            child,
            reach="children",
            prefix=(ctx.prefix if ctx.reach != "row" else ()) + collection,
            collections=ctx.collections + (collection,),
            windows_allowed=False,
        )
        arg = None if node.arg is None else self.typed(node.arg, inner)
        result = self._aggregate_type(node, arg)
        token = object()
        function = _aggregate_function(node.name, None if arg is None else arg.fn)
        child_name, relationship = collection

        def aggregate(row: RowView, session: Session) -> Any:
            key = (token, row.key)
            if key not in session.aggregates:
                rows = row.children(child_name, relationship)
                session.aggregates[key] = function(rows, session)
            return session.aggregates[key]

        carries = arg is not None and arg.concat and node.name in ("MIN", "MAX")
        return _Typed(result, aggregate, concat=carries)

    def _aggregate_type(self, node: Aggregate, arg: Optional[_Typed]) -> SqlType:
        name = node.name
        if name == "COUNT":
            if arg is not None:
                _require_bool(arg.type, "COUNT's second argument, a predicate,", node.position)
            return _ROUNDS_AND_COUNTS
        if arg is None:
            raise Expr2Error(
                FLX_6109,
                f"{name} takes a value to summarize: {name}(CHILDREN.<expose>, x)",
                position=node.position,
            )
        _refuse_untyped([arg.type], name, node.position)
        if name in ("SUM", "AVG"):
            _require_number(arg.type, name, node.position)
            if arg.type.scale is None:
                raise Expr2Error(
                    FLX_6110,
                    f"{name} takes a number of bounded scale; wrap a quotient in ROUND(x, s)",
                    position=node.position,
                )
            return SqlType(Kind.NUMBER, None) if name == "AVG" else arg.type
        if arg.type.kind is Kind.BOOL:
            raise Expr2Error(
                FLX_6109, f"{name} takes an orderable type, not bool", position=node.position
            )
        return arg.type

    def _window(self, node: Window, ctx: _Ctx) -> _Typed:
        scope = ctx.expose
        if not ctx.windows_allowed:
            raise Expr2Error(
                FLX_6107,
                f"{node.name} is a window inside an aggregate's argument or another window's",
                position=node.position,
            )
        link = scope.links.get(node.partition)
        if link is None:
            raise Expr2Error(
                FLX_6106,
                f"PARTITION BY {node.partition}: {scope.name} has no relationship of that name"
                f"{_suggest(node.partition, scope.links)}",
                position=node.position,
            )
        if node.partition_bare and node.partition in scope.columns:
            raise Expr2Error(
                FLX_6105,
                f"PARTITION BY {node.partition}: {scope.name} also has a column of that name; "
                f"write PARTITION BY link.{node.partition}",
                position=node.position,
            )
        inner = _Ctx(
            scope,
            reach="siblings",
            prefix=(ctx.prefix if ctx.reach != "row" else ()) + (link.name,),
            collections=ctx.collections,
            windows_allowed=False,
        )
        args = [self.typed(arg, inner) for arg in node.args]
        keys: List[_Typed] = []
        own_columns: Set[str] = set()
        for sort_key in node.order:
            typed = self.typed(sort_key.key, inner)
            if typed.type.kind is Kind.BOOL:
                raise Expr2Error(
                    FLX_6109, "ORDER BY takes an orderable type, not bool", position=node.position
                )
            keys.append(typed)
            key_node = sort_key.key
            if isinstance(key_node, Column) and key_node.relationship is None:
                own_columns.add(key_node.name)
        if node.name in _ORDER_SENSITIVE:
            covered = own_columns | set(link.from_columns)
            missing = [column for column in scope.key if column not in covered]
            if not scope.key:
                raise Expr2Error(
                    FLX_6108,
                    f"{node.name} needs a total order, and {scope.name} declares no key",
                    position=node.position,
                )
            if missing:
                raise Expr2Error(
                    FLX_6108,
                    f"{node.name}'s order is not total: add {', '.join(missing)} to ORDER BY",
                    position=node.position,
                )
        result = self._window_type(node, args)
        directions = [(k.desc, k.nulls_first) for k in node.order]
        value_fn = args[0].fn if args else None
        default_fn = args[2].fn if len(args) == 3 else _constant(None)
        offset = 1
        if len(args) >= 2:
            offset_node = node.args[1]
            assert isinstance(offset_node, Number)
            offset = int(offset_node.text)
        key_fns = [k.fn for k in keys]
        part_decoders = [
            (column, decoder(scope.columns[column]) if column in scope.columns else _same)
            for column in link.from_columns
        ]
        token = object()
        name, relationship = node.name, link.name

        def window(row: RowView, session: Session) -> Any:
            partition = tuple(decode(row.get(column)) for column, decode in part_decoders)
            cache_key = (token, partition)
            table = session.windows.get(cache_key)
            if table is None:
                rows = list(row.siblings(relationship))
                table = _window_table(
                    name, rows, key_fns, directions, value_fn, offset, default_fn, session
                )
                session.windows[cache_key] = table
            if row.key not in table:
                raise LookupError(
                    "the row view's siblings() did not include the row itself (RowView contract)"
                )
            return table[row.key]

        carries = node.name in ("FIRST_VALUE", "LAST_VALUE", "LAG", "LEAD") and any(
            a.concat for a in (args[0], *args[2:3])
        )
        return _Typed(result, window, concat=carries)

    def _window_type(self, node: Window, args: List[_Typed]) -> SqlType:
        if node.name in ("ROW_NUMBER", "RANK", "DENSE_RANK"):
            return _ROUNDS_AND_COUNTS
        values = [args[0].type] + ([args[2].type] if len(args) == 3 else [])
        return _unify(values, node.name, node.position)


# ------------------------------------------------------------- helpers


class _NoRow:
    """Evaluates the constant items of an IN list, which read no row."""

    key: Hashable = None

    def get(self, column: str) -> Any:  # pragma: no cover - constants read nothing
        raise AssertionError("a literal reads no column")

    def follow(self, relationship: str) -> Any:  # pragma: no cover
        raise AssertionError("a literal follows no relationship")

    def previous(self) -> Any:  # pragma: no cover
        raise AssertionError("a literal has no previous version")

    def children(self, expose: str, relationship: str) -> Sequence[Any]:  # pragma: no cover
        raise AssertionError("a literal has no children")

    def siblings(self, relationship: str) -> Sequence[Any]:  # pragma: no cover
        raise AssertionError("a literal has no siblings")


_NO_ROW: RowView = _NoRow()


def _same(value: Any) -> Any:
    return value


def _getter(
    steps: Tuple[str, ...], previous: bool, column: str, decode: Callable[[Any], Any]
) -> Fn:
    def get(row: RowView, session: Session) -> Any:
        current: Optional[RowView] = row.previous() if previous else row
        for step in steps:
            if current is None:
                return None
            current = current.follow(step)
        if current is None:
            return None
        return decode(current.get(column))

    return get


def _shadows_a_name(root: str, scope: Expose) -> bool:
    """Whether the root, read case-insensitively as roots are, is also a column or relationship.

    The canonical text writes a root in one case (``previous.``, ``CHILDREN.``),
    so a check that compared the written case would accept ``PREVIOUS.x`` and
    refuse its own canonical text (EXPR-SPEC-2 §11).
    """
    word = root.lower()
    return any(name.lower() == word for name in (*scope.columns, *scope.links))


def _require_number(found: SqlType, what: str, position: int) -> None:
    if found.kind not in (Kind.NUMBER, None):
        raise Expr2Error(
            FLX_6109,
            f"{what} takes numbers, and an operand is {found.describe()}",
            position=position,
        )


def _require_date(found: SqlType, what: str, position: int) -> None:
    if found.kind is not Kind.DATE:
        raise Expr2Error(
            FLX_6109,
            f"{what} takes a date, and an operand is {found.describe()}",
            position=position,
        )


def _require_integer(found: SqlType, what: str, position: int) -> None:
    """A number of static scale 0 (EXPR-SPEC-3 §3): not a fraction, a quotient or an unbounded number."""
    if found.kind is not Kind.NUMBER or found.scale != 0:
        raise Expr2Error(
            FLX_6109,
            f"{what} takes an integer (a number of scale 0), and an operand is {found.describe()}",
            position=position,
        )


def _text(value: Any) -> str:
    """A concatenation operand as text: a string as stored, an integer as its decimal digits."""
    if isinstance(value, str):
        return value
    # str(int) refuses more than 4,300 digits; Decimal(int) is exact at any size.
    return str(Decimal(Fraction(value).numerator))


def _require_bool(found: SqlType, what: str, position: int) -> None:
    if found.is_null:
        # A NULL where a bool is needed counts as FALSE (RFC §2.5), so it says nothing.
        raise Expr2Error(
            FLX_6104,
            f"{what} takes a bool, and NULL there would always count as FALSE",
            position=position,
        )
    if found.kind is not Kind.BOOL:
        raise Expr2Error(
            FLX_6109,
            f"{what} takes a bool, and an operand is {found.describe()}",
            position=position,
        )


def _check_enum(side: _Typed, other: _Typed, position: int) -> None:
    """A string literal compared with an enum column is one of its levels (RFC §2.3)."""
    for column_side, literal_side in ((side, other), (other, side)):
        column = column_side.column
        literal = literal_side.literal
        if column is None or column.enum is None or literal is None:
            continue
        # A placeholder is a valid value of its column, exempt from the enum (RFC §4).
        if literal not in column.enum and not column.is_placeholder(literal):
            raise Expr2Error(
                FLX_6109,
                f"'{literal}' is not a level of the enum column it is compared with "
                f"({', '.join(column.enum)}), nor one of its placeholders",
                position=position,
            )


def _suggest(name: str, known: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.6)
    return f"; did you mean {close[0]!r}?" if close else ""


def _exact_sum(found: List[Any]) -> Any:
    """The sum of *found*, a non-empty list of numbers, as ``sum(found, Fraction(0))`` gives it.

    Adding Fractions one at a time reduces every partial sum by a gcd. The same exact value is
    one reduction when the terms are put over their common denominator first (a Fraction is
    held in lowest terms, so equal values are equal objects). Anything that is not a Fraction is
    summed the plain way.
    """
    if any(type(term) is not Fraction for term in found):
        return sum(found, Fraction(0))
    denominator = 1
    for term in found:
        d = term.denominator
        if d != denominator and denominator % d:
            denominator = lcm(denominator, d)
    numerator = 0
    for term in found:
        d = term.denominator
        numerator += term.numerator * (denominator // d) if d != denominator else term.numerator
    return Fraction(numerator, denominator)


def _aggregate_function(
    name: str, arg: Optional[Fn]
) -> Callable[[Sequence[RowView], Session], Any]:
    def count(rows: Sequence[RowView], session: Session) -> Any:
        if arg is None:
            return Fraction(len(rows))
        return Fraction(sum(1 for child in rows if _truthy(arg(child, session))))

    def values(rows: Sequence[RowView], session: Session) -> List[Any]:
        assert arg is not None
        found = (arg(child, session) for child in rows)
        return [value for value in found if value is not None]

    def total(rows: Sequence[RowView], session: Session) -> Any:
        found = values(rows, session)
        return _exact_sum(found) if found else None

    def smallest(rows: Sequence[RowView], session: Session) -> Any:
        found = values(rows, session)
        return min(found) if found else None

    def largest(rows: Sequence[RowView], session: Session) -> Any:
        found = values(rows, session)
        return max(found) if found else None

    def average(rows: Sequence[RowView], session: Session) -> Any:
        found = values(rows, session)
        return _exact_sum(found) / len(found) if found else None

    return {"COUNT": count, "SUM": total, "MIN": smallest, "MAX": largest, "AVG": average}[name]


def _window_table(
    name: str,
    rows: List[RowView],
    key_fns: List[Fn],
    directions: List[Tuple[bool, bool]],
    value_fn: Optional[Fn],
    offset: int,
    default_fn: Fn,
    session: Session,
) -> Dict[Hashable, Any]:
    keys = [[fn(row, session) for fn in key_fns] for row in rows]
    order = sort_rows(rows, keys, directions)
    table: Dict[Hashable, Any] = {}
    if name in ("ROW_NUMBER", "RANK", "DENSE_RANK"):
        rank = dense = 0
        for place, index in enumerate(order):
            if place == 0 or not peers(keys, order[place - 1], index):
                rank = place + 1
                dense += 1
            value = place + 1 if name == "ROW_NUMBER" else rank if name == "RANK" else dense
            table[rows[index].key] = Fraction(value)
        return table
    assert value_fn is not None
    if name in ("FIRST_VALUE", "LAST_VALUE"):
        edge = rows[order[0] if name == "FIRST_VALUE" else order[-1]]
        value = value_fn(edge, session)
        return {row.key: value for row in rows}
    step = -offset if name == "LAG" else offset
    for place, index in enumerate(order):
        other = place + step
        if 0 <= other < len(order):
            table[rows[index].key] = value_fn(rows[order[other]], session)
        else:
            table[rows[index].key] = default_fn(rows[index], session)
    return table
