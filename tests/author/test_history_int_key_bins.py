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

from pathlib import Path
from typing import Any, Dict

import pytest

from ._written import applied_and_verified, emitted_rows
from .test_history_init import HISTORY, _bins, _entries, _init, _profile, _states

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
