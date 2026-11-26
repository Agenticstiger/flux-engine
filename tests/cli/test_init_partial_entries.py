# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A release that drops each statistic the inventory marks unused leaves a profile the engine reads.

The inventory of ``init --json`` gives each statistic of a profile its state: ``used``, ``unused``
or ``withheld``. A release step drops the unused ones, one by one. When one member of an entry is
withheld and another unused, what is left holds part of the entry: a date pair with its order and
no statistic, an exposure with one of its counts, a history or a tracked column with some of its
members. The strict reader requires those members; the engine reads such an entry with the
members that are left (the profile reader's ``allow_emptied_entries``).

Pinned for each entry kind and each of its members, withheld in turn, on a plain profile and on a
released one: the profile with every unused statistic dropped is read again, ``init`` writes the
same bundle from it, and ``plan`` accepts that bundle.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest

from flux_engine.seam.parameters import check_profile_citations
from flux_stats.errors import FluxError
from flux_stats.profile import load_profile_text
from tests.cli.test_init_emptied_entries import _released_editions, _weak_chain_editions
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


def _withhold(node: Any) -> Any:
    """*node* as a profile that suppresses it publishes it: a status, and no value or counts."""
    if isinstance(node, list):
        return [_withhold(each) for each in node]
    found = copy.deepcopy(node)
    if "groups" in found:
        for group in found["groups"]:
            group["status"] = "C"
            for key in ("k", "levelCounts", "binCounts", "cellStatus", "noisyCounts"):
                group.pop(key, None)
        found.pop("noise", None)
        if found.get("disclosure") == "noisy":
            found["disclosure"] = "none"
    else:
        found["status"] = "C"
        found.pop("value", None)
    return found


def _at(document: Dict[str, Any], tokens: Tuple[Any, ...]) -> Any:
    node: Any = document
    for token in tokens:
        node = node[token]
    return node


#: Each entry kind: its name, the plain and the released profile, the contract of each, where the
#: entry is, and its members.
_KINDS: Tuple[
    Tuple[
        str,
        Callable[[], Dict[str, Any]],
        Callable[[], Dict[str, Any]],
        str,
        str,
        Tuple[Any, ...],
        Tuple[str, ...],
    ],
    ...,
] = (  # noqa: E501
    (
        "date pair",
        _profile,
        lambda: _released_library("exact", "exact"),
        CONTRACT,
        _RELEASED_CONTRACT,
        ("tables", "member", "datePairs", 1),
        ("statistics", "order"),
    ),
    (
        "relationship date pair",
        _profile,
        lambda: _released_library("exact", "exact"),
        CONTRACT,
        _RELEASED_CONTRACT,
        ("relationships", 0, "dates", 1),
        ("statistics", "order"),
    ),
    (
        "exposure",
        _profile,
        lambda: _released_library("exact", "exact"),
        CONTRACT,
        _RELEASED_CONTRACT,
        ("relationships", 0, "exposure", 1),
        ("children", "activeDays"),
    ),
    (
        "history",
        _weak_chain_editions,
        lambda: _released_editions(_weak_chain_editions()),
        EDITION_CONTRACT,
        EDITION_CONTRACT,
        ("tables", "edition", "history"),
        ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion"),
    ),
    (
        "tracked column",
        _edition_profile,
        lambda: _released_editions(_edition_profile()),
        EDITION_CONTRACT,
        EDITION_CONTRACT,
        ("tables", "edition", "history", "columns", "language"),
        ("initial", "transitions"),
    ),
)

_CASES = [
    pytest.param(
        kind, member, released, id=f"{kind[0]}-{member}-{'released' if released else 'plain'}"
    )
    for kind in _KINDS
    for member in kind[6]
    for released in (False, True)
]


def _unused(written: Authored) -> List[str]:
    """Every statistic the inventory of ``init --json`` marks unused, as a release step drops them."""
    return [
        item["pointer"]
        for item in written.result.artifacts["ledger"]["statistics"]
        if item["state"] == "unused"
    ]


@pytest.mark.parametrize(("kind", "member", "released"), _CASES)
def test_a_release_that_drops_the_unused_statistics_beside_a_withheld_one_is_read_again(
    tmp_path: Path, kind: Tuple[Any, ...], member: str, released: bool
) -> None:
    _, plain, release, contract, released_contract, entry, _ = kind
    profile = release() if released else plain()
    held = _at(profile, entry)
    if member not in held:
        pytest.skip(f"the entry holds no {member}")
    held[member] = _withhold(held[member])
    first = Authored(tmp_path / "first", profile, released_contract if released else contract)
    assert first.result.exit_code == 0, first.result.stderr
    dropped = _drop(profile, _unused(first))
    # The engine reads what the release leaves of the entry.
    load_profile_text(json.dumps(dropped), allow_emptied_entries=True)
    second = Authored(tmp_path / "second", dropped, released_contract if released else contract)
    assert second.result.exit_code == 0, second.result.stderr
    assert second.comparable() == first.comparable()
    planned = _plan(second, tmp_path / "plan")
    assert planned.success, planned.stderr


def test_a_citation_of_a_member_a_release_cut_is_refused_and_one_of_a_member_left_is_read() -> None:
    profile = _profile()
    del profile["relationships"][0]["exposure"][1]["children"]
    with pytest.raises(FluxError) as raised:
        check_profile_citations(
            _cites("/relationships/0/exposure/1/children", "count/1"), profile, where="sim-x"
        )
    assert raised.value.code.code == "FLX-3113"
    assert "/relationships/0/exposure/1" in str(raised.value)
    check_profile_citations(
        _cites("/relationships/0/exposure/1/activeDays", "count/1"), profile, where="sim-x"
    )
