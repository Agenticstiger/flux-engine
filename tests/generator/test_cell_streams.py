# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The generator's own stream source hands every cell the stream it always had.

:class:`~flux_engine.generator.paths.CellStreams` builds a cell's label from
parts checked once and keeps no stream after handing it out.
:class:`~flux_engine.generator.paths.GeneratorStreams` builds the label part
by part and keeps every stream. The tests below hold the first to the second:
the same labels, the same words, the same refusals, the same rows.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import path_label
from flux_engine.generator.paths import CellStreams, GeneratorStreams, generator_column_path
from flux_engine.generator.rows import build_plan, generate_column, generate_rows

from .conftest import EXPOSE_ID, SIM_ID, TIME_BOUNDS, H, expose_from_schema, fidelity, root_for

pytestmark = pytest.mark.unit

#: A composed and a decomposed spelling, and the longest ordinal in the run.
_COLUMNS = ["amount", "caf\u00e9", "cafe\u0301", "x" * 64]
_ROWS = [0, 1, 9, 10, 99, 12345, 10**12]


@pytest.mark.parametrize("column", _COLUMNS)
@pytest.mark.parametrize("row_index", _ROWS)
def test_a_cell_label_is_the_label_of_its_path(row_index: int, column: str) -> None:
    cells = CellStreams(root_for(), SIM_ID, EXPOSE_ID)
    path = generator_column_path(SIM_ID, EXPOSE_ID, row_index, column)
    assert cells.label(row_index, column) == path_label(path)


@pytest.mark.parametrize("column", _COLUMNS)
def test_a_cell_stream_draws_the_words_the_stream_table_draws(column: str) -> None:
    root = root_for()
    cells = CellStreams(root, SIM_ID, EXPOSE_ID)
    table = GeneratorStreams(root=root)
    for row_index in (0, 7, 4096):
        mine = cells.stream_at(row_index, column)
        theirs = table.stream(generator_column_path(SIM_ID, EXPOSE_ID, row_index, column))
        assert mine.label == theirs.label
        assert mine.next_raw_words(70) == [theirs.next_raw() for _ in range(70)]


def test_asking_twice_starts_a_new_stream_rather_than_continuing_one() -> None:
    """The one difference, and why a pass asks for each cell once."""
    cells = CellStreams(root_for(), SIM_ID, EXPOSE_ID)
    first = cells.stream_at(3, "amount")
    first.next_raw()
    assert cells.stream_at(3, "amount").position == 0


@pytest.mark.parametrize(
    "sim_id,expose_id,row_index,column",
    [
        ("", EXPOSE_ID, 0, "c"),
        (SIM_ID, "", 0, "c"),
        (SIM_ID, EXPOSE_ID, -1, "c"),
        (SIM_ID, EXPOSE_ID, True, "c"),
        (SIM_ID, EXPOSE_ID, 0, ""),
        (SIM_ID, EXPOSE_ID, 0, "a\x1fb"),
        ("a\x1eb", EXPOSE_ID, 0, "c"),
    ],
)
def test_a_bad_part_is_refused_as_the_path_refuses_it(
    sim_id: str, expose_id: str, row_index: Any, column: str
) -> None:
    with pytest.raises(FluxError) as expected:
        path_label(generator_column_path(sim_id, expose_id, row_index, column))
    with pytest.raises(FluxError) as refused:
        CellStreams(root_for(), sim_id, expose_id).label(row_index, column)
    assert refused.value.code == expected.value.code
    assert str(refused.value) == str(expected.value)


# ---------------------------------------------------------------------------
# Whole rows: the generator's own source against the stream table
# ---------------------------------------------------------------------------

_SCHEMA: List[Dict[str, Any]] = [
    {"name": "widget_id", "type": "STRING", "required": True, "semanticType": "identifier"},
    {"name": "token", "type": "UUID", "semanticType": "identifier"},
    {"name": "market", "type": "STRING"},
    {"name": "tier", "type": "STRING"},
    {"name": "count", "type": "INT"},
    {"name": "wait", "type": "DECIMAL(12,3)"},
    {"name": "amount", "type": "DECIMAL(18,4)"},
    {"name": "score", "type": "DECIMAL(9,2)"},
    {"name": "spend", "type": "DECIMAL(18,2)"},
    {"name": "active", "type": "BOOL"},
    {"name": "seen_on", "type": "DATE"},
    {"name": "seen_at", "type": "TIMESTAMP"},
    {"name": "caf\u00e9", "type": "INT"},
]

_FIDELITY = fidelity(
    {
        "token": {"identifierLength": 32},
        "market": {"support": {"values": ["IE", "GB", "FR"]}, "nullFraction": "0.1"},
        "tier": {"levels": {"gold": "0.2", "silver": "0.3", "bronze": "0.5"}},
        "count": {"mean": "3"},
        "wait": {"family": "rate", "rate": "0.25"},
        "amount": {"range": {"min": "0", "max": "10000"}, "nullFraction": "0.05"},
        "score": {"range": {"min": "1", "max": "99"}, "mean": "60", "stddev": "8"},
        "spend": {
            "range": {"min": "0", "max": "300"},
            "bins": ["0", "100", "200", "300"],
            "binWeights": ["1", "1", "1"],
        },
        "active": {"levels": {"true": "0.25", "false": "0.75"}},
        "seen_on": {"range": {"min": "2026-07-01", "max": "2026-09-30"}},
        "seen_at": {"range": {"min": "2026-07-01T00:00:00Z", "max": "2026-09-30T23:59:59Z"}},
        "caf\u00e9": {"range": {"min": "-5", "max": "5"}},
    }
)


def _plan(rows: int = 150) -> Any:
    return build_plan(
        expose_from_schema(_SCHEMA),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=rows,
        fidelity=_FIDELITY,
        time_bounds=TIME_BOUNDS,
    )


def test_the_rows_over_a_root_are_the_rows_over_the_stream_table() -> None:
    plan = _plan()
    root = root_for()
    over_root = generate_rows(plan, root=root)
    over_table = generate_rows(plan, source=GeneratorStreams(root=root))
    assert over_root == over_table
    assert len(over_root) == plan.rows
    # Every value class the plan declares reached the comparison.
    assert {type(value).__name__ for row in over_root for value in row} >= {
        "str",
        "int",
        "Decimal",
        "bool",
        "date",
        "NoneType",
    }


def test_a_column_over_a_root_is_the_column_over_the_stream_table() -> None:
    plan = _plan(rows=60)
    root = root_for()
    for column in plan.expose.column_names:
        assert generate_column(plan, column, root=root) == generate_column(
            plan, column, source=GeneratorStreams(root=root)
        )
