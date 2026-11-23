# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The engine's exact allocation of bins held to vector (c) of ``exactAllocations``.

KERNEL-SPEC-2 section 8.3: the classes of a histogram column are its bins in edge order, weighted by
``binWeights``; a bin's class is its index. This module asks the engine's own
:mod:`flux_engine.generator.allocation` for the counts, the first slots and the class of rows, and
imports nothing of the reference.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root
from flux_engine.generator.allocation import (
    allocate_key,
    allocated_levels,
    class_names,
    class_weights,
    layout_of,
)
from flux_engine.generator.statistics import ColumnStatistics

pytestmark = pytest.mark.unit

STREAMS: Dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "vectors" / "kernel2-streams-vectors.json").read_text(
        encoding="utf-8"
    )
)
CASE = next(case for case in STREAMS["exactAllocations"] if case["name"].startswith("(c)"))


def _root() -> Any:
    return derive_root(
        bytes.fromhex(STREAMS["seam"]),
        int(STREAMS["seed"]),
        seed_tree_version=kernel2.SEED_TREE_VERSION,
    )


def _statistics(null: str = "") -> ColumnStatistics:
    declaration = CASE["declaration"]
    return ColumnStatistics(
        column=CASE["path"][5],
        bins=tuple(Decimal(edge) for edge in declaration["bins"]),
        bin_weights=tuple(Decimal(weight) for weight in declaration["binWeights"]),
        null_fraction=Decimal(null) if null else None,
        allocation="exact",
    )


def test_the_classes_are_the_bins_by_index_with_the_vectors_weights_and_counts() -> None:
    statistics = _statistics()
    assert class_names(statistics) == [0, 1, 2, 3]
    assert [str(weight) for weight in class_weights(statistics)] == CASE["classWeights"]
    names, counts, starts = layout_of(statistics, CASE["rows"])
    assert names == (0, 1, 2, 3)
    assert list(counts) == CASE["counts"] and list(starts) == CASE["starts"]


def test_the_key_is_the_vectors() -> None:
    sim, expose = CASE["path"][1], CASE["path"][3]
    assert allocate_key(_root(), sim, expose, CASE["path"][5]).hex() == CASE["key"]


def test_the_bin_of_every_row_is_the_vector_and_each_bin_holds_its_count() -> None:
    sim, expose = CASE["path"][1], CASE["path"][3]
    rows = np.arange(CASE["rows"], dtype=np.int64)
    found = allocated_levels(_statistics(), _root(), sim, expose, CASE["rows"], rows)
    for chosen in CASE["rowClasses"]:
        assert found[chosen["row"]] == chosen["class"]
    assert [found.count(index) for index in range(4)] == CASE["counts"]


def test_a_null_fraction_adds_null_as_the_last_class() -> None:
    statistics = _statistics("0.1")
    names, counts, _starts = layout_of(statistics, 1000)
    assert names == (0, 1, 2, 3, None)
    assert list(counts) == [90, 180, 270, 360, 100]
