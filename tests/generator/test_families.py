# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""SEAM-FIDELITY-SPEC §1's table: six column types, four families.

The table is transcribed in the module and asserted here against a second,
independent transcription of the same six rows. Two people reading the
specification would have to make the same mistake for this to pass while
the table is wrong.

The other half of the file is the rule that nothing is guessed. A column
that declares nothing has no family (``FLX-3402``), a family a row does
not allow is refused (``FLX-3401``), and no path through
:func:`choose_family` reads a column's *name*.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Mapping

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator.families import (
    FAMILY_TABLE,
    ColumnType,
    Family,
    choose_family,
    classify_column,
    is_identifier,
)
from flux_engine.generator.statistics import ColumnStatistics, statistics_for
from flux_engine.seam.contract import CompiledColumn

from .conftest import expose_from_schema, fidelity

pytestmark = pytest.mark.unit


#: The six rows of §1's table, transcribed a second time and independently.
SPEC_TABLE: Mapping[str, FrozenSet[str]] = {
    "STRING categorical": frozenset({"categorical", "uniform/empirical"}),
    "INT": frozenset({"categorical", "bounded-normal", "rate", "uniform/empirical"}),
    "DECIMAL(p,s)": frozenset({"bounded-normal", "rate", "uniform/empirical", "categorical"}),
    "BOOL": frozenset({"categorical"}),
    "DATE / TIMESTAMP": frozenset({"uniform/empirical", "rate"}),
    "identifier": frozenset(),
}


def _column(declared: str, **extra: Any) -> CompiledColumn:
    return expose_from_schema([{"name": "c", "type": declared, **extra}]).columns[0]


def _statistics(**block: Any) -> ColumnStatistics:
    column = _column("STRING")
    return statistics_for(column, fidelity({"c": block}))


def _statistics_for(column: CompiledColumn, **block: Any) -> ColumnStatistics:
    return statistics_for(column, fidelity({column.name: block}))


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_the_table_has_six_rows_and_four_families() -> None:
    assert len(FAMILY_TABLE) == 6
    assert len(Family) == 4


def test_the_table_matches_an_independent_transcription_of_the_spec() -> None:
    transcribed = {
        row.column_type.value: frozenset(member.value for member in row.allowed)
        for row in FAMILY_TABLE.values()
    }
    assert transcribed == dict(SPEC_TABLE)


def test_the_identifier_row_allows_no_family() -> None:
    """§1's dash is load-bearing: an identifier is derived, never drawn."""
    assert FAMILY_TABLE[ColumnType.IDENTIFIER].allowed == frozenset()


def test_every_column_type_has_a_row() -> None:
    assert set(FAMILY_TABLE) == set(ColumnType)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("STRING", ColumnType.STRING_CATEGORICAL),
        ("varchar(64)", ColumnType.STRING_CATEGORICAL),
        ("INT", ColumnType.INT),
        ("BIGINT", ColumnType.INT),
        ("DECIMAL(18,4)", ColumnType.DECIMAL),
        ("BOOL", ColumnType.BOOL),
        ("DATE", ColumnType.DATE_TIMESTAMP),
        ("TIMESTAMP", ColumnType.DATE_TIMESTAMP),
    ],
)
def test_a_column_maps_onto_its_row(declared: str, expected: ColumnType) -> None:
    column = _column(declared)
    assert classify_column(column, _statistics_for(column)) is expected


@pytest.mark.parametrize("declared", ["JSON", "BYTES"])
def test_a_logical_type_with_no_row_is_refused_by_name(declared: str) -> None:
    """§1 names six column types; JSON and BYTES are not among them."""
    column = _column(declared)
    with pytest.raises(FluxError) as error:
        classify_column(column, _statistics_for(column))
    assert error.value.code.code == "FLX-3401"
    assert declared in str(error.value)


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "extra", "block"),
    [
        ("STRING", {}, {"unique": True}),
        ("uuid", {}, {}),
        ("GUID", {}, {}),
        ("uniqueidentifier", {}, {}),
        ("STRING", {"semanticType": "identifier"}, {}),
        ("STRING", {"semanticType": "IDENTIFIER"}, {}),
        ("STRING", {"tags": ["identifier"]}, {}),
        ("STRING", {"tags": ["pii", "primary-key"]}, {}),
    ],
)
def test_the_four_ways_a_contract_can_declare_an_identifier(
    declared: str, extra: Dict[str, Any], block: Dict[str, Any]
) -> None:
    column = _column(declared, **extra)
    assert is_identifier(column, _statistics_for(column, **block)) is True
    assert classify_column(column, _statistics_for(column, **block)) is ColumnType.IDENTIFIER


@pytest.mark.parametrize(
    "name",
    ["subscriber_id", "id", "uuid", "primary_key", "customer_identifier"],
)
def test_a_columns_name_never_makes_it_an_identifier(name: str) -> None:
    """Reading "id" out of a column name would be a guess about a contract."""
    column = expose_from_schema([{"name": name, "type": "STRING"}]).columns[0]
    assert is_identifier(column, statistics_for(column, None)) is False


def test_a_non_string_column_marked_unique_is_refused() -> None:
    """§1's identifier row is STRING/uuid; there is no INT identifier row."""
    column = _column("INT")
    with pytest.raises(FluxError) as error:
        classify_column(column, _statistics_for(column, unique=True))
    assert error.value.code.code == "FLX-3401"
    assert "STRING/uuid" in str(error.value)


def test_an_identifier_has_no_family() -> None:
    column = _column("uuid")
    statistics = _statistics_for(column)
    assert choose_family(column, statistics, ColumnType.IDENTIFIER) is None


# ---------------------------------------------------------------------------
# Choosing a family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "block", "expected"),
    [
        ("STRING", {"support": {"values": ["a", "b"]}}, Family.CATEGORICAL),
        ("STRING", {"levels": {"a": "0.5", "b": "0.5"}}, Family.CATEGORICAL),
        ("BOOL", {"levels": {"true": "0.3", "false": "0.7"}}, Family.CATEGORICAL),
        ("DECIMAL(18,4)", {"range": {"min": "0", "max": "10"}}, Family.BOUNDED_NORMAL),
        ("DECIMAL(18,4)", {"rate": "0.5"}, Family.RATE),
        ("INT", {"mean": "3"}, Family.RATE),
        ("INT", {"range": {"min": "0", "max": "10"}}, Family.UNIFORM_EMPIRICAL),
        ("INT", {"levels": {"1": "0.5", "2": "0.5"}}, Family.CATEGORICAL),
        (
            "TIMESTAMP",
            {"range": {"min": "2026-07-01T00:00:00Z", "max": "2026-07-02T00:00:00Z"}},
            Family.UNIFORM_EMPIRICAL,
        ),
        (
            "TIMESTAMP",
            {
                "rate": "0.001",
                "range": {"min": "2026-07-01T00:00:00Z", "max": "2026-07-02T00:00:00Z"},
            },
            Family.RATE,
        ),
        ("DATE", {"range": {"min": "2026-07-01", "max": "2026-07-31"}}, Family.UNIFORM_EMPIRICAL),
    ],
)
def test_the_declaration_decides_the_default_family(
    declared: str, block: Dict[str, Any], expected: Family
) -> None:
    """§1's two conditional default cells, resolved by what is declared."""
    column = _column(declared)
    statistics = _statistics_for(column, **block)
    column_type = classify_column(column, statistics)
    assert choose_family(column, statistics, column_type) is expected


def test_declared_bins_choose_the_histogram_case() -> None:
    column = _column("DECIMAL(18,4)")
    statistics = _statistics_for(column, bins=["0", "5", "10"], binWeights=["1", "1"])
    assert choose_family(column, statistics, ColumnType.DECIMAL) is Family.UNIFORM_EMPIRICAL


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("categorical", Family.CATEGORICAL),
        ("bernoulli", Family.CATEGORICAL),
        ("bounded-normal", Family.BOUNDED_NORMAL),
        ("bounded_normal", Family.BOUNDED_NORMAL),
        ("BoundedNormal", Family.BOUNDED_NORMAL),
        ("rate", Family.RATE),
        ("poisson", Family.RATE),
        ("exponential", Family.RATE),
        ("uniform", Family.UNIFORM_EMPIRICAL),
        ("empirical", Family.UNIFORM_EMPIRICAL),
        ("uniform/empirical", Family.UNIFORM_EMPIRICAL),
        ("empirical-histogram", Family.UNIFORM_EMPIRICAL),
    ],
)
def test_an_explicit_family_declaration_wins(spelling: str, expected: Family) -> None:
    column = _column("DECIMAL(18,4)")
    statistics = _statistics_for(
        column, family=spelling, range={"min": "0", "max": "10"}, mean="1", rate="1"
    )
    assert choose_family(column, statistics, ColumnType.DECIMAL) is expected


def test_a_family_name_the_set_has_no_member_for_is_refused() -> None:
    column = _column("DECIMAL(18,4)")
    statistics = _statistics_for(column, family="lognormal", range={"min": "0", "max": "1"})
    with pytest.raises(FluxError) as error:
        choose_family(column, statistics, ColumnType.DECIMAL)
    assert error.value.code.code == "FLX-3401"
    assert "lognormal" in str(error.value)


@pytest.mark.parametrize(
    ("declared", "family_name"),
    [
        ("BOOL", "bounded-normal"),
        ("BOOL", "rate"),
        ("BOOL", "uniform"),
        ("STRING", "rate"),
        ("STRING", "bounded-normal"),
        ("TIMESTAMP", "categorical"),
        ("DATE", "bounded-normal"),
    ],
)
def test_a_family_the_row_does_not_allow_is_refused(declared: str, family_name: str) -> None:
    column = _column(declared)
    statistics = _statistics_for(column, family=family_name)
    column_type = classify_column(column, statistics)
    with pytest.raises(FluxError) as error:
        choose_family(column, statistics, column_type)
    assert error.value.code.code == "FLX-3401"
    assert family_name in str(error.value) or "not allow" in str(error.value)


def test_a_declaration_that_implies_a_forbidden_family_is_refused() -> None:
    """A rate on a BOOL: the implication is checked against the row too."""
    column = _column("BOOL")
    statistics = _statistics_for(column, rate="0.5")
    with pytest.raises(FluxError) as error:
        choose_family(column, statistics, ColumnType.BOOL)
    assert error.value.code.code == "FLX-3401"


@pytest.mark.parametrize(
    "declared", ["STRING", "INT", "DECIMAL(18,4)", "BOOL", "DATE", "TIMESTAMP"]
)
def test_a_column_that_declares_nothing_has_no_family(declared: str) -> None:
    """Fail closed: with nothing declared there is nothing to derive from."""
    column = _column(declared)
    statistics = statistics_for(column, None)
    column_type = classify_column(column, statistics)
    with pytest.raises(FluxError) as error:
        choose_family(column, statistics, column_type)
    assert error.value.code.code == "FLX-3402"
    assert "declares no range, support, levels, rate or bins" in str(error.value)


def test_the_refusal_names_the_place_a_declaration_would_go() -> None:
    column = _column("DECIMAL(18,4)")
    with pytest.raises(FluxError) as error:
        choose_family(column, statistics_for(column, None), ColumnType.DECIMAL)
    assert "emits[].fidelity.columns" in str(error.value)


def test_no_family_choice_reads_a_column_name() -> None:
    """The same declaration on differently-named columns chooses the same family."""
    block = {"range": {"min": "0", "max": "10"}}
    families: List[Family] = []
    for name in ("amount", "id", "timestamp", "count", "uuid"):
        column = expose_from_schema([{"name": name, "type": "DECIMAL(18,4)"}]).columns[0]
        statistics = statistics_for(column, fidelity({name: block}))
        chosen = choose_family(column, statistics, classify_column(column, statistics))
        assert chosen is not None
        families.append(chosen)
    assert len(set(families)) == 1
