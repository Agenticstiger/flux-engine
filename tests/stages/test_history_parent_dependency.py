# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A dependency beside a history parent holds by construction (ADR 0016 section 1, ADR 0017).

``tests/fixtures/history-fleet`` is copied with a contract ``dependency`` on its history
``vehicle_version``: from the natural entity ``vehicle_id`` to the foreign key ``depot_id`` of the
history's parent relationship. The parent layout gives every version of an entity one depot, so
the dependency is already true. What is pinned:

* **It plans.** The dependency is accepted, and ``verify`` scores it at 100 %.
* **It draws nothing.** Every emit's bytes equal the bundle without the dependency, in one job
  and in four.
* **A weight is refused first.** A ``links`` weight on the history's parent relationship is refused
  (``FLX-3403``, a second way to pick the parent) with or without the dependency, so a weighted
  relationship never meets a dependency the layout holds.
* **The rest stays refused.** A dependency whose ``from`` lacks the natural entity column, or
  whose ``to`` adds a foreign key the layout does not set, is ``FLX-3403`` as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.verify import verify

from ._fleet import (
    CONTRACT,
    HISTORY,
    NAMESPACE,
    bundle,
    expose,
    plan_refused,
    repin,
    run_bundle,
)

pytestmark = pytest.mark.integration

#: A second foreign key to the depot, which the parent layout does not set.
HOME = "home_depot_id"


def _bundle(
    tmp_path: Path,
    name: str = "bundle",
    *,
    dependency: Optional[Dict[str, Any]] = None,
    home: bool = False,
    **sizes: Any,
) -> Path:
    """The fixture with a ``dependency`` on its history; ``home`` adds a second depot key."""
    target = bundle(tmp_path, name=name, **sizes)
    contract = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    item = expose(contract, HISTORY)
    if home:
        item["contract"]["schema"].append({"name": HOME, "type": "INT", "required": True})
        item["semantics"]["relationships"].append(
            {
                "name": "home_depot",
                "to": "depot",
                "fromColumns": [HOME],
                "toColumns": ["depot_id"],
                "cardinality": "many_to_one",
                "joinType": "inner",
            }
        )
        path = target / "simulation.flux.yml"
        document = yaml.safe_load(path.read_text("utf-8"))
        columns = document["extensions"][NAMESPACE]["emits"][HISTORY]["fidelity"]["columns"]
        columns[HOME] = {"range": {"min": "1", "max": "50"}}
        path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    if dependency is not None:
        item["contract"]["invariants"] = [{"id": "depot_of_vehicle", "dependency": dependency}]
    repin(target, contract)
    return target


def _lines(verified: Any) -> List[str]:
    return (verified.stdout + verified.stderr).splitlines()


DEPENDENCY = {"from": ["vehicle_id"], "to": ["depot_id"]}


def test_the_dependency_plans_and_verify_scores_it_in_full(tmp_path: Path) -> None:
    target = _bundle(tmp_path, dependency=DEPENDENCY)
    layout = run_bundle(target, tmp_path / "state")
    verified = verify(target, run_id=layout.run_id, state_dir=tmp_path / "state")
    assert verified.success, verified.stderr
    line = next(line for line in _lines(verified) if "depot_of_vehicle" in line)
    assert "depot_of_vehicle 200 of 200 (100.00%)" in line, line


@pytest.mark.parametrize("jobs", [1, 4])
def test_the_dependency_draws_nothing(tmp_path: Path, jobs: int) -> None:
    """On the plain bundle's draws, every emit's bytes are the plain bundle's, under any jobs."""
    _same_bytes(tmp_path, jobs)


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_the_dependency_draws_nothing_when_the_emit_is_cut_across_processes(
    tmp_path: Path,
) -> None:
    _same_bytes(
        tmp_path, 4, versions=3 * parallel.MIN_ROWS_PER_JOB + 11, entities=2500, depots=1200
    )


def _same_bytes(tmp_path: Path, jobs: int, **sizes: Any) -> None:
    plain = _bundle(tmp_path, "plain", **sizes)
    ruled = _bundle(tmp_path, "ruled", dependency=DEPENDENCY, **sizes)
    expected = pipeline.execute(plain, jobs=1, kernel=KERNEL_V2)
    got = pipeline.execute(ruled, jobs=jobs, kernel=KERNEL_V2, common_draws_of=plain)
    assert [e.resolved.expose_id for e in got.emits] == [
        e.resolved.expose_id for e in expected.emits
    ]
    for mine, theirs in zip(got.emits, expected.emits, strict=True):
        assert mine.writer.lines() == theirs.writer.lines()


@pytest.mark.parametrize(
    "dependency, home",
    [
        ({"from": ["model"], "to": ["depot_id"]}, False),
        ({"from": ["vehicle_id"], "to": ["depot_id", HOME]}, True),
    ],
)
def test_any_other_dependency_on_the_counted_relationship_stays_refused(
    tmp_path: Path, dependency: Dict[str, Any], home: bool
) -> None:
    target = _bundle(tmp_path, dependency=dependency, home=home)
    planned = plan_refused(target, tmp_path / "state")
    assert "FLX-3403" in planned.stderr, planned.stderr
    assert "a dependency sets its columns" in planned.stderr, planned.stderr


@pytest.mark.parametrize("dependency", [DEPENDENCY, None])
def test_a_weight_on_the_parent_relationship_is_refused_with_or_without_the_dependency(
    tmp_path: Path, dependency: Optional[Dict[str, Any]]
) -> None:
    target = _bundle(tmp_path, dependency=dependency)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    document["extensions"][NAMESPACE]["links"] = [
        {"emit": HISTORY, "relationship": "depot", "weight": "depot_id"}
    ]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    planned = plan_refused(target, tmp_path / "state")
    assert "FLX-3403" in planned.stderr and "pick the parent a second way" in planned.stderr
