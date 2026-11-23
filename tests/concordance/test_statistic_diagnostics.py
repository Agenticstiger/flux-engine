# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A refusal of ``S`` names the column, the row and the value class, never the value.

KERNEL-SPEC §9 keeps values out of diagnostics, and a refusal of the derived
statistic travels to the operator's terminal and the ``--json`` envelope.
Each case breaks one row of the vectors' committed slice in one way and pins
the code and the whole message, so a diagnostic that lost its column, its
row index or its subject fails here rather than in front of an operator.
The two floors are pinned too: a grouped property's group needs
``records.min`` records, an ungrouped property needs one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

import pytest

from flux_engine.concordance.boundary.statistic import (
    compute_property_statistic,
    compute_statistic,
    encode_value,
)
from flux_engine.concordance.compiler import CompiledFidelity, CompiledProperty
from flux_engine.core.errors import FluxError

pytestmark = pytest.mark.unit

MEAN = "000:mean:amount"
SUPPORT = "002:support:market"
GROUPED_MEAN = "003:mean:amount@market"
CORRELATION = "004:correlation:amount+attempts"

#: Leave the column out of the row altogether.
ABSENT = object()


def _entry(compiled: CompiledFidelity, property_id: str) -> CompiledProperty:
    return next(entry for entry in compiled.properties if entry.property_id == property_id)


def _changed(
    rows: Sequence[Mapping[str, Any]], index: int, column: str, value: Any
) -> List[Dict[str, Any]]:
    changed = [dict(row) for row in rows]
    if value is ABSENT:
        del changed[index][column]
    else:
        changed[index][column] = value
    return changed


def _refused(call: Callable[[], Any]) -> Tuple[str, str]:
    with pytest.raises(FluxError) as raised:
        call()
    return raised.value.code.code, raised.value.detail


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "value, value_class",
    [
        (True, "'bool'"),
        (1.5, "'float'"),
        (Decimal("NaN"), "'non-finite Decimal'"),
        ("abc", "'non-numeric string'"),
        ("Infinity", "'non-finite Decimal'"),
        ([1], "'list'"),
        # Eleven characters naming a hundred million digits: refused before the
        # value is formed, where forming it used to stall the check.
        ("1E+99999999", "'over-long number'"),
        ("1E-99999999", "'over-long number'"),
        (Decimal("1E+99999999"), "'over-long number'"),
        (10**1000, "'over-long number'"),
    ],
)
def test_a_value_the_grid_cannot_read_is_named_by_its_class(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    value: Any,
    value_class: str,
) -> None:
    rows = _changed(committed_rows, 2, "amount", value)
    refusal = _refused(
        lambda: compute_property_statistic(rows, _entry(compiled, MEAN), compiled.block)
    )
    assert refusal == ("FLX-5005", f"column 'amount' row 2: value class {value_class}")


def test_a_value_outside_the_range_is_refused_without_its_value(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    rows = _changed(committed_rows, 1, "amount", "99")
    refusal = _refused(
        lambda: compute_property_statistic(rows, _entry(compiled, MEAN), compiled.block)
    )
    assert refusal == ("FLX-5005", "column 'amount' row 1: value outside the declared range")


def test_an_instant_that_is_not_a_string_is_named_by_its_class(
    compiled: CompiledFidelity,
) -> None:
    column = compiled.block.columns["aborted_at"]
    refusal = _refused(lambda: encode_value(5, column, compiled.block.grid, row_index=3))
    assert refusal == ("FLX-5005", "column 'aborted_at' row 3: value class 'int'")


@pytest.mark.parametrize(
    "property_id, index, column, value, expected",
    [
        (GROUPED_MEAN, 1, "market", ABSENT, ("FLX-5031", "column 'market' is absent from row 1")),
        (GROUPED_MEAN, 1, "market", None, ("FLX-5005", "column 'market' row 1: null is refused")),
        (
            GROUPED_MEAN,
            1,
            "market",
            5.5,
            ("FLX-5005", "column 'market' row 1: value class 'float'"),
        ),
        (CORRELATION, 3, "amount", ABSENT, ("FLX-5031", "column 'amount' is absent from row 3")),
        (
            CORRELATION,
            3,
            "attempts",
            ABSENT,
            ("FLX-5031", "column 'attempts' is absent from row 3"),
        ),
        (
            CORRELATION,
            3,
            "attempts",
            None,
            ("FLX-5005", "column 'attempts' row 3: null is refused"),
        ),
        (
            CORRELATION,
            3,
            "amount",
            "99",
            ("FLX-5005", "column 'amount' row 3: value outside the declared range"),
        ),
        (
            CORRELATION,
            3,
            "attempts",
            99,
            ("FLX-5005", "column 'attempts' row 3: value outside the declared range"),
        ),
    ],
)
def test_a_grouping_column_and_a_pair_name_the_row_that_refused(
    compiled: CompiledFidelity,
    committed_rows: List[Dict[str, Any]],
    property_id: str,
    index: int,
    column: str,
    value: Any,
    expected: Tuple[str, str],
) -> None:
    rows = _changed(committed_rows, index, column, value)
    entry = _entry(compiled, property_id)
    assert _refused(lambda: compute_property_statistic(rows, entry, compiled.block)) == expected


def test_max_cardinality_admits_exactly_that_many_members(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    entry = _entry(compiled, SUPPORT)
    four = [
        dict(row, market=("ie", "uk", "fr", "de")[i % 4]) for i, row in enumerate(committed_rows)
    ]
    assert len(compute_property_statistic(four, entry, compiled.block).groups[0].support) == 4
    five = _changed(four, 7, "market", "es")
    assert _refused(lambda: compute_property_statistic(five, entry, compiled.block)) == (
        "FLX-5005",
        "column 'market': 5 distinct members exceed the declared maxCardinality 4",
    )


def test_a_record_count_refusal_names_its_subject(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    few = committed_rows[:3]
    assert _refused(lambda: compute_statistic(few, compiled)) == (
        "FLX-5005",
        "record set: 3 records, minimum is 4",
    )
    assert _refused(lambda: compute_statistic(few, compiled, subject="synthetic dataset")) == (
        "FLX-5005",
        "synthetic dataset: 3 records, minimum is 4",
    )


def test_an_empty_property_names_its_subject_and_its_id(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    empty = [dict(row, amount=None) for row in committed_rows]
    entry = _entry(compiled, MEAN)
    assert _refused(lambda: compute_property_statistic(empty, entry, compiled.block)) == (
        "FLX-5005",
        "record set: 000:mean:amount: 0 records, minimum is 1",
    )
    assert _refused(lambda: compute_statistic(empty, compiled, subject="synthetic dataset")) == (
        "FLX-5005",
        "synthetic dataset: 000:mean:amount: 0 records, minimum is 1",
    )


def test_an_ungrouped_property_needs_one_record_and_a_group_needs_records_min(
    compiled: CompiledFidelity, committed_rows: List[Dict[str, Any]]
) -> None:
    one = [dict(row, amount=None if i else row["amount"]) for i, row in enumerate(committed_rows)]
    statistic = compute_property_statistic(one, _entry(compiled, MEAN), compiled.block)
    assert [group.k for group in statistic.groups] == [1]
    short = _changed(committed_rows, 7, "market", "ie")
    entry = _entry(compiled, GROUPED_MEAN)
    assert _refused(lambda: compute_property_statistic(short, entry, compiled.block)) == (
        "FLX-5005",
        "record set: 003:mean:amount@market group #1: 3 records, minimum is 4",
    )
