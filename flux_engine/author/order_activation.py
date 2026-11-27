# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The activation column a contract's date order asks a history for (ADR 0024, ADR 0017 section 5).

A contract can order two dates of a table that a history sets, such as a membership that ends on or
after the day it began (``ended_on IS NULL OR ended_on >= began_on``), where the end is the start of
a version (``CASE WHEN status = 'ENDED' THEN valid_from END``) and the beginning is a plain column of
the versions. Drawn apart, the beginning falls after some of the starts, and the check breaks on a
share of the rows. A history holds the order when it keeps that column with the entity's first start
(its ``activation``): every version starts on or after the first start, so every end that is the
start or the end of a version is on or after the beginning.

This module reads the contract only. It finds the order and says whether the history can keep it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, List, Mapping, Optional, Tuple

from flux_engine.author.orders import DateOrder
from flux_engine.core.canonical import nfc
from flux_engine.expr2.ast import Case, Column, Node, Null
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.parser import Side, parse

__all__ = ["OrderedBy", "ordered_activation"]


@dataclass(frozen=True)
class OrderedBy:
    """A date order of a table whose rows are a history's versions, or its snapshot."""

    #: The table the contract declares the check on.
    expose: str
    order: DateOrder
    #: Whether the earlier date may be NULL on a row of that table: the contract allows it and the
    #: profile publishes a count of NULLs that is not zero.
    nulls: bool = False


def _values(node: Node) -> Optional[FrozenSet[str]]:
    """The columns an expression can take its value from, or ``None`` when it takes it from anything else.

    A column is itself, ``NULL`` is no column, and a ``CASE`` is every result and its ``ELSE``. A
    ``CASE`` with no ``ELSE`` is NULL where no branch holds.
    """
    if isinstance(node, Column):
        return frozenset({nfc(node.name)}) if node.relationship is None else None
    if isinstance(node, Null):
        return frozenset()
    if isinstance(node, Case):
        found: List[FrozenSet[str]] = []
        for _, result in node.whens:
            one = _values(result)
            if one is None:
                return None
            found.append(one)
        if node.otherwise is not None:
            other = _values(node.otherwise)
            if other is None:
                return None
            found.append(other)
        return frozenset().union(*found)
    return None


def _derived_from(contract_expose: Mapping[str, Any], column: str) -> Optional[FrozenSet[str]]:
    """The columns a ``derive`` rule of the expose takes *column* from, or ``None`` when it has none or reads more."""
    contract = contract_expose.get("contract")
    rules = contract.get("invariants") if isinstance(contract, Mapping) else None
    found: Optional[FrozenSet[str]] = None
    for rule in rules if isinstance(rules, list) else ():
        if not isinstance(rule, Mapping) or rule.get("derive") != column:
            continue
        text = rule.get("expr")
        if "when" in rule or not isinstance(text, str) or found is not None:
            return None
        try:
            found = _values(parse(text, side=Side.CORE).tree)
        except Expr2Error:
            return None
        if found is None:
            return None
    return found


def ordered_activation(
    contract_expose: Mapping[str, Any],
    orders: Tuple[OrderedBy, ...],
    start: str,
    end: str,
) -> Optional[Tuple[str, OrderedBy, Tuple[OrderedBy, ...]]]:
    """The column, the order that makes a history keep its activation, and the other orders that qualify, or ``None``.

    *orders* are the orders of the history's own table and of its snapshots. An order qualifies when
    it is not strict and its later date is, on every row, the start or the end of a version (the
    column is a validity column, or a ``derive`` rule takes it only from those and NULL), and its
    earlier date is no validity column. The first by rule id, then column, is taken.
    """
    window = {nfc(start), nfc(end)}
    found: List[Tuple[str, str, str, OrderedBy]] = []
    for item in orders:
        order = item.order
        if order.strict or order.start in window:
            continue
        reads = (
            frozenset({order.end})
            if order.end in window
            else _derived_from(contract_expose, order.end)
        )
        if reads and reads <= window:
            found.append((order.rule, order.start, item.expose, item))
    if not found:
        return None
    found.sort(key=lambda one: one[:3])
    best = found[0]
    return best[1], best[3], tuple(one[3] for one in found[1:] if one[1] != best[1])
