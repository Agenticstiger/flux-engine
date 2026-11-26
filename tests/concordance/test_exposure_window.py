# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The exposure line counts a run's active days over the profile's window (ADR 0025 section 8).

A profile's ``activeDays`` are the whole days each parent is active inside the entry's ``window``.
The run's days are counted over the same window, clipped to the run's own ``spec.timeBounds``, so
a run that follows the declared rate exactly passes whatever its time bounds are.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from flux_engine.concordance.exposure_lines import ExposureLine, ExposureSource, exposure_checks
from flux_stats.reading import MS_PER_DAY

DAY = MS_PER_DAY
#: The profile's window is the first 100 days of the epoch; the run's time bounds are 200 days.
PROFILE_WINDOW = (0, 99 * DAY)
RUN_WINDOW = (0, 199 * DAY)


def _source() -> ExposureSource:
    return ExposureSource(
        child="child",
        relationship="link",
        parent="parent",
        from_columns=("pid",),
        to_columns=("id",),
        start="start",
        end="end",
    )


def _profile(children: Optional[int], days: Optional[int]) -> Any:
    def count(value: Optional[int]) -> Any:
        return None if value is None else SimpleNamespace(value=value)

    entry = SimpleNamespace(
        start="start",
        end="end",
        children=count(children),
        active_days=count(days),
        window_start=PROFILE_WINDOW[0],
        window_end=PROFILE_WINDOW[1],
    )
    relationship = SimpleNamespace(
        child_table="child",
        parent_table="parent",
        child_columns=("pid",),
        parent_columns=("id",),
        exposure=(entry,),
    )
    return SimpleNamespace(relationships=(relationship,))


def _check(profile: Any, parents: List[Dict[str, Any]], children: int) -> ExposureLine:
    tables = {
        "parent": parents,
        "child": [{"pid": parents[index % len(parents)]["id"]} for index in range(children)],
    }
    types = {"start": "DATE", "end": "DATE"}
    found = exposure_checks(
        [_source()], profile, lambda name: (tables[name], None), lambda _: types, RUN_WINDOW
    )
    (line,) = found.lines
    return line


def test_days_after_the_profiles_window_are_not_counted() -> None:
    # Ten parents active from day 0 with no end: 100 days each in the profile's window, 200 in the run's.
    parents = [{"id": index, "start": 0, "end": None} for index in range(10)]
    line = _check(_profile(50, 1000), parents, 50)
    assert (line.run_children, line.run_days) == (50, 1000)
    assert line.within and line.gap == 0


def test_a_run_that_follows_the_declared_rate_passes_with_longer_time_bounds() -> None:
    # Dates are whole days: five parents end on day 49 (50 days), five on day 150 (100 days in the window).
    parents = [{"id": index, "start": 0, "end": 49} for index in range(5)]
    parents += [{"id": 5 + index, "start": 0, "end": 150} for index in range(5)]
    line = _check(_profile(30, 750), parents, 30)
    assert line.run_days == 750
    assert line.within


def test_a_parent_starting_after_the_profiles_window_has_no_day() -> None:
    parents = [{"id": 0, "start": 0, "end": None}, {"id": 1, "start": 150, "end": None}]
    line = _check(_profile(2, 100), parents, 2)
    assert line.run_days == 100


def _not_scored(profile: Any) -> List[Any]:
    parents = [{"id": 0, "start": 0, "end": None}]
    tables = {"parent": parents, "child": [{"pid": 0}]}
    found = exposure_checks(
        [_source()],
        profile,
        lambda name: (tables[name], None),
        lambda _: {"start": "DATE", "end": "DATE"},
        RUN_WINDOW,
    )
    assert found.lines == ()
    return [reason for _, reason in found.not_scored]


def test_an_exposure_a_release_cut_to_one_count_says_which_count_it_misses() -> None:
    assert _not_scored(_profile(5, None)) == [
        "the profile holds no count of active days, so the exposure is not compared"
    ]
    assert _not_scored(_profile(None, 100)) == [
        "the profile holds no count of children, so the exposure is not compared"
    ]


def test_an_exposure_a_release_cut_to_no_count_is_not_listed() -> None:
    assert _not_scored(_profile(None, None)) == []
