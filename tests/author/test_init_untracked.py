# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes the share of changes that move no tracked column (ADR 0017, amendment of 2026-11-27).

With one tracked column and no ``reasons``, every ordinary change moves it. A profile whose one
tracked column moves in far fewer changes than the history has therefore gave a history whose every
later version changed the column. Pinned here:

* the share is the changes the transitions do not count over the changes there are, exactly;
* the transitions of the one tracked column count every change that moves it, those that move
  several columns too, so ``reasons`` is never read to give the share;
* it is not written where the shortfall is within the profile's rounding, or where a group of the
  transitions or a cell it needs is withheld;
* under an end marker the cells into it leave the moves, and one forced ending version for each
  entity that ends leaves the changes, so the generated rows move the column as often as counted;
* the ledger says where the share came from, and the bundle generates the share it counts.
"""

from __future__ import annotations

import copy
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.author.columns import share_texts

from ._written import bundle_digests, emitted_rows, generated, plan_written, resolved_emits
from .test_history_init import (
    HISTORY,
    NAMESPACE,
    TIER,
    _entries,
    _init,
    _namespace,
    _profile,
    _states,
    _transitions,
)

pytestmark = pytest.mark.integration

#: 850 changes among 1850 versions of 1000 entities; the tier moves in 400 of them.
FEW = {"basic": [0, 150, 50], "plus": [60, 0, 90], "pro": [20, 30, 0]}
#: The tier moves in 840 of them: within the rounding of 850 (a rounding base of 5, three groups).
MOST = {"basic": [0, 300, 100], "plus": [100, 0, 190], "pro": [40, 110, 0]}


def _moving(rows: Dict[str, List[int]], *, reasons: bool = False) -> Dict[str, Any]:
    found = copy.deepcopy(_profile())
    history = found["tables"]["plan_version"]["history"]
    history["columns"]["tier"]["transitions"] = _transitions("tier", TIER, rows)
    if not reasons:
        del history["reasons"]
    return found


def _tier_moves(tmp_path: Path) -> Fraction:
    """The share of the generated changes that move the tier."""
    rows = emitted_rows(tmp_path, "plan_version", "plans.profile.json")
    chains: Dict[Any, List[List[Any]]] = {}
    for row in rows:
        chains.setdefault(row[1], []).append(row)
    moved = changes = 0
    for versions in chains.values():
        ordered = sorted(versions, key=lambda row: row[3])
        for before, after in zip(ordered, ordered[1:], strict=False):
            changes += 1
            moved += before[5] != after[5]
    assert changes == 850
    return Fraction(moved, changes)


def test_the_share_is_what_the_transitions_leave_of_the_changes(tmp_path: Path) -> None:
    _init(tmp_path, _moving(FEW))
    (entry,) = _entries(tmp_path)
    assert entry["untracked"] == share_texts([Fraction(450, 850), Fraction(400, 850)])[0]
    assert "reasons" not in entry


def test_the_bundle_generates_the_changes_the_profile_counts(tmp_path: Path) -> None:
    _init(tmp_path, _moving(FEW))
    assert _tier_moves(tmp_path) == Fraction(400, 850)
    plan_written(tmp_path)


def test_the_ledger_says_what_the_share_rests_on(tmp_path: Path) -> None:
    result = _init(tmp_path, _moving(FEW))
    states = _states(result)
    assert states[f"{HISTORY}/columns/tier/transitions"]["state"] == "used"
    register = [
        item
        for item in _namespace(tmp_path)["parameters"]
        if item["pointer"].endswith("/untracked")
    ]
    assert len(register) == 1 and register[0]["source"] == "profile"
    assert register[0]["statistic"]["pointer"] == f"{HISTORY}/columns/tier/transitions"
    (note,) = [line for line in result.stdout.splitlines() if "history untracked" in line]
    assert "400 changes that move tier among 850" in note
    assert "move no tracked column" in note


def test_a_reasons_histogram_is_not_read_beside_the_one_tracked_column(tmp_path: Path) -> None:
    result = _init(tmp_path, _moving(FEW, reasons=True))
    state = _states(result)[f"{HISTORY}/reasons"]
    assert state["state"] == "unused"
    assert "its moves give the share of changes it makes" in state["note"]


def test_a_reasons_histogram_beside_no_key_keeps_the_reason_it_had(tmp_path: Path) -> None:
    result = _init(tmp_path, _moving(MOST, reasons=True))
    state = _states(result)[f"{HISTORY}/reasons"]
    assert state["state"] == "unused"
    assert "one tracked column makes every change" in state["note"]


def test_a_change_count_within_the_rounding_writes_no_key_and_keeps_the_bytes(
    tmp_path: Path,
) -> None:
    profile = _moving(MOST)
    _init(tmp_path, profile)
    (entry,) = _entries(tmp_path)
    assert "untracked" not in entry
    # No key: the bundle is the one init wrote before the key existed, whatever its other keys.
    again = tmp_path / "again"
    again.mkdir()
    _init(again, copy.deepcopy(profile))
    assert bundle_digests(tmp_path) == bundle_digests(again)


def test_a_withheld_group_writes_no_key(tmp_path: Path) -> None:
    profile = _moving(FEW)
    groups = profile["tables"]["plan_version"]["history"]["columns"]["tier"]["transitions"][
        "groups"
    ]
    groups[1] = {"group": "plus", "status": "C"}
    _init(tmp_path, profile)
    (entry,) = _entries(tmp_path)
    assert "untracked" not in entry


def test_a_missing_group_writes_no_key(tmp_path: Path) -> None:
    profile = _moving(FEW)
    groups = profile["tables"]["plan_version"]["history"]["columns"]["tier"]["transitions"][
        "groups"
    ]
    del groups[2]
    _init(tmp_path, profile)
    assert all("untracked" not in entry for entry in _entries(tmp_path))


#: The tier moves in 750 changes, those that move several columns among them.
SEVENTY_FIVE = {"basic": [0, 300, 100], "plus": [100, 0, 150], "pro": [40, 60, 0]}


def test_a_second_candidate_that_moves_alone_leaves_changes_that_move_no_tracked_column(
    tmp_path: Path,
) -> None:
    # reasons: tier 700, channel 100, several 50, with the tier tracked alone. Its transitions
    # count 750, among them the 50 that move several columns, so the other 100 move none: the
    # changes that move the carried channel alone are untracked changes, and so are not counted
    # among the tier's.
    profile = _moving(SEVENTY_FIVE, reasons=True)
    result = _init(tmp_path, profile)
    (entry,) = _entries(tmp_path)
    assert entry["untracked"] == share_texts([Fraction(100, 850), Fraction(750, 850)])[0]
    (note,) = [line for line in result.stdout.splitlines() if "history untracked" in line]
    assert "750 changes that move tier among 850" in note


def test_the_count_of_changes_that_move_several_columns_does_not_change_the_share(
    tmp_path: Path,
) -> None:
    # 800 moves of the tier among 850 changes, whatever reasons counts as several.
    shares = []
    for several in (0, 50):
        profile = _moving({"basic": [0, 300, 100], "plus": [100, 0, 200], "pro": [40, 60, 0]})
        history = profile["tables"]["plan_version"]["history"]
        history["reasons"] = _levels_of(several)
        where = tmp_path / str(several)
        where.mkdir()
        _init(where, profile)
        shares.append(_entries(where)[0]["untracked"])
    assert shares == [share_texts([Fraction(50, 850), Fraction(800, 850)])[0]] * 2


def _levels_of(several: int) -> Dict[str, Any]:
    from .test_history_init import _levels

    return _levels(["channel", "several", "tier"], [100, several, 750 - several], 850)


def test_a_noisy_group_size_is_read_as_published(tmp_path: Path) -> None:
    profile = _moving(FEW)
    node = profile["tables"]["plan_version"]["history"]["columns"]["tier"]["transitions"]
    node["groups"][0]["k"] = 207  # its cells add up to 200; the size is what is read
    _init(tmp_path, profile)
    (entry,) = _entries(tmp_path)
    assert entry["untracked"] == share_texts([Fraction(443, 850), Fraction(407, 850)])[0]


def test_two_tracked_columns_write_no_key(tmp_path: Path) -> None:
    from .test_history_init import _two_columns

    profile = _two_columns(_profile())
    _init(tmp_path, profile)
    (entry,) = _entries(tmp_path)
    assert len(entry["tracked"]) == 2 and "untracked" not in entry


def test_the_bundle_validates_and_binds_the_key_into_the_emit(tmp_path: Path) -> None:
    _init(tmp_path, _moving(FEW))
    emits = resolved_emits(tmp_path)
    assert emits["plan_version"].extension is not None
    assert emits["plan_version"].extension["history"]["untracked"]
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    assert "untracked" in yaml.safe_load(text)["extensions"][NAMESPACE]["history"][0]


@pytest.mark.parametrize("reasons", [False, True])
def test_a_release_cut_by_the_unused_list_gives_the_bundle_of_the_whole_profile(
    tmp_path: Path, reasons: bool
) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    profile = _moving(FEW, reasons=reasons)
    whole = tmp_path / "whole"
    whole.mkdir()
    _init(whole, profile)
    assert "untracked" in _entries(whole)[0]
    listed = [item["statistic"]["pointer"] for item in _namespace(whole)["unused"]]
    cut = tmp_path / "cut"
    cut.mkdir()
    _init(cut, _drop(profile, listed), None)
    assert generated(_namespace(cut)) == generated(_namespace(whole))


def test_dropping_the_reasons_histogram_gives_the_same_generated_bundle(tmp_path: Path) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    profile = _moving(FEW, reasons=True)
    whole = tmp_path / "whole"
    whole.mkdir()
    _init(whole, profile)
    alone = tmp_path / "alone"
    alone.mkdir()
    _init(alone, _drop(profile, [f"{HISTORY}/reasons"]), None)
    assert generated(_namespace(alone)) == generated(_namespace(whole))


def _marker_profile(tmp_path: Path) -> Dict[str, Any]:
    from .test_history_date_pairs import _init as init_with
    from .test_init_declared_lifecycle import _lifecycle
    from .test_init_end_marker_share import MARKER, _forge_shaped

    profile = _forge_shaped()
    history = profile["tables"]["tier_version"]["history"]
    del history["columns"]["tier"], history["reasons"]
    init_with(tmp_path, profile, _lifecycle(**MARKER))
    return profile


def test_an_end_marker_takes_its_changes_and_its_forced_endings_from_the_counts(
    tmp_path: Path,
) -> None:
    from .test_init_end_marker_share import _entry

    _marker_profile(tmp_path)
    entry = _entry(tmp_path)
    assert [item["column"] for item in entry["tracked"]] == ["status"]
    # 1850 rows of 1000 entities and 200 that end (20 begin on the marker, 180 move into it): the
    # generator takes one forced ending version out of the changes for each, so 650 are ordinary.
    # The status moves 530 times, 180 of them into the marker, so 350 of the 650.
    assert entry["untracked"] == share_texts([Fraction(300, 650), Fraction(350, 650)])[0]


def test_the_generated_rows_move_the_status_in_the_changes_the_profile_counts(
    tmp_path: Path,
) -> None:
    _marker_profile(tmp_path)
    rows = emitted_rows(tmp_path, "tier_version", "tiers.profile.json")
    names = [
        column["name"]
        for expose in yaml.safe_load((tmp_path / "tiers.fluid.yml").read_text())["exposes"]
        if expose["exposeId"] == "tier_version"
        for column in expose["contract"]["schema"]
    ]
    status = names.index("status")
    start = next(i for i, name in enumerate(names) if "from" in name or "start" in name)
    chains: Dict[Any, List[List[Any]]] = {}
    for row in rows:
        chains.setdefault(row[1], []).append(row)
    ordinary = moved = 0
    for versions in chains.values():
        ordered = sorted(versions, key=lambda row: row[start])
        for before, after in zip(ordered, ordered[1:], strict=False):
            if after[status] == "ENDED":
                continue
            ordinary += 1
            moved += before[status] != after[status]
    assert (ordinary, moved) == (650, 350)


@pytest.mark.parametrize(
    ("pro", "written"),
    [([40, 92, 0], False), ([40, 91, 0], True)],
    ids=["shortfall-equal-to-the-allowance", "shortfall-one-above"],
)
def test_the_rounding_allowance_is_exceeded_by_one_not_met(
    tmp_path: Path, pro: List[int], written: bool
) -> None:
    # Three groups at a rounding base of 5 allow a shortfall of 18 of the 850 changes.
    _init(tmp_path, _moving({"basic": [0, 300, 100], "plus": [100, 0, 200], "pro": pro}))
    (entry,) = _entries(tmp_path)
    if written:
        assert entry["untracked"] == share_texts([Fraction(19, 850), Fraction(831, 850)])[0]
    else:
        assert "untracked" not in entry
