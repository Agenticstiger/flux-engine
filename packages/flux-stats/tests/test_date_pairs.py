# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The days between two dates: a profile's ``datePairs`` and ``dates``, and the request that asks.

Profile format 1-3-0 holds ``tables.<id>.datePairs`` and ``relationships[].dates``
(PROFILE-SPEC.md section 5); request format 1-1-0 asks for them (PROFILE-REQUEST-SPEC.md).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict

import pytest
from jsonschema import Draft202012Validator, FormatChecker

pytest.importorskip("yaml")

from flux_stats.errors import FluxError  # noqa: E402
from flux_stats.profile import (  # noqa: E402
    DATE_ORDER_LEVELS,
    MAX_DATE_PAIRS,
    SCHEMA_URL_1_2_0,
    SCHEMA_URL_1_3_0,
    parse_profile,
)
from flux_stats.request import (  # noqa: E402
    MOST_DATE_PAIRS,
    REQUEST_SCHEMA_URL,
    REQUEST_SCHEMA_URL_1_1_0,
    ProfileRequest,
    RequestColumn,
    RequestDateLink,
    RequestDatePair,
    RequestEnd,
    RequestFact,
    RequestRelationship,
    RequestTable,
    check_request,
    load_request_text,
    render_request,
    request_document,
)

pytestmark = pytest.mark.unit


def _root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "spec" / "PROFILE-SPEC.md").is_file():
            return parent
    raise RuntimeError("these tests read docs/spec from the flux-engine checkout around them")


SPEC = _root() / "docs" / "spec"
EXAMPLE: Dict[str, Any] = json.loads((SPEC / "profile-1-3-example.json").read_text("utf-8"))
SCHEMA: Dict[str, Any] = json.loads((SPEC / "profile-1-3.schema.json").read_text("utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

PAIRS = ("tables", "payment", "datePairs")
DATES = ("relationships", 0, "dates")


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _pair(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["tables"]["payment"]["datePairs"][0]


def _link(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["relationships"][0]["dates"][0]


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


# -- the profile reads them ---------------------------------------------------------------------


def test_the_example_validates_and_reads_both_members() -> None:
    assert not list(VALIDATOR.iter_errors(EXAMPLE))
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    (pair,) = profile.tables["payment"].date_pairs
    assert (pair.start, pair.end) == ("paid_at", "settled_at")
    assert [entry.identifier for entry in pair.statistics] == ["marginalHistogram/1"]
    assert pair.order.identifier == "levelHistogram/1"
    assert pair.order.levels == DATE_ORDER_LEVELS == ("after", "before", "same")
    (link,) = profile.relationships[0].dates
    assert (link.parent, link.child) == ("joined_at", "paid_at")
    assert link.order.levels == DATE_ORDER_LEVELS
    assert profile.tables["subscriber"].date_pairs == ()


def test_a_profile_without_them_has_empty_tuples() -> None:
    document = _edit(
        lambda d: (d["tables"]["payment"].pop("datePairs"), d["relationships"][0].pop("dates"))
    )
    profile = parse_profile(document)
    assert profile.tables["payment"].date_pairs == ()
    assert profile.relationships[0].dates == ()


def test_a_small_cell_of_a_pair_statistic_is_suppressed_as_for_a_column_statistic() -> None:
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    order = profile.relationships[0].dates[0].order
    group = order.groups[0]
    assert group.suppressed_cells == (1,) and group.cell_status == ("F", "A", "F")
    assert group.statistic is not None and group.statistic.level_counts[1] == 0


def test_a_withheld_group_of_a_pair_statistic_reads_as_withheld() -> None:
    def withhold(document: Dict[str, Any]) -> None:
        _pair(document)["statistics"][0]["groups"] = [{"group": "", "status": "A"}]

    group = parse_profile(_edit(withhold)).tables["payment"].date_pairs[0].statistics[0].groups[0]
    assert group.published is False and group.statistic is None


def test_a_pair_statistic_names_a_disclosure_rule_and_a_sample_size_as_any_statistic_does() -> None:
    def unknown_rule(document: Dict[str, Any]) -> None:
        _pair(document)["order"]["disclosure"] = "nope"

    assert "names no rule in disclosureRules" in _refused(_edit(unknown_rule))


def test_a_statistic_of_a_pair_may_be_grouped_by_a_column_of_the_table() -> None:
    def group(document: Dict[str, Any]) -> None:
        statistic = _pair(document)["statistics"][0]
        statistic["groupBy"] = "segment"
        statistic["groups"] = [
            {
                "group": "gold",
                "k": 40,
                "binCounts": [None, 10, 20],
                "cellStatus": ["A", "F", "F"],
                "status": "F",
            },
            {"group": "standard", "k": 1060, "binCounts": [620, 300, 140], "status": "F"},
        ]

    pair = parse_profile(_edit(group)).tables["payment"].date_pairs[0]
    assert pair.statistics[0].group_by == "segment"
    assert not list(VALIDATOR.iter_errors(_edit(group)))


# -- every refusal ----------------------------------------------------------------------------


def _set(path: Any, key: str, value: Any) -> Callable[[Dict[str, Any]], None]:
    def change(document: Dict[str, Any]) -> None:
        (_pair if path == "pair" else _link)(document)[key] = value

    return change


def _add_pair_key(document: Dict[str, Any]) -> None:
    _pair(document)["extra"] = 1


def _add_link_key(document: Dict[str, Any]) -> None:
    _link(document)["extra"] = 1


def _group_link(document: Dict[str, Any]) -> None:
    _pair(document)["statistics"][0]["per"] = ["subscriber_id"]


def _group_a_link_statistic(document: Dict[str, Any]) -> None:
    _link(document)["statistics"][0]["groupBy"] = "segment"


def _mean(document: Dict[str, Any]) -> None:
    _pair(document)["statistics"][0]["definition"] = "mean/1"


def _grouped_order(document: Dict[str, Any]) -> None:
    _pair(document)["order"]["groupBy"] = "segment"


def _duplicate(document: Dict[str, Any]) -> None:
    document["tables"]["payment"]["datePairs"].append(copy.deepcopy(_pair(document)))


def _duplicate_link(document: Dict[str, Any]) -> None:
    document["relationships"][0]["dates"].append(copy.deepcopy(_link(document)))


def _too_many(document: Dict[str, Any]) -> None:
    document["tables"]["payment"]["datePairs"] = [_pair(document)] * (MAX_DATE_PAIRS + 1)


def _too_many_links(document: Dict[str, Any]) -> None:
    document["relationships"][0]["dates"] = [_link(document)] * (MAX_DATE_PAIRS + 1)


def _noisy_link(document: Dict[str, Any]) -> None:
    _link(document)["order"]["noise"] = {}


REFUSALS = [
    pytest.param(_add_pair_key, "extra", id="unknown-key-in-a-pair"),
    pytest.param(_add_link_key, "extra", id="unknown-key-in-a-link"),
    pytest.param(_set("pair", "start", "nope"), "datePairs[0].start", id="start-not-a-column"),
    pytest.param(_set("pair", "end", "nope"), "datePairs[0].end", id="end-not-a-column"),
    pytest.param(_set("pair", "end", "attempts"), "datePairs[0].end", id="end-not-a-timestamp"),
    pytest.param(_set("pair", "end", "paid_at"), "two different columns", id="one-column-twice"),
    pytest.param(_set("link", "parent", "nope"), "dates[0].parent", id="parent-not-a-column"),
    pytest.param(_set("link", "child", "joined_at"), "dates[0].child", id="child-is-the-parent-s"),
    pytest.param(_set("link", "parent", "tenure_months"), "dates[0].parent", id="parent-a-number"),
    pytest.param(_group_link, "per", id="per-on-a-pair-statistic"),
    pytest.param(_group_a_link_statistic, "groupBy", id="groupBy-on-a-relationship-date"),
    pytest.param(_mean, "this section carries", id="mean-is-not-a-form-of-a-gap"),
    pytest.param(_grouped_order, "groupBy", id="grouped-order"),
    pytest.param(_duplicate, "names one pair twice", id="same-pair-twice"),
    pytest.param(_duplicate_link, "names one pair twice", id="same-link-twice"),
    pytest.param(_too_many, "holds at most", id="too-many-pairs"),
    pytest.param(_too_many_links, "holds at most", id="too-many-links"),
    pytest.param(_noisy_link, "noise", id="noise-on-a-relationship-date"),
]


@pytest.mark.parametrize(("change", "fragment"), REFUSALS)
def test_a_broken_member_is_refused_naming_where(
    change: Callable[[Dict[str, Any]], None], fragment: str
) -> None:
    assert fragment in _refused(_edit(change))


@pytest.mark.parametrize(
    "levels", [["false", "true"], ["after", "before"], ["before", "same", "after"]]
)
def test_levels_other_than_the_three_are_refused(levels: list) -> None:
    def change(document: Dict[str, Any]) -> None:
        _pair(document)["order"]["levels"] = levels

    assert "an order counts the levels" in _refused(_edit(change))


def test_the_schema_refuses_a_malformed_member_too() -> None:
    bad = [
        _add_pair_key,
        _add_link_key,
        _mean,
        _group_a_link_statistic,
        _grouped_order,
        lambda d: _pair(d)["order"].__setitem__("levels", ["false", "true"]),
        lambda d: _pair(d).pop("order"),
        lambda d: _link(d).pop("statistics"),
    ]
    for change in bad:
        assert list(VALIDATOR.iter_errors(_edit(change)))


def _without_other_1_3_forms(document: Dict[str, Any]) -> None:
    document.pop("references")
    document["relationships"][0].pop("structural", None)
    for table in document["tables"].values():
        table.pop("history", None)
    document["tables"]["payment"].pop("calendar")
    document["relationships"][0].pop("exposure")
    for table in document["tables"].values():
        for column in table["columns"].values():
            column["statistics"] = [
                statistic
                for statistic in column.get("statistics", [])
                if not {"groupByLink", "per"} & set(statistic)
                and statistic.get("groupBy") != "attempts"
            ]


@pytest.mark.parametrize("where", ["pair", "link"])
def test_an_earlier_format_that_carries_a_member_is_refused(where: str) -> None:
    def change(document: Dict[str, Any]) -> None:
        _without_other_1_3_forms(document)
        document["_schemaURL"] = SCHEMA_URL_1_2_0
        if where == "pair":
            document["relationships"][0].pop("dates")
        else:
            document["tables"]["payment"].pop("datePairs")

    message = _refused(_edit(change))
    assert "format 1-3-0" in message and ("datePairs" if where == "pair" else "dates") in message


def test_the_same_document_without_either_member_reads_as_an_earlier_format() -> None:
    def change(document: Dict[str, Any]) -> None:
        _without_other_1_3_forms(document)
        document["_schemaURL"] = SCHEMA_URL_1_2_0
        document["relationships"][0].pop("dates")
        document["tables"]["payment"].pop("datePairs")

    assert parse_profile(_edit(change)).tables["payment"].date_pairs == ()
    assert SCHEMA_URL_1_3_0 != SCHEMA_URL_1_2_0


# -- the request -------------------------------------------------------------------------------


def _request(*, pairs: bool = True, links: bool = True) -> ProfileRequest:
    return ProfileRequest(
        contract_digest="sha256:" + "ab" * 32,
        fractional_bits=16,
        tables=(
            RequestTable(
                expose_id="customers",
                row_count=True,
                columns=(
                    RequestColumn("closed", "timestamp"),
                    RequestColumn("created", "timestamp"),
                    RequestColumn("id", "number", ("min",), ("mean/1",)),
                ),
                date_pairs=(RequestDatePair("created", "closed"),) if pairs else (),
            ),
            RequestTable(
                expose_id="orders",
                row_count=False,
                columns=(
                    RequestColumn("customer_id", "number"),
                    RequestColumn("placed", "timestamp"),
                ),
            ),
        ),
        relationships=(
            RequestRelationship(
                parent=RequestEnd("customers", ("id",)),
                child=RequestEnd("orders", ("customer_id",)),
                statistics=("mean/1",),
                dates=(RequestDateLink("created", "placed"),) if links else (),
            ),
        ),
        facts=(RequestFact("orders.placed_late", "orders", "placed_late"),),
    )


#: ``_request(pairs=False, links=False)`` as the writer wrote it before format 1-1-0 existed.
BEFORE_1_1_0 = (
    "# A profile request: the statistics a profiler measures for the tables of a FLUID contract.\n"
    "# Its format is docs/spec/PROFILE-REQUEST-SPEC.md; a profiler writes the profile it asks for.\n"
    '"_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-0-0/FluxProfileRequest.json"\n'
    '"contract":\n  "digest": "sha256:abababababababababababababababababababababababababababababababab"\n'
    '"encoding":\n  "fractionalBits": 16\n  "rounding": "floor"\n'
    '"tables":\n  "customers":\n    "columns":\n'
    '      "closed":\n        "domain": "timestamp"\n'
    '      "created":\n        "domain": "timestamp"\n'
    '      "id":\n        "domain": "number"\n        "metrics":\n          - "min"\n'
    '        "statistics":\n          - "mean/1"\n'
    '    "rowCount": true\n'
    '  "orders":\n    "columns":\n'
    '      "customer_id":\n        "domain": "number"\n'
    '      "placed":\n        "domain": "timestamp"\n'
    '"relationships":\n  - "parent":\n      "table": "customers"\n      "columns":\n        - "id"\n'
    '    "child":\n      "table": "orders"\n      "columns":\n        - "customer_id"\n'
    '    "statistics":\n      - "mean/1"\n'
    '"facts":\n  - "name": "orders.placed_late"\n    "table": "orders"\n    "rule": "placed_late"\n'
)


def test_a_request_that_asks_for_neither_is_written_as_1_0_0_byte_for_byte() -> None:
    text = render_request(_request(pairs=False, links=False))
    assert text == BEFORE_1_1_0
    assert load_request_text(text) == _request(pairs=False, links=False)


def test_a_request_that_asks_for_either_is_written_as_1_1_0_and_read_back_equal() -> None:
    for request in (_request(), _request(links=False), _request(pairs=False)):
        text = render_request(request)
        assert REQUEST_SCHEMA_URL_1_1_0 in text and REQUEST_SCHEMA_URL not in text
        assert load_request_text(text) == request
        assert render_request(load_request_text(text)) == text
        check_request(request)
    document = request_document(_request())
    assert document["tables"]["customers"]["datePairs"] == [{"start": "created", "end": "closed"}]
    assert document["relationships"][0]["dates"] == [{"parent": "created", "child": "placed"}]


def _text(
    extra_table: str = "", extra_relationship: str = "", url: str = REQUEST_SCHEMA_URL_1_1_0
) -> str:
    return (
        f'_schemaURL: "{url}"\n'
        "tables:\n"
        "  p:\n"
        "    columns:\n"
        "      id: {domain: number}\n"
        "      a: {domain: timestamp}\n"
        "      b: {domain: timestamp}\n" + extra_table + "  c:\n"
        "    columns:\n"
        "      pid: {domain: number}\n"
        "      d: {domain: timestamp}\n"
        "relationships:\n"
        "  - parent: {table: p, columns: [id]}\n"
        "    child: {table: c, columns: [pid]}\n"
        "    statistics: [mean/1]\n" + extra_relationship
    )


def _refusal(text: str) -> str:
    with pytest.raises(FluxError) as refused:
        load_request_text(text)
    assert refused.value.code.code == "FLX-5038"
    return str(refused.value)


def test_a_1_0_0_document_that_carries_either_member_is_refused() -> None:
    old = REQUEST_SCHEMA_URL
    assert "datePairs" in _refusal(_text("    datePairs: [{start: a, end: b}]\n", url=old))
    assert "dates" in _refusal(
        _text(extra_relationship="    dates: [{parent: a, child: d}]\n", url=old)
    )


@pytest.mark.parametrize(
    ("table", "relationship", "fragment"),
    [
        ("    datePairs: [{start: a, end: a}]\n", "", "two different columns"),
        ("    datePairs: [{start: a, end: id}]\n", "", "datePairs[0].end"),
        ("    datePairs: [{start: a, end: zz}]\n", "", "datePairs[0].end"),
        ("    datePairs: [{start: a, end: b}, {start: a, end: b}]\n", "", "names one entry twice"),
        ("    datePairs: [{start: a, end: b, extra: 1}]\n", "", "unknown key"),
        ("    datePairs: [{start: a}]\n", "", "missing key"),
        ("", "    dates: [{parent: a, child: b}]\n", "dates[0].child"),
        ("", "    dates: [{parent: id, child: d}]\n", "dates[0].parent"),
        (
            "",
            "    dates: [{parent: a, child: d}, {parent: a, child: d}]\n",
            "names one entry twice",
        ),
        ("", "    dates: [{parent: a, child: d, x: 1}]\n", "unknown key"),
    ],
)
def test_a_malformed_date_ask_is_refused_naming_where(
    table: str, relationship: str, fragment: str
) -> None:
    assert fragment in _refusal(_text(table, relationship))


def test_a_good_1_1_0_document_reads() -> None:
    request = load_request_text(
        _text(
            "    datePairs: [{start: a, end: b}, {start: b, end: a}]\n",
            "    dates: [{parent: a, child: d}]\n",
        )
    )
    assert [(p.start, p.end) for p in request.table("p").date_pairs] == [("a", "b"), ("b", "a")]
    assert request.relationships[0].dates == (RequestDateLink("a", "d"),)


def test_the_lists_are_bounded() -> None:
    many = ", ".join(f"{{start: a, end: b{i}}}" for i in range(MOST_DATE_PAIRS + 1))
    assert "more than" in _refusal(_text(f"    datePairs: [{many}]\n"))
    request = _request()
    pairs = tuple(RequestDatePair("created", f"c{i}") for i in range(MOST_DATE_PAIRS + 1))
    table = RequestTable("customers", True, request.tables[0].columns, pairs)
    with pytest.raises(FluxError) as refused:
        check_request(ProfileRequest(None, 16, (table,)))
    assert "more than" in str(refused.value)


def test_check_request_refuses_a_pair_the_reader_would_refuse() -> None:
    request = _request()
    bad = RequestTable(
        "customers", True, request.tables[0].columns, (RequestDatePair("id", "closed"),)
    )
    with pytest.raises(FluxError) as refused:
        check_request(ProfileRequest(None, 16, (bad, request.tables[1])))
    assert "datePairs[0].start" in str(refused.value)
