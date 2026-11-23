# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The vector files of KERNEL-SPEC-2, and what holds the reference to them.

``tests/vectors/kernel2-streams-vectors.json`` pins kernel v2's integer side: Philox4x64-10,
the run root, each canonical path's key and counter, the draws, ``permute`` and the
identifiers. ``tests/vectors/kernel2-transforms-vectors.json`` pins its binary64 side: the
logarithm, AS241, the truncated normal and the exponential, as IEEE 754 bit patterns, and
the Poisson thresholds. Every value is computed by ``tests/kernel2/reference.py``, never
typed, and the first test fails until the files match it. Regenerate both with::

    python -m tests.kernel2.test_kernel2_vectors

Beside that, the tests hold the reference to what it must agree with: Random123's own
known-answer vectors, numpy's ``Philox``, kernel v1's rules where v2 keeps them, and v1's
decimal truncated normal where v2 replaces it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pytest

from flux_engine.core import rng as v1
from flux_engine.core.seedtree import CANONICAL_PATH_SHAPES
from flux_engine.generator.paths import allocate_path, count_path
from tests.kernel2 import reference as k2

pytestmark = pytest.mark.unit

VECTORS = Path(__file__).resolve().parents[1] / "vectors"
STREAMS = VECTORS / "kernel2-streams-vectors.json"
TRANSFORMS = VECTORS / "kernel2-transforms-vectors.json"
REGENERATE = "python -m tests.kernel2.test_kernel2_vectors"

#: Random123's published known-answer vectors for philox4x64-10 (tests/kat_vectors in the
#: Random123 distribution, BSD-3-Clause): counter, key, expected block.
RANDOM123_KAT: Tuple[Tuple[str, Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]], ...] = (
    (
        "Random123: zero counter, zero key",
        ("0000000000000000",) * 4,
        ("0000000000000000",) * 2,
        ("16554d9eca36314c", "db20fe9d672d0fdc", "d7e772cee186176b", "7e68b68aec7ba23b"),
    ),
    (
        "Random123: all-ones counter and key",
        ("ffffffffffffffff",) * 4,
        ("ffffffffffffffff",) * 2,
        ("87b092c3013fe90b", "438c3c67be8d0224", "9cc7d7c69cd777b6", "a09caebf594f0ba0"),
    ),
    (
        "Random123: digits of pi",
        ("243f6a8885a308d3", "13198a2e03707344", "a4093822299f31d0", "082efa98ec4e6c89"),
        ("452821e638d01377", "be5466cf34e90c6c"),
        ("a528f45403e61d95", "38c72dbd566e9788", "a5a1610e72fd18b5", "57bd43b5e52b7fe6"),
    ),
)

SEAM = hashlib.sha256(b"kernel2 vectors: H").digest()
SEED = 20261118
ROOT = k2.root(SEAM, SEED)

#: One path of every canonical shape, and one generator cell.
SAMPLE_PATHS: Tuple[Tuple[Any, ...], ...] = (
    ("world", "w", "cohorts"),
    ("world", "w", "market", 3),
    ("world", "w", "subject", 11, "trait", "income"),
    ("world", "w", "subject", 11, "products"),
    ("sim", "s", "signal", "visit", "subject", 11, 2),
    ("sim", "s", "journey", "onboarding", "subject", 11, 4),
    ("sim", "s", "campaign", "spring", "subject", 11),
    ("sim", "s", "temperature", "engagement", "subject", 11, 0),
    ("experiment", "e", "assignment"),
    ("sweep", 5),
    ("sim", "s", "agentic", "triage", "subject", 11, 1),
    ("sim", "s", "gen", "orders", "row", 7, "amount"),
)


def _hex(word: int) -> str:
    return f"{word:016x}"


def _block_hex(block: Sequence[int]) -> List[str]:
    return [_hex(word) for word in block]


def _parts(path: Sequence[Any]) -> List[Any]:
    return [part if isinstance(part, int) else str(part) for part in path]


# ---------------------------------------------------------------------------
# The integer side
# ---------------------------------------------------------------------------


def _philox_vectors() -> List[Dict[str, Any]]:
    cases = [(name, counter, key) for name, counter, key, _expected in RANDOM123_KAT]
    words = k2.philox_block((1, 2, 3, 4), (5, 6)) + k2.philox_block((7, 0, 0, 0), (8, 9))
    cases += [
        ("a row counter", ("0000000000000007", "0" * 16, "0" * 16, "0" * 16), ("0" * 16,) * 2),
        ("draw and attempt words", tuple(_block_hex((9, 0, 1, 2))), tuple(_block_hex(words[:2]))),
        ("high words", tuple(_block_hex(words[2:6])), tuple(_block_hex(words[6:8]))),
    ]
    vectors = []
    for name, counter_hex, key_hex in cases:
        counter = tuple(int(word, 16) for word in counter_hex)
        key = (int(key_hex[0], 16), int(key_hex[1], 16))
        vectors.append(
            {
                "name": name,
                "counter": list(counter_hex),
                "key": list(key_hex),
                "block": _block_hex(k2.philox_block(counter, key)),
            }
        )
    return vectors


def _path_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for path in SAMPLE_PATHS:
        label, ordinals = k2.split_path(path)
        draws = []
        for draw, attempt in ((0, 0), (1, 0), (1, 1)):
            block = k2.block(ROOT, path, draw, attempt)
            draws.append(
                {
                    "draw": draw,
                    "attempt": attempt,
                    "counter": [str(word) for word in k2.counter(path, draw, attempt)],
                    "block": _block_hex(block),
                    "uniform53": str(k2.uniform53(block[0])),
                }
            )
        vectors.append(
            {
                "path": _parts(path),
                "keyLabel": label.hex(),
                "ordinals": [str(ordinal) for ordinal in ordinals],
                "key": _block_hex(k2.stream_key(ROOT, path)),
                "draws": draws,
            }
        )
    return vectors


def _bounded_case(path: Sequence[Any], draw: int, n: int) -> Dict[str, Any]:
    words: List[str] = []

    def word_of_attempt(attempt: int) -> int:
        word = k2.block(ROOT, path, draw, attempt)[0]
        words.append(_hex(word))
        return word

    value = k2.bounded(word_of_attempt, n)
    return {"path": _parts(path), "draw": draw, "n": str(n), "words": words, "value": str(value)}


def _bounded_vectors() -> List[Dict[str, Any]]:
    path = ("sim", "s", "gen", "orders", "row", 7, "customer_id")
    return [
        _bounded_case(path, draw, n)
        for n in (1, 10, 4000, 1 << 32, (1 << 63) + 1, (1 << 64) - 1)
        for draw in (0, 1, 2)
    ]


def _bernoulli_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for p in ("0", "0.25", "0.5", "0.999999", "1"):
        threshold = v1._probability_threshold(Decimal(p))
        for u53 in sorted(
            {0, max(threshold - 1, 0), threshold, min(threshold + 1, k2.TWO_POW_53 - 1)}
        ):
            vectors.append({"p": p, "uniform53": str(u53), "value": k2.bernoulli(u53, Decimal(p))})
    return vectors


def _categorical_vectors() -> List[Dict[str, Any]]:
    levels = {"gold": Decimal("0.1"), "bronze": Decimal("0.6"), "silver": Decimal("0.3")}
    thresholds, _last = v1._categorical_table(tuple(levels.items()))
    probes = {0, k2.TWO_POW_53 - 1}
    for threshold, _key in thresholds:
        probes.update({threshold - 1, threshold})
    return [
        {
            "levels": {name: str(weight) for name, weight in sorted(levels.items())},
            "uniform53": str(u53),
            "value": k2.categorical(u53, levels),
        }
        for u53 in sorted(probes)
    ]


def _permutation_vectors() -> List[Dict[str, Any]]:
    path = ("experiment", "e", "assignment")
    return [
        {"path": _parts(path), "n": n, "items": k2.permutation(ROOT, path, n)}
        for n in (1, 2, 5, 12)
    ]


def _spread(thresholds: Sequence[int], count: int = 12) -> List[int]:
    """Indexes of *count* thresholds spread over the table's distinct values."""
    changes = [k for k in range(len(thresholds)) if k == 0 or thresholds[k] != thresholds[k - 1]]
    if len(changes) <= count:
        return changes
    return [changes[(i * (len(changes) - 1)) // (count - 1)] for i in range(count)]


def _poisson_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for mean in ("0", "0.5", "3", "12.25", "250"):
        thresholds = k2.poisson_thresholds(Decimal(mean))
        chosen = _spread(thresholds)
        probes = {0, k2.TWO_POW_53 - 1}
        for k in chosen:
            probes.update({thresholds[k] - 1, thresholds[k]})
        cases = [
            {"uniform53": str(u53), "k": k2.poisson(u53, Decimal(mean))}
            for u53 in sorted(p for p in probes if 0 <= p < k2.TWO_POW_53)
        ]
        vectors.append(
            {
                "mean": mean,
                "tableLength": len(thresholds),
                "thresholds": {str(k): str(thresholds[k]) for k in chosen},
                "cases": cases,
            }
        )
    return vectors


KEY_PARTS = ("sim", "s", "gen", "orders", "key", "order_id")


def _permute_vectors() -> List[Dict[str, Any]]:
    key = k2.relation_key(SEAM, KEY_PARTS)
    vectors = []
    for name, domain, indexes in (
        ("one value", 1, [0]),
        ("two values", 2, [0, 1]),
        ("ten values", 10, list(range(10))),
        ("a prime domain", 97, [0, 1, 50, 96]),
        ("32 hex digits", 16**32, [0, 1, 2, 16**32 - 1]),
        ("a UUID body", 2**122, [0, 1, 2**121, 2**122 - 1]),
        ("halves wider than one word", 2**200 + 3, [0, 1, 2**199, 2**200 + 2]),
    ):
        vectors.append(
            {
                "name": name,
                "keyParts": list(KEY_PARTS),
                "key": key.hex(),
                "domain": str(domain),
                "indexes": [str(index) for index in indexes],
                "images": [str(k2.permute(key, domain, index)) for index in indexes],
            }
        )
    return vectors


def _identifier_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for row in (0, 1, 1000):
        path = ("sim", "s", "gen", "orders", "row", row, "order_id")
        for length, as_uuid in ((8, False), (32, False), (64, False), (32, True)):
            vectors.append(
                {
                    "path": _parts(path),
                    "length": length,
                    "asUuid": as_uuid,
                    "value": k2.identifier(SEAM, path, length, as_uuid),
                }
            )
    return vectors


def _relation_pick_vectors() -> List[Dict[str, Any]]:
    """A ``uniform`` foreign-key pick, with and without a null draw before it."""
    vectors = []
    for null_fraction in (None, "0.25"):
        for row in (0, 9):
            path = ("sim", "s", "gen", "returns", "row", row, "sale_id")
            entry: Dict[str, Any] = {"path": _parts(path), "nullFraction": null_fraction}
            draw = 0
            if null_fraction is not None:
                null_u53 = k2.uniform53(k2.block(ROOT, path, 0)[0])
                entry["null"] = k2.bernoulli(null_u53, Decimal(null_fraction))
                draw = 1
            entry["pickDraw"] = draw
            entry["parentRows"] = "500"
            entry["pick"] = str(k2.bounded_draw(ROOT, path, draw, 500))
            vectors.append(entry)
    return vectors


def _seed_tree_identifier_vectors() -> List[Dict[str, Any]]:
    paths = (
        ("world", "w", "subject", 11, "id"),
        ("sim", "s", "signal", "visit", "subject", 11, 2),
    )
    return [
        {
            "path": _parts(path),
            "length": length,
            "value": k2.seed_tree_identifier(ROOT, path, length),
        }
        for path in paths
        for length in (12, 32)
    ]


COUNT_PATH = count_path("s", "invoice_line", "invoice")

SHAPE_BINS: Dict[str, Any] = {
    "bins": ["1", "2", "4", "10"],
    "binWeights": ["0.40", "0.35", "0.25"],
}
#: Four of the parents are of class 1; five children over them leave one parent with two.
SHAPE_ONE: Dict[str, Any] = {"levels": ["0", "1"], "levelWeights": ["0.5", "0.5"]}
SHAPE_LEVELS: Dict[str, Any] = {
    "levels": ["0", "1", "2", "3"],
    "levelWeights": ["0.1", "0.3", "0.4", "0.2"],
}

#: name, parents, eligible rows (None: all), child rows, minChildren, shape, hook values.
COUNTED_CASES: Tuple[Tuple[Any, ...], ...] = (
    ("(a) no shape, no hook", 10, None, 25, 0, None, None),
    ("(b) bins, minimum 1", 12, None, 30, 1, SHAPE_BINS, None),
    ("(c) levels whose slots total the child rows", 20, None, 34, 0, SHAPE_LEVELS, None),
    ("(d) a parent condition", 9, [0, 2, 3, 5, 8], 11, 2, None, None),
    (
        "(e) a hook, no shape",
        6,
        None,
        17,
        0,
        None,
        ["1/2", "2", "0", "3", "7/3", "1"],
    ),
    (
        "(f) a hook with the bins of (b), ties among the expected counts",
        12,
        None,
        30,
        1,
        SHAPE_BINS,
        ["3", "1", "1", "2", "5", "1", "2", "7/2", "3", "0", "2", "1/3"],
    ),
    ("(g) a constant hook with the levels of (c)", 20, None, 34, 0, SHAPE_LEVELS, ["1"] * 20),
    # The one_to_one cases end with a flag. Without a cap the first would give a parent two children.
    (
        "(h) one_to_one, a level of two parents over one",
        8,
        None,
        5,
        0,
        SHAPE_ONE,
        None,
        True,
    ),
    (
        "(i) one_to_one, no shape, fewer children than parents",
        9,
        [1, 2, 4, 5, 7, 8],
        4,
        0,
        None,
        None,
        True,
    ),
    (
        "(j) one_to_one, a hook, one spare child",
        7,
        None,
        6,
        0,
        None,
        ["0", "5", "2", "2", "1", "0", "9"],
        True,
    ),
)


def counted_layout_of(case: Tuple[Any, ...]) -> Dict[str, Any]:
    _name, parents, eligible, child_rows, minimum, shape, hook, *flag = case
    return k2.counted_layout(
        ROOT,
        COUNT_PATH,
        parents=parents,
        eligible=eligible,
        child_rows=child_rows,
        minimum=minimum,
        shape=shape,
        expected=None if hook is None else [Fraction(value) for value in hook],
        one_to_one=bool(flag),
    )


def _chosen_children(offsets: Sequence[int], counts: Sequence[int], child_rows: int) -> List[int]:
    """The first and last child rows and the two rows around the first run boundary."""
    runs = [offsets[p] for p, count in enumerate(counts) if count and offsets[p] > 0]
    around = [runs[0] - 1, runs[0]] if runs else []
    return sorted({0, *around, child_rows - 1})


def _counted_layout_vectors() -> List[Dict[str, Any]]:
    vectors = []
    for case in COUNTED_CASES:
        name, parents, eligible, child_rows, minimum, shape, hook, *flag = case
        layout = counted_layout_of(case)
        label, ordinals = k2.split_path(COUNT_PATH)
        assert ordinals == (0, 0)
        chosen = _chosen_children(layout["offsets"], layout["counts"], child_rows)
        vectors.append(
            {
                "name": name,
                "path": _parts(COUNT_PATH),
                "keyLabel": label.hex(),
                "key": layout["key"].hex(),
                "parentRows": parents,
                "eligible": eligible,
                "childRows": child_rows,
                "minChildren": minimum,
                "shape": shape,
                "hook": hook,
                "e": layout["e"],
                "sigma": layout["sigma"],
                "slots": layout["slots"],
                "expectedCounts": [str(value) for value in layout["x"]],
                "counts": layout["counts"],
                "offsets": layout["offsets"],
                "childParents": [
                    {"childRow": row, "parentRow": k2.parent_of_child(layout["offsets"], row)}
                    for row in chosen
                ],
                **({"oneToOne": True} if flag else {}),
            }
        )
    return vectors


LEVEL_WEIGHTS = ["0.40", "0.30", "0.20", "0.10"]
BIN_EDGES = ["0", "10", "20", "50", "100"]
BIN_WEIGHTS = ["0.1", "0.2", "0.3", "0.4"]
CHILD_WEIGHTS_GIVEN = (
    {"x": "0.5", "y": "0.5"},
    {"x": "0.25", "y": "0.75"},
    {"x": "0.6", "y": "0.4"},
    {"x": "0.2", "y": "0.8"},
)
REGION_PATH = allocate_path("s", "orders", "region")
PLAN_PATH = allocate_path("s", "orders", "plan")
AMOUNT_PATH = allocate_path("s", "orders", "amount")
CHANNEL_PATH = allocate_path("s", "orders", "channel")


def _allocation(
    path: Sequence[Any], rows: int, declared: Sequence[str], null_fraction: Any = None
) -> Dict[str, Any]:
    weights = k2.class_weights(declared, null_fraction)
    return k2.exact_classes(ROOT, path, rows=rows, weights=weights)


def _allocation_entry(
    name: str,
    path: Sequence[Any],
    rows: int,
    declaration: Dict[str, Any],
    declared: Sequence[str],
    null_fraction: Any = None,
    chosen: Any = None,
) -> Dict[str, Any]:
    result = _allocation(path, rows, declared, null_fraction)
    label, ordinals = k2.split_path(path)
    assert ordinals == (0, 0)
    shown = range(rows) if chosen is None else chosen
    return {
        "name": name,
        "path": _parts(path),
        "keyLabel": label.hex(),
        "key": result["key"].hex(),
        "rows": rows,
        "declaration": declaration,
        "classWeights": [str(weight) for weight in k2.class_weights(declared, null_fraction)],
        "counts": result["counts"],
        "starts": result["starts"],
        "rowClasses": [
            {"row": row, "slot": result["slots"][row], "class": result["classes"][row]}
            for row in shown
        ],
    }


def _given_entry() -> Dict[str, Any]:
    parent_weights = k2.class_weights(LEVEL_WEIGHTS)
    child_weights = [k2.class_weights([row["x"], row["y"]]) for row in CHILD_WEIGHTS_GIVEN]
    result = k2.exact_given(
        ROOT,
        REGION_PATH,
        CHANNEL_PATH,
        rows=26,
        parent_weights=parent_weights,
        child_weights=child_weights,
    )
    parent = result["parent"]
    pairs = [[0, 0] for _ in parent["counts"]]
    for level, child in zip(parent["classes"], result["classes"], strict=True):
        pairs[level][child] += 1
    label, _ordinals = k2.split_path(CHANNEL_PATH)
    return {
        "name": "(d) given a parent of (a)",
        "parentPath": _parts(REGION_PATH),
        "path": _parts(CHANNEL_PATH),
        "keyLabel": label.hex(),
        "key": result["key"].hex(),
        "rows": 26,
        "declaration": {
            "levels": ["x", "y"],
            "levelWeightsGiven": list(CHILD_WEIGHTS_GIVEN),
        },
        "parentClassCounts": parent["counts"],
        "groupCounts": result["groupCounts"],
        "rowClasses": [
            {
                "row": row,
                "parentSlot": parent["slots"][row],
                "parentClass": parent["classes"][row],
                "class": result["classes"][row],
            }
            for row in range(26)
        ],
        "pairCounts": pairs,
    }


def _exact_allocation_vectors() -> List[Dict[str, Any]]:
    declaration = {"levels": ["a", "b", "c", "d"], "levelWeights": LEVEL_WEIGHTS}
    return [
        _allocation_entry("(a) four levels", REGION_PATH, 26, declaration, LEVEL_WEIGHTS),
        _allocation_entry(
            "(b) the levels of (a) with a nullFraction",
            PLAN_PATH,
            26,
            {**declaration, "nullFraction": "0.10"},
            LEVEL_WEIGHTS,
            "0.10",
        ),
        _allocation_entry(
            "(c) four bins",
            AMOUNT_PATH,
            1000,
            {"bins": BIN_EDGES, "binWeights": BIN_WEIGHTS},
            BIN_WEIGHTS,
            chosen=(0, 1, 2, 3, 499, 500, 998, 999),
        ),
        _given_entry(),
    ]


def _plugin_seed_vectors() -> List[Dict[str, Any]]:
    """The seed of a plugin's expose: another simulation, another expose and a decomposed name each move it."""
    cases = (
        ("s", "subscriber"),
        ("s", "orders"),
        ("other", "subscriber"),
        ("s", "caf\u00e9"),
        ("s", "cafe\u0301"),
    )
    return [
        {
            "simId": sim_id,
            "exposeId": expose_id,
            "value": k2.plugin_seed(ROOT, sim_id, expose_id).hex(),
        }
        for sim_id, expose_id in cases
    ]


def streams_vectors() -> Dict[str, Any]:
    return {
        "description": (
            "Kernel v2's integer side (KERNEL-SPEC-2): Philox4x64-10 blocks, the run root, the key "
            "and counter of a path of every canonical shape, the draws, permute and the generated "
            "identifiers, a generator plugin's seed, and the counted layouts of KERNEL-SPEC-2 "
            "section 8.1. Every value is computed by tests/kernel2/reference.py, never typed; "
            "words and keys are 16 hex digits, other integers decimal strings."
        ),
        "regenerate": REGENERATE,
        "kernelVersion": k2.KERNEL_VERSION,
        "seedTreeVersion": k2.SEED_TREE_VERSION,
        "seam": SEAM.hex(),
        "seed": str(SEED),
        "root": ROOT.hex(),
        "philox4x64_10": _philox_vectors(),
        "paths": _path_vectors(),
        "bounded": _bounded_vectors(),
        "bernoulli": _bernoulli_vectors(),
        "categorical": _categorical_vectors(),
        "permutation": _permutation_vectors(),
        "poisson": _poisson_vectors(),
        "permute": _permute_vectors(),
        "identifiers": _identifier_vectors(),
        "seedTreeIdentifiers": _seed_tree_identifier_vectors(),
        "pluginSeeds": _plugin_seed_vectors(),
        "relationPicks": _relation_pick_vectors(),
        "countedLayouts": _counted_layout_vectors(),
        "exactAllocations": _exact_allocation_vectors(),
    }


# ---------------------------------------------------------------------------
# The binary64 side
# ---------------------------------------------------------------------------

INTERVALS: Tuple[k2.Interval, ...] = (
    k2.Interval("[-8, 8], 6 places", Decimal(0), Decimal(1), Decimal(-8), Decimal(8), 6),
    k2.Interval("[20, 80], 2 places", Decimal(50), Decimal(10), Decimal(20), Decimal(80), 2),
    k2.Interval("[3, 10], mirrored", Decimal(0), Decimal(1), Decimal(3), Decimal(10), 6),
    k2.Interval("[-12, -6], deep tail", Decimal(0), Decimal(1), Decimal(-12), Decimal(-6), 6),
    k2.Interval("[0, 5000], INT", Decimal(1000), Decimal(300), Decimal(0), Decimal(5000), 0),
    k2.Interval("[7, 7], one value", Decimal(7), Decimal(2), Decimal(7), Decimal(7), 0),
    k2.Interval("[-40, 40], tails flushed", Decimal(0), Decimal(1), Decimal(-40), Decimal(40), 6),
)


def probe_uniforms() -> List[int]:
    """The two ends, every power of two from each end, and a few between."""
    values = {0, 1, 2, 3, k2.TWO_POW_53 - 1, k2.TWO_POW_53 - 2, k2.TWO_POW_53 // 2}
    values.update(1 << k for k in range(0, 53, 4))
    values.update(k2.TWO_POW_53 - (1 << k) for k in range(0, 53, 4))
    values.update((k2.TWO_POW_53 * k) // 13 for k in range(1, 13))
    return sorted(values)


def _log_inputs() -> List[float]:
    values = [1.0, 0.5, 0.75, 2.0**-53, 2.0**-1022, 1.0 - 2.0**-53, 0.7071067811865476, 1e-300]
    values += [(k + 1) / 17.0 for k in range(16)]
    return values


def _ppnd16_inputs() -> List[float]:
    edge = 0.075
    return [
        2.0**-53,
        1e-300,
        1.3887943864964020e-11,
        1.3887943864964021e-11,
        np.nextafter(edge, 0.0),
        edge,
        np.nextafter(edge, 1.0),
        0.2,
        0.5,
        np.nextafter(0.5, 1.0),
        0.925,
        1.0 - 2.0**-53,
    ]


def _normal_vectors() -> List[Dict[str, Any]]:
    u53 = np.array(probe_uniforms(), dtype=np.uint64)
    vectors = []
    for interval in INTERVALS:
        plan = k2.plan_of(interval)
        z, units = k2.truncated_normal(u53, plan)
        vectors.append(
            {
                "name": interval.name,
                "declaration": {
                    "mean": str(interval.mean),
                    "stddev": str(interval.stddev),
                    "lo": str(interval.lo),
                    "hi": str(interval.hi),
                    "scale": interval.scale,
                },
                "mirror": plan.mirror,
                "binary64": {
                    "a": k2.bits(plan.a_f),
                    "b": k2.bits(plan.b_f),
                    "cdfA": k2.bits(plan.cdf_a_f),
                    "sfB": k2.bits(plan.sf_b_f),
                    "mass": k2.bits(plan.mass_f),
                    "mean": k2.bits(plan.mean_f),
                    "stddev": k2.bits(plan.stddev_f),
                },
                "units": [str(plan.lo_units), str(plan.hi_units)],
                "cases": [
                    {"uniform53": str(int(u)), "z": k2.bits(float(zz)), "units": str(int(uu))}
                    for u, zz, uu in zip(u53.tolist(), z.tolist(), units.tolist(), strict=True)
                ],
            }
        )
    return vectors


def _exponential_vectors() -> List[Dict[str, Any]]:
    u53 = np.array(probe_uniforms(), dtype=np.uint64)
    vectors = []
    for rate, scale in (("1", 6), ("0.25", 3), ("40", 0)):
        units = k2.exponential(u53, Decimal(rate), scale)
        vectors.append(
            {
                "rate": rate,
                "scale": scale,
                "cases": [
                    {"uniform53": str(int(u)), "units": str(int(x))}
                    for u, x in zip(u53.tolist(), units.tolist(), strict=True)
                ],
            }
        )
    return vectors


def transforms_vectors() -> Dict[str, Any]:
    logs = np.array(_log_inputs())
    ps = np.array(_ppnd16_inputs())
    return {
        "description": (
            "Kernel v2's binary64 side (KERNEL-SPEC-2 section 5): the constants by bit pattern, "
            "the logarithm, AS241's Phi inverse, the truncated normal and the exponential, every "
            "binary64 value as the 16 hex digits of its IEEE 754 bits, every integer a decimal "
            "string. Computed by tests/kernel2/reference.py, never typed."
        ),
        "regenerate": REGENERATE,
        "kernelVersion": k2.KERNEL_VERSION,
        "constants": {
            "log": dict(k2.LOG_CONSTANTS),
            "sqrtHalf": k2.bits(k2.SQRT_HALF),
            "as241": {
                name: [k2.bits(value) for value in table] for name, table in k2.AS241_TABLES.items()
            },
        },
        "log": [
            {"x": k2.bits(float(x)), "ln": k2.bits(float(y))}
            for x, y in zip(logs.tolist(), k2.log_basic(logs).tolist(), strict=True)
        ],
        "ppnd16": [
            {"p": k2.bits(float(p)), "z": k2.bits(float(z))}
            for p, z in zip(ps.tolist(), k2.ppnd16(ps).tolist(), strict=True)
        ],
        "truncatedNormal": _normal_vectors(),
        "exponential": _exponential_vectors(),
    }


def _render(vectors: Dict[str, Any]) -> str:
    return json.dumps(vectors, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, build",
    [(STREAMS, streams_vectors), (TRANSFORMS, transforms_vectors)],
    ids=["streams", "transforms"],
)
def test_the_vector_files_are_current(path: Path, build: Any) -> None:
    assert path.read_text(encoding="utf-8") == _render(build()), f"run {REGENERATE}"


#: fdlibm e_log.c prints each constant in decimal beside its bit pattern; both must agree.
FDLIBM_LOG_DECIMALS = {
    "ln2_hi": 6.93147180369123816490e-01,
    "ln2_lo": 1.90821492927058770002e-10,
    "Lg1": 6.666666666666735130e-01,
    "Lg2": 3.999999999940941908e-01,
    "Lg3": 2.857142874366239149e-01,
    "Lg4": 2.222219843214978396e-01,
    "Lg5": 1.818357216161805012e-01,
    "Lg6": 1.531383769920937332e-01,
    "Lg7": 1.479819860511658591e-01,
}


def test_the_log_constants_are_fdlibms() -> None:
    for name, value in FDLIBM_LOG_DECIMALS.items():
        assert k2.from_bits(k2.LOG_CONSTANTS[name]) == value, name


def test_philox_reproduces_random123() -> None:
    for _name, counter_hex, key_hex, expected in RANDOM123_KAT:
        counter = tuple(int(word, 16) for word in counter_hex)
        key = (int(key_hex[0], 16), int(key_hex[1], 16))
        assert tuple(_block_hex(k2.philox_block(counter, key))) == expected


def test_philox_agrees_with_numpy() -> None:
    material = k2.numpy_philox((3, 1), 0).random_raw(6 * 64)
    for case in range(64):
        words = [int(word) for word in material[6 * case : 6 * case + 6]]
        key = (words[0], words[1])
        counter = sum(word << (64 * i) for i, word in enumerate(words[2:]))
        expected = k2.numpy_philox(key, counter).random_raw(4)
        assert np.array_equal(expected, np.array(k2.philox_block(words[2:], key), dtype=np.uint64))


def test_a_column_is_one_numpy_call() -> None:
    path = ("sim", "s", "gen", "orders", "row", 0, "amount")
    key = k2.stream_key(ROOT, path)
    rows = 300
    column = k2.numpy_philox(key, 0).random_raw(4 * rows).reshape(rows, 4)
    for row in (0, 1, 299):
        cell = ("sim", "s", "gen", "orders", "row", row, "amount")
        assert tuple(int(word) for word in column[row]) == k2.block(ROOT, cell, 0)


def test_every_shape_has_its_own_key_label() -> None:
    labels = {k2.split_path(path)[0] for path in SAMPLE_PATHS}
    assert len(labels) == len(SAMPLE_PATHS) == len(CANONICAL_PATH_SHAPES) + 1
    # The counted layout's and the allocation's shapes are outside the sample list, which
    # pins the existing vectors; each still has a key label of its own, with no ordinal.
    extra = (count_path("s", "orders", "r"), allocate_path("s", "orders", "r"))
    extra_labels = {k2.split_path(path)[0] for path in extra}
    assert len(extra_labels) == 2 and not extra_labels & labels
    assert all(k2.split_path(path)[1] == (0, 0) for path in extra)


def _counted(name: str) -> Dict[str, Any]:
    return next(entry for entry in streams_vectors()["countedLayouts"] if entry["name"] == name)


def test_every_counted_layout_totals_its_child_rows() -> None:
    for entry in streams_vectors()["countedLayouts"]:
        assert sum(entry["counts"]) == entry["childRows"], entry["name"]
        assert sorted(entry["sigma"]) == list(range(entry["e"])), entry["name"]


def test_no_eligible_parent_has_fewer_than_the_minimum() -> None:
    for entry in streams_vectors()["countedLayouts"]:
        eligible = entry["eligible"]
        rows = range(entry["parentRows"]) if eligible is None else eligible
        assert all(entry["counts"][row] >= entry["minChildren"] for row in rows), entry["name"]
        if eligible is not None:
            outside = set(range(entry["parentRows"])) - set(eligible)
            assert all(entry["counts"][row] == 0 for row in outside)


def test_a_child_row_belongs_to_the_run_that_holds_it() -> None:
    for entry in streams_vectors()["countedLayouts"]:
        for chosen in entry["childParents"]:
            parent, row = chosen["parentRow"], chosen["childRow"]
            assert (
                entry["offsets"][parent] <= row < entry["offsets"][parent] + entry["counts"][parent]
            )


def test_levels_that_total_the_child_rows_give_each_parent_its_slot() -> None:
    entry = _counted("(c) levels whose slots total the child rows")
    assert sum(entry["slots"]) == entry["childRows"]
    assert entry["counts"] == [entry["slots"][rank] for rank in entry["sigma"]]


def test_a_constant_hook_changes_nothing_under_a_shape() -> None:
    plain = _counted("(c) levels whose slots total the child rows")
    hooked = _counted("(g) a constant hook with the levels of (c)")
    assert hooked["counts"] == plain["counts"]
    assert hooked["sigma"] == plain["sigma"]


def test_the_rank_coupling_orders_the_slots_by_expected_count() -> None:
    entry = _counted("(f) a hook with the bins of (b), ties among the expected counts")
    hook = [Fraction(value) for value in entry["hook"]]
    assert len(set(hook)) < len(hook), "no tie among the expected counts"
    ranks = sorted(range(entry["e"]), key=lambda r: (hook[r], entry["sigma"][r]))
    taken = [Fraction(entry["expectedCounts"][r]) for r in ranks]
    assert taken == sorted(taken) == [Fraction(value) for value in entry["slots"]]


def _allocated(prefix: str) -> Dict[str, Any]:
    return next(
        entry for entry in streams_vectors()["exactAllocations"] if entry["name"].startswith(prefix)
    )


def test_every_class_holds_its_largest_remainder_count() -> None:
    for prefix, rows, declared, null in (
        ("(a)", 26, LEVEL_WEIGHTS, None),
        ("(b)", 26, LEVEL_WEIGHTS, "0.10"),
        ("(c)", 1000, BIN_WEIGHTS, None),
    ):
        entry = _allocated(prefix)
        weights = k2.class_weights(declared, null)
        assert sum(weights) == 1
        assert entry["counts"] == k2._lrm(rows, weights)
        assert sum(entry["counts"]) == rows


def test_a_null_fraction_adds_a_last_class_and_scales_the_others() -> None:
    plain, nulled = _allocated("(a)"), _allocated("(b)")
    assert len(nulled["counts"]) == len(plain["counts"]) + 1
    assert Fraction(nulled["classWeights"][-1]) == Fraction("0.10")
    assert Fraction(nulled["classWeights"][0]) == Fraction("0.40") * Fraction("0.90")


def test_the_rows_of_each_class_are_the_slots_of_its_layout() -> None:
    for prefix in ("(a)", "(b)", "(c)"):
        entry = _allocated(prefix)
        for chosen in entry["rowClasses"]:
            k = chosen["class"]
            assert entry["starts"][k] <= chosen["slot"] < entry["starts"][k] + entry["counts"][k]


def test_the_allocation_of_every_row_is_one_slot_of_a_permutation() -> None:
    entry = _allocated("(a)")
    assert sorted(chosen["slot"] for chosen in entry["rowClasses"]) == list(range(26))
    held = [0] * len(entry["counts"])
    for chosen in entry["rowClasses"]:
        held[chosen["class"]] += 1
    assert held == entry["counts"]


def test_every_pair_of_a_parent_class_and_a_child_class_holds_its_largest_remainder_count() -> None:
    entry = _allocated("(d)")
    for level, size in enumerate(entry["parentClassCounts"]):
        weights = k2.class_weights([row for row in _given_row(level)])
        assert entry["pairCounts"][level] == k2._lrm(size, weights)
        assert entry["groupCounts"][level] == entry["pairCounts"][level]
    assert sum(sum(row) for row in entry["pairCounts"]) == entry["rows"]


def _given_row(level: int) -> List[str]:
    row = CHILD_WEIGHTS_GIVEN[level]
    return [row["x"], row["y"]]


def test_a_group_of_one_row_takes_slot_zero() -> None:
    root = ROOT
    weights = k2.class_weights(["1", "1"])
    result = k2.exact_given(
        root,
        REGION_PATH,
        CHANNEL_PATH,
        rows=2,
        parent_weights=weights,
        child_weights=[weights, weights],
    )
    assert result["parent"]["counts"] == [1, 1]
    assert result["groupCounts"] == [[1, 0], [1, 0]]
    assert result["classes"] == [0, 0]


def test_a_child_class_of_weight_zero_gets_no_rows() -> None:
    entry = k2.exact_classes(ROOT, REGION_PATH, rows=7, weights=k2.class_weights(["0", "3", "4"]))
    assert entry["counts"][0] == 0 and 0 not in entry["classes"]


def test_the_reference_refuses_what_section_8_3_refuses() -> None:
    for declared, null in (
        (["0", "0"], None),
        (["-1", "2"], None),
        (["1"], "1.5"),
        (["1"], "-0.1"),
    ):
        with pytest.raises(ValueError):
            k2.class_weights(declared, null)


def _refuses(**changes: Any) -> None:
    arguments: Dict[str, Any] = {
        "parents": 6,
        "eligible": None,
        "child_rows": 12,
        "minimum": 0,
        "shape": None,
        "expected": None,
    }
    arguments.update(changes)
    with pytest.raises(ValueError):
        k2.counted_layout(ROOT, COUNT_PATH, **arguments)


def test_the_reference_refuses_what_section_8_1_refuses() -> None:
    _refuses(minimum=3)  # S < 0
    _refuses(eligible=[], child_rows=4)  # e == 0 with N > 0
    _refuses(shape={"levels": ["1", "1"], "levelWeights": ["0.5", "0.5"]})  # not ascending
    _refuses(shape={"levels": ["2", "1"], "levelWeights": ["0.5", "0.5"]})
    _refuses(shape={"bins": ["1", "1", "4"], "binWeights": ["0.5", "0.5"]})
    _refuses(shape={"levels": ["-1", "1"], "levelWeights": ["0.5", "0.5"]})
    _refuses(one_to_one=True)  # twelve children over six parents
    _refuses(expected=[Fraction(-1)] * 6)


def test_a_one_to_one_layout_with_no_count_above_one_is_allowed() -> None:
    layout = k2.counted_layout(
        ROOT,
        COUNT_PATH,
        parents=6,
        eligible=None,
        child_rows=3,
        minimum=0,
        shape={"levels": ["0", "1"], "levelWeights": ["0.5", "0.5"]},
        expected=None,
        one_to_one=True,
    )
    assert sum(layout["counts"]) == 3 and max(layout["counts"]) == 1


def test_a_rejected_bounded_draw_retries_on_its_own_counter() -> None:
    entries = streams_vectors()["bounded"]
    assert any(len(entry["words"]) > 1 for entry in entries), "no vector exercises a rejection"
    for entry in entries:
        path = tuple(entry["path"])
        for attempt, word in enumerate(entry["words"]):
            assert int(word, 16) == k2.block(ROOT, path, entry["draw"], attempt)[0]
        last = int(entry["words"][-1], 16)
        assert int(entry["value"]) == (last * int(entry["n"])) >> 64


def test_sixty_four_rejections_take_the_last_word_modulo_n() -> None:
    n = (1 << 63) + 1  # 2**64 mod n is 2**63 - 1, and an even word below it is rejected
    words = [2 * (attempt + 1) for attempt in range(k2.BOUNDED_ATTEMPTS)]
    assert k2.bounded(lambda attempt: words[attempt], n) == words[-1] % n


@pytest.mark.parametrize("mean", ["0.5", "3", "12.25", "80", "250"])
def test_the_poisson_thresholds_agree_with_v1_at_every_edge(mean: str) -> None:
    thresholds = k2.poisson_thresholds(Decimal(mean))
    probes = sorted(
        {
            edge
            for threshold in thresholds
            for edge in (threshold - 1, threshold, threshold + 1)
            if 0 <= edge < k2.TWO_POW_53
        }
        | {0, k2.TWO_POW_53 - 1}
    )
    by_table = k2.poisson_by_thresholds(np.array(probes, dtype=np.uint64), Decimal(mean))
    assert by_table.tolist() == [k2.poisson(u53, Decimal(mean)) for u53 in probes]
    assert by_table.tolist() == [k2.poisson_v1(u53, Decimal(mean)) for u53 in probes]


def test_the_truncated_normal_rounds_as_v1_does_on_the_probes() -> None:
    for interval in INTERVALS:
        plan = k2.plan_of(interval)
        for u53, units in zip(
            probe_uniforms(),
            k2.truncated_normal(np.array(probe_uniforms(), dtype=np.uint64), plan)[1].tolist(),
            strict=True,
        ):
            expected = v1.truncated_normal(
                k2.OneWord(u53),  # type: ignore[arg-type]
                mean=interval.mean,
                stddev=interval.stddev,
                lo=interval.lo,
                hi=interval.hi,
                scale=interval.scale,
            )
            assert Decimal(units).scaleb(-interval.scale) == expected, (interval.name, u53)


@pytest.mark.parametrize(
    "interval",
    [
        k2.Interval("one value above the mean", Decimal(7), Decimal(2), Decimal(8), Decimal(8), 0),
        k2.Interval("one value below the mean", Decimal(7), Decimal(2), Decimal(5), Decimal(5), 0),
    ],
    ids=lambda interval: interval.name,
)
def test_a_one_value_interval_takes_its_end_as_v1_does(interval: k2.Interval) -> None:
    plan = k2.plan_of(interval)
    assert plan.degenerate
    for u53 in (0, 1, k2.TWO_POW_53 - 1):
        _z, units = k2.truncated_normal_scalar(u53, plan)
        expected = v1.truncated_normal(
            k2.OneWord(u53),  # type: ignore[arg-type]
            mean=interval.mean,
            stddev=interval.stddev,
            lo=interval.lo,
            hi=interval.hi,
            scale=interval.scale,
        )
        assert Decimal(units) == expected
        assert int(k2.truncated_normal(np.array([u53], dtype=np.uint64), plan)[1][0]) == units


def test_numpy_and_plain_python_agree_bit_for_bit() -> None:
    u53 = np.array(probe_uniforms(), dtype=np.uint64)
    for interval in INTERVALS:
        plan = k2.plan_of(interval)
        z, units = k2.truncated_normal(u53, plan)
        for index, u in enumerate(u53.tolist()):
            z_scalar, units_scalar = k2.truncated_normal_scalar(u, plan)
            assert k2.bits(z_scalar) == k2.bits(float(z[index]))
            assert units_scalar == int(units[index])


@pytest.mark.parametrize(
    "interval",
    [
        k2.Interval("mass", Decimal(0), Decimal(1), Decimal(40), Decimal(41), 6),
        k2.Interval("units", Decimal(0), Decimal(10**12), Decimal(-(10**12)), Decimal(10**12), 6),
        k2.Interval("stddev", Decimal(0), Decimal("1E-320"), Decimal(-1), Decimal(1), 6),
        k2.Interval("mean", Decimal("1E-320"), Decimal(1), Decimal(-1), Decimal(1), 6),
        k2.Interval("scale", Decimal(0), Decimal(1), Decimal(-1), Decimal(1), 23),
        k2.Interval("no value", Decimal(0), Decimal(1), Decimal("0.11"), Decimal("0.19"), 0),
        k2.Interval("held", Decimal(0), Decimal(1), Decimal("-1E60"), Decimal(1), 6),
    ],
    ids=lambda interval: interval.name,
)
def test_a_declaration_binary64_cannot_hold_is_refused(interval: k2.Interval) -> None:
    with pytest.raises(ValueError, match="FLX-5021"):
        k2.plan_of(interval)


@pytest.mark.parametrize(
    "rate, scale",
    [("1E-400", 6), ("1E300", 6), ("1E-9", 6), ("1", 23), ("NaN", 6), ("Infinity", 6)],
)
def test_an_exponential_binary64_cannot_hold_is_refused(rate: str, scale: int) -> None:
    with pytest.raises(ValueError, match="FLX-5021"):
        k2.exponential(np.array([0, 1], dtype=np.uint64), Decimal(rate), scale)


def test_a_negative_poisson_mean_is_refused() -> None:
    with pytest.raises(ValueError, match="FLX-5021"):
        k2.poisson_thresholds(Decimal(-1))


def test_the_scalar_philox_is_the_array_philox() -> None:
    material = k2.numpy_philox((11, 12), 0).random_raw(6 * 32)
    for case in range(32):
        words = [int(word) for word in material[6 * case : 6 * case + 6]]
        array = k2.philox4x64_10(np.array([words[2:]], dtype=np.uint64), (words[0], words[1]))[0]
        assert k2.philox_block(words[2:], (words[0], words[1])) == tuple(int(w) for w in array)


def test_identifiers_are_seed_free_and_distinct() -> None:
    rows = 1000
    full = {
        k2.identifier(SEAM, ("sim", "s", "gen", "orders", "row", row, "order_id"), 64)
        for row in range(rows)
    }
    assert len(full) == rows  # one key, distinct counters: Philox is a bijection
    uuid = k2.identifier(SEAM, ("sim", "s", "gen", "orders", "row", 0, "order_id"), 32, True)
    assert uuid[14] == "8" and uuid[19] in "89ab"


@pytest.mark.parametrize("domain", [2, 3, 10, 97, 256, 300])
def test_permute_is_a_bijection(domain: int) -> None:
    key = k2.relation_key(SEAM, ("sim", "s", "gen", "orders", "key", "order_id"))
    assert sorted(k2.permute(key, domain, index) for index in range(domain)) == list(range(domain))


def test_this_process_keeps_subnormals() -> None:
    assert k2.environment_keeps_subnormals()


if __name__ == "__main__":  # pragma: no cover - regenerates the vector files
    STREAMS.write_text(_render(streams_vectors()), encoding="utf-8")
    TRANSFORMS.write_text(_render(transforms_vectors()), encoding="utf-8")
    sys.stdout.write(f"wrote {STREAMS}\nwrote {TRANSFORMS}\n")
