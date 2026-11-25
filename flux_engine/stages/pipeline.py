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

import copy
import functools
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    FrozenSet,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from flux_engine.concordance.compiler import (
    CompiledFidelity,
    compile_fidelity,
    may_declare_no_column,
    names_a_snapshot,
)
from flux_engine.concordance.predicate import (
    DeclaredExpose,
    FidelityBlock,
    expose_from_contract,
    parse_fidelity_block,
)
from flux_engine.core import kernel2
from flux_engine.core import kernels as _kernels
from flux_engine.core.canonical import canonicalise, nfc
from flux_engine.core.clock import (
    Phase,
    Scheduler,
    VirtualClock,
    declared_time_bounds,
    format_rfc3339_ms,
    parse_rfc3339_ms,
)
from flux_engine.core.digest import DigestedDocument, build_input_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern, at_least
from flux_engine.core.kernels import Kernel
from flux_engine.core.rng import clear_memos as clear_draw_memos
from flux_engine.core.rng import kernel_context
from flux_engine.core.seedtree import (
    SeedTree,
    SeedTreeRoot,
    derive_plugin_seed,
    derive_root,
    effective_seed,
)
from flux_engine.core.trace import RunComponents
from flux_engine.expr.attrs import AttributeModel, build_attribute_model
from flux_engine.expr2.errors import FLX_6204, Expr2Error
from flux_engine.expr2.rules import check_rule_ids
from flux_engine.generator import GeneratorPlan, build_plan, generate_rows

# Imported for its registration: the exposure hook gives the counted layout a parent's expected
# count (ADR 0025 section 4), and must be registered before any plan is related.
from flux_engine.generator import exposure as _exposure  # noqa: F401
from flux_engine.generator.conditions import with_conditions
from flux_engine.generator.copula import declares_copula
from flux_engine.generator.counts import TooFewChildRows, TooManyChildRows
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.dates import (
    KIND_DATE,
    KIND_TIMESTAMP,
    DatesTally,
    Duration,
    Period,
    WithinLevel,
    bound_in_units,
    date_of_days,
    declared_bounds,
    positive_target,
    unit_of,
    window_in_units,
)
from flux_engine.generator.declared_range import (
    declared_ranges_of,
    range_cut_notes,
    scale_widened_notes,
)
from flux_engine.generator.dwell import (
    Dwell,
    DwellFamily,
    ExponentialDwell,
    HistogramDwell,
    family_span,
)
from flux_engine.generator.families import ColumnType, Family
from flux_engine.generator.history import (
    HistoryBinding,
    HistoryPick,
    HistoryPlan,
    end_share_note,
    parent_bound_notes,
)
from flux_engine.generator.plugin import PluginExpose, check_rows, generate_values, request_of
from flux_engine.generator.projections import chain_of, derived_rows, project_plan, projection_of
from flux_engine.generator.rates import Rates
from flux_engine.generator.reference import ReferenceRows, build_reference_plan
from flux_engine.generator.relations import (
    FLX_3403,
    KeySpaceTooSmall,
    TrackedLevelsCut,
    check_links_resolved,
    history_pick_of,
    relate_plans,
    related_columns,
)
from flux_engine.generator.rows import (
    GENERATOR_VERSION,
    GENERATOR_VERSION_2,
    ColumnPlan,
    _level_names,
    generate_row_slice,
    generator_version_for,
    written_rows,
)
from flux_engine.generator.rows import clear_memos as clear_generator_memos
from flux_engine.generator.rules import (
    RulePlan,
    declared_not_generated,
    flux_rule_entries,
    new_tally,
    plan_rules,
)
from flux_engine.generator.statistics import FLX_3402, statistics_from_fidelity
from flux_engine.generators.errors import FLX_3122
from flux_engine.generators.loading import (
    LoadedGenerator,
    check_statement,
    forbidden_roots,
    load_declared,
)
from flux_engine.governance.audit import DecisionAuditLog
from flux_engine.governance.privacy import effective_floors, sensitivities_of
from flux_engine.governance.skills import agentic_extension_available
from flux_engine.governance.sovereignty import (
    SovereigntyAuditRow,
    SovereigntyPolicy,
    enforce_sovereignty,
)
from flux_engine.outcomes.responses import (
    FLX_3671,
    Replicate,
    ResponseEstimates,
    ResponseRuntime,
    estimate_responses,
    register_estimates,
    replicate_seed,
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
    integral_payload_fields,
    requests_from_transitions,
    resolve_cast,
    run_campaign,
    run_detectors,
    run_journeys,
)
from flux_engine.runtime.cast import strip_range
from flux_engine.seam.asof import asof_entries_of
from flux_engine.seam.bridges import bridge_entries_of
from flux_engine.seam.conditions import (
    FLX_3643,
    FLX_3644,
    ConditionColumn,
    check_conditioned_columns,
    conditions_of,
)
from flux_engine.seam.contract import CompiledExpose, LogicalKind, find_expose
from flux_engine.seam.copies import copies_of
from flux_engine.seam.counts import CountsEntry, count_entries_of
from flux_engine.seam.coverage import ExposeCoverage, require_full_coverage, strictness_asked_by
from flux_engine.seam.dates import (
    LIFETIMES_KEY,
    RATES_KEY,
    WITHIN_KEY,
    DwellDeclaration,
    DwellFamilyDeclaration,
    ExponentialDeclaration,
    Histogram,
    lifetime_entries_of,
    rates_entries_of,
    within_entries_of,
)
from flux_engine.seam.declared import NotGenerated, require_all_generated
from flux_engine.seam.emit import (
    EmitResult,
    EmitWriter,
    SeamDigests,
    frame_rows,
    seam_digests,
    seam_entropy_bytes,
)
from flux_engine.seam.history import END_SHARE_OF_ENTITIES, HISTORY_KEY, history_entries_of
from flux_engine.seam.keys import compile_keys, declared_exposes, window_ends
from flux_engine.seam.links import link_entries_of
from flux_engine.seam.mapping import (
    ColumnMapping,
    integral_namespace,
    resolve_column_mapping,
    source_namespace,
)
from flux_engine.seam.parameters import (
    ParameterRegister,
    build_parameter_register,
    check_contract_citations,
)
from flux_engine.seam.profile import require_profile
from flux_engine.seam.projections import PROJECTS_KEY, projection_keys_of, refuse_projection
from flux_engine.seam.resolve import (
    EXTENSION_NAMESPACE,
    FLX_3105,
    LoadedBundle,
    ResolvedEmit,
    declared_conditions,
    declared_consumers,
    declared_copies,
    declared_counts,
    declared_generators,
    declared_overlay,
    declared_responses,
    load_bundle,
    other_sourced_columns,
    parameter_entries,
    privacy_floors,
    profile_reference,
    resolve_all_emits,
    strict_coverage_declared,
    unused_statistics,
)
from flux_engine.seam.responses import ResponseFamily
from flux_engine.seam.sinks import NullSink
from flux_engine.stages._consumer_check import check_consumer_sql
from flux_engine.stages._grades import require_declared_grade
from flux_engine.stages._parallel import (
    FramedEmit,
    framed_pool,
    require_jobs,
    reserve_workers,
    workers_for,
)
from flux_engine.stages._plugin_exposes import plugin_exposes, refuse_plugin_combinations
from flux_engine.stages._prewarm import start_forkserver_early
from flux_engine.stages._range_setters import range_setter_notes, uncut_columns
from flux_engine.stages.scaling import (
    FLX_3404,
    FLX_3405,
    ScaleFloor,
    child_rows_note,
    floor_for_cut,
    key_space_note,
    scale_emit,
    scale_emits,
    scale_text,
    scaled_rows,
    share_of,
    too_many_child_rows_note,
    widened_columns,
)
from flux_engine.stages.timings import lap, span
from flux_engine.verticals import emit_aliases, pack_of_world
from flux_engine.world import (
    Derivation,
    PopulationSpec,
    SubjectStore,
    TraceLog,
    derivation_for,
    integral_trait_names,
    materialise_population,
)

__all__ = [
    "EmitOutcome",
    "PipelineResult",
    "PlanEra",
    "advice_notes",
    "compile_declared_fidelity",
    "end_share_notes",
    "execute",
    "parent_bound_history_notes",
    "input_digest_of",
    "parameter_register_of",
    "parse_declared_fidelity",
    "require_supported_fidelity",
]


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
    #: The rows as the generator drew them. Empty when worker processes made the
    #: emit (``processes`` above one): they hand back its framed lines, which every
    #: digest, file and sink reads, and not the rows.
    rows: Tuple[Tuple[Any, ...], ...]
    result: EmitResult
    writer: EmitWriter
    mapping: Optional[ColumnMapping] = None
    #: How many processes made the rows: 1 is the run's own process.
    processes: int = 1
    #: The declared fidelity block as :func:`compile_declared_fidelity`
    #: compiled it before the population existed; ``None`` when the emit
    #: declares none. ``plan.json`` records it (SEAM-FIDELITY-SPEC §3).
    compiled_fidelity: Optional[CompiledFidelity] = None
    #: How each column was produced: declared, copied, derived or a type
    #: default (:mod:`flux_engine.seam.coverage`). Read off the plan or the
    #: mapping that decided the column, so it can never disagree with them.
    #: It is reported and recorded in ``column-provenance.json``, and the plan
    #: holds a digest of that record (``columnProvenanceDigest``).
    coverage: Optional[ExposeCoverage] = None
    #: What the expose declares and this version accepts without generating
    #: (:mod:`flux_engine.seam.declared`). Reported by ``plan``; no
    #: digest or record the plan binds carries it.
    not_generated: Tuple[NotGenerated, ...] = ()
    #: What each row of a fact of ``lifetimes`` or ``within`` came to (ADR 0024 section 7), per
    #: fact id; ``None`` unless a fact of the emit carries a target above zero. ``apply`` records
    #: it in the receipt's emit entry under ``dates``.
    dates: Optional[DatesTally] = None

    @property
    def source(self) -> str:
        """Where this emit's rows came from: ``generator`` or ``scenario``."""
        return "generator" if self.plan is not None else "scenario"


@dataclass(frozen=True)
class HiddenOutcome:
    """One hidden table: planned and generated as any emit is, and never written (ADR 0024 section 3).

    Its rows are what a path through a link reads, so it holds the plan and the digests the plan
    document names, and no rows, no writer and no output digest: the run has no stream for it.
    """

    resolved: ResolvedEmit
    digests: SeamDigests
    plan: GeneratorPlan
    compiled_fidelity: Optional[CompiledFidelity] = None


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
    #: The kernel this execution ran (:mod:`flux_engine.core.kernels`): the one
    #: its plan names, or the default for a new plan. The kernel and seed tree
    #: versions a stage stamps into a plan, a receipt, a run digest, a lineage
    #: event, a golden record or an evidence pack are this one's, never the
    #: default's. The trace version is the one format :mod:`flux_engine.core.trace`
    #: writes, which every kernel this engine implements names.
    kernel: Kernel
    #: The source of every number the run reads (parameter register,
    #: :mod:`flux_engine.seam.parameters`), or ``None`` when the Simulation
    #: declares no parameter register. The plan holds a digest of the record it
    #: renders to (``parameterRegisterDigest``) only when there is one.
    parameters: Optional[ParameterRegister] = None
    #: The ``--scale`` this execution ran at (:mod:`flux_engine.stages.scaling`); 1 is none.
    scale: Decimal = Decimal(1)
    #: ``(I, N)`` when only one share of each generated emit's rows was made: such a
    #: result is not a whole run, and ``apply`` writes only the shares.
    shard: Optional[Tuple[int, int]] = None
    #: The rows the pinned profile publishes for each reference expose of the run, by exposeId
    #: (ADR 0023 section 2); empty for a run with none. The plan records them.
    references: Mapping[str, ReferenceRows] = field(default_factory=dict)
    #: The generator plugins this execution loaded, by the ``exposeId`` each generates (ADR 0032):
    #: empty for a run whose bundle declares none. Each carries whether its code lies in a source
    #: tree, which the stage that records the run reads.
    generators: Mapping[str, LoadedGenerator] = field(default_factory=dict)
    #: ``True`` when ``execute`` stopped before asking any plugin for its rows, because its
    #: *stored_probe* said the run is already stored: every plugin expose then holds no rows, and
    #: the result is only the plan's. Nothing may be written from it.
    deferred: bool = False
    #: The hidden tables of the run, in declared order (ADR 0024 section 3). ``emits`` holds the
    #: written emits only; these are in the plan and in no stream, receipt or digest of the run.
    hidden_emits: Tuple[HiddenOutcome, ...] = ()
    #: The response families as the run applied them (ADR 0029): their estimates, their segments
    #: and the experiment's assignment. ``None`` for a Simulation that declares none.
    responses: Optional[ResponseRuntime] = None
    #: The sweep run this execution is, with the effects it took (ADR 0029 section 3); ``None``
    #: for every run that is not a forecast's replicate.
    replicate: Optional[Replicate] = None
    #: What a history's ``end`` share was a share of in this execution (ADR 0017, the amendment of
    #: 2026-11-24): every entity for a new plan, which the plan then says with ``historyEndShare``,
    #: or the eligible entities alone for a recorded plan without the key.
    end_share: str = END_SHARE_OF_ENTITIES
    #: Whether a column may be cut at its contract's range (ADR 0024); false replays a plan as it was.
    cut_ranges: bool = True
    #: The key columns ``--scale`` widened, by exposeId: their contract range is not verified.
    scale_widened: Mapping[str, FrozenSet[str]] = field(default_factory=dict)
    #: The drawn parents ``--scale`` kept at the rows their children's tracked levels need.
    scale_floors: Mapping[str, ScaleFloor] = field(default_factory=dict)

    def emit(self, expose_id: str) -> EmitOutcome:
        for outcome in self.emits:
            if outcome.resolved.expose_id == expose_id:
                return outcome
        raise KeyError(expose_id)


def parameter_register_of(loaded: LoadedBundle, cast: Cast) -> Optional[ParameterRegister]:
    """The run's parameter register, or ``None`` when its Simulation declares none.

    The documents a register speaks for are the ones the run reads: its cast,
    and every Playback whose ``worldRef`` names the cast's World (a Playback's
    scorecard is what an emit's ``minCredibility`` is checked against, so its
    numbers bear on the run though no reference reaches it). An entry that names
    a document outside them, a pointer that reaches nothing, or a pointer that
    reaches no counted number, is ``FLX-3105``
    (:func:`flux_engine.seam.parameters.build_parameter_register`), and so is a
    ``contract`` citation that names no contract the run emits from or reaches
    nothing in it (:func:`flux_engine.seam.parameters.check_contract_citations`).
    The register reads the Simulation's pin, never the profile, so it is the
    same with or without the profile at hand.
    """
    simulation = cast.simulation
    entries = parameter_entries(simulation, where=cast.simulation_id)
    if entries is None:
        return None
    documents: Dict[str, Mapping[str, Any]] = dict(cast.documents)
    for doc_id, document in loaded.flux_documents.items():
        if not isinstance(document, Mapping) or document.get("kind") != "Playback":
            continue
        spec = document.get("spec")
        world = spec.get("worldRef") if isinstance(spec, Mapping) else None
        if world is not None and strip_range(world) == cast.world_id:
            documents.setdefault(strip_range(doc_id), document)
    register = build_parameter_register(
        simulation_id=cast.simulation_id,
        documents=documents,
        namespace=EXTENSION_NAMESPACE,
        entries=entries,
        profile_digest=profile_reference(simulation, where=cast.simulation_id),
        code=FLX_3105,
        unused=unused_statistics(simulation, where=cast.simulation_id),
    )
    if register.uses_contract:
        check_contract_citations(
            register,
            _emitted_contracts(loaded, simulation),
            where=cast.simulation_id,
            code=FLX_3105,
        )
    return register


def _emitted_contracts(
    loaded: LoadedBundle, simulation: Mapping[str, Any]
) -> Dict[str, Mapping[str, Any]]:
    """The FLUID contracts the Simulation's emits name, by ``productRef``, as the bundle holds them.

    A ``contract`` register entry may cite only these. An emit that names no
    contract of the bundle is refused where the emit is resolved (``FLX-3102``),
    so it is left out here rather than refused a second time.
    """
    spec = simulation.get("spec")
    emits = spec.get("emits") if isinstance(spec, Mapping) else None
    contracts: Dict[str, Mapping[str, Any]] = {}
    for emit in emits if isinstance(emits, list) else ():
        product_ref = emit.get("productRef") if isinstance(emit, Mapping) else None
        if not isinstance(product_ref, str):
            continue
        found = loaded.fluid_documents.get(product_ref)
        if found is not None:
            contracts[product_ref] = found[1]
    return contracts


def input_digest_of(loaded: LoadedBundle, *, kernel: Kernel) -> DigestedDocument:
    """Build KERNEL-SPEC §11's ``inputDigest`` over a loaded bundle's files, under *kernel*.

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

    *kernel* is the kernel of the plan the digest is for. Its three versions
    are inside the digest, so a caller that checks a recorded plan passes that
    plan's kernel, and the digest is the one the plan recorded whatever kernel
    a new plan would take.
    """
    inputs = loaded.digest_inputs()
    return build_input_digest(
        kernel_version=kernel.version,
        seed_tree_version=kernel.seed_tree_version,
        trace_version=kernel.trace_version,
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
        raise SweepExecutorUnavailable()
    elif fidelity != "deterministic":
        raise FluxError(
            FLX_4104,
            f"fidelity {fidelity!r} is not one this engine recognises -- the FLUX "
            "schema's fidelity enum is deterministic/stochastic/agentic; a value "
            "outside that set cannot be executed",
        )


def _declared_contract(resolved: ResolvedEmit) -> Tuple[Any, DeclaredExpose]:
    """The contract and the expose ``verify`` reads for *resolved*'s fidelity block.

    ``apply`` records the contract canonicalised, in
    ``fidelity-input-<exposeId>.json``, and ``verify`` takes the expose from
    that record; this takes both the same way.
    """
    contract = canonicalise(resolved.contract_document)
    return contract, expose_from_contract(contract, resolved.expose_id)


def check_flux_side_rule_ids(resolved_emits: Sequence[ResolvedEmit], *, where: str) -> None:
    """Every FLUX-side rule id of a Simulation's emits is unique across all of them (``FLX-6201``).

    ``plan``, ``validate`` and ``fluxsim init``'s pre-flight each call it once the emits are
    resolved, because one emit's planner cannot see another emit's rules.
    """
    ids: List[Optional[str]] = []
    for resolved in resolved_emits:
        for entry in flux_rule_entries(
            resolved.flux_side_rules, where=f"{where}: emit {resolved.expose_id!r}"
        ):
            rule_id = entry.get("id") if isinstance(entry, Mapping) else None
            ids.append(rule_id if isinstance(rule_id, str) else None)
    if ids:
        check_rule_ids(ids, where=f"{where}: FLUX-side rules")


def _declared_null_fractions(resolved: ResolvedEmit) -> Dict[str, Any]:
    """The ``nullFraction`` each column declares under the emit's ``fidelity.columns``, as declared."""
    columns = resolved.fidelity.get("columns") if resolved.fidelity is not None else None
    if not isinstance(columns, Mapping):
        return {}
    return {
        nfc(str(name)): block["nullFraction"]
        for name, block in columns.items()
        if isinstance(block, Mapping) and "nullFraction" in block
    }


def _is_zero_share(declared: Any) -> bool:
    """Whether a declared ``nullFraction`` is zero, which no cut can lower."""
    try:
        return Fraction(str(declared)) == 0
    except (ValueError, ZeroDivisionError):
        return False


def period_end_null_notes(resolved: Sequence[ResolvedEmit]) -> Tuple[str, ...]:
    """One sentence for each period end that declares a ``nullFraction`` under a parent that can close.

    A period's end is cut at a closed parent's end (ADR 0024 section 5), so it is NULL only under a
    parent that is open, and the share generated can be lower than the share declared. The sentence
    names the emit, the column and the declared share. It is advice only: nothing here changes how a
    value is drawn, and the plan's document and digests do not carry it. A period whose parent has no
    end, or whose end column declares no ``nullFraction`` or a zero one, gets none.
    """
    held = {(str(emit.contract_path), emit.expose_id): emit for emit in resolved}
    notes: List[str] = []
    for emit in resolved:
        nullable_end = _nullable_period_end(emit)
        if nullable_end is None:
            continue
        end, declared = nullable_end
        parent_id = _closing_parent_of(emit, held)
        if parent_id is None:
            continue
        notes.append(
            f"note: {emit.expose_id}.{end} declares nullFraction {declared}, and it is a period "
            f"end within {parent_id}, which can close; an end is NULL only under a parent that "
            "is open, because it is cut at a closed parent's end (ADR 0024 section 5), so the "
            f"share generated can be lower than {declared}"
        )
    return tuple(notes)


def end_share_notes(plans: Sequence[GeneratorPlan], root: SeedTreeRoot) -> Tuple[str, ...]:
    """One sentence for each history whose declared end share yields less than it declares.

    The share is a share of every entity, and an ``end`` with a column ends only entities of two
    versions or more, so a history with too few of them ends every one that can and the share
    generated is lower than the share declared (ADR 0017, the amendment of 2026-11-24). Advice
    only: no plan document or digest carries it.
    """
    notes = (
        end_share_note(expose_id, pick.entry, chain)
        for expose_id, pick, chain in _history_chains(
            plans, root, lambda pick: pick.entry.end is not None
        )
    )
    return tuple(note for note in notes if note is not None)


def parent_bound_history_notes(
    plans: Sequence[GeneratorPlan], root: SeedTreeRoot
) -> Tuple[str, ...]:
    """The notes of each history that keeps to its parent's lifetime (:func:`parent_bound_notes`)."""
    return tuple(
        note
        for expose_id, _, chain in _history_chains(
            plans, root, lambda pick: pick.parent_lifetime is not None
        )
        for note in parent_bound_notes(expose_id, chain)
    )


def _history_chains(
    plans: Sequence[GeneratorPlan], root: SeedTreeRoot, wanted: Callable[[HistoryPick], bool]
) -> Iterator[Tuple[str, HistoryPick, HistoryPlan]]:
    """The exposeId, pick and layout of each history among *plans* that *wanted* names."""
    for plan in plans:
        pick = history_pick_of(plan)
        if pick is not None and wanted(pick):
            yield plan.expose.expose_id, pick, chain_of(plan, root)


def advice_notes(result: PipelineResult) -> Tuple[str, ...]:
    """The advice a plan prints, one sentence each: period ends, end shares, parent bounds and range cuts."""
    outcomes: List[Union[EmitOutcome, HiddenOutcome]] = [*result.emits, *result.hidden_emits]
    plans = [outcome.plan for outcome in outcomes if outcome.plan is not None]
    emits = [outcome.resolved for outcome in outcomes]
    return (
        period_end_null_notes(emits)
        + end_share_notes(plans, result.root)
        + parent_bound_history_notes(plans, result.root)
        + tuple(
            note for plan in plans for note in range_cut_notes(plan.expose.expose_id, plan.columns)
        )
        + tuple(
            note
            for outcome in outcomes
            if outcome.plan is not None
            for note in range_setter_notes(
                outcome.resolved, outcome.plan, emits, _related_of(outcome.resolved)
            )
        )
        + tuple(
            note
            for outcome in outcomes
            for note in scale_widened_notes(
                outcome.resolved.expose_id,
                declared_ranges_of(outcome.resolved.expose, outcome.resolved.compile()),
                result.scale_widened.get(outcome.resolved.expose_id, ()),
                scale_text(result.scale),
            )
        )
    )


def _nullable_period_end(emit: ResolvedEmit) -> Optional[Tuple[str, Any]]:
    """The end column of *emit*'s lifetime and its declared ``nullFraction``, when that is above zero."""
    own = lifetime_entries_of(emit.extension, emit.expose_id)[:1]
    end = own[0].end if own else None
    declared = _declared_null_fractions(emit).get(nfc(end)) if end is not None else None
    if end is None or declared is None or _is_zero_share(declared):
        return None
    return end, declared


def _closing_parent_of(
    emit: ResolvedEmit, held: Mapping[Tuple[str, str], ResolvedEmit]
) -> Optional[str]:
    """The parent of the first ``within`` lifetime of *emit* that has an end of its own, else ``None``."""
    for entry in within_entries_of(emit.extension, emit.expose_id):
        if not entry.lifetime:
            continue
        try:
            parent_id = _targets_along(emit, entry.relationship)[-1]
        except FluxError:  # pragma: no cover - the plan refused the relationship already
            continue
        parent = held.get((str(emit.contract_path), parent_id))
        lifetime = lifetime_entries_of(parent.extension, parent.expose_id)[:1] if parent else ()
        if lifetime and lifetime[0].end is not None:
            return parent_id
    return None


def counted_relationships(
    simulation: Mapping[str, Any], *, where: str
) -> FrozenSet[Tuple[str, str]]:
    """The ``(exposeId, relationship)`` pairs *simulation* lays out with a ``counts`` entry.

    A contract ``derive`` with ``aggregate`` generates over exactly these (ADR 0016 section 5), so
    ``plan``, ``validate`` and ``fluxsim init``'s pre-flight read them off the whole Simulation
    and hand them to :func:`plan_emit_rules`.
    """
    return frozenset(
        (entry.emit, entry.relationship) for entry in declared_counts(simulation, where=where)
    )


def lifetime_duration_of(resolved: ResolvedEmit, *, where: str) -> Optional[Duration]:
    """*resolved*'s lifetime when it draws its end as a duration (ADR 0024 section 4), else ``None``.

    The columns and their kinds are the planned expose's, hidden columns included, and
    ``endPastWindow`` takes the contract's default: ``kept`` for an end the contract requires,
    ``open`` for one it leaves nullable. A ``target`` on such a lifetime is the share of rows
    whose end is before their start (ADR 0024 section 7); a lifetime with a target and no duration
    draws its end from its own statistics, and ``verify`` alone checks it.
    """
    entries = lifetime_entries_of(resolved.extension, resolved.expose_id)
    entry = entries[0] if entries else None
    if entry is None or entry.duration is None or entry.end is None:
        return None
    columns = {column.name: column for column in resolved.compile_planned().columns}
    end = columns[entry.end]
    bounds = declared_bounds_of(resolved)
    duration = Duration(
        start=entry.start,
        end=entry.end,
        kind=KIND_DATE if end.logical_type.kind is LogicalKind.DATE else KIND_TIMESTAMP,
        histogram=entry.duration if isinstance(entry.duration, Histogram) else None,
        end_past_window=entry.end_past_window or ("kept" if end.required else "open"),
        target=entry.target,
        family=None if isinstance(entry.duration, Histogram) else _dwell_family(entry.duration),
        end_range=bounds.get(entry.end),
    )
    _refuse_unfittable_end(resolved, duration, bounds.get(entry.start), where)
    return duration


def declared_bounds_of(resolved: ResolvedEmit) -> Dict[str, Tuple[int, int]]:
    """The declared range ``R`` of each DATE or TIMESTAMP column of *resolved* that has one (ADR 0024 section 5).

    The range is what the column's declared statistics give it (:func:`declared_bounds`), in the
    column's units, which is the support ``FLX-5005`` checks a written value against. A column the
    statistics bound not, one that the run's time bounds would give a range, has none. An emit that
    declares no fidelity block has none.
    """
    if resolved.fidelity is None:
        return {}
    expose = resolved.compile_planned()
    kinds = {LogicalKind.DATE, LogicalKind.TIMESTAMP}
    found: Dict[str, Tuple[int, int]] = {}
    for column, statistics in statistics_from_fidelity(expose, resolved.fidelity).items():
        bounds = declared_bounds(statistics)
        if bounds is not None and expose.column(column).logical_type.kind in kinds:
            found[column] = bounds
    return found


def _day_text(kind: str, value: int) -> str:
    """*value*, epoch days or milliseconds of a column of *kind*, as the date or timestamp a bundle writes."""
    return date_of_days(value).isoformat() if kind == KIND_DATE else format_rfc3339_ms(value)


def _refuse_unfittable_end(
    resolved: ResolvedEmit,
    duration: Duration,
    start_range: Optional[Tuple[int, int]],
    where: str,
) -> None:
    """Refuse (``FLX-6204``) a duration whose end the end column's declared range can leave on some row (ADR 0024 section 4).

    The end is cut to stay inside the end column's range ``Re``, so a row with no value of the
    duration inside it has none to draw. Under ``endPastWindow: open`` that row has no end, and
    nothing is refused. Anywhere else the plan refuses it, when the latest start ``s_max`` plus the
    shortest duration ``d_min`` is after ``Re.max``, and, as the mirror, when the earliest start
    ``s_min`` plus the longest duration ``d_max`` is before ``Re.min``; an exponential has no
    longest. A start with no declared range, an end with none, or a duration no row draws
    leaves nothing to compare.
    """
    if duration.end_range is None or start_range is None or duration.end_past_window == "open":
        return
    span = family_span(duration.dwell, unit_of(duration.kind))
    if span is None:
        return
    shortest, longest = span
    low, high = duration.end_range
    kind = duration.kind
    problem = None
    if start_range[1] + shortest > high:
        problem = (
            f"a row that starts on {_day_text(kind, start_range[1])}, the latest {duration.start!r} "
            f"declares, ends no sooner than {_day_text(kind, start_range[1] + shortest)}, after the "
            f"{_day_text(kind, high)} that {duration.end!r} declares as its maximum"
        )
    elif longest is not None and start_range[0] + longest < low:
        problem = (
            f"a row that starts on {_day_text(kind, start_range[0])}, the earliest {duration.start!r} "
            f"declares, ends no later than {_day_text(kind, start_range[0] + longest)}, before the "
            f"{_day_text(kind, low)} that {duration.end!r} declares as its minimum"
        )
    if problem is not None:
        raise Expr2Error(
            FLX_6204,
            f"{where}: the lifetime of {resolved.expose_id!r} draws {duration.end!r} as "
            f"{duration.start!r} plus a duration, and {problem}, so no end could be drawn for it; "
            f"widen the declared range of {duration.end!r}, change the duration, or declare "
            "endPastWindow: open on a nullable end, which leaves such a row without an end",
        )


def _dwell(declared: DwellDeclaration) -> Dwell:
    """The generator's dwell for a declared histogram or exponential (ADR 0025 section 3)."""
    if isinstance(declared, Histogram):
        return HistogramDwell(declared)
    return ExponentialDwell(
        mean=Fraction(declared.mean),
        open=Fraction(declared.open) if declared.open is not None else Fraction(0),
    )


def _dwell_family(declared: Union[ExponentialDeclaration, DwellFamilyDeclaration]) -> DwellFamily:
    """The dwell family of a declared exponential or family: one dwell for every row, or by state."""
    if isinstance(declared, ExponentialDeclaration):
        return DwellFamily(given=None, states=(), otherwise=_dwell(declared))
    return DwellFamily(
        given=declared.given,
        states=tuple((text, _dwell(dwell)) for text, dwell in declared.states),
        otherwise=None if declared.otherwise is None else _dwell(declared.otherwise),
    )


def _targets_along(resolved: ResolvedEmit, names: Sequence[str]) -> Tuple[str, ...]:
    """The exposeId each relationship of the chain *names* reaches, from *resolved*'s expose."""
    targets: List[str] = []
    current: Mapping[str, Any] = resolved.expose
    for name in names:
        semantics = current.get("semantics")
        raw = semantics.get("relationships") if isinstance(semantics, Mapping) else None
        found = next(
            (
                item
                for item in (
                    raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else ()
                )
                if isinstance(item, Mapping) and nfc(str(item.get("name", ""))) == name
            ),
            None,
        )
        if found is None:  # pragma: no cover - resolve checked the relationship
            raise FluxError(FLX_3105, f"{resolved.expose_id!r} declares no relationship {name!r}")
        targets.append(nfc(str(found.get("to", ""))))
        current = find_expose(resolved.contract_document, targets[-1])
    return tuple(targets)


def rates_of(resolved: ResolvedEmit) -> Tuple[Rates, ...]:
    """*resolved*'s seasonal rates, one entry per date column its dates steps set (ADR 0025 section 2)."""
    return tuple(
        Rates(
            column=entry.column,
            kind=_kind_of(resolved, entry.column),
            month=entry.month,
            day_of_week=entry.day_of_week,
            hour_of_week=entry.hour_of_week,
        )
        for entry in rates_entries_of(resolved.extension, resolved.expose_id)
    )


def _kind_of(emit: ResolvedEmit, column: str) -> str:
    """The grain of *column* of the planned expose of *emit*: ``DATE`` or ``TIMESTAMP``."""
    found = next(item for item in emit.compile_planned().columns if item.name == column)
    return KIND_DATE if found.logical_type.kind is LogicalKind.DATE else KIND_TIMESTAMP


def within_levels_of(
    resolved: ResolvedEmit,
    emits: Sequence[ResolvedEmit],
    *,
    where: str,
    window: Optional[Tuple[int, int]] = None,
    repair: Optional[Dict[str, Tuple[int, int]]] = None,
) -> Tuple[WithinLevel, ...]:
    """*resolved*'s ``within`` entries for a date, one level each (ADR 0024 section 5), or the refusal each earns.

    An entry's ``target`` is the share of covered rows that violate it (ADR 0024 section 7). A period
    (``lifetime: true``) is the level of the emit's lifetime start, which must have an end
    (``FLX-3105``); its end is set by :func:`period_of`. Each entry reads the lifetime of the last expose of its relationship in
    *emits*. Several entries for one date along a chain meet only where the middle exposes are tied
    to the next level (see :func:`_refuse_untied_chains`), so an entry set without the ties is
    ``FLX-6204``. So is an enforced entry for a column that declares a range when the declared ranges
    allow a row whose parent's lifetime and that range do not meet (:func:`_refuse_empty_intersections`);
    *window* is the run's ``spec.timeBounds`` in milliseconds, when it declares them. With *repair*
    an entry the declared ranges leave without a value is not refused for it, and the range that
    would hold it is recorded per column (:func:`_refuse_empty_intersections`).
    """
    entries = within_entries_of(resolved.extension, resolved.expose_id)
    if not entries:
        return ()
    bounds = declared_bounds_of(resolved)
    held = {(str(emit.contract_path), emit.expose_id): emit for emit in emits}
    levels: List[Tuple[WithinLevel, ResolvedEmit]] = []
    for entry in entries:
        name = ".".join(entry.relationship)
        own = lifetime_entries_of(resolved.extension, resolved.expose_id)[:1]
        if entry.lifetime and (not own or own[0].end is None):
            raise FluxError(
                FLX_3105,
                f"{where}: the within entry of {resolved.expose_id!r} through {name!r} is "
                "lifetime: true, and the lifetime of its emit declares no end; a period is "
                "a start and an end within another lifetime",
            )
        date_column = own[0].start if entry.lifetime else entry.date
        assert date_column is not None
        last = _targets_along(resolved, entry.relationship)[-1]
        parent = held.get((str(resolved.contract_path), last))
        lifetime = lifetime_entries_of(parent.extension, parent.expose_id)[:1] if parent else ()
        if parent is None or not lifetime:
            raise FluxError(
                FLX_3105,
                f"{where}: the within entry of {resolved.expose_id!r} for {entry.column!r} "
                f"through {name!r} reaches {last!r}, which the Simulation does not emit with a "
                "lifetime",
            )
        levels.append(
            (
                WithinLevel(
                    column=date_column,
                    kind=_kind_of(resolved, date_column),
                    relationship=entry.relationship,
                    start=lifetime[0].start,
                    end=lifetime[0].end,
                    parent_kind=_kind_of(parent, lifetime[0].start),
                    offset=entry.offset,
                    period=entry.lifetime,
                    target=entry.target,
                    bounds=bounds.get(date_column),
                ),
                parent,
            )
        )
    _refuse_crossed_entries(resolved, [level for level, _ in levels], where)
    _refuse_untied_chains(resolved, levels, where)
    _refuse_empty_intersections(resolved, levels, window, where, repair)
    return tuple(level for level, _ in levels)


def window_of(time_bounds: Optional[Mapping[str, Any]]) -> Optional[Tuple[int, int]]:
    """A Simulation's ``spec.timeBounds`` as the clock's milliseconds, or ``None`` when it declares none or a malformed pair.

    A malformed pair is refused where the plan is built (``FLX-3402``), so it reads as none here.
    """
    if not isinstance(time_bounds, Mapping):
        return None
    start, end = time_bounds.get("start"), time_bounds.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return parse_rfc3339_ms(start), parse_rfc3339_ms(end)
    except FluxError:
        return None


def _parent_bounds(
    parent: ResolvedEmit, where: str
) -> Tuple[Optional[Tuple[int, int]], bool, Optional[int], str, str]:
    """What the declared ranges say of *parent*'s lifetime: ``(start range, ends, e_min, start kind, end kind)``.

    ``ends`` is whether the lifetime names an end column; a lifetime that names none is open, so
    it bounds no date from above. ``e_min`` is the smallest end the lifetime can take, in the
    end column's units: the least start plus the shortest duration, when the lifetime has a
    duration, or else the end column's declared minimum; ``None`` when neither is known.
    """
    entry = lifetime_entries_of(parent.extension, parent.expose_id)[0]
    bounds = declared_bounds_of(parent)
    start_range = bounds.get(entry.start)
    start_kind = _kind_of(parent, entry.start)
    if entry.end is None:
        return start_range, False, None, start_kind, start_kind
    end_kind = _kind_of(parent, entry.end)
    duration = lifetime_duration_of(parent, where=where)
    if duration is None:
        floor = bounds[entry.end][0] if entry.end in bounds else None
    else:
        span = family_span(duration.dwell, unit_of(duration.kind))
        floor = None if start_range is None or span is None else start_range[0] + span[0]
    return start_range, True, floor, start_kind, end_kind


def widened_ranges_of(
    emits: Sequence[ResolvedEmit], time_bounds: Optional[Mapping[str, Any]], *, where: str
) -> Dict[Tuple[str, str], Tuple[int, int]]:
    """The range each enforced ``within`` date of *emits* would need to meet the plan's check of the declared ranges.

    Keyed by ``(exposeId, column)``, in the column's units: the range the plan accepts, widened
    from the declared one (:func:`_widened_range`). A column the plan accepts, or that no range
    repairs, is not listed. ``fluxsim init`` writes a bundle with these ranges.
    """
    found: Dict[Tuple[str, str], Tuple[int, int]] = {}
    for emit in emits:
        repair: Dict[str, Tuple[int, int]] = {}
        within_levels_of(emit, emits, where=where, window=window_of(time_bounds), repair=repair)
        for column, widened in repair.items():
            found[(emit.expose_id, column)] = widened
    return found


def unrepairable_withins_of(
    emits: Sequence[ResolvedEmit], time_bounds: Optional[Mapping[str, Any]], *, where: str
) -> Dict[Tuple[str, str], str]:
    """Why each enforced ``within`` date of *emits* is refused by the plan's check and no wider range repairs it.

    Keyed by ``(exposeId, column)``. A column is listed when :func:`_refuse_empty_intersections`
    would refuse it even with repair on: the three conditions fail, and the parent's latest
    start, or a parent's earliest end the check reads, is not declared (:func:`_repaired`).
    The reason says so, and names the parent's start column when it is that start. A column the
    plan accepts, or that :func:`widened_ranges_of` lists, is not listed. ``fluxsim init`` writes
    no ``within`` entry for a listed date.
    """
    found: Dict[Tuple[str, str], str] = {}
    window = window_of(time_bounds)
    held = {(str(emit.contract_path), emit.expose_id): emit for emit in emits}
    for emit in emits:
        bounds = declared_bounds_of(emit)
        group_of: Dict[str, List[Tuple[WithinLevel, ResolvedEmit]]] = {}
        for entry in within_entries_of(emit.extension, emit.expose_id):
            own = lifetime_entries_of(emit.extension, emit.expose_id)[:1]
            column = own[0].start if entry.lifetime and own else entry.date
            targets = _targets_along(emit, entry.relationship)
            parent = held.get((str(emit.contract_path), targets[-1])) if targets else None
            lifetime = lifetime_entries_of(parent.extension, parent.expose_id)[:1] if parent else ()
            if column is None or parent is None or not lifetime:
                continue  # the plan refuses it for what it is: not a range
            level = WithinLevel(
                column=column,
                kind=_kind_of(emit, column),
                relationship=entry.relationship,
                start=lifetime[0].start,
                end=lifetime[0].end,
                parent_kind=_kind_of(parent, lifetime[0].start),
                offset=entry.offset,
                period=entry.lifetime,
                target=entry.target,
                bounds=bounds.get(column),
            )
            group_of.setdefault(column, []).append((level, parent))
        for column, group in group_of.items():
            if all(positive_target(level.target) for level, _ in group):
                continue
            terms = _intersection_terms(group, window, where)
            if terms is None or _repaired(emit, terms, column, {}):
                continue
            reason = _empty_intersection_reason(emit, terms)
            if reason is None:
                continue
            starts = sorted(
                {level.start for level, parent in group if _parent_bounds(parent, where)[0] is None}
            )
            if starts:
                named = ", ".join(repr(start) for start in starts)
                reason = (
                    f"the latest start of the parent's lifetime is not declared ({named} declares "
                    "no range), so an enforced entry could leave a row of the date without a value"
                )
            else:
                reason = (
                    f"no declared range makes the entry hold on every row: {reason}, so an "
                    "enforced entry could leave a row of the date without a value"
                )
            found[(emit.expose_id, column)] = reason
    return found


def _refuse_empty_intersections(
    resolved: ResolvedEmit,
    levels: Sequence[Tuple[WithinLevel, ResolvedEmit]],
    window: Optional[Tuple[int, int]],
    where: str,
    repair: Optional[Dict[str, Tuple[int, int]]] = None,
) -> None:
    """Refuse (``FLX-6204``) an enforced entry whose date the declared ranges can leave without a value (ADR 0024 section 5).

    A date is drawn inside ``J ∩ R``, ``J`` the parent's lifetime cut at the window ``W`` and ``R``
    the column's own declared range, so a row where the two do not meet has no date that holds the
    entry and that stays inside ``R``. An entry with no target admits no such row. With ``Rs`` the
    parent start column's declared range and ``e_min`` the smallest end the parent's lifetime can
    take (:func:`_parent_bounds`), ``J ∩ R`` is never empty when all three hold: ``Rs.max`` is at
    most ``min(R.max, W.end)``, ``e_min`` is at least ``max(R.min, W.start)``, and ``R`` meets ``W``.
    Where the parent's lifetime has no end, ``e_min`` bounds nothing. Several entries for one
    column are checked as one, on the latest start and the earliest end of all of them. A period is
    checked on its start so, and on its end too, against the end column's range, whenever a
    parent's end can fall below the range's minimum. A column that declares no range, and a column
    whose entries all carry a target, are not refused: a row that has no value is counted
    ``infeasible`` there.

    With *repair*, a column whose three conditions fail, and that a wider range would meet, is
    not refused: the range that meets them (:func:`_widened_range`) is recorded under the column's
    name, in the column's units. The period's end is still refused.
    """
    by_column: Dict[str, List[Tuple[WithinLevel, ResolvedEmit]]] = {}
    for held in levels:
        by_column.setdefault(held[0].column, []).append(held)
    for column, group in by_column.items():
        entries = [level for level, _ in group]
        if not any(not positive_target(level.target) for level in entries):
            continue
        terms = _intersection_terms(group, window, where)
        if terms is None:
            continue
        if repair is not None and _repaired(resolved, terms, column, repair):
            continue
        reason = _empty_intersection_reason(resolved, terms)
        if reason is None:
            continue
        raise Expr2Error(
            FLX_6204,
            f"{where}: the within entries of {resolved.expose_id!r} for {column!r} are enforced, "
            f"and the declared ranges allow a row where the parent's lifetime and the declared "
            f"range of {column!r} do not meet: {reason}. Widen the declared range of the column, "
            "or give the entry a target, which counts such a row as infeasible",
        )


@dataclass(frozen=True)
class _IntersectionTerms:
    """What the declared ranges say of one enforced date column, in the column's units."""

    kind: str
    low: int
    high: int
    frame: Optional[Tuple[int, int]]
    latest_start: Optional[int]
    earliest_end: Optional[int]
    ends: bool
    known_start: bool
    known_end: bool
    #: For each period entry whose parent has an end, the smallest end in the column's units.
    period_floors: Tuple[Tuple[str, Optional[int]], ...]


def _extreme(values: Sequence[int], pick: Callable[[Sequence[int]], int]) -> Optional[int]:
    """The largest or smallest of *values* (as *pick* says), or ``None`` when there are none."""
    return pick(values) if values else None


def _intersection_terms(
    group: Sequence[Tuple[WithinLevel, ResolvedEmit]],
    window: Optional[Tuple[int, int]],
    where: str,
) -> Optional[_IntersectionTerms]:
    """The terms of the three conditions for the entries of one column, or ``None`` when it declares no range."""
    first = group[0][0]
    if first.bounds is None:
        return None
    kind = first.kind
    low, high = first.bounds
    starts: List[Optional[int]] = []
    ends_found: List[Tuple[WithinLevel, Optional[int]]] = []
    for level, parent in group:
        start_range, parent_ends, floor, start_kind, end_kind = _parent_bounds(parent, where)
        starts.append(
            None
            if start_range is None
            else bound_in_units(start_range[1], start_kind, kind, end=False)
        )
        if parent_ends:
            ends_found.append(
                (
                    level,
                    None if floor is None else bound_in_units(floor, end_kind, kind, end=True),
                )
            )
    latest_start = _extreme([value for value in starts if value is not None], max)
    earliest_end = _extreme([value for _, value in ends_found if value is not None], min)
    period_floors = [
        (".".join(level.relationship), end) for level, end in ends_found if level.period
    ]
    return _IntersectionTerms(
        kind,
        low,
        high,
        window_in_units(kind, window),
        latest_start,
        earliest_end,
        bool(ends_found),
        all(value is not None for value in starts),
        all(value is not None for _, value in ends_found),
        tuple(period_floors),
    )


def _repaired(
    resolved: ResolvedEmit,
    terms: _IntersectionTerms,
    column: str,
    repair: Dict[str, Tuple[int, int]],
) -> bool:
    """Record the ranges that meet every condition for *column*, and say whether wider ranges do.

    The column's own range is widened for the three conditions (:func:`_widened_range`). A period
    also needs its end column's minimum at or below the earliest end of the parent's lifetime, so
    that range is lowered to it. Nothing is recorded when a condition no range repairs fails.
    """
    found: Dict[str, Tuple[int, int]] = {}
    if _reason_of(terms) is not None:
        widened = _widened_range(terms)
        if widened is None:
            return False
        found[column] = widened
    if terms.period_floors:
        end_column = _period_end_column(resolved)
        end_range = declared_bounds_of(resolved).get(end_column)
        floors = [floor for _, floor in terms.period_floors]
        if end_range is not None:
            if any(floor is None for floor in floors):
                return False
            lowest = min(floor for floor in floors if floor is not None)
            if lowest < end_range[0]:
                found[end_column] = (lowest, end_range[1])
    repair.update(found)
    return True


def _reason_of(terms: _IntersectionTerms) -> Optional[str]:
    """Why ``J ∩ R`` may be empty on the three conditions, or ``None`` when they hold.

    ``J`` is the lifetime cut at the window, the whole lifetime when it ended before the window, or
    its start alone when it starts after it: in every case ``J`` meets ``R`` once the parent starts
    no later than ``R``'s end, ends no earlier than ``R``'s start, and ``R`` meets the window. The
    window's own ends need not bound the start or the end.
    """

    def text(value: int) -> str:
        return _day_text(terms.kind, value)

    top, bottom = terms.high, terms.low
    if not terms.known_start or terms.latest_start is None or terms.latest_start > top:
        shown = (
            "not declared"
            if not terms.known_start or terms.latest_start is None
            else text(terms.latest_start)
        )
        return (
            f"the latest start of the parent's lifetime is {shown}, which must not be after "
            f"{text(top)}"
        )
    if terms.ends and (
        not terms.known_end or terms.earliest_end is None or terms.earliest_end < bottom
    ):
        shown = (
            "not known"
            if not terms.known_end or terms.earliest_end is None
            else text(terms.earliest_end)
        )
        return (
            f"the earliest end of the parent's lifetime is {shown}, which must not be before "
            f"{text(bottom)}"
        )
    frame = terms.frame
    if frame is not None and (terms.low > frame[1] or terms.high < frame[0]):
        return (
            f"the declared range {text(terms.low)} to {text(terms.high)} does not meet the run's "
            "time bounds"
        )
    return None


def _widened_range(terms: _IntersectionTerms) -> Optional[Tuple[int, int]]:
    """The smallest range holding the declared one that meets the three conditions, or ``None``.

    The minimum becomes the least of the declared minimum and the parent's earliest end, the
    maximum the greatest of the declared maximum and the parent's latest start, and the result is
    clipped so that it still meets the window. ``None`` when a start or an end the conditions
    read is not known, which no range repairs, and when the declared range already holds them.
    """
    if not terms.known_start or terms.latest_start is None:
        return None
    if terms.ends and (not terms.known_end or terms.earliest_end is None):
        return None
    low, high = terms.low, terms.high
    if terms.ends and terms.earliest_end is not None:
        low = min(low, terms.earliest_end)
    high = max(high, terms.latest_start)
    if terms.frame is not None:
        low, high = min(low, terms.frame[1]), max(high, terms.frame[0])
    return None if (low, high) == (terms.low, terms.high) else (low, high)


def _empty_intersection_reason(resolved: ResolvedEmit, terms: _IntersectionTerms) -> Optional[str]:
    """Why ``J ∩ R`` may be empty for the entries of one column, or ``None`` when the three conditions hold."""
    reason = _reason_of(terms)
    if reason is not None:
        return reason
    if not terms.period_floors:
        return None
    end_range = declared_bounds_of(resolved).get(_period_end_column(resolved))
    for relationship, end in terms.period_floors:
        if end_range is not None and (end is None or end < end_range[0]):
            shown = "not known" if end is None else _day_text(terms.kind, end)
            return (
                f"the period's end is cut at the end of {relationship!r}, the earliest of which is "
                f"{shown}, below the {_day_text(terms.kind, end_range[0])} that its end column "
                "declares as its minimum"
            )
    return None


def _period_end_column(resolved: ResolvedEmit) -> str:
    """The end column of *resolved*'s lifetime, which a period's end sets."""
    end = lifetime_entries_of(resolved.extension, resolved.expose_id)[0].end
    assert end is not None  # a period was refused without one
    return end


def _refuse_crossed_entries(
    resolved: ResolvedEmit, levels: Sequence[WithinLevel], where: str
) -> None:
    """Refuse (``FLX-3105``) the entries for one date column, periods included, that cannot be drawn together.

    A period is an entry on its lifetime's start column, so it counts with the date entries for
    that column: one relationship once, every relationship a prefix of the longest, at most
    one ``offset`` (ADR 0024 section 5) and at most one ``target`` above zero (section 7), since a
    violating row is a violation of one entry and holds every other. The block's own reading has
    checked each kind alone.
    """
    by_column: Dict[str, List[WithinLevel]] = {}
    for level in levels:
        by_column.setdefault(level.column, []).append(level)
    for column, group in by_column.items():
        names = [".".join(level.relationship) for level in group]
        longest = max((level.relationship for level in group), key=len)
        problem = None
        if len(set(names)) != len(names):
            problem = "names one relationship twice"
        elif any(level.relationship != longest[: len(level.relationship)] for level in group):
            problem = (
                f"has entries that do not lie on one chain (each a prefix of {'.'.join(longest)!r})"
            )
        elif sum(level.offset is not None for level in group) > 1:
            problem = "has more than one offset"
        elif sum(positive_target(level.target) for level in group) > 1:
            problem = "has more than one target"
        if problem is not None:
            raise FluxError(
                FLX_3105,
                f"{where}: the within entries of {resolved.expose_id!r} for {column!r}, a period "
                f"included, {problem}",
            )


def _refuse_untied_chains(
    resolved: ResolvedEmit,
    levels: Sequence[Tuple[WithinLevel, ResolvedEmit]],
    where: str,
) -> None:
    """Refuse (``FLX-6204``) entries for one date along a chain whose middle exposes are not tied to the next level.

    A date in level 1, ``[a.s, a.e]``, is inside level 2, ``[g.s, g.e]``, on every row only when
    ``a.s >= g.s`` and ``a.e <= g.e`` whenever ``g`` has ended. A tie on the first middle row's
    start alone (a ``within`` entry for that date) puts the levels' meeting point, ``a.s``, in
    level 2, which is all that two levels need. Every deeper middle expose must be tied by a
    period (``lifetime: true``) through the relationships to the next level, so that the middle
    row's whole lifetime lies in the next level's and ``a.s`` lies in every level.

    A tie that carries a target above zero lets a middle row leave its parent, so the levels'
    lifetimes need not meet on a row (ADR 0024 section 5). Every entry further out on that chain
    must then carry a target as well, else the row that does not meet would violate an enforced
    entry (``FLX-6204``); one target per column (:func:`_refuse_crossed_entries`) leaves such a
    chain two levels.
    """
    by_column: Dict[str, List[Tuple[WithinLevel, ResolvedEmit]]] = {}
    for held in levels:
        by_column.setdefault(held[0].column, []).append(held)
    for column, group in by_column.items():
        ordered = sorted(group, key=lambda held: len(held[0].relationship))
        for position, (near_held, far_held) in enumerate(zip(ordered, ordered[1:], strict=False)):
            _refuse_untied_step(resolved, where, column, ordered, position, near_held, far_held)


def _refuse_untied_step(
    resolved: ResolvedEmit,
    where: str,
    column: str,
    ordered: Sequence[Tuple[WithinLevel, ResolvedEmit]],
    position: int,
    near_held: Tuple[WithinLevel, ResolvedEmit],
    far_held: Tuple[WithinLevel, ResolvedEmit],
) -> None:
    """Refuse (``FLX-6204``) one step of a chain, *near_held* to *far_held*, when its middle expose is not tied.

    The middle expose is tied by a period, or, at the first step only, by a ``within`` entry for
    the near level's start. A tie that carries a target needs a target on every level further out.
    """
    near, middle = near_held
    far = far_held[0]
    remaining = far.relationship[len(near.relationship) :]
    entries = within_entries_of(middle.extension, middle.expose_id)
    ties = [
        e
        for e in entries
        if e.relationship == remaining and (e.lifetime or (position == 0 and e.date == near.start))
    ]
    if ties:
        if any(positive_target(e.target) for e in ties):
            _require_outer_targets(
                resolved, where, column, middle, remaining, ordered[position + 1 :]
            )
        return
    needs = (
        "a within entry for its start or a period (lifetime: true)"
        if position == 0
        else "a period (lifetime: true), since a date tie on its start keeps the "
        "row after the outer start and not before its end"
    )
    raise Expr2Error(
        FLX_6204,
        f"{where}: {resolved.expose_id!r} is within {'.'.join(near.relationship)!r} and "
        f"{'.'.join(far.relationship)!r} for {column!r}, and {middle.expose_id!r} needs "
        f"{needs} through {'.'.join(remaining)!r}; without that tie the lifetimes of the "
        "levels need not meet, so the entries could not all hold",
    )


def _require_outer_targets(
    resolved: ResolvedEmit,
    where: str,
    column: str,
    middle: ResolvedEmit,
    remaining: Tuple[str, ...],
    outer_levels: Sequence[Tuple[WithinLevel, ResolvedEmit]],
) -> None:
    """Refuse (``FLX-6204``) a targeted tie when a level further out carries no target."""
    untargeted = [outer for outer, _ in outer_levels if not positive_target(outer.target)]
    if untargeted:
        raise Expr2Error(
            FLX_6204,
            f"{where}: {middle.expose_id!r} ties its row to "
            f"{'.'.join(remaining)!r} with a target, so a row of it may lie "
            f"outside its parent, and {resolved.expose_id!r} must carry a target "
            f"for {column!r} through "
            f"{'.'.join(untargeted[0].relationship)!r} as well: an enforced "
            "entry would be violated by a row the generator made",
        )


def period_of(
    resolved: ResolvedEmit, levels: Sequence[WithinLevel], duration: Optional[Duration]
) -> Optional[Period]:
    """The end of *resolved*'s lifetime as a period, when its ``within`` *levels* hold one (ADR 0024 section 5), else ``None``.

    The levels of the period entries cut the end the lifetime draws: the duration's when it has
    one, otherwise the end column's own cell. ``endPastWindow`` takes the contract's default as
    :func:`lifetime_duration_of` does.
    """
    held = tuple(level for level in levels if level.period)
    if not held:
        return None
    entry = lifetime_entries_of(resolved.extension, resolved.expose_id)[0]
    assert entry.end is not None  # within_levels_of refused a period without an end
    # A target on any entry for the start column makes a row's start able to leave its parents'.
    start_targeted = any(
        level.column == entry.start and positive_target(level.target) for level in levels
    )
    if duration is not None:
        return Period(
            entry.start,
            entry.end,
            duration.kind,
            duration.end_past_window,
            held,
            duration,
            start_targeted,
            declared_bounds_of(resolved).get(entry.end),
        )
    columns = {column.name: column for column in resolved.compile_planned().columns}
    end = columns[entry.end]
    return Period(
        start=entry.start,
        end=entry.end,
        kind=KIND_DATE if end.logical_type.kind is LogicalKind.DATE else KIND_TIMESTAMP,
        end_past_window=entry.end_past_window or ("kept" if end.required else "open"),
        levels=held,
        start_targeted=start_targeted,
        end_range=declared_bounds_of(resolved).get(entry.end),
    )


def plan_emit_rules(
    resolved: ResolvedEmit,
    *,
    strict: bool,
    where: str,
    counter_based: bool,
    counted: FrozenSet[Tuple[str, str]] = frozenset(),
    emits: Sequence[ResolvedEmit] = (),
    windowed: bool = True,
    time_bounds: Optional[Mapping[str, Any]] = None,
) -> Optional[RulePlan]:
    """The contract's rules for *resolved*'s expose, planned as ``plan`` plans them.

    One call for ``plan``, ``validate`` and ``fluxsim init``'s pre-flight, so a
    rule ``plan`` refuses (EXPR-SPEC-2 §12) is refused by all three. *counter_based* says that
    the run is kernel 0.2.0's, which alone generates a rule that reads through a link. *counted*
    is the relationships the Simulation lays out with a ``counts`` entry
    (:func:`counted_relationships`), the ones a summary over children generates over. *emits* is
    every emit of the Simulation, which a ``within`` entry reads its parent's lifetime from,
    *windowed* says that the run declares ``spec.timeBounds``, and *time_bounds* is that pair, which
    the plan's check of the declared ranges reads (:func:`within_levels_of`).
    A projection key is refused here until its emits generate (ADR 0027).
    """
    refuse_projection(
        resolved.extension, counter_based=counter_based, where=where, target=resolved.expose_id
    )
    rates = rates_of(resolved)
    if rates and not counter_based:
        raise FluxError(
            FLX_3105,
            f"{where}: emit {resolved.expose_id!r} declares rates, which kernel 0.2.0 "
            "generates; this run is on kernel 0.1.0",
        )
    within = within_levels_of(
        resolved, emits or (resolved,), where=where, window=window_of(time_bounds)
    )
    duration = lifetime_duration_of(resolved, where=where)
    if within and not counter_based:
        raise FluxError(
            FLX_3105,
            f"{where}: emit {resolved.expose_id!r} declares within, which kernel 0.2.0 "
            "generates; this run is on kernel 0.1.0",
        )
    return plan_rules(
        contract_document=resolved.planning_document,
        expose_id=resolved.expose_id,
        settings=resolved.invariant_settings,
        related=_related_of(resolved),
        mapping=resolved.column_mapping,
        has_fidelity=resolved.fidelity is not None or resolved.generated_by_plugin,
        strict=strict,
        where=where,
        flux_rules=resolved.flux_side_rules,
        keys=resolved.keys,
        rows=resolved.declared_row_count,
        null_fractions=_declared_null_fractions(resolved),
        counter_based=counter_based,
        links=link_entries_of(resolved.extension, resolved.expose_id),
        copies=copies_of(resolved.extension),
        counted=counted,
        duration=duration,
        within=within,
        windowed=windowed,
        period=period_of(resolved, within, duration),
        rates=rates,
        history=history_entries_of(resolved.extension, resolved.expose_id),
        asof=asof_entries_of(resolved.extension, resolved.expose_id),
        history_emits=resolved.history_emits,
    )


def emit_not_generated(
    resolved: ResolvedEmit, *, where: str, counter_based: bool
) -> Tuple[NotGenerated, ...]:
    """What *resolved*'s expose declares that this version accepts and does not generate.

    *counter_based* says that the run is kernel 0.2.0's, which copies a ``sharedColumns`` or
    ``sharedAttribute`` into an emit it generates from statistics when the Simulation declares
    ``copies: true``.
    """
    return declared_not_generated(
        contract_document=resolved.planning_document,
        expose_id=resolved.expose_id,
        settings=resolved.invariant_settings,
        flux_rules=resolved.flux_side_rules,
        where=where,
        counter_based=counter_based,
        has_fidelity=resolved.fidelity is not None or resolved.generated_by_plugin,
        related=related_columns(resolved.keys),
        copies=copies_of(resolved.extension),
        enforced_checks=resolved.generated_by_plugin,
        history_emits=resolved.history_emits,
    )


def refuse_determined_columns(
    resolved: ResolvedEmit,
    rules: Optional[RulePlan],
    *,
    sim_id: str,
    simulation: Mapping[str, Any],
) -> None:
    """Refuse what ``plan`` refuses about the columns *rules* determine, before a row exists.

    :func:`~flux_engine.generator.build_plan` refuses a column drawn ``given`` a determined
    one, a solved column drawn ``given`` another, and a dependency's ``to`` column with
    nothing to draw once per key (EXPR-SPEC-2 sections 8 and 9). ``validate`` builds the
    same plan for an emit whose rules determine a column, with the init pre-flight's
    placeholder digest and row count, and raises only the rules' refusals, so the three
    refuse those columns alike and validate refuses nothing else it did not before.
    """
    if rules is None or not rules.order or resolved.fidelity is None:
        return
    try:
        build_plan(
            resolved.compile_planned(),
            sim_id=sim_id,
            seam_digest="sha256:" + "0" * 64,
            rows=resolved.declared_row_count or 1,
            fidelity=resolved.fidelity,
            time_bounds=simulation.get("spec", {}).get("timeBounds"),
            related=_related_of(resolved),
            rules=rules,
        )
    except Expr2Error:
        raise
    except FluxError:
        # A column the plan cannot build at all (FLX-3402 and kin) is `plan`'s
        # refusal, which validate leaves to it as it does for an emit without rules.
        return


def check_relations_declared(
    resolved_emits: Sequence[ResolvedEmit],
    simulation: Mapping[str, Any],
    sim_id: str,
    strict: bool,
    plugins: Optional[Mapping[str, PluginExpose]] = None,
) -> None:
    """Refuse the counts and weights ``plan`` refuses when it lays the relationships out, offline.

    One call into :func:`_relate_emits`, the relation step ``plan`` runs, over plans built as
    ``validate`` builds them, with the init pre-flight's placeholder digest. A bundle whose
    relation checks read what only a run knows is left to ``plan``: an emit that takes its rows
    from the run (no declared ``rows``) and a reference expose. An emit's plan is built as
    ``plan`` builds it, so what the plan refuses (an allocation on a key, a ``nullFraction`` a
    column cannot hold, no rows for a parent to reference) is refused here with ``plan``'s code.
    So it refuses nothing ``plan`` accepts.
    """
    counter_based = _kernels.new_plan_is_counter_based()
    version = GENERATOR_VERSION_2 if counter_based else GENERATOR_VERSION
    planned: List[_PlannedEmit] = []
    for resolved in resolved_emits:
        plugin = (plugins or {}).get(resolved.expose_id)
        if resolved.is_reference or (
            (resolved.fidelity is not None or plugin is not None)
            and resolved.declared_row_count is None
        ):
            return
        expose = resolved.compile()
        if resolved.fidelity is None and plugin is None:
            planned.append(
                _PlannedEmit(
                    expose=expose,
                    plan=None,
                    mapping=None,
                    coverage=ExposeCoverage(expose.expose_id, ()),
                )
            )
            continue
        rules = plan_emit_rules(
            resolved,
            strict=strict,
            where=sim_id,
            counter_based=counter_based,
            counted=counted_relationships(simulation, where=sim_id),
            emits=resolved_emits,
            windowed=simulation.get("spec", {}).get("timeBounds") is not None,
            time_bounds=simulation.get("spec", {}).get("timeBounds"),
        )
        built = build_plan(
            resolved.compile_planned(),
            sim_id=sim_id,
            seam_digest="sha256:" + "0" * 64,
            rows=resolved.declared_row_count or 0,
            fidelity=resolved.fidelity,
            time_bounds=simulation.get("spec", {}).get("timeBounds"),
            related=_related_of(resolved),
            rules=rules,
            generator_version=version,
            plugin=plugin,
            uncut=uncut_columns(resolved, resolved_emits),
        )
        planned.append(
            _PlannedEmit(expose=expose, plan=built, mapping=None, coverage=coverage_of_plan(built))
        )
    _relate_emits(resolved_emits, tuple(planned), _flux_namespace(simulation))


def parse_declared_fidelity(resolved: ResolvedEmit) -> Optional[FidelityBlock]:
    """Parse *resolved*'s fidelity block as ``verify`` does; ``None`` when it declares none.

    SEAM-FIDELITY-SPEC §2 puts the block's rules on ``fluxsim validate``,
    offline. They are :func:`~flux_engine.concordance.predicate.parse_fidelity_block`'s,
    the one parser ``verify`` and ``ship`` compile with, and it is given the
    block in the canonical form ``apply`` records for them: a YAML ``null:``
    key is the string ``"null"`` there, and a decimal is its string
    spelling. So a malformed property is refused before any run exists, with
    the code and the message ``verify`` gives it. Nothing here forms a
    statistic.
    """
    return None if resolved.fidelity is None else _parsed_fidelity(resolved)


def _parsed_fidelity(resolved: ResolvedEmit) -> FidelityBlock:
    """*resolved*'s declared fidelity block, parsed; a snapshot's may declare no column."""
    return parse_fidelity_block(
        canonicalise(resolved.written_fidelity),
        expose=_declared_contract(resolved)[1],
        allow_no_columns=may_declare_no_column(resolved.extension),
    )


def compile_declared_fidelity(resolved: ResolvedEmit) -> Optional[CompiledFidelity]:
    """Compile *resolved*'s fidelity block as ``verify`` does; ``None`` when it declares none.

    SEAM-FIDELITY-SPEC §3 computes every property's ``ε`` at plan time and
    refuses one that is not smaller than its tolerance (``FLX-5002``), and
    KERNEL-SPEC §12 bounds the intermediates at compile time (``FLX-5001``).
    Both belong to :func:`~flux_engine.concordance.compiler.compile_fidelity`,
    called here with what ``verify`` and ``ship`` call it with, and it parses
    the block with the parser :func:`parse_declared_fidelity` uses. It reads
    declarations only: no committed slice, no statistic.
    """
    if resolved.fidelity is None:
        return None
    contract, expose = _declared_contract(resolved)
    return compile_fidelity(
        contract=contract,
        emit=canonicalise(resolved.emit),
        expose=expose,
        extension=canonicalise(dict(resolved.extension or {})),
    )


@contextmanager
def _memos_for_one_run() -> Iterator[None]:
    """Clear the draws' memos when the run inside ends, however it ends."""
    try:
        yield
    finally:
        clear_draw_memos()
        kernel2.clear_memos()
        clear_generator_memos()


@dataclass(frozen=True)
class PlanEra:
    """What a recorded plan was made under, so that ``apply`` and ``replay`` run it as it was.

    A new plan takes the defaults; a recorded plan takes what its keys say
    (:func:`flux_engine.stages.apply.plan_end_share`, ``plan_date_ranges`` and ``plan_scale_floors``).

    *end_share* is what a history's ``end`` share is a share of (ADR 0017, the amendment of
    2026-11-24): every entity, or the entities that can end for a plan without ``historyEndShare``.
    *cut_ranges* is false for a plan without ``dateRanges`` (ADR 0024): it replays uncut.
    *tracked_levels* is false for a plan without ``scaleFloors`` (ADR 0017, the amendment of
    2026-11-25): its tracked foreign keys are not checked and no parent is kept at more rows than
    the scale gives it. Otherwise a ``--scale`` below 1 that cuts a drawn parent below a key a
    history's tracked level names runs again with that parent kept at the fewest rows that hold
    every level; *scale_floors* is what that has settled so far.
    """

    end_share: str = END_SHARE_OF_ENTITIES
    cut_ranges: bool = True
    tracked_levels: bool = True
    scale_floors: Mapping[str, ScaleFloor] = field(default_factory=dict)


def _execute_with_parent_kept(
    call: Mapping[str, Any], cut: TrackedLevelsCut, declared_emits: Sequence[ResolvedEmit]
) -> PipelineResult:
    """Run *call* again with the parent *cut* names kept at the rows its levels need, or refuse as it was.

    The refusal stands (*cut* is raised) at scale 1 or above, for a parent no floor is worked out
    for and for a refusal the floor already answered.
    """
    scale, era = call["scale"], call["era"]
    kept = None if scale is None else floor_for_cut(declared_emits, era.scale_floors, cut, scale)
    if kept is None:
        raise cut
    return execute(**{**call, "era": replace(era, scale_floors=kept)})


def _common_draw_digests(
    common_draws_of: Optional[Path],
    per_emit_digests: Sequence[SeamDigests],
    resolved_emits: Sequence[ResolvedEmit],
    *,
    simulation_id: Optional[str],
    scale: Optional[Decimal],
    tolerate_additive: bool,
    counter_based: bool,
) -> Tuple[SeamDigests, ...]:
    """The digests that seed this run: its own, or the ``H`` of the bundle *common_draws_of* names.

    ``H`` binds the fidelity block, so a candidate that moves one parameter would otherwise draw
    every row again. Each emit keeps its own ``G`` and takes the other bundle's ``H``, found by the
    emit's expose id and computed as this run computes it (the same scale), so the two bundles'
    rows differ only where a changed value decides them. An emit the other bundle does not have
    keeps its own ``H``.
    """
    if common_draws_of is None:
        return tuple(per_emit_digests)
    loaded = load_bundle(common_draws_of, tolerate_additive=tolerate_additive)
    cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
    base = resolve_all_emits(loaded, simulation_id=cast.simulation_id, counter_based=counter_based)
    if scale is not None and scale != 1:
        base = tuple(scale_emit(emit, scale) for emit in base)
    base_h = {
        emit.expose_id: seam_digests(
            emit.contract_document, emit.emit, emit.compile().column_names, emit.extension
        ).h
        for emit in base
    }
    return tuple(
        replace(digests, h=base_h.get(resolved.expose_id, digests.h))
        for resolved, digests in zip(resolved_emits, per_emit_digests, strict=True)
    )


def execute(
    directory: Any,
    *,
    simulation_id: Optional[str] = None,
    zone: Optional[str] = None,
    tolerate_additive: bool = True,
    jobs: int = 1,
    strict_coverage: bool = False,
    kernel: Optional[Kernel] = None,
    scale: Optional[Decimal] = None,
    shard: Optional[Tuple[int, int]] = None,
    profile: Union[str, Path, None] = None,
    references: Optional[Mapping[str, Any]] = None,
    allow_generators: Sequence[str] = (),
    state_dir: Union[str, Path, None] = None,
    require_staged: bool = False,
    stored_probe: Optional[Callable[[PipelineResult], bool]] = None,
    responses: Optional[Mapping[str, Any]] = None,
    replicate: Optional[Replicate] = None,
    common_draws_of: Optional[Path] = None,
    most_rows: Optional[int] = None,
    era: PlanEra = PlanEra(),
) -> PipelineResult:
    """Run every stage of the six-step loop's core once, in memory.

    Raises :class:`~flux_engine.core.errors.FluxError` (any code the
    kernel modules it calls can raise) on the first refusal; nothing is
    partially returned on failure.

    *strict_coverage* turns the coverage report into a refusal: a column that
    fell back to a type default raises ``FLX-3304`` naming every such column
    of every emit, before any row is drawn. Off, the same columns are
    reported on each outcome's ``coverage`` and the run goes on. The flag
    changes whether a run is accepted and never what an accepted run
    produces: every row and digest is the one the default gives. A Simulation
    that declares ``strictCoverage: true`` in its extensions asks for the same,
    and the flag cannot turn that declaration off: the run is strict when
    either asks.

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

    *kernel* is the kernel to run: the one a recorded plan names
    (:func:`flux_engine.stages.apply.plan_kernel`), or ``None`` for a new
    plan, which takes :data:`flux_engine.core.kernels.DEFAULT_KERNEL`. The
    result carries it, and every stage stamps the result's kernel.

    *scale* multiplies every emit's rows and key spans
    (:mod:`flux_engine.stages.scaling`): ``None`` or 1 leaves the bundle as it is.
    *shard* is ``(I, N)``: only the I-th of N contiguous shares of each generated
    emit's rows is made, and the result is not a whole run
    (:func:`flux_engine.stages.apply.apply` writes only the shares).

    *profile* is the profile document the Simulation pins and *references* the rows a recorded
    plan holds of each reference expose (ADR 0023 section 2): an emit that declares
    ``reference: true`` takes its rows from *references* when they are given, and from the
    pinned profile's ``references`` otherwise. Neither is read for a run with no reference expose.

    *era* is what a recorded plan was made under, so that it replays as it was (:class:`PlanEra`).

    *most_rows* bounds the rows the plans of every emit hold together, derived and hidden emits
    included. When their sum is above it, :class:`PlannedRowsAboveCeiling` is raised after the
    plans are made and before any row is drawn. ``None`` sets no bound and changes nothing.

    *allow_generators* names the generator plugins the person allows to load (``--allow-generator``,
    ADR 0032 section 3): a plugin the bundle declares and this does not name is refused
    (``FLX-3119``) before anything is looked up or imported. *state_dir* is the run-state
    directory, which no plugin's code may lie under; it is read for nothing else.

    *require_staged* is a plan made with ``--no-stage`` (ADR 0032 section 6): a plugin's run is
    stored and never generated again, so a Simulation that declares a plugin is refused
    (``FLX-3122``) before the person's word is read or any plugin code runs.
    *responses* is the estimates a recorded plan holds for the Simulation's response families
    (ADR 0029 section 2): a run that declares families takes its estimates from it when it is
    given, and from the pinned profile otherwise, so ``apply --run-id`` and ``replay`` need no
    profile. Neither is read for a Simulation with no ``responses``.

    *replicate* makes this run run ``r`` of the Simulation's sweep (ADR 0029 section 3): it is
    seeded as the sweep seeds its run ``r - 1`` (:func:`~flux_engine.outcomes.responses.replicate_seed`) and takes
    the effect the replicate names for each segment, or the midpoint of its part of the estimate's
    interval when the replicate names none. A recorded plan holds the effects, and ``replay``
    applies them as written.

    *stored_probe* is asked once everything a plan records is known and before any plugin is asked
    for rows, when the bundle declares one (ADR 0032 section 6): the result it is handed holds no
    plugin rows. When it answers ``True`` the result comes back with ``deferred`` set and no plugin
    was called for rows; otherwise the plugins are asked and the full result comes back.

    *common_draws_of* is a bundle directory whose per-emit ``H`` seeds every emit's rows and the
    run's seed root, in place of this bundle's own (:func:`_common_draw_digests`). Only
    ``calibrate --fit`` passes it, so that every candidate it evaluates draws the same rows
    (common random numbers, ADR 0028 section 8) and its queries differ by the candidate's values
    alone. The result's own digests are still this bundle's, so nothing replays from it; ``None``,
    the default, leaves every seed, row and digest as it was.

    However the run ends, the draws' memos are cleared when it does
    (:func:`flux_engine.core.rng.clear_memos`,
    :func:`flux_engine.generator.rows.clear_memos`): what they hold is worked
    out from this run's declarations, and a long-lived process that runs
    many bundles keeps none of it past the run.
    """
    call = dict(locals())  # the arguments, to run again with a parent kept
    require_jobs(jobs)
    # The forkserver's import of what a worker needs takes about 0.3 s; started here it runs
    # beside the load, the compile and the simulation below instead of after them. Nothing
    # starts at one job, and no worker is reserved: the pool is built later, as always.
    start_forkserver_early(jobs)
    run_kernel = _kernels.DEFAULT_KERNEL if kernel is None else kernel
    if run_kernel.seed_tree_version != kernel2.SEED_TREE_VERSION:
        # New generation features run on kernel 0.2.0 only: 0.1.0's draws are frozen.
        if scale is not None and scale != 1:
            raise FluxError(
                FLX_3405, f"--scale runs on kernel 0.2.0 only, not {run_kernel.version}"
            )
        if shard is not None:
            raise FluxError(
                FLX_3404, f"--shard runs on kernel 0.2.0 only, not {run_kernel.version}"
            )
    with _memos_for_one_run(), localcontext(kernel_context()):
        if run_kernel.seed_tree_version == kernel2.SEED_TREE_VERSION:
            # Kernel 0.2.0's continuous draws are binary64: refuse a process that would
            # compute other bits before anything is drawn (FLX-5050).
            kernel2.require_subnormals()
        lap("start")
        loaded = load_bundle(directory, tolerate_additive=tolerate_additive)
        lap("load")
        cast = resolve_cast(loaded.flux_documents, simulation_id=simulation_id)
        simulation = cast.simulation
        world_doc = cast.world

        fidelity = str(simulation.get("spec", {}).get("fidelity", "deterministic"))
        require_supported_fidelity(fidelity)

        sovereignty_audit = _sovereignty_audit_of(cast, simulation, world_doc, zone)

        # The two declarations the FLUX schema leaves optional and this engine
        # cannot run without, refused in the order `validate` checks them,
        # before the population exists (FLX-5042, FLX-5023).
        pack = pack_of_world(world_doc)
        start, end = declared_time_bounds(simulation)
        model: AttributeModel = build_attribute_model(
            _BundleView(loaded.flux_documents), pack, simulation_id=cast.simulation_id
        )

        resolved_emits = resolve_all_emits(
            loaded,
            simulation_id=cast.simulation_id,
            counter_based=run_kernel.seed_tree_version == kernel2.SEED_TREE_VERSION,
        )

        unscaled_refuses = functools.partial(
            _refuses_unscaled,
            directory,
            {
                "simulation_id": simulation_id,
                "zone": zone,
                "tolerate_additive": tolerate_additive,
                "strict_coverage": strict_coverage,
                "kernel": kernel,
                "shard": shard,
                "profile": profile,
                "references": references,
                "allow_generators": allow_generators,
                "state_dir": state_dir,
                "require_staged": require_staged,
                "responses": responses,
                "replicate": replicate,
                "era": era,
            },
        )

        declared_rows = {emit.expose_id: emit.declared_row_count for emit in resolved_emits}
        widened = _widened_by(resolved_emits, scale)
        declared_emits = resolved_emits
        if scale is not None and scale != 1:
            resolved_emits = scale_emits(resolved_emits, scale, era.scale_floors)
        check_flux_side_rule_ids(resolved_emits, where=cast.simulation_id)
        check_copies_declared(
            simulation,
            where=cast.simulation_id,
            counter_based=run_kernel.seed_tree_version == kernel2.SEED_TREE_VERSION,
        )
        check_hidden_declared(
            resolved_emits,
            where=cast.simulation_id,
            counter_based=run_kernel.seed_tree_version == kernel2.SEED_TREE_VERSION,
        )
        _check_declared_blocks(
            simulation,
            resolved_emits,
            where=cast.simulation_id,
            counter_based=run_kernel.seed_tree_version == kernel2.SEED_TREE_VERSION,
        )
        check_overlay_declared(
            simulation,
            where=cast.simulation_id,
            counter_based=run_kernel.seed_tree_version == kernel2.SEED_TREE_VERSION,
        )
        # Governance (ADR 0032 section 3): the grades the declared plugins state are checked
        # against every labelled column's minimum before anything else is done for the run.
        require_declared_grade(resolved_emits, simulation, where=cast.simulation_id)
        # Then the person's word and the plugin's code (ADR 0032 section 3, steps 3 to 6): a plugin
        # the command did not name is refused, and the rest is looked up and loaded only now.
        declared_plugins = declared_generators(simulation, where=cast.simulation_id)
        if require_staged and declared_plugins:
            raise FluxError(
                FLX_3122,
                f"{cast.simulation_id}: the bundle declares generator plugins for "
                f"{sorted(item.expose_id for item in declared_plugins)}, and their bytes are "
                "stored, not generated again, so the plan must stage them; plan without "
                "--no-stage",
            )
        loaded_generators = load_declared(
            declared_plugins,
            allow_generators,
            forbidden_roots=forbidden_roots(directory, Path(state_dir) if state_dir else None),
        )
        plugins = plugin_exposes(resolved_emits, declared_plugins)
        published = _published_rows(
            resolved_emits,
            simulation,
            where=cast.simulation_id,
            counter_based=run_kernel.seed_tree_version == kernel2.SEED_TREE_VERSION,
            profile=profile,
            recorded=references,
        )
        # SEAM-FIDELITY-SPEC §3: a declared block is compiled at plan time,
        # before the population exists, so a block `verify` would refuse is
        # refused here, with its code and message. The compiled blocks are
        # kept: `plan.json` records each one, epsilons included.
        compiled_blocks = tuple(compile_declared_fidelity(resolved) for resolved in resolved_emits)
        # Parameter register: the source of every number the run reads, checked against the
        # run's documents before the population exists, so an entry naming
        # nothing is refused before any row is drawn. After the blocks are
        # compiled, as `validate` does, so a bundle wrong in both ways is refused
        # with the same code by every command.
        parameters = parameter_register_of(loaded, cast)
        families, response_estimates, parameters, replicate = _response_setup(
            simulation,
            parameters,
            replicate,
            where=cast.simulation_id,
            profile=profile,
            recorded=responses,
        )
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
        if replicate is not None:
            seed = replicate_seed(simulation, seed, replicate)
        seed_digests = _common_draw_digests(
            common_draws_of,
            per_emit_digests,
            resolved_emits,
            simulation_id=simulation_id,
            scale=scale,
            tolerate_additive=tolerate_additive,
            counter_based=run_kernel.seed_tree_version == kernel2.SEED_TREE_VERSION,
        )
        root = derive_root(
            seam_entropy_bytes(list(seed_digests)),
            seed,
            seed_tree_version=run_kernel.seed_tree_version,
        )

        lap("compile")
        trace = TraceLog()
        seed_tree = SeedTree(root=root)

        spec = PopulationSpec.from_document(world_doc)
        personas = [cast.document(doc_id) for doc_id in cast.ids("Persona")]
        derivation = derivation_for(spec.vertical)
        population = materialise_population(
            world=world_doc,
            personas=personas,
            pack=pack,
            seed_tree=seed_tree,
            trace=trace,
            derivation=derivation,
        )

        clock = VirtualClock.from_time_bounds(start, end)
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
        campaign_ids = cast.ids("Campaign")
        # A response family (ADR 0029) needs the experiment's assignment before the journeys.
        # It reads only the cast and its own stream, so it is the assignment it was.
        early, compiled_early = _early_assignments(
            families,
            response_estimates,
            replicate,
            cast=cast,
            model=model,
            population=population,
            root=root,
            trace=trace,
        )
        transitions = run_journeys(
            cast=cast,
            world=spec,
            journeys=compile_journeys(cast, model),
            events=events,
            store=population,
            seed_tree=seed_tree,
            trace=trace,
            probability=_probability_of(early),
        )

        ledger: Optional[DeliveryLedger] = None
        if campaign_ids:
            policy = _campaign_policy(early, cast, campaign_ids[0], model)
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

        experiments = compiled_early if compiled_early is not None else compile_experiments(cast)
        experiment: Optional[CompiledExperiment] = experiments[0] if experiments else None
        assignment, counts = _assign_and_count(
            early, experiment, eligible, root, trace, transitions
        )

        lap("simulate")
        scenario = _Scenario.build(
            store=population,
            detections=detections,
            events=events,
            cast=cast,
            pack=pack,
            derivation=derivation,
            personas=personas,
        )
        # Every emit is planned before any emit's rows are drawn, so a column
        # no declaration reaches is refused (`strict_coverage`) before a row
        # exists, with every emit's gaps in the one message.
        asked_by = strictness_asked_by(
            flag=strict_coverage,
            declared=strict_coverage_declared(simulation, where=cast.simulation_id),
        )
        try:
            with _scale_noted(scale, unscaled_refuses, declared_rows):
                planned_emits = _planned_with_projections(
                    resolved_emits,
                    seed_digests,
                    cast,
                    simulation,
                    scenario,
                    generator_version_for(run_kernel.seed_tree_version),
                    strict=bool(asked_by),
                    published=published,
                    plugins=plugins,
                    root=root,
                    era=era,
                )
        except TrackedLevelsCut as cut:
            return _execute_with_parent_kept(call, cut, declared_emits)
        _require_coverage(planned_emits, asked_by)
        lap("plan emits")
        _require_rows_within(planned_emits, most_rows)
        # Step 7: each plugin states what it rests on, before any row is drawn.
        _check_statements(loaded_generators, planned_emits, root, sim_id=cast.simulation_id)
        written = [index for index, entry in enumerate(resolved_emits) if not entry.is_hidden_table]
        # Positions in `emits` (the written emits only) of the plugin exposes.
        plugin_indexes = [
            position
            for position, index in enumerate(written)
            if (plan := planned_emits[index].plan) is not None and plan.plugin is not None
        ]
        probing = stored_probe is not None and bool(plugin_indexes)
        with _scale_noted(scale, unscaled_refuses, declared_rows):
            emits = _generate_emits(
                [resolved_emits[index] for index in written],
                [per_emit_digests[index] for index in written],
                [planned_emits[index] for index in written],
                [compiled_blocks[index] for index in written],
                root,
                scenario,
                jobs,
                shard,
                loaded_generators,
                defer_plugins=probing,
            )
        hidden_emits = tuple(
            HiddenOutcome(
                resolved=resolved_emits[index],
                digests=per_emit_digests[index],
                plan=_hidden_plan(planned_emits[index]),
                compiled_fidelity=compiled_blocks[index],
            )
            for index in range(len(resolved_emits))
            if index not in set(written)
        )

        decisions = DecisionAuditLog()  # v0.1: no agentic skill calls to audit yet
        trace_digests = trace.component_digests()
        input_digest = input_digest_of(loaded, kernel=run_kernel)

        def finished(made: Tuple[EmitOutcome, ...], deferred: bool) -> PipelineResult:
            # One pass over the trace gives all five digests; asking for each in turn
            # was five passes.
            components = RunComponents(
                population=trace_digests["population"],
                events=trace_digests["events"],
                journeys=trace_digests["journeys"],
                delivery=trace_digests["delivery"],
                assignment=trace_digests["assignment"],
                emits={
                    outcome.resolved.expose_id: outcome.result.output_digest for outcome in made
                },
                decisions=decisions.decisions_digest(),
            )
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
                emits=made,
                decisions=decisions,
                sovereignty_audit=sovereignty_audit,
                input_digest=input_digest,
                files_not_hashed=loaded.digest_inputs().not_hashed,
                components=components,
                kernel=run_kernel,
                parameters=parameters,
                scale=Decimal(1) if scale is None else scale,
                shard=shard,
                references=published,
                generators=loaded_generators,
                deferred=deferred,
                hidden_emits=hidden_emits,
                responses=_runtime_of(early),
                replicate=replicate,
                end_share=era.end_share,
                cut_ranges=era.cut_ranges,
                scale_widened=widened,
                scale_floors=era.scale_floors,
            )

        if probing:
            assert stored_probe is not None
            if stored_probe(finished(emits, True)):
                return finished(emits, True)
            # Not stored: the plugins are asked now, for each plugin expose, as `_generate_emits`
            # asks them, and the run is the one it always was.
            made = list(emits)
            with _scale_noted(scale, unscaled_refuses, declared_rows):
                for position in plugin_indexes:
                    index = written[position]
                    resolved = resolved_emits[index]
                    made[position] = _generate_emit(
                        resolved,
                        per_emit_digests[index],
                        root,
                        scenario,
                        planned_emits[index],
                        compiled_blocks[index],
                        shard=shard,
                        generator=loaded_generators.get(resolved.expose_id),
                    )
            emits = tuple(made)
        lap("digests")
        return finished(emits, False)


def _widened_by(
    resolved_emits: Sequence[ResolvedEmit], scale: Optional[Decimal]
) -> Dict[str, FrozenSet[str]]:
    """The key columns *scale* widens, by exposeId, for the emits that have any."""
    found = {emit.expose_id: widened_columns(emit, scale or Decimal(1)) for emit in resolved_emits}
    return {expose_id: columns for expose_id, columns in found.items() if columns}


def _refuses_unscaled(directory: Any, options: Mapping[str, Any]) -> bool:
    """Whether the run of *directory* at scale 1 refuses for too few child rows as well.

    It runs in one process, so a layout refusal keeps its class (a worker re-raises it plain).
    """
    try:
        execute(directory, jobs=1, **options)
    except TooFewChildRows:
        return True
    except FluxError:
        return False
    return False


def _scale_causes(
    short: TooFewChildRows, declared_rows: Mapping[str, Optional[int]], scale: Decimal
) -> bool:
    """Whether child rows too few for a minimum at a scale below 1 are short because of the scale.

    The refusal names the child and the parent expose, and *declared_rows* holds what each declares
    before any scaling. At scale 1 the child rows are ``N1`` and the parents ``P1``. The scaled run
    had ``e`` eligible parents among the ``Ps`` it drew; with no ``parentWhen`` that is all of them,
    and otherwise the parents beyond the first ``Ps`` add at most ``P1 - Ps`` more, so no more than
    ``e + P1 - Ps`` parents are eligible at scale 1 (``P1`` when every parent is). The scale is the
    cause when ``N1 >= m * (e + P1 - Ps)``: scale 1 clears the minimum even if every one of those
    is eligible. Where it does not, scale 1 fails, or may fail, the same way, and the note would
    send the person to a retry that fails too, so it is left out. Rows an expose does not declare
    (they come from the run's own moments) give no such count, and the note is given as before.
    """
    children = declared_rows.get(short.child)
    parents = declared_rows.get(short.parent)
    if children is None or parents is None:
        return True
    drawn = scaled_rows(parents, scale)
    most_eligible = min(parents, short.eligible + max(0, parents - drawn))
    return children >= short.minimum * most_eligible


def _check_declared_blocks(
    simulation: Mapping[str, Any],
    resolved_emits: Sequence[ResolvedEmit],
    *,
    where: str,
    counter_based: bool,
) -> None:
    """Refuse the blocks a Simulation declares that the run's kernel cannot draw, and bad SQL."""
    check_generators_declared(simulation, where=where, counter_based=counter_based)
    check_responses_declared(simulation, where=where, counter_based=counter_based)
    check_consumers_declared(simulation, where=where, counter_based=counter_based)
    check_consumer_sql(simulation, resolved_emits, where=where)
    check_conditions_declared(simulation, where=where, counter_based=counter_based)


def _response_setup(
    simulation: Mapping[str, Any],
    parameters: Optional[Any],
    replicate: Optional[Replicate],
    *,
    where: str,
    profile: Union[str, Path, None],
    recorded: Optional[Mapping[str, Any]],
) -> Tuple[
    Sequence[ResponseFamily], Optional[ResponseEstimates], Optional[Any], Optional[Replicate]
]:
    """The families, their estimates, the register with them and the resolved replicate."""
    families, _declared_forecasts = declared_responses(simulation, where=where)
    estimates: Optional[ResponseEstimates] = None
    if families:
        estimates = _response_estimates(
            families, simulation, where=where, profile=profile, recorded=recorded
        )
        if parameters is not None:
            parameters = register_estimates(parameters, estimates, code=FLX_3105)
    if replicate is not None:
        replicate = replicate.resolved(families, estimates or ResponseEstimates(), where=where)
    return families, estimates, parameters, replicate


def _early_assignments(
    families: Sequence[ResponseFamily],
    estimates: Optional[ResponseEstimates],
    replicate: Optional[Replicate],
    *,
    cast: Cast,
    model: AttributeModel,
    population: SubjectStore,
    root: SeedTreeRoot,
    trace: TraceLog,
) -> Tuple[Optional[_BeforeJourneys], Optional[Tuple[CompiledExperiment, ...]]]:
    """What a response family needs before the journeys, and the experiments compiled for it.

    A response family (ADR 0029) needs the experiment's assignment before the journeys. It reads
    only the cast and its own stream, so it is the assignment it was. Both are ``None`` when the
    run has no estimates.
    """
    if estimates is None:
        return None, None
    compiled = compile_experiments(cast)
    early = _before_journeys(
        families,
        estimates,
        cast=cast,
        model=model,
        population=population,
        experiments=compiled,
        root=root,
        trace=trace,
        effects=replicate.by_name() if replicate is not None else None,
    )
    return early, compiled


def _runtime_of(early: Optional[_BeforeJourneys]) -> Optional[ResponseRuntime]:
    """The response runtime made before the journeys, or ``None`` when there was none."""
    return early.runtime if early is not None else None


def _probability_of(early: Optional[_BeforeJourneys]) -> Optional[Any]:
    """The journeys' probability hook of the response runtime, or ``None`` when there is none."""
    return early.runtime.probability if early is not None else None


class PlannedRowsAboveCeiling(Exception):
    """The plans of a run hold more rows than the caller's ceiling allows.

    This is not a :class:`~flux_engine.core.errors.FluxError`: it is the caller's own bound and
    not a refusal of the bundle, so no stage turns it into a result.
    """

    def __init__(self, rows: int, ceiling: int) -> None:
        self.rows = rows
        self.ceiling = ceiling
        super().__init__(f"the plans hold {rows} rows, more than the ceiling of {ceiling}")


def _require_rows_within(planned_emits: Sequence[Any], most_rows: Optional[int]) -> None:
    """Raise :class:`PlannedRowsAboveCeiling` when the planned emits hold more than *most_rows* rows."""
    if most_rows is None:
        return
    rows = sum(planned.plan.rows for planned in planned_emits if planned.plan is not None)
    if rows > most_rows:
        raise PlannedRowsAboveCeiling(rows, most_rows)


def _require_coverage(planned_emits: Sequence[Any], asked_by: Sequence[str]) -> None:
    """Refuse a gap in coverage or generation, when the flag or the Simulation asked for it."""
    if not asked_by:
        return
    require_full_coverage((planned.coverage for planned in planned_emits), asked_by=asked_by)
    require_all_generated(
        tuple(row for planned in planned_emits for row in planned.not_generated),
        asked_by=asked_by,
    )


def _campaign_policy(
    early: Optional[_BeforeJourneys], cast: Cast, campaign_id: str, model: AttributeModel
) -> Any:
    """The Campaign's compiled policy: the one made before the journeys, else a fresh one."""
    if early is not None and early.policy is not None:
        return early.policy
    return _compile_policy(cast, campaign_id, model)


def _assign_and_count(
    early: Optional[_BeforeJourneys],
    experiment: Optional[CompiledExperiment],
    eligible: Sequence[int],
    root: SeedTreeRoot,
    trace: TraceLog,
    transitions: Sequence[Any],
) -> Tuple[Mapping[int, str], Tuple[ArmCounts, ...]]:
    """The run's variant assignment and each arm's counts; both empty without an experiment."""
    if experiment is None:
        return {}, ()
    assignment = _assignment_of(early, experiment, eligible, root, trace)
    successes = frozenset(t.ordinal for t in transitions if t.success)
    return assignment, arm_counts(assignment, successes)


def _assignment_of(
    early: Optional[_BeforeJourneys],
    experiment: CompiledExperiment,
    eligible: Sequence[int],
    root: SeedTreeRoot,
    trace: TraceLog,
) -> Mapping[int, str]:
    """The variant of each subject: the one made before the journeys, else assigned now."""
    if early is not None:
        return early.assignments[experiment.experiment_id]
    return assign_variants(experiment=experiment, eligible=eligible, run_root=root, trace=trace)


@contextmanager
def _scale_noted(
    scale: Optional[Decimal],
    unscaled_refuses: Callable[[], bool],
    declared_rows: Mapping[str, Optional[int]],
) -> Iterator[None]:
    """Add what a scaled run says about a layout or key that the scale left too small (``FLX-3403``).

    A counted layout is worked out at plan time where its eligible parents are known then, and
    otherwise when its rows are first drawn, so the plan and the generation both run inside this.
    At scale 1 (or none) the refusal passes unchanged. So does a refusal for too few child rows
    when the unscaled run refuses for the same, which raising the scale cannot help; that is
    asked only of a scale above 1, since the unscaled run is then the smaller of the two. Below 1
    that run would be the larger, so the counts decide instead (:func:`_scale_causes`).
    """
    try:
        yield
    except (KeySpaceTooSmall, TooFewChildRows, TooManyChildRows) as small:
        if scale is None or scale == 1:
            raise
        if isinstance(small, KeySpaceTooSmall):
            note = key_space_note(scale)
        elif isinstance(small, TooManyChildRows):
            note = too_many_child_rows_note(scale)
        elif scale > 1 and unscaled_refuses():
            raise
        elif (
            scale < 1
            and isinstance(small, TooFewChildRows)
            and not _scale_causes(small, declared_rows, scale)
        ):
            raise
        else:
            note = child_rows_note(scale)
        raise FluxError(FLX_3403, f"{small.detail}; {note}") from small


def _check_statements(
    loaded: Mapping[str, LoadedGenerator],
    planned_emits: Sequence["_PlannedEmit"],
    root: SeedTreeRoot,
    *,
    sim_id: str,
) -> None:
    """Read each loaded plugin's statement and refuse one that differs from its declaration.

    The request is the one ``generate`` will be given for the whole expose: the columns the plugin
    generates, the expose's row count, the plugin's seed and the declared options.
    """
    for planned in planned_emits:
        expose_id = planned.expose.expose_id
        found = loaded.get(expose_id)
        if found is None:
            continue
        plan = planned.plan
        assert plan is not None and plan.plugin is not None  # a declared plugin plans its expose
        check_statement(
            found,
            request_of(
                plan.plugin,
                expose_id,
                n=plan.rows,
                seed=derive_plugin_seed(root, sim_id, expose_id),
                options=copy.deepcopy(dict(found.declaration.options)),
            ),
        )


def _hidden_plan(planned: "_PlannedEmit") -> GeneratorPlan:
    """The generator plan of a hidden table; it has one, since it declares a fidelity block."""
    assert planned.plan is not None  # a hidden table without a fidelity block is refused at resolve
    return planned.plan


@dataclass(frozen=True)
class _BeforeJourneys:
    """What a response family needs made before the journeys run (ADR 0029 section 2)."""

    runtime: ResponseRuntime
    #: The campaign's compiled policy, or ``None`` when the cast has no Campaign.
    policy: Optional[Any]
    #: ``{experiment id: {ordinal: variant}}`` of the run's experiment and each a family names.
    assignments: Mapping[str, Mapping[int, str]]


def _compile_policy(cast: Cast, campaign_id: str, model: AttributeModel) -> Any:
    consent_id = cast.document(campaign_id)["spec"]["consentRef"]
    use_case = cast.document(consent_id)["spec"]["allowedUseCases"][0]
    return compile_campaign(cast, campaign_id, model, use_case=use_case)


def _before_journeys(
    families: Sequence[ResponseFamily],
    estimates: ResponseEstimates,
    *,
    cast: Cast,
    model: AttributeModel,
    population: SubjectStore,
    experiments: Sequence[CompiledExperiment],
    root: SeedTreeRoot,
    trace: TraceLog,
    effects: Optional[Mapping[Tuple[str, str], Fraction]] = None,
) -> _BeforeJourneys:
    """Assign the experiments the run and its families use, and build the families' runtime.

    The run's own experiment (the first by id) is assigned first, then each experiment a family
    names that is another one, in the order the families are declared. Each is assigned from the
    subjects the Campaign's segment selects, as it is when no family is declared.
    """
    campaign_ids = cast.ids("Campaign")
    policy = _compile_policy(cast, campaign_ids[0], model) if campaign_ids else None
    eligible = [
        o
        for o in population.ordinals
        if policy is None or policy.segment(population.view(o, model.subject))
    ]
    by_id = {item.experiment_id: item for item in experiments}
    wanted = [experiments[0].experiment_id] if experiments else []
    for family in families:
        cast.document(family.experiment)  # FLX-2301 when the run's cast lacks it
        name = strip_range(family.experiment)
        if name not in wanted:
            wanted.append(name)
    assignments = {
        name: assign_variants(experiment=by_id[name], eligible=eligible, run_root=root, trace=trace)
        for name in wanted
    }
    runtime = ResponseRuntime.build(
        families=families,
        estimates=estimates,
        cast=cast,
        model=model,
        store=population,
        experiments=by_id,
        assignments=assignments,
        effects=effects,
    )
    return _BeforeJourneys(runtime=runtime, policy=policy, assignments=assignments)


def _response_estimates(
    families: Sequence[ResponseFamily],
    simulation: Mapping[str, Any],
    *,
    where: str,
    profile: Union[str, Path, None],
    recorded: Optional[Mapping[str, Any]],
) -> ResponseEstimates:
    """The estimates of *families*: a recorded plan's, else the pinned profile's (``FLX-3671``).

    The profile is the one the Simulation pins; ``--profile`` is checked against the pin as
    ``plan`` checks it (``FLX-3109``, ``FLX-3110``).
    """
    if recorded is not None:
        return ResponseEstimates.from_document(recorded, families, where=where)
    pinned_digest = profile_reference(simulation, where=where)
    if pinned_digest is None:
        raise FluxError(
            FLX_3671,
            f"{where}: the Simulation declares responses and pins no profile; pin the profile "
            "whose cells they read under extensions['com.agentics.flux/1'].profile",
        )
    pinned = require_profile(
        pinned_digest, Path(profile) if profile is not None else None, where=where
    )
    assert pinned is not None  # a pin and a profile are both here
    return estimate_responses(families, pinned.profile)


def check_reference_declared(
    resolved: ResolvedEmit, simulation: Mapping[str, Any], *, where: str, counter_based: bool
) -> None:
    """Refuse (``FLX-3105``) a reference expose a Simulation cannot take its rows from.

    ``reference: true`` needs kernel 0.2.0 and a Simulation that pins a profile. What the pinned
    profile holds is checked where the profile is read (:func:`_published_rows`).
    """
    if not resolved.is_reference:
        return
    here = f"{where}: emit {resolved.expose_id!r} declares reference: true"
    if not counter_based:
        raise FluxError(
            FLX_3105, f"{here}, which kernel 0.2.0 generates; this run is on kernel 0.1.0"
        )
    if profile_reference(simulation, where=where) is None:
        raise FluxError(
            FLX_3105,
            f"{here}, and the Simulation pins no profile; pin the profile that publishes its "
            "rows under extensions['com.agentics.flux/1'].profile",
        )


def check_copies_declared(
    simulation: Mapping[str, Any], *, where: str, counter_based: bool
) -> None:
    """Refuse (``FLX-3105``) ``copies: true`` on a run that is not kernel 0.2.0's.

    Kernel 0.1.0 reads a ``sharedColumns`` or a ``sharedAttribute`` as declared and never copies
    it (ADR 0022 section 4), so a Simulation that asks for copies there asks for what the run
    cannot do.
    """
    if not counter_based and declared_copies(simulation, where=where):
        raise FluxError(
            FLX_3105,
            f"{where}: extensions['com.agentics.flux/1'].copies is declared, which kernel 0.2.0 "
            "generates; this run is on kernel 0.1.0",
        )


def check_generators_declared(
    simulation: Mapping[str, Any], *, where: str, counter_based: bool
) -> None:
    """Refuse (``FLX-3105``) a ``generators`` declaration on a run that is not kernel 0.2.0's.

    Kernel 0.1.0 has no plugin seed (ADR 0032 section 5), so a Simulation that declares a plugin
    there asks for what the run cannot do.
    """
    if not counter_based and declared_generators(simulation, where=where):
        raise FluxError(
            FLX_3105,
            f"{where}: extensions['com.agentics.flux/1'].generators is declared, which kernel "
            "0.2.0 reads; this run is on kernel 0.1.0",
        )


def check_hidden_declared(
    resolved_emits: Sequence[ResolvedEmit], *, where: str, counter_based: bool
) -> None:
    """Refuse (``FLX-3105``) what a hidden column, a hidden table, a lifetime, a ``within`` or ``rates`` asks for.

    Kernel 0.1.0 has no column that is drawn and not written, no table that is generated and
    not written, and no date that follows its parent row's lifetime (ADR 0024 section 1) and no calendar for a date
    (ADR 0025 section 2), so an
    emit that declares any of them asks for what the run cannot do. On any kernel a hidden table that no written emit reads through a link is refused
    (section 3): nothing would use its rows.
    """
    if not counter_based:
        for resolved in resolved_emits:
            for key, declared in (
                ("rates", (resolved.extension or {}).get(RATES_KEY)),
                ("hiddenColumns", resolved.hidden_columns),
                ("hidden", resolved.is_hidden_table),
                ("lifetimes", (resolved.extension or {}).get(LIFETIMES_KEY)),
                ("within", (resolved.extension or {}).get(WITHIN_KEY)),
            ):
                if declared:
                    raise FluxError(
                        FLX_3105,
                        f"{where}: emit {resolved.expose_id!r} declares {key}, which kernel 0.2.0 "
                        "generates; this run is on kernel 0.1.0",
                    )
    _check_hidden_tables_read(resolved_emits, where)


def _relationship_targets(entry: ResolvedEmit) -> Set[Tuple[str, str]]:
    """``(contract path, exposeId)`` of every table the expose of *entry* declares a relationship to."""
    semantics = entry.expose.get("semantics")
    raw = semantics.get("relationships") if isinstance(semantics, Mapping) else None
    items = raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else ()
    return {
        (str(entry.contract_path), nfc(str(item["to"])))
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("to"), str)
    }


def _check_hidden_tables_read(resolved_emits: Sequence[ResolvedEmit], where: str) -> None:
    """Refuse (``FLX-3105``) a hidden table that no written emit reaches through a relationship.

    A table is reached when a written emit's expose declares a relationship to it, or when a hidden
    table that is reached does. The copies, rules and ``links`` entries that read a table read it
    through such a relationship.
    """
    hidden = {
        (str(entry.contract_path), entry.expose_id)
        for entry in resolved_emits
        if entry.is_hidden_table
    }
    if not hidden:
        return
    targets: Dict[Tuple[str, str], Set[Tuple[str, str]]] = {}
    for entry in resolved_emits:
        targets[(str(entry.contract_path), entry.expose_id)] = _relationship_targets(entry)
    reached: Set[Tuple[str, str]] = set()
    frontier = [key for key in targets if key not in hidden]
    while frontier:
        for target in targets.get(frontier.pop(), ()):
            if target in hidden and target not in reached:
                reached.add(target)
                frontier.append(target)
    unused = sorted(name for _, name in hidden - reached)
    if unused:
        raise FluxError(
            FLX_3105,
            f"{where}: emit {unused[0]!r} is hidden and no written emit reads it through a "
            "relationship, so nothing would use its rows",
        )


def check_overlay_declared(
    simulation: Mapping[str, Any], *, where: str, counter_based: bool
) -> None:
    """Refuse (``FLX-3105``) an ``overlay`` declaration on a run that is not kernel 0.2.0's.

    The overlay is a key of the namespace that kernel 0.1.0 does not read (ADR 0030 section 7), so
    a Simulation that declares one there asks for what the run cannot do.
    """
    if not counter_based and declared_overlay(simulation, where=where) is not None:
        raise FluxError(
            FLX_3105,
            f"{where}: extensions['com.agentics.flux/1'].overlay is declared, which kernel "
            "0.2.0 reads; this run is on kernel 0.1.0",
        )


def check_consumers_declared(
    simulation: Mapping[str, Any], *, where: str, counter_based: bool
) -> None:
    """Refuse (``FLX-3105``) a ``consumers`` block on a run that is not kernel 0.2.0's.

    Consumer queries are read, calibrated and scored on kernel 0.2.0's runs (ADR 0028), so a
    Simulation that declares them on kernel 0.1.0 asks for what the run cannot do.
    """
    if not counter_based and declared_consumers(simulation, where=where) is not None:
        raise FluxError(
            FLX_3105,
            f"{where}: extensions['com.agentics.flux/1'].consumers is declared, which kernel "
            "0.2.0 reads; this run is on kernel 0.1.0",
        )


def check_responses_declared(
    simulation: Mapping[str, Any], *, where: str, counter_based: bool
) -> None:
    """Refuse (``FLX-3105``) ``responses`` or ``forecasts`` on a run that is not kernel 0.2.0's.

    Response families and forecasts are applied and scored on kernel 0.2.0's runs (ADR 0029), so a
    Simulation that declares them on kernel 0.1.0 asks for what the run cannot do.
    """
    if counter_based:
        return
    families, forecasts = declared_responses(simulation, where=where)
    for key, declared in (("responses", bool(families)), ("forecasts", forecasts is not None)):
        if declared:
            raise FluxError(
                FLX_3105,
                f"{where}: extensions['com.agentics.flux/1'].{key} is declared, which kernel "
                "0.2.0 reads; this run is on kernel 0.1.0",
            )


def check_conditions_declared(
    simulation: Mapping[str, Any], *, where: str, counter_based: bool
) -> None:
    """Refuse (``FLX-3105``) ``conditions`` on a run that is not kernel 0.2.0's.

    Conditioned rows are new generation (ADR 0028 section 10), and kernel 0.1.0's draws are
    frozen, so a Simulation that declares groups there asks for what the run cannot do.
    """
    if not counter_based and declared_conditions(simulation, where=where):
        raise FluxError(
            FLX_3105,
            f"{where}: extensions['com.agentics.flux/1'].conditions is declared, which kernel "
            "0.2.0 generates; this run is on kernel 0.1.0",
        )


def _why_not_fixed(
    column_plan: ColumnPlan,
    name: str,
    related: Collection[str],
    determined: Collection[str],
    linked: Collection[str],
) -> Optional[str]:
    """Why something other than a group decides the column's value, or ``None`` when none does."""
    statistics = column_plan.statistics
    if name in related:
        return "a key or a column of a relationship"
    if column_plan.column_type is ColumnType.IDENTIFIER:
        return "an identifier"
    if name in determined:
        return "set by a rule"
    if name in linked:
        return "written by a conditional link"
    if statistics.given_link is not None:
        return "drawn given a column of a linked row"
    # A column that is not categorical is refused as that, whatever else it declares: `init`
    # writes a nullFraction on every column, and only a categorical column can be fixed at all.
    if statistics.null_fraction is not None and column_plan.family is Family.CATEGORICAL:
        return "a column that declares a nullFraction"
    return None


def _connected_to(fixed: Set[str], plan: GeneratorPlan) -> Set[str]:
    """The columns joined to *fixed* by a ``given`` edge, either way, the fixed ones included."""
    edges: Dict[str, Set[str]] = {}
    for column_plan in plan.columns:
        child = nfc(column_plan.column.name)
        for parent in column_plan.statistics.parents:
            edges.setdefault(child, set()).add(nfc(parent))
            edges.setdefault(nfc(parent), set()).add(child)
    found, frontier = set(fixed), list(fixed)
    while frontier:
        for neighbour in edges.get(frontier.pop(), ()):
            if neighbour not in found:
                found.add(neighbour)
                frontier.append(neighbour)
    return found


def _refuse_unapplied_conditions(
    resolved: ResolvedEmit,
    plan: Optional[GeneratorPlan],
    groups: Sequence[Any],
    *,
    where: str,
) -> None:
    """Refuse (``FLX-3643``) ``conditions`` on an emit whose rows the groups would not decide.

    A history emit lays its tracked columns out from the chain, and a projected emit (a snapshot,
    events or periods) derives its rows from a history, so neither draws the rows a group counts.
    On any other emit, a column that declares ``allocation: exact`` takes its class from its row's
    slot, so a group does not fix it, and a column drawn given a fixed column, or one a fixed
    column is drawn given, would need the conditioned draw its slot does not make.
    """
    extension = resolved.extension or {}
    emit = resolved.expose_id
    if HISTORY_KEY in extension:
        raise FluxError(
            FLX_3643,
            f"{where}: conditions are declared for {emit!r}, which declares a history; the "
            "history lays its tracked columns out, so a group would not hold. Remove the "
            "conditions from this emit",
        )
    if projection_keys_of(extension) or PROJECTS_KEY in extension:
        raise FluxError(
            FLX_3643,
            f"{where}: conditions are declared for {emit!r}, which is projected from a history; "
            "its rows are derived from the history, so a group would not hold. Remove the "
            "conditions from this emit",
        )
    if plan is None:
        return
    fixed = {nfc(column) for group in groups for column, _ in group.values}
    near = _connected_to(fixed, plan)
    for column_plan in plan.columns:
        name = nfc(column_plan.column.name)
        if column_plan.statistics.allocation is not None and name in near:
            raise FluxError(
                FLX_3643,
                f"{where}: conditions are declared for {emit!r}, and the column {name!r} declares "
                "`allocation: exact`, and it is "
                "fixed by a group or drawn given a fixed column (or a fixed column is drawn "
                f"given it), so the group would not hold. Remove `allocation: exact` from {name!r}, "
                "or drop the group",
            )


def conditioned_columns_of(
    resolved: ResolvedEmit,
    plan: GeneratorPlan,
    rules: Optional[RulePlan],
    simulation: Mapping[str, Any],
    *,
    where: str,
) -> Dict[str, ConditionColumn]:
    """What the checks of a group need to know of each column of *plan*'s expose.

    A column cannot be fixed when something other than the group decides its value: a key or a
    column of a relationship, an identifier, a column a rule sets (a copy included), a column a
    conditional link writes, a column drawn given a column of a linked row, and a column that
    declares a ``nullFraction``. Its levels are the ones it draws, for a categorical column. A
    column whose sensitivity sets a minimum grade of P3 or stronger (ADR 0020, the floors the
    governance gate reads) is marked ``sensitive``: a fixed value is one no grade can see.
    """
    floors = effective_floors(privacy_floors(simulation, where=where))
    labels = sensitivities_of([(resolved.expose_id, resolved.expose)])
    related = related_columns(resolved.keys)
    determined = rules.determined if rules is not None else frozenset()
    linked = frozenset(
        name for link in (rules.links if rules is not None else ()) for name in link.columns
    )
    found: Dict[str, ConditionColumn] = {}
    for column_plan in plan.columns:
        name = nfc(column_plan.column.name)
        statistics = column_plan.statistics
        blocked = _why_not_fixed(column_plan, name, related, determined, linked)
        label = labels.get((resolved.expose_id, column_plan.column.name))
        floor = floors.get(label) if label is not None else None
        found[name] = ConditionColumn(
            sensitive=label if floor is not None and at_least(floor, Pattern.P3) else None,
            levels=(
                tuple(sorted(_level_names(statistics)))
                if column_plan.family is Family.CATEGORICAL
                else None
            ),
            blocked=blocked,
        )
    return found


def refuse_conditions(
    resolved: ResolvedEmit,
    plan: Optional[GeneratorPlan],
    rules: Optional[RulePlan],
    simulation: Mapping[str, Any],
    *,
    where: str,
) -> None:
    """Refuse a ``conditions`` declaration of *resolved*'s emit that its columns cannot hold.

    ``FLX-3644`` for an emit that is not drawn from statistics (*plan* is ``None``), and
    otherwise what :func:`~flux_engine.seam.conditions.check_conditioned_columns` refuses.
    """
    groups = conditions_of(resolved.extension)
    if not groups:
        return
    _refuse_unapplied_conditions(resolved, plan, groups, where=where)
    if plan is None or resolved.is_reference:
        raise FluxError(
            FLX_3644,
            f"{where}: conditions are declared for {resolved.expose_id!r}, which is not drawn "
            "from declared statistics (no fidelity block, or a reference expose), so it has no "
            "column to fix",
        )
    check_conditioned_columns(
        groups,
        emit=resolved.expose_id,
        columns=conditioned_columns_of(resolved, plan, rules, simulation, where=where),
        where=where,
    )


def check_emit_conditions(
    resolved: ResolvedEmit,
    rules: Optional[RulePlan],
    *,
    sim_id: str,
    simulation: Mapping[str, Any],
) -> None:
    """:func:`refuse_conditions` for ``validate``, which has no plan: one is built to read the columns.

    Built with the placeholder digest and row count of :func:`refuse_determined_columns`. A plan
    that cannot be built at all (``FLX-3402`` and kin) is ``plan``'s refusal, which validate
    leaves to it.
    """
    if not conditions_of(resolved.extension):
        return
    plan: Optional[GeneratorPlan] = None
    if resolved.fidelity is not None and not resolved.is_reference:
        try:
            plan = build_plan(
                resolved.compile_planned(),
                sim_id=sim_id,
                seam_digest="sha256:" + "0" * 64,
                rows=resolved.declared_row_count or 1,
                fidelity=resolved.fidelity,
                time_bounds=simulation.get("spec", {}).get("timeBounds"),
                related=related_columns(resolved.keys),
                rules=rules,
                # Conditions are kernel 0.2.0's, as is `allocation`, which the plan reads then.
                generator_version=GENERATOR_VERSION_2,
            )
        except (Expr2Error, FluxError):
            return
    refuse_conditions(resolved, plan, rules, simulation, where=sim_id)


def refuse_copula_combinations(resolved: ResolvedEmit, *, where: str) -> None:
    """Refuse (``FLX-3402``) a ``fidelity.copula`` on an emit whose rows something else lays out.

    A history lays its tracked columns out from the entity's chain, a projected emit derives its rows
    from a history, and a hidden table is generated and not written, so the joint draw a copula
    makes for each row is not the row such an emit holds (ADR 0035 decision 4).
    """
    if not declares_copula(resolved.fidelity):
        return
    extension = resolved.extension or {}
    if HISTORY_KEY in extension or projection_keys_of(extension) or PROJECTS_KEY in extension:
        raise FluxError(
            FLX_3402,
            f"{where}: fidelity.copula is declared for {resolved.expose_id!r}, which declares a "
            "history or is projected from one; a history lays its rows out, so the copula would "
            "not hold. Remove the copula from this emit",
        )
    if resolved.is_hidden_table:
        raise FluxError(
            FLX_3402,
            f"{where}: fidelity.copula is declared for {resolved.expose_id!r}, a hidden table, "
            "which is generated and never written. Remove the copula from this emit",
        )


def check_emit_copula(
    resolved: ResolvedEmit,
    rules: Optional[RulePlan],
    *,
    sim_id: str,
    simulation: Mapping[str, Any],
    counter_based: bool,
) -> None:
    """What `plan` refuses of ``fidelity.copula``, for ``validate``, which has no plan.

    A plan is built with the placeholder digest and row count of :func:`check_emit_conditions`, at
    the kernel a new plan takes, so a copula that kernel cannot draw (``FLX-3105``) or that the
    emit's columns cannot hold (``FLX-3402``) is refused here as `plan` refuses it.
    """
    if resolved.is_reference or not declares_copula(resolved.fidelity):
        return
    build_plan(
        resolved.compile_planned(),
        sim_id=sim_id,
        seam_digest="sha256:" + "0" * 64,
        rows=resolved.declared_row_count or 1,
        fidelity=resolved.fidelity,
        time_bounds=simulation.get("spec", {}).get("timeBounds"),
        related=related_columns(resolved.keys),
        rules=rules,
        generator_version=GENERATOR_VERSION_2 if counter_based else GENERATOR_VERSION,
    )
    refuse_copula_combinations(resolved, where=sim_id)


def _published_rows(
    resolved_emits: Sequence[ResolvedEmit],
    simulation: Mapping[str, Any],
    *,
    where: str,
    counter_based: bool,
    profile: Union[str, Path, None],
    recorded: Optional[Mapping[str, Any]],
) -> Dict[str, ReferenceRows]:
    """The published rows of every reference expose of the run, by exposeId (``FLX-3105``).

    From *recorded*, the rows a plan holds, when they are given (``apply --run-id``), and from the
    pinned profile otherwise. The profile is the one the Simulation pins: ``--profile`` is
    checked against the pin as ``plan`` checks it (``FLX-3109``, ``FLX-3110``). A run with no
    reference expose reads neither and returns nothing.
    """
    wanted = [resolved for resolved in resolved_emits if resolved.is_reference]
    if not wanted:
        return {}
    for resolved in wanted:
        check_reference_declared(resolved, simulation, where=where, counter_based=counter_based)
    found: Dict[str, ReferenceRows] = {}
    if recorded is not None:
        for resolved in wanted:
            entry = recorded.get(resolved.expose_id)
            if entry is None:
                raise FluxError(
                    FLX_3105,
                    f"{where}: the plan records no published rows for the reference expose "
                    f"{resolved.expose_id!r}; plan again",
                )
            found[resolved.expose_id] = ReferenceRows.from_document(
                entry, where=f"{where}: emit {resolved.expose_id!r}"
            )
        return found
    pinned = require_profile(
        profile_reference(simulation, where=where),
        Path(profile) if profile is not None else None,
        where=where,
    )
    assert pinned is not None  # check_reference_declared refused a Simulation that pins none
    for resolved in wanted:
        table = pinned.profile.references.get(resolved.expose_id)
        if table is None:
            raise FluxError(
                FLX_3105,
                f"{where}: emit {resolved.expose_id!r} declares reference: true, and the pinned "
                "profile publishes no rows for it under references",
            )
        found[resolved.expose_id] = ReferenceRows.of(table)
    return found


@dataclass(frozen=True)
class _PlannedEmit:
    """What one emit will be produced from, decided before any row exists."""

    expose: CompiledExpose
    plan: Optional[GeneratorPlan]
    mapping: Optional[ColumnMapping]
    coverage: ExposeCoverage
    not_generated: Tuple[NotGenerated, ...] = ()


def _plan_emit(
    resolved: ResolvedEmit,
    digests: SeamDigests,
    cast: Cast,
    simulation: Mapping[str, Any],
    scenario: "_Scenario",
    generator_version: str,
    strict: bool = False,
    published: Optional[Mapping[str, ReferenceRows]] = None,
    plugins: Optional[Mapping[str, PluginExpose]] = None,
    emits: Sequence[ResolvedEmit] = (),
    rows: Optional[int] = None,
    cut_ranges: bool = True,
) -> _PlannedEmit:
    """Decide how one emit's columns will be produced, refusing what cannot be.

    *rows* is the count of a projected emit, which declares none (ADR 0027 section 2).

    Every refusal a contract can earn at plan time (``FLX-3302``,
    ``FLX-3303``, ``FLX-3401``, ``FLX-3402``) is raised here, and the
    coverage report is read off the same objects that raised them. It runs
    for every emit before the first row of any of them is drawn, so a strict
    run (``FLX-3304``) is refused with every emit's gaps in one message and
    without having generated a row it then throws away.
    """
    expose = resolved.compile()
    rules = plan_emit_rules(
        resolved,
        strict=strict,
        where=cast.simulation_id,
        counter_based=generator_version == GENERATOR_VERSION_2,
        counted=counted_relationships(simulation, where=cast.simulation_id),
        emits=emits,
        windowed=simulation.get("spec", {}).get("timeBounds") is not None,
        time_bounds=simulation.get("spec", {}).get("timeBounds"),
    )
    # What a plugin's expose cannot combine with is refused before anything is planned from it.
    refuse_plugin_combinations(resolved, rules, plugins or {}, where=cast.simulation_id)
    plugin = (plugins or {}).get(resolved.expose_id)
    not_generated = emit_not_generated(
        resolved, where=cast.simulation_id, counter_based=generator_version == GENERATOR_VERSION_2
    )
    if resolved.is_reference:
        assert published is not None  # execute reads the rows of every reference emit first
        reference = build_reference_plan(
            expose,
            sim_id=cast.simulation_id,
            seam_digest=digests.h.value,
            table=published[resolved.expose_id],
            generator_version=generator_version,
            where=f"{cast.simulation_id}: reference expose {resolved.expose_id!r}",
        )
        return _PlannedEmit(
            expose=expose,
            plan=reference,
            mapping=None,
            coverage=coverage_of_plan(reference),
            not_generated=not_generated,
        )
    if resolved.fidelity is not None or plugin is not None:
        groups = conditions_of(resolved.extension)
        count = rows if rows is not None else resolved.declared_row_count
        if count is None:
            counts = [group.count for group in groups]
            count = (
                sum(c for c in counts if c is not None)
                if groups and all(c is not None for c in counts)
                else len(scenario.store)
            )
        plan = build_plan(
            resolved.compile_planned(),
            sim_id=cast.simulation_id,
            seam_digest=digests.h.value,
            rows=count,
            fidelity=resolved.fidelity,
            time_bounds=simulation.get("spec", {}).get("timeBounds"),
            related=_related_of(resolved),
            rules=rules,
            generator_version=generator_version,
            plugin=plugin,
            projected=names_a_snapshot(resolved.extension),
            uncut=uncut_columns(resolved, emits) if cut_ranges else None,
        )
        refuse_conditions(resolved, plan, rules, simulation, where=cast.simulation_id)
        refuse_copula_combinations(resolved, where=cast.simulation_id)
        if groups:
            plan = with_conditions(plan, groups, where=cast.simulation_id)
        return _PlannedEmit(
            expose=expose,
            plan=plan,
            mapping=None,
            coverage=coverage_of_plan(plan),
            not_generated=not_generated,
        )
    if resolved.hidden_columns:
        raise FluxError(
            FLX_3105,
            f"{cast.simulation_id}: emit {resolved.expose_id!r} declares hiddenColumns and no "
            "fidelity block, so nothing draws them; declare the block that holds their statistics",
        )
    refuse_conditions(resolved, None, rules, simulation, where=cast.simulation_id)
    mapping = resolve_column_mapping(
        expose,
        declared_sources=scenario.declared_sources,
        integral_sources=scenario.integral_sources,
        aliases=scenario.aliases,
        overrides=resolved.column_mapping,
    )
    return _PlannedEmit(
        expose=expose,
        plan=None,
        mapping=mapping,
        coverage=mapping.coverage(expose),
        not_generated=not_generated,
    )


def _sovereignty_audit_of(
    cast: Cast, simulation: Mapping[str, Any], world_doc: Mapping[str, Any], zone: Optional[str]
) -> Tuple[SovereigntyAuditRow, ...]:
    """The audit rows the Simulation's and the World's sovereignty policies enforce in *zone*."""
    rows = []
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
            rows.append(row)
    return tuple(rows)


def _planned_with_projections(
    resolved_emits: Sequence[ResolvedEmit],
    per_emit_digests: Sequence[SeamDigests],
    cast: Cast,
    simulation: Mapping[str, Any],
    scenario: "_Scenario",
    generator_version: str,
    *,
    strict: bool,
    published: Optional[Mapping[str, ReferenceRows]],
    root: SeedTreeRoot,
    plugins: Optional[Mapping[str, PluginExpose]] = None,
    era: PlanEra = PlanEra(),
) -> Tuple[_PlannedEmit, ...]:
    """Every emit planned and related, a projected emit's rows derived from its history (ADR 0027 section 2).

    A projected emit declares no ``rows``: it is first planned at the most rows its history can
    give it, the history is related, and the count the history's layouts give is then the emit's
    ``rows``. An emit whose count is the first guess is kept; any other is planned again and the
    contract's emits related once more, so every layout over it holds its real rows.
    """
    namespace = _flux_namespace(simulation)
    projected = _projected_emits(resolved_emits)

    def plan_at(index: int, rows: Optional[int]) -> _PlannedEmit:
        return _plan_emit(
            resolved_emits[index],
            per_emit_digests[index],
            cast,
            simulation,
            scenario,
            generator_version,
            strict=strict,
            published=published,
            plugins=plugins,
            rows=rows,
            emits=resolved_emits,
            cut_ranges=era.cut_ranges,
        )

    guesses = {index: _most_rows(resolved_emits, found) for index, found in projected.items()}
    planned = tuple(plan_at(index, guesses.get(index)) for index in range(len(resolved_emits)))
    related = _relate_emits(resolved_emits, planned, namespace, era)

    def replan(rows: Mapping[int, int]) -> Tuple[_PlannedEmit, ...]:
        """Every emit planned again, the emits *rows* names at those rows, and related once more."""
        return _relate_emits(
            resolved_emits,
            tuple(plan_at(index, rows.get(index)) for index in range(len(resolved_emits))),
            namespace,
            era,
        )

    related, shrunk = _settled_histories(resolved_emits, related, guesses, root, replan)
    if not projected:
        return related
    counts = {
        index: derived_rows(
            *found,
            _history_plan_of(resolved_emits, related, found[1]),
            _plan_of(related[index]),
            root,
        )
        for index, found in projected.items()
    }
    if any(counts[index] != guesses[index] for index in counts):
        related = replan({**shrunk, **counts})
    return _with_projected_plans(resolved_emits, related, projected, root)


def _with_projected_plans(
    resolved_emits: Sequence[ResolvedEmit],
    related: Sequence[_PlannedEmit],
    projected: Mapping[int, Tuple[str, str, str]],
    root: SeedTreeRoot,
) -> Tuple[_PlannedEmit, ...]:
    """Each emit in *projected* with the plan derived from its history's layouts, and the others as they are."""
    return tuple(
        (
            replace(
                planned_emit,
                plan=(
                    projected_plan := project_plan(
                        _plan_of(planned_emit),
                        *projected[index],
                        _history_plan_of(resolved_emits, related, projected[index][1]),
                        root,
                    )
                ),
                coverage=coverage_of_plan(projected_plan),
            )
            if index in projected
            else planned_emit
        )
        for index, planned_emit in enumerate(related)
    )


def _settled_histories(
    resolved_emits: Sequence[ResolvedEmit],
    related: Tuple[_PlannedEmit, ...],
    guesses: Mapping[int, int],
    root: SeedTreeRoot,
    replan: Callable[[Mapping[int, int]], Tuple[_PlannedEmit, ...]],
) -> Tuple[Tuple[_PlannedEmit, ...], Dict[int, int]]:
    """The emits related after every history that holds fewer rows than it declares is planned at them.

    Each pass plans again the histories :func:`_shrunk_histories` finds, with *replan*, which can
    move the rows of another history, so the passes go on until none is found; the rows of every
    emit that shrank come back with the plans. More passes than there are emits is refused.
    """
    shrunk: Dict[int, int] = {}
    for _ in range(len(resolved_emits) + 1):
        found = _shrunk_histories(resolved_emits, related, root)
        if not found:
            return related, shrunk
        shrunk.update(found)
        related = replan({**guesses, **shrunk})
    raise FluxError(
        FLX_3403,
        "the rows of the histories that keep to their parent's lifetime are still changing "
        f"after {len(resolved_emits) + 1} passes ({sorted(shrunk.items())}); "
        "a history whose parent is a history that holds fewer rows than it declares cannot be laid out",
    )


def _shrunk_histories(
    resolved_emits: Sequence[ResolvedEmit], related: Sequence[_PlannedEmit], root: SeedTreeRoot
) -> Dict[int, int]:
    """The rows of each history that keeps to its parent's lifetime and holds fewer than it declares.

    The parents' lifetimes can leave no entity room for a version the layout was asked for, and the
    layout then holds fewer rows (ADR 0017, amendment of 2026-11-24). Its emit is planned again at
    the rows it holds, so that plan, apply and verify count the same rows.

    A history whose parent is itself a history that keeps to its parent's lifetime reads the
    parent's rows, which are the rows the parent's layout holds only once the parent is planned at
    them. Such a history is laid out in the pass after the one that settles its parent: a history is
    left out of a pass when a parent of it, or of a parent, is found to hold fewer rows in it.
    """
    bound = {
        index
        for index, planned in enumerate(related)
        if planned.plan is not None
        and (pick := history_pick_of(planned.plan)) is not None
        and pick.parent_lifetime is not None
    }
    found: Dict[int, int] = {}
    left_out: Set[int] = set()  # the histories that are not laid out in this pass
    visited: Set[int] = set()

    def visit(index: int) -> None:
        if index in visited:
            return
        visited.add(index)
        above = _parent_emit_of(resolved_emits, index)
        if above is not None and above in bound:
            visit(above)
            if above in found or above in left_out:
                left_out.add(index)
                return
        plan = _plan_of(related[index])
        held = chain_of(plan, root).rows
        if held != plan.rows:
            found[index] = held

    for index in sorted(bound):
        visit(index)
    return found


def _parent_emit_of(resolved_emits: Sequence[ResolvedEmit], index: int) -> Optional[int]:
    """The position of the emit the ``parent`` relationship of the history at *index* reaches."""
    emit = resolved_emits[index]
    entries = history_entries_of(emit.extension, emit.expose_id)
    if not entries or entries[0].parent is None or emit.keys is None:
        return None
    reached = next(
        (
            relationship.target
            for relationship in emit.keys.relationships
            if relationship.name == entries[0].parent.relationship
        ),
        None,
    )
    for position, other in enumerate(resolved_emits):
        if reached is not None and nfc(other.expose_id) == nfc(reached):
            return position
    return None


def _plan_of(planned: _PlannedEmit) -> GeneratorPlan:
    """The generator plan of a planned emit that has one (a projected emit generates from statistics)."""
    if planned.plan is None:
        raise FluxError(
            FLX_3403,
            f"emit {planned.expose.expose_id!r} is projected from a history and declares no "
            "fidelity block to generate its columns from",
        )
    return planned.plan


def _projected_emits(resolved_emits: Sequence[ResolvedEmit]) -> Dict[int, Tuple[str, str, str]]:
    """The emits a generated projection names, by their position: the key, ``of`` and ``ended``."""
    found: Dict[int, Tuple[str, str, str]] = {}
    for index, resolved in enumerate(resolved_emits):
        held = projection_of(resolved.extension, resolved.expose_id)
        if held is not None:
            found[index] = held
    return found


def _history_emit_of(resolved_emits: Sequence[ResolvedEmit], of: str) -> int:
    """The position of the emit *of* names, which a history entry declares."""
    for index, resolved in enumerate(resolved_emits):
        if nfc(resolved.expose_id) == nfc(of):
            return index
    raise FluxError(FLX_3403, f"a projection names {of!r}, which is not emitted")


def _history_plan_of(
    resolved_emits: Sequence[ResolvedEmit], related: Sequence[_PlannedEmit], of: str
) -> GeneratorPlan:
    """The related plan of the history's emit that a projection of *of* reads."""
    return _plan_of(related[_history_emit_of(resolved_emits, of)])


def _most_rows(resolved_emits: Sequence[ResolvedEmit], held: Tuple[str, str, str]) -> int:
    """The most rows a projection can take from its history: its entities, as a snapshot's."""
    entries = history_entries_of(
        resolved_emits[_history_emit_of(resolved_emits, held[1])].extension, held[1]
    )
    return max(1, entries[0].entities) if entries else 1


def _flux_namespace(simulation: Mapping[str, Any]) -> Mapping[str, Any]:
    """The Simulation's FLUX namespace, ``extensions['com.agentics.flux/1']``; empty when it has none."""
    extensions = simulation.get("extensions")
    namespace = extensions.get(EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else None
    return namespace if isinstance(namespace, Mapping) else {}


def _relate_emits(
    resolved_emits: Sequence[ResolvedEmit],
    planned_emits: Tuple[_PlannedEmit, ...],
    namespace: Mapping[str, Any],
    era: PlanEra = PlanEra(),
) -> Tuple[_PlannedEmit, ...]:
    """Relate the generator-driven emits of each contract that declares keys.

    KERNEL-SPEC section 8.1: the exposes one contract relates are generated
    together, so every key is unique and every foreign key names a row its
    parent holds (:func:`flux_engine.generator.relations.relate_plans`). The
    emits of a contract are grouped by the contract file they resolve to. A
    plan the contract relates nothing in comes back unchanged, and so does
    its coverage. Runs after every emit is planned and before any row is
    drawn, so a relationship that cannot hold is refused (``FLX-3403``) with
    nothing generated. *era* is :func:`execute`'s.
    """
    groups: Dict[str, List[int]] = {}
    for index, (resolved, planned) in enumerate(zip(resolved_emits, planned_emits, strict=True)):
        if planned.plan is not None and resolved.keys is not None:
            groups.setdefault(str(resolved.contract_path), []).append(index)
    related = list(planned_emits)
    for indexes in groups.values():
        _relate_group(resolved_emits, planned_emits, indexes, related, namespace, era)
    for planned in related:
        if planned.plan is not None:
            check_links_resolved(planned.plan)
    return tuple(related)


def _relate_group(
    resolved_emits: Sequence[ResolvedEmit],
    planned_emits: Tuple[_PlannedEmit, ...],
    indexes: Sequence[int],
    related: List[_PlannedEmit],
    namespace: Mapping[str, Any],
    era: PlanEra,
) -> None:
    """Relate the plans of the emits at *indexes*, one contract's, and set the changed ones in *related*.

    A contract that declares no keys is passed over.
    """
    contract_keys = compile_keys(resolved_emits[indexes[0]].contract_document)
    if contract_keys is None or not contract_keys.declares_keys:
        return
    with_relations = relate_plans(
        _plans_by_expose([planned_emits[index] for index in indexes]),
        contract_keys,
        _bridges_of([resolved_emits[index] for index in indexes]),
        _weights_of([resolved_emits[index] for index in indexes]),
        _counts_of([resolved_emits[index] for index in indexes]),
        resolved_emits[indexes[0]].contract_document,
        namespace,
        _histories_of([resolved_emits[index] for index in indexes], era),
    )
    for index in indexes:
        planned = planned_emits[index]
        assert planned.plan is not None  # grouped above
        plan = with_relations[nfc(planned.plan.expose.expose_id)]
        if plan is not planned.plan:
            related[index] = replace(planned, plan=plan, coverage=coverage_of_plan(plan))


def _related_of(resolved: ResolvedEmit) -> FrozenSet[str]:
    """The columns the relational plan decides: the contract's keys, and a history's natural entity."""
    related = related_columns(resolved.keys)
    if resolved.keys is None or not history_entries_of(resolved.extension, resolved.expose_id):
        return related
    natural = {e.column for e in resolved.keys.entities if e.kind == "natural"}
    return related | natural


def _histories_of(
    resolved: Sequence[ResolvedEmit], era: PlanEra = PlanEra()
) -> Dict[str, HistoryBinding]:
    """The history each emit declares, by exposeId, with the scaled entities and the validity columns (ADR 0017).

    The ``end`` of an entry is a share of the *era*'s ``end_share`` (:func:`execute`'s).
    """
    found: Dict[str, HistoryBinding] = {}
    for emit in resolved:
        entries = history_entries_of(emit.extension, emit.expose_id, end_share=era.end_share)
        if not entries:
            continue
        semantics = (
            declared_exposes(emit.contract_document).get(emit.expose_id, {}).get("semantics")
        )
        ends = window_ends(semantics)
        if "start" not in ends:
            continue  # relate_plans refuses the emit with the contract's own words
        found[nfc(emit.expose_id)] = HistoryBinding(
            entries[0],
            entries[0].entities,
            ends["start"],
            ends.get("end"),
            parent_lifetime := _parent_lifetime_of(emit, entries[0], resolved),
            None if parent_lifetime is None else emit.declared_row_count,
            tracked_levels=era.tracked_levels,
        )
    return found


def _parent_lifetime_of(
    emit: ResolvedEmit, entry: Any, resolved: Sequence[ResolvedEmit]
) -> Optional[Tuple[str, Optional[str]]]:
    """The start and end columns of the ``lifetimes`` entry on the emit a history's ``parent`` reaches.

    Read only for an entry that declares ``parent.lifetime`` (ADR 0017, amendment of 2026-11-24);
    ``None`` otherwise, and when the parent emit declares no lifetime, which the layout refuses.
    """
    if entry.parent is None or not entry.parent.lifetime or emit.keys is None:
        return None
    reached = next(
        (
            relationship.target
            for relationship in emit.keys.relationships
            if relationship.name == entry.parent.relationship
        ),
        None,
    )
    for other in resolved:
        if reached is not None and nfc(other.expose_id) == nfc(reached):
            held = lifetime_entries_of(other.extension, other.expose_id)[:1]
            if not held:
                return None
            _refuse_varying_lifetime(entry, other, (held[0].start, held[0].end))
            return (held[0].start, held[0].end)
    return None


def _refuse_varying_lifetime(
    entry: Any, parent: ResolvedEmit, columns: Tuple[str, Optional[str]]
) -> None:
    """Refuse (``FLX-3403``) ``parent.lifetime`` on a parent whose versions can differ on its lifetime columns.

    A parent that declares a history gives each entity a chain of versions, and only a carried
    column keeps one value over them (ADR 0017 section 6). A lifetime column that the parent's history
    sets (a validity column, the activation, a tracked or the end column) or that a rule sets can
    differ between versions, and the bound would hold the entity to whichever version its row
    reached. The message names the emit and the columns.
    """
    histories = history_entries_of(parent.extension, parent.expose_id)
    if not histories:
        return
    semantics = (
        declared_exposes(parent.contract_document).get(parent.expose_id, {}).get("semantics")
    )
    varying = set(window_ends(semantics).values()) | set(
        other_sourced_columns(
            parent.contract_document,
            parent.expose_id,
            parent.flux_side_rules,
            parent.keys,
            copies=False,
        )
    )
    for kept in histories:
        varying.update(item.column for item in kept.tracked)
        varying.update(
            column
            for column in (kept.activation, kept.end.column if kept.end is not None else None)
            if column is not None
        )
    named = sorted(column for column in columns if column is not None and column in varying)
    if named:
        raise FluxError(
            FLX_3403,
            f"history ({entry.emit}).parent.lifetime reads {', '.join(map(repr, named))} of "
            f"{parent.expose_id!r}, which declares a history that does not carry "
            f"{'it' if len(named) == 1 else 'them'}: the cells can differ between the versions "
            "of one parent, so the bound would hold an entity to an arbitrary one",
        )


def _bridges_of(resolved: Sequence[ResolvedEmit]) -> Dict[str, Tuple[Tuple[str, str], ...]]:
    """The bridges each emit declares, by exposeId (ADR 0022 section 8)."""
    found: Dict[str, Tuple[Tuple[str, str], ...]] = {}
    for emit in resolved:
        entries = bridge_entries_of(emit.extension, emit.expose_id)
        if entries:
            found[nfc(emit.expose_id)] = tuple(entry.relationships for entry in entries)
    return found


def _weights_of(resolved: Sequence[ResolvedEmit]) -> Dict[str, Dict[str, str]]:
    """The weight column each emit's relationships name, by exposeId and relationship (ADR 0023 section 4)."""
    found: Dict[str, Dict[str, str]] = {}
    for emit in resolved:
        weighted = {
            entry.relationship: entry.weight
            for entry in link_entries_of(emit.extension, emit.expose_id)
            if entry.weight is not None
        }
        if weighted:
            found[nfc(emit.expose_id)] = weighted
    return found


def _counts_of(resolved: Sequence[ResolvedEmit]) -> Dict[str, Dict[str, CountsEntry]]:
    """The counts entry each emit declares, by exposeId and relationship (ADR 0016 section 1)."""
    found: Dict[str, Dict[str, CountsEntry]] = {}
    for emit in resolved:
        entries = {
            entry.relationship: entry for entry in count_entries_of(emit.extension, emit.expose_id)
        }
        if entries:
            found[nfc(emit.expose_id)] = entries
    return found


def _plans_by_expose(planned: Sequence[_PlannedEmit]) -> Dict[str, GeneratorPlan]:
    """One contract's generated plans, by exposeId; an expose emitted twice is refused."""
    plans: Dict[str, GeneratorPlan] = {}
    for entry in planned:
        assert entry.plan is not None  # only generated emits are grouped
        expose_id = nfc(entry.plan.expose.expose_id)
        if expose_id in plans:
            raise FluxError(
                FLX_3403,
                f"expose {expose_id!r} is emitted from statistics more than once, so a foreign "
                "key into it would not know which of the tables to reference; emit it once",
            )
        plans[expose_id] = entry.plan
    return plans


def _generate_emits(
    resolved_emits: Sequence[ResolvedEmit],
    per_emit_digests: Sequence[SeamDigests],
    planned_emits: Sequence[_PlannedEmit],
    compiled_blocks: Sequence[Optional[CompiledFidelity]],
    root: SeedTreeRoot,
    scenario: "_Scenario",
    jobs: int,
    shard: Optional[Tuple[int, int]] = None,
    generators: Optional[Mapping[str, LoadedGenerator]] = None,
    defer_plugins: bool = False,
) -> Tuple[EmitOutcome, ...]:
    """Produce every planned emit's rows, in declared order, with one pool for the run.

    The emits worth workers (:func:`~flux_engine.stages._parallel.workers_for`)
    are cut into slices that go to one pool together, built with every such
    emit's plan; the others are made in this process while the workers run. Each
    emit is then joined in declared order, so a refusal is the one the first
    failing emit gives and the bytes are what one process makes. The workers
    are reserved from the process's budget for as long as the pool lives.

    An emit a generator plugin generates is never given to the pool: the plugin is called once, for
    the emit's rows (or its ``--shard`` range), in this process (ADR 0032 section 5).
    """

    def share(plan: GeneratorPlan) -> Tuple[int, int]:
        return share_of(plan.rows, shard) if shard is not None else (0, plan.rows)

    parallel: Dict[int, GeneratorPlan] = {
        index: planned.plan
        for index, planned in enumerate(planned_emits)
        if planned.plan is not None
        and planned.plan.plugin is None
        and workers_for(share(planned.plan)[1] - share(planned.plan)[0], jobs) > 1
    }
    total = sum(share(plan)[1] - share(plan)[0] for plan in parallel.values())
    with reserve_workers(workers_for(total, jobs) if parallel else 1) as processes:
        with ExitStack() as pooled:
            started: Dict[int, FramedEmit] = {}
            if processes > 1:
                pool = pooled.enter_context(
                    framed_pool(
                        {
                            planned_emits[index].expose.expose_id: (
                                plan,
                                planned_emits[index].expose,
                            )
                            for index, plan in parallel.items()
                        },
                        root=root,
                        workers=processes,
                    )
                )
                started = {
                    index: pool.submit(planned_emits[index].expose.expose_id, rows=share(plan))
                    for index, plan in parallel.items()
                }
            return tuple(
                _generate_emit(
                    resolved,
                    digests,
                    root,
                    scenario,
                    planned,
                    compiled,
                    framed=started.get(index),
                    processes=processes if index in started else 1,
                    shard=shard,
                    generator=(generators or {}).get(resolved.expose_id),
                    defer_plugin=defer_plugins,
                )
                for index, (resolved, digests, planned, compiled) in enumerate(
                    zip(
                        resolved_emits,
                        per_emit_digests,
                        planned_emits,
                        compiled_blocks,
                        strict=True,
                    )
                )
            )


def _generate_emit(
    resolved: ResolvedEmit,
    digests: SeamDigests,
    root: SeedTreeRoot,
    scenario: "_Scenario",
    planned: _PlannedEmit,
    compiled_fidelity: Optional[CompiledFidelity] = None,
    *,
    framed: Optional[FramedEmit] = None,
    processes: int = 1,
    shard: Optional[Tuple[int, int]] = None,
    generator: Optional[LoadedGenerator] = None,
    defer_plugin: bool = False,
) -> EmitOutcome:
    """Produce one planned emit's rows, from declared statistics or from the run.

    *framed* is the emit's slices in the run's pool, when the pool has them; the
    rows and framed lines are then joined from it. Without it the rows are made here.
    """
    expose, plan, mapping = planned.expose, planned.plan, planned.mapping
    rows: Tuple[Tuple[Any, ...], ...] = ()
    writer: Optional[EmitWriter] = None
    dates: Optional[DatesTally] = None

    name = resolved.expose_id
    if plan is not None and plan.plugin is not None and defer_plugin:
        pass  # no rows: the plugin is asked later, or never (a stored run)
    elif plan is not None and plan.plugin is not None:
        assert generator is not None  # `execute` loaded the plugin of every declared expose
        start, stop = share_of(plan.rows, shard) if shard is not None else (0, plan.rows)
        with span(f"emit {name}"):
            rows = _plugin_rows(plan, generator, root, start, stop)
        if shard is not None:
            with span(f"frame {name}"):
                writer = EmitWriter.from_framed(
                    expose,
                    resolved.contract_digest,
                    frame_rows(rows, expose, first_row=start),
                )
    elif plan is not None:
        if framed is not None:
            with span(f"emit {name}"):
                # The workers framed the rows and kept them: the lines are the emit.
                _, lines = framed.join()
            dates = framed.dates or new_tally(plan)
            writer = EmitWriter.from_framed(expose, resolved.contract_digest, lines)
        elif shard is not None:
            start, stop = share_of(plan.rows, shard)
            dates = new_tally(plan)
            with span(f"emit {name}"):
                rows = written_rows(
                    plan, generate_row_slice(plan, start, stop, root=root, tally=dates)
                )
            with span(f"frame {name}"):
                writer = EmitWriter.from_framed(
                    expose,
                    resolved.contract_digest,
                    frame_rows(rows, expose, first_row=start),
                )
        else:
            dates = new_tally(plan)
            with span(f"emit {name}"):
                rows = written_rows(plan, generate_rows(plan, root=root, tally=dates))
    else:
        assert mapping is not None  # `_plan_emit` yields a plan or a mapping, never neither
        with span(f"emit {name}"):
            rows = scenario.rows(mapping, limit=resolved.declared_row_count)
        if shard is not None and shard[0] != 1:
            rows = ()  # no row range to cut: the first share holds the whole stream

    if writer is None:
        writer = EmitWriter(
            expose=expose, contract_digest=resolved.contract_digest, sink=NullSink()
        )
        with span(f"frame {name}"), writer:
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
        processes=processes,
        compiled_fidelity=compiled_fidelity,
        coverage=planned.coverage,
        not_generated=planned.not_generated,
        dates=dates,
    )


def _plugin_rows(
    plan: GeneratorPlan, generator: LoadedGenerator, root: SeedTreeRoot, start: int, stop: int
) -> Tuple[Tuple[Any, ...], ...]:
    """Rows *start* to *stop* of a plugin's expose: the engine's own columns, and the plugin's.

    The plugin is called once for the range, in this process. What it returns is checked against
    each column (:func:`~flux_engine.generator.plugin.generate_values`), put into the rows the
    engine drew for the identifier, key and foreign key columns, and the final rows are held to the
    contract's checks.
    """
    assert plan.plugin is not None
    expose_id = plan.expose.expose_id
    values = generate_values(
        generator.plugin,
        generator.name,
        plan.plugin,
        request_of(
            plan.plugin,
            expose_id,
            n=plan.rows,
            seed=derive_plugin_seed(root, plan.sim_id, expose_id),
            options=copy.deepcopy(dict(generator.declaration.options)),
            start=start,
            stop=stop,
        ),
    )
    rows = generate_row_slice(plan, start, stop, root=root, plugin_values=values)
    check_rows(generator.name, plan, rows, start)
    return rows


@dataclass(frozen=True)
class _Scenario:
    """The run's own moments, ready to be rendered onto a declared row set.

    One instance per execution, built once from the population, the
    detections and the vertical pack. It owns four things and nothing
    else: which source paths the run *declares* (so a mapping can be
    refused at plan time rather than on the first row), which of them are
    always a whole number (so an ``INT`` column's source is checked at plan
    time too), the vertical's alias table, and the ordered moments.

    **Whole numbers are read off the families, never off the values.** A
    derived attribute qualifies when its base family and every lifecycle
    override draw only whole numbers, a trait when every Persona declaring
    it does, a payload field when every Signal of the run declaring it does
    (:attr:`flux_engine.world.families.Family.integral`). The population
    and the events already exist here, and a check of what they happen to
    hold would pass one run and fail the next on the same declarations.

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
    integral_sources: FrozenSet[str]
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
        derivation: Derivation,
        personas: Sequence[Mapping[str, Any]],
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
            integral_sources=integral_namespace(
                subject_attributes=derivation.integral_names(),
                subject_traits=integral_trait_names(personas),
                event_payload_fields=integral_payload_fields(signal_ids, pack),
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
            built.append(mapping.row(self._values(item.payload), row_index=len(built)))
        return tuple(built)
