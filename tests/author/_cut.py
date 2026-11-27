# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A release cut by the statistics ``init`` lists as unused, as a release step cuts it.

Dropping what ``init`` lists as unused from a profile is meant to give the bundle the whole profile
gave. A release step cannot drop every statistic alone: a member of a date pair or a relationship's
date entry that the reader requires goes only with its entry, and an entry goes once every
statistic it holds is unused. :func:`release_units` is that rule.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from flux_engine.cli._init import run_init
from tests.cli.test_init_ledger_entries import _drop

NAMESPACE = "com.agentics.flux/1"

#: The entries of a profile whose members go together: the pattern of a member's pointer, the
#: member names an entry holds, and the members the reader requires.
ENTRIES: Tuple[Tuple[Any, Tuple[str, ...], Tuple[str, ...]], ...] = (
    (
        re.compile(r"^(/tables/[^/]+/calendar/\d+)/(month|dayOfWeek|hourOfWeek)$"),
        ("month", "dayOfWeek", "hourOfWeek"),
        (),
    ),
    (
        re.compile(r"^(/tables/[^/]+/datePairs/\d+)/(order|statistics/\d+)$"),
        ("order", "statistics"),
        ("order",),
    ),
    (
        re.compile(r"^(/relationships/\d+/dates/\d+)/(order|statistics/\d+)$"),
        ("order", "statistics"),
        ("order",),
    ),
    (
        re.compile(r"^(/relationships/\d+/exposure/\d+)/(children|activeDays)$"),
        ("children", "activeDays"),
        ("children", "activeDays"),
    ),
    (re.compile(r"^(/tables/[^/]+/periods/\d+)/(statistics/\d+)$"), ("statistics",), ()),
)

#: What a bundle holds beside what it generates from: the pinned profile, the register of the
#: parameters, and the list of unused statistics, which name statistics by position.
FOLLOWS_THE_PROFILE = ("profile", "parameters", "unused")


def _at(document: Any, pointer: str) -> Any:
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        document = document[int(token)] if isinstance(document, list) else document[token]
    return document


def _members(document: Any, entry: str, keys: Sequence[str]) -> List[str]:
    node = _at(document, entry)
    found: List[str] = []
    for key in keys:
        if key == "statistics":
            found += [f"{entry}/statistics/{i}" for i in range(len(node.get("statistics") or []))]
        elif key in node:
            found.append(f"{entry}/{key}")
    return found


def release_units(profile: Dict[str, Any], unused: Sequence[str]) -> List[str]:
    """What a release can drop of the *unused* statistics, in their order.

    A member of an entry goes alone when the reader allows it; a whole entry goes, at its first
    member's place, once every statistic it holds is unused; a required member of an entry that
    stays is kept. Every other pointer goes alone.
    """
    listed = set(unused)
    gone: List[str] = []
    whole: set[str] = set()
    for pointer in unused:
        hit = next(
            (
                (found, keys, required)
                for rx, keys, required in ENTRIES
                if (found := rx.match(pointer))
            ),
            None,
        )
        if hit is None:
            gone.append(pointer)
            continue
        found, keys, required = hit
        entry, member = found.group(1), found.group(2)
        if entry in whole:
            continue
        if all(item in listed for item in _members(profile, entry, keys)):
            whole.add(entry)
            gone.append(entry)
        elif member not in required:
            gone.append(pointer)
    return gone


def generated(directory: Any) -> Dict[str, Any]:
    """What the bundle in *directory* generates from: its simulation, less what follows the profile."""
    text = (directory / "simulation.flux.yml").read_text(encoding="utf-8")
    document: Dict[str, Any] = yaml.safe_load(text)
    block = {
        key: value
        for key, value in document.get("extensions", {}).get(NAMESPACE, {}).items()
        if key not in FOLLOWS_THE_PROFILE
    }
    return {"spec": document.get("spec"), "extension": block}


def listed_unused(directory: Any) -> List[str]:
    """The statistics the bundle in *directory* lists as unused."""
    text = (directory / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Optional[Dict[str, Any]] = yaml.safe_load(text).get("extensions", {}).get(NAMESPACE)
    return [item["statistic"]["pointer"] for item in (block or {}).get("unused", [])]


def init_profile(directory: Path, contract: Path, profile: Dict[str, Any]) -> Any:
    """``init`` of *contract* and the document *profile* into *directory*; the result."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cut.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    where = {"contract": str(contract), "profile": str(directory / "cut.profile.json")}
    return run_init(str(directory / "bundle"), **where)


def broken_cuts(root: Path, contract: Path, profile: Dict[str, Any]) -> List[str]:
    """The cuts of *profile* whose bundle is not the whole profile's: none, for a release to cut.

    The cuts are the list the bundle names as unused and the ledger's statistics in the state
    unused as a release drops them (:func:`release_units`). A pair the engine refuses has none.
    """
    whole = init_profile(root / "whole", contract, profile)
    if whole.exit_code != 0:
        return []
    statistics = whole.artifacts["ledger"]["statistics"]
    cuts = {
        "bundle list": listed_unused(root / "whole" / "bundle"),
        "ledger states": release_units(
            profile, [item["pointer"] for item in statistics if item.get("state") == "unused"]
        ),
    }
    broken: List[str] = []
    for name, pointers in cuts.items():
        if not pointers:
            continue
        cut = init_profile(root / name.replace(" ", "-"), contract, _drop(profile, pointers))
        if cut.exit_code != 0 or generated(root / name.replace(" ", "-") / "bundle") != generated(
            root / "whole" / "bundle"
        ):
            broken.append(name)
    return broken
