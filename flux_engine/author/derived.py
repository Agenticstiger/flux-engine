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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

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

__all__ = [
    "Interval",
    "bounds",
    "contract_dependencies",
    "contract_copies",
    "contract_dependents",
    "contract_aggregates",
    "contract_derives",
    "contract_guarded",
    "numeric",
]

Interval = Tuple[Fraction, Fraction]


def _invariants(expose: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The mapping entries of *expose*'s contract ``invariants``, none when it declares none."""
    contract = expose.get("contract")
    found = contract.get("invariants") if isinstance(contract, Mapping) else None
    return (
        [entry for entry in found if isinstance(entry, Mapping)] if isinstance(found, list) else []
    )


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


def _derived_columns(
    expose: Mapping[str, Any], keep: Callable[[Mapping[str, Any]], bool]
) -> Set[str]:
    """The ``derive`` columns of *expose*'s contract entries that *keep* accepts."""
    return {
        str(entry["derive"])
        for entry in _invariants(expose)
        if keep(entry) and isinstance(entry.get("derive"), str)
    }


def contract_aggregates(expose: Mapping[str, Any]) -> Set[str]:
    """The columns an ``aggregate`` ``derive`` of *expose*'s contract sets from its child rows.

    The run sets such a column from the rows of the child expose, so it is never drawn from its own
    declaration and ``allocation`` cannot read it.
    """
    return _derived_columns(expose, lambda entry: "aggregate" in entry and "when" not in entry)


def contract_guarded(expose: Mapping[str, Any]) -> Set[str]:
    """The columns a ``derive`` with a ``when`` of *expose*'s contract sets, an aggregate or not.

    The generator treats every column a rule sets as set, so ``allocation`` cannot read it, even
    though the rule leaves the column drawn on the rows its guard does not hold.
    """
    return _derived_columns(expose, lambda entry: "when" in entry)


def contract_copies(
    contract: Mapping[str, Any],
    expose_id: str,
    expose_index: int,
    keyed: Set[str],
    taken: Set[str],
) -> Dict[str, Tuple[Tuple[str, ...], str, str]]:
    """The columns of *expose_id* that are copied: column -> (tokens, how, reason).

    Kernel 0.2.0 plans each column a ``sharedColumns`` lists as ``derive: <column>`` over
    ``<relationship>.<column>``, and each ``sharedAttribute`` member but the owner as a copy of the
    owner's column, on every row (ADR 0022 section 4), so the column is never drawn. A column that
    is a key or a foreign key (*keyed*), one another rule sets (*taken*, which `plan` then refuses
    as two sources), and one the expose or its parent does not hold is left out, as the plan
    leaves it. The tokens locate the declaration in the contract; *how* and *reason* are the
    sentences the bundle says of the column.
    """
    from flux_engine.core.canonical import nfc
    from flux_engine.generator.rules import shared_attribute_members
    from flux_engine.seam.keys import declared_exposes

    exposes = declared_exposes(contract)
    entry = exposes.get(nfc(expose_id))
    semantics = entry.get("semantics") if isinstance(entry, Mapping) else None
    relationships = semantics.get("relationships") if isinstance(semantics, Mapping) else None
    held = _schema_names(entry)
    copies: Dict[str, Tuple[Tuple[str, ...], str, str]] = {}
    for index, relationship in enumerate(relationships if isinstance(relationships, list) else ()):
        if not isinstance(relationship, Mapping):
            continue
        name, listed = relationship.get("name"), relationship.get("sharedColumns")
        target = exposes.get(nfc(str(relationship.get("to"))))
        if not isinstance(name, str) or not isinstance(listed, list) or target is None:
            continue
        parent = _schema_names(target)
        for position, column in enumerate(listed):
            if not isinstance(column, str):
                continue
            column = nfc(column)
            if column in keyed or column in taken or column in copies:
                continue
            if column in held and column in parent:
                tokens = (
                    "exposes",
                    str(expose_index),
                    "semantics",
                    "relationships",
                    str(index),
                    "sharedColumns",
                    str(position),
                )
                copies[column] = (
                    tokens,
                    f"copied from the row {name} reaches (sharedColumns), not drawn",
                    f"The relationship {name} copies this column from the row it reaches "
                    "(sharedColumns), so the column is not drawn and no property reads it: a "
                    "row holds its parent's value, and NULL where its key is NULL.",
                )
    schema = entry.get("contract", {}).get("schema") if isinstance(entry, Mapping) else None
    members = shared_attribute_members(contract, expose_id, frozenset(keyed))
    for position, column in enumerate(schema if isinstance(schema, list) else ()):
        if not isinstance(column, Mapping) or not isinstance(column.get("name"), str):
            continue
        name = nfc(column["name"])
        attribute = members.get(name)
        if attribute is None or name in taken or name in copies:
            continue
        copies[name] = (
            ("exposes", str(expose_index), "contract", "schema", str(position), "sharedAttribute"),
            f"copied from the owner of the shared attribute {attribute!r} (sharedAttribute), "
            "not drawn",
            f"The shared attribute {attribute!r} is generated once, on its owner, and this "
            "column copies it (sharedAttribute), so the column is not drawn and no property "
            "reads it: a row holds the owner's value, and NULL where its path reaches no owner row.",
        )
    return copies


def contract_pick_copies(contract: Mapping[str, Any]) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """The relationships whose pick a copy sets: (emit, relationship) -> (through, column).

    A ``sharedColumns`` entry of relationship ``R`` that lists the one-column foreign key of another
    relationship ``S`` of the same expose copies it into ``S``'s pick (ADR 0022 section 4, the
    amendment of 2026-11-24): ``S`` draws nothing of its own then, and a counts entry over it is
    ``FLX-3403`` ("a copy sets the pick alone"). Found as the plan finds them, by
    :func:`flux_engine.seam.keys.copied_pick`, where ``R``'s target holds the column. The caller
    asks only where the Simulation copies at all (kernel 0.2.0 and ``copies: true``).
    """
    from flux_engine.core.canonical import nfc
    from flux_engine.seam.keys import compile_keys, copied_pick, declared_exposes

    exposes = declared_exposes(contract)
    keys = compile_keys(contract)
    found: Dict[Tuple[str, str], Tuple[str, str]] = {}
    if keys is None:
        return found
    for expose_id, entry in exposes.items():
        for name, target, listed in _copying_relationships(entry):
            if target == expose_id:
                continue
            held = _schema_names(exposes.get(target))
            for column in (nfc(item) for item in listed if isinstance(item, str)):
                picked = copied_pick(keys, expose_id, name, column) if column in held else None
                if picked is not None:
                    found.setdefault((expose_id, picked[0].name), (name, column))
    return found


def _copying_relationships(entry: Mapping[str, Any]) -> List[Tuple[str, str, List[Any]]]:
    """The relationships of an expose that list ``sharedColumns``, as (name, target, columns)."""
    from flux_engine.core.canonical import nfc

    semantics = entry.get("semantics")
    relationships = semantics.get("relationships") if isinstance(semantics, Mapping) else None
    found: List[Tuple[str, str, List[Any]]] = []
    for relationship in relationships if isinstance(relationships, list) else ():
        if not isinstance(relationship, Mapping):
            continue
        name, listed = relationship.get("name"), relationship.get("sharedColumns")
        if isinstance(name, str) and isinstance(listed, list):
            found.append((name, nfc(str(relationship.get("to"))), listed))
    return found


def _schema_names(expose: Optional[Mapping[str, Any]]) -> Set[str]:
    from flux_engine.core.canonical import nfc

    contract = expose.get("contract") if isinstance(expose, Mapping) else None
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    return {
        nfc(column["name"])
        for column in (schema if isinstance(schema, list) else ())
        if isinstance(column, Mapping) and isinstance(column.get("name"), str)
    }


def contract_dependencies(expose: Mapping[str, Any]) -> Dict[str, Tuple[str, ...]]:
    """Each column a contract ``dependency`` of *expose* sets, with the ``from`` columns it follows.

    The first dependency that names a column decides (EXPR-SPEC-2 section 8): a column two
    dependencies set is refused when the bundle is planned.
    """
    contract = expose.get("contract")
    found = contract.get("invariants") if isinstance(contract, Mapping) else None
    follows: Dict[str, Tuple[str, ...]] = {}
    for entry in found if isinstance(found, list) else ():
        rule = entry.get("dependency") if isinstance(entry, Mapping) else None
        if not isinstance(rule, Mapping):
            continue
        sources, targets = rule.get("from"), rule.get("to")
        if not isinstance(sources, list) or not isinstance(targets, list):
            continue
        if not all(isinstance(name, str) for name in sources):
            continue
        for name in targets:
            if isinstance(name, str):
                follows.setdefault(name, tuple(sources))
    return follows


def contract_dependents(expose: Mapping[str, Any]) -> Set[str]:
    """The columns a contract ``dependency`` of *expose* sets (EXPR-SPEC-2 section 8).

    A row takes the value its ``from`` columns' values already have, so the run
    draws such a column once per value of the columns it follows.
    """
    dependents: Set[str] = set()
    for entry in _invariants(expose):
        rule = entry.get("dependency")
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
