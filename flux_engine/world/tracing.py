# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An append-only ``flux-trace/1`` log, shared by the world and the executors.

KERNEL-SPEC §10 defines the eleven observable decisions and the five component
digests taken over them; :mod:`flux_engine.core.trace` owns the record shape and
the digests. What the executors need on top is somewhere to put a record --
one object, threaded through population materialisation and every runtime
stage, so that the run's records arrive in one sequence in emission order and
the component digests are taken over that one sequence.

The log is deliberately append-only and offers no way to edit or remove a
record: a component digest that could be recomputed after a record was rewritten
would prove nothing about the run that produced it.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Mapping, Tuple

from flux_engine.core.digest import DigestRef
from flux_engine.core.trace import TraceOp, component_digests, trace_record

__all__ = ["TraceLog"]


class TraceLog:
    """Append-only sequence of canonical ``flux-trace/1`` records."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: List[Dict[str, Any]] = []

    def record(self, op: TraceOp, **fields: Any) -> Mapping[str, Any]:
        """Append one record and return it.

        Validation is :func:`flux_engine.core.trace.trace_record`'s: an op
        outside the closed vocabulary is ``FLX-2103`` and a field set that does
        not match §10's table is ``FLX-2104``, both raised here rather than at
        the digest, so a mis-traced decision names the op and the field.
        """
        built = trace_record(op, **fields)
        self._records.append(built)
        return built

    @property
    def records(self) -> Tuple[Mapping[str, Any], ...]:
        """Every record, in emission order."""
        return tuple(self._records)

    def of(self, op: TraceOp) -> Tuple[Mapping[str, Any], ...]:
        """Every record of one op, in emission order."""
        return tuple(record for record in self._records if record["op"] == op.value)

    def component_digests(self) -> Dict[str, DigestRef]:
        """The five §10 component digests over the records logged so far."""
        return component_digests(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[Mapping[str, Any]]:
        return iter(self._records)
