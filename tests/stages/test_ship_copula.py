# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A bundle with ``fidelity.copula`` goes through ``plan``, ``apply`` and ``ship`` (ADR 0035 decision 4).

The key lives in the fidelity block, so ``G`` of the plan and the ``G`` that ``ship`` recompiles
are one function of it: a mismatch would be ``FLX-8001`` (``contract.plannedG``). The runs use
``tests/fixtures/copula`` and the committed slice is the run's own rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from flux_engine.stages.apply import apply
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import plan
from flux_engine.stages.ship import ship
from tests.seam.test_copula_block import COPULA, bundle_with

pytestmark = pytest.mark.integration

_KEY = bytes(range(64, 96))


def _slice_of(layout: RunLayout, expose: str, target: Path) -> str:
    columns = json.loads(layout.seam_attestation_path(expose).read_text("utf-8"))["columns"]
    rows = [
        dict(zip(columns, json.loads(line), strict=True))
        for line in layout.emit_path(expose).read_text("utf-8").splitlines()
        if line
    ]
    target.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return target.as_uri()


def _shipped(bundle: Path, tmp_path: Path) -> Any:
    state = tmp_path / ".flux"
    applied = apply(bundle, state_dir=state)
    assert applied.success, applied.stderr
    run_id = str(applied.artifacts["run_id"])
    layout = RunLayout.for_state_dir(state, run_id)
    result = ship(
        bundle,
        run_id=run_id,
        out_dir=tmp_path / "dist",
        state_dir=state,
        committed_slice={"accounts": _slice_of(layout, "accounts", tmp_path / "slice.ndjson")},
        signing_key=_KEY,
    )
    assert result.success, result.stderr
    return layout, result


def test_a_copula_bundle_ships_and_writes_the_evidence(tmp_path: Path) -> None:
    layout, _ = _shipped(bundle_with(tmp_path, COPULA), tmp_path)
    assert (layout.run_dir / "EVIDENCE.md").is_file()


def test_the_plan_records_the_same_g_that_ship_recompiles(tmp_path: Path) -> None:
    state = tmp_path / ".flux"
    planned = plan(bundle_with(tmp_path, COPULA), state_dir=state)
    assert planned.success, planned.stderr
    layout, _ = _shipped(bundle_with(tmp_path, COPULA, "again"), tmp_path / "second")
    recorded = json.loads(layout.plan_path.read_text("utf-8"))["emits"]["accounts"]
    assert recorded["G"] == recorded["fidelity"]["G"]
