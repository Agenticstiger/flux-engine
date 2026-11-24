# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim calibrate --fit``: declared parameters are fitted to the calibration queries' rates (ADR 0028 section 8).

The work, in this order, and every refusal that needs no run is made before the first run:

1. The flags raise the command's ceilings (:func:`fit_ceilings`) and the output names are checked
   (:func:`check_output`).
2. The bundle's ``consumers`` block is read against the engine's own ceilings, as every stage reads it,
   so a bundle that declares more than 64 evaluations is refused here as everywhere (``FLX-3616``). The
   fit's budget is the bundle's own ``evaluations`` when it declares one, else the ceiling in force.
3. The pinned profile is read, and the calibration queries are checked against the bundle's schema
   (:func:`flux_engine.consumers.check.check_registry`). The fit reads the pinned profile only.
4. Each declared parameter is read against the parameter register (:mod:`flux_engine.calibrate.parameters`).
5. One query process is started for the whole fit. Each evaluation writes the candidate values, each
   rounded to its ``step``, into a copy of the bundle's files, plans and applies that copy at the
   calibration's ``scale`` under the state directory, loads the run's tables into the process, counts the
   calibration queries, and removes the copy and its run. Every evaluation seeds its rows from the
   unpatched bundle's per-emit ``H`` (common random numbers), so two candidates draw the same rows
   wherever the values that differ do not decide them. The objective is the sum of the squared gaps
   between each query's rate and its target.
6. The best candidate becomes the calibration patch, ``DIR.patch.json`` (:mod:`flux_engine.calibrate.patch`),
   and its digest is taken. The search evaluates the start point first and keeps it unless a candidate
   beats it; a fit that moves no value writes no bundle. A refusal made after this step (an unreadable held-out profile, one naming a calibration
   query) removes the patch again; a held-out query that misses is a report, and the patch stays.
7. Only then is the held-out profile opened (``--held-out``; the profile may not name a calibration
   query). It is graded under ADR 0020's rule 2 beside the pinned profile, the calibrated values are run
   at the same scale, and the held-out queries are counted and set against their tolerances
   (:mod:`flux_engine.calibrate.gate`). The held-out profile's name is checked before the fit and its
   queries are not read until the patch exists.
   A bundle that declares no held-out query is fitted without this step, and the outcome says so
   (:attr:`FitOutcome.held_out_note`); a bundle that declares one refuses a fit without ``--held-out``
   (:class:`HeldOutNeeded`) before anything runs.
8. When the check holds, the calibrated bundle is written at ``DIR`` (:mod:`flux_engine.calibrate.bundle`).
   Otherwise the patch and the report stand, and each held-out query that missed is named.

The SQL libraries and cma are imported when the fit needs them, never when this module is.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from flux_engine.calibrate.bundle import CalibratedBundle, write_calibrated_bundle
from flux_engine.calibrate.errors import (
    FLX_3704,
    FLX_3707,
    FLX_3708,
    FLX_3709,
    FLX_3710,
    FLX_7101,
)
from flux_engine.calibrate.gate import GateReport, judge
from flux_engine.calibrate.objective import score, stated_queries
from flux_engine.calibrate.parameters import FitParameter, exact_text, resolve_parameters
from flux_engine.calibrate.patch import (
    METHOD_ID,
    Located,
    Operation,
    locate,
    patch_digest,
    render_patch,
    splice,
)
from flux_engine.calibrate.search import INSTALL_COMMAND, CmaCalibrator, Evaluation, FitResult
from flux_engine.concordance.queries import QueryRow
from flux_engine.consumers.outcome import QueryTarget, profile_targets, query_rows, require_apart
from flux_engine.core.errors import FluxError
from flux_engine.safeio import read_text_nofollow, write_text_atomic
from flux_engine.seam.consumers import Ceilings, ConsumerRegistry, raised_ceilings
from flux_engine.seam.parameters import ParameterRegister
from flux_engine.seam.profile import FLX_3109, PinnedProfile, load_profile
from flux_engine.sinks._optional import is_available
from flux_stats.spelling import render_exact

__all__ = [
    "FitOutcome",
    "FitRequest",
    "HeldOutNeeded",
    "check_output",
    "fit_ceilings",
    "patch_path_of",
    "run_fit",
]

#: The libraries a fit needs: the ``[calibrate]`` extra.
_LIBRARIES = ("sqlglot", "duckdb", "pyarrow", "cma")


#: What the output says when the bundle declares no held-out query.
NO_HELD_OUT_QUERY = "no held-out check was made: the bundle declares no held-out query"
NO_HELD_OUT_QUERY_GIVEN = (
    "no held-out check was made: the held-out profile was not read, "
    "since the bundle declares no held-out query"
)


@dataclass(frozen=True)
class FitRequest:
    """What ``fluxsim calibrate --fit`` was asked: the bundle, the pinned profile and where to write."""

    bundle: Path
    profile: Path
    out: Path
    held_out: Optional[Path] = None
    state_dir: Optional[Path] = None
    max_evaluations: Optional[int] = None
    max_query_seconds: Optional[int] = None
    max_query_memory: Optional[int] = None


@dataclass(frozen=True)
class FitOutcome:
    """The fit's result: the patch as written, the held-out check, and the bundle when one was written."""

    patch_path: Path
    patch_text: str
    patch_digest: str
    patch: Mapping[str, Any]
    operations: Tuple[Operation, ...]
    result: FitResult
    parameters: Tuple[FitParameter, ...]
    registry: ConsumerRegistry
    pinned: PinnedProfile
    simulation_id: str
    #: The held-out check; ``None`` when no held-out profile was given, and then nothing is calibrated.
    gate: Optional[GateReport] = None
    #: The calibrated bundle; ``None`` unless the held-out check held.
    bundle: Optional[CalibratedBundle] = None
    #: Why no held-out check was made when the bundle declares no held-out query; else ``None``.
    held_out_note: Optional[str] = None

    @property
    def calibrated(self) -> bool:
        """Whether the bundle was called calibrated: the held-out check held and the bundle was written."""
        return self.bundle is not None


def patch_path_of(out: Path) -> Path:
    """Where the patch of a fit that writes *out* is written: ``DIR.patch.json``."""
    return out.with_name(out.name + ".patch.json")


def fit_ceilings(
    seconds: Optional[int], memory: Optional[int], evaluations: Optional[int]
) -> Ceilings:
    """The ceilings the flags ask for: ``FLX-3700`` for a query flag, ``FLX-3704`` for the evaluations."""
    from flux_engine.stages._queries import query_ceilings

    base = query_ceilings(seconds, memory)
    if evaluations is None:
        return base
    try:
        raised = raised_ceilings(evaluations=evaluations)
    except ValueError as error:
        raise FluxError(FLX_3704, f"--max-evaluations: {error}") from error
    return replace(base, evaluations=raised.evaluations)


def check_output(out: Path, bundle: Path) -> None:
    """``FLX-3710`` unless the patch and the bundle *out* names can be written: both free, outside *bundle*."""
    patch = patch_path_of(out)
    for path in (out, patch):
        if path.exists() or path.is_symlink():
            raise FluxError(FLX_3710, f"{path} already exists; nothing was run")
    if not out.parent.is_dir():
        raise FluxError(FLX_3710, f"{out.parent} is not a directory; nothing was run")
    root = bundle.resolve()
    place = out.parent.resolve() / out.name
    if place == root or root in place.parents:
        raise FluxError(FLX_3710, f"{out} is the bundle or inside it; nothing was run")


def _require_libraries() -> None:
    missing = [name for name in _LIBRARIES if not is_available(name)]
    if missing:
        raise FluxError(
            FLX_3708, f"the fit needs {', '.join(missing)} ({INSTALL_COMMAND}); nothing was run"
        )


@dataclass(frozen=True)
class _Counted:
    """A set of queries counted on a run together: their requests, registry and canonical digests."""

    requests: Tuple[Any, ...]
    registry: ConsumerRegistry
    digests: Mapping[str, str]


@dataclass
class _Evaluator:
    """Runs one candidate: copy, plan and apply at the scale, count, remove. One process serves them all."""

    bundle_files: Mapping[str, bytes]
    parameters: Tuple[FitParameter, ...]
    #: Where each parameter is written, by file name and pointer.
    locations: Mapping[str, Mapping[str, Located]]
    work: Path
    profile: Path
    scale: str
    plan: Any
    calibration: _Counted
    held_out: _Counted
    targets: Mapping[str, Tuple[QueryTarget, ...]]
    stated: FrozenSet[str]
    runner: Any
    #: The unpatched bundle: every evaluation seeds its rows from its ``H`` (common random numbers).
    draws_of: Path
    count: int = 0

    def __call__(self, values: Tuple[Fraction, ...]) -> Evaluation:
        rows = self._rows(values, self.calibration, self.targets, None)
        return score(rows, self.stated)

    def held_out_rows(
        self, values: Tuple[Fraction, ...], targets: Mapping[str, Tuple[QueryTarget, ...]]
    ) -> Tuple[QueryRow, ...]:
        """The held-out queries counted on a run of the bundle with *values* written, at the scale."""
        return self._rows(values, self.held_out, None, targets)

    def _rows(
        self,
        values: Tuple[Fraction, ...],
        counted: _Counted,
        pinned: Optional[Mapping[str, Tuple[QueryTarget, ...]]],
        held_out: Optional[Mapping[str, Tuple[QueryTarget, ...]]],
    ) -> Tuple[QueryRow, ...]:
        self.count += 1
        directory = self.work / f"evaluation-{self.count}"
        try:
            return self._count(directory, values, counted, pinned, held_out)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def _count(
        self,
        directory: Path,
        values: Tuple[Fraction, ...],
        counted: _Counted,
        pinned: Optional[Mapping[str, Tuple[QueryTarget, ...]]],
        held_out: Optional[Mapping[str, Tuple[QueryTarget, ...]]],
    ) -> Tuple[QueryRow, ...]:
        from flux_engine.stages._queries import run_tables
        from flux_engine.stages.layout import RunLayout
        from flux_engine.stages.run import run_on_common_draws

        bundle, state = directory / "bundle", directory / "state"
        bundle.mkdir(parents=True)
        self._write_candidate(bundle, values)
        applied = run_on_common_draws(
            bundle,
            common_draws_of=self.draws_of,
            state_dir=state,
            profile=self.profile,
            scale=self.scale,
        )
        if not applied.success:
            code = f" ({applied.error_code})" if applied.error_code else ""
            detail = (applied.stderr or "the run failed").strip().splitlines()[0]
            raise FluxError(
                FLX_7101,
                f"evaluation {self.count} was refused{code}: {detail}; nothing was written",
            )
        layout = RunLayout.for_state_dir(state, applied.artifacts["plan"]["run_id"])
        self.runner.load(run_tables(self.plan, layout, _receipt(layout)))
        batch = self.runner.count(counted.requests)
        return query_rows(counted.registry, counted.digests, list(batch.queries), pinned, held_out)

    def _write_candidate(self, bundle: Path, values: Tuple[Fraction, ...]) -> None:
        chosen = {
            parameter.key: exact_text(value)
            for parameter, value in zip(self.parameters, values, strict=True)
        }
        for name, data in self.bundle_files.items():
            where = self.locations.get(name)
            if where:
                edits = [
                    (where[parameter.pointer], chosen[parameter.key])
                    for parameter in self.parameters
                    if parameter.document == name
                ]
                data = splice(data.decode("utf-8"), edits).encode("utf-8")
            (bundle / name).write_bytes(data)


def _receipt(layout: Any) -> Mapping[str, Any]:
    document = json.loads(read_text_nofollow(layout.receipt_path))
    if not isinstance(document, dict):
        raise FluxError(FLX_7101, "the run's receipt is not a JSON object; nothing was written")
    return document


@dataclass(frozen=True)
class _Prepared:
    """What the fit settled before its first run."""

    loaded: Any
    cast: Any
    registry: ConsumerRegistry
    pinned: PinnedProfile
    plan: Any
    checked: Any
    parameters: Tuple[FitParameter, ...]
    ceilings: Ceilings
    evaluations: int
    seed: int
    targets: Mapping[str, Tuple[QueryTarget, ...]]
    calibration_ids: Tuple[str, ...]
    #: The calibration queries the pinned profile states a rate for.
    stated: FrozenSet[str]


def _register_of(loaded: Any, cast: Any) -> ParameterRegister:
    """The bundle's parameter register; the one it would have with no entries when it declares none."""
    from flux_engine.seam.parameters import build_parameter_register
    from flux_engine.seam.resolve import EXTENSION_NAMESPACE, FLX_3105
    from flux_engine.stages.pipeline import parameter_register_of

    register = parameter_register_of(loaded, cast)
    if register is not None:
        return register
    return build_parameter_register(
        simulation_id=cast.simulation_id,
        documents=dict(cast.documents),
        namespace=EXTENSION_NAMESPACE,
        entries=(),
        profile_digest=None,
        code=FLX_3105,
    )


def _prepare(request: FitRequest, ceilings: Ceilings) -> _Prepared:
    from flux_engine.consumers.check import SQLGLOT_VERSION, check_registry
    from flux_engine.runtime import resolve_cast
    from flux_engine.seam.resolve import declared_consumers, load_bundle, load_documents_exact
    from flux_engine.stages._queries import prepare_queries

    loaded = load_bundle(request.bundle)
    cast = resolve_cast(loaded.flux_documents)
    registry = declared_consumers(cast.simulation, where=cast.simulation_id)
    if registry is None or not registry.calibration.parameters:
        raise FluxError(
            FLX_3707,
            "the bundle declares no consumers calibration parameters to fit; nothing was run",
        )
    pinned = load_profile(request.profile)
    require_apart(registry, pinned.profile, None)
    targets = profile_targets(pinned.profile)
    calibration_ids = tuple(query.id for query in registry.queries if not query.held_out)
    stated = stated_queries((q for q in registry.queries if not q.held_out), targets)
    if not stated:
        raise FluxError(
            FLX_3707,
            "the pinned profile states the rate of no calibration query, so there is nothing "
            "to fit to; nothing was run",
        )
    if registry.compiled_sqlglot is not None and registry.compiled_sqlglot != SQLGLOT_VERSION:
        raise FluxError(
            FLX_3709,
            f"the bundle's queries were compiled with sqlglot {registry.compiled_sqlglot} and "
            f"{SQLGLOT_VERSION} is installed; nothing was run",
        )
    flux_files = {
        name: load_documents_exact(request.bundle / name) for name in loaded.flux_file_names
    }
    parameters = resolve_parameters(
        registry.calibration.parameters,
        register=_register_of(loaded, cast),
        flux_files=flux_files,
    )
    plan = prepare_queries(
        request.bundle,
        {"simulationId": cast.simulation_id},
        {},
        pinned=pinned,
        held_out=None,
        ceilings=ceilings,
    )
    assert plan is not None  # the registry is declared
    spec = cast.simulation.get("spec")
    seed = registry.calibration.seed
    if seed is None:
        seed = spec.get("seed", 0) if isinstance(spec, Mapping) else 0
    declared = registry.calibration
    return _Prepared(
        loaded=loaded,
        cast=cast,
        registry=registry,
        pinned=pinned,
        plan=plan,
        checked=check_registry(registry, plan.schema),
        parameters=parameters,
        ceilings=ceilings,
        evaluations=declared.evaluations if declared.evaluations_declared else ceilings.evaluations,
        seed=int(seed),
        targets=targets,
        calibration_ids=calibration_ids,
        stated=stated,
    )


def _locations(prepared: _Prepared, files: Mapping[str, bytes]) -> Dict[str, Dict[str, Located]]:
    """Where each parameter is written, once, in the bundle's own text."""
    found: Dict[str, Dict[str, Located]] = {}
    for name in sorted({parameter.document for parameter in prepared.parameters}):
        targets = [
            (parameter.pointer, parameter.tokens)
            for parameter in prepared.parameters
            if parameter.document == name
        ]
        found[name] = locate(files[name].decode("utf-8"), name, targets)
    return found


def _candidate_files(prepared: _Prepared) -> Dict[str, bytes]:
    """The files a candidate bundle holds: the FLUX and FLUID documents and the lockfile, as the bundle has them."""
    from flux_engine.seam.resolve import LOCK_FILE_NAME

    loaded = prepared.loaded
    names = [*loaded.flux_file_names, *loaded.fluid_file_names, LOCK_FILE_NAME]
    return {name: loaded.file_bytes[name] for name in names if name in loaded.file_bytes}


def patch_document(
    prepared: _Prepared,
    result: FitResult,
    operations: Sequence[Operation],
    versions: Mapping[str, str],
) -> Dict[str, Any]:
    """The calibration patch: its header and its operations."""
    return {
        "method": METHOD_ID,
        "versions": dict(versions),
        "seed": result.seed,
        "evaluations": result.evaluations,
        "scale": prepared.registry.calibration.declared_scale,
        "profile": prepared.pinned.digest.value,
        "gaps": [
            {"query": name, "gap": render_exact(gap) if gap is not None else None}
            for name, gap in result.evaluation.gaps
        ],
        "operations": [operation.as_document() for operation in operations],
    }


def _operations(
    parameters: Sequence[FitParameter], values: Sequence[Fraction]
) -> Tuple[Operation, ...]:
    """One ``replace`` for each value the fit moved, in the order of the parameters."""
    return tuple(
        Operation(parameter.document, parameter.pointer, exact_text(value))
        for parameter, value in zip(parameters, values, strict=True)
        if value != parameter.before
    )


def _counted(prepared: _Prepared, held_out: bool) -> _Counted:
    """The calibration queries, or the held-out ones, as requests, with the registry that holds only them."""
    from flux_engine.consumers.runner import CountRequest

    wanted = {query.id for query in prepared.registry.queries if query.held_out == held_out}
    queries = [query for query in prepared.checked.queries if query.id in wanted]
    return _Counted(
        requests=tuple(
            CountRequest(
                id=query.id,
                text=query.text,
                unit_text=query.unit_text,
                group_by=query.group_by,
                sqlglot_version=query.sqlglot_version,
            )
            for query in queries
        ),
        registry=replace(
            prepared.registry,
            queries=tuple(q for q in prepared.registry.queries if q.id in wanted),
        ),
        digests={query.id: query.digest for query in queries},
    )


def _grade_of(document: Mapping[str, Any]) -> str:
    """The grade of a profile document under ADR 0020's rule 2, as ``verify`` states it."""
    from flux_engine.evidence.grades import grade_profile

    return str(grade_profile(document, pinned=True).pattern.value)


class HeldOutNeeded(Exception):
    """The bundle declares a held-out query and the request names no held-out profile.

    A usage refusal, not a ``FLX`` code: the command reports it as it reports a missing option
    (exit status 2). Nothing was run.
    """


def _preflight_held_out(request: FitRequest, prepared: _Prepared) -> Optional[str]:
    """The refusals about the held-out profile that need no read of it; the note when it has no use.

    A bundle that declares no held-out query is fitted without a held-out check, and a held-out profile
    given with it is not read: the returned text says so. A bundle that declares one needs the profile.
    """
    if not any(query.held_out for query in prepared.registry.queries):
        return NO_HELD_OUT_QUERY_GIVEN if request.held_out is not None else NO_HELD_OUT_QUERY
    if request.held_out is None:
        raise HeldOutNeeded("--fit needs --held-out")
    if not request.held_out.is_file():
        raise FluxError(
            FLX_3109,
            f"the held-out profile at {request.held_out} could not be read: it is not a file; "
            "nothing was run",
        )
    return None


def _check_held_out(
    request: FitRequest,
    prepared: _Prepared,
    evaluator: _Evaluator,
    values: Tuple[Fraction, ...],
) -> GateReport:
    """Open the held-out profile, run the calibrated values and judge the held-out queries.

    Called only once the patch is written and its digest taken: the fit never saw this profile.
    """
    assert request.held_out is not None
    held = load_profile(request.held_out)
    require_apart(prepared.registry, prepared.pinned.profile, held.profile)
    targets = profile_targets(held.profile)
    stated = stated_queries((q for q in prepared.registry.queries if q.held_out), targets)
    judged, untargeted = judge(evaluator.held_out_rows(values, targets), stated)
    return GateReport(
        profile=held.digest.value,
        grade=_grade_of(held.document),
        pinned_grade=_grade_of(prepared.pinned.document),
        queries=judged,
        untargeted=untargeted,
    )


def _write_bundle(
    request: FitRequest,
    prepared: _Prepared,
    outcome_parts: Tuple[Sequence[Operation], str, Mapping[str, str]],
    gate: GateReport,
) -> CalibratedBundle:
    operations, digest, versions = outcome_parts
    return write_calibrated_bundle(
        request.bundle,
        request.out,
        parameters=prepared.parameters,
        operations=operations,
        queries=[name for name in prepared.calibration_ids if name in prepared.stated],
        patch=digest,
        targets=prepared.pinned.digest.value,
        held_out=gate.evidence(),
        versions=versions,
        profile_document=prepared.pinned.document,
    )


def run_fit(request: FitRequest) -> FitOutcome:
    """Fit the bundle's declared parameters, write the patch, check the held-out queries and, when they hold,
    write the calibrated bundle. See the module docstring."""
    from flux_engine.consumers.check import SQLGLOT_VERSION
    from flux_engine.consumers.runner import QueryRunner
    from flux_engine.stages._queries import effective_limits

    _require_libraries()
    ceilings = fit_ceilings(
        request.max_query_seconds, request.max_query_memory, request.max_evaluations
    )
    check_output(request.out, request.bundle)
    prepared = _prepare(request, ceilings)
    held_out_note = _preflight_held_out(request, prepared)
    files = _candidate_files(prepared)
    locations = _locations(prepared, files)
    calibration = _counted(prepared, held_out=False)
    state = Path(request.state_dir) if request.state_dir is not None else Path(".flux")
    state.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="calibrate-", dir=state))
    gate: Optional[GateReport] = None
    try:
        with QueryRunner(
            {},
            calibration.requests,
            effective_limits(prepared.plan),
            batches=prepared.evaluations + 2,
        ) as runner:
            evaluator = _Evaluator(
                bundle_files=files,
                parameters=prepared.parameters,
                locations=locations,
                work=work,
                profile=request.profile,
                scale=prepared.registry.calibration.declared_scale,
                plan=prepared.plan,
                calibration=calibration,
                held_out=_counted(prepared, held_out=True),
                targets=prepared.targets,
                stated=prepared.stated,
                runner=runner,
                draws_of=request.bundle,
            )
            calibrator = CmaCalibrator(
                prepared.parameters,
                evaluator,
                evaluations=prepared.evaluations,
                seed=prepared.seed,
            )
            calibrator.calibrate(
                {
                    "profile": prepared.pinned.digest.value,
                    "queries": list(prepared.calibration_ids),
                },
                {
                    parameter.key: Decimal(exact_text(parameter.before))
                    for parameter in prepared.parameters
                },
            )
            result = calibrator.result
            assert result is not None
            versions = {
                "cma": result.cma_version,
                "sqlglot": SQLGLOT_VERSION,
                "duckdb": str(runner.hello["duckdb"]),
            }
            operations = _operations(prepared.parameters, result.values)
            document = patch_document(prepared, result, operations, versions)
            text = render_patch(document)
            patch_path = patch_path_of(request.out)
            write_text_atomic(patch_path, text)
            digest = patch_digest(text)
            # The patch exists and its digest is taken: the held-out profile is opened now, never before.
            if held_out_note is None and request.held_out is not None:
                try:
                    gate = _check_held_out(request, prepared, evaluator, result.values)
                except BaseException:
                    # A refusal here is no report: the patch it follows is not left behind, so the
                    # rerun is not met by "already exists".
                    patch_path.unlink(missing_ok=True)
                    raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    bundle = (
        _write_bundle(request, prepared, (operations, digest, versions), gate)
        if gate is not None and gate.calibrated and operations
        else None
    )
    return FitOutcome(
        patch_path=patch_path,
        patch_text=text,
        patch_digest=digest,
        patch=document,
        operations=operations,
        result=result,
        parameters=prepared.parameters,
        registry=prepared.registry,
        pinned=prepared.pinned,
        simulation_id=prepared.cast.simulation_id,
        gate=gate,
        bundle=bundle,
        held_out_note=held_out_note,
    )
