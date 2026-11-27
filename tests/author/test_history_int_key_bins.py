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


def _binned(counts: Any, mean: Any = None) -> Dict[str, Any]:
    """The history profile, its natural key weighed by the versions each bin holds.

    With *mean*, the key also publishes its mean over the versions, as a profile does.
    """
    profile = _profile()
    column = profile["tables"]["plan_version"]["columns"]["plan_id"]
    column["statistics"] = [_bins(EDGES, counts, 1850)]
    if mean is not None:
        column["statistics"].append(
            {
                "definition": "mean/1",
                "sampleSize": {"min": 1850, "max": 1850},
                "disclosure": "acro-default",
                "groups": [{"group": "", "k": 1850, "Sx": str(mean * 1850 * 2**16), "status": "F"}],
            }
        )
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


def _key_properties(tmp_path: Path, kind: str = "marginalHistogram") -> List[Dict[str, Any]]:
    """The properties of *kind* the written bundle declares for the history's key."""
    found: List[Dict[str, Any]] = []
    for path in sorted((tmp_path / "bundle").glob("*.yml")):
        found += _properties_of_the_key(yaml.safe_load(path.read_text(encoding="utf-8")), kind)
    return found


def _properties_of_the_key(node: Any, kind: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("property") == kind and node.get("column") == "plan_id":
            found.append(node)
        for value in node.values():
            found += _properties_of_the_key(value, kind)
    elif isinstance(node, list):
        for value in node:
            found += _properties_of_the_key(value, kind)
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
    assert "so neither the key's histogram nor its mean is scored" in said


def test_a_filled_key_verifies_against_the_profile(tmp_path: Path) -> None:
    _init(tmp_path, _binned([600, 1250]))
    applied_and_verified(tmp_path, scored=True)


def test_a_key_whose_bins_fit_keeps_its_histogram_property(tmp_path: Path) -> None:
    # 185 of the 1850 versions fall in the first 100 ids: 100 of the 1000 entities, its width.
    _init(tmp_path, _binned([185, 1665]))
    (kept,) = _key_properties(tmp_path)
    assert kept["bins"] == EDGES
    applied_and_verified(tmp_path, scored=True)


def test_a_filled_key_does_not_score_its_mean_over_the_versions(tmp_path: Path) -> None:
    # The mean the histogram implies over the versions, about 388: a third of them hold the first
    # 100 ids. The filled bin holds a tenth of the generated rows, so their mean is near 500.
    result = _init(tmp_path, _binned([600, 1250], mean=388))
    assert _key_properties(tmp_path, "mean") == []
    # The mean is still read: the key's histogram is checked against it.
    entry = _states(result)["/tables/plan_version/columns/plan_id/statistics/1"]
    assert entry["state"] == "used"
    assert "checked against the histogram" in entry["note"]
    applied_and_verified(tmp_path, scored=True)


def test_a_key_whose_bins_fit_keeps_its_mean_property(tmp_path: Path) -> None:
    _init(tmp_path, _binned([185, 1665], mean=500))
    (kept,) = _key_properties(tmp_path, "mean")
    assert kept["column"] == "plan_id"
    applied_and_verified(tmp_path, scored=True)
