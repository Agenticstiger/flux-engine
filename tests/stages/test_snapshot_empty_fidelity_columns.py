# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A snapshot emit's rows are the last versions of a history, so its fidelity block may declare no column.

Every other emit keeps the ``FLX-5034`` refusal of an empty ``fidelity.columns``.
"""

from __future__ import annotations

import shutil
from importlib import import_module
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

from flux_engine.core.kernels import KERNEL_V2

from ._fleet import DEPOT, FIXTURE, NAMESPACE, SNAPSHOT, plan_module, run_bundle

validate_module = import_module("flux_engine.stages.validate")

pytestmark = pytest.mark.integration


def _bundle(
    tmp_path: Path, emit: str, *, drop: Optional[str] = None, projected: bool = True
) -> Path:
    """The fixture with *emit*'s columns emptied, or only column *drop* removed.

    Without *projected* the snapshot is an ordinary emit of 60 rows that no entry names.
    """
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    path = target / "simulation.flux.yml"
    document: Any = yaml.safe_load(path.read_text("utf-8"))
    flux = document["extensions"][NAMESPACE]
    fidelity = flux["emits"][emit]["fidelity"]
    if drop is None:
        fidelity["columns"] = {}
        fidelity["properties"] = []
    else:
        del fidelity["columns"][drop]
        fidelity["properties"] = [
            item for item in fidelity.get("properties", []) if item.get("column") != drop
        ]
    if not projected:
        del flux["snapshots"]
        flux["emits"][SNAPSHOT]["rows"] = 60
    path.write_text(yaml.safe_dump(document, sort_keys=False), "utf-8")
    return target


def _planned(bundle: Path, tmp_path: Path) -> Any:
    return plan_module.plan(
        bundle, state_dir=tmp_path / "state", kernel=KERNEL_V2.version, stage=False
    )


def test_a_snapshot_with_no_declared_column_validates_plans_and_applies(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, SNAPSHOT)
    validated = validate_module.validate(bundle)
    assert validated.success, validated.stderr
    run_bundle(bundle, tmp_path / "state")


def test_an_emit_no_snapshot_names_still_needs_a_column(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, DEPOT)
    validated = validate_module.validate(bundle)
    assert not validated.success
    assert validated.error_code == "FLX-5034"


def test_a_snapshots_column_with_no_statistics_plans(tmp_path: Path) -> None:
    """A key column of the snapshot that declares nothing is set by the history, not drawn."""
    planned = _planned(_bundle(tmp_path, SNAPSHOT, drop="model"), tmp_path)
    assert planned.success, planned.stderr


def test_the_same_column_on_an_emit_no_snapshot_names_still_needs_statistics(
    tmp_path: Path,
) -> None:
    """The column on an ordinary emit is drawn, so a column with no statistics is FLX-3402."""
    planned = _planned(_bundle(tmp_path, SNAPSHOT, drop="model", projected=False), tmp_path)
    assert not planned.success
    assert planned.error_code == "FLX-3402" and "model" in planned.stderr


def test_an_unnamed_emits_column_that_declares_nothing_is_still_refused(tmp_path: Path) -> None:
    """A column that is no key and declares no statistics is FLX-3402, as before."""
    planned = _planned(_bundle(tmp_path, DEPOT, drop="region"), tmp_path)
    assert not planned.success
    assert planned.error_code == "FLX-3402" and "region" in planned.stderr
