# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A weekday or week part is refused outside DuckDB, whose numbering differs from the dialects'.

T-SQL ``DATEPART(weekday, x)`` and BigQuery ``EXTRACT(DAYOFWEEK FROM x)`` number Sunday 1, the canonical
DuckDB text numbers it 0, and T-SQL ``DATEPART(week, x)`` is not the ISO week. The check refuses them
in every dialect but ``duckdb`` rather than count a different day.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import pytest

pytest.importorskip("sqlglot")

from .test_check import check, refused  # noqa: E402

pytestmark = pytest.mark.unit

PREFIX = "SELECT * FROM subscriptions WHERE "


@pytest.fixture(autouse=True)
def _quiet_sqlglot() -> None:
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


NUMBERED: List[Tuple[str, str]] = [
    ("tsql", "DATEPART(weekday, renewed_at) = 1"),
    ("tsql", "DATEPART(week, renewed_at) = 1"),
    ("bigquery", "EXTRACT(DAYOFWEEK FROM renewed_at) = 1"),
    ("bigquery", "EXTRACT(WEEK FROM renewed_at) = 1"),
    ("mysql", "DAYOFWEEK(renewed_at) = 1"),
    ("mysql", "WEEK(renewed_at) = 1"),
    ("snowflake", "DAYOFWEEK(renewed_at) = 1"),
    ("postgres", "EXTRACT(dow FROM renewed_at) = 1"),
    ("postgres", "EXTRACT(week FROM renewed_at) = 1"),
    ("postgres", "date_trunc('week', renewed_at) = DATE '2025-01-06'"),
]


@pytest.mark.parametrize(("dialect", "query"), NUMBERED)
def test_a_weekday_or_week_part_is_refused_outside_duckdb(dialect: str, query: str) -> None:
    error = refused(PREFIX + query, "FLX-3622", "weekday or week", dialect=dialect)
    assert dialect in str(error) and "numbering" in str(error)


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
