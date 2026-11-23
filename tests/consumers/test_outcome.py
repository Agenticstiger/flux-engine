# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A query's rate set against its target: every status, the groups, the two sets apart (ADR 0028 sections 4 and 7).

The profiles are the files of ``tests/fixtures/consumers``: the pinned profile holds the rates of the two
calibration queries (``premium-renewed`` at 180 of 1,200 and ``renewed-offered`` at 590 of 900), and the
held-out profile holds the held-out query's (720 of 1,200). Counts are written by hand, as the runner
returns them, so nothing here needs DuckDB.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import pytest

from flux_engine.concordance.queries import CALIBRATION, HELD_OUT, QueryRow
from flux_engine.consumers.outcome import (
    NO_TARGET,
    VERSIONS_DIFFER,
    WITHHELD,
    profile_targets,
    query_rows,
    require_apart,
)
from flux_engine.consumers.runner import QueryCounts
from flux_engine.core.errors import FluxError
from flux_engine.seam.consumers import Calibration, ConsumerQuery, ConsumerRegistry
from flux_stats.profile import Profile, load_profile_text

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "consumers"
DIGEST = "sha256:" + "0" * 64


def _document(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _profile(document: Dict[str, Any]) -> Profile:
    return load_profile_text(json.dumps(document))[0]


PINNED = _profile(_document("subscriptions.profile.json"))
HELD_OUT_PROFILE = _profile(_document("subscriptions.held-out-profile.json"))


def counted(
    query: str,
    rows: Optional[int],
    unit_rows: Optional[int],
    row_groups: Optional[Sequence[Tuple[Optional[str], int]]] = None,
    unit_groups: Optional[Sequence[Tuple[Optional[str], int]]] = None,
    status: str = "counted",
    reason: Optional[str] = None,
) -> QueryCounts:
    return QueryCounts(
        id=query,
        status=status,
        reason=reason,
        rows=rows,
        unit_rows=unit_rows,
        row_groups=tuple(row_groups) if row_groups is not None else None,
        unit_groups=tuple(unit_groups) if unit_groups is not None else None,
        duckdb_version="2.0.1",
        sqlglot_version="30.33.0",
    )


def registry(
    *queries: ConsumerQuery, held_out_tolerance: Optional[Fraction] = None
) -> ConsumerRegistry:
    return ConsumerRegistry(
        queries=tuple(queries),
        calibration=Calibration(held_out_tolerance=held_out_tolerance),
    )


def one(
    query: ConsumerQuery,
    counts: Optional[QueryCounts],
    *,
    pinned: Optional[Profile] = PINNED,
    held_out: Optional[Profile] = HELD_OUT_PROFILE,
    held_out_tolerance: Optional[Fraction] = None,
) -> QueryRow:
    rows = query_rows(
        registry(query, held_out_tolerance=held_out_tolerance),
        {query.id: DIGEST},
        None if counts is None else [counts],
        profile_targets(pinned) if pinned is not None else None,
        profile_targets(held_out) if held_out is not None else None,
    )
    assert len(rows) == 1
    return rows[0]


PREMIUM = ConsumerQuery(
    id="premium-renewed", sql="", unit="subscriptions", tolerance=Fraction(1, 50)
)


def test_the_targets_are_the_true_count_over_k_of_each_entry() -> None:
    assert profile_targets(PINNED)["premium-renewed"][0].groups[""].rate == Fraction(180, 1200)
    assert profile_targets(PINNED)["renewed-offered"][0].groups[""].rate == Fraction(590, 900)
    assert profile_targets(HELD_OUT_PROFILE)["renewed-since"][0].groups[""].rate == Fraction(3, 5)
    assert "renewed-since" not in profile_targets(PINNED)


@pytest.mark.parametrize(
    ("rows", "met", "gap"),
    [
        (180, True, Fraction(0)),
        (204, True, Fraction(1, 50)),  # exactly the tolerance: within, since the gap may equal it
        (205, False, Fraction(205 - 180, 1200)),
        (150, False, Fraction(-30, 1200)),
    ],
)
def test_a_measured_rate_is_within_when_its_gap_is_at_most_the_tolerance(
    rows: int, met: bool, gap: Fraction
) -> None:
    row = one(PREMIUM, counted("premium-renewed", rows, 1200))
    assert (row.status, row.met, row.set) == ("measured", met, CALIBRATION)
    assert row.rate == Fraction(rows, 1200)
    assert row.target == Fraction(3, 20)
    assert row.tolerance == Fraction(1, 50)
    assert row.gap == Fraction(rows - 180, 1200)
    assert (row.rows, row.unit_rows, row.digest) == (rows, 1200, DIGEST)


def test_a_query_with_no_entry_in_the_profile_is_no_target_and_is_in_no_share() -> None:
    query = ConsumerQuery(id="elsewhere", sql="", unit="subscriptions", tolerance=Fraction(1, 50))
    row = one(query, counted("elsewhere", 10, 100))
    assert (row.status, row.met, row.target, row.gap) == (NO_TARGET, None, None, None)
    assert row.rate == Fraction(1, 10) and not row.in_share


def test_a_cell_the_profile_withheld_is_withheld_not_zero() -> None:
    document = _document("subscriptions.profile.json")
    document["queries"][0]["statistics"][0]["groups"] = [{"group": "", "status": "S"}]
    row = one(PREMIUM, counted("premium-renewed", 180, 1200), pinned=_profile(document))
    assert (row.status, row.target, row.met) == (WITHHELD, None, None)


def test_a_query_with_no_tolerance_is_compared_with_no_bound() -> None:
    query = ConsumerQuery(id="premium-renewed", sql="", unit="subscriptions")
    row = one(query, counted("premium-renewed", 180, 1200))
    assert (row.status, row.gap, row.tolerance, row.met) == ("measured", 0, None, None)
    assert not row.in_share


def test_a_held_out_query_takes_its_target_from_the_held_out_profile_only() -> None:
    query = ConsumerQuery(id="renewed-since", sql="", unit="subscriptions", held_out=True)
    row = one(
        query,
        counted("renewed-since", 720, 1200),
        pinned=None,
        held_out_tolerance=Fraction(1, 50),
    )
    assert (row.set, row.target, row.gap, row.met) == (HELD_OUT, Fraction(3, 5), 0, True)
    assert row.tolerance == Fraction(1, 50)
    # With no held-out profile there is no target, however the pinned profile looks.
    absent = one(query, counted("renewed-since", 720, 1200), held_out=None)
    assert (absent.status, absent.target, absent.met) == (NO_TARGET, None, None)


def test_a_held_outs_own_tolerance_outranks_the_registrys() -> None:
    query = ConsumerQuery(
        id="renewed-since", sql="", unit="subscriptions", held_out=True, tolerance=Fraction(1, 1000)
    )
    row = one(query, counted("renewed-since", 730, 1200), held_out_tolerance=Fraction(1, 2))
    assert row.tolerance == Fraction(1, 1000) and row.met is False


def test_a_calibration_query_never_takes_the_registrys_held_out_tolerance() -> None:
    query = ConsumerQuery(id="premium-renewed", sql="", unit="subscriptions")
    row = one(query, counted("premium-renewed", 1, 1200), held_out_tolerance=Fraction(1, 2))
    assert row.tolerance is None and row.met is None


def test_more_rows_than_the_unit_holds_is_not_a_rate_and_is_never_compared() -> None:
    row = one(PREMIUM, counted("premium-renewed", 1300, 1200))
    assert (row.status, row.rate, row.gap, row.met) == ("notARate", None, None, None)
    assert row.target == Fraction(3, 20), "the target is shown; nothing is compared with it"


def test_an_empty_unit_with_no_rows_is_no_rows() -> None:
    row = one(PREMIUM, counted("premium-renewed", 0, 0))
    assert (row.status, row.rate, row.met) == ("noRows", None, None)


def test_a_query_without_a_unit_is_checked_for_rows_only() -> None:
    query = ConsumerQuery(id="premium-renewed", sql="")
    returned = one(query, counted("premium-renewed", 12, None))
    assert (returned.status, returned.rows, returned.unit_rows, returned.rate, returned.met) == (
        "measured",
        12,
        None,
        None,
        None,
    )
    empty = one(query, counted("premium-renewed", 0, None))
    assert (empty.status, empty.rows) == ("noRows", 0)


@pytest.mark.parametrize("status", ["timedOut", "outOfMemory", "crashed", "error"])
def test_a_query_that_did_not_finish_takes_the_runners_status(status: str) -> None:
    row = one(
        PREMIUM, counted("premium-renewed", None, None, status=status, reason="the query ended")
    )
    assert (row.status, row.rows, row.rate, row.met, row.reason) == (
        status,
        None,
        None,
        None,
        "the query ended",
    )


def test_recorded_versions_that_differ_leave_a_query_never_compared() -> None:
    row = one(PREMIUM, None)
    assert (row.status, row.rows, row.rate, row.target, row.met) == (
        VERSIONS_DIFFER,
        None,
        None,
        None,
        None,
    )
    assert row.digest == DIGEST


# ---------------------------------------------------------------- groups


def _grouped_profile() -> Profile:
    document = _document("subscriptions.profile.json")
    statistic = document["queries"][1]["statistics"][0]
    statistic["groupBy"] = "plan"
    statistic["groups"] = [
        {"group": "basic", "k": 500, "levelCounts": [200, 300], "status": "F"},
        {"group": "premium", "k": 400, "levelCounts": [110, 290], "status": "F"},
        {"group": "standard", "status": "S"},
    ]
    return _profile(document)


GROUPED = ConsumerQuery(
    id="renewed-offered",
    sql="",
    unit="offered",
    group_by="plan",
    tolerance=Fraction(1, 20),
)


def test_a_grouped_rate_is_taken_per_group_with_the_null_group_first() -> None:
    counts = counted(
        "renewed-offered",
        rows=700,
        unit_rows=910,
        row_groups=[(None, 5), ("basic", 300), ("premium", 290), ("standard", 105)],
        unit_groups=[(None, 10), ("basic", 500), ("premium", 400)] + [("standard", 0)],
    )
    row = one(GROUPED, counts, pinned=_grouped_profile())
    groups = {group.group: group for group in row.groups}
    assert [(g.group, g.null_group) for g in row.groups] == [
        (0, True),
        (1, False),
        (2, False),
        (3, False),
    ]
    # The null group has no level to look a target up by.
    assert (groups[0].rate, groups[0].target, groups[0].status) == (
        Fraction(1, 2),
        None,
        NO_TARGET,
    )
    assert (groups[1].rate, groups[1].target, groups[1].gap, groups[1].met) == (
        Fraction(3, 5),
        Fraction(3, 5),
        0,
        True,
    )
    assert (groups[2].rate, groups[2].target, groups[2].gap, groups[2].met) == (
        Fraction(29, 40),
        Fraction(29, 40),
        0,
        True,
    )
    # A group the query returned rows for in an empty unit group is no rate, whatever its target.
    assert (groups[3].status, groups[3].target, groups[3].met) == ("notARate", None, None)
    assert row.status == "notARate", "a group that is not a rate makes the query's status so"
    assert (row.rows, row.unit_rows, row.rate) == (700, 910, Fraction(70, 91))
    assert (row.target, row.gap) == (None, None), "a grouped query's target is its groups'"
    assert row.met is True


def test_a_grouped_query_is_outside_when_one_group_is() -> None:
    counts = counted(
        "renewed-offered",
        rows=330,
        unit_rows=900,
        row_groups=[("basic", 200), ("premium", 130)],
        unit_groups=[("basic", 500), ("premium", 400)],
    )
    row = one(GROUPED, counts, pinned=_grouped_profile())
    by_level = {g.group: g for g in row.groups}
    assert by_level[0].met is False and by_level[0].gap == Fraction(2, 5) - Fraction(3, 5)
    assert by_level[1].met is False
    assert row.met is False and row.status == "measured"


def test_a_grouped_query_with_only_an_ungrouped_entry_has_no_group_targets() -> None:
    counts = counted(
        "renewed-offered",
        rows=590,
        unit_rows=900,
        row_groups=[("basic", 300), ("premium", 290)],
        unit_groups=[("basic", 500), ("premium", 400)],
    )
    row = one(GROUPED, counts)  # the pinned profile's entry for it is ungrouped
    assert [g.status for g in row.groups] == [NO_TARGET, NO_TARGET]
    assert (row.status, row.met) == (NO_TARGET, None)


def test_a_grouped_query_whose_groups_were_all_withheld_says_withheld() -> None:
    document = _document("subscriptions.profile.json")
    statistic = document["queries"][1]["statistics"][0]
    statistic["groupBy"] = "plan"
    statistic["groups"] = [
        {"group": "basic", "status": "S"},
        {"group": "premium", "status": "S"},
    ]
    counts = counted(
        "renewed-offered",
        rows=590,
        unit_rows=900,
        row_groups=[("basic", 300), ("premium", 290)],
        unit_groups=[("basic", 500), ("premium", 400)],
    )
    row = one(GROUPED, counts, pinned=_profile(document))
    assert (row.status, row.met) == (WITHHELD, None)


def test_a_grouped_query_over_an_empty_unit_is_no_rows() -> None:
    row = one(GROUPED, counted("renewed-offered", 0, 0, row_groups=[], unit_groups=[]))
    assert (row.status, row.groups, row.met) == ("noRows", (), None)


# ---------------------------------------------------------------- the two sets apart


HELD = ConsumerQuery(id="renewed-since", sql="", unit="subscriptions", held_out=True)
CALIB = ConsumerQuery(id="premium-renewed", sql="", unit="subscriptions")


def test_a_pinned_profile_that_names_a_held_out_query_is_refused_naming_it() -> None:
    document = _document("subscriptions.profile.json")
    document["queries"].append(_document("subscriptions.held-out-profile.json")["queries"][0])
    with pytest.raises(FluxError) as caught:
        require_apart(registry(CALIB, HELD), _profile(document), None)
    assert caught.value.code.code == "FLX-3701"
    assert "'renewed-since'" in str(caught.value)


def test_a_held_out_profile_that_names_a_calibration_query_is_refused_naming_it() -> None:
    document = _document("subscriptions.held-out-profile.json")
    document["queries"].append(_document("subscriptions.profile.json")["queries"][0])
    with pytest.raises(FluxError) as caught:
        require_apart(registry(CALIB, HELD), PINNED, _profile(document))
    assert caught.value.code.code == "FLX-3702"
    assert "'premium-renewed'" in str(caught.value)


def test_a_held_out_profile_for_a_bundle_with_no_held_out_query_is_refused() -> None:
    with pytest.raises(FluxError) as caught:
        require_apart(registry(CALIB), PINNED, HELD_OUT_PROFILE)
    assert caught.value.code.code == "FLX-3702"
    assert "no held-out query" in str(caught.value)


def test_the_two_profiles_apart_are_accepted() -> None:
    require_apart(registry(CALIB, HELD), PINNED, HELD_OUT_PROFILE)
    require_apart(registry(CALIB, HELD), None, None)
    # A held-out profile may name a query the bundle does not declare: it answers what it is asked.
    document = _document("subscriptions.held-out-profile.json")
    document["queries"].append({**document["queries"][0], "name": "other"})
    require_apart(registry(CALIB, HELD), PINNED, _profile(document))
