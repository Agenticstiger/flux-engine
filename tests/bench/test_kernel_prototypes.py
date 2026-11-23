# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The ADR 0010 prototypes still work, and give the same bits wherever CI runs them.

``kernel_streams`` and ``kernel_normal`` are measurements, not engine code. These
checks keep them honest as the engine moves: the array Philox agrees with numpy's,
the normal transform agrees with the engine's ``truncated_normal`` at the declared
scale, and numpy agrees with plain Python bit for bit.

The two digests were computed on macOS (arm64) under Python 3.13 and numpy 2.4.6.
Unlike the rest of ``tests/bench`` this module is fast on purpose: the pull-request
matrix runs the fast suite on Linux (x86_64) under Python 3.11, 3.12 and 3.13, so
an operating system, CPU, interpreter or numpy release that computed one bit
differently fails here. That is the claim ADR 0010 makes for the transform.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import numpy as np
import pytest

from flux_engine.core.seedtree import derive_root
from flux_engine.generator.paths import CellStreams
from tests.bench import kernel_normal, kernel_streams

pytestmark = pytest.mark.unit

STREAMS_DIGEST = "1e60aea868776515fd5b09666bf8a786c16c13db0a35489800ee087b25ddadac"
NORMAL_DIGEST = "b3d5cd8d052595b47f9cadca234894d25fc549ac930b90db9ae0ef62d6848953"


def _pinned_key() -> kernel_streams.Key:
    return kernel_streams.column_key(hashlib.sha256(b"pinned").digest(), b"sim\x1fbench\x1famount")


def test_array_philox_agrees_with_numpy_philox() -> None:
    kernel_streams.check(_pinned_key(), cases=50, rows=2_000)


def test_hash_floor_computes_kernel_v1_stream_entropy() -> None:
    root = derive_root(hashlib.sha256(b"pinned").digest(), 7)
    kernel_streams.check_hash_floor(CellStreams(root, "bench", "orders"))


def test_array_philox_words_are_pinned() -> None:
    rows = np.arange(4096, dtype=np.uint64)
    counters = np.zeros((rows.size, 4), dtype=np.uint64)
    counters[:, 0] = rows
    words = kernel_streams.philox4x64_10(counters, _pinned_key())
    assert hashlib.sha256(words.astype("<u8").tobytes()).hexdigest() == STREAMS_DIGEST


@pytest.mark.parametrize("interval", kernel_normal.INTERVALS, ids=lambda item: item.name)
def test_normal_transform_agrees_with_kernel_v1(interval: kernel_normal.Interval) -> None:
    plan = kernel_normal.plan_of(interval)
    result = kernel_normal.accuracy(kernel_normal.uniforms(200), plan)
    assert result.scale_mismatches == 0
    assert result.largest_grid_error < 1e-6


@pytest.mark.parametrize("interval", kernel_normal.INTERVALS, ids=lambda item: item.name)
def test_normal_transform_keeps_order_at_the_declared_scale(
    interval: kernel_normal.Interval,
) -> None:
    order = kernel_normal.monotonicity(kernel_normal.plan_of(interval), 4_000)
    assert order.pairs > 0 and order.boundary_windows > 0
    assert order.backward_at_scale == 0
    assert order.largest_ulps <= 4
    assert order.reach <= 3


def test_an_interval_too_far_out_for_binary64_is_refused() -> None:
    far = kernel_normal.Interval(
        "far", Decimal(0), Decimal(1), Decimal(40), Decimal(41), kernel_normal.INTERVALS[0].scale
    )
    with pytest.raises(ValueError, match="too little for binary64"):
        kernel_normal.plan_of(far)


def test_numpy_and_plain_python_give_the_same_bits() -> None:
    inputs = kernel_normal.uniforms(2_000)
    for interval in kernel_normal.INTERVALS:
        assert kernel_normal.scalar_mismatches(inputs, kernel_normal.plan_of(interval)) == 0


def test_normal_transform_bits_are_pinned() -> None:
    plans = [kernel_normal.plan_of(interval) for interval in kernel_normal.INTERVALS]
    assert kernel_normal.bits_digest(kernel_normal.uniforms(2_000), plans) == NORMAL_DIGEST
