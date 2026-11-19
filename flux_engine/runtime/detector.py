# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Detectors: a per-subject, windowed, ordered pattern matcher.

``Detector.spec.pattern`` is an "ordered sequence of signal conditions that must
occur within the window" (FLUX 0.5.0) and ``spec.timeWindowMs`` is that window.
Four rules the schema states or implies, and this module enforces:

* **Ordered.** Element *e+1* is matched only against events strictly after the
  event that matched element *e*.
* **k distinct events.** A *k*-element pattern consumes *k* different events; one
  event never satisfies two elements, however many predicates it happens to
  pass.
* **Windowed.** Every matched event lies within ``timeWindowMs`` of the *first*
  matched event, inclusive at the bound.
* **Non-overlapping, consume-on-match.** A successful match consumes its events:
  scanning resumes at the event *after* the last one it matched, so no event is
  ever part of two detections. A failed attempt consumes only its starting
  event -- scanning resumes one event later, which is what makes the matcher
  find the earliest detection rather than the first one a greedy scan stumbles
  into.

Predicates are ``flux-expr/1`` over the ``event`` scope of the element's own
``signalRef`` (EXPR-SPEC §3), compiled once at plan time; an element with no
predicate matches any event of its signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.clock import Phase, Scheduler
from flux_engine.core.trace import TraceOp
from flux_engine.expr.attrs import AttributeModel
from flux_engine.expr.compile import Predicate, compile_source
from flux_engine.expr.types import ScopeModel, nfc
from flux_engine.runtime.cast import Cast, strip_range
from flux_engine.runtime.signals import SignalEvent
from flux_engine.world.tracing import TraceLog

__all__ = ["CompiledDetector", "Detection", "PatternElement", "compile_detectors", "run_detectors"]


@dataclass(frozen=True)
class PatternElement:
    """One element of a detector pattern: a signal and an optional predicate."""

    signal_id: str
    source: Optional[str]
    predicate: Optional[Predicate]
    scope: Optional[ScopeModel]

    def matches(self, event: SignalEvent) -> bool:
        """Whether *event* satisfies this element.

        The signal check comes first, so a predicate written against one
        signal's payload is never evaluated against another's -- the view for
        that event would be missing the paths the predicate reads, and
        EXPR-SPEC §2's null rules would silently make it false.
        """
        if event.signal_id != self.signal_id:
            return False
        if self.predicate is None or self.scope is None:
            return True
        return self.predicate(event.view(self.scope))


@dataclass(frozen=True)
class CompiledDetector:
    """A ``Detector`` with its predicates compiled and its §7 stream ordinal."""

    detector_id: str
    window_ms: int
    elements: Tuple[PatternElement, ...]
    stream_ordinal: int

    @property
    def signal_ids(self) -> Tuple[str, ...]:
        """The distinct signals this detector reads, NFC-sorted."""
        return tuple(sorted({element.signal_id for element in self.elements}, key=nfc))


@dataclass(frozen=True)
class Detection:
    """One firing: the events it consumed and the §10 ``detect`` op's fields."""

    detector_id: str
    ordinal: int
    first_t: int
    last_t: int
    events: Tuple[SignalEvent, ...]

    @property
    def event_count(self) -> int:
        return len(self.events)


def compile_detectors(cast: Cast, model: AttributeModel) -> Tuple[CompiledDetector, ...]:
    """Compile every ``Detector`` in *cast*, NFC-sorted by id."""
    built: List[CompiledDetector] = []
    for detector_id, document in cast.of_kind("Detector"):
        spec = document.get("spec", {})
        elements: List[PatternElement] = []
        for element in spec.get("pattern", []) or []:
            signal_id = strip_range(element["signalRef"])
            source = element.get("predicate")
            scope = model.event_scope(signal_id) if source else None
            elements.append(
                PatternElement(
                    signal_id=signal_id,
                    source=nfc(str(source)) if source else None,
                    predicate=compile_source(str(source), scope) if source and scope else None,
                    scope=scope,
                )
            )
        built.append(
            CompiledDetector(
                detector_id=detector_id,
                window_ms=int(spec.get("timeWindowMs", 0)),
                elements=tuple(elements),
                stream_ordinal=cast.stream_ordinal(detector_id),
            )
        )
    return tuple(built)


def _by_subject(events: Sequence[SignalEvent]) -> Mapping[int, Tuple[SignalEvent, ...]]:
    """Group events by subject ordinal, each group in the order it was emitted."""
    grouped: Dict[int, List[SignalEvent]] = {}
    for event in events:
        grouped.setdefault(event.ordinal, []).append(event)
    return {ordinal: tuple(grouped[ordinal]) for ordinal in sorted(grouped)}


def _match_from(
    events: Sequence[SignalEvent],
    start: int,
    elements: Sequence[PatternElement],
    window_ms: int,
) -> Optional[Tuple[int, ...]]:
    """Indices of the earliest match beginning at or after *start*, or ``None``."""
    picked: List[int] = []
    cursor = start
    for element in elements:
        while cursor < len(events):
            if picked and events[cursor].t_ms - events[picked[0]].t_ms > window_ms:
                return None
            if element.matches(events[cursor]):
                picked.append(cursor)
                cursor += 1
                break
            cursor += 1
        else:
            return None
    if events[picked[-1]].t_ms - events[picked[0]].t_ms > window_ms:
        return None
    return tuple(picked)


def run_detectors(
    *,
    detectors: Sequence[CompiledDetector],
    events: Sequence[SignalEvent],
    trace: TraceLog,
) -> Tuple[Detection, ...]:
    """Run every detector over every subject's events, tracing each firing.

    Detections are ordered through KERNEL-SPEC §7's scheduler at
    ``phase=DETECT`` and keyed on their **last** event's instant -- a detection
    is not observable until the event that completed it -- so the ``detect``
    ops enter ``eventDigest`` in the order a run would observe them.
    """
    scheduler: Scheduler[Detection] = Scheduler()
    grouped = _by_subject(events)
    for detector in detectors:
        if not detector.elements:
            continue
        for ordinal in sorted(grouped):
            timeline = grouped[ordinal]
            start = 0
            while start < len(timeline):
                picked = _match_from(timeline, start, detector.elements, detector.window_ms)
                if picked is None:
                    # A failed attempt consumes only its starting event: a match
                    # that could not close inside the window from *this* event
                    # may still close from the next one.
                    start += 1
                    continue
                matched = tuple(timeline[index] for index in picked)
                detection = Detection(
                    detector_id=detector.detector_id,
                    ordinal=ordinal,
                    first_t=matched[0].t_ms,
                    last_t=matched[-1].t_ms,
                    events=matched,
                )
                scheduler.schedule(
                    t_ms=detection.last_t,
                    phase=Phase.DETECT,
                    entity_ordinal=ordinal,
                    stream_ordinal=detector.stream_ordinal,
                    payload=detection,
                )
                start = picked[-1] + 1

    fired: List[Detection] = []
    for item in scheduler.drain():
        detection = item.payload
        fired.append(detection)
        trace.record(
            TraceOp.DETECT,
            t=detection.last_t,
            detectorId=detection.detector_id,
            ordinal=detection.ordinal,
            firstT=detection.first_t,
            lastT=detection.last_t,
            eventCount=detection.event_count,
        )
    return tuple(fired)
