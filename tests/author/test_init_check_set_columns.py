# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``init``'s pre-write check plans each emit as ``plan`` does, so it accepts what ``plan`` accepts.

``plan`` leaves the contract's date range off a column that a history or a projection sets, and off
every column of a snapshot, because the history writes those cells. The check ``init`` runs on the
bundle it is about to write built each emit's plan without that set, so a range that misses the
column's own statistics (which that column is not drawn from) was refused by ``init`` and accepted
by ``plan``. Pinned on the fleet (a history of vehicles, their snapshot and their change events),
with a range in 2024 on a column whose statistics lie in the first half of 2023:

* a history's validity start, a snapshot's column and an events entry's date pass the check;
* a column nothing else sets is still refused, with ``plan``'s code.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest
import yaml

from flux_engine.author.bundle import AuthorError, _check
from flux_engine.starter import StarterFile
from tests.stages._fleet import (
    EVENTS,
    HISTORY,
    NAMESPACE,
    SNAPSHOT,
    VISIT,
    bundle,
    plan_outcome,
    repin,
    with_range,
)

pytestmark = pytest.mark.integration

RANGE = "between '2024-01-01' and '2025-01-01'"
EARLY = {"range": {"min": "2023-01-01", "max": "2023-06-30"}}


def _missing_its_statistics(tmp_path: Path, expose_id: str, column: str) -> Path:
    """The fleet with *RANGE* on *column* of *expose_id*, whose own statistics lie in early 2023."""
    target = with_range(bundle(tmp_path), expose_id, column, RANGE)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["emits"][expose_id]["fidelity"]["columns"][column] = EARLY
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    repin(target, yaml.safe_load((target / "vehicles.fluid.yml").read_text("utf-8")))
    return target


def _files(target: Path) -> List[StarterFile]:
    return [StarterFile(path.name, "test", path.read_bytes()) for path in sorted(target.iterdir())]


@pytest.mark.parametrize(
    ("expose_id", "column"),
    [(HISTORY, "valid_from"), (SNAPSHOT, "commissioned_on"), (EVENTS, "recorded_at")],
)
def test_a_range_on_a_column_something_else_sets_is_accepted_as_plan_accepts_it(
    tmp_path: Path, expose_id: str, column: str
) -> None:
    target = _missing_its_statistics(tmp_path, expose_id, column)
    assert plan_outcome(target, tmp_path / "state").success
    _check(_files(target), {})


def test_a_range_on_a_column_nothing_else_sets_is_still_refused(tmp_path: Path) -> None:
    target = _missing_its_statistics(tmp_path, VISIT, "visited_on")
    with pytest.raises(AuthorError, match="FLX-3403"):
        _check(_files(target), {})
    assert not plan_outcome(target, tmp_path / "state").success
