# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's whole-number key whose bins weigh versions is written, not declined.

The profile measures a key's histogram over the table's rows, so a bin's weight counts versions.
A history lays the key out over its entities, and a bin whose entities have more versions than
average would take more entities than it has values. The layout fills such a bin up to its width
and shares the rest over the other bins, so ``init`` writes the history, and the bundle plans,
runs and verifies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.stages.apply import apply
from flux_engine.stages.plan import plan
from flux_engine.stages.verify import verify

from ._written import applied_and_verified, emitted_rows
from .test_history_init import (
    HISTORY,
    _bins,
    _entries,
    _init,
    _profile,
    _states,
)

pytestmark = pytest.mark.integration

EDGES = ["1", "101", "1000"]


def _binned(counts: Any) -> Dict[str, Any]:
    """The history profile, its natural key weighed by the versions each bin holds."""
    profile = _profile()
    column = profile["tables"]["plan_version"]["columns"]["plan_id"]
    column["statistics"] = [_bins(EDGES, counts, 1850)]
    return profile


def test_a_key_bin_that_weighs_more_versions_than_it_has_values_is_still_written(
    tmp_path: Path,
) -> None:
    # 600 of the 1850 versions fall in the first 100 ids, so its weight would give it 324
    # of the 1000 entities, and it holds 100 values.
    result = _init(tmp_path, _binned([600, 1250]))
    assert [entry["emit"] for entry in _entries(tmp_path)] == ["plan_version"]
    assert _states(result)[f"{HISTORY}/versionsPerEntity"]["state"] == "used"


def test_the_written_bundle_plans_generates_and_verifies(tmp_path: Path) -> None:
    _init(tmp_path, _binned([600, 1250]))
    applied_and_verified(tmp_path)
    rows = emitted_rows(tmp_path, "plan_version", "plans.profile.json")
    ids = sorted({row[1] for row in rows})
    assert len(ids) == 1000
    assert sum(1 for key in ids if key <= 100) == 100


def _verified_with_profile(tmp_path: Path) -> None:
    """Plan, generate and verify the written bundle, scoring it against the profile."""
    bundle, state = tmp_path / "bundle", tmp_path / "state"
    planned = plan(
        bundle,
        state_dir=state,
        profile=tmp_path / "plans.profile.json",
        kernel="0.2.0",
        stage=False,
    )
    assert planned.success, planned.stderr
    run_id = str(planned.artifacts["run_id"])
    applied = apply(bundle, state_dir=state, run_id=run_id)
    assert applied.success, applied.stderr
    checked = verify(
        bundle, state_dir=state, run_id=run_id, profile=tmp_path / "plans.profile.json"
    )
    assert checked.success, checked.stdout + checked.stderr


def _key_properties(tmp_path: Path) -> List[Dict[str, Any]]:
    """The histogram properties the written bundle declares for the history's key."""
    found: List[Dict[str, Any]] = []
    for path in sorted((tmp_path / "bundle").glob("*.yml")):
        found += _histograms_of_the_key(yaml.safe_load(path.read_text(encoding="utf-8")))
    return found


def _histograms_of_the_key(node: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("property") == "marginalHistogram" and node.get("column") == "plan_id":
            found.append(node)
        for value in node.values():
            found += _histograms_of_the_key(value)
    elif isinstance(node, list):
        for value in node:
            found += _histograms_of_the_key(value)
    return found


def _declared_key(tmp_path: Path) -> Dict[str, Any]:
    """The key's fidelity declaration in the written bundle."""
    text = (tmp_path / "bundle" / "simulation.flux.yml").read_text(encoding="utf-8")
    found: Dict[str, Any] = yaml.safe_load(text)
    declared: Dict[str, Any] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            columns = node.get("columns")
            if isinstance(columns, dict) and "plan_id" in columns:
                declared.update(columns["plan_id"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(found)
    return declared


def test_a_filled_key_does_not_score_its_histogram(tmp_path: Path) -> None:
    result = _init(tmp_path, _binned([600, 1250]))
    assert _key_properties(tmp_path) == []
    # The layout still reads the key's bins, so the statistic stays used and nothing cites the
    # property that is not written.
    declared = _declared_key(tmp_path)
    assert declared["bins"] == EDGES
    assert len(declared["binWeights"]) == 2
    states = _states(result)
    assert states["/tables/plan_version/columns/plan_id/statistics/0"]["state"] == "used"
    said = " ".join(result.stdout.split()) + json.dumps(result.artifacts, default=str)
    assert "bin 0 takes every value it holds and still falls short of its weight" in said
    assert "so the histogram is not scored" in said


def test_a_filled_key_verifies_against_the_profile(tmp_path: Path) -> None:
    _init(tmp_path, _binned([600, 1250]))
    _verified_with_profile(tmp_path)


def test_a_key_whose_bins_fit_keeps_its_histogram_property(tmp_path: Path) -> None:
    # 185 of the 1850 versions fall in the first 100 ids: 100 of the 1000 entities, its width.
    _init(tmp_path, _binned([185, 1665]))
    (kept,) = _key_properties(tmp_path)
    assert kept["bins"] == EDGES
    _verified_with_profile(tmp_path)
