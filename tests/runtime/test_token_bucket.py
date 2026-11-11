# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The delivery-side token bucket: deterministic, integer-only, virtual time."""

from __future__ import annotations

import pytest

from flux_engine.runtime.campaign import TokenBucket

pytestmark = pytest.mark.unit


def test_a_full_bucket_serves_a_second_of_traffic_then_stops() -> None:
    bucket = TokenBucket(3, start_ms=0)
    assert [bucket.take(0) for _ in range(4)] == [True, True, True, False]


def test_the_bucket_refills_in_virtual_time() -> None:
    bucket = TokenBucket(2, start_ms=0)
    assert bucket.take(0) and bucket.take(0)
    assert not bucket.take(0)
    assert bucket.take(500)


def test_the_bucket_never_exceeds_one_seconds_capacity() -> None:
    bucket = TokenBucket(2, start_ms=0)
    bucket.take(0)
    bucket.take(10**9)
    assert bucket.tokens == 2 * 1000 - 1000


def test_an_out_of_order_request_cannot_mint_tokens() -> None:
    bucket = TokenBucket(1, start_ms=1000)
    assert bucket.take(1000)
    assert not bucket.take(0)


def test_the_bucket_is_integer_only() -> None:
    bucket = TokenBucket(7, start_ms=0)
    bucket.take(3)
    assert isinstance(bucket.tokens, int)


def test_a_rate_limit_below_one_is_refused() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0, start_ms=0)


def test_an_out_of_order_request_does_not_drain_the_bucket_either() -> None:
    """The elapsed term is floored at zero, so time never runs backwards.

    An unguarded ``t_ms - self._last_ms`` would also *subtract* tokens for a
    request behind the last one served, which is not "cannot mint" but "can
    destroy": the bucket would still be dry a full refill period later. The
    later in-order request below is the one that tells the two apart.
    """
    bucket = TokenBucket(1, start_ms=1000)
    assert bucket.take(1000)
    assert not bucket.take(0)
    assert bucket.tokens == 0
    assert bucket.take(2000), "one second of virtual time did not refill one token"
