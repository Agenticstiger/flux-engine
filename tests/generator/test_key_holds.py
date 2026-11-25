# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which keys a parent holds in a plan, as a tracked foreign key's levels are asked (ADR 0017 section 6).

``KeyGrid.holds`` says whether some row's key is a value, ``reference`` columns say it by their
published values, and ``_key_span`` names what a parent holds when a level is not one of its keys.
The run through ``plan``, ``apply`` and ``verify`` is in ``tests/stages/test_scale_tracked_keys.py``.
"""

from __future__ import annotations

from decimal import localcontext
from typing import List

from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.generator import relations as _relations
from flux_engine.generator import rows as _rows
from flux_engine.generator.reference import ReferenceColumn
from flux_engine.generator.relations import KeyGrid

from .test_column_lookups import _grids


@settings(max_examples=150, deadline=None)
@given(grid=_grids(), probes=st.lists(st.integers(-20, 400), min_size=1, max_size=40))
def test_a_grid_holds_exactly_the_keys_its_rows_take(grid: KeyGrid, probes: List[int]) -> None:
    low = grid.firsts[0]
    with localcontext(_rows._KERNEL_CONTEXT):
        taken = {grid.units_at(row) for row in range(grid.rows)}
        for probe in probes:
            units = low + probe
            value = _relations._value_of_units(grid.kind, grid.exponent, units)
            assert grid.holds(value) is (units in taken)


def test_a_grid_that_skips_keys_holds_the_ones_it_takes_and_not_the_ones_between() -> None:
    # Three rows over the grid points 10 to 15: 10, 12 and 14.
    grid = KeyGrid("INT", 0, (10,), (6,), (3,), (0,), 3)
    assert [grid.holds(value) for value in range(8, 17)] == [
        False, False, True, False, True, False, True, False, False
    ]  # fmt: skip


def test_the_span_names_the_first_and_last_key_and_the_rows() -> None:
    grid = KeyGrid("INT", 0, (101,), (7,), (7,), (0,), 7)
    assert _relations._key_span(grid) == "keys 101 to 107, 7 rows"
    assert (
        _relations._key_span(KeyGrid("INT", 0, (5,), (1,), (1,), (0,), 1)) == "keys 5 to 5, 1 row"
    )


def test_a_reference_column_holds_what_it_publishes_and_its_span_is_the_published_range() -> None:
    column = ReferenceColumn(column="code", values=(30, 10, None, 20), digest="sha256:0")
    assert column.holds(20) and not column.holds(25)
    assert _relations._key_span(column) == "keys 10 to 30, 4 rows"
    empty = ReferenceColumn(column="code", values=(None,), digest="sha256:0")
    assert _relations._key_span(empty) == "no key, 1 row"
