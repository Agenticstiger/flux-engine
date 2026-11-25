# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The fidelity-block grammar and the per-group measurement, without the engine.

Everything here imports flux_stats and the standard library only, so it runs
in the job that installs this package alone.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List

import pytest

from flux_stats import fidelity
from flux_stats.errors import FluxError
from flux_stats.fidelity import (
    DeclaredExpose,
    FailureAction,
    FidelityBlock,
    PropertyKind,
    declared_enum_values,
    expose_from_contract,
    measure_group,
    parse_duration_ms,
    parse_fidelity_block,
    read_support_levels,
    require_number_length,
    split_type_params,
)

pytestmark = pytest.mark.unit

EXPOSE = DeclaredExpose(
    expose_id="recovery_moment",
    column_names=("subscriber_id", "aborted_at", "amount", "attempts", "market", "tier"),
    column_types={
        "subscriber_id": "STRING",
        "aborted_at": "TIMESTAMP",
        "amount": "DECIMAL(18,4)",
        "attempts": "INT",
        "market": "STRING",
        "tier": "enum('gold', 'basic')",
    },
)

BLOCK: Dict[str, Any] = {
    "encoding": {"fractionalBits": 4, "rounding": "floor"},
    "records": {"min": 2, "max": 8},
    "columns": {
        "amount": {"range": {"min": "1", "max": "8"}, "null": "exclude"},
        "attempts": {"range": {"min": "0", "max": "16"}, "null": "fail"},
        "market": {"support": {"values": ["ie", "uk"]}, "maxCardinality": 2, "null": "fail"},
        "tier": {"support": "declared", "maxCardinality": 4, "null": "fail"},
    },
    "properties": [
        {
            "property": "mean",
            "column": "amount",
            "tolerance": {"type": "relative", "value": "0.25"},
        },
        {
            "property": "marginalHistogram",
            "column": "amount",
            "bins": ["1", "2", "4", "8"],
            "tolerance": {"type": "distance", "measure": "totalVariation", "max": "0.2"},
        },
        {"property": "support", "column": "market", "tolerance": {"type": "count", "max": 0}},
        {
            "property": "mean",
            "column": "amount",
            "groupBy": "market",
            "tolerance": {"type": "absolute", "value": "2"},
        },
        {
            "property": "correlation",
            "columns": ["amount", "attempts"],
            "minVariance": "4",
            "tolerance": {"type": "absolute", "value": "0.5"},
        },
        {
            "property": "quantile",
            "column": "amount",
            "level": "0.5",
            "tolerance": {"type": "absolute", "value": "1"},
        },
        {
            "property": "levelHistogram",
            "column": "tier",
            "tolerance": {"type": "distance", "measure": "totalVariation", "max": "0.2"},
        },
        {"property": "rankCorrelation", "columns": ["amount", "attempts"], "measure": "kendall"},
    ],
    "budget": {"maxChecksPerState": 3, "maxStateAge": "P30D"},
    "failureAction": "withholdBundle",
}

ROWS: List[Dict[str, Any]] = [
    {"amount": "1", "attempts": "1", "market": "ie", "tier": "gold"},
    {"amount": "3", "attempts": "2", "market": "ie", "tier": "basic"},
    {"amount": "5", "attempts": "4", "market": "uk", "tier": "gold"},
    {"amount": "7", "attempts": "8", "market": "uk", "tier": "gold"},
]
EVERY = [0, 1, 2, 3]


def _block() -> Dict[str, Any]:
    return copy.deepcopy(BLOCK)


def _parse(raw: Dict[str, Any]) -> FidelityBlock:
    return parse_fidelity_block(raw, expose=EXPOSE)


def _refuses(raw: Dict[str, Any], code: str) -> FluxError:
    with pytest.raises(FluxError) as caught:
        _parse(raw)
    assert caught.value.code.code == code, caught.value
    return caught.value


def _property(block: FidelityBlock, kind: PropertyKind, group_by: Any = None) -> Any:
    for declaration in block.properties:
        if declaration.kind is kind and declaration.group_by == group_by:
            return declaration
    raise AssertionError(kind)


# ---------------------------------------------------------------------------
# The positive case
# ---------------------------------------------------------------------------


def test_a_well_formed_block_parses() -> None:
    block = _parse(_block())
    assert len(block.properties) == len(BLOCK["properties"])
    assert set(block.columns) == set(BLOCK["columns"])
    assert block.failure_action is FailureAction.WITHHOLD_BUNDLE
    assert block.encoding.fractional_bits == 4
    assert (block.records.minimum, block.records.maximum) == (2, 8)
    assert {declaration.kind for declaration in block.properties} == set(PropertyKind)


def test_no_declared_column_is_refused_unless_the_caller_allows_it() -> None:
    raw = {**_block(), "columns": {}, "properties": []}
    _refuses(raw, "FLX-5034")
    block = parse_fidelity_block(raw, expose=EXPOSE, allow_no_columns=True)
    assert block.columns == {} or len(block.columns) == 0
    # A property that names a column is still refused: no column is declared for it.
    with pytest.raises(FluxError):
        parse_fidelity_block({**_block(), "columns": {}}, expose=EXPOSE, allow_no_columns=True)


def test_an_empty_property_list_is_a_block_with_nothing_to_score() -> None:
    block = _parse({**_block(), "properties": []})
    assert block.properties == ()
    assert set(block.columns) == set(BLOCK["columns"])
    # Only an empty list: a non-list, and a missing section, are refused as they were.
    _refuses({**_block(), "properties": "mean"}, "FLX-5030")
    raw = _block()
    del raw["properties"]
    _refuses(raw, "FLX-5034")


def test_allocation_exact_is_a_column_key_the_declaration_carries() -> None:
    raw = _block()
    raw["columns"]["market"]["allocation"] = "exact"
    raw["columns"]["market"]["given"] = ["region", "tier"]
    declared = _parse(raw).columns["market"]
    plain = _parse(_block()).columns["market"]
    assert (declared.allocation, declared.given) == ("exact", ("region", "tier"))
    assert (plain.allocation, plain.given) == (None, ())
    assert replace(declared, allocation=None, given=()) == plain


def test_parsing_is_a_pure_function_of_the_declaration() -> None:
    raw = _block()
    reversed_columns = dict(reversed(list(raw["columns"].items())))
    assert _parse(raw) == _parse({**raw, "columns": reversed_columns})


def test_support_declared_reads_its_levels_from_the_enum_type() -> None:
    column = _parse(_block()).columns["tier"]
    assert column.support_values == ("gold", "basic")


def test_declared_enum_values_reads_the_types_own_list() -> None:
    assert declared_enum_values("enum('IE', \"GB\")") == ("IE", "GB")
    assert declared_enum_values("STRING") == ()
    assert declared_enum_values("enum()") == ()
    assert declared_enum_values(None) == ()


def test_declared_enum_values_refuses_a_repeated_or_empty_level() -> None:
    for declared in ("enum('a', 'a')", "enum('a', '')"):
        with pytest.raises(FluxError) as caught:
            declared_enum_values(declared)
        assert caught.value.code.code == "FLX-3001"


# ---------------------------------------------------------------------------
# Refusals, one class per code
# ---------------------------------------------------------------------------


def test_a_block_missing_a_section_is_refused() -> None:
    for section in ("records", "columns", "properties"):
        raw = _block()
        del raw[section]
        _refuses(raw, "FLX-5034")


def test_an_unknown_key_or_property_is_refused() -> None:
    _refuses({**_block(), "tolerances": {}}, "FLX-5030")
    raw = _block()
    raw["properties"][0]["property"] = "median"
    _refuses(raw, "FLX-5030")


def test_numbers_must_be_strings() -> None:
    raw = _block()
    raw["columns"]["amount"]["range"]["max"] = 8.5
    _refuses(raw, "FLX-5030")
    raw = _block()
    raw["properties"][0]["tolerance"]["value"] = 0.25
    _refuses(raw, "FLX-5030")


def test_a_column_the_expose_does_not_declare_is_refused() -> None:
    raw = _block()
    raw["columns"]["nowhere"] = {"range": {"min": "0", "max": "1"}, "null": "fail"}
    _refuses(raw, "FLX-5031")


def test_bins_must_partition_the_declared_range() -> None:
    for bins in (["1", "4", "2", "8"], ["2", "4", "8"], ["1", "2", "4"], ["1", "8"][:1]):
        raw = _block()
        raw["properties"][1]["bins"] = bins
        _refuses(raw, "FLX-5032")


_DATED = DeclaredExpose(
    expose_id="recovery_moment",
    column_names=("due_on", "aborted_at"),
    column_types={"due_on": "date", "aborted_at": "TIMESTAMP"},
)


def _dated_block(low: Any, high: Any, edges: List[Any]) -> Dict[str, Any]:
    return {
        "encoding": {"fractionalBits": 4, "rounding": "floor"},
        "records": {"min": 2, "max": 8},
        "columns": {"due_on": {"range": {"min": low, "max": high}, "null": "exclude"}},
        "properties": [
            {
                "property": "marginalHistogram",
                "column": "due_on",
                "bins": edges,
                "tolerance": {"type": "distance", "measure": "totalVariation", "max": "0.2"},
            }
        ],
        "budget": {"maxChecksPerState": 3, "maxStateAge": "P30D"},
        "failureAction": "withholdBundle",
    }


def test_a_date_column_reads_its_range_and_bins_as_dates_or_as_days() -> None:
    spelled = parse_fidelity_block(
        _dated_block("2026-01-01", "2026-12-31", ["2026-01-01", "2026-07-01", "2026-12-31"]),
        expose=_DATED,
    )
    counted = parse_fidelity_block(
        _dated_block(20454, "20818", [20454, 20635, 20818]), expose=_DATED
    )
    for block in (spelled, counted):
        column = block.columns["due_on"]
        assert column.domain is fidelity.ValueDomain.DATE
        assert (column.lower, column.upper) == (Fraction(20454), Fraction(20818))
    assert spelled.properties[0].bins == counted.properties[0].bins


def test_a_date_bound_that_is_no_date_is_refused_and_a_timestamp_is_not_a_date() -> None:
    for low in ("2026-02-30", "soon"):
        with pytest.raises(FluxError) as caught:
            parse_fidelity_block(
                _dated_block(low, "2026-12-31", ["2026-01-01", "2026-12-31"]), expose=_DATED
            )
        assert caught.value.code.code == "FLX-5030"
    # An instant and a day are two domains: a range cannot mix them.
    with pytest.raises(FluxError) as caught:
        parse_fidelity_block(
            _dated_block("2026-01-01T00:00:00Z", "2026-12-31", ["2026-01-01", "2026-12-31"]),
            expose=_DATED,
        )
    assert caught.value.code.code == "FLX-5030"
    with pytest.raises(FluxError) as caught:
        parse_fidelity_block(
            _dated_block("2026-01-01", "2026-12-31", ["2026-01-01", "2026-06-30", "2026-12-30"]),
            expose=_DATED,
        )
    assert caught.value.code.code == "FLX-5032"


def test_a_date_column_bounded_by_instants_reads_as_it_did_before_days() -> None:
    """A profiler's block, declared with RFC 3339 instants: the timestamp domain, in milliseconds."""
    block = parse_fidelity_block(
        _dated_block(
            "0001-01-01T00:00:00Z",
            "9999-12-31T23:59:59.999Z",
            ["0001-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "9999-12-31T23:59:59.999Z"],
        ),
        expose=_DATED,
    )
    column = block.columns["due_on"]
    assert column.domain is fidelity.ValueDomain.TIMESTAMP
    assert column.lower == Fraction(-62135596800000)


def test_a_column_that_is_not_declared_a_date_still_refuses_a_date() -> None:
    raw = _block()
    raw["columns"]["amount"]["range"] = {"min": "2026-01-01", "max": "2026-12-31"}
    _refuses(raw, "FLX-5022")


def test_a_correlation_is_declared_with_two_columns_and_a_variance_floor() -> None:
    raw = _block()
    raw["properties"][4]["columns"] = ["amount"]
    _refuses(raw, "FLX-5033")
    raw = _block()
    del raw["properties"][4]["minVariance"]
    _refuses(raw, "FLX-5033")


def test_a_parameter_the_property_needs_is_required() -> None:
    raw = _block()
    del raw["properties"][1]["bins"]
    _refuses(raw, "FLX-5034")
    raw = _block()
    del raw["properties"][5]["level"]
    _refuses(raw, "FLX-5034")
    raw = _block()
    del raw["columns"]["market"]["maxCardinality"]
    _refuses(raw, "FLX-5034")


def test_a_group_by_names_a_support_column() -> None:
    raw = _block()
    raw["properties"][3]["groupBy"] = "amount"
    _refuses(raw, "FLX-5030")
    raw = _block()
    raw["properties"][3]["column"] = "market"
    raw["properties"][3]["groupBy"] = "market"
    _refuses(raw, "FLX-5030")


def test_a_support_declared_column_over_a_type_with_no_levels_is_refused() -> None:
    raw = _block()
    raw["columns"]["market"]["support"] = "declared"
    _refuses(raw, "FLX-5034")


def test_a_duration_reads_as_whole_milliseconds() -> None:
    assert parse_duration_ms("PT1S") == 1000
    assert parse_duration_ms("P1DT1H") == 90_000_000
    with pytest.raises(FluxError):
        parse_duration_ms("P1M")


def test_an_expose_is_read_from_a_contract_in_declared_order() -> None:
    document = {
        "exposes": [
            {
                "exposeId": "e",
                "contract": {"schema": [{"name": "b", "type": "INT"}, {"name": "a"}]},
            }
        ]
    }
    expose = expose_from_contract(document, "e")
    assert expose.column_names == ("b", "a")
    with pytest.raises(FluxError) as caught:
        expose_from_contract(document, "absent")
    assert caught.value.code.code == "FLX-5030"


def test_the_codes_it_registers_are_in_the_catalog() -> None:
    from flux_stats.errors import is_registered

    for code in ("FLX-3001", "FLX-5030", "FLX-5032", "FLX-5033", "FLX-5034"):
        assert is_registered(code)
    assert fidelity.FLX_5030.code == "FLX-5030"


# ---------------------------------------------------------------------------
# measure_group: one group of one property, a kind at a time
# ---------------------------------------------------------------------------


def _measure(kind: PropertyKind, rows: Any = ROWS, indices: Any = EVERY) -> Any:
    block = _parse(_block())
    return measure_group(rows, indices, "all", _property(block, kind), block.columns, block.grid)


def test_measure_group_forms_the_mean_sums() -> None:
    group = _measure(PropertyKind.MEAN)
    assert group.k == 4
    # 1 + 3 + 5 + 7 = 16 on a grid of 2**4 units
    assert group.sum_x == 16 * 16


def test_measure_group_counts_a_histogram_on_the_edges_quantised_to_the_grid() -> None:
    group = _measure(PropertyKind.MARGINAL_HISTOGRAM)
    assert group.k == 4
    assert group.bin_counts == (1, 1, 2)


def test_measure_group_reads_a_quantile_witness() -> None:
    group = _measure(PropertyKind.QUANTILE)
    assert group.k == 4
    assert group.quantile_witness is not None


def test_measure_group_reads_a_correlation_from_pairs() -> None:
    group = _measure(PropertyKind.CORRELATION)
    assert group.k == 4
    assert group.sum_xy != 0


def test_measure_group_lists_the_support() -> None:
    group = _measure(PropertyKind.SUPPORT)
    assert group.support == ("ie", "uk")


def test_measure_group_refuses_support_beyond_the_declared_cardinality() -> None:
    rows = [{**row, "market": f"m{index}"} for index, row in enumerate(ROWS)]
    with pytest.raises(FluxError) as caught:
        _measure(PropertyKind.SUPPORT, rows)
    assert caught.value.code.code == "FLX-5005"
    assert "maxCardinality" in str(caught.value)


def test_measure_group_counts_each_declared_level() -> None:
    group = _measure(PropertyKind.LEVEL_HISTOGRAM)
    # declared levels, sorted: basic, gold
    assert group.level_counts == (1, 3)


def test_measure_group_refuses_a_level_outside_the_declared_support() -> None:
    rows = [{**ROWS[0], "tier": "platinum"}]
    with pytest.raises(FluxError) as caught:
        _measure(PropertyKind.LEVEL_HISTOGRAM, rows, [0])
    assert caught.value.code.code == "FLX-5005"
    assert "outside the declared support" in str(caught.value)


def test_measure_group_reads_only_the_indices_it_is_given() -> None:
    group = _measure(PropertyKind.MEAN, indices=[0, 1])
    assert group.k == 2
    assert group.sum_x == 4 * 16


def test_measure_group_refuses_a_value_outside_the_declared_range() -> None:
    with pytest.raises(FluxError) as caught:
        _measure(PropertyKind.MEAN, [{**ROWS[0], "amount": "99"}], [0])
    assert caught.value.code.code == "FLX-5005"


# ---------------------------------------------------------------------------
# Every refusal of the grammar, as an inline edit of the block above
# ---------------------------------------------------------------------------


def _set(path: List[Any], value: Any) -> Any:
    """An edit that sets the entry at *path* (deleting it when *value* is a marker)."""

    def edit(raw: Dict[str, Any]) -> None:
        node: Any = raw
        for step in path[:-1]:
            node = node[step]
        if value is _DELETE:
            del node[path[-1]]
        else:
            node[path[-1]] = value

    return edit


_DELETE = object()
_P = "properties"
#: ``(label, edit, code)``: the edit is applied to a fresh copy of the block.
REFUSALS = [
    ("block with no section at all", lambda raw: raw.clear(), "FLX-5034"),
    ("encoding is a list", _set(["encoding"], []), "FLX-5030"),
    ("encoding unknown key", _set(["encoding", "base"], 2), "FLX-5030"),
    ("fractionalBits is a string", _set(["encoding", "fractionalBits"], "4"), "FLX-5030"),
    ("fractionalBits is a bool", _set(["encoding", "fractionalBits"], True), "FLX-5030"),
    ("fractionalBits negative", _set(["encoding", "fractionalBits"], -1), "FLX-5030"),
    ("rounding other than floor", _set(["encoding", "rounding"], "ceiling"), "FLX-5030"),
    ("records is a string", _set(["records"], "4"), "FLX-5030"),
    ("records without max", _set(["records", "max"], _DELETE), "FLX-5034"),
    ("records min below one", _set(["records", "min"], 0), "FLX-5030"),
    ("records max below min", _set(["records", "max"], 1), "FLX-5030"),
    ("columns is empty", _set(["columns"], {}), "FLX-5034"),
    ("columns is a list", _set(["columns"], []), "FLX-5030"),
    ("properties is a string", _set([_P], "mean"), "FLX-5030"),
    ("a property is a string", _set([_P, 0], "mean"), "FLX-5030"),
    ("property names no property", _set([_P, 0, "property"], _DELETE), "FLX-5034"),
    ("property has an unknown key", _set([_P, 0, "weight"], 1), "FLX-5030"),
    ("property over an undeclared column", _set([_P, 0, "column"], "attempts2"), "FLX-5034"),
    ("mean over a support column", _set([_P, 0, "column"], "market"), "FLX-5030"),
    ("support over a range column", _set([_P, 2, "column"], "amount"), "FLX-5030"),
    ("column and columns together", _set([_P, 0, "columns"], ["amount"]), "FLX-5030"),
    ("columns is a string", _set([_P, 4, "columns"], "amount"), "FLX-5030"),
    ("correlation of one column twice", _set([_P, 4, "columns"], ["amount", "amount"]), "FLX-5033"),
    (
        "correlation of three columns",
        _set([_P, 4, "columns"], ["amount", "attempts", "amount"]),
        "FLX-5033",
    ),
    ("mean of two columns", _set([_P, 0, "column"], _DELETE), "FLX-5034"),
    ("correlation variance not positive", _set([_P, 4, "minVariance"], "0"), "FLX-5033"),
    ("minVariance on a mean", _set([_P, 0, "minVariance"], "1"), "FLX-5030"),
    ("groupBy undeclared", _set([_P, 3, "groupBy"], "nowhere"), "FLX-5034"),
    ("groupBy a range column", _set([_P, 3, "groupBy"], "attempts"), "FLX-5030"),
    ("property without a tolerance", _set([_P, 0, "tolerance"], _DELETE), "FLX-5034"),
    ("tolerance is a string", _set([_P, 0, "tolerance"], "5"), "FLX-5030"),
    ("tolerance unknown key", _set([_P, 0, "tolerance", "slack"], 1), "FLX-5030"),
    ("tolerance without a type", _set([_P, 0, "tolerance", "type"], _DELETE), "FLX-5034"),
    ("tolerance of an unknown type", _set([_P, 0, "tolerance", "type"], "fuzzy"), "FLX-5030"),
    ("tolerance of the wrong type", _set([_P, 0, "tolerance", "type"], "count"), "FLX-5030"),
    ("distance without a measure", _set([_P, 1, "tolerance", "measure"], _DELETE), "FLX-5034"),
    ("distance of an unknown measure", _set([_P, 1, "tolerance", "measure"], "emd"), "FLX-5030"),
    (
        "measure on an absolute tolerance",
        _set([_P, 3, "tolerance", "measure"], "totalVariation"),
        "FLX-5030",
    ),
    ("absolute without a value", _set([_P, 3, "tolerance", "value"], _DELETE), "FLX-5034"),
    ("absolute with a max", _set([_P, 3, "tolerance", "max"], "1"), "FLX-5030"),
    ("absolute value zero", _set([_P, 3, "tolerance", "value"], "0"), "FLX-5030"),
    ("absolute value not a number", _set([_P, 3, "tolerance", "value"], "many"), "FLX-5030"),
    ("count without a max", _set([_P, 2, "tolerance", "max"], _DELETE), "FLX-5034"),
    ("count with a value", _set([_P, 2, "tolerance", "value"], "1"), "FLX-5030"),
    ("count max a string", _set([_P, 2, "tolerance", "max"], "0"), "FLX-5030"),
    ("distance max zero", _set([_P, 1, "tolerance", "max"], "0"), "FLX-5030"),
    ("histogram bins a string", _set([_P, 1, "bins"], "1,2"), "FLX-5030"),
    (
        "histogram bins in another domain",
        _set([_P, 1, "bins"], ["2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z"]),
        "FLX-5032",
    ),
    ("histogram bins on the wrong edge", _set([_P, 1, "bins", 0], "0"), "FLX-5032"),
    ("bins on a mean", _set([_P, 0, "bins"], ["1", "8"]), "FLX-5030"),
    ("bins on a level histogram", _set([_P, 6, "bins"], ["a", "b"]), "FLX-5030"),
    ("quantile level at one", _set([_P, 5, "level"], "1"), "FLX-5030"),
    ("quantile level not a number", _set([_P, 5, "level"], "half"), "FLX-5030"),
    ("level on a mean", _set([_P, 0, "level"], "0.5"), "FLX-5030"),
    ("budget is a string", _set(["budget"], "3"), "FLX-5030"),
    ("budget unknown key", _set(["budget", "cap"], 1), "FLX-5030"),
    (
        "budget without maxChecksPerState",
        _set(["budget", "maxChecksPerState"], _DELETE),
        "FLX-5034",
    ),
    ("budget checks below one", _set(["budget", "maxChecksPerState"], 0), "FLX-5030"),
    ("maxStateAge not a duration", _set(["budget", "maxStateAge"], "30 days"), "FLX-5030"),
    ("unknown failure action", _set(["failureAction"], "shrug"), "FLX-5030"),
    (
        "column not in the expose",
        _set(["columns", "nowhere"], {"range": {"min": "0", "max": "1"}}),
        "FLX-5031",
    ),
    ("column is a string", _set(["columns", "amount"], "range"), "FLX-5030"),
    ("column unknown key", _set(["columns", "amount", "scale"], 2), "FLX-5030"),
    ("allocation other than exact", _set(["columns", "market", "allocation"], "even"), "FLX-5030"),
    ("allocation is a list", _set(["columns", "market", "allocation"], ["exact"]), "FLX-5030"),
    ("column unknown null policy", _set(["columns", "amount", "null"], "ignore"), "FLX-5030"),
    (
        "column with range and support",
        _set(["columns", "amount", "support"], {"values": ["a"]}),
        "FLX-5030",
    ),
    (
        "maxCardinality on a bare column",
        _set(["columns", "subscriber_id"], {"maxCardinality": 3}),
        "FLX-5030",
    ),
    ("range is a string", _set(["columns", "amount", "range"], "1-8"), "FLX-5030"),
    ("range unknown key", _set(["columns", "amount", "range", "step"], "1"), "FLX-5030"),
    ("range without max", _set(["columns", "amount", "range", "max"], _DELETE), "FLX-5034"),
    ("range inverted", _set(["columns", "amount", "range", "max"], "0"), "FLX-5030"),
    (
        "range mixing domains",
        _set(["columns", "amount", "range", "max"], "2026-07-01T00:00:00Z"),
        "FLX-5030",
    ),
    ("range bound a float", _set(["columns", "amount", "range", "max"], 8.5), "FLX-5030"),
    ("range bound a bool", _set(["columns", "amount", "range", "max"], True), "FLX-5030"),
    ("range bound a list", _set(["columns", "amount", "range", "max"], ["8"]), "FLX-5030"),
    ("range bound not finite", _set(["columns", "amount", "range", "max"], "Infinity"), "FLX-5030"),
    (
        "range bound neither number nor instant",
        _set(["columns", "amount", "range", "max"], "eight"),
        "FLX-5022",
    ),
    (
        "range bound too long",
        _set(["columns", "amount", "range", "max"], "1" + "0" * 1100),
        "FLX-5030",
    ),
    ("maxCardinality on a range", _set(["columns", "amount", "maxCardinality"], 3), "FLX-5030"),
    ("support is a number", _set(["columns", "market", "support"], 3), "FLX-5030"),
    ("support is a bare list", _set(["columns", "market", "support"], ["ie", "uk"]), "FLX-5030"),
    ("support is another word", _set(["columns", "market", "support"], "listed"), "FLX-5030"),
    ("support unknown key", _set(["columns", "market", "support", "names"], ["x"]), "FLX-5030"),
    ("support without values", _set(["columns", "market", "support"], {}), "FLX-5034"),
    ("support values a string", _set(["columns", "market", "support", "values"], "ie"), "FLX-5030"),
    ("support values empty", _set(["columns", "market", "support", "values"], []), "FLX-5034"),
    (
        "support level not a string",
        _set(["columns", "market", "support", "values"], ["ie", 3]),
        "FLX-5030",
    ),
    (
        "support level repeated",
        _set(["columns", "market", "support", "values"], ["ie", "ie"]),
        "FLX-5030",
    ),
    (
        "support level repeated after NFC",
        _set(["columns", "market", "support", "values"], ["é", "é"]),
        "FLX-5030",
    ),
    (
        "support without maxCardinality",
        _set(["columns", "tier", "maxCardinality"], _DELETE),
        "FLX-5034",
    ),
    ("maxCardinality zero", _set(["columns", "market", "maxCardinality"], 0), "FLX-5030"),
    (
        "more levels than maxCardinality",
        _set(["columns", "market", "maxCardinality"], 1),
        "FLX-5030",
    ),
]


@pytest.mark.parametrize("label, edit, code", REFUSALS, ids=[label for label, _, _ in REFUSALS])
def test_each_refusal_of_the_grammar_carries_its_code(label: str, edit: Any, code: str) -> None:
    raw = _block()
    edit(raw)
    _refuses(raw, code)


def test_a_block_that_is_not_a_mapping_is_refused() -> None:
    for raw in ([], "fidelity", None):
        with pytest.raises(FluxError) as caught:
            parse_fidelity_block(raw, expose=EXPOSE)
        assert caught.value.code.code == "FLX-5030"


def test_a_column_declared_only_with_draw_parameters_is_left_out() -> None:
    raw = _block()
    raw["columns"]["subscriber_id"] = {"null": "fail"}
    assert "subscriber_id" not in _parse(raw).columns


def test_integers_and_decimals_are_read_as_exact_numbers() -> None:
    raw = _block()
    raw["columns"]["amount"]["range"] = {"min": 1, "max": Decimal("8")}
    parsed = _parse(raw).columns["amount"]
    assert (parsed.lower, parsed.upper) == (1, 8)


def test_a_range_may_be_a_pair_of_instants() -> None:
    raw = _block()
    raw["columns"]["aborted_at"] = {
        "range": {"min": "2026-07-01T00:00:00Z", "max": "2026-07-02T00:00:00Z"}
    }
    column = _parse(raw).columns["aborted_at"]
    assert column.domain.value == "timestamp"
    assert column.interval.hi - column.interval.lo == 86_400_000


def test_a_support_column_has_no_interval() -> None:
    column = _parse(_block()).columns["market"]
    with pytest.raises(FluxError) as caught:
        _ = column.interval
    assert caught.value.code.code == "FLX-5034"


def test_a_column_lookup_names_the_one_it_lacks() -> None:
    block = _parse(_block())
    assert block.column("amount").name == "amount"
    with pytest.raises(FluxError) as caught:
        block.column("nowhere")
    assert caught.value.code.code == "FLX-5034"
    assert block.grid == block.encoding.grid


def test_a_property_id_is_stable_and_names_its_group() -> None:
    block = _parse(_block())
    ids = [declaration.property_id for declaration in block.properties]
    assert len(set(ids)) == len(ids)
    assert ids[3] == "003:mean:amount@market"
    assert block.columns_covered == tuple(sorted(block.columns_covered))


def test_a_decimal_string_with_an_exponent_is_still_a_number() -> None:
    raw = _block()
    raw["properties"][0]["tolerance"]["value"] = "2.5e-1"
    assert _parse(raw).properties[0].tolerance.magnitude == Fraction(1, 4)


# ---------------------------------------------------------------------------
# Durations, number length and the expose reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "30D",
        "P",
        "PT",
        "P1Y",
        "P2M",
        "P1DT1DT",
        "PT1T",
        "P1.5.5S",
        "PS",
        "P1W1W",
        "PT1D",
        "P1H",
        "PT1H1H",
        "P1D1",
        "P0D",
        "PT0.0005S",
        "PT1M1X",
    ],
)
def test_a_duration_the_gate_cannot_compare_is_refused(text: str) -> None:
    with pytest.raises(FluxError) as caught:
        parse_duration_ms(text)
    assert caught.value.code.code == "FLX-5030"


@pytest.mark.parametrize(
    "text, milliseconds",
    [("P1W", 604_800_000), ("PT0.5S", 500), ("PT1H30M", 5_400_000), ("P1DT12H", 129_600_000)],
)
def test_a_duration_is_the_sum_of_its_parts(text: str, milliseconds: int) -> None:
    assert parse_duration_ms(text) == milliseconds


def test_a_number_is_refused_when_it_needs_too_many_digits() -> None:
    for text in ("1" + "0" * 1200, "1e5000", "0e-5000", "0." + "0" * 1001):
        with pytest.raises(FluxError) as caught:
            require_number_length(Decimal(text), "x")
        assert caught.value.code.code == "FLX-5030"
    assert require_number_length(Decimal("1.5"), "x") == Decimal("1.5")
    # A zero counts the digits its scale spells, and keeps its scale.
    assert require_number_length(Decimal("0e-1000"), "x") == Decimal("0e-1000")
    assert require_number_length(Decimal("0e+99999999"), "x") == Decimal("0e+99999999")


def test_an_expose_that_is_not_a_list_or_has_no_schema_is_refused() -> None:
    cases = [
        ({}, "e"),
        ({"exposes": "e"}, "e"),
        ({"exposes": [{"exposeId": "e"}]}, "e"),
        ({"exposes": [{"exposeId": "e", "contract": {"schema": "abc"}}]}, "e"),
        ({"exposes": [{"exposeId": "e", "contract": {"schema": [{"type": "INT"}]}}]}, "e"),
        ({"exposes": [{"exposeId": "e", "contract": {"schema": ["a"]}}]}, "e"),
        ({"exposes": ["noise", {"exposeId": "other"}]}, "e"),
    ]
    for document, expose_id in cases:
        with pytest.raises(FluxError) as caught:
            expose_from_contract(document, expose_id)
        assert caught.value.code.code == "FLX-5030"


def test_an_expose_id_is_matched_after_normalisation() -> None:
    document = {
        "exposes": [
            "noise",
            {"exposeId": "café", "contract": {"schema": [{"name": "x"}]}},
        ]
    }
    assert expose_from_contract(document, "café").column_names == ("x",)


def test_read_support_levels_names_the_source_of_each_spelling() -> None:
    levels, source = read_support_levels({"values": ["a", "b"]}, subject="s", declared_type="")
    assert (levels, source) == (("a", "b"), "values")
    levels, source = read_support_levels("declared", subject="s", declared_type="enum('x')")
    assert (levels, source) == (("x",), "declared")


def test_a_group_by_may_not_repeat_the_property_column() -> None:
    raw = _block()
    raw["properties"][2]["groupBy"] = "market"
    _refuses(raw, "FLX-5030")


def test_a_budget_without_a_max_state_age_parses() -> None:
    raw = _block()
    del raw["budget"]["maxStateAge"]
    budget = _parse(raw).budget
    assert budget.max_state_age_ms is None
    assert budget.max_state_age_declared is None


# ---------------------------------------------------------------------------
# A declared type's parameter list and enum levels (FLUID 0.7.7 grammar)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("decimal(18,4)", ("decimal", "18,4")),
        ("  varchar ( 255 )  ", ("varchar", "255")),
        ("string", ("string", None)),
        ("varchar()", ("varchar", "")),
        ("enum('a)b', 'c')", ("enum", "'a)b', 'c'")),
        ("enum('O''Brien')", ("enum", "'O''Brien'")),
        ('enum("a)""b")', ("enum", '"a)""b"')),
        ("enum('(')", ("enum", "'('")),
        ("enum('a'", ("enum('a'", None)),
        ("enum('a)", ("enum", "'a")),
        ("varchar(a'b)", ("varchar", "a'b")),
        ("varchar(255')", ("varchar", "255'")),
        ("enum(a", ("enum(a", None)),
        ("map(string, array(int))", ("map(string, array(int))", None)),
        ("decimal(10,2) extra", ("decimal(10,2) extra", None)),
    ],
)
def test_split_type_params_reads_a_type_like_forge_cli(text: str, expected: Any) -> None:
    assert split_type_params(text) == expected


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("enum('Dr.', '>10000', 'O''Brien', 'a, b')", ("Dr.", ">10000", "O'Brien", "a, b")),
        ("enum('Dr.','>10000')", ("Dr.", ">10000")),
        ("enum('Dr.', 'Cir.', 'Mr. & Mrs.')", ("Dr.", "Cir.", "Mr. & Mrs.")),
        ("enum('a, b', 'c)d', '(e)')", ("a, b", "c)d", "(e)")),
        ("enum('a)b')", ("a)b",)),
        ("enum('a, b', 'c')", ("a, b", "c")),
        ("enum('it''s')", ("it's",)),
        ("enum('x (y)', \"p)q\")", ("x (y)", "p)q")),
        ('enum("say ""hi""")', ('say "hi"',)),
        ('enum("say ""hi""", "it\'s")', ('say "hi"', "it's")),
        ("enum('a\"b')", ('a"b',)),
        ("enum('''')", ("'",)),
        ("enum('a''')", ("a'",)),
        ("enum(United Kingdom, IE)", ("United Kingdom", "IE")),
        ("enum(-1, 0, 1)", ("-1", "0", "1")),
        ("ENUM ( 'a' , 'b' )", ("a", "b")),
        ("enum(' a ')", (" a ",)),
        ("enum('Zürich')", ("Zürich",)),
        ("enum('é')", ("é",)),
        ("enum('€', 'naïve', '日本')", ("€", "naïve", "日本")),
    ],
)
def test_an_enum_level_reads_as_written(declared: str, expected: Any) -> None:
    assert declared_enum_values(declared) == expected


@pytest.mark.parametrize(
    "declared",
    [
        "enum('a' 'b')",
        "enum('a)",
        "enum(a'b)",
        "enum('a\tb')",
        "enum('a\nb')",
        "enum('\x85')",
        "enum(1.5)",
        "enum(a.b)",
        "enum(Zürich)",
        "enum('a'b)",
        "enum('it's')",
        "enum('a'b')",
        "enum(a'b')",
    ],
)
def test_a_list_the_grammar_refuses_is_refused_never_empty(declared: str) -> None:
    with pytest.raises(FluxError, match="^FLX-3001: .* is not an enum level: "):
        declared_enum_values(declared)


def test_a_list_of_two_adjacent_quoted_levels_is_refused_by_name() -> None:
    """``enum('a' 'b')`` is a list the author wrote, not "declares no levels"."""
    with pytest.raises(FluxError, match="is not an enum level"):
        declared_enum_values("enum('a' 'b')")


@pytest.mark.parametrize(
    "declared",
    [
        "enum('a', 'b'",
        "enum(('a'))",
        "enum('a') x",
        "enum('a'))",
        "enum",
        "enumeration('a')",
        "varchar(255)",
        "enum()",
        "enum( )",
    ],
)
def test_a_type_that_is_not_a_parameterised_enum_has_no_levels(declared: str) -> None:
    assert declared_enum_values(declared) == ()


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        ("enum(a, , b)", "carries an empty enum level"),
        ("enum('a',)", "carries an empty enum level"),
        ("enum(,a)", "carries an empty enum level"),
        ("enum('')", "carries an empty enum level"),
        ("enum('it''s', \"it's\")", "repeats a level after NFC normalisation"),
        ("enum('a', 'a')", "repeats a level after NFC normalisation"),
    ],
)
def test_an_empty_or_repeated_level_keeps_its_refusal(declared: str, message: str) -> None:
    with pytest.raises(FluxError, match=f"^FLX-3001: .*{message}"):
        declared_enum_values(declared)


# ---------------------------------------------------------------------------
# A column whose name holds a dot is that column, not a link (SEAM-FIDELITY-SPEC section 2)
# ---------------------------------------------------------------------------

DOTTED = DeclaredExpose(
    expose_id="line",
    column_names=("l_id", "tier.code", "amount"),
    column_types={"l_id": "STRING", "tier.code": "STRING", "amount": "DECIMAL(18,4)"},
    relationship_names=("tier",),
)


def _dotted_block(**columns: Any) -> Dict[str, Any]:
    return {
        "records": {"min": 2, "max": 8},
        "columns": {"amount": {"range": {"min": "1", "max": "8"}}, **columns},
        "properties": [
            {
                "property": "mean",
                "column": "amount",
                "groupBy": "tier.code",
                "tolerance": {"type": "absolute", "value": "1"},
            }
        ],
        "budget": {"maxChecksPerState": 3, "maxStateAge": "P30D"},
    }


def test_a_column_named_like_a_link_is_declared_and_grouped_by_as_the_column() -> None:
    # The expose holds a column ``tier.code`` and a relationship ``tier``: the exact name is the
    # column, so it may declare a range, which a column of a linked row never may.
    block = parse_fidelity_block(
        _dotted_block(**{"tier.code": {"support": {"values": ["a", "b"]}, "maxCardinality": 2}}),
        expose=DOTTED,
    )
    assert "tier.code" in block.columns
    (declaration,) = block.properties
    assert declaration.group_by == "tier.code"
    numbered = DeclaredExpose(
        expose_id="line",
        column_names=("l_id", "tier.code", "amount"),
        column_types={"l_id": "STRING", "tier.code": "INT", "amount": "DECIMAL(18,4)"},
        relationship_names=("tier",),
    )
    declared = _dotted_block(**{"tier.code": {"range": {"min": "0", "max": "9"}}})
    declared["properties"][0] = {
        "property": "mean",
        "column": "tier.code",
        "tolerance": {"type": "absolute", "value": "1"},
    }
    assert "tier.code" in parse_fidelity_block(declared, expose=numbered).columns


def test_the_link_form_beside_it_is_a_column_of_the_linked_row() -> None:
    block = parse_fidelity_block(
        _dotted_block(
            **{
                "tier.code": {"support": {"values": ["a"]}, "maxCardinality": 1},
                "tier.label": {"support": {"values": ["x", "y"]}, "maxCardinality": 2},
            }
        ),
        expose=DOTTED,
    )
    assert {"tier.code", "tier.label"} <= set(block.columns)
    # A linked column declares its support and nothing else.
    with pytest.raises(FluxError) as caught:
        parse_fidelity_block(
            _dotted_block(**{"tier.label": {"range": {"min": "0", "max": "9"}}}), expose=DOTTED
        )
    assert caught.value.code.code == "FLX-5030"
    assert "names a column of a linked row" in str(caught.value)


# ---------------------------------------------------------------------------
# The sampling key of a distance tolerance (ADR 0016 section 8)
# ---------------------------------------------------------------------------

_HISTOGRAM = 1
_LEVELS = 6
_MEAN = 0


def _sampling_block(**tolerance: Any) -> Dict[str, Any]:
    raw = _block()
    raw["properties"][_LEVELS]["tolerance"].update(tolerance)
    return raw


def _tolerance_of(raw: Dict[str, Any], index: int = _LEVELS) -> Any:
    return _parse(raw).properties[index].tolerance


def test_a_distance_tolerance_may_declare_sampling_true() -> None:
    tolerance = _tolerance_of(_sampling_block(sampling=True))
    assert tolerance.sampling is True
    assert tolerance.declared["sampling"] == "true"
    assert tolerance.magnitude == _tolerance_of(_sampling_block()).magnitude


def test_sampling_false_is_accepted_and_does_not_widen() -> None:
    tolerance = _tolerance_of(_sampling_block(sampling=False))
    assert tolerance.sampling is False
    assert tolerance.declared["sampling"] == "false"


@pytest.mark.parametrize("value", ["true", 1, 0, None, "yes", [True]])
def test_sampling_that_is_not_a_boolean_is_refused(value: Any) -> None:
    with pytest.raises(FluxError) as caught:
        _tolerance_of(_sampling_block(sampling=value))
    assert caught.value.code.code == "FLX-5030"
    assert "sampling" in caught.value.detail


def test_sampling_on_a_tolerance_that_is_not_a_distance_is_refused() -> None:
    raw = _block()
    raw["properties"][_MEAN]["tolerance"]["sampling"] = True
    with pytest.raises(FluxError) as caught:
        _parse(raw)
    assert caught.value.code.code == "FLX-5030"


def test_a_block_without_the_key_reads_as_it_did() -> None:
    block = _parse(_block())
    for declaration in block.properties:
        assert declaration.tolerance.sampling is False
        assert "sampling" not in declaration.tolerance.declared
    histogram = block.properties[_HISTOGRAM]
    assert histogram.kind is PropertyKind.MARGINAL_HISTOGRAM
    assert dict(histogram.tolerance.declared) == {
        "max": "0.2",
        "measure": "totalVariation",
        "type": "distance",
    }
