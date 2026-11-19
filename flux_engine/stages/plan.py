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
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, TypeVar, Union

from flux_engine.api.stages import StageResult
from flux_engine.core import kernels as _kernels
from flux_engine.core.digest import DigestRef, build_plan_digest
from flux_engine.core.errors import FluxError
from flux_engine.core.kernels import Kernel
from flux_engine.core.seedtree import (
    SeedPath,
    SeedTree,
    SeedTreeRoot,
    parse_seed_paths,
    seed_paths_digest,
)
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
from flux_engine.lineage.emitter import dataset_for_expose
from flux_engine.safeio import read_bytes_nofollow, write_bytes_atomic, write_text_atomic
from flux_engine.seam.coverage import ExposeCoverage, printable
from flux_engine.seam.parameters import check_profile_citations
from flux_engine.seam.profile import require_profile
from flux_engine.seam.resolve import profile_reference, scorecard_declaration
from flux_engine.seam.scorecard import thresholds_document
from flux_engine.stages import pipeline as _pipeline
from flux_engine.stages.errors import (
    ColumnProvenanceMoved,
    KernelNotImplemented,
    ParameterRegisterMoved,
    SeedPathsMoved,
)
from flux_engine.stages.layout import RunLayout, run_id_for

__all__ = [
    "PROFILE_PIN_KEY",
    "SCORECARD_PLAN_KEY",
    "build_plan_document",
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


def _fidelity_summary(outcome: "_pipeline.EmitOutcome") -> Optional[Dict[str, Any]]:
    """A JSON-safe summary of an emit's generator plan, or ``None`` when undeclared."""
    if outcome.plan is None:
        return None
    return {
        "generatorDigest": outcome.plan.digest().value,
        "rows": outcome.plan.rows,
        "columns": [item.column.name for item in outcome.plan.columns],
    }


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

#: ``plan.json``'s key for the thresholds the Simulation declares on its
#: scorecard (docs/spec/SCORECARD-SPEC.md). Present only when it declares some.
SCORECARD_PLAN_KEY = "scorecard"


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
    return entries


def build_plan_document(result: "_pipeline.PipelineResult") -> Dict[str, Any]:
    """The document ``plan.json`` holds, before ``planDigest`` is folded in."""
    return {
        **_profile_entries(result),
        **_register_entries(result),
        # The kernel the execution ran, which is what every later stage reads
        # back (flux_engine.stages.apply.plan_kernel).
        "kernelVersion": result.kernel.version,
        "seedTreeVersion": result.kernel.seed_tree_version,
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
        "emits": {
            outcome.resolved.expose_id: {
                "H": outcome.digests.h.value,
                "G": outcome.digests.g.value,
                "productRef": outcome.resolved.product_ref,
                "declaresFidelity": outcome.resolved.fidelity is not None,
                "generator": _fidelity_summary(outcome),
                # SEAM-FIDELITY-SPEC §3: "every ε" is written to plan.json.
                # The key is the one the block is declared under, and an
                # emit that declares none gets no key, so its plan keeps
                # the bytes and the planDigest it had.
                **(
                    {"fidelity": outcome.compiled_fidelity.plan_document()}
                    if outcome.compiled_fidelity is not None
                    else {}
                ),
            }
            for outcome in result.emits
        },
        "planDigest": None,  # self-referential -- masked, filled by build_plan_digest
        "generated_at": None,  # masked -- see NON_DIGEST_FIELDS
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
) -> StageResult:
    """Execute the loop once and write ``<state_dir>/runs/<runId>/plan.json``.

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
    """
    return plan_with_result(
        bundle_directory,
        simulation_id=simulation_id,
        state_dir=state_dir,
        zone=zone,
        jobs=jobs,
        strict_coverage=strict_coverage,
        profile=profile,
        kernel=kernel,
    )[0]


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
        result = _pipeline.execute(
            directory,
            simulation_id=simulation_id,
            zone=zone,
            jobs=jobs,
            strict_coverage=strict_coverage,
            kernel=None if kernel is None else kernel_asked_for(kernel),
        )
        pinned = require_profile(
            profile_reference(result.cast.simulation, where=result.cast.simulation_id),
            Path(profile) if profile is not None else None,
            where=result.cast.simulation_id,
        )
        # Parameter register: a number said to come from the profile has to cite a statistic
        # the pinned profile publishes. Checked here, the one stage that is
        # always handed the profile a run is planned against.
        if result.parameters is not None and pinned is not None:
            check_profile_citations(
                result.parameters, pinned.document, where=result.cast.simulation_id
            )
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
    register = _register_summary(result)
    digested = build_plan_digest(document)
    document["planDigest"] = digested.ref.value
    document["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    run_id = run_id_for(digested.ref.value)
    layout = RunLayout.for_state_dir(state, run_id).ensure()
    write_text_atomic(layout.plan_path, render_document(document))
    # Written whole, never appended to: the bytes are a pure function of the
    # path set, so a run killed mid-write is repaired by the next plan rather
    # than leaving a file whose digest can never match again.
    write_bytes_atomic(layout.seed_paths_path, result.seed_tree.seed_paths_bytes())

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
                # How each column was produced, per emit. Reported here and
                # recorded by `apply` beside the run; kept out of plan.json,
                # which would move every planDigest for a fact no digest needs.
                "coverage": {
                    outcome.resolved.expose_id: outcome.coverage.as_document()
                    for outcome in result.emits
                    if outcome.coverage is not None
                },
                **({"profile_digest": pinned.digest.value} if pinned is not None else {}),
                **({"parameter_register": register.as_document()} if register is not None else {}),
            },
            stdout="\n".join(
                [
                    _plan_stdout(
                        layout,
                        run_id,
                        digested.ref.value,
                        result.files_not_hashed,
                        tuple(o.coverage for o in result.emits if o.coverage is not None),
                    ),
                    # Parameter register: the shares, for a Simulation that declares a register.
                    *([render_register_line(register)] if register is not None else []),
                ]
            ),
        ),
        result,
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
