# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``--scale`` above 1 widens key columns past a contract range, which ``verify`` then does not count (ADR 0024).

``depot.depot_id`` is a key column whose fidelity declares ``range: {min: 1, max: 50}``. A scale of 2
grows it to 100 keys, so a contract range of ``>= 1 and <= 50`` on it would fail every run. At a scale
above 1 ``verify`` does not count the contract range of a column the scale widened, and ``plan`` says
so once for the column; a column the scale does not widen, and a scale of 1 or below, stay checked.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pytest

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.runtime import resolve_cast
from flux_engine.seam.resolve import load_bundle, resolve_all_emits
from flux_engine.stages.scaling import widened_columns

from ._fleet import DEPOT, VISIT, apply_module, bundle, plan_module, with_range
from .test_declared_range_run import verify_module

pytestmark = pytest.mark.integration

KEY_RANGE = ">= 1 and <= 50"


def _ranged(tmp_path: Path, expose_id: str, column: str, rule: str) -> Path:
    return with_range(bundle(tmp_path), expose_id, column, rule)


def _run(target: Path, state: Path, scale: str) -> Any:
    planned = plan_module.plan(
        target, state_dir=state, kernel=KERNEL_V2.version, stage=False, scale=Decimal(scale)
    )
    assert planned.success, planned.stderr
    run_id = planned.artifacts["run_id"]
    applied = apply_module.apply(target, run_id=run_id, state_dir=state)
    assert applied.success, applied.stderr
    return planned, verify_module.verify(target, run_id=run_id, state_dir=state)


def _ranges(stdout: str, prefix: Optional[str] = None) -> list[str]:
    return [
        line
        for line in stdout.splitlines()
        if line.startswith("  ranges:") and (prefix is None or prefix in line)
    ]


def test_a_key_column_the_scale_widens_is_not_checked_and_plan_says_so(tmp_path: Path) -> None:
    target = _ranged(tmp_path, DEPOT, "depot_id", KEY_RANGE)
    planned, verified = _run(target, tmp_path / "state", "2")
    assert [line for line in planned.stderr.splitlines() if line.startswith("note: ")] == [
        "note: depot.depot_id's contract range (>= 1 and <= 50) is not checked at --scale 2: "
        "the scale widens the column's keys"
    ]
    assert verified.success, verified.stderr
    # Verify says it too, in its own line, and the run still passes.
    assert _ranges(verified.stdout) == [
        "  ranges: depot.depot_id: not checked at --scale 2: the scale widens the column's keys "
        "past its contract range"
    ]
    assert verified.artifacts["rangesNotChecked"] == [
        _ranges(verified.stdout)[0][len("  ranges: ") :]
    ]
    assert "rangeConformance" not in verified.artifacts


@pytest.mark.parametrize("scale", ["1", "0.5"])
def test_scale_one_and_below_keep_the_range_checked(tmp_path: Path, scale: str) -> None:
    target = _ranged(tmp_path, DEPOT, "depot_id", KEY_RANGE)
    planned, verified = _run(target, tmp_path / "state", scale)
    assert not [line for line in planned.stderr.splitlines() if "is not checked at" in line]
    # Every key is inside 1..50 at these scales, so the check passes by counting.
    assert verified.success, verified.stdout
    assert not _ranges(verified.stdout)
    assert "rangesNotChecked" not in verified.artifacts


def test_a_non_key_number_column_is_still_checked_at_scale_two(tmp_path: Path) -> None:
    target = _ranged(tmp_path, VISIT, "cost", "between 5 and 100")
    planned, verified = _run(target, tmp_path / "state", "2")
    assert not [line for line in planned.stderr.splitlines() if "is not checked at" in line]
    assert not verified.success
    assert _ranges(verified.stdout, "service_visit.cost")


def test_only_a_key_column_whose_range_the_scale_grows_is_widened(tmp_path: Path) -> None:
    target = bundle(tmp_path)
    loaded = load_bundle(target)
    emits = {
        emit.expose_id: emit
        for emit in resolve_all_emits(
            loaded, simulation_id=resolve_cast(loaded.flux_documents).simulation_id
        )
    }
    depot = emits[DEPOT]
    assert widened_columns(depot, Decimal(2)) == {"depot_id"}
    assert widened_columns(depot, Decimal(1)) == frozenset()
    assert widened_columns(depot, Decimal("0.5")) == frozenset()
    # A key that declares no range has none to grow.
    assert depot.extension is not None
    columns = dict(depot.extension["fidelity"]["columns"])
    columns["depot_id"] = {}
    bare = replace(
        depot,
        extension={
            **depot.extension,
            "fidelity": {**depot.extension["fidelity"], "columns": columns},
        },
    )
    assert widened_columns(bare, Decimal(2)) == frozenset()
    # A column that is no key is never widened.
    assert widened_columns(emits[VISIT], Decimal(2)) <= {"visit_id", "vehicle_id"}
