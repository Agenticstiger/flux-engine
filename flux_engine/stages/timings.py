# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``--timings``: where the time of one stage went, outside every digest.

A stage that is asked for timings collects them with :func:`collecting`, and the
code it runs notes a stretch of work with :func:`span`. Nothing is collected
otherwise, and a span then costs one lookup of a context variable. The numbers
are wall-clock seconds from :func:`time.perf_counter`. They are reported, in the
stage's output and its ``timings`` artifact, and written nowhere else: no plan,
receipt, digest or run file carries one, so a run's bytes are the same with and
without them.

The spans are the serial path of one process. An emit made by worker processes is
one span, from the time its slices were handed out to the time its lines were
joined, so its row generation and framing (done in the workers, in parallel) show
as the wait for them.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from flux_engine.api.stages import StageResult

__all__ = ["Timings", "collecting", "lap", "render", "span", "timed"]


class Timings:
    """The spans of one stage, in the order they ended, and the total they sit in."""

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self._last = self._start
        self._spans: List[Tuple[str, float]] = []

    def add(self, name: str, seconds: float) -> None:
        self._spans.append((name, seconds))
        self._last = time.perf_counter()

    def lap(self, name: str) -> None:
        """Note the time since the last span or lap ended, under *name*."""
        now = time.perf_counter()
        self._spans.append((name, now - self._last))
        self._last = now

    def summed(self) -> Dict[str, float]:
        """Each span's seconds, added up by name, in the order the names first ended."""
        found: Dict[str, float] = {}
        for name, seconds in self._spans:
            found[name] = found.get(name, 0.0) + seconds
        return found

    @property
    def total(self) -> float:
        return time.perf_counter() - self._start

    def as_document(self) -> Dict[str, object]:
        """The spans and the total, rounded to the millisecond, for a stage's artifacts."""
        return {
            "unit": "seconds",
            "spans": [
                {"name": name, "seconds": round(seconds, 3)}
                for name, seconds in self.summed().items()
            ],
            "total": round(self.total, 3),
        }


_CURRENT: ContextVar[Optional[Timings]] = ContextVar("flux_timings", default=None)


@contextmanager
def collecting(enabled: bool = True) -> Iterator[Optional[Timings]]:
    """Collect the spans of the block that runs inside; ``None`` when not *enabled*.

    Inside a block that is already collecting (``run`` calls ``plan`` and ``apply``) the
    spans go to the outer one, so one stage reports one set of timings.
    """
    if not enabled:
        yield None
        return
    outer = _CURRENT.get()
    if outer is not None:
        yield outer
        return
    timings = Timings()
    token = _CURRENT.set(timings)
    try:
        yield timings
    finally:
        _CURRENT.reset(token)


@contextmanager
def span(name: str) -> Iterator[None]:
    """Note how long the block takes, when a stage is collecting; otherwise do nothing."""
    timings = _CURRENT.get()
    if timings is None:
        yield
        return
    begun = time.perf_counter()
    try:
        yield
    finally:
        timings.add(name, time.perf_counter() - begun)


def timed(enabled: bool, call: Callable[[], StageResult]) -> StageResult:
    """*call*'s result, with the timings of the work it did when *enabled*.

    The lines are added to the result's output and its ``timings`` artifact. A result that
    nested stages collected for (``run``) is reported once, by the outermost.
    """
    outer = _CURRENT.get()
    with collecting(enabled) as timings:
        result = call()
    if timings is None or outer is not None:
        return result
    shown = "\n".join(line for line in (result.stdout, render(timings)) if line)
    return replace(
        result, stdout=shown, _artifacts={**result.artifacts, "timings": timings.as_document()}
    )


def lap(name: str) -> None:
    """Note the time since the previous span or lap ended; nothing when no stage is collecting.

    A span wraps a block, a lap marks a boundary in straight-line code, which is how the
    pipeline's long function is divided without being indented.
    """
    timings = _CURRENT.get()
    if timings is not None:
        if name == "start":
            timings._last = time.perf_counter()
        else:
            timings.lap(name)


def render(timings: Timings) -> str:
    """The lines a stage prints: each span, summed by name, then the total."""
    summed = timings.summed()
    width = max([len(name) for name in summed] + [len("total")])
    lines = ["timings (seconds, wall clock; outside every digest):"]
    lines.extend(f"  {name.ljust(width)}  {seconds:8.3f}" for name, seconds in summed.items())
    lines.append(f"  {'total'.ljust(width)}  {timings.total:8.3f}")
    return "\n".join(lines)
