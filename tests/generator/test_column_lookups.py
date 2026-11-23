# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A related column is looked up a column at a time, and holds what the per-row path holds.

Kernel 0.2.0 reads a child's foreign and key-part columns from its parents for a whole slice:
the parent rows are searched at once, and each distinct parent value is built once and gathered to
the rows that name it. The per-row code is still there for the other path, so every construct that
was changed is read both ways here, values and types, and the work is counted rather than timed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from typing import Any, Dict, List, Tuple

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core import kernel2
from flux_engine.generator import relations as _relations
from flux_engine.generator import rows as _rows
from flux_engine.generator.relations import (
    CompositeMember,
    ForeignKey,
    KeyGrid,
    RelationReader,
    owner_of,
)
from flux_engine.generator.rows import GENERATOR_VERSION_2, GeneratorPlan

from .test_parent_rows import _bridge_plans
from .test_relations import _column, _expose, _primary, _rel, related_plans, root_for

pytestmark = pytest.mark.unit

ROOT_TREE = kernel2.SEED_TREE_VERSION


def _typed(values: List[Any]) -> List[Tuple[type, Any]]:
    """Values with their classes, so ``1`` and ``Decimal(1)`` are not taken for each other."""
    return [(type(value), value) for value in values]


# ---------------------------------------------------------------------------
# KeyGrid: a column of rows, a column of values
# ---------------------------------------------------------------------------

KINDS = [("INT", 0), ("TIMESTAMP", 0), ("DATE", 0), ("DECIMAL", -2), ("DECIMAL", 0)]


@st.composite
def _grids(draw: Any) -> KeyGrid:
    kind, exponent = draw(st.sampled_from(KINDS))
    bins = draw(
        st.lists(
            st.tuples(st.integers(1, 40), st.integers(1, 40), st.integers(0, 30)),
            min_size=1,
            max_size=6,
        )
    )
    first = draw(st.integers(-1_000, 800_000))
    firsts: List[int] = []
    widths: List[int] = []
    counts: List[int] = []
    starts: List[int] = []
    row = 0
    for width, count, gap in bins:
        count = min(count, width)
        firsts.append(first)
        widths.append(width)
        counts.append(count)
        starts.append(row)
        row += count
        first += width + gap
    return KeyGrid(kind, exponent, tuple(firsts), tuple(widths), tuple(counts), tuple(starts), row)


@settings(max_examples=150, deadline=None)
@given(grid=_grids(), extra=st.lists(st.integers(0, 400), max_size=30))
def test_a_column_of_grid_rows_is_the_value_of_each_row(grid: KeyGrid, extra: List[int]) -> None:
    rows = list(range(grid.rows)) + [row % grid.rows for row in extra]
    with localcontext(_rows._KERNEL_CONTEXT):
        found = grid.values_at(rows)
        assert found is not None
        assert _typed(found) == _typed([grid.value_at(row) for row in rows])


@settings(max_examples=150, deadline=None)
@given(grid=_grids(), shifts=st.lists(st.integers(-30, 60), min_size=1, max_size=40))
def test_a_column_of_drawn_values_moves_to_the_rows_the_per_row_search_picks(
    grid: KeyGrid, shifts: List[int]
) -> None:
    low = grid.firsts[0]
    drawn: List[Any] = []
    for shift in shifts:
        units = low + shift * (1 + shift % 3)
        drawn.append(_relations._value_of_units(grid.kind, grid.exponent, units))
        if grid.kind == "DECIMAL" and grid.exponent:
            # Between two grid points: the point it lies on is the one below.
            drawn.append(
                _relations._value_of_units(grid.kind, grid.exponent, units) + Decimal("0.004")
            )
    with localcontext(_rows._KERNEL_CONTEXT):
        near = grid.rows_near(drawn)
        assert near is not None
        assert near.tolist() == [grid.row_near(value) for value in drawn]
        assert near.dtype == np.int64


def test_a_grid_whose_numbers_do_not_fit_an_array_is_left_to_the_per_row_code() -> None:
    huge = 1 << 70
    grid = KeyGrid("INT", 0, (0, huge), (huge, huge), (3, 3), (0, 3), 6)
    assert grid.values_at([0, 1, 5]) is None
    assert grid.rows_near([5, huge + 7]) is None
    assert grid.value_at(4) == huge + (1 * huge) // 3  # the per-row code takes any size


def test_a_grid_with_one_row_and_a_value_below_every_key_rounds_to_row_zero() -> None:
    grid = KeyGrid("INT", 0, (10,), (1,), (1,), (0,), 1)
    near = grid.rows_near([-50, 10, 11, 90])
    assert near is not None and near.tolist() == [grid.row_near(v) for v in (-50, 10, 11, 90)]


# ---------------------------------------------------------------------------
# A star with every kind of parent key
# ---------------------------------------------------------------------------


def _kinds_contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose("p_int", [_column("pi", "INT")], _primary("pi", "pi")),
            _expose("p_date", [_column("pd", "DATE")], _primary("pd", "pd")),
            _expose("p_dec", [_column("pm", "DECIMAL(10,2)")], _primary("pm", "pm")),
            _expose("p_text", [_column("pt", "STRING")], _primary("pt", "pt")),
            _expose(
                "child",
                [
                    _column("ci", "INT", False),
                    _column("cd", "DATE", False),
                    _column("cm", "DECIMAL(10,2)", False),
                    _column("ct", "STRING", False),
                    _column("ci2", "INT"),
                ],
                {
                    "relationships": [
                        _rel("a", "p_int", ["ci"], ["pi"]),
                        _rel("b", "p_date", ["cd"], ["pd"]),
                        _rel("c", "p_dec", ["cm"], ["pm"]),
                        _rel("d", "p_text", ["ct"], ["pt"]),
                        _rel("e", "p_int", ["ci2"], ["pi"]),
                    ]
                },
            ),
        ],
    }


CHILD_ROWS = 600


def _kinds_declarations() -> Dict[str, Tuple[Dict[str, Any], int]]:
    return {
        "p_int": ({"pi": {"range": {"min": "5", "max": "400"}}}, 90),
        "p_date": ({"pd": {"range": {"min": "2024-01-01", "max": "2024-12-31"}}}, 120),
        "p_dec": ({"pm": {"range": {"min": "1.5", "max": "80.25"}}}, 70),
        "p_text": ({}, 55),
        "child": (
            {
                "ci": {"nullFraction": "0.1", "range": {"min": "5", "max": "400"}},
                "cd": {"nullFraction": "0.2", "range": {"min": "2024-03-01", "max": "2024-09-30"}},
                "cm": {"range": {"min": "2", "max": "60"}},
                "ct": {"nullFraction": "0.05"},
                "ci2": {},
            },
            CHILD_ROWS,
        ),
    }


def _kinds() -> Dict[str, GeneratorPlan]:
    return related_plans(
        _kinds_contract(), _kinds_declarations(), generator_version=GENERATOR_VERSION_2
    )


def _per_row(root: Any, plan: GeneratorPlan, name: str, rows: List[int]) -> List[Any]:
    """The column as the per-row path reads it: a reader with nothing prefetched."""
    reader = RelationReader(root)
    driver = plan.relations.driver(name)  # type: ignore[union-attr]
    assert driver is not None
    return [reader.cell(owner_of(plan), plan.column_plan(name), driver, row) for row in rows]


def _by_column(root: Any, plan: GeneratorPlan, names: List[str], rows: List[int]) -> Any:
    reader = RelationReader(root)
    owner = owner_of(plan)
    array = np.array(rows, dtype=np.int64)
    drivers = [plan.relations.driver(column.column.name) for column in plan.columns]  # type: ignore[union-attr]
    reader.prefetch(owner, plan.columns, drivers, array)
    out = {}
    with localcontext(_rows._KERNEL_CONTEXT):
        for name in names:
            driver = plan.relations.driver(name)  # type: ignore[union-attr]
            out[name] = reader.column_values(owner, plan.column_plan(name), driver, array)
    return reader, out


def test_every_kind_of_foreign_key_reads_the_same_by_column_and_by_row() -> None:
    plans = _kinds()
    root = root_for(seed_tree_version=ROOT_TREE)
    child = plans["child"]
    names = ["ci", "cd", "cm", "ct", "ci2"]
    rows = list(range(CHILD_ROWS))
    _, held = _by_column(root, child, names, rows)
    sample = {
        "ci": int,
        "cd": date,
        "cm": Decimal,
        "ct": str,
        "ci2": int,
    }
    for name in names:
        assert _typed(held[name]) == _typed(_per_row(root, child, name, rows)), name
        assert any(value is None for value in held[name]) == (name in ("ci", "cd", "ct")), name
        assert all(value is None or type(value) is sample[name] for value in held[name]), name


def test_a_slice_out_of_order_with_repeats_reads_the_same_by_column_and_by_row() -> None:
    plans = _kinds()
    root = root_for(seed_tree_version=ROOT_TREE)
    child = plans["child"]
    rows = [599, 3, 3, 250, 0, 599, 17, 17, 17, 400]
    _, held = _by_column(root, child, ["ci", "cd", "cm", "ct"], rows)
    for name in ("ci", "cd", "cm", "ct"):
        assert _typed(held[name]) == _typed(_per_row(root, child, name, rows)), name


@settings(max_examples=25, deadline=None)
@given(rows=st.lists(st.integers(0, CHILD_ROWS - 1), min_size=1, max_size=120))
def test_any_rows_of_the_star_read_the_same_by_column_and_by_row(rows: List[int]) -> None:
    plans = _kinds()
    root = root_for(seed_tree_version=ROOT_TREE)
    child = plans["child"]
    _, held = _by_column(root, child, ["ci", "cd", "cm", "ct", "ci2"], rows)
    for name in held:
        assert _typed(held[name]) == _typed(_per_row(root, child, name, rows)), name


def test_a_view_value_column_is_each_rows_view_value() -> None:
    plans = _kinds()
    root = root_for(seed_tree_version=ROOT_TREE)
    child = plans["child"]
    rows = [0, 5, 5, 40, 89, 3, 40, 40, 1]
    for _, driver in child.relations.drivers:  # type: ignore[union-attr]
        assert isinstance(driver, ForeignKey)
        for column in driver.to_columns:
            valid = [row for row in rows if row < driver.parent.rows]
            with localcontext(_rows._KERNEL_CONTEXT):
                together = RelationReader(root).view_values(driver.parent, column, valid)
                single = RelationReader(root)
                alone = [single.view_value(driver.parent, column, row) for row in valid]
            assert _typed(together) == _typed(alone), (driver.relationship, column)
    with localcontext(_rows._KERNEL_CONTEXT):
        assert RelationReader(root).view_values(child.relations.drivers[0][1].parent, "pi", []) == []  # type: ignore[union-attr]


def test_the_parent_rows_a_column_prefetch_records_are_the_per_row_picks() -> None:
    plans = _kinds()
    root = root_for(seed_tree_version=ROOT_TREE)
    child = plans["child"]
    rows = list(range(CHILD_ROWS))
    reader, _ = _by_column(root, child, ["ci", "cd", "cm", "ct"], rows)
    fresh = RelationReader(root)
    for relationship in "abcd":
        for row in rows:
            assert reader.parent_row(child, relationship, row) == fresh.parent_row(
                child, relationship, row
            ), (relationship, row)


# ---------------------------------------------------------------------------
# Several-column keys
# ---------------------------------------------------------------------------


def test_the_parts_of_a_several_column_key_read_the_same_by_column_and_by_row() -> None:
    plans = _bridge_plans(GENERATOR_VERSION_2)
    root = root_for(seed_tree_version=ROOT_TREE)
    plan = plans["b"]
    assert isinstance(plan.relations.driver("b_x"), CompositeMember)  # type: ignore[union-attr]
    rows = list(range(plan.rows)) + [3, 3, 79, 0]
    _, held = _by_column(root, plan, ["b_x", "b_y"], rows)
    for name in ("b_x", "b_y"):
        assert _typed(held[name]) == _typed(_per_row(root, plan, name, rows)), name
    # A key that points at it reads the parts of that key through its own parent view.
    child = plans["c"]
    rows = list(range(child.rows))
    _, held = _by_column(root, child, ["c_x", "c_y"], rows)
    for name in ("c_x", "c_y"):
        assert _typed(held[name]) == _typed(_per_row(root, child, name, rows)), name


# ---------------------------------------------------------------------------
# The work done
# ---------------------------------------------------------------------------


class _Counter:
    def __init__(self) -> None:
        self.calls: Dict[str, int] = {}

    def wrap(self, monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> None:
        original = getattr(owner, name)

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.calls[name] = self.calls.get(name, 0) + 1
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)


def test_a_column_does_no_per_cell_lookup_for_parents_it_can_read_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = _kinds()
    root = root_for(seed_tree_version=ROOT_TREE)
    counted = _Counter()
    for name in ("view_value", "_view_value", "_cell"):
        counted.wrap(monkeypatch, RelationReader, name)
    for name in ("value_at", "units_at", "row_near"):
        counted.wrap(monkeypatch, KeyGrid, name)
    _, held = _by_column(
        root, plans["child"], ["ci", "cd", "cm", "ct", "ci2"], list(range(CHILD_ROWS))
    )
    assert all(len(values) == CHILD_ROWS for values in held.values())
    # Grid parents, text parents and the declared picks over them: not one call a cell.
    assert counted.calls == {}


def test_a_parent_row_is_worked_out_once_however_many_children_name_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A parent column that is none of the drivers read at once is built by the per-row code,
    # but once for each distinct parent row, not once for each child row.
    plans = _kinds()
    root = root_for(seed_tree_version=ROOT_TREE)
    child = plans["child"]
    driver = child.relations.driver("ci")  # type: ignore[union-attr]
    assert isinstance(driver, ForeignKey)
    view = driver.parent
    counted = _Counter()
    counted.wrap(monkeypatch, RelationReader, "_view_value")
    monkeypatch.setattr(
        RelationReader,
        "_view_batch",
        lambda self, view, column, rows: [self._view_value(view, column, r) for r in rows],
    )
    rows = [4, 7, 4, 4, 7, 9, 9, 9, 4, 7] * 20
    with localcontext(_rows._KERNEL_CONTEXT):
        reader = RelationReader(root)
        values = reader.view_values(view, "pi", rows)
        assert counted.calls == {"_view_value": 3}
        again = reader.view_values(view, "pi", rows)
    assert counted.calls == {"_view_value": 3}  # held for the rest of the slice
    assert again == values
    assert values[:3] == [driver.parent.driver("pi").value_at(4), driver.parent.driver("pi").value_at(7), driver.parent.driver("pi").value_at(4)]  # type: ignore[union-attr]


def test_a_composite_member_converts_each_distinct_part_index_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plans = _bridge_plans(GENERATOR_VERSION_2)
    root = root_for(seed_tree_version=ROOT_TREE)
    counted = _Counter()
    counted.wrap(monkeypatch, RelationReader, "view_value")
    counted.wrap(monkeypatch, RelationReader, "_member")
    counted.wrap(monkeypatch, RelationReader, "_point")
    plan = plans["b"]
    _by_column(root, plan, ["b_x", "b_y"], list(range(plan.rows)))
    assert counted.calls == {}


def test_grid_arrays_are_not_part_of_a_grid_s_identity() -> None:
    grid = KeyGrid("INT", 0, (1,), (10,), (5,), (0,), 5)
    twin = KeyGrid("INT", 0, (1,), (10,), (5,), (0,), 5)
    assert grid.values_at([0, 1]) == [1, 3]
    assert grid == twin and hash(grid) == hash(twin)
    assert "arrays" not in repr(grid)
    assert kernel2.ARRAY_ROWS > 0


@settings(max_examples=200, deadline=None)
@given(
    coefficient=st.integers(-(10**15), 10**15),
    exponent=st.integers(-8, 3),
    scale=st.integers(-3, 6),
)
def test_a_decimal_lands_on_the_grid_point_below_it_by_exact_integers(
    coefficient: int, exponent: int, scale: int
) -> None:
    from fractions import Fraction

    value = Decimal(coefficient).scaleb(exponent)
    scaled = Fraction(value) * Fraction(10) ** scale
    assert (
        _relations._units_of_value("DECIMAL", -scale, value)
        == scaled.numerator // scaled.denominator
    )
    whole = coefficient
    scaled = Fraction(whole) * Fraction(10) ** scale
    assert (
        _relations._units_of_value("DECIMAL", -scale, whole)
        == scaled.numerator // scaled.denominator
    )
