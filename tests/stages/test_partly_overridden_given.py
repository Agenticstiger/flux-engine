# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A column some kinds of a periods entry override, drawn ``given`` a cell the entry sets (ADR 0027).

``vehicle_month`` of ``tests/fixtures/history-fleet`` gains kinds. On the rows of a kind that does
not override ``band``, ``band`` is drawn given the level the row holds for its parent: the
parent's override on those rows, or the version's value. A new plan holds that level and the rows
follow it; a plan recorded by engine 0.12.0 replays the draw it was made with.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from flux_engine.stages import pipeline
from flux_engine.stages.replay import replay

from . import _fleet
from .test_declared_range_replay import run_as_before

pytestmark = pytest.mark.integration

#: ``band`` is ``x`` on a fuel row and ``y`` on a toll row.
BAND_OF = {"fuel": "x", "toll": "y"}
BY_CHARGE = {"fuel": {"x": "1", "y": "0"}, "toll": {"x": "0", "y": "1"}}
#: ``band`` is ``x`` on a sedan's or a van's row and ``y`` on a truck's.
BAND_OF_MODEL = {"sedan": "x", "van": "x", "truck": "y"}
BY_MODEL = {
    model: {"x": "1", "y": "0"} if band == "x" else {"x": "0", "y": "1"}
    for model, band in BAND_OF_MODEL.items()
}
#: The first kind overrides ``band``; the second draws it, on a row whose toll the entry sets.
OTHER = [
    {"count": 1, "overrides": {"charge": "fuel", "band": "y"}},
    {"count": 1, "overrides": {"charge": "toll"}},
]
#: The first kind overrides ``band`` and nothing else; the second draws every cell.
OWN = [{"count": 1, "overrides": {"band": "y"}}, {"count": 2}]
#: The first kind overrides ``band`` and ``charge``; the second draws both.
TOGETHER = [{"count": 1, "overrides": {"charge": "fuel", "band": "y"}}, {"count": 2}]


def _bundle(
    tmp_path: Path, kinds: List[Dict[str, Any]], parent: str = "charge", tier: bool = False
) -> Path:
    """The fleet bundle with *kinds* on ``vehicle_month``, whose ``band`` is drawn given *parent*.

    With *tier*, ``tier`` is drawn given ``model`` too, so ``model`` is held for it.
    """
    target = _fleet.bundle(tmp_path)
    contract = yaml.safe_load((target / _fleet.CONTRACT).read_text("utf-8"))
    schema = _fleet.expose(contract, _fleet.PERIODIC)["contract"]["schema"]
    added = ["charge", "band", *(["tier"] if tier else [])]
    schema.extend({"name": column, "type": "STRING", "required": True} for column in added)
    _fleet.repin(target, contract)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][_fleet.NAMESPACE]
    flux["periods"][0]["kinds"] = kinds
    declared = flux["emits"][_fleet.PERIODIC]["fidelity"]["columns"]
    declared["charge"] = {"levels": {"fuel": "0.6", "toll": "0.4"}}
    table = BY_CHARGE if parent == "charge" else BY_MODEL
    declared["band"] = {"given": parent, "levelWeightsGiven": table}
    if tier:
        declared["tier"] = {"given": "model", "levelWeightsGiven": BY_MODEL}
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _rows(target: Path, tmp_path: Path) -> List[Dict[str, Any]]:
    return _fleet.rows(_fleet.run_bundle(target, tmp_path / "state"), _fleet.PERIODIC)


@pytest.mark.parametrize(
    ("kinds", "parent", "tier", "band_of"),
    [
        (OWN, "model", True, BAND_OF_MODEL),
        (OWN, "model", False, BAND_OF_MODEL),
        (TOGETHER, "charge", False, BAND_OF),
    ],
    ids=["held", "version", "set-where-overridden"],
)
def test_a_column_some_kinds_override_is_planned_and_its_draw_reads_the_row(
    tmp_path: Path,
    kinds: List[Dict[str, Any]],
    parent: str,
    tier: bool,
    band_of: Dict[str, str],
) -> None:
    rows = _rows(_bundle(tmp_path, kinds, parent, tier), tmp_path)
    # A row holds the first kind's ``y``, or the ``band`` its parent's cell gives it.
    assert all(row["band"] in ("y", band_of[row[parent]]) for row in rows)
    assert any(row["band"] == "x" for row in rows)


def test_a_column_some_kinds_override_follows_the_override_of_its_parent(tmp_path: Path) -> None:
    # The first kind sets ``band`` to ``y``, and the second's rows hold a toll, whose ``band`` is
    # ``y``: no row holds ``x``, as one drawn given the level of ``charge``'s own draw would.
    rows = _rows(_bundle(tmp_path, OTHER), tmp_path)
    assert {row["band"] for row in rows} == {"y"}


def test_a_plan_recorded_before_overrides_were_held_replays_its_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _bundle(tmp_path, OTHER)
    state = tmp_path / "state"
    # As engine 0.12.0 planned it: ``band`` drawn given its parent's own draw.
    era = pipeline.PlanEra(override_givens=False)
    layout, digest = run_as_before(target, state, monkeypatch, "_planned_with_projections", era=era)
    recorded = layout.emit_path(_fleet.PERIODIC).read_bytes()
    # The rows engine 0.12.0 drew: a toll row's ``band`` takes ``x`` too, which the table gives
    # only a fuel row, since it was drawn given the fuel or toll of its parent's own draw.
    tolls = [row for row in _fleet.rows(layout, _fleet.PERIODIC) if row["charge"] == "toll"]
    assert {row["band"] for row in tolls} == {"x", "y"}
    for jobs in (1, 4):
        replayed = replay(
            target, run_id=layout.run_id, assert_digest=digest, state_dir=state, jobs=jobs
        )
        assert replayed.success, replayed.stderr
        assert layout.emit_path(_fleet.PERIODIC).read_bytes() == recorded
