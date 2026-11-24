# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The bundle the fit tests run on: ``tests/fixtures/consumers/bundle`` with one value a fit can move.

The fixture's own rows are what other tests count, so it is not changed. A copy gets two edits: the share of
``renewed`` the generator leaves NULL is stated, at a tenth, as an assumption of the register, and the
calibration block names it (``min`` 0, ``max`` 0.5, ``step`` 0.01). The profile states a higher rate for
``premium-renewed`` than a tenth of NULLs lets the bundle reach, so a fit moves the share down.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Dict

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "consumers"
NAMESPACE = "com.agentics.flux/1"
SIMULATION = "simulation.flux.yml"
NULLS = "/extensions/com.agentics.flux~11/emits/subscriptions/fidelity/columns/renewed/nullFraction"


def _assume_nulls(namespace: Dict[str, Any], simulation_id: str) -> None:
    columns = namespace["emits"]["subscriptions"]["fidelity"]["columns"]
    columns["renewed"]["nullFraction"] = "0.1"
    entries = namespace["parameters"]
    position = next(
        index
        for index, item in enumerate(entries)
        if item["pointer"].endswith("/columns/renewed/levels/true")
    )
    entries.insert(
        position + 1,
        {
            "document": simulation_id,
            "pointer": NULLS,
            "source": "assumption",
            "reason": "The contract allows NULL in renewed and the profile publishes no count of "
            "it: a tenth is assumed, and a calibration may move it.",
        },
    )
    namespace["consumers"]["calibration"]["parameters"] = [
        {"document": SIMULATION, "pointer": NULLS, "min": "0", "max": "0.5", "step": "0.01"}
    ]


def edit_simulation(bundle: Path, change: Callable[[Dict[str, Any]], None]) -> Path:
    """Apply *change* to the Simulation document of *bundle*, write it back, and return *bundle*."""
    path = bundle / SIMULATION
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return bundle


def copy_fit_bundle(root: Path) -> Path:
    """A copy of the fixture bundle under *root* that a fit can move, as described above."""
    bundle = root / "bundle"
    shutil.copytree(FIXTURES / "bundle", bundle)
    return edit_simulation(
        bundle, lambda document: _assume_nulls(document["extensions"][NAMESPACE], document["id"])
    )
