# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The virtual clock and the scheduler -- KERNEL-SPEC §7.

§7 in full:

* Time is integer milliseconds from ``Simulation.spec.timeBounds.start`` (RFC 3339,
  normalised to UTC). **No wall clock.**
* Tick = ``tickMs`` (engine-owned, declared in the receipt; default 3 600 000).
* Every scheduled item has the key ``(t_ms, phase, entity_ordinal, stream_ordinal, seq)``:
  ``phase`` is the fixed enum ``SIGNAL=0, DETECT=1, JOURNEY=2, CAMPAIGN=3, EXPERIMENT=4,
  EMIT=5``; ``entity_ordinal`` is the subject ordinal (``-1`` for world-level items);
  ``stream_ordinal`` is the canonical ordinal of the producing document in the bundle
  (documents sorted by NFC id); ``seq`` is a per-run monotonic counter. The key is unique
  by construction.
* The queue is a binary heap over that key; the run is single-threaded; iteration over any
  mapping is by sorted key.

Two defects this module is shaped to make impossible:

* **A set-iterating scheduler.** A queue backed by a ``set`` returns items in an order that
  depends on hash seeding and insertion history, so two runs of one plan can process the
  same two events in opposite orders and both look correct. :class:`Scheduler` is a binary
  heap over the whole key, and the ordering property is pinned by a test that a set-backed
  subclass fails.
* **A wall-clock timestamp.** Nothing here reads the host clock. Times enter as declared
  RFC 3339 strings and leave as integer milliseconds; the only conversion back to a string
  is :func:`format_rfc3339_ms`, which is the exact inverse of the parse and is a pure
  function of its argument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from heapq import heappop, heappush
from typing import Any, Generic, Iterable, Iterator, List, Mapping, Optional, Tuple, TypeVar

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError, register

__all__ = [
    "DEFAULT_TICK_MS",
    "FLX_5022",
    "WORLD_ORDINAL",
    "Phase",
    "RunClock",
    "ScheduleKey",
    "ScheduledItem",
    "Scheduler",
    "VirtualClock",
    "document_ordinals",
    "format_rfc3339_ms",
    "parse_rfc3339_ms",
]

_T = TypeVar("_T")

FLX_5022 = register(
    "FLX-5022",
    "virtual clock or scheduler input outside its declared domain",
    "The virtual clock carries integer milliseconds from a declared RFC 3339 start in "
    "UTC (KERNEL-SPEC §7). A timestamp without an offset, with sub-millisecond "
    "precision the clock cannot represent, a tick of zero or fewer milliseconds, time "
    "bounds that end before they start, or an item scheduled outside those bounds are "
    "all refused: a run whose clock is not the declared clock cannot be replayed "
    "against the plan that declared it.",
)

#: §7: "Tick = ``tickMs`` (engine-owned, declared in the receipt; default 3 600 000)."
DEFAULT_TICK_MS = 3_600_000

#: §7: "``entity_ordinal`` is the subject ordinal (``-1`` for world-level items)".
WORLD_ORDINAL = -1

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_MS = timedelta(milliseconds=1)

#: RFC 3339 ``date-time``: a full date, a ``T`` separator, a full time, an optional
#: fractional part, and a mandatory offset (``Z`` or ``±HH:MM``). Enforced ahead of
#: ``datetime.fromisoformat``, which accepts ISO 8601 forms RFC 3339 does not -- a bare
#: date, or a naive time with no offset at all.
_RFC3339 = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"[Tt]"
    r"(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d+)?"
    r"(?P<offset>[Zz]|[+-]\d{2}:\d{2})$"
)


class Phase(IntEnum):
    """§7's fixed phase enum. The values are normative: they order the key."""

    SIGNAL = 0
    DETECT = 1
    JOURNEY = 2
    CAMPAIGN = 3
    EXPERIMENT = 4
    EMIT = 5


# ---------------------------------------------------------------------------
# RFC 3339 <-> integer milliseconds
# ---------------------------------------------------------------------------


def parse_rfc3339_ms(text: str) -> int:
    """Parse an RFC 3339 timestamp to integer milliseconds since the Unix epoch, UTC.

    Refuses (``FLX-5022``) a value with no offset, and a value carrying sub-millisecond
    precision: §7's clock is integer milliseconds, so a microsecond in the declaration has
    no representation, and silently truncating it would make the run's time bounds
    different from the declared ones by an amount nothing records.
    """
    if not isinstance(text, str):
        raise FluxError(FLX_5022, f"timestamp must be a string, got {type(text).__name__}")
    match = _RFC3339.match(text)
    if match is None:
        raise FluxError(FLX_5022, "timestamp is not an RFC 3339 date-time with an offset")
    fraction = match.group("fraction")
    if fraction is not None:
        digits = fraction[1:]
        if len(digits) > 3 and set(digits[3:]) != {"0"}:
            raise FluxError(
                FLX_5022,
                "timestamp carries sub-millisecond precision, which the virtual clock "
                "does not represent",
            )
    normalised = text.replace("z", "Z")
    try:
        moment = datetime.fromisoformat(normalised)
    except ValueError as exc:  # pragma: no cover - the pattern above already rejects these
        raise FluxError(FLX_5022, "timestamp is not a valid date-time") from exc
    if moment.tzinfo is None:  # pragma: no cover - the pattern requires an offset
        raise FluxError(FLX_5022, "timestamp has no UTC offset")
    delta = moment.astimezone(timezone.utc) - _EPOCH
    if delta % _ONE_MS != timedelta(0):  # pragma: no cover - guarded by the fraction check
        raise FluxError(FLX_5022, "timestamp is not a whole number of milliseconds")
    return delta // _ONE_MS


def format_rfc3339_ms(t_ms: int) -> str:
    """Render integer milliseconds as RFC 3339 UTC with millisecond precision and ``Z``.

    The exact inverse of :func:`parse_rfc3339_ms`, and the rendering KERNEL-SPEC §9 names
    for the ``TIMESTAMP`` logical type. Always three fractional digits, so two timestamps
    one second apart have the same width and sort as strings the way they sort as instants.
    """
    if isinstance(t_ms, bool) or not isinstance(t_ms, int):
        raise FluxError(FLX_5022, f"milliseconds must be an integer, got {type(t_ms).__name__}")
    moment = _EPOCH + timedelta(milliseconds=t_ms)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def document_ordinals(document_ids: Iterable[str]) -> Mapping[str, int]:
    """§7's ``stream_ordinal``: bundle documents numbered in NFC-sorted id order.

    The ordinal is a property of the bundle, not of the order documents were loaded in, so
    two runs over one bundle give one numbering however the loader walked the directory.
    """
    ordered = sorted(set(document_ids), key=lambda document_id: (nfc(document_id), document_id))
    return {document_id: index for index, document_id in enumerate(ordered)}


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VirtualClock:
    """The run's virtual clock: integer milliseconds and a declared tick (KERNEL-SPEC §7).

    ``start_ms``/``end_ms`` are the parsed ``Simulation.spec.timeBounds``, and the bounds
    are **inclusive** at both ends -- the shipped example's
    ``end: "2026-09-30T23:59:59Z"`` names an instant inside the run, not the first instant
    after it, so :attr:`tick_count` counts the ticks that contain at least one instant of
    ``[start, end]``.
    """

    start_ms: int
    end_ms: int
    tick_ms: int = DEFAULT_TICK_MS

    def __post_init__(self) -> None:
        if self.tick_ms < 1:
            raise FluxError(FLX_5022, f"tickMs must be >= 1, got {self.tick_ms}")
        if self.end_ms < self.start_ms:
            raise FluxError(
                FLX_5022,
                f"time bounds end {self.end_ms} ms before they start ({self.start_ms} ms)",
            )

    @classmethod
    def from_time_bounds(
        cls, start: str, end: str, *, tick_ms: int = DEFAULT_TICK_MS
    ) -> "VirtualClock":
        """Build the clock from a declared ``timeBounds`` block."""
        return cls(
            start_ms=parse_rfc3339_ms(start),
            end_ms=parse_rfc3339_ms(end),
            tick_ms=tick_ms,
        )

    @property
    def duration_ms(self) -> int:
        """Milliseconds from the start bound to the end bound."""
        return self.end_ms - self.start_ms

    @property
    def tick_count(self) -> int:
        """Number of ticks covering ``[start, end]``."""
        return self.duration_ms // self.tick_ms + 1

    def contains(self, t_ms: int) -> bool:
        """Whether *t_ms* lies inside the declared bounds."""
        return self.start_ms <= t_ms <= self.end_ms

    def require_within(self, t_ms: int, what: str = "time") -> int:
        """Return *t_ms*, or refuse it when it falls outside the declared bounds."""
        if not self.contains(t_ms):
            raise FluxError(
                FLX_5022,
                f"{what} {t_ms} ms is outside the declared bounds "
                f"[{self.start_ms}, {self.end_ms}]",
            )
        return t_ms

    def tick_index(self, t_ms: int) -> int:
        """The index of the tick containing *t_ms*, counted from the start bound."""
        self.require_within(t_ms)
        return (t_ms - self.start_ms) // self.tick_ms

    def tick_start(self, index: int) -> int:
        """The first instant of tick *index*."""
        if not 0 <= index < self.tick_count:
            raise FluxError(FLX_5022, f"tick index {index} is outside [0, {self.tick_count - 1}]")
        return self.start_ms + index * self.tick_ms

    def ticks(self) -> Iterator[int]:
        """Yield the first instant of every tick, in order."""
        for index in range(self.tick_count):
            yield self.start_ms + index * self.tick_ms

    def offset_ms(self, t_ms: int) -> int:
        """Milliseconds from the start bound -- §7's "integer milliseconds from ... start"."""
        return t_ms - self.start_ms

    def to_rfc3339(self, t_ms: int) -> str:
        """Render an instant of this clock as RFC 3339 UTC with millisecond precision."""
        return format_rfc3339_ms(t_ms)


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class ScheduleKey:
    """§7's scheduling key: ``(t_ms, phase, entity_ordinal, stream_ordinal, seq)``.

    Ordered by the whole tuple, in that order of significance. ``seq`` is last and is
    assigned by the scheduler from a per-run monotonic counter, so two items can never
    share a key: the key is unique **by construction** rather than by a uniqueness check
    that a caller could forget to run.
    """

    t_ms: int
    phase: Phase
    entity_ordinal: int
    stream_ordinal: int
    seq: int

    def as_tuple(self) -> Tuple[int, int, int, int, int]:
        """The key as the plain integer tuple the heap orders on."""
        return (self.t_ms, int(self.phase), self.entity_ordinal, self.stream_ordinal, self.seq)


@dataclass(frozen=True)
class ScheduledItem(Generic[_T]):
    """One item on the queue: its key and the payload the executor will act on."""

    key: ScheduleKey
    payload: _T

    def __lt__(self, other: "ScheduledItem[_T]") -> bool:
        """Order by key alone.

        Defined so that a heap entry never falls through to comparing payloads: two
        payloads may not be orderable at all, and if they were, ordering by them would let
        a payload's contents decide execution order.
        """
        if not isinstance(other, ScheduledItem):  # pragma: no cover - heap only holds items
            return NotImplemented
        return self.key.as_tuple() < other.key.as_tuple()


class Scheduler(Generic[_T]):
    """The run's event queue: a binary heap over §7's key.

    Single-threaded, single-writer, and monotonic in the popped key: every :meth:`pop`
    returns an item whose key is greater than the key of the item before it, which is what
    makes an execution order reproducible rather than merely repeatable-in-practice.
    """

    __slots__ = ("_heap", "_next_seq", "_popped")

    def __init__(self) -> None:
        self._heap: List[Tuple[Tuple[int, int, int, int, int], ScheduledItem[_T]]] = []
        self._next_seq = 0
        self._popped: Optional[Tuple[int, int, int, int, int]] = None

    def __len__(self) -> int:
        return len(self._heap)

    def __bool__(self) -> bool:
        return bool(self._heap)

    @property
    def next_seq(self) -> int:
        """The value the next scheduled item's ``seq`` will take."""
        return self._next_seq

    def schedule(
        self,
        *,
        t_ms: int,
        phase: Phase,
        entity_ordinal: int,
        stream_ordinal: int,
        payload: _T,
    ) -> ScheduleKey:
        """Place *payload* on the queue and return the key it was placed under.

        ``seq`` is assigned here, from the scheduler's own counter -- the reason §7 can call
        the key unique by construction. Callers never supply it.
        """
        if isinstance(t_ms, bool) or not isinstance(t_ms, int):
            raise FluxError(FLX_5022, f"t_ms must be an integer, got {type(t_ms).__name__}")
        if not isinstance(phase, Phase):
            raise FluxError(FLX_5022, f"phase must be a Phase, got {type(phase).__name__}")
        if entity_ordinal < WORLD_ORDINAL:
            raise FluxError(
                FLX_5022,
                f"entity ordinal must be >= {WORLD_ORDINAL} (world-level), "
                f"got {entity_ordinal}",
            )
        if stream_ordinal < 0:
            raise FluxError(FLX_5022, f"stream ordinal must be >= 0, got {stream_ordinal}")

        key = ScheduleKey(
            t_ms=t_ms,
            phase=phase,
            entity_ordinal=entity_ordinal,
            stream_ordinal=stream_ordinal,
            seq=self._next_seq,
        )
        self._next_seq += 1
        self._store(ScheduledItem(key=key, payload=payload))
        return key

    def peek(self) -> Optional[ScheduledItem[_T]]:
        """The item that :meth:`pop` would return next, without removing it."""
        if not self._heap:
            return None
        return self._heap[0][1]

    def pop(self) -> ScheduledItem[_T]:
        """Remove and return the least item by key.

        Asserts the ordering it promises: a popped key that is not greater than the one
        before it means the queue is not a heap over the whole key, and the run stops there
        rather than emitting rows in an order no plan can reproduce.
        """
        if not self._heap:
            raise FluxError(FLX_5022, "pop from an empty scheduler")
        item = self._take()
        current = item.key.as_tuple()
        if self._popped is not None and current <= self._popped:
            raise FluxError(
                FLX_5022,
                f"scheduler returned key {current} after {self._popped}; "
                "the queue is not ordered by the whole scheduling key",
            )
        self._popped = current
        return item

    def drain(self) -> Iterator[ScheduledItem[_T]]:
        """Pop every item, in key order, including items scheduled while draining."""
        while self._heap:
            yield self.pop()

    # -- the two operations a broken subclass replaces -----------------------

    def _store(self, item: ScheduledItem[_T]) -> None:
        heappush(self._heap, (item.key.as_tuple(), item))

    def _take(self) -> ScheduledItem[_T]:
        return heappop(self._heap)[1]


@dataclass
class RunClock:
    """A clock and a scheduler travelling together, as an executor holds them.

    Convenience only: it owns no rule of its own beyond refusing to schedule an item
    outside the declared time bounds, which is :meth:`VirtualClock.require_within` applied
    at the point where a mistake is cheapest to see.
    """

    clock: VirtualClock
    scheduler: "Scheduler[Any]" = field(default_factory=Scheduler)

    def schedule(
        self,
        *,
        t_ms: int,
        phase: Phase,
        entity_ordinal: int,
        stream_ordinal: int,
        payload: Any,
    ) -> ScheduleKey:
        """Schedule an item, refusing a time outside the declared bounds."""
        self.clock.require_within(t_ms, "scheduled time")
        return self.scheduler.schedule(
            t_ms=t_ms,
            phase=phase,
            entity_ordinal=entity_ordinal,
            stream_ordinal=stream_ordinal,
            payload=payload,
        )
