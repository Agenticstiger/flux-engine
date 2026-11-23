# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Lineage emission, as a port.

A run that emits rows into a contract's column set is a producer in
somebody's lineage graph, and saying so is not optional -- it is how the
rows stay traceable once they leave. The port keeps the emission
side-effect out of the kernel: the kernel builds the event from values it
already holds, and something else decides where it goes and what happens
when the receiver is down.

:attr:`LineageEvent.event_time` is a caller-supplied string for the same
reason every other timestamp on this surface is: KERNEL-SPEC forbids the
kernel from reading a wall clock, so the read happens at the facade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = ["DatasetRef", "LineageEmitter", "LineageEvent", "LineageEventType"]


class LineageEventType(str, Enum):
    """The lifecycle points a run reports. Closed set."""

    START = "START"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"
    ABORT = "ABORT"


@dataclass(frozen=True)
class DatasetRef:
    """One end of a lineage edge."""

    namespace: str
    name: str


@dataclass(frozen=True)
class LineageEvent:
    """One report about a run, at one point in its life.

    :attr:`facets` carries the run's digests and declarations. Rows never
    appear in an event; a lineage receiver learns what was produced and
    under which contract, not what was in it.
    """

    event_type: LineageEventType
    event_time: str
    run_id: str
    job_namespace: str
    job_name: str
    inputs: tuple[DatasetRef, ...] = field(default_factory=tuple)
    outputs: tuple[DatasetRef, ...] = field(default_factory=tuple)
    facets: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class LineageEmitter(Protocol):
    """Sends lineage events somewhere.

    An emitter never fails a run: a receiver that is down loses the event,
    not the work. Whether an event was delivered is answered by
    :meth:`flush`, so a caller that needs to know can ask, and a caller
    that does not is not silently told everything was fine.
    """

    def emit(self, event: LineageEvent) -> None:
        """Send *event*. Does not raise on a delivery failure."""

    def flush(self, timeout_seconds: int = 5) -> bool:
        """Wait for pending events; return whether all of them were delivered."""
