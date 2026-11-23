# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The facts over dates, counted on hand-made rows (ADR 0024 section 8).

The rows are written here and not generated, so the arithmetic is checked on its own: the units
of a lifetime, the rows each fact covers, the open parents, the chain levels and the target.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from flux_engine.concordance.date_facts import (
    at_or_after_start,
    at_or_before_end,
    date_shares,
    is_open,
    sampling_allowance,
)
from flux_engine.concordance.rule_shares import RuleShare
from flux_engine.expr2.rows import Rows
from flux_engine.generator.rules import model_from_contract
from flux_engine.seam.date_lines import DateLine, contract_step, date_lines, date_rule_ids
from flux_engine.seam.dates import LifetimeEntry, WithinEntry

pytestmark = pytest.mark.unit

D = date
T = lambda y, m, d, h=0, n=0, s=0: datetime(y, m, d, h, n, s, tzinfo=timezone.utc)  # noqa: E731


def _expose(
    name: str, columns: Sequence[Tuple[str, str]], link: Optional[str] = None
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "exposeId": name,
        "contract": {"schema": [{"name": n, "type": t} for n, t in columns]},
        "semantics": {"entities": [{"name": name, "type": "primary", "expr": columns[0][0]}]},
    }
    if link is not None:
        entry["semantics"]["relationships"] = [
            {
                "name": link,
                "to": link,
                "fromColumns": [f"{name[0]}_{link}"],
                "toColumns": [f"{link[0]}_id"],
                "cardinality": "many_to_one",
            }
        ]
    return entry


#: root (DATE lifetime) <- mid (TIMESTAMP lifetime) <- leaf (a DATE and a TIMESTAMP column).
CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "t.dates",
    "exposes": [
        _expose("root", [("r_id", "STRING"), ("r_start", "DATE"), ("r_end", "DATE")]),
        _expose(
            "mid",
            [
                ("m_id", "STRING"),
                ("m_root", "STRING"),
                ("m_start", "TIMESTAMP"),
                ("m_end", "TIMESTAMP"),
            ],
            "root",
        ),
        _expose(
            "leaf",
            [("l_id", "STRING"), ("l_mid", "STRING"), ("l_day", "DATE"), ("l_at", "TIMESTAMP")],
            "mid",
        ),
    ],
}
MODEL = model_from_contract(CONTRACT)
STEP = contract_step(CONTRACT)

ROOT = LifetimeEntry(emit="root", start="r_start", end="r_end")
MID = LifetimeEntry(emit="mid", start="m_start", end="m_end")
MID_OPEN = LifetimeEntry(emit="mid", start="m_start")
LEAF_OPEN = LifetimeEntry(emit="leaf", start="l_day")


def _root(rid: str, start: Optional[date], end: Optional[date]) -> Dict[str, Any]:
    return {"r_id": rid, "r_start": start, "r_end": end}


def _mid(
    mid: str, root: Optional[str], start: Optional[datetime], end: Optional[datetime]
) -> Dict[str, Any]:
    return {"m_id": mid, "m_root": root, "m_start": start, "m_end": end}


def _leaf(
    leaf: str, mid: Optional[str], day: Optional[date], at: Optional[datetime] = None
) -> Dict[str, Any]:
    return {"l_id": leaf, "l_mid": mid, "l_day": day, "l_at": at}


def _rows(**tables: List[Dict[str, Any]]) -> Rows:
    return Rows(MODEL, tables)


def _only(
    lifetimes: Sequence[LifetimeEntry],
    within: Sequence[WithinEntry],
    rows: Rows,
    rule_id: str,
    generated: bool = True,
) -> RuleShare:
    lines = [line for line in date_lines(lifetimes, within, STEP) if line.rule_id == rule_id]
    assert len(lines) == 1, [line.rule_id for line in date_lines(lifetimes, within, STEP)]
    (share,) = date_shares(lines, rows, generated=generated)
    return share


def _counts(share: RuleShare) -> Tuple[int, int]:
    return share.holds.satisfied, share.holds.total


# ---------------------------------------------------------------------------
# The units
# ---------------------------------------------------------------------------


def test_a_date_start_bounds_a_timestamp_from_its_midnight_utc() -> None:
    assert at_or_after_start(T(2025, 3, 1), D(2025, 3, 1))
    assert not at_or_after_start(T(2025, 2, 28, 23, 59, 59), D(2025, 3, 1))


def test_a_date_end_bounds_a_timestamp_up_to_the_end_of_that_day() -> None:
    assert at_or_before_end(T(2025, 3, 1, 23, 59, 59), D(2025, 3, 1))
    assert not at_or_before_end(T(2025, 3, 2), D(2025, 3, 1))


def test_a_timestamp_bounds_a_date_by_the_day_that_holds_it() -> None:
    assert at_or_after_start(D(2025, 3, 1), T(2025, 3, 1, 18))
    assert not at_or_after_start(D(2025, 2, 28), T(2025, 3, 1, 0, 0, 1))
    assert at_or_before_end(D(2025, 3, 1), T(2025, 3, 1, 2))
    assert not at_or_before_end(D(2025, 3, 2), T(2025, 3, 1, 23, 59, 59))


def test_two_timestamps_and_two_dates_compare_as_themselves() -> None:
    assert at_or_after_start(T(2025, 1, 1, 12), T(2025, 1, 1, 12))
    assert not at_or_after_start(T(2025, 1, 1, 11), T(2025, 1, 1, 12))
    assert at_or_before_end(T(2025, 1, 1, 12), T(2025, 1, 1, 12))
    assert not at_or_before_end(T(2025, 1, 1, 13), T(2025, 1, 1, 12))
    assert at_or_before_end(D(2025, 1, 5), D(2025, 1, 5))
    assert not at_or_before_end(D(2025, 1, 6), D(2025, 1, 5))


def test_a_lifetime_is_open_without_an_end_or_with_an_end_before_its_start() -> None:
    assert is_open(D(2025, 1, 1), None)
    assert is_open(D(2025, 1, 10), D(2025, 1, 9))
    assert not is_open(D(2025, 1, 10), D(2025, 1, 10))  # a one day lifetime has ended
    assert is_open(D(2025, 1, 10), T(2025, 1, 9, 23))
    assert not is_open(T(2025, 1, 10, 5), D(2025, 1, 10))


# ---------------------------------------------------------------------------
# lifetime:
# ---------------------------------------------------------------------------


def test_a_lifetime_holds_where_the_end_is_not_before_the_start() -> None:
    rows = _rows(
        root=[
            _root("a", D(2025, 1, 1), D(2025, 1, 1)),
            _root("b", D(2025, 1, 1), D(2025, 6, 1)),
            _root("c", D(2025, 2, 1), D(2025, 1, 1)),
        ]
    )
    share = _only([ROOT], [], rows, "lifetime:r_start.r_end")
    assert _counts(share) == (2, 3)
    assert share.met is False and share.enforced is True and share.target is None


def test_a_lifetime_leaves_out_the_rows_where_either_side_is_unknown() -> None:
    rows = _rows(
        root=[
            _root("a", D(2025, 1, 1), None),
            _root("b", None, D(2025, 1, 1)),
            _root("c", None, None),
            _root("d", D(2025, 1, 1), D(2025, 1, 2)),
        ]
    )
    share = _only([ROOT], [], rows, "lifetime:r_start.r_end")
    assert _counts(share) == (1, 1) and share.met is True


def test_a_timestamp_lifetime_includes_its_last_instant() -> None:
    rows = _rows(
        mid=[
            _mid("a", None, T(2025, 1, 1, 8), T(2025, 1, 1, 8)),
            _mid("b", None, T(2025, 1, 1, 8), T(2025, 1, 1, 7)),
        ]
    )
    assert _counts(_only([MID], [], rows, "lifetime:m_start.m_end")) == (1, 2)


def test_a_lifetime_with_no_end_makes_no_line() -> None:
    assert [line.rule_id for line in date_lines([MID_OPEN], [], STEP)] == []


# ---------------------------------------------------------------------------
# within: a single date
# ---------------------------------------------------------------------------


def _mid_in_root(*mids: Dict[str, Any], roots: Optional[List[Dict[str, Any]]] = None) -> Rows:
    return _rows(root=roots or [_root("a", D(2025, 3, 1), D(2025, 3, 31))], mid=list(mids))


WITHIN_MID = WithinEntry(emit="mid", relationship=("root",), date="m_start")


def test_a_date_within_its_parents_lifetime_holds_on_both_bounds() -> None:
    rows = _mid_in_root(
        _mid("1", "a", T(2025, 3, 1), None),  # the first instant of the start day
        _mid("2", "a", T(2025, 3, 31, 23, 59, 59), None),  # the last instant of the end day
        _mid("3", "a", T(2025, 3, 15, 12), None),
    )
    share = _only([ROOT, MID_OPEN], [WITHIN_MID], rows, "within:root.m_start")
    assert _counts(share) == (3, 3) and share.met is True


def test_a_date_before_the_start_or_after_the_end_violates() -> None:
    rows = _mid_in_root(
        _mid("1", "a", T(2025, 2, 28, 23, 59, 59), None),
        _mid("2", "a", T(2025, 4, 1), None),
        _mid("3", "a", T(2025, 3, 10), None),
    )
    share = _only([ROOT, MID_OPEN], [WITHIN_MID], rows, "within:root.m_start")
    assert _counts(share) == (1, 3)
    assert share.met is False


def test_a_date_against_a_timestamp_lifetime_uses_the_day_that_holds_the_bound() -> None:
    within = WithinEntry(emit="leaf", relationship=("mid",), date="l_day")
    rows = _rows(
        root=[],
        mid=[_mid("a", None, T(2025, 3, 10, 18), T(2025, 3, 20, 3))],
        leaf=[
            _leaf("1", "a", D(2025, 3, 10)),  # the day that holds the start
            _leaf("2", "a", D(2025, 3, 20)),  # the day that holds the end
            _leaf("3", "a", D(2025, 3, 9)),
            _leaf("4", "a", D(2025, 3, 21)),
        ],
    )
    share = _only([MID, LEAF_OPEN], [within], rows, "within:mid.l_day")
    assert _counts(share) == (2, 4)


def test_a_timestamp_against_a_date_lifetime_runs_from_midnight_to_the_end_of_the_day() -> None:
    within = WithinEntry(emit="mid", relationship=("root",), date="m_start")
    rows = _mid_in_root(
        _mid("1", "a", T(2025, 3, 1), None),
        _mid("2", "a", T(2025, 3, 31, 23, 59, 59), None),
        _mid("3", "a", T(2025, 4, 1), None),
        _mid("4", "a", T(2025, 2, 28, 23, 59, 59), None),
    )
    assert _counts(_only([ROOT, MID_OPEN], [within], rows, "within:root.m_start")) == (2, 4)


@pytest.mark.parametrize(
    ("root_row", "child", "ok"),
    [
        (_root("a", D(2025, 3, 1), None), T(2030, 1, 1), True),  # the end is NULL: open
        (_root("a", D(2025, 3, 10), D(2025, 3, 1)), T(2030, 1, 1), True),  # end before start: open
        (_root("a", D(2025, 3, 1), D(2025, 3, 31)), T(2030, 1, 1), False),  # ended
    ],
)
def test_an_open_parent_lifetime_has_no_upper_bound(
    root_row: Dict[str, Any], child: datetime, ok: bool
) -> None:
    rows = _mid_in_root(_mid("1", "a", child, None), roots=[root_row])
    share = _only([ROOT, MID_OPEN], [WITHIN_MID], rows, "within:root.m_start")
    assert _counts(share) == (int(ok), 1)


def test_a_parent_whose_lifetime_declares_no_end_is_open() -> None:
    root_only_start = LifetimeEntry(emit="root", start="r_start")
    rows = _mid_in_root(
        _mid("1", "a", T(2040, 1, 1), None), roots=[_root("a", D(2025, 3, 1), D(2025, 3, 2))]
    )
    share = _only([root_only_start, MID_OPEN], [WITHIN_MID], rows, "within:root.m_start")
    assert _counts(share) == (1, 1)  # the column r_end is never read


def test_a_row_that_reaches_no_parent_start_or_has_no_value_is_not_covered() -> None:
    rows = _rows(
        root=[
            _root("a", D(2025, 3, 1), D(2025, 3, 31)),
            _root("b", None, D(2025, 3, 31)),
        ],
        mid=[
            _mid("1", "a", T(2025, 3, 5), None),  # covered and holds
            _mid("2", None, T(2025, 3, 5), None),  # a NULL foreign key
            _mid("3", "zz", T(2025, 3, 5), None),  # a key no parent holds
            _mid("4", "b", T(2025, 3, 5), None),  # a parent whose start is NULL
            _mid("5", "a", None, None),  # its own value is NULL
            _mid("6", "a", T(2025, 5, 5), None),  # covered and violates
        ],
    )
    share = _only([ROOT, MID_OPEN], [WITHIN_MID], rows, "within:root.m_start")
    assert _counts(share) == (1, 2)


# ---------------------------------------------------------------------------
# within: a period
# ---------------------------------------------------------------------------

PERIOD = WithinEntry(emit="mid", relationship=("root",), lifetime=True)


def _period(*mids: Dict[str, Any], root: Optional[Dict[str, Any]] = None) -> RuleShare:
    rows = _mid_in_root(*mids, roots=[root or _root("a", D(2025, 3, 1), D(2025, 3, 31))])
    return _only([ROOT, MID], [PERIOD], rows, "within:root.lifetime")


def test_a_period_within_its_parents_lifetime_holds() -> None:
    share = _period(
        _mid("1", "a", T(2025, 3, 1), T(2025, 3, 31, 23, 59, 59)),
        _mid("2", "a", T(2025, 3, 5), T(2025, 3, 6)),
    )
    assert _counts(share) == (2, 2)


def test_a_period_that_ends_after_its_parent_violates() -> None:
    share = _period(
        _mid("1", "a", T(2025, 3, 5), T(2025, 4, 1)),
        _mid("2", "a", T(2025, 3, 5), T(2025, 3, 20)),
    )
    assert _counts(share) == (1, 2)


def test_a_period_with_no_end_violates_once_its_parent_has_ended() -> None:
    share = _period(
        _mid("1", "a", T(2025, 3, 5), None), _mid("2", "a", T(2025, 3, 5), T(2025, 3, 9))
    )
    assert _counts(share) == (1, 2)


def test_a_period_may_have_no_end_while_its_parent_has_none() -> None:
    share = _period(_mid("1", "a", T(2025, 3, 5), None), root=_root("a", D(2025, 3, 1), None))
    assert _counts(share) == (1, 1)
    share = _period(
        _mid("1", "a", T(2025, 3, 12), None), root=_root("a", D(2025, 3, 10), D(2025, 3, 1))
    )  # an end before the start is open
    assert _counts(share) == (1, 1)


def test_a_period_that_starts_outside_its_parent_violates_and_one_with_no_start_is_left_out() -> (
    None
):
    share = _period(
        _mid("1", "a", T(2025, 2, 1), T(2025, 3, 5)),
        _mid("2", "a", None, T(2025, 3, 5)),
        _mid("3", "a", T(2025, 3, 2), T(2025, 3, 5)),
    )
    assert _counts(share) == (1, 2)


# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------

MID_WITHIN_ROOT = WithinEntry(emit="mid", relationship=("root",), lifetime=True, target="2")
LEAF_WITHIN_MID = WithinEntry(emit="leaf", relationship=("mid",), date="l_day", target="3")
MID_START_LIFETIME = LifetimeEntry(emit="mid", start="m_start", end="m_end")


def _lines(within: Sequence[WithinEntry]) -> Dict[str, DateLine]:
    lines = date_lines([ROOT, MID_START_LIFETIME, LEAF_OPEN], within, STEP)
    return {line.rule_id: line for line in lines}


def test_every_level_of_a_chain_is_a_line_with_the_outer_level_implied() -> None:
    found = _lines([MID_WITHIN_ROOT, LEAF_WITHIN_MID])
    assert list(found) == [
        "lifetime:r_start.r_end",
        "lifetime:m_start.m_end",
        "within:root.lifetime",
        "within:mid.l_day",
        "within:mid.root.l_day",
    ]
    outer = found["within:mid.root.l_day"]
    assert outer.path == ("mid", "root") and outer.parent == "root"
    assert outer.target == Fraction(5)  # the sum of the levels' targets
    assert outer.tables == ("leaf", "mid", "root")
    assert ("mid", "m_start") in outer.reads and ("root", "r_end") in outer.reads
    assert [line.target for line in (found["within:mid.l_day"], found["within:root.lifetime"])] == [
        Fraction(3),
        Fraction(2),
    ]


def test_a_date_tie_on_the_middle_start_makes_no_implied_line_and_a_period_tie_does() -> None:
    dated = WithinEntry(emit="mid", relationship=("root",), date="m_start")
    leaf = WithinEntry(emit="leaf", relationship=("mid",), date="l_day")
    assert "within:mid.root.l_day" not in _lines([dated, leaf])
    period = WithinEntry(emit="mid", relationship=("root",), lifetime=True)
    assert "within:mid.root.l_day" in _lines([period, leaf])


def test_the_implied_level_is_enforced_when_every_level_is_enforced() -> None:
    found = _lines(
        [
            WithinEntry(emit="mid", relationship=("root",), lifetime=True),
            WithinEntry(emit="leaf", relationship=("mid",), date="l_day"),
        ]
    )
    assert found["within:mid.root.l_day"].target is None


def test_an_implied_level_with_one_targeted_level_takes_that_target() -> None:
    found = _lines(
        [
            WithinEntry(emit="mid", relationship=("root",), lifetime=True, target="1.5"),
            WithinEntry(emit="leaf", relationship=("mid",), date="l_day"),
        ]
    )
    assert found["within:mid.root.l_day"].target == Fraction(3, 2)


def test_an_entry_that_states_the_outer_level_makes_no_second_line() -> None:
    stated = WithinEntry(emit="leaf", relationship=("mid", "root"), date="l_day", target="1")
    found = _lines([MID_WITHIN_ROOT, LEAF_WITHIN_MID, stated])
    names = list(found)
    assert names.count("within:mid.root.l_day") == 1 and len(names) == 5
    assert found["within:mid.root.l_day"].target == Fraction(1)  # its own target holds


def test_a_chain_makes_an_outer_line_for_each_date_of_the_emit() -> None:
    leaf_life = LifetimeEntry(emit="leaf", start="l_at", end=None)
    mid_in_root = WithinEntry(emit="mid", relationship=("root",), lifetime=True)
    leaf_start = WithinEntry(emit="leaf", relationship=("mid",), date="l_at")
    other = WithinEntry(emit="leaf", relationship=("mid",), date="l_day")
    lines = date_lines(
        [ROOT, MID_START_LIFETIME, leaf_life], [mid_in_root, leaf_start, other], STEP
    )
    assert [line.rule_id for line in lines if line.emit == "leaf"] == [
        "within:mid.l_at",
        "within:mid.root.l_at",
        "within:mid.l_day",
        "within:mid.root.l_day",
    ]


def test_a_chain_scores_the_outer_level_on_the_row_two_links_away() -> None:
    rows = _rows(
        root=[_root("a", D(2025, 3, 1), D(2025, 3, 31))],
        mid=[_mid("1", "a", T(2025, 3, 5), T(2025, 3, 6))],
        leaf=[
            _leaf("1", "1", D(2025, 3, 5)),
            _leaf("2", "1", D(2025, 3, 10)),  # outside the mid lifetime and inside the root's
            _leaf("3", "1", D(2025, 4, 10)),  # outside both
        ],
    )
    within = [WithinEntry(emit="leaf", relationship=("mid",), date="l_day")]
    inner = _only([ROOT, MID, LEAF_OPEN], within + [PERIOD_FREE], rows, "within:mid.l_day")
    outer = _only([ROOT, MID, LEAF_OPEN], within + [PERIOD_FREE], rows, "within:mid.root.l_day")
    assert _counts(inner) == (1, 3)
    assert _counts(outer) == (2, 3)


PERIOD_FREE = WithinEntry(emit="mid", relationship=("root",), lifetime=True)


def test_the_rule_ids_of_an_emit_are_what_a_facts_entry_may_name() -> None:
    entries = [MID_WITHIN_ROOT, LEAF_WITHIN_MID]
    lifetimes = [ROOT, MID_START_LIFETIME, LEAF_OPEN]
    assert date_rule_ids(lifetimes, entries, STEP, "leaf") == (
        "within:mid.l_day",
        "within:mid.root.l_day",
    )
    assert date_rule_ids(lifetimes, entries, STEP, "root") == ("lifetime:r_start.r_end",)


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def _targeted(
    violating: int, of: int, target: str, tolerance: Optional[Fraction] = None
) -> RuleShare:
    entry = WithinEntry(emit="mid", relationship=("root",), date="m_start", target=target)
    rows = _rows(
        root=[_root("a", D(2025, 3, 1), D(2025, 3, 31))],
        mid=[
            _mid(str(n), "a", T(2025, 9, 1) if n < violating else T(2025, 3, 5), None)
            for n in range(of)
        ],
    )
    (line,) = [
        line
        for line in date_lines([ROOT, MID_OPEN], [entry], STEP)
        if line.rule_id == "within:root.m_start"
    ]
    (share,) = date_shares([replace(line, tolerance=tolerance)], rows)
    return share


def test_a_fact_with_a_target_is_met_near_the_target_and_missed_far_from_it() -> None:
    near = _targeted(47, 1000, "5")
    assert near.target == Fraction(5) and near.met is True
    far = _targeted(200, 1000, "5")
    assert far.met is False


def test_a_fact_with_a_target_is_not_missed_by_fewer_violations_than_the_target() -> None:
    assert _targeted(0, 1000, "0.1").met is True
    assert _targeted(0, 1000, "30").met is True


def test_a_tolerance_replaces_the_sampling_allowance() -> None:
    assert _targeted(60, 1000, "5", tolerance=Fraction(1, 200)).met is False
    assert _targeted(60, 1000, "5", tolerance=Fraction(2, 100)).met is True
    assert _targeted(60, 1000, "5", tolerance=Fraction(2, 100)).allowed == Fraction(2, 100)


def test_a_fact_with_a_zero_target_is_enforced_like_a_fact_with_none() -> None:
    share = _only(
        [ROOT, MID_OPEN],
        [WithinEntry(emit="mid", relationship=("root",), date="m_start")],
        _mid_in_root(_mid("1", "a", T(2026, 1, 1), None)),
        "within:root.m_start",
    )
    assert share.target is None and share.met is False


def test_the_sampling_allowance_is_exact_and_allows_no_violation_at_a_zero_target() -> None:
    assert sampling_allowance(Fraction(0), 1000) < Fraction(1, 1000)
    assert sampling_allowance(Fraction(5), 0) == 0
    wide = sampling_allowance(Fraction(50), 100)
    assert Fraction(19, 100) < wide < Fraction(21, 100)  # four sigma of 0.05
    assert sampling_allowance(Fraction(5), 10_000) < sampling_allowance(Fraction(5), 100)


def test_a_rule_that_covers_no_row_is_met_and_holds_nothing() -> None:
    share = _only([ROOT, MID_OPEN], [WITHIN_MID], _rows(root=[], mid=[]), "within:root.m_start")
    assert _counts(share) == (0, 0) and share.met is True


def test_a_fact_the_run_did_not_generate_promises_nothing_without_a_target() -> None:
    rows = _mid_in_root(_mid("1", "a", T(2026, 1, 1), None))
    share = _only([ROOT, MID_OPEN], [WITHIN_MID], rows, "within:root.m_start", generated=False)
    assert share.met is None and share.enforced is False


def test_the_document_of_a_targeted_fact_names_its_target_and_allowance() -> None:
    document = _targeted(47, 1000, "5", tolerance=Fraction(1, 50)).to_document()
    assert document["target"] == "5" and document["allowed"] == "0.02"
    assert document["rule"] == "within:root.m_start" and "band" not in document


@pytest.mark.parametrize(
    ("value", "end", "ok"),
    [
        (T(9999, 12, 31, 23, 59, 59), D(9999, 12, 31), True),
        (datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc), D(9999, 12, 31), True),
        (T(9999, 12, 31), D(9999, 12, 30), False),
        (T(1, 1, 1), D(1, 1, 1), True),
        (T(1, 1, 2), D(1, 1, 1), False),
        (T(1, 1, 1), D(9999, 12, 31), True),
    ],
)
def test_a_timestamp_against_a_date_end_at_either_end_of_the_calendar_never_overflows(
    value: datetime, end: date, ok: bool
) -> None:
    assert at_or_before_end(value, end) is ok


@pytest.mark.parametrize(
    ("value", "start", "ok"),
    [
        (T(1, 1, 1), D(1, 1, 1), True),
        (T(9999, 12, 31), D(9999, 12, 31), True),
        (T(9999, 12, 30, 23, 59, 59), D(9999, 12, 31), False),
        (D(1, 1, 1), T(1, 1, 1), True),
        (D(9999, 12, 31), T(9999, 12, 31, 23), True),
        (D(9999, 12, 30), T(9999, 12, 31), False),
    ],
)
def test_a_start_at_either_end_of_the_calendar_is_compared_without_overflow(
    value: Any, start: Any, ok: bool
) -> None:
    assert at_or_after_start(value, start) is ok


def test_a_zoned_timestamp_at_the_edge_of_the_calendar_is_compared_as_an_instant() -> None:
    from datetime import timedelta

    late = datetime(
        9999, 12, 31, 23, 0, tzinfo=timezone(timedelta(hours=-5))
    )  # past 9999-12-31 UTC
    assert at_or_before_end(late, D(9999, 12, 31)) is False
    assert at_or_before_end(late, D(9999, 12, 31)) is False
    early = datetime(1, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=5)))  # before 0001-01-01 UTC
    assert at_or_after_start(early, D(1, 1, 1)) is False
    assert is_open(D(1, 1, 1), D(9999, 12, 31)) is False
