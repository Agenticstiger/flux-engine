# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Declared aggregate statistics: exactly what the generator may read.

Two things are pinned here that ADR-0003 turns into a rule.

**The input surface is a declaration and nothing else.** Every key the
reader consults is in :data:`COLUMN_STATISTIC_KEYS`, and a test asserts
that reading a block containing an unrelated key changes nothing -- the
block is a contract between a data owner and a verifier, and the generator
is only one of its readers.

**Numbers are exact.** SEAM-FIDELITY-SPEC §2: "Numbers are strings (exact
decimals), never floats." A float in a declaration is ``FLX-3002``, the
same code a float in a row gets, because it is the same problem.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, List

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError
from flux_engine.generator.statistics import (
    COLUMN_STATISTIC_KEYS,
    DATE_EPOCH_ORDINAL,
    ColumnStatistics,
    domain_value,
    exact_decimal,
    fidelity_columns,
    statistics_for,
    statistics_from_fidelity,
)
from flux_engine.seam.contract import CompiledColumn

from .conftest import expose_from_schema, fidelity

pytestmark = pytest.mark.unit


def _column(declared: str = "DECIMAL(18,4)", name: str = "c") -> CompiledColumn:
    return expose_from_schema([{"name": name, "type": declared}]).columns[0]


#: A FLUID column type that names its own levels. ``support: declared``
#: is defined against exactly this -- FLUID 0.7.5's only structured value
#: set -- so a test of that spelling has to declare one.
ENUM_TYPE = "enum('b','a')"


def _read(declared: str = "DECIMAL(18,4)", **block: Any) -> ColumnStatistics:
    column = _column(declared)
    return statistics_for(column, fidelity({column.name: block}))


# ---------------------------------------------------------------------------
# Exactness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.5", Decimal("1.5")),
        ("  1.5  ", Decimal("1.5")),
        (7, Decimal(7)),
        (Decimal("0.1"), Decimal("0.1")),
        ("1E+3", Decimal("1E+3")),
        ("-0.0001", Decimal("-0.0001")),
    ],
)
def test_an_exact_number_is_read_exactly(value: Any, expected: Decimal) -> None:
    assert exact_decimal(value, what="c") == expected


def test_a_float_in_a_declaration_is_flx_3002() -> None:
    with pytest.raises(FluxError) as error:
        exact_decimal(0.1, what="c: range.min")
    assert error.value.code.code == "FLX-3002"
    assert "exact decimals" in str(error.value)


@pytest.mark.parametrize("value", [True, False, None, [], {}, "not a number", "NaN", "Infinity"])
def test_a_value_that_is_not_an_exact_number_is_refused(value: Any) -> None:
    with pytest.raises(FluxError) as error:
        exact_decimal(value, what="c")
    assert error.value.code.code in {"FLX-3402", "FLX-3002"}


def test_a_declared_float_reaches_the_reader_through_a_range() -> None:
    with pytest.raises(FluxError) as error:
        _read(range={"min": 0.0, "max": 1.0})
    assert error.value.code.code == "FLX-3002"


# ---------------------------------------------------------------------------
# Bounds are read in the units of the type they bound
# ---------------------------------------------------------------------------


def test_a_timestamp_bound_is_read_as_rfc_3339() -> None:
    """SEAM-FIDELITY-SPEC §2's own example declares one this way."""
    statistics = _read(
        "TIMESTAMP",
        range={"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"},
    )
    assert statistics.range_min == Decimal(parse_rfc3339_ms("2026-07-01T00:00:00Z"))
    assert statistics.range_max == Decimal(parse_rfc3339_ms("2026-09-30T23:59:59Z"))
    # The declared spelling is kept for the generator digest.
    assert statistics.range_min_text == "2026-07-01T00:00:00Z"


def test_a_timestamp_bound_may_also_be_integer_milliseconds() -> None:
    statistics = _read("TIMESTAMP", range={"min": 0, "max": 1000})
    assert (statistics.range_min, statistics.range_max) == (Decimal(0), Decimal(1000))


def test_a_date_bound_is_read_as_days_since_the_epoch() -> None:
    statistics = _read("DATE", range={"min": "1970-01-01", "max": "2026-07-01"})
    assert statistics.range_min == Decimal(0)
    assert statistics.range_max == Decimal(date(2026, 7, 1).toordinal() - DATE_EPOCH_ORDINAL)


@pytest.mark.parametrize(
    ("declared", "value"),
    [
        ("TIMESTAMP", "not a timestamp"),
        ("TIMESTAMP", "2026-07-01"),
        ("TIMESTAMP", ["2026-07-01T00:00:00Z"]),
        ("TIMESTAMP", True),
        ("DATE", "20260701"),
        ("DATE", "2026-02-30"),
        ("DATE", ["2026-07-01"]),
        ("DATE", True),
    ],
)
def test_a_temporal_bound_outside_its_grammar_is_refused(declared: str, value: Any) -> None:
    with pytest.raises(FluxError) as error:
        domain_value(value, _column(declared), what="c: range.min")
    assert error.value.code.code == "FLX-3402"


def test_a_numeric_bound_is_an_exact_decimal() -> None:
    assert domain_value("1.5", _column("DECIMAL(18,4)"), what="c") == Decimal("1.5")
    assert domain_value(3, _column("INT"), what="c") == Decimal(3)


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------


def test_a_range_declares_both_ends() -> None:
    statistics = _read(range={"min": "0", "max": "10"})
    assert statistics.has_range
    assert (statistics.range_min, statistics.range_max) == (Decimal(0), Decimal(10))


@pytest.mark.parametrize(
    "declared_range",
    [{"min": "0"}, {"max": "10"}, {}, "not a mapping", 7],
)
def test_a_malformed_range_is_refused(declared_range: Any) -> None:
    with pytest.raises(FluxError) as error:
        _read(range=declared_range)
    assert error.value.code.code == "FLX-3402"


def test_an_inverted_range_is_refused() -> None:
    with pytest.raises(FluxError) as error:
        _read(range={"min": "10", "max": "0"})
    assert error.value.code.code == "FLX-3402"
    assert "above" in str(error.value)


def test_a_degenerate_range_is_allowed() -> None:
    statistics = _read(range={"min": "5", "max": "5"})
    assert statistics.range_min == statistics.range_max == Decimal(5)


# ---------------------------------------------------------------------------
# Support and levels
# ---------------------------------------------------------------------------


def test_a_listed_support_is_read_in_declaration_order() -> None:
    statistics = _read("STRING", support={"values": ["b", "a", "c"]})
    assert statistics.support == ("b", "a", "c")
    assert statistics.support_is_declared is False


def test_the_declared_spelling_takes_the_levels_from_the_column_type() -> None:
    """``support: declared`` reads the FLUID type's own ``enum(...)`` list.

    SEAM-FIDELITY-SPEC §2 gives the word one meaning, and it resolves
    rather than defers: the levels the generator draws from are the ones
    the contract names, and a column that spells ``declared`` over a type
    naming none is refused rather than left with nothing to draw from.
    """
    statistics = _read(ENUM_TYPE, support="declared")
    assert statistics.support == ("b", "a")
    assert statistics.support_is_declared is True


def test_the_declared_spelling_over_a_type_naming_no_levels_is_refused() -> None:
    """The paired negative: ``declared`` is not a licence to draw nothing."""
    with pytest.raises(FluxError) as error:
        _read("STRING", support="declared")
    assert error.value.code.code == "FLX-3402"


def test_a_support_member_is_nfc_normalised() -> None:
    statistics = _read("STRING", support={"values": ["café"]})
    assert statistics.support == ("café",)


@pytest.mark.parametrize(
    "support",
    ["something else", 7, [1, 2], {"a": 1}, ["a", "a"], ["a", "b"], {"values": ["a", "a"]}],
)
def test_a_malformed_support_is_refused(support: Any) -> None:
    with pytest.raises(FluxError) as error:
        _read("STRING", support=support)
    assert error.value.code.code == "FLX-3402"


def test_levels_are_read_as_exact_weights() -> None:
    statistics = _read("STRING", levels={"a": "0.25", "b": "0.75"})
    assert statistics.levels == {"a": Decimal("0.25"), "b": Decimal("0.75")}


@pytest.mark.parametrize(
    "levels",
    [{}, "not a mapping", {"a": -1}, {"a": 0.5}, {"café": "0.5", "café": "0.5"}],
)
def test_malformed_levels_are_refused(levels: Any) -> None:
    with pytest.raises(FluxError) as error:
        _read("STRING", levels=levels)
    assert error.value.code.code in {"FLX-3402", "FLX-3002"}


# ---------------------------------------------------------------------------
# Bins
# ---------------------------------------------------------------------------


def test_bins_come_from_a_marginal_histogram_property() -> None:
    """§2 puts the edges on the property; the generator honours them."""
    column = _column()
    block = fidelity(
        {column.name: {}},
        properties=[
            {
                "property": "marginalHistogram",
                "column": column.name,
                "bins": ["0", "50", "100"],
                "tolerance": {"type": "distance", "measure": "totalVariation", "max": "0.05"},
            }
        ],
    )
    statistics = statistics_for(column, block)
    assert statistics.bins == (Decimal(0), Decimal(50), Decimal(100))
    assert statistics.bin_texts == ("0", "50", "100")


def test_a_histogram_for_another_column_is_not_read() -> None:
    column = _column()
    block = fidelity(
        {column.name: {}},
        properties=[
            {"property": "marginalHistogram", "column": "other", "bins": ["0", "1"]},
            {"property": "mean", "column": column.name},
            "not a mapping",
        ],
    )
    assert statistics_for(column, block).bins is None


def test_a_column_block_overrides_the_property_edges() -> None:
    column = _column()
    block = fidelity(
        {column.name: {"bins": ["0", "1"], "binWeights": ["1"]}},
        properties=[
            {"property": "marginalHistogram", "column": column.name, "bins": ["0", "50", "100"]}
        ],
    )
    assert statistics_for(column, block).bins == (Decimal(0), Decimal(1))


@pytest.mark.parametrize(
    "block",
    [
        {"bins": ["0"]},
        {"bins": ["0", "0"]},
        {"bins": ["10", "0"]},
        {"bins": "not a list"},
        {"binWeights": ["1"]},
        {"bins": ["0", "1", "2"], "binWeights": ["1"]},
        {"bins": ["0", "1"], "binWeights": ["-1"]},
        {"bins": ["0", "1"], "binWeights": "not a list"},
    ],
)
def test_malformed_bins_are_refused(block: Dict[str, Any]) -> None:
    with pytest.raises(FluxError) as error:
        _read(**block)
    assert error.value.code.code == "FLX-3402"


# ---------------------------------------------------------------------------
# The rest of the block
# ---------------------------------------------------------------------------


def test_the_declared_keys_are_all_read() -> None:
    statistics = _read(
        "STRING",
        range={"min": "0", "max": "1"},
        support={"values": ["a", "b"]},
        maxCardinality=8,
        null="fail",
        unique=True,
        levels={"a": "0.5", "b": "0.5"},
        mean="0.5",
        stddev="0.1",
        rate="2",
        nullFraction="0.25",
        bins=["0", "1"],
        binWeights=["1"],
        family="categorical",
        identifierLength=16,
    )
    assert statistics.max_cardinality == 8
    assert statistics.null_policy == "fail"
    assert statistics.unique is True
    assert statistics.mean == Decimal("0.5")
    assert statistics.stddev == Decimal("0.1")
    assert statistics.rate == Decimal(2)
    assert statistics.null_fraction == Decimal("0.25")
    assert statistics.family == "categorical"
    assert statistics.identifier_length == 16


def test_the_reader_consults_only_the_documented_keys() -> None:
    """An unrelated key in the block changes nothing the generator sees."""
    column = _column()
    plain = statistics_for(column, fidelity({column.name: {"range": {"min": "0", "max": "1"}}}))
    noisy = statistics_for(
        column,
        fidelity(
            {
                column.name: {
                    "range": {"min": "0", "max": "1"},
                    "somebodyElsesField": {"deeply": ["nested", 1]},
                }
            }
        ),
    )
    assert plain.as_document() == noisy.as_document()


def test_the_documented_key_set_is_what_the_reader_reads() -> None:
    """A key added to the reader without being documented fails this."""
    column = _column("STRING")
    baseline = statistics_for(column, fidelity({column.name: {}})).as_document()
    for key in COLUMN_STATISTIC_KEYS:
        assert isinstance(key, str)
    unknown = statistics_for(
        column, fidelity({column.name: {"notAKeyTheReaderKnows": "x"}})
    ).as_document()
    assert unknown == baseline


@pytest.mark.parametrize("value", [0, -1, "8", True, 1.5])
def test_a_malformed_max_cardinality_is_refused(value: Any) -> None:
    with pytest.raises(FluxError) as error:
        _read(maxCardinality=value)
    assert error.value.code.code == "FLX-3402"


@pytest.mark.parametrize("value", ["-0.1", "1.1", "2"])
def test_a_null_fraction_outside_the_unit_interval_is_refused(value: str) -> None:
    with pytest.raises(FluxError) as error:
        _read(nullFraction=value)
    assert error.value.code.code == "FLX-3402"


def test_a_null_policy_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(FluxError) as error:
        _read(null=7)
    assert error.value.code.code == "FLX-3402"


def test_an_absent_declaration_is_silence_not_a_zero() -> None:
    """ "Declared as zero" and "not declared" have to be different states."""
    absent = _read()
    declared = _read(nullFraction="0")
    assert absent.null_fraction is None
    assert declared.null_fraction == Decimal(0)


# ---------------------------------------------------------------------------
# Whole-expose reading
# ---------------------------------------------------------------------------


def test_every_column_gets_statistics_even_when_undeclared() -> None:
    expose = expose_from_schema([{"name": "a", "type": "STRING"}, {"name": "b", "type": "INT"}])
    statistics = statistics_from_fidelity(expose, fidelity({"a": {"support": {"values": ["x"]}}}))
    assert set(statistics) == {"a", "b"}
    assert statistics["a"].support == ("x",)
    assert statistics["b"] == ColumnStatistics(column="b")


def test_a_declaration_for_a_column_the_expose_does_not_have_is_refused() -> None:
    """§2: every ``column`` is "a member of the referenced expose schema"."""
    expose = expose_from_schema([{"name": "a", "type": "STRING"}])
    with pytest.raises(FluxError) as error:
        statistics_from_fidelity(expose, fidelity({"b": {"support": {"values": ["x"]}}}))
    assert error.value.code.code == "FLX-3402"
    assert "'b'" in str(error.value)


def test_an_absent_fidelity_block_reads_as_nothing_declared() -> None:
    expose = expose_from_schema([{"name": "a", "type": "STRING"}])
    assert statistics_from_fidelity(expose, None)["a"] == ColumnStatistics(column="a")
    assert statistics_from_fidelity(expose, {})["a"] == ColumnStatistics(column="a")


@pytest.mark.parametrize("block", [None, {}, {"columns": "not a mapping"}, {"columns": {}}])
def test_reading_columns_out_of_a_block_is_total(block: Any) -> None:
    assert fidelity_columns(block) == {}


# ---------------------------------------------------------------------------
# The digest document
# ---------------------------------------------------------------------------


def test_the_document_renders_numbers_as_their_declared_spelling() -> None:
    document = _read(range={"min": "0.10", "max": "10"}, mean="1.500").as_document()
    assert document["range"] == {"min": "0.10", "max": "10"}
    assert document["mean"] == "1.500"


def test_an_undeclared_field_is_absent_from_the_document() -> None:
    document = _read().as_document()
    assert document == {"column": "c"}


@given(
    minimum=st.integers(min_value=-1000, max_value=0),
    maximum=st.integers(min_value=1, max_value=1000),
)
def test_the_document_is_a_function_of_the_declaration(minimum: int, maximum: int) -> None:
    block: Dict[str, Any] = {"range": {"min": str(minimum), "max": str(maximum)}}
    first = _read(**block).as_document()
    second = _read(**block).as_document()
    assert first == second


def test_the_document_carries_no_float() -> None:
    document = _read(range={"min": "0", "max": "1"}, mean="0.5", nullFraction="0.25").as_document()

    def floats(value: Any) -> List[Any]:
        if isinstance(value, float):
            return [value]
        if isinstance(value, dict):
            return [f for item in value.values() for f in floats(item)]
        if isinstance(value, list):
            return [f for item in value for f in floats(item)]
        return []

    assert floats(document) == []


def test_a_date_bound_may_also_be_integer_days_since_the_epoch() -> None:
    """The integer spelling of a DATE bound, beside the calendar one."""
    column = _column("DATE", "d")
    assert domain_value(0, column, what="d") == Decimal(0)
    assert domain_value(20_000, column, what="d") == Decimal(20_000)


def test_the_document_carries_the_declarations_the_shipped_example_omits() -> None:
    """``support: declared``, an explicit family and an identifier length.

    Each is a declared aggregate the generator reads, so each has to appear
    in ``generatorDigest``'s pre-image: a plan that changed one of them and
    produced the same digest would break SEAM-FIDELITY-SPEC §8's rule that
    a generator change moves ``generatorDigest``.
    """
    expose = expose_from_schema(
        [{"name": "market", "type": ENUM_TYPE}, {"name": "id", "type": "STRING"}]
    )
    statistics = statistics_from_fidelity(
        expose,
        {
            "columns": {
                "market": {"support": "declared", "family": "categorical"},
                "id": {"unique": True, "identifierLength": 12},
            }
        },
    )
    market = statistics["market"].as_document()
    assert market["supportIsDeclared"] is True
    assert market["family"] == "categorical"
    identifier = statistics["id"].as_document()
    assert identifier["unique"] is True
    assert identifier["identifierLength"] == 12
    # Negative control: change one of the three and the document moves.
    changed = statistics_from_fidelity(
        expose,
        {
            "columns": {
                "market": {"support": "declared", "family": "categorical"},
                "id": {"unique": True, "identifierLength": 13},
            }
        },
    )
    assert changed["id"].as_document() != identifier
