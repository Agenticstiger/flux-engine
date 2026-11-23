# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``parse(unparse(tree))`` is *tree*, for every tree the grammar can spell (EXPR-SPEC-2 §11).

Hypothesis builds trees from every node kind, with names chosen to collide
with keywords, roots, function and window words, quotes and non-ASCII
letters, and checks that the canonical text parses back to the same tree on
both sides, and that the canonical text is a fixed point.
"""

from __future__ import annotations

from typing import Any, List

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from flux_engine.expr2 import Side, parse, unparse
from flux_engine.expr2.ast import (
    AGGREGATES,
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
    Date,
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
    SortKey,
    String,
    Window,
)

pytestmark = pytest.mark.unit

NAMES = st.sampled_from(
    [
        "a",
        "net",
        "_x1",
        "row",
        "link",
        "previous",
        "children",
        "Row",
        "end",
        "date",
        "Net Amount",
        'q"t',
        "round",
        "count",
        "over",
        "order",
        "desc",
        "nulls",
        "by",
        "été",
        "ROW_NUMBER",
    ]
)
NUMBERS = st.builds(Number, st.from_regex(r"(0|[1-9][0-9]{0,3})(\.[0-9]{1,3})?", fullmatch=True))
STRINGS = st.builds(String, st.text(alphabet="ab '-*/;\"é\n", max_size=6))
DATES = st.builds(
    lambda y, m, d: Date(f"{y:04d}-{m:02d}-{d:02d}"),
    st.integers(1, 9999),
    st.integers(1, 12),
    st.integers(1, 28),
)
LITERALS = st.one_of(NUMBERS, STRINGS, st.builds(Bool, st.booleans()), DATES)
IN_ITEMS = st.one_of(LITERALS, st.builds(Neg, NUMBERS))
SCALES = st.integers(0, 38).map(lambda s: Number(str(s)))


def _is_null(node: Node) -> bool:
    while isinstance(node, Neg):
        node = node.operand
    return isinstance(node, Null)


def _not_null(strategy: Any) -> Any:
    # A comparison, IN or BETWEEN operand is never NULL, negated or not (FLX-6104).
    return strategy.filter(lambda node: not _is_null(node))


def _paths(flux: bool) -> Any:
    core = st.one_of(st.builds(Column, NAMES), st.builds(Column, NAMES, NAMES))
    if not flux:
        return core
    multi = st.builds(
        lambda hops, column: Path("link", tuple(hops) + (column,)),
        st.lists(NAMES, min_size=2, max_size=3),
        NAMES,
    )
    previous = st.one_of(
        st.builds(lambda c: Path("previous", (c,)), NAMES),
        st.builds(lambda r, c: Path("previous", (r, c)), NAMES, NAMES),
    )
    return st.one_of(core, multi, previous)


def _trees(flux: bool) -> Any:
    leaves = st.one_of(LITERALS, st.builds(Null), _paths(flux))

    def extend(children: Any) -> Any:
        value = _not_null(children)
        options: List[Any] = [
            st.builds(Neg, children),
            st.builds(Arith, st.sampled_from(["+", "-", "*", "/"]), children, children),
            st.builds(Compare, st.sampled_from(["=", "<>", "<", "<=", ">", ">="]), value, value),
            st.builds(
                In, value, st.lists(IN_ITEMS, min_size=1, max_size=3).map(tuple), st.booleans()
            ),
            st.builds(Between, value, value, value, st.booleans()),
            st.builds(IsNull, children, st.booleans()),
            st.builds(IsDistinctFrom, children, children, st.booleans()),
            st.builds(Not, children),
            st.builds(And, children, children),
            st.builds(Or, children, children),
            st.builds(
                Case,
                st.lists(st.tuples(children, children), min_size=1, max_size=2).map(tuple),
                st.one_of(st.none(), children),
            ),
            st.builds(
                lambda args: Call("COALESCE", tuple(args)),
                st.lists(children, min_size=2, max_size=3),
            ),
            st.builds(
                lambda x, s, mode: Call("ROUND", (x, s), mode),
                children,
                SCALES,
                st.sampled_from(["HALF_AWAY_FROM_ZERO", "HALF_TO_EVEN"]),
            ),
            st.builds(
                lambda name, a, b: Call(name, (a, b)),
                st.sampled_from(["DAYS_BETWEEN", "MONTHS_BETWEEN", "YEARS_BETWEEN"]),
                children,
                children,
            ),
        ]
        if flux:
            options.append(
                st.builds(
                    Aggregate,
                    st.sampled_from(sorted(AGGREGATES)),
                    st.builds(Children, NAMES, st.one_of(st.none(), NAMES)),
                    st.one_of(st.none(), children),
                )
            )
            keys = st.lists(
                st.builds(SortKey, _paths(True), st.booleans(), st.booleans()),
                min_size=1,
                max_size=2,
            ).map(tuple)
            ranks = st.builds(
                lambda name, partition, order: Window(name, (), partition, order),
                st.sampled_from(["ROW_NUMBER", "RANK", "DENSE_RANK"]),
                NAMES,
                keys,
            )
            edges = st.builds(
                lambda name, x, partition, order: Window(name, (x,), partition, order),
                st.sampled_from(["FIRST_VALUE", "LAST_VALUE"]),
                children,
                NAMES,
                keys,
            )
            offsets = st.builds(
                lambda name, x, k, d, partition, order: Window(
                    name,
                    (x,) + ((Number(str(k)),) if k else ()) + ((d,) if k and d is not None else ()),
                    partition,
                    order,
                ),
                st.sampled_from(["LAG", "LEAD"]),
                children,
                st.integers(0, 3),
                st.one_of(st.none(), LITERALS, st.builds(Null)),
                NAMES,
                keys,
            )
            options += [ranks, edges, offsets]
        return st.one_of(*options)

    return st.recursive(leaves, extend, max_leaves=10)


def _round_trip(tree: Node, side: Side) -> None:
    text = unparse(tree, flux_side=side is Side.FLUX)
    parsed = parse(text, side=side)
    assert parsed.tree == tree, text
    assert unparse(parsed.tree, flux_side=side is Side.FLUX) == text


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_trees(flux=False))
def test_core_trees_round_trip(tree: Node) -> None:
    _round_trip(tree, Side.CORE)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_trees(flux=True))
def test_flux_side_trees_round_trip(tree: Node) -> None:
    _round_trip(tree, Side.FLUX)


def test_a_partition_that_is_also_a_column_keeps_its_link_prefix() -> None:
    tree = parse("RANK() OVER (PARTITION BY link.customer ORDER BY x)", side=Side.FLUX).tree
    assert unparse(tree, flux_side=True) == "RANK() OVER (PARTITION BY customer ORDER BY x)"
    assert (
        unparse(tree, flux_side=True, columns=frozenset({"customer"}))
        == "RANK() OVER (PARTITION BY link.customer ORDER BY x)"
    )


def test_to_json_refuses_a_call_that_is_not_flux_expr_2() -> None:
    from flux_engine.expr2.ast import Call, Column, Number, String, to_json

    with pytest.raises(TypeError):
        to_json(Call("ABS", (Column("net"),)))
    with pytest.raises(TypeError):
        to_json(Call("ROUND", (Column("net"), String("2")), "HALF_TO_EVEN"))
    assert to_json(Call("ROUND", (Column("net"), Number("2")), None))["mode"] == (
        "HALF_AWAY_FROM_ZERO"
    )
