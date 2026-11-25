# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A snapshot emit's rows are the last versions of a history, so its fidelity block may declare no column.

Every other emit keeps the ``FLX-5034`` refusal of an empty ``fidelity.columns``.
"""

from __future__ import annotations

import json
import shutil
from importlib import import_module
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml

from flux_engine.concordance.compiler import EXTENSION_TRANSPORT_KEY, compile_fidelity
from flux_engine.concordance.predicate import expose_from_contract
from flux_engine.core.canonical import canonicalise
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import KERNEL_V2
from flux_engine.seam.resolve import load_bundle, resolve_all_emits

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


def _compiled(bundle: Path, emit: str) -> Any:
    """The block ``verify`` compiles for *emit*, handed over as ``apply`` records it."""
    simulation = yaml.safe_load((bundle / "simulation.flux.yml").read_text("utf-8"))
    for entry in resolve_all_emits(load_bundle(bundle), simulation_id=simulation["id"]):
        if entry.expose_id != emit:
            continue
        carried = dict(canonicalise(entry.emit))
        carried[EXTENSION_TRANSPORT_KEY] = canonicalise(dict(entry.extension or {}))
        recorded = json.loads(
            json.dumps({"contract": canonicalise(entry.contract_document), "emit": carried})
        )
        expose = expose_from_contract(recorded["contract"], emit)
        return compile_fidelity(contract=recorded["contract"], emit=recorded["emit"], expose=expose)
    raise AssertionError(emit)


def test_verify_compiles_a_snapshots_empty_columns(tmp_path: Path) -> None:
    assert _compiled(_bundle(tmp_path, SNAPSHOT), SNAPSHOT).block.properties == ()


@pytest.mark.parametrize("projected", [True, False], ids=["snapshot-entry", "no-entry"])
def test_verify_keeps_the_refusal_for_every_emit_no_snapshot_names(
    tmp_path: Path, projected: bool
) -> None:
    """Only a snapshots entry makes empty ``fidelity.columns`` legal on the verify side."""
    bundle = _bundle(tmp_path, DEPOT, projected=projected)
    with pytest.raises(FluxError) as caught:
        _compiled(bundle, DEPOT)
    assert caught.value.code.code == "FLX-5034"


def test_verify_refuses_a_snapshot_emit_once_no_entry_names_it(tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        _compiled(_bundle(tmp_path, SNAPSHOT, projected=False), SNAPSHOT)
    assert caught.value.code.code == "FLX-5034"
