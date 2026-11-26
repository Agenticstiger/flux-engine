# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history tracks a foreign key whose profile publishes it as a category (ADR 0017 section 13)."""

from __future__ import annotations

import json
from collections import Counter
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml

from flux_engine.author.history import _level_weights
from flux_engine.cli._init import run_init
from flux_engine.stages.run import run
from flux_engine.stages.verify import verify

from ._written import bundle_digests
from .test_history_init import _levels, _metrics, _profile, _transitions

pytestmark = pytest.mark.integration

PLAN_IDS = [str(key) for key in range(11, 36)]

CONTRACT_TEXT = """\
fluidVersion: "0.7.7"
kind: DataProduct
id: gym.memberships
name: Memberships
metadata:
  owner:
    team: gym-team
exposes:
  - exposeId: member
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/member.parquet }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
  - exposeId: plan
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/plan.parquet }
    contract:
      schema:
        - { name: plan_id, type: BIGINT, required: true }
        - { name: label, type: STRING, required: true }
    semantics:
      entities:
        - { name: plan, type: primary, expr: plan_id }
  - exposeId: membership_version
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/membership_version.parquet }
    contract:
      schema:
        - { name: version_id, type: INT, required: true }
        - { name: membership_id, type: INT, required: true }
        - { name: member_id, type: INT, required: true }
        - { name: valid_from, type: DATE, required: true }
        - { name: valid_to, type: DATE, required: false }
        - { name: plan_id, type: BIGINT, required: true }
    semantics:
      entities:
        - { name: membership_version, type: primary, expr: version_id }
        - { name: membership, type: natural, expr: membership_id }
      dimensions:
        - { name: valid_from, type: time, typeParams: { validityParams: { isStart: true } } }
        - { name: valid_to, type: time, typeParams: { validityParams: { isEnd: true } } }
      relationships:
        - name: member
          to: member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
        - name: plan
          to: plan
          fromColumns: [plan_id]
          toColumns: [plan_id]
          cardinality: many_to_one
"""


def _moves(levels: List[str]) -> Dict[str, List[int]]:
    """Each level moves to the next two (cyclically) in equal numbers."""
    size = len(levels)
    rows: Dict[str, List[int]] = {}
    for position, level in enumerate(levels):
        row = [0] * size
        row[(position + 1) % size] = 40
        row[(position + 2) % size] = 40
        rows[level] = row
    return rows


def _profile_of(
    levels: List[str], plan_rows: int = 25, shares: Optional[List[int]] = None
) -> Dict[str, Any]:
    base = _profile()
    version = base["tables"]["plan_version"]
    shares = shares or [100 + 10 * position for position in range(len(levels))]
    total = sum(shares)
    version["history"]["columns"] = {
        "plan_id": {
            "initial": _levels(levels, shares, total),
            "transitions": _transitions("plan_id", levels, _moves(levels)),
        }
    }
    version["history"]["entity"] = ["membership_id"]
    version["history"]["reasons"] = _levels(["plan_id", "several"], [800, 50], 850)
    columns = {
        "version_id": version["columns"]["version_id"],
        "membership_id": {"domain": "number", "metrics": _metrics("1", "1000", 1850)},
        "member_id": {"domain": "number", "metrics": _metrics("1", "1000", 1850)},
        "valid_from": version["columns"]["valid_from"],
        "valid_to": version["columns"]["valid_to"],
        "plan_id": {"domain": "category", "statistics": [_levels(levels, shares, total)]},
    }
    version["columns"] = columns
    base["tables"] = {
        "member": base["tables"]["account"]
        | {"columns": {"member_id": {"domain": "number", "metrics": _metrics("1", "1000", 1000)}}},
        "plan": {
            "rowCount": plan_rows,
            "columns": {
                "plan_id": {
                    "domain": "number",
                    "metrics": {"count": _metrics("1", "2", plan_rows)["count"]},
                },
                "label": {
                    "domain": "category",
                    "statistics": [_levels(["a", "b"], [15, 10], plan_rows)],
                },
            },
        },
        "membership_version": version,
    }
    return base


def _init(tmp_path: Path, profile: Dict[str, Any]) -> Any:
    (tmp_path / "c.fluid.yml").write_text(CONTRACT_TEXT, encoding="utf-8")
    (tmp_path / "c.profile.json").write_text(json.dumps(profile), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "c.fluid.yml"),
        profile=str(tmp_path / "c.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    return result


def _namespace(tmp_path: Path) -> Dict[str, Any]:
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)["extensions"]["com.agentics.flux/1"]


def _run(tmp_path: Path) -> Tuple[Any, List[List[Any]], List[List[Any]]]:
    """Run the written bundle, verify it, and read the version and plan rows it wrote."""
    bundle, state, profile = tmp_path / "bundle", tmp_path / "state", tmp_path / "c.profile.json"
    outcome = run(bundle, state_dir=state, profile=profile)
    assert outcome.success, outcome.stderr
    run_id = outcome.artifacts["plan"]["run_id"]
    verified = verify(bundle, run_id=run_id, state_dir=state, profile=profile)
    assert verified.success, verified.stdout + verified.stderr
    emits = state / "runs" / run_id / "emits"

    def rows(name: str) -> List[List[Any]]:
        return [json.loads(line) for line in (emits / f"{name}.ndjson").read_text().splitlines()]

    return verified, rows("membership_version"), rows("plan")


def test_the_history_tracks_a_foreign_key_published_as_a_category(tmp_path: Path) -> None:
    _init(tmp_path, _profile_of(PLAN_IDS))
    namespace = _namespace(tmp_path)
    column = namespace["emits"]["membership_version"]["fidelity"]["columns"]["plan_id"]
    assert list(column["levels"]) == PLAN_IDS
    tracked = {item["column"]: item for item in namespace["history"][0]["tracked"]}
    assert set(tracked) == {"plan_id"}
    assert tracked["plan_id"]["initial"]["levels"] == PLAN_IDS
    assert set(tracked["plan_id"]["transitions"]) == set(PLAN_IDS)
    # The catalogue's keys are the levels: the key spans them, one value per row.
    assert namespace["emits"]["plan"]["fidelity"]["columns"]["plan_id"]["bins"] == ["11", "35"]


def test_every_generated_foreign_key_is_a_key_of_the_catalogue(tmp_path: Path) -> None:
    _init(tmp_path, _profile_of(PLAN_IDS))
    verified, versions, plans = _run(tmp_path)
    assert "integrity membership_version.plan -> plan: found 1850 of 1850 (100.00%)" in (
        verified.stdout
    )
    keys = {row[0] for row in plans}
    assert keys == {int(level) for level in PLAN_IDS}
    assert {row[5] for row in versions} <= keys
    by_member: Dict[int, set] = {}
    for row in versions:
        by_member.setdefault(row[1], set()).add(row[5])
    assert sum(len(plans_held) > 1 for plans_held in by_member.values()) > 100
    counts = Counter(row[5] for row in versions)
    published = [Fraction(100 + 10 * position, 1) for position in range(len(PLAN_IDS))]
    total = sum(published)
    distance = (
        sum(
            abs(Fraction(counts[int(level)], len(versions)) - share / total)
            for level, share in zip(PLAN_IDS, published, strict=True)
        )
        / 2
    )
    assert distance < Fraction(1, 10)


def test_a_catalogue_with_one_row_fewer_than_the_levels_drops_the_small_end_level(
    tmp_path: Path,
) -> None:
    levels = [str(key) for key in range(11, 37)]
    shares = [40] * 25 + [17]
    result = _init(tmp_path, _profile_of(levels, shares=shares))
    namespace = _namespace(tmp_path)
    column = namespace["emits"]["membership_version"]["fidelity"]["columns"]["plan_id"]
    assert list(column["levels"]) == PLAN_IDS
    assert sum(Fraction(share) for share in column["levels"].values()) == 1
    assert namespace["emits"]["plan"]["fidelity"]["columns"]["plan_id"]["bins"] == ["11", "35"]
    tracked = {item["column"]: item for item in namespace["history"][0]["tracked"]}
    assert tracked["plan_id"]["initial"]["levels"] == PLAN_IDS
    assert "36" not in tracked["plan_id"]["transitions"]
    assert all("36" not in row for row in tracked["plan_id"]["transitions"].values())
    note = "plan writes 25 rows, so the foreign key keeps 11..35 and drops 36 (1.7 % of the rows)"
    assert note in result.stdout
    verified, versions, plans = _run(tmp_path)
    assert "integrity membership_version.plan -> plan: found 1850 of 1850 (100.00%)" in (
        verified.stdout
    )
    assert {row[5] for row in versions} <= {row[0] for row in plans}


def test_dropping_more_than_a_twentieth_of_the_rows_keeps_the_column_carried(
    tmp_path: Path,
) -> None:
    levels = [str(key) for key in range(11, 38)]
    result = _init(tmp_path, _profile_of(levels, shares=[40] * 27))
    namespace = _namespace(tmp_path)
    assert "levels" not in namespace["emits"]["membership_version"]["fidelity"]["columns"].get(
        "plan_id", {}
    )
    assert not namespace["history"][0].get("tracked")
    pointer = "/tables/membership_version/history/columns/plan_id/transitions"
    state = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}[pointer]
    assert state["state"] == "unused"
    assert "would drop 7.4 % of the rows, more than 5 %" in state["note"]
    assert "publish fewer levels or more rows" in state["note"]


def test_a_column_that_is_no_foreign_key_is_not_drawn_over_its_levels(tmp_path: Path) -> None:
    plain = CONTRACT_TEXT.split("        - name: plan\n")[0]
    (tmp_path / "c.fluid.yml").write_text(plain, encoding="utf-8")
    (tmp_path / "c.profile.json").write_text(json.dumps(_profile_of(PLAN_IDS)), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "c.fluid.yml"),
        profile=str(tmp_path / "c.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    column = _namespace(tmp_path)["emits"]["membership_version"]["fidelity"]["columns"]["plan_id"]
    assert "levels" not in column


def test_a_foreign_key_whose_levels_are_no_whole_numbers_is_unchanged(tmp_path: Path) -> None:
    result = _init(tmp_path, _profile_of([f"p{key}" for key in PLAN_IDS]))
    column = _namespace(tmp_path)["emits"]["membership_version"]["fidelity"]["columns"]["plan_id"]
    assert "levels" not in column
    assert not _namespace(tmp_path)["history"][0].get("tracked")
    pointer = "/tables/membership_version/history/columns/plan_id/transitions"
    state = {entry["pointer"]: entry for entry in result.artifacts["ledger"]["statistics"]}[pointer]
    assert "the emit declares no levels for the column, so it is carried" in state["note"]


#: sha256 of each file `fluxsim init` wrote for these inputs before a tracked foreign key was drawn
#: over its levels: a contract with no foreign key to plan, and a foreign key whose levels are text.
PLAIN_DIGESTS: Dict[str, str] = {
    "c.fluid.yml": "f1ec7e6002bab59b66e6bce40a7b05dc8246982175767688664a41ca3761baf0",
    "persona.flux.yml": "73d41e8428f1f7f454c22e06543d172135a1ac5272022335975a6a2ae6a97b9b",
    "simulation.flux.yml": "e24f833404af19bb08eb3c471097ec09eab32849be6f3bc550befa026c9ca59d",
    "world.flux.yml": "388491238d7fed9ced4cb31589058cf895c90aa7cca0fc991e5370ee3786b7db",
}
TEXT_DIGESTS: Dict[str, str] = {
    "c.fluid.yml": "440e45590711bbf0c9be5786b01243ca566c64e486dfd163eb954a3f8b871e98",
    "persona.flux.yml": "73d41e8428f1f7f454c22e06543d172135a1ac5272022335975a6a2ae6a97b9b",
    "simulation.flux.yml": "1e3270d97252ed2820d5ab280a47e05aeb07e00d97a80458c533131314270724",
    "world.flux.yml": "388491238d7fed9ced4cb31589058cf895c90aa7cca0fc991e5370ee3786b7db",
}


def test_a_bundle_without_a_whole_number_foreign_key_is_byte_identical(tmp_path: Path) -> None:
    plain = CONTRACT_TEXT.split("        - name: plan\n")[0]
    (tmp_path / "c.fluid.yml").write_text(plain, encoding="utf-8")
    (tmp_path / "c.profile.json").write_text(json.dumps(_profile_of(PLAN_IDS)), encoding="utf-8")
    result = run_init(
        str(tmp_path / "bundle"),
        contract=str(tmp_path / "c.fluid.yml"),
        profile=str(tmp_path / "c.profile.json"),
    )
    assert result.exit_code == 0, result.stderr
    assert bundle_digests(tmp_path) == PLAIN_DIGESTS
    other = tmp_path / "text"
    other.mkdir()
    _init(other, _profile_of([f"p{key}" for key in PLAN_IDS]))
    assert bundle_digests(other) == TEXT_DIGESTS


def test_a_dropped_level_leaves_the_records_the_withheld_cells_share() -> None:
    # A group of 1000 records: 300 and 500 are published and one cell is withheld, which shares
    # the 200 records they leave. Dropping the 500 level takes its records out of the group, so
    # the withheld cell still holds 200 and does not take the 500 over.
    node = {"levels": ["a", "b", "c", "d"]}
    group = {"k": 1000, "levelCounts": [0, 300, 500, None], "cellStatus": ["F", "F", "F", "C"]}
    source = SimpleNamespace(minimum_cell=5)
    pointer = ("tables", "t", "history")
    plain = _level_weights(node, group, pointer, source)  # type: ignore[arg-type]
    assert plain is not None and plain["d"] == 200 and plain["c"] == 500
    dropped = _level_weights(node, group, pointer, source, dropped=("c",))  # type: ignore[arg-type]
    assert dropped is not None
    assert dropped["c"] == 0 and dropped["b"] == 300 and dropped["d"] == 200
