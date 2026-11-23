# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The profile request is read exactly, within bounds, and refused whole when it is wrong."""

from __future__ import annotations

import pytest

pytest.importorskip("yaml")

from flux_stats.errors import FluxError  # noqa: E402
from flux_stats.request import (  # noqa: E402
    MOST_FACTS,
    REQUEST_SCHEMA_URL,
    ProfileRequest,
    RequestColumn,
    RequestEnd,
    RequestFact,
    RequestRelationship,
    RequestTable,
    load_request_text,
    parse_request,
    render_request,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "ab" * 32


def _request() -> ProfileRequest:
    return ProfileRequest(
        contract_digest=DIGEST,
        fractional_bits=16,
        tables=(
            RequestTable(
                expose_id="customers",
                row_count=True,
                columns=(
                    RequestColumn("id", "number", ("min", "max"), ("mean/1",)),
                    RequestColumn("segment", "category", ("nullCount",), ("levelHistogram/1",)),
                ),
            ),
            RequestTable(
                expose_id="orders",
                row_count=True,
                columns=(RequestColumn("customer_id", "number", (), ()),),
            ),
        ),
        relationships=(
            RequestRelationship(
                parent=RequestEnd("customers", ("id",)),
                child=RequestEnd("orders", ("customer_id",)),
                statistics=("mean/1", "marginalHistogram/1"),
            ),
        ),
        facts=(RequestFact("orders.paid_after_placed", "orders", "paid_after_placed"),),
    )


def _head(extra: str = "") -> str:
    return (
        f'_schemaURL: "{REQUEST_SCHEMA_URL}"\n'
        "tables:\n"
        "  t:\n"
        "    columns:\n"
        "      a: {domain: number}\n" + extra
    )


def _refusal(text: str) -> str:
    with pytest.raises(FluxError) as refused:
        load_request_text(text)
    return f"{refused.value.code.code} {refused.value}"


def test_a_request_is_written_and_read_back_equal() -> None:
    request = _request()
    text = render_request(request)
    assert load_request_text(text) == request
    assert render_request(load_request_text(text)) == text


def test_the_same_request_gives_the_same_bytes() -> None:
    assert render_request(_request()) == render_request(_request())


def test_a_minimal_request_reads() -> None:
    request = load_request_text(_head())
    assert request.tables[0].columns[0].domain == "number"
    assert request.fractional_bits == 16 and request.facts == ()


def test_an_unknown_key_is_refused_by_where_it_sits() -> None:
    message = _refusal(_head("      b: {domain: number, extra: 1}\n"))
    assert message.startswith("FLX-5038") and "request.tables.t.columns.b" in message
    assert "extra" in message


def test_an_unknown_top_level_key_is_refused() -> None:
    assert "unknown key" in _refusal(_head() + "profile: {}\n")


def test_a_wrong_schema_url_is_refused() -> None:
    assert "FLX-5038" in _refusal(_head().replace("1-0-0", "9-0-0"))


def test_a_statistic_that_does_not_read_the_domain_is_refused() -> None:
    text = _head("      c: {domain: category, statistics: [mean/1]}\n")
    assert "does not read a category column" in _refusal(text)


def test_an_unregistered_statistic_and_metric_are_refused() -> None:
    assert "no registered statistic" in _refusal(
        _head("      c: {domain: number, statistics: [median/9]}\n")
    )
    assert "no registered column metric" in _refusal(
        _head("      c: {domain: number, metrics: [median]}\n")
    )


def test_a_float_is_refused() -> None:
    text = _head("encoding: {fractionalBits: 1.5, rounding: floor}\n")
    assert "FLX-5038" in _refusal(text)


def test_a_timestamp_is_not_built() -> None:
    text = _head("facts: []\ncontract: {digest: 2026-01-01}\n")
    assert "FLX-5038" in _refusal(text)


def test_two_documents_are_refused() -> None:
    assert "exactly one" in _refusal(_head() + "---\n" + _head())


def test_an_alias_is_not_read_as_a_way_to_grow_a_request() -> None:
    text = _head("      b: &b {domain: number}\n      c: *b\n")
    # Aliases that stay inside the bounds are read; the bound is on what they write out.
    assert load_request_text(text).tables[0].columns[2].name == "c"
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
        f"b{i}: &b{i} [*{'a' if i == 0 else 'b' + str(i - 1)}, *{'a' if i == 0 else 'b' + str(i - 1)}, "
        f"*{'a' if i == 0 else 'b' + str(i - 1)}, *{'a' if i == 0 else 'b' + str(i - 1)}, "
        f"*{'a' if i == 0 else 'b' + str(i - 1)}, *{'a' if i == 0 else 'b' + str(i - 1)}, "
        f"*{'a' if i == 0 else 'b' + str(i - 1)}, *{'a' if i == 0 else 'b' + str(i - 1)}, "
        f"*{'a' if i == 0 else 'b' + str(i - 1)}, *{'a' if i == 0 else 'b' + str(i - 1)}]\n"
        for i in range(12)
    )
    assert "FLX-3105" in _refusal(bomb)


def test_a_name_with_a_control_or_a_line_separator_is_refused() -> None:
    for bad in ("a\\u2028b", "a\\u0007b", "a\\u200bb"):
        assert "FLX-5038" in _refusal(_head(f'      "{bad}": {{domain: number}}\n'))


def test_a_name_not_in_nfc_is_refused() -> None:
    assert "Normalization Form C" in _refusal(_head('      "e\\u0301": {domain: number}\n'))


def test_a_relationship_must_name_a_table_and_columns_of_the_request() -> None:
    base = _head()
    wrong_table = base + (
        "relationships:\n"
        "  - parent: {table: nope, columns: [a]}\n"
        "    child: {table: t, columns: [a]}\n"
        "    statistics: [mean/1]\n"
    )
    assert "names no table" in _refusal(wrong_table)
    wrong_column = base + (
        "relationships:\n"
        "  - parent: {table: t, columns: [zz]}\n"
        "    child: {table: t, columns: [a]}\n"
        "    statistics: [mean/1]\n"
    )
    assert "names columns of its table" in _refusal(wrong_column)


def test_a_list_past_its_bound_is_refused() -> None:
    facts = "".join(f"  - {{name: f{i}, table: t, rule: r}}\n" for i in range(MOST_FACTS + 1))
    assert "more than" in _refusal(_head("facts:\n" + facts))


def test_a_fact_name_is_used_once() -> None:
    text = _head("facts:\n  - {name: f, table: t, rule: r}\n  - {name: f, table: t, rule: s}\n")
    assert "names one entry twice" in _refusal(text)


def test_parse_request_refuses_a_document_that_is_not_a_mapping() -> None:
    with pytest.raises(FluxError):
        parse_request(["tables"])


def test_the_refusal_never_quotes_a_value() -> None:
    message = _refusal(_head("      c: {domain: number, statistics: [secret-value-9/9]}\n"))
    assert "secret-value" not in message


def test_check_request_takes_what_the_reader_takes() -> None:
    from flux_stats.request import check_request

    check_request(_request())
    assert load_request_text(render_request(_request())) == _request()


@pytest.mark.parametrize(
    ("name", "where"),
    [
        ("first name", "column 'first\\xa0name'"),
        ("a b", "column 'a\\u2028b'"),
        ("a\u0007b", "column 'a\\x07b'"),
        ("x" * 513, "column 'x"),
    ],
)
def test_check_request_names_the_column_the_reader_would_refuse(name: str, where: str) -> None:
    from dataclasses import replace

    from flux_stats.request import check_request

    request = _request()
    table = request.tables[0]
    bad = replace(table, columns=table.columns + (RequestColumn(name, "number"),))
    with pytest.raises(FluxError) as refused:
        check_request(replace(request, tables=(bad,) + request.tables[1:]))
    assert refused.value.code.code == "FLX-5038"
    assert "customers" in str(refused.value) and where in str(refused.value)
    # The reader refuses the written text for the same name.
    with pytest.raises(FluxError):
        load_request_text(render_request(replace(request, tables=(bad,) + request.tables[1:])))


def test_check_request_names_a_fact_the_reader_would_refuse() -> None:
    from dataclasses import replace

    from flux_stats.request import check_request

    request = _request()
    twice = replace(request, facts=request.facts + request.facts)
    with pytest.raises(FluxError) as refused:
        check_request(twice)
    assert "fact 'orders.paid_after_placed'" in str(refused.value)
    unnamed = replace(request, facts=(RequestFact("bad name", "orders", "r"),))
    with pytest.raises(FluxError) as refused:
        check_request(unnamed)
    assert "fact 'bad\\u2029name'" in str(refused.value)
    columns_twice = replace(
        request.tables[1], columns=request.tables[1].columns + request.tables[1].columns
    )
    with pytest.raises(FluxError) as refused:
        check_request(replace(request, tables=(request.tables[0], columns_twice)))
    assert "column 'customer_id'" in str(refused.value)


def test_a_request_is_written_in_the_order_the_reader_gives_back() -> None:
    from dataclasses import replace

    request = _request()
    backwards = replace(
        request,
        tables=tuple(
            replace(table, columns=tuple(reversed(table.columns)))
            for table in reversed(request.tables)
        ),
    )
    assert render_request(backwards) == render_request(request)
    assert load_request_text(render_request(backwards)) == request


def test_check_request_refuses_a_relationship_end_the_reader_would_refuse() -> None:
    from dataclasses import replace

    from flux_stats.request import check_request

    request = _request()
    relationship = request.relationships[0]
    stray = replace(relationship, parent=replace(relationship.parent, table="zz"))
    with pytest.raises(FluxError) as refused:
        check_request(replace(request, relationships=(stray,) + request.relationships[1:]))
    assert refused.value.code.code == "FLX-5038"
    assert "relationships" in str(refused.value)


# ---------------------------------------------------------------------------
# Format 1-1-0: the consumer additions (queries and a column workload)
# ---------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402

from flux_stats.request import (  # noqa: E402
    MOST_QUERIES,
    MOST_QUERY_BYTES,
    MOST_WORKLOAD_SETS,
    REQUEST_SCHEMA_URL_1_1_0,
    RequestQuery,
    RequestUnit,
    RequestWorkload,
    check_request,
    request_document,
)


def _consumer_request() -> ProfileRequest:
    base = _request()
    customers = replace(
        base.tables[0],
        columns=tuple(
            sorted(
                base.tables[0].columns
                + (
                    RequestColumn("plan", "category", (), ("levelHistogram/1",)),
                    RequestColumn("region", "category", (), ()),
                ),
                key=lambda column: column.name,
            )
        ),
        workload=(
            RequestWorkload(("plan",), 2),
            RequestWorkload(("plan", "region"), 1),
            RequestWorkload(("region",), 1000000),
        ),
    )
    return replace(
        base,
        tables=(customers,) + base.tables[1:],
        queries=(
            RequestQuery("premium", "SELECT 1\n\tFROM customers", RequestUnit(table="customers")),
            RequestQuery(
                "by-plan",
                "SELECT plan FROM customers",
                RequestUnit(sql="SELECT * FROM customers"),
                group_by="plan",
            ),
        ),
    )


def _with_additions(extra: str, url: str = REQUEST_SCHEMA_URL_1_1_0) -> str:
    return (
        f'_schemaURL: "{url}"\n'
        "tables:\n"
        "  t:\n"
        "    columns:\n"
        "      a: {domain: number}\n"
        "      c: {domain: category}\n"
        "      d: {domain: category}\n" + extra
    )


def test_the_constant_names_the_published_1_1_0_url() -> None:
    assert REQUEST_SCHEMA_URL_1_1_0 == (
        "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-1-0/FluxProfileRequest.json"
    )


def test_queries_and_a_workload_are_written_and_read_back_equal() -> None:
    request = _consumer_request()
    check_request(request)
    text = render_request(request)
    assert REQUEST_SCHEMA_URL_1_1_0 in text and REQUEST_SCHEMA_URL not in text
    assert load_request_text(text) == request
    assert render_request(load_request_text(text)) == text


def test_a_request_without_an_addition_is_still_1_0_0_and_keeps_its_text() -> None:
    text = render_request(_request())
    assert REQUEST_SCHEMA_URL in text and "1-1-0" not in text
    assert "queries" not in text and "workload" not in text
    assert request_document(_request())["_schemaURL"] == REQUEST_SCHEMA_URL


def test_either_addition_alone_makes_the_request_1_1_0() -> None:
    request = _consumer_request()
    only_queries = replace(
        request, tables=tuple(replace(table, workload=()) for table in request.tables)
    )
    only_workload = replace(request, queries=())
    assert request_document(only_queries)["_schemaURL"] == REQUEST_SCHEMA_URL_1_1_0
    assert request_document(only_workload)["_schemaURL"] == REQUEST_SCHEMA_URL_1_1_0
    assert load_request_text(render_request(only_workload)) == only_workload


def test_the_reader_reads_both_formats_and_no_other() -> None:
    assert load_request_text(_head()).tables[0].workload == ()
    assert load_request_text(_with_additions("")).queries == ()
    assert "FLX-5038" in _refusal(
        _with_additions("", url=REQUEST_SCHEMA_URL_1_1_0.replace("1-1-0", "1-2-0"))
    )


def test_a_held_out_request_lists_tables_with_no_column() -> None:
    held_out = ProfileRequest(
        contract_digest=DIGEST,
        fractional_bits=16,
        tables=(RequestTable("customers", False, ()),),
        queries=(RequestQuery("later", "SELECT 1", RequestUnit(table="customers")),),
    )
    check_request(held_out)
    text = render_request(held_out)
    assert "columns: {}" in text.replace('"', "")
    assert load_request_text(text) == held_out


def test_1_0_0_that_holds_either_addition_is_refused() -> None:
    queries = "queries:\n" "  - {name: q, sql: SELECT 1, unit: {table: t}}\n"
    for text in (
        _with_additions(queries, url=REQUEST_SCHEMA_URL),
        _with_additions("", url=REQUEST_SCHEMA_URL).replace(
            "      d: {domain: category}\n",
            "      d: {domain: category}\n    workload: [{columns: [c], weight: 1}]\n",
        ),
    ):
        message = _refusal(text)
        assert message.startswith("FLX-5038") and "format 1-1-0" in message and "1-0-0" in message


@pytest.mark.parametrize(
    ("extra", "mention"),
    [
        ("queries: [{name: q, sql: SELECT 1}]\n", "missing key 'unit'"),
        ("queries: [{name: q, sql: SELECT 1, unit: {table: t}, other: 1}]\n", "unknown key"),
        ("queries: [{name: q, sql: SELECT 1, unit: {}}]\n", "exactly one of table and sql"),
        (
            "queries: [{name: q, sql: SELECT 1, unit: {table: t, sql: SELECT 1}}]\n",
            "exactly one of table and sql",
        ),
        ("queries: [{name: q, sql: SELECT 1, unit: {table: zz}}]\n", "names no table"),
        ("queries: [{name: q, sql: SELECT 1, unit: {view: v}}]\n", "unknown key"),
        ("queries: [{name: q, sql: '', unit: {table: t}}]\n", "not empty"),
        ("queries: [{name: q, sql: '   ', unit: {table: t}}]\n", "not empty"),
        ("queries: [{name: q, sql: 5, unit: {table: t}}]\n", "expected a string"),
        ('queries: [{name: q, sql: "SELECT\\u0000 1", unit: {table: t}}]\n', "control character"),
        ('queries: [{name: q, sql: "SELECT\\r1", unit: {table: t}}]\n', "control character"),
        ('queries: [{name: q, sql: "SELECT\\u001b 1", unit: {table: t}}]\n', "control character"),
        ('queries: [{name: q, sql: "e\\u0301", unit: {table: t}}]\n', "Normalization Form C"),
        (
            "queries: [{name: q, sql: SELECT 1, unit: {table: t}},"
            " {name: q, sql: SELECT 2, unit: {table: t}}]\n",
            "names one entry twice",
        ),
        ("queries: [{name: '', sql: SELECT 1, unit: {table: t}}]\n", "a name is 1 to"),
        (
            "queries: [{name: q, sql: SELECT 1, unit: {table: t}, groupBy: 3}]\n",
            "expected a string",
        ),
        ("queries: {}\n", "expected a list"),
        ("queries: [3]\n", "expected a mapping"),
    ],
)
def test_each_query_refusal_names_where_it_is(extra: str, mention: str) -> None:
    message = _refusal(_with_additions(extra))
    assert message.startswith("FLX-5038"), message
    assert mention in message
    assert "request.queries" in message or "names one entry twice" in message


def test_a_query_text_may_hold_a_newline_and_a_tab() -> None:
    text = _with_additions('queries: [{name: q, sql: "SELECT 1\\n\\tFROM t", unit: {table: t}}]\n')
    assert load_request_text(text).queries[0].sql == "SELECT 1\n\tFROM t"


def test_a_query_text_is_bounded_in_utf8_bytes() -> None:
    fits = "x" * MOST_QUERY_BYTES
    over = "x" * (MOST_QUERY_BYTES - 1) + "é"  # one character, two bytes
    request = replace(
        _consumer_request(),
        queries=(RequestQuery("q", fits, RequestUnit(table="customers")),),
    )
    check_request(request)
    with pytest.raises(FluxError) as refused:
        check_request(
            replace(request, queries=(RequestQuery("q", over, RequestUnit(table="customers")),))
        )
    assert "UTF-8 bytes" in str(refused.value) and refused.value.code.code == "FLX-5038"


def test_the_number_of_queries_is_bounded() -> None:
    many = tuple(
        RequestQuery(f"q{n}", "SELECT 1", RequestUnit(table="customers"))
        for n in range(MOST_QUERIES + 1)
    )
    request = replace(_consumer_request(), queries=many)
    with pytest.raises(FluxError) as refused:
        check_request(request)
    assert "more than 1024" in str(refused.value)
    check_request(replace(request, queries=many[:MOST_QUERIES]))


@pytest.mark.parametrize(
    ("workload", "mention"),
    [
        ("[{columns: [a], weight: 1}]", "domain category"),
        ("[{columns: [zz], weight: 1}]", "domain category"),
        ("[{columns: [], weight: 1}]", "1 to 8 columns"),
        ("[{columns: [c, c], weight: 1}]", "each column once"),
        ("[{columns: [d, c], weight: 1}]", "sorted order"),
        ("[{columns: [c], weight: 0}]", "from 1 to 1000000"),
        ("[{columns: [c], weight: 1000001}]", "from 1 to 1000000"),
        ("[{columns: [c], weight: true}]", "from 1 to 1000000"),
        ("[{columns: [c], weight: '1'}]", "from 1 to 1000000"),
        ("[{columns: [c], weight: 1}, {columns: [c], weight: 2}]", "each set of columns once"),
        ("[{columns: [d], weight: 1}, {columns: [c], weight: 2}]", "sorted order"),
        ("[{columns: [c]}]", "missing key 'weight'"),
        ("[{columns: [c], weight: 1, extra: 1}]", "unknown key"),
        ("{}", "expected a list"),
    ],
)
def test_each_workload_refusal_names_where_it_is(workload: str, mention: str) -> None:
    text = _with_additions("").replace(
        "      d: {domain: category}\n",
        f"      d: {{domain: category}}\n    workload: {workload}\n",
    )
    message = _refusal(text)
    assert message.startswith("FLX-5038"), message
    assert mention in message and "request.tables.t.workload" in message


def test_a_workload_names_at_most_eight_columns() -> None:
    columns = tuple(RequestColumn(f"c{n}", "category") for n in range(9))
    names = tuple(column.name for column in columns)
    base = RequestTable("t", True, columns)
    check_request(
        ProfileRequest(None, 16, (replace(base, workload=(RequestWorkload(names[:8], 1),)),), ())
    )
    with pytest.raises(FluxError) as refused:
        check_request(
            ProfileRequest(None, 16, (replace(base, workload=(RequestWorkload(names, 1),)),), ())
        )
    assert "more than 8" in str(refused.value)


def test_a_table_lists_at_most_the_most_workload_sets() -> None:
    columns = tuple(RequestColumn(f"c{n:02d}", "category") for n in range(14))
    from itertools import combinations

    sets = [
        RequestWorkload(names, 1)
        for size in (1, 2, 3, 4, 5, 6)
        for names in combinations([column.name for column in columns], size)
    ]
    assert len(sets) > MOST_WORKLOAD_SETS
    table = RequestTable("t", True, columns, workload=tuple(sorted(sets, key=lambda s: s.columns)))
    with pytest.raises(FluxError) as refused:
        check_request(ProfileRequest(None, 16, (table,), ()))
    assert "more than 4096" in str(refused.value)


def test_a_workload_weight_edges_are_accepted() -> None:
    request = _consumer_request()
    assert request.tables[0].workload[-1].weight == 1000000
    check_request(request)
