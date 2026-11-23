# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The engine's exact allocation of levels held to ``exactAllocations`` of the streams vector file.

KERNEL-SPEC-2 section 8.3: the classes of a column are a pure function of its declaration, the rows
``N`` and the key of the path ``("sim", sim_id, "gen", exposeId, "allocate", column)``. The vector
file pins the key label and key, the class counts and first slots, and the slot and class of rows.
This module asks the engine's own :mod:`flux_engine.generator.allocation` for each of them, for the
two vectors that allot levels (a: four levels, b: the same with a ``nullFraction``). It imports
nothing of the reference.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root, key_label
from flux_engine.generator.allocation import (
    allocate_key,
    allocated_levels,
    class_names,
    class_weights,
    exact_counts,
    layout_of,
)
from flux_engine.generator.paths import ALLOCATE_PATH_SHAPE
from flux_engine.generator.statistics import ColumnStatistics

pytestmark = pytest.mark.unit

VECTORS = Path(__file__).resolve().parents[1] / "vectors"
STREAMS: Dict[str, Any] = json.loads(
    (VECTORS / "kernel2-streams-vectors.json").read_text(encoding="utf-8")
)
CASES: List[Dict[str, Any]] = [
    case for case in STREAMS["exactAllocations"] if case["name"][:3] in ("(a)", "(b)")
]


def _root() -> Any:
    return derive_root(
        bytes.fromhex(STREAMS["seam"]),
        int(STREAMS["seed"]),
        seed_tree_version=kernel2.SEED_TREE_VERSION,
    )


def _statistics(case: Dict[str, Any]) -> ColumnStatistics:
    declaration = case["declaration"]
    levels = {
        name: Decimal(weight)
        for name, weight in zip(declaration["levels"], declaration["levelWeights"], strict=True)
    }
    null = declaration.get("nullFraction")
    return ColumnStatistics(
        column=case["path"][5],
        levels=levels,
        null_fraction=None if null is None else Decimal(null),
        allocation="exact",
    )


def _where(case: Dict[str, Any]) -> Any:
    return case["path"][1], case["path"][3]


def test_the_vectors_include_the_two_that_allot_levels() -> None:
    assert [case["name"][:3] for case in CASES] == ["(a)", "(b)"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_key_label_and_the_key_are_the_vectors(case: Dict[str, Any]) -> None:
    label, ordinals = key_label(tuple(case["path"]), ALLOCATE_PATH_SHAPE)
    assert label.hex() == case["keyLabel"] and ordinals == (0, 0)
    sim, expose = _where(case)
    key = allocate_key(_root(), sim, expose, case["path"][5])
    assert key.hex() == case["key"]
    assert len(key) == 32 and key[:16] == bytes.fromhex(case["key"])[:16]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_class_weights_counts_and_first_slots_are_the_vectors(case: Dict[str, Any]) -> None:
    statistics = _statistics(case)
    weights = class_weights(statistics)
    assert [str(weight) for weight in weights] == case["classWeights"]
    assert sum(weights) == 1
    assert exact_counts(case["rows"], weights) == case["counts"]
    names, counts, starts = layout_of(statistics, case["rows"])
    assert list(counts) == case["counts"] and list(starts) == case["starts"]
    assert len(names) == len(case["counts"])
    assert (names[-1] is None) == ("nullFraction" in case["declaration"])


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_class_of_every_row_is_the_vector(case: Dict[str, Any]) -> None:
    statistics = _statistics(case)
    names, _counts, _starts = layout_of(statistics, case["rows"])
    sim, expose = _where(case)
    rows = np.arange(case["rows"], dtype=np.int64)
    found = allocated_levels(statistics, _root(), sim, expose, case["rows"], rows)
    for chosen in case["rowClasses"]:
        assert found[chosen["row"]] == names[chosen["class"]]
    held = [found.count(name) for name in names]
    assert held == case["counts"]


def test_equal_weights_over_a_support_and_normalised_declared_weights() -> None:
    support = ColumnStatistics(column="c", support=("b", "a", "c"), allocation="exact")
    assert class_weights(support)[0] == class_weights(support)[1]
    assert layout_of(support, 10)[1] == (4, 3, 3)
    counted = ColumnStatistics(
        column="c", levels={"a": Decimal("2"), "b": Decimal("6")}, allocation="exact"
    )
    assert [str(weight) for weight in class_weights(counted)] == ["1/4", "3/4"]


def test_a_value_other_than_exact_is_refused_as_a_bundle_key() -> None:
    from flux_engine.core.errors import FluxError
    from flux_engine.generator.statistics import statistics_for
    from flux_engine.seam.contract import CompiledColumn, LogicalKind, LogicalType

    column = CompiledColumn(
        name="c",
        logical_type=LogicalType(kind=LogicalKind.STRING, precision=None, scale=None),
        required=False,
        declared_type="STRING",
        semantic_type=None,
        tags=(),
        key=False,
    )
    block = {"columns": {"c": {"levels": {"a": "1"}, "allocation": "exact"}}}
    assert statistics_for(column, block).allocation == "exact"
    block["columns"]["c"]["allocation"] = "even"
    with pytest.raises(FluxError) as caught:
        statistics_for(column, block)
    assert caught.value.code.code == "FLX-3105"


def test_classes_are_in_nfc_order_of_their_names() -> None:
    decomposed, composed = "éa", "éb"  # NFC forms: "éa" and "éb"
    statistics = ColumnStatistics(
        column="c",
        levels={composed: Decimal("0.5"), decomposed: Decimal("0.5")},
        allocation="exact",
    )
    assert class_names(statistics) == [decomposed, composed]
