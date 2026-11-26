# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The order a contract's ``check`` declares between two date columns of one row (ADR 0024 section 10).

A check such as ``cancelled_on IS NULL OR cancelled_on >= issued_on`` says that a row's second date
is not before its first. The check is read from the contract's syntax tree (the engine's own
``flux-expr/2`` parser), never from its text, and only these shapes count:

* ``b >= a``, ``b > a``, ``a <= b`` and ``a < b``, with ``a`` and ``b`` columns of the row itself;
* any of them behind ``b IS NULL OR`` (a row with no end is not out of order) or ``a IS NULL OR``.

Anything else (a ``when``, a third column, a path through a link, a function) is no order here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

from flux_engine.expr2.ast import Column, Compare, IsNull, Node, Or
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.parser import Side, parse

__all__ = ["DateOrder", "contract_date_orders"]


@dataclass(frozen=True)
class DateOrder:
    """A check that the date *end* is not before (or, when *strict*, is after) the date *start*."""

    rule: str
    start: str
    end: str
    strict: bool
    #: Whether the check holds on a row whose end, or whose start, is NULL.
    null_end_holds: bool
    null_start_holds: bool


def contract_date_orders(expose: Mapping[str, Any]) -> List[DateOrder]:
    """The orders the ``check`` invariants of *expose*'s contract declare, in declared order."""
    contract = expose.get("contract")
    found = contract.get("invariants") if isinstance(contract, Mapping) else None
    orders: List[DateOrder] = []
    for entry in found if isinstance(found, list) else ():
        if not isinstance(entry, Mapping) or "when" in entry:
            continue
        text, rule = entry.get("check"), entry.get("id")
        if not isinstance(text, str) or not isinstance(rule, str):
            continue
        try:
            tree = parse(text, side=Side.CORE).tree
        except Expr2Error:
            continue
        order = _order(rule, tree)
        if order is not None:
            orders.append(order)
    return orders


def _disjuncts(tree: Node) -> List[Node]:
    """The operands of a chain of ORs, left to right, however the parser nested them."""
    found: List[Node] = []
    pending = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, Or):
            pending.extend((node.right, node.left))
        else:
            found.append(node)
    return found


def _order(rule: str, tree: Node) -> Optional[DateOrder]:
    *guards, tree = _disjuncts(tree)
    nulls: List[str] = []
    for guard in guards:
        name = _name(guard.value) if isinstance(guard, IsNull) and not guard.negated else None
        if name is None:
            return None
        nulls.append(name)
    pair = _compare(tree)
    if pair is None:
        return None
    start, end, strict = pair
    if len(set(nulls)) != len(nulls) or any(name not in (start, end) for name in nulls):
        return None
    return DateOrder(
        rule=rule,
        start=start,
        end=end,
        strict=strict,
        null_end_holds=end in nulls,
        null_start_holds=start in nulls,
    )


def _compare(tree: Node) -> Optional[Tuple[str, str, bool]]:
    """``(start, end, strict)`` of a comparison of two columns, or ``None``."""
    if not isinstance(tree, Compare):
        return None
    left, right = _name(tree.left), _name(tree.right)
    if left is None or right is None or left == right:
        return None
    if tree.op in (">=", ">"):
        return right, left, tree.op == ">"
    if tree.op in ("<=", "<"):
        return left, right, tree.op == "<"
    return None


def _name(node: Node) -> Optional[str]:
    if isinstance(node, Column) and node.relationship is None:
        return node.name
    return None
