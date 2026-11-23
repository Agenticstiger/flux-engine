# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A projected emit's rows are derived at plan time and recorded (ADR 0027 section 2), on ``tests/fixtures/history-projections``.

The plan records the snapshot's derived ``rows`` and ``rowsFrom`` and no other emit has them; the
count enters ``planDigest`` and not an entry for ``H``; a declared ``rows`` is refused; kernel
0.1.0 refuses the snapshot, and so does kernel 0.2.0 a projection that has no generator (periodic rows, for now).
"""

from __future__ import annotations

import json
import shutil
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V1, KERNEL_V2
from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout

apply_module = import_module("flux_engine.stages.apply")
plan_module = import_module("flux_engine.stages.plan")

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "history-projections"
NAMESPACE = "com.agentics.flux/1"
HISTORY = "subscription_version"
SNAPSHOT = "subscription_current"
STATUSES = {"status": {"levels": {"active": "0.7", "paused": "0.3", "cancelled": "0"}}}


def _bundle(
    tmp_path: Path,
    *,
    name: str = "bundle",
    versions: int = 200,
    entities: int = 60,
    customers: int = 50,
    window_end: Optional[str] = None,
    ended: Optional[str] = None,
    end: Optional[Dict[str, Any]] = None,
    snapshot_rows: Optional[int] = None,
) -> Path:
    """A copy of the fixture, with its sizes, its window end, its end mode or its snapshot's ``ended`` changed."""
    target = tmp_path / name
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    document = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][NAMESPACE]
    emits = flux["emits"]
    emits["customer"]["rows"] = customers
    emits["customer"]["fidelity"]["columns"]["customer_id"]["range"]["max"] = str(customers)
    emits[HISTORY]["rows"] = versions
    declared = emits[HISTORY]["fidelity"]["columns"]
    declared["subscription_sk"]["range"]["max"] = str(versions)
    declared["subscription_id"]["range"]["max"] = str(max(entities, 200))
    emits[SNAPSHOT]["fidelity"]["columns"]["subscription_id"]["range"]["max"] = str(
        max(entities, 200)
    )
    flux["history"][0]["entities"] = entities
    if end is not None:
        flux["history"][0]["end"] = end
        declared.update(STATUSES)
        emits[SNAPSHOT]["fidelity"]["columns"].update(STATUSES)
    if ended is not None:
        flux["snapshots"][0]["ended"] = ended
    if snapshot_rows is not None:
        emits[SNAPSHOT]["rows"] = snapshot_rows
    if window_end is not None:
        document["spec"]["timeBounds"]["end"] = window_end
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _rows(layout: RunLayout, expose_id: str) -> List[Dict[str, Any]]:
    names = json.loads(layout.seam_attestation_path(expose_id).read_text("utf-8"))["columns"]
    return [
        dict(zip(names, json.loads(line), strict=True))
        for line in layout.emit_path(expose_id).read_text("utf-8").splitlines()
    ]


def _run(bundle: Path, state: Path, **options: Any) -> RunLayout:
    planned = plan_module.plan(
        bundle, state_dir=state, kernel=KERNEL_V2.version, stage=False, **options
    )
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    applied = apply_module.apply(bundle, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr
    return RunLayout.for_state_dir(state, run_id)


def _plan_document(layout: RunLayout) -> Dict[str, Any]:
    return json.loads(layout.plan_path.read_text("utf-8"))


def _last_versions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The last version of each entity, in the order the entities first appear."""
    last: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        last[row["subscription_id"]] = row
    return list(last.values())


def _columns(row: Dict[str, Any], names: List[str]) -> Dict[str, Any]:
    return {name: row[name] for name in names}


SNAPSHOT_COLUMNS = ["subscription_id", "plan", "status", "started_on"]


def test_the_plan_records_the_derived_rows_and_where_they_come_from(tmp_path: Path) -> None:
    layout = _run(_bundle(tmp_path), tmp_path / "state")
    emits = _plan_document(layout)["emits"]
    assert emits[SNAPSHOT]["rows"] == 60
    assert emits[SNAPSHOT]["rowsFrom"] == {"of": HISTORY, "projection": "snapshots"}
    assert emits[SNAPSHOT]["generator"]["rows"] == 60
    for expose_id in ("customer", HISTORY, "invoice"):
        assert "rowsFrom" not in emits[expose_id] and "rows" not in emits[expose_id]


def test_the_derived_count_is_in_the_plan_digest_and_in_no_entry_for_h(tmp_path: Path) -> None:
    first = _run(_bundle(tmp_path, name="a"), tmp_path / "a")
    second = _run(_bundle(tmp_path, name="b", entities=61), tmp_path / "b")
    one, two = _plan_document(first), _plan_document(second)
    assert one["emits"][SNAPSHOT]["rows"] == 60 and two["emits"][SNAPSHOT]["rows"] == 61
    assert one["planDigest"] != two["planDigest"]
    # The count is no member of H: an equal history with another count of rows moves nothing there.
    assert one["emits"]["customer"]["H"] == two["emits"]["customer"]["H"]


def test_the_count_leaves_the_entry_for_h_alone(tmp_path: Path) -> None:
    """Dropping the ended entities moves the snapshot's entry for H (the entry) and no count enters it."""
    end = {"mode": "finalVersion", "share": "0.25", "column": "status", "value": "cancelled"}
    keep = _run(_bundle(tmp_path, name="k", end=end), tmp_path / "k")
    drop = _run(_bundle(tmp_path, name="d", end=end, ended="drop"), tmp_path / "d")
    kept, dropped = _plan_document(keep)["emits"][SNAPSHOT], _plan_document(drop)["emits"][SNAPSHOT]
    assert kept["rows"] == 60 and dropped["rows"] < 60
    assert kept["H"] != dropped["H"]  # the entry's own option moves H, as any option does


def test_a_declared_rows_on_a_projected_emit_is_still_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, snapshot_rows=120)
    planned = plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )
    assert not planned.success
    assert planned.error_code == "FLX-3403" and SNAPSHOT in planned.stderr


def test_kernel_010_refuses_the_snapshot(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        pipeline.execute(_bundle(tmp_path), kernel=KERNEL_V1)
    assert caught.value.code.code == "FLX-3105" and "kernel 0.2.0" in str(caught.value)
