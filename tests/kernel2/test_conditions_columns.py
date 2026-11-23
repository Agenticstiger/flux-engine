# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Kernel 0.2.0's column path draws a conditioned emit as the per-row path does (ADR 0010, 0028).

The per-row path (:func:`flux_engine.generator.rows._conditioned_cell`) is the oracle: a cell's
group chooses the table its uniform is searched in, a fixed cell draws nothing, and the column path
builds the same thresholds and searches the same words. The plan mixes what a conditioned emit meets:
a chain of categorical columns drawn given others, a histogram given a categorical column, a
categorical column with a null fraction, a whole number and a column nothing is given.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root, digest_bytes
from flux_engine.generator import columns
from flux_engine.generator.conditions import with_conditions
from flux_engine.generator.rows import (
    GENERATOR_VERSION_2,
    GeneratorPlan,
    build_plan,
    generate_row_slice,
    generate_rows,
)
from tests.generator.conftest import SIM_ID, TIME_BOUNDS, H, expose_from_schema, fidelity
from tests.generator.test_columns import _both
from tests.generator.test_conditions import group

pytestmark = pytest.mark.unit

_SCHEMA: List[Dict[str, Any]] = [
    {"name": "plan", "type": "STRING"},
    {"name": "region", "type": "STRING"},
    {"name": "channel", "type": "STRING"},
    {"name": "tier", "type": "STRING"},
    {"name": "flag", "type": "BOOLEAN"},
    {"name": "spend", "type": "INT"},
    {"name": "note", "type": "STRING"},
    {"name": "visits", "type": "INT"},
]

_DECLARED: Dict[str, Any] = {
    "plan": {"levels": {"basic": "0.6", "premium": "0.4"}},
    "region": {
        "given": "plan",
        "levelWeightsGiven": {
            "basic": {"APAC": "1", "EU": "3", "US": "1"},
            "premium": {"APAC": "2", "EU": "1", "US": "2"},
        },
    },
    "channel": {
        "given": "region",
        "levelWeightsGiven": {
            "APAC": {"online": "1", "store": "1"},
            "EU": {"online": "1", "store": "3"},
            "US": {"online": "4", "store": "1"},
        },
    },
    "tier": {
        "given": ["plan", "channel"],
        "levelWeightsGiven": {
            "basic": {
                "online": {"gold": "1", "silver": "2"},
                "store": {"gold": "0", "silver": "1"},
            },
            "premium": {
                "online": {"gold": "3", "silver": "1"},
                "store": {"gold": "1", "silver": "1"},
            },
        },
    },
    "flag": {"levels": {"true": "0.5", "false": "0.5"}},
    "spend": {
        "range": {"min": "0", "max": "300"},
        "bins": ["0", "100", "300"],
        "given": "plan",
        "binWeightsGiven": {"basic": ["3", "1"], "premium": ["1", "3"]},
    },
    "note": {"nullFraction": "0.5", "levels": {"a": "0.5", "b": "0.5"}},
    "visits": {"mean": "4", "range": {"min": "0", "max": "50"}, "rate": "0.25"},
}

_GROUPS = [
    group({"tier": "gold"}, 90),
    group({"plan": "premium", "region": "EU"}, 60),
    group({"flag": "true", "channel": "store"}, 50),
    group({"plan": "basic", "tier": "silver", "channel": "online"}, 100),
]
_ROWS = 300


def _plan(rows: int = _ROWS) -> GeneratorPlan:
    plan = build_plan(
        expose_from_schema(_SCHEMA),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=rows,
        fidelity=fidelity(_DECLARED),
        time_bounds=TIME_BOUNDS,
        generator_version=GENERATOR_VERSION_2,
    )
    return with_conditions(plan, _GROUPS)


def _root(seed: int = 42) -> Any:
    return derive_root(digest_bytes(H), seed, seed_tree_version=kernel2.SEED_TREE_VERSION)


@pytest.mark.parametrize("chunk", [1, 7, 64, columns.CHUNK_ROWS])
def test_the_column_path_holds_the_per_row_rows_of_a_conditioned_emit(
    monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(columns, "CHUNK_ROWS", chunk)
    plan, root = _plan(), _root()
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]
    rows = generate_rows(plan, root=root)
    assert {row[3] for row in rows[:90]} == {"gold"}
    assert {(row[0], row[1]) for row in rows[90:150]} == {("premium", "EU")}
    assert {(row[4], row[2]) for row in rows[150:200]} == {(True, "store")}
    assert {(row[0], row[3], row[2]) for row in rows[200:]} == {("basic", "silver", "online")}


@pytest.mark.parametrize("seed", [0, 1, 7, 99])
def test_it_agrees_under_any_seed(monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    plan, root = _plan(), _root(seed)
    by_columns, by_rows, taken = _both(monkeypatch, lambda: generate_rows(plan, root=root))
    assert by_columns == by_rows
    assert taken == [True]


@pytest.mark.parametrize(("start", "stop"), [(0, 1), (89, 91), (140, 260), (299, 300)])
def test_a_slice_across_group_boundaries_agrees_on_both_paths(
    monkeypatch: pytest.MonkeyPatch, start: int, stop: int
) -> None:
    plan, root = _plan(), _root()
    by_columns, by_rows, taken = _both(
        monkeypatch, lambda: generate_row_slice(plan, start, stop, root=root)
    )
    assert by_columns == by_rows
    assert taken == [True]
    assert (
        generate_row_slice(plan, start, stop, root=root)
        == generate_rows(plan, root=root)[start:stop]
    )


def test_a_column_nothing_depends_on_is_drawn_as_without_conditions() -> None:
    """``visits`` is given nothing and nothing is given it: its cells are the plain plan's."""
    plain = build_plan(
        expose_from_schema(_SCHEMA),
        sim_id=SIM_ID,
        seam_digest=H,
        rows=_ROWS,
        fidelity=fidelity(_DECLARED),
        time_bounds=TIME_BOUNDS,
        generator_version=GENERATOR_VERSION_2,
    )
    root = _root()
    assert [row[7] for row in generate_rows(_plan(), root=root)] == [
        row[7] for row in generate_rows(plain, root=root)
    ]
