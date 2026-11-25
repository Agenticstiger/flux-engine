# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes an association table instead of refusing it (ADR 0039).

The contract holds members, clubs and their memberships, a table of two foreign keys and no other
column; the profile publishes each relationship's children per parent. Pinned here:

* init writes a ``counts`` entry for each side and an ``associations`` entry with ``distinct:
  true``, and the membership emit declares no column of its own, as the pairing sets both keys;
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
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.stages.run import run
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


def _profile(*, means: bool = True) -> Dict[str, Any]:
    member = [_histogram(MEMBER_BINS, MEMBER_PARENTS, MEMBERS)]
    club = [_histogram(CLUB_BINS, CLUB_PARENTS, CLUBS)]
    if means:
        member.append(_mean(ROWS, MEMBERS))
        club.append(_mean(ROWS, CLUBS))
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
