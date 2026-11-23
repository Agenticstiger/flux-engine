# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Dialect spellings the check translates: T-SQL day counts and ``GETDATE``, and Snowflake ``LEN``.

Each accepted query's canonical text is run in DuckDB and its values are compared with what the
dialect's documentation gives, written out here and not read from DuckDB.
"""

from __future__ import annotations

import logging
from typing import List, Sequence, Tuple

import pytest

pytest.importorskip("sqlglot")
duckdb = pytest.importorskip("duckdb")

from .test_check import check, refused  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _quiet_sqlglot() -> None:
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


def _run(text: str, rows: Sequence[Tuple[int, str, str]]) -> List[object]:
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
    return [
        item[0] for item in connection.execute(f"SELECT v FROM ({text}) ORDER BY id").fetchall()
    ]


ONE_ROW = [(1, "x", "2025-01-05")]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # The vendor's example: SELECT DATEPART(year, 0), DATEPART(month, 0), DATEPART(day, 0)
        # returns 1900, 1, 1, because the integer 0 is 1900-01-01.
        ("YEAR(0)", 1900),
        ("MONTH(0)", 1),
        ("DAY(0)", 1),
        ("DATEPART(year, 0)", 1900),
        ("DATEPART(month, 0)", 1),
        ("DATEPART(day, 0)", 1),
        # 1900-01-01 is a Monday, which is 2 with DATEFIRST 7, and in week 1.
        ("DATEPART(weekday, 0)", 2),
        ("DATEPART(week, 5)", 1),
        ("DATEPART(week, 6)", 2),
        # An integer is that many days after 1900-01-01 (1900 is not a leap year).
        ("YEAR(365)", 1901),
        ("MONTH(31)", 2),
        ("DAY(59)", 1),
        ("YEAR(-1)", 1899),
        ("DAY(-1)", 31),
        ("YEAR(2958463)", 9999),
    ],
)
def test_tsql_reads_an_integer_as_days_after_1900_01_01(expression: str, expected: int) -> None:
    text = check(f"SELECT id, {expression} AS v FROM subscriptions", dialect="tsql").text
    assert _run(text, ONE_ROW) == [expected]


def test_tsql_year_of_a_column_is_unchanged() -> None:
    text = check("SELECT id, YEAR(renewed_at) AS v FROM subscriptions", dialect="tsql").text
    assert _run(text, ONE_ROW) == [2025]


@pytest.mark.parametrize(
    ("dialect", "expression"),
    [
        ("tsql", "YEAR(2958464)"),
        ("tsql", "YEAR(-53691)"),
        ("tsql", "YEAR(1.5)"),
        ("tsql", "MONTH(-1.5)"),
        ("tsql", "DATEPART(year, 2958464)"),
        ("mysql", "YEAR(0)"),
        ("spark", "DAY(5)"),
    ],
)
def test_a_number_given_to_a_date_function_is_refused_when_it_is_compiled(
    dialect: str, expression: str
) -> None:
    refused(f"SELECT {expression} AS v FROM subscriptions", "FLX-3622", "number", dialect=dialect)


def test_the_clock_in_datediff_is_the_registry_as_of() -> None:
    query = "SELECT id, DATEDIFF(day, renewed_at, GETDATE()) AS v FROM subscriptions"
    text = check(query, dialect="tsql", **{"asOf": "2025-03-10"}).text
    rows = [
        (1, "x", "2025-03-10"),  # the same day: 0
        (2, "x", "2025-03-02"),  # 8 days before
        (3, "x", "2025-01-05"),  # 26 + 28 + 10 = 64 days before
        (4, "x", "2024-02-17"),  # 366 to 2025-02-17 (2024 is a leap year) and 21 more
        (5, "x", "2025-03-11"),  # the day after: -1
    ]
    assert _run(text, rows) == [0, 8, 64, 387, -1]


def test_the_clock_in_datediff_never_reads_the_wall_clock() -> None:
    text = check(
        "SELECT id, DATEDIFF(day, renewed_at, GETDATE()) AS v FROM subscriptions",
        dialect="tsql",
        asOf="2025-03-10",
    ).text
    assert "CURRENT_TIMESTAMP" not in text.upper() and "NOW" not in text.upper()
    assert "2025-03-10 00:00:00" in text


def test_the_clock_in_datediff_is_refused_without_as_of() -> None:
    refused(
        "SELECT DATEDIFF(day, renewed_at, GETDATE()) AS v FROM subscriptions",
        "FLX-3629",
        dialect="tsql",
    )


def test_datediff_counts_the_boundaries_t_sql_counts() -> None:
    text = check(
        "SELECT id, DATEDIFF(month, renewed_at, GETDATE()) AS v FROM subscriptions",
        dialect="tsql",
        asOf="2025-03-01",
    ).text
    # DATEDIFF counts boundaries crossed: 2025-02-28 to 2025-03-01 is one month boundary.
    assert _run(text, [(1, "x", "2025-02-28"), (2, "x", "2025-03-31")]) == [1, 0]


def test_snowflake_len_is_the_character_length() -> None:
    for name in ("LEN", "LENGTH"):
        text = check(f"SELECT id, {name}(plan) AS v FROM subscriptions", dialect="snowflake").text
        assert "TYPEOF" not in text.upper()
        rows = [(1, "", "2025-01-05"), (2, "abc", "2025-01-05"), (3, "ñé€", "2025-01-05")]
        # Characters, not bytes: the last text is three characters and six bytes.
        assert _run(text, rows) == [0, 3, 3]


def test_snowflake_len_inside_a_filter() -> None:
    text = check(
        "SELECT id, id AS v FROM subscriptions WHERE LEN(plan) = 3", dialect="snowflake"
    ).text
    assert _run(text, [(1, "ab", "2025-01-05"), (2, "abc", "2025-01-05")]) == [2]
