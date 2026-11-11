# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim validate``: schema-valid, seam-resolvable, and executable in principle.

Three checks, in order, none of which materialises a population or draws
a single value:

1. :func:`flux_engine.seam.resolve.load_bundle` -- the bundle is
   schema-valid FLUX/FLUID, and the validating and exact reads agree on
   which documents exist (KERNEL-SPEC §2).
2. Every ``Simulation``'s cast resolves (every ``*Ref`` in its closure
   points at a document in the bundle) and every one of its ``emits[]``
   entries resolves against a FLUID contract in the bundle
   (KERNEL-SPEC §8).
3. Every ``Simulation``'s declared ``fidelity`` is one this engine
   version can execute (:func:`flux_engine.stages.pipeline.require_supported_fidelity`).

Nothing here is written to disk; ``validate`` never creates a run
directory, which is the difference between it and ``plan``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Union

from flux_engine.api.stages import StageResult
from flux_engine.core.errors import FluxError
from flux_engine.runtime import resolve_cast
from flux_engine.seam.resolve import load_bundle, resolve_all_emits
from flux_engine.stages.pipeline import require_supported_fidelity

__all__ = ["validate"]


def validate(bundle_directory: Union[str, Path], *, tolerate_additive: bool = True) -> StageResult:
    """Validate the bundle at *bundle_directory*. Never writes anything."""
    directory = Path(bundle_directory)
    try:
        loaded = load_bundle(directory, tolerate_additive=tolerate_additive)
    except FluxError as error:
        return StageResult(
            stage="validate",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )

    simulations: Dict[str, Any] = {}
    problems: List[str] = []
    for simulation_id in sorted(loaded.simulation_ids()):
        try:
            cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
            fidelity = str(cast.simulation.get("spec", {}).get("fidelity", "deterministic"))
            require_supported_fidelity(fidelity)
            resolved = resolve_all_emits(loaded, simulation_id=simulation_id)
            simulations[simulation_id] = {
                "fidelity": fidelity,
                "documents": len(cast.documents),
                "emits": [entry.expose_id for entry in resolved],
                "emitsWithFidelity": [
                    entry.expose_id for entry in resolved if entry.fidelity is not None
                ],
            }
        except FluxError as error:
            problems.append(f"{simulation_id}: {error}")

    if problems:
        return StageResult(
            stage="validate",
            exit_code=1,
            _artifacts={"simulations": simulations, "problems": problems},
            stderr="; ".join(problems),
        )

    return StageResult(
        stage="validate",
        exit_code=0,
        _artifacts={
            "documents": len(loaded.flux_documents) + len(loaded.fluid_documents),
            "fluxDocuments": len(loaded.flux_documents),
            "fluidDocuments": len(loaded.fluid_documents),
            "simulations": simulations,
        },
        stdout=(
            f"{directory}: valid -- {len(loaded.flux_documents)} FLUX document(s), "
            f"{len(loaded.fluid_documents)} FLUID contract(s), "
            f"{len(simulations)} Simulation(s)"
        ),
    )
