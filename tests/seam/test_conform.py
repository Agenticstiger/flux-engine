# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §9, second half: value rendering and the closed-world row check.

The rendering table is pinned type by type, and three properties are
pinned over generated input:

* **a float is always ``FLX-3002``**, at any depth and in any column type;
* **no diagnostic ever carries the value** -- §9's last clause, checked by
  generating values whose ``repr`` is a recognisable marker and asserting
  the marker never appears in the message;
* **rendering is idempotent in the ways that matter**: a rendered value is
  JSON-ready (``str``/``int``/``bool``/``None``, never ``float``), so an
  emitted row can always be canonicalised.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.errors import FluxError
from flux_engine.seam.conform import render_cell, render_row, render_rows
from flux_engine.seam.contract import CompiledExpose, LogicalType, parse_logical_type

from .conftest import expose_from_schema

pytestmark = pytest.mark.unit

UTC = timezone.utc


def _type(declared: str) -> LogicalType:
    return parse_logical_type(declared)


# ---------------------------------------------------------------------------
# §9's rendering table, entry by entry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "value", "expected"),
    [
        # STRING -> NFC string
        ("STRING", "plain", "plain"),
        ("STRING", "café", "café"),
        ("STRING", "", ""),
        # INT -> JSON integer
        ("INT", 0, 0),
        ("INT", -17, -17),
        # DECIMAL(p,s) -> string at scale s
        ("DECIMAL(18,4)", Decimal("12.5"), "12.5000"),
        ("DECIMAL(18,4)", Decimal("12.50000"), "12.5000"),
        ("DECIMAL(18,4)", 7, "7.0000"),
        ("DECIMAL(4,0)", Decimal("-3"), "-3"),
        ("DECIMAL(18,4)", Decimal("-0.00001"), "0.0000"),
        # half-to-even, the rounding a SQL cast applies
        ("DECIMAL(4,1)", Decimal("0.25"), "0.2"),
        ("DECIMAL(4,1)", Decimal("0.35"), "0.4"),
        # BOOL -> JSON bool
        ("BOOL", True, True),
        ("BOOL", False, False),
        # DATE -> YYYY-MM-DD
        ("DATE", date(2026, 7, 1), "2026-07-01"),
        ("DATE", "2026-07-01", "2026-07-01"),
        # TIMESTAMP -> RFC 3339 UTC, milliseconds, Z
        ("TIMESTAMP", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00.000Z"),
        ("TIMESTAMP", "2026-07-01T02:00:00+02:00", "2026-07-01T00:00:00.000Z"),
        ("TIMESTAMP", datetime(2026, 7, 1, tzinfo=UTC), "2026-07-01T00:00:00.000Z"),
        (
            "TIMESTAMP",
            datetime(2026, 7, 1, 2, 0, tzinfo=timezone(timedelta(hours=2))),
            "2026-07-01T00:00:00.000Z",
        ),
        ("TIMESTAMP", 0, "1970-01-01T00:00:00.000Z"),
        ("TIMESTAMP", 1_782_000_000_000, "2026-06-21T00:00:00.000Z"),
        # JSON -> canonical JSON string
        ("JSON", {"b": 1, "a": 2}, '{"a":2,"b":1}'),
        ("JSON", [1, "two", None, True], '[1,"two",null,true]'),
        ("JSON", {"n": Decimal("1.50")}, '{"n":"1.50"}'),
        ("JSON", "café", '"café"'),
        # BYTES -> base64
        ("BYTES", b"hi", "aGk="),
        ("BYTES", bytearray(b""), ""),
        # null renders as JSON null for every type
        ("STRING", None, None),
        ("DECIMAL(18,4)", None, None),
        ("JSON", None, None),
    ],
)
def test_the_rendering_table(declared: str, value: Any, expected: Any) -> None:
    assert render_cell(value, _type(declared)) == expected


def test_bytes_rendering_round_trips_through_base64() -> None:
    payload = bytes(range(256))
    rendered = render_cell(payload, _type("BYTES"))
    assert base64.b64decode(rendered) == payload


@given(
    value=st.one_of(
        st.text(),
        st.integers(min_value=-(2**52), max_value=2**52),
        st.booleans(),
        st.none(),
        st.decimals(
            allow_nan=False, allow_infinity=False, places=4, min_value=-1000, max_value=1000
        ),
    )
)
def test_a_rendered_value_is_always_json_ready(value: Any) -> None:
    """Whatever renders, canonicalises. That is what makes §8's line() total."""
    declared = {
        str: "STRING",
        int: "INT",
        bool: "BOOL",
        Decimal: "DECIMAL(18,4)",
        type(None): "STRING",
    }[type(value)]
    rendered = render_cell(value, _type(declared))
    assert not isinstance(rendered, float)
    canonical_bytes([rendered])


# ---------------------------------------------------------------------------
# float -> FLX-3002
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared", ["STRING", "INT", "DECIMAL(18,4)", "BOOL", "DATE", "TIMESTAMP", "JSON", "BYTES"]
)
def test_a_float_is_flx_3002_in_every_column_type(declared: str) -> None:
    """§9 gives a float its own code, so it is reported as a float everywhere."""
    with pytest.raises(FluxError) as error:
        render_cell(1.5, _type(declared))
    assert error.value.code.code == "FLX-3002"


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        [1.5],
        [[{"deep": [0.0]}]],
        {"a": {"b": [1, 2, 3.0]}},
        {1.5: "a float key"},
        (1, 2.0),
    ],
)
def test_a_float_at_any_depth_of_a_json_column_is_flx_3002(value: Any) -> None:
    with pytest.raises(FluxError) as error:
        render_cell(value, _type("JSON"))
    assert error.value.code.code == "FLX-3002"


def test_a_float_in_a_row_names_the_column_and_the_row() -> None:
    expose = expose_from_schema([{"name": "a", "type": "INT"}, {"name": "b", "type": "INT"}])
    with pytest.raises(FluxError) as error:
        render_row({"a": 1, "b": 2.5}, expose, row_index=7)
    message = str(error.value)
    assert error.value.code.code == "FLX-3002"
    assert "row 7" in message
    assert "'b'" in message
    assert "2.5" not in message


# ---------------------------------------------------------------------------
# Wrong value classes -> FLX-3003
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "value"),
    [
        ("STRING", 1),
        ("STRING", b"bytes"),
        ("INT", "1"),
        ("INT", True),
        ("INT", Decimal("1")),
        ("DECIMAL(18,4)", "1.5"),
        ("DECIMAL(18,4)", True),
        ("DECIMAL(18,4)", Decimal("NaN")),
        ("DECIMAL(4,2)", Decimal("100")),
        ("BOOL", 1),
        ("BOOL", "true"),
        ("DATE", datetime(2026, 7, 1, tzinfo=UTC)),
        ("DATE", "2026-7-01"),
        ("DATE", "20260701"),
        ("DATE", "2026-02-30"),
        ("DATE", 19_000),
        ("TIMESTAMP", datetime(2026, 7, 1)),
        ("TIMESTAMP", True),
        ("TIMESTAMP", date(2026, 7, 1)),
        ("BYTES", "aGk="),
        ("JSON", {1, 2}),
        ("JSON", b"raw"),
    ],
)
def test_a_value_outside_its_declared_type_is_flx_3003(declared: str, value: Any) -> None:
    with pytest.raises(FluxError) as error:
        render_cell(value, _type(declared))
    assert error.value.code.code == "FLX-3003"


def test_a_microsecond_timestamp_is_refused_rather_than_truncated() -> None:
    """The clock is integer milliseconds; truncating would lose a declaration."""
    with pytest.raises(FluxError) as error:
        render_cell(datetime(2026, 7, 1, 0, 0, 0, 123_456, tzinfo=UTC), _type("TIMESTAMP"))
    assert error.value.code.code == "FLX-3003"


def test_a_decimal_that_rounding_cannot_fit_is_refused_not_truncated() -> None:
    with pytest.raises(FluxError) as error:
        render_cell(Decimal("12345.6"), _type("DECIMAL(6,2)"))
    assert error.value.code.code == "FLX-3003"
    assert "integer digits" in str(error.value)


def test_a_decimal_that_rounding_does_fit_is_rounded() -> None:
    assert render_cell(Decimal("9999.994"), _type("DECIMAL(6,2)")) == "9999.99"


def test_a_decimal_that_rounding_pushes_over_the_precision_is_refused() -> None:
    """Rounding happens first, and the precision check sees the rounded value.

    ``9999.999`` fits ``DECIMAL(6,2)``'s four integer digits before rounding
    and carries five after it. The rendered value is what the stream would
    hold, so it is the rendered value the precision is checked against --
    the alternative is a stream carrying a number its own declared type
    cannot represent.
    """
    with pytest.raises(FluxError) as error:
        render_cell(Decimal("9999.999"), _type("DECIMAL(6,2)"))
    assert error.value.code.code == "FLX-3003"


# ---------------------------------------------------------------------------
# The closed world
# ---------------------------------------------------------------------------


@pytest.fixture
def two_columns() -> CompiledExpose:
    return expose_from_schema(
        [
            {"name": "id", "type": "STRING", "required": True},
            {"name": "note", "type": "STRING"},
        ]
    )


def test_a_mapping_row_renders_into_declared_order(two_columns: CompiledExpose) -> None:
    assert render_row({"note": "n", "id": "i"}, two_columns) == ("i", "n")


def test_a_positional_row_is_taken_as_given(two_columns: CompiledExpose) -> None:
    assert render_row(["i", "n"], two_columns) == ("i", "n")


def test_an_undeclared_column_is_flx_3201(two_columns: CompiledExpose) -> None:
    with pytest.raises(FluxError) as error:
        render_row({"id": "i", "extra": "x"}, two_columns, row_index=3)
    message = str(error.value)
    assert error.value.code.code == "FLX-3201"
    assert "row 3" in message
    assert "'extra'" in message


def test_a_non_string_row_key_is_flx_3201(two_columns: CompiledExpose) -> None:
    with pytest.raises(FluxError) as error:
        render_row({7: "x"}, two_columns)
    assert error.value.code.code == "FLX-3201"


def test_two_row_keys_that_normalise_together_are_flx_3201() -> None:
    expose = expose_from_schema([{"name": "café", "type": "STRING"}])
    with pytest.raises(FluxError) as error:
        render_row({"café": "a", "café": "b"}, expose)
    assert error.value.code.code == "FLX-3201"


def test_a_row_key_in_the_other_spelling_still_finds_its_column() -> None:
    expose = expose_from_schema([{"name": "café", "type": "STRING"}])
    assert render_row({"café": "a"}, expose) == ("a",)


@pytest.mark.parametrize("row", [{}, {"note": "n"}, {"id": None, "note": "n"}, [None, "n"]])
def test_a_missing_required_column_is_flx_3202(two_columns: CompiledExpose, row: Any) -> None:
    """Absent and null are one state: neither carries a value."""
    with pytest.raises(FluxError) as error:
        render_row(row, two_columns, row_index=2)
    message = str(error.value)
    assert error.value.code.code == "FLX-3202"
    assert "row 2" in message
    assert "'id'" in message


def test_an_absent_optional_column_renders_as_null(two_columns: CompiledExpose) -> None:
    assert render_row({"id": "i"}, two_columns) == ("i", None)


@pytest.mark.parametrize("row", [[], ["only"], ["a", "b", "c"]])
def test_a_positional_row_of_the_wrong_length_is_flx_3203(
    two_columns: CompiledExpose, row: List[Any]
) -> None:
    with pytest.raises(FluxError) as error:
        render_row(row, two_columns, row_index=1)
    assert error.value.code.code == "FLX-3203"
    assert "2 columns" in str(error.value)


@pytest.mark.parametrize("row", ["a string", b"bytes", 7, None])
def test_a_row_that_is_neither_mapping_nor_sequence_is_refused(
    two_columns: CompiledExpose, row: Any
) -> None:
    with pytest.raises(FluxError) as error:
        render_row(row, two_columns, row_index=0)
    assert error.value.code.code == "FLX-3003"


def test_render_rows_numbers_its_diagnostics_from_zero(two_columns: CompiledExpose) -> None:
    rows: List[Dict[str, Any]] = [{"id": "a"}, {"id": "b"}, {"note": "c"}]
    with pytest.raises(FluxError) as error:
        render_rows(rows, two_columns)
    assert "row 2" in str(error.value)


def test_render_rows_renders_every_row_in_order(two_columns: CompiledExpose) -> None:
    assert render_rows([{"id": "a"}, {"id": "b", "note": "n"}], two_columns) == (
        ("a", None),
        ("b", "n"),
    )


# ---------------------------------------------------------------------------
# §9's last clause: a diagnostic never carries the value
# ---------------------------------------------------------------------------


class _Marked:
    """A value whose every textual form contains a marker a test can look for."""

    MARKER = "SENSITIVE-PAYLOAD-9d3f"

    def __repr__(self) -> str:
        return self.MARKER

    def __str__(self) -> str:
        return self.MARKER


@pytest.mark.parametrize(
    "declared", ["STRING", "INT", "DECIMAL(18,4)", "BOOL", "DATE", "TIMESTAMP", "JSON", "BYTES"]
)
def test_no_diagnostic_ever_carries_the_value(declared: str) -> None:
    """KERNEL-SPEC §9: errors name "value **class**, never the value"."""
    expose = expose_from_schema([{"name": "col", "type": declared}])
    with pytest.raises(FluxError) as error:
        render_row({"col": _Marked()}, expose, row_index=4)
    message = str(error.value)
    assert _Marked.MARKER not in message
    assert "_Marked" in message
    assert "row 4" in message
    assert "'col'" in message


@given(text=st.text(min_size=1))
# Both texts are part of "got str", so assume() skips them under the str
# control below. They are there to fail if the control ever loses that wording.
@example(" s")
@example("str")
def test_a_bad_string_value_is_never_quoted_back(text: str) -> None:
    """Generated over real strings: the message names the class, not the text.

    The message is first produced for a *different* bad value, and the
    generated text is only tested when it is not already a substring of
    that fixed template -- otherwise a single generated character such as
    ``"0"`` would "appear in the message" by way of the row index, and the
    property would fail without anything having leaked.

    That control value is a ``str`` too: the marker string, which the
    ``replace`` strips again should it ever be quoted back. So the template
    ends in the same "got str" that every generated text meets. A control
    of another class would end it in that class's name instead, and a text
    such as ``" s"`` or ``"str"``, part of "got str" but not of the
    template, would fail the property although nothing leaked.
    """
    expose = expose_from_schema([{"name": "n", "type": "INT"}])
    with pytest.raises(FluxError) as control:
        render_row({"n": _Marked.MARKER}, expose, row_index=0)
    template = str(control.value).replace(_Marked.MARKER, "")
    assume(text not in template)

    with pytest.raises(FluxError) as error:
        render_row({"n": text}, expose, row_index=0)
    assert text not in str(error.value)
    assert "INT requires an int" in str(error.value)
