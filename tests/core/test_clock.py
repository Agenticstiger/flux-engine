# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""KERNEL-SPEC §7: the virtual clock and the scheduler.

The properties pinned here:

* time is **integer milliseconds**, parsed from a declared RFC 3339 instant in UTC, and
  the rendering is the exact inverse of the parse;
* the scheduler pops in **whole-key order** -- ``(t_ms, phase, entity_ordinal,
  stream_ordinal, seq)`` -- for every insertion order, which is the property a set-backed
  queue fails;
* the key is **unique by construction**: ``seq`` comes from the scheduler's own counter,
  so no caller can mint two items with one key.

``assert_pops_in_key_order`` takes the scheduler factory under test, so
``test_negative_kernels_streams.py`` can point it at a set-iterating scheduler and show it
fires.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, List, Tuple

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from flux_engine.core import clock
from flux_engine.core.errors import FluxError

# simulation.flux.yml spec.timeBounds of the shipped telco example.
TELCO_START = "2026-07-01T00:00:00Z"
TELCO_END = "2026-09-30T23:59:59Z"

SchedulerFactory = Callable[[], "clock.Scheduler[str]"]

KeyTuple = Tuple[int, int, int, int, int]


# ---------------------------------------------------------------------------
# RFC 3339 <-> milliseconds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_epoch_is_the_unix_epoch() -> None:
    assert clock.parse_rfc3339_ms("1970-01-01T00:00:00Z") == 0
    assert clock.parse_rfc3339_ms("1970-01-01T00:00:00.001Z") == 1


@pytest.mark.unit
def test_an_offset_is_normalised_to_utc() -> None:
    assert clock.parse_rfc3339_ms("2026-07-01T02:00:00+02:00") == clock.parse_rfc3339_ms(
        TELCO_START
    )
    assert clock.parse_rfc3339_ms("2026-06-30T22:00:00-02:00") == clock.parse_rfc3339_ms(
        TELCO_START
    )


@pytest.mark.unit
def test_the_declared_time_bounds_span_the_quarter_they_name() -> None:
    """Computed from the declared instants, never transcribed."""
    start = clock.parse_rfc3339_ms(TELCO_START)
    end = clock.parse_rfc3339_ms(TELCO_END)
    expected = (
        datetime(2026, 10, 1, tzinfo=timezone.utc) - datetime(2026, 7, 1, tzinfo=timezone.utc)
    ) // timedelta(milliseconds=1) - 1000
    assert end - start == expected


@pytest.mark.property
@given(t_ms=st.integers(min_value=0, max_value=4_102_444_800_000))
def test_rendering_is_the_inverse_of_parsing(t_ms: int) -> None:
    assert clock.parse_rfc3339_ms(clock.format_rfc3339_ms(t_ms)) == t_ms


@pytest.mark.unit
def test_the_rendering_is_the_shape_the_seam_declares() -> None:
    """§9: RFC 3339 UTC with millisecond precision and ``Z``."""
    rendered = clock.format_rfc3339_ms(clock.parse_rfc3339_ms(TELCO_START))
    assert rendered == "2026-07-01T00:00:00.000Z"
    assert len(rendered) == len("2026-07-01T00:00:00.000Z")


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "2026-07-01",  # a date, not a date-time
        "2026-07-01T00:00:00",  # naive: no offset at all
        "2026-07-01 00:00:00Z",  # no T separator
        "2026-07-01T00:00:00.0001Z",  # sub-millisecond precision
        "2026-07-01T00:00:00+0200",  # offset without a colon
        "",
    ],
)
def test_a_timestamp_the_clock_cannot_represent_is_refused(text: str) -> None:
    with pytest.raises(FluxError) as caught:
        clock.parse_rfc3339_ms(text)
    assert caught.value.code.code == "FLX-5022"


@pytest.mark.unit
def test_trailing_zeros_below_the_millisecond_are_not_a_loss_of_precision() -> None:
    assert clock.parse_rfc3339_ms("2026-07-01T00:00:00.500000Z") == clock.parse_rfc3339_ms(
        "2026-07-01T00:00:00.5Z"
    )


@pytest.mark.unit
def test_a_non_string_timestamp_and_a_non_integer_instant_are_refused() -> None:
    with pytest.raises(FluxError):
        clock.parse_rfc3339_ms(20260701)  # type: ignore[arg-type]
    with pytest.raises(FluxError):
        clock.format_rfc3339_ms(1.5)  # type: ignore[arg-type]
    with pytest.raises(FluxError):
        clock.format_rfc3339_ms(True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


def telco_clock(tick_ms: int = clock.DEFAULT_TICK_MS) -> clock.VirtualClock:
    return clock.VirtualClock.from_time_bounds(TELCO_START, TELCO_END, tick_ms=tick_ms)


@pytest.mark.unit
def test_the_default_tick_is_the_declared_one_hour() -> None:
    assert clock.DEFAULT_TICK_MS == 3_600_000
    assert telco_clock().tick_ms == clock.DEFAULT_TICK_MS


@pytest.mark.unit
def test_the_tick_count_covers_the_declared_bounds() -> None:
    virtual = telco_clock()
    assert virtual.tick_count == virtual.duration_ms // virtual.tick_ms + 1
    assert virtual.tick_start(0) == virtual.start_ms
    assert virtual.contains(virtual.tick_start(virtual.tick_count - 1))
    assert not virtual.contains(virtual.end_ms + 1)
    assert len(list(virtual.ticks())) == virtual.tick_count


@pytest.mark.property
@given(offset=st.integers(min_value=0, max_value=7_948_799_000))
def test_every_instant_lands_in_exactly_one_tick(offset: int) -> None:
    virtual = telco_clock()
    t_ms = virtual.start_ms + offset
    assume(virtual.contains(t_ms))
    index = virtual.tick_index(t_ms)
    assert 0 <= index < virtual.tick_count
    assert virtual.tick_start(index) <= t_ms < virtual.tick_start(index) + virtual.tick_ms


@pytest.mark.unit
def test_an_instant_outside_the_bounds_is_refused() -> None:
    virtual = telco_clock()
    for outside in (virtual.start_ms - 1, virtual.end_ms + 1):
        with pytest.raises(FluxError) as caught:
            virtual.tick_index(outside)
        assert caught.value.code.code == "FLX-5022"
    with pytest.raises(FluxError):
        virtual.tick_start(virtual.tick_count)


@pytest.mark.unit
def test_a_clock_that_cannot_run_is_refused() -> None:
    with pytest.raises(FluxError):
        clock.VirtualClock(start_ms=10, end_ms=9)
    with pytest.raises(FluxError):
        clock.VirtualClock(start_ms=0, end_ms=10, tick_ms=0)


@pytest.mark.unit
def test_the_offset_is_measured_from_the_declared_start() -> None:
    virtual = telco_clock()
    assert virtual.offset_ms(virtual.start_ms) == 0
    assert virtual.offset_ms(virtual.end_ms) == virtual.duration_ms
    assert virtual.to_rfc3339(virtual.start_ms) == "2026-07-01T00:00:00.000Z"


# ---------------------------------------------------------------------------
# Document ordinals
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_document_ordinals_follow_nfc_sorted_ids_not_load_order() -> None:
    ids = ["sim-payment-recovery", "cam-payment-recovery", "q3-retention-world"]
    forward = clock.document_ordinals(ids)
    backward = clock.document_ordinals(list(reversed(ids)))
    assert forward == backward
    assert forward["cam-payment-recovery"] == 0
    assert sorted(forward.values()) == [0, 1, 2]


@pytest.mark.unit
def test_document_ordinals_order_by_the_nfc_form() -> None:
    """Raw code points would split the two spellings of one id across 'z-doc'."""
    composed = "\u00e9-doc"
    decomposed = "e\u0301-doc"
    assert decomposed < "z-doc" < composed
    assert clock.document_ordinals([decomposed, "z-doc"])["z-doc"] == 0
    assert clock.document_ordinals([composed, "z-doc"])["z-doc"] == 0


# ---------------------------------------------------------------------------
# The scheduling key
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_phase_enum_is_the_fixed_one_the_spec_names() -> None:
    assert [(phase.name, int(phase)) for phase in clock.Phase] == [
        ("SIGNAL", 0),
        ("DETECT", 1),
        ("JOURNEY", 2),
        ("CAMPAIGN", 3),
        ("EXPERIMENT", 4),
        ("EMIT", 5),
    ]


@pytest.mark.unit
def test_the_key_orders_by_time_then_phase_then_entity_then_stream_then_seq() -> None:
    def key(**overrides: int) -> clock.ScheduleKey:
        fields = dict(
            t_ms=100, phase=clock.Phase.JOURNEY, entity_ordinal=5, stream_ordinal=2, seq=9
        )
        fields.update(overrides)
        return clock.ScheduleKey(**fields)  # type: ignore[arg-type]

    base = key()
    assert key(t_ms=99) < base < key(t_ms=101)
    assert key(phase=clock.Phase.DETECT) < base < key(phase=clock.Phase.CAMPAIGN)
    assert key(entity_ordinal=4) < base < key(entity_ordinal=6)
    assert key(stream_ordinal=1) < base < key(stream_ordinal=3)
    assert key(seq=8) < base < key(seq=10)
    assert base.as_tuple() == (100, int(clock.Phase.JOURNEY), 5, 2, 9)


@pytest.mark.unit
def test_the_world_ordinal_sorts_ahead_of_every_subject() -> None:
    world = clock.ScheduleKey(0, clock.Phase.SIGNAL, clock.WORLD_ORDINAL, 0, 1)
    subject = clock.ScheduleKey(0, clock.Phase.SIGNAL, 0, 0, 0)
    assert clock.WORLD_ORDINAL == -1
    assert world < subject


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------

_ITEMS = [
    (t_ms, phase, entity, stream)
    for t_ms in (0, 1_000, 3_600_000)
    for phase in (clock.Phase.SIGNAL, clock.Phase.JOURNEY, clock.Phase.EMIT)
    for entity in (clock.WORLD_ORDINAL, 0, 7)
    for stream in (0, 3)
]


def assert_pops_in_key_order(make_scheduler: SchedulerFactory, insertion_order: List[int]) -> None:
    """Every pop is greater than the one before it, for any insertion order.

    The property a binary heap over the whole key satisfies and a set-backed queue does
    not: the pop order is decided by the key, not by how the queue happened to store it.
    """
    scheduler = make_scheduler()
    for index in insertion_order:
        t_ms, phase, entity, stream = _ITEMS[index]
        scheduler.schedule(
            t_ms=t_ms,
            phase=phase,
            entity_ordinal=entity,
            stream_ordinal=stream,
            payload=f"item-{index}",
        )
    popped = [item.key.as_tuple() for item in scheduler.drain()]
    assert popped == sorted(popped)
    assert len(popped) == len(insertion_order)
    assert len(set(popped)) == len(popped)


@pytest.mark.property
@settings(max_examples=50)
@given(
    order=st.lists(
        st.integers(min_value=0, max_value=len(_ITEMS) - 1), min_size=1, max_size=len(_ITEMS)
    )
)
def test_the_scheduler_pops_in_key_order_for_any_insertion_order(order: List[int]) -> None:
    assert_pops_in_key_order(lambda: clock.Scheduler(), order)


@pytest.mark.unit
def test_seq_is_assigned_by_the_scheduler_and_makes_every_key_unique() -> None:
    scheduler: clock.Scheduler[str] = clock.Scheduler()
    keys = [
        scheduler.schedule(
            t_ms=0, phase=clock.Phase.SIGNAL, entity_ordinal=0, stream_ordinal=0, payload="same"
        )
        for _ in range(50)
    ]
    assert len({key.as_tuple() for key in keys}) == 50
    assert [key.seq for key in keys] == list(range(50))
    assert scheduler.next_seq == 50


@pytest.mark.unit
def test_items_scheduled_while_draining_are_drained_too() -> None:
    scheduler: clock.Scheduler[str] = clock.Scheduler()
    scheduler.schedule(
        t_ms=0, phase=clock.Phase.SIGNAL, entity_ordinal=0, stream_ordinal=0, payload="first"
    )
    seen = []
    for item in scheduler.drain():
        seen.append(item.payload)
        if item.payload == "first":
            scheduler.schedule(
                t_ms=10,
                phase=clock.Phase.EMIT,
                entity_ordinal=0,
                stream_ordinal=0,
                payload="second",
            )
    assert seen == ["first", "second"]


@pytest.mark.unit
def test_peek_does_not_consume_and_an_empty_pop_is_refused() -> None:
    scheduler: clock.Scheduler[str] = clock.Scheduler()
    assert scheduler.peek() is None
    assert not scheduler
    scheduler.schedule(
        t_ms=5, phase=clock.Phase.DETECT, entity_ordinal=1, stream_ordinal=0, payload="only"
    )
    assert len(scheduler) == 1
    assert scheduler.peek() is not None and scheduler.peek().payload == "only"
    assert len(scheduler) == 1
    assert scheduler.pop().payload == "only"
    with pytest.raises(FluxError) as caught:
        scheduler.pop()
    assert caught.value.code.code == "FLX-5022"


@pytest.mark.unit
def test_the_scheduler_refuses_an_item_it_cannot_order() -> None:
    scheduler: clock.Scheduler[str] = clock.Scheduler()
    with pytest.raises(FluxError):
        scheduler.schedule(
            t_ms=1.5,  # type: ignore[arg-type]
            phase=clock.Phase.SIGNAL,
            entity_ordinal=0,
            stream_ordinal=0,
            payload="x",
        )
    with pytest.raises(FluxError):
        scheduler.schedule(
            t_ms=0,
            phase=0,  # type: ignore[arg-type]
            entity_ordinal=0,
            stream_ordinal=0,
            payload="x",
        )
    with pytest.raises(FluxError):
        scheduler.schedule(
            t_ms=0,
            phase=clock.Phase.SIGNAL,
            entity_ordinal=-2,
            stream_ordinal=0,
            payload="x",
        )
    with pytest.raises(FluxError):
        scheduler.schedule(
            t_ms=0,
            phase=clock.Phase.SIGNAL,
            entity_ordinal=0,
            stream_ordinal=-1,
            payload="x",
        )


@pytest.mark.unit
def test_a_payload_never_decides_the_order() -> None:
    """Two items at one instant whose payloads are not orderable at all."""

    class Unorderable:
        def __lt__(self, other: object) -> bool:  # pragma: no cover - must never be called
            raise AssertionError("the payload was compared")

    scheduler: clock.Scheduler[Unorderable] = clock.Scheduler()
    for _ in range(20):
        scheduler.schedule(
            t_ms=0,
            phase=clock.Phase.SIGNAL,
            entity_ordinal=0,
            stream_ordinal=0,
            payload=Unorderable(),
        )
    assert len(list(scheduler.drain())) == 20


# ---------------------------------------------------------------------------
# RunClock
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_run_clock_refuses_an_item_outside_the_declared_bounds() -> None:
    run = clock.RunClock(clock=telco_clock())
    inside = run.schedule(
        t_ms=run.clock.start_ms,
        phase=clock.Phase.SIGNAL,
        entity_ordinal=0,
        stream_ordinal=0,
        payload="ok",
    )
    assert inside.seq == 0
    with pytest.raises(FluxError) as caught:
        run.schedule(
            t_ms=run.clock.end_ms + 1,
            phase=clock.Phase.SIGNAL,
            entity_ordinal=0,
            stream_ordinal=0,
            payload="late",
        )
    assert caught.value.code.code == "FLX-5022"


@pytest.mark.unit
def test_nothing_in_the_module_reads_the_host_clock() -> None:
    """An independent check of the same rule the architecture test enforces by AST scan."""
    import inspect

    source = inspect.getsource(clock)
    assert "datetime.now" not in source
    assert "time.time" not in source
    assert "utcnow" not in source


@pytest.mark.unit
def test_items_compare_by_key_alone() -> None:
    """The heap never reaches this comparison, because the key is unique -- but if the
    key ever stopped being unique, ordering by the payload is the failure mode to avoid."""
    earlier = clock.ScheduledItem(
        key=clock.ScheduleKey(0, clock.Phase.SIGNAL, 0, 0, 0), payload="zzz"
    )
    later = clock.ScheduledItem(
        key=clock.ScheduleKey(0, clock.Phase.SIGNAL, 0, 0, 1), payload="aaa"
    )
    assert earlier < later
    assert not later < earlier
