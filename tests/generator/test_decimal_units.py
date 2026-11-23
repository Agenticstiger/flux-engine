# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Decimals held as whole units are built once per distinct value, and built exactly.

The column path draws a grid point as an integer number of units and turns it into a
``Decimal`` at the edge. These tests pin that the conversion is the one the per-row path
makes (compared by ``as_tuple``, since ``Decimal('1.0')`` and ``Decimal('1.00')`` are equal
and render differently) and that a repeated value is converted once, by counting calls.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any, List

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root, digest_bytes
from flux_engine.generator import columns
from flux_engine.generator import rows as _rows
from flux_engine.generator.rows import GENERATOR_VERSION_2, build_plan, generate_rows
from flux_engine.generator.rulearrays import units_to_decimals

from .conftest import SIM_ID, TIME_BOUNDS, H, expose_from_schema, fidelity

pytestmark = pytest.mark.unit

_UNITS = st.integers(min_value=-(10**40), max_value=10**40)


def _reference_from_units(units: int, exponent: int) -> Decimal:
    digits = tuple(int(character) for character in str(abs(units)))
    return Decimal((0 if units >= 0 else 1, digits, exponent))


def _reference_quantise(value: Decimal, scale: int, precision: int) -> Decimal:
    with localcontext(_rows._KERNEL_CONTEXT) as context:
        context.prec = max(_rows._KERNEL_CONTEXT.prec, precision + scale + 40)
        return value.quantize(Decimal(1).scaleb(-scale))


@given(_UNITS, st.integers(min_value=-40, max_value=0))
def test_decimal_from_units_has_the_tuple_constructors_sign_digits_and_exponent(
    units: int, exponent: int
) -> None:
    with localcontext() as context:
        context.prec = 5  # a narrow ambient context must not round it
        built = _rows._decimal_from_units(units, exponent)
    assert built.as_tuple() == _reference_from_units(units, exponent).as_tuple()


@given(_UNITS, st.integers(min_value=0, max_value=30), st.integers(min_value=0, max_value=3))
def test_units_to_decimals_is_decimal_from_units_at_the_negative_scale(
    units: int, scale: int, extra: int
) -> None:
    got = units_to_decimals([units, units, units + extra], scale)
    want = [_rows._decimal_from_units(u, -scale) for u in (units, units, units + extra)]
    assert [d.as_tuple() for d in got] == [d.as_tuple() for d in want]


@given(
    _UNITS,
    st.integers(min_value=-6, max_value=0),
    st.integers(min_value=0, max_value=6),
    st.integers(min_value=1, max_value=38),
)
def test_quantise_keeps_the_exponent_and_digits_quantize_would(
    units: int, exponent: int, scale: int, precision: int
) -> None:
    class _Type:
        pass

    column = type("C", (), {})()
    column.logical_type = _Type()
    column.logical_type.scale = scale
    column.logical_type.precision = precision
    value = _rows._decimal_from_units(units, exponent)
    try:
        want = _reference_quantise(value, scale, precision)
    except Exception as error:
        with pytest.raises(type(error)):
            _rows._quantise(value, column)
        return
    assert _rows._quantise(value, column).as_tuple() == want.as_tuple()


_SCHEMA = [{"name": "spend", "type": "DECIMAL(18,2)"}, {"name": "visits", "type": "INT"}]
_DECLARED = {
    "spend": {
        "range": {"min": "0", "max": "3"},
        "bins": ["0", "1", "3"],
        "binWeights": ["1", "2"],
    },
    "visits": {
        "range": {"min": "0", "max": "3"},
        "bins": ["0", "1", "3"],
        "binWeights": ["1", "2"],
    },
}


def _plan(rows: int) -> Any:
    return build_plan(
        expose_from_schema(list(_SCHEMA)),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=rows,
        fidelity=fidelity(_DECLARED),
        time_bounds=TIME_BOUNDS,
        generator_version=GENERATOR_VERSION_2,
    )


def _tuples(data: Any) -> List[Any]:
    return [tuple(v.as_tuple() if isinstance(v, Decimal) else v for v in row) for row in data]


def test_a_histogram_column_converts_each_distinct_grid_point_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = 3000
    plan = _plan(rows)
    root = derive_root(digest_bytes(H), 42, seed_tree_version=kernel2.SEED_TREE_VERSION)
    calls = 0
    original = _rows._decimal_from_units

    def counting(units: int, exponent: int) -> Decimal:
        nonlocal calls
        calls += 1
        return original(units, exponent)

    monkeypatch.setattr(_rows, "_decimal_from_units", counting)
    by_columns = generate_rows(plan, root=root)
    column_calls = calls
    calls = 0
    with monkeypatch.context() as patch:
        patch.setattr(columns, "column_rows", lambda *args: None)
        by_rows = generate_rows(plan, root=root)
    row_calls = calls
    assert _tuples(by_columns) == _tuples(by_rows)
    assert repr(by_columns) == repr(by_rows)
    # 300 grid points per column at most (0 to 3 at scale 2); the per-row path builds one per row.
    assert column_calls <= 2 * 301
    assert row_calls >= rows


def test_units_to_decimals_builds_each_distinct_value_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: List[str] = []
    real = Decimal

    class Counting(real):  # type: ignore[misc, valid-type]
        def __new__(cls, value: Any = "0", *args: Any, **kwargs: Any) -> Decimal:
            built.append(str(value))
            return real.__new__(real, value, *args, **kwargs)

    from flux_engine.generator import rulearrays

    monkeypatch.setattr(rulearrays, "Decimal", Counting)
    out = units_to_decimals([5, 7, 5, 5, 7, -1], 2)
    assert len(built) == 3
    assert [d.as_tuple() for d in out] == [
        real("5E-2").as_tuple(),
        real("7E-2").as_tuple(),
        real("5E-2").as_tuple(),
        real("5E-2").as_tuple(),
        real("7E-2").as_tuple(),
        real("-1E-2").as_tuple(),
    ]
