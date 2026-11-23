# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What the profile request asks about the consumers' queries (ADR 0028 section 6).

Given the checked registry, this module reads off the three things a request carries for the
consumers: the calibration queries that have a unit (``queries``), the held-out ones (for the
held-out request, never in the main one), and the columns each calibration query reads together
(``reads``, from which :func:`flux_engine.seam.profile_request.request_for_contract` builds each
table's workload). A query's text is the consumers' question: its literals come from the consumers,
never from the data, and nothing here reads a value of the data.

A column is *read* by a query when it sits in a predicate (a comparison, ``IN``, ``LIKE``,
``BETWEEN``, ``IS NULL``, or a bare boolean column of a ``WHERE``, ``HAVING`` or join condition) or in
a ``GROUP BY``, or is the query's ``groupBy``. Each is resolved to the column of an expose through
aliases, ``WITH`` entries, views and derived tables, as :mod:`flux_engine.consumers.requirements`
resolves a requirement's column.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Set, Tuple

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from flux_engine.consumers.check import CheckedQuery, RegistryCheck
from flux_engine.consumers.requirements import column_origin
from flux_engine.consumers.schema import BundleSchema
from flux_engine.seam.consumers import ConsumerRegistry
from flux_stats.request import RequestQuery, RequestUnit

__all__ = ["ConsumerAsk", "build_ask"]

_PREDICATES = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.In,
    exp.Like,
    exp.ILike,
    exp.Is,
    exp.Between,
)


@dataclass(frozen=True)
class ConsumerAsk:
    """The consumers' part of a profile request."""

    #: The calibration queries that have a unit, in the registry's order.
    queries: Tuple[RequestQuery, ...] = ()
    #: The held-out queries that have a unit, in the registry's order.
    held_out: Tuple[RequestQuery, ...] = ()
    #: The exposes the held-out queries' units read, sorted: the tables of the held-out request.
    held_out_tables: Tuple[str, ...] = ()
    #: For each calibration query, ``(exposeId, columns read together)`` per expose it reads.
    reads: Tuple[Tuple[str, FrozenSet[str]], ...] = ()


def _node_columns(node: exp.Expr) -> List[exp.Expr]:
    """The columns of one node of a scope that count as read: in a predicate, a grouping or a filter."""
    if isinstance(node, _PREDICATES):
        return list(_own_columns(node))
    if isinstance(node, exp.Group):
        return [column for expression in node.expressions for column in _own_columns(expression)]
    if isinstance(node, exp.Column) and _is_condition(node):
        return [node]
    return []


def _grouped_selects(query: CheckedQuery, root: Scope) -> List[exp.Expr]:
    """The selects of the root scope that the query's ``group_by`` names."""
    if query.group_by is None:
        return []
    wanted = query.group_by.lower()
    return [
        select
        for select in getattr(root.expression, "selects", [])
        if select.alias_or_name.lower() == wanted
    ]


def _columns_read(query: CheckedQuery, schema: BundleSchema) -> Dict[str, Set[str]]:
    """The columns *query* reads in predicates and groupings, per expose."""
    found: Dict[str, Set[str]] = {}
    root = build_scope(query.tree)
    if root is None:
        return found

    def note(column: exp.Expr, scope: Scope) -> None:
        if isinstance(column, exp.Column):
            origin = column_origin(column, scope, schema)
            if origin is not None:
                found.setdefault(origin[0], set()).add(origin[1])

    for scope in root.traverse():
        for node in scope.walk():
            for column in _node_columns(node):
                note(column, scope)
    for select in _grouped_selects(query, root):
        note(select.unalias(), root)
    return found


def _is_condition(column: exp.Column) -> bool:
    """Whether *column* is itself a condition: a bare boolean column of a filter or a join."""
    parent = column.parent
    if isinstance(parent, (exp.And, exp.Or, exp.Not, exp.Where, exp.Having)):
        return True
    return isinstance(parent, exp.Join) and parent.args.get("on") is column


def _own_columns(node: exp.Expr) -> List[exp.Column]:
    """The columns of *node* that belong to its own select, not to a subquery inside it."""
    own: List[exp.Column] = []
    for column in node.find_all(exp.Column):
        parent = column.parent
        while parent is not None and parent is not node:
            if isinstance(parent, (exp.Subquery, exp.Select)):
                break
            parent = parent.parent
        else:
            own.append(column)
    return own


def build_ask(
    registry: ConsumerRegistry, checked: RegistryCheck, schema: BundleSchema
) -> ConsumerAsk:
    """The queries, the held-out queries and the column sets the registry puts in a request."""
    held_by_id = {query.id: query.held_out for query in registry.queries}
    views = {view.name: view for view in checked.views}
    queries: List[RequestQuery] = []
    held_out: List[RequestQuery] = []
    held_tables: Set[str] = set()
    reads: List[Tuple[str, FrozenSet[str]]] = []
    for query in checked.queries:
        is_held_out = held_by_id[query.id]
        if not is_held_out:
            for expose, columns in sorted(_columns_read(query, schema).items()):
                reads.append((expose, frozenset(columns)))
        if query.unit is None or query.unit_text is None:
            continue
        if query.unit in views:
            unit = RequestUnit(sql=query.unit_text)
            unit_exposes = views[query.unit].exposes
        else:
            unit = RequestUnit(table=query.unit)
            unit_exposes = (query.unit,)
        entry = RequestQuery(name=query.id, sql=query.text, unit=unit, group_by=query.group_by)
        if is_held_out:
            held_out.append(entry)
            held_tables.update(unit_exposes)
        else:
            queries.append(entry)
    return ConsumerAsk(
        queries=tuple(queries),
        held_out=tuple(held_out),
        held_out_tables=tuple(sorted(held_tables)),
        reads=tuple(reads),
    )
