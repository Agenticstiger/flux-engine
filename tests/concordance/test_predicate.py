# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §2: what ``fluxsim validate`` refuses, offline.

Every rule §2 names gets a test that it is enforced *and* a test that the
enforcement is not vacuous -- the paired positive case parses. A refusal
test that never had a passing counterpart proves only that the parser
raises somewhere.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Mapping

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.concordance.predicate import (
    ColumnKind,
    DeclaredExpose,
    FailureAction,
    NullPolicy,
    PropertyKind,
    ToleranceKind,
    ValueDomain,
    expose_from_contract,
    parse_duration_ms,
    parse_fidelity_block,
)
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from tests.concordance.conftest import build_expose, published_blocks

pytestmark = pytest.mark.unit


def _load_vectors() -> Dict[str, Any]:
    from tests.concordance.conftest import vectors_document

    return vectors_document()


#: The published vectors, loaded once for the property tests below --
#: hypothesis strategies are built at import time and cannot take a fixture.
_VECTORS = _load_vectors()


def _block(document: Mapping[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(dict(document["extension"]["fidelity"]))


def _parse(raw: Mapping[str, Any], expose: DeclaredExpose) -> Any:
    return parse_fidelity_block(raw, expose=expose)


def _refuses(raw: Mapping[str, Any], expose: DeclaredExpose, code: str) -> FluxError:
    with pytest.raises(FluxError) as caught:
        _parse(raw, expose)
    assert caught.value.code.code == code, caught.value
    return caught.value


# ---------------------------------------------------------------------------
# The positive case
# ---------------------------------------------------------------------------


def test_the_published_block_parses(block, document) -> None:
    declared = document["extension"]["fidelity"]
    assert len(block.properties) == len(declared["properties"])
    assert set(block.columns) == set(declared["columns"])
    assert block.failure_action is FailureAction.WITHHOLD_BUNDLE
    assert block.encoding.fractional_bits == declared["encoding"]["fractionalBits"]
    assert block.records.minimum == declared["records"]["min"]
    assert block.records.maximum == declared["records"]["max"]


def test_every_property_kind_the_spec_names_has_a_parsed_form() -> None:
    parsed = {
        declaration.kind
        for _, published in published_blocks(_VECTORS)
        for declaration in _parse(
            published["extension"]["fidelity"], build_expose(published)
        ).properties
    }
    assert parsed == set(PropertyKind), "the published blocks must exercise every property kind"


def test_columns_iterate_in_one_order_whatever_the_yaml_order_was(document, expose) -> None:
    raw = _block(document)
    reversed_columns = dict(reversed(list(raw["columns"].items())))
    assert list(reversed_columns) != list(raw["columns"])
    forward = _parse(raw, expose)
    backward = _parse({**raw, "columns": reversed_columns}, expose)
    assert list(forward.columns) == list(backward.columns)


def test_a_property_id_is_stable_and_unique(block) -> None:
    identifiers = [declaration.property_id for declaration in block.properties]
    assert len(set(identifiers)) == len(identifiers)
    assert identifiers == sorted(identifiers), "declaration order leads the id"


def test_columns_covered_names_every_column_a_property_reads(block) -> None:
    expected = set()
    for declaration in block.properties:
        expected.update(declaration.columns)
        if declaration.group_by is not None:
            expected.add(declaration.group_by)
    assert set(block.columns_covered) == expected
    assert list(block.columns_covered) == sorted(block.columns_covered)


# ---------------------------------------------------------------------------
# "every column and every groupBy is a member of the referenced expose schema"
# ---------------------------------------------------------------------------


def test_a_column_the_expose_does_not_declare_is_refused(document, expose) -> None:
    raw = _block(document)
    raw["columns"]["not_a_column"] = {"range": {"min": "0", "max": "1"}, "null": "exclude"}
    _refuses(raw, expose, "FLX-5031")


def test_a_group_by_the_block_does_not_declare_is_refused(document, expose) -> None:
    raw = _block(document)
    raw["properties"][0] = {**raw["properties"][0], "groupBy": "subscriber_id"}
    _refuses(raw, expose, "FLX-5034")


def test_a_property_over_a_column_the_block_does_not_declare_is_refused(document, expose) -> None:
    raw = _block(document)
    raw["properties"][0] = {**raw["properties"][0], "column": "subscriber_id"}
    _refuses(raw, expose, "FLX-5034")


def test_group_by_needs_a_support_column(document, expose) -> None:
    raw = _block(document)
    raw["properties"][0] = {**raw["properties"][0], "groupBy": "attempts"}
    error = _refuses(raw, expose, "FLX-5030")
    assert "support column" in error.detail


def test_group_by_may_not_repeat_the_property_column(document, expose) -> None:
    raw = _block(document)
    raw["properties"] = [
        {
            "property": "support",
            "column": "market",
            "groupBy": "market",
            "tolerance": {"type": "count", "max": 0},
        }
    ]
    _refuses(raw, expose, "FLX-5030")


# ---------------------------------------------------------------------------
# "bins are strictly increasing and partition the declared range"
# ---------------------------------------------------------------------------


def _histogram_index(raw: Mapping[str, Any]) -> int:
    for index, entry in enumerate(raw["properties"]):
        if entry["property"] == PropertyKind.MARGINAL_HISTOGRAM.value:
            return index
    raise AssertionError("the published block declares no histogram property")


def test_bins_that_do_not_increase_are_refused(document, expose) -> None:
    raw = _block(document)
    index = _histogram_index(raw)
    edges = list(raw["properties"][index]["bins"])
    edges[1], edges[2] = edges[2], edges[1]
    raw["properties"][index] = {**raw["properties"][index], "bins": edges}
    _refuses(raw, expose, "FLX-5032")


def test_bins_that_start_above_the_declared_minimum_are_refused(document, expose) -> None:
    raw = _block(document)
    index = _histogram_index(raw)
    edges = list(raw["properties"][index]["bins"])[1:]
    raw["properties"][index] = {**raw["properties"][index], "bins": edges}
    error = _refuses(raw, expose, "FLX-5032")
    assert "declared minimum and maximum" in error.detail


def test_bins_that_stop_below_the_declared_maximum_are_refused(document, expose) -> None:
    raw = _block(document)
    index = _histogram_index(raw)
    edges = list(raw["properties"][index]["bins"])[:-1]
    _refuses({**raw, "properties": _replace(raw, index, {"bins": edges})}, expose, "FLX-5032")


def test_a_single_bin_edge_is_refused(document, expose) -> None:
    raw = _block(document)
    index = _histogram_index(raw)
    _refuses({**raw, "properties": _replace(raw, index, {"bins": ["1"]})}, expose, "FLX-5032")


def _replace(raw: Mapping[str, Any], index: int, changes: Mapping[str, Any]) -> List[Any]:
    properties = [dict(entry) for entry in raw["properties"]]
    properties[index] = {**properties[index], **changes}
    return properties


# ---------------------------------------------------------------------------
# "a correlation property declares two columns and a minVariance"
# ---------------------------------------------------------------------------


def _correlation_index(raw: Mapping[str, Any]) -> int:
    for index, entry in enumerate(raw["properties"]):
        if entry["property"] == PropertyKind.CORRELATION.value:
            return index
    raise AssertionError("the published block declares no correlation property")


def test_a_correlation_with_one_column_is_refused(document, expose) -> None:
    raw = _block(document)
    index = _correlation_index(raw)
    columns = list(raw["properties"][index]["columns"])[:1]
    _refuses({**raw, "properties": _replace(raw, index, {"columns": columns})}, expose, "FLX-5033")


def test_a_correlation_with_two_of_the_same_column_is_refused(document, expose) -> None:
    raw = _block(document)
    index = _correlation_index(raw)
    first = raw["properties"][index]["columns"][0]
    _refuses(
        {**raw, "properties": _replace(raw, index, {"columns": [first, first]})},
        expose,
        "FLX-5033",
    )


def test_a_correlation_without_a_min_variance_is_refused(document, expose) -> None:
    raw = _block(document)
    index = _correlation_index(raw)
    properties = [dict(entry) for entry in raw["properties"]]
    properties[index].pop("minVariance")
    _refuses({**raw, "properties": properties}, expose, "FLX-5033")


def test_a_non_positive_min_variance_is_refused(document, expose) -> None:
    raw = _block(document)
    index = _correlation_index(raw)
    _refuses({**raw, "properties": _replace(raw, index, {"minVariance": "0"})}, expose, "FLX-5033")


def test_only_a_correlation_carries_a_min_variance(document, expose) -> None:
    raw = _block(document)
    _refuses({**raw, "properties": _replace(raw, 0, {"minVariance": "1"})}, expose, "FLX-5030")


# ---------------------------------------------------------------------------
# "every property's parameters are present"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dropped", ["property", "tolerance"])
def test_a_property_missing_a_universal_parameter_is_refused(document, expose, dropped) -> None:
    raw = _block(document)
    properties = [dict(entry) for entry in raw["properties"]]
    properties[0].pop(dropped)
    _refuses({**raw, "properties": properties}, expose, "FLX-5034")


def test_a_property_naming_no_column_is_refused(document, expose) -> None:
    raw = _block(document)
    properties = [dict(entry) for entry in raw["properties"]]
    properties[0].pop("column")
    _refuses({**raw, "properties": properties}, expose, "FLX-5034")


def test_a_histogram_without_bins_is_refused(document, expose) -> None:
    raw = _block(document)
    index = _histogram_index(raw)
    properties = [dict(entry) for entry in raw["properties"]]
    properties[index].pop("bins")
    _refuses({**raw, "properties": properties}, expose, "FLX-5034")


def test_a_quantile_without_a_level_is_refused(document, expose) -> None:
    raw = _block(document)
    properties = [
        entry for entry in raw["properties"] if entry["property"] == PropertyKind.QUANTILE.value
    ]
    assert properties, "the published block declares no quantile property"
    stripped = dict(properties[0])
    stripped.pop("level")
    _refuses({**raw, "properties": [stripped]}, expose, "FLX-5034")


@pytest.mark.parametrize("level", ["0", "1", "-0.5", "2"])
def test_a_quantile_level_outside_the_open_unit_interval_is_refused(
    document, expose, level
) -> None:
    raw = _block(document)
    properties = [
        dict(entry)
        for entry in raw["properties"]
        if entry["property"] == PropertyKind.QUANTILE.value
    ]
    properties[0]["level"] = level
    _refuses({**raw, "properties": properties}, expose, "FLX-5030")


@pytest.mark.parametrize("section", ["records", "columns", "properties", "budget"])
def test_a_block_missing_a_section_is_refused(document, expose, section) -> None:
    raw = _block(document)
    raw.pop(section)
    _refuses(raw, expose, "FLX-5034")


def test_an_empty_property_list_is_refused(document, expose) -> None:
    _refuses({**_block(document), "properties": []}, expose, "FLX-5034")


def test_an_empty_column_mapping_is_refused(document, expose) -> None:
    _refuses({**_block(document), "columns": {}}, expose, "FLX-5034")


# ---------------------------------------------------------------------------
# Closed sets and shapes
# ---------------------------------------------------------------------------


def test_an_unknown_property_name_is_refused(document, expose) -> None:
    raw = _block(document)
    _refuses({**raw, "properties": _replace(raw, 0, {"property": "median"})}, expose, "FLX-5030")


def test_an_unknown_key_anywhere_in_the_block_is_refused(document, expose) -> None:
    _refuses({**_block(document), "tolerances": {}}, expose, "FLX-5030")


def test_a_tolerance_a_property_cannot_consume_is_refused(document, expose) -> None:
    raw = _block(document)
    _refuses(
        {
            **raw,
            "properties": _replace(raw, 0, {"tolerance": {"type": "count", "max": 1}}),
        },
        expose,
        "FLX-5030",
    )


def test_a_distance_tolerance_needs_a_measure_with_a_comparison(document, expose) -> None:
    raw = _block(document)
    index = _histogram_index(raw)
    _refuses(
        {
            **raw,
            "properties": _replace(
                raw,
                index,
                {"tolerance": {"type": "distance", "measure": "hellinger", "max": "0.1"}},
            ),
        },
        expose,
        "FLX-5030",
    )


def test_an_absolute_tolerance_may_not_carry_a_max(document, expose) -> None:
    raw = _block(document)
    _refuses(
        {
            **raw,
            "properties": _replace(
                raw, 0, {"tolerance": {"type": "relative", "value": "0.1", "max": "0.1"}}
            ),
        },
        expose,
        "FLX-5030",
    )


def test_a_rounding_other_than_floor_is_refused(document, expose) -> None:
    raw = _block(document)
    error = _refuses({**raw, "encoding": {"rounding": "nearest"}}, expose, "FLX-5030")
    assert "-inf" in error.detail


def test_a_column_declaring_both_a_range_and_a_support_is_refused(document, expose) -> None:
    raw = _block(document)
    columns = dict(raw["columns"])
    columns["amount"] = {**columns["amount"], "support": "declared", "maxCardinality": 2}
    _refuses({**raw, "columns": columns}, expose, "FLX-5030")


def test_a_support_column_needs_a_max_cardinality(document, expose) -> None:
    raw = _block(document)
    columns = dict(raw["columns"])
    columns["market"] = {"support": "declared", "null": "fail"}
    _refuses({**raw, "columns": columns}, expose, "FLX-5034")


def test_an_inverted_range_is_refused(document, expose) -> None:
    raw = _block(document)
    columns = dict(raw["columns"])
    columns["amount"] = {**columns["amount"], "range": {"min": "8", "max": "1"}}
    _refuses({**raw, "columns": columns}, expose, "FLX-5030")


def test_records_max_below_records_min_is_refused(document, expose) -> None:
    _refuses({**_block(document), "records": {"min": 10, "max": 4}}, expose, "FLX-5030")


def test_an_unknown_failure_action_is_refused(document, expose) -> None:
    _refuses({**_block(document), "failureAction": "warn"}, expose, "FLX-5030")


# ---------------------------------------------------------------------------
# "Numbers are strings (exact decimals), never floats"
# ---------------------------------------------------------------------------


def test_a_float_tolerance_is_refused(document, expose) -> None:
    raw = _block(document)
    error = _refuses(
        {**raw, "properties": _replace(raw, 0, {"tolerance": {"type": "relative", "value": 0.25}})},
        expose,
        "FLX-5030",
    )
    assert "float" in error.detail


def test_a_float_range_bound_is_refused(document, expose) -> None:
    raw = _block(document)
    columns = dict(raw["columns"])
    columns["amount"] = {**columns["amount"], "range": {"min": 1.0, "max": "8"}}
    _refuses({**raw, "columns": columns}, expose, "FLX-5030")


#: Written out without an exponent, a number may have this many digits at
#: most, not counting a leading ``0.``: jackson-core's default longest number.
_MOST_DIGITS = 1000


def _with_bound(document: Mapping[str, Any], column: str, bound: str, value: Any) -> Dict[str, Any]:
    raw = _block(document)
    raw["columns"][column]["range"][bound] = value
    return raw


def _too_long(bound: str, needed: int) -> str:
    return (
        f"fidelity.columns.amount.range.{bound}: written out, this number needs {needed} "
        f"digits, and a number in a fidelity block may need at most {_MOST_DIGITS}"
    )


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    ("bound", "value", "detail"),
    [
        pytest.param("max", "1E+99999999", _too_long("max", 10**8), id="an-exponent-of-1e8"),
        pytest.param("min", "1E-99999999", _too_long("min", 10**8 - 1), id="a-negative-exponent"),
        pytest.param("max", "1" * 1001, _too_long("max", 1001), id="one-digit-too-many"),
        pytest.param("max", "0." + "1" * 1001, _too_long("max", 1001), id="one-decimal-too-many"),
        pytest.param("max", "1" * 500 + "." + "1" * 501, _too_long("max", 1001), id="mixed"),
        pytest.param("max", "1E+1000", _too_long("max", 1001), id="one-exponent-step-too-many"),
        pytest.param("max", Decimal("1E+99999999"), _too_long("max", 10**8), id="a-decimal-object"),
        pytest.param(
            "max",
            Decimal("NaN"),
            "fidelity.columns.amount.range.max: a non-finite decimal has no value",
            id="a-non-finite-decimal-object",
        ),
    ],
)
def test_a_number_too_long_to_form_is_refused_before_it_is_formed(
    document, expose, bound: str, value: Any, detail: str
) -> None:
    """A short spelling can name a huge exact value: ``1E+99999999`` has a
    hundred million digits, and forming it as a ``Fraction`` took minutes
    before any rule could refuse it. It is refused at once, by its length,
    whether it arrives as a string or as a ``Decimal``."""
    error = _refuses(_with_bound(document, "amount", bound, value), expose, "FLX-5030")
    assert error.detail == detail


@pytest.mark.parametrize(
    ("bound", "value"),
    [
        ("max", "1" * _MOST_DIGITS),
        ("max", f"1E+{_MOST_DIGITS - 1}"),
        ("max", "0." + "1" * _MOST_DIGITS),
        ("min", "0E+99999999"),
    ],
)
def test_a_number_of_at_most_the_most_digits_still_parses(
    document, expose, bound: str, value: str
) -> None:
    """A fraction counts the digits after its point once, and zero is one
    digit whatever its exponent: both are what the number writes out."""
    interval = (
        _parse(_with_bound(document, "attempts", bound, value), expose).column("attempts").interval
    )
    assert (interval.lo if bound == "min" else interval.hi) == Fraction(Decimal(value))


def test_an_integer_bound_is_read_as_its_exact_decimal(document, expose) -> None:
    raw = _block(document)
    columns = dict(raw["columns"])
    columns["amount"] = {**columns["amount"], "range": {"min": 1, "max": 8}}
    parsed = _parse({**raw, "columns": columns}, expose)
    assert parsed.column("amount").interval.lo == Fraction(1)
    assert parsed.column("amount").interval.hi == Fraction(8)


# ---------------------------------------------------------------------------
# The timestamp / decimal range reading
# ---------------------------------------------------------------------------


def test_a_timestamp_range_is_read_as_integer_milliseconds(block, document) -> None:
    declared = document["extension"]["fidelity"]["columns"]["aborted_at"]["range"]
    column = block.column("aborted_at")
    assert column.domain is ValueDomain.TIMESTAMP
    assert column.interval.lo == Fraction(parse_rfc3339_ms(declared["min"]))
    assert column.interval.hi == Fraction(parse_rfc3339_ms(declared["max"]))


def test_a_decimal_range_stays_in_the_number_domain(block) -> None:
    assert block.column("amount").domain is ValueDomain.NUMBER


def test_a_range_mixing_a_decimal_and_a_timestamp_bound_is_refused(document, expose) -> None:
    raw = _block(document)
    columns = dict(raw["columns"])
    columns["amount"] = {
        **columns["amount"],
        "range": {"min": "1", "max": "2026-07-01T00:00:00Z"},
    }
    _refuses({**raw, "columns": columns}, expose, "FLX-5030")


def test_a_bound_that_is_neither_a_decimal_nor_a_timestamp_is_refused(document, expose) -> None:
    raw = _block(document)
    columns = dict(raw["columns"])
    columns["amount"] = {**columns["amount"], "range": {"min": "yesterday", "max": "8"}}
    with pytest.raises(FluxError) as caught:
        _parse({**raw, "columns": columns}, expose)
    assert caught.value.code.code == "FLX-5022"


def test_a_support_column_has_no_interval(block) -> None:
    with pytest.raises(FluxError) as caught:
        _ = block.column("market").interval
    assert caught.value.code.code == "FLX-5034"


def test_an_undeclared_column_lookup_is_refused(block) -> None:
    with pytest.raises(FluxError) as caught:
        block.column("subscriber_id")
    assert caught.value.code.code == "FLX-5034"


# ---------------------------------------------------------------------------
# maxStateAge
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_ms"),
    [
        ("P30D", 30 * 24 * 60 * 60 * 1000),
        ("P1W", 7 * 24 * 60 * 60 * 1000),
        ("PT1H", 60 * 60 * 1000),
        ("PT90S", 90_000),
        ("P1DT2H3M4S", ((24 + 2) * 3600 + 3 * 60 + 4) * 1000),
        ("PT0.5S", 500),
    ],
)
def test_a_duration_reads_as_integer_milliseconds(text, expected_ms) -> None:
    assert parse_duration_ms(text) == expected_ms


@pytest.mark.parametrize(
    "text",
    ["30D", "P", "P1Y", "P1M", "P1H", "PT1D", "P1D1D", "PT1", "P0D", "PT0.0001S", "PTT1S"],
)
def test_a_duration_the_gate_cannot_compare_is_refused(text) -> None:
    with pytest.raises(FluxError) as caught:
        parse_duration_ms(text)
    assert caught.value.code.code == "FLX-5030"


def test_a_month_long_max_state_age_names_why_it_is_refused(document, expose) -> None:
    raw = _block(document)
    error = _refuses({**raw, "budget": {**raw["budget"], "maxStateAge": "P1M"}}, expose, "FLX-5030")
    assert "fixed length" in error.detail


def test_a_block_without_a_max_state_age_parses(document, expose) -> None:
    raw = _block(document)
    parsed = _parse({**raw, "budget": {"maxChecksPerState": 1}}, expose)
    assert parsed.budget.max_state_age_ms is None
    assert parsed.budget.max_state_age_declared is None


# ---------------------------------------------------------------------------
# Reading an expose out of a contract
# ---------------------------------------------------------------------------


def test_an_expose_is_read_in_the_contract_s_declared_column_order(document) -> None:
    read = expose_from_contract(document["contract"], document["expose"]["exposeId"])
    declared = document["contract"]["exposes"][0]["contract"]["schema"]
    assert list(read.column_names) == [column["name"] for column in declared]
    assert read.column_types["amount"] == "DECIMAL(18,4)"


def test_an_absent_expose_is_refused(document) -> None:
    with pytest.raises(FluxError) as caught:
        expose_from_contract(document["contract"], "no_such_expose")
    assert caught.value.code.code == "FLX-5030"


def test_a_contract_with_no_exposes_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        expose_from_contract({"kind": "DataProduct"}, "anything")
    assert caught.value.code.code == "FLX-5030"


def test_an_unnamed_column_is_refused() -> None:
    contract = {"exposes": [{"exposeId": "e", "contract": {"schema": [{"type": "STRING"}]}}]}
    with pytest.raises(FluxError) as caught:
        expose_from_contract(contract, "e")
    assert caught.value.code.code == "FLX-5030"


def test_an_expose_with_no_schema_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        expose_from_contract({"exposes": [{"exposeId": "e", "contract": {}}]}, "e")
    assert caught.value.code.code == "FLX-5030"


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@given(
    days=st.integers(min_value=1, max_value=3650),
    hours=st.integers(min_value=0, max_value=23),
    minutes=st.integers(min_value=0, max_value=59),
)
@pytest.mark.property
def test_a_duration_is_the_sum_of_its_parts(days, hours, minutes) -> None:
    text = f"P{days}D" + (f"T{hours}H{minutes}M" if hours or minutes else "")
    expected = ((days * 24 + hours) * 60 + minutes) * 60 * 1000
    assert parse_duration_ms(text) == expected


@given(digits=st.integers(min_value=1, max_value=40))
@pytest.mark.property
def test_a_duration_longer_than_the_decimal_context_is_still_exact(digits) -> None:
    """A ``Decimal`` product rounds at 28 significant digits; this must not.

    ``maxStateAge`` is compared against a committed state's age in
    milliseconds (SEAM-FIDELITY-SPEC §7), so a quantity whose scaled
    value the arithmetic context would round is a declaration read as a
    different maximum age from the one it spells. The reference is
    integer arithmetic over the same digits, never a typed constant.
    """
    quantity = int("9" * digits)
    assert parse_duration_ms(f"P{quantity}D") == quantity * 24 * 60 * 60 * 1000


def test_the_longest_duration_a_block_may_declare_still_prints() -> None:
    """A ``maxStateAge`` quantity as long as a number may be, in its largest
    unit, is 1009 digits of milliseconds: inside CPython's 4300-digit limit
    on printing an ``int``, which the gate's messages would otherwise trip."""
    assert len(str(parse_duration_ms("P" + "9" * _MOST_DIGITS + "W"))) == _MOST_DIGITS + 9
    with pytest.raises(FluxError) as caught:
        parse_duration_ms("P" + "9" * (_MOST_DIGITS + 1) + "W")
    assert caught.value.code.code == "FLX-5030"


@pytest.mark.parametrize("seconds", ["0.5", "0.0005", "0.1", "0.001"])
def test_a_duration_is_a_whole_number_of_milliseconds_or_it_is_refused(seconds) -> None:
    """The paired case: exactness cuts both ways.

    The expectation is derived from the same exact arithmetic the parser
    is required to do, so the split between the accepted and the refused
    spellings is computed rather than asserted from a list.
    """
    milliseconds = Fraction(Decimal(seconds)) * 1000
    if milliseconds.denominator == 1:
        assert parse_duration_ms(f"PT{seconds}S") == int(milliseconds)
        return
    with pytest.raises(FluxError) as caught:
        parse_duration_ms(f"PT{seconds}S")
    assert caught.value.code.code == "FLX-5030"


@given(st.permutations(sorted(_VECTORS["extension"]["fidelity"]["columns"])))
@pytest.mark.property
def test_parsing_is_a_pure_function_of_the_declaration(order) -> None:
    """Two parses of one block build one object, whatever order keys arrive in.

    The block is the published one with its ``columns`` mapping rebuilt in
    a permuted order. Column iteration and property ids must not move: a
    loader that happened to yield keys in a different order must not be
    able to change a digest taken over the parsed result.
    """
    raw = copy.deepcopy(dict(_VECTORS["extension"]["fidelity"]))
    permuted = {name: raw["columns"][name] for name in order}
    expose = DeclaredExpose(
        expose_id=_VECTORS["expose"]["exposeId"],
        column_names=tuple(_VECTORS["expose"]["columns"]),
    )
    reference = parse_fidelity_block(raw, expose=expose)
    permuted_block = parse_fidelity_block({**raw, "columns": permuted}, expose=expose)
    assert list(permuted_block.columns) == list(reference.columns)
    assert [p.property_id for p in permuted_block.properties] == [
        p.property_id for p in reference.properties
    ]
    assert permuted_block.columns_covered == reference.columns_covered


# ---------------------------------------------------------------------------
# The closed sets are the ones the spec names
# ---------------------------------------------------------------------------


def test_the_closed_sets_have_exactly_the_members_the_spec_names() -> None:
    assert {member.value for member in NullPolicy} == {"exclude", "fail"}
    assert {member.value for member in ColumnKind} == {"range", "support"}
    assert {member.value for member in ValueDomain} == {"number", "timestamp"}
    assert {member.value for member in FailureAction} == {"withholdBundle"}
    assert {member.value for member in ToleranceKind} == {
        "absolute",
        "relative",
        "distance",
        "count",
    }
    assert {member.value for member in PropertyKind} == {
        "mean",
        "marginalHistogram",
        "support",
        "correlation",
        "quantile",
        "levelHistogram",
    }


def test_a_decimal_declared_value_is_accepted_without_a_float_detour(document, expose) -> None:
    raw = _block(document)
    parsed = _parse(
        {
            **raw,
            "properties": _replace(
                raw, 0, {"tolerance": {"type": "relative", "value": Decimal("0.25")}}
            ),
        },
        expose,
    )
    assert parsed.properties[0].tolerance.magnitude == Fraction(1, 4)
