# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The two node classes sqlglot writes for a dialect's own spelling.

* ``TsOrDsToDate`` is what sqlglot writes for ``YEAR``, ``MONTH`` and ``DAY`` in MySQL, T-SQL, Spark,
  Hive and Databricks; ``Trunc`` is what its DuckDB writer writes for integer division in PostgreSQL,
  Redshift, T-SQL, SQLite, Trino and Drill. Both are admitted, with their arguments checked, and each
  accepted query's canonical text is run in DuckDB and compared with the same question written in
  DuckDB's own dialect. Hostile queries that put anything else into those nodes are still refused.
"""

from __future__ import annotations

import logging
from typing import List

import pytest

sqlglot = pytest.importorskip("sqlglot")
duckdb = pytest.importorskip("duckdb")

from sqlglot import exp  # noqa: E402

from flux_engine.consumers import _allow  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402

from .test_check import check, refused  # noqa: E402

pytestmark = pytest.mark.unit

PREFIX = "SELECT * FROM subscriptions WHERE "


@pytest.fixture(autouse=True)
def _quiet_sqlglot() -> None:
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


def _ids(text: str) -> List[int]:
    """The ids the canonical *text* selects over a small table, in DuckDB."""
    connection = duckdb.connect(":memory:")
    connection.execute(
        'CREATE TABLE "subscriptions" (id BIGINT, plan VARCHAR, region VARCHAR, renewed BOOLEAN, '
        "renewed_at DATE, started TIMESTAMP, amount DECIMAL(18,4))"
    )
    rows = [
        (1, "ab", "EU", True, "2025-01-05", "2025-01-05 10:00:00", "1.5"),
        (2, "abc", "EU", False, "2024-02-17", "2024-02-17 10:00:00", "2.5"),
        (3, "abcd", "US", True, "2025-02-28", "2025-02-28 23:59:59", "3.5"),
        (4, "abcde", "US", True, "2025-03-02", "2025-03-02 00:00:00", "4.5"),
        (5, "abcdef", "US", False, "2025-03-31", "2025-03-31 08:00:00", "5.5"),
    ]
    for row in rows:
        connection.execute('INSERT INTO "subscriptions" VALUES (?, ?, ?, ?, ?, ?, ?)', row)
    return sorted(item[0] for item in connection.execute(f"SELECT id FROM ({text})").fetchall())


def _same_meaning(dialect: str, written: str, reference: str) -> None:
    accepted = check(PREFIX + written, dialect=dialect)
    expected = check(PREFIX + reference, dialect="duckdb")
    got = _ids(accepted.text)
    assert got == _ids(expected.text) and got, (dialect, written, accepted.text)


DATE_DIALECTS = ["mysql", "tsql", "spark", "hive", "databricks"]
DATE_PARTS = [("YEAR", "year", 2025), ("MONTH", "month", 3), ("DAY", "day", 31)]


@pytest.mark.parametrize("dialect", DATE_DIALECTS)
@pytest.mark.parametrize(("name", "reference", "value"), DATE_PARTS)
def test_year_month_and_day_are_accepted_and_mean_the_same_in_duckdb(
    dialect: str, name: str, reference: str, value: int
) -> None:
    _same_meaning(dialect, f"{name}(renewed_at) = {value}", f"{reference}(renewed_at) = {value}")
    assert "TsOrDsToDate" in {
        type(node).__name__
        for node in sqlglot.parse_one(f"SELECT {name}(renewed_at)", read=dialect).walk()
    }


@pytest.mark.parametrize("dialect", ["postgres", "redshift", "sqlite", "trino", "drill"])
def test_integer_division_is_accepted_and_truncates_as_in_duckdb(dialect: str) -> None:
    _same_meaning(dialect, "length(plan) / 2 = 2", "length(plan) // 2 = 2")


def test_integer_division_in_tsql_is_accepted_and_truncates_as_in_duckdb() -> None:
    _same_meaning("tsql", "LEN(plan) / 2 = 2", "length(plan) // 2 = 2")


@pytest.mark.parametrize(
    "query",
    [
        # a subquery, a table function, an unlisted function, a string table
        "YEAR((SELECT max(renewed_at) FROM subscriptions)) = 2025",
        "YEAR(read_csv('x.csv')) = 2025",
        "YEAR(foo(renewed_at)) = 2025",
        "MONTH(CASE WHEN renewed THEN (SELECT max(renewed_at) FROM subscriptions) END) = 1",
        "DAY(generate_series(1, 3)) = 1",
    ],
)
def test_a_hostile_argument_of_year_month_or_day_is_still_refused(query: str) -> None:
    refused(PREFIX + query, "FLX-3622", dialect="mysql")


def test_a_string_table_beside_year_is_still_refused() -> None:
    refused("SELECT * FROM 'a.csv' WHERE YEAR(renewed_at) = 2025", "FLX-3623", dialect="mysql")


@pytest.mark.parametrize(
    "query",
    [
        "trunc((SELECT 1)) = 2",
        "trunc(foo(1)) = 2",
        "trunc(unnest([1])) = 2",
        "trunc(amount, 'a') = 2",
        "trunc(amount, id) = 2",
        "trunc(read_csv('x.csv')) = 2",
    ],
)
def test_a_hostile_argument_of_trunc_is_refused(query: str) -> None:
    refused(PREFIX + query, "FLX-3622")


def test_a_hostile_divisor_is_refused_on_the_second_reading() -> None:
    with pytest.raises(FluxError) as caught:
        check(PREFIX + "length(plan) / (SELECT 2) = 2", dialect="postgres")
    assert caught.value.code.code in {"FLX-3622", "FLX-3634"}


def _column() -> exp.Expr:
    return exp.column("renewed_at", table="subscriptions")


def _vet_in_select(node: exp.Expr) -> None:
    tree = exp.select(exp.alias_(node, "a")).from_("subscriptions")
    _allow.vet_tree(tree, label="query 'q'", caps=_allow.SOURCE_CAPS, canonical=False)


def test_the_arguments_sqlglot_writes_for_them_pass_the_node_check() -> None:
    _vet_in_select(exp.TsOrDsToDate(this=_column()))
    _vet_in_select(exp.TsOrDsToDate(this=_column(), default_date=exp.Literal.string("1900-01-01")))
    _vet_in_select(exp.Trunc(this=exp.column("amount")))
    _vet_in_select(exp.Trunc(this=exp.column("amount"), decimals=exp.Literal.number(1)))


@pytest.mark.parametrize(
    "node",
    [
        exp.TsOrDsToDate(this=_column(), format=exp.Literal.string("%Y")),
        exp.TsOrDsToDate(this=_column(), safe=True),
        exp.TsOrDsToDate(this=_column(), default_date=exp.Literal.string("2000-01-01")),
        exp.TsOrDsToDate(this=_column(), default_date=exp.column("renewed_at")),
        exp.TsOrDsToDate(this=_column(), default_date="x"),
        exp.TsOrDsToDate(this=exp.Anonymous(this="foo", expressions=[_column()])),
        exp.TsOrDsToDate(this=exp.Table(this=exp.to_identifier("subscriptions"))),
        exp.Trunc(this=exp.column("amount"), fractions_supported=True),
        exp.Trunc(this=exp.column("amount"), decimals=exp.Literal.string("1")),
        exp.Trunc(this=exp.column("amount"), decimals=exp.column("id")),
        exp.Trunc(this=exp.Anonymous(this="foo", expressions=[exp.Literal.number(1)])),
        exp.Trunc(this=exp.Subquery(this=exp.select("1"))),
    ],
)
def test_any_other_argument_of_the_two_classes_is_refused(node: exp.Expr) -> None:
    with pytest.raises(FluxError) as caught:
        _vet_in_select(node)
    assert caught.value.code.code in {"FLX-3621", "FLX-3622"}


def test_snowflake_to_date_with_a_format_stays_refused() -> None:
    with pytest.raises(FluxError) as caught:
        check(PREFIX + "TO_DATE(plan, 'YYYY') = DATE '2025-01-01'", dialect="snowflake")
    assert caught.value.code.code in {"FLX-3621", "FLX-3622"}


def test_the_depth_limit_still_counts_the_new_nodes() -> None:
    nested = "renewed_at"
    for _ in range(_allow.SOURCE_CAPS.depth + 1):
        nested = f"COALESCE({nested}, renewed_at)"
    with pytest.raises(FluxError) as caught:
        check(PREFIX + f"YEAR({nested}) = 2025", dialect="mysql")
    assert caught.value.code.code == "FLX-3627"
