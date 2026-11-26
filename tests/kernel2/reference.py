# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The executable reference for KERNEL-SPEC-2: kernel v2's streams, draws and transforms.

Written to be read beside ``docs/spec/KERNEL-SPEC-2.md``, rule by rule, and to generate the
vector files that pin it (``tests/vectors/kernel2-*-vectors.json``). It is not engine code,
and nothing in ``flux_engine`` imports it: the engine's kernel v2 (step 3 of ADR 0010) is
held to the vector files, not to this module.

Every binary64 operation is a separate numpy ufunc or Python operation, written in the
order the specification gives, so no two are ever fused into one multiply-add. Where a rule
is kernel v1's unchanged (``bernoulli``, ``categorical``, ``poisson``, Φ at a declaration's
bounds, the UUID layout), this module calls kernel v1's own code on a one-word stream, so
the two cannot drift apart.

Sources: Philox4x64-10 is Random123's (Salmon, Moraes, Dror and Shaw, "Parallel random
numbers: as easy as 1, 2, 3", SC11), checked against its published known-answer vectors.
The bounded draw is Lemire's test ("Fast random integer generation in an interval", ACM
TOMACS 29(1), 2019). Φ⁻¹ is Wichura's AS241 (Applied Statistics 37(3), 1988) with the
coefficients and the order of operations of CPython's pure-Python
``statistics.NormalDist.inv_cdf``. The logarithm follows the method of fdlibm's
``e_log.c`` (Sun Microsystems, 1993).
"""

from __future__ import annotations

import bisect
import hashlib
import math
import struct
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from numpy.random import Philox

from flux_engine.core import rng as v1
from flux_engine.core.seedtree import PathKind, PathShape, match_shape, path_label
from flux_engine.generator.paths import (
    ALLOCATE_PATH_SHAPE,
    ASSOCIATE_PATH_SHAPE,
    COUNT_PATH_SHAPE,
    GENERATOR_PATH_SHAPE,
    matches_generator_shape,
)
from flux_engine.generator.rows import _variant_nibble

KERNEL_VERSION = "0.2.0"
SEED_TREE_VERSION = "flux-seed/2"
IDENTIFIER_LABEL = b"flux-gen/2"
KEY_LABEL = b"flux-key/2"
PERMUTE_LABEL = b"flux-permute/2"

NUL = b"\x00"
SEPARATOR = b"\x1f"
WORD_MASK = (1 << 64) - 1
COUNTER_SPACE = 1 << 256

Key = Tuple[int, int]
Block = Tuple[int, int, int, int]


# ---------------------------------------------------------------------------
# Philox4x64-10 (KERNEL-SPEC-2 section 3)
# ---------------------------------------------------------------------------

PHILOX_M0 = 0xD2E7470EE14C6C93
PHILOX_M1 = 0xCA5A826395121157
PHILOX_W0 = 0x9E3779B97F4A7C15
PHILOX_W1 = 0xBB67AE8584CAA73B
PHILOX_ROUNDS = 10

_LOW32 = np.uint64(0xFFFFFFFF)
_SHIFT32 = np.uint64(32)


def _mulhilo(a: "np.ndarray", m: int) -> Tuple["np.ndarray", "np.ndarray"]:
    """The high and low words of ``a * m`` for 64-bit ``a`` and ``m``, from 32-bit halves."""
    m_lo = np.uint64(m & 0xFFFFFFFF)
    m_hi = np.uint64(m >> 32)
    a_lo = a & _LOW32
    a_hi = a >> _SHIFT32
    low_low = a_lo * m_lo
    high_low = a_hi * m_lo
    low_high = a_lo * m_hi
    cross = (low_low >> _SHIFT32) + (high_low & _LOW32) + (low_high & _LOW32)
    high = a_hi * m_hi + (high_low >> _SHIFT32) + (low_high >> _SHIFT32) + (cross >> _SHIFT32)
    return high, a * np.uint64(m)


def philox4x64_10(counters: "np.ndarray", key: Key) -> "np.ndarray":
    """Philox4x64-10 of every row of *counters* (shape ``(n, 4)``, uint64) under *key*.

    Random123's round and key schedule: two 64-bit multiplications per round, a Weyl step
    of the key between rounds, ten rounds. Word 0 of a counter or key is the one
    Random123 and numpy call ``v[0]``.
    """
    c0, c1, c2, c3 = (np.ascontiguousarray(counters[:, i], dtype=np.uint64) for i in range(4))
    k0, k1 = key
    for round_index in range(PHILOX_ROUNDS):
        if round_index:
            k0 = (k0 + PHILOX_W0) & WORD_MASK
            k1 = (k1 + PHILOX_W1) & WORD_MASK
        high0, low0 = _mulhilo(c0, PHILOX_M0)
        high1, low1 = _mulhilo(c2, PHILOX_M1)
        c0, c1, c2, c3 = high1 ^ c1 ^ np.uint64(k0), low1, high0 ^ c3 ^ np.uint64(k1), low0
    return np.stack((c0, c1, c2, c3), axis=1)


def philox_block(counter: Sequence[int], key: Key) -> Block:
    """One block in Python integers: the same function as :func:`philox4x64_10`, written
    a second time so the two can be held to each other."""
    c0, c1, c2, c3 = (int(word) for word in counter)
    k0, k1 = key
    if not all(0 <= word <= WORD_MASK for word in (c0, c1, c2, c3, k0, k1)):
        raise ValueError("a counter or key word does not fit in 64 bits")
    for round_index in range(PHILOX_ROUNDS):
        if round_index:
            k0 = (k0 + PHILOX_W0) & WORD_MASK
            k1 = (k1 + PHILOX_W1) & WORD_MASK
        product0 = PHILOX_M0 * c0
        product1 = PHILOX_M1 * c2
        c0, c1, c2, c3 = (
            (product1 >> 64) ^ c1 ^ k0,
            product1 & WORD_MASK,
            (product0 >> 64) ^ c3 ^ k1,
            product0 & WORD_MASK,
        )
    return c0, c1, c2, c3


def numpy_philox(key: Key, first_counter: int) -> Philox:
    """numpy's ``Philox`` positioned so that its first block is the block of *first_counter*.

    numpy steps the counter before it makes a block (``philox_next`` in ``philox.h``), so
    the object starts one below the counter whose block comes first. Its counter carries
    from word 0 into word 1, so consecutive blocks are consecutive rows.
    """
    return Philox(key=np.array(key, dtype=np.uint64), counter=(first_counter - 1) % COUNTER_SPACE)


def _key_of(digest: bytes) -> Key:
    return int.from_bytes(digest[0:8], "big"), int.from_bytes(digest[8:16], "big")


# ---------------------------------------------------------------------------
# The root, a path's key and counter (KERNEL-SPEC-2 section 2)
# ---------------------------------------------------------------------------


def root(seam: bytes, seed: int) -> bytes:
    """The run root: ``sha256("flux-seed/2" NUL H NUL seed)``, the seed 8 signed big-endian bytes."""
    if not seam:
        raise ValueError("H_bytes must not be empty")
    material = (
        SEED_TREE_VERSION.encode("utf-8") + NUL + seam + NUL + seed.to_bytes(8, "big", signed=True)
    )
    return hashlib.sha256(material).digest()


def _matches_names(path: Sequence[Any], shape: PathShape) -> bool:
    """Whether *path* has *shape*, a shape of literals and names only (no ordinal part)."""
    if len(path) != len(shape.pattern):
        return False
    for expected, part in zip(shape.pattern, path, strict=True):
        if not isinstance(part, str) or not part:
            return False
        if isinstance(expected, str) and part != expected:
            return False
    return True


def _shape(path: Sequence[Any]) -> PathShape:
    if matches_generator_shape(path):
        return GENERATOR_PATH_SHAPE
    for extra in (COUNT_PATH_SHAPE, ALLOCATE_PATH_SHAPE, ASSOCIATE_PATH_SHAPE):
        if _matches_names(path, extra):
            return extra
    shape = match_shape(path)
    if shape is None:
        raise ValueError(f"{tuple(path)!r} is not a canonical path")
    return shape


def split_path(path: Sequence[Any]) -> Tuple[bytes, Tuple[int, int]]:
    """The key label of *path* and its ordinals.

    The key label is kernel v1's label with every ordinal part made empty. A name part is
    never empty, so no two paths of different shapes share a key label. The ordinals, at
    most two, are padded with zeros.
    """
    shape = _shape(path)
    parts = path_label(path).split(SEPARATOR)
    ordinals: List[int] = []
    for position, kind in enumerate(shape.pattern):
        if kind is PathKind.ORDINAL:
            ordinal = int(path[position])
            if not 0 <= ordinal <= WORD_MASK:
                raise ValueError(f"ordinal {ordinal} does not fit in one counter word")
            ordinals.append(ordinal)
            parts[position] = b""
    if len(ordinals) > 2:
        raise ValueError("a path holds at most two ordinals")
    padded = tuple(ordinals) + (0,) * (2 - len(ordinals))
    return SEPARATOR.join(parts), (padded[0], padded[1])


def stream_key(run_root: bytes, path: Sequence[Any]) -> Key:
    """The 128-bit Philox key of *path*: 16 bytes of ``sha256("flux-seed/2" NUL root NUL label)``."""
    label, _ordinals = split_path(path)
    return _key_of(
        hashlib.sha256(SEED_TREE_VERSION.encode("utf-8") + NUL + run_root + NUL + label).digest()
    )


def counter(path: Sequence[Any], draw: int, attempt: int = 0) -> Block:
    """The counter of one draw: the path's two ordinals, the draw's index and the attempt."""
    if not 0 <= draw <= WORD_MASK or not 0 <= attempt <= WORD_MASK:
        raise ValueError("a draw index and an attempt each fit in one counter word")
    _label, (first, second) = split_path(path)
    return first, second, draw, attempt


def block(run_root: bytes, path: Sequence[Any], draw: int, attempt: int = 0) -> Block:
    return philox_block(counter(path, draw, attempt), stream_key(run_root, path))


# ---------------------------------------------------------------------------
# Integer draws (KERNEL-SPEC-2 section 4)
# ---------------------------------------------------------------------------

BOUNDED_ATTEMPTS = 64


class OneWord:
    """A stream holding one raw word, so kernel v1's samplers read back exactly *u53*."""

    __slots__ = ("word",)

    def __init__(self, u53: int) -> None:
        self.word = u53 << 11

    def next_raw(self) -> int:
        return self.word


def uniform53(word: int) -> int:
    """Kernel v1's rule on a draw's word 0: its top 53 bits."""
    return word >> 11


def bernoulli(u53: int, p: Decimal) -> bool:
    return v1.bernoulli(OneWord(u53), p)  # type: ignore[arg-type]


def categorical(u53: int, levels: Mapping[str, Decimal]) -> str:
    return v1.categorical(OneWord(u53), levels)  # type: ignore[arg-type]


def bounded(word_of_attempt: "Any", n: int) -> int:
    """``[0, n)`` by Lemire's test, attempt after attempt, at most 64 of them.

    *word_of_attempt(a)* is word 0 of the draw's block at attempt *a*. The first word whose
    low product clears the threshold decides; if all 64 are rejected, the last one is
    taken modulo *n*, kernel v1's documented fallback.
    """
    if not 1 <= n <= 1 << 64:
        raise ValueError(f"a bounded draw needs 1 <= n <= 2**64, got {n}")
    threshold = (1 << 64) % n
    word = 0
    for attempt in range(BOUNDED_ATTEMPTS):
        word = word_of_attempt(attempt)
        product = word * n
        if product & WORD_MASK >= threshold:
            return product >> 64
    return word % n


def bounded_draw(run_root: bytes, path: Sequence[Any], draw: int, n: int) -> int:
    return bounded(lambda attempt: block(run_root, path, draw, attempt)[0], n)


def permutation(run_root: bytes, path: Sequence[Any], n: int, first_draw: int = 0) -> List[int]:
    """Kernel v1's Fisher-Yates, its k-th swap drawn at index ``first_draw + k``."""
    items = list(range(n))
    for k, i in enumerate(range(n - 1, 0, -1)):
        j = bounded_draw(run_root, path, first_draw + k, i + 1)
        items[i], items[j] = items[j], items[i]
    return items


@lru_cache(maxsize=64)
def poisson_thresholds(mean: Decimal) -> Tuple[int, ...]:
    """``t_k = ceil(c_k * 2**53)`` for the cumulative sums ``c_0 .. c_{cap-1}``.

    The sums are kernel v1's, by its recurrence, every operation in ``decimal`` at 50
    digits rounding half-even; ``cap`` is v1's ``poisson_cap``. A sum holds at most 50
    digits and ``2**53`` sixteen, so the product is exact at 70 digits and its ceiling is
    the threshold exactly.
    """
    if mean < 0:
        raise ValueError(f"poisson mean must be >= 0, got {mean} (FLX-5021)")
    if mean == 0:
        return ()
    cap = v1.poisson_cap(mean)
    sums: List[Decimal] = []
    with localcontext(v1._pinned(v1._WORKING_PRECISION)):
        term = (-mean).exp()
        cumulative = term
        for k in range(cap):
            if k:
                term = term * mean / k
                cumulative += term
            sums.append(cumulative)
    with localcontext(v1._pinned(v1._WORKING_PRECISION + 20)):
        return tuple(
            int((value * v1.UNIFORM53_DENOMINATOR).to_integral_value(rounding=ROUND_CEILING))
            for value in sums
        )


def poisson(u53: int, mean: Decimal) -> int:
    """Kernel v2's Poisson draw: the smallest ``k`` with ``u53 < t_k``, or the cap."""
    return bisect.bisect_right(poisson_thresholds(mean), u53)


def poisson_by_thresholds(u53: "np.ndarray", mean: Decimal) -> "np.ndarray":
    """:func:`poisson` over an array of uniforms, by one sorted search."""
    table = np.array(poisson_thresholds(mean), dtype=np.uint64)
    return np.searchsorted(table, u53, side="right").astype(np.int64)


def poisson_v1(u53: int, mean: Decimal) -> int:
    """Kernel v1's capped search, which rounds the uniform to 50 digits before it compares."""
    return v1.poisson(OneWord(u53), mean=mean)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Identifiers and permutations (KERNEL-SPEC-2 sections 6 and 7)
# ---------------------------------------------------------------------------


def identifier(
    seam: bytes, cell_path: Sequence[Any], length: int = 32, as_uuid: bool = False
) -> str:
    """A generated identifier: Philox words keyed by ``H`` and the column's path, never the seed."""
    if not matches_generator_shape(cell_path):
        raise ValueError("an identifier belongs to a generator cell")
    if not 1 <= length <= 64 or (as_uuid and length < 32):
        raise ValueError("identifier length must lie in [1, 64], and at least 32 for a UUID")
    label, (row, _unused) = split_path(cell_path)
    key = _key_of(hashlib.sha256(IDENTIFIER_LABEL + NUL + seam + NUL + label).digest())
    digits = "".join(f"{word:016x}" for word in philox_block((row, 0, 0, 0), key))
    if not as_uuid:
        return digits[:length]
    body = digits[:32]
    versioned = body[:12] + "8" + body[13:16] + _variant_nibble(body[16]) + body[17:]
    return (
        f"{versioned[:8]}-{versioned[8:12]}-{versioned[12:16]}-{versioned[16:20]}-{versioned[20:]}"
    )


def seed_tree_identifier(run_root: bytes, path: Sequence[Any], length: int = 32) -> str:
    """Subject and event ids: kernel v1's rule under ``flux-seed/2`` and the 0.2.0 root.

    ``sha256("flux-seed/2" NUL "id" NUL root NUL label(path))`` in lowercase hex, its first
    *length* digits. The path need not be canonical (a subject's id path is not).
    """
    if not 1 <= length <= 64:
        raise ValueError(f"identifier length must lie in [1, 64], got {length}")
    material = SEED_TREE_VERSION.encode("utf-8") + NUL + b"id" + NUL + run_root + NUL
    return hashlib.sha256(material + path_label(path)).hexdigest()[:length]


def plugin_seed(run_root: bytes, sim_id: str, expose_id: str) -> bytes:
    """A generator plugin's seed: KERNEL-SPEC-2 section 6.3.

    ``sha256("flux-seed/2" NUL "plugin" NUL root NUL label(("sim", sim_id, "gen", exposeId)))``,
    the 32 digest bytes whole.
    """
    material = SEED_TREE_VERSION.encode("utf-8") + NUL + b"plugin" + NUL + run_root + NUL
    return hashlib.sha256(material + path_label(("sim", sim_id, "gen", expose_id))).digest()


def relation_key(seam: bytes, parts: Sequence[Any]) -> bytes:
    """The 32-byte key of a key layout or a relationship: section 8.2's ``k``, labelled ``flux-key/2``."""
    return hashlib.sha256(KEY_LABEL + NUL + seam + NUL + path_label(parts)).digest()


PERMUTE_ROUNDS = 4


def permute(key: bytes, domain: int, index: int) -> int:
    """Kernel v1's cycle-walking Feistel permutation with a Philox block as its round function.

    The round key is 16 bytes of ``sha256("flux-permute/2" NUL key NUL domain)``, the domain
    as 33 big-endian bytes. Round ``i`` reads the half ``y`` as the counter
    ``(y mod 2**64, y >> 64, i, 0)`` and adds ``w0 + w1 * 2**64`` of the block.
    """
    if len(key) != 32 or not 1 <= domain <= (1 << 256) or not 0 <= index < domain:
        raise ValueError("permute needs a 32-byte key, a domain in [1, 2**256] and an index in it")
    if domain == 1:
        return 0
    bits = (domain - 1).bit_length()
    left_bits = bits // 2
    right_bits = bits - left_bits
    left_mask, right_mask = (1 << left_bits) - 1, (1 << right_bits) - 1
    round_key = _key_of(
        hashlib.sha256(PERMUTE_LABEL + NUL + key + NUL + domain.to_bytes(33, "big")).digest()
    )

    def round_value(round_index: int, half: int) -> int:
        words = philox_block((half & WORD_MASK, half >> 64, round_index, 0), round_key)
        return words[0] | (words[1] << 64)

    value = index
    while True:
        left, right = value >> right_bits, value & right_mask
        for round_index in range(PERMUTE_ROUNDS):
            if round_index % 2 == 0:
                left = (left + round_value(round_index, right)) & left_mask
            else:
                right = (right + round_value(round_index, left)) & right_mask
        value = (left << right_bits) | right
        if value < domain:
            return value


# ---------------------------------------------------------------------------
# The counted layout (KERNEL-SPEC-2 section 8.1)
# ---------------------------------------------------------------------------


def count_key(run_root: bytes, path: Sequence[Any]) -> bytes:
    """A counted layout's ``permute/2`` key: the whole digest whose first 16 bytes are the path's key."""
    label, _ordinals = split_path(path)
    return hashlib.sha256(SEED_TREE_VERSION.encode("utf-8") + NUL + run_root + NUL + label).digest()


def _lrm(total: int, weights: Sequence[Fraction]) -> List[int]:
    """The Largest Remainder Method (KERNEL-SPEC section 5) over classes keyed by position.

    Keys are the positions in decimal, zero-padded to one width, so their NFC order is the
    class order and a tie goes to the lower class.
    """
    if total < 1 or sum(weights) != 1:
        raise ValueError("the class weights must sum to 1 over at least one row")
    scaled = [total * weight for weight in weights]
    counts = [math.floor(value) for value in scaled]
    order = sorted(range(len(weights)), key=lambda k: (-(scaled[k] - counts[k]), k))
    for k in order[: total - sum(counts)]:
        counts[k] += 1
    return counts


def _whole(text: str) -> int:
    value = Fraction(text)
    if value.denominator != 1 or value < 0:
        raise ValueError(f"{text!r} is not a whole number of at least 0")
    return int(value)


def _classes(shape: Mapping[str, Any]) -> Tuple[List[Tuple[int, int]], List[Fraction]]:
    """The classes of a ``children`` shape as inclusive ``(lo, hi)`` counts, and their weights."""
    if "levels" in shape:
        levels = [_whole(text) for text in shape["levels"]]
        if any(a >= b for a, b in zip(levels, levels[1:], strict=False)):
            raise ValueError("levels must be strictly ascending")
        ranges = [(level, level) for level in levels]
        weights = shape["levelWeights"]
    else:
        edges = [_whole(text) for text in shape["bins"]]
        if len(edges) < 2 or any(a >= b for a, b in zip(edges, edges[1:], strict=False)):
            raise ValueError("bins must be strictly ascending, with at least two edges")
        ranges = [(edges[k], edges[k + 1] - 1) for k in range(len(edges) - 2)]
        ranges.append((edges[-2], edges[-1]))
        weights = shape["binWeights"]
    if len(weights) != len(ranges):
        raise ValueError("a shape needs one weight per class")
    return ranges, [Fraction(text) for text in weights]


def slot_values(parents: int, shape: Mapping[str, Any]) -> List[int]:
    """The *parents* slot values of a shape, class by class, non-decreasing in slot order."""
    ranges, weights = _classes(shape)
    values: List[int] = []
    for (lo, hi), in_class in zip(ranges, _lrm(parents, weights), strict=True):
        values += [lo + (j * (hi - lo + 1)) // in_class for j in range(in_class)]
    return values


def _expected_counts(
    sigma: Sequence[int],
    slots: Optional[Sequence[int]],
    hook: Optional[Sequence[Fraction]],
) -> List[Fraction]:
    """Each rank's expected count ``x``: the four cases of section 8.1."""
    ranks = len(sigma)
    if slots is not None and hook is None:
        return [Fraction(slots[sigma[r]]) for r in range(ranks)]
    if slots is None and hook is not None:
        return list(hook)
    if slots is None:
        return [Fraction(1)] * ranks
    assert hook is not None
    x = [Fraction(0)] * ranks
    for position, r in enumerate(sorted(range(ranks), key=lambda r: (hook[r], sigma[r]))):
        x[r] = Fraction(slots[position])
    return x


def _spread(spare: int, x: Sequence[Fraction], minimum: int) -> List[Fraction]:
    """Each rank's share ``y`` of the spare children: ``max(x - minimum, 0)``, or ``1`` for all when none."""
    y = [max(value - minimum, Fraction(0)) for value in x]
    if sum(y) == 0 and spare > 0:
        y = [Fraction(1)] * len(y)
    return y


def _cap(counts: Sequence[int], y: Sequence[Fraction]) -> List[int]:
    """A ``one_to_one`` layout: no rank has more than one child (section 8.1).

    Every rank keeps at most one child, and the children above one go one each to the ranks that
    have none, the largest ``y`` first and a tie to the lower rank.
    """
    spare = sum(max(count - 1, 0) for count in counts)
    capped = [min(count, 1) for count in counts]
    empty = sorted((r for r, count in enumerate(capped) if count == 0), key=lambda r: (-y[r], r))
    if spare > len(empty):
        raise ValueError("a one_to_one relationship has more child rows than eligible parents")
    for r in empty[:spare]:
        capped[r] = 1
    return capped


def _allot(spare: int, x: Sequence[Fraction], minimum: int) -> List[int]:
    """Section 8.1's allotment of *spare* children over the ranks, on exact rationals."""
    y = _spread(spare, x, minimum)
    total = sum(y)
    if spare == 0:
        return [0] * len(y)
    share = [spare * value / total for value in y]
    allotted = [math.floor(value) for value in share]
    order = sorted(range(len(y)), key=lambda r: (-(share[r] - allotted[r]), r))
    for r in order[: spare - sum(allotted)]:
        allotted[r] += 1
    return allotted


def counted_layout(
    run_root: bytes,
    path: Sequence[Any],
    *,
    parents: int,
    eligible: Optional[Sequence[int]],
    child_rows: int,
    minimum: int,
    shape: Optional[Mapping[str, Any]],
    expected: Optional[Sequence[Fraction]],
    one_to_one: bool = False,
) -> Dict[str, Any]:
    """The counted layout of KERNEL-SPEC-2 section 8.1: counts and offsets per parent row.

    *eligible* holds the parent rows whose ``parentWhen`` is TRUE, ascending, or is ``None``
    for every row. *expected* holds the hook's exact value per eligible parent, in rank
    order, or is ``None`` without a hook. Every refusal (``FLX-3403``) is a ``ValueError``.
    """
    rows = list(range(parents)) if eligible is None else list(eligible)
    if any(a >= b for a, b in zip(rows, rows[1:], strict=False)) or any(
        not 0 <= row < parents for row in rows
    ):
        raise ValueError("eligible rows must be ascending parent rows")
    e = len(rows)
    if expected is not None and len(expected) != e:
        raise ValueError("a hook gives one expected count per eligible parent")
    if expected is not None and any(Fraction(value) < 0 for value in expected):
        raise ValueError("an expected count must not be negative")
    spare = child_rows - minimum * e
    if spare < 0:
        raise ValueError("the child rows cannot give every eligible parent its minimum")
    if e == 0 and child_rows > 0:
        raise ValueError("no parent is eligible for the child rows")
    key = count_key(run_root, path)
    sigma = [permute(key, e, r) for r in range(e)]
    slots = slot_values(e, shape) if shape is not None and e > 0 else None
    if shape is not None and e == 0:
        _classes(shape)
    x = _expected_counts(
        sigma, slots, None if expected is None else [Fraction(v) for v in expected]
    )
    per_rank = [minimum + a for a in _allot(spare, x, minimum)] if e else []
    if one_to_one and minimum > 1:
        raise ValueError("a one_to_one relationship gives no parent more than one child")
    if one_to_one:
        per_rank = _cap(per_rank, _spread(spare, x, minimum))
    counts = [0] * parents
    for row, count in zip(rows, per_rank, strict=True):
        counts[row] = count
    offsets = [sum(counts[:p]) for p in range(parents)]
    return {
        "key": key,
        "e": e,
        "sigma": sigma,
        "slots": slots,
        "x": x,
        "counts": counts,
        "offsets": offsets,
    }


def parent_of_child(offsets: Sequence[int], child_row: int) -> int:
    """The parent row whose run holds *child_row*: bisection over the offsets."""
    return bisect.bisect_right(offsets, child_row) - 1


# ---------------------------------------------------------------------------
# Exact allocation (KERNEL-SPEC-2 section 8.3)
# ---------------------------------------------------------------------------


def allocate_key(run_root: bytes, path: Sequence[Any]) -> bytes:
    """An allocation's ``permute/2`` key: the whole digest whose first 16 bytes are the path's key."""
    label, _ordinals = split_path(path)
    return hashlib.sha256(SEED_TREE_VERSION.encode("utf-8") + NUL + run_root + NUL + label).digest()


def class_weights(declared: Sequence[str], null_fraction: Optional[str] = None) -> List[Fraction]:
    """Section 8.3's class weights: the declared ones normalised to 1, NULL last when declared.

    With a ``nullFraction`` above 0 the declared classes' weights are scaled by
    ``1 - nullFraction`` and NULL takes ``nullFraction``, all on exact rationals.
    """
    given = [Fraction(text) for text in declared]
    if not given or any(weight < 0 for weight in given) or sum(given) == 0:
        raise ValueError("the declared weights need some weight and none negative")
    shares = [weight / sum(given) for weight in given]
    if null_fraction is None or Fraction(null_fraction) == 0:
        return shares
    null = Fraction(null_fraction)
    if not 0 < null <= 1:
        raise ValueError("a nullFraction above 0 lies in (0, 1]")
    return [share * (1 - null) for share in shares] + [null]


def _class_starts(counts: Sequence[int]) -> List[int]:
    """The first slot of each class when the slots are laid out class by class."""
    starts, total = [], 0
    for count in counts:
        starts.append(total)
        total += count
    return starts


def _class_of_slot(starts: Sequence[int], slot: int) -> int:
    """The class whose slots hold *slot*: the last class that starts at or before it.

    An empty class shares its start with the next one, so the last such class is never empty.
    """
    return bisect.bisect_right(starts, slot) - 1


def exact_classes(
    run_root: bytes, path: Sequence[Any], *, rows: int, weights: Sequence[Fraction]
) -> Dict[str, Any]:
    """The class of each of *rows* rows under exact allocation, and the class counts.

    *weights* are the class weights of :func:`class_weights`, NULL last. The counts are
    ``LRM(rows, weights)`` and row ``i`` takes the class of slot ``permute/2(K, rows, i)``.
    """
    counts = _lrm(rows, weights)
    starts = _class_starts(counts)
    key = allocate_key(run_root, path)
    slots = [permute(key, rows, i) for i in range(rows)]
    return {
        "key": key,
        "counts": counts,
        "starts": starts,
        "slots": slots,
        "classes": [_class_of_slot(starts, slot) for slot in slots],
    }


def exact_given(
    run_root: bytes,
    parent_path: Sequence[Any],
    child_path: Sequence[Any],
    *,
    rows: int,
    parent_weights: Sequence[Fraction],
    child_weights: Sequence[Sequence[Fraction]],
) -> Dict[str, Any]:
    """A column allocated exactly given a parent column that is exact too.

    *child_weights* holds, for each parent class, the child class weights given it. Row ``i``'s
    parent class ``l`` comes from its slot ``s``; its index in the group is ``j = s - start_l``
    and the child's class is that of slot ``permute/2(K_c, n_l, j)`` of ``LRM(n_l, weights)``.
    """
    parent = exact_classes(run_root, parent_path, rows=rows, weights=parent_weights)
    key = allocate_key(run_root, child_path)
    group_counts = [
        _lrm(size, child_weights[level]) if size else [0] * len(child_weights[level])
        for level, size in enumerate(parent["counts"])
    ]
    classes = []
    for slot, level in zip(parent["slots"], parent["classes"], strict=True):
        index = slot - parent["starts"][level]
        size = parent["counts"][level]
        counts = group_counts[level]
        within = permute(key, size, index)
        classes.append(_class_of_slot(_class_starts(counts), within))
    return {
        "parent": parent,
        "key": key,
        "groupCounts": group_counts,
        "classes": classes,
    }


# ---------------------------------------------------------------------------
# binary64 transforms (KERNEL-SPEC-2 section 5)
# ---------------------------------------------------------------------------

TWO_POW_53 = 1 << 53
TWO_POW_MINUS_53 = 1.0 / TWO_POW_53
_PRECISION = 50
SMALLEST_NORMAL = Decimal(2) ** -1022
SMALLEST_MASS = Decimal(2) ** -969
LARGEST_UNITS = 1 << 52
#: Magnitudes a declaration's binary64 inputs may take (zero aside, for the mean): with these
#: and the bounds above, every intermediate of section 5 is zero or a normal number.
SMALLEST_INPUT = math.ldexp(1.0, -900)
LARGEST_INPUT = math.ldexp(1.0, 900)
#: 10**22 is the largest power of ten binary64 holds exactly.
LARGEST_SCALE = 22


def _refuse(what: str) -> ValueError:
    return ValueError(f"{what}; kernel v2 refuses it (FLX-5021)")


def _input_ok(value: float, *, zero_allowed: bool = False) -> bool:
    if value == 0.0:
        return zero_allowed
    return SMALLEST_INPUT <= abs(value) <= LARGEST_INPUT


def bits(value: float) -> str:
    """A binary64 value as 16 hex digits of its IEEE 754 bit pattern."""
    return struct.pack(">d", value).hex()


def from_bits(pattern: str) -> float:
    value: float = struct.unpack(">d", bytes.fromhex(pattern))[0]
    return value


# fdlibm e_log.c: ln 2 split in two, and the coefficients of its polynomial in s.
LOG_CONSTANTS: Dict[str, str] = {
    "ln2_hi": "3fe62e42fee00000",
    "ln2_lo": "3dea39ef35793c76",
    "Lg1": "3fe5555555555593",
    "Lg2": "3fd999999997fa04",
    "Lg3": "3fd2492494229359",
    "Lg4": "3fcc71c51d8e78af",
    "Lg5": "3fc7466496cb03de",
    "Lg6": "3fc39a09d078c69f",
    "Lg7": "3fc2f112df3e5244",
}
_LN2_HI, _LN2_LO = from_bits(LOG_CONSTANTS["ln2_hi"]), from_bits(LOG_CONSTANTS["ln2_lo"])
_LG1, _LG2, _LG3, _LG4, _LG5, _LG6, _LG7 = (from_bits(LOG_CONSTANTS[f"Lg{i}"]) for i in range(1, 8))
SQRT_HALF = math.sqrt(0.5)

# AS241's PPND16 coefficients, highest power first, as CPython's statistics module holds them.
CENTRAL_NUM = (
    2.50908_09287_30122_6727e3, 3.34305_75583_58812_8105e4, 6.72657_70927_00870_0853e4,
    4.59219_53931_54987_1457e4, 1.37316_93765_50946_1125e4, 1.97159_09503_06551_4427e3,
    1.33141_66789_17843_7745e2, 3.38713_28727_96366_6080e0,
)  # fmt: skip
CENTRAL_DEN = (
    5.22649_52788_52854_5610e3, 2.87290_85735_72194_2674e4, 3.93078_95800_09271_0610e4,
    2.12137_94301_58659_5867e4, 5.39419_60214_24751_1077e3, 6.87187_00749_20579_0830e2,
    4.23133_30701_60091_1252e1, 1.0,
)  # fmt: skip
NEAR_NUM = (
    7.74545_01427_83414_07640e-4, 2.27238_44989_26918_45833e-2, 2.41780_72517_74506_11770e-1,
    1.27045_82524_52368_38258e0, 3.64784_83247_63204_60504e0, 5.76949_72214_60691_40550e0,
    4.63033_78461_56545_29590e0, 1.42343_71107_49683_57734e0,
)  # fmt: skip
NEAR_DEN = (
    1.05075_00716_44416_84324e-9, 5.47593_80849_95344_94600e-4, 1.51986_66563_61645_71966e-2,
    1.48103_97642_74800_74590e-1, 6.89767_33498_51000_04550e-1, 1.67638_48301_83803_84940e0,
    2.05319_16266_37758_82187e0, 1.0,
)  # fmt: skip
FAR_NUM = (
    2.01033_43992_92288_13265e-7, 2.71155_55687_43487_57815e-5, 1.24266_09473_88078_43860e-3,
    2.65321_89526_57612_30930e-2, 2.96560_57182_85048_91230e-1, 1.78482_65399_17291_33580e0,
    5.46378_49111_64114_36990e0, 6.65790_46435_01103_77720e0,
)  # fmt: skip
FAR_DEN = (
    2.04426_31033_89939_78564e-15, 1.42151_17583_16445_88870e-7, 1.84631_83175_10054_68180e-5,
    7.86869_13114_56132_59100e-4, 1.48753_61290_85061_48525e-2, 1.36929_88092_27358_05310e-1,
    5.99832_20655_58879_37690e-1, 1.0,
)  # fmt: skip
AS241_TABLES = {
    "centralNumerator": CENTRAL_NUM,
    "centralDenominator": CENTRAL_DEN,
    "nearNumerator": NEAR_NUM,
    "nearDenominator": NEAR_DEN,
    "farNumerator": FAR_NUM,
    "farDenominator": FAR_DEN,
}


def _horner(coefficients: Sequence[float], x: "np.ndarray") -> "np.ndarray":
    total = np.full_like(x, coefficients[0])
    for coefficient in coefficients[1:]:
        total = total * x + coefficient
    return total


def log_basic(x: "np.ndarray") -> "np.ndarray":
    """``ln x`` for positive normal *x*: fdlibm's reduction to ``[√2/2, √2)`` and polynomial."""
    mantissa, exponent = np.frexp(x)
    low = mantissa < SQRT_HALF
    m = np.where(low, mantissa * 2.0, mantissa)
    k = (exponent - low).astype(np.float64)
    f = m - 1.0
    s = f / (2.0 + f)
    z = s * s
    w = z * z
    t1 = w * (_LG2 + w * (_LG4 + w * _LG6))
    t2 = z * (_LG1 + w * (_LG3 + w * (_LG5 + w * _LG7)))
    r = t2 + t1
    hfsq = 0.5 * f * f
    return k * _LN2_HI - ((hfsq - (s * (hfsq + r) + k * _LN2_LO)) - f)


def ppnd16(p: "np.ndarray") -> "np.ndarray":
    """Φ⁻¹(p) for ``0 < p < 1``: AS241's PPND16 in the order of CPython's pure-Python copy.

    CPython's C copy of the same formula differs in the last bits: its compiler fuses
    each multiply and add of the Horner steps into one instruction.
    """
    q = p - 0.5
    r = 0.180625 - q * q
    central = _horner(CENTRAL_NUM, r) * q / _horner(CENTRAL_DEN, r)
    t = np.sqrt(-log_basic(np.where(q <= 0.0, p, 1.0 - p)))
    near = t - 1.6
    far = t - 5.0
    tail = np.where(
        t <= 5.0,
        _horner(NEAR_NUM, near) / _horner(NEAR_DEN, near),
        _horner(FAR_NUM, far) / _horner(FAR_DEN, far),
    )
    tail = np.where(q < 0.0, -tail, tail)
    return np.where(np.abs(q) <= 0.425, central, tail)


def _horner_scalar(coefficients: Sequence[float], x: float) -> float:
    total = coefficients[0]
    for coefficient in coefficients[1:]:
        total = total * x + coefficient
    return total


def log_basic_scalar(x: float) -> float:
    mantissa, exponent = math.frexp(x)
    if mantissa < SQRT_HALF:
        mantissa *= 2.0
        exponent -= 1
    k = float(exponent)
    f = mantissa - 1.0
    s = f / (2.0 + f)
    z = s * s
    w = z * z
    t1 = w * (_LG2 + w * (_LG4 + w * _LG6))
    t2 = z * (_LG1 + w * (_LG3 + w * (_LG5 + w * _LG7)))
    r = t2 + t1
    hfsq = 0.5 * f * f
    return k * _LN2_HI - ((hfsq - (s * (hfsq + r) + k * _LN2_LO)) - f)


def ppnd16_scalar(p: float) -> float:
    q = p - 0.5
    if abs(q) <= 0.425:
        r = 0.180625 - q * q
        return _horner_scalar(CENTRAL_NUM, r) * q / _horner_scalar(CENTRAL_DEN, r)
    t = math.sqrt(-log_basic_scalar(p if q <= 0.0 else 1.0 - p))
    if t <= 5.0:
        t -= 1.6
        x = _horner_scalar(NEAR_NUM, t) / _horner_scalar(NEAR_DEN, t)
    else:
        t -= 5.0
        x = _horner_scalar(FAR_NUM, t) / _horner_scalar(FAR_DEN, t)
    return -x if q < 0.0 else x


def environment_keeps_subnormals() -> bool:
    """Whether this process computes subnormal results, checked with arithmetic.

    numpy takes its subnormal constants from compiled tables, so a flush-to-zero mode
    that a library switched on while running would not show there.
    """
    smallest_normal = math.ldexp(1.0, -1022)
    half = np.array([smallest_normal]) / 2.0
    return bool(half[0] != 0.0) and smallest_normal / 2.0 != 0.0


@dataclass(frozen=True)
class Interval:
    name: str
    mean: Decimal
    stddev: Decimal
    lo: Decimal
    hi: Decimal
    scale: int


@dataclass(frozen=True)
class Plan:
    """What a bounded-normal declaration fixes before any draw (mirrored when it lies above
    the mean, as v1 mirrors it). The decimal values are v1's; the ``_f`` fields are the same
    values rounded once to binary64, so no draw converts anything."""

    interval: Interval
    mirror: bool
    a: Decimal
    b: Decimal
    cdf_a: Decimal
    sf_b: Decimal
    mass: Decimal
    lo_units: int
    hi_units: int
    a_f: float
    b_f: float
    cdf_a_f: float
    sf_b_f: float
    mass_f: float
    mean_f: float
    stddev_f: float
    unit_f: float

    @property
    def degenerate(self) -> bool:
        """v1 returns the lower bound when the interval's mass is not positive."""
        return self.mass <= 0


def plan_of(interval: Interval) -> Plan:
    if interval.stddev <= 0:
        raise ValueError("the binary64 transform covers a positive stddev; v1's rule takes zero")
    if not 0 <= interval.scale <= LARGEST_SCALE:
        raise _refuse(f"{interval.name}: scale {interval.scale} lies outside [0, 22]")
    try:
        return _plan_of(interval)
    except InvalidOperation:
        raise _refuse(f"{interval.name}: a bound cannot be held at its scale") from None


def _plan_of(interval: Interval) -> Plan:
    with localcontext(v1._pinned(_PRECISION)):
        a = (interval.lo - interval.mean) / interval.stddev
        b = (interval.hi - interval.mean) / interval.stddev
        mirror = a > 0
        if mirror:
            a, b = -b, -a
        cdf_a = v1.normal_cdf(a, precision=_PRECISION)
        sf_b = v1.normal_cdf(-b, precision=_PRECISION)
        mass = v1.normal_cdf(b, precision=_PRECISION) - cdf_a
        if 0 < mass < SMALLEST_MASS:
            raise _refuse(
                f"{interval.name}: the interval holds {mass:.3e} of its distribution, too "
                "little for binary64"
            )
        cdf_a = cdf_a if cdf_a >= SMALLEST_NORMAL else Decimal(0)
        sf_b = sf_b if sf_b >= SMALLEST_NORMAL else Decimal(0)
        step = Decimal(1).scaleb(-interval.scale)
        lo_units = int(interval.lo.quantize(step, rounding=ROUND_CEILING).scaleb(interval.scale))
        hi_units = int(interval.hi.quantize(step, rounding=ROUND_FLOOR).scaleb(interval.scale))
    if lo_units > hi_units:
        raise _refuse(f"{interval.name}: no value at scale {interval.scale} lies inside it")
    if max(abs(lo_units), abs(hi_units)) > LARGEST_UNITS:
        raise _refuse(f"{interval.name}: its units at scale {interval.scale} exceed 2**52")
    if not _input_ok(float(interval.stddev)):
        raise _refuse(f"{interval.name}: a stddev outside [2**-900, 2**900]")
    if not _input_ok(float(interval.mean), zero_allowed=True):
        raise _refuse(
            f"{interval.name}: a mean that is not zero and lies outside [2**-900, 2**900]"
        )
    return Plan(
        interval,
        mirror,
        a,
        b,
        cdf_a,
        sf_b,
        mass,
        lo_units,
        hi_units,
        a_f=float(a),
        b_f=float(b),
        cdf_a_f=float(cdf_a),
        sf_b_f=float(sf_b),
        mass_f=float(mass),
        mean_f=float(interval.mean),
        stddev_f=float(interval.stddev),
        unit_f=float(10**interval.scale),
    )


def _ends(plan: Plan) -> Tuple[int, int]:
    """The units of the plan's lower and upper end: the interval's, swapped when mirrored."""
    return (plan.hi_units, plan.lo_units) if plan.mirror else (plan.lo_units, plan.hi_units)


def truncated_normal(u53: "np.ndarray", plan: Plan) -> Tuple["np.ndarray", "np.ndarray"]:
    """The standardised draw ``z`` and the value in units of the declared scale, per uniform.

    A draw at an end of the plan's interval (no mass, or a tail probability of zero) takes
    that end's units directly, and its ``z`` is reported as that end's standardised bound.
    """
    lower_end, upper_end = _ends(plan)
    u = u53.astype(np.float64) * TWO_POW_MINUS_53
    if plan.mirror:
        u = 1.0 - u
    if plan.degenerate:
        z = np.full(u.shape, -plan.a_f if plan.mirror else plan.a_f)
        return z, np.full(u.shape, lower_end, dtype=np.int64)
    # Each tail's probability is formed from that tail's own mass, so neither is ever 1
    # minus a number close to 1. ``1.0 - u`` is exact for every uniform53.
    lower = plan.cdf_a_f + u * plan.mass_f
    upper = plan.sf_b_f + (1.0 - u) * plan.mass_f
    use_lower = lower <= 0.5
    p = np.where(use_lower, lower, upper)
    at_lower = use_lower & (p <= 0.0)
    at_upper = ~use_lower & (p <= 0.0)
    inside = ~(at_lower | at_upper)
    z = ppnd16(np.where(inside, p, 0.5))
    z = np.where(use_lower, z, -z)
    z = np.where(inside, z, 0.0)
    if plan.mirror:
        z = -z
    value = plan.mean_f + plan.stddev_f * z
    units = np.clip(np.rint(value * plan.unit_f), plan.lo_units, plan.hi_units).astype(np.int64)
    units = np.where(at_lower, lower_end, np.where(at_upper, upper_end, units))
    bound_low, bound_high = (-plan.a_f, -plan.b_f) if plan.mirror else (plan.a_f, plan.b_f)
    z = np.where(at_lower, bound_low, np.where(at_upper, bound_high, z))
    return z, units


def truncated_normal_scalar(u53: int, plan: Plan) -> Tuple[float, int]:
    """:func:`truncated_normal` for one uniform, in Python floats, the same operations."""
    lower_end, upper_end = _ends(plan)
    sign = -1.0 if plan.mirror else 1.0
    u = float(u53) * TWO_POW_MINUS_53
    if plan.mirror:
        u = 1.0 - u
    if plan.degenerate:
        return sign * plan.a_f, lower_end
    lower = plan.cdf_a_f + u * plan.mass_f
    if lower <= 0.5:
        if lower <= 0.0:
            return sign * plan.a_f, lower_end
        z = ppnd16_scalar(lower)
    else:
        upper = plan.sf_b_f + (1.0 - u) * plan.mass_f
        if upper <= 0.0:
            return sign * plan.b_f, upper_end
        z = -ppnd16_scalar(upper)
    if plan.mirror:
        z = -z
    value = plan.mean_f + plan.stddev_f * z
    units = min(max(round(value * plan.unit_f), plan.lo_units), plan.hi_units)
    return z, units


def exponential(u53: "np.ndarray", rate: Decimal, scale: int) -> "np.ndarray":
    """``-log(1 - u) / rate`` in binary64, in units of the declared scale."""
    if not rate.is_finite() or rate <= 0:
        raise _refuse(f"a rate of {rate}")
    if not 0 <= scale <= LARGEST_SCALE:
        raise _refuse(f"scale {scale} lies outside [0, 22]")
    rate_f = float(rate)
    unit_f = float(10**scale)
    if not _input_ok(rate_f):
        raise _refuse(f"a rate outside [2**-900, 2**900]: {rate}")
    if (37.0 / rate_f) * unit_f > LARGEST_UNITS:
        raise _refuse(f"a rate of {rate}, too small for scale {scale} in binary64")
    u = u53.astype(np.float64) * TWO_POW_MINUS_53
    x = -log_basic(1.0 - u) / rate_f
    return np.rint(x * unit_f).astype(np.int64)
