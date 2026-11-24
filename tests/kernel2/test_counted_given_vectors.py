# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The counted layout given a parent column, held to ``kernel2-counted-given-vectors.json``.

KERNEL-SPEC-2 section 8.1, extended: the eligible parents are split into classes by the given
column's value on each parent's final row, keeping their order. Class ``l`` has its own weights
over the classes of ``children``; ``sigma`` is taken over every eligible parent, as without a
given column, and the parents of a class in ascending order of ``sigma`` take its slots in order.
With a hook the rank coupling applies inside each class. The minimum, the allotment and the rows
are the ungrouped layout's. No seed path is added: ``K`` is the key of the ``count`` path.

The vector file holds, per layout, the path's key, ``sigma``, the class of every eligible parent,
each class's slot values, the expected counts as exact fractions, the counts per parent row and
their offsets. The values are written out by a reference built from the specification and the
reference ``permute/2`` of ``tests/kernel2/reference.py``, never typed; the first test fails until
the file matches. Regenerate it with::

    python -m tests.kernel2.test_counted_given_vectors

The rest of the module asks the engine's :func:`flux_engine.generator.counts.counted_layout` for
each layout. The existing ``countedLayouts`` of the streams vectors are not touched.
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root
from flux_engine.generator.counts import count_key, counted_layout
from tests.kernel2 import reference as k2

pytestmark = pytest.mark.unit

VECTORS = Path(__file__).resolve().parents[1] / "vectors"
FILE = VECTORS / "kernel2-counted-given-vectors.json"
REGENERATE = "python -m tests.kernel2.test_counted_given_vectors"

SEAM = bytes(range(32, 64))
SEED = 20261124
ROOT = k2.root(SEAM, SEED)
PATH = ["sim", "s", "gen", "invoice", "count", "account"]

BINS = ["0", "1", "3", "10", "40"]


def _shape(weights: Sequence[str]) -> Dict[str, Any]:
    return {"bins": BINS, "binWeights": list(weights)}


BASE = ["0.10", "0.40", "0.35", "0.15"]
BUSINESS = ["0.02", "0.18", "0.45", "0.35"]
PERSONAL = ["0.12", "0.48", "0.32", "0.08"]


def _groups(e: int, classes: int) -> List[int]:
    """The class of each eligible parent in rank order: a fixed, uneven pattern."""
    return [(rank * 7 + rank // 3) % classes for rank in range(e)]


def _reference(
    *,
    parents: int,
    eligible: Optional[Sequence[int]],
    child_rows: int,
    minimum: int,
    shapes: Sequence[Dict[str, Any]],
    groups: Sequence[int],
    hook: Optional[Sequence[Fraction]],
) -> Dict[str, Any]:
    rows = list(range(parents)) if eligible is None else list(eligible)
    e = len(rows)
    key = k2.count_key(ROOT, PATH)
    sigma = [k2.permute(key, e, r) for r in range(e)]
    x = [Fraction(0)] * e
    slots: List[List[int]] = []
    for level, shape in enumerate(shapes):
        members = [r for r in range(e) if groups[r] == level]
        held = k2.slot_values(len(members), shape) if members else []
        slots.append(held)
        by = (lambda r: (sigma[r],)) if hook is None else (lambda r: (hook[r], sigma[r]))
        for position, r in enumerate(sorted(members, key=by)):
            x[r] = Fraction(held[position])
    per_rank = [minimum + a for a in k2._allot(child_rows - minimum * e, x, minimum)]
    counts = [0] * parents
    for row, count in zip(rows, per_rank, strict=True):
        counts[row] = count
    return {
        "key": key.hex(),
        "e": e,
        "sigma": sigma,
        "slots": slots,
        "expectedCounts": [str(value) for value in x],
        "counts": counts,
        "offsets": [sum(counts[:p]) for p in range(parents)],
    }


def _case(
    name: str,
    *,
    parents: int,
    child_rows: int,
    classes: Sequence[str],
    shapes: Sequence[Sequence[str]],
    eligible: Optional[Sequence[int]] = None,
    minimum: int = 0,
    groups: Optional[Sequence[int]] = None,
    hook: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    e = parents if eligible is None else len(eligible)
    held = list(groups) if groups is not None else _groups(e, len(classes))
    made = [_shape(weights) for weights in shapes]
    found = _reference(
        parents=parents,
        eligible=eligible,
        child_rows=child_rows,
        minimum=minimum,
        shapes=made,
        groups=held,
        hook=None if hook is None else [Fraction(text) for text in hook],
    )
    return {
        "name": name,
        "path": PATH,
        "parentRows": parents,
        "eligible": None if eligible is None else list(eligible),
        "childRows": child_rows,
        "minChildren": minimum,
        "classes": list(classes),
        "shapes": made,
        "groups": held,
        "hook": None if hook is None else list(hook),
        **found,
    }


def counted_given_vectors() -> Dict[str, Any]:
    return {
        "description": (
            "KERNEL-SPEC-2 section 8.1 given a parent column: the eligible parents split by the "
            "class of the given column on their final rows, each class with its own weights over "
            "the classes of children; sigma over every eligible parent, the parents of a class "
            "in ascending order of sigma taking its slots in order, the rank coupling of a hook "
            "within each class. The path's key, sigma, the class index of each eligible parent, "
            "each class's slot values, the expected counts, the counts per parent row and their "
            "offsets. Keys are hex, other integers decimal. Computed by "
            "tests/kernel2/test_counted_given_vectors.py, never typed."
        ),
        "regenerate": REGENERATE,
        "seam": SEAM.hex(),
        "seed": str(SEED),
        "root": ROOT.hex(),
        "layouts": [
            _case(
                "(a) two classes, a level each",
                parents=24,
                child_rows=60,
                classes=["business", "personal"],
                shapes=[BUSINESS, PERSONAL],
            ),
            _case(
                "(b) three classes, the second with no list takes the shape's weights",
                parents=30,
                child_rows=75,
                classes=["a", "b", "c"],
                shapes=[BUSINESS, BASE, PERSONAL],
            ),
            _case(
                "(c) a class with no parent, a minimum of one",
                parents=18,
                child_rows=40,
                classes=["a", "b", "c"],
                shapes=[BUSINESS, PERSONAL, BASE],
                minimum=1,
                groups=[0, 2] * 9,
            ),
            _case(
                "(d) a parent condition, classes over the eligible parents",
                parents=20,
                child_rows=33,
                classes=["0", "100"],
                shapes=[PERSONAL, BUSINESS],
                eligible=[0, 1, 2, 4, 5, 7, 8, 9, 11, 12, 14, 15, 17, 18],
            ),
            _case(
                "(e) a hook, ranked into each class's slots",
                parents=16,
                child_rows=44,
                classes=["business", "personal"],
                shapes=[BUSINESS, PERSONAL],
                hook=[
                    "3",
                    "1",
                    "4",
                    "1",
                    "5",
                    "9",
                    "2",
                    "6",
                    "5",
                    "3",
                    "5",
                    "8",
                    "9",
                    "7",
                    "9",
                    "3",
                ],
            ),
            _case(
                "(f) one class holding every parent, the ungrouped layout",
                parents=12,
                child_rows=30,
                classes=["only"],
                shapes=[BASE],
                groups=[0] * 12,
            ),
        ],
    }


def _render(vectors: Dict[str, Any]) -> str:
    return json.dumps(vectors, indent=2) + "\n"


STORED: Dict[str, Any] = (
    json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else counted_given_vectors()
)
CASES: List[Dict[str, Any]] = STORED["layouts"]


def test_the_vector_file_is_current() -> None:
    assert FILE.read_text(encoding="utf-8") == _render(counted_given_vectors()), f"run {REGENERATE}"


def test_the_layouts_hold_what_they_say() -> None:
    assert all(sum(case["counts"]) == case["childRows"] for case in CASES)
    assert any(case["hook"] is not None for case in CASES)
    assert any(len(set(case["groups"])) < len(case["classes"]) for case in CASES)


def _engine(case: Dict[str, Any]) -> Any:
    root = derive_root(SEAM, SEED, seed_tree_version=kernel2.SEED_TREE_VERSION)
    hook = case["hook"]
    return counted_layout(
        count_key(root, case["path"][1], case["path"][3], case["path"][5]),
        case["parentRows"],
        case["childRows"],
        minimum=case["minChildren"],
        eligible=case["eligible"],
        expected=None if hook is None else [Fraction(text) for text in hook],
        class_shapes=case["shapes"],
        groups=case["groups"],
    )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_engines_counts_and_offsets_are_the_vectors(case: Dict[str, Any]) -> None:
    layout = _engine(case)
    assert layout.counts.tolist() == case["counts"]
    assert layout.offsets.tolist() == case["offsets"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_key_is_the_count_paths_and_sigma_is_the_ungrouped_permutation(
    case: Dict[str, Any],
) -> None:
    from flux_engine.generator.counts import sigma_of

    root = derive_root(SEAM, SEED, seed_tree_version=kernel2.SEED_TREE_VERSION)
    key = count_key(root, case["path"][1], case["path"][3], case["path"][5])
    assert key.hex() == case["key"]
    assert sigma_of(key, case["e"]) == case["sigma"]


def test_one_class_of_every_parent_is_the_layout_without_a_given_column() -> None:
    case = CASES[-1]
    root = derive_root(SEAM, SEED, seed_tree_version=kernel2.SEED_TREE_VERSION)
    plain = counted_layout(
        count_key(root, "s", "invoice", "account"),
        case["parentRows"],
        case["childRows"],
        shape=case["shapes"][0],
    )
    assert plain.counts.tolist() == case["counts"]


def main() -> None:
    FILE.write_text(_render(counted_given_vectors()), encoding="utf-8")
    sys.stdout.write(f"wrote {FILE}\n")


if __name__ == "__main__":
    main()
