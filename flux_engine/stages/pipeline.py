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
from dataclasses import dataclass, replace
from decimal import localcontext
from typing import Any, Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.compiler import CompiledFidelity, compile_fidelity
from flux_engine.concordance.predicate import (
    DeclaredExpose,
    FidelityBlock,
    expose_from_contract,
    parse_fidelity_block,
)
from flux_engine.core import kernel2
from flux_engine.core import kernels as _kernels
from flux_engine.core.canonical import canonicalise, nfc
from flux_engine.core.clock import Phase, Scheduler, VirtualClock, declared_time_bounds
from flux_engine.core.digest import DigestedDocument, build_input_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import Kernel
from flux_engine.core.rng import clear_memos as clear_draw_memos
from flux_engine.core.rng import kernel_context
from flux_engine.core.seedtree import SeedTree, SeedTreeRoot, derive_root, effective_seed
from flux_engine.core.trace import RunComponents
from flux_engine.expr.attrs import AttributeModel, build_attribute_model
from flux_engine.expr2.errors import Expr2Error
from flux_engine.expr2.rules import check_rule_ids
from flux_engine.generator import GeneratorPlan, build_plan, generate_rows
from flux_engine.generator.coverage import coverage_of_plan
from flux_engine.generator.relations import FLX_3403, relate_plans, related_columns
from flux_engine.generator.rows import clear_memos as clear_generator_memos
from flux_engine.generator.rows import generator_version_for
from flux_engine.generator.rules import RulePlan, flux_rule_entries, plan_rules
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
    integral_payload_fields,
    requests_from_transitions,
    resolve_cast,
    run_campaign,
    run_detectors,
    run_journeys,
)
from flux_engine.runtime.cast import strip_range
from flux_engine.seam.contract import CompiledExpose
from flux_engine.seam.coverage import ExposeCoverage, require_full_coverage, strictness_asked_by
from flux_engine.seam.emit import (
    EmitResult,
    EmitWriter,
    SeamDigests,
    seam_digests,
    seam_entropy_bytes,
)
from flux_engine.seam.keys import compile_keys
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
from flux_engine.seam.resolve import (
    EXTENSION_NAMESPACE,
    FLX_3105,
    LoadedBundle,
    ResolvedEmit,
    load_bundle,
    parameter_entries,
    profile_reference,
    resolve_all_emits,
    strict_coverage_declared,
)
from flux_engine.seam.sinks import NullSink
from flux_engine.stages._parallel import (
    generate_framed,
    require_jobs,
    reserve_workers,
    workers_for,
)
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
    "compile_declared_fidelity",
    "execute",
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


def plan_emit_rules(resolved: ResolvedEmit, *, strict: bool, where: str) -> Optional[RulePlan]:
    """The contract's rules for *resolved*'s expose, planned as ``plan`` plans them.

    One call for ``plan``, ``validate`` and ``fluxsim init``'s pre-flight, so a
    rule ``plan`` refuses (EXPR-SPEC-2 §12) is refused by all three.
    """
    return plan_rules(
        contract_document=resolved.contract_document,
        expose_id=resolved.expose_id,
        settings=resolved.invariant_settings,
        related=related_columns(resolved.keys),
        mapping=resolved.column_mapping,
        has_fidelity=resolved.fidelity is not None,
        strict=strict,
        where=where,
        flux_rules=resolved.flux_side_rules,
        keys=resolved.keys,
        rows=resolved.declared_row_count,
        null_fractions=_declared_null_fractions(resolved),
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
            resolved.compile(),
            sim_id=sim_id,
            seam_digest="sha256:" + "0" * 64,
            rows=resolved.declared_row_count or 1,
            fidelity=resolved.fidelity,
            time_bounds=simulation.get("spec", {}).get("timeBounds"),
            related=related_columns(resolved.keys),
            rules=rules,
        )
    except Expr2Error:
        raise
    except FluxError:
        # A column the plan cannot build at all (FLX-3402 and kin) is `plan`'s
        # refusal, which validate leaves to it as it does for an emit without rules.
        return


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
    if resolved.fidelity is None:
        return None
    _, expose = _declared_contract(resolved)
    return parse_fidelity_block(canonicalise(resolved.fidelity), expose=expose)


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


def execute(
    directory: Any,
    *,
    simulation_id: Optional[str] = None,
    zone: Optional[str] = None,
    tolerate_additive: bool = True,
    jobs: int = 1,
    strict_coverage: bool = False,
    kernel: Optional[Kernel] = None,
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

    However the run ends, the draws' memos are cleared when it does
    (:func:`flux_engine.core.rng.clear_memos`,
    :func:`flux_engine.generator.rows.clear_memos`): what they hold is worked
    out from this run's declarations, and a long-lived process that runs
    many bundles keeps none of it past the run.
    """
    require_jobs(jobs)
    run_kernel = _kernels.DEFAULT_KERNEL if kernel is None else kernel
    with _memos_for_one_run(), localcontext(kernel_context()):
        if run_kernel.seed_tree_version == kernel2.SEED_TREE_VERSION:
            # Kernel 0.2.0's continuous draws are binary64: refuse a process that would
            # compute other bits before anything is drawn (FLX-5050).
            kernel2.require_subnormals()
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

        # The two declarations the FLUX schema leaves optional and this engine
        # cannot run without, refused in the order `validate` checks them,
        # before the population exists (FLX-5042, FLX-5023).
        pack = pack_of_world(world_doc)
        start, end = declared_time_bounds(simulation)
        model: AttributeModel = build_attribute_model(
            _BundleView(loaded.flux_documents), pack, simulation_id=cast.simulation_id
        )

        resolved_emits = resolve_all_emits(loaded, simulation_id=cast.simulation_id)
        check_flux_side_rule_ids(resolved_emits, where=cast.simulation_id)
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
        root = derive_root(
            seam_entropy_bytes(list(per_emit_digests)),
            seed,
            seed_tree_version=run_kernel.seed_tree_version,
        )

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
        planned_emits = _relate_emits(
            resolved_emits,
            tuple(
                _plan_emit(
                    resolved,
                    digests,
                    cast,
                    simulation,
                    scenario,
                    generator_version_for(run_kernel.seed_tree_version),
                    strict=bool(asked_by),
                )
                for resolved, digests in zip(resolved_emits, per_emit_digests, strict=True)
            ),
        )
        if asked_by:
            require_full_coverage(
                (planned.coverage for planned in planned_emits), asked_by=asked_by
            )
        emits = tuple(
            _generate_emit(resolved, digests, root, scenario, planned, jobs, compiled)
            for resolved, digests, planned, compiled in zip(
                resolved_emits, per_emit_digests, planned_emits, compiled_blocks, strict=True
            )
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

        input_digest = input_digest_of(loaded, kernel=run_kernel)

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
            kernel=run_kernel,
            parameters=parameters,
        )


@dataclass(frozen=True)
class _PlannedEmit:
    """What one emit will be produced from, decided before any row exists."""

    expose: CompiledExpose
    plan: Optional[GeneratorPlan]
    mapping: Optional[ColumnMapping]
    coverage: ExposeCoverage


def _plan_emit(
    resolved: ResolvedEmit,
    digests: SeamDigests,
    cast: Cast,
    simulation: Mapping[str, Any],
    scenario: "_Scenario",
    generator_version: str,
    strict: bool = False,
) -> _PlannedEmit:
    """Decide how one emit's columns will be produced, refusing what cannot be.

    Every refusal a contract can earn at plan time (``FLX-3302``,
    ``FLX-3303``, ``FLX-3401``, ``FLX-3402``) is raised here, and the
    coverage report is read off the same objects that raised them. It runs
    for every emit before the first row of any of them is drawn, so a strict
    run (``FLX-3304``) is refused with every emit's gaps in one message and
    without having generated a row it then throws away.
    """
    expose = resolved.compile()
    rules = plan_emit_rules(resolved, strict=strict, where=cast.simulation_id)
    if resolved.fidelity is not None:
        count = (
            resolved.declared_row_count
            if resolved.declared_row_count is not None
            else len(scenario.store)
        )
        plan = build_plan(
            expose,
            sim_id=cast.simulation_id,
            seam_digest=digests.h.value,
            rows=count,
            fidelity=resolved.fidelity,
            time_bounds=simulation.get("spec", {}).get("timeBounds"),
            related=related_columns(resolved.keys),
            rules=rules,
            generator_version=generator_version,
        )
        return _PlannedEmit(expose=expose, plan=plan, mapping=None, coverage=coverage_of_plan(plan))
    mapping = resolve_column_mapping(
        expose,
        declared_sources=scenario.declared_sources,
        integral_sources=scenario.integral_sources,
        aliases=scenario.aliases,
        overrides=resolved.column_mapping,
    )
    return _PlannedEmit(
        expose=expose, plan=None, mapping=mapping, coverage=mapping.coverage(expose)
    )


def _relate_emits(
    resolved_emits: Sequence[ResolvedEmit], planned_emits: Tuple[_PlannedEmit, ...]
) -> Tuple[_PlannedEmit, ...]:
    """Relate the generator-driven emits of each contract that declares keys.

    KERNEL-SPEC section 8.1: the exposes one contract relates are generated
    together, so every key is unique and every foreign key names a row its
    parent holds (:func:`flux_engine.generator.relations.relate_plans`). The
    emits of a contract are grouped by the contract file they resolve to. A
    plan the contract relates nothing in comes back unchanged, and so does
    its coverage. Runs after every emit is planned and before any row is
    drawn, so a relationship that cannot hold is refused (``FLX-3403``) with
    nothing generated.
    """
    groups: Dict[str, List[int]] = {}
    for index, (resolved, planned) in enumerate(zip(resolved_emits, planned_emits, strict=True)):
        if planned.plan is not None and resolved.keys is not None:
            groups.setdefault(str(resolved.contract_path), []).append(index)
    related = list(planned_emits)
    for indexes in groups.values():
        contract_keys = compile_keys(resolved_emits[indexes[0]].contract_document)
        if contract_keys is None or not contract_keys.declares_keys:
            continue
        with_relations = relate_plans(
            _plans_by_expose([planned_emits[index] for index in indexes]), contract_keys
        )
        for index in indexes:
            planned = planned_emits[index]
            assert planned.plan is not None  # grouped above
            plan = with_relations[nfc(planned.plan.expose.expose_id)]
            if plan is not planned.plan:
                related[index] = replace(planned, plan=plan, coverage=coverage_of_plan(plan))
    return tuple(related)


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


def _generate_emit(
    resolved: ResolvedEmit,
    digests: SeamDigests,
    root: SeedTreeRoot,
    scenario: "_Scenario",
    planned: _PlannedEmit,
    jobs: int = 1,
    compiled_fidelity: Optional[CompiledFidelity] = None,
) -> EmitOutcome:
    """Produce one planned emit's rows, from declared statistics or from the run."""
    expose, plan, mapping = planned.expose, planned.plan, planned.mapping
    rows: Tuple[Tuple[Any, ...], ...] = ()
    writer: Optional[EmitWriter] = None
    processes = 1

    if plan is not None:
        with reserve_workers(workers_for(plan.rows, jobs)) as processes:
            if processes > 1:
                rows, lines = generate_framed(plan, root=root, expose=expose, workers=processes)
                writer = EmitWriter.from_framed(expose, resolved.contract_digest, lines)
            else:
                rows = generate_rows(plan, root=root)
    else:
        assert mapping is not None  # `_plan_emit` yields a plan or a mapping, never neither
        rows = scenario.rows(mapping, limit=resolved.declared_row_count)

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
        processes=processes,
        compiled_fidelity=compiled_fidelity,
        coverage=planned.coverage,
    )


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
