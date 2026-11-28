# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Which entity of a history takes which version count (ADR 0017, the amendment of 2026-11-28).

The counts as a multiset follow from the declared rows, entities and shape alone, so what is left
to decide is who takes which. An entity takes a count its room fits: its own drawn lengths give a
capacity, the most versions its chain holds before its limit, and the counts go out from the
largest down, each to the entity with room for it that comes first in a keyed permutation. An
entity no count fits takes the largest count left, and its chain is compressed as the overrun
rule of the ADR says.

Everything here is exact integer arithmetic. What is drawn is the lengths of each entity's first
versions, at the addresses its chain draws them at (:func:`capacities`: an entity's capacity also
reads lengths past its own count, at addresses no counter moves), and the one permutation.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from flux_engine.core import kernel2

__all__ = ["assign_counts", "capacities", "priorities"]

#: How many lengths :func:`capacities` holds at once: each chunk draws its lengths, reduces them to
#: a capacity per entity and drops them, so the run holds this many words at most (and an entity at
#: least, whatever ``c`` is).
_CHUNK_WORDS = 1 << 13


def capacities(
    key: kernel2.Key, table: Any, rooms: "np.ndarray", closing: int, largest: int
) -> "np.ndarray":
    """Each entity's capacity: the most versions its drawn lengths fit before its limit.

    *rooms* is each entity's limit less its first start; *closing* is 1 under ``closeLast`` and 0
    otherwise; *largest* is ``c``. With ``d_j`` the length drawn at position 0 of version ``j``'s
    ``change`` path (``table.values`` reads it as the chain does), the capacity is
    ``1 + max {k <= c - 1 : d_0 + ... + d_(k-1) <= room - 1 - closing}``, and 1 where no ``k``
    qualifies. A length is held at the room it can no longer exceed, so the sums never overflow.
    """
    entities = int(rooms.shape[0])
    found = np.ones(entities, dtype=np.int64)
    steps = largest - 1
    if entities == 0 or steps <= 0:
        return found
    bound = rooms.astype(np.int64) - 1 - closing
    chunk = max(1, _CHUNK_WORDS // steps)
    versions = np.arange(steps, dtype=np.uint64)
    for low in range(0, entities, chunk):
        high = min(entities, low + chunk)
        size = high - low
        zero = np.zeros(size * steps, dtype=np.uint64)
        words = kernel2.philox_arrays(
            np.repeat(np.arange(low, high, dtype=np.uint64), steps),
            np.tile(versions, size),
            zero,
            zero,
            key,
        )[0]
        lengths = table.values(words >> np.uint64(11)).reshape(size, steps)
        here = bound[low:high]
        lengths = np.minimum(lengths, np.maximum(here, 0)[:, None] + 1)
        reached = np.cumsum(lengths, axis=1) <= here[:, None]
        found[low:high] = 1 + reached.sum(axis=1)
    return found


def priorities(key: bytes, entities: int) -> "np.ndarray":
    """``permute/2(K_fit, E, e)`` for every entity: who comes first among those a count fits."""
    return kernel2.permute2_ndarray(key, entities, np.arange(entities, dtype=np.int64))


def assign_counts(
    multiset: "np.ndarray", capacity: "np.ndarray", priority: "np.ndarray"
) -> "np.ndarray":
    """The count of each entity: *multiset* dealt out by *capacity* and *priority*.

    The counts go out from the largest down. Count ``k`` goes to the entity with no count yet
    whose capacity is at least ``k`` and whose priority is the smallest. When there is none, it
    goes to the entity with no count that has the most capacity, a tie to the smaller priority.
    *priority* is a permutation of ``0 .. E - 1``, so no two entities tie.

    One count value ``k`` held ``m`` times deals ``m`` such counts one after the other, and the
    entities that fit ``k`` do not change while it does, so they go to the ``m`` smallest
    priorities among those that fit and have no count. What is left of ``m`` goes by capacity,
    largest first, then priority. The loop is over the distinct counts, with integer arrays only:
    ``O(distinct counts * E)``, and no entity is visited one at a time.
    """
    entities = int(capacity.shape[0])
    given = np.zeros(entities, dtype=np.int64)
    if entities == 0:
        return given
    values, copies = np.unique(multiset, return_counts=True)
    # The entities with no count yet, as their numbers and the two keys they are chosen by.
    left = np.arange(entities, dtype=np.int64)
    room = capacity.astype(np.int64, copy=False)
    first = priority.astype(np.int64, copy=False)
    for value, many in zip(values[::-1].tolist(), copies[::-1].tolist(), strict=True):
        fits = np.flatnonzero(room >= value)
        taken = min(many, int(fits.shape[0]))
        chosen = np.zeros(room.shape[0], dtype=bool)
        if taken:
            if taken < fits.shape[0]:
                near = np.argpartition(first[fits], taken - 1)[:taken]
                fits = fits[near]
            chosen[fits] = True
        if many > taken:
            # Nobody left has room: the most capacity first, a tie to the smaller priority.
            # Those that fit are all taken, so every one left is below *value*.
            free = np.flatnonzero(~chosen)
            chosen[free[np.lexsort((first[free], -room[free]))[: many - taken]]] = True
        given[left[chosen]] = value
        keep = ~chosen
        left, room, first = left[keep], room[keep], first[keep]
    return given
