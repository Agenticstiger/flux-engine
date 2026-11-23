# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What the consumers' queries need the copy to hold (ADR 0028 section 5).

A filter on ``plan = 'premium'`` needs the level ``premium``, a test for ``ended_at = DATE
'9999-12-31'`` needs that value, and ``sku LIKE 'PRO-%'`` needs values of that shape. This module
reads each checked query for those needs, decides which a declaration already meets, and writes
the counts that show whether a run meets them.

**Deriving.** :func:`derive_requirements` reads the qualified tree of each query
(:attr:`flux_engine.consumers.check.CheckedQuery.tree`: placeholders are already bound, every
column names its table or its alias) and resolves each column through aliases, ``WITH`` entries,
views and derived tables to the column of an expose, with sqlglot's scope helpers. Only a predicate
between one resolved column and literals counts:

=====================================  ==============================  =========================
predicate                              on                              requirement
=====================================  ==============================  =========================
``c = 'x'``, ``'x' = c``, ``c IN``     a string or boolean column      ``level``: one per literal
``c = v``, ``c IN (v, ...)``           a number, date or timestamp     ``value``: one per literal
``c LIKE p``, ``c ILIKE p``            a string column                 ``pattern``
``c IS NULL``                          any column                      ``nulls``
``c BETWEEN a AND b``, ``<``, ``>=``   a number, date or timestamp     ``range``
=====================================  ==============================  =========================

A predicate under ``NOT`` (including ``IS NOT NULL`` and ``NOT IN``) asks for rows that lack
something, so it is no requirement. The text of a query is read and nothing else: a held-out query
counts as a calibration query does, and no rate takes part.

``values`` holds, by kind: ``level`` and ``value`` the literal's text; ``pattern`` the pattern, then
``like`` or ``ilike``, then the escape character when one is written; ``nulls`` nothing; ``range``
the lower and the upper bound, each empty or an operator (``>=``, ``>``, ``<=`` or ``<``) and the
bound's text.

**Deciding at init.** :func:`assess_requirements` compares each requirement with what a declaration
carries (:class:`ColumnFacts`) and says ``met``, ``unmet`` or ``verify`` (a shape that only a run can
show, left to ``verify``), with the reason. ``init`` refuses nothing for an unmet one.

**Checking at verify.** :func:`requirement_requests` writes, for each requirement, the canonical
text of the rows that meet it, the shape :class:`flux_engine.consumers.runner.CountRequest` counts;
the text is built from the tree, never by joining strings, and is read and checked again as the
checker's canonical texts are. :func:`check_requirements` turns the counts into decisions. A
requirement that was compiled under another sqlglot version than the one that checks it is
``versionsDiffer``, and is never compared silently (ADR 0028 section 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from flux_engine.consumers import _allow
from flux_engine.consumers.check import SQLGLOT_VERSION, CheckedQuery, _parse
from flux_engine.consumers.errors import FLX_3715
from flux_engine.consumers.runner import CountRequest, QueryCounts
from flux_engine.consumers.schema import BundleSchema
from flux_engine.core.errors import FluxError

__all__ = [
    "Assessment",
    "ColumnFacts",
    "KINDS",
    "MOST_PER_QUERY",
    "MOST_PER_REGISTRY",
    "Requirement",
    "RequirementResult",
    "assess_requirements",
    "check_requirements",
    "column_origin",
    "derive_requirements",
    "like_matches",
    "requirement_requests",
]

KINDS: Tuple[str, ...] = ("level", "value", "pattern", "nulls", "range")

#: The most requirements one query may derive, and the most a registry may (``FLX-3715``).
MOST_PER_QUERY = 256
MOST_PER_REGISTRY = 10_000

_DUCKDB = "duckdb"
_DEPTH = 64
_COMPARE_OPS: Dict[type, str] = {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}
_FLIPPED = {">": "<", ">=": "<=", "<": ">", "<=": ">="}
_NUMBER = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")

#: The kinds of column the predicates read, from the DuckDB type the schema gives.
_STRING, _BOOL, _NUMERIC, _DATE, _TIMESTAMP = "string", "bool", "number", "date", "timestamp"


@dataclass(frozen=True)
class Requirement:
    """One thing a query needs the copy to hold, read from its text."""

    query: str
    expose: str
    column: str
    kind: str
    values: Tuple[str, ...] = ()

    def describe(self) -> str:
        """The requirement in words, for the report of ``init`` and ``verify``."""
        if self.kind == "level":
            return f"the level {self.values[0]!r}"
        if self.kind == "value":
            return f"the value {self.values[0]!r}"
        if self.kind == "pattern":
            return f"values {self.values[1].upper()} {self.values[0]!r}"
        if self.kind == "nulls":
            return "nulls"
        lower, upper = self.values
        return "the range " + " and ".join(repr(bound) for bound in (lower, upper) if bound)


# ---------------------------------------------------------------------------
# Deriving the requirements of a query
# ---------------------------------------------------------------------------


def _column_kind(db_type: str) -> Optional[str]:
    upper = db_type.upper()
    if upper == "VARCHAR":
        return _STRING
    if upper == "BOOLEAN":
        return _BOOL
    if upper == "BIGINT" or upper.startswith("DECIMAL"):
        return _NUMERIC
    if upper == "DATE":
        return _DATE
    if upper == "TIMESTAMP":
        return _TIMESTAMP
    return None


def _fold(name: str) -> str:
    return name.lower()


def _literal(node: Optional[exp.Expr]) -> Optional[Tuple[str, str]]:
    """``(kind, text)`` of a literal, a signed number, a boolean or a cast of one; else ``None``."""
    if isinstance(node, exp.Cast) and isinstance(node.this, (exp.Literal, exp.Neg)):
        return _literal(node.this)
    if isinstance(node, exp.Literal):
        return ("string" if node.is_string else "number", str(node.this))
    if isinstance(node, exp.Boolean):
        return ("boolean", "true" if node.this else "false")
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) and not node.this.is_string:
        return ("number", "-" + str(node.this.this))
    return None


def _select_at(scope: Scope, index: int) -> Optional[exp.Expr]:
    selects = getattr(scope.expression, "selects", None)
    if not selects or index >= len(selects):
        return None
    return selects[index]


def _branches(scope: Scope) -> List[Scope]:
    """The selects a set operation unions, left to right; a plain select is its own branch."""
    if not scope.set_operation_scopes:
        return [scope]
    return [leaf for child in scope.set_operation_scopes for leaf in _branches(child)]


def column_origin(
    column: exp.Column, scope: Scope, schema: BundleSchema, depth: int = 0
) -> Optional[Tuple[str, str]]:
    """The ``(exposeId, contract column)`` that *column* of *scope* is read from, or ``None``."""
    if depth > _DEPTH:
        return None
    walker: Optional[Scope] = scope
    source: Union[exp.Table, Scope, None] = None
    while walker is not None and source is None:
        source = walker.sources.get(str(column.table))
        walker = walker.parent
    if source is None:
        return None
    return _through(source, str(column.name), schema, depth)


def _through_table(source: exp.Table, name: str, schema: BundleSchema) -> Optional[Tuple[str, str]]:
    """The ``(exposeId, contract column)`` a column of an expose table is, or ``None``."""
    for expose in schema.exposes:
        if _fold(expose.expose_id) == _fold(str(source.name)):
            for written, _type in expose.columns:
                if _fold(written) == _fold(name):
                    return expose.expose_id, written
    return None


def _branch_origin(
    branch: Scope, index: int, schema: BundleSchema, depth: int
) -> Optional[Tuple[str, str]]:
    """Where the select at *index* of one set-operation branch reads from, or ``None``."""
    selected = _select_at(branch, index)
    inner = selected.unalias() if selected is not None else None
    if not isinstance(inner, exp.Column):
        return None
    return column_origin(inner, branch, schema, depth + 1)


def _through(
    source: Union[exp.Table, Scope], name: str, schema: BundleSchema, depth: int
) -> Optional[Tuple[str, str]]:
    if isinstance(source, exp.Table):
        return _through_table(source, name, schema)
    if not isinstance(source, Scope):
        return None
    branches = _branches(source)
    names = [_fold(str(each)) for each in getattr(branches[0].expression, "named_selects", [])]
    if _fold(name) not in names:
        return None
    index = names.index(_fold(name))
    found: Set[Tuple[str, str]] = set()
    for branch in branches:
        reached = _branch_origin(branch, index, schema, depth)
        if reached is None:
            return None
        found.add(reached)
    return found.pop() if len(found) == 1 else None


def _negated(node: exp.Expr) -> bool:
    """Whether *node* sits under a ``NOT`` within its own query."""
    parent = node.parent
    while parent is not None and not isinstance(parent, (exp.Query, exp.Subquery)):
        if isinstance(parent, exp.Not):
            return True
        parent = parent.parent
    return False


_Need = Tuple[exp.Column, str, Tuple[str, ...], str]


def _need_equal(node: exp.EQ) -> List[_Need]:
    for column, other in ((node.this, node.expression), (node.expression, node.this)):
        lit = _literal(other)
        if isinstance(column, exp.Column) and lit is not None:
            return [(column, "equal", (lit[1],), lit[0])]
    return []


def _need_in(node: exp.In) -> List[_Need]:
    found: List[_Need] = []
    if isinstance(node.this, exp.Column) and not node.args.get("query"):
        for member in node.expressions:
            lit = _literal(member)
            if lit is not None:
                found.append((node.this, "equal", (lit[1],), lit[0]))
    return found


def _need_like(node: exp.Expr) -> List[_Need]:
    if not isinstance(node.this, exp.Column):
        return []
    pattern = _literal(node.expression)
    if pattern is None or pattern[0] != "string":
        return []
    mode = "ilike" if isinstance(node, exp.ILike) else "like"
    escape: Tuple[str, ...] = ()
    if isinstance(node.parent, exp.Escape):
        written = _literal(node.parent.expression)
        escape = (written[1],) if written is not None else ()
    return [(node.this, "pattern", (pattern[1], mode, *escape), "string")]


def _need_between(node: exp.Between) -> List[_Need]:
    if not isinstance(node.this, exp.Column):
        return []
    low, high = _literal(node.args.get("low")), _literal(node.args.get("high"))
    if low is None or high is None:
        return []
    return [(node.this, "range", (">=" + low[1], "<=" + high[1]), low[0])]


def _need_compare(node: exp.Expr) -> List[_Need]:
    symbol = _COMPARE_OPS[type(node)]
    lit = _literal(node.expression)
    if isinstance(node.this, exp.Column) and lit is not None:
        return [(node.this, "range", _one_sided(symbol, lit[1]), lit[0])]
    lit = _literal(node.this)
    if isinstance(node.expression, exp.Column) and lit is not None:
        return [(node.expression, "range", _one_sided(_FLIPPED[symbol], lit[1]), lit[0])]
    return []


def _need(node: exp.Expr) -> List[_Need]:
    """The predicates *node* is: ``(column, kind, values, literal kind)`` per requirement."""
    if isinstance(node, exp.EQ):
        return _need_equal(node)
    if isinstance(node, exp.In):
        return _need_in(node)
    if isinstance(node, (exp.Like, exp.ILike)):
        return _need_like(node)
    if isinstance(node, exp.Is):
        if isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Null):
            return [(node.this, "nulls", (), "")]
        return []
    if isinstance(node, exp.Between):
        return _need_between(node)
    if isinstance(node, tuple(_COMPARE_OPS)):
        return _need_compare(node)
    return []


def _one_sided(symbol: str, text: str) -> Tuple[str, str]:
    return (symbol + text, "") if symbol.startswith(">") else ("", symbol + text)


def _equal_kind(kind: str, literal_kind: str) -> Optional[str]:
    """The requirement kind an equality on a column of *kind* with this literal makes, or ``None``."""
    if kind == _STRING and literal_kind == "string":
        return "level"
    if kind == _BOOL and literal_kind == "boolean":
        return "level"
    if kind in (_NUMERIC, _DATE, _TIMESTAMP) and literal_kind != "boolean":
        return "value"
    return None


def _requirement(
    query: str,
    origin: Tuple[str, str],
    db_type: str,
    shape: str,
    values: Tuple[str, ...],
    literal_kind: str,
) -> Optional[Requirement]:
    kind = _column_kind(db_type)
    expose, column = origin
    if shape == "nulls":
        return Requirement(query, expose, column, "nulls")
    if kind is None:
        return None
    made: Optional[str] = None
    if shape == "equal":
        made = _equal_kind(kind, literal_kind)
    elif shape == "pattern":
        made = "pattern" if kind == _STRING else None
    elif shape == "range" and kind in (_NUMERIC, _DATE, _TIMESTAMP) and literal_kind != "boolean":
        made = "range"
    return Requirement(query, expose, column, made, values) if made is not None else None


def _bound_query(query: str, found: Set[Requirement]) -> None:
    if len(found) > MOST_PER_QUERY:
        raise FluxError(
            FLX_3715,
            f"query {query!r} needs more than {MOST_PER_QUERY} requirements of the copy; "
            f"shorten its IN list or split it",
        )


def _scope_requirements(
    query: CheckedQuery, scope: Scope, schema: BundleSchema, into: Set[Requirement]
) -> None:
    """Add to *into* the requirements of the predicates in one scope of *query*."""
    for node in scope.walk():
        if not isinstance(node, exp.Expr) or _negated(node):
            continue
        for column, shape, values, literal_kind in _need(node):
            origin = column_origin(column, scope, schema)
            if origin is None:
                continue
            expose = schema.expose(origin[0])
            assert expose is not None
            db_type = dict(expose.columns)[origin[1]]
            made = _requirement(query.id, origin, db_type, shape, values, literal_kind)
            if made is not None:
                into.add(made)
                _bound_query(query.id, into)


def derive_requirements(
    queries: Iterable[CheckedQuery], schema: BundleSchema
) -> Tuple[Requirement, ...]:
    """The requirements of *queries*, calibration and held-out alike, in query order.

    Within one query they are sorted and none is repeated. A column that does not resolve to one
    column of one expose (an expression, a set operation whose branches read different columns) gives
    none.
    """
    out: List[Requirement] = []
    for query in queries:
        root = build_scope(query.tree)
        if root is None:
            continue
        mine: Set[Requirement] = set()
        for scope in root.traverse():
            _scope_requirements(query, scope, schema, mine)
        out.extend(sorted(mine, key=_order))
        if len(out) > MOST_PER_REGISTRY:
            raise FluxError(
                FLX_3715,
                f"the queries up to {query.id!r} need more than {MOST_PER_REGISTRY} requirements "
                f"of the copy between them",
            )
    return tuple(out)


def _order(item: Requirement) -> Tuple[str, str, str, str, Tuple[str, ...]]:
    return (item.query, item.expose, item.column, item.kind, item.values)


# ---------------------------------------------------------------------------
# LIKE
# ---------------------------------------------------------------------------


def _like_tokens(pattern: str, escape: Optional[str]) -> List[Tuple[str, str]]:
    """The LIKE *pattern* as ``(kind, character)`` tokens: ``any``, ``one`` or ``literal``."""
    tokens: List[Tuple[str, str]] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if escape and char == escape and index + 1 < len(pattern):
            tokens.append(("literal", pattern[index + 1]))
            index += 2
            continue
        tokens.append(
            ("any", "") if char == "%" else ("one", "") if char == "_" else ("literal", char)
        )
        index += 1
    return tokens


def like_matches(
    pattern: str, text: str, *, ignore_case: bool = False, escape: Optional[str] = None
) -> bool:
    """Whether *text* matches the LIKE *pattern*: ``%`` any run, ``_`` one character.

    *escape*, one character, makes the character after it literal; a trailing escape is itself.
    ``ILIKE`` (*ignore_case*) compares without regard to case. The match tracks the
    positions of the text reached so far, token by token, so a pattern of many ``%`` cannot take
    exponential time.
    """
    if ignore_case:
        pattern, text = pattern.casefold(), text.casefold()
        escape = escape.casefold() if escape else escape
    reachable = {0}
    for kind, char in _like_tokens(pattern, escape):
        if kind == "any":
            reachable = set(range(min(reachable), len(text) + 1))
        else:
            reachable = {
                start + 1
                for start in reachable
                if start < len(text) and (kind == "one" or text[start] == char)
            }
        if not reachable:
            return False
    return len(text) in reachable


# ---------------------------------------------------------------------------
# Deciding at init
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnFacts:
    """What the declaration says of one column: all that :func:`assess_requirements` reads."""

    #: ``string``, ``bool``, ``number``, ``date`` or ``timestamp``.
    kind: str = _STRING
    #: The levels the column draws, or ``None`` when it is not drawn from named levels
    #: (an identifier, opaque text, a number or a date).
    levels: Optional[Tuple[str, ...]] = None
    #: The contract's ``enum(...)`` levels, or ``None``.
    enum: Optional[Tuple[str, ...]] = None
    #: The values a number, date or timestamp column draws one of (its declared levels).
    values: Tuple[str, ...] = ()
    #: The column draws nulls: a declared null fraction above zero, or a profile null count above zero.
    draws_nulls: bool = False
    #: The declared range ``(low, high)``, or ``None``.
    range: Optional[Tuple[str, str]] = None


@dataclass(frozen=True)
class Assessment:
    """A requirement and what ``init`` makes of it."""

    requirement: Requirement
    #: ``met``, ``unmet``, or ``verify`` for a shape only a run can show.
    status: str
    note: str


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ordinal(kind: str, text: str) -> Optional[Decimal]:
    """*text* as a number, or an instant as microseconds since the epoch (UTC).

    So two spellings of one value compare equal, and a date and a timestamp compare with each other.
    """
    try:
        if kind == _NUMERIC:
            return Decimal(text)
        if kind in (_DATE, _TIMESTAMP):
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
                day = date.fromisoformat(text)
                instant = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
            else:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T"))
                instant = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            return Decimal((instant - _EPOCH) // timedelta(microseconds=1))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return None


def _same_value(kind: str, left: str, right: str) -> bool:
    if left == right:
        return True
    a, b = _ordinal(kind, left), _ordinal(kind, right)
    return a is not None and b is not None and a == b


def _bound(text: str) -> Tuple[str, str]:
    for symbol in (">=", "<=", ">", "<"):
        if text.startswith(symbol):
            return symbol, text[len(symbol) :]
    return "", text


def _bound_met(
    kind: str, bound: str, other: Decimal, *, strict: str, above: bool
) -> Optional[bool]:
    """Whether *other* (the span's high end for a lower bound, its low end for an upper bound) meets *bound*."""
    symbol, text = _bound(bound)
    wanted = _ordinal(kind, text)
    if wanted is None:
        return None
    if above:
        return other > wanted if symbol == strict else other >= wanted
    return other < wanted if symbol == strict else other <= wanted


def _range_met(kind: str, bounds: Tuple[str, str], span: Tuple[str, str]) -> Optional[bool]:
    """Whether a column that holds ``span`` (low, high) has values the bounds ask for."""
    low, high = _ordinal(kind, span[0]), _ordinal(kind, span[1])
    if low is None or high is None:
        return None
    lower, upper = bounds
    if lower:
        met = _bound_met(kind, lower, high, strict=">", above=True)
        if met is not True:
            return met
    if upper:
        met = _bound_met(kind, upper, low, strict="<", above=False)
        if met is not True:
            return met
    return True


def assess_requirements(
    requirements: Iterable[Requirement],
    facts: Mapping[Tuple[str, str], ColumnFacts],
    *,
    sentinels: Optional[Mapping[Tuple[str, str], Sequence[str]]] = None,
    window: Optional[Tuple[str, str]] = None,
) -> Tuple[Assessment, ...]:
    """Decide each requirement against what the declaration carries.

    *facts* is keyed by ``(exposeId, column)``; *sentinels* gives the texts a link writes in a
    column in place of a missing parent (``links[].sentinels``); *window* is the run's time window,
    the span a date or timestamp column with no declared range is drawn over.
    """
    sentinels = sentinels or {}
    out: List[Assessment] = []
    for requirement in requirements:
        key = (requirement.expose, requirement.column)
        known = facts.get(key)
        if known is None:
            out.append(
                Assessment(requirement, "unmet", "no declaration draws this column in the bundle")
            )
            continue
        status, note = _decide(requirement, known, tuple(sentinels.get(key, ())), window)
        out.append(Assessment(requirement, status, note))
    return tuple(out)


def _decide(
    requirement: Requirement,
    facts: ColumnFacts,
    sentinels: Tuple[str, ...],
    window: Optional[Tuple[str, str]],
) -> Tuple[str, str]:
    kind = requirement.kind
    if kind == "level":
        return _decide_level(requirement.values[0], facts)
    if kind == "pattern":
        return _decide_pattern(requirement.values, facts)
    if kind == "nulls":
        if facts.draws_nulls:
            return "met", "the column draws nulls"
        return "unmet", "the column draws no nulls: declare a null fraction above zero"
    if kind == "value":
        return _decide_value(requirement.values[0], facts, sentinels)
    return _decide_range((requirement.values[0], requirement.values[1]), facts, window)


def _decide_level(level: str, facts: ColumnFacts) -> Tuple[str, str]:
    if facts.enum is not None and level not in facts.enum:
        return "unmet", "the contract's enum does not list the level, so no row may hold it"
    if facts.levels is None:
        return (
            "unmet",
            "the column is not drawn from named levels, so no declaration draws this one",
        )
    if level in facts.levels:
        return "met", "the column declares the level"
    return "unmet", "the column's declared levels do not include it"


def _decide_pattern(values: Tuple[str, ...], facts: ColumnFacts) -> Tuple[str, str]:
    pattern, mode = values[0], values[1]
    escape = values[2] if len(values) > 2 else None
    if facts.levels is None:
        return "verify", "the column is an identifier or text: its values are checked on a run"
    for level in facts.levels:
        if like_matches(pattern, level, ignore_case=mode == "ilike", escape=escape):
            return "met", f"the declared level {level!r} matches"
    return "unmet", "no declared level of the column matches"


def _decide_value(value: str, facts: ColumnFacts, sentinels: Tuple[str, ...]) -> Tuple[str, str]:
    if any(_same_value(facts.kind, value, sentinel) for sentinel in sentinels):
        return "met", "a link writes it as the sentinel of a missing parent"
    candidates = facts.values + (facts.levels or ())
    if any(_same_value(facts.kind, value, declared) for declared in candidates):
        return "met", "the column declares the value"
    return (
        "unmet",
        "no declaration produces it: a link's sentinel (links[].sentinels) on this column, or a "
        "declared value of the column",
    )


def _decide_range(
    bounds: Tuple[str, str], facts: ColumnFacts, window: Optional[Tuple[str, str]]
) -> Tuple[str, str]:
    span = facts.range
    source = "the column's declared range"
    if span is None and facts.kind in (_DATE, _TIMESTAMP) and window is not None:
        span, source = window, "the run's time window"
    if span is None:
        return "unmet", "the column declares no range and no time window covers it"
    met = _range_met(facts.kind, bounds, span)
    if met is None:
        return "unmet", f"{source} cannot be compared with the bound"
    return ("met", f"{source} meets it") if met else ("unmet", f"{source} does not meet it")


# ---------------------------------------------------------------------------
# Checking on a run
# ---------------------------------------------------------------------------


def _name(identifier: str) -> exp.Identifier:
    return exp.Identifier(this=identifier, quoted=True)


def _column_node(name: str) -> exp.Column:
    return exp.Column(this=_name(name))


def _bound_literal(text: str, db_type: str) -> exp.Expr:
    kind = _column_kind(db_type)
    if kind in (_DATE, _TIMESTAMP, _NUMERIC) and not _NUMBER.fullmatch(text):
        return exp.Cast(
            this=exp.Literal.string(text), to=exp.DataType.build(db_type, dialect=_DUCKDB)
        )
    if _NUMBER.fullmatch(text) and kind in (_NUMERIC, None):
        return (
            exp.Literal.number(text)
            if not text.startswith("-")
            else exp.Neg(this=exp.Literal.number(text[1:]))
        )
    return exp.Literal.string(text)


def _predicate(requirement: Requirement, db_type: str) -> exp.Expr:
    column = _column_node(requirement.column)
    kind = requirement.kind
    if kind == "level":
        text = requirement.values[0]
        if _column_kind(db_type) == _BOOL:
            return exp.EQ(this=column, expression=exp.Boolean(this=text == "true"))
        return exp.EQ(this=column, expression=exp.Literal.string(text))
    if kind == "value":
        return exp.EQ(this=column, expression=_bound_literal(requirement.values[0], db_type))
    if kind == "pattern":
        pattern = exp.Literal.string(requirement.values[0])
        node: exp.Expr = (
            exp.ILike(this=column, expression=pattern)
            if requirement.values[1] == "ilike"
            else exp.Like(this=column, expression=pattern)
        )
        if len(requirement.values) > 2:
            node = exp.Escape(this=node, expression=exp.Literal.string(requirement.values[2]))
        return node
    if kind == "nulls":
        return exp.Is(this=column, expression=exp.Null())
    classes = {">": exp.GT, ">=": exp.GTE, "<": exp.LT, "<=": exp.LTE}
    parts: List[exp.Expr] = []
    for bound in requirement.values:
        if bound:
            symbol, text = _bound(bound)
            parts.append(
                classes[symbol](this=column.copy(), expression=_bound_literal(text, db_type))
            )
    return parts[0] if len(parts) == 1 else exp.And(this=parts[0], expression=parts[1])


def _text_of(requirement: Requirement, db_type: str, label: str) -> Tuple[str, str]:
    """The canonical text of the rows meeting *requirement*, and of all rows of its expose."""
    table = exp.Table(this=_name(requirement.expose))
    everything = exp.Select(expressions=[exp.Star()], from_=exp.From(this=table.copy()))
    chosen = everything.copy().where(_predicate(requirement, db_type))
    texts = (
        chosen.sql(dialect=_DUCKDB, comments=False),
        everything.sql(dialect=_DUCKDB, comments=False),
    )
    for text in texts:
        again = _parse(text, _DUCKDB, label)
        _allow.vet_tree(again, label=label, caps=_allow.CANONICAL_CAPS, canonical=True)
    return texts


def requirement_requests(
    requirements: Sequence[Requirement], schema: BundleSchema
) -> Tuple[CountRequest, ...]:
    """One count per requirement: the rows that meet it, and, as its unit, every row of the expose.

    A request's ``rows`` is how many rows meet the requirement, so it is met when that is above
    zero. The ids are ``requirement-1``, ``requirement-2``, in the order of *requirements*.
    """
    requests: List[CountRequest] = []
    for position, requirement in enumerate(requirements, start=1):
        expose = schema.expose(requirement.expose)
        types: Dict[str, str] = dict(expose.columns) if expose is not None else {}
        text, unit = _text_of(
            requirement, types.get(requirement.column, ""), f"requirement {position}"
        )
        requests.append(
            CountRequest(
                id=f"requirement-{position}",
                text=text,
                unit_text=unit,
                sqlglot_version=SQLGLOT_VERSION,
            )
        )
    return tuple(requests)


@dataclass(frozen=True)
class RequirementResult:
    """One requirement on a run: its decision, and the rows that met it when they were counted."""

    requirement: Requirement
    #: ``met``, ``unmet``, ``versionsDiffer``, or the status the count took when it did not finish
    #: (``timedOut``, ``outOfMemory``, ``error``, ``crashed``).
    status: str
    rows: Optional[int] = None


def check_requirements(
    requirements: Sequence[Requirement],
    counts: Optional[Sequence[QueryCounts]] = None,
    *,
    recorded_sqlglot: Optional[str] = None,
    current_sqlglot: str = SQLGLOT_VERSION,
) -> Tuple[RequirementResult, ...]:
    """Decide each requirement from the counts of :func:`requirement_requests`, in the same order.

    A requirement the bundle recorded as compiled under another sqlglot version
    (*recorded_sqlglot*, the registry's ``compiledWith``) than the one running now
    (*current_sqlglot*) is ``versionsDiffer``, whatever the counts say, since another version may
    write its text another way; no count is needed for it. With no recorded version, the
    requirements are the ones this version derived and are compared.
    """
    if recorded_sqlglot is not None and recorded_sqlglot != current_sqlglot:
        return tuple(RequirementResult(each, "versionsDiffer") for each in requirements)
    if counts is None or len(counts) != len(requirements):
        raise ValueError("check_requirements needs one count per requirement")
    out: List[RequirementResult] = []
    for requirement, counted in zip(requirements, counts, strict=True):
        if counted.status != "counted" or counted.rows is None:
            out.append(RequirementResult(requirement, counted.status))
        else:
            out.append(
                RequirementResult(requirement, "met" if counted.rows > 0 else "unmet", counted.rows)
            )
    return tuple(out)
