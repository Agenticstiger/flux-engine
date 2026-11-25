# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Request format 1-3-0: a history and a null count (PROFILE-REQUEST-SPEC.md section 8).

A table may ask for ``history`` and a column for ``nullCount/2``. A request that asks for neither
keeps the format it named and its bytes, a request of an earlier format that holds one is refused
with ``FLX-5038`` naming the addition and the format to name, and what is written is read back
equal.
"""

from __future__ import annotations

import pytest

from flux_stats.errors import FluxError
from flux_stats.request import (
    REQUEST_SCHEMA_URL,
    REQUEST_SCHEMA_URL_1_1_0,
    REQUEST_SCHEMA_URL_1_2_0,
    REQUEST_SCHEMA_URL_1_3_0,
    ProfileRequest,
    RequestColumn,
    RequestHistory,
    RequestTable,
    check_request,
    load_request_text,
    render_request,
    request_document,
)

pytestmark = pytest.mark.unit

URL_1_3_0 = (
    "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-3-0/FluxProfileRequest.json"
)


def _text(url: str, table: str = "", column: str = "") -> str:
    return (
        f'_schemaURL: "{url}"\n'
        "tables:\n"
        "  loan:\n"
        "    columns:\n"
        f"      branch: {{domain: category{column}}}\n"
        "      valid_from: {domain: timestamp}\n"
        "      valid_to: {domain: timestamp}\n"
        "      due: {domain: number}\n" + table
    )


def _refusal(text: str) -> str:
    with pytest.raises(FluxError) as refused:
        load_request_text(text)
    assert refused.value.code.code == "FLX-5038"
    return str(refused.value)


def _request(history: object = None, statistics: tuple = ()) -> ProfileRequest:
    return ProfileRequest(
        contract_digest="sha256:" + "0" * 64,
        fractional_bits=16,
        tables=(
            RequestTable(
                expose_id="loan",
                row_count=False,
                columns=(
                    RequestColumn("branch", "category", (), statistics),
                    RequestColumn("due", "number", (), ()),
                ),
                history=history,  # type: ignore[arg-type]
            ),
        ),
    )


def test_the_constant_names_the_published_1_3_0_url() -> None:
    assert REQUEST_SCHEMA_URL_1_3_0 == URL_1_3_0


def test_a_history_is_read_with_and_without_candidate_columns() -> None:
    bare = load_request_text(_text(URL_1_3_0, "    history: {}\n"))
    assert bare.tables[0].history == RequestHistory(columns=None)
    named = load_request_text(_text(URL_1_3_0, "    history: {columns: [branch]}\n"))
    assert named.tables[0].history == RequestHistory(columns=("branch",))


def test_a_null_count_is_asked_of_a_column_of_any_domain() -> None:
    text = (
        f'_schemaURL: "{URL_1_3_0}"\n'
        "tables:\n"
        "  loan:\n"
        "    columns:\n"
        '      branch: {domain: category, statistics: ["nullCount/2"]}\n'
        '      due: {domain: number, statistics: ["nullCount/2", "mean/1"]}\n'
        '      returned: {domain: timestamp, statistics: ["nullCount/2"]}\n'
    )
    request = load_request_text(text)
    assert all("nullCount/2" in column.statistics for column in request.tables[0].columns)


def test_the_candidate_columns_are_columns_of_the_table_and_categorical() -> None:
    unknown = _refusal(_text(URL_1_3_0, "    history: {columns: [nowhere]}\n"))
    assert "history.columns[0]" in unknown
    not_categorical = _refusal(_text(URL_1_3_0, "    history: {columns: [due]}\n"))
    assert "history.columns[0]" in not_categorical and "categorical" in not_categorical
    twice = _refusal(_text(URL_1_3_0, "    history: {columns: [branch, branch]}\n"))
    assert "history.columns" in twice
    assert "nothing" in _refusal(_text(URL_1_3_0, "    history: {nothing: 1}\n"))


@pytest.mark.parametrize(
    "url", [REQUEST_SCHEMA_URL, REQUEST_SCHEMA_URL_1_1_0, REQUEST_SCHEMA_URL_1_2_0]
)
def test_an_earlier_format_that_holds_a_history_is_refused_naming_the_format_to_name(
    url: str,
) -> None:
    message = _refusal(_text(url, "    history: {}\n"))
    assert "history" in message and "1-3-0" in message and URL_1_3_0 in message


@pytest.mark.parametrize(
    "url", [REQUEST_SCHEMA_URL, REQUEST_SCHEMA_URL_1_1_0, REQUEST_SCHEMA_URL_1_2_0]
)
def test_an_earlier_format_that_asks_a_null_count_is_refused_naming_the_format_to_name(
    url: str,
) -> None:
    text = _text(url).replace("domain: category", 'domain: category, statistics: ["nullCount/2"]')
    message = _refusal(text)
    assert "statistics[0]" in message and URL_1_3_0 in message


def test_a_request_with_neither_keeps_its_format_and_bytes() -> None:
    request = _request()
    assert request_document(request)["_schemaURL"] == REQUEST_SCHEMA_URL
    text = render_request(request)
    assert "history" not in text and "nullCount" not in text


def test_a_request_that_asks_for_either_names_1_3_0_and_reads_back_equal() -> None:
    for request in (
        _request(history=RequestHistory()),
        _request(history=RequestHistory(columns=("branch",))),
        _request(statistics=("nullCount/2",)),
    ):
        check_request(request)
        text = render_request(request)
        assert URL_1_3_0 in text
        assert load_request_text(text) == request
