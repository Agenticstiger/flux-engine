# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The arithmetic of seasonal rates and their thinning (ADR 0025 section 2).

``lambda`` is read for known instants against ``datetime`` (UTC month, ISO weekday, hour of week; a
DATE is its day), ``lambda_max`` is the product of each given factor's largest weight, the acceptance
rule is checked on exact integers at its boundary, and the thinning is held to the positions the ADR
gives it: proposal ``k`` at ``1 + 2k``, its test at ``2 + 2k``, 128 proposals, a NULL proposal
accepted at once, and proposal 0 when none is accepted.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from typing import List, Optional

import pytest

from flux_engine.core import kernel2
from flux_engine.core.seedtree import derive_root
from flux_engine.generator.dates import DAY_MS, KIND_DATE, KIND_TIMESTAMP, UNIFORM53, DatesTally
from flux_engine.generator.paths import CellStreams
from flux_engine.generator.rates import PROPOSALS, Rates, civil_month, thin, weekday_of

pytestmark = pytest.mark.unit

MONTHS = tuple(str(n) for n in range(1, 13))
WEEKDAYS = tuple(str(n) for n in range(1, 8))
HOURS = tuple(str(n) for n in range(1, 169))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(moment: datetime) -> int:
    return (moment - EPOCH) // timedelta(milliseconds=1)


def test_lambda_of_a_timestamp_reads_month_weekday_and_hour_of_week_in_utc() -> None:
    rates = Rates("c", KIND_TIMESTAMP, MONTHS, WEEKDAYS, HOURS)
    # Wednesday 5 March 2025, 14:30 UTC: month 3, weekday 3 (Monday is 1), hour of week 2 * 24 + 14.
    moment = datetime(2025, 3, 5, 14, 30, tzinfo=timezone.utc)
    assert rates.lam(_ms(moment)) == 3 * 3 * (2 * 24 + 14 + 1)
    # The last millisecond of Sunday 31 December 2023 and the first of Monday 1 January 2024.
    last = datetime(2023, 12, 31, 23, 59, 59, 999000, tzinfo=timezone.utc)
    assert rates.lam(_ms(last)) == 12 * 7 * 168
    assert rates.lam(_ms(last) + 1) == 1 * 1 * 1


def test_lambda_agrees_with_datetime_for_many_instants() -> None:
    rates = Rates("c", KIND_TIMESTAMP, MONTHS, WEEKDAYS, HOURS)
    moment = datetime(1969, 12, 30, 23, 0, tzinfo=timezone.utc)
    for step in range(0, 24 * 366 * 6, 7):
        here = moment + timedelta(hours=step)
        expected = here.month * (here.weekday() + 1) * (here.weekday() * 24 + here.hour + 1)
        assert rates.lam(_ms(here)) == expected


def test_lambda_of_a_date_is_its_day_with_no_hour() -> None:
    rates = Rates("c", KIND_DATE, MONTHS, WEEKDAYS)
    for offset in range(-800, 800, 13):
        day = date(2000, 2, 28) + timedelta(days=offset)
        found = rates.lam((day - date(1970, 1, 1)).days)
        assert found == day.month * (day.weekday() + 1)


def test_a_missing_factor_counts_as_one() -> None:
    only_weekday = Rates("c", KIND_TIMESTAMP, day_of_week=("1", "2", "3", "4", "5", "6", "7"))
    friday = datetime(2025, 6, 6, 3, 0, tzinfo=timezone.utc)
    assert only_weekday.lam(_ms(friday)) == 5
    assert only_weekday.intensity.maximum == 7


def test_the_month_and_weekday_helpers_hold_across_the_calendar() -> None:
    for days in range(-800_000, 800_000, 997):
        shown = date.fromordinal(days + 719_163) if 1 <= days + 719_163 <= 3_652_059 else None
        if shown is None:
            continue
        assert civil_month(days) == shown.month
        assert weekday_of(days) == shown.weekday()
    # outside the years a DATE holds there is still a month
    assert 1 <= civil_month(10**9) <= 12 and 1 <= civil_month(-(10**9)) <= 12


def test_lambda_max_is_the_product_of_each_given_factors_largest_weight() -> None:
    rates = Rates(
        "c",
        KIND_TIMESTAMP,
        month=("1", "0.5", "3") + ("1",) * 9,
        day_of_week=("2",) + ("1",) * 6,
        hour_of_week=("0.25",) * 167 + ("5",),
    )
    assert rates.intensity.maximum == Fraction(3) * 2 * 5
    assert Rates("c", KIND_DATE, day_of_week=("1",) * 7).intensity.maximum == 1


def test_the_acceptance_rule_is_exact_at_its_boundary() -> None:
    # lambda_max 1, lambda 1/2: accepted iff a < 2**52.
    half = Rates("c", KIND_DATE, day_of_week=("1",) + ("0.5",) * 6)
    monday, tuesday = 4, 5  # 1970-01-01 is a Thursday, so day 4 is a Monday
    assert half.lam(monday) == 1 and half.lam(tuesday) == Fraction(1, 2)
    assert half.accepts(2**52 - 1, tuesday) and not half.accepts(2**52, tuesday)
    assert half.accepts(UNIFORM53 - 1, monday)  # lambda = lambda_max accepts every a
    # lambda_max 3, lambda 2: accepted iff 3a < 2 * 2**53, a <= 6004799503160661.
    third = Rates("c", KIND_DATE, day_of_week=("3", "2") + ("0",) * 5)
    assert third.lam(monday) == 3 and third.lam(tuesday) == 2
    assert third.accepts(6004799503160661, tuesday)
    assert not third.accepts(6004799503160662, tuesday)
    # intensity 0 is never accepted, not even by a = 0
    assert not third.accepts(0, 6) and third.lam(6) == 0


# -- the positions ---------------------------------------------------------------------------


def _streams() -> CellStreams:
    seam = hashlib.sha256(b"rates arithmetic").digest()
    root = derive_root(seam, 7, seed_tree_version=kernel2.SEED_TREE_VERSION)
    return CellStreams(root, "s", "e")


def _expected(rates: Rates, row: int, low: int, high: int) -> tuple[Optional[int], bool, int]:
    """Thinning written out with one stream per position: proposal ``k`` at ``1 + 2k``, its test at ``2 + 2k``."""
    streams = _streams()
    first = None
    for k in range(PROPOSALS):
        value = low + streams.dates_stream_at(row, "d", 1 + 2 * k).bounded(high - low + 1)
        a = streams.dates_stream_at(row, "d", 2 + 2 * k).uniform53()
        if k == 0:
            first = value
        if rates.accepts(a, value):
            return value, False, k
    return first, True, PROPOSALS


def test_proposal_k_sits_at_one_plus_two_k_and_its_test_at_two_plus_two_k() -> None:
    # Only a Monday is accepted, so rows are accepted after some rejections.
    rates = Rates("d", KIND_DATE, day_of_week=("1", "0", "0", "0", "0", "0", "0"))
    low, high = 20_089, 20_119
    streams = _streams()
    later = 0
    for row in range(40):
        stream = streams.dates_stream_at(row, "d", 1)
        first = low + stream.bounded(high - low + 1)
        found, fell_back = thin(
            rates, stream, first, lambda stream=stream: low + stream.bounded(high - low + 1)
        )
        value, expected_back, index = _expected(rates, row, low, high)
        assert (found, fell_back) == (value, expected_back)
        later += index > 0
    assert later > 0


class _Counting:
    """A stream that counts its calls; ``uniform53`` always returns a test that rejects (or accepts)."""

    def __init__(self, test: int) -> None:
        self.test = test
        self.tests = 0
        self.redraws = 0

    def uniform53(self) -> int:
        self.tests += 1
        return self.test

    def redraw(self) -> Optional[int]:
        self.redraws += 1
        return 4  # a Monday


def test_a_row_stops_at_its_first_acceptance() -> None:
    rates = Rates("d", KIND_DATE, day_of_week=("1",) + ("0",) * 6)
    stream = _Counting(test=0)
    # proposal 0 is a Tuesday (rejected), proposal 1 a Monday (accepted by a = 0)
    assert thin(rates, stream, 5, stream.redraw) == (4, False)
    assert (stream.tests, stream.redraws) == (2, 1)


def test_the_value_is_proposal_zero_after_128_rejections_and_the_row_falls_back() -> None:
    # Every value has intensity 0 on a Sunday-only weekday factor, whatever the draw.
    rates = Rates("d", KIND_DATE, day_of_week=("0",) * 6 + ("1",))
    stream = _Counting(test=0)
    assert PROPOSALS == 128
    value, fell_back = thin(rates, stream, 5, stream.redraw)
    assert (value, fell_back) == (5, True)
    assert stream.tests == 128 and stream.redraws == 127


def test_a_column_whose_every_value_has_intensity_zero_falls_back_on_every_row() -> None:
    rates = Rates("d", KIND_DATE, day_of_week=("0", "0", "0", "0", "0", "0", "1"))
    monday = 4
    streams = _streams()  # an interval of one day, a Monday: its intensity is 0
    for row in range(5):
        stream = streams.dates_stream_at(row, "d", 1)
        first = monday + stream.bounded(1)
        found = thin(rates, stream, first, lambda stream=stream: monday + stream.bounded(1))
        assert found == (monday, True)


def test_a_null_proposal_is_accepted_at_once_and_draws_no_test() -> None:
    rates = Rates("d", KIND_DATE, day_of_week=("0", "0", "0", "0", "0", "0", "1"))
    stream = _Counting(test=0)
    assert thin(rates, stream, None, stream.redraw) == (None, False)
    assert (stream.tests, stream.redraws) == (0, 0)
    # a later NULL proposal is accepted at once as well

    class Later(_Counting):
        def redraw(self) -> Optional[int]:
            self.redraws += 1
            return None

    later = Later(test=0)
    assert thin(rates, later, 5, later.redraw) == (None, False)
    assert (later.tests, later.redraws) == (1, 1)


def test_a_timestamp_proposal_one_day_later_has_the_next_weekday() -> None:
    rates = Rates("t", KIND_TIMESTAMP, day_of_week=("1", "2", "3", "4", "5", "6", "7"))
    monday = _ms(datetime(2025, 1, 6, 12, tzinfo=timezone.utc))
    assert [rates.lam(monday + n * DAY_MS) for n in range(8)] == [1, 2, 3, 4, 5, 6, 7, 1]


def test_the_tally_adds_the_rates_outcome_only_when_asked() -> None:
    plain = DatesTally(["a"])
    assert "ratesFallbacks" not in plain.as_document()["a"]
    seasonal = DatesTally(["a"], rates=True)
    seasonal.add("a", "ratesFallbacks")
    other = DatesTally(["a", "b"], rates=True)
    other.add("b", "held")
    total = DatesTally()
    for part in (seasonal, other):
        total.merge(part)
    found = total.as_document()
    assert found["a"]["ratesFallbacks"] == 1 and found["b"]["held"] == 1
    assert list(found["a"]) == [*DatesTally(["a"]).as_document()["a"], "ratesFallbacks"]


def test_the_document_lists_each_factor_only_when_given() -> None:
    assert Rates("c", KIND_DATE, month=MONTHS).as_document() == {
        "column": "c",
        "kind": KIND_DATE,
        "month": list(MONTHS),
    }
    both: List[str] = list(Rates("c", KIND_TIMESTAMP, None, WEEKDAYS, HOURS).as_document())
    assert both == ["column", "kind", "dayOfWeek", "hourOfWeek"]


# -- the weights are parsed when the Rates is built --------------------------------------------------


def test_lambda_and_lambda_max_are_unchanged_by_parsing_up_front():
    rates = Rates(
        "c",
        KIND_TIMESTAMP,
        month=("1", "2", "0.5") + ("1",) * 9,
        day_of_week=("1", "3", "1", "1", "1", "1", "5"),
        hour_of_week=tuple(str(1 + hour % 4) for hour in range(168)),
    )
    assert rates.intensity.maximum == Fraction(2) * 5 * 4
    moment = datetime(2024, 2, 3, 13, tzinfo=timezone.utc)  # a Saturday in February
    value = int(moment.timestamp()) * 1000
    expected = Fraction(2) * 1 * (1 + (5 * 24 + 13) % 4)
    assert rates.lam(value) == expected
    assert rates.intensity is rates.intensity
    assert Rates("c", KIND_DATE, day_of_week=("1",) * 7).intensity.maximum == 1


def test_building_a_rates_parses_its_weights_once(monkeypatch):
    import flux_engine.generator.rates as module

    parsed: List[str] = []
    real = module.Fraction

    class Counting(real):  # type: ignore[valid-type, misc]
        def __new__(cls, *args, **kwargs):
            if args and isinstance(args[0], str):
                parsed.append(args[0])
            return super().__new__(cls, *args, **kwargs)

    monkeypatch.setattr(module, "Fraction", Counting)
    rates = Rates("c", KIND_DATE, month=("1",) * 12, day_of_week=("2",) * 7)
    built = len(parsed)
    assert built == 19
    for value in range(0, 400, 7):
        rates.lam(value)
        rates.accepts(12345, value)
    assert len(parsed) == built


def test_many_rated_columns_cost_no_more_per_call_than_a_few():
    import time

    def build(count):
        return [
            Rates(f"c{i}", KIND_TIMESTAMP, month=("1",) * 12, hour_of_week=("3",) * 168)
            for i in range(count)
        ]

    def per_call(rates):
        start = time.perf_counter()
        for _ in range(200):
            for each in rates:
                each.lam(1_700_000_000_000)
                _ = each.intensity.maximum
        return (time.perf_counter() - start) / (200 * len(rates))

    few, many = build(3), build(300)
    per_call(few)
    assert per_call(many) < 3 * per_call(few) + 2e-6
