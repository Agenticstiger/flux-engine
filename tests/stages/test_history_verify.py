# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The lines a history adds to verify's rules (ADR 0017 section 14), on hand-made written rows.

Rows that hold every line pass, and each violation of each line is one failing entity, under both
chain modes and both end modes. The lines count entities, and they never read a draw.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from flux_engine.concordance.history_checks import (
    ENDED_IS_LAST,
    NO_GAPS_NO_OVERLAP,
    ONE_CURRENT,
    history_shares,
)
from flux_engine.concordance.shares import Holds
from flux_engine.expr2.scope import Versions
from flux_engine.seam.history import Histogram, HistoryEnd, HistoryEntry

VERSIONS = Versions(("sub",), "valid_from", "valid_to")
OPEN = date(9999, 12, 31)


def day(n: int) -> date:
    return date(2024, 1, 1) + timedelta(days=n)


def entry(
    chain: str = "nextStart",
    end: Optional[HistoryEnd] = None,
    open_end: Optional[str] = None,
) -> HistoryEntry:
    return HistoryEntry(
        emit="sub",
        entities=1,
        length=Histogram(("1", "2"), ("1",)),
        until="2025-01-01",
        chain=chain,
        end=end,
        open_end=open_end,
    )


def row(sub: str, start: Any, stop: Any, status: str = "active") -> Dict[str, Any]:
    return {"sub": sub, "valid_from": start, "valid_to": stop, "status": status}


def lines(
    rows: List[Dict[str, Any]], held: HistoryEntry, versions: Versions = VERSIONS
) -> Dict[str, Holds]:
    return dict(history_shares(held, rows, versions))


def fails(found: Dict[str, Holds], rule: str) -> bool:
    return found[rule].satisfied < found[rule].total


CLOSE = HistoryEnd("closeLast", "0.5", "status", "ended")
FINAL = HistoryEnd("finalVersion", "0.5", "status", "ended")


def test_a_next_start_chain_that_holds_passes_every_line() -> None:
    rows = [row("a", day(0), day(5)), row("a", day(5), None), row("b", day(1), None)]
    found = lines(rows, entry())
    assert found == {NO_GAPS_NO_OVERLAP: Holds(2, 2), ONE_CURRENT: Holds(2, 2)}


def test_a_day_before_chain_that_holds_passes_with_one_day_versions() -> None:
    rows = [row("a", day(0), day(0)), row("a", day(1), day(4)), row("a", day(5), None)]
    found = lines(rows, entry("dayBefore"))
    assert found[NO_GAPS_NO_OVERLAP] == Holds(1, 1)
    assert found[ONE_CURRENT] == Holds(1, 1)


def test_rows_in_any_order_are_ordered_by_start() -> None:
    rows = [row("a", day(5), None), row("a", day(0), day(5))]
    assert lines(rows, entry())[NO_GAPS_NO_OVERLAP] == Holds(1, 1)


def test_the_open_end_sentinel_is_an_open_version() -> None:
    rows = [row("a", day(0), day(3)), row("a", day(3), OPEN)]
    found = lines(rows, entry(open_end="9999-12-31"))
    assert found[NO_GAPS_NO_OVERLAP] == Holds(1, 1)
    assert found[ONE_CURRENT] == Holds(1, 1)


def test_timestamp_chains_pass_and_fail_the_same_way() -> None:
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    ms = timedelta(milliseconds=1)
    good = [row("a", t, t + 5 * ms), row("a", t + 5 * ms, None)]
    gap = [row("a", t, t + 5 * ms), row("a", t + 6 * ms, None)]
    assert lines(good, entry())[NO_GAPS_NO_OVERLAP] == Holds(1, 1)
    assert fails(lines(gap, entry()), NO_GAPS_NO_OVERLAP)


@pytest.mark.parametrize("chain", ["nextStart", "dayBefore"])
def test_a_gap_fails_no_gaps_no_overlap(chain: str) -> None:
    follow = day(6) if chain == "nextStart" else day(7)
    rows = [row("a", day(0), day(5)), row("a", follow, None), row("b", day(0), None)]
    assert lines(rows, entry(chain))[NO_GAPS_NO_OVERLAP] == Holds(1, 2)


@pytest.mark.parametrize("chain", ["nextStart", "dayBefore"])
def test_an_overlap_fails_no_gaps_no_overlap(chain: str) -> None:
    rows = [row("a", day(0), day(5)), row("a", day(3), None)]
    assert fails(lines(rows, entry(chain)), NO_GAPS_NO_OVERLAP)


def test_next_start_refuses_a_zero_length_version_and_day_before_allows_it() -> None:
    zero = [row("a", day(0), day(0)), row("a", day(0), None)]
    assert fails(lines(zero, entry("nextStart")), NO_GAPS_NO_OVERLAP)
    one_day = [row("a", day(0), day(0)), row("a", day(1), None)]
    assert not fails(lines(one_day, entry("dayBefore")), NO_GAPS_NO_OVERLAP)


@pytest.mark.parametrize("chain", ["nextStart", "dayBefore"])
def test_an_end_before_its_start_fails(chain: str) -> None:
    rows = [row("a", day(5), day(2)), row("a", day(3), None)]
    assert fails(lines(rows, entry(chain)), NO_GAPS_NO_OVERLAP)


def test_the_day_before_chain_refuses_the_next_start_join() -> None:
    rows = [row("a", day(0), day(5)), row("a", day(5), None)]
    assert fails(lines(rows, entry("dayBefore")), NO_GAPS_NO_OVERLAP)


def test_an_open_version_that_is_not_last_fails_both_lines() -> None:
    rows = [row("a", day(0), None), row("a", day(5), None)]
    found = lines(rows, entry())
    assert fails(found, NO_GAPS_NO_OVERLAP) and fails(found, ONE_CURRENT)


def test_a_missing_start_fails() -> None:
    rows = [row("a", None, day(5)), row("a", day(5), None)]
    assert fails(lines(rows, entry()), NO_GAPS_NO_OVERLAP)


def test_an_entity_with_no_open_version_and_no_end_fails_one_current() -> None:
    rows = [row("a", day(0), day(5)), row("a", day(5), day(9))]
    found = lines(rows, entry())
    assert fails(found, ONE_CURRENT) and not fails(found, NO_GAPS_NO_OVERLAP)


def test_a_plain_end_does_not_allow_a_closed_last_version() -> None:
    rows = [row("a", day(0), day(5)), row("a", day(5), day(9), "ended")]
    assert fails(lines(rows, entry(end=FINAL)), ONE_CURRENT)


def test_close_last_allows_an_ended_entity_with_no_open_version() -> None:
    rows = [
        row("a", day(0), day(5)),
        row("a", day(5), day(9), "ended"),
        row("b", day(0), None),
    ]
    found = lines(rows, entry(end=CLOSE))
    assert found[ONE_CURRENT] == Holds(2, 2)
    assert found[ENDED_IS_LAST] == Holds(2, 2)


def test_close_last_without_a_column_allows_a_closed_last_version() -> None:
    rows = [row("a", day(0), day(5)), row("a", day(5), day(9))]
    found = lines(rows, entry(end=HistoryEnd("closeLast", "0.5")))
    assert found == {NO_GAPS_NO_OVERLAP: Holds(1, 1), ONE_CURRENT: Holds(1, 1)}


def test_close_last_closed_without_the_value_on_its_last_version_fails() -> None:
    rows = [row("a", day(0), day(5)), row("a", day(5), day(9), "active")]
    assert fails(lines(rows, entry(end=CLOSE)), ONE_CURRENT)


def test_close_last_with_two_open_versions_fails() -> None:
    rows = [row("a", day(0), None), row("a", day(5), None, "ended")]
    assert fails(lines(rows, entry(end=CLOSE)), ONE_CURRENT)


def test_final_version_keeps_one_open_version_carrying_the_value() -> None:
    rows = [row("a", day(0), day(5)), row("a", day(5), None, "ended"), row("b", day(0), None)]
    found = lines(rows, entry(end=FINAL))
    assert found[ONE_CURRENT] == Holds(2, 2)
    assert found[ENDED_IS_LAST] == Holds(2, 2)


def test_final_version_with_its_last_version_closed_fails_one_current() -> None:
    rows = [row("a", day(0), day(5)), row("a", day(5), day(9), "ended")]
    assert fails(lines(rows, entry(end=FINAL)), ONE_CURRENT)


@pytest.mark.parametrize("end", [CLOSE, FINAL])
def test_the_value_on_an_earlier_version_fails_ended_is_last(end: HistoryEnd) -> None:
    rows = [
        row("a", day(0), day(5), "ended"),
        row("a", day(5), None, "ended"),
        row("b", day(0), None),
    ]
    found = lines(rows, entry(end=end))
    assert found[ENDED_IS_LAST] == Holds(1, 2)


def test_the_ended_line_is_present_only_with_a_column_and_a_value() -> None:
    rows = [row("a", day(0), None)]
    assert ENDED_IS_LAST not in lines(rows, entry())
    assert ENDED_IS_LAST not in lines(rows, entry(end=HistoryEnd("closeLast", "0.5")))
    assert ENDED_IS_LAST in lines(rows, entry(end=CLOSE))


def test_a_composite_natural_entity_groups_on_every_column() -> None:
    versions = Versions(("org", "sub"), "valid_from", "valid_to")
    rows = [
        {"org": "x", "sub": "a", "valid_from": day(0), "valid_to": None},
        {"org": "y", "sub": "a", "valid_from": day(0), "valid_to": None},
    ]
    assert lines(rows, entry(), versions)[ONE_CURRENT] == Holds(2, 2)


def test_every_violating_entity_is_counted_once() -> None:
    rows = [row("a", day(0), day(5)), row("a", day(7), day(9)), row("a", day(9), None)]
    rows += [row("b", day(0), day(2)), row("b", day(2), day(4)), row("c", day(0), None)]
    found = lines(rows, entry())
    assert found[NO_GAPS_NO_OVERLAP] == Holds(2, 3)
    assert found[ONE_CURRENT] == Holds(2, 3)


def test_no_rows_hold_no_entities() -> None:
    assert lines([], entry())[ONE_CURRENT] == Holds(0, 0)
