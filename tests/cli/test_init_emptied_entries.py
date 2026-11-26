# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An entry of a profile that holds only its identity keys is read as holding nothing.

A release step drops, one by one, each statistic the inventory of ``init --json`` marks unused.
That can leave an entry of a format 1-3-0 profile with nothing but the keys that name it: a date
pair, a relationship's date pair, an exposure, a table's history, or a tracked column of one. The
strict reader refuses such an entry, and the engine reads it, in its place, as holding nothing
(the profile reader's ``allow_emptied_entries``).

Pinned here, from small hand-written profiles of a lending library and its editions:

* every statistic of one entry dropped, a profile ``init`` reads again and ``plan`` accepts, on a
  plain profile and on a released one;
* a citation at or under such an entry is refused (``FLX-5036``), naming the entry.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest

from flux_engine.seam.parameters import check_profile_citations
from flux_stats.errors import FluxError
from flux_stats.profile import SCHEMA_URL_1_4_0, load_profile_text
from tests.author import test_history_init as history_init
from tests.author.test_noisy_profile import _noisy as _noisy_levels
from tests.cli.test_init_ledger_entries import (
    _RELEASED_CONTRACT,
    CONTRACT,
    EDITION_CONTRACT,
    Authored,
    _cites,
    _drop,
    _edition_profile,
    _plan,
    _profile,
    _released_library,
)

pytestmark = pytest.mark.integration

#: The keys that name an entry. Everything else an entry holds is a statistic.
_IDENTITY = frozenset({"column", "start", "end", "parent", "child", "window", "entity"})


def _emptied(profile: Dict[str, Any], entry: Tuple[Any, ...]) -> Dict[str, Any]:
    """*profile* with every statistic of the entry at *entry* dropped, as a release step drops them."""
    found = copy.deepcopy(profile)
    node: Any = found
    for token in entry:
        node = node[token]

    def strip(held: Dict[str, Any]) -> None:
        for key in list(held):
            if key == "columns":
                for column in held[key].values():
                    strip(column)
            elif key == "statistics":
                held[key] = []  # each statistic of the list is dropped, and the list is left
            elif key not in _IDENTITY:
                del held[key]

    strip(node)
    return found


def _weak_chain_editions() -> Dict[str, Any]:
    """The editions with a chain too loose for the bundle to read any member of the history."""
    profile = _edition_profile()
    profile["tables"]["edition"]["history"]["chain"] = history_init._levels(
        ["dayBefore", "gap", "nextStart", "overlap"], [0, 51, 449, 0], 500
    )
    return profile


def _released_editions(profile: Dict[str, Any]) -> Dict[str, Any]:
    """*profile* as released under a privacy unit: the editions' language is a noisy histogram."""
    found = copy.deepcopy(profile)
    found["_schemaURL"] = SCHEMA_URL_1_4_0
    found["disclosureRules"] = {**found["disclosureRules"], "noisy": {}}
    found["privacy"] = {
        "measure": "zeroConcentratedDP",
        "privacyLoss": {"rho": "0.5"},
        "privacyUnit": {"expose": "edition"},
    }
    found["tables"]["edition"]["columns"]["language"]["statistics"] = [
        _noisy_levels(["en", "fr"], ["1100", "750"])
    ]
    return found


#: Each kind: its name, the plain profile and the released one, the contract, and the entry.
_KINDS: Tuple[
    Tuple[str, Callable[[], Dict[str, Any]], Callable[[], Dict[str, Any]], str, str], ...
] = (
    (
        "date pair",
        _profile,
        lambda: _released_library("exact", "exact"),
        "/tables/member/datePairs/1",
        CONTRACT,
    ),
    (
        "relationship date pair",
        _profile,
        lambda: _released_library("exact", "exact"),
        "/relationships/0/dates/1",
        CONTRACT,
    ),
    (
        "exposure",
        _profile,
        lambda: _released_library("exact", "exact"),
        "/relationships/0/exposure/1",
        CONTRACT,
    ),
    (
        "history",
        _weak_chain_editions,
        lambda: _released_editions(_weak_chain_editions()),
        "/tables/edition/history",
        EDITION_CONTRACT,
    ),
    (
        "tracked column",
        _edition_profile,
        lambda: _released_editions(_edition_profile()),
        "/tables/edition/history/columns/language",
        EDITION_CONTRACT,
    ),
)
_IDS = [kind[0] for kind in _KINDS]


def _contract_of(contract: str, released: bool) -> str:
    return _RELEASED_CONTRACT if released and contract is CONTRACT else contract


def _unused_statistics(written: Authored) -> List[str]:
    """Every statistic the inventory of ``init --json`` marks unused, as a release step drops them."""
    return [
        item["pointer"]
        for item in written.result.artifacts["ledger"]["statistics"]
        if item["state"] == "unused"
    ]


def _tokens(pointer: str) -> Tuple[Any, ...]:
    return tuple(int(t) if t.isdigit() else t for t in pointer.strip("/").split("/"))


def _walk(document: Any, pointer: str) -> Any:
    node = document
    for token in pointer.strip("/").split("/"):
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


def _holds_only_its_names(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    for key, value in node.items():
        if key == "columns":
            if not all(_holds_only_its_names(each) for each in value.values()):
                return False
        elif key not in _IDENTITY and value != []:
            return False
    return True


@pytest.mark.parametrize("released", [False, True], ids=["plain", "released"])
@pytest.mark.parametrize(("name", "plain", "release", "entry", "contract"), _KINDS, ids=_IDS)
def test_a_profile_with_every_unused_statistic_dropped_is_read_by_init_and_plan(
    tmp_path: Path,
    released: bool,
    name: str,
    plain: Callable[[], Dict[str, Any]],
    release: Callable[[], Dict[str, Any]],
    entry: str,
    contract: str,
) -> None:
    profile = release() if released else plain()
    held = _contract_of(contract, released)
    first = Authored(tmp_path / "first", profile, held)
    assert first.result.exit_code == 0, first.result.stderr
    members = [p for p in _unused_statistics(first) if p.startswith(entry + "/")]
    assert members, f"the {name} is expected to be read by nothing"
    dropped = _drop(profile, _unused_statistics(first))
    # What the inventory called unused is every statistic of the entry, so only its names are left.
    assert _holds_only_its_names(_walk(dropped, entry)), _walk(dropped, entry)
    second = Authored(tmp_path / "second", dropped, held)
    assert second.result.exit_code == 0, second.result.stderr
    # An entry the profile publishes nothing of lists nothing of itself, used or not.
    listed = second.result.artifacts["ledger"]["statistics"]
    assert not [item for item in listed if item["pointer"].startswith(entry + "/")]
    planned = _plan(second, tmp_path / "second")
    assert planned.exit_code == 0, planned.stderr


@pytest.mark.parametrize(("name", "plain", "release", "entry", "contract"), _KINDS, ids=_IDS)
def test_the_strict_reader_refuses_what_the_engine_reads_as_holding_nothing(
    name: str,
    plain: Callable[[], Dict[str, Any]],
    release: Callable[[], Dict[str, Any]],
    entry: str,
    contract: str,
) -> None:
    profile = _emptied(plain(), _tokens(entry))
    with pytest.raises(FluxError):
        load_profile_text(json.dumps(profile))
    read, _ = load_profile_text(json.dumps(profile), allow_emptied_entries=True)
    assert read is not None


_MEMBERS = {
    "date pair": ("statistics/0", "order"),
    "relationship date pair": ("statistics/0", "order"),
    "exposure": ("children", "activeDays"),
    "history": ("chain", "lastVersion", "columns/language/initial", "reasons"),
    "tracked column": ("initial", "transitions"),
}


def _members_of(name: str, entry: str) -> List[str]:
    """Pointers a citation might use at, or under, *entry*."""
    return [entry, *(f"{entry}/{member}" for member in _MEMBERS[name])]


@pytest.mark.parametrize(("name", "plain", "release", "entry", "contract"), _KINDS, ids=_IDS)
def test_a_citation_at_or_under_an_entry_that_holds_nothing_is_refused_naming_the_entry(
    name: str,
    plain: Callable[[], Dict[str, Any]],
    release: Callable[[], Dict[str, Any]],
    entry: str,
    contract: str,
) -> None:
    tokens = _tokens(entry)
    profile = _emptied(plain(), tokens)
    for pointer in _members_of(name, entry):
        with pytest.raises(FluxError) as raised:
            check_profile_citations(_cites(pointer), profile, where="sim-x")
        assert raised.value.code.code == "FLX-5036", pointer
        message = str(raised.value)
        assert entry in message, pointer
        assert "holds none of its statistics" in message, pointer


#: A statistic a bundle may cite in each kind of entry, and its definition.
_READ = {
    "date pair": ("/tables/member/datePairs/0/order", "levelHistogram/1"),
    "relationship date pair": ("/relationships/0/dates/0/order", "levelHistogram/1"),
    "exposure": ("/relationships/0/exposure/0/children", "count/1"),
    "history": ("/tables/edition/history/chain", "levelHistogram/1"),
    "tracked column": (
        "/tables/edition/history/columns/binding_kind/initial",
        "levelHistogram/1",
    ),
}


@pytest.mark.parametrize(("name", "plain", "release", "entry", "contract"), _KINDS, ids=_IDS)
def test_a_citation_into_an_entry_that_still_holds_a_statistic_is_read_as_before(
    name: str,
    plain: Callable[[], Dict[str, Any]],
    release: Callable[[], Dict[str, Any]],
    entry: str,
    contract: str,
) -> None:
    pointer, definition = _READ[name]
    # The entry of the same kind that still holds its statistics, beside the emptied one. A
    # history stands alone in its table, so it is cited where it is whole.
    profile = plain() if name == "history" else _emptied(plain(), _tokens(entry))
    check_profile_citations(_cites(pointer, definition), profile, where="sim-x")


def test_an_entry_that_holds_some_of_its_statistics_is_still_refused_by_the_engine() -> None:
    # The option is for an entry that holds nothing: a date pair with statistics and no order,
    # an exposure with one of its counts, a history without its chain, are refused as before.
    cases: List[Tuple[Dict[str, Any], Tuple[Any, ...], str]] = [
        (_profile(), ("tables", "member", "datePairs", 1, "order"), "order"),
        (_profile(), ("relationships", 0, "dates", 1, "order"), "order"),
        (_profile(), ("relationships", 0, "exposure", 1, "children"), "children"),
        (_edition_profile(), ("tables", "edition", "history", "chain"), "chain"),
        (
            _edition_profile(),
            ("tables", "edition", "history", "columns", "language", "initial"),
            "initial",
        ),
    ]
    for profile, path, key in cases:
        node: Any = profile
        for token in path[:-1]:
            node = node[token]
        del node[path[-1]]
        with pytest.raises(FluxError, match=key):
            load_profile_text(json.dumps(profile), allow_emptied_entries=True)
