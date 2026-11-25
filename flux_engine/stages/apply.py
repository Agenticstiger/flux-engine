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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypeGuard, Union

from flux_engine.api.lineage import LineageEvent, LineageEventType
from flux_engine.api.stages import StageResult
from flux_engine.concordance.compiler import EXTENSION_TRANSPORT_KEY
from flux_engine.core import kernels as _kernels
from flux_engine.core.canonical import canonicalise
from flux_engine.core.digest import (
    DigestDomain,
    DigestRef,
    build_plan_digest,
    digest_of_document,
)
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import Kernel
from flux_engine.core.sizing import MOST_ROWS
from flux_engine.core.trace import TraceOp, build_run_digest_document, component_of
from flux_engine.evidence.provenance import PROVENANCE_DIGEST_KEY, provenance_digest
from flux_engine.evidence.register import REGISTER_DIGEST_KEY, register_digest
from flux_engine.generators.errors import FLX_3123
from flux_engine.lineage.emitter import (
    NdjsonLineageEmitter,
    build_run_id,
    dataset_for_expose,
    determinism_facet,
    governance_facet,
    job_namespace,
)
from flux_engine.outcomes.responses import REPLICATE_PLAN_KEY, RESPONSES_PLAN_KEY, Replicate
from flux_engine.safeio import (
    is_regular_file,
    write_bytes_atomic,
    write_text_atomic,
)
from flux_engine.seam.attest import build_seam_attestation
from flux_engine.seam.coverage import printable
from flux_engine.seam.history import END_SHARE_OF_ELIGIBLE, END_SHARE_OF_ENTITIES
from flux_engine.seam.resolve import load_bundle, profile_reference
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages._grades import (
    recorded_grade,
    require_minimum_grade,
    require_recorded_minimum,
    with_generators,
)
from flux_engine.stages.deliver import open_sink
from flux_engine.stages.errors import (
    FLX_2101,
    FLX_2102,
    FLX_2109,
    FLX_2110,
    ColumnProvenanceMoved,
    InputDigestMoved,
    KernelNotImplemented,
    NoRecordedPlan,
    ParameterRegisterMoved,
    PlanDigestMoved,
)
from flux_engine.stages.layout import (
    EVENTS_LEDGER_COMPONENTS,
    FLX_2113,
    POPULATION_LEDGER_COMPONENTS,
    RunLayout,
    atomic_write_text,
    read_json_object,
    run_id_for,
)
from flux_engine.stages.plan import (
    DATE_RANGES_CUT,
    DATE_RANGES_PLAN_KEY,
    END_SHARE_PLAN_KEY,
    MAX_ROWS_PLAN_KEY,
    RANGE_SETTERS_HELD,
    RANGE_SETTERS_PLAN_KEY,
    REFERENCES_PLAN_KEY,
    SCALE_FLOORS_PLAN_KEY,
    SCALE_FLOORS_TRACKED,
    column_provenance_text,
    parameter_register_text,
)
from flux_engine.stages.plan import plan_with_result as run_plan_with_result
from flux_engine.stages.scaling import optional_scale, parse_shard
from flux_engine.stages.staging import (
    commit_stage,
    discard_stage,
    read_stage,
    scenario_scale_note,
)
from flux_engine.stages.stored import declared_for_run, require_record
from flux_engine.stages.timings import lap, timed

__all__ = [
    "apply",
    "load_recorded_plan",
    "plan_date_ranges",
    "plan_scale_floors",
    "plan_max_rows",
    "plan_range_setters",
    "plan_end_share",
    "plan_kernel",
    "require_plan_shortfalls",
    "require_run_digest_binds_plan",
    "write_run",
]


#: One line per record, canonical (NFC, Decimal-as-string) JSON -- readable
#: NDJSON, and re-digestible with `flux_engine.core.canonical.canonical_bytes`
#: line by line.
def _write_ndjson(path: Path, records: Any) -> None:
    lines = [
        json.dumps(canonicalise(record), sort_keys=True, ensure_ascii=False) for record in records
    ]
    write_text_atomic(path, "".join(line + "\n" for line in lines))


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

    The plan must also name a kernel this engine implements (``FLX-2119``,
    :func:`plan_kernel`), because every reader of it runs or stamps that
    kernel: ``apply`` and ``replay`` run it, ``verify`` and ``ship`` stamp it.
    """
    if not is_regular_file(layout.plan_path):
        raise NoRecordedPlan(plan_path=layout.plan_path)
    document = read_json_object(
        layout.plan_path,
        code=FLX_2102,
        remedy="the plan was truncated or edited; run `fluxsim plan` again",
    )
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
    plan_kernel(document)
    plan_end_share(document)
    plan_date_ranges(document)
    plan_scale_floors(document)
    plan_max_rows(document)
    plan_range_setters(document)
    require_plan_shortfalls(document)
    return document


def plan_kernel(plan_document: Mapping[str, Any]) -> Kernel:
    """The kernel a recorded plan names, or the refusal (``FLX-2119``) when this engine has none.

    The plan's ``kernelVersion`` picks the kernel and its ``seedTreeVersion``
    must be that kernel's. A recorded plan is never run or stamped under the
    kernel a new plan would take (:data:`flux_engine.core.kernels.DEFAULT_KERNEL`),
    so its ``inputDigest`` and every digest after it are the ones it recorded,
    whatever that default is.
    """
    named = plan_document.get("kernelVersion")
    kernel = _kernels.kernel_named(named)
    if kernel is None or plan_document.get("seedTreeVersion") != kernel.seed_tree_version:
        seed_tree = plan_document.get("seedTreeVersion")
        raise KernelNotImplemented(
            named=f"{printable(str(named))} (seed tree {printable(str(seed_tree))})",
            implemented=[
                f"{known.version} (seed tree {known.seed_tree_version})"
                for _, known in sorted(_kernels.KERNELS.items())
            ],
        )
    return kernel


#: How much of a recorded value a diagnostic quotes: a plan is read before it is trusted.
_QUOTED_VALUE_CHARS = 40


def _short_repr(value: object) -> str:
    """*value* as a repr a diagnostic can carry: escaped, and cut at :data:`_QUOTED_VALUE_CHARS`."""
    text = repr(value)
    if len(text) <= _QUOTED_VALUE_CHARS:
        return text
    return text[: _QUOTED_VALUE_CHARS - 3] + "..."


def plan_end_share(plan_document: Mapping[str, Any]) -> str:
    """What a recorded plan's history ``end`` shares are shares of, or the refusal (``FLX-2102``).

    A plan with ``historyEndShare`` says ``"entities"``: the share is a share of every entity (ADR
    0017, the amendment of 2026-11-24). A plan without it was written by engine 0.11.1 or earlier,
    whose share was a share of the entities that can end, and it replays under that allocation, so
    the run id it made keeps its digests. Any other value comes from an engine this one does not
    know, and a plan that names one is refused rather than run under the allocation it was not made
    with.
    """
    if not _marker_named(
        plan_document, END_SHARE_PLAN_KEY, END_SHARE_OF_ENTITIES, f"takes {END_SHARE_OF_ELIGIBLE!r}"
    ):
        return END_SHARE_OF_ELIGIBLE
    return END_SHARE_OF_ENTITIES


def plan_date_ranges(plan_document: Mapping[str, Any]) -> bool:
    """Whether a recorded plan cuts its columns at their contract's ranges, or the refusal (``FLX-2102``).

    A plan with ``dateRanges`` says ``"cut"`` (ADR 0024, the amendment of 2026-11-25). A plan
    without it cut nothing: it was written before the cut, or it cut no column, and it replays with
    no cut, so the rows it made keep their bytes. Any other value is refused as ``historyEndShare``
    is.
    """
    return _marker_named(plan_document, DATE_RANGES_PLAN_KEY, DATE_RANGES_CUT, "cuts nothing")


def plan_scale_floors(plan_document: Mapping[str, Any]) -> bool:
    """Whether a recorded plan checks tracked levels and keeps its parents' keys, or the refusal (``FLX-2102``).

    A plan with ``scaleFloors`` says ``"trackedLevels"`` (ADR 0017, the amendment of 2026-11-25):
    it kept a drawn parent at the rows its children's tracked levels need. A plan without it was
    written before that, or needed no parent kept: it replays with neither the check nor the
    floor, so the rows it made keep their bytes. Any other value is refused as ``dateRanges`` is.
    """
    return _marker_named(
        plan_document, SCALE_FLOORS_PLAN_KEY, SCALE_FLOORS_TRACKED, "checks no tracked level"
    )


def plan_range_setters(plan_document: Mapping[str, Any]) -> bool:
    """Whether a recorded plan keeps its setters to the columns' ranges, or the refusal (``FLX-2102``).

    A plan with ``rangeSetters`` says ``"held"`` (ADR 0024, the amendment of 2026-11-25): a history,
    its projections and a periods entry keep to the contract's ranges on the columns they set. A
    plan without it replays as it was made. Any other value is refused as ``historyEndShare`` is.
    """
    return _marker_named(
        plan_document, RANGE_SETTERS_PLAN_KEY, RANGE_SETTERS_HELD, "holds no setter to a range"
    )


def plan_max_rows(plan_document: Mapping[str, Any]) -> Optional[int]:
    """The periodic rows' budget a recorded plan names, or the refusal (``FLX-2102``).

    A plan with ``maxRows`` was made with ``--max-rows`` (ADR 0027 section 5, the amendment of
    2026-11-25); a plan without it took the default. A run of a recorded plan makes no estimate,
    since the plan states its rows, so the value is read to check the plan. One that is not a
    count from 1 to ``MOST_ROWS`` is refused as an unknown ``historyEndShare`` is.
    """
    if MAX_ROWS_PLAN_KEY not in plan_document:
        return None
    named = plan_document[MAX_ROWS_PLAN_KEY]
    if not _is_count(named) or not 1 <= named <= MOST_ROWS:
        raise FluxError(
            FLX_2102,
            f"the recorded plan names {MAX_ROWS_PLAN_KEY} {_short_repr(named)}, which is not a "
            f"budget of rows from 1 to {MOST_ROWS}; plan again with this engine",
        )
    return named


def _marker_named(plan_document: Mapping[str, Any], key: str, known: str, without: str) -> bool:
    """Whether *plan_document* carries *key*, refusing (``FLX-2102``) a value other than *known*."""
    if key not in plan_document:
        return False
    named = plan_document[key]
    if named != known:
        raise FluxError(
            FLX_2102,
            f"the recorded plan names {key} {_short_repr(named)}, a value this engine does not "
            f"know (a plan written by a newer or incompatible version; it knows {known!r}, and a "
            f"plan without the key {without}); plan again with this engine",
        )
    return True


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def require_plan_shortfalls(plan_document: Mapping[str, Any]) -> None:
    """Refuse (``FLX-2102``) a recorded plan whose emit states a shortfall that does not hold together.

    ``plan`` writes ``declaredRows`` and ``shortfall`` on an emit only when a history holds fewer
    rows than it declares (:func:`flux_engine.stages.plan._shortfall_entry`), so an entry has both
    or neither; both are counts, the shortfall is above zero, and the declared rows less the rows
    the emit's generator writes are the shortfall. ``verify`` prints them as read, so a plan whose
    digest was recomputed after an edit is refused here, before anything reads them.
    """
    emits = plan_document.get("emits")
    if not isinstance(emits, Mapping):
        return
    for expose_id, entry in emits.items():
        if not isinstance(entry, Mapping) or not ({"declaredRows", "shortfall"} & set(entry)):
            continue
        why = _shortfall_fault(entry)
        if why is not None:
            raise FluxError(
                FLX_2102,
                f"the recorded plan's emit {_short_repr(expose_id)} states declaredRows "
                f"{_short_repr(entry.get('declaredRows'))} and shortfall "
                f"{_short_repr(entry.get('shortfall'))}, which do not hold together: {why}; the "
                "plan was edited or written by an incompatible version, so plan again with this "
                "engine",
            )


def _shortfall_fault(entry: Mapping[str, Any]) -> Optional[str]:
    """Why an emit entry's ``declaredRows`` and ``shortfall`` do not hold together, or ``None``."""
    if "declaredRows" not in entry or "shortfall" not in entry:
        return "it states one of declaredRows and shortfall without the other"
    declared, short = entry["declaredRows"], entry["shortfall"]
    if not _is_count(declared) or not _is_count(short):
        return "declaredRows and shortfall must both be whole numbers"
    if short <= 0:
        return "a shortfall must be above zero"
    generator = entry.get("generator")
    written = generator.get("rows") if isinstance(generator, Mapping) else None
    if not _is_count(written):
        return "its generator does not state the rows it writes"
    if declared - written != short:
        return f"declaredRows less the generator's rows {_short_repr(written)} is not the shortfall"
    return None


def require_run_digest_binds_plan(
    layout: RunLayout, plan_document: Mapping[str, Any], run_digest_document: Mapping[str, Any]
) -> None:
    """Refuse (``FLX-2113``) a ``run-digest.json`` that names a different plan than ``plan.json``.

    ``apply`` writes the plan's own ``planDigest`` into the run digest document,
    and the run digest is what the runtime statement names, so the chain from a
    plan's contents to a shipped statement runs through that one field. A run
    directory whose two files disagree about it was assembled from more than one
    run, and a statement over it would name a run digest whose plan is not the
    plan beside it.

    The same holds for ``kernelVersion``: ``apply`` writes the plan's kernel into
    the run digest document, and ``ship`` writes it into the evidence pack, so a
    run digest that names another kernel than its plan was not made from it.
    """
    recorded = run_digest_document.get("planDigest")
    planned = plan_document.get("planDigest")
    if recorded != planned:
        raise FluxError(
            FLX_2113,
            f"run-digest.json in {layout.run_dir} records planDigest "
            f"{printable(str(recorded))}, but the plan there is {printable(str(planned))}",
        )
    recorded_kernel = run_digest_document.get("kernelVersion")
    planned_kernel = plan_document.get("kernelVersion")
    if recorded_kernel != planned_kernel:
        raise FluxError(
            FLX_2113,
            f"run-digest.json in {layout.run_dir} records kernelVersion "
            f"{printable(str(recorded_kernel))}, but the plan there names "
            f"{printable(str(planned_kernel))}",
        )


def _record_the_plan_does_not_name(
    layout: RunLayout, plan_document: Mapping[str, Any], provenance_text: str
) -> Optional[StageResult]:
    """The refusal (``FLX-2115``) for a record this apply would write that the plan does not name.

    The plan holds a digest of the text its own execution gave, and ``ship`` reads
    the file back only through it. A record that hashes to something else is
    refused here, before a row is written, and not left for ``verify`` and
    ``ship`` to refuse at the end of a run that looked complete. A plan that holds
    no digest was written before it carried one and binds nothing.
    """
    planned = plan_document.get(PROVENANCE_DIGEST_KEY)
    if planned is None:
        return None
    produced = provenance_digest(provenance_text.encode("utf-8")).value
    return _unnamed_sidecar(planned, produced, ColumnProvenanceMoved, layout.column_provenance_path)


def _unnamed_sidecar(
    planned: Any, produced: Optional[str], moved: Any, path: Any
) -> Optional[StageResult]:
    """``None`` when the record this apply would write is the one the plan names, else the refusal.

    *planned* is the plan's digest of the record, or ``None`` when the plan holds
    none; *produced* is the digest of the record this apply would write, or ``None``
    when it would write none; *moved* is the sidecar's ``FLX-21xx`` error.
    """
    if planned is not None and produced == str(planned):
        return None
    drifted = moved(
        path=path,
        recorded=printable(str(planned)) if planned is not None else None,
        recomputed=produced,
        at_apply=True,
    )
    return StageResult(
        stage="apply",
        exit_code=1,
        _artifacts={"code": drifted.code.code},
        stderr=str(drifted),
        error_code=drifted.code.code,
    )


def _register_the_plan_does_not_name(
    layout: RunLayout, plan_document: Mapping[str, Any], register_text: Optional[str]
) -> Optional[StageResult]:
    """The refusal (``FLX-2116``) for a parameter register the plan does not name.

    The register's counterpart of :func:`_record_the_plan_does_not_name`, with
    one more case, because its digest is in the plan only when the Simulation
    declares a register: a register this apply would write that the plan holds no
    digest of, and a plan that holds a digest this apply has no register for.
    Both are refused before a row is written. No register and no digest is a run
    whose Simulation declares none, and writes none.
    """
    planned = plan_document.get(REGISTER_DIGEST_KEY)
    if planned is None and register_text is None:
        return None
    produced = (
        register_digest(register_text.encode("utf-8")).value if register_text is not None else None
    )
    return _unnamed_sidecar(
        planned, produced, ParameterRegisterMoved, layout.parameter_register_path
    )


#: Why ``apply --run-id`` refuses ``--max-rows``: the plan states the rows it derives.
_MAX_ROWS_NEEDS_A_FRESH_PLAN = (
    "--max-rows is read only when apply takes a fresh plan: the plan of --run-id states the rows "
    "it derives, and is applied with no estimate"
)
#: Why ``apply --run-id`` refuses ``--scale``: the plan records the size it was made at.
_SCALE_NEEDS_A_FRESH_PLAN = (
    "--scale is read only when apply takes a fresh plan: the plan of --run-id records the "
    "scale it was made at, and apply runs that"
)

#: Why ``apply --run-id`` refuses ``--profile`` without ``--sink``: a recorded plan was paired
#: with the profile its Simulation pins when the plan was taken. With ``--sink`` the profile is
#: read to grade the run for the sink's audience.
_PROFILE_NEEDS_A_FRESH_PLAN = (
    "--profile is read only when apply takes a fresh plan, or with --sink: the plan of "
    "--run-id was paired with the profile its Simulation pins when it was taken"
)


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
    strict_coverage: bool = False,
    profile: Union[str, Path, None] = None,
    use_stage: bool = True,
    scale: Any = None,
    shard: Any = None,
    timings: bool = False,
    sink: Optional[str] = None,
    sink_options: Optional[Mapping[str, str]] = None,
    allow_generators: Sequence[str] = (),
    max_rows: Optional[int] = None,
) -> StageResult:
    """Apply the plan at ``<state_dir>/runs/<run_id>/plan.json``.

    *use_stage* commits the execution ``plan`` staged for *run_id* when there is one
    (:mod:`flux_engine.stages.staging`); ``replay`` passes ``False``, because it exists to
    execute the plan again.

    *scale* sizes the fresh plan (:mod:`flux_engine.stages.scaling`); with *run_id* it is
    refused, because the plan records its own, and so is *max_rows*, the fresh plan's periodic
    rows' budget (:func:`flux_engine.stages.plan.plan`), since a recorded plan states its rows. *shard* is ``(I, N)`` or ``"I/N"``: only
    that share of every generated emit's rows is made and written, as
    ``emits/<exposeId>.shard-I-of-N.ndjson`` beside ``shard-I-of-N.json``, which joined in
    order are the bytes of the run's own emit streams. A shard writes no run digest and no
    receipt: it is a part of a run, not a run. *timings* adds where the time went to the
    output and the ``timings`` artifact.

    *sink* (``NAME:TARGET``, with *sink_options*) also writes every stream of the applied run
    to that sink, from the files of the run directory, and records its receipts in
    ``<run>/sink-receipts.json`` and under the ``sinks`` artifact
    (:func:`flux_engine.stages.deliver.deliver_run`). The sink is resolved before anything is
    generated, so a sink set up wrong stops the call before any work; a sink that fails
    leaves the run applied. A shard is part of a run, so it has no sink.

    *strict_coverage* is :func:`flux_engine.stages.pipeline.execute`'s: a
    column that fell back to a type default is refused (``FLX-3304``) in
    whichever execution this call makes. It has no effect on a
    *precomputed* execution, which was already accepted or refused when it
    was made.

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

    *profile* goes to the fresh plan, which pairs it with the pinned profile
    (:func:`flux_engine.stages.plan.plan`); with *run_id* and no *sink* it is refused, exit 2.
    With *sink* it is also the profile the sink's audience is graded from
    (:func:`flux_engine.stages.deliver.privacy_grade_of`).

    *allow_generators* names the generator plugins the person allows to load
    (``--allow-generator``, ADR 0032 section 3). It is read only when this call generates rows
    (a fresh plan, a plan with nothing staged, ``replay``): committing a staged execution imports
    nothing. A plan that records generator plugins is never generated again by ``apply --run-id``
    (ADR 0032 section 6): the run is committed from its stage, and a missing or mismatched stage,
    another *simulation_id* or *zone* than the stage was made with, *strict_coverage*, or a *shard*
    (the stage holds the whole run) is refused (``FLX-3123``).
    """
    if sink is None and sink_options:
        return StageResult(stage="apply", exit_code=2, stderr="--sink-opt needs --sink")
    if sink is not None and shard is not None:
        return StageResult(
            stage="apply",
            exit_code=2,
            stderr="--sink takes a whole run: a share is only part of one, and shares joined "
            "are not a run, so apply the run whole, without --shard "
            "(`fluxsim apply --run-id RUN_ID --sink ...`)",
        )
    target, refused = open_sink(sink, sink_options, stage="apply")
    if refused is not None:
        return refused
    # With a recorded plan the profile is for the sink alone, so `_apply` is not handed it.
    planned_with = None if run_id is not None and target is not None else profile
    return timed(
        timings,
        lambda: _delivered(
            target,
            Path(bundle_directory),
            state_dir,
            profile,
            _apply(
                bundle_directory,
                run_id=run_id,
                simulation_id=simulation_id,
                state_dir=state_dir,
                zone=zone,
                auto_plan=auto_plan,
                precomputed=precomputed,
                jobs=jobs,
                strict_coverage=strict_coverage,
                profile=planned_with,
                use_stage=use_stage,
                scale=scale,
                shard=shard,
                allow_generators=allow_generators,
                max_rows=max_rows,
            ),
        ),
    )


def _delivered(
    target: Any,
    directory: Path,
    state_dir: Union[str, Path, None],
    profile: Union[str, Path, None],
    applied: StageResult,
) -> StageResult:
    """*applied*, then every stream of its run into *target* when one was asked for."""
    if target is None or not applied.success:
        return applied
    from flux_engine.stages.deliver import deliver_run, receipt_line, write_receipts

    layout = RunLayout.for_state_dir(
        Path(state_dir) if state_dir is not None else Path(".flux"), applied.artifacts["run_id"]
    )
    artifacts: Dict[str, Any] = dict(applied.artifacts)
    try:
        layout.require_real_dirs()
        receipts = deliver_run(
            layout,
            directory,
            target=target,
            record=lambda receipt: write_receipts(layout, [receipt]),
            profile=profile,
        )
    except FluxError as error:
        artifacts["sinks"] = {"ran": True, "ok": False, "sink": target.name}
        return StageResult(
            stage="apply",
            exit_code=1,
            _artifacts=artifacts,
            stdout=applied.stdout,
            stderr=str(error),
            error_code=error.code.code,
        )
    artifacts["sinks"] = {
        "ran": True,
        "ok": True,
        "sink": target.name,
        "receipts": [receipt.as_document() for receipt in receipts],
        "receiptsPath": str(layout.sink_receipts_path),
    }
    return StageResult(
        stage="apply",
        exit_code=0,
        _artifacts=artifacts,
        stdout="\n".join([applied.stdout, *(receipt_line(receipt) for receipt in receipts)]),
    )


def _apply(
    bundle_directory: Union[str, Path],
    *,
    run_id: Optional[str],
    simulation_id: Optional[str],
    state_dir: Union[str, Path, None],
    zone: Optional[str],
    auto_plan: bool,
    precomputed: Optional["_pipeline.PipelineResult"],
    jobs: int,
    strict_coverage: bool,
    profile: Union[str, Path, None],
    use_stage: bool,
    scale: Any,
    shard: Any,
    allow_generators: Sequence[str],
    max_rows: Optional[int] = None,
) -> StageResult:
    if run_id is not None and profile is not None:
        return StageResult(stage="apply", exit_code=2, stderr=_PROFILE_NEEDS_A_FRESH_PLAN)
    if run_id is not None and scale is not None:
        return StageResult(stage="apply", exit_code=2, stderr=_SCALE_NEEDS_A_FRESH_PLAN)
    if run_id is not None and max_rows is not None:
        return StageResult(stage="apply", exit_code=2, stderr=_MAX_ROWS_NEEDS_A_FRESH_PLAN)
    try:
        shares = None if shard is None else parse_shard(shard)
        optional_scale(scale)  # refused here when it names no usable size
    except FluxError as error:
        return StageResult(
            stage="apply",
            exit_code=2,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )
    directory = Path(bundle_directory)
    state = Path(state_dir) if state_dir is not None else Path(".flux")

    layout: Optional[RunLayout] = None
    if run_id is not None:
        layout = RunLayout.for_state_dir(state, run_id)
    elif auto_plan:
        planned, planned_result = run_plan_with_result(
            directory,
            simulation_id=simulation_id,
            state_dir=state,
            zone=zone,
            jobs=jobs,
            strict_coverage=strict_coverage,
            profile=profile,
            scale=scale,
            shard=shares,
            allow_generators=allow_generators,
            reuse_committed=shares is None,
            max_rows=max_rows,
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
        layout.require_real_dirs()
        plan_document = load_recorded_plan(layout)
        kernel = plan_kernel(plan_document)
    except FluxError as error:
        return StageResult(
            stage="apply",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )

    # Whether the run's plugin bytes are stored is the bundle's declaration, never plan.json's
    # record, which a person who can write the state directory can rewrite (ADR 0032 section 6).
    # A committed run is looked at too, whatever execution this call holds: the bytes it holds are
    # the record, and a call that was handed rows may not replace them.
    holds_run = is_regular_file(layout.receipt_path)
    stored = False
    if precomputed is None or holds_run:
        try:
            declared = declared_for_run(directory, plan_document, kernel, simulation_id)
            if declared is not None:
                require_record(declared, plan_document)
                stored = bool(declared)
        except FluxError as error:
            return _failed(_named_for_run(error, layout, run_id))

    refused_share = _refuse_share_of_stored_run(layout, stored, run_id, shares)
    if refused_share is not None:
        return refused_share

    if stored and holds_run and shares is None:
        # Its plugin bytes are stored and the run is committed: kept as it stands, never asked
        # for again and never written over, whatever call reached here (ADR 0032 section 6).
        if strict_coverage:
            return _failed(
                FluxError(
                    FLX_3123,
                    "--strict-coverage asks for the rows to be generated again, and the bytes "
                    "of a bundle that declares generator plugins are stored, never generated "
                    "again; apply without --strict-coverage, or plan with it",
                )
            )
        return _keep_committed(layout, plan_document)

    stored_only = stored and precomputed is None and use_stage and shares is None
    if stored_only and strict_coverage:
        return _failed(
            FluxError(
                FLX_3123,
                "--strict-coverage asks for the rows to be generated again, and the bytes of a "
                "bundle that declares generator plugins are stored, never generated again; "
                "apply without --strict-coverage, or plan with it",
            )
        )
    if (
        run_id is not None
        and precomputed is None
        and not strict_coverage
        and use_stage
        and shares is None
    ):
        committed = _commit_staged(
            directory, layout, plan_document, kernel, simulation_id=simulation_id, zone=zone
        )
        if committed is not None:
            return committed
        if stored_only:
            return _failed(
                FluxError(
                    FLX_3123,
                    f"run {layout.run_id} has no stage made for this plan, simulation id "
                    f"{simulation_id!r} and zone {zone!r}, and the bytes of a bundle that "
                    "declares generator plugins are stored, never generated again; run "
                    "`fluxsim plan` again without --no-stage, and apply with the "
                    "--simulation-id and --zone it was planned with",
                )
            )

    if precomputed is not None:
        result = precomputed
        if result.kernel != kernel:
            # The inputDigest comparison below would refuse it too, since the
            # kernel's versions are inside the digest, but as a moved bundle.
            mismatched = FluxError(
                FLX_2101,
                f"the execution apply was handed ran kernel {result.kernel.version}, "
                f"but the plan names kernel {kernel.version}",
            )
            return StageResult(
                stage="apply",
                exit_code=1,
                _artifacts={"code": mismatched.code.code},
                stderr=str(mismatched),
                error_code=mismatched.code.code,
            )
    else:
        try:
            result = _pipeline.execute(
                directory,
                simulation_id=simulation_id,
                zone=zone,
                jobs=jobs,
                strict_coverage=strict_coverage,
                kernel=kernel,
                scale=optional_scale(plan_document.get("scale")),
                era=_pipeline.PlanEra(
                    end_share=plan_end_share(plan_document),
                    cut_ranges=plan_date_ranges(plan_document),
                    tracked_levels=plan_scale_floors(plan_document),
                    periodic_rows=plan_max_rows(plan_document),
                    hold_ranges=plan_range_setters(plan_document),
                    estimate_rows=False,
                ),
                shard=shares,
                references=plan_document.get(REFERENCES_PLAN_KEY),
                allow_generators=allow_generators,
                state_dir=state,
                responses=plan_document.get(RESPONSES_PLAN_KEY),
                replicate=(
                    Replicate.from_document(
                        plan_document[REPLICATE_PLAN_KEY], where=plan_document["simulationId"]
                    )
                    if REPLICATE_PLAN_KEY in plan_document
                    else None
                ),
            )
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
        moved_error = _named_for_run(
            InputDigestMoved(planned=planned_input_digest, current=current_input_digest),
            layout,
            run_id,
        )
        return StageResult(
            stage="apply",
            exit_code=1,
            _artifacts={"code": moved_error.code.code},
            stderr=str(moved_error),
            error_code=moved_error.code.code,
        )

    # Governance (ADR 0020): the minimum grade a column's sensitivity sets is checked here too,
    # against the grade the plan records, so a plan from an engine that did not check, an
    # `apply --run-id` and a `replay` meet it as a fresh plan does.
    try:
        require_minimum_grade(
            with_generators(
                recorded_grade(
                    plan_document,
                    pinned=profile_reference(
                        result.cast.simulation, where=result.cast.simulation_id
                    )
                    is not None,
                ),
                result.cast.simulation,
                where=result.cast.simulation_id,
            ),
            [outcome.resolved for outcome in result.emits],
            result.cast.simulation,
            where=result.cast.simulation_id,
        )
    except FluxError as error:
        return StageResult(
            stage="apply",
            exit_code=1,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )

    if shares is not None:
        return _write_share(layout, plan_document, result, shares)

    provenance_text = column_provenance_text(result)
    unnamed = _record_the_plan_does_not_name(layout, plan_document, provenance_text)
    if unnamed is not None:
        return unnamed
    register_text = parameter_register_text(result)
    unnamed = _register_the_plan_does_not_name(layout, plan_document, register_text)
    if unnamed is not None:
        return unnamed

    run_digest_doc, receipt = write_run(
        layout, directory, plan_document, result, provenance_text, register_text
    )

    # A stage of this plan the apply above did not use (a plan made for another zone, or
    # a strict apply) is not a run's to keep: the run directory now holds the run.
    discard_stage(layout)

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
                _emit_summary_line(result, optional_scale(plan_document.get("scale"))),
            ]
        ),
    )


def write_run(
    layout: RunLayout,
    directory: Path,
    plan_document: Mapping[str, Any],
    result: "_pipeline.PipelineResult",
    provenance_text: str,
    register_text: Optional[str],
) -> Tuple[Any, Dict[str, Any]]:
    """Write every artifact of one run under *layout*; return the run digest and the receipt.

    The one writer of a run directory: ``apply`` calls it over the run's own
    layout, and a plan that stages its execution calls it over the staging
    layout (:mod:`flux_engine.stages.staging`), so the staged bytes are the
    bytes ``apply`` would have written. The receipt, the run's commit marker,
    is written last.
    """
    layout.ensure()
    trace = result.trace
    _write_ndjson(layout.population_path, _ledger(trace, POPULATION_LEDGER_COMPONENTS))
    _write_ndjson(layout.events_path, _ledger(trace, EVENTS_LEDGER_COMPONENTS))
    _write_ndjson(layout.suppressions_path, trace.of(TraceOp.SUPPRESS))
    result.decisions.write(layout.run_dir)
    lap("write ledgers")

    for outcome in result.emits:
        emit_path = layout.emit_path(outcome.resolved.expose_id)
        write_bytes_atomic(emit_path, outcome.writer.stream_bytes())
        lap("write emits")

        generator_digest = outcome.plan.digest() if outcome.plan is not None else None
        attestation = build_seam_attestation(
            outcome.resolved, outcome.expose, outcome.result, generator_digest=generator_digest
        )
        attestation_path = layout.seam_attestation_path(outcome.resolved.expose_id)
        write_text_atomic(
            attestation_path, json.dumps(attestation.as_document(), indent=2, sort_keys=True) + "\n"
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
            write_text_atomic(
                fidelity_input_path,
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
            )
        lap("attestation")

    # Written before the receipt, the run's commit marker, and rewritten whole on
    # every apply like the files above it, through a staged file and one rename, so a
    # link planted at this name is replaced rather than written through.
    atomic_write_text(layout.column_provenance_path, provenance_text)
    if register_text is not None:
        atomic_write_text(layout.parameter_register_path, register_text)

    plan_digest_ref = DigestRef(
        value=plan_document["planDigest"], domain=DigestDomain.JCS_DOCUMENT, subject="planDigest"
    )
    run_digest_doc = build_run_digest_document(
        kernel_version=result.kernel.version,
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
        "kernelVersion": result.kernel.version,
        "seedTreeVersion": result.kernel.seed_tree_version,
        "traceVersion": result.kernel.trace_version,
        "engineVersion": _engine_version,
        "fidelity": result.fidelity,
        "seed": result.seed,
        "inputDigest": result.input_digest.ref.value,
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
                # What each row of a date fact came to (ADR 0024 section 7, ADR 0025 section 2),
                # only for an emit whose facts carry a target above zero or whose dates have
                # seasonal rates, so a run without either keeps its receipt.
                **({"dates": outcome.dates.as_document()} if outcome.dates is not None else {}),
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
    lap("write run files")
    return run_digest_doc, receipt


def _write_share(
    layout: RunLayout,
    plan_document: Mapping[str, Any],
    result: "_pipeline.PipelineResult",
    shard: Tuple[int, int],
) -> StageResult:
    """Write one share of every emit, and the record of what it holds. No receipt."""
    from flux_engine.core.digest import digest_of_bytes
    from flux_engine.stages.scaling import share_of

    index, count = shard
    layout.ensure()
    entries: List[Dict[str, Any]] = []
    for outcome in result.emits:
        data = outcome.writer.stream_bytes()
        write_bytes_atomic(layout.shard_emit_path(outcome.resolved.expose_id, index, count), data)
        total = outcome.plan.rows if outcome.plan is not None else None
        start, stop = share_of(total, shard) if total is not None else (0, data.count(b"\n"))
        entries.append(
            {
                "exposeId": outcome.resolved.expose_id,
                "rows": data.count(b"\n"),
                "firstRow": start,
                "endRow": start + data.count(b"\n") if total is None else stop,
                "totalRows": total,
                "bytes": len(data),
                "sha256": digest_of_bytes(data, subject="shardFile").value,
            }
        )
    record = {
        "version": "flux-shard/1",
        "runId": layout.run_id,
        "planDigest": plan_document["planDigest"],
        "inputDigest": plan_document["inputDigest"],
        "shard": {"index": index, "of": count},
        "emits": entries,
    }
    atomic_write_text(
        layout.shard_manifest_path(index, count),
        json.dumps(record, indent=2, sort_keys=True) + "\n",
    )
    parts = "; ".join(f"{entry['exposeId']}: {entry['rows']} rows" for entry in entries)
    return StageResult(
        stage="apply",
        exit_code=0,
        _artifacts={
            "run_id": layout.run_id,
            "run_dir": str(layout.run_dir),
            "shard": {"index": index, "of": count},
            "emits": entries,
        },
        stdout=(
            f"wrote share {index} of {count} of run {layout.run_id} to {layout.run_dir} "
            f"(this share: {parts}); "
            "the shares joined in order are the run's emit streams, and no receipt is written"
        ),
    )


def _failed(error: FluxError) -> StageResult:
    return StageResult(
        stage="apply",
        exit_code=1,
        _artifacts={"code": error.code.code},
        stderr=str(error),
        error_code=error.code.code,
    )


def _named_for_run(error: FluxError, layout: RunLayout, run_id: Optional[str]) -> FluxError:
    """*error*, said for ``apply --run-id`` when it is the moved bundle (``FLX-2101``).

    The run is named and the edit is said to have come after it was made, when the run is committed
    (its receipt exists); any other error, a staged plan's included, and a call without
    ``--run-id``, is returned as it is.
    """
    if (
        run_id is None
        or not isinstance(error, InputDigestMoved)
        or not is_regular_file(layout.receipt_path)
    ):
        return error
    return FluxError(
        error.code,
        f"plan recorded {error.planned}, bundle is now {error.current}\n"
        f"run {layout.run_id} was made from the bundle as the plan recorded it: the bundle "
        "changed after the run was made",
    )


def _keep_committed(layout: RunLayout, plan_document: Mapping[str, Any]) -> StageResult:
    """The committed run of a plan whose plugin bytes are stored, kept as it stands.

    Nothing is generated and nothing is written. The run's files are first checked against its
    receipt, the way ``ship`` checks them (the components ``run-digest.json`` records, and the
    run digest the receipt records), so a run whose stored bytes moved is refused (``FLX-2110``)
    and never made again: those bytes are the only record there is.
    """
    from flux_engine.stages.verify import component_mismatches

    remedy = (
        "restore the run directory from a copy or from the pack that shipped it, or start a new "
        "run (for example with another seed); its plugin output is stored and never generated again"
    )
    if not is_regular_file(layout.run_digest_path):
        # What verify and ship say of a run directory without it: there is nothing to check the
        # stored bytes against, so they are neither kept as checked nor made again.
        return _failed(
            FluxError(
                FLX_2109,
                f"run {layout.run_id} holds stored plugin output but {layout.run_digest_path} is "
                f"missing, so its files cannot be checked; it was left as it is and is never "
                f"generated again; {remedy}",
            )
        )
    try:
        receipt = read_json_object(layout.receipt_path, code=FLX_2109, remedy=remedy)
        run_digest_document = read_json_object(layout.run_digest_path, code=FLX_2110, remedy=remedy)
        require_run_digest_binds_plan(layout, plan_document, run_digest_document)
        moved = list(
            component_mismatches(layout, receipt, run_digest_document.get("components", {}))
        )
        recorded = digest_of_document(run_digest_document, subject="runDigest").value
        if recorded != receipt.get("runDigest"):
            moved.append("run-digest.json")
    except FluxError as error:
        return _failed(error)
    if moved:
        return _failed(
            FluxError(
                FLX_2110,
                f"run {layout.run_id} holds stored plugin output that its receipt no longer "
                f"matches ({', '.join(moved)}); it was left as it is and is never generated "
                f"again; {remedy}",
            )
        )
    run_digest = str(receipt["runDigest"])
    return StageResult(
        stage="apply",
        exit_code=0,
        _artifacts={
            "run_id": layout.run_id,
            "run_dir": str(layout.run_dir),
            "run_digest": run_digest,
            "receipt": receipt,
            "kept_stored_run": True,
        },
        stdout=(
            f"run {layout.run_id} keeps its stored plugin output (runDigest={run_digest}): it is "
            "never generated again, and a new sample needs a new run, for example another seed"
        ),
    )


def _refuse_share_of_stored_run(
    layout: RunLayout,
    stored: bool,
    run_id: Optional[str],
    shares: Optional[Tuple[int, int]],
) -> Optional[StageResult]:
    """``FLX-3123`` for ``apply --run-id --shard`` of a run whose bundle declares generator plugins.

    The stage holds the whole run, and a share would ask the plugin for its rows again.
    """
    if run_id is None or shares is None or not stored:
        return None
    return _failed(
        FluxError(
            FLX_3123,
            f"--shard asks for a share of run {layout.run_id} to be generated, and the bytes "
            "of a bundle that declares generator plugins are stored, never generated "
            "again; apply the run whole, without --shard",
        )
    )


def _commit_staged(
    directory: Path,
    layout: RunLayout,
    plan_document: Mapping[str, Any],
    kernel: Kernel,
    *,
    simulation_id: Optional[str],
    zone: Optional[str],
) -> Optional[StageResult]:
    """Commit the execution ``plan`` staged, without generating a row; ``None`` when none is usable.

    Three checks come before a byte is written to the run directory. The stage is
    the one of this run (its directory is keyed by the plan's digest, and its record
    names the plan and the input digest). The bundle's bytes are the ones the plan
    hashed: ``inputDigest`` is worked out again from the bundle, as an apply that
    generates does (``FLX-2101``). And every staged file is the file the record
    lists (:func:`flux_engine.stages.staging.read_stage`, ``FLX-2120``), the two
    records the plan binds included (``FLX-2115``, ``FLX-2116``).
    """
    try:
        staged = read_stage(layout, plan_document, simulation_id=simulation_id, zone=zone)
        if staged is None:
            return None
        lap("check stage")
        loaded = load_bundle(directory)
        current = _pipeline.input_digest_of(loaded, kernel=kernel).ref.value
        planned = plan_document["inputDigest"]
        if current != planned:
            raise InputDigestMoved(planned=planned, current=current)
        # Governance (ADR 0020): a staged commit generates nothing, so the minimum grade a column's
        # sensitivity sets is read here, from the recorded grade, before a byte is committed.
        require_recorded_minimum(loaded, plan_document, simulation_id=simulation_id)
        for key, digest, moved, path_of in (
            (PROVENANCE_DIGEST_KEY, provenance_digest, ColumnProvenanceMoved, "column-provenance"),
            (REGISTER_DIGEST_KEY, register_digest, ParameterRegisterMoved, "parameter-register"),
        ):
            data = staged.file(f"{path_of}.json")
            produced = digest(data).value if data is not None else None
            if plan_document.get(key) is None and (
                produced is None or key == PROVENANCE_DIGEST_KEY
            ):
                continue  # the plan binds no such record, as `_record_the_plan_does_not_name` reads it
            unnamed = _unnamed_sidecar(
                plan_document.get(key),
                produced,
                moved,
                layout.run_dir / f"{path_of}.json",
            )
            if unnamed is not None:
                return unnamed
        lap("check bundle")
        commit_stage(layout, staged)
        lap("commit")
    except FluxError as error:
        return _failed(error)
    return StageResult(
        stage="apply",
        exit_code=0,
        _artifacts={
            "run_id": layout.run_id,
            "run_dir": str(layout.run_dir),
            "run_digest": staged.run_digest,
            "receipt": dict(staged.receipt),
            "committed_from_stage": True,
        },
        stdout="\n".join(
            [
                f"applied to {layout.run_dir} (runDigest={staged.run_digest})",
                staged.summary_line(optional_scale(plan_document.get("scale"))),
            ]
        ),
    )


def _emit_summary_line(result: "_pipeline.PipelineResult", scale: Any = None) -> str:
    """One line naming every emit, its row count and where the rows came from.

    A run's headline number. It is printed rather than only written into
    the receipt because "how many rows did this produce" is the first
    question anyone asks of a simulation, and a stream that silently
    produced none is exactly the failure this line makes impossible to
    miss. Emits are named in declared order (KERNEL-SPEC §3's ``emits[]``
    order), so the line is stable across runs. An emit made by more than one
    process says how many, so ``--jobs`` shows whether it took effect; one
    process, the common case, adds nothing to the line. When the plan's *scale*
    is not one, an emit built from the run's events says the scale leaves it as it is.
    """
    if not result.emits:
        return "no emits declared"
    parts = []
    for outcome in result.emits:
        source = scenario_scale_note(outcome.source, scale)
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
    write_bytes_atomic(layout.lineage_path, b"")
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    facets = {
        "flux.determinism": determinism_facet(
            kernel_version=result.kernel.version,
            seed_tree_version=result.kernel.seed_tree_version,
            trace_version=result.kernel.trace_version,
            input_digest=result.input_digest.ref,
            plan_digest=plan_digest,
            run_digest=run_digest,
        ),
        "flux.governance": governance_facet(decisions_digest=result.decisions.decisions_digest()),
    }
    outputs = tuple(
        dataset_for_expose(outcome.resolved.contract_document, outcome.resolved.expose_id)
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
