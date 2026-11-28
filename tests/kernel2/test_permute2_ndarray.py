# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The numpy ``permute/2`` is the array form it replaced, image for image (KERNEL-SPEC-2 section 7).

Every permutation of the engine passes through :func:`flux_engine.core.kernel2.permute2_array`,
which is now :func:`~flux_engine.core.kernel2.permute2_ndarray`'s images as a list. The array form it
was before is kept in ``_permute2_reference`` and the scalar :func:`flux_engine.core.permute.permute2`
is the specification, so each case below is held to both. The cases:

* domains 1 to 300 in full, and domains up to ``10**6`` sampled, with the boundaries of a word, of a
  chunk and of the halves (65535, 65536, 65537) in full;
* five keys on random domains and indexes;
* sizes 0, 1 and 2, and ``2**k - 1``, ``2**k`` and ``2**k + 1`` for every ``k`` up to 63 as domains;
* arrays one index short of, at and one past a chunk, and of two chunks;
* the largest domain an ndarray covers (``2**63``) and the largest an array covers (``2**128``);
* the refusals of the scalar function, and the vectors of ``permute/2``.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.permute import permute2

from ._permute2_reference import reference_permute2_array

pytestmark = pytest.mark.unit

KEYS = [bytes([seed]) * 32 for seed in (1, 2, 3, 5, 8)] + [bytes(range(32))]
CHOOSER = random.Random(2026_11_28)
VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "kernel2-streams-vectors.json"
#: The largest domain :func:`kernel2.permute2_ndarray` covers, and :func:`kernel2.permute2_array`.
NDARRAY_MAX_DOMAIN = kernel2.ARRAY_ROWS
ARRAY_MAX_DOMAIN = kernel2.PERMUTE_ARRAY_MAX_DOMAIN


def _same(key: bytes, domain: int, indexes: "np.ndarray") -> List[int]:
    """The reference's images, and the new array form's, which must be them; returns them."""
    wanted = reference_permute2_array(key, domain, indexes)
    assert kernel2.permute2_array(key, domain, indexes) == wanted
    if domain <= NDARRAY_MAX_DOMAIN:
        found = kernel2.permute2_ndarray(key, domain, indexes)
        assert found.dtype == np.int64 and found.tolist() == wanted
    return wanted


def _picked(domain: int, count: int) -> "np.ndarray":
    top = min(domain, 1 << 63)
    return np.array(
        sorted({0, top - 1} | {CHOOSER.randrange(top) for _ in range(count)}), dtype=np.int64
    )


@pytest.mark.parametrize("domain", range(1, 301))
def test_every_small_domain_is_permuted_as_it_was(domain: int) -> None:
    key = KEYS[domain % len(KEYS)]
    images = _same(key, domain, np.arange(domain, dtype=np.int64))
    assert sorted(images) == list(range(domain)), "a bijection"
    assert images == [permute2(key, domain, index) for index in range(domain)]


@pytest.mark.parametrize("key", KEYS[:2], ids=lambda key: key[:2].hex())
@pytest.mark.parametrize("domain", [65535, 65536, 65537])
def test_the_domains_around_a_word_are_permuted_in_full(key: bytes, domain: int) -> None:
    images = _same(key, domain, np.arange(domain, dtype=np.int64))
    assert sorted(images) == list(range(domain))


def test_domains_up_to_a_million_are_permuted_as_they_were() -> None:
    domains = sorted(
        {CHOOSER.randrange(1, 10**6 + 1) for _ in range(120)}
        | {99991, 99999, 100000, 100001, 524287, 524288, 524289, 10**6 - 1, 10**6}
        | {1 << k for k in range(1, 20)}
        | {(1 << k) - 1 for k in range(2, 20)}
        | {(1 << k) + 1 for k in range(1, 20)}
    )
    for domain in domains:
        key = KEYS[domain % len(KEYS)]
        _same(key, domain, _picked(domain, 400))


@pytest.mark.parametrize("key", KEYS, ids=lambda key: key[:2].hex())
def test_random_domains_under_every_key(key: bytes) -> None:
    for _ in range(25):
        domain = CHOOSER.randrange(1, 1 << CHOOSER.randrange(1, 64))
        _same(key, domain, _picked(domain, 300))


@pytest.mark.parametrize("k", range(0, 64))
def test_the_sizes_around_every_power_of_two_are_permuted_as_they_were(k: int) -> None:
    for domain in (1 << k, (1 << k) - 1, (1 << k) + 1):
        if not 1 <= domain <= NDARRAY_MAX_DOMAIN:
            continue
        key = KEYS[(domain + k) % len(KEYS)]
        _same(key, domain, _picked(domain, 120))
        # the first and the last indexes of the domain, where a half wraps
        top = domain - 1
        _same(key, domain, np.array([0, top, max(top - 1, 0), min(1, top)], dtype=np.int64))


@pytest.mark.parametrize("domain", [1, 2, 3, 1 << 20, 1 << 40])
def test_an_array_of_no_index_one_or_two(domain: int) -> None:
    key = KEYS[0]
    assert kernel2.permute2_ndarray(key, domain, np.array([], dtype=np.int64)).shape == (0,)
    assert kernel2.permute2_array(key, domain, np.array([], dtype=np.int64)) == []
    _same(key, domain, np.array([0], dtype=np.int64))
    if domain >= 2:
        _same(key, domain, np.array([0, 1], dtype=np.int64))
        _same(key, domain, np.array([1, 0], dtype=np.int64))


@pytest.mark.parametrize("extra", [-1, 0, 1])
@pytest.mark.parametrize("chunks", [1, 2])
def test_an_array_one_short_of_at_and_one_past_a_chunk(extra: int, chunks: int) -> None:
    size = chunks * kernel2.PERMUTE_CHUNK + extra
    for domain in (size, size + 3, 1 << 40):
        key = KEYS[domain % len(KEYS)]
        indexes = np.arange(size, dtype=np.int64)
        _same(key, domain, indexes)


def test_indexes_out_of_order_and_repeated_are_each_permuted_alone() -> None:
    key, domain = KEYS[1], 1_000_003
    indexes = np.array(
        [CHOOSER.randrange(domain) for _ in range(kernel2.PERMUTE_CHUNK + 5)], dtype=np.int64
    )
    _same(key, domain, indexes)
    again = kernel2.permute2_ndarray(key, domain, indexes[::-1].copy())
    assert again.tolist() == kernel2.permute2_ndarray(key, domain, indexes)[::-1].tolist()


def test_the_largest_domain_an_ndarray_covers_is_two_to_the_63() -> None:
    assert NDARRAY_MAX_DOMAIN == 1 << 63
    key = KEYS[2]
    for domain in (NDARRAY_MAX_DOMAIN, NDARRAY_MAX_DOMAIN - 1):
        indexes = _picked(domain, 2000)
        found = kernel2.permute2_ndarray(key, domain, indexes)
        assert found.tolist() == [permute2(key, domain, int(index)) for index in indexes]
        assert int(found.min()) >= 0 and int(found.max()) < domain
        _same(key, domain, indexes)
    with pytest.raises(FluxError):
        kernel2.permute2_ndarray(key, NDARRAY_MAX_DOMAIN + 1, np.array([0], dtype=np.int64))


@pytest.mark.parametrize(
    "domain", [(1 << 63) + 1, 1 << 64, (1 << 64) + 3, (1 << 100) + 7, (1 << 127) + 1, 1 << 128]
)
def test_the_array_form_still_covers_up_to_two_to_the_128(domain: int) -> None:
    assert ARRAY_MAX_DOMAIN == 1 << 128
    key = KEYS[3]
    indexes = _picked(domain, 500)
    for dtype in (np.int64, np.uint64):
        held = indexes.astype(dtype)
        assert kernel2.permute2_array(key, domain, held) == [
            permute2(key, domain, int(index)) for index in indexes
        ]
        assert reference_permute2_array(key, domain, held) == kernel2.permute2_array(
            key, domain, held
        )


def test_an_array_of_python_integers_past_a_word_is_permuted_as_it_was() -> None:
    key, domain = KEYS[4], 1 << 127
    indexes = np.array([0, 1, (1 << 64) + 5, (1 << 126) + 9], dtype=object)
    assert kernel2.permute2_array(key, domain, indexes) == reference_permute2_array(
        key, domain, indexes
    )


@pytest.mark.parametrize(
    ("key", "domain", "indexes"),
    [
        (KEYS[0], 10, [3, 10]),
        (KEYS[0], 10, [-1, 3]),
        (bytes(range(31)), 10, [3]),
        (KEYS[0], 0, [0]),
        (KEYS[0], (1 << 128) + 1, [0]),
    ],
)
def test_what_the_reference_refuses_is_refused(key: bytes, domain: int, indexes: List[int]) -> None:
    held = np.array(indexes, dtype=np.int64)
    with pytest.raises(FluxError):
        reference_permute2_array(key, domain, held)
    with pytest.raises(FluxError):
        kernel2.permute2_array(key, domain, held)
    with pytest.raises(FluxError):
        kernel2.permute2_ndarray(key, domain, held)


def _vectors() -> List[Dict[str, Any]]:
    document = json.loads(VECTORS.read_text(encoding="utf-8"))
    return list(document["permute"])


@pytest.mark.parametrize("case", _vectors(), ids=lambda case: case["name"])
def test_both_array_forms_give_the_images_the_permute_2_vectors_pin(case: Dict[str, Any]) -> None:
    from flux_engine.generator.relations import layout_key

    seam = "sha256:" + json.loads(VECTORS.read_text(encoding="utf-8"))["seam"]
    key = layout_key(seam, case["keyParts"], counter_based=True)
    domain = int(case["domain"])
    indexes = np.array([int(index) for index in case["indexes"]], dtype=object)
    wanted = [int(image) for image in case["images"]]
    if domain <= ARRAY_MAX_DOMAIN:
        assert kernel2.permute2_array(key, domain, indexes) == wanted
    if domain <= NDARRAY_MAX_DOMAIN:
        held = indexes.astype(np.int64)
        assert kernel2.permute2_ndarray(key, domain, held).tolist() == wanted
