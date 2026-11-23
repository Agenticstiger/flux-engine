# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A slice rendered a column at a time frames the lines the row renderer frames.

:func:`flux_engine.seam.emit.frame_rows` renders and frames a slice through each column's
lookup table or integer and decimal formatting. The row renderer, ``row_line(render_row(row))``,
is the oracle. Over generated rows of every column kind, nulls, edge values and strings that
need escaping, the two give the same bytes, and a row the column renderer does not take, or
that the row renderer refuses, is rendered and refused by the row renderer.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, Context, Decimal, localcontext
from typing import Any, List, Sequence, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core.canonical import JCS_MAX_SAFE_INTEGER
from flux_engine.core.digest import digest_of_stream
from flux_engine.core.errors import FluxError
from flux_engine.seam import conform
from flux_engine.seam.conform import render_columns, render_row
from flux_engine.seam.emit import EmitWriter, frame_rows, row_line
from flux_engine.seam.sinks import NullSink

from .conftest import expose_from_schema

pytestmark = pytest.mark.unit

UTC = timezone.utc

_SCHEMA = [
    {"name": "id", "type": "STRING", "required": True},
    {"name": "tier", "type": "STRING"},
    {"name": "qty", "type": "INT"},
    {"name": "amount", "type": "DECIMAL(12,2)"},
    {"name": "fine", "type": "DECIMAL(30,10)"},
    {"name": "whole", "type": "DECIMAL(9,0)"},
    {"name": "on", "type": "BOOL"},
    {"name": "day", "type": "DATE"},
    {"name": "at", "type": "TIMESTAMP"},
]
_EXPOSE = expose_from_schema(_SCHEMA)

_STRINGS = st.one_of(
    st.sampled_from(["", "gold", "café", "café", 'say "hi"', "back\\slash", "tab\there"]),
    st.text(max_size=12).filter(lambda s: all(not 0xD800 <= ord(c) <= 0xDFFF for c in s)),
)
_AMOUNTS = st.one_of(
    st.sampled_from(
        [
            Decimal(x)
            for x in ("0", "0.00", "-0.00", "-0.001", "0.005", "0.015", "-0.005", "9999999999.99")
        ]
    ),
    st.integers(-(10**11), 10**11).map(lambda u: Decimal(u).scaleb(-2)),
    st.integers(-(10**13), 10**13).map(lambda u: Decimal(u).scaleb(-3)),
)
_FINE = st.integers(-(10**25), 10**25).map(lambda u: Decimal(u).scaleb(-10))
_WHOLE = st.integers(-(10**8), 10**8).map(Decimal)
_DAYS = st.dates(date(1, 1, 1), date(9999, 12, 31))
_INSTANTS = st.one_of(
    st.integers(-(10**12), 10**13),
    st.datetimes(datetime(1970, 1, 1), datetime(2100, 1, 1), timezones=st.just(UTC)).map(
        lambda d: d.replace(microsecond=(d.microsecond // 1000) * 1000)
    ),
)


def _optional(strategy: Any) -> Any:
    return st.one_of(st.none(), strategy)


_ROW = st.tuples(
    _STRINGS,
    _optional(_STRINGS),
    _optional(
        st.one_of(
            st.integers(-(2**31), 2**31),
            st.sampled_from([0, JCS_MAX_SAFE_INTEGER, -JCS_MAX_SAFE_INTEGER]),
        )
    ),
    _optional(_AMOUNTS),
    _optional(_FINE),
    _optional(_WHOLE),
    _optional(st.booleans()),
    _optional(_DAYS),
    _optional(_INSTANTS),
)


def _oracle(rows: Sequence[Tuple[Any, ...]]) -> List[bytes]:
    return [row_line(render_row(row, _EXPOSE, row_index=i)) for i, row in enumerate(rows)]


@pytest.mark.property
@settings(max_examples=150, deadline=None)
@given(rows=st.lists(_ROW, min_size=1, max_size=30))
def test_a_slice_is_framed_as_the_row_renderer_frames_it(rows: List[Tuple[Any, ...]]) -> None:
    assert _outcome(lambda: frame_rows(rows, _EXPOSE)) == _outcome(lambda: _oracle(rows))
    try:
        expected = _oracle(rows)
    except FluxError:
        return
    # Every row of these classes is the column renderer's own: it never leaves one to the rows.
    assert [line.encode() for line in render_columns(rows, _EXPOSE)] == expected


def _outcome(make: Any) -> str:
    """What *make* returns, or the code and message of the refusal it raises."""
    try:
        return repr(make())
    except FluxError as error:
        return f"{error.code.code}: {error}"


def test_a_slice_in_chunks_is_the_slice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conform, "_COLUMN_CHUNK", 3)
    rows = [
        ("a%d" % i, None, i, Decimal(i).scaleb(-2), None, Decimal(i), i % 2 == 0, None, i)
        for i in range(20)
    ]
    assert frame_rows(rows, _EXPOSE) == _oracle(rows)


@pytest.mark.parametrize(
    "context",
    [Context(rounding=ROUND_DOWN), Context(prec=5), Context()],
    ids=["round-down", "five-digits", "default"],
)
def test_the_ambient_decimal_context_decides_a_tie_as_it_does_for_the_row_renderer(
    context: Context,
) -> None:
    rows = [
        ("a", None, 1, Decimal(x), Decimal(x), Decimal(x), None, None, None)
        for x in ("1.005", "2.015", "-3.995", "0.0049")
    ]
    with localcontext(context):
        columns = frame_rows(rows, _EXPOSE)
        per_row = _oracle(rows)
    assert columns == per_row


def test_a_decimal_past_its_precision_is_refused_by_the_row_renderer() -> None:
    rows = [("a", None, 1, Decimal("10000000000.00"), None, None, None, None, None)]
    with pytest.raises(FluxError) as column_error:
        frame_rows(rows, _EXPOSE)
    with pytest.raises(FluxError) as row_error:
        _oracle(rows)
    assert column_error.value.code is row_error.value.code
    assert str(column_error.value) == str(row_error.value)
    assert "row 0, column 'amount'" in str(column_error.value)


@pytest.mark.parametrize(
    "bad",
    [
        ("a", None, True, None, None, None, None, None, None),  # a bool in an INT column
        ("a", None, 1.5, None, None, None, None, None, None),  # a float
        ("a", None, None, 3, None, None, None, None, None),  # an int in a DECIMAL column
        ("a", None, None, None, None, None, 1, None, None),  # an int in a BOOL column
        ("a", None, None, None, None, None, None, datetime(2026, 1, 1), None),  # a datetime in DATE
        ("a", None, 2**60, None, None, None, None, None, None),  # past JCS's integers
        (None, None, None, None, None, None, None, None, None),  # a required column left null
    ],
)
def test_what_the_row_renderer_refuses_or_decides_it_refuses_or_decides(
    bad: Tuple[Any, ...],
) -> None:
    good = ("b", "gold", 2, Decimal("1.50"), None, None, True, date(2026, 1, 2), 5)
    rows = [good, bad, good]
    assert _outcome(lambda: frame_rows(rows, _EXPOSE)) == _outcome(lambda: _oracle(rows))


def test_a_lone_surrogate_is_the_canonical_writers_to_refuse() -> None:
    rows = [("\ud800", None, None, None, None, None, None, None, None)]
    outcomes = []
    for make in (lambda: frame_rows(rows, _EXPOSE), lambda: _oracle(rows)):
        with pytest.raises(FluxError) as error:
            make()
        outcomes.append((error.value.code, str(error.value)))
    assert outcomes[0] == outcomes[1]


def test_a_row_that_is_not_a_tuple_is_rendered_by_the_row_renderer() -> None:
    mapping = {"id": "x", "qty": 3}
    row = ("y", None, None, None, None, None, None, None, None)
    with pytest.raises(ValueError):
        render_columns([mapping, row], _EXPOSE)  # type: ignore[list-item]
    assert frame_rows([mapping, row], _EXPOSE) == [
        row_line(render_row(mapping, _EXPOSE, row_index=0)),
        row_line(render_row(row, _EXPOSE, row_index=1)),
    ]


def test_the_writer_digests_the_same_bytes_either_way() -> None:
    contract = digest_of_stream(b"contract", subject="contract")
    rows = [
        ("id%d" % i, "t", i, Decimal(i).scaleb(-2), None, None, None, None, i) for i in range(50)
    ]
    outputs = []
    for sink in (None, NullSink()):
        writer = EmitWriter(expose=_EXPOSE, contract_digest=contract, sink=sink)
        with writer:
            writer.write_all(rows)
        outputs.append((writer.stream_bytes(), writer.result()))
    reference = EmitWriter(expose=_EXPOSE, contract_digest=contract)
    with reference:
        for row in rows:
            reference.write(row)
    for stream, result in outputs:
        assert stream == reference.stream_bytes()
        assert result.output_digest == reference.result().output_digest
        assert result.rows == 50
