# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What consumer queries need the copy to hold (ADR 0028 section 5).

Pinned here: each predicate kind on tables, aliases, views, ``WITH`` entries and derived tables;
placeholders that are already bound; what is no requirement (``NOT``, functions, expressions, a
set operation whose branches read different columns); the LIKE matcher; the decisions ``init``
makes for each kind; the count texts a run is checked with; and that a requirement compiled under
another sqlglot version is ``versionsDiffer``. No DuckDB runs here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pytest

sqlglot = pytest.importorskip("sqlglot")

from flux_engine.consumers.check import SQLGLOT_VERSION, check_registry  # noqa: E402
from flux_engine.consumers.requirements import (  # noqa: E402
    MOST_PER_QUERY,
    MOST_PER_REGISTRY,
    ColumnFacts,
    Requirement,
    RequirementResult,
    assess_requirements,
    check_requirements,
    derive_requirements,
    like_matches,
    requirement_requests,
)
from flux_engine.consumers.runner import QueryCounts  # noqa: E402
from flux_engine.consumers.schema import ExposeSchema, bundle_schema  # noqa: E402
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.seam.consumers import read_consumers_block  # noqa: E402

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
                ("sku", "VARCHAR"),
                ("renewed", "BOOLEAN"),
                ("renewed_at", "DATE"),
                ("started", "TIMESTAMP"),
                ("amount", "DECIMAL(10,2)"),
                ("raw", "BLOB"),
            ),
            unique=(("id",),),
        ),
        ExposeSchema("accounts", (("id", "BIGINT"), ("owner", "VARCHAR")), unique=(("id",),)),
    ]
)


@pytest.fixture(autouse=True)
def _quiet_sqlglot() -> None:
    logging.getLogger("sqlglot").setLevel(logging.ERROR)


def derive(sql: str, **query: Any) -> List[Requirement]:
    registry = read_consumers_block(
        {"queries": [{"id": "q", "sql": sql, **query}]},
        emits=EMITS,
        where="consumers",
    )
    checked = check_registry(registry, SCHEMA)
    return list(derive_requirements(checked.queries, SCHEMA))


def one(sql: str, **query: Any) -> Requirement:
    found = derive(sql, **query)
    assert len(found) == 1, found
    return found[0]


def req(column: str, kind: str, *values: str, expose: str = "subscriptions") -> Requirement:
    return Requirement("q", expose, column, kind, tuple(values))


# ---------------------------------------------------------------------------
# One requirement per predicate
# ---------------------------------------------------------------------------


def test_equality_with_a_string_is_a_level() -> None:
    assert one("SELECT * FROM subscriptions WHERE plan = 'premium'") == req(
        "plan", "level", "premium"
    )


def test_a_literal_on_the_left_is_the_same_requirement() -> None:
    assert one("SELECT * FROM subscriptions WHERE 'premium' = plan") == req(
        "plan", "level", "premium"
    )


def test_in_gives_one_level_per_literal() -> None:
    found = derive("SELECT * FROM subscriptions WHERE plan IN ('basic', 'premium', 'basic')")
    assert found == [req("plan", "level", "basic"), req("plan", "level", "premium")]


def test_a_boolean_column_compared_with_a_boolean_is_a_level() -> None:
    assert one("SELECT * FROM subscriptions WHERE renewed = TRUE") == req(
        "renewed", "level", "true"
    )
    assert one("SELECT * FROM subscriptions WHERE renewed = FALSE") == req(
        "renewed", "level", "false"
    )


def test_equality_on_a_number_a_date_or_a_timestamp_is_a_value() -> None:
    assert one("SELECT * FROM subscriptions WHERE amount = 19.99") == req(
        "amount", "value", "19.99"
    )
    assert one("SELECT * FROM subscriptions WHERE amount = -5") == req("amount", "value", "-5")
    assert one("SELECT * FROM subscriptions WHERE renewed_at = DATE '9999-12-31'") == req(
        "renewed_at", "value", "9999-12-31"
    )
    assert one(
        "SELECT * FROM subscriptions WHERE started = TIMESTAMP '2026-01-02 03:04:05'"
    ) == req("started", "value", "2026-01-02 03:04:05")
    assert one("SELECT * FROM subscriptions WHERE id IN (7)") == req("id", "value", "7")


def test_like_and_ilike_are_patterns() -> None:
    assert one("SELECT * FROM subscriptions WHERE sku LIKE 'PRO-%'") == req(
        "sku", "pattern", "PRO-%", "like"
    )
    assert one("SELECT * FROM subscriptions WHERE sku ILIKE 'pro-_'") == req(
        "sku", "pattern", "pro-_", "ilike"
    )


def test_is_null_is_nulls_on_any_column() -> None:
    assert one("SELECT * FROM subscriptions WHERE renewed_at IS NULL") == req("renewed_at", "nulls")
    assert one("SELECT * FROM subscriptions WHERE plan IS NULL") == req("plan", "nulls")


def test_between_and_comparisons_are_ranges() -> None:
    assert one("SELECT * FROM subscriptions WHERE amount BETWEEN 5 AND 10") == req(
        "amount", "range", ">=5", "<=10"
    )
    assert one("SELECT * FROM subscriptions WHERE renewed_at >= DATE '2026-01-01'") == req(
        "renewed_at", "range", ">=2026-01-01", ""
    )
    assert one("SELECT * FROM subscriptions WHERE amount < 3") == req("amount", "range", "", "<3")
    assert one("SELECT * FROM subscriptions WHERE amount > 3") == req("amount", "range", ">3", "")
    assert one("SELECT * FROM subscriptions WHERE amount <= 3") == req("amount", "range", "", "<=3")


def test_a_literal_on_the_left_of_a_comparison_flips_it() -> None:
    assert one("SELECT * FROM subscriptions WHERE 3 < amount") == req("amount", "range", ">3", "")
    assert one("SELECT * FROM subscriptions WHERE 3 >= amount") == req("amount", "range", "", "<=3")


def test_two_bounds_give_two_requirements_and_a_repeat_gives_one() -> None:
    found = derive("SELECT * FROM subscriptions WHERE amount >= 5 AND amount < 10 AND amount >= 5")
    assert found == [req("amount", "range", "", "<10"), req("amount", "range", ">=5", "")]


# ---------------------------------------------------------------------------
# What is no requirement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "predicate",
    [
        "plan <> 'basic'",
        "NOT plan = 'basic'",
        "plan NOT IN ('basic')",
        "NOT (plan = 'basic' OR region = 'north')",
        "renewed_at IS NOT NULL",
        "lower(plan) = 'basic'",
        "plan || region = 'basic'",
        "plan = region",
        "plan = (SELECT 'basic')",
        "plan LIKE region",
        "plan IN (SELECT owner FROM accounts)",
        "renewed",
        "amount + 1 > 3",
        "plan > 'a'",
        "renewed = 1",
        "plan = 5",
    ],
)
def test_other_predicates_are_no_requirement(predicate: str) -> None:
    assert derive(f"SELECT * FROM subscriptions WHERE {predicate}") == []


def test_a_blob_column_gives_no_value_but_still_nulls() -> None:
    assert derive("SELECT * FROM subscriptions WHERE raw = 'x'") == []
    assert derive("SELECT * FROM subscriptions WHERE raw IS NULL") == [req("raw", "nulls")]


def test_not_covers_only_its_own_query() -> None:
    sql = (
        "SELECT * FROM subscriptions WHERE NOT EXISTS "
        "(SELECT 1 FROM accounts WHERE owner = 'x') AND plan = 'basic'"
    )
    assert derive(sql) == [
        req("owner", "level", "x", expose="accounts"),
        req("plan", "level", "basic"),
    ]


# ---------------------------------------------------------------------------
# Resolution through aliases, WITH entries, views and derived tables
# ---------------------------------------------------------------------------


def test_an_alias_resolves_to_its_table() -> None:
    assert one("SELECT s.id FROM subscriptions AS s WHERE s.plan = 'basic'") == req(
        "plan", "level", "basic"
    )


def test_a_join_reads_each_side_from_its_own_table() -> None:
    found = derive(
        "SELECT s.id FROM subscriptions s JOIN accounts a ON a.id = s.id "
        "WHERE s.plan = 'basic' AND a.owner = 'x'"
    )
    assert found == [
        req("owner", "level", "x", expose="accounts"),
        req("plan", "level", "basic"),
    ]


def test_a_with_entry_resolves_through_a_renaming_alias() -> None:
    found = derive(
        "WITH a AS (SELECT plan AS p, region FROM subscriptions WHERE region = 'west') "
        "SELECT * FROM a WHERE p = 'basic'"
    )
    assert found == [req("plan", "level", "basic"), req("region", "level", "west")]


def test_a_derived_table_resolves() -> None:
    found = derive("SELECT * FROM (SELECT plan AS kind FROM subscriptions) t WHERE kind = 'basic'")
    assert found == [req("plan", "level", "basic")]


def test_a_view_resolves_and_its_own_filter_counts() -> None:
    registry = read_consumers_block(
        {
            "views": [
                {"name": "offered", "sql": "SELECT * FROM subscriptions WHERE region = 'north'"}
            ],
            "queries": [{"id": "q", "sql": "SELECT plan FROM offered WHERE plan = 'basic'"}],
        },
        emits=EMITS,
        where="consumers",
    )
    found = derive_requirements(check_registry(registry, SCHEMA).queries, SCHEMA)
    assert list(found) == [req("plan", "level", "basic"), req("region", "level", "north")]


def test_a_set_operation_resolves_only_when_every_branch_reads_one_column() -> None:
    same = derive(
        "SELECT * FROM (SELECT plan FROM subscriptions UNION ALL SELECT plan FROM subscriptions) t "
        "WHERE plan = 'x'"
    )
    assert same == [req("plan", "level", "x")]
    different = derive(
        "SELECT * FROM (SELECT plan FROM subscriptions UNION ALL SELECT region FROM subscriptions) "
        "t WHERE plan = 'x'"
    )
    assert different == []


def test_a_column_made_by_an_expression_resolves_to_nothing() -> None:
    assert (
        derive("SELECT * FROM (SELECT lower(plan) AS p FROM subscriptions) t WHERE p = 'x'") == []
    )


def test_a_correlated_subquery_reads_the_outer_table() -> None:
    sql = (
        "SELECT * FROM subscriptions s WHERE EXISTS "
        "(SELECT 1 FROM accounts a WHERE a.id = s.id AND s.plan = 'basic')"
    )
    assert derive(sql) == [req("plan", "level", "basic")]


# ---------------------------------------------------------------------------
# The tree is the bound one, and the names are the contract's
# ---------------------------------------------------------------------------


def test_a_placeholder_already_bound_is_read_as_its_value() -> None:
    found = derive(
        "SELECT * FROM subscriptions WHERE plan = :tier AND renewed_at >= :since",
        parameters={"tier": "premium", "since": "2026-01-01"},
    )
    assert found == [
        req("plan", "level", "premium"),
        req("renewed_at", "range", ">=2026-01-01", ""),
    ]


def test_a_written_column_name_is_read_as_the_contracts() -> None:
    registry = read_consumers_block(
        {
            "dialect": "snowflake",
            "tables": {"prod.analytics.subs": "subscriptions"},
            "columns": {"subscriptions": {"sub_plan": "plan"}},
            "queries": [
                {"id": "q", "sql": "SELECT * FROM prod.analytics.subs WHERE sub_plan = 'a'"}
            ],
        },
        emits=EMITS,
        where="consumers",
    )
    found = derive_requirements(check_registry(registry, SCHEMA).queries, SCHEMA)
    assert list(found) == [req("plan", "level", "a")]


def test_every_query_is_read_and_a_held_out_one_counts() -> None:
    registry = read_consumers_block(
        {
            "queries": [
                {"id": "b", "sql": "SELECT * FROM subscriptions WHERE plan = 'x'", "heldOut": True},
                {"id": "a", "sql": "SELECT * FROM subscriptions WHERE plan = 'y'"},
            ]
        },
        emits=EMITS,
        where="consumers",
    )
    found = derive_requirements(check_registry(registry, SCHEMA).queries, SCHEMA)
    assert [(item.query, item.values) for item in found] == [("b", ("x",)), ("a", ("y",))]


# ---------------------------------------------------------------------------
# LIKE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "text", "expected"),
    [
        ("PRO-%", "PRO-1", True),
        ("PRO-%", "PRO-", True),
        ("PRO-%", "pro-1", False),
        ("%-1", "PRO-1", True),
        ("P_O", "PRO", True),
        ("P_O", "PO", False),
        ("%", "", True),
        ("_", "", False),
        ("a%b%c", "aXXbYYc", True),
        ("a%b%c", "aXXcYYb", False),
        ("abc", "abc", True),
        ("abc", "abcd", False),
        ("", "", True),
        ("", "a", False),
    ],
)
def test_like_matches(pattern: str, text: str, expected: bool) -> None:
    assert like_matches(pattern, text) is expected


def test_ilike_ignores_case() -> None:
    assert like_matches("pro-%", "PRO-1", ignore_case=True)
    assert not like_matches("pro-%", "PRO-1")


def test_an_escape_makes_the_next_character_literal() -> None:
    assert like_matches("50!%", "50%", escape="!")
    assert not like_matches("50!%", "500", escape="!")
    assert like_matches("a!_b", "a_b", escape="!")
    assert not like_matches("a!_b", "aXb", escape="!")
    assert like_matches("!!", "!", escape="!")
    assert like_matches("a!", "a!", escape="!")  # a trailing escape is itself


def test_many_wildcards_do_not_take_exponential_time() -> None:
    assert not like_matches("%a" * 40 + "b", "a" * 200)


# ---------------------------------------------------------------------------
# Deciding at init
# ---------------------------------------------------------------------------

KEY = ("subscriptions", "plan")


def decide(
    requirement: Requirement,
    facts: ColumnFacts,
    *,
    sentinels: Optional[Dict[Tuple[str, str], Tuple[str, ...]]] = None,
    window: Optional[Tuple[str, str]] = None,
) -> Tuple[str, str]:
    (assessment,) = assess_requirements(
        [requirement],
        {(requirement.expose, requirement.column): facts},
        sentinels=sentinels,
        window=window,
    )
    return assessment.status, assessment.note


def test_a_declared_level_is_met_and_an_undeclared_one_is_not() -> None:
    facts = ColumnFacts(levels=("basic", "premium"))
    assert decide(req("plan", "level", "premium"), facts)[0] == "met"
    status, note = decide(req("plan", "level", "gold"), facts)
    assert status == "unmet" and "do not include it" in note


def test_a_level_outside_the_contracts_enum_is_unmet_whatever_is_declared() -> None:
    facts = ColumnFacts(levels=("basic", "gold"), enum=("basic", "premium"))
    status, note = decide(req("plan", "level", "gold"), facts)
    assert status == "unmet" and "enum" in note
    assert (
        decide(
            req("plan", "level", "premium"),
            ColumnFacts(levels=("premium",), enum=("basic", "premium")),
        )[0]
        == "met"
    )


def test_a_level_on_a_column_without_named_levels_is_unmet() -> None:
    assert decide(req("sku", "level", "x"), ColumnFacts(levels=None))[0] == "unmet"


def test_a_pattern_is_met_by_a_matching_level() -> None:
    facts = ColumnFacts(levels=("PRO-1", "FREE"))
    assert decide(req("sku", "pattern", "PRO-%", "like"), facts)[0] == "met"
    assert decide(req("sku", "pattern", "pro-%", "like"), facts)[0] == "unmet"
    assert decide(req("sku", "pattern", "pro-%", "ilike"), facts)[0] == "met"
    assert decide(req("sku", "pattern", "X%", "ilike"), facts)[0] == "unmet"


def test_a_pattern_on_an_identifier_or_text_column_is_left_to_verify() -> None:
    status, note = decide(req("sku", "pattern", "PRO-%", "like"), ColumnFacts(levels=None))
    assert status == "verify" and "checked on a run" in note


def test_nulls_are_met_when_the_column_draws_them() -> None:
    assert decide(req("plan", "nulls"), ColumnFacts(draws_nulls=True))[0] == "met"
    assert decide(req("plan", "nulls"), ColumnFacts(draws_nulls=False))[0] == "unmet"


def test_a_value_is_met_by_a_link_sentinel_or_a_declared_value() -> None:
    wanted = req("renewed_at", "value", "9999-12-31")
    facts = ColumnFacts(kind="date")
    assert decide(wanted, facts)[0] == "unmet"
    status, note = decide(
        wanted, facts, sentinels={("subscriptions", "renewed_at"): ("9999-12-31",)}
    )
    assert status == "met" and "sentinel" in note
    assert decide(wanted, ColumnFacts(kind="date", values=("9999-12-31",)))[0] == "met"


def test_an_unmet_value_names_what_would_produce_it() -> None:
    status, note = decide(req("amount", "value", "0"), ColumnFacts(kind="number"))
    assert status == "unmet" and "links[].sentinels" in note


def test_a_value_compares_numbers_and_dates_by_what_they_are() -> None:
    assert (
        decide(req("amount", "value", "5"), ColumnFacts(kind="number", values=("5.00",)))[0]
        == "met"
    )
    assert (
        decide(req("amount", "value", "5.5"), ColumnFacts(kind="number", values=("5.00",)))[0]
        == "unmet"
    )
    assert (
        decide(
            req("started", "value", "2026-01-01 00:00:00"),
            ColumnFacts(kind="timestamp", values=("2026-01-01T00:00:00Z",)),
        )[0]
        == "met"
    )


def test_a_range_is_met_by_the_declared_range_or_the_time_window() -> None:
    wide = ColumnFacts(kind="number", range=("0", "100"))
    assert decide(req("amount", "range", ">=5", "<=10"), wide)[0] == "met"
    assert decide(req("amount", "range", ">100", ""), wide)[0] == "unmet"
    assert decide(req("amount", "range", ">=100", ""), wide)[0] == "met"
    assert decide(req("amount", "range", "", "<0"), wide)[0] == "unmet"
    assert decide(req("amount", "range", "", "<=0"), wide)[0] == "met"
    assert decide(req("amount", "range", ">=5", "<=10"), ColumnFacts(kind="number"))[0] == "unmet"
    window = ("2025-01-01T00:00:00Z", "2025-07-01T00:00:00Z")
    dated = ColumnFacts(kind="date")
    status, note = decide(req("renewed_at", "range", ">=2025-03-01", ""), dated, window=window)
    assert status == "met" and "time window" in note
    assert (
        decide(req("renewed_at", "range", ">=2026-01-01", ""), dated, window=window)[0] == "unmet"
    )
    own = ColumnFacts(kind="date", range=("2026-01-01", "2026-12-31"))
    assert decide(req("renewed_at", "range", ">=2026-06-01", ""), own, window=window)[0] == "met"


def test_a_column_the_bundle_does_not_draw_is_unmet() -> None:
    (assessment,) = assess_requirements([req("plan", "level", "x")], {})
    assert assessment.status == "unmet" and "no declaration" in assessment.note


# ---------------------------------------------------------------------------
# Checking on a run
# ---------------------------------------------------------------------------


def test_the_count_texts_are_pinned() -> None:
    requirements = [
        req("plan", "level", "premium"),
        req("renewed", "level", "true"),
        req("renewed_at", "value", "9999-12-31"),
        req("amount", "value", "-5.5"),
        req("sku", "pattern", "PRO-%", "like"),
        req("sku", "pattern", "pro-_", "ilike"),
        req("renewed_at", "nulls"),
        req("amount", "range", ">=5", "<10"),
        req("renewed_at", "range", ">=2026-01-01", ""),
    ]
    requests = requirement_requests(requirements, SCHEMA)
    assert [item.id for item in requests] == [f"requirement-{n}" for n in range(1, 10)]
    assert [item.text for item in requests] == [
        'SELECT * FROM "subscriptions" WHERE "plan" = \'premium\'',
        'SELECT * FROM "subscriptions" WHERE "renewed" = TRUE',
        'SELECT * FROM "subscriptions" WHERE "renewed_at" = CAST(\'9999-12-31\' AS DATE)',
        'SELECT * FROM "subscriptions" WHERE "amount" = -5.5',
        'SELECT * FROM "subscriptions" WHERE "sku" LIKE \'PRO-%\'',
        'SELECT * FROM "subscriptions" WHERE "sku" ILIKE \'pro-_\'',
        'SELECT * FROM "subscriptions" WHERE "renewed_at" IS NULL',
        'SELECT * FROM "subscriptions" WHERE "amount" >= 5 AND "amount" < 10',
        'SELECT * FROM "subscriptions" WHERE "renewed_at" >= CAST(\'2026-01-01\' AS DATE)',
    ]
    assert {item.unit_text for item in requests} == {'SELECT * FROM "subscriptions"'}
    assert {item.sqlglot_version for item in requests} == {SQLGLOT_VERSION}


def test_a_literal_is_written_as_a_literal_never_as_sql() -> None:
    hostile = "x' OR 1=1; DROP TABLE subscriptions; --"
    (request,) = requirement_requests([req("plan", "level", hostile)], SCHEMA)
    assert (
        request.text
        == "SELECT * FROM \"subscriptions\" WHERE \"plan\" = 'x'' OR 1=1; DROP TABLE subscriptions; --'"
    )
    from flux_engine.consumers.check import _parse

    tree = _parse(request.text, "duckdb", "probe")
    assert type(tree).__name__ == "Select"


def _in_list(first: int, count: int) -> str:
    return ", ".join(f"'level-{first + each}'" for each in range(count))


def test_the_bounds_are_256_a_query_and_10000_a_registry() -> None:
    assert (MOST_PER_QUERY, MOST_PER_REGISTRY) == (256, 10_000)


def test_a_query_of_256_requirements_is_read_and_one_of_257_is_refused() -> None:
    assert len(derive(f"SELECT * FROM subscriptions WHERE plan IN ({_in_list(0, 256)})")) == 256
    with pytest.raises(FluxError) as caught:
        derive(f"SELECT * FROM subscriptions WHERE plan IN ({_in_list(0, 257)})")
    assert caught.value.code.code == "FLX-3715"
    assert "query 'q'" in str(caught.value) and "256" in str(caught.value)


def test_an_in_list_of_300_literals_is_refused_before_any_count_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flux_engine.consumers import requirements as module

    built: List[Any] = []
    monkeypatch.setattr(module, "_text_of", lambda *args: built.append(args) or ("", ""))
    with pytest.raises(FluxError) as caught:
        requirement_requests(
            derive_requirements(
                check_registry(
                    read_consumers_block(
                        {
                            "queries": [
                                {
                                    "id": "wide",
                                    "sql": f"SELECT * FROM subscriptions WHERE plan IN ({_in_list(0, 300)})",
                                }
                            ]
                        },
                        emits=EMITS,
                        where="consumers",
                    ),
                    SCHEMA,
                ).queries,
                SCHEMA,
            ),
            SCHEMA,
        )
    assert caught.value.code.code == "FLX-3715" and "'wide'" in str(caught.value)
    assert built == []


def test_a_registry_past_10000_requirements_is_refused_naming_the_query_that_passes_it() -> None:
    # 41 queries of 250 distinct literals each: every query is within its bound, the registry is not.
    queries = [
        {
            "id": f"q{number:02d}",
            "sql": f"SELECT * FROM subscriptions WHERE plan IN ({_in_list(number * 250, 250)})",
        }
        for number in range(41)
    ]
    registry = read_consumers_block({"queries": queries}, emits=EMITS, where="consumers")
    checked = check_registry(registry, SCHEMA)
    assert len(derive_requirements(checked.queries[:40], SCHEMA)) == 10_000
    with pytest.raises(FluxError) as caught:
        derive_requirements(checked.queries, SCHEMA)
    assert caught.value.code.code == "FLX-3715"
    assert "'q40'" in str(caught.value) and "10000" in str(caught.value)


def test_a_range_bound_is_written_as_a_literal_in_its_description() -> None:
    hostile = req("amount", "range", ">=5\r\x1b[2J", "")
    assert hostile.describe() == "the range '>=5\\r\\x1b[2J'"
    assert "\r" not in hostile.describe() and "\x1b" not in hostile.describe()
    assert req("amount", "range", ">=5", "<10").describe() == "the range '>=5' and '<10'"
    assert req("renewed_at", "range", ">=2026-01-01", "").describe() == "the range '>=2026-01-01'"


def counts(*rows: Optional[int], status: str = "counted") -> List[QueryCounts]:
    return [
        QueryCounts(
            id=f"requirement-{index}",
            status=status,
            reason=None,
            rows=value,
            unit_rows=10,
            row_groups=None,
            unit_groups=None,
            duckdb_version="2.0.1",
            sqlglot_version=SQLGLOT_VERSION,
        )
        for index, value in enumerate(rows, start=1)
    ]


def test_a_requirement_is_met_when_a_row_meets_it() -> None:
    wanted = [req("plan", "level", "a"), req("plan", "level", "b")]
    result = check_requirements(wanted, counts(3, 0))
    assert result == (
        RequirementResult(wanted[0], "met", 3),
        RequirementResult(wanted[1], "unmet", 0),
    )


def test_a_count_that_did_not_finish_keeps_its_status() -> None:
    wanted = [req("plan", "level", "a")]
    (result,) = check_requirements(wanted, counts(None, status="timedOut"))
    assert result.status == "timedOut" and result.rows is None


def test_a_requirement_compiled_under_another_version_is_versions_differ() -> None:
    wanted = [req("plan", "level", "a"), req("plan", "level", "b")]
    result = check_requirements(wanted, recorded_sqlglot="1.0.0", current_sqlglot="2.0.0")
    assert [item.status for item in result] == ["versionsDiffer", "versionsDiffer"]
    # Whatever the counts say, and with no count needed at all.
    again = check_requirements(
        wanted, counts(5, 5), recorded_sqlglot="1.0.0", current_sqlglot="2.0.0"
    )
    assert [item.status for item in again] == ["versionsDiffer", "versionsDiffer"]


def test_the_same_version_or_none_recorded_is_compared() -> None:
    wanted = [req("plan", "level", "a")]
    assert (
        check_requirements(wanted, counts(1), recorded_sqlglot=SQLGLOT_VERSION)[0].status == "met"
    )
    assert check_requirements(wanted, counts(1))[0].status == "met"


def test_counts_must_line_up_with_the_requirements() -> None:
    with pytest.raises(ValueError):
        check_requirements([req("plan", "level", "a")], counts(1, 2))
    with pytest.raises(ValueError):
        check_requirements([req("plan", "level", "a")])
