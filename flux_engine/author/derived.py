# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Columns a contract ``derive`` sets: no draw, and a range only from the rule's exact bounds.

ADR 0014 section 6. A row-local ``derive`` with no ``when`` determines its
column, so the column is never drawn and never declares the range its own
profile publishes: a property over it would then refuse the run for every value
the rule writes outside that range (``FLX-5005``). Its range, when a property
needs one, comes from :func:`bounds`, an interval in exact fractions evaluated
over the rule's syntax tree with the ranges of the columns it reads: the sum,
difference and product of intervals have exact interval results, a quotient
by an interval that keeps away from zero too, and ``ROUND`` is monotone, so its
bounds are the rounded ends. An expression the interval cannot follow gives no
range. The column's own published statistics are then checked through the
rule: they become the properties ``verify`` scores against what it wrote.

The interval is the textbook one (R. E. Moore, "Interval Analysis", 1966), over
the engine's own tree, in :class:`~fractions.Fraction`: nothing here rounds, and
nothing is a float.

A date is its day ordinal (``date.toordinal``, 1 for 0001-01-01), so a date
column's range is an interval of ordinals. ``ADD_DAYS``, ``ADD_MONTHS`` and
``ADD_YEARS`` are monotone non-decreasing in the date and in the count, so their
bounds are the function at the two low ends and at the two high ends; a NULL
there (the result leaves 0001 to 9999) means no bound.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from fractions import Fraction
from math import ceil, floor
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.expr2.ast import (
    Arith,
    Call,
    Case,
    Column,
    Date,
    DateAdd,
    Neg,
    Node,
    Number,
)
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.functions import (
    HALF_AWAY_FROM_ZERO,
    add_days,
    add_years,
    round_exact,
    try_add_months,
)
from flux_engine.expr2.parser import Side, parse
from flux_stats.spelling import MOST_DIGITS

__all__ = ["Interval", "bounds", "contract_dependents", "contract_derives", "numeric"]

Interval = Tuple[Fraction, Fraction]


def contract_derives(expose: Mapping[str, Any]) -> Dict[str, Tuple[int, str]]:
    """The columns a ``derive`` of *expose*'s contract determines: column -> (index, expr).

    Only a ``derive`` with an ``expr`` and no ``when``: a guarded one leaves the
    column drawn on the rows its guard does not hold, so the column is planned
    like any other (EXPR-SPEC-2 section 9).
    """
    contract = expose.get("contract")
    found = contract.get("invariants") if isinstance(contract, Mapping) else None
    derives: Dict[str, Tuple[int, str]] = {}
    for index, entry in enumerate(found if isinstance(found, list) else ()):
        if not isinstance(entry, Mapping) or "when" in entry or "aggregate" in entry:
            continue
        column, text = entry.get("derive"), entry.get("expr")
        if isinstance(column, str) and isinstance(text, str):
            derives.setdefault(column, (index, text))
    return derives


def contract_dependents(expose: Mapping[str, Any]) -> Set[str]:
    """The columns a contract ``dependency`` of *expose* sets (EXPR-SPEC-2 section 8).

    A row takes the value its ``from`` columns' values already have, so the run
    draws such a column once per value of the columns it follows.
    """
    contract = expose.get("contract")
    found = contract.get("invariants") if isinstance(contract, Mapping) else None
    dependents: Set[str] = set()
    for entry in found if isinstance(found, list) else ():
        rule = entry.get("dependency") if isinstance(entry, Mapping) else None
        targets = rule.get("to") if isinstance(rule, Mapping) else None
        for name in targets if isinstance(targets, list) else ():
            if isinstance(name, str):
                dependents.add(name)
    return dependents


def _hull(intervals: Sequence[Optional[Interval]]) -> Optional[Interval]:
    if not intervals or any(interval is None for interval in intervals):
        return None
    lows = [interval[0] for interval in intervals if interval is not None]
    highs = [interval[1] for interval in intervals if interval is not None]
    return min(lows), max(highs)


def _arith(op: str, left: Interval, right: Interval) -> Optional[Interval]:
    (a, b), (c, d) = left, right
    if op == "+":
        return a + c, b + d
    if op == "-":
        return a - d, b - c
    if op == "*":
        products = (a * c, a * d, b * c, b * d)
        return min(products), max(products)
    if op == "/" and (c > 0 or d < 0):
        quotients = (a / c, a / d, b / c, b / d)
        return min(quotients), max(quotients)
    return None


#: The widest scale a DECIMAL column declares: the digits a bound may need after its point.
_WIDEST_SCALE = 38

#: The most bits an end of an interval may carry. A chain of products doubles them at
#: every link, so past this the rule is given no bound rather than one the bundle could
#: not write: ``2**MOST_BITS`` has no more digits than a fidelity block's number may
#: (:data:`flux_stats.spelling.MOST_DIGITS`), less a DECIMAL's widest scale.
MOST_BITS = math.floor((MOST_DIGITS - _WIDEST_SCALE) * math.log2(10))


def _bounded(interval: Optional[Interval]) -> Optional[Interval]:
    if interval is None:
        return None
    for end in interval:
        if max(end.numerator.bit_length(), end.denominator.bit_length()) > MOST_BITS:
            return None
    return interval


def _node_bounds(node: Node, ranges: Mapping[str, Interval]) -> Optional[Interval]:
    return _bounded(_unbounded(node, ranges))


def _unbounded(node: Node, ranges: Mapping[str, Interval]) -> Optional[Interval]:
    if isinstance(node, Number):
        return node.value, node.value
    if isinstance(node, Column):
        return ranges.get(node.name) if node.relationship is None else None
    if isinstance(node, Neg):
        inner = _node_bounds(node.operand, ranges)
        return None if inner is None else (-inner[1], -inner[0])
    if isinstance(node, Arith):
        left, right = _node_bounds(node.left, ranges), _node_bounds(node.right, ranges)
        return None if left is None or right is None else _arith(node.op, left, right)
    if isinstance(node, Case):
        branches = [then for _, then in node.whens]
        if node.otherwise is not None:
            branches.append(node.otherwise)
        return _hull([_node_bounds(branch, ranges) for branch in branches])
    if isinstance(node, Date):
        day = Fraction(date.fromisoformat(node.value).toordinal())
        return day, day
    if isinstance(node, DateAdd):
        return _date_add_bounds(node, ranges)
    if isinstance(node, Call):
        return _call_bounds(node, ranges)
    return None


_MOVES = {"day": add_days, "month": try_add_months, "year": add_years}


def _date_add_bounds(node: DateAdd, ranges: Mapping[str, Interval]) -> Optional[Interval]:
    start = _node_bounds(node.date, ranges)
    count = _node_bounds(node.count, ranges)
    if start is None or count is None:
        return None
    # A date and a count are whole numbers: only the integers inside an end-pair count.
    first, last = ceil(start[0]), floor(start[1])
    fewest, most = ceil(count[0]), floor(count[1])
    if first > last or fewest > most:
        return None
    move = _MOVES[node.unit]
    ends = []
    for day, number in ((first, fewest), (last, most)):
        if not 1 <= day <= date.max.toordinal():
            return None
        moved = move(date.fromordinal(day), number)
        if moved is None:
            return None
        ends.append(Fraction(moved.toordinal()))
    return ends[0], ends[1]


def _call_bounds(node: Call, ranges: Mapping[str, Interval]) -> Optional[Interval]:
    name = node.name.upper()
    if name == "ROUND" and node.args:
        inner = _node_bounds(node.args[0], ranges)
        scale = 0
        if len(node.args) == 2:
            places = node.args[1]
            if not isinstance(places, Number) or places.value.denominator != 1:
                return None
            scale = int(places.value)
        if inner is None:
            return None
        mode = node.mode or HALF_AWAY_FROM_ZERO
        return round_exact(inner[0], scale, mode), round_exact(inner[1], scale, mode)
    if name == "COALESCE":
        return _hull([_node_bounds(arg, ranges) for arg in node.args])
    return None


def bounds(text: str, ranges: Mapping[str, Interval]) -> Optional[Interval]:
    """The exact bounds of a contract expression over the *ranges* of the columns it reads.

    ``None`` when the text does not parse as FLUID SQL/1, or reads a column of
    unknown range, a link, or a function the interval does not follow.
    """
    try:
        tree = parse(text, side=Side.CORE).tree
    except Expr2Error:
        return None
    return _node_bounds(tree, ranges)


def numeric(value: Any) -> Optional[Fraction]:
    """A declared bound as a fraction, or ``None`` when it is not a number."""
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return Fraction(number) if number.is_finite() else None
