# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Draw from the engine the way a run does: one path-derived stream per value.

A run never reads two values off one long stream. Every cell, trait and
reaction has its own seed-tree path (KERNEL-SPEC §3) and usually consumes a
single word of it, so the randomness a column sees is the first word of
each of many *sibling* streams. Goodness of fit measured on one long PCG64
stream would test PCG64; measured here it tests the whole chain the engine
ships -- root, path label, entropy, ``SeedSequence``, ``PCG64``, then the
sampler's own transform.

The paths have the generator's canonical shape,
``("sim", <sim>, "gen", <expose>, "row", <i>, <column>)``, so they are paths
a real run could materialise.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Iterator, List, TypeVar

from flux_engine.core.rng import DrawStream
from flux_engine.core.seedtree import SeedTreeRoot, derive_root, stream_entropy

__all__ = ["draw_many", "root_for", "sibling_streams"]

T = TypeVar("T")


def root_for(label: str, seed: int) -> SeedTreeRoot:
    """A seed-tree root for one experiment: *label* stands in for ``H``."""
    seam = hashlib.sha256(b"flux-accuracy/root\x00" + label.encode("utf-8")).digest()
    return derive_root(seam, seed % (1 << 63))


def sibling_streams(
    label: str, seed: int, count: int, column: str = "value"
) -> Iterator[DrawStream]:
    """*count* sibling streams, one per row index, under one root."""
    root = root_for(label, seed)
    for row in range(count):
        path = ("sim", "accuracy", "gen", label, "row", row, column)
        yield DrawStream.from_entropy(stream_entropy(root, path))


def draw_many(label: str, seed: int, count: int, draw: Callable[[DrawStream], T]) -> List[T]:
    """Call *draw* once on each of *count* sibling streams."""
    return [draw(stream) for stream in sibling_streams(label, seed, count)]
