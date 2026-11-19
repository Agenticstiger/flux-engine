# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The windowed ordered matcher: order, distinctness, window, non-overlap."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import pytest

from flux_engine.core.trace import TraceOp
from flux_engine.expr.compile import compile_source
from flux_engine.expr.types import NUMBER, STRING, Scope, ScopeModel
from flux_engine.runtime.detector import (
    CompiledDetector,
    PatternElement,
    compile_detectors,
    run_detectors,
)
from flux_engine.world.tracing import TraceLog

from .conftest import Staged, requires_example

pytestmark = pytest.mark.unit

SCOPE = ScopeModel(Scope.EVENT, {"data.amount": NUMBER, "type": STRING}, {"amount": "data.amount"})


class FakeEvent:
    """The smallest thing the matcher needs: a signal, an instant, and a view."""

    def __init__(self, signal_id: str, t_ms: int, amount: Decimal) -> None:
        self.signal_id = signal_id
        self.t_ms = t_ms
        self.ordinal = 0
        self.occurrence = 0
        self._amount = amount

    def view(self, model: ScopeModel) -> Dict[str, Any]:
        return {"data.amount": self._amount, "type": "t"}


def element(signal_id: str, predicate: Optional[str] = None) -> PatternElement:
    return PatternElement(
        signal_id=signal_id,
        source=predicate,
        predicate=compile_source(predicate, SCOPE) if predicate else None,
        scope=SCOPE if predicate else None,
    )


def detector(*elements: PatternElement, window_ms: int = 1000) -> CompiledDetector:
    return CompiledDetector(
        detector_id="det", window_ms=window_ms, elements=elements, stream_ordinal=0
    )


def run(det: CompiledDetector, events: Sequence[Any]) -> Any:
    log = TraceLog()
    fired = run_detectors(detectors=[det], events=events, trace=log)
    return fired, log


def test_a_two_element_pattern_needs_two_distinct_events() -> None:
    only_one = [FakeEvent("s", 0, Decimal(5))]
    fired, _ = run(detector(element("s"), element("s")), only_one)
    assert fired == ()


def test_a_pattern_matches_the_earliest_qualifying_pair() -> None:
    events = [FakeEvent("s", t, Decimal(5)) for t in (0, 100, 200)]
    fired, _ = run(detector(element("s"), element("s")), events)
    assert [(d.first_t, d.last_t) for d in fired] == [(0, 100)]


def test_matches_do_not_overlap_and_consume_their_events() -> None:
    events = [FakeEvent("s", t, Decimal(5)) for t in (0, 100, 200, 300)]
    fired, _ = run(detector(element("s"), element("s")), events)
    assert [(d.first_t, d.last_t) for d in fired] == [(0, 100), (200, 300)]


def test_an_event_outside_the_window_does_not_complete_a_match() -> None:
    events = [FakeEvent("s", 0, Decimal(5)), FakeEvent("s", 5000, Decimal(5))]
    fired, _ = run(detector(element("s"), element("s"), window_ms=1000), events)
    assert fired == ()


def test_the_window_bound_is_inclusive() -> None:
    events = [FakeEvent("s", 0, Decimal(5)), FakeEvent("s", 1000, Decimal(5))]
    fired, _ = run(detector(element("s"), element("s"), window_ms=1000), events)
    assert len(fired) == 1


def test_a_failed_attempt_consumes_only_its_starting_event() -> None:
    """The match that could not close from event 0 still closes from event 1."""
    events = [
        FakeEvent("s", 0, Decimal(5)),
        FakeEvent("s", 5000, Decimal(5)),
        FakeEvent("s", 5100, Decimal(5)),
    ]
    fired, _ = run(detector(element("s"), element("s"), window_ms=1000), events)
    assert [(d.first_t, d.last_t) for d in fired] == [(5000, 5100)]


def test_the_order_of_the_elements_matters() -> None:
    events = [FakeEvent("a", 0, Decimal(5)), FakeEvent("b", 100, Decimal(5))]
    assert len(run(detector(element("a"), element("b")), events)[0]) == 1
    assert run(detector(element("b"), element("a")), events)[0] == ()


def test_a_predicate_filters_which_events_may_match() -> None:
    events = [
        FakeEvent("s", 0, Decimal(0)),
        FakeEvent("s", 100, Decimal(9)),
        FakeEvent("s", 200, Decimal(9)),
    ]
    fired, _ = run(detector(element("s", "amount > 0"), element("s")), events)
    assert [(d.first_t, d.last_t) for d in fired] == [(100, 200)]


def test_an_element_never_evaluates_a_predicate_against_another_signal() -> None:
    """A view for the wrong signal would make the predicate silently false."""
    matched = element("s", "amount > 0")
    assert matched.matches(FakeEvent("s", 0, Decimal(5)))
    assert not matched.matches(FakeEvent("other", 0, Decimal(5)))


def test_subjects_are_matched_independently() -> None:
    first = FakeEvent("s", 0, Decimal(5))
    second = FakeEvent("s", 100, Decimal(5))
    second.ordinal = 1
    fired, _ = run(detector(element("s"), element("s")), [first, second])
    assert fired == ()


def test_a_detection_traces_one_op_carrying_the_spec_fields() -> None:
    events = [FakeEvent("s", 0, Decimal(5)), FakeEvent("s", 100, Decimal(5))]
    fired, log = run(detector(element("s"), element("s")), events)
    record = log.of(TraceOp.DETECT)[0]
    assert record["t"] == fired[0].last_t
    assert record["firstT"] == 0 and record["lastT"] == 100
    assert record["eventCount"] == 2
    assert record["detectorId"] == "det"


def test_detections_are_traced_in_completion_order() -> None:
    slow = FakeEvent("s", 0, Decimal(5))
    slow_second = FakeEvent("s", 900, Decimal(5))
    fast = FakeEvent("s", 100, Decimal(5))
    fast.ordinal = 1
    fast_second = FakeEvent("s", 200, Decimal(5))
    fast_second.ordinal = 1
    fired, _ = run(detector(element("s"), element("s")), [slow, fast, fast_second, slow_second])
    assert [d.last_t for d in fired] == sorted(d.last_t for d in fired)


def test_a_detector_with_an_empty_pattern_fires_nothing() -> None:
    fired, _ = run(detector(), [FakeEvent("s", 0, Decimal(5))])
    assert fired == ()


def test_a_single_element_pattern_fires_once_per_qualifying_event() -> None:
    events = [FakeEvent("s", t, Decimal(5)) for t in (0, 10, 20)]
    fired, _ = run(detector(element("s")), events)
    assert [d.event_count for d in fired] == [1, 1, 1]


@requires_example
@pytest.mark.integration
def test_the_shipped_detector_compiles_and_fires_on_the_shipped_signals(
    staged: Staged,
) -> None:
    """Everything read from the bundle: the predicate, the window, the signal."""
    detectors = compile_detectors(staged.cast, staged.model)
    assert detectors, "the cast holds no Detector"
    log = TraceLog()
    fired = run_detectors(detectors=detectors, events=staged.events, trace=log)
    assert fired, "the shipped detector never fires on the shipped arrival model"
    document = staged.cast.document(detectors[0].detector_id)
    window = document["spec"]["timeWindowMs"]
    depth = len(document["spec"]["pattern"])
    for detection in fired:
        assert detection.event_count == depth
        assert detection.last_t - detection.first_t <= window
        assert len({event.occurrence for event in detection.events}) == depth
    assert len(log.of(TraceOp.DETECT)) == len(fired)


@requires_example
@pytest.mark.integration
def test_the_compiled_predicate_is_the_documents_own_text(staged: Staged) -> None:
    detectors = compile_detectors(staged.cast, staged.model)
    document = staged.cast.document(detectors[0].detector_id)
    declared = [entry.get("predicate") for entry in document["spec"]["pattern"]]
    assert [entry.source for entry in detectors[0].elements] == declared


@requires_example
@pytest.mark.integration
def test_no_two_detections_of_one_subject_share_an_event(staged: Staged) -> None:
    detectors = compile_detectors(staged.cast, staged.model)
    fired = run_detectors(detectors=detectors, events=staged.events, trace=TraceLog())
    seen: Dict[int, List[int]] = {}
    for detection in fired:
        for event in detection.events:
            key = (event.ordinal, event.signal_id, event.occurrence)
            assert key not in seen, "an event was consumed by two detections"
            seen[key] = [1]
