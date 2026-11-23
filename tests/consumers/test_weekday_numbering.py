# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A weekday or week part gives, in the DuckDB text the engine runs, the value its dialect gives it.

T-SQL ``DATEPART(weekday, x)`` and BigQuery ``EXTRACT(DAYOFWEEK FROM x)`` number Sunday 1, DuckDB's
``dayofweek`` numbers it 0, and each dialect numbers its weeks by its own rule. The check maps each
part to the DuckDB expression with the dialect's value under the dialect's documented default
settings (T-SQL ``DATEFIRST`` 7, BigQuery, Snowflake ``WEEK_START`` 0 and ``WEEK_OF_YEAR_POLICY`` 0,
MySQL ``default_week_format`` 0), and refuses a part it cannot map exactly. The expected values
below are written out from each vendor's documented definition, never taken from DuckDB's output.
"""

from __future__ import annotations

import logging
from typing import List, Sequence, Tuple

import pytest

pytest.importorskip("sqlglot")
duckdb = pytest.importorskip("duckdb")

from flux_engine.core.errors import FluxError  # noqa: E402

from .test_check import check, refused  # noqa: E402

pytestmark = pytest.mark.unit

PREFIX = "SELECT * FROM subscriptions WHERE "


@pytest.fixture(autouse=True)
def _quiet_sqlglot() -> None:
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


#: Saturday 2024-12-28 to Sunday 2025-01-05: a full week and a bit, across a year boundary. 2024 began
#: on a Monday and 2025 begins on a Wednesday.
DATES = [
    "2024-12-28",  # Saturday
    "2024-12-29",  # Sunday
    "2024-12-30",  # Monday
    "2024-12-31",  # Tuesday
    "2025-01-01",  # Wednesday
    "2025-01-02",  # Thursday
    "2025-01-03",  # Friday
    "2025-01-04",  # Saturday
    "2025-01-05",  # Sunday
]

#: Documented, not computed: Sunday 1 to Saturday 7 (T-SQL with DATEFIRST 7, BigQuery DAYOFWEEK, MySQL).
SUNDAY_FIRST_FROM_ONE = [7, 1, 2, 3, 4, 5, 6, 7, 1]
#: Snowflake with WEEK_START 0: Sunday 0 to Saturday 6.
SUNDAY_FIRST_FROM_ZERO = [6, 0, 1, 2, 3, 4, 5, 6, 0]
#: Snowflake DAYOFWEEKISO: Monday 1 to Sunday 7.
MONDAY_FIRST_FROM_ONE = [6, 7, 1, 2, 3, 4, 5, 6, 7]
#: ISO 8601 weeks: 2024-12-30 to 2025-01-05 is week 1 of 2025, because it holds Thursday 2025-01-02.
ISO_WEEK = [52, 52, 1, 1, 1, 1, 1, 1, 1]
#: T-SQL week with DATEFIRST 7: week 1 holds 1 January, weeks start on Sunday, so 2024 has 53.
TSQL_WEEK = [52, 53, 53, 53, 1, 1, 1, 1, 2]
#: BigQuery WEEK (Sunday): days before the year's first Sunday are week 0. 2025's first Sunday is 5 January.
BIGQUERY_WEEK_SUNDAY = [51, 52, 52, 52, 0, 0, 0, 0, 1]
#: BigQuery WEEK(MONDAY): 2024's 53rd Monday is 30 December, and 2025's first is 6 January.
BIGQUERY_WEEK_MONDAY = [52, 52, 53, 53, 0, 0, 0, 0, 0]
#: Truncation to the Sunday on or before the date (BigQuery DATE_TRUNC(x, WEEK)).
TRUNC_SUNDAY = [
    "2024-12-22",
    "2024-12-29",
    "2024-12-29",
    "2024-12-29",
    "2024-12-29",
    "2024-12-29",
    "2024-12-29",
    "2024-12-29",
    "2025-01-05",
]
#: Truncation to the Monday on or before the date (ISOWEEK, WEEK(MONDAY), Snowflake's default).
TRUNC_MONDAY = [
    "2024-12-23",
    "2024-12-23",
    "2024-12-30",
    "2024-12-30",
    "2024-12-30",
    "2024-12-30",
    "2024-12-30",
    "2024-12-30",
    "2024-12-30",
]


def _values(
    dialect: str, expression: str, rows: Sequence[Tuple[int, str, str]], **kwargs: object
) -> List[object]:
    """What the canonical text of ``SELECT id, <expression> AS v`` gives for each row, in DuckDB."""
    text = check(f"SELECT id, {expression} AS v FROM subscriptions", dialect=dialect, **kwargs).text
    connection = duckdb.connect(":memory:")
    connection.execute(
        'CREATE TABLE "subscriptions" (id BIGINT, plan VARCHAR, region VARCHAR, renewed BOOLEAN, '
        "renewed_at DATE, started TIMESTAMP, amount DECIMAL(18,4))"
    )
    for row_id, plan, day in rows:
        connection.execute(
            "INSERT INTO \"subscriptions\" VALUES (?, ?, 'EU', TRUE, ?, ?, 1.5)",
            (row_id, plan, day, f"{day} 10:30:00"),
        )
    found = connection.execute(f"SELECT v FROM ({text}) ORDER BY id").fetchall()
    return [item[0] if not hasattr(item[0], "isoformat") else item[0].isoformat() for item in found]


def _week_of(dialect: str, expression: str, expected: Sequence[object]) -> None:
    rows = [(index + 1, "x", day) for index, day in enumerate(DATES)]
    assert _values(dialect, expression, rows) == list(expected), (dialect, expression)


CASES: List[Tuple[str, str, Sequence[object]]] = [
    ("tsql", "DATEPART(weekday, renewed_at)", SUNDAY_FIRST_FROM_ONE),
    ("tsql", "DATEPART(dw, renewed_at)", SUNDAY_FIRST_FROM_ONE),
    ("tsql", "DATEPART(week, renewed_at)", TSQL_WEEK),
    ("tsql", "DATEPART(wk, renewed_at)", TSQL_WEEK),
    ("tsql", "DATEPART(ww, renewed_at)", TSQL_WEEK),
    ("tsql", "DATEPART(iso_week, renewed_at)", ISO_WEEK),
    ("tsql", "DATEPART(weekday, started)", SUNDAY_FIRST_FROM_ONE),
    ("bigquery", "EXTRACT(DAYOFWEEK FROM renewed_at)", SUNDAY_FIRST_FROM_ONE),
    ("bigquery", "EXTRACT(WEEK FROM renewed_at)", BIGQUERY_WEEK_SUNDAY),
    ("bigquery", "EXTRACT(WEEK(SUNDAY) FROM renewed_at)", BIGQUERY_WEEK_SUNDAY),
    ("bigquery", "EXTRACT(WEEK(MONDAY) FROM renewed_at)", BIGQUERY_WEEK_MONDAY),
    ("bigquery", "EXTRACT(ISOWEEK FROM renewed_at)", ISO_WEEK),
    ("bigquery", "DATE_TRUNC(renewed_at, WEEK)", TRUNC_SUNDAY),
    ("bigquery", "DATE_TRUNC(renewed_at, WEEK(SUNDAY))", TRUNC_SUNDAY),
    ("bigquery", "DATE_TRUNC(renewed_at, WEEK(MONDAY))", TRUNC_MONDAY),
    ("bigquery", "DATE_TRUNC(renewed_at, ISOWEEK)", TRUNC_MONDAY),
    ("snowflake", "DAYOFWEEK(renewed_at)", SUNDAY_FIRST_FROM_ZERO),
    ("snowflake", "DATE_PART(dayofweek, renewed_at)", SUNDAY_FIRST_FROM_ZERO),
    ("snowflake", "DAYOFWEEKISO(renewed_at)", MONDAY_FIRST_FROM_ONE),
    ("snowflake", "WEEK(renewed_at)", ISO_WEEK),
    ("snowflake", "WEEKOFYEAR(renewed_at)", ISO_WEEK),
    ("snowflake", "WEEKISO(renewed_at)", ISO_WEEK),
    ("snowflake", "DATE_PART(week, renewed_at)", ISO_WEEK),
    ("mysql", "DAYOFWEEK(renewed_at)", SUNDAY_FIRST_FROM_ONE),
    ("mysql", "WEEK(renewed_at)", BIGQUERY_WEEK_SUNDAY),
    ("mysql", "WEEK(renewed_at, 0)", BIGQUERY_WEEK_SUNDAY),
    ("mysql", "WEEK(renewed_at, 3)", ISO_WEEK),
    ("mysql", "WEEKOFYEAR(renewed_at)", ISO_WEEK),
]


@pytest.mark.parametrize(("dialect", "expression", "expected"), CASES)
def test_a_weekday_or_week_part_has_the_value_its_dialect_documents(
    dialect: str, expression: str, expected: Sequence[object]
) -> None:
    _week_of(dialect, expression, expected)


def test_snowflake_date_trunc_to_the_week_goes_to_monday() -> None:
    _week_of("snowflake", "DATE_TRUNC('week', renewed_at)", TRUNC_MONDAY)


@pytest.mark.parametrize(
    ("dialect", "expression", "day", "expected"),
    [
        # SET DATEFIRST 7: DATEPART(week, '2007-04-21') is 16 and DATEPART(weekday, ...) is 7;
        # 1999-01-01 is a Friday, the sixth day of the week from Sunday.
        ("tsql", "DATEPART(week, renewed_at)", "2007-04-21", 16),
        ("tsql", "DATEPART(weekday, renewed_at)", "2007-04-21", 7),
        ("tsql", "DATEPART(weekday, renewed_at)", "1999-01-01", 6),
        ("tsql", "DATEPART(week, renewed_at)", "2031-01-01", 1),
        # MySQL's manual: WEEK('2008-02-20') is 7 and WEEKOFYEAR('2008-02-20') is 8.
        ("mysql", "WEEK(renewed_at)", "2008-02-20", 7),
        ("mysql", "WEEKOFYEAR(renewed_at)", "2008-02-20", 8),
        # Snowflake's manual: DATE_TRUNC('week') of the Sunday 2017-01-01 is 2016-12-26.
        ("snowflake", "DATE_TRUNC('week', renewed_at)", "2017-01-01", "2016-12-26"),
        # BigQuery: the Sunday, the Monday and the Saturday around 2017-11-05 (a Sunday).
        ("bigquery", "DATE_TRUNC(renewed_at, WEEK)", "2017-11-05", "2017-11-05"),
        ("bigquery", "DATE_TRUNC(renewed_at, WEEK)", "2017-11-06", "2017-11-05"),
        ("bigquery", "DATE_TRUNC(renewed_at, WEEK)", "2017-11-11", "2017-11-05"),
        ("bigquery", "DATE_TRUNC(renewed_at, WEEK)", "2017-11-12", "2017-11-12"),
    ],
)
def test_the_examples_in_each_vendors_documentation(
    dialect: str, expression: str, day: str, expected: object
) -> None:
    assert _values(dialect, expression, [(1, "x", day)]) == [expected]


def test_a_week_part_repeating_a_complex_operand_keeps_its_value() -> None:
    rows = [(index + 1, "x", day) for index, day in enumerate(DATES)]
    got = _values(
        "tsql", "DATEPART(weekday, COALESCE(NULLIF(renewed_at, DATE '1999-01-01'), started))", rows
    )
    assert got == SUNDAY_FIRST_FROM_ONE


@pytest.mark.parametrize(
    ("dialect", "query", "part"),
    [
        ("tsql", "DATETRUNC(week, renewed_at) = DATE '2025-01-05'", "WEEK"),
        ("bigquery", "TIMESTAMP_TRUNC(started, WEEK) = TIMESTAMP '2025-01-05 00:00:00'", "WEEK"),
        (
            "bigquery",
            "DATETIME_TRUNC(started, WEEK(MONDAY)) = TIMESTAMP '2025-01-06 00:00:00'",
            "WEEK(MONDAY)",
        ),
        ("mysql", "WEEK(renewed_at, 1) = 1", "WEEK with mode 1"),
        ("mysql", "WEEK(renewed_at, 7) = 1", "WEEK with mode 7"),
        ("spark", "DAYOFWEEK(renewed_at) = 1", "DayOfWeek"),
        ("postgres", "EXTRACT(dow FROM renewed_at) = 1", "DOW"),
        ("postgres", "date_trunc('week', renewed_at) = DATE '2025-01-06'", "WEEK"),
        ("hive", "WEEKOFYEAR(renewed_at) = 1 AND DAYOFWEEK(renewed_at) = 1", "DayOfWeek"),
    ],
)
def test_a_part_with_no_exact_mapping_is_refused_naming_the_dialect_and_the_part(
    dialect: str, query: str, part: str
) -> None:
    error = refused(PREFIX + query, "FLX-3622", part, dialect=dialect)
    assert f"the dialect {dialect}" in str(error)


def test_the_t_sql_part_w_is_refused_because_the_parser_reads_it_as_the_week() -> None:
    refused(PREFIX + "DATEPART(w, renewed_at) = 1", "FLX-3622", "part w", dialect="tsql")


@pytest.mark.parametrize(
    "call",
    [
        "DATEPART(/*x*/w, renewed_at)",
        "DATEPART([w], renewed_at)",
        'DATEPART("w", renewed_at)',
        "DATEPART(--c\nw, renewed_at)",
        "DATEPART(W, renewed_at)",
        "DATEPART ( w , renewed_at)",
        "DATENAME([W], renewed_at)",
        "DATEADD(w, 1, renewed_at)",
        "DATEADD([y], 1, renewed_at)",
        "DATEDIFF(w, started, renewed_at)",
        "DATEDIFF_BIG(y, started, renewed_at)",
        "DATETRUNC(Y, renewed_at)",
        "DATETRUNC(m, renewed_at)",
        "DATENAME(dy, renewed_at)",
    ],
)
def test_a_t_sql_part_the_parser_reads_differently_is_refused_however_it_is_written(
    call: str,
) -> None:
    refused(PREFIX + f"{call} IS NOT NULL", "FLX-3622", "tsql", dialect="tsql")


@pytest.mark.parametrize(
    "call",
    ["DATEPART(weekday, renewed_at)", "DATEADD(d, 1, renewed_at)", "DATENAME(month, renewed_at)"],
)
def test_the_parts_t_sql_and_the_parser_agree_on_are_not_refused_by_the_part_check(
    call: str,
) -> None:
    try:
        check(PREFIX + f"{call} IS NOT NULL", dialect="tsql")
    except FluxError as error:
        assert "reads the T-SQL part" not in str(error)


def test_a_huge_day_count_is_refused_not_raised_as_a_value_error() -> None:
    refused("SELECT YEAR(" + "9" * 4400 + ") AS y FROM subscriptions", "FLX-3622", dialect="tsql")


def test_an_operand_too_large_to_repeat_is_refused() -> None:
    operand = " + ".join(["LEN(plan)"] * 120)
    refused(
        PREFIX + f"DATEPART(weekday, DATEADD(day, {operand}, renewed_at)) = 1",
        "FLX-3627",
        dialect="tsql",
    )


def test_a_hostile_operand_of_a_week_part_is_still_refused() -> None:
    for query in (
        "DATEPART(weekday, foo(renewed_at)) = 1",
        "DATEPART(weekday, read_csv('x.csv')) = 1",
    ):
        with pytest.raises(FluxError) as caught:
            check(PREFIX + query, dialect="tsql")
        assert caught.value.code.code in {"FLX-3621", "FLX-3622", "FLX-3623"}, query


@pytest.mark.parametrize(
    "query",
    [
        "EXTRACT(DAYOFWEEK FROM renewed_at) = 0",
        "EXTRACT(WEEK FROM renewed_at) = 1",
        "date_trunc('week', renewed_at) = DATE '2025-01-06'",
        "dayofweek(renewed_at) = 0",
    ],
)
def test_the_duckdb_dialect_keeps_its_weekday_and_week_parts(query: str) -> None:
    assert check(PREFIX + query, dialect="duckdb").text


@pytest.mark.parametrize(
    ("dialect", "query"),
    [
        ("tsql", "DATEPART(month, renewed_at) = 3"),
        ("postgres", "EXTRACT(year FROM renewed_at) = 2025"),
        ("bigquery", "EXTRACT(DAYOFYEAR FROM renewed_at) = 5"),
        ("mysql", "WEEKOFYEAR(renewed_at) = 10"),
    ],
)
def test_the_other_date_parts_are_unchanged_outside_duckdb(dialect: str, query: str) -> None:
    assert check(PREFIX + query, dialect=dialect).text
