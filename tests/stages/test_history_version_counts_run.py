# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An entity takes a version count its room fits (ADR 0017, the amendment of 2026-11-28).

A run of ``tests/fixtures/history-chain`` and ``tests/fixtures/history-fleet`` through ``plan`` and
``apply``. What is pinned:

* **The era.** A new plan of a history with a ``versions`` shape records ``versionCounts: "fit"``
  and no other plan does. A recorded plan without it replays the rank coupling, bytes and run id,
  and a value this engine does not know is refused (``FLX-2102``).
* **The cells at an address.** A plan of each era draws the same values at one (entity, version)
  address wherever both have that version: the first start, the lengths, the tracked moves and the
  keys. Only what the count itself decides, the last version's end, may differ.
* **The caches.** Two plans of one bundle, one of each era, run in one process whose memos are
  never cleared, give each the bytes it gives alone.
* **Sharing.** A run with workers works the layout out once, in the process that starts them.
* **The kernel.** Kernel 0.1.0 refuses a history, so no plan of it records the marker.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import events, history, periods, projections, versionview
from flux_engine.generator import rows as generator_rows
from flux_engine.seam.history import VERSION_COUNTS_FIT, VERSION_COUNTS_RANK
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout

from ._fleet import EVENTS, HISTORY, PERIODIC, by_vehicle, rows
from ._fleet import bundle as fleet_bundle
from .test_history_end_share_replay import RECORDED, RECORDED_RUN_DIGEST, RECORDED_RUN_ID

apply_module = import_module("flux_engine.stages.apply")
plan_module = import_module("flux_engine.stages.plan")

pytestmark = pytest.mark.integration

MARKER = "versionCounts"
#: Lengths of 1 to 9 days: a chain of nine versions fits any room the fixture leaves.
SHORT = {"bins": ["1", "5", "10"], "binWeights": ["0.5", "0.5"], "closed": "lower"}
#: The model of a vehicle changes between versions, as the fixture's three levels allow.
TRACKED = {
    "tracked": [
        {
            "column": "model",
            "transitions": {
                "sedan": {"van": "0.5", "truck": "0.5"},
                "van": {"sedan": "1"},
                "truck": {"sedan": "0.5", "van": "0.5"},
            },
        }
    ]
}


def _bundle(tmp_path: Path, **options: Any) -> Path:
    return fleet_bundle(tmp_path, **options)


def _plan_as(
    target: Path, state: Path, monkeypatch: pytest.MonkeyPatch, version_counts: str
) -> RunLayout:
    """Plan *target* as an engine of the era *version_counts* names would, and apply the plan."""
    execute = pipeline.execute

    def era(*args: Any, **options: Any) -> Any:
        held = options.get("era", pipeline.PlanEra())
        return execute(*args, **{**options, "era": replace(held, version_counts=version_counts)})

    with monkeypatch.context() as patched:
        patched.setattr(pipeline, "execute", era)
        planned = plan_module.plan(target, state_dir=state, kernel=KERNEL_V2.version, stage=False)
    assert planned.success, planned.stderr
    layout = RunLayout.for_state_dir(state, planned.artifacts["run_id"])
    applied = apply_module.apply(target, run_id=layout.run_id, state_dir=state)
    assert applied.success, applied.stderr
    return layout


def _document(layout: RunLayout) -> Dict[str, Any]:
    found = json.loads(layout.plan_path.read_text("utf-8"))
    assert isinstance(found, dict)
    return found


def test_a_new_plan_of_a_shaped_history_records_the_fit(tmp_path: Path) -> None:
    target = _bundle(tmp_path)
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr
    document = _document(RunLayout.for_state_dir(tmp_path / "state", planned.artifacts["run_id"]))
    assert document[MARKER] == VERSION_COUNTS_FIT == "fit"


def test_a_plan_of_a_history_without_a_shape_records_nothing(tmp_path: Path) -> None:
    target = _bundle(tmp_path)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    del document["extensions"]["com.agentics.flux/1"]["history"][0]["versions"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    planned = plan_module.plan(
        target, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert planned.success, planned.stderr
    assert MARKER not in _document(
        RunLayout.for_state_dir(tmp_path / "state", planned.artifacts["run_id"])
    )


def test_a_bundle_with_no_history_keeps_its_plan_without_the_marker(tmp_path: Path) -> None:
    planned = plan_module.plan(
        Path(__file__).resolve().parents[1] / "fixtures" / "versioned",
        state_dir=tmp_path / "state",
        kernel=KERNEL_V2.version,
        stage=False,
    )
    assert planned.success, planned.stderr
    assert MARKER not in _document(
        RunLayout.for_state_dir(tmp_path / "state", planned.artifacts["run_id"])
    )


def test_a_marker_this_engine_does_not_know_is_refused() -> None:
    assert apply_module.plan_version_counts({}) == VERSION_COUNTS_RANK
    assert apply_module.plan_version_counts({MARKER: "fit"}) == VERSION_COUNTS_FIT
    with pytest.raises(FluxError) as caught:
        apply_module.plan_version_counts({MARKER: "tomorrow"})
    assert caught.value.code.code == "FLX-2102" and MARKER in str(caught.value)


def test_a_plan_recorded_before_the_fit_replays_its_bytes_and_its_run_id() -> None:
    state = RECORDED / "state"
    document = json.loads((state / "runs" / RECORDED_RUN_ID / "plan.json").read_text("utf-8"))
    assert MARKER not in document
    assert apply_module.plan_version_counts(document) == VERSION_COUNTS_RANK
    era = apply_module._recorded_era(document)
    assert era.version_counts == VERSION_COUNTS_RANK
    # Its run digest is the one an engine without the fit wrote; the replay test beside it
    # (``test_history_end_share_replay``) runs the plan and compares that digest.
    assert RECORDED_RUN_DIGEST.startswith("sha256:")


def test_a_recorded_plan_with_the_marker_replays_the_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _bundle(tmp_path)
    fitted = _plan_as(target, tmp_path / "fit", monkeypatch, VERSION_COUNTS_FIT)
    ranked = _plan_as(target, tmp_path / "rank", monkeypatch, VERSION_COUNTS_RANK)
    assert MARKER in _document(fitted) and MARKER not in _document(ranked)
    assert fitted.run_id != ranked.run_id
    again = apply_module.apply(target, run_id=fitted.run_id, state_dir=tmp_path / "fit")
    assert again.success, again.stderr
    first = fitted.emit_path(HISTORY).read_bytes()
    assert first != ranked.emit_path(HISTORY).read_bytes()
    assert sorted(len(v) for v in by_vehicle(rows(fitted, HISTORY)).values()) == sorted(
        len(v) for v in by_vehicle(rows(ranked, HISTORY)).values()
    )


def test_each_era_draws_the_same_cells_at_an_address_it_shares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the count does not decide: the first start, the lengths, the tracked moves, the keys.

    The chains here are short against the room, so neither era compresses one. A column drawn
    from the row's index (a plain column) moves with the row, and every other row moves with the
    counts, so only what a path of the entity and the version addresses is compared. The last
    version's end and its closing are the count's.
    """
    target = _bundle(tmp_path, history={"length": SHORT, **TRACKED})
    fitted = _plan_as(target, tmp_path / "fit", monkeypatch, VERSION_COUNTS_FIT)
    ranked = _plan_as(target, tmp_path / "rank", monkeypatch, VERSION_COUNTS_RANK)
    by_era = [by_vehicle(rows(layout, HISTORY)) for layout in (fitted, ranked)]
    assert by_era[0].keys() == by_era[1].keys()
    moved = shared = 0
    for entity, chain in by_era[0].items():
        other = by_era[1][entity]
        moved += len(chain) != len(other)
        for version in range(min(len(chain), len(other))):
            mine, theirs = chain[version], other[version]
            last = version in (len(chain) - 1, len(other) - 1)
            for column in ("depot_id", "valid_from", "model"):
                assert mine[column] == theirs[column], (entity, version, column)
            if not last:
                assert mine["valid_to"] == theirs["valid_to"], (entity, version)
            shared += 1
    assert moved > 0 and shared > 0


def _every_memo() -> None:
    """What the end of a run clears, and the caches of the layouts that it leaves."""
    generator_rows.clear_memos()
    kernel2.clear_memos()
    history._PLANS.clear()
    history._OUTERS.clear()
    history._HANDED.clear()
    events._LAYOUTS.clear()
    periods._LAYOUTS.clear()
    projections.clear_sources()
    versionview._VIEWS.clear()


def test_two_plans_of_one_bundle_in_one_process_keep_their_own_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The layout and what reads it (events, periods) are held per era, however long the process lives."""
    target = _bundle(tmp_path)
    emits = (HISTORY, EVENTS, PERIODIC)
    counter = iter(range(100))

    def made(version_counts: str) -> Dict[str, bytes]:
        layout = _plan_as(target, tmp_path / f"state-{next(counter)}", monkeypatch, version_counts)
        return {emit: layout.emit_path(emit).read_bytes() for emit in emits}

    _every_memo()
    alone_fit = made(VERSION_COUNTS_FIT)
    _every_memo()
    alone_rank = made(VERSION_COUNTS_RANK)
    assert alone_fit[HISTORY] != alone_rank[HISTORY]
    assert alone_fit[EVENTS] != alone_rank[EVENTS]

    # The end-of-run reset held back: nothing a run remembered is forgotten before the next.
    monkeypatch.setattr(pipeline, "_memos_for_one_run", lambda: nullcontext())
    _every_memo()
    for order in (
        (VERSION_COUNTS_FIT, VERSION_COUNTS_RANK, VERSION_COUNTS_FIT),
        (VERSION_COUNTS_RANK, VERSION_COUNTS_FIT, VERSION_COUNTS_RANK),
    ):
        _every_memo()
        for version_counts in order:
            expected = alone_fit if version_counts == VERSION_COUNTS_FIT else alone_rank
            assert made(version_counts) == expected, version_counts
    assert history._PLANS, "the layouts were kept between the runs"


def test_a_bound_history_keeps_its_parents_limits(tmp_path: Path) -> None:
    """Under ``parent.lifetime`` no chain of a plan runs past the lifetime of its depot."""
    from ._fleet import run_bundle
    from .test_history_parent_run import _bundle as parent_bound
    from .test_history_parent_run import _check_bound

    layout = run_bundle(parent_bound(tmp_path), tmp_path / "state")
    assert _document(layout)[MARKER] == VERSION_COUNTS_FIT
    assert _check_bound(layout, None) > 0


def test_kernel_010_refuses_a_history_so_no_plan_of_it_records_the_fit(tmp_path: Path) -> None:
    target = _bundle(tmp_path)
    state = tmp_path / "state"
    refused = plan_module.plan(target, state_dir=state, kernel="0.1.0", stage=False)
    assert not refused.success
    assert "FLX-3105" in refused.stderr
    assert (
        "configures a history, which needs kernel 0.2.0; this run is kernel 0.1.0" in refused.stderr
    )
    assert not list(state.glob("runs/*/plan.json"))
