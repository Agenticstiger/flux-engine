# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The one reproducible execution core every stage in this package builds on.

:func:`execute` walks a bundle exactly the way KERNEL-SPEC describes it:
load and validate (§2), resolve the seam and derive the seed-tree root
(§3, §8), materialise the population (§5, world/), run signals, detectors,
journeys, campaigns and experiments in scheduler order (§6, §7,
runtime/), generate and emit every declared row stream (SEAM-FIDELITY-
SPEC §1, generator/ + seam/emit.py), and fold everything traced into the
five component digests plus the run digest (§10). It is a pure function
of the bundle directory and the caller's own parameters -- nothing here
touches a clock, a random source, or the filesystem outside the bundle it
was given; where the run's own artifacts are written to disk is entirely
the callers' business (:mod:`flux_engine.stages.apply`).

``validate``, ``plan``, ``apply`` and ``run`` all call :func:`execute`
once each; they differ only in which parts of the result they keep, and
in whether they insist the bundle still hashes to a previously recorded
digest first. Executing the pipeline twice on an unchanged bundle and
seed always produces byte-identical traces, emit streams and digests --
that is KERNEL-SPEC's determinism promise, and every one of those four
stages leans on it rather than re-deriving anything.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import localcontext
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.clock import Phase, Scheduler, VirtualClock
from flux_engine.core.digest import DigestedDocument, build_input_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.rng import clear_memos as clear_draw_memos
from flux_engine.core.rng import kernel_context
from flux_engine.core.seedtree import SeedTree, SeedTreeRoot, derive_root, effective_seed
from flux_engine.core.trace import RunComponents
from flux_engine.expr.attrs import AttributeModel, build_attribute_model
from flux_engine.generator import GeneratorPlan, build_plan, generate_rows
from flux_engine.generator.rows import clear_memos as clear_generator_memos
from flux_engine.governance.audit import DecisionAuditLog
from flux_engine.governance.skills import agentic_extension_available
from flux_engine.governance.sovereignty import (
    SovereigntyAuditRow,
    SovereigntyPolicy,
    enforce_sovereignty,
)
from flux_engine.runtime import (
    ArmCounts,
    Cast,
    CompiledExperiment,
    DeliveryLedger,
    Detection,
    SignalEvent,
    Transition,
    arm_counts,
    assign_variants,
    compile_campaign,
    compile_detectors,
    compile_experiments,
    compile_journeys,
    emit_signals,
    requests_from_transitions,
    resolve_cast,
    run_campaign,
    run_detectors,
    run_journeys,
)
from flux_engine.seam.contract import CompiledExpose
from flux_engine.seam.emit import (
    EmitResult,
    EmitWriter,
    SeamDigests,
    seam_digests,
    seam_entropy_bytes,
)
from flux_engine.seam.mapping import ColumnMapping, resolve_column_mapping, source_namespace
from flux_engine.seam.resolve import LoadedBundle, ResolvedEmit, load_bundle, resolve_all_emits
from flux_engine.seam.sinks import NullSink
from flux_engine.stages._parallel import generate_framed, require_jobs, workers_for
from flux_engine.verticals import emit_aliases, get_pack
from flux_engine.world import PopulationSpec, SubjectStore, TraceLog, materialise_population

__all__ = [
    "KERNEL_VERSION",
    "SEED_TREE_VERSION",
    "TRACE_VERSION",
    "EmitOutcome",
    "PipelineResult",
    "execute",
    "input_digest_of",
    "require_supported_fidelity",
]

#: This engine's normative-behaviour version (KERNEL-SPEC §1). Distinct
#: from ``flux_engine.__version__`` (the package version, informational
#: only, never hashed) -- a bump here is a deliberate golden move.
KERNEL_VERSION = "0.1.0"
SEED_TREE_VERSION = "flux-seed/1"
TRACE_VERSION = "flux-trace/1"


@dataclass(frozen=True)
class _BundleView:
    """Adapts :class:`LoadedBundle`'s exact documents to :class:`BundleLike`."""

    flux_docs: Mapping[str, Any]


@dataclass(frozen=True)
class EmitOutcome:
    """One resolved emit's rows, before they are written anywhere.

    **Two sources, and the declaration picks.** An emit whose extension
    block declares a fidelity block with column statistics is drawn by the
    contract-schema generator (SEAM-FIDELITY-SPEC §1): ``plan`` is that
    :class:`~flux_engine.generator.GeneratorPlan`, and the declared
    statistics decide every value. An emit that declares none is
    **scenario-driven**: ``plan`` is ``None``, ``mapping`` says which
    runtime attribute fills which column, and the rows are the run's own
    moments -- one detector firing for one subject is one row (see
    :mod:`flux_engine.seam.mapping` for why the firing, and not the
    journey transition, is the moment).

    Declared statistics win where both could apply, because they are the
    stronger claim: the fidelity check is specified over them, and a
    scenario row that quietly replaced a drawn one would leave a record
    describing a dataset nobody produced.

    ADR-0003 holds either way: the generator reads aggregates and the
    scenario path reads the simulation's own state. Neither reads a record.
    """

    resolved: ResolvedEmit
    expose: CompiledExpose
    digests: SeamDigests
    plan: Optional[GeneratorPlan]
    rows: Tuple[Tuple[Any, ...], ...]
    result: EmitResult
    writer: EmitWriter
    mapping: Optional[ColumnMapping] = None

    @property
    def source(self) -> str:
        """Where this emit's rows came from: ``generator`` or ``scenario``."""
        return "generator" if self.plan is not None else "scenario"


@dataclass(frozen=True)
class PipelineResult:
    """Everything one execution of the six-step loop's core produced.

    Nothing here has been written to disk -- :mod:`flux_engine.stages.apply`
    and :mod:`flux_engine.stages.ship` decide what of this to persist and
    where. Each :class:`EmitOutcome` already holds its rendered rows, so a
    caller writes them without re-rendering.
    """

    loaded: LoadedBundle
    cast: Cast
    fidelity: str
    seed: int
    root: SeedTreeRoot
    seed_tree: SeedTree
    trace: TraceLog
    population: SubjectStore
    events: Tuple[SignalEvent, ...]
    detections: Tuple[Detection, ...]
    transitions: Tuple[Transition, ...]
    ledger: Optional[DeliveryLedger]
    experiment: Optional[CompiledExperiment]
    assignment: Mapping[int, str]
    arm_counts: Tuple[ArmCounts, ...]
    emits: Tuple[EmitOutcome, ...]
    decisions: DecisionAuditLog
    sovereignty_audit: Tuple[SovereigntyAuditRow, ...]
    input_digest: DigestedDocument
    files_not_hashed: Tuple[str, ...]
    components: RunComponents

    def emit(self, expose_id: str) -> EmitOutcome:
        for outcome in self.emits:
            if outcome.resolved.expose_id == expose_id:
                return outcome
        raise KeyError(expose_id)


def input_digest_of(loaded: LoadedBundle) -> DigestedDocument:
    """Build KERNEL-SPEC §11's ``inputDigest`` over a loaded bundle's files.

    The one place the engine decides which of a bundle's files an
    ``inputDigest`` covers. :func:`execute` calls it while running and
    :func:`flux_engine.stages.verify.recompute_input_digest` calls it
    while checking, so apply time and verify time cannot come to hold a
    second opinion about which files are inputs. That is not a
    theoretical worry: the two sites used to carry byte-identical
    filtering with no shared helper, and a fix applied to one of them
    would have reported a moved input digest on every run.

    The selection itself is
    :meth:`~flux_engine.seam.resolve.LoadedBundle.digest_inputs`: the
    documents the loader matched, the lockfile, and nothing else.
    """
    inputs = loaded.digest_inputs()
    return build_input_digest(
        kernel_version=KERNEL_VERSION,
        seed_tree_version=SEED_TREE_VERSION,
        trace_version=TRACE_VERSION,
        documents=inputs.documents,
        fluid_documents=inputs.fluid_documents,
        lock=inputs.lock,
    )


def require_supported_fidelity(fidelity: str) -> None:
    """Refuse anything but ``deterministic`` -- v0.1 executes one seed only.

    ``flux_engine.governance.skills.require_supported_fidelity`` already
    refuses ``agentic`` without the ``flux_engine_agentic`` extension; this
    wraps it and additionally refuses ``stochastic``, because *this* build
    -- unlike the governance module, which validates the escalation ladder
    the FLUX schema defines -- ships no sweep executor at all yet (only the
    seed-tree primitives ``sweep_effective_seed``/``sweep_path`` exist).
    Both raise :class:`~flux_engine.governance.skills.UnsupportedFidelity`
    (``agentic`` as ``FLX-4101``, ``stochastic`` as
    :class:`~flux_engine.stages.errors.SweepExecutorUnavailable`,
    ``FLX-4103``) so a caller branches on one exception type -- unlike the
    schema, whose ``fidelity`` enum is open to values this engine has never
    heard of, an unrecognised value is refused rather than silently treated
    as executable.
    """
    from flux_engine.stages.errors import FLX_4104, SweepExecutorUnavailable

    if fidelity == "agentic":
        from flux_engine.governance.skills import (
            require_supported_fidelity as require_agentic_extension,
        )

        require_agentic_extension(fidelity, available=agentic_extension_available())
    elif fidelity == "stochastic":
        raise SweepExecutorUnavailable(kernel_version=KERNEL_VERSION)
    elif fidelity != "deterministic":
        raise FluxError(
            FLX_4104,
            f"fidelity {fidelity!r} is not one this engine recognises -- the FLUX "
            "schema's fidelity enum is deterministic/stochastic/agentic; a value "
            "outside that set cannot be executed",
        )


@contextmanager
def _memos_for_one_run() -> Iterator[None]:
    """Clear the draws' memos when the run inside ends, however it ends."""
    try:
        yield
    finally:
        clear_draw_memos()
        clear_generator_memos()


def execute(
    directory: Any,
    *,
    simulation_id: Optional[str] = None,
    zone: Optional[str] = None,
    tolerate_additive: bool = True,
    jobs: int = 1,
) -> PipelineResult:
    """Run every stage of the six-step loop's core once, in memory.

    Raises :class:`~flux_engine.core.errors.FluxError` (any code the
    kernel modules it calls can raise) on the first refusal; nothing is
    partially returned on failure.

    The whole run is evaluated in the kernel's ``decimal`` context
    (:func:`~flux_engine.core.rng.kernel_context`), whatever context the
    caller has installed. The modules that do decimal arithmetic pin it
    themselves; this is the guard that keeps a run a function of its bundle
    and seed when one of them does not, because the CLI's default context
    equals the kernel's and would never show the difference.

    *jobs* is how many processes may generate an emit's rows from declared
    statistics (:mod:`flux_engine.stages._parallel`). It changes how long a
    run takes, never what it produces: every row, line and digest is the
    one ``jobs=1`` gives. A script that passes more than one must start
    from an ``if __name__ == "__main__":`` block, as any program that
    starts processes with ``spawn`` must.

    However the run ends, the draws' memos are cleared when it does
    (:func:`flux_engine.core.rng.clear_memos`,
    :func:`flux_engine.generator.rows.clear_memos`): what they hold is worked
    out from this run's declarations, and a long-lived process that runs
    many bundles keeps none of it past the run.
    """
    require_jobs(jobs)
    with _memos_for_one_run(), localcontext(kernel_context()):
        loaded = load_bundle(directory, tolerate_additive=tolerate_additive)
        cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
        simulation = cast.simulation
        world_doc = cast.world

        fidelity = str(simulation.get("spec", {}).get("fidelity", "deterministic"))
        require_supported_fidelity(fidelity)

        sovereignty_audit = []
        for owner_id, owner_kind, document in (
            (cast.simulation_id, "Simulation", simulation),
            (cast.world_id, "World", world_doc),
        ):
            sovereignty_policy = SovereigntyPolicy.from_document(
                document.get("spec", {}).get("sovereignty")
            )
            row = enforce_sovereignty(
                sovereignty_policy,
                zone=zone,
                document_id=owner_id,
                document_kind=owner_kind,
                event_time=str(simulation.get("spec", {}).get("timeBounds", {}).get("start", "")),
            )
            if row is not None:
                sovereignty_audit.append(row)

        pack = get_pack(str(world_doc.get("spec", {}).get("vertical", "")))
        model: AttributeModel = build_attribute_model(
            _BundleView(loaded.flux_documents), pack, simulation_id=cast.simulation_id
        )

        resolved_emits = resolve_all_emits(loaded, simulation_id=cast.simulation_id)
        per_emit_digests = tuple(
            seam_digests(
                resolved.contract_document,
                resolved.emit,
                resolved.compile().column_names,
                resolved.extension,
            )
            for resolved in resolved_emits
        )
        seed = effective_seed(
            simulation.get("spec", {}).get("seed"), world_doc.get("spec", {}).get("seed")
        )
        root = derive_root(seam_entropy_bytes(list(per_emit_digests)), seed)

        trace = TraceLog()
        seed_tree = SeedTree(root=root)

        spec = PopulationSpec.from_document(world_doc)
        population = materialise_population(
            world=world_doc,
            personas=[cast.document(doc_id) for doc_id in cast.ids("Persona")],
            pack=pack,
            seed_tree=seed_tree,
            trace=trace,
        )

        bounds = simulation.get("spec", {}).get("timeBounds", {})
        clock = VirtualClock.from_time_bounds(bounds["start"], bounds["end"])
        events = emit_signals(
            cast=cast,
            world=spec,
            store=population,
            clock=clock,
            seed_tree=seed_tree,
            trace=trace,
            pack=pack,
        )
        detections = run_detectors(
            detectors=compile_detectors(cast, model), events=events, trace=trace
        )
        transitions = run_journeys(
            cast=cast,
            world=spec,
            journeys=compile_journeys(cast, model),
            events=events,
            store=population,
            seed_tree=seed_tree,
            trace=trace,
        )

        campaign_ids = cast.ids("Campaign")
        ledger: Optional[DeliveryLedger] = None
        if campaign_ids:
            campaign_id = campaign_ids[0]
            consent_id = cast.document(campaign_id)["spec"]["consentRef"]
            use_case = cast.document(consent_id)["spec"]["allowedUseCases"][0]
            policy = compile_campaign(cast, campaign_id, model, use_case=use_case)
            ledger = run_campaign(
                cast=cast,
                policy=policy,
                requests=requests_from_transitions(transitions),
                store=population,
                model=model,
                pack=pack,
                seed_tree=seed_tree,
                trace=trace,
            )
            eligible = [
                o for o in population.ordinals if policy.segment(population.view(o, model.subject))
            ]
        else:
            eligible = list(population.ordinals)

        experiments = compile_experiments(cast)
        experiment: Optional[CompiledExperiment] = experiments[0] if experiments else None
        assignment: Mapping[int, str] = {}
        counts: Tuple[ArmCounts, ...] = ()
        if experiment is not None:
            assignment = assign_variants(
                experiment=experiment, eligible=eligible, run_root=root, trace=trace
            )
            successes = frozenset(t.ordinal for t in transitions if t.success)
            counts = arm_counts(assignment, successes)

        scenario = _Scenario.build(
            store=population, detections=detections, events=events, cast=cast, pack=pack
        )
        emits = tuple(
            _generate_emit(resolved, digests, root, cast, simulation, scenario, jobs)
            for resolved, digests in zip(resolved_emits, per_emit_digests, strict=True)
        )

        decisions = DecisionAuditLog()  # v0.1: no agentic skill calls to audit yet

        components = RunComponents(
            population=trace.component_digests()["population"],
            events=trace.component_digests()["events"],
            journeys=trace.component_digests()["journeys"],
            delivery=trace.component_digests()["delivery"],
            assignment=trace.component_digests()["assignment"],
            emits={outcome.resolved.expose_id: outcome.result.output_digest for outcome in emits},
            decisions=decisions.decisions_digest(),
        )

        input_digest = input_digest_of(loaded)

        return PipelineResult(
            loaded=loaded,
            cast=cast,
            fidelity=fidelity,
            seed=seed,
            root=root,
            seed_tree=seed_tree,
            trace=trace,
            population=population,
            events=events,
            detections=detections,
            transitions=transitions,
            ledger=ledger,
            experiment=experiment,
            assignment=assignment,
            arm_counts=counts,
            emits=emits,
            decisions=decisions,
            sovereignty_audit=tuple(sovereignty_audit),
            input_digest=input_digest,
            files_not_hashed=loaded.digest_inputs().not_hashed,
            components=components,
        )


def _generate_emit(
    resolved: ResolvedEmit,
    digests: SeamDigests,
    root: SeedTreeRoot,
    cast: Cast,
    simulation: Mapping[str, Any],
    scenario: "_Scenario",
    jobs: int = 1,
) -> EmitOutcome:
    """Produce one emit's rows, from declared statistics or from the run."""
    expose = resolved.compile()
    plan: Optional[GeneratorPlan] = None
    mapping: Optional[ColumnMapping] = None
    rows: Tuple[Tuple[Any, ...], ...] = ()
    writer: Optional[EmitWriter] = None
    declared_rows = resolved.declared_row_count

    if resolved.fidelity is not None:
        count = declared_rows if declared_rows is not None else len(scenario.store)
        plan = build_plan(
            expose,
            sim_id=cast.simulation_id,
            seam_digest=digests.h.value,
            rows=count,
            fidelity=resolved.fidelity,
            time_bounds=simulation.get("spec", {}).get("timeBounds"),
        )
        if workers_for(plan.rows, jobs) > 1:
            rows, lines = generate_framed(plan, root=root, expose=expose, jobs=jobs)
            writer = EmitWriter.from_framed(expose, resolved.contract_digest, lines)
        else:
            rows = generate_rows(plan, root=root)
    else:
        mapping = resolve_column_mapping(
            expose,
            declared_sources=scenario.declared_sources,
            aliases=scenario.aliases,
            overrides=resolved.column_mapping,
        )
        rows = scenario.rows(mapping, limit=declared_rows)

    if writer is None:
        writer = EmitWriter(
            expose=expose, contract_digest=resolved.contract_digest, sink=NullSink()
        )
        with writer:
            writer.write_all(rows)
    return EmitOutcome(
        resolved=resolved,
        expose=expose,
        digests=digests,
        plan=plan,
        rows=rows,
        result=writer.result(),
        writer=writer,
        mapping=mapping,
    )


@dataclass(frozen=True)
class _Scenario:
    """The run's own moments, ready to be rendered onto a declared row set.

    One instance per execution, built once from the population, the
    detections and the vertical pack. It owns three things and nothing
    else: which source paths the run *declares* (so a mapping can be
    refused at plan time rather than on the first row), the vertical's
    alias table, and the ordered moments.

    **Scheduler order, at the EMIT phase.** KERNEL-SPEC §7 fixes the
    ordering key ``(t_ms, phase, entity_ordinal, stream_ordinal, seq)`` and
    numbers ``EMIT`` last of the six phases. The rows below go through the
    same :class:`~flux_engine.core.clock.Scheduler` every other phase uses,
    keyed on the instant of the event that *completed* each moment -- so
    the row order is the order a run observes the moments in, and not the
    order the loop that collected them happened to build.
    """

    store: SubjectStore
    detections: Tuple[Detection, ...]
    aliases: Mapping[str, str]
    declared_sources: FrozenSet[str]
    stream_ordinal: int

    @classmethod
    def build(
        cls,
        *,
        store: SubjectStore,
        detections: Sequence[Detection],
        events: Sequence[SignalEvent],
        cast: Cast,
        pack: Any,
    ) -> "_Scenario":
        signal_ids = {nfc(str(event.signal_id)) for event in events}
        signal_ids.update(nfc(str(signal_id)) for signal_id, _ in cast.of_kind("Signal"))
        payload_fields = {
            nfc(str(field))
            for signal_id in sorted(signal_ids)
            for field in (pack.signal_attributes.get(signal_id) or {})
        }
        return cls(
            store=store,
            detections=tuple(detections),
            aliases=emit_aliases(pack.name),
            declared_sources=source_namespace(
                subject_attributes=store.derived_names,
                subject_traits=store.trait_names,
                event_payload_fields=payload_fields,
            ),
            stream_ordinal=cast.stream_ordinal(cast.simulation_id),
        )

    def _values(self, detection: Detection) -> Dict[str, Any]:
        """Every source path this moment can fill, and its value.

        The moment's event is the **last** matched one: it is the event
        that completed the pattern, so it is the instant the moment
        happened at and the payload the moment is about.
        """
        subject = self.store.subject(detection.ordinal)
        values: Dict[str, Any] = {
            "subject.id": subject.id,
            "subject.ordinal": subject.ordinal,
            "subject.lifecycle": subject.lifecycle,
            "subject.persona": subject.persona,
            "subject.market": subject.market,
            "moment.detectorId": detection.detector_id,
            "moment.firstTime": detection.first_t,
            "moment.lastTime": detection.last_t,
            "moment.eventCount": detection.event_count,
        }
        for name, value in subject.traits.items():
            values[f"subject.traits.{name}"] = value
        for name, value in subject.derived.items():
            values[f"subject.{name}"] = value
        event = detection.events[-1]
        values["event.id"] = event.id
        values["event.time"] = event.t_ms
        values["event.type"] = event.event.get_type()
        values["event.source"] = event.event.get_source()
        values["event.subject"] = event.event.get_subject() or ""
        for name, value in event.payload.items():
            values[f"event.data.{name}"] = value
        return values

    def rows(
        self, mapping: ColumnMapping, *, limit: Optional[int] = None
    ) -> Tuple[Tuple[Any, ...], ...]:
        """The moments as positional rows, in scheduler order.

        *limit* is the extension block's declared ``rows``: SEAM-FIDELITY-
        SPEC §1's row count, which **caps** a scenario-driven stream rather
        than padding it. A run with fewer moments than the declared cap
        emits what it has -- inventing rows to reach a number would be the
        one thing this path exists not to do.
        """
        scheduler: Scheduler[Detection] = Scheduler()
        for detection in self.detections:
            if not detection.events:  # pragma: no cover - a firing consumes >= 1 event
                continue
            scheduler.schedule(
                t_ms=detection.last_t,
                phase=Phase.EMIT,
                entity_ordinal=detection.ordinal,
                stream_ordinal=self.stream_ordinal,
                payload=detection,
            )
        built: List[Tuple[Any, ...]] = []
        for item in scheduler.drain():
            if limit is not None and len(built) >= limit:
                break
            built.append(mapping.row(self._values(item.payload)))
        return tuple(built)
