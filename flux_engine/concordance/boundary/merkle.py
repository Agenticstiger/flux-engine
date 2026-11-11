# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``C1``: the per-column commitment -- SEAM-FIDELITY-SPEC §4.

    **C1**: per-column Merkle root (sha256 in v0.1; the leaf function is a
    port so it can become circuit-friendly later) over fixed-point-encoded
    values padded to ``records.max`` with the reserved padding encoding;
    nulls carry the null encoding; a validity flag per position.

Five choices, each made because the alternative is a way for the
commitment to say less than it appears to.

**Padding to ``records.max``, always.** The tree's shape is a function of
the declaration, not of the data, so the commitment's shape leaks nothing
about how many records the state holds. The count travels separately, as
§4's "record count (or declared range)", where the owner can choose.

**Three leaf statuses, not two.** ``present``, ``null`` and ``padding``
are distinct reserved encodings, and each carries its own validity flag.
If padding reused the null encoding, a state could be extended with nulls
and keep its root; if null reused the zero value, a null would be
indistinguishable from a real zero.

**A fixed body width per column.** Every leaf of one column has the same
length, so no leaf's preimage can be a prefix of another's. A ``RANGE``
column's body is its grid value in two's-complement big-endian at a width
derived from the declared range; a ``SUPPORT`` column's body is the
sha256 of its NFC bytes, which is fixed-width by construction and is what
lets a categorical column be committed in the same tree as a numeric one.

**Domain-separated node hashing.** A leaf is hashed under a different
prefix from an internal node, so no internal node's preimage can be
mistaken for a leaf's -- the standard second-preimage separation.

**No duplicate promotion at an odd level.** The leaf count is fixed at
``records.max`` and committed, and the tree is padded up to a power of two
with a reserved empty-node constant rather than by duplicating the last
node, so two different leaf lists cannot produce one root.

Nothing here reads a clock, draws randomness, or holds state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from flux_engine.concordance.errors import FLX_5035
from flux_engine.core.canonical import nfc, sha256_hex
from flux_engine.core.errors import FluxError
from flux_engine.core.fixedpoint import Grid, Interval

__all__ = [
    "CATEGORY_BODY_BYTES",
    "ColumnCommitment",
    "EMPTY_NODE",
    "LEAF_PREFIX",
    "LeafFunction",
    "LeafStatus",
    "NODE_PREFIX",
    "Sha256Leaf",
    "category_body",
    "commit_column",
    "leaf_preimage",
    "merkle_root",
    "order_commitments",
    "units_body",
    "units_body_width",
]

#: Domain tag on the preimage of every leaf. Carries the commitment's
#: version so a later leaf function is a different tag, not a silent
#: reinterpretation of the same bytes.
LEAF_PREFIX = b"flux-fidelity-leaf/1\x00"

#: Domain tag on the preimage of every internal node.
NODE_PREFIX = b"flux-fidelity-node/1\x00"

#: The reserved digest of a node with no leaves beneath it, used to pad a
#: level up to a power of two. A fixed constant rather than a duplicated
#: sibling, so the tree shape is determined by the leaf count alone.
EMPTY_NODE = sha256_hex(b"flux-fidelity-empty/1")

#: The body width of a ``SUPPORT`` column's leaf: one sha256 digest.
CATEGORY_BODY_BYTES = 32


class LeafStatus(Enum):
    """What one position of a committed column holds.

    The byte value is part of the leaf preimage, so the three statuses are
    three different commitments to the same position and none can be
    substituted for another.
    """

    PRESENT = 0
    NULL = 1
    PADDING = 2


@runtime_checkable
class LeafFunction(Protocol):
    """The hash a leaf and an internal node are taken under.

    §4 keeps this a port "so it can become circuit-friendly later". The
    v0.1 implementation is :class:`Sha256Leaf`; anything else is a
    different commitment and says so through :attr:`name`, which travels
    in the committed state.
    """

    @property
    def name(self) -> str:
        """The function's wire name, recorded with every commitment."""

    def __call__(self, data: bytes) -> str:
        """Return the hex digest of *data*."""


@dataclass(frozen=True)
class Sha256Leaf:
    """The v0.1 leaf function: sha256, hex-encoded."""

    name: str = "sha256"

    def __call__(self, data: bytes) -> str:
        return sha256_hex(data)


def units_body_width(grid: Grid, interval: Interval) -> int:
    """The fixed byte width a ``RANGE`` column's grid values commit at.

    Derived from the declared range and the grid, with one extra bit
    reserved for the sign, and at least one byte so a degenerate range
    still commits to something.
    """
    magnitude = max(
        abs(grid.quantise(interval.lo, subject="range.min")),
        abs(grid.quantise(interval.hi, subject="range.max")),
    )
    return max(1, (magnitude.bit_length() + 8) // 8)


def units_body(units: int, width: int) -> bytes:
    """A grid value as a fixed-width two's-complement big-endian body."""
    try:
        return units.to_bytes(width, "big", signed=True)
    except OverflowError:
        raise FluxError(
            FLX_5035,
            f"an encoded value needs more than the column's committed width of {width} bytes",
        ) from None


def category_body(value: str) -> bytes:
    """A categorical value's fixed-width body: sha256 of its NFC bytes."""
    return bytes.fromhex(sha256_hex(nfc(value).encode("utf-8")))


def leaf_preimage(status: LeafStatus, body: Optional[bytes], width: int) -> bytes:
    """The bytes one committed position is hashed over.

    ``LEAF_PREFIX || status || validity || body``. A ``null`` or
    ``padding`` position carries the reserved all-zero body together with
    its own status byte and a validity flag of zero, so its preimage
    differs from a present zero in two places rather than none.
    """
    if status is LeafStatus.PRESENT:
        if body is None:
            raise FluxError(FLX_5035, "a present position carries a value")
        if len(body) != width:
            raise FluxError(
                FLX_5035,
                f"a committed body is {len(body)} bytes where the column commits at {width}",
            )
        return LEAF_PREFIX + bytes([status.value]) + b"\x01" + body
    if body is not None:
        raise FluxError(FLX_5035, f"a {status.name.lower()} position carries no value")
    return LEAF_PREFIX + bytes([status.value]) + b"\x00" + bytes(width)


def merkle_root(leaves: Sequence[str], leaf_function: LeafFunction) -> str:
    """Fold *leaves* (already hashed) into one root.

    The level is padded up to a power of two with :data:`EMPTY_NODE`, then
    folded pairwise under :data:`NODE_PREFIX`. An empty list has the empty
    node as its root, which is the honest answer for a column with no
    committed positions -- and is refused earlier, by ``records.min``.
    """
    if not leaves:
        return EMPTY_NODE
    level: List[str] = list(leaves)
    width = 1
    while width < len(level):
        width *= 2
    level.extend([EMPTY_NODE] * (width - len(level)))
    while len(level) > 1:
        level = [
            leaf_function(NODE_PREFIX + bytes.fromhex(level[i]) + bytes.fromhex(level[i + 1]))
            for i in range(0, len(level), 2)
        ]
    return level[0]


@dataclass(frozen=True)
class ColumnCommitment:
    """One column's ``C1`` and the shape it was taken over.

    ``present_count`` and ``null_count`` stay inside the boundary: they
    are not part of :meth:`to_document`, because §4's list of what leaves
    is closed and a per-column null count is not on it.
    """

    column: str
    root: str
    leaf_count: int
    present_count: int
    null_count: int
    leaf_function: str

    def to_document(self) -> Dict[str, Any]:
        """The canonical form that travels in the committed state."""
        return {
            "column": self.column,
            "root": f"sha256:{self.root}",
            "leafCount": self.leaf_count,
            "leafFunction": self.leaf_function,
        }


def commit_column(
    *,
    column: str,
    bodies: Sequence[Optional[bytes]],
    padded_to: int,
    width: int,
    leaf_function: Optional[LeafFunction] = None,
) -> ColumnCommitment:
    """Take one column's ``C1`` over *bodies*, padded to *padded_to*.

    *bodies* carries one entry per committed record, in the committed
    order: the fixed-width body for a present value, ``None`` for a null.
    Positions beyond the record count up to *padded_to* carry the padding
    encoding. Refuses (``FLX-5035``) a record set larger than the declared
    ``records.max``, since committing more positions than declared would
    make the tree's shape a function of the data.
    """
    function: LeafFunction = leaf_function if leaf_function is not None else Sha256Leaf()
    if len(bodies) > padded_to:
        raise FluxError(
            FLX_5035,
            f"column {column!r}: {len(bodies)} records exceed the declared records.max "
            f"of {padded_to}",
        )
    leaves: List[str] = []
    present = 0
    nulls = 0
    for body in bodies:
        if body is None:
            nulls += 1
            leaves.append(function(leaf_preimage(LeafStatus.NULL, None, width)))
        else:
            present += 1
            leaves.append(function(leaf_preimage(LeafStatus.PRESENT, body, width)))
    padding_leaf = function(leaf_preimage(LeafStatus.PADDING, None, width))
    leaves.extend([padding_leaf] * (padded_to - len(bodies)))
    return ColumnCommitment(
        column=column,
        root=merkle_root(leaves, function),
        leaf_count=padded_to,
        present_count=present,
        null_count=nulls,
        leaf_function=function.name,
    )


def order_commitments(
    commitments: Sequence[ColumnCommitment],
) -> Tuple[ColumnCommitment, ...]:
    """Order per-column commitments by column name.

    Every artefact this package builds is iterated in one order, so two
    runs over one record set produce one committed state byte for byte.
    """
    return tuple(sorted(commitments, key=lambda entry: entry.column))
