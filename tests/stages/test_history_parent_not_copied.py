# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A history's parent relationship may not be the target of a foreign-key copy (ADR 0022 point 4).

The history lays its entities out under the parent rows of that relationship, so the pick is set
by the layout and a copy that also sets it would give the foreign key two owners. The fleet
fixture gets a ``garage`` table that holds a depot of its own; the vehicle history names a garage
and lists ``depot_id`` as a column shared with it, so the depot of a version would be its
garage's. ``plan`` refuses that, naming the relationship, with ``FLX-3403``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from ._fleet import CONTRACT, DEPOT, HISTORY, NAMESPACE, expose, plan_refused, repin
from ._fleet import bundle as fleet_bundle

pytestmark = pytest.mark.integration

GARAGE = "garage"


def _bundle(tmp_path: Path) -> Path:
    target = fleet_bundle(tmp_path)
    contract: Dict[str, Any] = yaml.safe_load((target / CONTRACT).read_text("utf-8"))
    depot = expose(contract, DEPOT)
    garage = yaml.safe_load(yaml.safe_dump(depot))
    garage["exposeId"] = GARAGE
    garage["binding"]["location"]["table"] = GARAGE
    garage["contract"]["schema"] = [
        {"name": "garage_id", "type": "INT", "required": True},
        {"name": "depot_id", "type": "INT", "required": True},
    ]
    garage["semantics"] = {
        "entities": [{"name": GARAGE, "type": "primary", "expr": "garage_id"}],
        "relationships": [
            {
                "name": "depot",
                "to": DEPOT,
                "fromColumns": ["depot_id"],
                "toColumns": ["depot_id"],
                "cardinality": "many_to_one",
                "joinType": "inner",
            }
        ],
    }
    contract["exposes"].insert(1, garage)
    history = expose(contract, HISTORY)
    history["contract"]["schema"].append({"name": "garage_id", "type": "INT", "required": True})
    history["semantics"]["relationships"].append(
        {
            "name": GARAGE,
            "to": GARAGE,
            "fromColumns": ["garage_id"],
            "toColumns": ["garage_id"],
            "cardinality": "many_to_one",
            "joinType": "inner",
            "sharedColumns": ["depot_id"],
        }
    )
    repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][NAMESPACE]
    flux["copies"] = True
    flux["emits"][GARAGE] = yaml.safe_load(yaml.safe_dump(flux["emits"][DEPOT]))
    flux["emits"][GARAGE]["rows"] = 30
    flux["emits"][GARAGE]["fidelity"]["columns"] = {
        "garage_id": {"range": {"min": "1", "max": "30"}},
        "depot_id": {"range": {"min": "1", "max": "50"}},
    }
    flux["emits"][GARAGE]["fidelity"]["properties"][0]["column"] = "garage_id"
    flux["emits"][HISTORY]["fidelity"]["columns"]["garage_id"] = {
        "range": {"min": "1", "max": "30"}
    }
    emits = document["spec"]["emits"]
    emits.append({**emits[0], "exposeId": GARAGE})
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def test_a_copy_into_the_relationship_a_history_lays_out_under_is_refused(tmp_path: Path) -> None:
    planned = plan_refused(_bundle(tmp_path), tmp_path / "state")
    assert "FLX-3403" in planned.stderr, planned.stderr
    assert "it is the history's parent" in planned.stderr, planned.stderr
