# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Kernel 0.2.0 a column at a time (ADR 0010, step 4): each array function is its scalar twin.

The scalar functions are held to the vector files (test_engine_conformance.py). These tests
hold every array function in :mod:`flux_engine.core.kernel2` to its scalar twin, row by row
and bit for bit, so the column path computes exactly what the per-row path does.
"""

from __future__ import annotations

import math
import random
from decimal import Decimal
from typing import Any, List, Tuple

import numpy as np
import pytest

from flux_engine.core import kernel2, rng
from flux_engine.core.errors import FluxError
from flux_engine.core.kernel2 import CounterStream
from flux_engine.core.permute import permute2

KEY = (0x0123456789ABCDEF, 0xFEDCBA9876543210)
CHOOSER = random.Random("kernel2 columns")
ROWS = np.array(
    sorted(
        {0, 1, 2, 3, 4095, 4096, 65535, (1 << 40) + 7}
        | {CHOOSER.randrange(1 << 32) for _ in range(300)}
    ),
    dtype=np.int64,
)


def _streams(key: Tuple[int, int] = KEY) -> List[CounterStream]:
    return [CounterStream(key, (int(row), 0)) for row in ROWS.tolist()]


def test_philox_arrays_is_the_scalar_block() -> None:
    counters = [tuple(CHOOSER.randrange(1 << 64) for _ in range(4)) for _ in range(200)]
    c0, c1, c2, c3 = (np.array([c[i] for c in counters], dtype=np.uint64) for i in range(4))
    words = kernel2.philox_arrays(c0, c1, c2, c3, KEY)
    for index, counter in enumerate(counters):
        expected = kernel2.philox_block(counter, KEY)
        assert tuple(int(word[index]) for word in words) == expected


@pytest.mark.parametrize("draw", [0, 1, 5])
def test_a_columns_uniforms_are_its_cells(draw: int) -> None:
    drawn = kernel2.column_uniform53(KEY, ROWS, draw).tolist()
    assert drawn == [stream.block(draw)[0] >> 11 for stream in _streams()]


@pytest.mark.parametrize(
    "n",
    [1, 2, 7, 1_000_003, (1 << 32) + 1, (1 << 62) + 1, (1 << 63) + 1, (1 << 64) - 1, 1 << 64],
)
def test_a_columns_bounded_draws_are_its_cells(n: int) -> None:
    drawn = kernel2.column_bounded(KEY, ROWS, 2, n).tolist()
    expected = []
    for stream in _streams():
        stream._position = 2
        expected.append(stream.bounded(n))
    assert drawn == expected


def _scripted_words(monkeypatch: pytest.MonkeyPatch, word_at: Any) -> None:
    """Every row's block at attempt *a* starts with ``word_at(a)``, on both paths."""
    monkeypatch.setattr(
        kernel2,
        "column_words",
        lambda key, rows, draw, attempt=0: np.full(rows.shape[0], word_at(attempt), np.uint64),
    )
    monkeypatch.setattr(
        CounterStream, "block", lambda self, draw, attempt=0: (word_at(attempt), 0, 0, 0)
    )


def _bounded_cells(n: int) -> List[int]:
    return [CounterStream(KEY, (int(row), 0)).bounded(n) for row in ROWS[:5].tolist()]


def test_a_low_product_at_the_threshold_is_accepted_as_the_cells_accept_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lemire's test accepts a low product equal to ``2**64 mod n``: its comparison is >=."""
    accepted = pow(3, -1, 1 << 64)  # its low product with 3 is 1, which is 2**64 mod 3
    _scripted_words(monkeypatch, lambda attempt: accepted if attempt == 0 else 5)
    drawn = kernel2.column_bounded(KEY, ROWS[:5], 0, 3).tolist()
    assert drawn == _bounded_cells(3) == [2] * 5


def test_the_bounded_fallback_is_the_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    """All 64 attempts rejected: the last attempt's word modulo n, which here is not 0."""
    last = pow(3, -1, 1 << 63)  # 6 * last is 2 modulo 2**64, below 2**64 mod 6 = 4
    assert (last * 6) % (1 << 64) < (1 << 64) % 6
    _scripted_words(monkeypatch, lambda attempt: last if attempt == 63 else 0)
    drawn = kernel2.column_bounded(KEY, ROWS[:5], 0, 6).tolist()
    assert drawn == _bounded_cells(6) == [last % 6] * 5
    assert last % 6 != 0


class _Fixed:
    """A kernel 0.2.0 stream whose every uniform53 is one given value."""

    counter_based = True

    def __init__(self, u53: int) -> None:
        self.u53 = u53

    def uniform53(self) -> int:
        return self.u53


@pytest.mark.parametrize("p", ["0.04", "0.5", "0.999"])
def test_a_bernoulli_draw_at_its_threshold_is_its_cells(p: str) -> None:
    threshold = rng._probability_threshold(Decimal(p))
    u53 = [threshold - 1, threshold, threshold + 1]
    drawn = kernel2.column_bernoulli(np.array(u53, dtype=np.uint64), Decimal(p)).tolist()
    assert drawn == [rng.bernoulli(_Fixed(u), Decimal(p)) for u in u53] == [True, False, False]


def test_a_categorical_draw_at_each_threshold_is_its_cells() -> None:
    levels = {"bronze": Decimal("0.6"), "gold": Decimal("0.1"), "silver": Decimal("0.3")}
    thresholds, _last = rng._categorical_table(tuple(levels.items()))
    u53 = sorted({t + d for t, _key in thresholds for d in (-1, 0, 1)})
    drawn = kernel2.column_categorical(np.array(u53, dtype=np.uint64), tuple(levels.items()))
    assert drawn == [rng.categorical(_Fixed(u), levels) for u in u53]


@pytest.mark.parametrize("p", ["0", "0.04", "0.5", "0.999", "1"])
def test_a_columns_bernoulli_draws_are_its_cells(p: str) -> None:
    u53 = kernel2.column_uniform53(KEY, ROWS, 0)
    drawn = kernel2.column_bernoulli(u53, Decimal(p)).tolist()
    assert drawn == [rng.bernoulli(stream, Decimal(p)) for stream in _streams()]


def test_a_columns_categorical_draws_are_its_cells() -> None:
    levels = {"bronze": Decimal("0.6"), "gold": Decimal("0.1"), "silver": Decimal("0.3")}
    u53 = kernel2.column_uniform53(KEY, ROWS, 0)
    drawn = kernel2.column_categorical(u53, tuple(levels.items()))
    assert drawn == [rng.categorical(stream, levels) for stream in _streams()]


@pytest.mark.parametrize("mean", ["0", "0.5", "3", "40", "2500"])
def test_a_columns_poisson_draws_are_its_cells(mean: str) -> None:
    value = Decimal(mean)
    thresholds = kernel2.poisson_thresholds(value)
    probes = {0, 1, (1 << 53) - 1} | {t for t in thresholds if t < 1 << 53}
    u53 = np.array(
        sorted(probes | {CHOOSER.randrange(1 << 53) for _ in range(300)}), dtype=np.uint64
    )
    drawn = kernel2.column_poisson(u53, value).tolist()
    assert drawn == [kernel2.poisson_of(int(u), value) if value else 0 for u in u53.tolist()]


PLANS = [
    ("0", "1", "-8", "8", 6),
    ("0", "1", "2", "6", 8),
    ("0", "1", "7", "9", 10),
    ("50", "15", "1", "10000", 2),
    ("0", "1", "-12", "-6", 6),
    ("0", "1", "7", "7", 6),
    ("0", "1", "-40", "40", 6),
    ("250.5", "100", "1", "500", 2),
]


@pytest.mark.parametrize("declaration", PLANS, ids=lambda d: "[" + d[2] + "," + d[3] + "]")
def test_a_columns_truncated_normal_draws_are_its_cells(declaration: Tuple[str, ...]) -> None:
    mean, stddev, lo, hi, scale = declaration
    plan = kernel2.normal_plan(Decimal(mean), Decimal(stddev), Decimal(lo), Decimal(hi), int(scale))
    edges = [0, 1, 2, 3, 16, (1 << 52), (1 << 53) - 2, (1 << 53) - 1]
    u53 = np.array(edges + [CHOOSER.randrange(1 << 53) for _ in range(2000)], dtype=np.uint64)
    drawn = kernel2.column_truncated_normal_units(u53, plan).tolist()
    assert drawn == [kernel2.truncated_normal_units(int(u), plan) for u in u53.tolist()]


@pytest.mark.parametrize(("rate", "scale"), [("1", 6), ("0.01", 0), ("2", 6), ("1e12", 23 - 1)])
def test_a_columns_exponential_draws_are_its_cells(rate: str, scale: int) -> None:
    u53 = np.array(
        [0, 1, (1 << 53) - 1] + [CHOOSER.randrange(1 << 53) for _ in range(2000)], dtype=np.uint64
    )
    drawn = kernel2.column_exponential_units(u53, Decimal(rate), scale).tolist()
    assert drawn == [kernel2.exponential_units(int(u), Decimal(rate), scale) for u in u53.tolist()]


def test_the_array_ppnd16_is_the_scalar_one_bit_for_bit() -> None:
    """Every branch and both cuts: |p - 0.5| = 0.425 and t = 5, that is p = exp(-25)."""
    edges = [1e-300, 1e-100, 1e-20, 0.075, 0.925, math.exp(-25), 0.3, 0.5, 0.99]
    ps = [math.nextafter(x, 0.0) for x in edges] + edges + [math.nextafter(x, 1.0) for x in edges]
    ps += [CHOOSER.random() for _ in range(3000)]
    ps += [math.ldexp(CHOOSER.random(), -CHOOSER.randrange(1, 900)) for _ in range(1000)]
    ps = [p for p in ps if 0.0 < p < 1.0]
    array = kernel2._ppnd16_array(np.array(ps))
    assert [value.hex() for value in array.tolist()] == [kernel2.ppnd16(p).hex() for p in ps]


def test_the_array_logarithm_is_the_scalar_one() -> None:
    xs = [math.ldexp(1.0, -1022), 1e-300, 0.5, 0.70710678, 1.0, 1.5, 2.0, 1e300]
    xs += [CHOOSER.random() for _ in range(2000)]
    array = kernel2.log_basic_array(np.array(xs))
    assert [value.hex() for value in array.tolist()] == [kernel2.log_basic(x).hex() for x in xs]


@pytest.mark.parametrize("scale", [0, 2, 6, 22])
def test_a_columns_units_are_the_scalar_decimals(scale: int) -> None:
    units = [0, 1, -1, 7, -(1 << 52), 1 << 52] + [
        CHOOSER.randrange(-(1 << 52), 1 << 52) for _ in range(500)
    ]
    drawn = kernel2.column_units_at(units, scale)
    assert [repr(value) for value in drawn] == [repr(kernel2.units_at(u, scale)) for u in units]


def test_a_columns_identifiers_are_its_cells() -> None:
    seam, label = b"\x07" * 32, b"sim\x1fs\x1fgen\x1forders\x1frow\x1f\x1forder_id"
    drawn = kernel2.column_identifier_digits(seam, label, ROWS)
    assert drawn == [kernel2.identifier_digits(seam, label, int(row)) for row in ROWS.tolist()]


@pytest.mark.parametrize(
    "domain",
    [
        1,
        2,
        3,
        1000,
        (1 << 32) + 5,
        (1 << 63),
        (1 << 64) - 1,
        1 << 64,
        (1 << 64) + 3,
        (1 << 100) + 7,
        (1 << 127) + 1,
        1 << 128,
    ],
)
def test_permute_2_on_an_array_is_the_scalar_one(domain: int) -> None:
    key = bytes(range(32))
    # The indexes a layout permutes are rows, which fit a word whatever the domain.
    top = min(domain, 1 << 63)
    indexes = sorted({0, top - 1} | {CHOOSER.randrange(top) for _ in range(200)})
    drawn = kernel2.permute2_array(key, domain, np.array(indexes, dtype=np.uint64))
    assert drawn == [permute2(key, domain, index) for index in indexes]


@pytest.mark.parametrize(
    ("key", "domain", "indexes"),
    [
        (bytes(range(32)), 10, [3, 10]),
        (bytes(range(32)), 10, [-1, 3]),
        (bytes(range(31)), 10, [3]),
        (bytes(range(32)), 0, [0]),
        (bytes(range(32)), (1 << 128) + 1, [0]),
    ],
    ids=["an index at the domain", "a negative index", "a short key", "no domain", "too wide"],
)
def test_permute_2_on_an_array_refuses_what_the_scalar_one_refuses(
    key: bytes, domain: int, indexes: List[int]
) -> None:
    """A refusal is what sends a slice back to the per-row path, so none may be missed."""
    with pytest.raises(FluxError):
        kernel2.permute2_array(key, domain, np.array(indexes, dtype=np.int64))
    if domain <= 1 << 128:
        with pytest.raises(FluxError):
            for index in indexes:
                permute2(key, domain, index)
