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
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pytest

from flux_engine.core import rng as v1
from flux_engine.core.seedtree import CANONICAL_PATH_SHAPES
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


def streams_vectors() -> Dict[str, Any]:
    return {
        "description": (
            "Kernel v2's integer side (KERNEL-SPEC-2): Philox4x64-10 blocks, the run root, the key "
            "and counter of a path of every canonical shape, the draws, permute and the generated "
            "identifiers. Every value is computed by tests/kernel2/reference.py, never typed; words "
            "and keys are 16 hex digits, other integers decimal strings."
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
        "relationPicks": _relation_pick_vectors(),
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
