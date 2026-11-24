# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Request format 1-2-0: a relationship asks ``groupBy`` and ``linked``.

``groupBy`` lists columns of the relationship's parent table, each once, at most 64; ``linked`` lists
a child column and a parent column, each pair once, at most 1,024. Each is read exactly, refused with
``FLX-5038`` when it breaks a rule or sits in a document that names an earlier format, written back to
the same bytes, and leaves a request without it in the format it named before.
"""

from __future__ import annotations

from dataclasses import replace
from typing import List

import pytest

pytest.importorskip("yaml")

from flux_stats.errors import FluxError  # noqa: E402
from flux_stats.request import (  # noqa: E402
    MOST_RELATIONSHIP_GROUPS,
    MOST_RELATIONSHIP_LINKS,
    REQUEST_SCHEMA_URL,
    REQUEST_SCHEMA_URL_1_1_0,
    REQUEST_SCHEMA_URL_1_2_0,
    ProfileRequest,
    RequestColumn,
    RequestEnd,
    RequestLinked,
    RequestRelationship,
    RequestTable,
    check_request,
    load_request_text,
    render_request,
    request_document,
)

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "ab" * 32

# in name order, the order the reader gives a table's columns
PARENT = (
    RequestColumn("balance", "number", (), ()),
    RequestColumn("id", "category", (), ()),
    RequestColumn("opened", "timestamp", (), ()),
    RequestColumn("tier", "category", (), ()),
)
CHILD = (
    RequestColumn("account_id", "category", (), ()),
    RequestColumn("amount", "number", (), ()),
    RequestColumn("kind", "category", (), ()),
)


def _relationship(**more: object) -> RequestRelationship:
    return RequestRelationship(
        RequestEnd("account", ("id",)),
        RequestEnd("invoice", ("account_id",)),
        ("mean/1",),
        **more,  # type: ignore[arg-type]
    )


def _request(**more: object) -> ProfileRequest:
    return ProfileRequest(
        contract_digest=DIGEST,
        fractional_bits=16,
        tables=(
            RequestTable("account", True, PARENT),
            RequestTable("invoice", True, CHILD),
        ),
        relationships=(_relationship(**more),),
    )


def _asks() -> ProfileRequest:
    return _request(
        group_by=("tier", "opened", "balance"),
        linked=(RequestLinked("kind", "tier"), RequestLinked("amount", "tier")),
    )


def _text(url: str, extra: str) -> str:
    return (
        f'_schemaURL: "{url}"\n'
        "tables:\n"
        "  account:\n"
        "    columns:\n"
        "      id: {domain: category}\n"
        "      tier: {domain: category}\n"
        "  invoice:\n"
        "    columns:\n"
        "      account_id: {domain: category}\n"
        "      kind: {domain: category}\n"
        "relationships:\n"
        "  - parent: {table: account, columns: [id]}\n"
        "    child: {table: invoice, columns: [account_id]}\n"
        "    statistics: [mean/1]\n" + extra
    )


def _refusal(text: str) -> str:
    with pytest.raises(FluxError) as refused:
        load_request_text(text)
    return f"{refused.value.code.code} {refused.value}"


def test_the_asks_are_written_and_read_back_equal_in_format_1_2_0() -> None:
    request = _asks()
    check_request(request)
    text = render_request(request)
    assert REQUEST_SCHEMA_URL_1_2_0 in text and REQUEST_SCHEMA_URL not in text
    assert "groupBy" in text and "linked" in text and "parentColumn" in text
    assert load_request_text(text) == request
    assert render_request(load_request_text(text)) == text
    assert request_document(request)["_schemaURL"] == REQUEST_SCHEMA_URL_1_2_0


def test_either_ask_alone_makes_the_request_1_2_0() -> None:
    request = _asks()
    only_group = replace(request, relationships=(replace(request.relationships[0], linked=()),))
    only_linked = replace(request, relationships=(replace(request.relationships[0], group_by=()),))
    for asked in (only_group, only_linked):
        assert request_document(asked)["_schemaURL"] == REQUEST_SCHEMA_URL_1_2_0
        assert load_request_text(render_request(asked)) == asked


def test_a_request_without_the_asks_keeps_the_format_and_the_bytes_it_had() -> None:
    plain = _request()
    text = render_request(plain)
    assert REQUEST_SCHEMA_URL in text and "1-2-0" not in text
    assert "groupBy" not in text and "linked" not in text
    assert render_request(_request(group_by=(), linked=())) == text
    dated = _request(exposure=())
    assert request_document(dated)["_schemaURL"] == REQUEST_SCHEMA_URL


@pytest.mark.parametrize("url", [REQUEST_SCHEMA_URL, REQUEST_SCHEMA_URL_1_1_0])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("groupBy", "[tier]"),
        ("linked", "[{column: kind, parentColumn: tier}]"),
    ],
)
def test_an_ask_in_a_document_that_names_an_earlier_format_is_refused_naming_the_addition(
    url: str, key: str, value: str
) -> None:
    message = _refusal(_text(url, f"    {key}: {value}\n"))
    assert message.startswith("FLX-5038") and f"request.relationships[0].{key}" in message
    assert "addition of format 1-2-0" in message and REQUEST_SCHEMA_URL_1_2_0 in message
    assert load_request_text(_text(REQUEST_SCHEMA_URL_1_2_0, f"    {key}: {value}\n"))


@pytest.mark.parametrize(
    ("extra", "where"),
    [
        ("    groupBy: [tier, tier]\n", "one entry twice"),
        ("    groupBy: [kind]\n", "names no column of the relationship's parent table"),
        ("    groupBy: [nope]\n", "groupBy[0]"),
        ("    groupBy: {tier: 1}\n", "groupBy"),
        ("    groupBy: [1.5]\n", "groupBy[0]"),
        ("    linked: [{column: kind}]\n", "missing key"),
        ("    linked: [{column: kind, parentColumn: tier, extra: 1}]\n", "unknown key"),
        ("    linked: [{column: tier, parentColumn: tier}]\n", "linked[0].column"),
        ("    linked: [{column: kind, parentColumn: kind}]\n", "linked[0].parentColumn"),
        (
            "    linked: [{column: kind, parentColumn: tier}, {column: kind, parentColumn: tier}]\n",
            "one entry twice",
        ),
        ("    linked: {kind: tier}\n", "linked"),
    ],
)
def test_every_refusal_of_an_ask_is_flx_5038_naming_where_and_quoting_no_value(
    extra: str, where: str
) -> None:
    message = _refusal(_text(REQUEST_SCHEMA_URL_1_2_0, extra))
    assert message.startswith("FLX-5038"), message
    assert "request.relationships[0]." in message and where in message, message


def test_one_child_column_may_be_linked_to_two_parent_columns_and_the_reverse() -> None:
    text = _text(
        REQUEST_SCHEMA_URL_1_2_0,
        "    linked: [{column: kind, parentColumn: tier}, {column: kind, parentColumn: id},"
        " {column: account_id, parentColumn: tier}]\n",
    )
    assert len(load_request_text(text).relationships[0].linked) == 3


def test_the_bounds_are_64_group_columns_and_1024_linked_pairs() -> None:
    assert MOST_RELATIONSHIP_GROUPS == 64 and MOST_RELATIONSHIP_LINKS == 1024

    def text(parents: List[str], links: int) -> str:
        columns = "".join(f"      {name}: {{domain: category}}\n" for name in parents)
        pairs = ", ".join(
            f"{{column: c{n // 2}, parentColumn: {parents[n % 2]}}}" for n in range(links)
        )
        children = "".join(f"      c{n}: {{domain: category}}\n" for n in range(links))
        return (
            f'_schemaURL: "{REQUEST_SCHEMA_URL_1_2_0}"\ntables:\n  account:\n    columns:\n{columns}'
            f"      id: {{domain: category}}\n  invoice:\n    columns:\n{children}"
            "      account_id: {domain: category}\n"
            "relationships:\n  - parent: {table: account, columns: [id]}\n"
            "    child: {table: invoice, columns: [account_id]}\n    statistics: [mean/1]\n"
            f"    groupBy: [{', '.join(parents)}]\n    linked: [{pairs}]\n"
        )

    names = [f"p{n:03d}" for n in range(65)]
    ok = load_request_text(text(names[:64], 2)).relationships[0]
    assert len(ok.group_by) == 64
    assert "holds more than 64" in _refusal(text(names, 2))
    # 1,024 distinct pairs need 512 child columns by two parent columns
    ok = load_request_text(text(names[:2], 1024)).relationships[0]
    assert len(ok.linked) == 1024
    assert "holds more than 1024" in _refusal(text(names[:2], 1025))


def test_check_request_takes_what_the_reader_takes_and_names_what_it_would_refuse() -> None:
    check_request(_asks())
    too_wide = _request(group_by=tuple(f"c{n}" for n in range(MOST_RELATIONSHIP_GROUPS + 1)))
    with pytest.raises(FluxError) as refused:
        check_request(too_wide)
    assert refused.value.code.code == "FLX-5038" and "groupBy" in str(refused.value)
    bad = _request(linked=(RequestLinked("nope", "tier"),))
    with pytest.raises(FluxError) as refused:
        check_request(bad)
    assert "linked[0].column" in str(refused.value)
    too_many = _request(
        linked=tuple(RequestLinked(f"c{n}", "tier") for n in range(MOST_RELATIONSHIP_LINKS + 1))
    )
    with pytest.raises(FluxError) as refused:
        check_request(too_many)
    assert "linked" in str(refused.value)
