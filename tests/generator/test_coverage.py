# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How each column of a generator-driven emit was produced (the coverage report).

A plan built from declared statistics knows, per column, whether the value is
the author's declaration, a derivation or the type's own default. These cases
pin that reading, and pin the one property that matters most about it: it is
a report about the plan, so no digest the plan produces moves for it.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.rows import (
    DRAW_FIELDS,
    NON_DRAW_FIELDS,
    GeneratorPlan,
    _declares_a_draw,
    build_plan,
)
from flux_engine.generator.statistics import ColumnStatistics
from flux_engine.seam.coverage import SOURCE_IDENTIFIER, SOURCE_TIME_BOUNDS, Origin

from .conftest import EXPOSE_ID, SIM_ID, TIME_BOUNDS, H, expose_from_schema, fidelity

pytestmark = pytest.mark.unit

SCHEMA: List[Dict[str, Any]] = [
    {"name": "widget_id", "type": "STRING", "required": True, "semanticType": "identifier"},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "seen_at", "type": "TIMESTAMP"},
    {"name": "seen_on", "type": "DATE"},
]

DECLARES_AMOUNT_ONLY = fidelity({"amount": {"range": {"min": "0", "max": "100"}}})


def _plan(block: Any = DECLARES_AMOUNT_ONLY, *, time_bounds: Any = TIME_BOUNDS) -> GeneratorPlan:
    return build_plan(
        expose_from_schema(SCHEMA),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=8,
        fidelity=block,
        time_bounds=time_bounds,
    )


def test_an_identifier_is_derived_a_declared_column_is_declared_and_a_bare_date_is_a_default() -> (
    None
):
    coverage = coverage_of_plan(_plan())
    assert coverage.expose_id == EXPOSE_ID
    assert [(row.column, row.origin, row.source, row.decided_by) for row in coverage.columns] == [
        ("widget_id", Origin.DERIVED, SOURCE_IDENTIFIER, "identifier"),
        ("amount", Origin.DECLARED, "fidelity.columns.amount", "fidelity"),
        ("seen_at", Origin.DEFAULT, SOURCE_TIME_BOUNDS, "time-bounds"),
        ("seen_on", Origin.DEFAULT, SOURCE_TIME_BOUNDS, "time-bounds"),
    ]
    assert [row.column for row in coverage.defaulted] == ["seen_at", "seen_on"]


def test_each_row_carries_the_columns_own_type_and_required_flag() -> None:
    rows = {row.column: row for row in coverage_of_plan(_plan()).columns}
    assert (rows["widget_id"].type_name, rows["widget_id"].required) == ("STRING", True)
    assert (rows["seen_at"].type_name, rows["seen_at"].required) == ("TIMESTAMP", False)


def test_a_date_column_that_declares_a_range_is_declared_not_defaulted() -> None:
    block = fidelity(
        {
            "amount": {"range": {"min": "0", "max": "100"}},
            "seen_at": {"range": {"min": "2026-07-01T00:00:00Z", "max": "2026-07-02T00:00:00Z"}},
            "seen_on": {"range": {"min": "2026-07-01", "max": "2026-07-05"}},
        }
    )
    assert coverage_of_plan(_plan(block)).defaulted == ()


def test_a_date_column_that_declares_a_rate_is_declared_though_its_start_is_the_time_bounds() -> (
    None
):
    """The family is the author's; only where the arrivals start is filled in."""
    block = fidelity(
        {
            "amount": {"range": {"min": "0", "max": "100"}},
            "seen_at": {"rate": "2"},
            "seen_on": {"range": {"min": "2026-07-01", "max": "2026-07-05"}},
        }
    )
    rows = {row.column: row for row in coverage_of_plan(_plan(block)).columns}
    assert rows["seen_at"].origin is Origin.DECLARED


def test_a_null_fraction_alone_declares_no_draw_so_the_column_is_still_a_default() -> None:
    block = fidelity(
        {
            "amount": {"range": {"min": "0", "max": "100"}},
            "seen_at": {"nullFraction": "0.1"},
        }
    )
    rows = {row.column: row for row in coverage_of_plan(_plan(block)).columns}
    assert rows["seen_at"].origin is Origin.DEFAULT


@pytest.mark.parametrize("family", ["uniform", "empirical"])
def test_naming_the_family_it_already_gets_declares_no_draw_so_the_column_is_still_a_default(
    family: str,
) -> None:
    """``family`` supplies no parameter, so the draw is the default one under another name."""
    block = fidelity(
        {
            "amount": {"range": {"min": "0", "max": "100"}},
            "seen_at": {"family": family},
            "seen_on": {"family": family},
        }
    )
    assert [row.column for row in coverage_of_plan(_plan(block)).defaulted] == [
        "seen_at",
        "seen_on",
    ]


def test_without_time_bounds_a_date_column_that_declares_nothing_is_still_refused() -> None:
    """Coverage adds a report; it loosens no refusal (``FLX-3402``)."""
    with pytest.raises(FluxError) as error:
        _plan(time_bounds=None)
    assert error.value.code.code == "FLX-3402"


# ---------------------------------------------------------------------------
# What counts as declaring a draw
# ---------------------------------------------------------------------------


def test_every_statistics_field_is_placed_as_a_draw_or_not_a_draw_exactly_once() -> None:
    """A field added to the class has to be decided here, or this fails."""
    fields = {field.name for field in dataclasses.fields(ColumnStatistics)}
    assert fields == set(DRAW_FIELDS) | set(NON_DRAW_FIELDS)
    assert not set(DRAW_FIELDS) & set(NON_DRAW_FIELDS)


@pytest.mark.parametrize(
    ("statistics", "declares"),
    [
        (ColumnStatistics(column="c"), False),
        (ColumnStatistics(column="c", null_fraction=Decimal("0.5")), False),
        (ColumnStatistics(column="c", null_policy="fail", max_cardinality=8), False),
        (ColumnStatistics(column="c", mean=Decimal(0)), True),
        (ColumnStatistics(column="c", rate=Decimal(1)), True),
        (ColumnStatistics(column="c", support_is_declared=True), True),
        (ColumnStatistics(column="c", family="uniform"), False),
        (ColumnStatistics(column="c", range_min=Decimal(0), range_max=Decimal(1)), True),
    ],
)
def test_a_declared_zero_is_a_declaration_and_nulls_alone_are_not(
    statistics: ColumnStatistics, declares: bool
) -> None:
    assert _declares_a_draw(statistics) is declares


# ---------------------------------------------------------------------------
# No digest moves
# ---------------------------------------------------------------------------


def test_the_report_is_not_part_of_the_plan_document_or_its_digest() -> None:
    plan = _plan()
    document = plan.as_document()
    assert "defaulted" not in json.dumps(document)
    assert set(document["columns"][0]) == {
        "name",
        "type",
        "required",
        "columnType",
        "family",
        "statistics",
    }
    flipped = dataclasses.replace(
        plan,
        columns=tuple(
            dataclasses.replace(column, defaulted=not column.defaulted) for column in plan.columns
        ),
    )
    assert flipped.as_document() == document
    assert flipped.digest() == plan.digest()
