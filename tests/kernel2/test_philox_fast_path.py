# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Contiguous counters through numpy's C Philox equal the ufunc rounds, bit for bit.

numpy's Philox is Random123's Philox4x64-10; the known-answer vector below is Random123's
(counter 0, key 0).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from flux_engine.core import kernel2

WORD = (1 << 64) - 1
RANDOM123_ZERO = (
    0x16554D9ECA36314C,
    0xDB20FE9D672D0FDC,
    0xD7E772CEE186176B,
    0x7E68B68AEC7BA23B,
)


def _arr(values: Any) -> "np.ndarray":
    return np.array(values, dtype=np.uint64)


def _run(first: int, size: int, c1: int, c2: int, c3: int) -> Any:
    rows = _arr([first + i for i in range(size)])
    full = lambda v: np.full(size, v, dtype=np.uint64)  # noqa: E731
    return rows, full(c1), full(c2), full(c3)


@pytest.fixture(autouse=True)
def _fresh_decision() -> Any:
    saved = kernel2._FAST_PATH
    kernel2._FAST_PATH = None
    yield
    kernel2._FAST_PATH = saved


def test_random123_known_answer_both_paths() -> None:
    args = _run(0, 1, 0, 0, 0)
    fast = kernel2.philox_arrays(*args, (0, 0))
    slow = kernel2._philox_ufunc(*args, (0, 0))
    assert tuple(int(w[0]) for w in fast) == RANDOM123_ZERO
    assert tuple(int(w[0]) for w in slow) == RANDOM123_ZERO
    assert kernel2.fast_path_enabled()


@settings(max_examples=60, deadline=None)
@given(
    key=st.tuples(st.integers(0, WORD), st.integers(0, WORD)),
    first=st.one_of(st.integers(0, WORD - 40), st.integers(WORD - 40, WORD - 20)),
    size=st.integers(0, 20),
    c1=st.integers(0, WORD),
    c2=st.integers(0, WORD),
    c3=st.integers(0, WORD),
)
def test_fast_equals_ufunc(key: Any, first: int, size: int, c1: int, c2: int, c3: int) -> None:
    args = _run(first, size, c1, c2, c3)
    fast = kernel2.philox_arrays(*args, key)
    slow = kernel2._philox_ufunc(*args, key)
    for got, want in zip(fast, slow, strict=True):
        assert got.dtype == np.uint64
        assert np.array_equal(got, want)
    assert np.array_equal(kernel2.philox_word0(*args, key), slow[0])


def test_non_contiguous_counters_use_the_oracle(monkeypatch: pytest.MonkeyPatch) -> None:
    assert kernel2.fast_path_enabled()
    calls: list = []
    monkeypatch.setattr(kernel2, "_fast_blocks", lambda *a: calls.append(a))
    z = _arr([0, 0, 0])
    for rows in ([1, 3, 5], [4, 4, 5], [WORD - 1, WORD, 0]):  # step, duplicate, carry
        kernel2.philox_arrays(_arr(rows), z, z, z, (1, 2))
    kernel2.philox_arrays(_arr([1, 2, 3]), _arr([0, 1, 0]), z, z, (1, 2))
    assert calls == []


def test_contiguous_column_draw_runs_no_ufunc_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert kernel2.fast_path_enabled()
    rounds = []
    real = kernel2._mulhilo
    monkeypatch.setattr(kernel2, "_mulhilo", lambda a, m: rounds.append(1) or real(a, m))
    rows = np.arange(500, dtype=np.int64)
    got = kernel2.column_words((3, 4), rows, 2)
    assert rounds == []
    monkeypatch.setattr(kernel2, "_FAST_PATH", False)
    assert np.array_equal(got, kernel2.column_words((3, 4), rows, 2))
    assert len(rounds) == 20  # ten rounds, two multiplies each


def test_pending_rows_retry_equals_the_oracle() -> None:
    rows = np.arange(2000, dtype=np.int64)
    n = (1 << 63) + 12345  # rejects often, so the pending rows are not contiguous
    got = kernel2.column_bounded((9, 10), rows, 1, n)
    kernel2._FAST_PATH = False
    assert np.array_equal(got, kernel2.column_bounded((9, 10), rows, 1, n))


def test_forced_mismatch_disables_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    real = kernel2._fast_blocks

    def broken(first: int, count: int, key: Any) -> Any:
        blocks = real(first, count, key).copy()
        blocks ^= np.uint64(1)
        return blocks

    monkeypatch.setattr(kernel2, "_fast_blocks", broken)
    assert not kernel2.fast_path_enabled()
    args = _run(0, 4, 0, 0, 0)
    assert np.array_equal(
        kernel2.philox_arrays(*args, (5, 6))[0], kernel2._philox_ufunc(*args, (5, 6))[0]
    )


def test_a_failing_numpy_disables_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any) -> Any:
        raise ValueError("state layout changed")

    monkeypatch.setattr(kernel2, "_fast_blocks", boom)
    assert not kernel2.fast_path_enabled()


def test_threads_draw_independently() -> None:
    from concurrent.futures import ThreadPoolExecutor

    def draw(seed: int) -> bool:
        args = _run(seed * 100, 50, seed, 0, 1)
        key = (seed, seed + 1)
        return all(
            np.array_equal(kernel2.philox_word0(*args, key), kernel2._philox_ufunc(*args, key)[0])
            for _ in range(200)
        )

    with ThreadPoolExecutor(8) as pool:
        assert all(pool.map(draw, range(8)))
