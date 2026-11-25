# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Kafka sink's pace: parsing it, ordering a paced stream and waiting for each record."""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks._kafka_pace import Pace, Pacer, parse_pace, time_order
from flux_engine.sinks._kafka_records import KafkaRecord


class FakeClock:
    """A monotonic clock that only the fake sleep moves."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: List[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def record(
    key: str, timestamp_ms: int, row: int, *, value: Optional[Dict[str, Any]]
) -> KafkaRecord:
    return KafkaRecord(
        key=key.encode(),
        value=value,
        timestamp_ms=timestamp_ms,
        headers=(),
        row=row,
        entity=key.encode(),
    )


def live(key: str, timestamp_ms: int, row: int) -> KafkaRecord:
    return record(key, timestamp_ms, row, value={"row": row})


def test_parse_pace_forms() -> None:
    assert parse_pace(None) == Pace("max", None)
    assert parse_pace("max") == Pace("max", None)
    assert parse_pace("realtime") == Pace("realtime", Fraction(1))
    assert parse_pace("x2").speed == Fraction(2)
    assert parse_pace("x0.5").speed == Fraction(1, 2)
    assert parse_pace("x3600").speed == Fraction(3600)
    assert parse_pace("x1.25").speed == Fraction(5, 4)


def test_paced_is_false_only_for_max() -> None:
    assert not parse_pace("max").paced
    assert parse_pace("realtime").paced
    assert parse_pace("x2").paced


@pytest.mark.parametrize(
    "text",
    ["", "fast", "x", "x0", "x0.0", "x-2", "x2x", "2", "X2", "x1e3", "x 2", "realtime ", "xNaN"],
)
def test_a_pace_that_is_not_a_form_is_refused(text: str) -> None:
    with pytest.raises(FluxError) as caught:
        parse_pace(text)
    assert caught.value.code.code == "FLX-3504"
    assert "pace" in str(caught.value) and "realtime" in str(caught.value)


def test_time_order_sorts_across_keys_by_event_time() -> None:
    records = [live("a", 300, 0), live("b", 100, 1), live("c", 200, 2)]
    assert time_order(records) == [1, 2, 0]


def test_time_order_keeps_each_keys_own_order() -> None:
    # a's second record is earlier in event time than its first, yet it stays after it.
    records = [live("a", 300, 0), live("b", 200, 1), live("a", 100, 2), live("b", 250, 3)]
    order = time_order(records)
    assert order == [1, 3, 0, 2]
    for key in (b"a", b"b"):
        rows = [records[index].row for index in order if records[index].key == key]
        assert rows == sorted(rows)


def test_time_order_is_stable_for_ties() -> None:
    records = [live("a", 100, 0), live("b", 100, 1), live("c", 100, 2)]
    assert time_order(records) == [0, 1, 2]


def test_a_tombstone_stays_right_after_its_delete() -> None:
    records = [
        live("a", 500, 0),
        live("b", 100, 1),
        record("b", 100, 1, value=None),  # b's tombstone
        live("c", 100, 2),
    ]
    assert time_order(records) == [1, 2, 3, 0]


def test_a_tombstone_follows_its_delete_when_the_key_was_late() -> None:
    records = [
        live("a", 400, 0),
        live("a", 100, 1),
        record("a", 100, 1, value=None),
        live("b", 450, 2),
    ]
    order = time_order(records)
    assert order == [0, 1, 2, 3]


def test_time_order_is_deterministic_and_a_permutation() -> None:
    records = [live("k%d" % (i % 3), (i * 7919) % 1000, i) for i in range(40)]
    first = time_order(records)
    assert first == time_order(list(records))
    assert sorted(first) == list(range(40))


def test_time_order_of_nothing_is_nothing() -> None:
    assert time_order([]) == []


def make(text: str, clock: FakeClock) -> Pacer:
    return Pacer(parse_pace(text), monotonic=clock.monotonic, sleep=clock.sleep)


def test_realtime_waits_the_event_time_offset() -> None:
    clock = FakeClock()
    pacer = make("realtime", clock)
    pacer.start(10_000)
    assert pacer.delay(10_000) == 0.0
    assert pacer.delay(12_500) == pytest.approx(2.5)
    assert pacer.wait(12_500) == pytest.approx(2.5)
    assert clock.slept == [pytest.approx(2.5)]
    assert pacer.delay(12_500) == 0.0


def test_x2_runs_twice_as_fast() -> None:
    clock = FakeClock()
    pacer = make("x2", clock)
    pacer.start(0)
    assert pacer.delay(10_000) == pytest.approx(5.0)


def test_x0_5_runs_at_half_speed() -> None:
    clock = FakeClock()
    pacer = make("x0.5", clock)
    pacer.start(0)
    assert pacer.delay(10_000) == pytest.approx(20.0)


def test_a_late_sender_does_not_sleep() -> None:
    clock = FakeClock()
    pacer = make("realtime", clock)
    pacer.start(0)
    clock.now += 100.0
    assert pacer.delay(5_000) == 0.0
    assert pacer.wait(5_000) == 0.0
    assert clock.slept == []


def test_waits_are_counted_from_the_anchor_not_from_each_other() -> None:
    clock = FakeClock()
    pacer = make("realtime", clock)
    pacer.start(0)
    pacer.wait(1_000)
    pacer.wait(3_000)
    assert clock.now == pytest.approx(1003.0)
    assert sum(clock.slept) == pytest.approx(3.0)


def test_max_never_sleeps() -> None:
    clock = FakeClock()
    pacer = make("max", clock)
    pacer.start(0)
    assert pacer.delay(10**12) == 0.0
    assert pacer.wait(10**12) == 0.0
    assert clock.slept == []


def test_a_day_of_event_time_at_x3600_is_24_seconds() -> None:
    clock = FakeClock()
    pacer = make("x3600", clock)
    pacer.start(0)
    assert pacer.delay(86_400_000) == pytest.approx(24.0)


def test_negative_event_times_are_waited_for_from_the_first() -> None:
    clock = FakeClock()
    pacer = make("realtime", clock)
    pacer.start(-86_400_000)
    assert pacer.delay(-86_399_000) == pytest.approx(1.0)
