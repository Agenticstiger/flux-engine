# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An association's pairing, held to ``kernel2-association-vectors.json`` (KERNEL-SPEC-2 section 8.7).

Each side's rows per parent are the counted layout of its ``count`` path over the ``E`` rows; with
``distinct`` they are capped at the other side's parents and moved until they meet the Gale-Ryser
condition. Row ``r``'s second-side stub is ``permute/2(K, E, r)`` under the key ``K`` of the path
``("sim", s, "gen", e, "associate", a, b, "stubs")``, and a repeated pair is repaired by the keyed
switches of the path ending ``"switches"``, a Havel-Hakimi pass over the rows left, or a whole
Havel-Hakimi layout.

The values are written out by a reference built from the specification, plain Python over the
reference ``permute/2`` and counted layout of ``tests/kernel2/reference.py``, never typed; the first
test fails until the file matches. Regenerate it with::

    python -m tests.kernel2.test_association_vectors

The rest of the module asks the engine's :func:`flux_engine.generator.associations.pair_layout` for
each case.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root
from flux_engine.generator.associations import PairedSide, pair_keys, pair_layout
from flux_engine.seam.counts import ChildShape
from tests.kernel2 import reference as k2

pytestmark = pytest.mark.unit

VECTORS = Path(__file__).resolve().parents[1] / "vectors"
FILE = VECTORS / "kernel2-association-vectors.json"
REGENERATE = "python -m tests.kernel2.test_association_vectors"

SEAM = bytes(range(64, 96))
SEED = 20261125
ROOT = k2.root(SEAM, SEED)
SIM, EMIT, PAIR = "s", "membership", ("member", "club")
ATTEMPTS = 64


def _key(purpose: str) -> bytes:
    path = ["sim", SIM, "gen", EMIT, "associate", PAIR[0], PAIR[1], purpose]
    label, _ = k2.split_path(path)
    return hashlib.sha256(
        k2.SEED_TREE_VERSION.encode("utf-8") + k2.NUL + ROOT + k2.NUL + label
    ).digest()


def _capped(degrees: List[int], cap: int) -> Tuple[List[int], int]:
    found = [min(value, cap) for value in degrees]
    excess = left = sum(max(value - cap, 0) for value in degrees)
    while left > 0:
        weights = [held if held < cap else 0 for held in found]
        if sum(weights) == 0:
            weights = [1 if held < cap else 0 for held in found]
        total = sum(weights)
        share = [Fraction(left * weight, total) for weight in weights]
        given = [math.floor(value) for value in share]
        order = sorted(range(len(found)), key=lambda r: (-(share[r] - given[r]), r))
        for r in order[: left - sum(given)]:
            given[r] += 1
        for r, extra in enumerate(given):
            taken = min(extra, cap - found[r])
            found[r] += taken
            left -= taken
    return found, excess


def _gap(first: Sequence[int], second: Sequence[int]) -> int:
    ordered = sorted(second, reverse=True)
    return max(
        [0]
        + [
            sum(ordered[:k]) - sum(min(value, k) for value in first)
            for k in range(1, len(second) + 1)
        ]
    )


def _realisable(first: Sequence[int], second: List[int]) -> Tuple[List[int], int]:
    found, moved = list(second), 0
    while (gap := _gap(first, found)) > 0:
        for _ in range(gap):
            most = found.index(max(found))
            fewest = found.index(min(found))
            found[most] -= 1
            found[fewest] += 1
            moved += 1
    return found, moved


def _parent(offsets: Sequence[int], stub: int) -> int:
    return max(p for p, start in enumerate(offsets) if start <= stub)


def _repeated(owners: List[int], second: List[int]) -> Tuple[List[int], Set[Tuple[int, int]]]:
    """The rows whose pair an earlier row holds, in row order, and the pairs held more than once."""
    seen: Dict[Tuple[int, int], int] = {}
    waiting: List[int] = []
    for row, pair in enumerate(zip(owners, second, strict=True)):
        if pair in seen:
            waiting.append(row)
        seen[pair] = seen.get(pair, 0) + 1
    return waiting, {pair for pair, count in seen.items() if count > 1}


def _switch(
    owners: List[int], second: List[int], waiting: List[int], held: Set[Tuple[int, int]]
) -> Tuple[List[int], int]:
    """The keyed switches: the rows they leave, and how many they made."""
    switches, edges = _key("switches"), len(owners)
    _, repeated = _repeated(owners, second)
    pending, switched, left = set(waiting), 0, []
    for row in waiting:
        for attempt in range(ATTEMPTS):
            other = k2.permute(switches, edges, (row * ATTEMPTS + attempt) % edges)
            a, b, a2, b2 = owners[row], second[row], owners[other], second[other]
            blocked = other in pending or a == a2 or b == b2 or (a2, b2) in repeated
            if blocked or (a, b2) in held or (a2, b) in held:
                continue
            held.discard((a2, b2))
            held.update({(a, b2), (a2, b)})
            second[row], second[other] = b2, b
            pending.discard(row)
            switched += 1
            break
        else:
            left.append(row)
    return left, switched


def _place(
    owners: List[int], second: List[int], left: List[int], held: Set[Tuple[int, int]]
) -> Tuple[int, bool]:
    """Havel-Hakimi over the rows left: how many it placed, and ``False`` when a parent finds too few."""
    stubs: Dict[int, int] = {}
    rows_of: Dict[int, List[int]] = {}
    for row in left:
        stubs[second[row]] = stubs.get(second[row], 0) + 1
        rows_of.setdefault(owners[row], []).append(row)
    placed = 0
    for parent in sorted(rows_of, key=lambda p: (-len(rows_of[p]), p)):
        free = sorted(
            (b for b in stubs if stubs[b] > 0 and (parent, b) not in held),
            key=lambda b: (-stubs[b], b),
        )
        if len(free) < len(rows_of[parent]):
            return placed, False
        for row, b in zip(rows_of[parent], free, strict=False):
            stubs[b] -= 1
            second[row] = b
            held.add((parent, b))
            placed += 1
    return placed, True


def _repair(owners: List[int], second: List[int]) -> Tuple[List[int], int, int, bool]:
    """The repair: switches, then Havel-Hakimi over the rows left; ``rebuilt`` when it fails."""
    waiting, _ = _repeated(owners, second)
    held = set(zip(owners, second, strict=True))
    left, switched = _switch(owners, second, waiting, held)
    placed, served = _place(owners, second, left, held)
    return second, switched, placed, not served


def _rebuilt(first: Sequence[int], second: Sequence[int]) -> List[int]:
    stubs = _key("stubs")
    width = len(second)
    left = list(second)
    rank = [k2.permute(stubs, width, b) for b in range(width)]
    offsets = [sum(first[:p]) for p in range(len(first))]
    held = [0] * sum(first)
    for parent in sorted(range(len(first)), key=lambda p: (-first[p], p)):
        picked = sorted(range(width), key=lambda b: (-left[b], b))[: first[parent]]
        for b in picked:
            left[b] -= 1
        for offset, b in enumerate(sorted(picked, key=lambda b: rank[b])):
            held[offsets[parent] + offset] = b
    return held


def _reference(
    parents: Tuple[int, int],
    shapes: Sequence[Optional[Dict[str, Any]]],
    edges: int,
    distinct: bool,
) -> Dict[str, Any]:
    degrees = [
        k2.counted_layout(
            ROOT,
            ["sim", SIM, "gen", EMIT, "count", PAIR[side]],
            parents=parents[side],
            eligible=None,
            child_rows=edges,
            minimum=0,
            shape=shapes[side],
            expected=None,
        )["counts"]
        for side in (0, 1)
    ]
    moved = [0, 0, 0]
    if distinct:
        degrees[0], moved[0] = _capped(degrees[0], parents[1])
        degrees[1], moved[1] = _capped(degrees[1], parents[0])
        degrees[1], moved[2] = _realisable(degrees[0], degrees[1])
    offsets = [sum(degrees[1][:p]) for p in range(parents[1])]
    owners = [p for p, count in enumerate(degrees[0]) for _ in range(count)]
    stubs = _key("stubs")
    second = [_parent(offsets, k2.permute(stubs, edges, row)) for row in range(edges)]
    switched, placed, rebuilt = 0, 0, False
    if distinct and edges:
        second, switched, placed, rebuilt = _repair(owners, second)
        if rebuilt:
            second = _rebuilt(degrees[0], degrees[1])
    return {
        "stubsKey": stubs.hex(),
        "switchesKey": _key("switches").hex(),
        "degrees": degrees,
        "moved": moved,
        "switches": switched,
        "placed": placed,
        "rebuilt": rebuilt,
        "second": second,
    }


def _shape(kind: str, values: Sequence[str], weights: Sequence[str]) -> Dict[str, Any]:
    keys = ("levels", "levelWeights") if kind == "levels" else ("bins", "binWeights")
    return {keys[0]: list(values), keys[1]: list(weights)}


BINS = _shape("bins", ["0", "1", "3", "6"], ["0.1", "0.5", "0.4"])
CASES_IN = [
    ("(a) distinct, sparse, a shape on the first side", (12, 9), (BINS, None), 30, True),
    ("(b) repeats allowed: no repair", (8, 5), (BINS, None), 40, False),
    (
        "(c) a degree above the other side's parents is capped",
        (10, 6),
        (_shape("levels", ["1", "20"], ["0.5", "0.5"]), None),
        50,
        True,
    ),
    (
        "(d) the second side moved until it meets Gale-Ryser",
        (4, 4),
        (
            _shape("levels", ["0", "4"], ["0.5", "0.5"]),
            _shape("levels", ["1", "3"], ["0.5", "0.5"]),
        ),
        8,
        True,
    ),
    ("(e) dense: 100 of 120 pairs", (12, 10), (BINS, None), 100, True),
]


def association_vectors() -> Dict[str, Any]:
    return {
        "description": (
            "KERNEL-SPEC-2 section 8.7: an association's pairing. Each side's rows per parent "
            "(the counted layout of its count path over the rows), the rows the caps and the "
            "Gale-Ryser step moved, the keys of the stubs and switches paths, what the repair did, "
            "and the second side's parent row of every row in row order (the first side's parents "
            "are runs in row order). Keys are hex, other integers decimal. Computed by "
            "tests/kernel2/test_association_vectors.py, never typed."
        ),
        "regenerate": REGENERATE,
        "seam": SEAM.hex(),
        "seed": str(SEED),
        "root": ROOT.hex(),
        "cases": [
            {
                "name": name,
                "path": ["sim", SIM, "gen", EMIT, "associate", *PAIR],
                "parentRows": list(parents),
                "shapes": list(shapes),
                "rows": edges,
                "distinct": distinct,
                **_reference(parents, shapes, edges, distinct),
            }
            for name, parents, shapes, edges, distinct in CASES_IN
        ],
    }


def _render(vectors: Dict[str, Any]) -> str:
    return json.dumps(vectors, indent=2) + "\n"


STORED: Dict[str, Any] = (
    json.loads(FILE.read_text(encoding="utf-8")) if FILE.exists() else association_vectors()
)
CASES: List[Dict[str, Any]] = STORED["cases"]


def test_the_vector_file_is_current() -> None:
    assert FILE.read_text(encoding="utf-8") == _render(association_vectors()), f"run {REGENERATE}"


def test_the_cases_cover_the_caps_the_moves_the_switches_and_both_repairs() -> None:
    assert any(case["moved"][0] for case in CASES) and any(case["moved"][2] for case in CASES)
    assert any(case["switches"] for case in CASES) and any(case["rebuilt"] for case in CASES)
    assert any(not case["distinct"] for case in CASES)


def _child(shape: Optional[Dict[str, Any]]) -> Optional[ChildShape]:
    if shape is None:
        return None
    kind = "levels" if "levels" in shape else "bins"
    values, weights = (
        (shape["levels"], shape["levelWeights"])
        if kind == "levels"
        else (
            shape["bins"],
            shape["binWeights"],
        )
    )
    return ChildShape(kind, tuple(values), tuple(weights))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_the_engines_pairing_is_the_vector(case: Dict[str, Any]) -> None:
    root = derive_root(SEAM, SEED, seed_tree_version=kernel2.SEED_TREE_VERSION)
    keys = pair_keys(root, SIM, EMIT, PAIR)
    assert (keys.stubs.hex(), keys.switches.hex()) == (case["stubsKey"], case["switchesKey"])
    side = PairedSide(
        PAIR,
        0,
        (_child(case["shapes"][0]), _child(case["shapes"][1])),
        (0, 0),
        (case["parentRows"][0], case["parentRows"][1]),
        case["distinct"],
    )
    pairing = pair_layout(keys, side, case["rows"])
    assert [held.tolist() for held in pairing.degrees] == case["degrees"]
    assert list(pairing.moved) == case["moved"]
    assert (pairing.switches, pairing.placed, pairing.rebuilt) == (
        case["switches"],
        case["placed"],
        case["rebuilt"],
    )
    rows = np.arange(case["rows"], dtype=np.int64)
    assert pairing.side(1).parents_of(rows).tolist() == case["second"]


def main() -> None:
    FILE.write_text(_render(association_vectors()), encoding="utf-8")
    sys.stdout.write(f"wrote {FILE}\n")


if __name__ == "__main__":
    main()
