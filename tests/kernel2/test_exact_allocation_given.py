# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The engine's exact allocation of a column given an exact column, held to vector (d).

KERNEL-SPEC-2 section 8.3: the rows of a parent class are its slots, a row's index in the group is
its slot less the class's first slot, and the group's own counts and permutation give the row its
class, so every pair of a parent class and a class of the column holds its count. This module asks
the engine's own :mod:`flux_engine.generator.allocation` and imports nothing of the reference.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import derive_root
from flux_engine.generator.allocation import allocate_key, allocated_given, allocated_levels
from flux_engine.generator.statistics import ColumnStatistics

pytestmark = pytest.mark.unit

STREAMS: Dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "vectors" / "kernel2-streams-vectors.json").read_text(
        encoding="utf-8"
    )
)
CASES = {case["name"][:3]: case for case in STREAMS["exactAllocations"]}
CASE = CASES["(d)"]
PARENT_CASE = CASES["(a)"]


def _root() -> Any:
    return derive_root(
        bytes.fromhex(STREAMS["seam"]),
        int(STREAMS["seed"]),
        seed_tree_version=kernel2.SEED_TREE_VERSION,
    )


def _parent() -> ColumnStatistics:
    declaration = PARENT_CASE["declaration"]
    return ColumnStatistics(
        column=CASE["parentPath"][5],
        levels={
            name: Decimal(weight)
            for name, weight in zip(declaration["levels"], declaration["levelWeights"], strict=True)
        },
        allocation="exact",
    )


def _child() -> ColumnStatistics:
    declaration = CASE["declaration"]
    parent_levels = PARENT_CASE["declaration"]["levels"]
    return ColumnStatistics(
        column=CASE["path"][5],
        levels={name: Decimal(1) for name in declaration["levels"]},
        given=CASE["parentPath"][5],
        level_weights_given={
            level: {name: Decimal(weight) for name, weight in table.items()}
            for level, table in zip(parent_levels, declaration["levelWeightsGiven"], strict=True)
        },
        allocation="exact",
    )


def _classes(rows: int, at: Any) -> Any:
    sim, expose = CASE["path"][1], CASE["path"][3]
    parent = allocated_levels(_parent(), _root(), sim, expose, rows, at)
    child = allocated_given(_child(), _parent(), _root(), sim, expose, rows, at)
    return parent, child


def test_the_key_is_the_vectors() -> None:
    sim, expose = CASE["path"][1], CASE["path"][3]
    assert allocate_key(_root(), sim, expose, CASE["path"][5]).hex() == CASE["key"]


def test_the_class_of_chosen_rows_is_the_vector() -> None:
    names = CASE["declaration"]["levels"]
    parent_names = PARENT_CASE["declaration"]["levels"]
    parent, child = _classes(CASE["rows"], np.arange(CASE["rows"], dtype=np.int64))
    for chosen in CASE["rowClasses"]:
        assert child[chosen["row"]] == names[chosen["class"]]
        assert parent[chosen["row"]] == parent_names[chosen["parentClass"]]


def test_every_pair_of_a_parent_class_and_a_class_holds_its_count() -> None:
    names = CASE["declaration"]["levels"]
    parent_names = PARENT_CASE["declaration"]["levels"]
    parent, child = _classes(CASE["rows"], np.arange(CASE["rows"], dtype=np.int64))
    pairs = Counter(zip(parent, child, strict=True))
    for level, counts in zip(parent_names, CASE["pairCounts"], strict=True):
        assert [pairs[(level, name)] for name in names] == counts
    assert [sum(row) for row in CASE["pairCounts"]] == CASE["parentClassCounts"]


def test_a_row_alone_takes_the_class_it_takes_among_all_rows() -> None:
    _, child = _classes(CASE["rows"], np.arange(CASE["rows"], dtype=np.int64))
    for row in (0, 7, 25):
        _, alone = _classes(CASE["rows"], np.array([row], dtype=np.int64))
        assert alone == [child[row]]


def test_a_parent_null_class_is_refused_not_a_key_error() -> None:
    """A given column has no level to condition on in a NULL; plan time refuses it first."""
    parent = replace(_parent(), null_fraction=Decimal("0.5"))
    sim, expose = CASE["path"][1], CASE["path"][3]
    rows = CASE["rows"]
    with pytest.raises(FluxError) as caught:
        allocated_given(
            _child(), parent, _root(), sim, expose, rows, np.arange(rows, dtype=np.int64)
        )
    assert caught.value.code.code == "FLX-3402"


def test_a_many_row_per_row_slice_forms_the_class_weights_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from flux_engine.generator import allocation

    calls: list[Any] = []
    original = allocation.class_weights

    def counted(statistics: ColumnStatistics, given_level: Any = None) -> Any:
        calls.append(given_level)
        return original(statistics, given_level)

    monkeypatch.setattr(allocation, "class_weights", counted)
    allocation._CLASSES.clear()
    allocation._layout.cache_clear()
    parent = _parent()
    root = _root()
    sim, expose = CASE["path"][1], CASE["path"][3]
    rows = CASE["rows"]
    together = allocated_levels(parent, root, sim, expose, rows, np.arange(rows, dtype=np.int64))
    calls.clear()
    one_by_one = [
        allocated_levels(parent, root, sim, expose, rows, np.array([row], dtype=np.int64))[0]
        for row in range(rows)
    ]
    assert one_by_one == together
    assert calls == []
    allocation._CLASSES.clear()
    for row in range(rows):
        allocated_levels(parent, root, sim, expose, rows, np.array([row], dtype=np.int64))
    assert len(calls) == 1
