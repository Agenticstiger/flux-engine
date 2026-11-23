# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The record reader, without the engine: every value class, null, group and refusal.

flux-sim-engine's fidelity check reads its records through :mod:`flux_stats.reading`,
and a profiler does too, so these tests pin the reading itself: each diagnostic in
full (it names a column, a row and a value class, never a value), each null policy,
the group order, and the instant parser, which reads the same on every Python this
package supports. The columns are a small dataclass, as a profiler's would be.
"""

from __future__ import annotations

import functools
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from flux_stats.errors import FluxError, catalog
from flux_stats.fixedpoint import Grid, Interval
from flux_stats.reading import (
    FLX_5022,
    FLX_5031,
    UNGROUPED,
    Column,
    NamedColumn,
    NullPolicy,
    ValueColumn,
    ValueDomain,
    category,
    cell,
    digits_written_out,
    encode_value,
    exact,
    group_keys,
    level_cells,
    nfc,
    parse_rfc3339_ms,
    read_column,
    read_exact,
    read_pairs,
)

pytestmark = pytest.mark.unit

GRID = Grid(fractional_bits=16)


@dataclass(frozen=True)
class Col:
    """A column declaration of the shape :class:`flux_stats.reading.Column` names."""

    name: str
    null_policy: NullPolicy = NullPolicy.EXCLUDE
    domain: ValueDomain = ValueDomain.NUMBER
    interval: Interval = field(default_factory=lambda: Interval(Fraction(0), Fraction(10)))
    support_values: Tuple[str, ...] = ()


def _refusal(error: pytest.ExceptionInfo[FluxError]) -> Tuple[str, str]:
    return error.value.code.code, error.value.detail


# ---------------------------------------------------------------------------
# The closed sets and the codes
# ---------------------------------------------------------------------------


def test_the_closed_sets_spell_the_declaration_words() -> None:
    assert [policy.value for policy in NullPolicy] == ["exclude", "fail"]
    assert [domain.value for domain in ValueDomain] == ["number", "timestamp", "date"]
    assert NullPolicy("fail") is NullPolicy.FAIL
    assert ValueDomain("timestamp") is ValueDomain.TIMESTAMP


def test_the_reader_registers_its_two_codes() -> None:
    codes = {entry.code: entry for entry in catalog()}
    assert codes["FLX-5022"] is FLX_5022
    assert codes["FLX-5031"] is FLX_5031


def test_the_ungrouped_key_is_the_empty_string() -> None:
    assert UNGROUPED == ""


def test_each_column_protocol_widens_the_one_before() -> None:
    assert NamedColumn in ValueColumn.__mro__
    assert ValueColumn in Column.__mro__


# ---------------------------------------------------------------------------
# nfc
# ---------------------------------------------------------------------------


def test_nfc_composes_and_leaves_composed_text_alone() -> None:
    assert nfc("é") == "é"
    assert nfc("é") == "é"
    assert nfc("plain") == "plain"


# ---------------------------------------------------------------------------
# parse_rfc3339_ms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1970-01-01T00:00:00Z", 0),
        ("1970-01-01t00:00:00z", 0),
        ("1970-01-01T00:00:00.001Z", 1),
        ("1969-12-31T23:59:59.999Z", -1),
        ("2000-01-01T00:00:00Z", 946_684_800_000),
        ("2024-02-29T12:00:00Z", 1_709_208_000_000),
        ("0001-01-01T00:00:00Z", -62_135_596_800_000),
        ("9999-12-31T23:59:59.999Z", 253_402_300_799_999),
        # An offset moves the instant to UTC, whichever its sign and its hours.
        ("1970-01-01T01:00:00+01:00", 0),
        ("1969-12-31T19:00:00-05:00", 0),
        ("1970-01-01T05:45:00+05:45", 0),
        ("1970-01-01T12:30:00+12:30", 0),
        ("1969-12-31T11:30:00-12:30", 0),
        ("1970-01-01T00:00:00-00:00", 0),
        # Offset minutes add to the hours, as datetime.fromisoformat read them on
        # Python 3.11 to 3.13.
        ("1970-01-01T01:00:00+00:60", 0),
        # A fraction of any length, read as milliseconds, with zeros beyond them.
        ("1970-01-01T00:00:00.1Z", 100),
        ("1970-01-01T00:00:00.12Z", 120),
        ("1970-01-01T00:00:00.123Z", 123),
        ("1970-01-01T00:00:00.1230Z", 123),
        ("1970-01-01T00:00:00.123000000Z", 123),
        ("1970-01-01T00:00:00.0Z", 0),
        # Still inside the years 1 to 9999 once moved to UTC.
        ("0001-01-01T00:00:00-00:01", -62_135_596_740_000),
        ("9999-12-31T23:58:59+00:01", 253_402_300_679_000),
    ],
)
def test_an_rfc3339_date_time_reads_as_integer_milliseconds(text: str, expected: int) -> None:
    parsed = parse_rfc3339_ms(text)
    assert parsed == expected
    assert type(parsed) is int


NOT_RFC3339 = "timestamp is not an RFC 3339 date-time with an offset"
SUB_MILLISECOND = (
    "timestamp carries sub-millisecond precision, which the virtual clock does not represent"
)
INVALID = "timestamp is not a valid date-time"
OUTSIDE = "timestamp falls outside the years 1 to 9999 once moved to UTC"


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("2024-01-01", NOT_RFC3339),
        ("2024-01-01T00:00:00", NOT_RFC3339),
        ("2024-01-01 00:00:00Z", NOT_RFC3339),
        ("2024-01-01T00:00Z", NOT_RFC3339),
        ("2024-01-01T00:00:00+0100", NOT_RFC3339),
        ("2024-01-01T00:00:00Z ", NOT_RFC3339),
        ("2024-01-01T00:00:00.Z", NOT_RFC3339),
        ("", NOT_RFC3339),
        ("1970-01-01T00:00:00.1234Z", SUB_MILLISECOND),
        ("1970-01-01T00:00:00.0001Z", SUB_MILLISECOND),
        ("1970-01-01T00:00:00.9990001Z", SUB_MILLISECOND),
        ("2023-02-29T00:00:00Z", INVALID),
        ("2024-13-01T00:00:00Z", INVALID),
        ("2024-01-00T00:00:00Z", INVALID),
        ("0000-01-01T00:00:00Z", INVALID),
        # Hour 24, which datetime.fromisoformat accepts from Python 3.14 on.
        ("2024-01-01T24:00:00Z", INVALID),
        ("2024-01-01T23:60:00Z", INVALID),
        ("2024-01-01T23:59:60Z", INVALID),
        ("2024-01-01T00:00:00+24:00", INVALID),
        ("2024-01-01T00:00:00-23:60", INVALID),
        # Digits of another script, and a trailing newline: the pattern's \d and $
        # admit them, the read does not.
        ("٢٠٢٤-01-01T00:00:00Z", INVALID),
        ("2024-01-01T00:00:00.１Z", INVALID),
        ("2024-01-01T00:00:00+٠٥:00", INVALID),
        ("1970-01-01T00:00:00Z\n", INVALID),
        # A real date-time whose UTC instant has no year in 1 to 9999.
        ("0001-01-01T00:00:00+00:01", OUTSIDE),
        ("9999-12-31T23:59:59-00:01", OUTSIDE),
    ],
)
def test_a_timestamp_the_clock_cannot_hold_is_refused(text: str, detail: str) -> None:
    with pytest.raises(FluxError) as caught:
        parse_rfc3339_ms(text)
    assert _refusal(caught) == ("FLX-5022", detail)


@pytest.mark.parametrize(("value", "name"), [(0, "int"), (None, "NoneType"), (b"x", "bytes")])
def test_a_timestamp_that_is_not_a_string_is_refused(value: Any, name: str) -> None:
    with pytest.raises(FluxError) as caught:
        parse_rfc3339_ms(value)
    assert _refusal(caught) == ("FLX-5022", f"timestamp must be a string, got {name}")


def _fromisoformat_reading(text: str) -> int:
    """The parser this module replaced, kept as the oracle for the test below.

    It called ``datetime.fromisoformat``, whose accepted forms change between
    Python versions, so it is only an oracle on 3.11 to 3.13, where the engine
    is tested. The one intended difference: it let an ``OverflowError`` escape.
    """
    match = re.match(
        r"^(?P<date>\d{4}-\d{2}-\d{2})[Tt](?P<time>\d{2}:\d{2}:\d{2})"
        r"(?P<fraction>\.\d+)?(?P<offset>[Zz]|[+-]\d{2}:\d{2})$",
        text,
    )
    if match is None:
        raise FluxError(FLX_5022, NOT_RFC3339)
    fraction = match.group("fraction")
    if fraction is not None and len(fraction) > 4 and set(fraction[4:]) != {"0"}:
        raise FluxError(FLX_5022, SUB_MILLISECOND)
    try:
        moment = datetime.fromisoformat(text.replace("z", "Z"))
    except ValueError:
        raise FluxError(FLX_5022, INVALID) from None
    delta = moment.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta // timedelta(milliseconds=1)


def _differential_corpus() -> List[str]:
    rng = random.Random(20261117)
    parts = (
        ["0000", "0001", "0999", "1969", "1970", "2000", "2024", "9999"],
        ["00", "01", "02", "12", "13"],
        ["00", "01", "28", "29", "30", "31", "32"],
        ["T", "t"],
        ["00", "05", "23", "24"],
        ["00", "30", "59", "60"],
        ["00", "30", "59", "60", "61"],
        ["", ".0", ".1", ".12", ".123", ".1230", ".1234", ".123000", ".1230000", ".9999"],
        ["Z", "z", "+00:00", "-00:00", "+23:59", "-23:59", "+24:00", "+00:60", "+12:30"],
    )
    corpus = set()
    for _ in range(20_000):
        year, month, day, sep, hour, minute, second, frac, offset = (
            rng.choice(choices) for choices in parts
        )
        corpus.add(f"{year}-{month}-{day}{sep}{hour}:{minute}:{second}{frac}{offset}")
    base = "2024-03-05T06:07:08.1230+05:30"
    for zero in ("\u0660", "\u0966", "\uff10", "\U0001d7ce"):
        for position, char in enumerate(base):
            if char.isdigit():
                foreign = chr(ord(zero) + int(char))
                corpus.add(base[:position] + foreign + base[position + 1 :])
    for tail in ("\n", "\r", " ", "\t", "\x00"):
        corpus.update({base + tail, tail + base})
    return sorted(corpus)


@pytest.mark.skipif(
    not (3, 11) <= sys.version_info[:2] <= (3, 13),
    reason="datetime.fromisoformat is the oracle only where the engine is tested",
)
def test_the_parser_reads_every_input_as_the_fromisoformat_reading_did() -> None:
    def outcome(parse: Any, text: str) -> Tuple[str, Any]:
        try:
            return ("value", parse(text))
        except FluxError as error:
            return (error.code.code, error.detail)
        except OverflowError:
            return ("FLX-5022", OUTSIDE)

    corpus = _differential_corpus()
    assert len(corpus) > 15_000
    differ = [
        text
        for text in corpus
        if outcome(parse_rfc3339_ms, text) != outcome(_fromisoformat_reading, text)
    ]
    assert differ == []


@pytest.mark.parametrize(
    "text", ["2026-01-01T00:00:00+99:99", "2023-02-29T00:00:00Z", "0001-01-01T00:00:00+00:01"]
)
def test_a_timestamp_refusal_carries_no_error_that_spells_the_value(text: str) -> None:
    with pytest.raises(FluxError) as caught:
        parse_rfc3339_ms(text)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


# ---------------------------------------------------------------------------
# exact and read_exact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, Fraction(7)),
        (-3, Fraction(-3)),
        (Decimal("1.25"), Fraction(5, 4)),
        ("1.25", Fraction(5, 4)),
        ("-0", Fraction(0)),
        ("1e3", Fraction(1000)),
        (" 2.5 ", Fraction(5, 2)),
    ],
)
def test_a_number_reads_as_the_exact_rational_it_spells(value: Any, expected: Fraction) -> None:
    read = exact(value, "cell")
    assert read == expected
    assert type(read) is Fraction


@pytest.mark.parametrize(
    ("value", "value_class"),
    [
        (True, "'bool'"),
        (False, "'bool'"),
        (1.0, "'float'"),
        (Decimal("NaN"), "'non-finite Decimal'"),
        (Decimal("-Infinity"), "'non-finite Decimal'"),
        ("abc", "'non-numeric string'"),
        ("", "'non-numeric string'"),
        ("NaN", "'non-finite Decimal'"),
        ("Infinity", "'non-finite Decimal'"),
        (None, "'NoneType'"),
        ([1], "'list'"),
        (Fraction(1, 3), "'Fraction'"),
    ],
)
def test_a_value_that_is_not_an_exact_number_is_refused(value: Any, value_class: str) -> None:
    with pytest.raises(FluxError) as caught:
        exact(value, "column 'x' row 4")
    assert _refusal(caught) == ("FLX-5005", f"column 'x' row 4: value class {value_class}")


# ---------------------------------------------------------------------------
# The size of a number
# ---------------------------------------------------------------------------

OVER_LONG = "value class 'over-long number'"


@pytest.mark.parametrize(
    ("text", "digits"),
    [
        ("0", 1),
        ("0E+99999999", 1),
        ("-0.000", 3),
        ("0.0", 1),
        ("0.00", 2),
        ("0E-1000", 1000),
        ("0E-1001", 1001),
        ("0E-99999999", 99_999_999),
        ("7", 1),
        ("123.45", 5),
        ("1.500", 4),
        ("0.001", 3),
        ("-0.25", 2),
        ("1.2E+3", 4),
        ("1E+999", 1000),
        ("1E+1000", 1001),
        ("1E-1000", 1000),
        ("1E+99999999", 100_000_000),
        ("1E-99999999", 99_999_999),
    ],
)
def test_digits_written_out_counts_without_forming_the_value(text: str, digits: int) -> None:
    """A leading ``0.`` does not count, and a zero counts the digits its scale spells."""
    assert digits_written_out(Decimal(text)) == digits


@pytest.mark.parametrize(
    "value",
    [
        "1" * 1001,
        "-" + "1" * 1001,
        "0." + "1" * 1001,
        "1" * 500 + "." + "1" * 501,
        "1E+1000",
        "1E-1001",
        "1E+99999999",
        "1e99999999999",
        "1E-99999999",
        "0E-1001",
        "0E-99999999",
        "1e999999999999999999",
        Decimal("0E-1001"),
        Decimal("1E+1000"),
        Decimal("-1E-1001"),
        Decimal("1E+99999999"),
        10**1000,
        -(10**1000),
        int("9" * 1001),
    ],
)
def test_a_number_needing_more_than_the_most_digits_is_refused_before_it_is_formed(
    value: Any,
) -> None:
    started = time.perf_counter()
    with pytest.raises(FluxError) as caught:
        exact(value, "column 'x' row 4")
    # A refusal costs microseconds; forming the value took minutes.
    assert time.perf_counter() - started < 5.0
    assert _refusal(caught) == ("FLX-5005", f"column 'x' row 4: {OVER_LONG}")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1" * 1000, Fraction(int("1" * 1000))),
        ("-" + "1" * 1000, Fraction(-int("1" * 1000))),
        ("1E+999", Fraction(10**999)),
        ("0." + "1" * 1000, Fraction(int("1" * 1000), 10**1000)),
        ("1E-1000", Fraction(1, 10**1000)),
        ("0E+99999999", Fraction(0)),
        ("0E-1000", Fraction(0)),
        (Decimal("-1E+999"), Fraction(-(10**999))),
        (10**1000 - 1, Fraction(10**1000 - 1)),
        (-(10**1000) + 1, Fraction(-(10**1000) + 1)),
    ],
)
def test_a_number_of_at_most_the_most_digits_reads_exactly(value: Any, expected: Fraction) -> None:
    assert exact(value, "c") == expected


def test_an_over_long_value_is_refused_before_its_range_is_asked() -> None:
    class NoRange:
        name = "x"
        null_policy = NullPolicy.EXCLUDE
        domain = ValueDomain.NUMBER
        support_values: Tuple[str, ...] = ()

        @property
        def interval(self) -> Interval:
            raise AssertionError("the range is read after the value")

    with pytest.raises(FluxError) as caught:
        encode_value("1E+99999999", NoRange(), GRID, row_index=2)
    assert _refusal(caught) == ("FLX-5005", f"column 'x' row 2: {OVER_LONG}")


def test_read_exact_reads_a_number_column_through_exact() -> None:
    assert read_exact("2.5", Col("amount"), row_index=0) == Fraction(5, 2)
    with pytest.raises(FluxError) as caught:
        read_exact(2.5, Col("amount"), row_index=6)
    assert _refusal(caught) == ("FLX-5005", "column 'amount' row 6: value class 'float'")


def test_read_exact_reads_a_timestamp_column_as_its_milliseconds() -> None:
    column = Col("at", domain=ValueDomain.TIMESTAMP)
    read = read_exact("1970-01-01T00:00:01.5Z", column, row_index=0)
    assert read == Fraction(1500)
    assert type(read) is Fraction


def test_a_timestamp_cell_that_is_not_a_string_is_a_value_class_refusal() -> None:
    column = Col("at", domain=ValueDomain.TIMESTAMP)
    with pytest.raises(FluxError) as caught:
        read_exact(1500, column, row_index=3)
    assert _refusal(caught) == ("FLX-5005", "column 'at' row 3: value class 'int'")


def test_a_timestamp_cell_the_parser_refuses_keeps_the_parser_code() -> None:
    column = Col("at", domain=ValueDomain.TIMESTAMP)
    with pytest.raises(FluxError) as caught:
        read_exact("1970-01-01", column, row_index=3)
    assert _refusal(caught) == ("FLX-5022", NOT_RFC3339)


def test_a_date_is_read_as_the_days_since_the_epoch() -> None:
    from flux_stats.reading import parse_date_days

    assert parse_date_days("1970-01-01") == 0
    assert parse_date_days("2026-01-01") == 20454
    assert parse_date_days("1969-12-31") == -1
    assert read_exact("2026-01-01", Col("d", domain=ValueDomain.DATE), row_index=0) == Fraction(
        20454
    )


@pytest.mark.parametrize(
    "text",
    [
        "2026-02-30",
        "0000-01-01",
        "2026-1-1",
        "2026-01-01\n",
        "2026-01-01T00:00:00Z",
        "",
        "٢٠٢٦-٠١-٠١",
    ],
)
def test_a_date_that_is_no_calendar_date_is_refused(text: str) -> None:
    from flux_stats.reading import parse_date_days

    with pytest.raises(FluxError) as caught:
        parse_date_days(text)
    assert caught.value.code.code == "FLX-5022"


def test_a_date_cell_keeps_a_number_of_days_and_never_quotes_a_bad_value() -> None:
    column = Col("d", domain=ValueDomain.DATE)
    assert read_exact(20454, column, row_index=0) == Fraction(20454)
    with pytest.raises(FluxError) as caught:
        read_exact("2026-02-30", column, row_index=4)
    assert _refusal(caught) == ("FLX-5005", "column 'd' row 4: value is not a calendar date")
    with pytest.raises(FluxError) as caught:
        read_exact("soon", column, row_index=4)
    assert _refusal(caught) == ("FLX-5005", "column 'd' row 4: value class 'non-numeric string'")


# ---------------------------------------------------------------------------
# encode_value
# ---------------------------------------------------------------------------


def test_a_value_inside_the_range_is_put_on_the_grid_rounding_down() -> None:
    column = Col("x")
    assert encode_value(1, column, GRID, row_index=0) == 65_536
    assert encode_value("0.5", column, GRID, row_index=0) == 32_768
    assert encode_value("0.33333", column, GRID, row_index=0) == 21_845
    # The declared range is closed at both ends.
    assert encode_value(0, column, GRID, row_index=0) == 0
    assert encode_value(10, column, GRID, row_index=0) == 655_360


def test_a_timestamp_is_encoded_as_its_milliseconds() -> None:
    column = Col(
        "at",
        domain=ValueDomain.TIMESTAMP,
        interval=Interval(Fraction(0), Fraction(10**13)),
    )
    assert encode_value("1970-01-01T00:00:01Z", column, Grid(fractional_bits=0), row_index=0) == (
        1000
    )


@pytest.mark.parametrize("value", [11, "-0.000001", Decimal("10.0001")])
def test_a_value_outside_the_declared_range_is_refused(value: Any) -> None:
    with pytest.raises(FluxError) as caught:
        encode_value(value, Col("x"), GRID, row_index=2)
    assert _refusal(caught) == ("FLX-5005", "column 'x' row 2: value outside the declared range")


def test_a_value_that_does_not_read_is_refused_before_its_range_is_asked() -> None:
    class NoRange:
        name = "x"
        null_policy = NullPolicy.EXCLUDE
        domain = ValueDomain.NUMBER
        support_values: Tuple[str, ...] = ()

        @property
        def interval(self) -> Interval:
            raise AssertionError("the range is read after the value")

    with pytest.raises(FluxError) as caught:
        encode_value("abc", NoRange(), GRID, row_index=2)
    assert _refusal(caught) == ("FLX-5005", "column 'x' row 2: value class 'non-numeric string'")


# ---------------------------------------------------------------------------
# category, cell, level_cells
# ---------------------------------------------------------------------------


def test_a_category_reads_as_its_nfc_string() -> None:
    assert category("é", Col("plan"), row_index=0) == "é"
    assert category("", Col("plan"), row_index=0) == ""


def test_a_whole_number_reads_as_the_category_of_its_decimal_text() -> None:
    # An INT column drawn over a few integer levels is a categorical one (ADR 0023 section 5).
    assert category(7, Col("tier"), row_index=0) == "7"
    assert category(-3, Col("tier"), row_index=0) == "-3"
    assert category(0, Col("tier"), row_index=0) == "0"
    assert sorted(group_keys([{"g": "a"}, {"g": 3}], Col("g"))) == [("3", [1]), ("a", [0])]


@pytest.mark.parametrize(("value", "name"), [(Decimal("1"), "Decimal"), (1.5, "float")])
def test_a_category_that_is_not_a_string_is_refused(value: Any, name: str) -> None:
    with pytest.raises(FluxError) as caught:
        category(value, Col("plan"), row_index=5)
    assert _refusal(caught) == ("FLX-5005", f"column 'plan' row 5: value class {name!r}")


def test_a_boolean_reads_as_the_category_false_or_true() -> None:
    assert category(True, Col("active"), row_index=0) == "true"
    assert category(False, Col("active"), row_index=0) == "false"
    # Only a category does: a boolean is still no number.
    with pytest.raises(FluxError):
        exact(True, "cell")


def test_a_cell_is_returned_raw_and_a_null_cell_is_none() -> None:
    assert cell({"x": "1.5"}, Col("x"), row_index=0) == "1.5"
    assert cell({"x": None}, Col("x"), row_index=0) is None


def test_an_absent_column_is_refused_with_its_own_code() -> None:
    with pytest.raises(FluxError) as caught:
        cell({"y": 1}, Col("x"), row_index=4)
    assert _refusal(caught) == ("FLX-5031", "column 'x' is absent from row 4")


def test_the_level_cells_are_the_declared_levels_in_code_point_order() -> None:
    column = Col("plan", support_values=("pro", "Élite", "basic", "Zeta"))
    assert level_cells(column) == ("Zeta", "basic", "pro", "Élite")


# ---------------------------------------------------------------------------
# group_keys
# ---------------------------------------------------------------------------


def test_an_ungrouped_property_is_one_group_of_every_row() -> None:
    rows = [{"g": "a"}, {"g": None}, {}]
    assert group_keys(rows, None) == [(UNGROUPED, [0, 1, 2])]
    assert group_keys([], None) == [(UNGROUPED, [])]


def test_groups_come_back_in_key_order_whatever_the_row_order() -> None:
    rows = [{"g": "b"}, {"g": "a"}, {"g": "b"}, {"g": "c"}]
    assert group_keys(rows, Col("g")) == [("a", [1]), ("b", [0, 2]), ("c", [3])]
    swapped = [rows[3], rows[2], rows[1], rows[0]]
    assert [key for key, _ in group_keys(swapped, Col("g"))] == ["a", "b", "c"]


def test_a_group_key_is_the_nfc_string_so_both_spellings_are_one_group() -> None:
    rows = [{"g": "é"}, {"g": "é"}]
    assert group_keys(rows, Col("g")) == [("é", [0, 1])]


def test_the_empty_string_is_a_group_like_any_other() -> None:
    rows = [{"g": ""}, {"g": "a"}]
    assert group_keys(rows, Col("g")) == [("", [0]), ("a", [1])]


def test_a_null_group_value_under_exclude_leaves_its_row_out() -> None:
    rows = [{"g": "a"}, {"g": None}, {"g": "a"}]
    assert group_keys(rows, Col("g")) == [("a", [0, 2])]
    assert group_keys([{"g": None}], Col("g")) == []


def test_a_null_group_value_under_fail_is_refused() -> None:
    rows = [{"g": "a"}, {"g": None}]
    with pytest.raises(FluxError) as caught:
        group_keys(rows, Col("g", null_policy=NullPolicy.FAIL))
    assert _refusal(caught) == ("FLX-5005", "column 'g' row 1: null is refused")


def test_a_group_value_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        group_keys([{"g": "a"}, {"g": 3.5}], Col("g"))
    assert _refusal(caught) == ("FLX-5005", "column 'g' row 1: value class 'float'")


def test_a_row_without_the_group_column_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        group_keys([{"g": "a"}, {"h": "a"}], Col("g"))
    assert _refusal(caught) == ("FLX-5031", "column 'g' is absent from row 1")


def test_a_slice_of_a_larger_record_set_names_its_rows_as_the_whole_set_does() -> None:
    rows = [{"g": "a", "x": "1"}, {"g": "b", "x": "oops"}]
    # The indices stay positions in the slice; only a refusal adds first_row.
    assert group_keys(rows, Col("g"), first_row=40) == [("a", [0]), ("b", [1])]
    with pytest.raises(FluxError) as caught:
        group_keys([{"g": "a"}, {"g": 3.5}], Col("g"), first_row=40)
    assert _refusal(caught) == ("FLX-5005", "column 'g' row 41: value class 'float'")
    read = functools.partial(encode_value, column=Col("x"), grid=GRID)
    with pytest.raises(FluxError) as caught:
        read_column(rows, [0, 1], Col("x"), read, first_row=40)
    assert "column 'x' row 41" in _refusal(caught)[1]
    with pytest.raises(FluxError) as caught:
        read_column([{"x": None}], [0], Col("x", null_policy=NullPolicy.FAIL), read, first_row=7)
    assert _refusal(caught) == ("FLX-5005", "column 'x' row 7: null is refused")


def test_declared_only_refuses_a_group_the_declaration_does_not_name() -> None:
    column = Col("g", support_values=("a", "b"))
    rows = [{"g": "b"}, {"g": "a"}, {"g": "z"}]
    assert group_keys(rows[:2], column, declared_only=True) == [("a", [1]), ("b", [0])]
    assert group_keys(rows, column) == [("a", [1]), ("b", [0]), ("z", [2])]
    with pytest.raises(FluxError) as caught:
        group_keys(rows, column, declared_only=True)
    assert _refusal(caught) == ("FLX-5005", "column 'g' row 2: value outside the declared support")


def test_declared_only_checks_the_nfc_key() -> None:
    column = Col("g", support_values=("é",))
    assert group_keys([{"g": "é"}], column, declared_only=True) == [("é", [0])]


# ---------------------------------------------------------------------------
# read_column
# ---------------------------------------------------------------------------


def _recorder() -> Tuple[List[Tuple[Any, int]], Any]:
    seen: List[Tuple[Any, int]] = []

    def read(value: Any, *, row_index: int) -> str:
        seen.append((value, row_index))
        return f"{value}@{row_index}"

    return seen, read


def test_read_column_hands_each_non_null_cell_and_its_row_in_order() -> None:
    rows = [{"x": "a"}, {"x": None}, {"x": "c"}, {"x": "d"}]
    seen, read = _recorder()
    assert read_column(rows, [3, 0, 1, 2], Col("x"), read) == ["d@3", "a@0", "c@2"]
    assert seen == [("d", 3), ("a", 0), ("c", 2)]


def test_read_column_reads_only_the_rows_it_is_given() -> None:
    rows = [{"x": "a"}, {}, {"x": None}]
    seen, read = _recorder()
    assert read_column(rows, [0], Col("x", null_policy=NullPolicy.FAIL), read) == ["a@0"]
    assert read_column(rows, [], Col("x"), read) == []


def test_read_column_refuses_a_null_under_fail_at_the_first_null_row() -> None:
    rows = [{"x": "a"}, {"x": None}, {}]
    seen, read = _recorder()
    with pytest.raises(FluxError) as caught:
        read_column(rows, [0, 1, 2], Col("x", null_policy=NullPolicy.FAIL), read)
    assert _refusal(caught) == ("FLX-5005", "column 'x' row 1: null is refused")
    assert seen == [("a", 0)]


def test_read_column_refuses_an_absent_column() -> None:
    seen, read = _recorder()
    with pytest.raises(FluxError) as caught:
        read_column([{"x": "a"}, {}], [0, 1], Col("x"), read)
    assert _refusal(caught) == ("FLX-5031", "column 'x' is absent from row 1")


# ---------------------------------------------------------------------------
# read_pairs
# ---------------------------------------------------------------------------


def _pair_columns(
    first: NullPolicy = NullPolicy.EXCLUDE, second: NullPolicy = NullPolicy.EXCLUDE
) -> Tuple[Col, Col]:
    return Col("x", null_policy=first), Col("y", null_policy=second)


def _pairs(rows: Sequence[Dict[str, Any]], *columns: Col) -> List[Tuple[int, int]]:
    first, second = columns if columns else _pair_columns()
    return read_pairs(rows, list(range(len(rows))), first, second, GRID)


def test_read_pairs_encodes_both_halves_of_each_row() -> None:
    rows = [{"x": 1, "y": "2"}, {"x": "0.5", "y": 0}]
    assert _pairs(rows) == [(65_536, 131_072), (32_768, 0)]


def test_a_pair_missing_a_half_under_exclude_contributes_nothing() -> None:
    rows = [{"x": None, "y": 1}, {"x": 1, "y": None}, {"x": None, "y": None}, {"x": 2, "y": 3}]
    assert _pairs(rows) == [(131_072, 196_608)]


@pytest.mark.parametrize(
    ("row", "first", "second", "named"),
    [
        ({"x": None, "y": 1}, NullPolicy.FAIL, NullPolicy.EXCLUDE, "x"),
        ({"x": 1, "y": None}, NullPolicy.EXCLUDE, NullPolicy.FAIL, "y"),
        # Null in both halves is null in each: the second column's fail is not
        # hidden by the first column's exclude, and the first column is asked first.
        ({"x": None, "y": None}, NullPolicy.EXCLUDE, NullPolicy.FAIL, "y"),
        ({"x": None, "y": None}, NullPolicy.FAIL, NullPolicy.FAIL, "x"),
    ],
)
def test_a_null_in_a_fail_column_of_a_pair_is_refused(
    row: Dict[str, Any], first: NullPolicy, second: NullPolicy, named: str
) -> None:
    with pytest.raises(FluxError) as caught:
        _pairs([{"x": 1, "y": 1}, row], *_pair_columns(first, second))
    assert _refusal(caught) == ("FLX-5005", f"column {named!r} row 1: null is refused")


def test_a_pair_whose_fail_column_holds_a_value_is_dropped_when_the_other_is_null() -> None:
    rows = [{"x": None, "y": 1}, {"x": 1, "y": 1}]
    assert _pairs(rows, *_pair_columns(NullPolicy.EXCLUDE, NullPolicy.FAIL)) == [(65_536, 65_536)]


def test_read_pairs_refuses_an_absent_half_naming_it() -> None:
    with pytest.raises(FluxError) as caught:
        _pairs([{"x": 1, "y": 1}, {"x": 1}])
    assert _refusal(caught) == ("FLX-5031", "column 'y' is absent from row 1")
    with pytest.raises(FluxError) as caught:
        _pairs([{"y": 1}])
    assert _refusal(caught) == ("FLX-5031", "column 'x' is absent from row 0")


def test_read_pairs_refuses_a_value_outside_either_range() -> None:
    with pytest.raises(FluxError) as caught:
        _pairs([{"x": 11, "y": 1}])
    assert _refusal(caught) == ("FLX-5005", "column 'x' row 0: value outside the declared range")
    with pytest.raises(FluxError) as caught:
        _pairs([{"x": 1, "y": 11}])
    assert _refusal(caught) == ("FLX-5005", "column 'y' row 0: value outside the declared range")


def test_read_pairs_reads_only_the_rows_it_is_given() -> None:
    rows = [{"x": 1, "y": 1}, {}, {"x": 2, "y": 2}]
    first, second = _pair_columns()
    assert read_pairs(rows, [2, 0], first, second, GRID) == [(131_072, 131_072), (65_536, 65_536)]


def test_reading_one_value_asks_a_column_only_for_its_name_and_domain() -> None:
    """A profiler declares no range, null policy or levels, and needs none to read a value."""

    @dataclass(frozen=True)
    class Named:
        name: str

    @dataclass(frozen=True)
    class Valued:
        name: str
        domain: ValueDomain

    assert read_exact("2.5", Valued("x", ValueDomain.NUMBER), row_index=0) == Fraction(5, 2)
    assert read_exact(
        "1970-01-01T00:00:01Z", Valued("t", ValueDomain.TIMESTAMP), row_index=0
    ) == Fraction(1000)
    assert category("e\u0301", Named("c"), row_index=0) == "\u00e9"
    assert cell({"c": None}, Named("c"), row_index=0) is None
    with pytest.raises(FluxError) as caught:
        read_exact(2.5, Valued("x", ValueDomain.NUMBER), row_index=1)
    assert _refusal(caught) == ("FLX-5005", "column 'x' row 1: value class 'float'")


def test_a_reader_column_can_be_any_object_with_the_five_attributes() -> None:
    class Plain:
        name = "x"
        null_policy = NullPolicy.EXCLUDE
        domain = ValueDomain.NUMBER
        interval = Interval(Fraction(0), Fraction(1))
        support_values: Tuple[str, ...] = ()

    assert encode_value(1, Plain(), GRID, row_index=0) == 65_536
    assert group_keys([{"x": "a"}], Plain()) == [("a", [0])]
