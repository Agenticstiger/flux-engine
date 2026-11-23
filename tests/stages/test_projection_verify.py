# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The lines a projection adds to verify's rules (ADR 0027 section 10), on hand-made written rows.

Rows that hold every line pass, and each violation of each line is one failing entity or row. The
lines read the projected rows and the history's rows as written and never regenerate a row.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import pytest
import yaml

from flux_engine.concordance.history_checks import chain_ended
from flux_engine.concordance.projection_checks import (
    EVENT_MATCHES_TRANSITION,
    EVENT_TRANSITION_MATCHES,
    PERIOD_MATCHES_VALID_VERSION,
    PERIOD_WITHIN_VALIDITY,
    SNAPSHOT_EQUALS_LAST_VERSION,
    entity_columns,
    projected_history,
    projection_entry_of,
    projection_shares,
    window_of,
)
from flux_engine.concordance.rule_shares import RuleSource, rule_shares, tables_needed
from flux_engine.concordance.shares import Holds
from flux_engine.expr2.scope import Versions
from flux_engine.generator.rules import model_from_contract
from flux_engine.seam.history import Histogram, HistoryEnd, HistoryEntry, HistoryTracked
from flux_engine.seam.projections import EventsEntry, PeriodKind, PeriodsEntry, SnapshotEntry
from flux_engine.stages._module_checks import _note_unwritten_history

CONTRACT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "history-chain" / "subscriptions.fluid.yml"
)
VERSIONS = Versions(("subscription_id",), "valid_from", "valid_to")
CLOSE = HistoryEnd("closeLast", "0.5", "status", "ended")
FINAL = HistoryEnd("finalVersion", "0.5", "status", "ended")


def day(n: int) -> date:
    return date(2024, 1, 1) + timedelta(days=n)


PLAN = HistoryTracked(
    "plan",
    (
        ("basic", (("pro", "1"), ("premium", "0"))),
        ("pro", (("basic", "1"), ("premium", "1"))),
        ("premium", (("pro", "1"),)),
    ),
)
STATUS = HistoryTracked("status", (("active", (("paused", "1"),)), ("paused", (("active", "1"),))))


def history(
    chain: str = "nextStart",
    end: Optional[HistoryEnd] = None,
    open_end: Optional[str] = None,
    tracked: Tuple[HistoryTracked, ...] = (PLAN,),
) -> HistoryEntry:
    return HistoryEntry(
        emit="subscription_version",
        entities=1,
        length=Histogram(("1", "2"), ("1",)),
        until="2025-01-01",
        chain=chain,
        end=end,
        open_end=open_end,
        tracked=tracked,
    )


def version(
    sub: int, start: date, stop: Optional[date], plan: str = "basic", status: str = "active"
) -> Dict[str, Any]:
    return {
        "subscription_id": sub,
        "valid_from": start,
        "valid_to": stop,
        "plan": plan,
        "status": status,
    }


def current(sub: int, plan: str = "basic", status: str = "active") -> Dict[str, Any]:
    return {"subscription_id": sub, "plan": plan, "status": status}


def snapshot(
    rows: List[Dict[str, Any]],
    versions_rows: List[Dict[str, Any]],
    ended: str = "keep",
    held: Optional[HistoryEntry] = None,
) -> Holds:
    entry = SnapshotEntry("subscription_current", "subscription_version", ended)
    found = dict(projection_shares(entry, held or history(), rows, versions_rows, VERSIONS))
    return found[SNAPSHOT_EQUALS_LAST_VERSION]


def fails(share: Holds) -> bool:
    return share.satisfied < share.total


# ---------------------------------------------------------------------------
# snapshot:equals_last_version
# ---------------------------------------------------------------------------

CHAIN = [
    version(1, day(0), day(5), "basic"),
    version(1, day(5), None, "pro"),
    version(2, day(0), None, "basic"),
]


def test_a_snapshot_of_every_last_version_passes() -> None:
    rows = [current(1, "pro"), current(2, "basic")]
    assert snapshot(rows, CHAIN) == Holds(2, 2)


def test_history_rows_in_any_order_leave_the_last_version_the_one_with_the_latest_start() -> None:
    rows = [current(1, "pro"), current(2, "basic")]
    assert snapshot(rows, list(reversed(CHAIN))) == Holds(2, 2)


def test_a_snapshot_row_holding_an_earlier_version_fails() -> None:
    found = snapshot([current(1, "basic"), current(2, "basic")], CHAIN)
    assert found == Holds(1, 2)


def test_a_snapshot_row_differing_in_one_column_fails() -> None:
    assert fails(snapshot([current(1, "pro", "paused"), current(2)], CHAIN))


def test_an_entity_that_appears_twice_fails() -> None:
    rows = [current(1, "pro"), current(1, "pro"), current(2)]
    assert snapshot(rows, CHAIN) == Holds(1, 2)


def test_an_entity_the_snapshot_lacks_fails_under_keep() -> None:
    assert snapshot([current(1, "pro")], CHAIN) == Holds(1, 2)


def test_an_entity_the_history_lacks_fails() -> None:
    rows = [current(1, "pro"), current(2), current(3)]
    assert snapshot(rows, CHAIN) == Holds(2, 3)


def test_columns_the_snapshot_does_not_hold_are_not_compared() -> None:
    rows = [{"subscription_id": 1, "plan": "pro"}, {"subscription_id": 2, "plan": "basic"}]
    assert snapshot(rows, CHAIN) == Holds(2, 2)


ENDED_CLOSE = [
    version(1, day(0), day(5), "basic"),
    version(1, day(5), day(9), "pro", "ended"),
    version(2, day(0), None, "basic"),
]
ENDED_FINAL = [
    version(1, day(0), day(5), "basic"),
    version(1, day(5), None, "pro", "ended"),
    version(2, day(0), None, "basic"),
]


@pytest.mark.parametrize(
    ("rows", "end"), [(ENDED_CLOSE, CLOSE), (ENDED_FINAL, FINAL)], ids=["closeLast", "finalVersion"]
)
def test_keep_keeps_an_ended_entity_with_its_last_version(
    rows: List[Dict[str, Any]], end: HistoryEnd
) -> None:
    shown = [current(1, "pro", "ended"), current(2)]
    assert snapshot(shown, rows, "keep", history(end=end)) == Holds(2, 2)


@pytest.mark.parametrize(
    ("rows", "end"), [(ENDED_CLOSE, CLOSE), (ENDED_FINAL, FINAL)], ids=["closeLast", "finalVersion"]
)
def test_drop_leaves_exactly_the_ended_entities_out(
    rows: List[Dict[str, Any]], end: HistoryEnd
) -> None:
    assert snapshot([current(2)], rows, "drop", history(end=end)) == Holds(1, 1)


@pytest.mark.parametrize(
    ("rows", "end"), [(ENDED_CLOSE, CLOSE), (ENDED_FINAL, FINAL)], ids=["closeLast", "finalVersion"]
)
def test_drop_that_keeps_an_ended_entity_fails(rows: List[Dict[str, Any]], end: HistoryEnd) -> None:
    shown = [current(1, "pro", "ended"), current(2)]
    assert snapshot(shown, rows, "drop", history(end=end)) == Holds(1, 2)


def test_drop_that_leaves_a_live_entity_out_fails() -> None:
    assert snapshot([], ENDED_CLOSE, "drop", history(end=CLOSE)) == Holds(0, 1)


def test_drop_with_no_end_declared_drops_nothing() -> None:
    assert snapshot([current(1, "pro"), current(2)], CHAIN, "drop") == Holds(2, 2)


def test_the_open_end_sentinel_is_not_an_ended_entity() -> None:
    sentinel = date(9999, 12, 31)
    rows = [version(1, day(0), sentinel, "pro"), version(2, day(0), sentinel)]
    held = history(end=CLOSE, open_end="9999-12-31")
    assert snapshot([current(1, "pro"), current(2)], rows, "drop", held) == Holds(2, 2)


def test_chain_ended_reads_each_mode() -> None:
    assert chain_ended(ENDED_CLOSE[:2], VERSIONS, history(end=CLOSE), None)
    assert not chain_ended(CHAIN[:2], VERSIONS, history(end=CLOSE), None)
    assert chain_ended(ENDED_FINAL[:2], VERSIONS, history(end=FINAL), None)
    assert not chain_ended(CHAIN[:2], VERSIONS, history(), None)


def test_no_rows_hold_no_entities() -> None:
    assert snapshot([], []) == Holds(0, 0)


def test_a_composite_natural_entity_groups_on_every_column() -> None:
    versions = Versions(("org", "sub"), "valid_from", "valid_to")
    rows = [
        {"org": "x", "sub": "a", "valid_from": day(0), "valid_to": None, "plan": "p"},
        {"org": "y", "sub": "a", "valid_from": day(0), "valid_to": None, "plan": "q"},
    ]
    shown = [{"org": "x", "sub": "a", "plan": "p"}, {"org": "y", "sub": "a", "plan": "q"}]
    entry = SnapshotEntry("s", "h")
    found = dict(projection_shares(entry, history(), shown, rows, versions))
    assert found[SNAPSHOT_EQUALS_LAST_VERSION] == Holds(2, 2)


# ---------------------------------------------------------------------------
# Through rule_shares: the bound extension, the contract and the tables
# ---------------------------------------------------------------------------


def _contract() -> Any:
    return yaml.safe_load(CONTRACT.read_text("utf-8"))


def _extension(held: HistoryEntry, **projection: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        **projection,
        "projects": {"emit": "subscription_version", "entry": {"history": held.as_member()}},
    }


def _shown(
    rows: Mapping[str, List[Dict[str, Any]]], extension: Dict[str, Any], emit: str
) -> Dict[str, Holds]:
    tables: Dict[str, List[Dict[str, Any]]] = {name: list(held) for name, held in rows.items()}
    found = rule_shares(_contract(), emit, tables, RuleSource(extension=extension, generated=True))
    return {share.rule_id: share.holds for share in found if share.rule_id.count(":") == 1}


def test_the_bound_extension_is_read_back_as_the_entry_and_its_history() -> None:
    held = history(end=CLOSE)
    extension = _extension(held, snapshots={"of": "subscription_version", "ended": "drop"})
    entry = projection_entry_of(extension, "subscription_current")
    assert entry == SnapshotEntry("subscription_current", "subscription_version", "drop")
    assert projected_history(extension, "subscription_version") == held
    assert projection_entry_of({}, "subscription_current") is None
    assert projected_history({}, "subscription_version") is None


def test_rule_shares_scores_a_snapshot_on_the_rows_it_is_given() -> None:
    extension = _extension(history(), snapshots={"of": "subscription_version", "ended": "keep"})
    tables = {
        "subscription_version": CHAIN,
        "subscription_current": [current(1, "basic"), current(2)],
    }
    found = _shown(tables, extension, "subscription_current")
    assert found[SNAPSHOT_EQUALS_LAST_VERSION] == Holds(1, 2)


def test_a_snapshot_whose_history_rows_were_not_given_is_not_scored() -> None:
    extension = _extension(history(), snapshots={"of": "subscription_version", "ended": "keep"})
    assert _shown({"subscription_current": [current(1)]}, extension, "subscription_current") == {}


def test_an_emit_with_no_projection_adds_no_line() -> None:
    assert _shown({"subscription_current": [current(1)]}, {}, "subscription_current") == {}


def test_the_history_rows_are_a_table_the_projection_needs() -> None:
    extension = _extension(history(), snapshots={"of": "subscription_version", "ended": "keep"})
    needed = tables_needed(_contract(), "subscription_current", extension)
    assert needed == {"subscription_current", "subscription_version"}
    assert tables_needed(_contract(), "subscription_current", None) == {"subscription_current"}


# ---------------------------------------------------------------------------
# event:matches_transition and event:transition_matches
# ---------------------------------------------------------------------------

BEFORE = (("before_plan", "plan"),)
AFTER = (("after_plan", "plan"),)


def events_entry(**changes: Any) -> EventsEntry:
    base: Dict[str, Any] = {
        "emit": "subscription_change",
        "of": "subscription_version",
        "date": "recorded_at",
        "at": "changed_at",
        "op": "op",
        "before": BEFORE,
        "after": AFTER,
    }
    return EventsEntry(**{**base, **changes})


def event(
    sub: int,
    op: str,
    at: Any,
    before: Optional[str],
    after: Optional[str],
    recorded: Any = None,
) -> Dict[str, Any]:
    return {
        "subscription_id": sub,
        "op": op,
        "changed_at": at,
        "recorded_at": at if recorded is None else recorded,
        "before_plan": before,
        "after_plan": after,
    }


EVENTS = [
    event(1, "c", day(0), None, "basic"),
    event(1, "u", day(5), "basic", "pro"),
    event(2, "c", day(0), None, "basic"),
]


def events(
    rows: List[Dict[str, Any]],
    versions_rows: List[Dict[str, Any]] = CHAIN,
    entry: Optional[EventsEntry] = None,
    held: Optional[HistoryEntry] = None,
    covers: Optional[Callable[[Mapping[str, Any]], bool]] = None,
) -> Dict[str, Holds]:
    shown = projection_shares(
        entry or events_entry(),
        held or history(),
        rows,
        versions_rows,
        VERSIONS,
        ("subscription_id",),
        covers,
    )
    return dict(shown)


def test_events_that_equal_the_changes_pass_both_lines() -> None:
    assert events(EVENTS) == {
        EVENT_MATCHES_TRANSITION: Holds(2, 2),
        EVENT_TRANSITION_MATCHES: Holds(1, 1),
    }


def test_the_row_order_of_the_events_does_not_matter() -> None:
    assert events(list(reversed(EVENTS)))[EVENT_MATCHES_TRANSITION] == Holds(2, 2)
    assert events(EVENTS, list(reversed(CHAIN)))[EVENT_MATCHES_TRANSITION] == Holds(2, 2)


@pytest.mark.parametrize(
    "broken",
    [
        EVENTS[1:],
        EVENTS[:1] + EVENTS[2:],
        EVENTS + [event(1, "u", day(5), "basic", "pro")],
        EVENTS + [event(1, "u", day(7), "pro", "basic")],
        EVENTS + [event(3, "c", day(0), None, "basic")],
    ],
    ids=["no c", "no u", "a u twice", "a u too many", "an entity the history lacks"],
)
def test_events_that_differ_from_the_changes_as_a_set_fail_the_entity(
    broken: List[Dict[str, Any]],
) -> None:
    found = events(broken)[EVENT_MATCHES_TRANSITION]
    assert found.satisfied < found.total


@pytest.mark.parametrize(
    "wrong",
    [
        event(1, "u", day(5), "premium", "pro"),
        event(1, "u", day(5), "basic", "premium"),
        event(1, "u", day(6), "basic", "pro"),
        event(1, "c", day(5), "basic", "pro"),
        event(1, "u", day(5), None, "pro"),
    ],
    ids=["before", "after", "instant", "op", "a null before"],
)
def test_a_u_that_differs_in_a_field_fails_its_entity(wrong: Dict[str, Any]) -> None:
    found = events([EVENTS[0], wrong, EVENTS[2]])[EVENT_MATCHES_TRANSITION]
    assert found == Holds(1, 2)


def test_a_c_must_have_a_null_before_and_the_first_versions_after() -> None:
    wrong = [EVENTS[0] | {"before_plan": "basic"}, EVENTS[1], EVENTS[2]]
    assert events(wrong)[EVENT_MATCHES_TRANSITION] == Holds(1, 2)
    wrong = [EVENTS[0] | {"after_plan": "pro"}, EVENTS[1], EVENTS[2]]
    assert events(wrong)[EVENT_MATCHES_TRANSITION] == Holds(1, 2)


def test_a_date_before_its_at_fails_and_a_lag_passes() -> None:
    late = [row | {"recorded_at": row["changed_at"] + timedelta(days=3)} for row in EVENTS]
    assert events(late)[EVENT_MATCHES_TRANSITION] == Holds(2, 2)
    early = [EVENTS[0], EVENTS[1] | {"recorded_at": day(4)}, EVENTS[2]]
    assert events(early)[EVENT_MATCHES_TRANSITION] == Holds(1, 2)


def test_without_an_at_the_date_is_read_against_the_change_itself() -> None:
    entry = events_entry(at=None)
    rows = [{k: v for k, v in row.items() if k != "changed_at"} for row in EVENTS]
    assert events(rows, entry=entry)[EVENT_MATCHES_TRANSITION] == Holds(2, 2)
    rows[1] = rows[1] | {"recorded_at": day(4)}
    assert events(rows, entry=entry)[EVENT_MATCHES_TRANSITION] == Holds(1, 2)


def test_a_date_that_is_missing_fails_its_entity() -> None:
    rows = [EVENTS[0] | {"recorded_at": None}, EVENTS[1], EVENTS[2]]
    assert events(rows)[EVENT_MATCHES_TRANSITION] == Holds(1, 2)


def test_timestamps_and_dates_compare_by_the_instant() -> None:
    first = datetime(2024, 1, 1, 8, tzinfo=timezone.utc)
    second = datetime(2024, 1, 3, 9, tzinfo=timezone.utc)
    stamp = [version(1, first, second, "basic"), version(1, second, None, "pro")]
    rows = [event(1, "c", first, None, "basic"), event(1, "u", second, "basic", "pro")]
    assert events(rows, stamp)[EVENT_MATCHES_TRANSITION] == Holds(1, 1)
    as_dates = [row | {"changed_at": row["changed_at"].date()} for row in rows]
    assert events(as_dates, stamp)[EVENT_MATCHES_TRANSITION] == Holds(1, 1)
    naive = [row | {"changed_at": row["changed_at"].replace(tzinfo=None)} for row in rows]
    assert events(naive, stamp)[EVENT_MATCHES_TRANSITION] == Holds(1, 1)


def test_on_limits_the_ops_that_are_expected() -> None:
    only = events_entry(on=("c",))
    assert events([EVENTS[0], EVENTS[2]], entry=only)[EVENT_MATCHES_TRANSITION] == Holds(2, 2)
    assert events(EVENTS, entry=only)[EVENT_MATCHES_TRANSITION] == Holds(1, 2)
    assert events([], entry=events_entry(on=("d",)))[EVENT_MATCHES_TRANSITION] == Holds(2, 2)


def test_when_limits_the_changes_to_those_the_version_satisfies() -> None:
    def pro(row: Mapping[str, Any]) -> bool:
        return bool(row["plan"] == "pro")

    assert events([EVENTS[1]], covers=pro)[EVENT_MATCHES_TRANSITION] == Holds(2, 2)
    assert events(EVENTS, covers=pro)[EVENT_MATCHES_TRANSITION] == Holds(0, 2)


CLOSED = [
    version(1, day(0), day(5), "basic"),
    version(1, day(5), day(9), "pro", "ended"),
    version(2, day(0), None, "basic"),
]
CLOSED_EVENTS = [
    event(1, "c", day(0), None, "basic"),
    event(1, "u", day(5), "basic", "pro"),
    event(1, "d", day(9), "pro", None),
    event(2, "c", day(0), None, "basic"),
]


def test_a_close_last_d_is_dated_at_the_close_and_holds_the_last_version_as_its_before() -> None:
    found = events(CLOSED_EVENTS, CLOSED, held=history(end=CLOSE))
    assert found[EVENT_MATCHES_TRANSITION] == Holds(2, 2)
    for wrong in (
        CLOSED_EVENTS[2] | {"before_plan": "basic"},
        CLOSED_EVENTS[2] | {"before_plan": None},
        CLOSED_EVENTS[2] | {"after_plan": "pro"},
        CLOSED_EVENTS[2] | {"changed_at": day(5), "recorded_at": day(5)},
    ):
        rows = CLOSED_EVENTS[:2] + [wrong] + CLOSED_EVENTS[3:]
        shown = events(rows, CLOSED, held=history(end=CLOSE))
        assert shown[EVENT_MATCHES_TRANSITION] == Holds(1, 2)


def test_a_close_last_entity_with_no_d_fails() -> None:
    rows = CLOSED_EVENTS[:2] + CLOSED_EVENTS[3:]
    assert events(rows, CLOSED, held=history(end=CLOSE))[EVENT_MATCHES_TRANSITION] == Holds(1, 2)


def test_a_d_for_an_entity_that_did_not_end_fails() -> None:
    rows = [*EVENTS, event(1, "d", day(9), "pro", None)]
    assert events(rows)[EVENT_MATCHES_TRANSITION] == Holds(1, 2)


FINAL_ROWS = [
    version(1, day(0), day(5), "basic"),
    version(1, day(5), None, "pro", "ended"),
    version(2, day(0), None, "basic"),
]


def test_under_final_version_the_change_into_the_final_version_is_the_d() -> None:
    rows = [
        event(1, "c", day(0), None, "basic"),
        event(1, "d", day(5), "basic", None),
        event(2, "c", day(0), None, "basic"),
    ]
    found = events(rows, FINAL_ROWS, held=history(end=FINAL))
    assert found[EVENT_MATCHES_TRANSITION] == Holds(2, 2)
    assert found[EVENT_TRANSITION_MATCHES] == Holds(0, 0)
    as_update = [rows[0], event(1, "u", day(5), "basic", "pro"), rows[2]]
    shown = events(as_update, FINAL_ROWS, held=history(end=FINAL))
    assert shown[EVENT_MATCHES_TRANSITION] == Holds(1, 2)


def test_a_one_version_entity_that_the_history_ended_has_a_c_and_a_close() -> None:
    rows = [version(1, day(0), day(4), "basic", "ended")]
    shown = [event(1, "c", day(0), None, "basic"), event(1, "d", day(4), "basic", None)]
    found = events(shown, rows, held=history(end=CLOSE))
    assert found[EVENT_MATCHES_TRANSITION] == Holds(1, 1)


def test_the_ops_are_read_from_the_images_when_there_is_no_op_column() -> None:
    entry = events_entry(op=None)
    rows = [{k: v for k, v in row.items() if k != "op"} for row in EVENTS]
    assert events(rows, entry=entry)[EVENT_MATCHES_TRANSITION] == Holds(2, 2)
    assert events(rows[1:], entry=entry)[EVENT_MATCHES_TRANSITION] == Holds(1, 2)


def test_events_that_carry_neither_an_op_nor_an_image_are_not_scored() -> None:
    entry = events_entry(op=None, before=(), after=())
    assert events(EVENTS, entry=entry) == {}


def test_events_without_the_entity_are_not_scored() -> None:
    assert dict(projection_shares(events_entry(), history(), EVENTS, CHAIN, VERSIONS)) == {}


def test_a_u_that_moves_one_tracked_column_along_a_weighted_transition_holds() -> None:
    rows = [event(1, "u", day(5), "pro", "premium")]
    assert events(rows, [], held=history())[EVENT_TRANSITION_MATCHES] == Holds(1, 1)


@pytest.mark.parametrize(
    "wrong",
    [
        event(1, "u", day(5), "pro", "pro"),
        event(1, "u", day(5), "basic", "premium"),
        event(1, "u", day(5), None, "pro"),
    ],
    ids=["no column moves", "a transition with no weight", "no before"],
)
def test_a_u_that_moves_no_column_or_an_unweighted_one_fails(wrong: Dict[str, Any]) -> None:
    assert events([wrong], [])[EVENT_TRANSITION_MATCHES] == Holds(0, 1)


def test_a_u_that_moves_two_tracked_columns_fails() -> None:
    both = events_entry(
        before=BEFORE + (("before_status", "status"),), after=AFTER + (("after_status", "status"),)
    )
    held = history(tracked=(PLAN, STATUS))
    moved_one = event(1, "u", day(5), "basic", "pro") | {
        "before_status": "active",
        "after_status": "active",
    }
    moved_two = event(1, "u", day(5), "basic", "pro") | {
        "before_status": "active",
        "after_status": "paused",
    }
    shown = events([moved_one, moved_two], [], both, held)
    assert shown[EVENT_TRANSITION_MATCHES] == Holds(1, 2)


def test_an_ending_change_is_checked_for_the_end_value_alone() -> None:
    held = history(end=CLOSE, tracked=(PLAN, STATUS))
    entry = events_entry(
        before=BEFORE + (("before_status", "status"),), after=AFTER + (("after_status", "status"),)
    )
    ending = event(1, "u", day(5), "basic", "basic") | {
        "before_status": "active",
        "after_status": "ended",
    }
    plain = event(1, "u", day(5), "basic", "basic") | {
        "before_status": "active",
        "after_status": "active",
    }
    shown = events([ending, plain], [], entry, held)
    assert shown[EVENT_TRANSITION_MATCHES] == Holds(1, 2)


def test_an_ending_change_is_found_by_the_type_text_when_the_end_column_is_not_carried() -> None:
    entry = events_entry(type_column="kind", type_values=(("end", "closed"), ("plan", "plan")))
    held = history(end=CLOSE)
    row = event(1, "u", day(5), "basic", "basic") | {"kind": "closed"}
    other = event(1, "u", day(5), "basic", "basic") | {"kind": "plan"}
    assert events([row, other], [], entry, held)[EVENT_TRANSITION_MATCHES] == Holds(1, 2)


SEATS = HistoryTracked("seats", (("s1", (("s5", "1"),)), ("s5", (("s1", "1"),))))
SEATS_BEFORE = BEFORE + (("before_seats", "seats"),)
SEATS_AFTER = AFTER + (("after_seats", "seats"),)


def seat_event(
    sub: int, plan: Tuple[str, str], seats: Tuple[str, str], op: str = "u", at: Any = None
) -> Dict[str, Any]:
    return event(sub, op, day(5) if at is None else at, plan[0], plan[1]) | {
        "before_seats": seats[0],
        "after_seats": seats[1],
    }


def seats_chain(*states: Tuple[str, str]) -> List[Dict[str, Any]]:
    """One entity's versions, each three days long and the last open, from its (plan, seats) states."""
    last = len(states) - 1
    return [
        version(1, day(3 * n), None if n == last else day(3 * n + 3), plan) | {"seats": seats}
        for n, (plan, seats) in enumerate(states)
    ]


def updates(*moves: Tuple[str, str]) -> List[Dict[str, Any]]:
    """One ``u`` event for each (before, after) plan, as the events map ``plan`` alone."""
    return [event(1, "u", day(3 * n + 3), before, after) for n, (before, after) in enumerate(moves)]


@pytest.mark.parametrize(
    ("states", "moves", "expected"),
    [
        (
            [("pro", "s1"), ("pro", "s5"), ("premium", "s5")],
            [("pro", "pro"), ("pro", "premium")],
            Holds(2, 2),
        ),
        (
            [("basic", "s1"), ("pro", "s1"), ("premium", "s1")],
            [("pro", "pro"), ("premium", "premium")],
            Holds(0, 2),
        ),
        (
            [("basic", "s1"), ("basic", "s5"), ("basic", "s1"), ("pro", "s1")],
            [("basic", "basic")] * 3,
            Holds(2, 3),
        ),
    ],
    ids=[
        "a u on an unmapped column is not scored against a mapped one",
        "a hidden mapped move fails where only mapped columns changed",
        "one u is excused for each unmapped change",
    ],
)
def test_a_move_less_u_is_excused_only_as_often_as_an_unmapped_column_changed(
    states: List[Tuple[str, str]], moves: List[Tuple[str, str]], expected: Holds
) -> None:
    """Fix 2: the events map ``plan`` of a history that tracks ``plan`` and ``seats``."""
    held = history(tracked=(PLAN, SEATS))
    shown = events(updates(*moves), seats_chain(*states), held=held)
    assert shown[EVENT_TRANSITION_MATCHES] == expected


def test_a_mapped_move_off_its_transitions_still_fails_under_a_partial_mapping() -> None:
    """Fix 2: a partial mapping keeps the check on the columns it maps."""
    held = history(tracked=(PLAN, SEATS))
    off = event(1, "u", day(5), "basic", "premium")
    assert events([off], [], held=held)[EVENT_TRANSITION_MATCHES] == Holds(0, 1)


def test_a_u_with_no_mapped_move_still_fails_when_every_tracked_column_is_mapped() -> None:
    """Fix 2: the teeth when every tracked column is mapped."""
    held = history(tracked=(PLAN, SEATS))
    entry = events_entry(before=SEATS_BEFORE, after=SEATS_AFTER)
    moved = seat_event(1, ("basic", "pro"), ("s1", "s1"))
    dropped = seat_event(1, ("pro", "pro"), ("s1", "s1"))
    shown = events([moved, dropped], [], entry, held)
    assert shown[EVENT_TRANSITION_MATCHES] == Holds(1, 2)


def test_two_mapped_columns_moving_on_one_u_still_fail_under_a_partial_mapping() -> None:
    """Fix 2: one column changes per version, so two mapped moves fail whatever else is unmapped."""
    third = HistoryTracked("tier", (("a", (("b", "1"),)), ("b", (("a", "1"),))))
    held = history(tracked=(PLAN, SEATS, third))
    entry = events_entry(before=SEATS_BEFORE, after=SEATS_AFTER)
    both = seat_event(1, ("basic", "pro"), ("s1", "s5"))
    assert events([both], [], entry, held)[EVENT_TRANSITION_MATCHES] == Holds(0, 1)


ENDED_CHAIN = [
    version(1, day(0), day(3), "basic"),
    version(1, day(3), day(5), "pro"),
    version(1, day(5), day(9), "pro", "ended"),
]


@pytest.mark.parametrize(
    ("moves", "on", "expected"),
    [
        ([("basic", "pro"), ("pro", "pro")], ("c", "u", "d"), Holds(2, 2)),
        ([("basic", "pro"), ("pro", "pro")], ("u",), Holds(2, 2)),
        ([("pro", "pro"), ("pro", "pro")], ("u",), Holds(1, 2)),
    ],
    ids=[
        "the ending u is scored by the ended entity",
        "the ending u holds when on leaves the closing d out",
        "a dropped move on an ended entity still fails",
    ],
)
def test_a_close_last_ending_the_events_do_not_expose_is_excused_once_for_the_ended_entity(
    moves: List[Tuple[str, str]], on: Tuple[str, ...], expected: Holds
) -> None:
    """Fix 2: no ``end.column`` image and no ``end`` text, so the ending ``u`` moves no mapped column."""
    shown = history(end=CLOSE, tracked=(PLAN,))
    rows = updates(*moves)
    if "d" in on:
        rows.append(event(1, "d", day(9), "pro", None))
    found = events(rows, ENDED_CHAIN, events_entry(on=on), shown)
    assert found[EVENT_TRANSITION_MATCHES] == expected


def test_an_ending_hidden_u_of_an_entity_the_history_did_not_end_still_fails() -> None:
    """Fix 2: without an ended entity there is no ending to excuse a move-less ``u``."""
    shown = history(end=CLOSE, tracked=(PLAN,))
    row = event(1, "u", day(5), "pro", "pro")
    assert events([row], CHAIN, held=shown)[EVENT_TRANSITION_MATCHES] == Holds(0, 1)


def test_the_transition_line_is_present_only_when_a_tracked_column_is_carried() -> None:
    assert EVENT_TRANSITION_MATCHES not in events(EVENTS, entry=events_entry(before=(), after=()))
    assert EVENT_TRANSITION_MATCHES not in events(EVENTS, held=history(tracked=()))
    assert EVENT_TRANSITION_MATCHES not in events(
        EVENTS, entry=events_entry(before=BEFORE, after=())
    )


def test_only_u_events_count_in_the_transition_line() -> None:
    assert events(EVENTS[:1] + EVENTS[2:])[EVENT_TRANSITION_MATCHES] == Holds(0, 0)


def test_the_entity_columns_follow_a_relationship_or_the_same_names() -> None:
    model = model_from_contract(_contract())
    held = "subscription_version"
    assert entity_columns(model, "subscription_change", held, VERSIONS) == ("subscription_id",)
    assert entity_columns(model, "subscription_current", held, VERSIONS) == ("subscription_id",)
    assert entity_columns(model, "customer", held, VERSIONS) is None


def test_rule_shares_scores_events_with_a_when_on_the_history_rows() -> None:
    held = history()
    member = {"of": "subscription_version", "date": "recorded_at", "at": "changed_at", "op": "op"}
    member |= {"before": {"before_plan": "plan"}, "after": {"after_plan": "plan"}}
    member["when"] = "plan = 'pro'"
    extension = _extension(held, events=member)
    tables = {"subscription_version": CHAIN, "subscription_change": [EVENTS[1]]}
    found = _shown(tables, extension, "subscription_change")
    assert found[EVENT_MATCHES_TRANSITION] == Holds(2, 2)
    assert found[EVENT_TRANSITION_MATCHES] == Holds(1, 1)
    tables["subscription_change"] = EVENTS
    assert _shown(tables, extension, "subscription_change")[EVENT_MATCHES_TRANSITION] == Holds(0, 2)
    member.pop("when")
    again = _shown(tables, _extension(held, events=member), "subscription_change")
    assert again[EVENT_MATCHES_TRANSITION] == Holds(2, 2)


# ---------------------------------------------------------------------------
# period:within_validity and period:matches_valid_version
# ---------------------------------------------------------------------------

BOUNDS = {"start": "2024-01-01T00:00:00Z", "end": "2024-04-01T00:00:00Z"}
WINDOW = window_of(BOUNDS)
JAN, FEB, MAR = date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)

#: sub 1 is basic until 15 February and pro after it; sub 2 is basic from 10 February.
LIVE = [
    version(1, JAN, date(2024, 2, 15), "basic"),
    version(1, date(2024, 2, 15), None, "pro"),
    version(2, date(2024, 2, 10), None, "basic"),
]


def periods_entry(**changes: Any) -> PeriodsEntry:
    base: Dict[str, Any] = {
        "emit": "subscription_month",
        "of": "subscription_version",
        "column": "month",
        "grain": "month",
    }
    return PeriodsEntry(**{**base, **changes})


def periodic(sub: int, month: Any, plan: str = "basic", period_id: int = 1) -> Dict[str, Any]:
    return {"period_id": period_id, "subscription_id": sub, "month": month, "plan": plan}


def periods(
    rows: List[Dict[str, Any]],
    versions_rows: List[Dict[str, Any]] = LIVE,
    entry: Optional[PeriodsEntry] = None,
    held: Optional[HistoryEntry] = None,
) -> Dict[str, Holds]:
    shown = projection_shares(
        entry or periods_entry(),
        held or history(),
        rows,
        versions_rows,
        VERSIONS,
        ("subscription_id",),
        None,
        WINDOW,
    )
    return dict(shown)


def both(found: Dict[str, Holds]) -> Tuple[Holds, Holds]:
    return found[PERIOD_WITHIN_VALIDITY], found[PERIOD_MATCHES_VALID_VERSION]


def test_the_window_is_read_as_naive_utc_instants() -> None:
    assert WINDOW == (datetime(2024, 1, 1), datetime(2024, 4, 1))
    assert window_of({"start": "2024-01-01T02:00:00+02:00", "end": "2024-02-01"}) == (
        datetime(2024, 1, 1),
        datetime(2024, 2, 1),
    )
    assert window_of(None) is None
    assert window_of({"start": "2024-01-01"}) is None
    assert window_of({"start": "soon", "end": "later"}) is None


def test_periodic_rows_that_hold_a_version_of_their_entity_pass_both_lines() -> None:
    rows = [
        periodic(1, JAN, "basic"),
        periodic(1, FEB, "basic"),
        periodic(1, FEB, "pro"),
        periodic(1, MAR, "pro"),
        periodic(2, FEB, "basic"),
        periodic(2, MAR, "basic"),
    ]
    assert both(periods(rows)) == (Holds(6, 6), Holds(6, 6))


def test_a_period_with_a_change_holds_both_versions_under_overlap() -> None:
    rows = [periodic(1, FEB, "basic"), periodic(1, FEB, "pro"), periodic(1, FEB, "premium")]
    assert both(periods(rows)) == (Holds(3, 3), Holds(2, 3))


def test_a_row_whose_version_was_not_valid_in_its_period_fails_the_second_line() -> None:
    assert both(periods([periodic(1, JAN, "pro")])) == (Holds(1, 1), Holds(0, 1))
    assert both(periods([periodic(1, MAR, "basic")])) == (Holds(1, 1), Holds(0, 1))


def test_a_row_before_the_first_start_fails_both_lines() -> None:
    assert both(periods([periodic(2, JAN, "basic")])) == (Holds(0, 1), Holds(0, 1))


def test_an_entity_the_history_lacks_fails_both_lines() -> None:
    assert both(periods([periodic(9, FEB)])) == (Holds(0, 1), Holds(0, 1))


def test_a_closed_end_holds_no_later_period() -> None:
    rows = [version(1, JAN, date(2024, 2, 10), "basic", "ended")]
    held = history(end=CLOSE)
    found = both(periods([periodic(1, JAN), periodic(1, FEB), periodic(1, MAR)], rows, None, held))
    assert found == (Holds(2, 3), Holds(2, 3))


def test_an_open_version_holds_every_period_up_to_the_windows_end() -> None:
    rows = [periodic(1, MAR, "pro"), periodic(1, date(2024, 4, 1), "pro")]
    assert both(periods(rows)) == (Holds(1, 2), Holds(1, 2))


def test_the_open_end_sentinel_is_an_open_version() -> None:
    rows = [version(1, JAN, date(9999, 12, 31), "basic")]
    held = history(open_end="9999-12-31")
    assert both(periods([periodic(1, MAR)], rows, None, held)) == (Holds(1, 1), Holds(1, 1))


def test_match_start_reads_the_version_valid_at_the_first_instant() -> None:
    entry = periods_entry(match="start")
    rows = [periodic(1, FEB, "basic"), periodic(1, FEB, "pro"), periodic(2, FEB, "basic")]
    assert both(periods(rows, entry=entry)) == (Holds(2, 3), Holds(1, 3))


def test_match_end_reads_the_version_valid_at_the_last_instant_inside_the_window() -> None:
    entry = periods_entry(match="end")
    rows = [periodic(1, FEB, "basic"), periodic(1, FEB, "pro"), periodic(2, FEB, "basic")]
    assert both(periods(rows, entry=entry)) == (Holds(3, 3), Holds(2, 3))
    assert both(periods([periodic(1, JAN, "basic")], entry=entry)) == (Holds(1, 1), Holds(1, 1))
    assert both(periods([periodic(2, JAN, "basic")], entry=entry)) == (Holds(0, 1), Holds(0, 1))


def test_a_version_that_ends_where_a_period_starts_does_not_hold_it_under_next_start() -> None:
    rows = [version(1, JAN, FEB, "basic"), version(1, FEB, None, "pro")]
    found = periods([periodic(1, FEB, "basic"), periodic(1, FEB, "pro")], rows)
    assert both(found) == (Holds(2, 2), Holds(1, 2))
    assert both(periods([periodic(1, JAN, "pro")], rows)) == (Holds(1, 1), Holds(0, 1))


def test_under_day_before_a_date_end_covers_its_whole_day() -> None:
    rows = [version(1, date(2024, 1, 10), date(2024, 1, 19), "basic")]
    rows += [version(1, date(2024, 1, 20), None, "pro")]
    entry = periods_entry(grain="day")
    held = history("dayBefore")
    on = [periodic(1, date(2024, 1, 19), "basic"), periodic(1, date(2024, 1, 20), "pro")]
    assert both(periods(on, rows, entry, held)) == (Holds(2, 2), Holds(2, 2))
    late = [periodic(1, date(2024, 1, 20), "basic")]
    assert both(periods(late, rows, entry, held)) == (Holds(1, 1), Holds(0, 1))
    assert both(periods([periodic(1, date(2024, 1, 20), "basic")], rows, entry))[0] == Holds(1, 1)


def test_a_period_that_is_not_laid_from_the_windows_start_fails() -> None:
    rows = [periodic(1, date(2024, 2, 2)), periodic(1, date(2024, 4, 1)), periodic(1, None)]
    assert both(periods(rows)) == (Holds(0, 3), Holds(0, 3))


def test_a_timestamp_period_column_is_read_by_its_instant() -> None:
    rows = [periodic(1, datetime(2024, 2, 1), "basic"), periodic(1, datetime(2024, 2, 1, 1))]
    assert both(periods(rows)) == (Holds(1, 2), Holds(1, 2))


def test_months_are_added_from_the_anchor_and_clamped_to_the_months_last_day() -> None:
    bounds = {"start": "2024-01-31", "end": "2024-06-01"}
    entry = periods_entry()
    on_grid = [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31), date(2024, 4, 30)]
    rows = [periodic(1, day_, "basic", index) for index, day_ in enumerate(on_grid)]
    versions_rows = [version(1, date(2024, 1, 1), None, "basic")]
    shown = projection_shares(
        entry,
        history(),
        rows,
        versions_rows,
        VERSIONS,
        ("subscription_id",),
        None,
        window_of(bounds),
    )
    assert both(dict(shown)) == (Holds(4, 4), Holds(4, 4))
    off = [periodic(1, date(2024, 3, 29)), periodic(1, date(2024, 3, 1))]
    shown = projection_shares(
        entry,
        history(),
        off,
        versions_rows,
        VERSIONS,
        ("subscription_id",),
        None,
        window_of(bounds),
    )
    assert both(dict(shown)) == (Holds(0, 2), Holds(0, 2))


@pytest.mark.parametrize(
    ("grain", "good", "bad"),
    [
        ("day", date(2024, 1, 9), None),
        ("week", date(2024, 1, 8), date(2024, 1, 9)),
        ("quarter", date(2024, 4, 1), date(2024, 3, 1)),
        ("year", date(2025, 1, 1), date(2024, 12, 1)),
    ],
)
def test_the_grain_lays_the_periods(grain: str, good: date, bad: Optional[date]) -> None:
    bounds = {"start": "2024-01-01", "end": "2026-01-01"}
    versions_rows = [version(1, date(2023, 1, 1), None, "basic")]
    found = projection_shares(
        periods_entry(grain=grain),
        history(),
        [periodic(1, good), periodic(1, bad or date(2024, 1, 1))],
        versions_rows,
        VERSIONS,
        ("subscription_id",),
        None,
        window_of(bounds),
    )
    assert both(dict(found)) == (Holds(2 if bad is None else 1, 2),) * 2


def test_the_period_column_and_the_kinds_overrides_are_not_compared() -> None:
    entry = periods_entry(kinds=(PeriodKind(overrides=(("plan", "usage"),)),))
    rows = [periodic(1, FEB, "usage")]
    assert both(periods(rows, entry=entry)) == (Holds(1, 1), Holds(1, 1))
    assert both(periods([periodic(1, FEB, "usage")])) == (Holds(1, 1), Holds(0, 1))


def test_columns_the_history_does_not_name_are_not_compared() -> None:
    rows = [periodic(1, FEB, "pro", 77) | {"usage": 12}]
    assert both(periods(rows)) == (Holds(1, 1), Holds(1, 1))


def test_every_row_of_a_period_is_counted_once_per_line() -> None:
    rows = [periodic(1, FEB, "pro")] * 3 + [periodic(1, JAN, "pro")] * 2
    assert both(periods(rows)) == (Holds(5, 5), Holds(3, 5))


def test_periods_without_a_grain_a_window_or_the_entity_are_not_scored() -> None:
    entry = periods_entry(grain=None)
    assert periods([periodic(1, FEB)], entry=entry) == {}
    shown = projection_shares(
        periods_entry(), history(), [periodic(1, FEB)], LIVE, VERSIONS, ("subscription_id",)
    )
    assert shown == ()
    nameless = projection_shares(periods_entry(), history(), [], LIVE, VERSIONS, (), None, WINDOW)
    assert nameless == ()


def test_no_rows_hold_no_periodic_rows() -> None:
    assert both(periods([])) == (Holds(0, 0), Holds(0, 0))


def test_rule_shares_scores_periods_on_the_windows_of_the_run() -> None:
    member = {"of": "subscription_version", "column": "month", "grain": "month", "match": "overlap"}
    extension = _extension(history(), periods=member)
    tables = {"subscription_version": LIVE, "subscription_month": [periodic(1, JAN, "pro")]}
    source = RuleSource(extension=extension, generated=True, time_bounds=BOUNDS)
    found = {
        share.rule_id: share.holds
        for share in rule_shares(_contract(), "subscription_month", tables, source)
        if share.rule_id.startswith("period:")
    }
    assert found == {PERIOD_WITHIN_VALIDITY: Holds(1, 1), PERIOD_MATCHES_VALID_VERSION: Holds(0, 1)}
    without = RuleSource(extension=extension, generated=True)
    assert not [
        share
        for share in rule_shares(_contract(), "subscription_month", tables, without)
        if share.rule_id.startswith("period:")
    ]


# ---------------------------------------------------------------------------
# A history that the run did not write
# ---------------------------------------------------------------------------


class _Emit:
    def __init__(self, extension: Dict[str, Any]) -> None:
        self.extension = extension
        self.expose_id = "subscription_current"


class _Layout:
    def __init__(self, root: Path) -> None:
        self.root = root

    def emit_path(self, expose_id: str) -> Path:
        return self.root / f"{expose_id}.jsonl"


def test_a_projection_whose_history_wrote_no_rows_is_listed_as_not_scored(tmp_path: Path) -> None:
    extension = _extension(history(), snapshots={"of": "subscription_version", "ended": "keep"})
    emit, layout = _Emit(extension), _Layout(tmp_path)
    needed = {"subscription_current", "subscription_version"}
    notes: List[str] = []
    _note_unwritten_history(emit, {"subscription_current": {}}, layout, needed, notes)
    assert needed == {"subscription_current"}
    assert len(notes) == 1
    assert "subscription_version" in notes[0] and "not scored" in notes[0]
    assert "hidden" in notes[0]


def test_a_history_whose_rows_the_run_wrote_is_read(tmp_path: Path) -> None:
    extension = _extension(history(), snapshots={"of": "subscription_version", "ended": "keep"})
    (tmp_path / "subscription_version.jsonl").write_text("", "utf-8")
    needed = {"subscription_current", "subscription_version"}
    notes: List[str] = []
    emitted = {"subscription_current": {}, "subscription_version": {}}
    _note_unwritten_history(_Emit(extension), emitted, _Layout(tmp_path), needed, notes)
    assert needed == {"subscription_current", "subscription_version"} and notes == []
