# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The calendar of a timestamp column and the exposure of a parent: profile 1-3-0 and request 1-1-0.

Profile format 1-3-0 holds ``tables.<id>.calendar`` and ``relationships[].exposure``
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
    CALENDAR_LEVELS,
    MAX_CALENDAR_ENTRIES,
    MAX_EXPOSURES,
    SCHEMA_URL_1_2_0,
    load_profile_text,
    parse_profile,
)
from flux_stats.request import (  # noqa: E402
    MOST_CALENDAR_COLUMNS,
    MOST_EXPOSURES,
    REQUEST_SCHEMA_URL,
    REQUEST_SCHEMA_URL_1_1_0,
    ProfileRequest,
    RequestColumn,
    RequestEnd,
    RequestExposure,
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


def _edit(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    document = copy.deepcopy(EXAMPLE)
    change(document)
    return document


def _calendar(document: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    return document["tables"]["payment"]["calendar"][index]


def _exposure(document: Dict[str, Any]) -> Dict[str, Any]:
    return document["relationships"][0]["exposure"][0]


def _refused(document: Dict[str, Any]) -> str:
    with pytest.raises(FluxError) as refused:
        parse_profile(document)
    assert refused.value.code.code == "FLX-5036"
    return str(refused.value)


# -- the profile reads them ---------------------------------------------------------------------


def test_the_example_validates_and_reads_both_members() -> None:
    assert not list(VALIDATOR.iter_errors(EXAMPLE))
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    full, partial = profile.tables["payment"].calendar
    assert full.column == "paid_at"
    assert full.month is not None and full.month.levels == CALENDAR_LEVELS["month"]
    assert full.day_of_week is not None and full.day_of_week.levels == tuple("1234567")
    assert full.hour_of_week is not None and len(full.hour_of_week.levels) == 168
    assert partial.column == "settled_at"
    assert partial.day_of_week is None and partial.hour_of_week is None
    (exposure,) = profile.relationships[0].exposure
    assert (exposure.start, exposure.end) == ("joined_at", None)
    assert exposure.children.value == 5400 and exposure.active_days.value == 1051200
    assert (exposure.window_start, exposure.window_end) == (1704067200000, 1735689600000)
    assert profile.tables["subscriber"].calendar == ()


def test_the_levels_sort_in_code_point_order_as_their_numbers_do() -> None:
    for levels in CALENDAR_LEVELS.values():
        assert list(levels) == sorted(levels)
    assert CALENDAR_LEVELS["month"][0] == "01" and CALENDAR_LEVELS["month"][-1] == "12"
    assert CALENDAR_LEVELS["dayOfWeek"] == ("1", "2", "3", "4", "5", "6", "7")
    assert CALENDAR_LEVELS["hourOfWeek"][0] == "000" and CALENDAR_LEVELS["hourOfWeek"][-1] == "167"


def test_a_profile_without_them_has_empty_tuples() -> None:
    def change(document: Dict[str, Any]) -> None:
        document["tables"]["payment"].pop("calendar")
        document["relationships"][0].pop("exposure")

    document = _edit(change)
    profile = parse_profile(document)
    assert profile.tables["payment"].calendar == ()
    assert profile.relationships[0].exposure == ()


def test_a_suppressed_calendar_cell_reads_as_unknown() -> None:
    profile = parse_profile(copy.deepcopy(EXAMPLE))
    group = profile.tables["payment"].calendar[1].month.groups[0]  # type: ignore[union-attr]
    assert group.suppressed_cells == (2,) and group.cell_status[2] == "A"
    assert group.statistic is not None and group.statistic.level_counts[2] == 0


def test_a_suppressed_count_reads_as_unknown() -> None:
    def withhold(document: Dict[str, Any]) -> None:
        _exposure(document)["children"] = {
            "definition": "count/1",
            "status": "A",
            "sampleSize": {"min": 0, "max": 9},
            "disclosure": "acro-default",
        }

    (exposure,) = parse_profile(_edit(withhold)).relationships[0].exposure
    assert exposure.children.value is None and exposure.children.status == "A"
    assert exposure.active_days.value == 1051200


def test_an_exposure_may_name_an_end_column() -> None:
    def add_end(document: Dict[str, Any]) -> None:
        document["tables"]["subscriber"]["columns"]["left_at"] = {"domain": "timestamp"}
        _exposure(document)["end"] = "left_at"

    (exposure,) = parse_profile(_edit(add_end)).relationships[0].exposure
    assert (exposure.start, exposure.end) == ("joined_at", "left_at")


def test_a_calendar_histogram_follows_the_disclosure_and_noise_rules_of_a_statistic() -> None:
    def unknown_rule(document: Dict[str, Any]) -> None:
        _calendar(document)["month"]["disclosure"] = "nope"

    assert "disclosure" in _refused(_edit(unknown_rule))


# -- refusals -----------------------------------------------------------------------------------


def _set(target: str, key: str, value: Any) -> Callable[[Dict[str, Any]], None]:
    def change(document: Dict[str, Any]) -> None:
        holder = _calendar(document) if target == "calendar" else _exposure(document)
        holder[key] = value

    return change


def _level(key: str, levels: list) -> Callable[[Dict[str, Any]], None]:
    def change(document: Dict[str, Any]) -> None:
        _calendar(document)[key]["levels"] = levels

    return change


def _missing_level(document: Dict[str, Any]) -> None:
    _calendar(document)["dayOfWeek"]["levels"] = list("123456")


def _unpadded_months(document: Dict[str, Any]) -> None:
    _calendar(document)["month"]["levels"] = [str(m) for m in range(1, 13)]


def _sunday_zero(document: Dict[str, Any]) -> None:
    _calendar(document)["dayOfWeek"]["levels"] = list("0123456")


def _no_histogram(document: Dict[str, Any]) -> None:
    for key in ("month", "dayOfWeek", "hourOfWeek"):
        _calendar(document).pop(key)


def _grouped(document: Dict[str, Any]) -> None:
    _calendar(document)["month"]["groupBy"] = "segment"


def _duplicate_column(document: Dict[str, Any]) -> None:
    document["tables"]["payment"]["calendar"][1]["column"] = "paid_at"


def _duplicate_exposure(document: Dict[str, Any]) -> None:
    document["relationships"][0]["exposure"].append(copy.deepcopy(_exposure(document)))


def _too_many_calendar(document: Dict[str, Any]) -> None:
    document["tables"]["payment"]["calendar"] = [_calendar(document)] * (MAX_CALENDAR_ENTRIES + 1)


def _too_many_exposures(document: Dict[str, Any]) -> None:
    document["relationships"][0]["exposure"] = [_exposure(document)] * (MAX_EXPOSURES + 1)


def _inverted_window(document: Dict[str, Any]) -> None:
    _exposure(document)["window"] = {"start": "2", "end": "1"}


def _float_count(document: Dict[str, Any]) -> None:
    _exposure(document)["activeDays"]["value"] = 1.5


def _wrong_metric(document: Dict[str, Any]) -> None:
    _exposure(document)["children"]["definition"] = "nullCount/1"


REFUSALS = [
    pytest.param(_set("calendar", "extra", 1), "extra", id="unknown-key-in-a-calendar-entry"),
    pytest.param(_set("exposure", "extra", 1), "extra", id="unknown-key-in-an-exposure"),
    pytest.param(_set("calendar", "column", "nope"), "calendar[0].column", id="not-a-column"),
    pytest.param(_set("calendar", "column", "amount"), "calendar[0].column", id="a-number-column"),
    pytest.param(_set("calendar", "column", "segment"), "calendar[0].column", id="a-category"),
    pytest.param(_level("month", ["a", "b"]), "calendar[0].month.levels", id="month-other-levels"),
    pytest.param(_unpadded_months, "calendar[0].month.levels", id="month-without-leading-zeros"),
    pytest.param(_missing_level, "calendar[0].dayOfWeek.levels", id="a-level-missing"),
    pytest.param(_sunday_zero, "calendar[0].dayOfWeek.levels", id="week-from-sunday-zero"),
    pytest.param(_level("hourOfWeek", ["0", "1"]), "hourOfWeek.levels", id="hour-other-levels"),
    pytest.param(_no_histogram, "at least one", id="no-histogram-given"),
    pytest.param(_grouped, "groupBy", id="grouped-histogram"),
    pytest.param(_duplicate_column, "names one column twice", id="one-column-twice"),
    pytest.param(_duplicate_exposure, "names one pair twice", id="one-pair-twice"),
    pytest.param(_too_many_calendar, "holds at most", id="too-many-calendar-entries"),
    pytest.param(_too_many_exposures, "holds at most", id="too-many-exposures"),
    pytest.param(_set("exposure", "start", "nope"), "exposure[0].start", id="start-not-a-column"),
    pytest.param(
        _set("exposure", "start", "tenure_months"), "exposure[0].start", id="start-a-number"
    ),
    pytest.param(_set("exposure", "end", "paid_at"), "exposure[0].end", id="end-of-the-child"),
    pytest.param(_set("exposure", "end", "joined_at"), "two different columns", id="end-is-start"),
    pytest.param(_set("exposure", "window", {"start": "1"}), "window", id="window-without-end"),
    pytest.param(
        _set("exposure", "window", {"start": "1", "end": 2}), "window.end", id="window-int"
    ),
    pytest.param(_inverted_window, "does not start after", id="window-inverted"),
    pytest.param(_float_count, "count", id="a-float-count"),
    pytest.param(_wrong_metric, "definition", id="not-a-count-metric"),
]


@pytest.mark.parametrize(("change", "fragment"), REFUSALS)
def test_a_broken_member_is_refused_naming_where(
    change: Callable[[Dict[str, Any]], None], fragment: str
) -> None:
    assert fragment in _refused(_edit(change))


def _emptied(document: Dict[str, Any]) -> None:
    """The first calendar entry without its histograms: the entry a release step leaves."""
    document["tables"]["payment"]["calendar"][0] = {"column": "paid_at"}


def test_an_entry_with_no_histogram_is_refused_by_default() -> None:
    assert "at least one of its three histograms" in _refused(_edit(_emptied))
    with pytest.raises(FluxError) as refused:
        parse_profile(_edit(_emptied), allow_emptied_entries=False)
    assert refused.value.code.code == "FLX-5036"


def test_an_entry_with_no_histogram_is_kept_in_place_when_the_reader_accepts_it() -> None:
    profile = parse_profile(_edit(_emptied), allow_emptied_entries=True)
    empty, partial = profile.tables["payment"].calendar
    assert empty.column == "paid_at"
    assert (empty.month, empty.day_of_week, empty.hour_of_week) == (None, None, None)
    # The entries after it keep their place, and what they hold.
    assert partial.column == "settled_at" and partial.month is not None


def test_loading_the_text_passes_the_option_on() -> None:
    text = json.dumps(_edit(_emptied))
    with pytest.raises(FluxError):
        load_profile_text(text)
    profile, document = load_profile_text(text, allow_emptied_entries=True)
    assert document["tables"]["payment"]["calendar"][0] == {"column": "paid_at"}
    assert profile.tables["payment"].calendar[0].month is None


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        pytest.param(_set("calendar", "extra", 1), "extra", id="unknown-key"),
        pytest.param(_set("calendar", "column", "amount"), "calendar[0].column", id="not-a-time"),
        pytest.param(_duplicate_column, "names one column twice", id="one-column-twice"),
        pytest.param(_level("month", ["a", "b"]), "calendar[0].month.levels", id="other-levels"),
    ],
)
def test_the_option_loosens_no_other_rule_of_a_calendar_entry(
    change: Callable[[Dict[str, Any]], None], fragment: str
) -> None:
    with pytest.raises(FluxError) as refused:
        parse_profile(_edit(change), allow_emptied_entries=True)
    assert fragment in str(refused.value)


def test_an_empty_entry_still_names_a_timestamp_column_once() -> None:
    def change(document: Dict[str, Any]) -> None:
        _emptied(document)
        document["tables"]["payment"]["calendar"][1] = {"column": "paid_at"}

    with pytest.raises(FluxError, match="names one column twice"):
        parse_profile(_edit(change), allow_emptied_entries=True)

    def not_a_timestamp(document: Dict[str, Any]) -> None:
        document["tables"]["payment"]["calendar"][0] = {"column": "amount"}

    with pytest.raises(FluxError, match=r"calendar\[0\]\.column"):
        parse_profile(_edit(not_a_timestamp), allow_emptied_entries=True)


def test_the_schema_refuses_a_malformed_member_too() -> None:
    bad = [
        _set("calendar", "extra", 1),
        _set("exposure", "extra", 1),
        _level("month", ["a", "b"]),
        _unpadded_months,
        _missing_level,
        _no_histogram,
        _float_count,
        _set("exposure", "window", {"start": "1"}),
        lambda d: _exposure(d).pop("children"),
        lambda d: _calendar(d).pop("column"),
    ]
    for change in bad:
        assert list(VALIDATOR.iter_errors(_edit(change)))


def _without_other_1_3_forms(document: Dict[str, Any]) -> None:
    document.pop("references")
    document["relationships"][0].pop("structural", None)
    for table in document["tables"].values():
        table.pop("history", None)
    document["tables"]["payment"].pop("datePairs")
    document["relationships"][0].pop("dates")
    for table in document["tables"].values():
        for column in table["columns"].values():
            column["statistics"] = [
                statistic
                for statistic in column.get("statistics", [])
                if not {"groupByLink", "per"} & set(statistic)
                and statistic.get("groupBy") != "attempts"
            ]


@pytest.mark.parametrize("where", ["calendar", "exposure"])
def test_an_earlier_format_that_carries_a_member_is_refused(where: str) -> None:
    def change(document: Dict[str, Any]) -> None:
        _without_other_1_3_forms(document)
        document["_schemaURL"] = SCHEMA_URL_1_2_0
        if where == "calendar":
            document["relationships"][0].pop("exposure")
        else:
            document["tables"]["payment"].pop("calendar")

    message = _refused(_edit(change))
    assert "format 1-3-0" in message and where in message


def test_the_same_document_without_either_member_reads_as_an_earlier_format() -> None:
    def change(document: Dict[str, Any]) -> None:
        _without_other_1_3_forms(document)
        document["_schemaURL"] = SCHEMA_URL_1_2_0
        document["relationships"][0].pop("exposure")
        document["tables"]["payment"].pop("calendar")

    profile = parse_profile(_edit(change))
    assert profile.tables["payment"].calendar == ()
    assert profile.relationships[0].exposure == ()


# -- the request -------------------------------------------------------------------------------


def _request(*, calendar: bool = True, exposure: bool = True) -> ProfileRequest:
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
                calendar=("created", "closed") if calendar else (),
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
                exposure=(
                    (RequestExposure("created", "closed"), RequestExposure("closed"))
                    if exposure
                    else ()
                ),
            ),
        ),
        facts=(RequestFact("orders.placed_late", "orders", "placed_late"),),
    )


#: ``_request(calendar=False, exposure=False)`` as the writer wrote it before format 1-1-0 existed.
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


def test_a_request_that_asks_for_none_is_written_as_1_0_0_byte_for_byte() -> None:
    text = render_request(_request(calendar=False, exposure=False))
    assert text == BEFORE_1_1_0
    assert load_request_text(text) == _request(calendar=False, exposure=False)


def test_a_request_that_asks_for_either_is_written_as_1_1_0_and_read_back_equal() -> None:
    for request in (_request(), _request(exposure=False), _request(calendar=False)):
        text = render_request(request)
        assert REQUEST_SCHEMA_URL_1_1_0 in text and REQUEST_SCHEMA_URL not in text
        assert load_request_text(text) == request
        assert render_request(load_request_text(text)) == text
        check_request(request)
    document = request_document(_request())
    assert document["tables"]["customers"]["calendar"] == ["created", "closed"]
    assert document["relationships"][0]["exposure"] == [
        {"start": "created", "end": "closed"},
        {"start": "closed"},
    ]


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
    assert "calendar" in _refusal(_text("    calendar: [a]\n", url=old))
    assert "exposure" in _refusal(_text(extra_relationship="    exposure: [{start: a}]\n", url=old))


@pytest.mark.parametrize(
    ("table", "relationship", "fragment"),
    [
        ("    calendar: [id]\n", "", "calendar[0]"),
        ("    calendar: [zz]\n", "", "calendar[0]"),
        ("    calendar: [a, a]\n", "", "names one entry twice"),
        ("    calendar: [{a: 1}]\n", "", "expected a string"),
        ("", "    exposure: [{start: id}]\n", "exposure[0].start"),
        ("", "    exposure: [{start: d}]\n", "exposure[0].start"),
        ("", "    exposure: [{start: a, end: d}]\n", "exposure[0].end"),
        ("", "    exposure: [{start: a, end: a}]\n", "two different columns"),
        ("", "    exposure: [{start: a}, {start: a}]\n", "names one entry twice"),
        ("", "    exposure: [{start: a, end: b}, {start: a, end: b}]\n", "names one entry twice"),
        ("", "    exposure: [{end: b}]\n", "missing key"),
        ("", "    exposure: [{start: a, extra: 1}]\n", "unknown key"),
    ],
)
def test_a_malformed_ask_is_refused_naming_where(
    table: str, relationship: str, fragment: str
) -> None:
    assert fragment in _refusal(_text(table, relationship))


def test_a_good_1_1_0_document_reads() -> None:
    request = load_request_text(
        _text(
            "    calendar: [b, a]\n", "    exposure: [{start: a, end: b}, {start: a}, {start: b}]\n"
        )
    )
    table = request.table("p")
    assert table is not None and table.calendar == ("b", "a")
    assert request.relationships[0].exposure == (
        RequestExposure("a", "b"),
        RequestExposure("a"),
        RequestExposure("b"),
    )


def test_the_lists_are_bounded() -> None:
    many = ", ".join(f"c{i}" for i in range(MOST_CALENDAR_COLUMNS + 1))
    assert "more than" in _refusal(_text(f"    calendar: [{many}]\n"))
    many_exposures = ", ".join(f"{{start: a, end: b{i}}}" for i in range(MOST_EXPOSURES + 1))
    assert "more than" in _refusal(_text("", f"    exposure: [{many_exposures}]\n"))
    request = _request()
    table = RequestTable(
        "customers",
        True,
        request.tables[0].columns,
        calendar=tuple(f"c{i}" for i in range(MOST_CALENDAR_COLUMNS + 1)),
    )
    with pytest.raises(FluxError) as refused:
        check_request(ProfileRequest(None, 16, (table,)))
    assert "more than" in str(refused.value)


def test_check_request_refuses_a_column_the_reader_would_refuse() -> None:
    request = _request()
    bad = RequestTable("customers", True, request.tables[0].columns, calendar=("id",))
    with pytest.raises(FluxError) as refused:
        check_request(ProfileRequest(None, 16, (bad, request.tables[1])))
    assert "calendar[0]" in str(refused.value)
