# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A date pair of a table never gives a second source to a column its history sets.

Pinned here, through ``run_init`` and the plan stage, on the tier history of a gym's members:

* a table's date pair on the history's validity start or end, or on its activation column, is
  recorded unused, and the history is written, with no ``lifetimes`` or ``within`` entry on those
  columns, whether the history comes from the profile or from ``--history FILE``;
* the same holds when only one of the pair's two columns is a history column, whichever place it
  takes in the pair, and whichever of the history's columns it is;
* a relationship's date pair whose parent column, or whose child column, is a history column is
  recorded unused, and no ``within`` entry or start-only lifetime is written from it;
* when the plan refuses the history, none is written, and the same date pairs declare as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.cli._init import run_init
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages.plan import plan
from tests.cli.test_init_dates import HOLDS, _levels, _link_pair, _pair

from .test_history_init import NAMESPACE, _history, _metrics, _ms

pytestmark = pytest.mark.integration

HISTORY_REASON = "the table's history sets"

#: The tier a gym member holds over time. ``pass_id`` is the entity the versions follow;
#: ``enrolled_on`` is the activation column of a declared history.
TIERS = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: gym.tiers
name: Tiers
metadata:
  owner:
    team: gym-team
exposes:
  - exposeId: gym_member
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/gym_member.parquet }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
  - exposeId: tier_version
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/tier_version.parquet }
    contract:
      schema:
        - { name: version_id, type: INT, required: true }
        - { name: pass_id, type: INT, required: true }
        - { name: member_id, type: INT, required: true }
        - { name: valid_from, type: DATE, required: true }
        - { name: valid_to, type: DATE, required: false }
        - { name: tier, type: STRING, required: true }
        - { name: channel, type: STRING, required: true }
        - { name: enrolled_on, type: DATE, required: true }
        - { name: renewed_on, type: DATE, required: false }
    semantics:
      entities:
        - { name: tier_version, type: primary, expr: version_id }
        - { name: pass, type: natural, expr: pass_id }
      dimensions:
        - { name: valid_from, type: time, typeParams: { validityParams: { isStart: true } } }
        - { name: valid_to, type: time, typeParams: { validityParams: { isEnd: true } } }
        - { name: tier, type: categorical }
        - { name: channel, type: categorical }
        - { name: enrolled_on, type: time }
        - { name: renewed_on, type: time }
      relationships:
        - name: member
          to: gym_member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
"""

#: The same contract with the member as the natural entity, which is also the relationship's
#: foreign key: a history lays its natural entity out on its own, so the plan refuses it.
REFUSED = TIERS.replace("expr: pass_id }", "expr: member_id }")


def _profile(*pairs: Dict[str, Any], entity: str = "pass_id") -> Dict[str, Any]:
    """The profile of ``TIERS``: a published history of ``tier_version`` and its *pairs*."""

    def column(low: str, high: str) -> Dict[str, Any]:
        return {"domain": "timestamp", "metrics": _metrics(low, high, 1850)}

    def number(high: str) -> Dict[str, Any]:
        return {"domain": "number", "metrics": _metrics("1", high, 1850)}

    history = _history()
    history["entity"] = [entity]
    version: Dict[str, Any] = {
        "rowCount": 1850,
        "columns": {
            "version_id": number("1850"),
            "pass_id": number("1000"),
            "member_id": number("1000"),
            "valid_from": column(_ms(2025, 1, 1), _ms(2025, 9, 1)),
            "valid_to": column(_ms(2025, 2, 1), _ms(2025, 10, 1)),
            "tier": {
                "domain": "category",
                "statistics": [
                    _history()["columns"]["tier"]["initial"],
                ],
            },
            "channel": {
                "domain": "category",
                "statistics": [
                    _history()["columns"]["channel"]["initial"],
                ],
            },
            "enrolled_on": column(_ms(2024, 6, 1), _ms(2025, 6, 1)),
            "renewed_on": column(_ms(2024, 7, 1), _ms(2025, 7, 1)),
        },
        "history": history,
    }
    if pairs:
        version["datePairs"] = list(pairs)
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-3-0/FluxProfile.json",
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "none": {},
            "acro-default": {
                "dominance": {"k": "0.90", "n": 2},
                "minimumCell": 10,
                "pRatio": "0.10",
                "roundingBase": 5,
                "zerosDisclosive": True,
            },
        },
        "tables": {
            "gym_member": {"rowCount": 1000, "columns": {"member_id": number("1000")}},
            "tier_version": version,
        },
    }


def _init(
    tmp_path: Path, profile: Dict[str, Any], contract: str = TIERS, history: Optional[Path] = None
) -> Any:
    (tmp_path / "tiers.fluid.yml").write_text(contract, encoding="utf-8")
    (tmp_path / "tiers.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "tiers.fluid.yml"),
        profile=str(tmp_path / "tiers.profile.json"),
        history=None if history is None else str(history),
    )


def _block(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    block: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return block


def _reasons(tmp_path: Path, pointer: str) -> list[str]:
    return [
        item["reason"]
        for item in _block(tmp_path)["unused"]
        if item["statistic"]["pointer"].startswith(pointer)
    ]


def _planned(tmp_path: Path) -> None:
    planned = plan(
        tmp_path / "bundle",
        state_dir=tmp_path / "state",
        profile=tmp_path / "tiers.profile.json",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr


def _history_file(tmp_path: Path) -> Path:
    """A history file that gives ``tier_version`` the activation column ``enrolled_on``."""
    document = {
        "timeBounds": {"start": "2024-01-01", "end": "2025-12-31"},
        "history": [
            {
                "emit": "tier_version",
                "entities": 1000,
                "versions": {"bins": ["1", "2", "4"], "binWeights": ["0.6", "0.4"]},
                "start": {"bins": ["2025-01-01", "2025-06-01"], "binWeights": ["1"]},
                "length": {
                    "bins": ["30", "90", "365"],
                    "binWeights": ["0.5", "0.5"],
                    "closed": "lower",
                },
                "until": "2025-12-31",
                "chain": "nextStart",
                "activation": "enrolled_on",
            }
        ],
    }
    path = tmp_path / "history.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_a_date_pair_on_the_validity_window_of_a_published_history_is_unused(
    tmp_path: Path,
) -> None:
    pair = _pair("valid_from", "valid_to", HOLDS)
    result = _init(tmp_path, _profile(pair))
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert "lifetimes" not in block and "within" not in block
    reasons = _reasons(tmp_path, "/tables/tier_version/datePairs/0")
    assert reasons and all(HISTORY_REASON in reason for reason in reasons)
    _planned(tmp_path)


def test_a_date_pair_on_the_activation_column_of_a_history_file_is_unused(tmp_path: Path) -> None:
    pair = _pair("enrolled_on", "valid_to", HOLDS)
    result = _init(tmp_path, _profile(pair), history=_history_file(tmp_path))
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert "lifetimes" not in block and "within" not in block
    reasons = _reasons(tmp_path, "/tables/tier_version/datePairs/0")
    assert reasons and all(HISTORY_REASON in reason for reason in reasons)
    _planned(tmp_path)


def test_a_date_pair_on_the_validity_window_of_a_history_file_is_unused(tmp_path: Path) -> None:
    pair = _pair("valid_from", "valid_to", HOLDS)
    result = _init(tmp_path, _profile(pair), history=_history_file(tmp_path))
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert "lifetimes" not in block
    reasons = _reasons(tmp_path, "/tables/tier_version/datePairs/0")
    assert reasons and all(HISTORY_REASON in reason for reason in reasons)


def test_a_date_pair_on_dates_the_history_leaves_alone_is_still_a_lifetime(tmp_path: Path) -> None:
    pair = _pair("enrolled_on", "renewed_on", HOLDS)
    result = _init(tmp_path, _profile(pair))
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert [(entry["start"], entry["end"]) for entry in block["lifetimes"]] == [
        ("enrolled_on", "renewed_on")
    ]
    _planned(tmp_path)


def test_a_history_the_plan_refuses_leaves_its_date_pairs_to_the_lifetime(tmp_path: Path) -> None:
    pair = _pair("valid_from", "valid_to", HOLDS)
    result = _init(tmp_path, _profile(pair, entity="member_id"), REFUSED)
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert "history" not in block
    assert block["lifetimes"] == [
        {
            "emit": "tier_version",
            "start": "valid_from",
            "end": "valid_to",
            "duration": {"bins": ["0", "1", "10", "30"], "binWeights": ["4", "3", "3"]},
        }
    ]
    assert _reasons(tmp_path, "/tables/tier_version/datePairs/0") == []


#: ``REFUSED`` with a member table no history projects: its ``nickname`` is no column of the
#: versions, so the bundle holds the history and no projection.
UNPROJECTED = REFUSED.replace(
    "        - { name: member_id, type: INT, required: true }\n    semantics:",
    "        - { name: member_id, type: INT, required: true }\n"
    "        - { name: nickname, type: STRING, required: true }\n    semantics:",
    1,
)


def test_a_history_the_plan_refuses_is_left_out_when_no_projection_is_written(
    tmp_path: Path,
) -> None:
    # Before, the history's refusal was reported (exit 1) when the bundle held no projection to
    # leave out first; it is left out, as it is when a projection is written.
    assert UNPROJECTED != REFUSED
    result = _init(tmp_path, _profile(entity="member_id"), UNPROJECTED)
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    assert "history" not in block
    assert not any(key in block for key in ("snapshots", "events", "periods"))
    reasons = _reasons(tmp_path, "/tables/tier_version/history")
    assert reasons and all("the bundle written with this history is refused" in r for r in reasons)
    assert all("the natural entity 'member_id'" in reason for reason in reasons)
    _planned(tmp_path)


#: One of a history's date columns, and one that is not, as a table's date pair names them. Each
#: case is (first, second, where the history comes from, the column the history sets).
ONE_HISTORY_COLUMN = {
    "the activation column, first": ("enrolled_on", "renewed_on", "file", "enrolled_on"),
    "the activation column, second": ("renewed_on", "enrolled_on", "file", "enrolled_on"),
    "the validity start, first": ("valid_from", "renewed_on", "profile", "valid_from"),
    "the validity start, second": ("renewed_on", "valid_from", "profile", "valid_from"),
    "the validity end, first": ("valid_to", "renewed_on", "profile", "valid_to"),
    "the validity end, second": ("renewed_on", "valid_to", "profile", "valid_to"),
}


@pytest.mark.parametrize("case", ONE_HISTORY_COLUMN)
def test_a_date_pair_with_one_history_column_is_unused_whichever_place_it_takes(
    tmp_path: Path, case: str
) -> None:
    first, second, source, column = ONE_HISTORY_COLUMN[case]
    pair = _pair(first, second, HOLDS)
    history = _history_file(tmp_path) if source == "file" else None
    result = _init(tmp_path, _profile(pair), history=history)
    assert result.exit_code == 0, result.stderr
    block = _block(tmp_path)
    # What is written: the history, and no lifetime or within entry that dates the pair.
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert "lifetimes" not in block and "within" not in block
    # What is unused, and why: the pair, for the one column the history sets.
    reasons = _reasons(tmp_path, "/tables/tier_version/datePairs/0")
    assert reasons and all(f"{HISTORY_REASON} '{column}'" in reason for reason in reasons)
    _planned(tmp_path)


# -- the date pairs of a relationship ---------------------------------------------------------------

#: ``TIERS`` with a date on the member, and visits that each refer to a version of a tier: the
#: version is the parent of a visit, and the member is the parent of a version.
LINKED = (
    TIERS.replace(
        "        - { name: member_id, type: INT, required: true }\n"
        "    semantics:\n"
        "      entities:\n"
        "        - { name: member, type: primary, expr: member_id }\n",
        "        - { name: member_id, type: INT, required: true }\n"
        "        - { name: joined_on, type: DATE, required: false }\n"
        "    semantics:\n"
        "      entities:\n"
        "        - { name: member, type: primary, expr: member_id }\n",
    )
    + """\
  - exposeId: visit
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/visit.parquet }
    contract:
      schema:
        - { name: visit_id, type: INT, required: true }
        - { name: version_id, type: INT, required: true }
        - { name: visited_on, type: DATE, required: false }
    semantics:
      entities:
        - { name: visit, type: primary, expr: visit_id }
      relationships:
        - name: tier
          to: tier_version
          fromColumns: [version_id]
          toColumns: [version_id]
          cardinality: many_to_one
"""
)

#: Every visit follows the start of its version, and every version follows the member's join date.
AFTER = _levels(after=300, before=0, same=100)


def _linked_profile(*links: Tuple[str, str, str, Dict[str, Any]]) -> Dict[str, Any]:
    """The profile of ``LINKED``, with a relationship's date pair for each of *links*.

    A link is (child table, parent table, joining column, date pair of the relationship).
    """
    found = _profile()

    def stamp(low: str, high: str, rows: int) -> Dict[str, Any]:
        return {"domain": "timestamp", "metrics": _metrics(low, high, rows)}

    found["tables"]["gym_member"]["columns"]["joined_on"] = stamp(
        _ms(2024, 1, 1), _ms(2024, 12, 1), 1000
    )
    found["tables"]["visit"] = {
        "rowCount": 3000,
        "columns": {
            "visit_id": {"domain": "number", "metrics": _metrics("1", "3000", 3000)},
            "version_id": {"domain": "number", "metrics": _metrics("1", "1850", 3000)},
            "visited_on": stamp(_ms(2025, 2, 1), _ms(2025, 11, 1), 3000),
        },
    }
    relationships: List[Dict[str, Any]] = []
    for child, parent, column, date in links:
        relationships.append(
            {
                "parent": {"table": parent, "columns": [column]},
                "child": {"table": child, "columns": [column]},
                "statistics": [],
                "dates": [date],
            }
        )
    found["relationships"] = relationships
    return found


def _linked(tmp_path: Path, profile: Dict[str, Any]) -> None:
    result = _init(tmp_path, profile, LINKED)
    assert result.exit_code == 0, result.stderr


def test_a_relationship_date_pair_on_a_history_column_of_the_parent_is_unused(
    tmp_path: Path,
) -> None:
    # A visit follows the start of the version it refers to; the version's history sets that start.
    link = _link_pair("valid_from", "visited_on", AFTER)
    _linked(tmp_path, _linked_profile(("visit", "tier_version", "version_id", link)))
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    # No entry dates the visit, and the version holds no start-only lifetime for it to follow.
    assert "within" not in block and "lifetimes" not in block
    reasons = _reasons(tmp_path, "/relationships/0/dates/0")
    assert reasons and all(f"{HISTORY_REASON} 'valid_from'" in reason for reason in reasons)
    _planned(tmp_path)


def test_a_relationship_date_pair_on_a_history_column_of_the_child_is_unused(
    tmp_path: Path,
) -> None:
    # A version starts after its member joined; the version's history sets that start.
    link = _link_pair("joined_on", "valid_from", AFTER)
    _linked(tmp_path, _linked_profile(("tier_version", "gym_member", "member_id", link)))
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    # No entry dates the version's start, and the member holds no start-only lifetime for it.
    assert "within" not in block and "lifetimes" not in block
    reasons = _reasons(tmp_path, "/relationships/0/dates/0")
    assert reasons and all(f"{HISTORY_REASON} 'valid_from'" in reason for reason in reasons)
    _planned(tmp_path)


def test_a_relationship_date_pair_on_dates_the_history_leaves_alone_is_still_a_within_entry(
    tmp_path: Path,
) -> None:
    # The same relationships, on columns the history does not set: the pair is declared.
    link = _link_pair("joined_on", "renewed_on", AFTER)
    _linked(tmp_path, _linked_profile(("tier_version", "gym_member", "member_id", link)))
    block = _block(tmp_path)
    assert [entry["emit"] for entry in block["history"]] == ["tier_version"]
    assert [(entry["emit"], entry["relationship"], entry["date"]) for entry in block["within"]] == [
        ("tier_version", "member", "renewed_on")
    ]
    assert _reasons(tmp_path, "/relationships/0/dates/0") == []
    _planned(tmp_path)


# -- --allow and an entry the unused list names whole -----------------------------------------------


def _strict(tmp_path: Path, name: str, allow: Tuple[str, ...] = ()) -> Any:
    return run_init(
        str(tmp_path / name),
        contract=str(tmp_path / "tiers.fluid.yml"),
        profile=str(tmp_path / "tiers.profile.json"),
        strict=True,
        allow=allow,
    )


def test_an_allow_of_a_member_of_an_entry_the_unused_list_names_whole_is_stale(
    tmp_path: Path,
) -> None:
    # The pair on the validity window is unused with every one of its statistics: the unused list
    # names the pair whole, by its own pointer, in place of its members.
    whole = "/tables/tier_version/datePairs/0"
    pair = _pair("valid_from", "valid_to", HOLDS)
    written = _init(tmp_path, _profile(pair))
    every = _strict(tmp_path, "every")
    assert every.exit_code == 1
    findings = every.artifacts["strict"]
    unused = [item["pointer"] for item in findings["unused"]]
    assert whole in unused
    assert not [pointer for pointer in unused if pointer.startswith(f"{whole}/")]
    accept = tuple(unused + [item["pointer"] for item in findings["gaps"]])
    # What the ledger holds under the pair: the members the unused list leaves out.
    members = [
        entry["pointer"]
        for entry in written.artifacts["ledger"]["statistics"]
        if entry["pointer"].startswith(f"{whole}/")
    ]
    assert members

    # The pair's own pointer accepts it, and so does a pointer above it.
    assert _strict(tmp_path, "whole", accept).exit_code == 0
    above = tuple(pointer for pointer in accept if pointer != whole) + ("/tables/tier_version",)
    assert _strict(tmp_path, "above", above).exit_code == 0

    # A member's pointer accepts nothing: the pair is still unused, and the pointer is stale.
    for position, member in enumerate(members):
        named = tuple(pointer for pointer in accept if pointer != whole) + (member,)
        result = _strict(tmp_path, f"member-{position}", named)
        assert result.exit_code == 1, member
        assert result.artifacts["strict"]["staleAllow"] == [member]
        assert [item["pointer"] for item in result.artifacts["strict"]["unused"]] == [whole]
        assert f"  --allow {member} accepts nothing" in result.stderr
        assert f"  unused {whole}" in result.stderr
