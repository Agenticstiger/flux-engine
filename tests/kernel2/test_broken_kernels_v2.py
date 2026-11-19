# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC-2 section 10: the three broken kernels kernel 0.2.0 adds to KERNEL-SPEC §14.

Each is the real implementation with one defect, and each must fail the property stated
next to it, while the real implementation passes the same property, so the property
proves something:

* **one counter for two draws** -- a stream that hands every sampler call the same
  draw index. Property: a path's draws are as many distinct values as there are draws.
* **a fused multiply-add in a transform** -- AS241's Horner steps computed as one
  rounding each, as a compiler that contracts them does. Property: every pinned
  transform vector reproduces bit for bit.
* **a key label that keeps the ordinals** -- a column's cells keyed by their whole label,
  row included. Property: the cells of one column share one key, so a slice of rows is
  one Philox call over consecutive counters.

A fused multiply-add is emulated exactly: ``fma(a, b, c)`` is ``a * b + c`` rounded once,
which is what one ``Fraction`` sum rounded to binary64 is. ``math.fma`` would do the same
but exists only from Python 3.13.
"""

from __future__ import annotations

import json
import math
import struct
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

from flux_engine.core import kernel2, rng
from flux_engine.core.kernel2 import CounterStream
from flux_engine.core.seedtree import PathKind, SeedTreeRoot, derive_root, key_label, path_key
from flux_engine.generator.paths import GENERATOR_PATH_SHAPE, CellStreams, generator_column_path

TRANSFORMS: Dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "vectors" / "kernel2-transforms-vectors.json").read_text(
        encoding="utf-8"
    )
)


def _root() -> SeedTreeRoot:
    return derive_root(b"broken kernels: H", 7, seed_tree_version=kernel2.SEED_TREE_VERSION)


def _bits(value: float) -> str:
    return struct.pack(">d", value).hex()


def _from_bits(pattern: str) -> float:
    value: float = struct.unpack(">d", bytes.fromhex(pattern))[0]
    return value


# ---------------------------------------------------------------------------
# One counter for two draws
# ---------------------------------------------------------------------------


class _OneCounter(CounterStream):
    """Every sampler call reads draw 0: the defect a draw index exists to rule out."""

    __slots__ = ()

    def _take(self) -> int:
        return 0


def _draws_are_distinct(make: Callable[[], CounterStream], draws: int = 256) -> bool:
    stream = make()
    values = [rng.uniform53(stream) for _ in range(draws)]
    return len(set(values)) == draws


def _real_stream() -> CounterStream:
    return CellStreams(_root(), "s", "orders").stream_at(3, "amount")  # type: ignore[return-value]


def _one_counter_stream() -> CounterStream:
    key = path_key(_root(), CellStreams(_root(), "s", "orders").key_label("amount"))
    return _OneCounter(key, (3, 0))


def test_the_real_stream_gives_a_path_distinct_draws() -> None:
    assert _draws_are_distinct(_real_stream)


def test_one_counter_for_two_draws_fails_the_property() -> None:
    assert not _draws_are_distinct(_one_counter_stream)


# ---------------------------------------------------------------------------
# A fused multiply-add in a transform
# ---------------------------------------------------------------------------


def _fma(a: float, b: float, c: float) -> float:
    return float(Fraction(a) * Fraction(b) + Fraction(c))


def _fused_horner(coefficients: Sequence[float], x: float) -> float:
    total = coefficients[0]
    for coefficient in coefficients[1:]:
        total = _fma(total, x, coefficient)
    return total


def _ppnd16_fused(p: float) -> float:
    """kernel2.ppnd16 with every Horner step fused, and nothing else changed."""
    q = p - 0.5
    if abs(q) <= 0.425:
        r = 0.180625 - q * q
        return _fused_horner(kernel2._CENTRAL_NUM, r) * q / _fused_horner(kernel2._CENTRAL_DEN, r)
    t = math.sqrt(-kernel2.log_basic(p if q <= 0.0 else 1.0 - p))
    if t <= 5.0:
        t -= 1.6
        x = _fused_horner(kernel2._NEAR_NUM, t) / _fused_horner(kernel2._NEAR_DEN, t)
    else:
        t -= 5.0
        x = _fused_horner(kernel2._FAR_NUM, t) / _fused_horner(kernel2._FAR_DEN, t)
    return -x if q < 0.0 else x


def _reproduces_the_vectors(ppnd16: Callable[[float], float]) -> bool:
    return all(_bits(ppnd16(_from_bits(case["p"]))) == case["z"] for case in TRANSFORMS["ppnd16"])


def test_the_real_transform_reproduces_the_vectors() -> None:
    assert _reproduces_the_vectors(kernel2.ppnd16)


def test_a_fused_transform_fails_the_property() -> None:
    assert not _reproduces_the_vectors(_ppnd16_fused)


# ---------------------------------------------------------------------------
# A key label that keeps the ordinals
# ---------------------------------------------------------------------------


def _key_label_keeping_ordinals(path: Sequence[Any], _shape: Any) -> Tuple[bytes, Tuple[int, int]]:
    """section 2's key label with the ordinals left in: every row gets a key of its own."""
    from flux_engine.core.seedtree import path_label

    ordinals = [
        part
        for part, kind in zip(path, GENERATOR_PATH_SHAPE.pattern, strict=True)
        if kind is PathKind.ORDINAL
    ]
    padded = (*ordinals, 0, 0)
    return path_label(path), (padded[0], padded[1])


def _one_key_per_column(
    label_of: Callable[[Sequence[Any], Any], Tuple[bytes, Tuple[int, int]]], rows: int = 16
) -> bool:
    keys: List[kernel2.Key] = []
    for row in range(rows):
        path = generator_column_path("s", "orders", row, "amount")
        label, (ordinal, _second) = label_of(path, GENERATOR_PATH_SHAPE)
        assert ordinal == row
        keys.append(path_key(_root(), label))
    return len(set(keys)) == 1


def test_the_real_key_label_gives_a_column_one_key() -> None:
    assert _one_key_per_column(key_label)


def test_a_key_label_keeping_the_ordinals_fails_the_property() -> None:
    assert not _one_key_per_column(_key_label_keeping_ordinals)
