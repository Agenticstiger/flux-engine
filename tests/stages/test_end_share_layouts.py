# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Two recorded plans of one bundle that differ only in what an ``end`` share is a share of.

A plan made before the share moved to every entity carries no ``historyEndShare``; a new plan does.
Their histories have the same digest, and the entities that end differ, so the periods and the
events of the two differ too. A layout a process keeps for one must not serve the other.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import events, periods
from flux_engine.seam.history import END_SHARE_OF_ELIGIBLE
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout

from ._fleet import EVENTS, PERIODIC, apply_module, bundle, plan_module, rows

pytestmark = pytest.mark.integration

#: An end that closes a share of the entities with a ``status`` the draw can give, so that the
#: entities that can end are fewer than the entities.
END = {"mode": "closeLast", "share": "0.9", "column": "status", "value": "retired"}


def _plan_both(
    target: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[RunLayout, RunLayout]:
    """Two recorded plans of one bundle: one with ``historyEndShare`` and one made without."""
    current = plan_module.plan(target, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    assert current.success, current.stderr
    execute = pipeline.execute

    def earlier(*args: Any, **options: Any) -> Any:
        era = replace(options.get("era", pipeline.PlanEra()), end_share=END_SHARE_OF_ELIGIBLE)
        return execute(*args, **{**options, "era": era})

    with monkeypatch.context() as patched:
        patched.setattr(pipeline, "execute", earlier)
        before = plan_module.plan(target, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    assert before.success, before.stderr
    return (
        RunLayout.for_state_dir(state, current.artifacts["run_id"]),
        RunLayout.for_state_dir(state, before.artifacts["run_id"]),
    )


def test_two_recorded_plans_that_end_differently_keep_their_bytes_when_runs_share_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = bundle(tmp_path, end=END)
    state = tmp_path / "state"
    current, before = _plan_both(target, state, monkeypatch)
    assert "historyEndShare" in json.loads(current.plan_path.read_text("utf-8"))
    assert "historyEndShare" not in json.loads(before.plan_path.read_text("utf-8"))

    def run(layout: RunLayout) -> Dict[str, List[Dict[str, Any]]]:
        applied = apply_module.apply(target, run_id=layout.run_id, state_dir=state)
        assert applied.success, applied.stderr
        return {expose_id: rows(layout, expose_id) for expose_id in (PERIODIC, EVENTS)}

    alone = {"current": run(current), "before": run(before)}
    for expose_id in (PERIODIC, EVENTS):
        assert alone["current"][expose_id] != alone["before"][expose_id], expose_id
    # Two runs that overlap share the process's layouts: the reset between runs is held back.
    monkeypatch.setattr(periods, "clear_layouts", lambda: None)
    monkeypatch.setattr(events, "clear_layouts", lambda: None)
    periods._LAYOUTS.clear()
    events._LAYOUTS.clear()
    together = {"current": run(current), "before": run(before)}
    assert together == alone
