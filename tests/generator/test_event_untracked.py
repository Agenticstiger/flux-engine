# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The events of a history with a change that moves no tracked column (ADR 0027 section 4).

The ``u`` of such a change takes ``type.values["u"]``; its ``before`` and ``after`` are equal, so a
``when`` over ``previous`` drops it. A history with no tracked column has only these changes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.generator.projections import sources_of
from flux_engine.seam.history import HistoryEnd

from .test_event_projection import (
    TYPES,
    event_rows,
    events_plan,
    fresh_sources,  # noqa: F401  (an autouse fixture)
)
from .test_history_end import (
    CLOSE_WITH,
    FINAL,
    entry,
    fresh_layouts,  # noqa: F401  (an autouse fixture)
    plans_for,
    run_of,
)
from .test_history_layout import root_for
from .test_history_tracked import BOTH, PLAN_ONLY, tracked_entry

pytestmark = pytest.mark.unit

WITH_U = (*TYPES, ("u", "unchanged"))


def _plans(end: Optional[HistoryEnd] = None, **options: Any) -> Dict[str, Any]:
    shown = tracked_entry(PLAN_ONLY, end=end, activation="activated", **options)
    return plans_for(shown, rows=80, entities=24, priced=True)


@pytest.mark.parametrize("end", [None, FINAL, CLOSE_WITH])
def test_a_change_that_moves_no_column_takes_the_u_text(end: Optional[HistoryEnd]) -> None:
    plans = _plans(end, untracked="0.5")
    data = run_of(plans)
    plan = events_plan(plans, typed=True, type_values=WITH_U)
    rows = event_rows(plan)
    layout = sources_of(plan, root_for())
    seen = set()
    for row, about, before in zip(rows, layout.about.tolist(), layout.before.tolist(), strict=True):
        if row["op"] != "u":
            continue
        old, new = data[before], data[about]
        if end is not None and new["status"] == "gone":
            assert row["change_type"] == "ended"
        elif old["plan"] == new["plan"]:
            assert row["change_type"] == "unchanged"
            assert row["before_plan"] == row["after_plan"]
        else:
            assert row["change_type"] == "plan_change"
        seen.add(row["change_type"])
    assert {"unchanged", "plan_change"} <= seen


def test_a_when_over_previous_drops_the_events_that_change_nothing() -> None:
    plans = _plans(None, untracked="0.5")
    rows = event_rows(events_plan(plans, on=("u",), when="plan <> previous.plan"))
    assert rows and all(row["before_plan"] != row["after_plan"] for row in rows)
    every = event_rows(events_plan(plans, on=("u",)))
    assert len(rows) < len(every)


def test_a_type_with_no_text_for_u_is_refused_at_generate() -> None:
    plans = _plans(None, untracked="0.5")
    with pytest.raises(FluxError) as caught:
        event_rows(events_plan(plans, typed=True))
    assert caught.value.code.code == "FLX-3403"


def test_a_history_that_tracks_nothing_has_only_u_changes_and_u_may_be_declared() -> None:
    plans = plans_for(entry(activation="activated"), rows=80, entities=24, priced=True)
    rows = event_rows(events_plan(plans, typed=True, type_values=WITH_U))
    assert {row["change_type"] for row in rows if row["op"] == "u"} == {"unchanged"}


def test_a_history_that_tracks_two_columns_reads_u_as_the_rest() -> None:
    shown = tracked_entry(BOTH, activation="activated", untracked="0.4")
    plans = plans_for(shown, rows=80, entities=24, priced=True)
    rows = event_rows(events_plan(plans, typed=True, type_values=WITH_U))
    seen = {row["change_type"] for row in rows}
    assert {"plan_change", "tier_change", "unchanged"} <= seen


# -- the seam's check of the type -----------------------------------------------------------------


def _checked(
    monkeypatch: pytest.MonkeyPatch, values: Any, *, tracked: Any, untracked: Optional[str]
) -> None:
    from types import SimpleNamespace

    from flux_engine.core.errors import get
    from flux_engine.seam import projections as seam

    from .test_event_projection import events_entry

    monkeypatch.setattr(seam, "_check_text_column", lambda *args: None)
    held = tracked_entry(tracked, untracked=untracked) if tracked else entry()
    target = SimpleNamespace(history=held)
    seam._check_type(
        events_entry(type_column="change_type", type_values=values, on=("c", "u", "d")),
        target,  # type: ignore[arg-type]
        "events[0]",
        get("FLX-3403"),
    )


BASE = (("c", "created"), ("d", "deleted"), ("plan", "plan_change"))


def test_u_is_required_beside_a_share_and_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _checked(monkeypatch, (*BASE, ("u", "unchanged")), tracked=PLAN_ONLY, untracked="0.5")
    with pytest.raises(FluxError, match="no text for 'u'"):
        _checked(monkeypatch, BASE, tracked=PLAN_ONLY, untracked="0.5")


def test_u_is_refused_beside_no_share_and_one_tracked_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _checked(monkeypatch, BASE, tracked=PLAN_ONLY, untracked=None)
    with pytest.raises(FluxError, match="names 'u'"):
        _checked(monkeypatch, (*BASE, ("u", "unchanged")), tracked=PLAN_ONLY, untracked=None)


def test_u_is_allowed_and_not_required_beside_no_tracked_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _checked(monkeypatch, (("c", "created"), ("d", "deleted")), tracked=None, untracked=None)
    _checked(
        monkeypatch,
        (("c", "created"), ("d", "deleted"), ("u", "unchanged")),
        tracked=None,
        untracked=None,
    )
