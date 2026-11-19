# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A keyed permutation of ``[0, domain)`` that can be read at any index (KERNEL-SPEC section 4.1).

:func:`flux_engine.core.rng.permutation` shuffles a whole sequence, so reading one
element of it means building all of it. The generator needs the opposite: the
value at row ``i`` of a table of millions, computed in whichever process holds
row ``i``, without the other rows. :func:`permute` gives exactly that. It is a
bijection on ``[0, domain)`` for every key, so distinct indexes always land on
distinct values, which is what makes a generated key unique by construction
rather than unique with high probability (a hashed ten-digit identifier already
repeats about 5,000 times in 10**7 rows).

The construction is the textbook one for an arbitrary domain:

* an alternating Feistel network on the ``b`` bits that hold ``domain - 1``,
  split into a left part of ``b // 2`` bits and a right part of the rest, as
  NIST SP 800-38G's FF1 splits a numeral string. Each round adds a keyed
  SHA-256 of one part to the other, modulo that part's size. A Feistel
  network is invertible whatever its round function is, so the map is a
  permutation of ``[0, 2**b)``;
* cycle walking (Black and Rogaway, "Ciphers with Arbitrary Finite Domains",
  2002): a value that lands at or above ``domain`` is fed through the network
  again until it lands below it. The walk stays on the cycle of the starting
  index, which holds that index, so it ends; ``2**b < 2 * domain``, so it
  takes fewer than two steps on average. No stream is read, so the number of
  steps moves no stream position.

Four rounds: Luby and Rackoff showed that four rounds of a pseudorandom
round function make a strong pseudorandom permutation, and keyed SHA-256 is
the round function. The purpose here is statistical (a spread that looks
uniform, and no repeats), not secrecy, so more rounds would buy nothing a
reader of the data could see. The round function reads
:func:`flux_engine.core.canonical.sha256_hex`, the engine's one hashing
primitive.

Prior art: Kensler's permute() (Pixar Technical Memo 13-01) and pbrt-v4's
``PermutationElement`` (Apache-2.0) walk cycles the same way over a
power-of-two hash; a Feistel network is used here instead because it is a
bijection by construction, for any round function.
"""

from __future__ import annotations

from typing import Tuple

from flux_engine.core import kernel2
from flux_engine.core.canonical import sha256_hex
from flux_engine.core.errors import FluxError
from flux_engine.core.rng import FLX_5021

__all__ = [
    "PERMUTE_KEY_BYTES",
    "PERMUTE_LABEL",
    "PERMUTE_MAX_DOMAIN",
    "PERMUTE_ROUNDS",
    "permute",
    "permute2",
]

#: Domain-separation label, first in every round's pre-image. Changing it
#: moves every permuted value.
PERMUTE_LABEL = b"flux-permute/1"

#: Bytes in a permutation key.
PERMUTE_KEY_BYTES = 32

#: Feistel rounds: two for each part.
PERMUTE_ROUNDS = 4

#: The largest domain a key of :data:`PERMUTE_KEY_BYTES` bytes is spelled for.
PERMUTE_MAX_DOMAIN = 1 << 256

_NUL = b"\x00"
#: Bytes a part or a domain is written in inside a round's pre-image.
_WORD_BYTES = 32


def _split(domain: int) -> Tuple[int, int]:
    """The bit widths of the left and right parts for *domain* (``domain >= 2``)."""
    bits = (domain - 1).bit_length()
    left = bits // 2
    return left, bits - left


def _check(key: bytes, domain: int, index: int) -> None:
    if not isinstance(key, bytes) or len(key) != PERMUTE_KEY_BYTES:
        raise FluxError(FLX_5021, f"a permutation key is {PERMUTE_KEY_BYTES} bytes")
    if (
        isinstance(domain, bool)
        or not isinstance(domain, int)
        or not 1 <= domain <= PERMUTE_MAX_DOMAIN
    ):
        raise FluxError(
            FLX_5021, f"a permutation domain is an integer in [1, 2**256], got {domain!r}"
        )
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < domain:
        raise FluxError(FLX_5021, f"a permutation index lies in [0, {domain}), got {index!r}")


def permute(key: bytes, domain: int, index: int) -> int:
    """The image of *index* under the permutation of ``[0, domain)`` that *key* selects.

    For a fixed key and domain, distinct indexes give distinct images, and every
    image lies in ``[0, domain)``. The result is a function of the three
    arguments alone.
    """
    _check(key, domain, index)
    if domain == 1:
        return 0
    left_bits, right_bits = _split(domain)
    left_mask, right_mask = (1 << left_bits) - 1, (1 << right_bits) - 1
    prefix = PERMUTE_LABEL + _NUL + key + _NUL + domain.to_bytes(_WORD_BYTES + 1, "big")
    value = index
    while True:
        left, right = value >> right_bits, value & right_mask
        for round_index in range(PERMUTE_ROUNDS):
            if round_index % 2 == 0:
                material = prefix + bytes((round_index,)) + right.to_bytes(_WORD_BYTES, "big")
                left = (left + int(sha256_hex(material), 16)) & left_mask
            else:
                material = prefix + bytes((round_index,)) + left.to_bytes(_WORD_BYTES, "big")
                right = (right + int(sha256_hex(material), 16)) & right_mask
        value = (left << right_bits) | right
        if value < domain:
            return value


def permute2(key: bytes, domain: int, index: int) -> int:
    """:func:`permute` under kernel ``0.2.0``: ``permute/2`` (KERNEL-SPEC-2 section 7).

    The split, the four alternating Feistel rounds and the cycle walking are
    :func:`permute`'s. Only the round function changes: a Philox block keyed by
    ``sha256("flux-permute/2" NUL key NUL domain)``, whose counter holds the half and
    the round, so a key's values come from integer arithmetic rather than a sha256 per
    round.
    """
    _check(key, domain, index)
    if domain == 1:
        return 0
    left_bits, right_bits = _split(domain)
    left_mask, right_mask = (1 << left_bits) - 1, (1 << right_bits) - 1
    round_key = kernel2.permute_round_key(key, domain)
    value = index
    while True:
        left, right = value >> right_bits, value & right_mask
        for round_index in range(PERMUTE_ROUNDS):
            if round_index % 2 == 0:
                left = (left + kernel2.permute_round(round_key, round_index, right)) & left_mask
            else:
                right = (right + kernel2.permute_round(round_key, round_index, left)) & right_mask
        value = (left << right_bits) | right
        if value < domain:
            return value
