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
from fractions import Fraction
from typing import Any, List, Mapping, Optional, Tuple

from flux_engine.expr2.ast import Column, Compare, IsNull, Node, Or
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.parser import Side, parse
from flux_engine.generator.rules import Band

__all__ = ["DateOrder", "STANDARD_ERRORS", "contract_date_orders"]

#: How many standard errors of a share, at the table's row count, a kept order leaves below its band.
STANDARD_ERRORS = 3


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
    #: The share of the covered rows that may break the check (0 when it declares no band, or a
    #: band that is not an upper bound in percent), and whether the share itself is too many.
    tolerance: Fraction = Fraction(0)
    tolerance_open: bool = False

    def tolerates(self, share: Fraction, rows: int = 0) -> bool:
        """Whether a run of *rows* rows, in which *share* of them break the check, stays within its band.

        A run draws its rows by chance, so the share it breaks the check on falls about
        ``sqrt(share * (1 - share) / rows)`` either side of *share*. The share is tolerated only
        where it and :data:`STANDARD_ERRORS` such errors are within the band (below it, for an
        open band), so that a run of this size stays within it all but about once in a thousand.
        With no row count (*rows* of 0) the share itself is compared. Exact, in fractions.
        """
        room = self.tolerance - share
        if room < 0 or (self.tolerance_open and room == 0):
            return False
        if rows <= 0:
            return True
        noise = STANDARD_ERRORS * STANDARD_ERRORS * share * (1 - share)
        reach = rows * room * room
        return noise < reach if self.tolerance_open else noise <= reach


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
        order = _order(rule, tree, _tolerance(entry))
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


def _tolerance(invariant: Mapping[str, Any]) -> Tuple[Fraction, bool]:
    """The share of rows a check's band lets break it, and whether that share is itself too many.

    Only a ``percent`` band with an upper bound counts: ``mustBeLessThan`` and
    ``mustBeLessOrEqualTo``, and ``mustBeBetween`` from zero. Any other band, a band in rows, or no
    band reads as no tolerance.
    """
    try:
        band = Band.read(invariant, "invariant")
    except ValueError:
        return Fraction(0), False
    if band is None or band.unit != "percent":
        return Fraction(0), False
    if band.operator in ("mustBeLessThan", "mustBeLessOrEqualTo"):
        return Fraction(band.values[0]) / 100, band.operator == "mustBeLessThan"
    if band.operator == "mustBeBetween" and band.values[0] == 0:
        return Fraction(band.values[1]) / 100, False
    return Fraction(0), False


def _order(rule: str, tree: Node, tolerance: Tuple[Fraction, bool]) -> Optional[DateOrder]:
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
        tolerance=tolerance[0],
        tolerance_open=tolerance[1],
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
