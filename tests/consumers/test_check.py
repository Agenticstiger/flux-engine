# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The consumer SQL check: what it accepts, what it refuses, and the text it writes (ADR 0028).

Pinned here: the canonical text and digest of each accepted query in each dialect; one refusal per
rule of ADR 0028 section 2, with the hostile texts a bundle from someone else could carry; the
bounds of the read; that a comment never carries a second statement into the canonical text; the
total-order rule for LIMIT, OFFSET and windows; table and column resolution; and that the text
and digest are the same in every process. No DuckDB runs here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest
import yaml

sqlglot = pytest.importorskip("sqlglot")

from sqlglot import exp  # noqa: E402

from flux_engine.consumers import _allow  # noqa: E402
from flux_engine.consumers.check import (  # noqa: E402
    SQLGLOT_VERSION,
    CheckedQuery,
    _Checker,
    check_query,
    check_registry,
    digest_of_text,
)
from flux_engine.consumers.schema import (  # noqa: E402
    ExposeSchema,
    bundle_schema,
    expose_schema,
    sql_type,
)
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.seam.consumers import (  # noqa: E402
    ConsumerQuery,
    ConsumerRegistry,
    read_consumers_block,
)
from flux_engine.seam.contract import (  # noqa: E402
    LogicalKind,
    LogicalType,
    compile_expose,
    find_expose,
)
from flux_engine.seam.keys import ExposeKeys, compile_keys  # noqa: E402
from flux_engine.seam.resolve import declared_consumers  # noqa: E402

pytestmark = pytest.mark.unit

EMITS = frozenset({"subscriptions", "accounts"})
SCHEMA = bundle_schema(
    [
        ExposeSchema(
            "subscriptions",
            (
                ("id", "BIGINT"),
                ("plan", "VARCHAR"),
                ("region", "VARCHAR"),
                ("renewed", "BOOLEAN"),
                ("renewed_at", "DATE"),
                ("started", "TIMESTAMP"),
                ("amount", "DECIMAL(18,4)"),
            ),
            unique=(("id",),),
        ),
        ExposeSchema("accounts", (("id", "BIGINT"), ("owner", "VARCHAR")), unique=(("id",),)),
    ]
)
SUBSCRIPTION_COLUMNS = SCHEMA.exposes[1].column_names


@pytest.fixture(autouse=True)
def _quiet_sqlglot() -> None:
    # sqlglot logs a warning each time it falls back to a Command, as the hostile texts make it.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


def _registry(
    sql: str,
    *,
    dialect: str = "duckdb",
    parameters: Any = None,
    unit: Optional[str] = None,
    groupBy: Optional[str] = None,
    **block: Any,
) -> Any:
    query: Dict[str, Any] = {"id": "q", "sql": sql}
    if parameters:
        query["parameters"] = parameters
    if unit is not None:
        query["unit"] = unit
    if groupBy is not None:
        query["groupBy"] = groupBy
    queries = block.pop("queries", [query])
    return read_consumers_block(
        {"dialect": dialect, "queries": queries, **block}, emits=EMITS, where="consumers"
    )


def check(sql: str, **kwargs: Any) -> CheckedQuery:
    """The checked query for *sql*, in the dialect and with the registry keys *kwargs* give."""
    registry = _registry(sql, **kwargs)
    return check_query(registry, registry.queries[0], SCHEMA)


def refused(sql: str, code: str, mention: str = "", **kwargs: Any) -> FluxError:
    with pytest.raises(FluxError) as caught:
        check(sql, **kwargs)
    assert caught.value.code.code == code, str(caught.value)
    assert "query 'q'" in str(caught.value)
    assert mention in str(caught.value)
    return caught.value


def refused_query(query: ConsumerQuery, code: str, mention: str = "", **registry: Any) -> None:
    with pytest.raises(FluxError) as caught:
        check_query(ConsumerRegistry(**registry), query, SCHEMA)
    assert caught.value.code.code == code, str(caught.value)
    assert mention in str(caught.value)


ACCEPTED: List[Tuple[str, str, str, Dict[str, Any], Dict[str, Any], str]] = [
    (
        "duckdb-plain",
        "duckdb",
        "SELECT plan, count(*) AS n FROM subscriptions WHERE renewed GROUP BY plan HAVING count(*) > 1 ORDER BY n DESC, plan",
        {},
        {},
        'SELECT "subscriptions"."plan" AS "plan", COUNT(*) AS "n" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."renewed" GROUP BY "subscriptions"."plan" HAVING COUNT(*) > 1 ORDER BY "n" DESC, "plan"',
    ),
    (
        "snowflake-prefix",
        "snowflake",
        "SELECT plan FROM prod.analytics.subscriptions WHERE region IN ('EU', 'US') AND renewed_at >= DATEADD(day, -30, CURRENT_DATE())",
        {},
        {"asOf": "2026-06-30", "prefixes": ["prod.analytics"]},
        'SELECT "subscriptions"."plan" AS "plan" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."region" IN (\'EU\', \'US\') AND "subscriptions"."renewed_at" >= (CAST(\'2026-06-30\' AS DATE) + INTERVAL (-30) DAY)',
    ),
    (
        "bigquery-backtick",
        "bigquery",
        "SELECT plan FROM `prod.analytics.subscriptions` WHERE started >= TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL -1 DAY)",
        {},
        {"asOf": "2026-06-30", "prefixes": ["prod.analytics"]},
        'SELECT "subscriptions"."plan" AS "plan" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."started" >= (CAST(\'2026-06-30 00:00:00\' AS TIMESTAMP) + INTERVAL \'-1\' DAY)',
    ),
    (
        "postgres-cast",
        "postgres",
        "SELECT plan, date_trunc('month', renewed_at) AS m FROM public.subscriptions s WHERE s.amount::numeric(18, 2) > 10",
        {},
        {"prefixes": ["public"]},
        'SELECT "s"."plan" AS "plan", DATE_TRUNC(\'MONTH\', "s"."renewed_at") AS "m" FROM "subscriptions" AS "s" WHERE CAST("s"."amount" AS DECIMAL(18, 2)) > 10',
    ),
    (
        "tables-alias",
        "snowflake",
        "SELECT sub_plan FROM prod.analytics.subs",
        {},
        {
            "tables": {"prod.analytics.subs": "subscriptions"},
            "columns": {"subscriptions": {"sub_plan": "plan"}},
        },
        'SELECT "subs"."plan" AS "plan" FROM "subscriptions" AS "subs"',
    ),
    (
        "column-rename",
        "duckdb",
        "SELECT sub_plan, count(*) AS n FROM subscriptions WHERE sub_plan <> 'x' GROUP BY sub_plan",
        {},
        {"columns": {"subscriptions": {"sub_plan": "plan"}}},
        'SELECT "subscriptions"."plan" AS "plan", COUNT(*) AS "n" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."plan" <> \'x\' GROUP BY "subscriptions"."plan"',
    ),
    (
        "view-of-view",
        "duckdb",
        "SELECT plan FROM premium WHERE id > 3",
        {},
        {
            "views": [
                {
                    "name": "renewed",
                    "sql": "SELECT id, plan, region FROM subscriptions WHERE renewed",
                },
                {"name": "premium", "sql": "SELECT id, plan FROM renewed WHERE plan = 'premium'"},
            ]
        },
        'WITH "renewed" AS (SELECT "subscriptions"."id" AS "id", "subscriptions"."plan" AS "plan", "subscriptions"."region" AS "region" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."renewed"), "premium" AS (SELECT "renewed"."id" AS "id", "renewed"."plan" AS "plan" FROM "renewed" AS "renewed" WHERE "renewed"."plan" = \'premium\') SELECT "premium"."plan" AS "plan" FROM "premium" AS "premium" WHERE "premium"."id" > 3',
    ),
    (
        "placeholder-string",
        "duckdb",
        "SELECT id FROM subscriptions WHERE plan = :plan",
        {"plan": "pre'mium"},
        {},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."plan" = \'pre\'\'mium\'',
    ),
    (
        "placeholder-int",
        "snowflake",
        "SELECT id FROM subscriptions WHERE id < :n",
        {"n": 7},
        {},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."id" < 7',
    ),
    (
        "placeholder-negative",
        "duckdb",
        "SELECT id FROM subscriptions WHERE id > $n",
        {"n": -3},
        {},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."id" > -3',
    ),
    (
        "placeholder-bool",
        "duckdb",
        "SELECT id FROM subscriptions WHERE renewed = $flag",
        {"flag": True},
        {},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."renewed" = TRUE',
    ),
    (
        "placeholder-date",
        "bigquery",
        "SELECT id FROM subscriptions WHERE renewed_at >= @since",
        {"since": "2026-01-01"},
        {},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."renewed_at" >= CAST(\'2026-01-01\' AS DATE)',
    ),
    (
        "placeholder-timestamp",
        "snowflake",
        "SELECT id FROM subscriptions WHERE started < :ts",
        {"ts": "2026-01-01 12:00:00"},
        {},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."started" < CAST(\'2026-01-01 12:00:00\' AS TIMESTAMP)',
    ),
    (
        "placeholder-decimal",
        "postgres",
        "SELECT id FROM subscriptions WHERE amount BETWEEN %(low)s AND %(high)s",
        {"low": "1.5", "high": "9.25"},
        {},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."amount" BETWEEN CAST(\'1.5\' AS DECIMAL(18, 4)) AND CAST(\'9.25\' AS DECIMAL(18, 4))',
    ),
    (
        "placeholder-in",
        "duckdb",
        "SELECT id FROM subscriptions WHERE renewed_at IN ($a, $b)",
        {"a": "2026-01-01", "b": "2026-02-01"},
        {},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."renewed_at" IN (CAST(\'2026-01-01\' AS DATE), CAST(\'2026-02-01\' AS DATE))',
    ),
    (
        "current-date",
        "duckdb",
        "SELECT id FROM subscriptions WHERE renewed_at = CURRENT_DATE",
        {},
        {"asOf": "2026-06-30"},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."renewed_at" = CAST(\'2026-06-30\' AS DATE)',
    ),
    (
        "current-timestamp",
        "snowflake",
        "SELECT id FROM subscriptions WHERE started < CURRENT_TIMESTAMP()",
        {},
        {"asOf": "2026-06-30"},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."started" < CAST(\'2026-06-30 00:00:00\' AS TIMESTAMP)',
    ),
    (
        "now-function",
        "duckdb",
        "SELECT id FROM subscriptions WHERE started < now()",
        {},
        {"asOf": "2026-06-30"},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."started" < CAST(\'2026-06-30 00:00:00\' AS TIMESTAMP)',
    ),
    (
        "join",
        "duckdb",
        "SELECT s.id, a.owner FROM subscriptions s LEFT JOIN accounts a ON a.id = s.id WHERE a.owner IS NOT NULL",
        {},
        {},
        'SELECT "s"."id" AS "id", "a"."owner" AS "owner" FROM "subscriptions" AS "s" LEFT JOIN "accounts" AS "a" ON "a"."id" = "s"."id" WHERE NOT "a"."owner" IS NULL',
    ),
    (
        "set-operation",
        "duckdb",
        "SELECT id FROM subscriptions WHERE renewed UNION ALL SELECT id FROM accounts EXCEPT SELECT id FROM subscriptions WHERE region = 'EU'",
        {},
        {},
        'SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."renewed" UNION ALL SELECT "accounts"."id" AS "id" FROM "accounts" AS "accounts" EXCEPT SELECT "subscriptions"."id" AS "id" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."region" = \'EU\'',
    ),
    (
        "cte-and-subquery",
        "duckdb",
        "WITH r AS (SELECT id, plan FROM subscriptions WHERE renewed) SELECT plan FROM r WHERE id IN (SELECT id FROM accounts) AND EXISTS (SELECT 1 FROM r r2 WHERE r2.id = r.id)",
        {},
        {},
        'WITH "r" AS (SELECT "subscriptions"."id" AS "id", "subscriptions"."plan" AS "plan" FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."renewed") SELECT "r"."plan" AS "plan" FROM "r" AS "r" WHERE "r"."id" IN (SELECT "accounts"."id" AS "id" FROM "accounts" AS "accounts") AND EXISTS(SELECT 1 AS "1" FROM "r" AS "r2" WHERE "r2"."id" = "r"."id")',
    ),
    (
        "windows",
        "duckdb",
        "SELECT id, row_number() OVER (PARTITION BY plan ORDER BY id) AS rn, sum(amount) OVER (PARTITION BY plan) AS total, rank() OVER (ORDER BY id DESC) AS r FROM subscriptions",
        {},
        {},
        'SELECT "subscriptions"."id" AS "id", ROW_NUMBER() OVER (PARTITION BY "subscriptions"."plan" ORDER BY "subscriptions"."id") AS "rn", SUM("subscriptions"."amount") OVER (PARTITION BY "subscriptions"."plan") AS "total", RANK() OVER (ORDER BY "subscriptions"."id" DESC) AS "r" FROM "subscriptions" AS "subscriptions"',
    ),
    (
        "case-coalesce",
        "duckdb",
        "SELECT CASE WHEN amount > 5 THEN 'big' ELSE coalesce(region, 'none') END AS size, nullif(plan, '') AS p, CAST(id AS VARCHAR) AS i, lower(plan) || upper(region) AS lu FROM subscriptions WHERE NOT renewed AND (plan LIKE 'a%' OR region ILIKE 'e%')",
        {},
        {},
        'SELECT CASE WHEN "subscriptions"."amount" > 5 THEN \'big\' ELSE COALESCE("subscriptions"."region", \'none\') END AS "size", NULLIF("subscriptions"."plan", \'\') AS "p", CAST("subscriptions"."id" AS TEXT) AS "i", LOWER("subscriptions"."plan") || UPPER("subscriptions"."region") AS "lu" FROM "subscriptions" AS "subscriptions" WHERE NOT "subscriptions"."renewed" AND ("subscriptions"."plan" LIKE \'a%\' OR "subscriptions"."region" ILIKE \'e%\')',
    ),
    (
        "limit-ordered-by-key",
        "duckdb",
        "SELECT plan FROM subscriptions ORDER BY id LIMIT 10 OFFSET 5",
        {},
        {},
        'SELECT "subscriptions"."plan" AS "plan" FROM "subscriptions" AS "subscriptions" ORDER BY "subscriptions"."id" LIMIT 10 OFFSET 5',
    ),
    (
        "dates",
        "duckdb",
        "SELECT extract(year FROM renewed_at) AS y, year(renewed_at) AS y2, date_trunc('month', started) AS m, renewed_at + INTERVAL 7 DAY AS w FROM subscriptions",
        {},
        {},
        'SELECT EXTRACT(YEAR FROM "subscriptions"."renewed_at") AS "y", YEAR("subscriptions"."renewed_at") AS "y2", DATE_TRUNC(\'MONTH\', "subscriptions"."started") AS "m", "subscriptions"."renewed_at" + INTERVAL \'7\' DAY AS "w" FROM "subscriptions" AS "subscriptions"',
    ),
]


@pytest.mark.parametrize(
    "name,dialect,sql,parameters,block,expected", ACCEPTED, ids=[case[0] for case in ACCEPTED]
)
def test_accepted_query_has_its_pinned_canonical_text(
    name: str,
    dialect: str,
    sql: str,
    parameters: Dict[str, Any],
    block: Dict[str, Any],
    expected: str,
) -> None:
    checked = check(sql, dialect=dialect, parameters=parameters, **block)
    assert checked.text == expected
    assert checked.digest == "sha256:" + hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert checked.sqlglot_version == SQLGLOT_VERSION == "30.33.0"
    # The text is one statement that parses again, in DuckDB's dialect, to the tree that is kept.
    again = sqlglot.parse(expected, read="duckdb")
    assert len(again) == 1
    assert checked.tree == again[0]
    assert checked.tree.sql(dialect="duckdb", comments=False) == expected


def test_the_kept_tree_is_qualified_and_has_no_star_left() -> None:
    checked = check("SELECT * FROM subscriptions s JOIN accounts a ON a.id = s.id")
    for column in checked.tree.find_all(exp.Column):
        assert column.table
    assert not list(checked.tree.find_all(exp.Star))
    assert checked.exposes == ("accounts", "subscriptions")


def test_every_dialect_the_brief_names_reads_a_prefixed_table() -> None:
    texts = {
        "duckdb": "SELECT plan FROM prod.analytics.subscriptions",
        "snowflake": "SELECT plan FROM PROD.ANALYTICS.SUBSCRIPTIONS",
        "bigquery": "SELECT plan FROM `prod.analytics.subscriptions`",
        "postgres": 'SELECT plan FROM prod.analytics."subscriptions"',
    }
    for dialect, sql in texts.items():
        checked = check(sql, dialect=dialect, prefixes=["prod.analytics"])
        assert checked.text == (
            'SELECT "subscriptions"."plan" AS "plan" FROM "subscriptions" AS "subscriptions"'
        ), dialect


# ---------------------------------------------------------------------------------------------
# Stable text and digest
# ---------------------------------------------------------------------------------------------

_STABLE_BLOCK = {
    "dialect": "snowflake",
    "asOf": "2026-06-30",
    "prefixes": ["prod.analytics"],
    "columns": {"subscriptions": {"sub_plan": "plan"}},
    "views": [
        {
            "name": "renewed",
            "sql": "SELECT id, sub_plan FROM prod.analytics.subscriptions WHERE renewed",
        },
        {"name": "premium", "sql": "SELECT * FROM renewed WHERE sub_plan = 'premium'"},
    ],
    "queries": [
        {
            "id": "a",
            "sql": "SELECT sub_plan, count(*) AS n FROM premium GROUP BY sub_plan",
            "unit": "premium",
            "groupBy": "plan",
        },
        {
            "id": "b",
            "sql": "SELECT s.id FROM prod.analytics.subscriptions s JOIN accounts a ON a.id = s.id "
            "WHERE s.renewed_at < CURRENT_DATE AND s.sub_plan IN ('x', 'y') AND s.amount > :min",
            "parameters": {"min": "2.5"},
        },
    ],
}


def _digests(block: Mapping[str, Any]) -> List[str]:
    registry = read_consumers_block(dict(block), emits=EMITS, where="consumers")
    result = check_registry(registry, SCHEMA)
    return [v.digest for v in result.views] + [q.digest for q in result.queries]


def test_text_and_digest_are_stable_across_two_runs() -> None:
    first, second = _digests(_STABLE_BLOCK), _digests(_STABLE_BLOCK)
    assert first == second and len(first) == 4 and len(set(first)) == 4


_STABLE_SCRIPT = """
import json, sys
from flux_engine.consumers.check import check_registry
from flux_engine.consumers.schema import ExposeSchema, bundle_schema
from flux_engine.seam.consumers import read_consumers_block
block, schema = json.loads(sys.stdin.read())
bundle = bundle_schema(
    [ExposeSchema(n, tuple(map(tuple, c)), tuple(map(tuple, u))) for n, c, u in schema]
)
registry = read_consumers_block(block, emits=frozenset(s[0] for s in schema), where="c")
out = check_registry(registry, bundle)
print(json.dumps([v.digest for v in out.views] + [q.digest for q in out.queries]))
"""


@pytest.mark.parametrize("seed", ["1", "2", "random"])
def test_digest_does_not_depend_on_the_hash_seed(seed: str) -> None:
    schema = [[e.expose_id, e.columns, e.unique] for e in SCHEMA.exposes]
    result = subprocess.run(
        [sys.executable, "-c", _STABLE_SCRIPT],
        input=json.dumps([_STABLE_BLOCK, schema]),
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
    )
    assert json.loads(result.stdout) == _digests(_STABLE_BLOCK)


def test_digest_is_the_sha256_of_the_utf8_text_as_the_engine_writes_digests() -> None:
    assert digest_of_text("é") == "sha256:" + hashlib.sha256("é".encode("utf-8")).hexdigest()
    checked = check("SELECT id FROM subscriptions WHERE plan = 'é'")
    assert checked.digest == digest_of_text(checked.text)


def test_the_sqlglot_version_that_wrote_the_text_is_recorded_on_views_too() -> None:
    registry = _registry("SELECT 1", views=[{"name": "v", "sql": "SELECT id FROM subscriptions"}])
    result = check_registry(registry, SCHEMA)
    assert result.views[0].sqlglot_version == result.queries[0].sqlglot_version == SQLGLOT_VERSION


# ---------------------------------------------------------------------------------------------
# One statement for both parsers: comments are dropped, and the text is read again
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 /* ; DROP TABLE t */",
        "SELECT 1 -- ; DROP TABLE t\n",
        "SELECT id /* ; DROP TABLE t */ FROM subscriptions WHERE id = 1 -- ; ATTACH 'x.db'\n",
        "SELECT /* ; */ id FROM /* ; DROP TABLE subscriptions */ subscriptions",
    ],
)
def test_a_comment_carrying_a_statement_never_reaches_the_canonical_text(sql: str) -> None:
    checked = check(sql)
    assert ";" not in checked.text and "--" not in checked.text and "/*" not in checked.text
    assert "DROP" not in checked.text.upper() and "ATTACH" not in checked.text.upper()
    assert len(sqlglot.parse(checked.text, read="duckdb")) == 1


def test_a_comment_marker_inside_a_string_is_a_literal_and_stays() -> None:
    checked = check("SELECT id FROM subscriptions WHERE plan = '-- ; /* x */'")
    assert checked.text.endswith("= '-- ; /* x */'")


def test_a_second_statement_behind_a_comment_is_still_refused() -> None:
    refused("SELECT 1 /* x */; DROP TABLE t -- y", "FLX-3619", "2 statements")


def test_the_canonical_text_is_read_again_and_must_read_the_same_tables() -> None:
    checker = _Checker(_registry("SELECT 1"), SCHEMA)
    first = [("expose", "subscriptions")]
    checker._reread(
        'SELECT 1 AS "1" FROM "subscriptions" AS "subscriptions"', first, {}, "query 'q'"
    )
    for text, mention in [
        ('SELECT 1 AS "1" FROM "accounts" AS "accounts"', "other tables"),
        ('SELECT 1; DROP TABLE "subscriptions"', "FLX-3619"),
        ('SELECT RANDOM() FROM "subscriptions"', "FLX-3622"),
        ('SELECT 1 FROM "subscriptions" AS "subscriptions" WHERE x = ?', "FLX-"),
        ("SELECT * FROM 'subscriptions.parquet'", "FLX-3623"),
        ('SELECT 1 FROM "nowhere"', "no expose or WITH entry"),
        ("PRAGMA database_list", "FLX-3620"),
        ('SELECT 1 FROM "subscriptions" AS "subscriptions" LIMIT 3', "FLX-3630"),
    ]:
        with pytest.raises(FluxError) as caught:
            checker._reread(text, first, {"subscriptions": SCHEMA.exposes[1]}, "query 'q'")
        assert caught.value.code.code == "FLX-3634", text
        assert mention in str(caught.value), text


def test_a_canonical_text_that_fails_the_second_reading_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlglot.expressions import Select

    original = Select.sql

    def evil(self: Any, *args: Any, **kwargs: Any) -> str:
        text = original(self, *args, **kwargs)
        return text + "; DROP TABLE subscriptions" if kwargs.get("comments") is False else text

    monkeypatch.setattr(Select, "sql", evil)
    refused("SELECT id FROM subscriptions", "FLX-3634", "FLX-3619")


# ---------------------------------------------------------------------------------------------
# Refusals: the statement
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql,code",
    [
        ("SELECT * FROM read_csv('/etc/passwd')", "FLX-3623"),
        ("SELECT * FROM 'x.parquet'", "FLX-3623"),
        ("SELECT * FROM subscriptions JOIN 'x.csv' ON 1 = 1", "FLX-3623"),
        ("SELECT * FROM subscriptions, 'x.csv'", "FLX-3623"),
        ("ATTACH 'x.db'", "FLX-3620"),
        ("COPY subscriptions TO 'x.csv'", "FLX-3620"),
        ("PRAGMA database_list", "FLX-3620"),
        ("SET threads = 4", "FLX-3620"),
        ("INSTALL httpfs", "FLX-3620"),
        ("LOAD httpfs", "FLX-3620"),
        ("CALL pragma_version()", "FLX-3620"),
        ("EXPORT DATABASE 'd'", "FLX-3618"),
        ("CREATE TABLE t (a INT)", "FLX-3620"),
        ("INSERT INTO t VALUES (1)", "FLX-3620"),
        ("UPDATE subscriptions SET id = 1", "FLX-3620"),
        ("DELETE FROM subscriptions", "FLX-3620"),
        ("DROP TABLE subscriptions", "FLX-3620"),
        ("ALTER TABLE subscriptions ADD COLUMN x INT", "FLX-3620"),
        ("DESCRIBE subscriptions", "FLX-3620"),
        ("EXPLAIN SELECT 1", "FLX-3620"),
        ("VALUES (1)", "FLX-3620"),
        ("(SELECT 1)", "FLX-3620"),
        ("SELECT 1; DROP TABLE t", "FLX-3619"),
        ("SELECT 1; SELECT 2", "FLX-3619"),
        ("", "FLX-3618"),
        (";", "FLX-3618"),
        ("SELECT FROM WHERE", "FLX-3618"),
        ("SELECT 'unterminated", "FLX-3618"),
    ],
)
def test_a_text_that_is_not_one_query_is_refused(sql: str, code: str) -> None:
    query = ConsumerQuery(id="q", sql=sql)
    refused_query(query, code, "query 'q'")


def test_with_recursive_is_refused_and_a_plain_with_is_not() -> None:
    refused(
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT n FROM r",
        "FLX-3621",
        "WITH RECURSIVE",
    )
    check("WITH r AS (SELECT id FROM subscriptions) SELECT id FROM r")


def test_a_with_entry_cannot_read_itself_or_a_later_entry() -> None:
    refused("WITH r AS (SELECT n FROM r) SELECT n FROM r", "FLX-3624", "'r'")
    refused(
        "WITH a AS (SELECT id FROM b), b AS (SELECT id FROM subscriptions) SELECT id FROM a",
        "FLX-3624",
        "'b'",
    )


def test_a_with_name_cannot_repeat() -> None:
    refused("WITH a AS (SELECT 1 AS x), a AS (SELECT 2 AS x) SELECT x FROM a", "FLX-3621", "twice")


# ---------------------------------------------------------------------------------------------
# Refusals: functions
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT getenv('HOME')",
        "SELECT current_setting('threads')",
        "SELECT random()",
        "SELECT uuid()",
        "SELECT nextval('s')",
        "SELECT currval('s')",
        "SELECT read_text('/etc/passwd')",
        "SELECT read_csv('/etc/passwd')",
        "SELECT current_schema()",
        "SELECT current_database()",
        "SELECT version()",
        "SELECT md5(plan) FROM subscriptions",
        "SELECT hash(plan) FROM subscriptions",
        "SELECT least(id, 1) FROM subscriptions",
        "SELECT regexp_matches(plan, 'x') FROM subscriptions",
        "SELECT plan ~ 'x' FROM subscriptions",
        "SELECT TRY_CAST(plan AS INT) FROM subscriptions",
        "SELECT current_time",
        "SELECT unnest([1, 2])",
        "SELECT epoch(now()) FROM subscriptions",
        "SELECT * FROM subscriptions WHERE id IN (SELECT unnest([1]))",
    ],
)
def test_a_function_outside_the_list_is_refused(sql: str) -> None:
    refused(sql, "FLX-3622", "which is not on the list", asOf="2026-06-30")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM range(10)",
        "SELECT * FROM generate_series(1, 10)",
        "SELECT * FROM glob('*')",
        "SELECT * FROM read_parquet('x.parquet')",
        "SELECT * FROM subscriptions WHERE id IN (SELECT * FROM read_parquet('x'))",
        "SELECT * FROM (SELECT * FROM glob('*')) AS g",
    ],
)
def test_a_table_function_is_refused(sql: str) -> None:
    refused(sql, "FLX-3623", "not a name")


def test_a_stage_reference_is_not_a_table() -> None:
    refused("SELECT * FROM @my_stage/file.csv", "FLX-3623", dialect="snowflake")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM subscriptions, LATERAL (SELECT 1)",
        "SELECT * FROM (VALUES (1)) v",
        "SELECT * FROM subscriptions NATURAL JOIN accounts",
        "SELECT * FROM subscriptions s ASOF JOIN accounts a ON s.id >= a.id",
        "SELECT * FROM subscriptions s JOIN accounts a USING (id)",
        "SELECT plan FROM subscriptions GROUP BY ROLLUP (plan)",
        "SELECT plan FROM subscriptions GROUP BY ALL",
        "SELECT plan FROM subscriptions GROUP BY GROUPING SETS ((plan))",
        "SELECT sum(id) OVER (ORDER BY id ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) FROM subscriptions",
        "SELECT sum(id) OVER w FROM subscriptions WINDOW w AS (ORDER BY id)",
        "SELECT count(*) FILTER (WHERE renewed) FROM subscriptions",
        "SELECT * FROM subscriptions QUALIFY row_number() OVER (ORDER BY id) = 1",
        "SELECT * EXCLUDE (plan) FROM subscriptions",
        "SELECT DISTINCT ON (plan) plan FROM subscriptions",
        "SELECT * FROM subscriptions LIMIT 10 PERCENT",
        "SELECT * FROM subscriptions FETCH FIRST 3 ROWS ONLY",
        "SELECT * FROM subscriptions FOR UPDATE",
        "SELECT * FROM subscriptions TABLESAMPLE (10 PERCENT)",
        "SELECT 1 INTO x",
        "SELECT [1, 2][1]",
        "SELECT {'a': 1}",
        "SELECT * FROM subscriptions WHERE id > ANY (SELECT id FROM accounts)",
        "SELECT * FROM subscriptions AS s(a, b, c, d, e, f, g)",
        "SELECT a.b.c.id FROM subscriptions",
        "SELECT plan LIKE 'a!%' ESCAPE '!' FROM subscriptions",
        "SELECT CAST(plan AS DOUBLE) FROM subscriptions",
        "SELECT plan::JSON FROM subscriptions",
        "SELECT CAST(plan AS BLOB) FROM subscriptions",
        "SELECT plan COLLATE nocase FROM subscriptions",
        "SELECT rank() FROM subscriptions",
        "SELECT sum(DISTINCT id) OVER (ORDER BY id) FROM subscriptions",
        "SELECT",
    ],
)
def test_an_expression_outside_the_list_is_refused(sql: str) -> None:
    error = pytest.raises(FluxError, check, sql)
    assert error.value.code.code in {"FLX-3621", "FLX-3622"}, str(error.value)
    assert "query 'q'" in str(error.value)


def test_a_limit_or_offset_that_is_not_a_whole_number_is_refused() -> None:
    refused(
        "SELECT id FROM subscriptions ORDER BY id LIMIT (SELECT 1)", "FLX-3621", "LIMIT or OFFSET"
    )


def test_a_unit_that_is_not_a_date_part_is_refused() -> None:
    refused("SELECT extract(foo FROM renewed_at) FROM subscriptions", "FLX-3621", "date part")


# ---------------------------------------------------------------------------------------------
# The allowlists themselves
# ---------------------------------------------------------------------------------------------


def test_no_hostile_class_is_on_the_lists() -> None:
    allowed = {*_allow.EXPRESSIONS, *_allow.FUNCTIONS}
    hostile = {
        exp.Command,
        exp.Pragma,
        exp.Set,
        exp.Copy,
        exp.Attach,
        exp.Install,
        exp.Create,
        exp.Insert,
        exp.Drop,
        exp.Anonymous,
        exp.ReadCSV,
        exp.GenerateSeries,
        exp.Rand,
        exp.Uuid,
        exp.Unnest,
        exp.Lateral,
        exp.Values,
        exp.TryCast,
        exp.CurrentTime,
        exp.CurrentDate,
        exp.CurrentTimestamp,
        exp.Placeholder,
        exp.Parameter,
        exp.SessionParameter,
    }
    assert not allowed & hostile
    assert all(isinstance(item, type) and issubclass(item, exp.Expr) for item in allowed)


def test_a_subclass_of_an_allowed_class_is_not_allowed() -> None:
    class Sneaky(exp.And):
        pass

    tree = exp.Select(
        expressions=[
            exp.Alias(
                this=Sneaky(this=exp.true(), expression=exp.true()), alias=exp.to_identifier("a")
            )
        ]
    )
    with pytest.raises(FluxError) as caught:
        _allow.vet_tree(tree, label="query 'q'", caps=_allow.SOURCE_CAPS, canonical=False)
    assert caught.value.code.code == "FLX-3622" and "Sneaky" in str(caught.value)


def test_the_canonical_pass_allows_neither_a_clock_nor_a_placeholder_nor_an_unknown_function() -> (
    None
):
    for text in ("SELECT CURRENT_DATE", "SELECT now()", "SELECT $x", "SELECT foo(1)"):
        tree = sqlglot.parse_one(text, read="duckdb")
        with pytest.raises(FluxError):
            _allow.vet_tree(tree, label="q", caps=_allow.CANONICAL_CAPS, canonical=True)


def test_every_function_the_list_names_is_accepted() -> None:
    accepted = [
        "count(*)",
        "count(DISTINCT plan)",
        "sum(amount)",
        "avg(amount)",
        "min(id)",
        "max(id)",
        "stddev(amount)",
        "variance(amount)",
        "lower(plan)",
        "upper(plan)",
        "trim(plan)",
        "length(plan)",
        "substring(plan, 1, 2)",
        "concat(plan, region)",
        "replace(plan, 'a', 'b')",
        "abs(amount)",
        "round(amount, 2)",
        "floor(amount)",
        "ceil(amount)",
        "date_trunc('month', renewed_at)",
        "extract(year FROM renewed_at)",
        "year(renewed_at)",
        "month(renewed_at)",
        "day(renewed_at)",
        "renewed_at + INTERVAL 3 DAY",
        "started - INTERVAL '2' HOUR",
        "date_diff('day', renewed_at, DATE '2026-01-01')",
        "coalesce(region, plan)",
        "nullif(plan, 'x')",
        "CASE WHEN renewed THEN 1 ELSE 0 END",
        "CAST(id AS VARCHAR)",
        "CAST(amount AS DECIMAL(10, 2))",
        "CAST(started AS DATE)",
        "CAST(renewed_at AS TIMESTAMP)",
        "CAST(id AS BIGINT)",
        "CAST(renewed AS BOOLEAN)",
    ]
    for item in accepted:
        check(f"SELECT {item} AS v FROM subscriptions")
    check("SELECT row_number() OVER (ORDER BY id) AS v FROM subscriptions")
    check("SELECT rank() OVER (PARTITION BY plan ORDER BY id) AS v FROM subscriptions")
    check("SELECT dense_rank() OVER (ORDER BY id) AS v FROM subscriptions")
    check("SELECT sum(amount) OVER (PARTITION BY plan) AS v FROM subscriptions")


# ---------------------------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------------------------


def test_100_000_nested_brackets_are_refused_by_the_token_cap_before_the_parser_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the parser must not run")

    monkeypatch.setattr(sqlglot, "parse", never)
    sql = "SELECT " + "(" * 100_000 + "1" + ")" * 100_000
    refused_query(ConsumerQuery(id="q", sql=sql), "FLX-3627", "20000 tokens")


def test_brackets_nested_past_64_are_refused_on_the_tokens() -> None:
    deep = "SELECT " + "(" * 65 + "1" + ")" * 65
    refused_query(ConsumerQuery(id="q", sql=deep), "FLX-3627", "brackets more than 64")
    refused_query(
        ConsumerQuery(id="q", sql="SELECT 1 WHERE " + "[" * 65 + "1" + "]" * 65),
        "FLX-3627",
        "brackets more than 64",
    )


def test_a_very_long_or_chain_is_refused() -> None:
    for terms, mention in [(100, "levels deep"), (5_000, "tokens"), (40_000, "tokens")]:
        sql = "SELECT id FROM subscriptions WHERE " + " OR ".join(["id = 1"] * terms)
        refused_query(ConsumerQuery(id="q", sql=sql), "FLX-3627", mention, as_of=None)


def test_a_chain_of_40_terms_is_read() -> None:
    check("SELECT id FROM subscriptions WHERE " + " OR ".join(["id = 1"] * 40))


def test_more_than_20_000_tokens_are_refused() -> None:
    sql = "SELECT " + ", ".join(["1"] * 10_001)
    refused_query(ConsumerQuery(id="q", sql=sql), "FLX-3627", "20000 tokens")


def test_more_than_10_000_nodes_are_refused() -> None:
    sql = "SELECT " + ", ".join(["plan"] * 6_000) + " FROM subscriptions"
    refused_query(ConsumerQuery(id="q", sql=sql), "FLX-3627", "10000 expression nodes")


def test_nesting_past_64_levels_of_the_tree_is_refused() -> None:
    sql = "SELECT 1" + " + 1" * 80
    refused_query(ConsumerQuery(id="q", sql=sql), "FLX-3627", "64 levels")


def test_a_recursion_error_inside_sqlglot_is_a_named_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import flux_engine.consumers.check as module

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(sqlglot, "parse", boom)
    refused("SELECT 1", "FLX-3628", "sqlglot")
    monkeypatch.undo()
    monkeypatch.setattr(module, "qualify", boom)
    refused("SELECT id FROM subscriptions", "FLX-3628", "sqlglot")
    monkeypatch.undo()
    monkeypatch.setattr(module, "check_total_orders", boom)
    refused("SELECT id FROM subscriptions", "FLX-3628", "sqlglot")


def test_a_recursion_error_while_checking_a_registry_is_named_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RecursionError

    registry = _registry("SELECT 1", views=[{"name": "v", "sql": "SELECT id FROM subscriptions"}])
    monkeypatch.setattr(sqlglot, "parse", boom)
    with pytest.raises(FluxError) as caught:
        check_registry(registry, SCHEMA)
    assert caught.value.code.code == "FLX-3628" and "view 'v'" in str(caught.value)


def test_a_sqlglot_error_of_any_kind_is_a_named_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    import flux_engine.consumers.check as module

    def odd(*args: Any, **kwargs: Any) -> Any:
        raise KeyError("odd")

    monkeypatch.setattr(sqlglot, "parse", odd)
    refused("SELECT 1", "FLX-3618", "does not parse")
    monkeypatch.undo()
    monkeypatch.setattr(module, "qualify", odd)
    refused("SELECT id FROM subscriptions", "FLX-3633")


def test_a_nul_character_is_refused() -> None:
    refused_query(ConsumerQuery(id="q", sql="SELECT 1\x00"), "FLX-3618", "NUL")


def test_a_placeholder_in_limit_or_offset_takes_a_non_negative_whole_number() -> None:
    checked = check(
        "SELECT plan FROM subscriptions ORDER BY id LIMIT :n OFFSET :m", parameters={"n": 5, "m": 2}
    )
    assert checked.text.endswith('ORDER BY "subscriptions"."id" LIMIT 5 OFFSET 2')
    for value in ("5", -1, True):
        refused(
            "SELECT plan FROM subscriptions ORDER BY id LIMIT :n",
            "FLX-3626",
            "non-negative whole number",
            parameters={"n": value},
        )


def test_an_error_nobody_named_is_still_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    import flux_engine.consumers.check as module

    def odd(*args: Any, **kwargs: Any) -> Any:
        raise KeyError("odd")

    monkeypatch.setattr(module, "check_total_orders", odd)
    error = refused("SELECT id FROM subscriptions", "FLX-3636", "KeyError")
    assert isinstance(error.__cause__, KeyError)


def test_a_query_that_reads_too_many_views_is_refused_before_it_is_assembled() -> None:
    column_list = ", ".join(["id"] * 3_000)
    views = [{"name": "v0", "sql": f"SELECT {column_list} FROM accounts"}]
    for index in range(1, 25):
        views.append({"name": f"v{index}", "sql": f"SELECT {column_list} FROM v{index - 1}"})
    registry = _registry("SELECT id FROM v24", views=views)
    with pytest.raises(FluxError) as caught:
        check_query(registry, registry.queries[0], SCHEMA)
    assert caught.value.code.code == "FLX-3627"
    assert "with the views it reads" in str(caught.value)


# ---------------------------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------------------------


def test_a_table_resolves_through_tables_then_prefixes_then_the_expose_id() -> None:
    tables = {"prod.analytics.subs": "subscriptions", "subscriptions": "accounts"}
    # 1. the tables map, on all its parts, wins over an expose of that name.
    assert check("SELECT id FROM subscriptions", tables=tables).exposes == ("accounts",)
    # 2. a prefix is dropped and the rest is matched against tables and then exposeIds.
    checked = check("SELECT id FROM warehouse.accounts", prefixes=["warehouse", "prod.analytics"])
    assert checked.exposes == ("accounts",)
    checked = check("SELECT id FROM prod.analytics.accounts", prefixes=["prod.analytics"])
    assert checked.exposes == ("accounts",)
    checked = check(
        "SELECT id FROM warehouse.subs",
        tables={"subs": "accounts"},
        prefixes=["warehouse"],
    )
    assert checked.exposes == ("accounts",)
    # 3. an expose id; 4. a WITH name or a view.
    assert check("SELECT id FROM accounts").exposes == ("accounts",)
    assert check("WITH w AS (SELECT id FROM accounts) SELECT id FROM w").exposes == ("accounts",)


def test_an_alias_defaults_to_the_name_the_consumer_wrote() -> None:
    checked = check(
        "SELECT subs.id FROM prod.analytics.subs",
        tables={"prod.analytics.subs": "subscriptions"},
    )
    assert checked.text == ('SELECT "subs"."id" AS "id" FROM "subscriptions" AS "subs"')


def test_a_table_that_resolves_to_nothing_names_what_the_bundle_exposes() -> None:
    error = refused("SELECT * FROM nowhere", "FLX-3624", "'nowhere'")
    assert "accounts" in str(error) and "subscriptions" in str(error)
    refused("SELECT * FROM other.subscriptions", "FLX-3624", "'other.subscriptions'")
    refused("SELECT * FROM a.b.c.subscriptions", "FLX-3624")


def test_unquoted_names_compare_without_regard_to_case_and_quoted_names_exactly() -> None:
    assert check("SELECT id FROM SUBSCRIPTIONS").exposes == ("subscriptions",)
    assert check('SELECT id FROM "subscriptions"').exposes == ("subscriptions",)
    refused('SELECT id FROM "Subscriptions"', "FLX-3624", "'Subscriptions'")
    assert check("SELECT id FROM PROD.Analytics.subscriptions", prefixes=["prod.analytics"])
    refused(
        'SELECT id FROM "PROD"."analytics".subscriptions', "FLX-3624", prefixes=["prod.analytics"]
    )
    assert check('SELECT id FROM "prod"."analytics".subscriptions', prefixes=["prod.analytics"])


def _twin_schema() -> Any:
    return bundle_schema(
        [
            ExposeSchema("Orders", (("id", "BIGINT"),)),
            ExposeSchema("orders", (("id", "BIGINT"),)),
            ExposeSchema("items", (("id", "BIGINT"),)),
        ]
    )


@pytest.mark.parametrize(
    "sql", ["SELECT id FROM ORDERS", "SELECT id FROM orders", 'SELECT id FROM "orders"']
)
def test_two_exposes_that_one_name_reads_are_ambiguous(sql: str) -> None:
    registry = ConsumerRegistry()
    with pytest.raises(FluxError) as caught:
        check_query(registry, ConsumerQuery(id="q", sql=sql), _twin_schema())
    assert caught.value.code.code == "FLX-3625"
    assert "Orders" in str(caught.value) and "orders" in str(caught.value)
    # an expose with no twin reads as before
    check_query(registry, ConsumerQuery(id="q", sql="SELECT id FROM items"), _twin_schema())


def test_a_with_entry_or_a_view_that_shadows_an_expose_is_ambiguous() -> None:
    refused("WITH subscriptions AS (SELECT 1 AS id) SELECT id FROM subscriptions", "FLX-3625")
    refused(
        "SELECT id FROM accounts WHERE id IN (WITH accounts AS (SELECT 1 AS id) SELECT id FROM accounts)",
        "FLX-3625",
    )
    registry = _registry(
        "WITH v AS (SELECT 1 AS id) SELECT id FROM v",
        views=[{"name": "v", "sql": "SELECT id FROM accounts"}],
    )
    with pytest.raises(FluxError) as caught:
        check_query(registry, registry.queries[0], SCHEMA)
    assert caught.value.code.code == "FLX-3625"


def test_a_with_entry_visible_only_where_it_is_defined() -> None:
    check("WITH w AS (SELECT id FROM accounts) SELECT id FROM w WHERE id IN (SELECT id FROM w)")
    refused(
        "SELECT id FROM accounts WHERE id IN (WITH w AS (SELECT 1 AS id) SELECT id FROM w) "
        "AND id IN (SELECT id FROM w)",
        "FLX-3624",
        "'w'",
    )


# ---------------------------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------------------------

RENAME = {"subscriptions": {"sub_plan": "plan", "since": "started"}}


def test_a_written_name_is_the_contracts_name_wherever_a_column_name_is_written() -> None:
    checked = check(
        "SELECT sub_plan, since FROM subscriptions WHERE sub_plan = 'a' ORDER BY since, id",
        columns=RENAME,
    )
    assert checked.text == (
        'SELECT "subscriptions"."plan" AS "plan", "subscriptions"."started" AS "started" '
        'FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."plan" = \'a\' '
        'ORDER BY "started", "subscriptions"."id"'
    )


def test_the_contracts_own_name_reads_the_same_column() -> None:
    both = check("SELECT plan, sub_plan FROM subscriptions WHERE plan = sub_plan", columns=RENAME)
    assert both.text == (
        'SELECT "subscriptions"."plan" AS "plan", "subscriptions"."plan" AS "plan" '
        'FROM "subscriptions" AS "subscriptions" WHERE "subscriptions"."plan" = "subscriptions"."plan"'
    )
    plain = check("SELECT plan FROM subscriptions", columns=RENAME)
    assert plain.text == check("SELECT sub_plan FROM subscriptions", columns=RENAME).text


def test_a_star_and_every_output_name_are_the_contracts() -> None:
    star = check("SELECT * FROM subscriptions", columns=RENAME)
    assert star.tree.named_selects == list(SUBSCRIPTION_COLUMNS)
    renamed = check("SELECT s.sub_plan AS since FROM subscriptions s", columns=RENAME)
    assert renamed.tree.named_selects == ["started"]


def test_a_written_name_is_read_through_aliases_views_with_entries_and_subqueries() -> None:
    sql = (
        "WITH w AS (SELECT s.sub_plan AS p, s.id FROM subscriptions s) "
        "SELECT p FROM w WHERE EXISTS (SELECT 1 FROM subscriptions t WHERE t.sub_plan = w.p AND t.id = w.id)"
    )
    checked = check(sql, columns=RENAME)
    assert '"s"."plan" AS "p"' in checked.text
    assert '"t"."plan" = "w"."p"' in checked.text
    union = check(
        "SELECT sub_plan FROM subscriptions UNION SELECT owner FROM accounts", columns=RENAME
    )
    assert union.text.startswith('SELECT "subscriptions"."plan" AS "plan"')
    listed = check("WITH w(sub_plan, n) AS (SELECT 'x', 1) SELECT sub_plan FROM w", columns=RENAME)
    assert listed.text.startswith('WITH "w" AS (SELECT \'x\' AS "plan", 1 AS "n")')
    assert listed.tree.named_selects == ["plan"]


def test_a_table_name_that_is_a_written_name_is_not_renamed() -> None:
    checked = check(
        "WITH sub_plan AS (SELECT id FROM subscriptions) SELECT sub_plan.id FROM sub_plan",
        columns=RENAME,
    )
    assert 'FROM "sub_plan" AS "sub_plan"' in checked.text


def test_a_name_that_is_in_no_table_is_not_a_column() -> None:
    refused("SELECT nope FROM subscriptions", "FLX-3633", "'nope'")
    refused("SELECT x.id FROM subscriptions", "FLX-3633")
    refused("SELECT id FROM subscriptions s JOIN accounts a ON s.id = a.id", "FLX-3633", "'id'")
    refused("SELECT sub_plan FROM subscriptions", "FLX-3633", "'sub_plan'")


def test_columns_that_differ_only_in_case_are_refused() -> None:
    schema = bundle_schema([ExposeSchema("t", (("Plan", "VARCHAR"), ("plan", "VARCHAR")))])
    with pytest.raises(FluxError) as caught:
        check_query(ConsumerRegistry(), ConsumerQuery(id="q", sql="SELECT 1 FROM t"), schema)
    assert caught.value.code.code == "FLX-3635"


def test_a_registry_that_names_what_the_schema_lacks_is_refused() -> None:
    for kwargs in (
        {"tables": (("x", "nowhere"),)},
        {"columns": (("nowhere", (("a", "b"),)),)},
        {"columns": (("accounts", (("a", "nope"),)),)},
        {"columns": (("accounts", (("owner", "id"),)),)},
        {"columns": (("accounts", (("x", "owner"),)), ("subscriptions", (("x", "plan"),)))},
        {"columns": (("accounts", (("region", "owner"),)),)},
        {"dialect": "klingon"},
    ):
        registry = ConsumerRegistry(**kwargs)
        query = ConsumerQuery(id="q", sql="SELECT id FROM accounts")
        with pytest.raises(FluxError) as caught:
            check_query(registry, query, SCHEMA)
        assert caught.value.code.code == "FLX-3635", kwargs


# ---------------------------------------------------------------------------------------------
# Placeholders and the clock
# ---------------------------------------------------------------------------------------------


def test_a_placeholder_becomes_a_typed_literal_in_the_tree_never_text() -> None:
    hostile = "x'); DROP TABLE subscriptions; --"
    checked = check("SELECT id FROM subscriptions WHERE plan = :p", parameters={"p": hostile})
    assert checked.text.endswith("= 'x''); DROP TABLE subscriptions; --'")
    assert len(sqlglot.parse(checked.text, read="duckdb")) == 1
    literals = [n for n in checked.tree.find_all(exp.Literal) if n.is_string]
    assert [n.this for n in literals] == [hostile]


def test_each_placeholder_style_reads_in_its_dialect() -> None:
    for dialect, sql in [
        ("duckdb", "SELECT id FROM subscriptions WHERE id = $v"),
        ("snowflake", "SELECT id FROM subscriptions WHERE id = :v"),
        ("bigquery", "SELECT id FROM subscriptions WHERE id = @v"),
        ("postgres", "SELECT id FROM subscriptions WHERE id = %(v)s"),
        ("tsql", "SELECT id FROM subscriptions WHERE id = @v"),
    ]:
        checked = check(sql, dialect=dialect, parameters={"v": 7})
        assert checked.text.endswith('"subscriptions"."id" = 7'), dialect


def test_a_string_compared_with_a_date_timestamp_or_decimal_becomes_a_cast() -> None:
    sql = (
        "SELECT id FROM subscriptions WHERE renewed_at = :d AND started > :t AND amount <= :a "
        "AND plan = :p AND id IN (:i, 5)"
    )
    checked = check(
        sql,
        parameters={"d": "2026-01-01", "t": "2026-01-01 00:00:00", "a": "9.5", "p": "x", "i": 4},
    )
    assert "= CAST('2026-01-01' AS DATE)" in checked.text
    assert "> CAST('2026-01-01 00:00:00' AS TIMESTAMP)" in checked.text
    assert "<= CAST('9.5' AS DECIMAL(18, 4))" in checked.text
    assert "\"plan\" = 'x'" in checked.text
    assert "IN (4, 5)" in checked.text


def test_a_boolean_and_a_whole_number_keep_their_types() -> None:
    checked = check(
        "SELECT id FROM subscriptions WHERE renewed = :b AND id > :n AND id < :m",
        parameters={"b": False, "n": 0, "m": -12},
    )
    assert checked.text.endswith(
        '= FALSE AND "subscriptions"."id" > 0 AND "subscriptions"."id" < -12'
    )


def test_placeholders_and_parameters_must_match() -> None:
    refused("SELECT id FROM subscriptions WHERE id = :x", "FLX-3626", "'x' and no value")
    refused(
        "SELECT id FROM subscriptions WHERE id = :x",
        "FLX-3626",
        "['y']",
        parameters={"x": 1, "y": 2},
    )
    refused("SELECT id FROM subscriptions WHERE id = ?", "FLX-3626", "positional")
    refused(
        "SELECT id FROM subscriptions WHERE id = $1", "FLX-3626", "positional", dialect="postgres"
    )
    refused(
        "SELECT id FROM subscriptions WHERE id = %s", "FLX-3626", "positional", dialect="postgres"
    )
    refused(
        "SELECT id FROM subscriptions WHERE id = ?", "FLX-3626", "positional", dialect="snowflake"
    )


def test_a_parameter_value_outside_the_closed_types_is_refused() -> None:
    for value in (2**63, "a\x00b"):
        query = ConsumerQuery(
            id="q", sql="SELECT id FROM subscriptions WHERE id = :v", parameters=(("v", value),)
        )
        refused_query(query, "FLX-3626", "'v'")
    for value in (1.5, None, [1]):
        query = ConsumerQuery(
            id="q", sql="SELECT id FROM subscriptions WHERE id = :v", parameters=(("v", value),)
        )
        refused_query(query, "FLX-3626", "'v'")


def test_a_placeholder_in_a_view_has_no_value() -> None:
    registry = _registry(
        "SELECT id FROM v",
        views=[{"name": "v", "sql": "SELECT id FROM subscriptions WHERE id = :x"}],
    )
    with pytest.raises(FluxError) as caught:
        check_query(registry, registry.queries[0], SCHEMA)
    assert caught.value.code.code == "FLX-3626" and "view 'v'" in str(caught.value)


@pytest.mark.parametrize(
    "dialect,sql,kind",
    [
        ("duckdb", "SELECT CURRENT_DATE AS d", "date"),
        ("duckdb", "SELECT today() AS d", "date"),
        ("snowflake", "SELECT CURRENT_DATE() AS d", "date"),
        ("bigquery", "SELECT CURRENT_DATE() AS d", "date"),
        ("duckdb", "SELECT CURRENT_TIMESTAMP AS d", "timestamp"),
        ("duckdb", "SELECT now() AS d", "timestamp"),
        ("duckdb", "SELECT LOCALTIMESTAMP AS d", "timestamp"),
        ("snowflake", "SELECT SYSDATE() AS d", "timestamp"),
        ("tsql", "SELECT GETDATE() AS d", "timestamp"),
        ("bigquery", "SELECT CURRENT_DATETIME() AS d", "timestamp"),
        ("postgres", "SELECT now() AS d", "timestamp"),
    ],
)
def test_the_current_time_needs_as_of_and_reads_as_it(dialect: str, sql: str, kind: str) -> None:
    refused(sql, "FLX-3629", "asOf", dialect=dialect)
    checked = check(sql, dialect=dialect, asOf="2026-06-30")
    expected = {
        "date": "SELECT CAST('2026-06-30' AS DATE) AS \"d\"",
        "timestamp": "SELECT CAST('2026-06-30 00:00:00' AS TIMESTAMP) AS \"d\"",
    }[kind]
    assert checked.text == expected


def test_the_current_time_inside_a_view_needs_as_of_too() -> None:
    registry = _registry(
        "SELECT id FROM v",
        views=[{"name": "v", "sql": "SELECT id FROM accounts WHERE now() > now()"}],
    )
    with pytest.raises(FluxError) as caught:
        check_query(registry, registry.queries[0], SCHEMA)
    assert caught.value.code.code == "FLX-3629" and "view 'v'" in str(caught.value)


# ---------------------------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------------------------


def test_views_become_with_entries_in_dependency_order_before_the_querys_own() -> None:
    registry = _registry(
        "SELECT id FROM c",
        views=[
            {"name": "c", "sql": "SELECT id FROM b"},
            {"name": "b", "sql": "SELECT id FROM a"},
            {"name": "a", "sql": "SELECT id FROM accounts"},
        ],
        queries=[
            {
                "id": "q",
                "sql": "WITH own AS (SELECT id FROM c) SELECT own.id FROM own JOIN b ON b.id = own.id",
            }
        ],
    )
    checked = check_query(registry, registry.queries[0], SCHEMA)
    assert checked.views == ("a", "b", "c")
    assert checked.text.startswith('WITH "a" AS (SELECT')
    order = [checked.text.index(f'"{name}" AS (SELECT') for name in ("a", "b", "c", "own")]
    assert order == sorted(order)


def test_a_view_cycle_is_refused() -> None:
    for views in (
        [{"name": "a", "sql": "SELECT id FROM b"}, {"name": "b", "sql": "SELECT id FROM a"}],
        [{"name": "a", "sql": "SELECT id FROM a"}],
        [
            {"name": "a", "sql": "SELECT id FROM b"},
            {"name": "b", "sql": "SELECT id FROM c"},
            {"name": "c", "sql": "SELECT id FROM a"},
        ],
    ):
        registry = _registry("SELECT id FROM a", views=views)
        with pytest.raises(FluxError) as caught:
            check_query(registry, registry.queries[0], SCHEMA)
        assert caught.value.code.code == "FLX-3631"
        assert "'a'" in str(caught.value)
        with pytest.raises(FluxError) as caught:
            check_registry(registry, SCHEMA)
        assert caught.value.code.code == "FLX-3631"


def test_a_registry_is_checked_in_one_pass_and_each_view_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        "SELECT id FROM b",
        views=[
            {"name": "a", "sql": "SELECT id FROM accounts"},
            {"name": "b", "sql": "SELECT id FROM a"},
        ],
        queries=[
            {"id": "one", "sql": "SELECT id FROM b", "unit": "b"},
            {"id": "two", "sql": "SELECT id FROM a WHERE id > 1", "unit": "a"},
        ],
    )
    calls: List[str] = []
    original = _Checker._build

    def counting(self: Any, prepared: Any, order: Any) -> Any:
        calls.append(prepared.label)
        return original(self, prepared, order)

    monkeypatch.setattr(_Checker, "_build", counting)
    result = check_registry(registry, SCHEMA)
    assert [v.name for v in result.views] == ["a", "b"]
    assert [q.id for q in result.queries] == ["one", "two"]
    # a and b are built once each, and the queries use the views' cached results as their units
    assert calls == ["view 'a'", "view 'b'", "query 'one'", "query 'two'"]
    assert result.queries[0].unit_text == result.views[1].text


def test_a_view_reports_its_output_columns_and_what_it_reads() -> None:
    registry = _registry(
        "SELECT 1",
        views=[
            {
                "name": "v",
                "sql": "SELECT s.id, s.plan AS p, count(*) OVER () AS n FROM subscriptions s",
            }
        ],
    )
    view = check_registry(registry, SCHEMA).views[0]
    assert [name for name, _ in view.columns] == ["id", "p", "n"]
    assert view.exposes == ("subscriptions",) and view.views == ()
    assert view.digest == digest_of_text(view.text)
    assert dict(view.columns)["id"] == "BIGINT"


# ---------------------------------------------------------------------------------------------
# The unit and groupBy
# ---------------------------------------------------------------------------------------------


def test_an_expose_unit_is_select_star_over_it_and_a_view_unit_is_its_own_text() -> None:
    checked = check("SELECT id FROM subscriptions WHERE renewed", unit="subscriptions")
    assert checked.unit == "subscriptions"
    assert checked.unit_text == 'SELECT * FROM "subscriptions"'
    assert check("SELECT id FROM subscriptions").unit_text is None
    registry = _registry(
        "SELECT plan FROM v",
        views=[{"name": "v", "sql": "SELECT plan FROM subscriptions WHERE renewed"}],
        queries=[{"id": "q", "sql": "SELECT plan FROM v", "unit": "v"}],
    )
    result = check_registry(registry, SCHEMA)
    assert result.queries[0].unit_text == result.views[0].text


def test_group_by_is_a_column_of_the_rows_and_of_the_unit() -> None:
    sql = "SELECT plan, count(*) AS n FROM subscriptions GROUP BY plan"
    checked = check(sql, unit="subscriptions", groupBy="plan")
    assert checked.group_by == "plan"
    refused(sql, "FLX-3632", "not a column of the rows", unit="subscriptions", groupBy="region")
    refused(
        "SELECT plan, region FROM subscriptions",
        "FLX-3632",
        "not a column of its unit",
        unit="accounts",
        groupBy="plan",
    )
    star = check("SELECT * FROM subscriptions", unit="subscriptions", groupBy="region")
    assert star.group_by == "region"


def test_group_by_names_a_column_as_the_contract_does_in_an_expose_unit() -> None:
    ok = check(
        "SELECT sub_plan FROM subscriptions", columns=RENAME, unit="subscriptions", groupBy="plan"
    )
    assert ok.group_by == "plan"
    refused(
        "SELECT sub_plan FROM subscriptions",
        "FLX-3632",
        "not a column of the rows",
        columns=RENAME,
        unit="subscriptions",
        groupBy="sub_plan",
    )


def test_a_view_unit_names_its_columns_as_the_view_returns_them() -> None:
    registry = _registry(
        "SELECT sub_plan FROM v",
        columns=RENAME,
        views=[{"name": "v", "sql": "SELECT sub_plan, id FROM subscriptions"}],
        queries=[{"id": "q", "sql": "SELECT plan FROM v", "unit": "v", "groupBy": "plan"}],
    )
    assert check_registry(registry, SCHEMA).queries[0].group_by == "plan"


def test_a_unit_the_schema_lacks_is_refused() -> None:
    query = ConsumerQuery(id="q", sql="SELECT id FROM accounts", unit="nowhere")
    refused_query(query, "FLX-3632", "nowhere")


# ---------------------------------------------------------------------------------------------
# Total orders
# ---------------------------------------------------------------------------------------------

TOTAL = [
    "SELECT plan FROM subscriptions ORDER BY id LIMIT 3",
    "SELECT plan FROM subscriptions ORDER BY plan LIMIT 3",
    "SELECT plan FROM subscriptions ORDER BY id DESC LIMIT 3 OFFSET 2",
    "SELECT plan FROM subscriptions ORDER BY plan, id LIMIT 3",
    "SELECT plan FROM subscriptions ORDER BY 1, id LIMIT 3",
    "SELECT plan, region FROM subscriptions ORDER BY region, plan LIMIT 3",
    "SELECT DISTINCT plan FROM subscriptions ORDER BY plan LIMIT 3",
    "SELECT * FROM subscriptions ORDER BY id, plan, region, renewed, renewed_at, started, amount LIMIT 2",
    "SELECT s.plan, a.owner FROM subscriptions s JOIN accounts a ON a.id = s.id ORDER BY s.id, a.id LIMIT 3",
    "SELECT plan, count(*) AS n FROM subscriptions GROUP BY plan ORDER BY plan LIMIT 3",
    "SELECT plan, count(*) AS n FROM subscriptions GROUP BY plan ORDER BY n, plan LIMIT 3",
    "SELECT count(*) FROM subscriptions LIMIT 1",
    "SELECT max(id) AS m FROM subscriptions LIMIT 5 OFFSET 0",
    "SELECT 1 AS one LIMIT 1",
    "SELECT id FROM subscriptions UNION SELECT id FROM accounts ORDER BY id LIMIT 3",
    "SELECT * FROM (SELECT plan FROM subscriptions) AS d ORDER BY plan LIMIT 3",
    "WITH r AS (SELECT plan, id FROM subscriptions ORDER BY id LIMIT 4) SELECT plan FROM r",
    "SELECT row_number() OVER (ORDER BY id) AS rn FROM subscriptions",
    "SELECT row_number() OVER (PARTITION BY plan ORDER BY id) AS rn FROM subscriptions",
    "SELECT row_number() OVER (PARTITION BY id ORDER BY plan) AS rn FROM subscriptions",
    "SELECT dense_rank() OVER (PARTITION BY id ORDER BY lower(plan)) AS r FROM subscriptions",
    "SELECT rank() OVER (ORDER BY id DESC, plan) AS r FROM subscriptions",
    "SELECT sum(amount) OVER (PARTITION BY plan) AS t FROM subscriptions",
    "SELECT count(*) OVER () AS t FROM subscriptions",
    "SELECT plan, rank() OVER (ORDER BY plan) AS r FROM subscriptions GROUP BY plan",
    "SELECT row_number() OVER (ORDER BY plan, region, renewed, renewed_at, started, amount, id) AS rn FROM subscriptions",
    "SELECT row_number() OVER (ORDER BY a.id, s.id) AS rn FROM subscriptions s, accounts a",
]

NOT_TOTAL = [
    "SELECT plan FROM subscriptions LIMIT 3",
    "SELECT plan FROM subscriptions OFFSET 3",
    "SELECT plan FROM subscriptions ORDER BY region LIMIT 3",
    "SELECT plan, id FROM subscriptions ORDER BY plan LIMIT 3",
    "SELECT plan FROM subscriptions ORDER BY lower(plan), upper(plan) LIMIT 3",
    "SELECT plan FROM subscriptions ORDER BY id + 0 LIMIT 3",
    "SELECT s.plan FROM subscriptions s JOIN accounts a ON a.id = s.id ORDER BY s.id LIMIT 3",
    "SELECT s.plan FROM subscriptions s JOIN accounts a ON a.id = s.id ORDER BY a.id LIMIT 3",
    "SELECT plan, count(*) AS n FROM subscriptions GROUP BY plan ORDER BY n LIMIT 3",
    "SELECT plan, region, count(*) AS n FROM subscriptions GROUP BY plan, region ORDER BY plan LIMIT 3",
    "SELECT DISTINCT plan, region FROM subscriptions ORDER BY plan LIMIT 3",
    "SELECT id FROM subscriptions UNION SELECT id FROM accounts LIMIT 3",
    "SELECT id, 1 AS one FROM subscriptions UNION SELECT id, 2 FROM accounts ORDER BY id LIMIT 3",
    "SELECT * FROM (SELECT plan, id FROM subscriptions) AS d ORDER BY plan LIMIT 3",
    "WITH r AS (SELECT plan, region FROM subscriptions ORDER BY plan LIMIT 4) SELECT plan FROM r",
    "SELECT row_number() OVER () AS rn FROM subscriptions",
    "SELECT row_number() OVER (ORDER BY plan) AS rn FROM subscriptions",
    "SELECT row_number() OVER (PARTITION BY plan ORDER BY region) AS rn FROM subscriptions",
    "SELECT rank() OVER (ORDER BY amount) AS r FROM subscriptions",
    "SELECT dense_rank() OVER (PARTITION BY plan ORDER BY lower(plan)) AS r FROM subscriptions",
    "SELECT sum(amount) OVER (ORDER BY started) AS t FROM subscriptions",
    "SELECT sum(amount) OVER (PARTITION BY plan ORDER BY region) AS t FROM subscriptions",
    "SELECT row_number() OVER (ORDER BY s.id) AS rn FROM subscriptions s JOIN accounts a ON a.id = s.id",
    "SELECT row_number() OVER (ORDER BY id) AS rn FROM (SELECT id FROM subscriptions) AS d",
    "SELECT plan, rank() OVER (ORDER BY id) AS r FROM subscriptions GROUP BY plan",
]


@pytest.mark.parametrize("sql", TOTAL)
def test_an_order_that_is_total_is_accepted(sql: str) -> None:
    check(sql)


@pytest.mark.parametrize("sql", NOT_TOTAL)
def test_an_order_that_is_not_total_is_refused(sql: str) -> None:
    refused(sql, "FLX-3630")


def test_a_key_the_contract_declares_is_what_makes_an_order_total() -> None:
    sql = "SELECT plan, region FROM subscriptions ORDER BY plan LIMIT 3"
    keyless = bundle_schema(
        [
            ExposeSchema("subscriptions", SCHEMA.exposes[1].columns),
            SCHEMA.exposes[0],
        ]
    )
    with pytest.raises(FluxError) as caught:
        check_query(ConsumerRegistry(), ConsumerQuery(id="q", sql=sql), keyless)
    assert caught.value.code.code == "FLX-3630"
    unique_plan = bundle_schema(
        [ExposeSchema("subscriptions", SCHEMA.exposes[1].columns, unique=(("plan",),))]
    )
    check_query(ConsumerRegistry(), ConsumerQuery(id="q", sql=sql), unique_plan)
    composite = bundle_schema(
        [ExposeSchema("subscriptions", SCHEMA.exposes[1].columns, unique=(("plan", "region"),))]
    )
    check_query(
        ConsumerRegistry(),
        ConsumerQuery(id="q", sql="SELECT id FROM subscriptions ORDER BY region, plan LIMIT 2"),
        composite,
    )
    with pytest.raises(FluxError):
        check_query(ConsumerRegistry(), ConsumerQuery(id="q", sql=sql), composite)


def test_an_order_by_a_renamed_column_is_read_under_its_contract_name() -> None:
    check("SELECT id FROM subscriptions ORDER BY sub_plan, id LIMIT 2", columns=RENAME)
    unique_plan = bundle_schema(
        [ExposeSchema("subscriptions", SCHEMA.exposes[1].columns, unique=(("plan",),))]
    )
    registry = _registry(
        "SELECT id, region FROM subscriptions ORDER BY sub_plan LIMIT 2", columns=RENAME
    )
    check_query(registry, registry.queries[0], unique_plan)


def test_the_total_order_message_names_the_construct() -> None:
    refused("SELECT plan FROM subscriptions LIMIT 3", "FLX-3630", "LIMIT, and it has no ORDER BY")
    refused("SELECT plan FROM subscriptions OFFSET 3", "FLX-3630", "OFFSET")
    refused("SELECT row_number() OVER (ORDER BY plan) FROM subscriptions", "FLX-3630", "RowNumber")
    refused("SELECT row_number() OVER () FROM subscriptions", "FLX-3630", "no ORDER BY")


# ---------------------------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "logical,expected",
    [
        (LogicalType(LogicalKind.STRING), "VARCHAR"),
        (LogicalType(LogicalKind.INT), "BIGINT"),
        (LogicalType(LogicalKind.DECIMAL, 18, 4), "DECIMAL(18,4)"),
        (LogicalType(LogicalKind.DECIMAL, 38, 0), "DECIMAL(38,0)"),
        (LogicalType(LogicalKind.BOOL), "BOOLEAN"),
        (LogicalType(LogicalKind.DATE), "DATE"),
        (LogicalType(LogicalKind.TIMESTAMP), "TIMESTAMP"),
        (LogicalType(LogicalKind.JSON), "VARCHAR"),
        (LogicalType(LogicalKind.BYTES), "BLOB"),
    ],
)
def test_each_logical_type_has_one_duckdb_type(logical: LogicalType, expected: str) -> None:
    assert sql_type(logical) == expected


def test_the_type_map_agrees_with_the_arrow_layer() -> None:
    pa = pytest.importorskip("pyarrow")
    from flux_engine.sinks.arrow import _arrow_type

    expected = {
        "VARCHAR": pa.types.is_string,
        "BIGINT": pa.types.is_int64,
        "BOOLEAN": pa.types.is_boolean,
        "DATE": pa.types.is_date32,
        "TIMESTAMP": pa.types.is_timestamp,
        "BLOB": pa.types.is_binary,
    }
    for kind in LogicalKind:
        logical = LogicalType(kind, 18, 4) if kind is LogicalKind.DECIMAL else LogicalType(kind)
        arrow = _arrow_type("c", logical, False)
        duck = sql_type(logical)
        if kind is LogicalKind.DECIMAL:
            assert (
                pa.types.is_decimal(arrow) and duck == f"DECIMAL({arrow.precision},{arrow.scale})"
            )
        else:
            assert expected[duck](arrow), (kind, arrow, duck)


def test_a_decimal_wider_than_duckdbs_is_refused_by_name() -> None:
    with pytest.raises(FluxError) as caught:
        sql_type(LogicalType(LogicalKind.DECIMAL, 39, 2), where="column 'x'")
    assert caught.value.code.code == "FLX-3635" and "column 'x'" in str(caught.value)


def _contract_expose() -> Dict[str, Any]:
    return {
        "exposeId": "t",
        "contract": {
            "schema": [
                {"name": "id", "type": "string"},
                {"name": "a", "type": "integer"},
                {"name": "b", "type": "decimal(12,2)"},
                {"name": "c", "type": "date"},
            ]
        },
    }


def test_expose_schema_reads_a_compiled_expose_and_its_declared_keys() -> None:
    compiled = compile_expose(_contract_expose())
    plain = expose_schema(compiled)
    assert plain.columns == (
        ("id", "VARCHAR"),
        ("a", "BIGINT"),
        ("b", "DECIMAL(12,2)"),
        ("c", "DATE"),
    )
    assert plain.unique == () and plain.column_names == ("id", "a", "b", "c")
    keys = ExposeKeys(expose_id="t", keys=(("id",), ("a", "c")))
    both = expose_schema(compiled, keys, unique_columns=["b"])
    assert both.unique == (("a", "c"), ("b",), ("id",))


def test_expose_schema_refuses_a_key_the_expose_does_not_have() -> None:
    compiled = compile_expose(_contract_expose())
    for kwargs in (
        {"unique_columns": ["nope"]},
        {"keys": ExposeKeys(expose_id="t", keys=(("nope",),))},
    ):
        with pytest.raises(FluxError) as caught:
            expose_schema(compiled, **kwargs)
        assert caught.value.code.code == "FLX-3635"


def test_a_bundle_schema_lists_each_expose_once() -> None:
    one = ExposeSchema("a", (("x", "BIGINT"),))
    assert [e.expose_id for e in bundle_schema([ExposeSchema("b", ()), one]).exposes] == ["a", "b"]
    assert bundle_schema([one]).expose("a") is one and bundle_schema([one]).expose("z") is None
    with pytest.raises(FluxError) as caught:
        bundle_schema([one, one])
    assert caught.value.code.code == "FLX-3635"


# ---------------------------------------------------------------------------------------------
# The fixture bundle
# ---------------------------------------------------------------------------------------------

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "consumers" / "bundle"


def test_the_fixture_bundles_registry_checks_against_its_contract() -> None:
    simulation = yaml.safe_load((FIXTURE / "simulation.flux.yml").read_text(encoding="utf-8"))
    contract = yaml.safe_load((FIXTURE / "subscriptions.fluid.yaml").read_text(encoding="utf-8"))
    registry = declared_consumers(simulation, where="simulation")
    assert registry is not None
    compiled = compile_expose(find_expose(contract, "subscriptions"))
    keys = compile_keys(contract)
    assert keys is not None
    schema = bundle_schema([expose_schema(compiled, keys.expose("subscriptions"))])
    assert schema.exposes[0].unique == (("subscription_id",),)
    result = check_registry(registry, schema)
    offered = result.views[0]
    assert [name for name, _ in offered.columns][:3] == ["subscription_id", "plan", "region"]
    first, second, third = result.queries
    assert first.text.startswith('SELECT "subscriptions"."subscription_id" AS "subscription_id"')
    assert '"subscriptions"."plan" = \'premium\'' in first.text
    assert second.views == ("offered",) and second.unit_text == offered.text
    assert second.group_by == "plan"
    assert second.text.endswith(
        'SELECT "offered"."plan" AS "plan" FROM "offered" AS "offered" WHERE "offered"."renewed"'
    )
    assert third.text.endswith("\"renewed_at\" >= CAST('2025-03-01' AS DATE)")
    assert third.unit_text == 'SELECT * FROM "subscriptions"'
    assert [q.id for q in result.queries] == ["premium-renewed", "renewed-offered", "renewed-since"]


# ---------------------------------------------------------------------------------------------
# Projection aliases and the bounds that come before sqlglot's work
# ---------------------------------------------------------------------------------------------


def _doubling(steps: int) -> str:
    """A query whose every projection reads the one before it, twice: 2**steps nodes expanded."""
    projections = ["amount AS a0"] + [f"a{i - 1} + a{i - 1} AS a{i}" for i in range(1, steps + 1)]
    return f"SELECT {', '.join(projections)} FROM subscriptions WHERE a{steps} > 0"


def test_a_query_whose_aliases_double_its_tree_is_refused_before_qualify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qualified = _spy_on_qualify(monkeypatch)
    error = refused(_doubling(40), "FLX-3713", "'a0'")
    assert qualified == []
    assert "write the expression out" in str(error)


@pytest.mark.parametrize(
    "sql, alias",
    [
        ("SELECT amount AS a, a + a AS b FROM subscriptions", "a"),
        ("SELECT amount AS a FROM subscriptions WHERE a > 0", "a"),
        ("SELECT lower(plan) AS p, count(*) AS n FROM subscriptions GROUP BY p", "p"),
        (
            "SELECT plan, count(*) AS n FROM subscriptions GROUP BY plan HAVING n > 1",
            "n",
        ),
        ("SELECT plan, id AS k FROM subscriptions ORDER BY k + 1, id", "k"),
        ("SELECT * FROM (SELECT amount AS a, a * 2 AS b FROM subscriptions) AS d", "a"),
        ("WITH w AS (SELECT amount AS a FROM subscriptions WHERE a > 1) SELECT a FROM w", "a"),
    ],
)
def test_a_column_that_reads_a_projection_alias_of_its_own_select_is_refused(
    sql: str, alias: str
) -> None:
    refused(sql, "FLX-3713", f"'{alias}'")


def test_the_alias_rule_reads_a_view_a_query_reads_and_names_the_query() -> None:
    registry = _registry(
        "SELECT a FROM v",
        views=[{"name": "v", "sql": "SELECT amount AS a, a + a AS b FROM subscriptions"}],
    )
    with pytest.raises(FluxError) as caught:
        check_query(registry, registry.queries[0], SCHEMA)
    assert caught.value.code.code == "FLX-3713"
    assert "query 'q'" in str(caught.value)


@pytest.mark.parametrize(
    "sql",
    [
        # An alias that is only the column's own name changes nothing when read.
        "SELECT plan AS plan FROM subscriptions WHERE plan = 'a' GROUP BY plan",
        # A whole ORDER BY key that names an output column is read as written.
        "SELECT plan, count(*) AS n FROM subscriptions GROUP BY plan ORDER BY n DESC, plan",
        # The same alias in two different selects is no reading of one by the other.
        "SELECT * FROM (SELECT amount AS a FROM subscriptions) AS d WHERE d.a > 1",
        # A qualified column is the source's column, never an alias.
        "SELECT lower(s.plan) AS plan, s.region FROM subscriptions AS s WHERE s.plan = 'a'",
        # A projection after the alias's own, naming a source column that the alias shadows.
        "SELECT lower(plan) AS plan FROM subscriptions",
    ],
)
def test_a_use_of_a_name_that_is_no_alias_reading_is_still_accepted(sql: str) -> None:
    assert check(sql).text


def test_qualify_is_told_not_to_expand_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    from flux_engine.consumers import check as module

    seen: List[Any] = []
    real = module.qualify

    def spy(tree: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("expand_alias_refs"))
        return real(tree, **kwargs)

    monkeypatch.setattr(module, "qualify", spy)
    check("SELECT plan FROM subscriptions")
    assert seen == [False]


def test_a_like_pattern_of_512_characters_is_read_and_one_of_513_is_refused() -> None:
    assert check(f"SELECT id FROM subscriptions WHERE plan LIKE '{'a' * 512}'").text
    refused(f"SELECT id FROM subscriptions WHERE plan LIKE '{'a' * 513}'", "FLX-3714", "513")
    refused(f"SELECT id FROM subscriptions WHERE plan ILIKE '{'%' * 600}'", "FLX-3714", "600")


def test_a_long_pattern_bound_through_a_placeholder_is_refused_too() -> None:
    refused(
        "SELECT id FROM subscriptions WHERE plan LIKE :p",
        "FLX-3714",
        "513",
        parameters={"p": "a" * 513},
    )


# ---------------------------------------------------------------------------------------------
# Stars and positional GROUP BY keys, written out by qualify
# ---------------------------------------------------------------------------------------------

WIDE_COLUMNS = 200
WIDE = bundle_schema(
    [ExposeSchema("wide", tuple((f"c{i}", "BIGINT") for i in range(WIDE_COLUMNS)))]
)
#: A projection of 401 nodes: each positional key that names it copies all of them.
LONG = "coalesce(" + ", ".join(["c0"] * 400) + ")"


def _check_wide(sql: str) -> CheckedQuery:
    return check_query(ConsumerRegistry(), ConsumerQuery(id="q", sql=sql), WIDE)


def _stars(count: int, star: str = "*") -> str:
    return ", ".join([star] * count)


def _spy_on_qualify(monkeypatch: pytest.MonkeyPatch) -> List[exp.Expr]:
    """Record each tree ``check_query`` hands to sqlglot's ``qualify``, and run the real one."""
    from flux_engine.consumers import check as check_module

    seen: List[exp.Expr] = []
    real = check_module.qualify

    def spy(tree: exp.Expr, *args: Any, **kwargs: Any) -> exp.Expr:
        seen.append(tree)
        return real(tree, *args, **kwargs)

    monkeypatch.setattr(check_module, "qualify", spy)
    return seen


@pytest.mark.parametrize(
    "sql",
    [
        # 1000 stars of 200 columns: 25 s and 1.6 GB inside qualify before the bound.
        f"SELECT {_stars(1000)} FROM wide",
        # 1000 keys that each copy a projection of 401 nodes: 25 s and 1.4 GB before the bound.
        f"SELECT {LONG} AS a, count(*) AS n FROM wide GROUP BY {', '.join(['1'] * 1000)}",
        # A position counts the columns a star before it writes: 201 is the long projection.
        f"SELECT *, {LONG} AS a FROM wide GROUP BY {', '.join(['201'] * 300)}",
        # A star over a WITH entry, a subquery or a set operation reads the columns it returns.
        f"WITH w AS (SELECT * FROM wide) SELECT {_stars(300)} FROM w",
        f"SELECT {_stars(300)} FROM (SELECT * FROM wide) AS s",
        f"SELECT {_stars(300)} FROM (SELECT * FROM wide UNION ALL SELECT * FROM wide) AS u",
        # A qualified star, with its table named in another case than its alias.
        f"SELECT {_stars(300, 'W.*')} FROM wide AS w",
    ],
)
def test_stars_and_keys_that_write_out_past_the_canonical_cap_are_refused_before_qualify(
    sql: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    qualified = _spy_on_qualify(monkeypatch)
    with pytest.raises(FluxError) as caught:
        _check_wide(sql)
    assert qualified == []
    assert caught.value.code.code == "FLX-3627", str(caught.value)
    assert "query 'q' writes its stars and positional GROUP BY keys out" in str(caught.value)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM wide",
        "SELECT w.*, c1 FROM wide AS w",
        "SELECT c0 + c1 AS s, count(*) AS n FROM wide GROUP BY 1",
        "SELECT *, c0 + c1 AS s FROM wide GROUP BY 201",
    ],
)
def test_a_star_or_a_positional_key_within_the_cap_is_accepted(sql: str) -> None:
    assert _check_wide(sql).text


def test_a_projection_that_many_keys_name_is_counted_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flux_engine.consumers import _bounds

    counted: List[exp.Expr] = []
    real = _bounds._node_count

    def counting(node: exp.Expr) -> int:
        counted.append(node)
        return real(node)

    monkeypatch.setattr(_bounds, "_node_count", counting)
    sql = f"SELECT {LONG} AS a, count(*) AS n FROM wide GROUP BY {', '.join(['1'] * 1000)}"
    added = _bounds.expansion_nodes(
        sqlglot.parse_one(sql, dialect="duckdb"), {"wide": 200}, most=10**9
    )
    assert len(counted) == 1
    assert added >= 1000 * 400


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM wide",
        "SELECT *, * FROM wide",
        "SELECT w.*, c1 FROM wide AS w",
        "WITH x AS (SELECT * FROM wide) SELECT * FROM x",
        "SELECT c0 + c1 AS s, count(*) AS n FROM wide GROUP BY 1",
        "SELECT c0 + c1 AS s, c2 + c3 AS t FROM wide GROUP BY 0, 1",
        "SELECT *, c0 + c1 AS s FROM wide GROUP BY 201",
        "SELECT * FROM (SELECT * FROM wide UNION ALL SELECT * FROM wide) AS u",
    ],
)
def test_the_nodes_the_bound_counts_are_a_floor_of_what_qualify_adds(sql: str) -> None:
    from sqlglot.optimizer.qualify import qualify

    from flux_engine.consumers._bounds import expansion_nodes

    tree = sqlglot.parse_one(sql, dialect="duckdb")
    before = sum(1 for _ in tree.walk())
    counted = expansion_nodes(tree, {"wide": WIDE_COLUMNS}, most=10**9)
    columns = {f"c{i}": "BIGINT" for i in range(WIDE_COLUMNS)}
    qualified = qualify(
        tree.copy(), schema={"wide": columns}, dialect="duckdb", expand_alias_refs=False
    )
    assert 0 < counted <= sum(1 for _ in qualified.walk()) - before


# ---------------------------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------------------------

_IMPORT_PROBE = (
    "import sys, flux_engine, flux_engine.cli, flux_engine.consumers, "
    "flux_engine.consumers.errors, flux_engine.consumers.schema; "
    "print([m for m in ('sqlglot', 'duckdb', 'pyarrow') if m in sys.modules])"
)


def test_importing_the_package_loads_no_sql_library() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]", result.stderr


def test_the_error_catalog_loads_without_sqlglot() -> None:
    probe = (
        "import sys; from flux_engine.core.errors import ensure_core_codes_loaded, get; "
        "ensure_core_codes_loaded(); get('FLX-3618'); get('FLX-3635'); "
        "print('sqlglot' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False", result.stderr
