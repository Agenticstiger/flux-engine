# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""flux-expr/3's DuckDB oracle (EXPR-SPEC-3 section 6).

Every accepted vector of ``expr3-vectors.json`` also runs as a DuckDB query,
written as section 6 says, and must agree. This module imports the flux-expr/2
oracle, which skips without DuckDB, so only the oracle is skipped; the
engine's own flux-expr/3 tests in ``test_expr3.py`` run regardless. DuckDB is a
test dependency only; the engine never imports it.
"""

from __future__ import annotations

from datetime import date
from typing import Any, List, Mapping, Optional

import pytest

from flux_engine.expr2 import (
    Expr2Error,
    Parsed,
    Side,
    add_days,
    add_months,
    add_years,
    check,
    parse,
)
from flux_engine.expr2.ast import Concat, DateAdd, Extract, Node
from flux_engine.expr2.scope import Expose

from .support import decode_row, matches
from .test_duckdb_oracle import _connect, _normal, _quote, _Renderer
from .test_expr3 import CALENDAR, DOCUMENT, LANGUAGE_3, VECTORS

pytestmark = pytest.mark.unit


_IN_RANGE = "BETWEEN DATE '0001-01-01' AND DATE '9999-12-31'"


class _Expr3Renderer(_Renderer):
    """EXPR-SPEC-3 section 6's DuckDB spelling of the three extensions.

    A date outside 0001-01-01 to 9999-12-31 is NULL in flux-expr/3, which
    DuckDB does not share (its dates run far past both ends), so the query
    writes that range out.
    """

    def kind(self, node: Node, scope: Expose) -> Optional[str]:
        try:
            checked = check(
                Parsed("", node, Side.FLUX, 0, LANGUAGE_3),
                self.model,
                scope.name,
                position="value",
            )
        except Expr2Error:
            return None  # a NULL literal
        return None if checked.type.kind is None else checked.type.kind.value

    def text(self, node: Node, scope: Expose, alias: str) -> str:
        rendered = self.value(node, scope, alias)
        return f"CAST({rendered} AS VARCHAR)" if self.kind(node, scope) == "number" else rendered

    def bounded(self, date_text: str) -> str:
        return f"(CASE WHEN {date_text} {_IN_RANGE} THEN {date_text} END)"

    def value(self, node: Node, scope: Expose, alias: str) -> str:
        if isinstance(node, Extract):
            return f"extract({node.part} FROM {self.value(node.arg, scope, alias)})"
        if isinstance(node, DateAdd):
            start = self.value(node.date, scope, alias)
            count = f"CAST({self.value(node.count, scope, alias)} AS INTEGER)"
            if node.unit == "day":
                return self.bounded(f"({start} + {count})")
            interval = {"month": "to_months", "year": "to_years"}[node.unit]
            return self.bounded(f"CAST({start} + {interval}({count}) AS DATE)")
        if isinstance(node, Concat):
            return (
                f"({self.text(node.left, scope, alias)} || {self.text(node.right, scope, alias)})"
            )
        return super().value(node, scope, alias)


def _oracle_cases() -> List[Any]:
    return [pytest.param(p.values[0], id=p.id) for p in VECTORS if p.values[0]["refused"] is None]


ORACLE_CASES = _oracle_cases()


def test_the_oracle_runs_every_accepted_vector() -> None:
    assert len(ORACLE_CASES) >= 40


@pytest.fixture(scope="module")
def calendar_connection() -> Any:
    duckdb = pytest.importorskip("duckdb")
    assert duckdb is not None
    data = DOCUMENT["data"]["calendar"]
    model = CALENDAR["calendar"]
    tables = {
        name: [decode_row(row, model.exposes[name]) for row in rows]
        for name, rows in data["rows"].items()
    }
    connection = _connect(model, tables)
    yield connection
    connection.close()


@pytest.mark.parametrize("vector", ORACLE_CASES)
def test_duckdb_agrees(vector: Mapping[str, Any], calendar_connection: Any) -> None:
    model = CALENDAR[vector["model"]]
    scope = model.exposes[vector["expose"]]
    tree = parse(vector["text"], side=vector["side"], language=vector["language"]).tree
    renderer = _Expr3Renderer(model)
    rendered = (
        renderer.predicate(tree, scope, "t0")
        if vector["position"] == "predicate"
        else renderer.value(tree, scope, "t0")
    )
    query = f"SELECT t0.__row, {rendered} FROM {_quote(scope.name)} t0 ORDER BY t0.__row"
    expected = {int(case["row"]): case["result"] for case in vector["eval"]}
    got = calendar_connection.execute(query).fetchall()
    assert len(got) == len(expected)
    for row, value in got:
        assert matches(expected[row], _normal(value)), (vector["id"], row, value, rendered)


def test_the_date_functions_agree_with_duckdb_over_month_ends_and_leap_days() -> None:
    duckdb = pytest.importorskip("duckdb")
    days: List[date] = []
    for year, month in [
        (1999, 12),
        (2000, 1),
        (2000, 2),
        (2000, 3),
        (2001, 1),
        (2001, 2),
        (2003, 2),
    ]:
        for day in (1, 15, 28, 29, 30, 31):
            try:
                days.append(date(year, month, day))
            except ValueError:
                continue
    counts = list(range(-30, 31)) + [-400, -49, 47, 120, 365, 1461]
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TABLE d (x DATE)")
        connection.executemany("INSERT INTO d VALUES (?)", [[day] for day in days])
        connection.execute("CREATE TABLE c (n INTEGER)")
        connection.executemany("INSERT INTO c VALUES (?)", [[n] for n in counts])
        rows = connection.execute(
            "SELECT d.x, c.n, d.x + c.n, CAST(d.x + to_months(c.n) AS DATE), "
            "CAST(d.x + to_years(c.n) AS DATE) FROM d, c"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == len(days) * len(counts)
    for start, n, plus_days, plus_months, plus_years in rows:
        assert add_days(start, n) == plus_days
        assert add_months(start, n) == plus_months
        assert add_years(start, n) == plus_years
