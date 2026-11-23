# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What flux-expr/2 and flux-expr/3 decide about a rule from its texts alone (EXPR-SPEC-2 §1, §5, §6, §12).

* :func:`check_language` reads a FLUX-side rule's required ``language``
  key (``FLX-6101``, ``FLX-6102``): ``flux-expr/2`` or ``flux-expr/3``.
* :func:`check_rule_ids` checks ids within one contract or among a bundle's
  FLUX-side rules (``FLX-6201``); FLUX does not assume a validator ran.
* :func:`aggregate_tree` reads a contract ``derive`` with ``aggregate`` as
  the aggregate EXPR-SPEC-2 §5 maps it to, so FLUX evaluates the summary
  with the same code as an aggregate written in a FLUX-side text.
* :func:`check_flux_side_rule` parses and checks a FLUX-side rule's texts:
  bool where a predicate is expected (``FLX-6206``), a derived value that
  fits its column (``FLX-6110``), and ``FLX-6209`` for a rule the contract
  can carry, one that uses none of the extensions (four under flux-expr/2, seven
  under flux-expr/3) or whose whole value is exactly a contract aggregate.

Everything that needs bands, statistics, FLUX settings, dependencies or the
planner (the other rule codes of EXPR-SPEC-2 §12) belongs to the step that
generates from rules, which reads the shape of the rule entries too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple

from flux_engine.expr2.ast import (
    LANGUAGES,
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
    Number,
    Or,
    String,
    Window,
    is_extension,
    unparse,
)
from flux_engine.expr2.check import PREDICATE, VALUE, Checked, check
from flux_engine.expr2.errors import FLX_6101, FLX_6102, FLX_6103, FLX_6201, FLX_6209, Expr2Error
from flux_engine.expr2.parser import Parsed, Side, parse
from flux_engine.expr2.scope import Model

__all__ = [
    "LANGUAGE",
    "LANGUAGES",
    "FluxSideRule",
    "aggregate_tree",
    "check_flux_side_rule",
    "check_language",
    "check_rule_ids",
    "is_contract_aggregate",
    "subtrees",
    "uses_extension",
]

#: The value of a FLUX-side rule's ``language`` key.
LANGUAGE = "flux-expr/2"

_RULE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CONTRACT_SUMMARIES = frozenset({"SUM", "MIN", "MAX"})


def check_language(entry: Mapping[str, Any], *, where: str) -> str:
    """Refuse a FLUX-side rule with no ``language`` (``FLX-6101``) or another one (``FLX-6102``).

    Returns the declared language: ``flux-expr/2`` or ``flux-expr/3`` (EXPR-SPEC-3 §1).
    """
    if "language" not in entry:
        raise Expr2Error(FLX_6101, f"{where} declares no language; add 'language: {LANGUAGE}'")
    declared = entry["language"]
    if not isinstance(declared, str) or declared not in LANGUAGES:
        raise Expr2Error(
            FLX_6102,
            f"{where} declares language {declared!r}; a FLUX-side rule takes "
            f"'{LANGUAGES[0]}' or '{LANGUAGES[1]}'",
        )
    return declared


def check_rule_ids(ids: Sequence[Optional[str]], *, where: str) -> None:
    """Every id present, an identifier, and unique within *where* (``FLX-6201``)."""
    seen = set()
    for index, rule_id in enumerate(ids):
        if not isinstance(rule_id, str) or not rule_id:
            raise Expr2Error(FLX_6201, f"{where}: rule {index} has no id")
        if not _RULE_ID.fullmatch(rule_id):
            raise Expr2Error(
                FLX_6201,
                f"{where}: id {rule_id!r} is not ASCII letters, digits and underscores "
                "starting with a letter",
            )
        if rule_id in seen:
            raise Expr2Error(FLX_6201, f"{where}: id {rule_id!r} repeats")
        seen.add(rule_id)


def _parts(node: Node) -> Iterable[Node]:
    if isinstance(node, (Neg, Not)):
        yield node.operand
    elif isinstance(node, (Arith, Compare, And, Or, IsDistinctFrom)):
        yield node.left
        yield node.right
    elif isinstance(node, In):
        yield node.value
        yield from node.items
    elif isinstance(node, Between):
        yield node.value
        yield node.low
        yield node.high
    elif isinstance(node, IsNull):
        yield node.value
    elif isinstance(node, Case):
        for condition, value in node.whens:
            yield condition
            yield value
        if node.otherwise is not None:
            yield node.otherwise
    elif isinstance(node, Call):
        yield from node.args
    elif isinstance(node, Extract):
        yield node.arg
    elif isinstance(node, DateAdd):
        yield node.date
        yield node.count
    elif isinstance(node, Concat):
        yield node.left
        yield node.right
    elif isinstance(node, Aggregate):
        yield node.children
        if node.arg is not None:
            yield node.arg
    elif isinstance(node, Window):
        yield from node.args
        for key in node.order:
            yield key.key


def subtrees(node: Node) -> Iterable[Node]:
    """The direct operands of *node*, in the order the text writes them."""
    return _parts(node)


def uses_extension(tree: Node) -> bool:
    """Whether *tree* uses one of the extensions: previous, a multi-link path, an aggregate, a
    window (EXPR-SPEC-2), or a date part, date arithmetic or ``||`` (EXPR-SPEC-3).

    ``row.<column>`` and a one-hop ``link.`` path parse to the core's nodes,
    so they are not extensions (EXPR-SPEC-2 §1).
    """
    return _contains(tree, is_extension)


def _contains(tree: Node, test: Callable[[Node], bool]) -> bool:
    stack = [tree]
    while stack:
        node = stack.pop()
        if test(node):
            return True
        stack.extend(_parts(node))
    return False


def _is_literal(node: Node) -> bool:
    if isinstance(node, Neg):
        return isinstance(node.operand, Number)
    return isinstance(node, (Number, String, Bool, Date))


def is_contract_aggregate(tree: Node) -> bool:
    """Whether *tree* is exactly what a contract ``derive`` with ``aggregate`` reads as (EXPR-SPEC-2 §5)."""
    if isinstance(tree, Call) and tree.name == "COALESCE":
        return len(tree.args) == 2 and _is_literal(tree.args[1]) and _bare_summary(tree.args[0])
    return _bare_summary(tree)


def _bare_summary(node: Node) -> bool:
    if not isinstance(node, Aggregate):
        return False
    if node.name == "COUNT":
        return node.arg is None or not uses_extension(node.arg)
    if node.name not in _CONTRACT_SUMMARIES or node.arg is None:
        return False
    arg = node.arg
    if isinstance(arg, Case) and arg.otherwise is None and len(arg.whens) == 1:
        condition, arg = arg.whens[0]
        if uses_extension(condition):
            return False
    return isinstance(arg, Column) and arg.relationship is None


def _literal(value: Any, where: str) -> Node:
    if isinstance(value, bool):
        return Bool(value)
    if isinstance(value, str):
        return String(value)
    if isinstance(value, datetime):
        raise ValueError(
            f"{where} is a date, not a timestamp: FLUID SQL/1 has no timestamp literal"
        )
    if isinstance(value, date):
        return Date(value.isoformat())
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(repr(float(value))) if isinstance(value, float) else Decimal(value)
        if not number.is_finite():
            raise ValueError(f"{where} is a finite number")
        # copy_abs, unlike abs(), never rounds to the 28-digit default context.
        text = format(number.copy_abs(), "f")
        node: Node = Number(text)
        return Neg(node) if number < 0 else node
    raise ValueError(f"{where} is a literal: a number, a string, a bool or a date")


def aggregate_tree(aggregate: Mapping[str, Any]) -> Parsed:
    """A contract ``aggregate`` as the aggregate EXPR-SPEC-2 §5 maps it to, ready to check.

    ``{function, column, from, via, filter, ifNone}`` reads as
    ``<FUNCTION>(CHILDREN.<from>.<via>, <column>)``, with a filter as
    ``CASE WHEN <filter> THEN <column> END`` (``COUNT`` takes the filter as
    its predicate), and with ``ifNone`` wrapped in ``COALESCE(..., <ifNone>)``.
    The filter is a core text, parsed as one. The result is checked as a
    FLUX-side tree on the parent expose.
    """
    if not isinstance(aggregate, Mapping):
        raise ValueError("an aggregate is an object: {function, column, from, via, filter, ifNone}")
    function = str(aggregate.get("function", "")).upper()
    if function not in ("COUNT", "SUM", "MIN", "MAX"):
        raise ValueError(f"an aggregate's function is count, sum, min or max, not {function!r}")
    names = ("from", "via") if function == "COUNT" else ("from", "via", "column")
    for key in names:
        if not isinstance(aggregate.get(key), str) or not aggregate[key]:
            raise ValueError(f"an aggregate's {key!r} is a name")
    if aggregate.get("filter") is not None and not isinstance(aggregate["filter"], str):
        raise ValueError("an aggregate's 'filter' is a FLUID SQL/1 text")
    # The canonical text spells the CHILDREN root, so the parent's names get the
    # FLX-6105 check a written aggregate gets: that text must parse back (§11).
    children = Children(aggregate["from"], aggregate["via"], root_note="CHILDREN")
    condition = None
    if aggregate.get("filter") is not None:
        condition = parse(aggregate["filter"], side=Side.CORE).tree
    arg: Optional[Node]
    if function == "COUNT":
        arg = condition
    else:
        column: Node = Column(aggregate["column"])
        arg = column if condition is None else Case(((condition, column),))
    tree: Node = Aggregate(function, children, arg)
    if aggregate.get("ifNone") is not None:
        tree = Call("COALESCE", (tree, _literal(aggregate["ifNone"], "ifNone")))
    # The plan stores the canonical text, so the aggregate is that text: parsing it
    # counts the parentheses it needs and the summary around the filter, which a
    # walk of the built tree cannot see, and it NFC-normalizes the contract's names
    # and literals as every text is (§11: it must parse back to what is checked).
    text = unparse(tree, flux_side=True)
    try:
        written = parse(text, side=Side.FLUX)
    except Expr2Error as error:
        if error.code is not FLX_6103:
            raise
        raise Expr2Error(
            FLX_6103,
            "the aggregate written out with the summary around its filter is past a limit: "
            + error.detail,
        ) from None
    return written


@dataclass(frozen=True)
class FluxSideRule:
    """A FLUX-side rule's texts, checked: its id, the column it derives, and its checked texts."""

    id: str
    derive: Optional[str]
    value: Checked
    when: Optional[Checked]


def check_flux_side_rule(entry: Mapping[str, Any], model: Model, expose: str) -> FluxSideRule:
    """Check one FLUX-side rule entry's language and texts.

    The entry is ``{id, language, check}`` or ``{id, language, derive,
    expr}``, with an optional ``when``; the bundle's schema checks that
    shape, so a malformed one is a ``ValueError`` here.
    """
    rule_id = entry.get("id")
    where = f"FLUX-side rule {rule_id!r}" if isinstance(rule_id, str) else "a FLUX-side rule"
    language = check_language(entry, where=where)
    check_rule_ids([rule_id], where=where)
    assert isinstance(rule_id, str)
    has_check = "check" in entry
    has_derive = "derive" in entry
    if has_check == has_derive or (has_derive and "expr" not in entry):
        raise ValueError(f"{where} has exactly one of 'check', or 'derive' with 'expr'")
    if has_check:
        value = check(
            parse(entry["check"], side=Side.FLUX, language=language),
            model,
            expose,
            position=PREDICATE,
        )
        target = None
    else:
        target = entry["derive"]
        parsed = parse(entry["expr"], side=Side.FLUX, language=language)
        value = check(parsed, model, expose, position=VALUE, target=target)
    when = None
    if entry.get("when") is not None:
        when = check(
            parse(entry["when"], side=Side.FLUX, language=language),
            model,
            expose,
            position=PREDICATE,
        )
    texts: Tuple[Node, ...] = (value.parsed.tree,) + (() if when is None else (when.parsed.tree,))
    if not any(uses_extension(tree) for tree in texts):
        raise Expr2Error(
            FLX_6209,
            f"{where} uses none of the {'seven' if language == LANGUAGES[1] else 'four'} "
            "extensions, so it belongs in the FLUID contract's invariants",
        )
    if target is not None and is_contract_aggregate(value.parsed.tree):
        raise Expr2Error(
            FLX_6209,
            f"{where}'s value is exactly a contract derive with aggregate; declare it in the "
            "FLUID contract's invariants",
        )
    return FluxSideRule(rule_id, target, value, when)
