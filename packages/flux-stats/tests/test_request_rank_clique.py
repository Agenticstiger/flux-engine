# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Request format 1-2-0: the table asks ``rankCorrelations`` and ``cliqueColumns``.

Each is read exactly, refused with ``FLX-5038`` when it breaks a rule or sits in a document that
names an earlier format, written back to the same bytes, and leaves a request without it in the
format it named before.
"""

from __future__ import annotations

from dataclasses import replace
from typing import List

import pytest

pytest.importorskip("yaml")

from flux_stats.errors import FluxError  # noqa: E402
from flux_stats.request import (  # noqa: E402
    MOST_CLIQUE_CANDIDATES,
    MOST_RANK_COLUMNS,
    REQUEST_SCHEMA_URL,
    REQUEST_SCHEMA_URL_1_1_0,
    REQUEST_SCHEMA_URL_1_2_0,
    ProfileRequest,
    RequestColumn,
    RequestRankCorrelation,
    RequestTable,
    check_request,
    load_request_text,
    render_request,
    request_document,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "ab" * 32
URL_1_2_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-2-0/FluxProfileRequest.json"
)

COLUMNS = (
    RequestColumn("age", "number", (), ()),
    RequestColumn("income", "number", (), ()),
    RequestColumn("joined", "timestamp", (), ()),
    RequestColumn("plan", "category", (), ()),
)


def _request(**table: object) -> ProfileRequest:
    return ProfileRequest(
        contract_digest=DIGEST,
        fractional_bits=16,
        tables=(RequestTable("subscriber", True, COLUMNS, **table),),  # type: ignore[arg-type]
    )


def _asks() -> ProfileRequest:
    return _request(
        rank_correlations=(
            RequestRankCorrelation("kendallTau/1", ("age", "income", "joined")),
            RequestRankCorrelation("spearmanRho/1", ("age", "income")),
        ),
        clique_columns=("age", "plan", "joined"),
    )


def _text(url: str, extra: str) -> str:
    return (
        f'_schemaURL: "{url}"\n'
        "tables:\n"
        "  t:\n"
        "    columns:\n"
        "      a: {domain: number}\n"
        "      b: {domain: timestamp}\n"
        "      c: {domain: category}\n" + extra
    )


def _refusal(text: str) -> str:
    with pytest.raises(FluxError) as refused:
        load_request_text(text)
    return f"{refused.value.code.code} {refused.value}"


def test_the_constant_names_the_published_1_2_0_url() -> None:
    assert REQUEST_SCHEMA_URL_1_2_0 == URL_1_2_0


def test_the_asks_are_written_and_read_back_equal_in_format_1_2_0() -> None:
    request = _asks()
    check_request(request)
    text = render_request(request)
    assert URL_1_2_0 in text and REQUEST_SCHEMA_URL not in text
    assert "rankCorrelations" in text and "cliqueColumns" in text
    assert load_request_text(text) == request
    assert render_request(load_request_text(text)) == text
    assert request_document(request)["_schemaURL"] == URL_1_2_0


def test_either_ask_alone_makes_the_request_1_2_0() -> None:
    request = _asks()
    only_rank = replace(request.tables[0], clique_columns=())
    only_clique = replace(request.tables[0], rank_correlations=())
    for table in (only_rank, only_clique):
        asked = replace(request, tables=(table,))
        assert request_document(asked)["_schemaURL"] == URL_1_2_0
        assert load_request_text(render_request(asked)) == asked


def test_a_request_without_the_asks_keeps_the_format_and_the_bytes_it_had() -> None:
    plain = _request()
    text = render_request(plain)
    assert REQUEST_SCHEMA_URL in text and "1-2-0" not in text
    assert "rankCorrelations" not in text and "cliqueColumns" not in text
    # an empty ask is no ask
    assert render_request(_request(rank_correlations=(), clique_columns=())) == text
    # a 1-1-0 request stays 1-1-0
    dated = ProfileRequest(
        contract_digest=DIGEST,
        fractional_bits=16,
        tables=(RequestTable("subscriber", True, COLUMNS, calendar=("joined",)),),
    )
    assert request_document(dated)["_schemaURL"] == REQUEST_SCHEMA_URL_1_1_0


def test_a_1_2_0_request_holds_what_1_1_0_holds_and_the_reader_reads_all_three() -> None:
    text = _text(URL_1_2_0, "    calendar: [b]\n    rowCount: true\n")
    assert load_request_text(text).tables[0].calendar == ("b",)
    for url in (REQUEST_SCHEMA_URL, REQUEST_SCHEMA_URL_1_1_0, URL_1_2_0):
        assert load_request_text(_text(url, "")).tables[0].rank_correlations == ()
    assert "FLX-5038" in _refusal(_text(URL_1_2_0.replace("1-2-0", "1-4-0"), ""))


@pytest.mark.parametrize("url", [REQUEST_SCHEMA_URL, REQUEST_SCHEMA_URL_1_1_0])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("rankCorrelations", "[{definition: kendallTau/1, columns: [a, b]}]"),
        ("cliqueColumns", "[a, c]"),
    ],
)
def test_an_ask_in_a_document_that_names_an_earlier_format_is_refused_naming_the_addition(
    url: str, key: str, value: str
) -> None:
    message = _refusal(_text(url, f"    {key}: {value}\n"))
    assert "FLX-5038" in message and f"request.tables.t.{key}" in message
    assert "addition of format 1-2-0" in message and URL_1_2_0 in message
    assert load_request_text(_text(URL_1_2_0, f"    {key}: {value}\n"))


@pytest.mark.parametrize(
    ("extra", "where"),
    [
        ("    rankCorrelations: {kendallTau/1: [a, b]}\n", "rankCorrelations"),
        ("    rankCorrelations: [{definition: kendallTau/1}]\n", "missing key"),
        (
            "    rankCorrelations: [{definition: kendallTau/1, columns: [a, b], extra: 1}]\n",
            "unknown key",
        ),
        ("    rankCorrelations: [{definition: correlation/1, columns: [a, b]}]\n", "definition"),
        ("    rankCorrelations: [{definition: pearson, columns: [a, b]}]\n", "definition"),
        ("    rankCorrelations: [{definition: kendallTau/1, columns: [a]}]\n", "2 to 64 columns"),
        ("    rankCorrelations: [{definition: kendallTau/1, columns: []}]\n", "2 to 64 columns"),
        (
            "    rankCorrelations: [{definition: kendallTau/1, columns: [a, a]}]\n",
            "one entry twice",
        ),
        (
            "    rankCorrelations: [{definition: kendallTau/1, columns: [a, c]}]\n",
            "number or timestamp",
        ),
        (
            "    rankCorrelations: [{definition: kendallTau/1, columns: [a, z]}]\n",
            "number or timestamp",
        ),
        (
            "    rankCorrelations: [{definition: kendallTau/1, columns: [a, b]},"
            " {definition: kendallTau/1, columns: [a, b]}]\n",
            "one entry twice",
        ),
        (
            "    rankCorrelations: [{definition: kendallTau/1, columns: [a, b]},"
            " {definition: spearmanRho/1, columns: [a, b]},"
            " {definition: kendallTau/1, columns: [a, b]}]\n",
            "holds more than",
        ),
        ("    cliqueColumns: [a, a]\n", "one entry twice"),
        ("    cliqueColumns: [z]\n", "names no column of this table"),
        ("    cliqueColumns: {a: 1}\n", "cliqueColumns"),
        ("    cliqueColumns: [1.5]\n", "cliqueColumns[0]"),
    ],
)
def test_every_refusal_of_an_ask_is_flx_5038_naming_where_and_quoting_no_value(
    extra: str, where: str
) -> None:
    message = _refusal(_text(URL_1_2_0, extra))
    assert message.startswith("FLX-5038"), message
    assert "request.tables.t." in message and where in message, message


def test_the_bounds_are_64_columns_a_rank_correlation_and_1024_clique_columns() -> None:
    assert MOST_RANK_COLUMNS == 64 and MOST_CLIQUE_CANDIDATES == 1024

    def table(names: List[str], rank: List[str], clique: List[str]) -> str:
        columns = "".join(f"      {name}: {{domain: number}}\n" for name in names)
        return (
            f'_schemaURL: "{URL_1_2_0}"\ntables:\n  t:\n    columns:\n{columns}'
            f"    rankCorrelations: [{{definition: spearmanRho/1, columns: [{', '.join(rank)}]}}]\n"
            f"    cliqueColumns: [{', '.join(clique)}]\n"
        )

    names = [f"c{n:04d}" for n in range(1025)]
    table_ok = load_request_text(table(names, names[:64], names[:1024])).tables[0]
    assert len(table_ok.rank_correlations[0].columns) == 64 and len(table_ok.clique_columns) == 1024
    assert "holds more than 64" in _refusal(table(names, names[:65], names[:2]))
    assert "holds more than 1024" in _refusal(table(names, names[:2], names[:1025]))


def test_check_request_takes_what_the_reader_takes_and_names_what_it_would_refuse() -> None:
    check_request(_asks())
    too_wide = tuple(f"c{n}" for n in range(65))
    wide = _request(rank_correlations=(RequestRankCorrelation("kendallTau/1", too_wide),))
    with pytest.raises(FluxError) as refused:
        check_request(wide)
    assert refused.value.code.code == "FLX-5038" and "rank correlation columns" in str(
        refused.value
    )
    bad = _request(rank_correlations=(RequestRankCorrelation("kendallTau/1", ("age", "plan")),))
    with pytest.raises(FluxError) as refused:
        check_request(bad)
    assert "rankCorrelations[0].columns[1]" in str(refused.value)
    too_many = _request(clique_columns=tuple(f"c{n}" for n in range(MOST_CLIQUE_CANDIDATES + 1)))
    with pytest.raises(FluxError) as refused:
        check_request(too_many)
    assert "clique columns" in str(refused.value)


def test_the_list_of_keys_that_need_1_2_0_is_the_one_the_reader_and_the_writer_use() -> None:
    from flux_stats import request as module

    keys = [(item.section, item.key) for item in module._ADDITIONS_OF_1_2_0]
    assert keys == [
        ("table", "rankCorrelations"),
        ("table", "cliqueColumns"),
        ("relationship", "groupBy"),
        ("relationship", "linked"),
    ]
    assert module._keys_of_1_2_0("table") == ("rankCorrelations", "cliqueColumns")
    assert module._keys_of_1_2_0("relationship") == ("groupBy", "linked")
