# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC section 4.1: the keyed permutation a generated key reads at any index.

What is pinned here:

* **It is a permutation.** For every domain from 1 to 300 and several keys,
  the images of ``0..domain-1`` are exactly ``0..domain-1``. A bijection is
  what makes a laid-out key unique by construction.
* **It stays in its domain** at the sizes the generator uses: a product of a
  parent's rows and a range (4.32e9), a 128-bit identifier space and the
  largest domain a key is spelled for.
* **It is a function of its arguments**, and a different key or a different
  domain gives an unrelated arrangement (the negative controls).
* **It spreads.** The first half of a domain's indexes lands on every tenth of
  the domain in roughly equal measure: nothing like the identity, and nothing
  that piles up.
* **It refuses** what it cannot form (``FLX-5021``), as the other kernel
  primitives do.

``tests/vectors/permute-vectors.json`` pins images for a handful of domains,
so a second implementation can check itself against this one. Regenerate it
with ``python tests/core/test_permute.py`` after a deliberate change to the
construction, which moves every key FLUX generates for a contract that
declares keys.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.permute import (
    PERMUTE_KEY_BYTES,
    PERMUTE_MAX_DOMAIN,
    PERMUTE_ROUNDS,
    permute,
)

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "permute-vectors.json"

KEY = bytes(range(PERMUTE_KEY_BYTES))
OTHER_KEY = bytes(reversed(range(PERMUTE_KEY_BYTES)))

#: Domains the vector file pins, with the indexes it pins in each.
_VECTOR_DOMAINS: List[Dict[str, Any]] = [
    {"name": "one value", "domain": 1, "indexes": [0]},
    {"name": "two values: one bit, an empty left part", "domain": 2, "indexes": [0, 1]},
    {
        "name": "ten values: walked from a 16-value network",
        "domain": 10,
        "indexes": list(range(10)),
    },
    {"name": "a power of two", "domain": 1024, "indexes": [0, 1, 2, 511, 1023]},
    {"name": "one above a power of two", "domain": 1025, "indexes": [0, 1, 1024]},
    {
        "name": "items times tickets, a several-column key",
        "domain": 18_000 * 240_000,
        "indexes": [0, 1, 2_880_404],
    },
    {"name": "a 32-hex-character identifier", "domain": 16**32, "indexes": [0, 1, 99]},
    {"name": "the largest domain", "domain": PERMUTE_MAX_DOMAIN, "indexes": [0, 7]},
]


def _vectors() -> Dict[str, Any]:
    return {
        "description": (
            "Pinned images of flux_engine.core.permute.permute (KERNEL-SPEC section 4.1): "
            "a four-round alternating Feistel network keyed by SHA-256, with cycle walking. "
            "Every image below is computed by the engine, never typed."
        ),
        "regenerate": "python tests/core/test_permute.py",
        "rounds": PERMUTE_ROUNDS,
        "vectors": [
            {
                "name": entry["name"],
                "key": key.hex(),
                "domain": str(entry["domain"]),
                "indexes": [str(index) for index in entry["indexes"]],
                "images": [str(permute(key, entry["domain"], index)) for index in entry["indexes"]],
            }
            for key in (KEY, OTHER_KEY)
            for entry in _VECTOR_DOMAINS
        ],
    }


def load_vectors() -> List[Dict[str, Any]]:
    document = json.loads(VECTORS.read_text(encoding="utf-8"))
    vectors = document["vectors"]
    assert isinstance(vectors, list)
    return vectors


@pytest.mark.golden
@pytest.mark.parametrize(
    "vector", load_vectors(), ids=lambda v: f"{str(v['name'])[:40]}-{str(v['key'])[:4]}"
)
def test_the_pinned_images_are_reproduced(vector: Dict[str, Any]) -> None:
    key, domain = bytes.fromhex(vector["key"]), int(vector["domain"])
    images = [permute(key, domain, int(index)) for index in vector["indexes"]]
    assert [str(image) for image in images] == vector["images"]


@pytest.mark.golden
def test_the_vector_file_pins_the_round_count() -> None:
    document = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert document["rounds"] == PERMUTE_ROUNDS == 4


@pytest.mark.unit
@pytest.mark.parametrize("key", [KEY, OTHER_KEY, bytes(PERMUTE_KEY_BYTES)])
def test_every_small_domain_is_permuted(key: bytes) -> None:
    for domain in range(1, 301):
        assert sorted(permute(key, domain, index) for index in range(domain)) == list(
            range(domain)
        ), domain


@pytest.mark.unit
@pytest.mark.parametrize("domain", [18_000 * 240_000, 16**32, 1 << 122, PERMUTE_MAX_DOMAIN])
def test_a_large_domain_keeps_its_images_inside_and_apart(domain: int) -> None:
    indexes = [0, 1, 2, 3, 1_000, domain // 2, domain - 1]
    images = [permute(KEY, domain, index) for index in indexes]
    assert all(0 <= image < domain for image in images)
    assert len(set(images)) == len(images)


@pytest.mark.unit
def test_the_image_is_a_function_of_the_arguments() -> None:
    assert [permute(KEY, 1000, i) for i in range(50)] == [permute(KEY, 1000, i) for i in range(50)]


@pytest.mark.unit
def test_a_different_key_or_domain_arranges_differently() -> None:
    base = [permute(KEY, 1000, i) for i in range(100)]
    assert base != [permute(OTHER_KEY, 1000, i) for i in range(100)]
    assert base != [permute(KEY, 1001, i) for i in range(100)]
    assert base != list(range(100))


@pytest.mark.unit
def test_the_first_half_of_the_indexes_spreads_over_the_domain() -> None:
    domain = 10_000
    tenths = [0] * 10
    for index in range(domain // 2):
        tenths[permute(KEY, domain, index) * 10 // domain] += 1
    # 5,000 points over ten equal parts: 500 each in expectation. A sample
    # without replacement varies less than a binomial (sd below 21), so 150
    # either side is a loose bound that the identity (5,000 / 0) fails.
    assert all(350 <= count <= 650 for count in tenths), tenths


@pytest.mark.unit
@pytest.mark.parametrize(
    "key, domain, index",
    [
        (b"short", 10, 0),
        (KEY + b"x", 10, 0),
        ("0" * 32, 10, 0),
        (KEY, 0, 0),
        (KEY, -3, 0),
        (KEY, True, 0),
        (KEY, PERMUTE_MAX_DOMAIN + 1, 0),
        (KEY, 10, 10),
        (KEY, 10, -1),
        (KEY, 10, True),
        (KEY, 10.0, 1),
    ],
)
def test_what_cannot_be_formed_is_refused(key: Any, domain: Any, index: Any) -> None:
    with pytest.raises(FluxError) as caught:
        permute(key, domain, index)
    assert caught.value.code.code == "FLX-5021"


if __name__ == "__main__":  # pragma: no cover - regenerates the vector file
    VECTORS.write_text(json.dumps(_vectors(), indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(f"wrote {VECTORS}\n")
