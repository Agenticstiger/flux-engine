# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes an association table instead of refusing it (ADR 0039).

The contract holds members, clubs and their memberships, a table of two foreign keys and no other
column; the profile publishes each relationship's children per parent. Pinned here:

* init writes a ``counts`` entry for each side and an ``associations`` entry, with ``distinct:
  false`` when the published rows exceed the pairs of the two parents and ``distinct: true``
  otherwise, and the membership emit declares no column of its own, as the pairing sets both keys;
* a side's children follow no column of its parent: a grouping of them is listed unused;
* a run of the bundle holds every pair once, each side's rows per parent within verify's
  tolerance of its shape;
* with no published ``rowCount`` the rows are the inverse-variance mean of the two sides' totals:
  a published total wins over a histogram's, and two histograms meet between their sums.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.author import bundle
from flux_engine.author.association import association_pair
from flux_engine.seam.keys import ExposeKeys, Relationship
from flux_engine.stages.run import run
from flux_stats.profile import SCHEMA_URL_1_4_0
from tests.bundles import init_from_texts

from .test_children import NAMESPACE, _histogram, _mean, _metric

pytestmark = pytest.mark.integration

MEMBERS, CLUBS, ROWS = 600, 30, 1800
MEMBER_BINS, MEMBER_PARENTS = ["1", "2", "3", "5", "9"], [150, 200, 150, 100]
CLUB_BINS, CLUB_PARENTS = ["30", "50", "70", "100"], [10, 12, 8]
NAMES = ("club.fluid.yaml", "club.profile.json")

CONTRACT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: clubs.registry
name: Club registry
metadata:
  owner:
    team: registry-team
exposes:
  - exposeId: member
    kind: table
    binding: { platform: local, format: parquet, location: { path: ./out/member.parquet } }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
  - exposeId: club
    kind: table
    binding: { platform: local, format: parquet, location: { path: ./out/club.parquet } }
    contract:
      schema:
        - { name: club_id, type: INT, required: true }
    semantics:
      entities:
        - { name: club, type: primary, expr: club_id }
  - exposeId: membership
    kind: table
    binding: { platform: local, format: parquet, location: { path: ./out/membership.parquet } }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
        - { name: club_id, type: INT, required: true }
    semantics:
      relationships:
        - name: membership_to_member
          to: member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
        - name: membership_to_club
          to: club
          fromColumns: [club_id]
          toColumns: [club_id]
          cardinality: many_to_one
"""


def _key(rows: int) -> Dict[str, Any]:
    metrics = {"count": _metric("count/1", rows), "min": _metric("min/1", "1")}
    metrics["max"] = _metric("max/1", str(rows))
    return {"domain": "number", "metrics": metrics}


def _relationship(parent: str, column: str, statistics: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "parent": {"table": parent, "columns": [column]},
        "child": {"table": "membership", "columns": [column]},
        "statistics": statistics,
    }


def _profile(*, means: bool = True, total: int = ROWS) -> Dict[str, Any]:
    member = [_histogram(MEMBER_BINS, MEMBER_PARENTS, MEMBERS)]
    club = [_histogram(CLUB_BINS, CLUB_PARENTS, CLUBS)]
    if means:
        member.append(_mean(total, MEMBERS))
        club.append(_mean(total, CLUBS))
    count = {"domain": "number", "metrics": {"count": _metric("count/1", ROWS)}}
    return {
        "_producer": "https://example.invalid/profiler",
        "_schemaURL": "https://agenticstiger.github.io/flux/engine/spec/profile/1-0-0/FluxProfile.json",
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "disclosureRules": {
            "acro-default": {
                "dominance": {"k": "0.90", "n": 2},
                "minimumCell": 10,
                "pRatio": "0.10",
                "roundingBase": 5,
                "zerosDisclosive": True,
            }
        },
        "tables": {
            "member": {"rowCount": MEMBERS, "columns": {"member_id": _key(MEMBERS)}},
            "club": {"rowCount": CLUBS, "columns": {"club_id": _key(CLUBS)}},
            "membership": {
                "rowCount": ROWS,
                "columns": {"member_id": copy.deepcopy(count), "club_id": copy.deepcopy(count)},
            },
        },
        "relationships": [
            _relationship("member", "member_id", member),
            _relationship("club", "club_id", club),
        ],
    }


def _init(tmp_path: Path, profile: Dict[str, Any]) -> Dict[str, Any]:
    result = init_from_texts(tmp_path, CONTRACT, profile, NAMES)
    assert result.exit_code == 0, result.stderr
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    namespace: Dict[str, Any] = yaml.safe_load(text)["extensions"][NAMESPACE]
    return namespace


def test_init_writes_the_association_and_both_sides_counts(tmp_path: Path) -> None:
    namespace = _init(tmp_path, _profile())
    assert namespace["associations"] == [
        {
            "emit": "membership",
            "relationships": ["membership_to_member", "membership_to_club"],
            "distinct": True,
        }
    ]
    laid = {entry["relationship"] for entry in namespace["counts"] if entry["emit"] == "membership"}
    assert laid == {"membership_to_member", "membership_to_club"}
    emit = namespace["emits"]["membership"]
    assert emit["rows"] == ROWS and emit["fidelity"]["columns"] == {}


def test_a_run_of_the_bundle_holds_every_pair_once_and_each_side_s_shape(tmp_path: Path) -> None:
    _init(tmp_path, _profile())
    state = tmp_path / "state"
    outcome = run(tmp_path / "bundle", state_dir=state, profile=tmp_path / NAMES[1])
    assert outcome.success, outcome.stderr
    run_id = outcome.artifacts["plan"]["run_id"]
    text = (state / "runs" / run_id / "emits" / "membership.ndjson").read_text(encoding="utf-8")
    pairs = [tuple(json.loads(line)) for line in text.splitlines() if line]
    assert len(pairs) == ROWS and len(set(pairs)) == ROWS
    plan = json.loads((state / "runs" / run_id / "plan.json").read_text(encoding="utf-8"))
    report = plan["emits"]["membership"]["association"]
    assert all(Fraction(distance) <= Fraction(1, 10) for distance in report["distance"])


def _rows(tmp_path: Path, profile: Dict[str, Any]) -> int:
    del profile["tables"]["membership"]["rowCount"]
    tmp_path.mkdir(exist_ok=True)
    rows: int = _init(tmp_path, profile)["emits"]["membership"]["rows"]
    return rows


def test_with_no_row_count_a_published_total_sets_the_rows(tmp_path: Path) -> None:
    assert _rows(tmp_path / "both", _profile()) == ROWS
    one = _profile()
    one["relationships"][0]["statistics"].pop()  # members: a histogram alone
    assert _rows(tmp_path / "one", one) == ROWS  # the clubs' published total outweighs it


def test_with_no_row_count_two_histograms_meet_at_their_inverse_variance_mean(
    tmp_path: Path,
) -> None:
    def total(bins: List[str], parents: List[int]) -> "tuple[Fraction, Fraction]":
        edges = [int(edge) for edge in bins]
        found, variance = Fraction(0), Fraction(0)
        for index, held in enumerate(parents):
            high = edges[index + 1] - (0 if index == len(parents) - 1 else 1)
            width = high - edges[index] + 1
            found += Fraction(held * (edges[index] + high), 2)
            variance += Fraction(held * (width * width - 1), 12)
        return found, variance

    sides = [total(MEMBER_BINS, MEMBER_PARENTS), total(CLUB_BINS, CLUB_PARENTS)]
    weights = [1 / variance for _, variance in sides]
    mean = sum(w * s for w, (s, _) in zip(weights, sides, strict=True)) / sum(weights)
    rows = _rows(tmp_path, _profile(means=False))
    assert rows == int(mean + Fraction(1, 2))
    assert min(s for s, _ in sides) <= rows <= max(s for s, _ in sides)


def _run(tmp_path: Path) -> Any:
    state = tmp_path / "state"
    outcome = run(tmp_path / "bundle", state_dir=state, profile=tmp_path / NAMES[1])
    assert outcome.success, outcome.stderr
    run_id = outcome.artifacts["plan"]["run_id"]
    text = (state / "runs" / run_id / "emits" / "membership.ndjson").read_text(encoding="utf-8")
    return [tuple(json.loads(line)) for line in text.splitlines() if line]


def test_published_rows_above_the_pairs_say_that_pairs_repeat(tmp_path: Path) -> None:
    # 20,000 rows over 600 members and 30 clubs, which make 18,000 pairs: some pair repeats.
    profile = _profile(total=20000)
    profile["tables"]["membership"]["rowCount"] = 20000
    result = init_from_texts(tmp_path, CONTRACT, profile, NAMES)
    assert result.exit_code == 0, result.stderr
    namespace = yaml.safe_load(
        (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    )["extensions"][NAMESPACE]
    assert namespace["emits"]["membership"]["rows"] == 20000
    assert namespace["associations"][0]["distinct"] is False
    assert "more than the 18000 pairs" in result.stdout
    assert len(_run(tmp_path)) == 20000


def test_published_rows_at_the_pairs_keep_each_pair_once(tmp_path: Path) -> None:
    profile = _profile(total=MEMBERS * CLUBS)
    profile["tables"]["membership"]["rowCount"] = MEMBERS * CLUBS
    namespace = _init(tmp_path, profile)
    assert namespace["associations"][0]["distinct"] is True
    pairs = _run(tmp_path)
    assert len(pairs) == len(set(pairs)) == MEMBERS * CLUBS


def test_a_published_row_count_wins_over_the_totals(tmp_path: Path) -> None:
    profile = _profile()  # the totals say 1,800
    profile["tables"]["membership"]["rowCount"] = 1700
    assert _init(tmp_path, profile)["emits"]["membership"]["rows"] == 1700


def test_derived_rows_are_kept_within_the_pairs(tmp_path: Path) -> None:
    # Both totals say 20,000, more than the 18,000 pairs, and no rowCount says otherwise.
    namespace = _init(tmp_path, _rowless(_profile(total=20000)))
    assert namespace["emits"]["membership"]["rows"] == MEMBERS * CLUBS
    assert namespace["associations"][0]["distinct"] is True


def _rowless(profile: Dict[str, Any]) -> Dict[str, Any]:
    del profile["tables"]["membership"]["rowCount"]
    return profile


def _keys(*columns: str) -> ExposeKeys:
    relationships = tuple(
        Relationship(name, "link", name, (column,), (column,), "many_to_one", "left")
        for name, column in zip(("a", "b"), columns, strict=False)
    )
    return ExposeKeys("link", relationships=relationships)


@pytest.mark.parametrize(
    ("keys", "columns", "found"),
    [
        (_keys("x", "y"), ["x", "y"], True),
        (_keys("x", "y"), ["x", "y", "note"], False),  # a column of its own: not an association
        (_keys("x", "y"), ["x"], False),
        (_keys("x", "x"), ["x"], False),  # both keys in one column
        (_keys("x"), ["x"], False),
    ],
)
def test_an_association_is_two_keys_and_every_column(
    keys: ExposeKeys, columns: List[str], found: bool
) -> None:
    assert (association_pair(keys, columns) is not None) is found


#: The members carry a segment, by which the profile also groups their children per member.
SEGMENTED = CONTRACT.replace(
    "        - { name: member_id, type: INT, required: true }\n"
    "    semantics:\n      entities:\n        - { name: member, type: primary, expr: member_id }",
    "        - { name: member_id, type: INT, required: true }\n"
    "        - { name: segment, type: \"enum('a','b')\", required: true }\n"
    "    semantics:\n      entities:\n        - { name: member, type: primary, expr: member_id }",
    1,
)


def _segmented() -> Dict[str, Any]:
    """The profile with each member's segment, and the children per member grouped by it."""
    profile = _profile()
    profile["_schemaURL"] = SCHEMA_URL_1_4_0
    half = MEMBERS // 2
    profile["tables"]["member"]["columns"]["segment"] = {
        "domain": "category",
        "metrics": {"count": _metric("count/1", MEMBERS), "nullCount": _metric("nullCount/1", 0)},
        "statistics": [
            {
                "definition": "levelHistogram/1",
                "levels": ["a", "b"],
                "sampleSize": {"min": 1, "max": 99999},
                "disclosure": "acro-default",
                "groups": [{"group": "", "k": MEMBERS, "levelCounts": [half, half], "status": "F"}],
            }
        ],
    }
    grouped = copy.deepcopy(profile["relationships"][0]["statistics"][0])
    grouped["groupBy"] = "segment"
    grouped["groups"] = [
        {"group": name, "k": half, "binCounts": [75, 100, 75, 50], "status": "F"}
        for name in ("a", "b")
    ]
    profile["relationships"][0]["statistics"].append(grouped)
    return profile


def test_a_side_s_children_follow_no_column_of_its_parent(tmp_path: Path) -> None:
    # A side whose children followed the segment would pair with no stub of the other side, so
    # init lists the grouping unused and still writes the table.
    assert "segment" in SEGMENTED
    result = init_from_texts(tmp_path, SEGMENTED, _segmented(), NAMES)
    assert result.exit_code == 0, result.stderr
    namespace = yaml.safe_load(
        (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    )["extensions"][NAMESPACE]
    assert [entry["emit"] for entry in namespace["associations"]] == ["membership"]
    member = next(
        entry for entry in namespace["counts"] if entry["relationship"] == "membership_to_member"
    )
    assert "given" not in member and "childrenGiven" not in member
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    grouped = states["/relationships/0/statistics/2"]
    assert grouped["state"] == "unused"
    assert "stub by stub" in grouped["note"]
    assert len(_run(tmp_path)) == ROWS


def test_a_table_with_one_side_counted_keeps_the_given_of_its_children(tmp_path: Path) -> None:
    # Without a profile relationship for the clubs the table is no association, so its one counted
    # side keeps the grouping by segment: nothing pairs its rows with the other side's stubs.
    profile = _segmented()
    del profile["relationships"][1]
    result = init_from_texts(tmp_path, SEGMENTED, profile, NAMES)
    assert result.exit_code == 0, result.stderr
    namespace = yaml.safe_load(
        (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    )["extensions"][NAMESPACE]
    assert "associations" not in namespace
    member = next(
        entry for entry in namespace["counts"] if entry["relationship"] == "membership_to_member"
    )
    assert member["given"] == "segment" and "childrenGiven" in member
    states = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}
    assert states["/relationships/0/statistics/2"]["state"] == "used"


def test_a_reference_table_is_never_an_association_table(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reference expose is written whole, with no counts entry and no association, whatever its
    # shape: the table of two keys that is drawn is the only one named.
    pair = (SimpleNamespace(name="a"), SimpleNamespace(name="b"))
    monkeypatch.setattr(bundle, "association_pair", lambda keys, columns: pair)
    keys = SimpleNamespace(expose=lambda expose_id: expose_id)
    planned = {
        "whole": SimpleNamespace(reference=True, columns=[]),
        "drawn": SimpleNamespace(reference=False, columns=[]),
    }
    slots = [
        SimpleNamespace(child_table=table, declared=side, placed=SimpleNamespace(entry={}))
        for table in planned
        for side in pair
    ]
    held = bundle._association_tables(planned, keys, slots)  # type: ignore[arg-type]
    assert held == frozenset({"drawn"})
