# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A level the row holds stands in for a given column's own draw (ADR 0027, amendment of 2026-11-25).

An events or periods emit sets a column of the version before it draws its own cells, and hands
:func:`~flux_engine.generator.rows.cells_before_rules` the level each row holds. A column drawn
given it, and the columns below that one, read the held level, by the column path and by the
per-row path alike, in any slice and across chunk boundaries.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator import columns
from flux_engine.generator.rows import (
    OWN_DRAW,
    UNNAMED_LEVEL,
    GeneratorPlan,
    cells_before_rules,
    class_names,
    held_levels,
)

from .test_columns import _GIVEN_DECLARED, _GIVEN_SCHEMA, _both, _given_plan, _plan, _root

pytestmark = pytest.mark.unit

MARKETS = ("DE", "FR", "IE")


def _held(start: int, stop: int) -> List[str]:
    """The market each row of a slice holds: ``IE`` on every third row, ``DE`` and ``FR`` between."""
    return [MARKETS[(row * 7) % 3] for row in range(start, stop)]


def _rows(start: int, stop: int) -> Tuple[Tuple[Any, ...], ...]:
    plan = _given_plan()
    return cells_before_rules(plan, start, stop, root=_root(), held={"market": _held(start, stop)})


@pytest.mark.parametrize(("start", "stop"), [(0, 300), (5, 9), (13, 29), (299, 300)])
def test_a_held_level_is_drawn_given_by_both_paths_alike(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)
    by_columns, by_rows, taken = _both(monkeypatch, lambda: _rows(start, stop))
    assert by_columns == by_rows
    assert taken == [True]


def test_a_column_given_a_held_level_follows_it_and_so_does_the_one_below() -> None:
    data = _rows(0, 300)
    held = _held(0, 300)
    # The table gives IE only bronze, and bronze only the plan b.
    tiers = [row[1] for row, market in zip(data, held, strict=True) if market == "IE"]
    plans = [row[2] for row, market in zip(data, held, strict=True) if market == "IE"]
    assert tiers and set(tiers) == {"bronze"}
    assert set(plans) == {"b"}
    # Without the held levels the same rows draw their own markets, and the tiers differ.
    plain = cells_before_rules(_given_plan(), 0, 300, root=_root())
    assert [row[1] for row in plain] != [row[1] for row in data]


def test_a_held_value_that_is_no_level_is_refused() -> None:
    with pytest.raises(FluxError, match="FLX-3403.*'market' holds 'NL'"):
        held_levels(_given_plan(), {"market": ["DE", "NL"]})


def test_held_values_are_read_as_their_levels() -> None:
    assert held_levels(_given_plan(), {"market": ["IE", "DE"]}) == {"market": ["IE", "DE"]}


def test_a_value_no_level_names_holds_the_unnamed_level_in_a_fallback_column() -> None:
    held = held_levels(_given_plan(), {"market": ["DE", "NL", None]}, ("market",))
    assert held == {"market": ["DE", UNNAMED_LEVEL, UNNAMED_LEVEL]}


# -- a level held on some rows only (ADR 0027, the amendment of 2026-11-26) ------------------------


def _partly(start: int, stop: int, every: int, level: str) -> List[Optional[str]]:
    """*level* on every *every*-th row of a slice, and no level held on the others."""
    return [level if row % every == 0 else None for row in range(start, stop)]


def _partly_held(start: int, stop: int) -> Dict[str, List[Optional[str]]]:
    """``IE`` held in ``market`` on every third row, ``bronze`` in ``tier`` on every second."""
    return {"market": _partly(start, stop, 3, "IE"), "tier": _partly(start, stop, 2, "bronze")}


@pytest.mark.parametrize(("start", "stop"), [(0, 300), (5, 9), (13, 29), (299, 300)])
def test_a_level_held_on_some_rows_is_drawn_given_by_both_paths_alike(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)

    def make() -> Tuple[Tuple[Any, ...], ...]:
        held = _partly_held(start, stop)
        return cells_before_rules(_given_plan(), start, stop, root=_root(), held=held)

    by_columns, by_rows, taken = _both(monkeypatch, make)
    assert by_columns == by_rows
    assert taken == [True]


def test_a_row_that_holds_no_level_keeps_the_level_of_its_own_draw() -> None:
    data = cells_before_rules(_given_plan(), 0, 300, root=_root(), held=_partly_held(0, 300))
    plain = cells_before_rules(_given_plan(), 0, 300, root=_root())
    for row, (cells, own) in enumerate(zip(data, plain, strict=True)):
        if row % 2 and row % 3:
            assert cells == own, row
            continue
        # A held IE gives ``tier`` only bronze, and a bronze, held or drawn, only the plan b.
        assert cells[2] == "b", row
        if row % 2:
            assert cells[1] == "bronze", row
    # On a row that holds bronze in ``tier`` and nothing in ``market``, ``tier``'s own draw stands
    # as its cell, given the market that draw made: some of those rows are not bronze.
    assert {data[row][1] for row in range(0, 300, 2) if row % 3} > {"bronze"}


def test_a_cell_the_row_draws_itself_holds_no_level() -> None:
    plan = _given_plan()
    assert held_levels(plan, {"market": ["DE", OWN_DRAW]}) == {"market": ["DE", None]}
    low = class_names(plan.column_plan("amount"))[0]
    assert held_levels(plan, {"amount": [Decimal("5"), OWN_DRAW]}) == {"amount": [low, None]}


def _child_first_plan() -> GeneratorPlan:
    """``tier`` declared before the ``market`` it is drawn given, and ``plan`` given ``tier``."""
    by_name = {column["name"]: column for column in _GIVEN_SCHEMA}
    schema = [by_name["tier"], by_name["market"], by_name["plan"]]
    declared = {name: _GIVEN_DECLARED[name] for name in ("market", "tier", "plan")}
    return _plan(rows=300, schema=schema, declared=declared)


@pytest.mark.parametrize(("start", "stop"), [(0, 300), (13, 29)])
def test_a_column_held_on_some_rows_is_drawn_after_its_parent_held_on_some(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    # ``tier`` comes first in the row, yet on a row that holds IE in ``market`` and nothing in
    # ``tier`` its own draw is given IE, by the column path as by the per-row path.
    monkeypatch.setattr(columns, "CHUNK_ROWS", 7)

    def make() -> Tuple[Tuple[Any, ...], ...]:
        held = _partly_held(start, stop)
        return cells_before_rules(_child_first_plan(), start, stop, root=_root(), held=held)

    by_columns, by_rows, taken = _both(monkeypatch, make)
    assert by_columns == by_rows
    assert taken == [True]
