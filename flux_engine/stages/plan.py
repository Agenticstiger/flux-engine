# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim plan``: execute the whole loop once, in memory, and record it.

Terraform's shape, on this engine: a plan runs the *entire* pipeline
(:func:`flux_engine.stages.pipeline.execute`) exactly the way ``apply``
will, and writes down what it saw -- ``inputDigest``, the seed-tree paths
it materialised (``seedTreeDigest``), the per-emit seam digests (``H``,
``G``), and, when an emit declares a fidelity block, its generator plan and
the compiled block. The block is compiled first, as SEAM-FIDELITY-SPEC §3
says (:func:`flux_engine.stages.pipeline.compile_declared_fidelity`), so a
block ``verify`` would refuse is refused before the population exists, and
the emit's entry records what the compile gave, under ``fidelity``: every
property's epsilon, margin and magnitude bounds, because §3 writes "every
``ε``" to ``plan.json``. It writes two files: ``plan.json``, and the
``seed-paths.ndjson`` beside it that KERNEL-SPEC §3 puts the path list in.
Everything else is ``apply``'s. By default the plan also stages what ``apply``
would write under ``<run>/.stage/``, and ``apply --run-id`` commits that after
checking it, so the two cost one generation (ADR 0015); ``--no-stage`` leaves
``apply`` to execute the plan again, which KERNEL-SPEC's determinism promise says
reaches the same trace and the same emitted rows.

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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar, Union

from flux_engine.api.stages import StageResult
from flux_engine.core import kernels as _kernels
from flux_engine.core.digest import DigestRef, build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.grades import Pattern, weakest
from flux_engine.core.kernels import Kernel
from flux_engine.core.seedtree import (
    SeedPath,
    SeedTree,
    SeedTreeRoot,
    parse_seed_paths,
    seed_paths_digest,
)
from flux_engine.core.sizing import MOST_ROWS
from flux_engine.evidence.provenance import (
    PROVENANCE_DIGEST_KEY,
    BoundProvenance,
    build_provenance_document,
    parse_provenance,
    provenance_digest,
)
from flux_engine.evidence.register import (
    REGISTER_DIGEST_KEY,
    BoundRegister,
    RegisterSummary,
    parse_register,
    register_digest,
    render_register,
    render_register_line,
)
from flux_engine.generator.periods import DEFAULT_PERIODIC_ROWS
from flux_engine.generator.relations import ForeignKey, history_pick_of
from flux_engine.generator.statistics import FLX_3402
from flux_engine.generators import environment as _environment
from flux_engine.lineage.emitter import dataset_for_expose
from flux_engine.outcomes.responses import REPLICATE_PLAN_KEY, RESPONSES_PLAN_KEY, Replicate
from flux_engine.safeio import (
    is_regular_file,
    read_bytes_nofollow,
    write_bytes_atomic,
    write_text_atomic,
)
from flux_engine.seam.coverage import ExposeCoverage, printable
from flux_engine.seam.declared import NotGenerated, render_not_generated
from flux_engine.seam.history import END_SHARE_OF_ENTITIES, history_entries_of
from flux_engine.seam.mean_reach import check_restated_means
from flux_engine.seam.overlay import overlaid_profile
from flux_engine.seam.parameters import check_profile_citations
from flux_engine.seam.profile import require_profile
from flux_engine.seam.resolve import (
    FLX_3105,
    declared_generators,
    declared_overlay,
    profile_reference,
    scorecard_declaration,
)
from flux_engine.seam.scorecard import thresholds_document
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages._consumer_check import consumers_unchecked, unchecked_line
from flux_engine.stages._grades import GRADE_KEY
from flux_engine.stages.errors import (
    ColumnProvenanceMoved,
    KernelNotImplemented,
    ParameterRegisterMoved,
    SeedPathsMoved,
)
from flux_engine.stages.layout import RunLayout, run_id_for
from flux_engine.stages.scaling import optional_scale, scale_text
from flux_engine.stages.staging import STAGE_DIRNAME, discard_stage, stage_execution
from flux_engine.stages.timings import lap, timed

__all__ = [
    "DATE_RANGES_CUT",
    "DATE_RANGES_PLAN_KEY",
    "SCALE_FLOORS_PLAN_KEY",
    "END_SHARE_PLAN_KEY",
    "GENERATORS_PLAN_KEY",
    "OVERLAY_PLAN_KEY",
    "PROFILE_PIN_KEY",
    "SCORECARD_PLAN_KEY",
    "build_plan_document",
    "check_simulation_against_profile",
    "column_provenance_text",
    "parameter_register_text",
    "plan",
    "plan_with_result",
    "planned_seed_tree",
    "read_column_provenance",
    "read_parameter_register",
    "read_seed_paths",
    "render_document",
]


def _fidelity_summary(
    outcome: Union["_pipeline.EmitOutcome", "_pipeline.HiddenOutcome"],
) -> Optional[Dict[str, Any]]:
    """A JSON-safe summary of an emit's generator plan, or ``None`` when undeclared."""
    if outcome.plan is None:
        return None
    summary: Dict[str, Any] = {
        "generatorDigest": outcome.plan.digest().value,
        "rows": outcome.plan.rows,
        "columns": [item.column.name for item in outcome.plan.columns if not item.column.hidden],
    }
    hidden = [item.column.name for item in outcome.plan.columns if item.column.hidden]
    if hidden:
        # Drawn and never written (ADR 0024 section 2): listed apart, so `columns` is what is written.
        summary["hiddenColumns"] = hidden
    return summary


def _copula_entry(
    outcome: Union["_pipeline.EmitOutcome", "_pipeline.HiddenOutcome"],
) -> Dict[str, Any]:
    """``copula: {alpha, factor}`` for an emit whose plan holds a copula, else nothing."""
    copula = None if outcome.plan is None else getattr(outcome.plan, "copula", None)
    return {} if copula is None else {"copula": copula.as_document()}


def _counts_given_entry(
    outcome: Union["_pipeline.EmitOutcome", "_pipeline.HiddenOutcome"],
) -> Dict[str, Any]:
    """``countsGiven`` for an emit with a counted relationship given a parent column, else nothing.

    One member per such relationship, in relationship order: the column, the weights of every
    class of it over the classes of ``children``, and the classes that took ``children``'s own
    weights because the entry gave them no list (ADR 0037 decision 4). Any other emit has no key,
    so its plan keeps its bytes.
    """
    plan = outcome.plan
    relations = None if plan is None else plan.relations
    if relations is None:
        return {}
    found = [
        {"relationship": driver.relationship, **driver.counted.as_document_given()}
        for _, driver in relations.drivers
        if isinstance(driver, ForeignKey)
        and driver.counted is not None
        and driver.counted.given is not None
    ]
    return (
        {"countsGiven": sorted(found, key=lambda item: str(item["relationship"]))} if found else {}
    )


def _derived_rows_entry(
    outcome: Union["_pipeline.EmitOutcome", "_pipeline.HiddenOutcome"],
) -> Dict[str, Any]:
    """A projected emit's derived ``rows`` and where they come from (ADR 0027 section 2).

    Only an emit a projection names has them, so every other plan keeps its bytes. The count
    reaches ``planDigest`` here and never an entry for ``H``.
    """
    plan = outcome.plan
    projection = None if plan is None else plan.projection
    if plan is None or projection is None:
        return {}
    return {
        "rows": plan.rows,
        "rowsFrom": {"of": projection.of, "projection": projection.kind},
    }


def _shortfall_entry(
    outcome: Union["_pipeline.EmitOutcome", "_pipeline.HiddenOutcome"],
) -> Dict[str, Any]:
    """``declaredRows`` and ``shortfall`` of a history that holds fewer rows than it declares (ADR 0017).

    A history that keeps to its parent's lifetime holds fewer rows than the contract declares when
    no entity has room left for a version; the emit's ``generator.rows`` is what it holds. The two
    keys say what was declared and how many rows are missing, and only an emit with a shortfall
    above zero has them, so every other plan keeps its bytes and its planDigest. The digest covers
    them, and ``verify`` reports them from the plan.
    """
    pick = None if outcome.plan is None else history_pick_of(outcome.plan)
    if pick is None or pick.declared_rows is None or pick.declared_rows <= pick.rows:
        return {}
    return {"declaredRows": pick.declared_rows, "shortfall": pick.declared_rows - pick.rows}


def column_provenance_text(result: "_pipeline.PipelineResult") -> str:
    """The exact text of ``column-provenance.json`` for *result*.

    One function for the two stages that need it: ``plan`` hashes it into
    ``plan.json`` and ``apply`` writes it, so the file on disk is the file the
    plan's digest was taken over, by construction and not by two builders
    agreeing. It is a function of the bundle's declarations and nothing else.
    """
    return render_document(
        build_provenance_document(
            simulation_id=result.cast.simulation_id,
            exposes=tuple(
                (
                    outcome.coverage,
                    dataset_for_expose(
                        outcome.resolved.contract_document, outcome.resolved.expose_id
                    ),
                )
                for outcome in result.emits
                if outcome.coverage is not None
            ),
        )
    )


def _not_generated_of(result: "_pipeline.PipelineResult") -> Tuple[NotGenerated, ...]:
    """Every declaration of the run's emits that is accepted and not generated, in emit order."""
    return tuple(row for outcome in result.emits for row in outcome.not_generated)


def parameter_register_text(result: "_pipeline.PipelineResult") -> Optional[str]:
    """The exact text of ``parameter-register.json`` for *result*, or ``None`` when it declares none.

    One function for ``plan``, which hashes it into ``plan.json``, and ``apply``,
    which writes it, as :func:`column_provenance_text` is for the column record.
    It is a function of the bundle's documents and the Simulation's pin, never of
    the profile, so ``apply --run-id`` and ``replay`` rebuild the same bytes
    without being handed the profile.
    """
    if result.parameters is None:
        return None
    return render_register(result.parameters)


def _register_entries(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """``parameterRegisterDigest``, only for a Simulation that declares a register.

    Absent otherwise, so a plan whose Simulation declares none keeps the bytes
    and the planDigest it had.
    """
    text = parameter_register_text(result)
    if text is None:
        return {}
    return {REGISTER_DIGEST_KEY: register_digest(text.encode("utf-8")).value}


#: ``plan.json``'s key for the digest of the profile the Simulation pins
#: (docs/spec/PROFILE-SPEC.md §6). Present only when it pins one.
PROFILE_PIN_KEY = "profileDigest"

#: ``plan.json``'s key for the rows the pinned profile publishes for each reference expose
#: (ADR 0023 section 2), so ``apply --run-id``, ``replay`` and ``verify`` need no profile for
#: them. Present only when a run has a reference expose.
REFERENCES_PLAN_KEY = "references"

#: ``plan.json``'s key for the thresholds the Simulation declares on its
#: scorecard (docs/spec/SCORECARD-SPEC.md). Present only when it declares some.
SCORECARD_PLAN_KEY = "scorecard"


#: ``plan.json``'s key for the profile overlay the Simulation declares, ``{name, patch}``
#: (ADR 0030 section 7), so ``verify`` and ``ship`` score against the overlaid profile through the
#: plan's digest. Present only when the Simulation declares one.
OVERLAY_PLAN_KEY = "overlay"

#: ``plan.json``'s key for what each generator plugin of the run was and ran in (ADR 0032
#: section 6). Present only when the Simulation declares a plugin.
GENERATORS_PLAN_KEY = "generators"


#: ``plan.json``'s key for what a history's ``end`` share is a share of (ADR 0017, the amendment of
#: 2026-11-24): ``"entities"``, every entity. Present only when an emit of the run declares a
#: history whose ``end`` leaves some entity unable to end (a ``column`` or ``finalVersion``), the one
#: allotment the two takes of the share tell apart, so every other plan keeps its bytes and its
#: planDigest. A recorded plan
#: without it is one written by engine 0.11.1 or earlier, whose share was a share of the entities
#: that can end, and it replays under that allocation (:func:`flux_engine.stages.apply.plan_end_share`).
END_SHARE_PLAN_KEY = "historyEndShare"


#: ``plan.json``'s key for a contract's date range cutting what a column draws (ADR 0024, the
#: amendment of 2026-11-25): ``"cut"``. Present only when the plan cut at least one column, so every
#: other plan keeps its bytes and its planDigest. A recorded plan without it replays with no cut
#: (:func:`flux_engine.stages.apply.plan_date_ranges`), so a plan written before the cut keeps the
#: rows and the run id it had.
DATE_RANGES_PLAN_KEY = "dateRanges"
DATE_RANGES_CUT = "cut"


#: ``plan.json``'s key for a ``--scale`` below 1 keeping a drawn parent at the rows its children's
#: tracked levels need (ADR 0017, the amendment of 2026-11-25): ``"trackedLevels"``. Present only
#: when a parent was kept at more rows than the scale gives it, so every other plan keeps its bytes.
#: A recorded plan without it replays with no check of tracked levels and no floor
#: (:func:`flux_engine.stages.apply.plan_scale_floors`), as a plan written before them was made.
SCALE_FLOORS_PLAN_KEY = "scaleFloors"
SCALE_FLOORS_TRACKED = "trackedLevels"


def _scale_floor_entries(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """``scaleFloors``, only for a plan in which some parent was kept above its scaled rows."""
    return {SCALE_FLOORS_PLAN_KEY: SCALE_FLOORS_TRACKED} if result.scale_floors else {}


def _date_range_entries(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """``dateRanges``, only for a plan in which some column was cut at its contract's range."""
    if not result.cut_ranges:
        return {}
    outcomes: List[Union["_pipeline.EmitOutcome", "_pipeline.HiddenOutcome"]] = [
        *result.emits,
        *result.hidden_emits,
    ]
    cut = any(
        column.range_cut is not None
        for outcome in outcomes
        if outcome.plan is not None
        for column in outcome.plan.columns
    )
    return {DATE_RANGES_PLAN_KEY: DATE_RANGES_CUT} if cut else {}


#: ``plan.json``'s key for the rows a periodic emit may be estimated to derive (ADR 0027 section 5,
#: the amendment of 2026-11-25): the budget ``fluxsim plan --max-rows`` set. Present only for a
#: budget other than :data:`~flux_engine.generator.periods.DEFAULT_PERIODIC_ROWS`, so every other
#: plan keeps its bytes and its planDigest, and ``apply --run-id`` holds the run to the same budget.
MAX_ROWS_PLAN_KEY = "maxRows"


#: ``plan.json``'s key for a history and a periods entry keeping to the contract's ranges on the
#: columns they set (ADR 0024, the amendment of 2026-11-25): ``"held"``. Present only when a range cut
#: a history's layout or a periods entry's periods, so every other plan keeps its bytes and its
#: planDigest. A recorded plan without it replays as it was made
#: (:func:`flux_engine.stages.apply.plan_range_setters`).
RANGE_SETTERS_PLAN_KEY = "rangeSetters"
RANGE_SETTERS_HELD = "held"


def _range_setter_entries(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """``rangeSetters``, only for a plan in which a range cut a history or a periods entry."""
    if not result.bounds.ranges:
        return {}
    outcomes: List[Union["_pipeline.EmitOutcome", "_pipeline.HiddenOutcome"]] = [
        *result.emits,
        *result.hidden_emits,
    ]
    plans = [outcome.plan for outcome in outcomes if outcome.plan is not None]
    held = any(
        ((pick := history_pick_of(plan)) is not None and pick.ranged is not None)
        or (plan.projection is not None and plan.projection.clip is not None)
        for plan in plans
    )
    return {RANGE_SETTERS_PLAN_KEY: RANGE_SETTERS_HELD} if held else {}


def _max_rows_entries(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """``maxRows``, only for a plan whose periodic rows' budget is not the default."""
    budget = result.bounds.budget
    if budget is None or budget == DEFAULT_PERIODIC_ROWS:
        return {}
    return {MAX_ROWS_PLAN_KEY: budget}


def _periodic_budget(max_rows: Optional[int]) -> Optional[int]:
    """*max_rows* as the pipeline takes it; a budget that is not a count from 1 to ``MOST_ROWS`` is a ``ValueError``."""
    if max_rows is not None and (
        isinstance(max_rows, bool)
        or not isinstance(max_rows, int)
        or not 1 <= max_rows <= MOST_ROWS
    ):
        raise ValueError(f"max_rows is a whole number from 1 to {MOST_ROWS}, not {max_rows!r}")
    return max_rows


def _end_share_entries(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """``historyEndShare``, only for a plan that allots a history's ``end`` a share of every entity.

    Only an ``end`` that leaves an entity unable to end has an allotment the old share could give
    differently: with every entity eligible the share of the eligible entities is the share of all.

    A run that executes a recorded plan (``apply --run-id``, ``replay``) never builds a plan document,
    so the key a plan says is the one it was made with; the plan digest covers it, so planning a
    bundle again moves the run id.
    """
    if result.end_share != END_SHARE_OF_ENTITIES:
        return {}
    outcomes: List[Union["_pipeline.EmitOutcome", "_pipeline.HiddenOutcome"]] = [
        *result.emits,
        *result.hidden_emits,
    ]
    emits = [outcome.resolved for outcome in outcomes]
    declared = any(
        entry.end is not None and not entry.end.every_entity_eligible
        for resolved in emits
        for entry in history_entries_of(resolved.extension, resolved.expose_id)
    )
    return {END_SHARE_PLAN_KEY: END_SHARE_OF_ENTITIES} if declared else {}


def _generator_entries(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """``generators``: per expose a plugin generates, what ran it, only for a Simulation that declares one.

    The entry point, the distribution and its version, whether the install is editable, the grade,
    the privacy account (``P4``), the reproducibility the run is recorded with, the environment
    digest the engine measured and the model digest (when declared). An editable install is recorded
    ``none`` whatever the declaration says, because its code lies in a source tree that changes
    without a new version. Absent otherwise, so a plan whose Simulation declares none keeps the
    bytes and the planDigest it had.
    """
    if not result.generators:
        return {}
    environment = _environment.environment_digest()
    entries: Dict[str, Any] = {}
    for expose_id, loaded in sorted(result.generators.items()):
        declaration = loaded.declaration
        entry: Dict[str, Any] = {
            "plugin": loaded.resolved.name,
            "distribution": loaded.resolved.distribution,
            "version": loaded.resolved.version,
            "editable": loaded.editable,
            "grade": declaration.grade.value,
            "reproducibility": "none" if loaded.editable else declaration.reproducibility,
            "environmentDigest": environment,
        }
        if declaration.privacy is not None:
            entry["privacy"] = declaration.privacy.to_document()
        if declaration.model_digest is not None:
            entry["modelDigest"] = declaration.model_digest
        entries[expose_id] = entry
    return {GENERATORS_PLAN_KEY: entries}


def _profile_entries(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """What the plan records of a pinned profile: its digest, and any scorecard thresholds.

    Read off the Simulation, never off the profile document, so ``replay``
    re-derives the same plan without being handed the profile. ``verify``
    and ``ship`` read both through the plan's digest. A Simulation that pins
    none gets neither key, so its plan keeps the bytes and the planDigest it
    had.
    """
    simulation, where = result.cast.simulation, result.cast.simulation_id
    pinned = profile_reference(simulation, where=where)
    if pinned is None:
        return {}
    entries: Dict[str, Any] = {PROFILE_PIN_KEY: pinned}
    declaration = scorecard_declaration(simulation, where=where)
    if declaration is not None:
        entries[SCORECARD_PLAN_KEY] = thresholds_document(declaration)
    overlay = declared_overlay(simulation, where=where)
    if overlay is not None:
        entries[OVERLAY_PLAN_KEY] = overlay.to_document()
    return entries


def build_plan_document(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """The document ``plan.json`` holds, before ``planDigest`` is folded in."""
    return {
        **_profile_entries(result),
        **_register_entries(result),
        **_generator_entries(result),
        **_end_share_entries(result),
        **_date_range_entries(result),
        **_scale_floor_entries(result),
        **_max_rows_entries(result),
        **_range_setter_entries(result),
        **(
            {
                REFERENCES_PLAN_KEY: {
                    expose_id: rows.as_document() for expose_id, rows in result.references.items()
                }
            }
            if result.references
            else {}
        ),
        # The estimates the response families took from the pinned profile (ADR 0029), so
        # `apply --run-id`, `replay` and `verify` need no profile for them. Present only for a
        # Simulation that declares `responses`.
        **(
            {RESPONSES_PLAN_KEY: result.responses.estimates.as_document()}
            if result.responses is not None
            else {}
        ),
        # The sweep run a forecast's replicate is, with the effects it took, so that `replay`
        # applies the same ones. Only a replicate plan holds it.
        **(
            {REPLICATE_PLAN_KEY: result.replicate.as_document()}
            if result.replicate is not None
            else {}
        ),
        # The kernel the execution ran, which is what every later stage reads
        # back (flux_engine.stages.apply.plan_kernel).
        "kernelVersion": result.kernel.version,
        "seedTreeVersion": result.kernel.seed_tree_version,
        # Only a run at another size than the bundle's: a plan without it keeps its bytes.
        **({"scale": scale_text(result.scale)} if result.scale != 1 else {}),
        "simulationId": result.cast.simulation_id,
        "fidelity": result.fidelity,
        "seed": result.seed,
        "inputDigest": result.input_digest.ref.value,
        "seedTreeDigest": result.seed_tree.digest(),
        "seedTreeShapes": result.seed_tree.shape_counts(),
        # SEAM-FIDELITY-SPEC §1: how each column is produced is a statement the
        # plan makes, as the epsilons are. The record is a file `apply` writes,
        # as `seed-paths.ndjson` is, so the plan carries its digest, and `ship`
        # and `verify` read the file back only through it (FLX-2115).
        PROVENANCE_DIGEST_KEY: provenance_digest(
            column_provenance_text(result).encode("utf-8")
        ).value,
        "emits": _plan_emits(result),
        "planDigest": None,  # self-referential -- masked, filled by build_plan_digest
        "generated_at": None,  # masked -- see NON_DIGEST_FIELDS
    }


def _plan_emits(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """The plan's entry for every emit, in declared order: the written emits and the hidden tables."""
    everyone: List[Union["_pipeline.EmitOutcome", "_pipeline.HiddenOutcome"]] = [
        *result.emits,
        *result.hidden_emits,
    ]
    everyone.sort(key=lambda outcome: outcome.resolved.emit_index)
    return {
        outcome.resolved.expose_id: {
            "H": outcome.digests.h.value,
            "G": outcome.digests.g.value,
            "productRef": outcome.resolved.product_ref,
            "declaresFidelity": outcome.resolved.fidelity is not None,
            "generator": _fidelity_summary(outcome),
            **_derived_rows_entry(outcome),
            # A history that holds fewer rows than it declares says what it declared and by how
            # many it fell short; any other emit has no key, so its plan keeps its bytes.
            **_shortfall_entry(outcome),
            # An emit that declares a copula records its shrinkage and its factor's digest (ADR
            # 0035 decision 5); any other emit has no key, so its plan keeps its bytes.
            **_copula_entry(outcome),
            # An emit with a counted relationship given a parent column states the weights each
            # class took, those it took from the shape included (ADR 0037 decision 4).
            **_counts_given_entry(outcome),
            # SEAM-FIDELITY-SPEC §3: "every ε" is written to plan.json.
            # The key is the one the block is declared under, and an
            # emit that declares none gets no key, so its plan keeps
            # the bytes and the planDigest it had.
            **(
                {"fidelity": outcome.compiled_fidelity.plan_document()}
                if outcome.compiled_fidelity is not None
                else {}
            ),
            # A hidden table is in the plan with its generatorDigest and in no stream of the run
            # (ADR 0024 section 3); the key is there only for one.
            **({"hidden": True} if outcome.resolved.is_hidden_table else {}),
        }
        for outcome in everyone
    }


def kernel_asked_for(version: str) -> Kernel:
    """The kernel a new plan asks for by *version*, or the refusal (``FLX-2119``)."""
    found = _kernels.kernel_named(version)
    if found is None:
        raise KernelNotImplemented(
            named=printable(str(version)),
            implemented=[
                f"{known.version} (seed tree {known.seed_tree_version})"
                for _, known in sorted(_kernels.KERNELS.items())
            ],
        )
    return found


def plan(
    bundle_directory: Union[str, Path],
    *,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    jobs: int = 1,
    strict_coverage: bool = False,
    profile: Union[str, Path, None] = None,
    kernel: Optional[str] = None,
    stage: bool = True,
    scale: Any = None,
    timings: bool = False,
    allow_generators: Sequence[str] = (),
    max_rows: Optional[int] = None,
) -> StageResult:
    """Execute the loop once and write ``<state_dir>/runs/<runId>/plan.json``.

    *stage* keeps the execution: what ``apply`` would write is staged under the run
    directory (:mod:`flux_engine.stages.staging`), and ``apply --run-id`` commits it
    without generating the rows again. ``stage=False`` (``fluxsim plan --no-stage``)
    writes ``plan.json`` and the seed paths only, as a plan did before.

    *scale* multiplies every emit's rows and key spans (:mod:`flux_engine.stages.scaling`);
    the plan records it, and ``apply`` and ``replay`` read it back. 1 or ``None`` is the
    bundle as it is, and records nothing. *timings* adds where the time went to the output
    and the ``timings`` artifact (:mod:`flux_engine.stages.timings`).

    *allow_generators* names the generator plugins the person allows to load
    (``fluxsim plan --allow-generator``, ADR 0032 section 3): a bundle that declares a plugin
    this does not name is refused (``FLX-3119``) before anything is imported.

    *jobs* is how many processes may generate rows from declared statistics;
    it never changes what is generated (:func:`flux_engine.stages.pipeline.execute`).

    *strict_coverage* refuses (``FLX-3304``) a run in which a column fell
    back to a type default. Without it the plan reports those columns and
    goes on, unless the Simulation declares ``strictCoverage: true`` in its
    extensions, which asks for the same refusal and which the argument cannot
    turn off; either way ``plan.json`` is the same bytes.

    *profile* is the profile document the Simulation pins, if it pins one
    (docs/spec/PROFILE-SPEC.md): the plan refuses a pin with no profile, a
    profile with no pin (``FLX-3109``) and a profile whose digest is not the
    pinned one (``FLX-3110``), before anything is written.

    *kernel* is the ``kernelVersion`` the new plan takes (``fluxsim plan --kernel``):
    one of :data:`flux_engine.core.kernels.KERNELS`, or ``None`` for the default.
    Every later stage reads it back from the plan. One this engine does not
    implement is ``FLX-2119``.

    *max_rows* is the rows a periodic emit may be estimated to derive (``fluxsim plan
    --max-rows``), at most :data:`~flux_engine.core.sizing.MOST_ROWS`; ``None`` is
    :data:`~flux_engine.generator.periods.DEFAULT_PERIODIC_ROWS`. Another budget is recorded as
    ``maxRows``, which ``apply --run-id`` reads back.
    """
    return timed(
        timings,
        lambda: plan_with_result(
            bundle_directory,
            simulation_id=simulation_id,
            state_dir=state_dir,
            zone=zone,
            jobs=jobs,
            strict_coverage=strict_coverage,
            profile=profile,
            kernel=kernel,
            stage=stage,
            scale=scale,
            allow_generators=allow_generators,
            stage_required=not stage,
            reuse_committed=True,
            max_rows=max_rows,
        )[0],
    )


def _check_against_profile(result: "_pipeline.PipelineResult", pinned: Optional[Any]) -> None:
    """Read the overlay and the register's citations in the profile ``plan`` is handed (``FLX-3105``, ``FLX-3113``).

    Checked here, the one stage that is always handed the profile a run is planned against;
    ``validate --profile`` runs the same check (:func:`check_simulation_against_profile`).
    """
    check_simulation_against_profile(
        result.cast.simulation, result.cast.simulation_id, result.parameters, pinned
    )


def check_simulation_against_profile(
    simulation: Mapping[str, Any],
    where: str,
    parameters: Optional[Any],
    pinned: Optional[Any],
) -> None:
    """Refuse a Simulation that the profile it pins does not describe (``FLX-3105``, ``FLX-3113``).

    The Simulation's overlay has to make a profile the reader accepts, so an overlay that names a
    table or column the profile lacks is refused. A number said to come from the profile has to
    cite a statistic the pinned profile publishes, and one said to come from the overlay a
    statistic of the overlaid profile; a number the overlay changed is not cited as the profile's.
    A table or column of the contract that the profile does not describe is refused here, by the
    citation that reaches it. *parameters* is the Simulation's parameter register, or ``None``
    when it declares none; *pinned* is the profile it pins, or ``None`` when it has none.
    """
    if pinned is None:
        return
    overlay = declared_overlay(simulation, where=where)
    overlaid = None
    if overlay is not None:
        profile, overlaid = overlaid_profile(pinned.document, overlay, code=FLX_3105)
        check_restated_means(profile, overlay, code=FLX_3402)
    if parameters is not None:
        check_profile_citations(
            parameters,
            pinned.document,
            where=where,
            overlay=(overlay.name, overlaid) if overlay is not None and overlaid else None,
        )


def _profile_grade(result: "_pipeline.PipelineResult", pinned: Optional[Any]) -> Pattern:
    """The grade of the run (ADR 0020): the weakest of the pinned profile's and every declared
    generator plugin's (ADR 0032).

    Refuses (``FLX-4005``) a run in which a labelled column's floor is above it. Governance refuses
    here beside sovereignty, before anything is written. The grade is recorded in the plan when the
    Simulation pins a profile or declares a plugin, so ``apply`` and ``ship`` can enforce the same
    minimum.
    """
    from flux_engine.evidence.grades import grade_profile
    from flux_engine.stages._grades import generator_grades, require_minimum_grade

    graded = grade_profile(
        pinned.document if pinned is not None else None, pinned=pinned is not None
    )
    grade = weakest(
        [
            graded.pattern,
            *generator_grades(result.cast.simulation, where=result.cast.simulation_id),
        ]
    )
    require_minimum_grade(
        grade,
        [outcome.resolved for outcome in result.emits],
        result.cast.simulation,
        where=result.cast.simulation_id,
        accounting=graded.accounting,
    )
    return grade


def _period_note_fields(notes: Sequence[str]) -> Dict[str, Any]:
    """The plan result's ``period_end_notes`` entry, empty when there are no notes."""
    return {"period_end_notes": list(notes)} if notes else {}


def plan_with_result(
    bundle_directory: Union[str, Path],
    *,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    jobs: int = 1,
    strict_coverage: bool = False,
    profile: Union[str, Path, None] = None,
    kernel: Optional[str] = None,
    stage: bool = False,
    scale: Any = None,
    shard: Optional[Tuple[int, int]] = None,
    allow_generators: Sequence[str] = (),
    stage_required: bool = False,
    reuse_committed: bool = False,
    replicate: Optional[Replicate] = None,
    common_draws_of: Optional[Path] = None,
    most_rows: Optional[int] = None,
    max_rows: Optional[int] = None,
) -> Tuple[StageResult, Optional["_pipeline.PipelineResult"]]:
    """:func:`plan`, plus the execution it planned from (``None`` when it failed).

    A plan *is* an execution -- the whole loop, in memory, exactly as
    ``apply`` will run it. A caller that plans implicitly and is about to
    execute the very same bundle bytes a millisecond later can take that
    execution rather than paying to materialise 25 000 subjects a second
    time. The population is the expensive half of the loop, and nothing in
    it depends on the plan having been written first.

    A plan that declares a generator plugin is staged always (ADR 0032 section 6), because its
    bytes are stored and never generated again. *stage_required* is ``fluxsim plan``'s side of
    that: when the caller asked for no stage, a Simulation that declares a plugin is refused
    (``FLX-3122``) before any plugin code runs. ``fluxsim run`` and an ``apply`` that takes a
    fresh plan hold the execution in memory and write it once, so they leave it ``False``.

    *reuse_committed* is those two callers' side of the same rule: when the plan's run id already
    holds a committed run of a bundle that declares a plugin, no plugin is asked for rows and
    nothing is written. The result is the plan's ``run_id`` with ``reused`` set and no execution
    (``None``), so the caller that goes on to apply the run keeps the committed bytes.

    *common_draws_of* is a bundle directory whose ``H`` seeds the rows in place of this bundle's
    own (:func:`flux_engine.stages.pipeline.execute`); only ``calibrate --fit`` passes it, and
    the run it plans is never replayed.

    *most_rows* is the ceiling on the rows the plans hold, passed to the pipeline. A plan above it
    raises :class:`~flux_engine.stages.pipeline.PlannedRowsAboveCeiling`, which is not a
    :class:`~flux_engine.core.errors.FluxError` and so is not turned into a result here.

    *max_rows* is :func:`plan`'s, the periodic rows' budget, a different bound from *most_rows*.
    """
    directory = Path(bundle_directory)
    state = Path(state_dir) if state_dir is not None else Path(".flux")

    def planned_record(
        result: "_pipeline.PipelineResult",
    ) -> Tuple[Any, Dict[str, Any], Any]:
        pinned = require_profile(
            profile_reference(result.cast.simulation, where=result.cast.simulation_id),
            Path(profile) if profile is not None else None,
            where=result.cast.simulation_id,
        )
        # Profile overlay and parameter register: the patch has to make a profile the reader
        # accepts, and a number said to come from the profile has to cite a statistic the pinned
        # profile publishes (see :func:`_check_against_profile`). The grade below reads the pinned
        # profile, never the overlaid one (ADR 0030 section 7).
        _check_against_profile(result, pinned)
        # Governance: a column's sensitivity sets the minimum privacy grade of the run that
        # generates it (ADR 0020, flux_engine.governance.privacy), refused here beside sovereignty.
        grade = _profile_grade(result, pinned)
        document = build_plan_document(result)
        if pinned is not None or declared_generators(
            result.cast.simulation, where=result.cast.simulation_id
        ):
            document[GRADE_KEY] = grade.value
        digested = build_plan_digest(document)
        document["planDigest"] = digested.ref.value
        return pinned, document, digested

    def holds_committed_run(result: "_pipeline.PipelineResult") -> bool:
        _, _, found = planned_record(result)
        layout_of = RunLayout.for_state_dir(state, run_id_for(found.ref.value))
        return is_regular_file(layout_of.receipt_path)

    try:
        result = _pipeline.execute(
            directory,
            simulation_id=simulation_id,
            zone=zone,
            jobs=jobs,
            strict_coverage=strict_coverage,
            kernel=None if kernel is None else kernel_asked_for(kernel),
            scale=optional_scale(scale),
            shard=shard,
            profile=profile,
            allow_generators=allow_generators,
            state_dir=state,
            require_staged=stage_required,
            stored_probe=holds_committed_run if reuse_committed else None,
            replicate=replicate,
            common_draws_of=common_draws_of,
            most_rows=most_rows,
            era=_pipeline.PlanEra(periodic_rows=_periodic_budget(max_rows), hold_ranges=True),
        )
        pinned, document, digested = planned_record(result)
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

    lap("profile")
    register = _register_summary(result)
    unchecked = consumers_unchecked(result.cast.simulation, where=result.cast.simulation_id)
    document["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if result.deferred:
        # A committed run of this plan holds its plugin bytes and no plugin was asked for rows:
        # nothing is written, so the run directory is as it was.
        return (
            StageResult(
                stage="plan",
                exit_code=0,
                _artifacts={
                    "run_id": run_id_for(digested.ref.value),
                    "run_dir": str(
                        RunLayout.for_state_dir(state, run_id_for(digested.ref.value)).run_dir
                    ),
                    "staged": False,
                    "reused": True,
                    "plan_digest": digested.ref.value,
                    "input_digest": result.input_digest.ref.value,
                },
                stdout=(
                    f"run {run_id_for(digested.ref.value)} keeps its stored plugin output: it is "
                    "never generated again, and a new sample needs a new run, for example "
                    "another seed"
                ),
            ),
            None,
        )

    run_id = run_id_for(digested.ref.value)
    layout = RunLayout.for_state_dir(state, run_id).ensure()
    write_text_atomic(layout.plan_path, render_document(document))
    # Written whole, never appended to: the bytes are a pure function of the
    # path set, so a run killed mid-write is repaired by the next plan rather
    # than leaving a file whose digest can never match again.
    write_bytes_atomic(layout.seed_paths_path, result.seed_tree.seed_paths_bytes())
    lap("plan record")
    staged_path: Optional[Path] = None
    # A run directory that was applied holds its run: a stage put beside it would be
    # packed with it by `ship`, and `apply` has nothing left to commit.
    if stage and not layout.receipt_path.exists():
        try:
            staged_path = stage_execution(
                layout,
                directory,
                document,
                result,
                column_provenance_text(result),
                parameter_register_text(result),
                simulation_id=simulation_id,
                zone=zone,
            )
            lap("stage")
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
    else:
        # No stage asked for, or none to make: an older stage of this plan is not left for an
        # apply to commit.
        discard_stage(layout)

    period_notes = _pipeline.advice_notes(result)
    return (
        StageResult(
            stage="plan",
            exit_code=0,
            _artifacts={
                "run_id": run_id,
                "run_dir": str(layout.run_dir),
                "plan_path": str(layout.plan_path),
                "seed_paths_path": str(layout.seed_paths_path),
                "staged": staged_path is not None,
                **({"stage_path": str(staged_path)} if staged_path is not None else {}),
                "plan_digest": digested.ref.value,
                "input_digest": result.input_digest.ref.value,
                "seed_tree_digest": document["seedTreeDigest"],
                "files_not_hashed": list(result.files_not_hashed),
                "plan_document": document,
                # How each column was produced, per emit. Reported here and
                # recorded by `apply` beside the run; kept out of plan.json,
                # which would move every planDigest for a fact no digest needs.
                "coverage": {
                    outcome.resolved.expose_id: outcome.coverage.as_document()
                    for outcome in result.emits
                    if outcome.coverage is not None
                },
                # Declared and not generated (flux_engine.seam.declared): reported here
                # only. Nothing the plan binds or hashes carries it, so no bundle's
                # plan.json or planDigest moves; `ship` works it out again from the bundle.
                **(
                    {"declared_not_generated": [r.as_document() for r in _not_generated_of(result)]}
                    if _not_generated_of(result)
                    else {}
                ),
                **({"profile_digest": pinned.digest.value} if pinned is not None else {}),
                **({"parameter_register": register.as_document()} if register is not None else {}),
                **({"consumer_queries_checked": False} if unchecked else {}),
                **_period_note_fields(period_notes),
            },
            stderr="\n".join(period_notes),
            stdout="\n".join(
                [
                    _plan_stdout(
                        layout,
                        run_id,
                        digested.ref.value,
                        result.files_not_hashed,
                        tuple(o.coverage for o in result.emits if o.coverage is not None),
                        _not_generated_of(result),
                    ),
                    *(
                        result.scale_floors[name].note(result.scale)
                        for name in sorted(result.scale_floors)
                    ),
                    *([_staged_line(result, layout)] if staged_path is not None else []),
                    # Parameter register: the shares, for a Simulation that declares a register.
                    *([render_register_line(register)] if register is not None else []),
                    *([unchecked_line()] if unchecked else []),
                ]
            ),
        ),
        result,
    )


def _staged_line(result: "_pipeline.PipelineResult", layout: RunLayout) -> str:
    """The line that says the rows are kept, and which command commits them."""
    rows = sum(outcome.result.rows for outcome in result.emits)
    return (
        f"staged {rows} rows in {len(result.emits)} emits under {layout.run_dir / STAGE_DIRNAME} "
        f"(apply --run-id {layout.run_id} commits them without generating; "
        "--no-stage keeps only the plan)"
    )


def _register_summary(result: "_pipeline.PipelineResult") -> Optional[RegisterSummary]:
    """What the plan reports of its register: read back from the very text it hashed."""
    text = parameter_register_text(result)
    return parse_register(text) if text is not None else None


def _plan_stdout(
    layout: RunLayout,
    run_id: str,
    plan_digest: str,
    files_not_hashed: Sequence[str],
    coverage: Sequence[ExposeCoverage] = (),
    not_generated: Sequence[NotGenerated] = (),
) -> str:
    """Where the plan landed, which files it did not hash, and which columns defaulted.

    The second line exists because a reader has to be able to tell that a
    file in the bundle directory was left out of ``inputDigest``, and to
    tell which digest does cover it. Naming it in the plan's output is
    the difference between a rule and a surprise.

    The list stays out of :func:`build_plan_document`. A digest that
    folded in the names of the files it did not hash would depend on
    those files again, which is the defect this reporting exists to make
    visible.

    A column that fell back to a type default is named, per emit, with what
    it fell back to: the plan's own output is where an author is looking
    when a column they expected to be declared is not. Nothing is printed
    when every column was produced by a declaration, a copy or a derivation.

    A declaration the engine accepts and does not generate (a ``check`` that determines no
    column; a relationship's ``sharedColumns`` and a column's ``sharedAttribute``, which kernel
    0.2.0 copies when the Simulation declares ``copies: true``, unless the column is a key or a
    foreign key)
    is named too, per expose, after the columns. Nothing is printed when the bundle declares none.
    """
    lines = [f"plan written to {layout.plan_path} (runId={run_id}, planDigest={plan_digest})"]
    if files_not_hashed:
        listed = ", ".join(files_not_hashed)
        lines.append(
            f"not hashed into inputDigest: {listed} "
            "(written by a pack, not read by a run; covered by the evidence "
            "archive's own manifest)"
        )
    reported = False
    for expose in coverage:
        defaulted = expose.defaulted
        if not defaulted:
            continue
        lines.append(
            f"coverage: {expose.expose_id}: {len(defaulted)} of {len(expose.columns)} columns "
            "fell back to a type default"
        )
        lines.extend(
            f"  {printable(row.column)} ({row.type_name}): {row.detail}" for row in defaulted
        )
        reported = True
    if reported:
        lines.append(
            "--strict-coverage refuses the run, and so does strictCoverage in the bundle; "
            "fluxsim explain FLX-3304 says how to cover a column"
        )
    if not_generated:
        lines.extend(render_not_generated(not_generated))
        lines.append(
            "--strict-coverage refuses the run, and so does strictCoverage in the bundle; "
            "fluxsim explain FLX-3305 says what to do about a declaration"
        )
    return "\n".join(lines)


def render_document(document: Dict[str, Any]) -> str:
    """*document* as ``plan`` and ``apply`` write it: sorted keys, two-space indent, a final newline."""
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
    try:
        data = read_bytes_nofollow(path)
    except OSError as error:  # missing, a directory, a link, a pipe, unreadable
        raise SeedPathsMoved(path=path, recorded=recorded, recomputed=None) from error
    recomputed = seed_paths_digest(data)
    if recomputed != recorded:
        raise SeedPathsMoved(path=path, recorded=recorded, recomputed=recomputed)
    return parse_seed_paths(data)


_Bound = TypeVar("_Bound")


def _read_bound(
    path: Path,
    expected: str,
    digest: Callable[[bytes], DigestRef],
    moved: Callable[..., FluxError],
    read: Callable[[bytes], _Bound],
) -> _Bound:
    """A sidecar the plan binds, checked against the plan's digest of it, then read.

    The one reading of the column record and the parameter register: the file is
    read from the descriptor that was opened (:func:`flux_engine.safeio.read_bytes_nofollow`) and
    digested as its bytes are, and *moved* builds the refusal for a file that is
    missing, not a regular file, or no longer hashes to *expected*. *read* turns
    the bytes that hashed into what the caller carries; bytes it cannot read
    (``ValueError``) are refused the same way: they are the ones the plan vouches
    for, written by an engine that spoke another format.
    """
    shown = printable(expected)  # the plan's own text, which a diagnostic must not pass on raw
    try:
        data = read_bytes_nofollow(path)
    except OSError as error:  # missing, a directory, a link, a pipe, unreadable
        raise moved(path=path, recorded=shown, recomputed=None) from error
    recomputed = digest(data).value
    if recomputed != expected:
        raise moved(path=path, recorded=shown, recomputed=recomputed)
    try:
        return read(data)
    except (ValueError, RecursionError) as error:
        raise moved(path=path, recorded=shown, recomputed=None) from error


def read_column_provenance(
    layout: RunLayout, plan_document: Mapping[str, Any]
) -> Optional[BoundProvenance]:
    """Read back the column provenance record *plan_document* bound, or ``None`` when it binds none.

    The record is only ever read through this function, so what a report says of
    how a column was produced is what the plan's execution recorded and nothing
    a later edit of the file put there. ``None`` is a plan written before the plan
    carried a digest: there is nothing to check a file against, so nothing is
    believed of it. A record that is missing, does not hash to the plan's
    ``columnProvenanceDigest``, or is not one this build can read is ``FLX-2115``.
    The bytes that hashed come back with what they say
    (:class:`~flux_engine.evidence.provenance.BoundProvenance`), so the pack can
    carry exactly them.
    """
    recorded = plan_document.get(PROVENANCE_DIGEST_KEY)
    if recorded is None:
        return None
    return _read_bound(
        layout.column_provenance_path,
        str(recorded),
        provenance_digest,
        ColumnProvenanceMoved,
        lambda data: BoundProvenance(coverages=parse_provenance(data.decode("utf-8")), data=data),
    )


def read_parameter_register(
    layout: RunLayout, plan_document: Mapping[str, Any]
) -> Optional[BoundRegister]:
    """Read back the parameter register *plan_document* binds, or ``None`` when it binds none.

    The register is only ever read through this function, as the column record is
    through :func:`read_column_provenance`: what a report says of the sources of
    the run's numbers is what the plan's execution recorded. ``None`` is a plan
    whose Simulation declares no register; a ``parameter-register.json`` in its run
    directory all the same is ``FLX-2116``, because a pack would carry it as if the
    plan vouched for it. A record that is missing, does not hash to the plan's
    ``parameterRegisterDigest``, or is not one this build can read is ``FLX-2116``.
    """
    path = layout.parameter_register_path
    recorded = plan_document.get(REGISTER_DIGEST_KEY)
    if recorded is None:
        if path.exists() or path.is_symlink():
            raise ParameterRegisterMoved(path=path, recorded=None, recomputed=None, stray=True)
        return None
    return _read_bound(
        path,
        str(recorded),
        register_digest,
        ParameterRegisterMoved,
        lambda data: BoundRegister(summary=parse_register(data.decode("utf-8")), data=data),
    )


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
