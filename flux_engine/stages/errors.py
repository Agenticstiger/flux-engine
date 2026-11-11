# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``FLX-21nn`` codes owned by the stage/engine/CLI layer.

KERNEL-SPEC section 13's "plan/replay binding" band (``2000``) is shared
with :mod:`flux_engine.core.trace` (``FLX-2103``..``FLX-2105``) and
:mod:`flux_engine.core.seedtree` (``FLX-2201``, ``FLX-2202``); this module
takes the numbers those two do not use for the things neither of them can
check -- whether the *bundle on disk*, or the plan's own recorded
artifacts, have moved since a plan (or a prior run) was recorded. That
check only exists once there is a plan or a run digest to compare
against, which is a fact about the six-step loop, not about a kernel
primitive.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.governance.skills import UnsupportedFidelity

__all__ = [
    "FLX_2101",
    "FLX_2102",
    "FLX_2106",
    "FLX_2107",
    "FLX_2108",
    "FLX_2109",
    "FLX_2110",
    "FLX_2111",
    "FLX_2114",
    "FLX_4103",
    "FLX_4104",
    "InputDigestMoved",
    "NoAppliedRun",
    "NoRecordedPlan",
    "PlanDigestMoved",
    "ReplayDigestMismatch",
    "RunArtifactsMoved",
    "SeedPathsMoved",
    "SweepExecutorUnavailable",
    "UnknownCommittedSliceExpose",
]

FLX_2101: ErrorCode = register(
    "FLX-2101",
    "the bundle's inputDigest has moved since the plan was recorded",
    "KERNEL-SPEC section 11: apply/run refuse to proceed once a document the "
    "plan covered has changed on disk, an in-bundle FLUID contract has "
    "changed, or a document was added or removed. Re-run `fluxsim plan` "
    "against the bundle as it stands now, review the new plan, then apply "
    "it -- an apply is a decision about the bundle the plan describes, not "
    "about whatever the bundle happens to be by the time apply runs.",
)

FLX_2102: ErrorCode = register(
    "FLX-2102",
    "the recorded plan's own planDigest has moved",
    "The plan.json read back from the run directory does not hash to the "
    "planDigest it was written with -- it was edited, truncated, or "
    "written by an incompatible version after the fact. A plan is a fixed "
    "point once written; re-run `fluxsim plan` to produce a fresh one rather "
    "than editing this one by hand.",
)

FLX_2107: ErrorCode = register(
    "FLX-2107",
    "no recorded plan at this run id",
    "`fluxsim apply --run-id <id>` (or `fluxsim verify`/`fluxsim ship` against that "
    "id) found no `plan.json` under `<state_dir>/runs/<id>/` -- the run id "
    "was never planned in this state directory, or the state directory is "
    "not the one `fluxsim plan` wrote into. Run `fluxsim plan` first, or omit "
    "`--run-id` to let `apply` plan implicitly.",
)

FLX_4103: ErrorCode = register(
    "FLX-4103",
    "fidelity: stochastic declared with no sweep executor available",
    "Simulation.spec.fidelity: stochastic (schema-declared, escalating "
    "past deterministic) requires a sweep executor that runs one "
    'Simulation per seed in `("sweep", run_index)` (KERNEL-SPEC section '
    "3) and aggregates the results. This build ships the seed-tree "
    "primitives (`sweep_effective_seed`/`sweep_path`) but no executor "
    "yet -- run one Simulation.spec.seed at a time (fidelity: "
    "deterministic) until a sweep executor is available.",
)

FLX_4104: ErrorCode = register(
    "FLX-4104",
    "Simulation.spec.fidelity names a value this engine does not recognise",
    "The FLUX schema's fidelity enum is deterministic/stochastic/agentic; "
    "a value outside that set has no execution path in this engine and is "
    "refused rather than silently treated as deterministic (which "
    "`require_supported_fidelity` would otherwise do by only checking the "
    "two escalated values by name).",
)

FLX_2106: ErrorCode = register(
    "FLX-2106",
    "a replay produced a different run digest than the one asserted",
    "`fluxsim replay --assert-digest <digest>` re-executes a recorded plan and "
    "compares the resulting runDigest against the digest given on the "
    "command line. A mismatch means the engine, the bundle, or the "
    "environment moved between the two runs -- `fluxsim verify` on the same "
    "run directory names which run-digest component moved.",
)


FLX_2108: ErrorCode = register(
    "FLX-2108",
    "the recorded seed-path list is missing or no longer matches the plan",
    "KERNEL-SPEC section 3: the seed-tree paths a plan anticipated live in "
    "`seed-paths.ndjson` beside `plan.json`, and the plan carries that file's "
    "raw-bytes digest as `seedTreeDigest`. A missing file, or one whose bytes "
    "no longer hash to the recorded digest, means the set of streams the run "
    "is allowed to draw from is no longer the set that was planned -- so the "
    "FLX-2201 check would be made against the wrong list. Re-run `fluxsim plan`; "
    "the file is rewritten whole from the plan's own execution, never edited.",
)

FLX_2109: ErrorCode = register(
    "FLX-2109",
    "no applied run at this run id",
    "`fluxsim verify` and `fluxsim ship` read a run directory `fluxsim apply` "
    "wrote: the receipt and the run digest are the artifacts they check. "
    "Neither is present at this run id in this state directory, so there "
    "is nothing to verify or ship. Run `fluxsim apply` (or `fluxsim run`) first, "
    "then pass the run id it reports.",
)


FLX_2110: ErrorCode = register(
    "FLX-2110",
    "run artifacts do not reproduce the recorded run digest",
    "KERNEL-SPEC section 10 builds the run digest from one component "
    "digest per artifact: each emit stream's framed outputDigest, the raw "
    "bytes of decisions.ndjson, and the trace ledgers. `fluxsim verify` and "
    "`fluxsim ship` recompute every one of them from the files on disk and "
    "compare. A difference means the run directory no longer holds the "
    "bytes the recorded digest was taken over -- a stream was edited, "
    "truncated or replaced after `apply` wrote it, or an artifact is "
    "missing. Re-run `fluxsim apply` (or `fluxsim replay --assert-digest`) "
    "rather than shipping artifacts nothing in the run produced.",
)

FLX_2111: ErrorCode = register(
    "FLX-2111",
    "a --committed-slice names an expose this run does not emit",
    "Each `--committed-slice EXPOSE_ID=file://...` is looked up by expose "
    "id against the run's own receipt. An id no emit of the run carries -- "
    "a typo, or a slice meant for a different bundle -- would otherwise be "
    "silently discarded and the emit it was meant for reported as having "
    "no slice at all, so the caller is told to supply an argument they did "
    "supply. The refusal names the unknown ids and the ones this run does "
    "emit.",
)


#: ``FLX-2112`` and ``FLX-2113`` are taken, by
#: :mod:`flux_engine.stages.layout` -- the run id's grammar and the run
#: directory's binding to its own plan. The golden regression needed a
#: code of its own and 2112 was briefly proposed for it; two failures
#: under one code is the one thing an error catalog exists to prevent, so
#: the regression got the next free number instead.
FLX_2114: ErrorCode = register(
    "FLX-2114",
    "the run digest moved from the recorded golden baseline",
    "KERNEL-SPEC section 10 records one golden baseline per simulation "
    "under `<state_dir>/golden/`, and `fluxsim verify` compares this run's "
    "runDigest against it. A difference means the same bundle no longer "
    "produces the same run -- the engine, a document, or an input moved. "
    "`fluxsim verify` names the component that moved (or says the inputs "
    "moved with every component identical). When the move is intended, "
    're-record the baseline with `--update-golden --reason "<why>"`; the '
    "reason is written into the golden record, so a baseline can never be "
    "moved silently.",
)


class InputDigestMoved(FluxError):
    """Raised by ``apply``/``run`` when the bundle's ``inputDigest`` moved."""

    def __init__(self, *, planned: str, current: str) -> None:
        super().__init__(FLX_2101, f"plan recorded {planned}, bundle is now {current}")
        self.planned = planned
        self.current = current


class PlanDigestMoved(FluxError):
    """Raised when ``plan.json``'s own ``planDigest`` no longer matches its bytes."""

    def __init__(self, *, recorded: str, recomputed: str) -> None:
        super().__init__(FLX_2102, f"plan.json declares {recorded}, recomputes as {recomputed}")
        self.recorded = recorded
        self.recomputed = recomputed


class ReplayDigestMismatch(FluxError):
    """Raised by ``fluxsim replay --assert-digest`` on a mismatched run digest."""

    def __init__(self, *, asserted: str, replayed: str) -> None:
        super().__init__(FLX_2106, f"asserted {asserted}, replay produced {replayed}")
        self.asserted = asserted
        self.replayed = replayed


class NoRecordedPlan(FluxError):
    """Raised by ``apply``/``verify``/``ship`` when a run id has no ``plan.json``.

    Distinct from :class:`InputDigestMoved` (``FLX-2101``): that code means
    a plan *was* found but the bundle has since changed underneath it. This
    one means no plan was ever recorded at this run id in this state
    directory -- two different failures that should not share a title.
    """

    def __init__(self, *, plan_path: "object") -> None:
        super().__init__(FLX_2107, f"no plan.json at {plan_path}; run `fluxsim plan` first")
        self.plan_path = plan_path


class NoAppliedRun(FluxError):
    """Raised by ``verify``/``ship`` when a run id has no applied artifacts.

    Distinct from :class:`NoRecordedPlan` (``FLX-2107``): a plan can be
    recorded at a run id that was never applied, and "you have not planned"
    and "you have planned but not applied" are different next steps.
    """

    def __init__(self, *, run_dir: "object") -> None:
        super().__init__(FLX_2109, f"no applied run at {run_dir}; run `fluxsim apply` first")
        self.run_dir = run_dir


class SeedPathsMoved(FluxError):
    """Raised when ``seed-paths.ndjson`` is absent or has moved since the plan.

    Distinct from :class:`PlanDigestMoved` (``FLX-2102``): ``plan.json`` can be
    perfectly intact and still describe a path set whose file was deleted,
    truncated or edited -- and that file is what the ``FLX-2201`` membership
    test reads, so it gets its own refusal rather than being folded into the
    plan's.
    """

    def __init__(self, *, path: Path, recorded: str, recomputed: Optional[str]) -> None:
        detail = (
            f"no seed-paths.ndjson at {path}"
            if recomputed is None
            else f"{path} declares {recorded}, recomputes as {recomputed}"
        )
        super().__init__(FLX_2108, detail)
        self.path = path
        self.recorded = recorded
        self.recomputed = recomputed


class RunArtifactsMoved(FluxError):
    """Raised when a run directory no longer reproduces its recorded run digest.

    Distinct from :class:`InputDigestMoved` (``FLX-2101``), which is about
    the *bundle* moving between plan and apply. This one is about the run's
    own output: the artifacts on disk digest to something other than what
    ``run-digest.json`` records for them, so the recorded digest describes
    a run these files did not produce.
    """

    def __init__(self, *, components: "Sequence[str]") -> None:
        named = ", ".join(components)
        super().__init__(
            FLX_2110,
            f"recomputed from the run directory, these components differ from "
            f"run-digest.json: {named}",
        )
        self.components = tuple(components)


class UnknownCommittedSliceExpose(FluxError):
    """Raised when ``--committed-slice`` names an expose the run does not emit."""

    def __init__(self, *, unknown: "Sequence[str]", known: "Sequence[str]") -> None:
        known_text = ", ".join(known) if known else "none -- this run declares no emit"
        super().__init__(
            FLX_2111,
            f"--committed-slice names {', '.join(unknown)}; this run emits {known_text}",
        )
        self.unknown = tuple(unknown)
        self.known = tuple(known)


class SweepExecutorUnavailable(UnsupportedFidelity):
    """Raised for ``fidelity: stochastic`` when no sweep executor is installed.

    Subclasses :class:`~flux_engine.governance.skills.UnsupportedFidelity`
    (rather than raising the bare :class:`FluxError` this replaces) so a
    caller can ``except UnsupportedFidelity`` and catch both the
    ``agentic`` and the ``stochastic`` refusal with one exception type, as
    :func:`flux_engine.stages.pipeline.require_supported_fidelity`'s own
    docstring promises.
    """

    def __init__(self, *, kernel_version: str) -> None:
        # Deliberately does not call UnsupportedFidelity.__init__ (which
        # hardcodes FLX_4101's agentic-extension wording) -- FLX-4103 is
        # the code and detail this refusal actually needs.
        FluxError.__init__(
            self,
            FLX_4103,
            "fidelity 'stochastic' requires a sweep executor, which flux_engine "
            f"{kernel_version} does not ship yet -- run one Simulation.spec.seed "
            "at a time (fidelity: deterministic)",
        )
        self.fidelity = "stochastic"
