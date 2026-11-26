# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A profile that counts a table's rows per entity of a history keeps the history.

A profiler counts a child's rows per parent over every relationship, and a relationship onto a
versioned table's natural entity is counted per entity: the children of every version of one
member, say. A parent row there is a version, not an entity, so no layout places those counts: a
``counts`` entry over the relationship is refused when the table has a history (``FLX-3403``), and
needs each version's entity to be unique when it has none. ``fluxsim init`` leaves the statistic
unused with the reason, and writes the history and its projections as it would without it.

Pinned here, in a gym (members, their membership versions, monthly visit rows, change events and
locker rentals): the history and both projections are written; the ledger says why each count is
unused, a projection's in its own words; the rental's member is drawn as it is without the count;
the bundle validates, plans, applies and verifies.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.seam.projections import EVENTS_KEY, PERIODS_KEY

from ._written import applied_and_verified, bundle_digests
from .test_history_init import _bins, _init, _namespace, _states
from .test_history_init import _profile as _history_profile
from .test_projection_init import _number, _table, _tables

pytestmark = pytest.mark.integration

GYM = (
    Path(__file__).resolve().parents[1] / "fixtures" / "projection-init" / "gym.fluid.yml"
).read_text(encoding="utf-8")

#: The history fixture's names, each with its name in the gym.
_NAMES = {
    "account_id": "club_id",
    "account": "club",
    "plan_version": "membership",
    "plan_id": "member_id",
    "plan_change": "membership_change",
    "plan_month": "visit_month",
}

_WORD = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _NAMES), key=len, reverse=True)) + r")\b"
)


def _gym(text: str) -> str:
    """*text* with the history fixture's names read as the gym's."""
    return _WORD.sub(lambda found: _NAMES[found.group(1)], text)


#: The tables that read the membership history, each with its rows per member.
_PER_MEMBER = {
    "visit_month": (["0", "1", "3", "5", "9"], [0, 300, 400, 300]),
    "membership_change": (["1", "2", "3", "4", "6"], [300, 300, 200, 200]),
    "locker_rental": (["0", "1", "3", "5", "9"], [100, 300, 400, 200]),
}


def _per_member(child: str) -> Dict[str, Any]:
    edges, counts = _PER_MEMBER[child]
    return {
        "parent": {"table": "membership", "columns": ["member_id"]},
        "child": {"table": child, "columns": ["member_id"]},
        "statistics": [_bins(edges, counts, 1000)],
    }


def _profile(*, history: bool = True, counted: bool = True) -> Dict[str, Any]:
    """The gym's profile: the membership history, its three readers, and each one's rows per member."""
    found: Dict[str, Any] = json.loads(_gym(json.dumps(_history_profile(history=history))))
    tables = json.loads(_gym(json.dumps(_tables())))
    for name in ("membership_change", "visit_month"):
        found["tables"][name] = tables[name]
    found["tables"]["locker_rental"] = _table(
        3000, rental_id=_number(3000), member_id=_number(1000), fee=_number(3000)
    )
    if counted:
        found["relationships"] = [_per_member(name) for name in sorted(_PER_MEMBER)]
    return found


def _pointer(profile: Dict[str, Any], child: str) -> str:
    index = [item["child"]["table"] for item in profile["relationships"]].index(child)
    return f"/relationships/{index}/statistics/0"


def test_rows_per_member_of_the_readers_keep_the_history_and_its_projections(
    tmp_path: Path,
) -> None:
    _init(tmp_path, _profile(), GYM)
    namespace = _namespace(tmp_path)
    assert [entry["emit"] for entry in namespace["history"]] == ["membership"]
    assert [entry["emit"] for entry in namespace[EVENTS_KEY]] == ["membership_change"]
    assert namespace[PERIODS_KEY] == [
        {
            "emit": "visit_month",
            "of": "membership",
            "column": "month",
            "grain": "month",
            "match": "overlap",
            "kinds": [{"count": 1}],
        }
    ]
    assert "counts" not in namespace
    assert "rows" not in namespace["emits"]["visit_month"]
    assert "rows" not in namespace["emits"]["membership_change"]


def _reasons(tmp_path: Path, profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    states = _states(_init(tmp_path, profile, GYM))
    return {child: states[_pointer(profile, child)] for child in _PER_MEMBER}


def test_each_count_is_unused_and_says_why(tmp_path: Path) -> None:
    states = _reasons(tmp_path, _profile())
    assert {state["state"] for state in states.values()} == {"unused"}
    assert states["visit_month"]["note"] == (
        "the emit is projected from membership as its periodic rows, one row per version and "
        "period it holds, so its rows per member_id follow from the versions and their periods; "
        "the profile counts the rows, not the periods each row holds, so no rows per period are "
        "read from it"
    )
    assert states["membership_change"]["note"] == (
        "the emit is projected from membership as its change events, one row per change, so its "
        "rows per member_id are the changes of that entity's versions and follow from the history"
    )
    assert states["locker_rental"]["note"] == (
        "the relationship reaches membership, which has a history, by its natural entity, and a "
        "child picks a version as any child does, so no layout places its children per entity"
    )


def test_the_run_prints_each_projection_reason_once(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile(), GYM)
    lines: List[str] = result.stdout.splitlines()
    assert sum("visit_month: member: the emit is projected" in line for line in lines) == 1
    assert sum("membership_change: member: the emit is projected" in line for line in lines) == 1


def test_the_rental_draws_its_member_as_it_does_without_the_count(tmp_path: Path) -> None:
    counted, plain = tmp_path / "counted", tmp_path / "plain"
    counted.mkdir()
    plain.mkdir()
    _init(counted, _profile(), GYM)
    _init(plain, _profile(counted=False), GYM)
    columns = [
        _namespace(where)["emits"]["locker_rental"]["fidelity"]["columns"]["member_id"]
        for where in (counted, plain)
    ]
    assert columns[0] == columns[1]


def test_the_written_bundle_validates_plans_applies_and_verifies(tmp_path: Path) -> None:
    _init(tmp_path, _profile(), GYM)
    applied_and_verified(tmp_path)


def test_the_history_and_its_projections_are_those_written_without_the_counts(
    tmp_path: Path,
) -> None:
    counted, plain = tmp_path / "counted", tmp_path / "plain"
    counted.mkdir()
    plain.mkdir()
    _init(counted, _profile(), GYM)
    _init(plain, _profile(counted=False), GYM)
    for key in ("history", EVENTS_KEY, PERIODS_KEY):
        assert _namespace(counted)[key] == _namespace(plain)[key]


#: sha256 of each file ``fluxsim init`` wrote for the gym's contract and its profile without the
#: counts per member, before a count over a versioned table's natural entity was left unused.
WITHOUT_COUNTS: Dict[str, str] = {
    "persona.flux.yml": "9743f6a2d96613cd7d8cab4ba9f2e124bf3a7010415d4a921c26e9b089169040",
    "plans.fluid.yml": "0318f77bab99dee58868b746010eff203ccc55d977499debb3c053fdfe4a6e1f",
    "simulation.flux.yml": "69a2274c3f096a7fd8d233fbefab432b6b1fbd3950efcd3d8cfb66ece2e1cbfb",
    "world.flux.yml": "4b358e01c4453cc7b9f1927a493bbbd7e6ebbf89c5c6c4f22e7c6eb74759e5a0",
}


def test_a_profile_without_the_counts_writes_the_bundle_it_always_did(tmp_path: Path) -> None:
    _init(tmp_path, _profile(counted=False), GYM)
    assert bundle_digests(tmp_path) == WITHOUT_COUNTS


def _keys(text: str = GYM) -> Any:
    from flux_engine.seam.keys import compile_keys

    found = compile_keys(yaml.safe_load(text))
    assert found is not None
    return found


def _named(keys: Any, expose: str, name: str) -> Any:
    """The relationship *name* that *expose* declares, as the keys compile it."""
    (found,) = [item for item in keys.expose(expose).relationships if item.name == name]
    return found


def test_only_a_relationship_into_a_table_with_a_history_is_held_back() -> None:
    from flux_engine.author.bundle import _into_a_history

    keys = _keys()
    held = frozenset({"membership"})
    assert "by its natural entity" in str(_into_a_history(keys, held, "locker_rental", "member"))
    assert "children per entity" in str(_into_a_history(keys, held, "locker_rental", "member"))
    # The club has no history, and a table no history is written for is counted as before.
    assert _into_a_history(keys, held, "membership", "club") is None
    assert _into_a_history(keys, frozenset(), "locker_rental", "member") is None
    assert _into_a_history(keys, held, "locker_rental", "no_such") is None


def test_a_key_of_the_versions_is_held_back_and_counted_per_version() -> None:
    from flux_engine.author.bundle import _into_a_history

    text = GYM.replace(
        "          fromColumns: [member_id]\n          toColumns: [member_id]\n",
        "          fromColumns: [member_id]\n          toColumns: [version_id]\n",
        1,
    )
    assert text != GYM
    reason = str(_into_a_history(_keys(text), frozenset({"membership"}), "locker_rental", "member"))
    assert "by a key of its versions" in reason and "children per version" in reason


def test_the_tables_with_a_history_are_those_whose_profile_publishes_one() -> None:
    from flux_engine.author.bundle import _history_tables

    keys = _keys()
    assert _history_tables(None, keys, _profile()["tables"]) == {"membership"}
    assert _history_tables(None, keys, _profile(history=False)["tables"]) == frozenset()
