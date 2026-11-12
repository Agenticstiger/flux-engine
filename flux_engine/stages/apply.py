# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim apply``: re-execute a recorded plan and write its run directory.

Refuses (``FLX-2102``) when ``plan.json`` no longer hashes to the
``planDigest`` it declares -- someone edited the file by hand -- and
refuses (``FLX-2101``) when the bundle's freshly recomputed ``inputDigest``
no longer matches the one the plan recorded -- the bundle moved since
`fluxsim plan` ran. Both are checked *before* a single row is written.

Every artifact KERNEL-SPEC names lands under the run
directory: ``population.ndjson`` (cohort/market/trait), ``events.ndjson``
(signal/detect/transition/deliver/suppress/assign), ``suppressions.ndjson``,
``decisions.ndjson`` + its meta sidecar, one ``emits/<exposeId>.ndjson``
per declared stream, one ``seam-attestation-<exposeId>.json`` per emit,
``run-digest.json``, and ``receipt.json``.

Every one of those is rewritten whole on every apply -- ``lineage.ndjson``
included, which is the one file this stage used to append to, so a second
apply of one unchanged bundle grew a run directory that KERNEL-SPEC and
:mod:`flux_engine.stages.layout` both describe as overwritten from row
zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from flux_engine.api.lineage import DatasetRef, LineageEvent, LineageEventType
from flux_engine.api.stages import StageResult
from flux_engine.concordance.compiler import EXTENSION_TRANSPORT_KEY
from flux_engine.core.canonical import canonicalise
from flux_engine.core.digest import DigestDomain, DigestRef, build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.trace import TraceOp, build_run_digest_document, component_of
from flux_engine.lineage.emitter import (
    NdjsonLineageEmitter,
    build_run_id,
    dataset_ref_for_binding,
    determinism_facet,
    governance_facet,
    job_namespace,
)
from flux_engine.seam.attest import build_seam_attestation
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages.errors import InputDigestMoved, NoRecordedPlan, PlanDigestMoved
from flux_engine.stages.layout import (
    EVENTS_LEDGER_COMPONENTS,
    FLX_2113,
    POPULATION_LEDGER_COMPONENTS,
    RunLayout,
    atomic_write_text,
    run_id_for,
)
from flux_engine.stages.plan import plan_with_result as run_plan_with_result

__all__ = ["apply", "load_recorded_plan"]


#: One line per record, canonical (NFC, Decimal-as-string) JSON -- readable
#: NDJSON, and re-digestible with `flux_engine.core.canonical.canonical_bytes`
#: line by line.
def _write_ndjson(path: Path, records: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(canonicalise(record), sort_keys=True, ensure_ascii=False))
            handle.write("\n")


def _ledger(trace: Any, components: Sequence[str]) -> List[Mapping[str, Any]]:
    """Every op of *components*, in the order the run emitted them.

    KERNEL-SPEC §10's component digests are taken over each component's
    ops **in emission order** -- that is what
    :func:`flux_engine.core.trace.component_digests` does, grouping by
    component and keeping the sequence inside each group. A ledger written
    grouped by op instead carries the right records in a sequence no
    digest was ever taken over, so nothing reading the file back can
    arrive at the value ``run-digest.json`` records. ``verify`` and
    ``ship`` do exactly that reading back now (``FLX-2110``), so the
    ledgers are written in the order the digest was computed in.
    """
    wanted = set(components)
    return [record for record in trace.records if component_of(record["op"]) in wanted]


def load_recorded_plan(layout: RunLayout) -> Dict[str, Any]:
    """Read ``plan.json`` back and confirm it belongs here and still hashes to itself.

    Two facts, not one. ``planDigest`` shows the document was not edited
    after it was written; the run id derived from that digest pins the
    document as *this* directory's plan. Without the second check a run
    directory copied under another name kept every internal comparison
    green -- ``plan.json``, ``fidelity-input-<id>.json`` and
    ``seam-attestation-<id>.json`` all travel together, so a consistently
    copied tree agreed with itself all the way to a signed evidence pack
    (``FLX-2113``).
    """
    if not layout.plan_path.is_file():
        raise NoRecordedPlan(plan_path=layout.plan_path)
    document = json.loads(layout.plan_path.read_text(encoding="utf-8"))
    recorded = document.get("planDigest")
    digested = build_plan_digest(document)
    if recorded != digested.ref.value:
        raise PlanDigestMoved(recorded=str(recorded), recomputed=digested.ref.value)
    derived = run_id_for(str(recorded))
    if derived != layout.run_id:
        raise FluxError(
            FLX_2113,
            f"{layout.run_dir} holds the plan of run {derived} "
            f"(planDigest {recorded}); the directory is named {layout.run_id!r}",
        )
    return document


def apply(
    bundle_directory: Union[str, Path],
    *,
    run_id: Optional[str] = None,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    auto_plan: bool = True,
    precomputed: Optional["_pipeline.PipelineResult"] = None,
    jobs: int = 1,
) -> StageResult:
    """Apply the plan at ``<state_dir>/runs/<run_id>/plan.json``.

    When *run_id* is omitted (or *auto_plan* finds no existing plan), a
    fresh plan is taken first -- the same convenience `terraform apply`
    (with no `-out` given) offers, and just as safe here: planning is
    read-only and deterministic, so an implicit plan changes nothing about
    what gets applied.

    *precomputed* is the execution a caller already has in hand for these
    exact bundle bytes -- what :func:`~flux_engine.stages.plan.plan_with_result`
    hands back. A plan *is* an execution, so re-running the pipeline over
    the same bytes a millisecond later doubles the cost of the loop's most
    expensive half (25 000 subjects) to reach a result the caller is
    holding. The ``inputDigest`` equality check below still runs against it
    -- trivially satisfied on this path, and still worth asserting, since a
    caller that passed a result for a *different* bundle must be refused
    exactly as a moved bundle is.
    """
    directory = Path(bundle_directory)
    state = Path(state_dir) if state_dir is not None else Path(".flux")

    layout: Optional[RunLayout] = None
    if run_id is not None:
        layout = RunLayout.for_state_dir(state, run_id)
    elif auto_plan:
        planned, planned_result = run_plan_with_result(
            directory, simulation_id=simulation_id, state_dir=state, zone=zone, jobs=jobs
        )
        if not planned.success:
            return StageResult(
                stage="apply",
                exit_code=1,
                _artifacts=dict(planned.artifacts),
                stderr=planned.stderr,
                error_code=planned.error_code,
            )
        if precomputed is None:
            precomputed = planned_result
        layout = RunLayout.for_state_dir(state, planned.artifacts["run_id"])

    assert layout is not None
    try:
        plan_document = load_recorded_plan(layout)
    except FluxError as error:
        return StageResult(
            stage="apply",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )

    if precomputed is not None:
        result = precomputed
    else:
        try:
            result = _pipeline.execute(directory, simulation_id=simulation_id, zone=zone, jobs=jobs)
        except FluxError as error:
            return StageResult(
                stage="apply",
                exit_code=1,
                _artifacts={"code": error.code.code},
                stderr=str(error),
                error_code=error.code.code,
            )

    current_input_digest = result.input_digest.ref.value
    planned_input_digest = plan_document["inputDigest"]
    if current_input_digest != planned_input_digest:
        moved_error = InputDigestMoved(planned=planned_input_digest, current=current_input_digest)
        return StageResult(
            stage="apply",
            exit_code=1,
            _artifacts={"code": moved_error.code.code},
            stderr=str(moved_error),
            error_code=moved_error.code.code,
        )

    layout.ensure()
    trace = result.trace
    _write_ndjson(layout.population_path, _ledger(trace, POPULATION_LEDGER_COMPONENTS))
    _write_ndjson(layout.events_path, _ledger(trace, EVENTS_LEDGER_COMPONENTS))
    _write_ndjson(layout.suppressions_path, trace.of(TraceOp.SUPPRESS))
    result.decisions.write(layout.run_dir)

    for outcome in result.emits:
        emit_path = layout.emit_path(outcome.resolved.expose_id)
        emit_path.parent.mkdir(parents=True, exist_ok=True)
        emit_path.write_bytes(outcome.writer.stream_bytes())

        generator_digest = outcome.plan.digest() if outcome.plan is not None else None
        attestation = build_seam_attestation(
            outcome.resolved, outcome.expose, outcome.result, generator_digest=generator_digest
        )
        attestation_path = layout.seam_attestation_path(outcome.resolved.expose_id)
        attestation_path.write_text(
            json.dumps(attestation.as_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        if outcome.resolved.fidelity is not None:
            # The block travels **as authored**. There is no narrowing pass
            # any more: `flux_engine.concordance.predicate.COLUMN_KEYS` is
            # the one `fidelity.columns` schema both the check and the
            # generator read, so nothing has to be dropped for
            # `compile_fidelity` to accept it.
            fidelity_input_path = layout.fidelity_input_path(outcome.resolved.expose_id)
            emit_document = dict(canonicalise(outcome.resolved.emit))
            emit_document[EXTENSION_TRANSPORT_KEY] = canonicalise(
                dict(outcome.resolved.extension or {})
            )
            fidelity_input_path.write_text(
                json.dumps(
                    {
                        "contract": canonicalise(outcome.resolved.contract_document),
                        "emit": emit_document,
                        "exposeId": outcome.resolved.expose_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

    plan_digest_ref = DigestRef(
        value=plan_document["planDigest"], domain=DigestDomain.JCS_DOCUMENT, subject="planDigest"
    )
    run_digest_doc = build_run_digest_document(
        kernel_version=_pipeline.KERNEL_VERSION,
        input_digest=result.input_digest.ref,
        plan_digest=plan_digest_ref,
        components=result.components,
    )
    atomic_write_text(
        layout.run_digest_path, json.dumps(run_digest_doc.document, indent=2, sort_keys=True) + "\n"
    )

    _emit_lineage(directory, layout, result, plan_digest_ref, run_digest_doc.ref)

    from flux_engine import __version__ as _engine_version

    receipt = {
        "runId": layout.run_id,
        "simulationId": result.cast.simulation_id,
        # KERNEL-SPEC §1's version table names four fields the receipt
        # carries. Three of them were missing, and `engineVersion` occurred
        # nowhere in the engine except inside NON_DIGEST_FIELDS -- the mask
        # set masked a field no artifact ever wrote.
        "kernelVersion": _pipeline.KERNEL_VERSION,
        "seedTreeVersion": _pipeline.SEED_TREE_VERSION,
        "traceVersion": _pipeline.TRACE_VERSION,
        "engineVersion": _engine_version,
        "fidelity": result.fidelity,
        "seed": result.seed,
        "inputDigest": current_input_digest,
        "planDigest": plan_document["planDigest"],
        "runDigest": run_digest_doc.ref.value,
        "seedTreeDigest": result.seed_tree.digest(),
        "declaredGoldenBaseline": result.cast.simulation.get("spec", {})
        .get("golden", {})
        .get("baselineDigest"),
        "population": len(result.population),
        "events": len(result.events),
        "emits": {
            outcome.resolved.expose_id: {
                "rows": outcome.result.rows,
                "outputDigest": outcome.result.output_digest.value,
                "declaresFidelity": outcome.resolved.fidelity is not None,
                "source": outcome.source,
                **(
                    {"mapping": outcome.mapping.as_document()["bindings"]}
                    if outcome.mapping is not None
                    else {}
                ),
            }
            for outcome in result.emits
        },
        "sovereigntyAudit": [
            {
                "zone": row.zone,
                "allowedZones": list(row.allowed_zones),
                "documentId": row.document_id,
                "documentKind": row.document_kind,
                "eventTime": row.event_time,
            }
            for row in result.sovereignty_audit
        ],
    }
    # The commit marker, written last and written whole: `ship` and
    # `verify` read the presence of `receipt.json` as "this run
    # completed", so a `write_text` interrupted part-way left a truncated
    # file that is present, is not JSON, and reads to the next stage as a
    # finished run (see `atomic_write_text`).
    atomic_write_text(layout.receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    return StageResult(
        stage="apply",
        exit_code=0,
        _artifacts={
            "run_id": layout.run_id,
            "run_dir": str(layout.run_dir),
            "run_digest": run_digest_doc.ref.value,
            "receipt": receipt,
            "pipeline": result,
        },
        stdout="\n".join(
            [
                f"applied to {layout.run_dir} (runDigest={run_digest_doc.ref.value})",
                _emit_summary_line(result),
            ]
        ),
    )


def _emit_summary_line(result: "_pipeline.PipelineResult") -> str:
    """One line naming every emit, its row count and where the rows came from.

    A run's headline number. It is printed rather than only written into
    the receipt because "how many rows did this produce" is the first
    question anyone asks of a simulation, and a stream that silently
    produced none is exactly the failure this line makes impossible to
    miss. Emits are named in declared order (KERNEL-SPEC §3's ``emits[]``
    order), so the line is stable across runs. An emit made by more than one
    process says how many, so ``--jobs`` shows whether it took effect; one
    process, the common case, adds nothing to the line.
    """
    if not result.emits:
        return "no emits declared"
    parts = []
    for outcome in result.emits:
        source = outcome.source
        if outcome.processes > 1:
            source = f"{source}, {outcome.processes} processes"
        parts.append(f"{outcome.resolved.expose_id}: {outcome.result.rows} rows ({source})")
    return "emitted " + "; ".join(parts)


def _emit_lineage(
    bundle_directory: Path,
    layout: RunLayout,
    result: "_pipeline.PipelineResult",
    plan_digest: DigestRef,
    run_digest: DigestRef,
) -> None:
    """Write ``lineage.ndjson``: one START, one COMPLETE OpenLineage RunEvent.

    Best-effort and never fatal to the run: :class:`NdjsonLineageEmitter`
    swallows its own I/O errors, matching the
    :class:`~flux_engine.api.lineage.LineageEmitter` Protocol's rule that a
    lost delivery is answerable, not a run-ending exception.

    **Truncated first, then written.** The emitter appends, so without
    this the file was the one artifact in the run directory that grew on
    every re-apply and every replay: two more RunEvents for one logical
    run, a lineage graph reading repeated executions, and an evidence
    archive whose Merkle root moved over byte-identical inputs. Every
    other artifact here is rewritten whole; so is this one.

    **The namespace names the Simulation, not the directory.** A job
    namespace built from ``bundle_directory.name`` fragments a lineage
    graph per checkout name (``flux://bundle``, ``flux://src``,
    ``flux://workspace``) and puts the operator's local layout into a
    published event. ``Simulation.id`` is the identity the bundle declares
    and travels with it.
    """
    from datetime import datetime, timezone

    del bundle_directory  # identity comes from the Simulation, not from a local path
    run_id = build_run_id(plan_digest, result.root.value, 0)
    namespace = job_namespace(result.cast.simulation_id)
    layout.lineage_path.parent.mkdir(parents=True, exist_ok=True)
    layout.lineage_path.write_bytes(b"")
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    facets = {
        "flux.determinism": determinism_facet(
            kernel_version=_pipeline.KERNEL_VERSION,
            seed_tree_version=_pipeline.SEED_TREE_VERSION,
            trace_version=_pipeline.TRACE_VERSION,
            input_digest=result.input_digest.ref,
            plan_digest=plan_digest,
            run_digest=run_digest,
        ),
        "flux.governance": governance_facet(decisions_digest=result.decisions.decisions_digest()),
    }
    outputs = tuple(
        _output_dataset(outcome.resolved.contract_document, outcome.resolved.expose_id)
        for outcome in result.emits
    )
    emitter = NdjsonLineageEmitter(path=layout.lineage_path)
    emitter.emit(
        LineageEvent(
            event_type=LineageEventType.START,
            event_time=now,
            run_id=run_id,
            job_namespace=namespace,
            job_name=result.cast.simulation_id,
            outputs=outputs,
            facets=facets,
        )
    )
    emitter.emit(
        LineageEvent(
            event_type=LineageEventType.COMPLETE,
            event_time=now,
            run_id=run_id,
            job_namespace=namespace,
            job_name=result.cast.simulation_id,
            outputs=outputs,
            facets=facets,
        )
    )


def _output_dataset(contract_document: "Mapping[str, Any]", expose_id: str) -> DatasetRef:
    for expose in contract_document.get("exposes", []):
        if expose.get("exposeId") == expose_id:
            location = expose.get("binding", {}).get("location", {})
            if isinstance(location, dict) and location:
                first = next(iter(location.values()))
                return dataset_ref_for_binding(str(first))
    return DatasetRef(namespace="flux", name=expose_id)
