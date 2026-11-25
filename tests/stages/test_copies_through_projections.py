# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A copy through a relationship into an emit projected from a history is refused (ADR 0022 section 4).

``service_visit`` reaches a projection of the fleet history (``vehicle_current``, ``vehicle_change``
or ``vehicle_month``) and lists ``sharedColumns: [depot_id]``. A projected emit's foreign key is
the history's cell on the row it reads, not the pick of a draw of its own, so the depot a copy
would take from the pick is not the depot the projected row holds: the visit's depot would
differ from its vehicle's. The plan refuses it (``FLX-3403``), naming the relationship, before a
row exists. A periodic emit is no parent of a pick at all, so its copy is refused earlier, by that
rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.stages import pipeline

from . import _fleet

pytestmark = pytest.mark.integration

#: Each projection, with the column its rows are unique on (the target of a many_to_one).
PROJECTIONS = [
    (_fleet.SNAPSHOT, "vehicle_id"),
    (_fleet.EVENTS, "event_id"),
    (_fleet.PERIODIC, "period_id"),
]


def _depot_relationship() -> Dict[str, Any]:
    return {
        "name": "depot",
        "to": _fleet.DEPOT,
        "fromColumns": ["depot_id"],
        "toColumns": ["depot_id"],
        "cardinality": "many_to_one",
        "joinType": "inner",
    }


def _copy_bundle(tmp_path: Path, projection: str, key: str) -> Path:
    """A visit that copies ``depot_id`` through its relationship to *projection*, which holds it."""
    target = _fleet.bundle(tmp_path)
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    held = _fleet.expose(contract, projection)
    held["contract"]["schema"].append({"name": "depot_id", "type": "INT", "required": True})
    held["semantics"].setdefault("relationships", []).append(_depot_relationship())
    visit = _fleet.expose(contract, _fleet.VISIT)
    visit["contract"]["schema"].append({"name": "depot_id", "type": "INT", "required": True})
    relationships = visit["semantics"]["relationships"]
    visit["contract"]["schema"].append({"name": "ref_id", "type": "INT", "required": True})
    relationships[0].update({"to": projection, "fromColumns": ["ref_id"], "toColumns": [key]})
    relationships[0]["sharedColumns"] = ["depot_id"]
    relationships.append(_depot_relationship())
    _fleet.repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][_fleet.NAMESPACE]
    flux["copies"] = True
    columns = flux["emits"][projection]["fidelity"]["columns"]
    columns["depot_id"] = {"range": {"min": "1", "max": "50"}}
    emits = flux["emits"][_fleet.VISIT]["fidelity"]["columns"]
    emits["ref_id"] = {"range": {"min": "1", "max": "50"}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


@pytest.mark.parametrize(("projection", "key"), PROJECTIONS)
def test_a_copy_through_an_emit_projected_from_a_history_is_refused(
    tmp_path: Path, projection: str, key: str
) -> None:
    target = _copy_bundle(tmp_path, projection, key)
    with pytest.raises(FluxError) as caught:
        pipeline.execute(target, kernel=KERNEL_V2)
    text = str(caught.value)
    assert caught.value.code.code == "FLX-3403", text
    assert "relationship 'vehicle'" in text and projection in text, text
    if projection == _fleet.PERIODIC:
        # A periodic emit is no parent of a pick at all (its rows grow with the window): refused
        # before the copy is read, with its own words.
        return
    for word in ("copies 'depot_id'", "relationship 'depot'", "projected from a history"):
        assert word in text, text
