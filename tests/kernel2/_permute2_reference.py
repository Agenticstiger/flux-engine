# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The array form of ``permute/2`` as it was before :func:`flux_engine.core.kernel2.permute2_ndarray`.

Kept only as the reference the numpy implementation is pinned against (``test_permute2_ndarray``):
the same rounds and cycle walking, with the values held as Python integers on the way in and out.
Do not change it; a change here changes what the tests hold the engine to.
"""

from __future__ import annotations

from typing import List

import numpy as np

from flux_engine.core.errors import FluxError
from flux_engine.core.kernel2 import (
    PERMUTE_ARRAY_MAX_DOMAIN,
    permute_round_key,
    philox_word0,
)
from flux_engine.core.rng import FLX_5021

_PERMUTE_KEY_BYTES = 32


def reference_permute2_array(key: bytes, domain: int, indexes: "np.ndarray") -> List[int]:
    """``permute/2`` of every index in *indexes*, for a domain of at most ``2**128``.

    :func:`flux_engine.core.permute.permute2`'s rounds and cycle walking on uint64 arrays.
    A value is held as its two halves, each of which fits a word, so a round adds the
    block's word 0 to one half and masks it, and only the values that walked out of the
    domain take another pass. The images come back as Python integers, in order.
    """
    if not 1 <= domain <= PERMUTE_ARRAY_MAX_DOMAIN:
        raise FluxError(
            FLX_5021, f"an array permutation covers at most 2**128 values, got {domain}"
        )
    size = indexes.shape[0]
    # permute2's own refusals (flux_engine.core.permute._check), for every index at once,
    # so that an index it refuses is refused here too.
    if not isinstance(key, bytes) or len(key) != _PERMUTE_KEY_BYTES:
        raise FluxError(FLX_5021, f"a permutation key is {_PERMUTE_KEY_BYTES} bytes")
    if size and not (int(indexes.min()) >= 0 and int(indexes.max()) < domain):
        raise FluxError(FLX_5021, f"a permutation index lies in [0, {domain})")
    if domain == 1:
        return [0] * size
    bits = (domain - 1).bit_length()
    left_bits = bits // 2
    right_bits = bits - left_bits
    left_mask = np.uint64((1 << left_bits) - 1)
    right_mask = np.uint64((1 << right_bits) - 1)
    # Every value of `bits` bits lies inside a domain that is exactly 2**bits.
    whole = domain == 1 << bits
    domain_left = np.uint64(0 if whole else domain >> right_bits)
    domain_right = np.uint64(0 if whole else domain & ((1 << right_bits) - 1))
    round_key = permute_round_key(key, domain)
    values = [int(index) for index in indexes.tolist()]
    left = np.array([value >> right_bits for value in values], dtype=np.uint64)
    right = np.array([value & ((1 << right_bits) - 1) for value in values], dtype=np.uint64)
    done_left = np.zeros(size, dtype=np.uint64)
    done_right = np.zeros(size, dtype=np.uint64)
    pending = np.arange(size)
    while pending.shape[0]:
        count = pending.shape[0]
        zeros = np.zeros(count, dtype=np.uint64)
        for round_index in range(4):
            stage = np.full(count, round_index, dtype=np.uint64)
            if round_index % 2 == 0:
                word = philox_word0(right, zeros, stage, zeros, round_key)
                left = (left + word) & left_mask
            else:
                word = philox_word0(left, zeros, stage, zeros, round_key)
                right = (right + word) & right_mask
        if whole:
            inside = np.ones(count, dtype=bool)
        else:
            inside = (left < domain_left) | ((left == domain_left) & (right < domain_right))
        done_left[pending[inside]] = left[inside]
        done_right[pending[inside]] = right[inside]
        outside = ~inside
        pending, left, right = pending[outside], left[outside], right[outside]
    return [
        (high << right_bits) | low
        for high, low in zip(done_left.tolist(), done_right.tolist(), strict=True)
    ]
