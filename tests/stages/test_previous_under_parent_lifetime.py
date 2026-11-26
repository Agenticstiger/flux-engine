# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A rule that reads ``previous`` runs on a history that keeps to its parent's lifetime (ADR 0017).

The ``history-fleet`` fixture gets a ``lifetimes`` entry on its depot and ``parent.lifetime: true``
on the history, as ``test_history_parent_run`` does, and a ``reason`` column on the history that a
rule derives from the version before it: ``first`` on an entity's first version and ``later`` on
every other. The plan is made, and every version holds the value the rule gives it, at one job and
at four.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.stages.verify import verify

from . import _fleet
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_history_parent_run import _bundle as parent_bundle

pytestmark = pytest.mark.integration

REASON = "CASE WHEN previous.valid_from IS NULL THEN 'first' ELSE 'later' END"


def previous_bundle(tmp_path: Path, *, bound: bool = True, rule: bool = True) -> Path:
    """The parent-lifetime fixture with ``reason`` derived from ``previous.model``.

    Without *bound* the history keeps no lifetime of its parent; without *rule* nothing reads
    ``previous``.
    """
    target = parent_bundle(tmp_path)
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    schema = _fleet.expose(contract, _fleet.HISTORY)["contract"]["schema"]
    schema.append({"name": "reason", "type": "STRING", "required": True})
    _fleet.repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][_fleet.NAMESPACE]
    if not bound:
        del flux["history"][0]["parent"]["lifetime"]
    if rule:
        flux["emits"][_fleet.HISTORY]["rules"] = [
            {
                "id": "reason_of_version",
                "language": "flux-expr/2",
                "derive": "reason",
                "expr": REASON,
            }
        ]
    else:
        flux["emits"][_fleet.HISTORY]["fidelity"]["columns"]["reason"] = {"levels": {"first": "1"}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def expected_reasons(rows: List[Dict[str, Any]]) -> List[str]:
    """The reason each version row holds: ``first`` unless a version of its entity was written before it."""
    return [
        "first" if position == 0 else "later"
        for versions in _fleet.by_vehicle(rows).values()
        for position, _ in enumerate(versions)
    ]


@pytest.mark.parametrize("bound", [True, False])
def test_a_rule_that_reads_previous_plans_with_or_without_the_parents_lifetime(
    tmp_path: Path, bound: bool
) -> None:
    state = tmp_path / "state"
    layout = _fleet.run_bundle(previous_bundle(tmp_path, bound=bound), state)
    rows = _fleet.rows(layout, _fleet.HISTORY)
    ordered = [row for versions in _fleet.by_vehicle(rows).values() for row in versions]
    assert [row["reason"] for row in ordered] == expected_reasons(rows)
    assert {"first", "later"} == {row["reason"] for row in rows}


def test_every_version_of_a_bound_history_holds_the_rule_in_verify(tmp_path: Path) -> None:
    state = tmp_path / "state"
    layout = _fleet.run_bundle(previous_bundle(tmp_path), state)
    verified = verify(tmp_path / "bundle", run_id=layout.run_id, state_dir=state)
    assert verified.success, verified.stderr
    lines = {line["rule"]: line for line in verified.artifacts["rules"]["emits"][_fleet.HISTORY]}
    held = lines["reason_of_version"]
    assert held["total"] > 0 and held["satisfied"] == held["total"], held
    assert lines["history:within_parent"]["met"]


def test_a_bound_history_with_a_previous_rule_is_the_same_run_at_one_and_four_jobs_and_in_shares(
    tmp_path: Path,
) -> None:
    target = previous_bundle(tmp_path)
    assert_same_run_at_one_and_four_jobs(target)
    assert_shares_join_to_the_run(target, tmp_path, _fleet.run_bundle, 3, [_fleet.HISTORY])


def test_a_bound_history_without_the_rule_plans(tmp_path: Path) -> None:
    layout = _fleet.run_bundle(previous_bundle(tmp_path, rule=False), tmp_path / "state")
    assert _fleet.rows(layout, _fleet.HISTORY)
