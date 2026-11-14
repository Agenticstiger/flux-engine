# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim plan``: execute the whole loop once, in memory, and record it.

Terraform's shape, on this engine: a plan runs the *entire* pipeline
(:func:`flux_engine.stages.pipeline.execute`) exactly the way ``apply``
will, and writes down what it saw -- ``inputDigest``, the seed-tree paths
it materialised (``seedTreeDigest``), the per-emit seam digests (``H``,
``G``), and, when an emit declares a fidelity block, its generator plan.
The block is compiled first, as SEAM-FIDELITY-SPEC §3 says
(:func:`flux_engine.stages.pipeline.compile_declared_fidelity`), so a
block ``verify`` would refuse is refused before the population exists; the
margins it computes are not written down. It writes two files: ``plan.json``, and the
``seed-paths.ndjson`` beside it that KERNEL-SPEC §3 puts the path list in.
Everything else is ``apply``'s: it re-executes (KERNEL-SPEC's determinism
promise says the second execution reaches the same trace and the same
emitted rows) and writes the rest.

**Why the paths are not in the plan.** A 25 000-subject run addresses
millions of streams. Inlined, they made ``plan.json`` a 16 MB file whose
first few dozen lines were the only ones a reviewer or a golden cared
about. §3 keeps them one canonical path per line, sorted, in
``seed-paths.ndjson``, and puts that file's raw-bytes digest --
``seedTreeDigest`` -- plus a per-shape count table (``seedTreeShapes``)
into the plan. The plan stays readable and golden-pinnable; the addresses
stay verifiable with ``sha256sum``; and ``FLX-2201`` still has the exact
set to check a run's streams against (:func:`planned_seed_tree`).

``planDigest`` (KERNEL-SPEC §11) is taken over ``plan.json`` with its own
``planDigest`` and ``generated_at`` masked out -- :func:`build_plan_digest`
does exactly that masking, so this module never has to.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.api.stages import StageResult
from flux_engine.core.digest import build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import (
    SeedPath,
    SeedTree,
    SeedTreeRoot,
    parse_seed_paths,
    seed_paths_digest,
)
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages.errors import SeedPathsMoved
from flux_engine.stages.layout import RunLayout, run_id_for

__all__ = [
    "build_plan_document",
    "plan",
    "plan_with_result",
    "planned_seed_tree",
    "read_seed_paths",
]


def _fidelity_summary(outcome: "_pipeline.EmitOutcome") -> Optional[Dict[str, Any]]:
    """A JSON-safe summary of an emit's generator plan, or ``None`` when undeclared."""
    if outcome.plan is None:
        return None
    return {
        "generatorDigest": outcome.plan.digest().value,
        "rows": outcome.plan.rows,
        "columns": [item.column.name for item in outcome.plan.columns],
    }


def build_plan_document(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """The document ``plan.json`` holds, before ``planDigest`` is folded in."""
    return {
        "kernelVersion": _pipeline.KERNEL_VERSION,
        "seedTreeVersion": _pipeline.SEED_TREE_VERSION,
        "simulationId": result.cast.simulation_id,
        "fidelity": result.fidelity,
        "seed": result.seed,
        "inputDigest": result.input_digest.ref.value,
        "seedTreeDigest": result.seed_tree.digest(),
        "seedTreeShapes": result.seed_tree.shape_counts(),
        "emits": {
            outcome.resolved.expose_id: {
                "H": outcome.digests.h.value,
                "G": outcome.digests.g.value,
                "productRef": outcome.resolved.product_ref,
                "declaresFidelity": outcome.resolved.fidelity is not None,
                "generator": _fidelity_summary(outcome),
            }
            for outcome in result.emits
        },
        "planDigest": None,  # self-referential -- masked, filled by build_plan_digest
        "generated_at": None,  # masked -- see NON_DIGEST_FIELDS
    }


def plan(
    bundle_directory: Union[str, Path],
    *,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    jobs: int = 1,
) -> StageResult:
    """Execute the loop once and write ``<state_dir>/runs/<runId>/plan.json``.

    *jobs* is how many processes may generate rows from declared statistics;
    it never changes what is generated (:func:`flux_engine.stages.pipeline.execute`).
    """
    return plan_with_result(
        bundle_directory, simulation_id=simulation_id, state_dir=state_dir, zone=zone, jobs=jobs
    )[0]


def plan_with_result(
    bundle_directory: Union[str, Path],
    *,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    jobs: int = 1,
) -> Tuple[StageResult, Optional["_pipeline.PipelineResult"]]:
    """:func:`plan`, plus the execution it planned from (``None`` when it failed).

    A plan *is* an execution -- the whole loop, in memory, exactly as
    ``apply`` will run it. A caller that plans implicitly and is about to
    execute the very same bundle bytes a millisecond later can take that
    execution rather than paying to materialise 25 000 subjects a second
    time. The population is the expensive half of the loop, and nothing in
    it depends on the plan having been written first.
    """
    directory = Path(bundle_directory)
    state = Path(state_dir) if state_dir is not None else Path(".flux")

    try:
        result = _pipeline.execute(directory, simulation_id=simulation_id, zone=zone, jobs=jobs)
    except FluxError as error:
        return (
            StageResult(
                stage="plan",
                exit_code=1,
                _artifacts={"code": error.code.code},
                stderr=str(error),
                error_code=error.code.code,
            ),
            None,
        )

    document = build_plan_document(result)
    digested = build_plan_digest(document)
    document["planDigest"] = digested.ref.value
    document["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    run_id = run_id_for(digested.ref.value)
    layout = RunLayout.for_state_dir(state, run_id).ensure()
    layout.plan_path.write_text(_render(document), encoding="utf-8")
    # Written whole, never appended to: the bytes are a pure function of the
    # path set, so a run killed mid-write is repaired by the next plan rather
    # than leaving a file whose digest can never match again.
    layout.seed_paths_path.write_bytes(result.seed_tree.seed_paths_bytes())

    return (
        StageResult(
            stage="plan",
            exit_code=0,
            _artifacts={
                "run_id": run_id,
                "run_dir": str(layout.run_dir),
                "plan_path": str(layout.plan_path),
                "seed_paths_path": str(layout.seed_paths_path),
                "plan_digest": digested.ref.value,
                "input_digest": result.input_digest.ref.value,
                "seed_tree_digest": document["seedTreeDigest"],
                "files_not_hashed": list(result.files_not_hashed),
                "plan_document": document,
            },
            stdout=_plan_stdout(layout, run_id, digested.ref.value, result.files_not_hashed),
        ),
        result,
    )


def _plan_stdout(
    layout: RunLayout, run_id: str, plan_digest: str, files_not_hashed: Sequence[str]
) -> str:
    """Where the plan landed, and which files beside the documents it did not hash.

    The second line exists because a reader has to be able to tell that a
    file in the bundle directory was left out of ``inputDigest``, and to
    tell which digest does cover it. Naming it in the plan's output is
    the difference between a rule and a surprise.

    The list stays out of :func:`build_plan_document`. A digest that
    folded in the names of the files it did not hash would depend on
    those files again, which is the defect this reporting exists to make
    visible.
    """
    line = f"plan written to {layout.plan_path} (runId={run_id}, planDigest={plan_digest})"
    if not files_not_hashed:
        return line
    listed = ", ".join(files_not_hashed)
    return (
        f"{line}\nnot hashed into inputDigest: {listed} "
        "(written by a pack, not read by a run; covered by the evidence "
        "archive's own manifest)"
    )


def _render(document: Dict[str, Any]) -> str:
    import json

    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def read_seed_paths(layout: RunLayout, plan_document: Mapping[str, Any]) -> Tuple[SeedPath, ...]:
    """Read back the seed-tree paths *plan_document* anticipated.

    The file's raw-bytes digest is checked against the plan's own
    ``seedTreeDigest`` first (``FLX-2108``): a path list that has moved
    since the plan is not the planned set, and letting ``FLX-2201`` check
    against it would be checking against the wrong list -- exactly the
    failure the plan/replay binding exists to make impossible.
    """
    recorded = str(plan_document.get("seedTreeDigest"))
    path = layout.seed_paths_path
    if not path.is_file():
        raise SeedPathsMoved(path=path, recorded=recorded, recomputed=None)
    data = path.read_bytes()
    recomputed = seed_paths_digest(data)
    if recomputed != recorded:
        raise SeedPathsMoved(path=path, recorded=recorded, recomputed=recomputed)
    return parse_seed_paths(data)


def planned_seed_tree(
    root: SeedTreeRoot, layout: RunLayout, plan_document: Mapping[str, Any]
) -> SeedTree:
    """A :class:`SeedTree` restricted to the paths the recorded plan anticipated.

    The ``FLX-2201`` half of the plan/replay binding, now that the paths
    live beside the plan rather than in it: the file is read once, its
    digest checked, and the labels held in memory, so the membership test
    a run makes per stream stays one hash lookup.
    """
    return SeedTree.for_plan(root, read_seed_paths(layout, plan_document))
