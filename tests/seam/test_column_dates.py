# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""DATE and TIMESTAMP columns render a column at a time to the bytes the row renderer writes.

The row renderer, ``row_line(render_row(row))``, is the oracle. The column renderer asks
the clock for the text of a day once per distinct day and builds the time of day with
integer arithmetic, so these tests pin both halves: the bytes over edge instants, and the
work done.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, List, Sequence, Tuple

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core.clock import format_rfc3339_ms
from flux_engine.seam import conform
from flux_engine.seam.conform import render_columns, render_row
from flux_engine.seam.emit import row_line

from .conftest import expose_from_schema

pytestmark = pytest.mark.unit

_EXPOSE = expose_from_schema(
    [
        {"name": "day", "type": "DATE"},
        {"name": "at", "type": "TIMESTAMP"},
        {"name": "name", "type": "STRING"},
    ]
)

_DAY_MS = 86_400_000
_MIN_MS = -62_135_596_800_000
_MAX_MS = 253_402_300_799_999

_EDGE_INSTANTS = [
    0,
    1,
    999,
    1000,
    -1,
    -999,
    -1000,
    -_DAY_MS,
    -_DAY_MS - 1,
    _DAY_MS - 1,
    _DAY_MS,
    951_782_399_999,  # 2000-02-28T23:59:59.999Z, the eve of a leap day
    951_782_400_000,  # 2000-02-29T00:00:00.000Z
    1_709_251_199_999,  # 2024-02-29T23:59:59.999Z
    1_767_225_599_999,  # 2025-12-31T23:59:59.999Z, the end of a year
    1_767_225_600_000,  # 2026-01-01T00:00:00.000Z
    -2_208_988_800_000,  # 1900-01-01T00:00:00.000Z, not a leap year
    -2_203_891_200_000 - 1,
    _MIN_MS,
    _MIN_MS + 1,
    _MAX_MS,
    _MAX_MS - 1,
]
_EDGE_DAYS = [
    date(1, 1, 1),
    date(999, 12, 31),
    date(1000, 1, 1),
    date(1900, 2, 28),
    date(1969, 12, 31),
    date(1970, 1, 1),
    date(2000, 2, 29),
    date(2024, 12, 31),
    date(9999, 12, 31),
]


def _oracle(rows: Sequence[Tuple[Any, ...]]) -> List[bytes]:
    return [row_line(render_row(row, _EXPOSE, row_index=i)) for i, row in enumerate(rows)]


def _columns(rows: Sequence[Tuple[Any, ...]]) -> List[bytes]:
    return [line.encode() for line in render_columns(rows, _EXPOSE)]


def test_edge_instants_and_days_are_the_row_renderers_bytes() -> None:
    size = max(len(_EDGE_INSTANTS), len(_EDGE_DAYS))
    rows = [
        (
            _EDGE_DAYS[i % len(_EDGE_DAYS)],
            _EDGE_INSTANTS[i % len(_EDGE_INSTANTS)],
            "r%d" % i,
        )
        for i in range(size)
    ]
    assert _columns(rows) == _oracle(rows)


def test_nulls_among_instants_are_the_row_renderers_bytes() -> None:
    rows = [(None, None, "a"), (date(2026, 7, 1), 5, "b"), (None, -5, "c"), (None, None, "d")]
    assert _columns(rows) == _oracle(rows)


def test_every_second_of_a_day_is_the_clocks_spelling() -> None:
    # The time of day is integer arithmetic; the clock is the oracle for all 86 400 seconds.
    # A day and a half-open stride of the milliseconds covers each of them.
    values = [s * 1000 + (s % 1000) for s in range(86_400)]
    tokens = conform._instant_tokens(values)
    assert tokens == ['"' + format_rfc3339_ms(v) + '"' for v in values]


def test_every_second_of_a_day_before_the_epoch_is_the_clocks_spelling() -> None:
    values = [-_DAY_MS + s * 1000 + (s * 7 % 1000) for s in range(86_400)]
    assert conform._instant_tokens(values) == ['"' + format_rfc3339_ms(v) + '"' for v in values]


@pytest.mark.parametrize("value", [_MIN_MS - 1, _MAX_MS + 1, 10**30, -(10**30)])
def test_an_instant_the_clock_cannot_spell_is_left_to_the_row_renderer(value: int) -> None:
    # Not taken by the column renderer's integer path: the table path and then the row
    # renderer decide what an instant outside the clock's range raises.
    assert not conform._instants_in_range([0, value], False)
    assert conform._instants_in_range([_MIN_MS, _MAX_MS], False)


def test_a_timestamp_column_with_a_string_or_a_datetime_keeps_its_table() -> None:
    rows = [
        (date(2026, 7, 1), 0, "a"),
        (date(2026, 7, 1), "2026-07-01T00:00:00Z", "b"),
        (date(2026, 7, 1), datetime(2026, 7, 1, 1, 2, 3, tzinfo=timezone.utc), "c"),
    ]
    assert _columns(rows) == _oracle(rows)


@pytest.mark.property
@settings(max_examples=150, deadline=None)
@given(
    rows=st.lists(
        st.tuples(
            st.dates(date(1, 1, 1), date(9999, 12, 31)),
            st.one_of(st.none(), st.integers(_MIN_MS, _MAX_MS)),
            st.text(max_size=6).filter(lambda s: all(not 0xD800 <= ord(c) <= 0xDFFF for c in s)),
        ),
        min_size=1,
        max_size=30,
    )
)
def test_any_instant_in_the_clocks_range_is_the_row_renderers_bytes(
    rows: List[Tuple[Any, ...]],
) -> None:
    assert _columns(rows) == _oracle(rows)


def test_the_clock_formats_one_instant_per_distinct_day(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[int] = []
    real = conform.format_rfc3339_ms

    def counting(t_ms: int) -> str:
        calls.append(t_ms)
        return real(t_ms)

    monkeypatch.setattr(conform, "format_rfc3339_ms", counting)
    start = 1_782_864_000_000  # a midnight
    rows = [(date(2026, 7, 1), start + i * 977, "x") for i in range(3000)]
    distinct_days = len({row[1] // _DAY_MS for row in rows})
    assert distinct_days < len(rows)
    assert _columns(rows) == _oracle_without_counting(rows, monkeypatch, real)
    assert len(calls) == distinct_days


def _oracle_without_counting(
    rows: Sequence[Tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch, real: Any
) -> List[bytes]:
    monkeypatch.setattr(conform, "format_rfc3339_ms", real)
    return _oracle(rows)


def test_the_date_renderer_runs_once_per_distinct_day(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Any] = []
    real = conform._render_date

    def counting(value: Any) -> str:
        calls.append(value)
        return real(value)

    monkeypatch.setattr(conform, "_render_date", counting)
    days = [date(2026, 7, 1 + i % 5) for i in range(500)]
    rows = [(day, 0, "x") for day in days]
    assert len(render_columns(rows, _EXPOSE)) == 500
    assert len(calls) == len(set(days)) == 5
