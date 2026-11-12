# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``canonical_array_bytes`` is ``canonical_bytes`` for a flat array of JSON scalars.

The emit stream frames every row with it, so it is held to the bytes and to
the refusals of the canonicaliser it shortcuts: generated arrays, every escape
class, the edges of the JCS integer domain, and the values that must take the
long way round.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, List

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core.canonical import (
    JCS_MAX_SAFE_INTEGER,
    canonical_array_bytes,
    canonical_bytes,
)
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit

_SCALARS = st.one_of(
    st.text(),
    st.integers(min_value=-JCS_MAX_SAFE_INTEGER, max_value=JCS_MAX_SAFE_INTEGER),
    st.booleans(),
    st.none(),
)


@given(values=st.lists(_SCALARS, max_size=12))
@settings(max_examples=400)
def test_a_flat_array_of_scalars_has_the_bytes_canonical_bytes_gives(values: List[Any]) -> None:
    expected = canonical_bytes(list(values))
    assert canonical_array_bytes(values) == expected
    assert canonical_array_bytes(tuple(values)) == expected


@pytest.mark.parametrize(
    "values",
    [
        [],
        [""],
        ["e\u0301", "\u00e9"],  # NFC'd, as canonicalise does
        ["\x00\x01\x08\x09\x0a\x0c\x0d\x1f", '"', "\\", "/"],  # every escape class
        ["\x7f\u0080\u2028\u2029\ufeff\U0001f600"],  # written as themselves
        [JCS_MAX_SAFE_INTEGER, -JCS_MAX_SAFE_INTEGER, 0, -0, 1],
        [True, False, None],
        ["12.5000", None, 7, "IE", True],  # a rendered row
    ],
)
def test_the_edges_of_each_scalar_class(values: List[Any]) -> None:
    assert canonical_array_bytes(values) == canonical_bytes(list(values))


@pytest.mark.parametrize(
    "values",
    [
        [JCS_MAX_SAFE_INTEGER + 1],
        [-JCS_MAX_SAFE_INTEGER - 1],
        ["ok", "\ud800"],  # a lone surrogate is not UTF-8
        [1.5],
        [Decimal("NaN")],
    ],
)
def test_a_value_jcs_refuses_is_refused_the_same_way(values: List[Any]) -> None:
    with pytest.raises(FluxError) as expected:
        canonical_bytes(list(values))
    with pytest.raises(FluxError) as refused:
        canonical_array_bytes(values)
    assert refused.value.code == expected.value.code
    assert str(refused.value) == str(expected.value)


class _Text(str):
    pass


class _Number(int):
    pass


@pytest.mark.parametrize(
    "values",
    [
        [Decimal("1.50")],
        [["nested", 1]],
        [{"b": 1, "a": 2}],
        [_Text("x")],
        [_Number(3)],
        ("tuple", [None]),
    ],
)
def test_anything_else_takes_the_canonical_path(values: Any) -> None:
    assert canonical_array_bytes(values) == canonical_bytes(list(values))
