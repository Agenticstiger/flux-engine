# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected table that is also a counted child takes the history's layout (ADR 0027 section 9).

A snapshot, events or periodic emit takes its rows from the history, and a ``counts`` entry over one
of its relationships is refused. ``fluxsim init`` therefore writes the projection, drops the emit's
own ``counts`` entry and gives that statistic to the history's ``parent`` when the history has none
of its own. Pinned here: the entries written, the ledger's account of where each statistic went,
the cases where it is kept and where it is unused, and the bundle of a contract without the shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from flux_engine.seam.projections import SNAPSHOTS_KEY
from flux_engine.stages.validate import validate

from ._written import applied_and_verified, bundle_digests, generated, plan_written
from .test_history_init import _bins, _entries, _init, _metrics, _namespace, _states
from .test_history_parent import _relationship
from .test_projection_init import CONTRACT, _profile_with

pytestmark = pytest.mark.integration

#: ``plan_current`` is a snapshot that also names the account it belongs to.
COUNTED = CONTRACT.read_text(encoding="utf-8").replace(
    """        - { name: plan_id, type: INT, required: true }
        - { name: tier, type: STRING, required: true }
        - { name: channel, type: STRING, required: true }
    semantics:
      entities:
        - { name: plan, type: primary, expr: plan_id }
      dimensions:
        - { name: tier, type: categorical }
        - { name: channel, type: categorical }
""",
    """        - { name: plan_id, type: INT, required: true }
        - { name: account_id, type: INT, required: true }
        - { name: tier, type: STRING, required: true }
        - { name: channel, type: STRING, required: true }
    semantics:
      entities:
        - { name: plan, type: primary, expr: plan_id }
      dimensions:
        - { name: tier, type: categorical }
        - { name: channel, type: categorical }
      relationships:
        - name: account
          to: account
          fromColumns: [account_id]
          toColumns: [account_id]
          cardinality: many_to_one
""",
    1,
)

RELATIONSHIPS = "/relationships"


def _profile(history_children: bool = False, *, snapshot_children: bool = True) -> Dict[str, Any]:
    profile = _profile_with("plan_current")
    profile["tables"]["account"]["rowCount"] = 400
    profile["tables"]["account"]["columns"]["account_id"]["metrics"] = _metrics("1", "400", 400)
    current = profile["tables"]["plan_current"]
    current["columns"]["account_id"] = {
        "domain": "number",
        "metrics": _metrics("1", "400", 1000),
    }
    profile["relationships"] = []
    if history_children:
        profile["relationships"].append(_relationship())
    if snapshot_children:
        # One row per plan, so these are plans per account: 100 accounts hold one, 200 two, 100
        # three to five.
        profile["relationships"].append(
            {
                "parent": {"table": "account", "columns": ["account_id"]},
                "child": {"table": "plan_current", "columns": ["account_id"]},
                "statistics": [_bins(["0", "1", "2", "3", "6"], [0, 100, 200, 100], 400)],
                "structural": ["childless"],
            }
        )
    return profile


def _written(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    return _init(tmp_path, profile, COUNTED)


def test_the_snapshot_stays_a_projection_and_its_counts_entry_goes_to_the_history_parent(
    tmp_path: Path,
) -> None:
    _written(tmp_path, _profile())
    namespace = _namespace(tmp_path)
    assert namespace[SNAPSHOTS_KEY] == [
        {"emit": "plan_current", "of": "plan_version", "ended": "keep"}
    ]
    assert "counts" not in namespace
    assert "rows" not in namespace["emits"]["plan_current"]
    parent = _entries(tmp_path)[0]["parent"]
    assert parent["relationship"] == "account"
    assert parent["minChildren"] == 1
    assert parent["children"]["binWeights"] == ["0", "0.25", "0.5", "0.25"]


def test_the_ledger_says_which_statistic_moved_where(tmp_path: Path) -> None:
    result = _written(tmp_path, _profile())
    states = _states(result)
    state = states["/relationships/0/statistics/0"]
    assert state["state"] == "used"
    assert "projected from a history" in state["note"]
    assert "plan_version's parent account takes it as its children" in state["note"]
    assert "minChildren 1 on the history's parent" in states["/relationships/0/structural"]["note"]
    assert "plan_current: counts" in result.stdout


def test_the_history_keeps_its_own_children_and_the_statistic_is_read_for_the_key(
    tmp_path: Path,
) -> None:
    result = _written(tmp_path, _profile(history_children=True))
    parent = _entries(tmp_path)[0]["parent"]
    assert parent["children"]["bins"] == ["0", "1", "2", "3", "9"]
    assert _namespace(tmp_path)[SNAPSHOTS_KEY]
    assert "counts" not in _namespace(tmp_path)
    state = _states(result)["/relationships/1/statistics/0"]
    assert state["state"] == "used"
    assert "own children per parent are written and are kept" in state["note"]
    assert "follow the history" in state["note"]


#: ``COUNTED`` with the history's relationship to the account removed: the history writes no
#: ``parent``, though it holds the ``account_id`` column the counted table would copy.
NO_HISTORY_PARENT = COUNTED.replace(
    """      relationships:
        - name: account
          to: account
          fromColumns: [account_id]
          toColumns: [account_id]
          cardinality: many_to_one
  - exposeId: plan_invoice""",
    "  - exposeId: plan_invoice",
    1,
)


def test_with_no_history_parent_the_table_is_not_projected_and_says_why(tmp_path: Path) -> None:
    assert NO_HISTORY_PARENT != COUNTED
    result = _init(tmp_path, _profile(), NO_HISTORY_PARENT)
    namespace = _namespace(tmp_path)
    assert "parent" not in _entries(tmp_path)[0]
    # The table would copy the history's drawn account_id values, which are no account keys, so it
    # keeps its own rows and its own counts entry.
    assert SNAPSHOTS_KEY not in namespace
    assert [item["emit"] for item in namespace["counts"]] == ["plan_current"]
    assert "rows" in namespace["emits"]["plan_current"]
    assert (
        "plan_current: snapshots: unused, the expose meets only part of the rule: its column "
        "account_id is set by a counts entry, which a projected emit refuses, and plan_version "
        "writes no parent over account, so the values it holds in account_id are no keys of "
        "account"
    ) in result.stdout


def test_a_history_parent_over_another_expose_is_no_parent_over_the_counted_one(
    tmp_path: Path,
) -> None:
    # The history's parent is laid out under another expose than the counted table's account.
    other = COUNTED.replace(
        "        - name: account\n          to: account\n          fromColumns: [account_id]\n"
        "          toColumns: [account_id]\n          cardinality: many_to_one\n  - exposeId: plan_invoice",
        "        - name: account\n          to: plan_current\n          fromColumns: [account_id]\n"
        "          toColumns: [plan_id]\n          cardinality: many_to_one\n  - exposeId: plan_invoice",
        1,
    )
    assert other != COUNTED
    result = _init(tmp_path, _profile(), other)
    assert SNAPSHOTS_KEY not in _namespace(tmp_path)
    assert "plan_version writes its parent over plan_current, not over account" in result.stdout


@pytest.mark.parametrize("contract", [COUNTED, NO_HISTORY_PARENT], ids=["parent", "no-parent"])
def test_the_written_bundle_validates_plans_applies_and_verifies(
    tmp_path: Path, contract: str
) -> None:
    _init(tmp_path, _profile(), contract)
    applied_and_verified(tmp_path)


def test_the_written_bundle_validates_and_plans(tmp_path: Path) -> None:
    _written(tmp_path, _profile())
    assert validate(tmp_path / "bundle").exit_code == 0
    plan_written(tmp_path)


def test_a_snapshot_that_is_no_counted_child_writes_the_bundle_it_always_did(
    tmp_path: Path,
) -> None:
    profile = _profile_with("plan_current")
    _init(tmp_path, profile, CONTRACT.read_text(encoding="utf-8"))
    digests = bundle_digests(tmp_path)
    assert digests == WITHOUT_COUNTS


#: sha256 of each file `fluxsim init` wrote for this contract and profile before a counted
#: projection was written. The bundle includes the history `parent` that the commit "feat(init):
#: write a history's parent from its many-to-one relationship" writes, so these digests cannot be
#: recreated on main, which has no such `parent`. Its header names no profile file, and its unused
#: list names a history's tracked column whole.
WITHOUT_COUNTS: Dict[str, str] = {
    "persona.flux.yml": "74f6062c0e15e70d2689583ad345103c7189b244886c3280c244573b49677755",
    "plans.fluid.yml": "f50c1adff7eeae2f5dca996a48fa329ca9704765f408013097d796d5cee73e53",
    "simulation.flux.yml": "263b4653bd06d3622dcc42bf4735ead9ddf0a0dd2d64a85491722f423a82826c",
    "world.flux.yml": "20a3e0be50c0216bb8ef8ac07cdc72b0100a3cf4bddccbb91b7841fcc96b88cc",
}


def _counted_child(expose: str, anchor: str, text: Optional[str] = None) -> str:
    """The contract with *expose* naming the account each row belongs to, after the line *anchor*."""
    text = CONTRACT.read_text(encoding="utf-8") if text is None else text
    start = text.index(f"  - exposeId: {expose}\n")
    end = text.find("\n  - exposeId:", start + 1)
    block = text[start : end if end != -1 else len(text)]
    changed = block.replace(
        anchor, anchor + "\n        - { name: account_id, type: INT, required: true }", 1
    ).replace(
        "      relationships:\n",
        "      relationships:\n        - name: account\n          to: account\n"
        "          fromColumns: [account_id]\n          toColumns: [account_id]\n"
        "          cardinality: many_to_one\n",
        1,
    )
    assert changed != block
    return text.replace(block, changed, 1)


def _counted_profile(expose: str) -> Dict[str, Any]:
    profile = _profile_with(expose)
    profile["tables"]["account"]["rowCount"] = 400
    profile["tables"]["account"]["columns"]["account_id"]["metrics"] = _metrics("1", "400", 400)
    table = profile["tables"][expose]
    table["columns"]["account_id"] = {
        "domain": "number",
        "metrics": _metrics("1", "400", table["rowCount"]),
    }
    profile["relationships"] = [
        {
            "parent": {"table": "account", "columns": ["account_id"]},
            "child": {"table": expose, "columns": ["account_id"]},
            "statistics": [_bins(["0", "1", "2", "3", "6"], [0, 100, 200, 100], 400)],
            "structural": ["childless"],
        }
    ]
    return profile


@pytest.mark.parametrize(
    ("expose", "anchor", "key"),
    [
        ("plan_change", "        - { name: event_id, type: INT, required: true }", "events"),
        ("plan_month", "        - { name: period_id, type: INT, required: true }", "periods"),
    ],
    ids=["events", "periods"],
)
def test_an_events_or_periodic_table_gives_no_count_to_the_history_parent(
    tmp_path: Path, expose: str, anchor: str, key: str
) -> None:
    # Its count is events or periods per account, not plans per account: the unit differs.
    result = _init(tmp_path, _counted_profile(expose), _counted_child(expose, anchor))
    namespace = _namespace(tmp_path)
    assert [item["emit"] for item in namespace[key]] == [expose]
    assert "counts" not in namespace
    parent = _entries(tmp_path)[0].get("parent")
    assert parent is None or "children" not in parent
    state = _states(result)["/relationships/0/statistics/0"]
    assert state["state"] == "used"
    assert "the emit is projected from a history" in state["note"]
    assert "count events or periods, not entities" in state["note"]


def _swapped(text: str, first: str, second: str) -> str:
    """*text* with the expose *second* written before the expose *first*."""

    def block(name: str) -> str:
        start = text.index(f"  - exposeId: {name}\n")
        end = text.find("\n  - exposeId:", start + 1)
        return text[start : end + 1 if end != -1 else len(text)]

    one, two = block(first), block(second)
    return text.replace(one, "\0", 1).replace(two, one, 1).replace("\0", two, 1)


def test_the_snapshot_takes_the_count_whichever_projection_comes_first(tmp_path: Path) -> None:
    # A snapshot and an events table are both counted children of the account: only the snapshot's
    # statistic is entities per parent, whichever of them the profile lists first.
    event_id = "        - { name: event_id, type: INT, required: true }"
    contract = _counted_child("plan_change", event_id, COUNTED)
    for order in ("snapshot-first", "events-first"):
        profile = _counted_profile("plan_current")
        events = _counted_profile("plan_change")
        profile["tables"]["plan_change"] = events["tables"]["plan_change"]
        profile["relationships"].append(events["relationships"][0])
        if order == "events-first":
            profile["relationships"].reverse()
            contract = _swapped(contract, "plan_current", "plan_change")
        sub = tmp_path / order
        sub.mkdir()
        result = _init(sub, profile, contract)
        parent = _entries(sub)[0]["parent"]
        assert parent["children"]["binWeights"] == ["0", "0.25", "0.5", "0.25"]
        states = _states(result)
        snapshot = next(
            r for r in profile["relationships"] if r["child"]["table"] == "plan_current"
        )
        index = profile["relationships"].index(snapshot)
        for position in (0, 1):
            assert states[f"/relationships/{position}/statistics/0"]["state"] == "used"
        # Only the snapshot's count is given to the history's parent.
        assert "takes it as its children" in states[f"/relationships/{index}/statistics/0"]["note"]
        other = 1 - index
        assert (
            "takes it as its children" not in states[f"/relationships/{other}/statistics/0"]["note"]
        )


_EVENT_ID = "        - { name: event_id, type: INT, required: true }"
_PERIOD_ID = "        - { name: period_id, type: INT, required: true }"


@pytest.mark.parametrize(
    ("contract", "profile"),
    [
        (COUNTED, _profile(history_children=True)),
        (_counted_child("plan_change", _EVENT_ID), _counted_profile("plan_change")),
        (_counted_child("plan_month", _PERIOD_ID), _counted_profile("plan_month")),
    ],
    ids=["snapshot-with-its-own-history-count", "events", "periods"],
)
def test_a_count_a_projected_emit_refuses_and_no_history_parent_takes_is_read_by_its_key(
    tmp_path: Path, contract: str, profile: Dict[str, Any]
) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    # The profile counts the emit's rows per account. The count is refused and no layout places
    # it, but its presence is what has the emit's key follow the history instead of being drawn
    # over the span of the key it references: dropping it alone gives another bundle.
    whole = tmp_path / "whole"
    whole.mkdir()
    result = _init(whole, profile, contract)
    pointer = next(
        f"/relationships/{index}/statistics/0"
        for index, item in enumerate(profile["relationships"])
        if item["child"]["table"] != "plan_version"
    )
    assert pointer not in [item["statistic"]["pointer"] for item in _namespace(whole)["unused"]]
    assert _states(result)[pointer]["state"] == "used"
    alone = tmp_path / "alone"
    alone.mkdir()
    # The reader takes a profile without the statistic, and the bundle it gives is another one.
    _init(alone, _drop(profile, [pointer]), contract)
    assert generated(_namespace(alone)) != generated(_namespace(whole))


@pytest.mark.parametrize(
    ("contract", "profile"),
    [
        (COUNTED, _profile(history_children=True)),
        (_counted_child("plan_change", _EVENT_ID), _counted_profile("plan_change")),
        (_counted_child("plan_month", _PERIOD_ID), _counted_profile("plan_month")),
    ],
    ids=["snapshot-with-its-own-history-count", "events", "periods"],
)
def test_a_release_cut_by_the_unused_list_gives_the_bundle_of_the_whole_profile(
    tmp_path: Path, contract: str, profile: Dict[str, Any]
) -> None:
    from tests.cli.test_init_ledger_entries import _drop

    whole = tmp_path / "whole"
    whole.mkdir()
    _init(whole, profile, contract)
    listed = [item["statistic"]["pointer"] for item in _namespace(whole)["unused"]]
    cut = tmp_path / "cut"
    cut.mkdir()
    _init(cut, _drop(profile, listed), contract)
    assert generated(_namespace(cut)) == generated(_namespace(whole))


@pytest.mark.parametrize(
    ("contract", "profile", "table"),
    [
        (COUNTED, _profile(history_children=True), "plan_current"),
        (_counted_child("plan_change", _EVENT_ID), _counted_profile("plan_change"), "plan_change"),
        (_counted_child("plan_month", _PERIOD_ID), _counted_profile("plan_month"), "plan_month"),
    ],
    ids=["snapshot-with-its-own-history-count", "events", "periods"],
)
def test_a_mean_beside_the_refused_histogram_stays_unused_and_the_cut_is_the_same(
    tmp_path: Path, contract: str, profile: Dict[str, Any], table: str
) -> None:
    import copy

    from tests.author.test_children import _mean
    from tests.cli.test_init_ledger_entries import _drop

    # Only the histogram has the emit's key follow the history. A mean beside it decides nothing:
    # it is listed as unused, and the bundle without it is the one of the whole profile.
    profile = copy.deepcopy(profile)
    index, relationship = next(
        (position, item)
        for position, item in enumerate(profile["relationships"])
        if item["child"]["table"] == table
    )
    relationship["statistics"].append(_mean(profile["tables"][table]["rowCount"], 400))
    histogram = f"/relationships/{index}/statistics/0"
    mean = f"/relationships/{index}/statistics/1"
    whole = tmp_path / "whole"
    whole.mkdir()
    result = _init(whole, profile, contract)
    listed = [item["statistic"]["pointer"] for item in _namespace(whole)["unused"]]
    assert _states(result)[histogram]["state"] == "used"
    assert histogram not in listed
    assert _states(result)[mean]["state"] == "unused"
    assert mean in listed
    for name, dropped in (("alone", [mean]), ("cut", listed)):
        other = tmp_path / name
        other.mkdir()
        _init(other, _drop(profile, dropped), contract)
        assert generated(_namespace(other)) == generated(_namespace(whole))
