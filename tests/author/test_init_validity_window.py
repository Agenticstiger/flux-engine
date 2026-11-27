# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A contract's validity window keeps its order when a published pair lays out one of its dates.

A versioned table's window runs from a start to an end, and every version ends on or after its
start. A table declares one lifetime. Where the release publishes the days between the end and
another column (a column of the gym's members, say, which is enrolled on some day) and no pair
for the window itself, that pair laid the end out from the other column, the start was drawn on
its own, and a share of the versions ended before they started.

Pinned here, through ``run_init`` and a run of the bundle it writes, on the tier history of a
gym's members:

* the window takes the lifetime, with gaps derived from the published histograms of its two dates
  (or drawn evenly), and the pair that would have laid the date out is unused, with a reason;
* a pair the release publishes on the start's day does not take it back, and neither does a pair
  the release withholds for the window itself;
* the window's own published pair, a history that is written, and a window the histograms already
  keep in order stay as they were.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ._written import emitted_rows
from .test_history_init import _bins, _levels, _ms, _states
from .test_init_history_without_lengths import (
    NO_OPEN_END,
    OPEN_ENDED,
    _init,
    _lifetimes,
    _profile,
    _reasons,
)
from .test_init_history_without_lengths import _namespace as _block

pytestmark = pytest.mark.integration

ORDER = ["after", "before", "same"]
OPEN = _ms(9999, 12, 31)

#: The gym's nullable end: no open end is declared, and none is required.
NULLABLE_END = NO_OPEN_END.replace(
    "{ name: valid_to, type: DATE, required: true }",
    "{ name: valid_to, type: DATE, required: false }",
)


def _pair(
    start: str, end: str, order: List[int], days: Optional[List[int]] = None
) -> Dict[str, Any]:
    """A date pair of ``tier_version``: how its rows are ordered and, if given, the days between."""
    total = sum(order)
    found: Dict[str, Any] = {
        "start": start,
        "end": end,
        "order": _levels(ORDER, order, total),
        "statistics": [],
    }
    if days is not None:
        found["statistics"] = [_bins(["1", "30", "90", "365"], days, sum(days))]
    return found


def _withheld(start: str, end: str) -> Dict[str, Any]:
    """A pair whose order the release cuts, as it does a cell that holds too few records."""
    found = _pair(start, end, [0, 0, 1000])
    found["order"]["groups"] = [{"group": "", "status": "C"}]
    return found


#: The window's order as a check, which does not hold on a NULL end.
CHECKED = NULLABLE_END.replace(
    "        - { name: renewed_on, type: DATE, required: false }\n",
    "        - { name: renewed_on, type: DATE, required: false }\n"
    "      invariants:\n"
    '        - { id: window_ordered, check: "valid_to >= valid_from" }\n',
)
assert CHECKED != NULLABLE_END

#: The days from the day a member enrolled to the end of the version.
ENROLLED_TO_END = _pair("enrolled_on", "valid_to", [1000, 0, 0], [300, 400, 300])
#: The start of the version and the day the member enrolled are the same day.
SAME_DAY = _pair("valid_from", "enrolled_on", [0, 0, 1000])


def _release(*pairs: Dict[str, Any], history: bool = False) -> Dict[str, Any]:
    """The release of the gym's tier table: its histograms, the *pairs* it publishes, no lengths."""
    profile = _profile()
    version = profile["tables"]["tier_version"]
    if not history:
        del version["history"]
    version["datePairs"] = list(pairs)
    return profile


def _ends_by_the_last_start(profile: Dict[str, Any]) -> Dict[str, Any]:
    """*profile* whose end publishes nothing after the last start: the gaps can draw past it."""
    ends = profile["tables"]["tier_version"]["columns"]["valid_to"]
    edges = [_ms(2025, 2, 1), _ms(2025, 5, 1), _ms(2025, 8, 1), _ms(2025, 9, 1)]
    ends["statistics"] = [_bins(edges, [300, 300, 400], 1000)]
    ends["metrics"] = {**ends["metrics"], "max": {**ends["metrics"]["max"], "value": edges[-1]}}
    return profile


def _written(tmp_path: Path, profile: Dict[str, Any], contract: str = NO_OPEN_END) -> Any:
    result = _init(tmp_path, profile, contract)
    life = [item for item in _lifetimes(tmp_path) if item["emit"] == "tier_version"]
    assert len(life) <= 1, life
    return result, (life[0] if life else None)


def _unused(tmp_path: Path, pointer: str) -> List[str]:
    """The reasons the pair at *pointer* and its statistics are unused for, each once."""
    return sorted(
        {
            item["reason"]
            for item in _block(tmp_path)["unused"]
            if item["statistic"]["pointer"] == pointer
            or item["statistic"]["pointer"].startswith(pointer + "/")
        }
    )


def _versions(tmp_path: Path) -> List[List[Any]]:
    """The rows of ``tier_version`` a run of the bundle draws, by column position."""
    return emitted_rows(tmp_path, "tier_version", "tiers.profile.json")


FROM, TO = 3, 4


def _ended_before_started(rows: List[List[Any]]) -> int:
    return sum(row[TO] is not None and row[TO] < row[FROM] for row in rows)


# ---------------------------------------------------------------------------
# A published pair lays out a date of the window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("history", [False, True], ids=["no history entry", "history entry"])
def test_the_window_takes_the_lifetime_from_a_pair_that_lays_its_end_out(
    tmp_path: Path, history: bool
) -> None:
    result, life = _written(tmp_path, _release(ENROLLED_TO_END, history=history))
    assert life is not None
    assert (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert life["duration"]["bins"][0] == "1" and len(life["duration"]["bins"]) > 2
    assert any(
        "derived from the published histograms" in text
        for text in _reasons(tmp_path, "lifetimes/0/duration")
    )
    # The pair that would have laid the end out is unused, with a reason that names what orders
    # the window: the history the release publishes where it does (a lifetime of its own before
    # the window was read), the contract's window where it does not.
    (why,) = _unused(tmp_path, "/tables/tier_version/datePairs/0")
    if history:
        assert "the history the release publishes" in why
    else:
        assert "validity window" in why and "'valid_from' to 'valid_to'" in why
    assert "is traded for a lifetime" in result.stdout
    # No version ends before it starts.
    rows = _versions(tmp_path)
    assert len(rows) == 1000 and _ended_before_started(rows) == 0


def test_a_pair_the_release_publishes_on_the_start_day_does_not_take_the_lifetime_back(
    tmp_path: Path,
) -> None:
    # Both pairs hold on every row; the one that starts at the window's start is listed first.
    _, life = _written(tmp_path, _release(SAME_DAY, ENROLLED_TO_END))
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    # The pair that decided it is used, and the other is unused with a reason that names the window.
    reasons = [_unused(tmp_path, f"/tables/tier_version/datePairs/{index}") for index in (0, 1)]
    assert sorted(len(found) for found in reasons) == [0, 1]
    assert "validity window" in " ".join(reasons[0] + reasons[1])
    assert _ended_before_started(_versions(tmp_path)) == 0


def test_the_pair_that_lays_the_end_out_is_the_same_whichever_the_release_lists_first(
    tmp_path: Path,
) -> None:
    _, life = _written(tmp_path, _release(ENROLLED_TO_END, SAME_DAY))
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert _ended_before_started(_versions(tmp_path)) == 0


def test_a_pair_that_lays_the_start_out_leaves_the_end_in_order_too(tmp_path: Path) -> None:
    # The days from the enrolment to the start of the version, and the end drawn on its own.
    pair = _pair("enrolled_on", "valid_from", [1000, 0, 0], [300, 400, 300])
    _, life = _written(tmp_path, _release(pair))
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert _ended_before_started(_versions(tmp_path)) == 0


def test_the_window_that_has_its_own_published_pair_is_laid_by_that_pair(tmp_path: Path) -> None:
    own = _pair("valid_from", "valid_to", [1000, 0, 0], [100, 500, 400])
    _, life = _written(tmp_path, _release(ENROLLED_TO_END, own))
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert life["duration"]["bins"] == ["1", "30", "90", "365"]
    assert not any("derived" in text for text in _reasons(tmp_path, "lifetimes/0/duration"))
    (why,) = _unused(tmp_path, "/tables/tier_version/datePairs/0")
    assert "validity window" in why
    assert _ended_before_started(_versions(tmp_path)) == 0


def test_a_table_with_no_validity_window_keeps_the_pair_it_published(tmp_path: Path) -> None:
    contract = NO_OPEN_END.replace(
        "typeParams: { validityParams: { isStart: true } }", "typeParams: {}"
    ).replace("typeParams: { validityParams: { isEnd: true } }", "typeParams: {}")
    assert contract != NO_OPEN_END
    _, life = _written(tmp_path, _release(ENROLLED_TO_END), contract)
    assert life is not None and (life["start"], life["end"]) == ("enrolled_on", "valid_to")


def test_a_window_the_published_histograms_already_keep_in_order_is_left_alone(
    tmp_path: Path,
) -> None:
    # Every end the release publishes lies after every start, so drawn apart no version breaks it.
    profile = _release(SAME_DAY, ENROLLED_TO_END)
    ends = profile["tables"]["tier_version"]["columns"]["valid_to"]
    ends["statistics"] = [
        _bins([_ms(2025, 10, 1), _ms(2025, 12, 1), OPEN], [600, 400], 1000),
    ]
    _, life = _written(tmp_path, profile)
    assert life is not None and (life["start"], life.get("end")) == ("valid_from", None)


# ---------------------------------------------------------------------------
# A window that no pair ends, and a pair the release withholds
# ---------------------------------------------------------------------------


def test_a_pair_the_release_withholds_for_the_window_is_laid_out_all_the_same(
    tmp_path: Path,
) -> None:
    result, life = _written(tmp_path, _release(_withheld("valid_from", "valid_to")))
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert _states(result)["/tables/tier_version/datePairs/0/order"]["state"] == "withheld"
    assert _ended_before_started(_versions(tmp_path)) == 0


def test_a_pair_that_lays_the_start_out_leaves_a_window_the_histograms_keep_in_order_alone(
    tmp_path: Path,
) -> None:
    pair = _pair("enrolled_on", "valid_from", [1000, 0, 0], [300, 400, 300])
    profile = _release(pair)
    ends = profile["tables"]["tier_version"]["columns"]["valid_to"]
    ends["statistics"] = [_bins([_ms(2025, 10, 1), _ms(2025, 12, 1), OPEN], [600, 400], 1000)]
    _, life = _written(tmp_path, profile)
    assert life is not None and (life["start"], life["end"]) == ("enrolled_on", "valid_from")
    assert life["duration"]["bins"] == ["1", "30", "90", "365"]


def test_a_withheld_pair_leaves_a_window_the_histograms_keep_in_order_alone(
    tmp_path: Path,
) -> None:
    profile = _release(_withheld("valid_from", "valid_to"))
    ends = profile["tables"]["tier_version"]["columns"]["valid_to"]
    ends["statistics"] = [_bins([_ms(2025, 10, 1), _ms(2025, 12, 1), OPEN], [600, 400], 1000)]
    _, life = _written(tmp_path, profile)
    assert life is None


def test_a_published_window_pair_with_no_days_does_not_take_the_lifetime_from_a_pair_with_days(
    tmp_path: Path,
) -> None:
    no_days = _pair("valid_from", "valid_to", [1000, 0, 0])
    _, life = _written(tmp_path, _release(ENROLLED_TO_END, no_days))
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert "duration" in life
    assert "publishes no days" in " ".join(_unused(tmp_path, "/tables/tier_version/datePairs/1"))
    assert _ended_before_started(_versions(tmp_path)) == 0


def test_a_check_that_fails_on_a_null_end_keeps_the_end_past_the_run(tmp_path: Path) -> None:
    _, life = _written(tmp_path, _release(ENROLLED_TO_END), CHECKED)
    assert life is not None and life.get("endPastWindow") == "kept"
    assert all(row[TO] is not None for row in _versions(tmp_path))


def test_a_check_on_a_date_the_window_lays_out_says_so(tmp_path: Path) -> None:
    contract = CHECKED.replace("valid_to >= valid_from", "valid_to >= enrolled_on")
    result, life = _written(tmp_path, _release(SAME_DAY), contract)
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    notes = {item["subject"]: item["assumption"] for item in result.artifacts["notes"]}
    assert "laid out by the contract's validity window" in notes["tier_version.window_ordered"]


def test_a_release_that_publishes_no_pair_for_the_window_writes_none(tmp_path: Path) -> None:
    # Nothing in the release or the contract orders the two dates: they are drawn as published.
    _, life = _written(tmp_path, _release())
    assert life is None


# ---------------------------------------------------------------------------
# An end that may be open, and a history that is written
# ---------------------------------------------------------------------------


def test_a_nullable_end_that_the_release_publishes_past_the_run_keeps_the_date(
    tmp_path: Path,
) -> None:
    _, life = _written(tmp_path, _release(ENROLLED_TO_END), NULLABLE_END)
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert life.get("endPastWindow") == "kept"
    assert _ended_before_started(_versions(tmp_path)) == 0


def test_a_nullable_end_the_release_publishes_nothing_past_the_run_of_stays_open(
    tmp_path: Path,
) -> None:
    profile = _ends_by_the_last_start(_release(ENROLLED_TO_END))
    _, life = _written(tmp_path, profile, NULLABLE_END)
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert "endPastWindow" not in life
    assert _ended_before_started(_versions(tmp_path)) == 0


def test_a_required_end_keeps_the_date_past_the_run(tmp_path: Path) -> None:
    _, life = _written(tmp_path, _release(ENROLLED_TO_END), NO_OPEN_END)
    assert life is not None and life.get("endPastWindow") == "kept"


def test_a_window_with_a_declared_open_end_is_written_as_a_history_and_no_lifetime(
    tmp_path: Path,
) -> None:
    result = _init(tmp_path, _release(ENROLLED_TO_END, history=True), OPEN_ENDED)
    assert [item for item in _lifetimes(tmp_path) if item["emit"] == "tier_version"] == []
    assert len(_block(tmp_path)["history"]) == 1
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# An end the contract's range stops short of the start's reach
# ---------------------------------------------------------------------------


def test_a_nullable_end_is_laid_out_though_the_gaps_can_draw_past_its_range(
    tmp_path: Path,
) -> None:
    # An end past the run is NULL, so no end the gaps draw can fall outside the declared range.
    profile = _ends_by_the_last_start(_release(ENROLLED_TO_END))
    _, life = _written(tmp_path, profile, NULLABLE_END)
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    assert _ended_before_started(_versions(tmp_path)) == 0


# ---------------------------------------------------------------------------
# The range the contract gives the end
# ---------------------------------------------------------------------------

#: The end the contract holds inside 2025 to the middle of 2026, which the published end passes.
BOUNDED_END = NO_OPEN_END.replace(
    "{ name: valid_to, type: DATE, required: true }",
    "{ name: valid_to, type: DATE, required: true, validationRules: "
    "[{ type: range, constraint: \"between '2025-01-01' and '2026-06-30'\" }] }",
)


def test_the_window_lifetime_declares_the_range_the_contract_gives_its_end(tmp_path: Path) -> None:
    assert BOUNDED_END != NO_OPEN_END
    _, life = _written(tmp_path, _release(ENROLLED_TO_END), BOUNDED_END)
    assert life is not None and (life["start"], life["end"]) == ("valid_from", "valid_to")
    column = _block(tmp_path)["emits"]["tier_version"]["fidelity"]["columns"]["valid_to"]
    assert column["range"] == {"min": "2025-02-01", "max": "2026-06-30"}
    rows = _versions(tmp_path)
    assert _ended_before_started(rows) == 0
    assert max(row[TO] for row in rows if row[TO] is not None) <= "2026-06-30"


# ---------------------------------------------------------------------------
# What the ledger lists as unused changes no bundle
# ---------------------------------------------------------------------------


def _generated(tmp_path: Path) -> Dict[str, Any]:
    """What the bundle in *tmp_path* generates from: its namespace less the register, the profile
    digest and the unused list, which name statistics by position."""
    found = dict(_block(tmp_path))
    for key in ("parameters", "unused", "profile"):
        found.pop(key, None)
    return found


@pytest.mark.parametrize("shape", ["tie", "pair"])
def test_a_pair_a_decision_reads_is_used_and_a_release_without_the_unused_gives_the_bundle(
    tmp_path: Path, shape: str
) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    # A column published on the start's day keeps the history out; a pair of a window date and
    # another column gives the window the table's lifetime. Each pair is read by that decision.
    profile = _release(SAME_DAY, history=True) if shape == "tie" else _release(ENROLLED_TO_END)
    contract = OPEN_ENDED if shape == "tie" else NO_OPEN_END
    whole = tmp_path / "whole"
    whole.mkdir()
    _init(whole, profile, contract)
    assert (_block(whole).get("history", []) == []) and (
        any(item["emit"] == "tier_version" for item in _block(whole)["lifetimes"])
    )
    listed = [item["statistic"]["pointer"] for item in _block(whole)["unused"]]
    pair = "/tables/tier_version/datePairs/0"
    assert pair not in listed and pair + "/order" not in listed
    cut = tmp_path / "cut"
    cut.mkdir()
    _init(cut, _drop(profile, listed), contract)
    assert _generated(cut) == _generated(whole)
