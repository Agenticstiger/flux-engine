# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The emit seam: where rows leave the engine.

KERNEL-SPEC section 8 draws the line this module exists to hold: the
``outputDigest`` over an emitted stream is computed **above** the sink, so
"the sink cannot influence it". A sink therefore receives rows that have
already been rendered and already been accounted for; its only job is to
put them somewhere. Swapping one sink for another changes where the rows
land and changes nothing about what the run is answerable for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from flux_engine.api.digests import DigestRef

__all__ = ["EmitHeader", "EmitSink"]


@dataclass(frozen=True)
class EmitHeader:
    """The declared shape of one emitted stream (KERNEL-SPEC section 8).

    Rows travel as arrays aligned to :attr:`columns`, in the expose's
    declared column order -- an ordered header rather than per-row objects,
    because canonical JSON sorts object keys and would erase that order.

    Attributes
    ----------
    expose_id
        The expose this stream realises.
    columns
        Declared column names, in declared order. A tuple, not a list: the
        order is part of what the stream is digested under.
    types
        The logical type of each column, positionally aligned to
        :attr:`columns` (KERNEL-SPEC section 9's closed type set).
    contract_digest
        The referenced FLUID contract, digested in the ``jcs-document``
        domain. A sink that writes rows under a header whose contract
        digest does not match the shipped contract is writing rows nobody
        can place.
    """

    expose_id: str
    columns: tuple[str, ...]
    types: tuple[str, ...]
    contract_digest: "DigestRef"


@runtime_checkable
class EmitSink(Protocol):
    """Somewhere emitted rows are written.

    The three calls arrive in one order -- :meth:`open` once, :meth:`write`
    zero or more times, :meth:`close` once -- and :meth:`close` runs even
    when the run fails, so a sink holding an open handle always gets the
    chance to release it.
    """

    def open(self, header: EmitHeader) -> None:
        """Begin a stream described by *header*. Called once, before any row."""

    def write(self, row: Sequence[object]) -> None:
        """Accept one row, positionally aligned to ``header.columns``.

        Values arrive already rendered per KERNEL-SPEC section 9; a sink
        neither re-renders nor re-orders them.
        """

    def close(self) -> None:
        """Release the stream. Called once, on the failure path too."""
