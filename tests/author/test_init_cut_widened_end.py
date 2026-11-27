# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A date pair that had a range widened is read, though the table's lifetime later dates the column.

A gym's tier versions are valid from a start to an end, and the members they follow join on some
day. The release publishes how many days after the member joined a version ends, and that end
(an open end of the year 9999 for most versions) lies entirely after every start, so the window
needs no lifetime on the first pass. The pair dates the end by a ``within`` entry, whose range the
plan asks to be widened to the member's lifetime; with the range widened the end no longer lies
after every start, so the next pass lays the window out as a lifetime and the entry is not written.

The widened range stays, so the pair is what decides it: it is read, and a release cut by the
unused list keeps it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from ._written import generated
from .test_history_date_pairs import TIERS
from .test_history_date_pairs import _profile as _tier_profile
from .test_history_init import _bins, _init, _levels, _metrics, _ms, _namespace, _states

pytestmark = pytest.mark.integration

#: The members carry the day they joined and the day they left; the versions' window is checked.
CONTRACT = TIERS.replace(
    """        - { name: member_id, type: INT, required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
""",
    """        - { name: member_id, type: INT, required: true }
        - { name: joined_on, type: DATE, required: true }
        - { name: left_on, type: DATE, required: false }
    semantics:
      dimensions:
        - { name: joined_on, type: time }
        - { name: left_on, type: time }
      entities:
        - { name: member, type: primary, expr: member_id }
""",
    1,
).replace(
    "        - { name: renewed_on, type: DATE, required: false }\n",
    "        - { name: renewed_on, type: DATE, required: false }\n"
    "      invariants:\n"
    '        - { id: window_ordered, check: "valid_to IS NULL OR valid_to >= valid_from" }\n',
    1,
)
assert CONTRACT != TIERS

ORDER = ["after", "before", "same"]
OPEN_END = _ms(9999, 12, 31)
#: The days from the day a member joined to the end of a version.
ENDS_AFTER_JOINING = "/relationships/0/dates/1"


def _link(
    child: str, parent: str, days: List[int], counts: List[int], order: List[int]
) -> Dict[str, Any]:
    """The days from the member's *parent* date to the version's *child* date."""
    return {
        "child": child,
        "parent": parent,
        "order": _levels(ORDER, order, sum(order)),
        "statistics": [_bins([str(day) for day in days], counts, sum(counts))],
    }


def _profile() -> Dict[str, Any]:
    profile = _tier_profile()
    version = profile["tables"]["tier_version"]
    del version["history"]
    ends = version["columns"]["valid_to"]
    ends["statistics"] = [_bins([_ms(2099, 1, 1), _ms(2100, 1, 1), OPEN_END], [300, 700], 1000)]
    ends["metrics"]["min"]["value"] = _ms(2099, 1, 1)
    ends["metrics"]["max"]["value"] = OPEN_END
    starts = [_ms(2025, month, 1) for month in (1, 3, 5, 7, 9)]
    version["columns"]["valid_from"]["statistics"] = [_bins(starts, [250, 250, 250, 250], 1000)]
    member = profile["tables"]["gym_member"]
    member["columns"]["joined_on"] = {
        "domain": "timestamp",
        "metrics": _metrics(_ms(2021, 1, 1), _ms(2024, 6, 1), 1000),
    }
    member["columns"]["left_on"] = {
        "domain": "timestamp",
        "metrics": _metrics(_ms(2021, 6, 1), _ms(2025, 6, 1), 600),
    }
    member["datePairs"] = [
        {
            "start": "joined_on",
            "end": "left_on",
            "order": _levels(ORDER, [600, 0, 0], 600),
            "statistics": [_bins(["1", "90", "365", "730"], [200, 300, 100], 600)],
        }
    ]
    profile["relationships"] = [
        {
            "parent": {"table": "gym_member", "columns": ["member_id"]},
            "child": {"table": "tier_version", "columns": ["member_id"]},
            "statistics": [_bins(["0", "1", "2", "3", "6"], [0, 500, 300, 200], 1000)],
            "dates": [
                _link("valid_from", "joined_on", [181, 366, 731], [400, 600], [1000, 0, 0]),
                _link(
                    "valid_to",
                    "joined_on",
                    [366, 731, 1461, 3653, 3652061],
                    [200, 300, 300, 200],
                    [1000, 0, 0],
                ),
                _link("valid_from", "left_on", [1, 181, 366], [300, 300], [0, 600, 0]),
                _link("valid_to", "left_on", [1, 181, 366, 3652061], [200, 200, 200], [0, 600, 0]),
            ],
        }
    ]
    return profile


def test_the_pair_that_had_a_range_widened_is_read_though_a_lifetime_dates_the_column(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _profile(), CONTRACT)
    namespace = _namespace(tmp_path)
    # The end is laid out as the table's lifetime, and no entry dates it.
    assert [item["end"] for item in namespace["lifetimes"] if item["emit"] == "tier_version"] == [
        "valid_to"
    ]
    assert all(item.get("date") != "valid_to" for item in namespace.get("within", []))
    listed = [item["statistic"]["pointer"] for item in namespace["unused"]]
    assert ENDS_AFTER_JOINING not in listed
    assert ENDS_AFTER_JOINING + "/statistics/0" not in listed
    state = _states(result)[ENDS_AFTER_JOINING + "/statistics/0"]
    assert state["state"] == "used"
    assert "ask for its declared range to be widened" in state["note"]


def test_a_release_cut_by_the_unused_list_gives_the_bundle_of_the_whole_profile(
    tmp_path: Path,
) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    profile = _profile()
    whole = tmp_path / "whole"
    whole.mkdir()
    _init(whole, profile, CONTRACT)
    listed = [item["statistic"]["pointer"] for item in _namespace(whole)["unused"]]
    cut = tmp_path / "cut"
    cut.mkdir()
    _init(cut, _drop(profile, listed), CONTRACT)
    assert generated(_namespace(cut)) == generated(_namespace(whole))


def test_the_pair_alone_decides_the_widened_range(tmp_path: Path) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    # Without the pair the end is not widened, and the table has no lifetime for it.
    whole = tmp_path / "whole"
    whole.mkdir()
    _init(whole, _profile(), CONTRACT)
    alone = tmp_path / "alone"
    alone.mkdir()
    _init(alone, _drop(_profile(), [ENDS_AFTER_JOINING]), CONTRACT)
    assert generated(_namespace(alone)) != generated(_namespace(whole))


def test_a_declined_entry_gives_the_widening_back_and_its_statistic_is_unused_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.author import bundle

    # The plan asks for the range to be widened, and the next pass declines the entry that asked:
    # the range is given back, so nothing of it rests on the pair and the pair is listed again.
    real = bundle._refuse_unless_widened

    def declining(files: Any, exposes: Any, widened: Any, contract: Any = None) -> None:
        if widened:
            raise bundle._WithinDeclined({key: "declined for the test" for key in widened})
        real(files, exposes, widened, contract)

    monkeypatch.setattr(bundle, "_refuse_unless_widened", declining)
    result = _init(tmp_path, _profile(), CONTRACT)
    state = _states(result)[ENDS_AFTER_JOINING + "/statistics/0"]
    assert state["state"] == "unused"
    assert "declined for the test" in state["note"]
    assert "ask for its declared range to be widened" not in state["note"]
