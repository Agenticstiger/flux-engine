# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An association over a parent whose row count is an assumption takes the pairs from the other side.

ADR 0039, amendment of 2026-11-26. Both sides of an association count the same pairs, so the sum
of each side's children per parent is the pairs. A parent the profile gives no row count for has
only an assumed one, and a total of children resting on it is no total: ``init`` takes the pairs
from the side whose parent rows are published, and gives the assumed parent the rows its own
children per parent make of them. Where both parents are assumed, or the rows derived cannot hold
the pairs, no association is written.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.author import bundle
from flux_engine.author.association import derive_parent_rows
from flux_engine.author.noisy import point_estimates
from flux_engine.seam.keys import ContractKeys, ExposeKeys, Relationship
from tests.bundles import init_from_texts

from .test_association_init import (
    CLUB_BINS,
    CLUB_PARENTS,
    CLUBS,
    CONTRACT,
    MEMBERS,
    NAMES,
    ROWS,
    _histogram,
    _profile,
    _rowless,
    _run,
)

pytestmark = pytest.mark.integration


def _without_rows(profile: Dict[str, Any], table: str, column: str) -> Dict[str, Any]:
    """The profile with no row count of *table*, and no count of its key column either."""
    del profile["tables"][table]["rowCount"]
    profile["tables"][table]["columns"][column] = {"domain": "number"}
    return profile


def _written(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    result = init_from_texts(tmp_path, CONTRACT, profile, NAMES)
    assert result.exit_code == 0, result.stderr
    document = yaml.safe_load((tmp_path / "bundle" / "simulation.flux.yml").read_text("utf-8"))
    (namespace,) = document["extensions"].values()
    assert isinstance(namespace, dict)
    return result, namespace


def test_the_assumed_parent_takes_the_rows_its_children_per_parent_make_of_the_pairs(
    tmp_path: Path,
) -> None:
    # The clubs publish no row count: 5 are assumed. The members' children per member total 1,775
    # pairs, and a club holds 59.6 of them on average, which is 30 clubs.
    profile = _without_rows(_rowless(_profile(means=False)), "club", "club_id")
    result, namespace = _written(tmp_path, profile)
    assert namespace["emits"]["club"]["rows"] == 30
    assert namespace["emits"]["membership"]["rows"] == 1775
    assert [item["emit"] for item in namespace["associations"]] == ["membership"]
    notes = result.stdout
    assert "The profile has no row count for club: 30 rows" in notes
    assert "the 1775 pairs of member and club over the 59.63 children per club" in notes
    assert "the rows of its club parent being assumed" in notes
    register = [
        item for item in namespace["parameters"] if item["pointer"].endswith("/emits/club/rows")
    ]
    assert [item["source"] for item in register] == ["assumption"]
    assert "same pairs" in register[0]["reason"]


def test_the_derived_rows_draw_and_hold_every_pair_once(tmp_path: Path) -> None:
    profile = _without_rows(_rowless(_profile(means=False)), "club", "club_id")
    _written(tmp_path, profile)
    pairs = _run(tmp_path)
    assert len(pairs) == len(set(pairs)) == 1775
    assert max(club for _, club in pairs) <= 30


def test_a_published_mean_with_its_parents_gives_the_same(tmp_path: Path) -> None:
    profile = _without_rows(_rowless(_profile()), "club", "club_id")
    _, namespace = _written(tmp_path, profile)
    assert namespace["emits"]["club"]["rows"] == CLUBS
    assert namespace["emits"]["membership"]["rows"] == ROWS


def test_where_both_parents_are_assumed_no_association_is_written(tmp_path: Path) -> None:
    profile = _without_rows(_rowless(_profile(means=False)), "club", "club_id")
    profile = _without_rows(profile, "member", "member_id")
    result, namespace = _written(tmp_path, profile)
    assert "associations" not in namespace and "membership" not in namespace["emits"]
    assert "No association is written" in result.stdout and "keeps its" not in result.stdout
    assert "rows of both the 'member' and the 'club' parents are assumptions" in result.stdout
    assert namespace["emits"]["club"]["rows"] == namespace["emits"]["member"]["rows"] == 5


def test_a_mean_no_other_parent_can_hold_writes_no_association(tmp_path: Path) -> None:
    # A club of a thousand members on average, with 600 members: the pairs cannot be drawn.
    profile = _without_rows(_rowless(_profile(means=False)), "club", "club_id")
    profile["relationships"][1]["statistics"] = [
        _histogram(["1000", "2000", "3000"], [10, 12], CLUBS)
    ]
    result, namespace = _written(tmp_path, profile)
    assert "associations" not in namespace
    assert "No association is written" in result.stdout
    assert namespace["emits"]["club"]["rows"] == 5


def test_a_side_that_publishes_no_children_per_parent_writes_no_association(
    tmp_path: Path,
) -> None:
    profile = _without_rows(_rowless(_profile(means=False)), "club", "club_id")
    del profile["relationships"][1]
    result, namespace = _written(tmp_path, profile)
    assert "associations" not in namespace


def test_parents_with_published_rows_keep_the_rows_they_had(tmp_path: Path) -> None:
    # Neither parent is an assumption: the rows are the inverse-variance mean, as before.
    _, namespace = _written(tmp_path, _rowless(_profile(means=False)))
    assert namespace["emits"]["club"]["rows"] == CLUBS
    assert namespace["emits"]["member"]["rows"] == MEMBERS
    assert [item["emit"] for item in namespace["associations"]] == ["membership"]


def test_a_parent_another_table_refers_to_is_not_settled_by_the_pairs() -> None:
    def keys(*items: Relationship) -> ContractKeys:
        held: Dict[str, ExposeKeys] = {"club": ExposeKeys("club")}
        for item in items:
            held[item.source] = ExposeKeys(item.source, relationships=(item,))
        return ContractKeys("0.7.7", held)

    def link(source: str, target: str) -> Relationship:
        return Relationship(
            f"{source}_to_{target}", source, target, ("x",), ("x",), "many_to_one", "inner"
        )

    assert bundle._pins_parent(keys(link("membership", "club")), "club", "membership") is None
    reason = bundle._pins_parent(
        keys(link("membership", "club"), link("event", "club")), "club", "membership"
    )
    assert reason is not None and "'event' also refers to 'club'" in reason
    pinned = ContractKeys(
        "0.7.7", {"club": ExposeKeys("club", relationships=(link("club", "region"),))}
    )
    assert "child of a relationship of its own" in str(bundle._pins_parent(pinned, "club", "m"))


def test_the_derivation_is_a_function_of_the_two_sides_alone() -> None:
    profile = copy.deepcopy(_profile(means=False))
    pair = (
        Relationship(
            "m", "membership", "member", ("member_id",), ("member_id",), "many_to_one", "l"
        ),
        Relationship("c", "membership", "club", ("club_id",), ("club_id",), "many_to_one", "l"),
    )
    found = derive_parent_rows(profile, pair, (MEMBERS, 5), (False, True), 16)
    assert not isinstance(found, str) and (found.side, found.parent_rows) == (1, 30)
    assert isinstance(derive_parent_rows(profile, pair, (5, 5), (True, True), 16), str)
    assert CLUB_BINS and CLUB_PARENTS


def _pair() -> Any:
    return (
        Relationship(
            "m", "membership", "member", ("member_id",), ("member_id",), "many_to_one", "l"
        ),
        Relationship("c", "membership", "club", ("club_id",), ("club_id",), "many_to_one", "l"),
    )


def test_a_number_beyond_what_a_bundle_declares_gives_a_reason_not_an_error() -> None:
    profile = _profile(total=10**400)
    found = derive_parent_rows(profile, _pair(), (MEMBERS, 5), (False, True), 16)
    assert isinstance(found, str) and "beyond what a bundle can declare" in found


def test_a_parent_that_holds_more_children_than_the_other_side_has_parents_is_refused() -> None:
    # The members' classes reach 120 children each, with about 57 clubs derived: no member has 120 clubs.
    profile = _profile(means=False)
    profile["relationships"][0]["statistics"] = [
        _histogram(["1", "2", "3", "5", "120"], [300, 200, 60, 40], MEMBERS)
    ]
    found = derive_parent_rows(profile, _pair(), (MEMBERS, 5), (False, True), 16)
    assert isinstance(found, str) and "holds up to 120 children" in found


def test_rows_far_from_the_parents_a_side_counts_are_refused() -> None:
    profile = _profile()
    profile["relationships"][1]["statistics"] = [_mean_with(total=1, parents=CLUBS)]
    found = derive_parent_rows(profile, _pair(), (MEMBERS, 5), (False, True), 16)
    assert isinstance(found, str) and f"far from the {CLUBS} that" in found


@pytest.mark.parametrize(
    ("mean", "rows", "accepted"),
    [(12, 150, True), (3, 600, False), (300, 6, True), (1200, 2, False)],
)
def test_rows_five_times_the_counted_parents_are_kept_and_twenty_times_are_not(
    mean: int, rows: int, accepted: bool
) -> None:
    # 1,800 pairs over 12 children per club make 150 clubs, five times the 30 the club side counts;
    # over 3 they make 600, twenty times. The bound is ten times, either way round.
    profile = _profile()
    profile["relationships"][1]["statistics"] = [
        _mean_with(total=mean * CLUBS * 65536, parents=CLUBS)
    ]
    found = derive_parent_rows(profile, _pair(), (MEMBERS, 5), (False, True), 16)
    if accepted:
        assert not isinstance(found, str) and found.parent_rows == rows
    else:
        assert isinstance(found, str) and f"the {rows} 'club' the pairs give are far from" in found


def _mean_with(total: int, parents: int) -> Dict[str, Any]:
    """A ``mean/1`` of *parents* parents whose children add up to *total* units of the encoding."""
    found = _profile()["relationships"][1]["statistics"][-1]
    found["groups"][0].update({"k": parents, "Sx": str(total)})
    return found


def _noisy_clubs(counts: List[str]) -> Dict[str, Any]:
    """The profile whose clubs' children per club are a noisy histogram with an open top class."""
    profile = _profile(means=False)
    statistic = _histogram(["30", "50", "70", "100", "1000000"], [0, 0, 0, 0], CLUBS)
    statistic["disclosure"] = "noisy"
    statistic["noise"] = {
        "mechanism": "discreteGaussian",
        "scale": "2",
        "privacyLoss": {"rho": "1"},
    }
    statistic["groups"] = [{"group": "", "status": "F", "noisyCounts": counts}]
    profile["relationships"][1]["statistics"] = [statistic]
    return profile


def test_the_mean_of_a_noisy_side_is_read_over_the_cells_that_stand_clear_of_the_noise() -> None:
    # The last cell, a class from a hundred to a million children, is inside the noise (6 against
    # a floor of 7.5): read at its middle it would put half a million children on a club.
    profile = _noisy_clubs(["10", "12", "8", "6"])
    view, notes = point_estimates(profile)
    found = derive_parent_rows(view, _pair(), (MEMBERS, 5), (False, True), 16, notes)
    assert not isinstance(found, str) and found.parent_rows == 30
    unread = derive_parent_rows(view, _pair(), (MEMBERS, 5), (False, True), 16)
    assert isinstance(unread, str)


def test_a_noisy_side_with_no_cell_clear_of_the_noise_gives_no_mean() -> None:
    profile = _noisy_clubs(["3", "4", "2", "1"])
    view, notes = point_estimates(profile)
    found = derive_parent_rows(view, _pair(), (MEMBERS, 5), (False, True), 16, notes)
    assert isinstance(found, str) and "stands clear of the noise" in found
