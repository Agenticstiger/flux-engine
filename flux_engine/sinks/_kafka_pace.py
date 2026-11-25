# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""When a Kafka record is sent, never what it holds (ADR 0038 section 10).

``pace=max`` sends as fast as the brokers take. ``pace=realtime`` and ``pace=xN`` hold a stream
until its last row, put its records in event-time order while keeping each key's order
(:func:`time_order`), and send each one when the wall clock, counted from the first record,
reaches its offset from the first event time divided by the speed (:class:`Pacer`).

The clock and the sleep are parameters, so a test runs a day of event time in no time. Speeds
are exact fractions: the only float is the number of seconds handed to ``sleep``.

Standard library and :mod:`flux_engine` only.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Sequence

from flux_engine.core.errors import FluxError
from flux_engine.sinks._kafka_records import KafkaRecord
from flux_engine.sinks.errors import FLX_3504

__all__ = ["MIN_SPEED", "Pace", "Pacer", "parse_pace", "scheduled_times", "time_order"]

_SPEED = re.compile(r"x([0-9]+(?:\.[0-9]+)?)", re.ASCII)
_FORMS = "max, realtime, or xN with N a number of at least 0.001 (x2, x0.5)"

#: The slowest ``xN`` takes: a thousandth of real time.
MIN_SPEED = Fraction(1, 1000)


@dataclass(frozen=True)
class Pace:
    """How fast a stream is sent: ``speed`` is ``None`` for ``max``, else a multiple of real time."""

    text: str
    speed: Optional[Fraction]

    @property
    def paced(self) -> bool:
        """Whether records wait for their time."""
        return self.speed is not None


def parse_pace(value: Optional[str]) -> Pace:
    """The pace *value* names (``max`` when absent); ``FLX-3504`` for any other text."""
    if value is None or value == "max":
        return Pace("max", None)
    if value == "realtime":
        return Pace("realtime", Fraction(1))
    match = _SPEED.fullmatch(value)
    if match is not None:
        speed = Fraction(match.group(1))
        if speed >= MIN_SPEED:
            return Pace(value, speed)
    raise FluxError(FLX_3504, f"the option pace takes {_FORMS}, not {value!r}")


def scheduled_times(records: Sequence[KafkaRecord]) -> List[int]:
    """The event time each of *records* is sent at when paced, in milliseconds.

    A record is scheduled at the running maximum of the timestamps of the records of its key so
    far, so a key whose rows arrive out of event-time order keeps its own order.
    """
    latest: Dict[bytes, int] = {}
    scheduled: List[int] = []
    for record in records:
        when = max(latest.get(record.key, record.timestamp_ms), record.timestamp_ms)
        latest[record.key] = when
        scheduled.append(when)
    return scheduled


def time_order(records: Sequence[KafkaRecord]) -> List[int]:
    """Indices of *records* in the order a paced stream sends them.

    A record is scheduled at :func:`scheduled_times`. The sort is stable on
    ``(scheduled, index)``, so ties keep their stream order, and a tombstone (a record
    with no value) stays right after the record before it with the same key and timestamp.
    """
    scheduled = scheduled_times(records)
    return sorted(range(len(records)), key=lambda index: (scheduled[index], index))


class Pacer:
    """Turns a record's scheduled event time into a wait."""

    def __init__(
        self,
        pace: Pace,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pace = pace
        self._monotonic = monotonic
        self._sleep = sleep
        self._wall = 0.0
        self._event_ms = 0

    def start(self, first_scheduled_ms: int) -> None:
        """Anchor wall time now and event time at *first_scheduled_ms*."""
        self._wall = self._monotonic()
        self._event_ms = first_scheduled_ms

    def delay(self, scheduled_ms: int) -> float:
        """Seconds until the record scheduled at *scheduled_ms* is due; 0 when it is, or at max."""
        speed = self._pace.speed
        if speed is None:
            return 0.0
        offset = Fraction(scheduled_ms - self._event_ms, 1000) / speed
        due = Fraction(self._wall) + offset
        return max(0.0, float(due - Fraction(self._monotonic())))

    def wait(self, scheduled_ms: int) -> float:
        """Sleep until the record is due; the seconds slept."""
        seconds = self.delay(scheduled_ms)
        if seconds > 0:
            self._sleep(seconds)
        return seconds
