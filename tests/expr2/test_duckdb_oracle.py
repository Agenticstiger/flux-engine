# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""DuckDB is the oracle for what flux-expr/2 takes from SQL (EXPR-SPEC-2 §11).

Every accepted vector with an aggregate, a window or a date difference is
rendered to a DuckDB query and run over the vector's rows, and DuckDB must
give the vector's values. The rendering is RFC-data-rules.md §2.8's: a
comparison becomes ``COALESCE(p, FALSE)``, so the query is two-valued as the
language is; ``NULLS LAST`` is written out, since it is DuckDB's default only
by accident of version; ``FIRST_VALUE`` and ``LAST_VALUE`` get the whole
partition as their frame; a relationship is a scalar subquery that takes the
first matching row, as the in-memory rows do. Division and ``AVG`` are the
exception the specification names: DuckDB 2.0.0 computes them in
``DOUBLE``, so those vectors are checked against the exact rational only.

The second test compares the three date differences with DuckDB's
``date_sub`` over every ordered pair of 160 dates chosen around month ends
and leap days, as ADR 0008 did on 40,864 pairs.

DuckDB is a test dependency only; the engine never imports it.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from flux_engine.expr2 import Kind, Model, days_between, months_between, parse, years_between
from flux_engine.expr2.ast import (
    Aggregate,
    And,
    Arith,
    Between,
    Bool,
    Call,
    Case,
    Column,
    Compare,
    Date,
    DateAdd,
    In,
    IsDistinctFrom,
    IsNull,
    Neg,
    Node,
    Not,
    Null,
    Number,
    Or,
    Path,
    String,
    Window,
)
from flux_engine.expr2.scope import Expose

from .support import (
    CORE_VECTORS,
    EXTENSION_VECTORS,
    SHARED_VECTORS,
    decode_row,
    load,
    matches,
    models_of,
    normal,
)

duckdb = pytest.importorskip("duckdb")

_PREDICATES = (Compare, In, Between, IsNull, IsDistinctFrom, Not, And, Or)


def _sql_type(expose: Expose, column: str) -> str:
    declared = expose.columns[column]
    if declared.kind is Kind.NUMBER:
        if declared.is_float:
            return "DOUBLE"
        if declared.integer_range is not None:
            return "BIGINT"
        # DuckDB's widest decimal has 38 digits; a wider column (forge-cli's
        # vectors declare decimal(1000, 2)) only has to exist for the query.
        if declared.precision is not None and declared.precision <= 38:
            return f"DECIMAL({declared.precision}, {declared.scale})"
        return "DECIMAL(38, 10)"
    return {
        Kind.BOOL: "BOOLEAN",
        Kind.DATE: "DATE",
        Kind.TIMESTAMP: "BIGINT",
        Kind.TIMESTAMPTZ: "BIGINT",
    }.get(
        declared.kind, "VARCHAR"
    )  # type: ignore[arg-type]


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class _Renderer:
    def __init__(self, model: Model) -> None:
        self.model = model
        self.aliases = 0

    def alias(self) -> str:
        self.aliases += 1
        return f"x{self.aliases}"

    # -------------------------------------------------------- predicates

    def predicate(self, node: Node, scope: Expose, alias: str) -> str:
        if isinstance(node, Compare):
            return f"COALESCE(({self.value(node.left, scope, alias)} {node.op} {self.value(node.right, scope, alias)}), FALSE)"
        if isinstance(node, In):
            items = ", ".join(self.value(item, scope, alias) for item in node.items)
            keyword = "NOT IN" if node.negated else "IN"
            return f"COALESCE(({self.value(node.value, scope, alias)} {keyword} ({items})), FALSE)"
        if isinstance(node, Between):
            v = self.value(node.value, scope, alias)
            lo = self.value(node.low, scope, alias)
            hi = self.value(node.high, scope, alias)
            if node.negated:
                return f"(COALESCE(({v} < {lo}), FALSE) OR COALESCE(({v} > {hi}), FALSE))"
            return f"COALESCE(({v} BETWEEN {lo} AND {hi}), FALSE)"
        if isinstance(node, IsNull):
            return (
                f"({self.value(node.value, scope, alias)} IS {'NOT ' if node.negated else ''}NULL)"
            )
        if isinstance(node, IsDistinctFrom):
            keyword = "IS NOT DISTINCT FROM" if node.negated else "IS DISTINCT FROM"
            return f"({self.value(node.left, scope, alias)} {keyword} {self.value(node.right, scope, alias)})"
        if isinstance(node, Not):
            return f"(NOT {self.predicate(node.operand, scope, alias)})"
        if isinstance(node, And):
            return f"({self.predicate(node.left, scope, alias)} AND {self.predicate(node.right, scope, alias)})"
        if isinstance(node, Or):
            return f"({self.predicate(node.left, scope, alias)} OR {self.predicate(node.right, scope, alias)})"
        return f"COALESCE({self.value(node, scope, alias)}, FALSE)"

    # ------------------------------------------------------------ values

    def value(self, node: Node, scope: Expose, alias: str) -> str:
        if isinstance(node, _PREDICATES):
            return self.predicate(node, scope, alias)
        if isinstance(node, Number):
            return node.text
        if isinstance(node, String):
            return _literal(node.value)
        if isinstance(node, Bool):
            return "TRUE" if node.value else "FALSE"
        if isinstance(node, Null):
            return "NULL"
        if isinstance(node, Date):
            return f"DATE {_literal(node.value)}"
        if isinstance(node, Column):
            if node.relationship is None:
                return f"{alias}.{_quote(node.name)}"
            return self.hops((node.relationship,), node.name, scope, alias)
        if isinstance(node, Path):
            if node.root == "previous":
                return self.previous(node.parts, scope, alias)
            return self.hops(node.parts[:-1], node.parts[-1], scope, alias)
        if isinstance(node, Neg):
            return f"(-{self.value(node.operand, scope, alias)})"
        if isinstance(node, Arith):
            return f"({self.value(node.left, scope, alias)} {node.op} {self.value(node.right, scope, alias)})"
        if isinstance(node, Case):
            parts = ["CASE"]
            for condition, result in node.whens:
                parts += [
                    "WHEN",
                    self.predicate(condition, scope, alias),
                    "THEN",
                    self.value(result, scope, alias),
                ]
            if node.otherwise is not None:
                parts += ["ELSE", self.value(node.otherwise, scope, alias)]
            return " ".join(parts + ["END"])
        if isinstance(node, Call):
            args = [self.value(arg, scope, alias) for arg in node.args]
            if node.name == "COALESCE":
                return f"COALESCE({', '.join(args)})"
            if node.name == "ROUND":
                assert (
                    node.mode == "HALF_AWAY_FROM_ZERO"
                ), "DuckDB's ROUND rounds ties away from zero only"
                return f"ROUND({args[0]}, {args[1]})"
            unit = {"DAYS_BETWEEN": "day", "MONTHS_BETWEEN": "month", "YEARS_BETWEEN": "year"}[
                node.name
            ]
            return f"date_sub('{unit}', {args[0]}, {args[1]})"
        if isinstance(node, DateAdd):
            moved = self.value(node.date, scope, alias)
            count = self.value(node.count, scope, alias)
            # DuckDB's interval counts are 32-bit; a count past them leaves the range
            # whatever the date is, so TRY_CAST's NULL is the language's answer too.
            if node.unit == "day":
                return f"CAST(({moved} + to_days(TRY_CAST({count} AS INTEGER))) AS DATE)"
            months = f"TRY_CAST({count} AS BIGINT)"
            if node.unit == "year":
                months = f"TRY_CAST(TRY_CAST({count} AS BIGINT) * 12 AS INTEGER)"
            return f"CAST(({moved} + to_months(TRY_CAST({months} AS INTEGER))) AS DATE)"
        if isinstance(node, Aggregate):
            return self.aggregate(node, scope, alias)
        if isinstance(node, Window):
            return self.window(node, scope, alias)
        raise AssertionError(f"no DuckDB rendering for {node!r}")

    def hops(self, steps: Sequence[str], column: str, scope: Expose, alias: str) -> str:
        if not steps:
            return f"{alias}.{_quote(column)}"
        link = scope.links[steps[0]]
        target = self.model.exposes[link.target]
        inner = self.alias()
        join = " AND ".join(
            f"{inner}.{_quote(to)} = {alias}.{_quote(frm)}"
            for frm, to in zip(link.from_columns, link.to_columns, strict=True)
        )
        rest = self.hops(steps[1:], column, target, inner)
        return f"(SELECT {rest} FROM {_quote(target.name)} {inner} WHERE {join} ORDER BY {inner}.__row LIMIT 1)"

    def previous(self, parts: Sequence[str], scope: Expose, alias: str) -> str:
        versions = scope.versions
        assert versions is not None
        inner = self.alias()
        same = " AND ".join(f"{inner}.{_quote(c)} = {alias}.{_quote(c)}" for c in versions.entity)
        start = _quote(versions.start)
        rest = self.hops(parts[:-1], parts[-1], scope, inner)
        return (
            f"(SELECT {rest} FROM {_quote(scope.name)} {inner} WHERE {same} AND {inner}.{start} < {alias}.{start} "
            f"ORDER BY {inner}.{start} DESC, {inner}.__row LIMIT 1)"
        )

    def aggregate(self, node: Aggregate, scope: Expose, alias: str) -> str:
        child = self.model.exposes[node.children.expose]
        if node.children.relationship is not None:
            link = child.links[node.children.relationship]
        else:
            (link,) = self.model.children_links(scope.name, child.name)
        inner = self.alias()
        join = " AND ".join(
            f"{inner}.{_quote(frm)} = {alias}.{_quote(to)}"
            for frm, to in zip(link.from_columns, link.to_columns, strict=True)
        )
        if node.name == "COUNT":
            summary = "COUNT(*)"
            if node.arg is not None:
                summary += f" FILTER (WHERE {self.predicate(node.arg, child, inner)})"
        else:
            assert node.arg is not None
            summary = f"{node.name}({self.value(node.arg, child, inner)})"
        return f"(SELECT {summary} FROM {_quote(child.name)} {inner} WHERE {join})"

    def window(self, node: Window, scope: Expose, alias: str) -> str:
        link = scope.links[node.partition]
        args = ", ".join(self.value(arg, scope, alias) for arg in node.args)
        partition = ", ".join(f"{alias}.{_quote(c)}" for c in link.from_columns)
        keys = []
        for key in node.order:
            text = self.value(key.key, scope, alias) + (" DESC" if key.desc else " ASC")
            keys.append(text + (" NULLS FIRST" if key.nulls_first else " NULLS LAST"))
        frame = ""
        if node.name in ("FIRST_VALUE", "LAST_VALUE"):
            frame = " ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING"
        return (
            f"{node.name}({args}) OVER (PARTITION BY {partition} ORDER BY {', '.join(keys)}{frame})"
        )


def _walk(node: Node) -> List[Node]:
    from flux_engine.expr2.rules import _parts

    out = [node]
    for part in _parts(node):
        out.extend(_walk(part))
    return out


def _wants_oracle(tree: Node) -> bool:
    nodes = _walk(tree)
    names = {getattr(n, "name", None) for n in nodes}
    borrowed = any(isinstance(n, (Aggregate, Window, DateAdd)) for n in nodes) or bool(
        names & {"DAYS_BETWEEN", "MONTHS_BETWEEN", "YEARS_BETWEEN"}
    )
    exact_only = "AVG" in names or any(isinstance(n, Arith) and n.op == "/" for n in nodes)
    return borrowed and not exact_only


def _connect(model: Model, tables: Mapping[str, List[Dict[str, Any]]]) -> Any:
    connection = duckdb.connect()
    for name, expose in model.exposes.items():
        columns = ", ".join(f"{_quote(c)} {_sql_type(expose, c)}" for c in expose.columns)
        connection.execute(f"CREATE TABLE {_quote(name)} (__row INTEGER, {columns})")
        rows = tables.get(name, [])
        if rows:
            names = list(expose.columns)
            marks = ", ".join("?" for _ in range(len(names) + 1))
            connection.executemany(
                f"INSERT INTO {_quote(name)} VALUES ({marks})",
                [[index] + [row.get(c) for c in names] for index, row in enumerate(rows)],
            )
    return connection


def _normal(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+-\d\d-\d\d( \(BC\))?", value):
        # DuckDB's DATE reaches past 9999-12-31 and before 0001-01-01; the language's does
        # not (EXPR-SPEC-3 §4), and a result outside it is NULL.
        return value if 1 <= int(value.split("-")[0]) <= 9999 and "BC" not in value else None
    if isinstance(value, (int, Decimal)):
        return Fraction(value)
    return value


#: A vector whose ADD_* result leaves 0001-01-01 to 9999-12-31 inside a larger
#: expression: DuckDB's DATE goes on past the range, so its difference is a number
#: where the language's is NULL.
_OUT_OF_THE_ORACLES_RANGE = {"add-months-then-months-between"}


def _oracle_cases() -> List[Any]:
    cases = []
    for path in (SHARED_VECTORS, CORE_VECTORS, EXTENSION_VECTORS):
        for raw in load(path)["vectors"]:
            vector = normal(raw)
            if vector["refused"] is not None:
                continue
            if vector["id"] in _OUT_OF_THE_ORACLES_RANGE:
                continue
            if _wants_oracle(parse(vector["text"], side=vector["side"]).tree):
                cases.append(pytest.param(path, vector, id=vector["id"]))
    return cases


ORACLE_CASES = _oracle_cases()


def test_the_oracle_runs_every_kind_of_borrowed_vector() -> None:
    ids = " ".join(case.id for case in ORACLE_CASES)
    for kind in ("aggregate-", "window-", "days-between", "months-between", "years-between"):
        assert kind in ids, kind
    assert len(ORACLE_CASES) >= 30


@pytest.fixture(scope="module")
def shared() -> Any:
    """One connection per dataset, built once: the extension vectors share theirs."""
    connections: Dict[Tuple[str, str], Any] = {}
    yield connections
    for connection in connections.values():
        connection.close()


@pytest.mark.parametrize(("path", "vector"), ORACLE_CASES)
def test_duckdb_agrees(
    path: Any, vector: Mapping[str, Any], shared: Dict[Tuple[str, str], Any]
) -> None:
    document = load(path)
    models = models_of(document)
    model = models[vector["model"]]
    scope = model.exposes[vector["expose"]]
    tree = parse(vector["text"], side=vector["side"]).tree
    cases = vector["eval"]
    renderer = _Renderer(model)
    rendered = (
        renderer.predicate(tree, scope, "t0")
        if vector["position"] == "predicate"
        else renderer.value(tree, scope, "t0")
    )
    query = f"SELECT t0.__row, {rendered} FROM {_quote(scope.name)} t0 ORDER BY t0.__row"
    if "data" in cases[0]:
        key = (path.name, cases[0]["data"])
        if key not in shared:
            data = document["data"][key[1]]
            tables = {
                name: [decode_row(row, model.exposes[name]) for row in rows]
                for name, rows in data["rows"].items()
            }
            shared[key] = _connect(model, tables)
        expected = {int(case["row"]): case["result"] for case in cases}
        got = shared[key].execute(query).fetchall()
    else:
        tables = {scope.name: [decode_row(case["row"], scope) for case in cases]}
        expected = {index: case["result"] for index, case in enumerate(cases)}
        connection = _connect(model, tables)
        try:
            got = connection.execute(query).fetchall()
        finally:
            connection.close()
    assert len(got) == len(expected)
    for row, value in got:
        assert matches(expected[row], _normal(value)), (vector["id"], row, value, rendered)


def _dates() -> List[date]:
    days: List[date] = []
    months = (
        [(1999, m) for m in (11, 12)]
        + [(2000, m) for m in range(1, 13)]
        + [(2001, m) for m in (1, 2, 3)]
    )
    months += [(2023, 12)] + [(2024, m) for m in range(1, 13)] + [(2025, m) for m in (1, 2, 3)]
    for year, month in months:
        for day in (1, 15, 28, 29, 30, 31):
            try:
                days.append(date(year, month, day))
            except ValueError:
                continue
    return sorted(set(days))


def test_date_differences_agree_with_duckdb_date_sub() -> None:
    dates = _dates()
    assert len(dates) >= 160
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TABLE d (x DATE)")
        connection.executemany("INSERT INTO d VALUES (?)", [[day] for day in dates])
        rows = connection.execute(
            "SELECT a.x, b.x, date_sub('day', a.x, b.x), date_sub('month', a.x, b.x), "
            "date_sub('year', a.x, b.x) FROM d a, d b"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == len(dates) ** 2
    for a, b, days, months, years in rows:
        assert (days_between(a, b), months_between(a, b), years_between(a, b)) == (
            days,
            months,
            years,
        ), (a, b)


def test_date_differences_match_their_definition() -> None:
    """The count is the largest k with a moved forward k units not after b, for a sample of pairs."""
    from flux_engine.expr2.functions import add_months

    dates = _dates()
    for a in dates[::7]:
        for b in dates[::5]:
            if b < a:
                assert months_between(a, b) == -months_between(b, a)
                continue
            k = 0
            while add_months(a, k + 1) <= b:
                k += 1
            assert months_between(a, b) == k
            assert years_between(a, b) == max(
                j for j in range(0, k // 12 + 1) if add_months(a, 12 * j) <= b
            )
            assert days_between(a, b) == (b - a) // timedelta(days=1)
