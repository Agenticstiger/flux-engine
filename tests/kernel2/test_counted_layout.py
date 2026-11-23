# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The engine's counted layout held to ``countedLayouts`` of the streams vector file.

KERNEL-SPEC-2 section 8.1: the layout is a pure function of the child rows, the parent rows and the
declaration. The vector file pins, per chosen layout, the path's key label and key, ``sigma`` for
every rank, the slot values, the expected counts, the counts per parent row with their offsets, and
the parent of a first, a last and a boundary child row. This module asks the engine's own
:mod:`flux_engine.generator.counts` for each of them. It imports nothing of the reference.

The bundle keys that make a minimum, a condition and a hook are not read yet, but the pure
function takes them, so every vector is asserted through its parameters.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import derive_root, key_label
from flux_engine.generator.counts import (
    CountedLayout,
    class_counts,
    classes_of,
    count_key,
    counted_layout,
    sigma_of,
    slot_values,
)
from flux_engine.generator.paths import COUNT_PATH_SHAPE

pytestmark = pytest.mark.unit

VECTORS = Path(__file__).resolve().parents[1] / "vectors"
STREAMS: Dict[str, Any] = json.loads(
    (VECTORS / "kernel2-streams-vectors.json").read_text(encoding="utf-8")
)
CASES: List[Dict[str, Any]] = STREAMS["countedLayouts"]


def _root() -> Any:
    return derive_root(
        bytes.fromhex(STREAMS["seam"]),
        int(STREAMS["seed"]),
        seed_tree_version=kernel2.SEED_TREE_VERSION,
    )


def _layout(case: Dict[str, Any]) -> CountedLayout:
    sim, expose, relationship = case["path"][1], case["path"][3], case["path"][5]
    hook = case["hook"]
    return counted_layout(
        count_key(_root(), sim, expose, relationship),
        case["parentRows"],
        case["childRows"],
        shape=case["shape"],
        minimum=case["minChildren"],
        eligible=case["eligible"],
        expected=None if hook is None else [Fraction(text) for text in hook],
        one_to_one=case.get("oneToOne", False),
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_key_label_and_the_key_are_the_vectors(case: Dict[str, Any]) -> None:
    label, ordinals = key_label(tuple(case["path"]), COUNT_PATH_SHAPE)
    assert label.hex() == case["keyLabel"] and ordinals == (0, 0)
    sim, expose, relationship = case["path"][1], case["path"][3], case["path"][5]
    key = count_key(_root(), sim, expose, relationship)
    assert key.hex() == case["key"]
    assert len(key) == 32 and key[:16] == bytes.fromhex(case["key"])[:16]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_sigma_is_the_permutation_of_the_ranks(case: Dict[str, Any]) -> None:
    key = bytes.fromhex(case["key"])
    assert sigma_of(key, case["e"]) == case["sigma"]
    assert sorted(case["sigma"]) == list(range(case["e"]))


@pytest.mark.parametrize(
    "case", [case for case in CASES if case["shape"] is not None], ids=lambda case: case["name"]
)
def test_the_slot_values_are_the_vectors(case: Dict[str, Any]) -> None:
    slots = slot_values(case["e"], classes_of(case["shape"]))
    assert slots.tolist() == case["slots"]
    assert slots.tolist() == sorted(slots.tolist())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_counts_and_offsets_are_the_vectors(case: Dict[str, Any]) -> None:
    layout = _layout(case)
    assert layout.counts.tolist() == case["counts"]
    assert layout.offsets.tolist() == case["offsets"]
    assert int(layout.counts.sum()) == case["childRows"]
    if case["eligible"] is not None:
        outside = set(range(case["parentRows"])) - set(case["eligible"])
        assert all(layout.counts[row] == 0 for row in outside)
    assert all(
        layout.counts[row] >= case["minChildren"]
        for row in (range(case["parentRows"]) if case["eligible"] is None else case["eligible"])
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_chosen_child_rows_take_their_vector_parents(case: Dict[str, Any]) -> None:
    layout = _layout(case)
    for chosen in case["childParents"]:
        assert layout.parent_of(chosen["childRow"]) == chosen["parentRow"]
    rows = np.arange(case["childRows"], dtype=np.int64)
    parents = layout.parents_of(rows)
    assert parents.tolist() == [layout.parent_of(row) for row in range(case["childRows"])]
    for parent in range(case["parentRows"]):
        run = layout.children_of(parent)
        assert len(run) == case["counts"][parent]
        assert all(layout.parent_of(row) == parent for row in run)


def test_a_constant_hook_gives_the_counts_of_the_shape_alone() -> None:
    shaped, constant = CASES[2], CASES[6]
    assert _layout(shaped).counts.tolist() == _layout(constant).counts.tolist()


def test_the_levels_case_meets_its_declared_histogram_exactly() -> None:
    case = CASES[2]
    counts = _layout(case).counts.tolist()
    weights = [Fraction(text) for text in case["shape"]["levelWeights"]]
    held = [counts.count(int(level)) for level in case["shape"]["levels"]]
    assert held == [int(case["e"] * weight) for weight in weights]
    assert (
        sum(level * n for level, n in zip(map(int, case["shape"]["levels"]), held, strict=True))
        == case["childRows"]
    )


def test_the_class_counts_follow_the_largest_remainder_with_ties_to_the_lower_class() -> None:
    classes = classes_of({"levels": ["0", "1", "2"], "levelWeights": ["0.5", "0.25", "0.25"]})
    assert class_counts(1, classes) == [1, 0, 0]
    assert class_counts(2, classes) == [1, 1, 0]
    assert class_counts(3, classes) == [1, 1, 1]
    assert class_counts(10, classes) == [5, 3, 2]
    assert class_counts(10, classes) == class_counts(10, classes)


@pytest.mark.parametrize(
    ("children", "needle"),
    [
        ({"levels": ["1", "1"], "levelWeights": ["0.5", "0.5"]}, "strictly ascending"),
        ({"levels": ["2", "1"], "levelWeights": ["0.5", "0.5"]}, "strictly ascending"),
        ({"levels": ["-1", "1"], "levelWeights": ["0.5", "0.5"]}, "whole number"),
        ({"levels": ["1.5"], "levelWeights": ["1"]}, "whole number"),
        ({"levels": ["1", "2"], "levelWeights": ["0.5", "0.4"]}, "sum to"),
        ({"levels": ["1", "2"], "levelWeights": ["1"]}, "one weight each"),
        ({"bins": ["3"], "binWeights": []}, "at least two"),
        ({"bins": ["1", "4", "3"], "binWeights": ["0.5", "0.5"]}, "strictly ascending"),
        ({"bins": ["1", "2"], "levelWeights": ["1"]}, "nothing else"),
    ],
)
def test_a_shape_that_is_not_whole_ascending_counts_with_weights_of_one_is_refused(
    children: Dict[str, Any], needle: str
) -> None:
    with pytest.raises(FluxError) as caught:
        classes_of(children)
    assert caught.value.code.code == "FLX-3403" and needle in str(caught.value)


def test_the_layout_refuses_what_it_cannot_lay_out() -> None:
    key = bytes(32)
    for call, needle in (
        (lambda: counted_layout(key, 5, 4, minimum=1), "cannot give each"),
        (lambda: counted_layout(key, 0, 4), "no parent is eligible"),
        (lambda: counted_layout(key, 5, 9, eligible=[3, 1]), "ascending"),
        (lambda: counted_layout(key, 3, 6, expected=[Fraction(1)]), "one expected count"),
        (lambda: counted_layout(key, 2, 4, expected=[Fraction(-1), Fraction(2)]), "not negative"),
        (lambda: counted_layout(key, 3, 6, one_to_one=True), "no parent more than one"),
    ):
        with pytest.raises(FluxError) as caught:
            call()
        assert caught.value.code.code == "FLX-3403" and needle in str(caught.value)
    assert counted_layout(key, 4, 0).counts.tolist() == [0, 0, 0, 0]
    assert counted_layout(key, 3, 3, one_to_one=True).counts.tolist() == [1, 1, 1]


def _uncapped(case: Dict[str, Any]) -> CountedLayout:
    sim, expose, relationship = case["path"][1], case["path"][3], case["path"][5]
    hook = case["hook"]
    return counted_layout(
        count_key(_root(), sim, expose, relationship),
        case["parentRows"],
        case["childRows"],
        shape=case["shape"],
        minimum=case["minChildren"],
        eligible=case["eligible"],
        expected=None if hook is None else [Fraction(text) for text in hook],
    )


ONE_TO_ONE_CASES = [case for case in CASES if case.get("oneToOne")]


@pytest.mark.parametrize("case", ONE_TO_ONE_CASES, ids=lambda case: case["name"])
def test_a_capped_layout_gives_no_parent_a_second_child_and_keeps_every_other_count(
    case: Dict[str, Any],
) -> None:
    capped, plain = _layout(case), _uncapped(case)
    assert max(capped.counts.tolist()) == 1 and int(capped.counts.sum()) == case["childRows"]
    # A parent the allotment gave one child or none keeps it; only the spare children move.
    assert all(c >= min(p, 1) for c, p in zip(capped.counts, plain.counts, strict=True))
    assert all(
        c == p for c, p in zip(capped.counts, plain.counts, strict=True) if p <= 1 and c == 0
    )


def test_the_cap_is_step_4_whenever_step_4_gives_no_parent_a_second_child() -> None:
    key = bytes(range(32))
    shape = {"levels": ["0", "1"], "levelWeights": ["0.5", "0.5"]}
    for parents, children in ((8, 4), (8, 3), (9, 2), (10, 5)):
        plain = counted_layout(key, parents, children, shape=shape)
        assert max(plain.counts.tolist()) <= 1
        assert counted_layout(
            key, parents, children, shape=shape, one_to_one=True
        ).counts.tolist() == (plain.counts.tolist())


def test_the_spare_children_go_to_the_parents_with_the_largest_y_and_a_tie_to_the_lower_rank() -> (
    None
):
    key = bytes(32)
    # Step 4 gives [0, 1, 1, 1, 0, 0, 3] over the hook's weights; the two spare children go to the
    # childless ranks by y: rank 4 (y = 1) first, then rank 0 (y = 0) before rank 5 (y = 0).
    hook = [Fraction(v) for v in (0, 5, 2, 2, 1, 0, 9)]
    plain = counted_layout(key, 7, 6, expected=hook)
    assert plain.counts.tolist() == [0, 1, 1, 1, 0, 0, 3]
    assert counted_layout(key, 7, 6, expected=hook, one_to_one=True).counts.tolist() == [
        1,
        1,
        1,
        1,
        1,
        0,
        1,
    ]


def test_a_capped_layout_under_a_scale_like_rounding_never_refuses_up_to_the_eligible_parents() -> (
    None
):
    key = bytes(range(32))
    shape = {"levels": ["0", "1"], "levelWeights": ["0.5", "0.5"]}
    for parents in range(2, 40):
        for children in range(0, parents + 1):
            counts = counted_layout(key, parents, children, shape=shape, one_to_one=True).counts
            assert max(counts.tolist(), default=0) <= 1 and int(counts.sum()) == children
        with pytest.raises(FluxError) as caught:
            counted_layout(key, parents, parents + 1, shape=shape, one_to_one=True)
        assert caught.value.code.code == "FLX-3403" and "more than its" in str(caught.value)


def test_a_one_to_one_minimum_above_one_is_refused_and_one_fills_every_parent() -> None:
    key = bytes(32)
    with pytest.raises(FluxError) as caught:
        counted_layout(key, 3, 3, minimum=2, one_to_one=True)
    assert caught.value.code.code == "FLX-3403"
    assert counted_layout(key, 4, 4, minimum=1, one_to_one=True).counts.tolist() == [1, 1, 1, 1]
    with pytest.raises(FluxError):
        counted_layout(key, 4, 5, minimum=1, one_to_one=True)


def test_a_child_row_outside_the_layout_is_refused() -> None:
    layout = counted_layout(bytes(32), 3, 6)
    with pytest.raises(IndexError):
        layout.parent_of(6)
    with pytest.raises(IndexError):
        layout.parents_of(np.array([0, 6], dtype=np.int64))


def test_a_layout_over_a_million_parents_meets_its_histogram_and_totals_exactly() -> None:
    parents = 1_000_000
    shape = {
        "levels": ["0", "1", "2", "3", "5", "9"],
        "levelWeights": ["0.05", "0.25", "0.30", "0.20", "0.15", "0.05"],
    }
    slots = slot_values(parents, classes_of(shape))
    children = int(slots.sum())
    layout = counted_layout(bytes(range(32)), parents, children, shape=shape)
    assert int(layout.counts.sum()) == children
    assert np.bincount(layout.counts).tolist() == np.bincount(slots).tolist()
    assert layout.parent_of(children - 1) == int(np.flatnonzero(layout.counts)[-1])


# -- a weight or a count with an exponent cannot hang or crash the layout ----------------------

HUGE_SHAPES = [
    ({"levels": ["1", "2"], "levelWeights": ["1", "1E-999999999"]}, "too many digits"),
    ({"levels": ["1", "2"], "levelWeights": ["1E+999999999", "0"]}, "too many digits"),
    ({"bins": ["1", "2", "3"], "binWeights": ["0.5", "5E-1000000"]}, "too many digits"),
    ({"levels": ["1E+999999999", "1E+9999999999"], "levelWeights": ["1", "0"]}, "too many digits"),
    ({"levels": ["1", "2"], "levelWeights": ["Infinity", "0"]}, "no decimal"),
    ({"levels": ["1", "2"], "levelWeights": ["NaN", "1"]}, "no decimal"),
]


@pytest.mark.parametrize(("shape", "needle"), HUGE_SHAPES, ids=lambda value: str(value)[:40])
def test_a_weight_or_count_with_a_huge_exponent_is_refused_at_once(
    shape: Dict[str, Any], needle: str
) -> None:
    import time

    classes_of({"levels": ["1"], "levelWeights": ["1"]})  # the first call imports
    started = time.monotonic()
    with pytest.raises(FluxError) as caught:
        classes_of(shape)
    assert caught.value.code.code == "FLX-3403" and needle in str(caught.value)
    with pytest.raises(FluxError) as layout:
        counted_layout(bytes(32), 5, 9, shape=shape)
    assert layout.value.code.code == "FLX-3403"
    assert time.monotonic() - started < 1.0


def test_a_weight_at_the_edge_of_the_bound_is_accepted_and_laid_out() -> None:
    # 1E-1000 needs 1000 digits written out, the most a fidelity block's number may need.
    shape = {"levels": ["1", "2"], "levelWeights": ["1", "1E-1000"]}
    assert [cls.weight for cls in classes_of(shape)][1] == Decimal("1E-1000")
    assert counted_layout(bytes(32), 3, 3, shape=shape).counts.tolist() == [1, 1, 1]
    with pytest.raises(FluxError):
        classes_of({"levels": ["1", "2"], "levelWeights": ["1", "1E-1001"]})


def test_the_remainder_method_refuses_an_exponent_beyond_4096_before_it_forms_the_fraction() -> (
    None
):
    import time

    from flux_engine.core.lrm import FLX_5020, MOST_EXPONENT, largest_remainder

    assert MOST_EXPONENT == 4096
    started = time.monotonic()
    for text in ("1E-999999999", "1E+999999999", "1E-4097"):
        with pytest.raises(FluxError) as caught:
            largest_remainder(3, {"a": Decimal("1"), "b": Decimal(text)})
        assert caught.value.code is FLX_5020
    with pytest.raises(FluxError) as caught:
        largest_remainder(3, {"a": Decimal("1"), "b": Decimal("NaN")})
    assert caught.value.code is FLX_5020
    assert time.monotonic() - started < 1.0
    assert largest_remainder(3, {"a": Decimal("1"), "b": Decimal("1E-4096")}) == {"a": 3, "b": 0}


def test_the_exponent_guard_reads_decimals_only_so_a_fraction_or_an_int_weight_still_allots() -> (
    None
):
    from flux_engine.core.lrm import largest_remainder

    weights: Any = {"a": Fraction(1, 3), "b": Fraction(2, 3)}
    assert largest_remainder(9, weights) == {"a": 3, "b": 6}
    # A Fraction of a huge denominator is no Decimal exponent, and an int is exact.
    assert largest_remainder(4, {"a": Fraction(1, 10**5000), "b": 1 - Fraction(1, 10**5000)}) == {
        "a": 0,
        "b": 4,
    }
    assert largest_remainder(5, {"a": 1, "b": 0}) == {"a": 5, "b": 0}
