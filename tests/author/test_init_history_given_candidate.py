# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history candidate that is drawn given another column is carried, and so is the column it is drawn given.

A tracked column takes its values from the history alone: its own given draw is never read. So a
candidate drawn given another column cannot be tracked, and nor can the column another candidate is
drawn given, whose first value the history would move under it. ``init`` carries both, and the
history is written with the rest of its entry, where it used to be declined for the whole bundle.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from flux_engine.core.errors import FluxError

from ._written import generated, plan_written
from .test_history_init import (
    HISTORY,
    TIER,
    _entries,
    _init,
    _levels,
    _namespace,
    _profile,
    _states,
    _transitions,
    _two_columns,
)

pytestmark = pytest.mark.integration

CARRIED_GIVEN = "the column is drawn given another, and a tracked column takes its values"
CARRIED_PARENT = "another column is drawn given it, and a carried column keeps its first value"


def _given(
    levels: List[str], child: str, parent: str, rows: Dict[str, List[int]]
) -> Dict[str, Any]:
    """The statistic of *child*'s levels by the levels of *parent*."""
    node = _transitions(parent, levels, rows)
    node["groupBy"] = parent
    return node


def _tier_given_channel(profile: Dict[str, Any]) -> Dict[str, Any]:
    found = copy.deepcopy(profile)
    found["tables"]["plan_version"]["columns"]["tier"]["statistics"].append(
        _given(TIER, "tier", "channel", {"app": [700, 300, 100], "web": [300, 300, 150]})
    )
    return found


def _renamed(profile: Dict[str, Any], contract: str) -> Any:
    """*profile* and *contract* with ``channel`` named ``zone``, which sorts after ``tier``."""
    found = json.loads(json.dumps(profile).replace("channel", "zone"))
    history = found["tables"]["plan_version"]["history"]
    history["reasons"] = _levels(["several", "tier", "zone"], [50, 800, 100], 850)
    return found, contract.replace("channel", "zone")


def _both_move(profile: Dict[str, Any]) -> Dict[str, Any]:
    return _tier_given_channel(_two_columns(profile))


def _note(result: Any, column: str, key: str) -> Dict[str, Any]:
    return _states(result)[f"{HISTORY}/columns/{column}/{key}"]


def test_a_candidate_drawn_given_another_column_is_carried_and_the_history_is_written(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _both_move(_profile()))
    (entry,) = _entries(tmp_path)
    assert "tracked" not in entry
    for key in ("initial", "transitions"):
        assert CARRIED_GIVEN in _note(result, "tier", key)["note"]
        assert _note(result, "tier", key)["state"] == "unused"
        assert CARRIED_PARENT in _note(result, "channel", key)["note"]
        assert _note(result, "channel", key)["state"] == "unused"
    # The grouped statistic still draws the column, as it did before the history.
    assert _states(result)["/tables/plan_version/columns/tier/statistics/1"]["state"] == "used"
    assert _states(result)[f"{HISTORY}/versionsPerEntity"]["state"] == "used"
    plan_written(tmp_path)


def test_the_carrying_does_not_depend_on_how_the_columns_sort(tmp_path: Path) -> None:
    from .test_history_init import CONTRACT

    profile, contract = _renamed(_both_move(_profile()), CONTRACT.read_text(encoding="utf-8"))
    result = _init(tmp_path, profile, contract)
    (entry,) = _entries(tmp_path)
    assert "tracked" not in entry
    # The forest draws the column that sorts first from the other, so the roles swap with the name.
    assert CARRIED_PARENT in _note(result, "tier", "transitions")["note"]
    assert CARRIED_GIVEN in _note(result, "zone", "transitions")["note"]
    plan_written(tmp_path)


def test_a_candidate_drawn_given_a_column_that_is_no_candidate_is_carried(tmp_path: Path) -> None:
    profile = _tier_given_channel(_profile())
    del profile["tables"]["plan_version"]["history"]["columns"]["channel"]
    del profile["tables"]["plan_version"]["history"]["reasons"]
    result = _init(tmp_path, profile)
    (entry,) = _entries(tmp_path)
    assert "tracked" not in entry
    assert CARRIED_GIVEN in _note(result, "tier", "transitions")["note"]
    plan_written(tmp_path)


def test_a_candidate_that_never_moves_and_is_drawn_given_one_that_does_is_carried(
    tmp_path: Path,
) -> None:
    profile = _profile()
    columns = profile["tables"]["plan_version"]["history"]["columns"]
    # channel moves; tier never does, and is drawn given channel.
    columns["channel"]["transitions"] = _transitions(
        "channel", ["app", "web"], {"app": [0, 500], "web": [350, 0]}
    )
    columns["tier"]["transitions"] = _transitions(
        "tier", TIER, {"basic": [0, 0, 0], "plus": [0, 0, 0], "pro": [0, 0, 0]}
    )
    profile["tables"]["plan_version"]["history"]["reasons"] = _levels(
        ["channel", "several", "tier"], [850, 0, 0], 850
    )
    profile = _tier_given_channel(profile)
    result = _init(tmp_path, profile)
    (entry,) = _entries(tmp_path)
    assert "tracked" not in entry
    assert "no published change moves the column" in _note(result, "tier", "transitions")["note"]
    assert CARRIED_PARENT in _note(result, "channel", "transitions")["note"]
    plan_written(tmp_path)


def test_a_release_cut_by_the_unused_list_gives_the_same_generated_bundle(tmp_path: Path) -> None:
    profile = _both_move(_profile())
    whole = tmp_path / "whole"
    whole.mkdir()
    _init(whole, profile)
    from tests.cli.test_init_ledger_entries import _drop

    listed = [item["statistic"]["pointer"] for item in _namespace(whole)["unused"]]
    assert f"{HISTORY}/columns/tier" in listed
    cut = tmp_path / "cut"
    cut.mkdir()
    _init(cut, _drop(profile, listed))
    assert generated(_namespace(cut)) == generated(_namespace(whole))


def test_a_never_moving_candidate_drawn_given_a_column_keeps_its_text_and_bytes(
    tmp_path: Path,
) -> None:
    # Neither column moves: the checks for a carried column come after the one for a column that
    # moves no change, so its sentence and the bundle are what they were.
    profile = _tier_given_channel(_profile())
    columns = profile["tables"]["plan_version"]["history"]["columns"]
    columns["tier"]["transitions"] = _transitions(
        "tier", TIER, {"basic": [0, 0, 0], "plus": [0, 0, 0], "pro": [0, 0, 0]}
    )
    result = _init(tmp_path, profile)
    for column in ("tier", "channel"):
        assert (
            "no published change moves the column" in _note(result, column, "transitions")["note"]
        )
    assert CARRIED_GIVEN not in str(_states(result))
    assert CARRIED_PARENT not in str(_states(result))


def test_a_hand_written_bundle_that_tracks_a_column_given_another_is_still_refused(
    tmp_path: Path,
) -> None:
    from tests.seam.test_history import ENTRY, bundle_with, resolved

    columns = {"seats": {"given": ["plan"]}}
    tracked = [{"column": "seats", "transitions": {"a": {"b": "1"}}}]
    entry = {**ENTRY, "tracked": tracked}
    with pytest.raises(FluxError, match="not categorical") as caught:
        resolved(bundle_with(tmp_path, "tracked", [entry], columns=columns))
    assert caught.value.code.code == "FLX-3403"


def test_an_end_marker_column_drawn_given_another_is_declined_and_the_bundle_validates(
    tmp_path: Path,
) -> None:
    from .test_init_end_marker_share import MARKER, STATUS, _entry, _forge_shaped, _lifecycle

    profile = _forge_shaped()
    profile["tables"]["tier_version"]["columns"]["status"]["statistics"].append(
        _given(
            STATUS,
            "status",
            "tier",
            {"basic": [300, 100, 100], "plus": [300, 100, 100], "pro": [300, 100, 100]},
        )
    )
    result = _init(tmp_path, profile, _lifecycle(**MARKER))
    entry = _entry(tmp_path)
    assert "end" not in entry
    assert "tracked" not in entry
    assert "drawn given" in result.stdout
    plan_written(tmp_path)
