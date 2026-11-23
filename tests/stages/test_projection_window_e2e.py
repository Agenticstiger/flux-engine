# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Change events and periodic rows, and a window that grows, through the ``fluxsim`` command line.

The bundle is ``tests/fixtures/history-window``, with ``subscription_change``, an event for each change of
``subscription_version`` recorded after a lag, with a text for each kind of change, and ``subscription_month``,
a row for each month a version is valid, in two kinds with a count in levels, a condition and an override each.
Neither declares ``rows``: ``plan`` derives them from the history. Every run is ``fluxsim plan``, ``apply`` and
``verify`` in a subprocess, and every assertion reads the rows the run wrote. What is pinned:

* **The lines.** Every ``event:*`` and ``period:*`` line of ``verify`` passes on every entity and row.
* **The rows.** An event is dated at its change or after it and no later than ``until``, and carries its kind's
  text; a periodic row carries the plan of a version valid in its month and the override of its kind.
* **The window.** Under two windows with one ``until`` the events are the same bytes with the same digest, and
  the periodic rows of the longer window begin with the shorter window's rows, their keys included, and the rows
  after them all lie in the periods after the shorter window's end (ADR 0027 section 7).
* **Size.** ``--shard`` with shares that cut across a period boundary joins into the whole run's bytes, and
  ``--scale`` below 1 follows the scaled history.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from .test_history_window_e2e import (
    DEVICES,
    SNAPSHOT,
    SUBSCRIPTIONS,
    UNTIL,
    _apply,
    _bundle,
    _by_entity,
    _day,
    _digests,
    _lines,
    _rows,
    _run,
    _verify,
)

pytestmark = pytest.mark.integration

EVENTS, PERIODIC = "subscription_change", "subscription_month"
#: The windows' ends: the second cuts a month (March 2026) in two, the first and the third end on a month's edge.
NARROW_ENDS = ("2027-01-01T00:00:00Z", "2026-03-15T00:00:00Z")
LONG_WINDOW_END = "2030-06-30T00:00:00Z"
#: The text of each kind of change, as the fixture declares it.
TYPES = {"c": "created", "d": "deleted", "u": "plan_change"}
SPLIT = date(2024, 6, 1)  # the condition the two kinds of periodic row share
ENTITIES = 60
LINES = {
    EVENTS: {"event:matches_transition", "event:transition_matches"},
    PERIODIC: {"period:within_validity", "period:matches_valid_version"},
}


def _check_lines(verified: Dict[str, Any]) -> None:
    """Every event and period line passes on everything it checks."""
    assert verified["exitCode"] == 0
    found = _lines(verified)
    for expose_id, rules in LINES.items():
        assert {
            rule for rule in found[expose_id] if rule.split(":")[0] in ("event", "period")
        } == rules
        for rule in rules:
            entry = found[expose_id][rule]
            assert entry["met"] is True and entry["enforced"] is True, (expose_id, rule)
            assert entry["satisfied"] == entry["total"] > 0, (expose_id, rule)


def _month(row: Dict[str, Any]) -> date:
    return _day(row["month"])


def _next_month(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def _lines_of(run_path: Path) -> List[bytes]:
    return run_path.read_bytes().splitlines()


@pytest.fixture(scope="module")
def whole(tmp_path_factory: pytest.TempPathFactory) -> Any:
    tmp_path = tmp_path_factory.mktemp("whole")
    return _run(tmp_path, _bundle(tmp_path))


# --- the lines and the rows --------------------------------------------------------------------


def test_every_event_and_period_line_passes(whole: Any) -> None:
    _check_lines(_verify(whole))
    versions = _rows(whole.layout, SUBSCRIPTIONS)
    assert len(_rows(whole.layout, EVENTS)) == len(versions)  # a change for each version
    assert len(_rows(whole.layout, PERIODIC)) > 0
    plan = json.loads(whole.layout.plan_path.read_text("utf-8"))["emits"]
    assert plan[EVENTS]["rows"] == len(versions) and plan[EVENTS]["rowsFrom"]["of"] == SUBSCRIPTIONS
    assert plan[PERIODIC]["rows"] == len(_rows(whole.layout, PERIODIC))


def test_an_event_is_recorded_after_its_change_by_a_lag_and_carries_its_kinds_text(
    whole: Any,
) -> None:
    versions = _by_entity(_rows(whole.layout, SUBSCRIPTIONS), "subscription_id")
    events = _rows(whole.layout, EVENTS)
    lags = []
    for event in events:
        assert event["change_type"] == TYPES[event["op"]]
        assert _day(event["changed_at"]) <= _day(event["recorded_at"]) <= UNTIL
        lags.append((_day(event["recorded_at"]) - _day(event["changed_at"])).days)
    assert 0 in lags and 0 < max(lags) <= 20
    assert {event["change_type"] for event in events} == set(TYPES.values())
    # One c per subscription, one u per later version, and a d where the history ended it with a final version.
    held = _by_entity(events, "subscription_id")
    assert set(held) == set(versions) and len(held) == ENTITIES
    for subscription, rows in held.items():
        assert [row["op"] for row in rows].count("c") == 1
        assert len(rows) == len(versions[subscription])


def test_a_periodic_row_carries_a_version_valid_in_its_month_and_its_kinds_override(
    whole: Any,
) -> None:
    versions = _by_entity(_rows(whole.layout, SUBSCRIPTIONS), "subscription_id")
    rows = _rows(whole.layout, PERIODIC)
    months = [_month(row) for row in rows]
    assert months == sorted(months) and months[0] == date(
        2023, 1, 1
    )  # period by period from the window's start
    assert {row["kind"] for row in rows} == {"old", "new"}
    for row in rows:
        month, end = _month(row), _next_month(_month(row))
        held = [
            version
            for version in versions[row["subscription_id"]]
            if _day(version["valid_from"]) < end
            and (version["valid_to"] is None or _day(version["valid_to"]) > month)
            and version["plan"] == row["plan"]
        ]
        assert held
        if row["kind"] == "old":
            assert any(_day(version["valid_from"]) < SPLIT for version in held)
        else:
            assert month >= date(2024, 1, 1) and any(
                _day(version["valid_from"]) >= SPLIT for version in held
            )
    # The key takes one step per row from the low end of its range.
    assert [row["period_id"] for row in rows] == list(range(1, len(rows) + 1))
    assert any(row["usage"] != rows[0]["usage"] for row in rows)  # a column nothing sets is drawn


# --- the window --------------------------------------------------------------------------------


@pytest.mark.parametrize("narrow_end", NARROW_ENDS)
def test_a_longer_window_keeps_the_events_and_appends_periods_after_the_old_ones(
    tmp_path: Path, narrow_end: str
) -> None:
    narrow = _run(tmp_path, _bundle(tmp_path, "narrow", window_end=narrow_end), "narrow-state")
    wide = _run(tmp_path, _bundle(tmp_path, "wide", window_end=LONG_WINDOW_END), "wide-state")
    # The events, and every emit that is not periodic, are the same bytes with the same digests.
    kept = (SUBSCRIPTIONS, DEVICES, SNAPSHOT, EVENTS)
    for expose_id in kept:
        assert (
            narrow.layout.emit_path(expose_id).read_bytes()
            == wide.layout.emit_path(expose_id).read_bytes()
        )
        assert _digests(narrow.layout)["components"]["emits"][expose_id] == (
            _digests(wide.layout)["components"]["emits"][expose_id]
        )
    assert _digests(narrow.layout)["planDigest"] != _digests(wide.layout)["planDigest"]
    # The periodic rows begin with the shorter window's, keys included, and the rest are in new periods.
    old = _lines_of(narrow.layout.emit_path(PERIODIC))
    new = _lines_of(wide.layout.emit_path(PERIODIC))
    assert len(new) > len(old) > 0 and new[: len(old)] == old
    old_rows, new_rows = _rows(narrow.layout, PERIODIC), _rows(wide.layout, PERIODIC)
    last_old = _month(old_rows[-1])
    assert last_old < date.fromisoformat(narrow_end[:10])
    assert all(_month(row) > last_old for row in new_rows[len(old) :])
    assert [row["period_id"] for row in new_rows] == list(range(1, len(new_rows) + 1))
    _check_lines(_verify(wide))


# --- shard and scale ---------------------------------------------------------------------------


def _cut_inside_a_period(shares: Sequence[Path]) -> int:
    """How many borders between shares fall inside one month: both sides hold a row of it."""
    held = [json.loads(share.read_bytes().splitlines()[i]) for share in shares for i in (0, -1)]
    # held is first, last of each share: a border is a share's last row and the next share's first row.
    return sum(
        1 for last, first in zip(held[1::2], held[2::2], strict=False) if last[2] == first[2]
    )


@pytest.mark.parametrize("count", [3, 7])
def test_shares_that_cut_across_a_period_join_into_the_whole_runs_bytes(
    whole: Any, count: int
) -> None:
    for index in range(1, count + 1):
        _apply(whole.bundle, whole.layout, whole.state, "--shard", f"{index}/{count}")
    for expose_id in (EVENTS, PERIODIC):
        shares = [
            whole.layout.shard_emit_path(expose_id, index, count) for index in range(1, count + 1)
        ]
        joined = b"".join(share.read_bytes() for share in shares)
        assert joined == whole.layout.emit_path(expose_id).read_bytes(), expose_id
    shares = [whole.layout.shard_emit_path(PERIODIC, index, count) for index in range(1, count + 1)]
    assert _cut_inside_a_period(shares) >= 1


@pytest.mark.parametrize(("scale", "entities"), [("0.5", 30), ("0.25", 15)])
def test_a_scale_below_one_follows_the_scaled_history(
    tmp_path: Path, scale: str, entities: int
) -> None:
    run = _run(tmp_path, _bundle(tmp_path), "scaled", scale=scale)
    versions = _rows(run.layout, SUBSCRIPTIONS)
    assert len(_by_entity(versions, "subscription_id")) == entities
    assert len(_rows(run.layout, SNAPSHOT)) == entities
    assert len(_rows(run.layout, EVENTS)) == len(versions)  # not scaled again
    plan = json.loads(run.layout.plan_path.read_text("utf-8"))["emits"]
    assert plan[EVENTS]["rows"] == len(versions)
    assert plan[PERIODIC]["rows"] == len(_rows(run.layout, PERIODIC)) > 0
    _check_lines(_verify(run))
