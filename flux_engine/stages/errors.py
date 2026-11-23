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
    "FLX_2115",
    "FLX_2116",
    "FLX_2119",
    "FLX_2121",
    "FLX_4103",
    "FLX_4104",
    "ColumnProvenanceMoved",
    "InputDigestMoved",
    "KernelNotImplemented",
    "NoAppliedRun",
    "NoRecordedPlan",
    "ParameterRegisterMoved",
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
    "planDigest it was written with: it was edited, truncated, or "
    "written by an incompatible version after the fact. A plan.json that cannot "
    "be read at all as the JSON object `fluxsim plan` wrote (truncated, not UTF-8 "
    "text, or not an object) is refused with this code too. A plan is a fixed "
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
    "then pass the run id it reports. A run directory that holds only shares "
    "(`fluxsim apply --shard I/N` writes `shard-I-of-N.json` and the shares' streams, and "
    "no receipt) is this case too: a share is part of a run, not a run, so apply the run "
    "whole, without --shard, before verifying or shipping it. A receipt that is present "
    "but cannot be read as the JSON object `fluxsim apply` wrote (truncated, not UTF-8 "
    "text, or not an object) is this case too: the directory holds no completed run, so "
    "delete it and run `fluxsim plan` and `fluxsim apply` again.",
)


FLX_2110: ErrorCode = register(
    "FLX-2110",
    "run artifacts do not reproduce the recorded run digest",
    "KERNEL-SPEC section 10 builds the run digest from one component "
    "digest per artifact: each emit stream's framed outputDigest, the raw "
    "bytes of decisions.ndjson, and the trace ledgers. `fluxsim verify` and "
    "`fluxsim ship` recompute every one of them from the files on disk and "
    "compare. A difference means the run directory no longer holds the "
    "bytes the recorded digest was taken over: a stream was edited, "
    "truncated or replaced after `apply` wrote it, or an artifact is "
    "missing. A run-digest.json that cannot be read as the JSON object `apply` "
    "wrote (truncated, not UTF-8 text, or not an object) is refused with this "
    "code too. Re-run `fluxsim apply` (or `fluxsim replay --assert-digest`) "
    "rather than shipping artifacts nothing in the run produced.",
)

FLX_2111: ErrorCode = register(
    "FLX-2111",
    "a --committed-slice or a story --emit names an expose this run does not emit",
    "`--committed-slice EXPOSE_ID=file://...` and `fluxsim story --emit "
    "EXPOSE_ID` are looked up by expose id against the run's own receipt. "
    "`fluxsim story` refuses an id no emit of the run carries and names the "
    "ones the run does emit. A `--committed-slice` id no emit of the run carries -- "
    "a typo, or a slice meant for a different bundle -- would otherwise be "
    "silently discarded and the emit it was meant for reported as having "
    "no slice at all, so the caller is told to supply an argument they did "
    "supply. The refusal names the unknown ids and the ones this run does "
    "emit.",
)


FLX_2121: ErrorCode = register(
    "FLX-2121",
    "a story --key names no row of the expose, or is not a key it can read",
    "`fluxsim story --emit EXPOSE --key ...` finds the entity by comparing `--key` with the "
    "key columns of the rows the run emitted for that expose. The story is refused when no "
    "row has that key, and when `--key` cannot be read as a key: a composite key given "
    "without `COLUMN=VALUE` once per column, a column named twice, a column the expose does "
    "not have, or a bare value for an expose that declares no one-column key. Check the "
    "value against the run's own stream, and name the column as `COLUMN=VALUE` when the key "
    "has more than one column or the column is not the expose's primary key. The refusal "
    "names the key it looked for and, for an unknown column, the columns the expose has.",
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


FLX_2115: ErrorCode = register(
    "FLX-2115",
    "the recorded column provenance is missing or no longer matches the plan",
    "SEAM-FIDELITY-SPEC section 1: `apply` writes `column-provenance.json` into the "
    "run directory, saying how each column of each exposed table was produced, and "
    "the plan carries a digest of that record as `columnProvenanceDigest`. `ship` "
    "and `verify` read the record back and compare. A missing file, one that does "
    "not hash to the recorded digest, or one this build cannot read as a record, "
    "means the pack would state how columns were produced from a file nothing in the "
    "plan vouches for. `apply` makes the same comparison before it writes anything: "
    "the record its own execution produces must hash to the plan's digest, or it "
    "refuses with this code and writes no row. Run `fluxsim apply` again: the record "
    "is rewritten whole from the plan's own execution, never edited. If `apply` "
    "refuses, or the record differs again, the plan was written by an engine that "
    "classified a column differently from this one, so run `fluxsim plan` and apply "
    "the new plan.",
)


FLX_2116: ErrorCode = register(
    "FLX-2116",
    "the recorded parameter register is missing or no longer matches the plan",
    "docs/spec/PARAMETER-REGISTER-SPEC.md: when a Simulation declares a parameter register, "
    "`apply` writes `parameter-register.json` into the run directory, saying what each number "
    "the run reads rests on, and the plan carries a digest of that record as "
    "`parameterRegisterDigest`. `ship` and `verify` read the record back only through it. A "
    "missing file, one that does not hash to the recorded digest, or one this build cannot "
    "read as a register, means the pack would state the sources of the run's numbers from a "
    "file nothing in the plan vouches for. So does a `parameter-register.json` in the run "
    "directory of a plan that binds none, which `ship` would otherwise pack. `apply` makes the "
    "same comparison before it writes anything: the record its own execution produces must "
    "hash to the plan's digest, or it refuses with this code and writes no row. Run `fluxsim "
    "apply` again: the record is rewritten whole from the plan's own execution, never edited. "
    "Remove a stray file from a run whose plan binds no register. If `apply` refuses, or the "
    "record differs again, the plan was written by an engine that read the register "
    "differently from this one, so run `fluxsim plan` and apply the new plan.",
)


#: ``FLX-2117`` and ``FLX-2118`` are taken, by :mod:`flux_engine.safeio`: a run
#: file, or a state directory, that is a link.
FLX_2119: ErrorCode = register(
    "FLX-2119",
    "the plan names a kernel this engine does not implement",
    "KERNEL-SPEC section 1: a plan records the kernel it was made with as `kernelVersion`, "
    "and `apply`, `replay`, `verify` and `ship` run and stamp the kernel the plan names, "
    "never the one a new plan would take. A kernel decides every draw and is inside "
    "`inputDigest`, so running the plan under another kernel would compute other rows and "
    "other digests and report them as the plan's. This plan hashes to itself and was "
    "written by an engine that implements a kernel this one does not. The refusal names "
    "the plan's kernel and the kernels this engine implements. Apply, verify or ship the "
    "run with an engine that implements its kernel, or run `fluxsim plan` with this one and "
    "apply the new plan, whose digests are those of the kernel it names.",
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


class KernelNotImplemented(FluxError):
    """Raised when a recorded plan names a kernel this engine does not implement.

    Distinct from :class:`PlanDigestMoved` (``FLX-2102``): this plan hashes to
    itself. An engine that implements a kernel this one does not wrote it, and
    running it under another kernel would report that kernel's digests as the
    plan's. *named* is the plan's ``kernelVersion`` as a diagnostic prints it.
    """

    def __init__(self, *, named: str, implemented: "Sequence[str]") -> None:
        super().__init__(
            FLX_2119,
            f"the plan names kernel {named}; this engine implements kernel "
            f"{', '.join(implemented)}",
        )
        self.named = named
        self.implemented = tuple(implemented)


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
        shares = sorted(Path(str(run_dir)).glob("shard-*-of-*.json"))
        if shares:
            what = (
                f"no applied run at {run_dir}: it holds only shares ({shares[0].name}"
                f"{' and others' if len(shares) > 1 else ''}), which are parts of a run and "
                "have no receipt; run `fluxsim apply` without --shard to write the whole run"
            )
        else:
            what = f"no applied run at {run_dir}; run `fluxsim apply` first"
        super().__init__(FLX_2109, what)
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


def _moved_detail(
    path: Path, recorded: str, recomputed: Optional[str], at_apply: bool, record: str
) -> str:
    """What a sidecar of ``plan.json`` that moved says: missing, rewritten, or not this one."""
    if recomputed is None:
        return f"{path} is missing or is not {record} this build can read"
    if at_apply:
        return (
            f"the record this apply would write to {path} hashes to {recomputed}, "
            f"but the plan holds {recorded}"
        )
    return f"{path} declares {recorded}, recomputes as {recomputed}"


class ColumnProvenanceMoved(FluxError):
    """Raised when ``column-provenance.json`` is absent or has moved since the plan.

    Distinct from :class:`SeedPathsMoved` (``FLX-2108``) only in what it
    guards: that file is what the ``FLX-2201`` membership test reads, this
    one is what ``EVIDENCE.md`` says about how each column was produced.
    Both are sidecars of ``plan.json`` whose digest the plan carries, and both
    are rewritten whole by a stage, never edited.
    """

    def __init__(
        self, *, path: Path, recorded: str, recomputed: Optional[str], at_apply: bool = False
    ) -> None:
        detail = _moved_detail(path, recorded, recomputed, at_apply, "a column provenance record")
        super().__init__(FLX_2115, detail)
        self.path = path
        self.recorded = recorded
        self.recomputed = recomputed


class ParameterRegisterMoved(FluxError):
    """Raised when ``parameter-register.json`` is absent, has moved, or is not bound by the plan.

    The register's counterpart of :class:`ColumnProvenanceMoved` (``FLX-2115``):
    the same sidecar of ``plan.json``, whose digest the plan carries, rewritten
    whole by ``apply`` and never edited. One more case is its own: a record in
    the run directory of a plan that binds none (*stray*), which nothing vouches
    for and which a pack would carry as if something did.
    """

    def __init__(
        self,
        *,
        path: Path,
        recorded: Optional[str],
        recomputed: Optional[str],
        at_apply: bool = False,
        stray: bool = False,
    ) -> None:
        if stray:
            detail = (
                f"{path} is in the run directory, but this run's plan binds no parameter "
                "register; remove the file"
            )
        elif recorded is None:
            detail = (
                f"this apply would write a parameter register to {path}, and the plan binds none"
            )
        elif at_apply and recomputed is None:
            detail = f"the plan holds {recorded}, and this apply has no parameter register to write"
        else:
            detail = _moved_detail(path, recorded, recomputed, at_apply, "a parameter register")
        super().__init__(FLX_2116, detail)
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

    def __init__(self) -> None:
        # Deliberately does not call UnsupportedFidelity.__init__ (which
        # hardcodes FLX_4101's agentic-extension wording) -- FLX-4103 is
        # the code and detail this refusal actually needs. The refusal is
        # about this build, which has no sweep executor under any kernel, so
        # it names no kernel version.
        FluxError.__init__(
            self,
            FLX_4103,
            "fidelity 'stochastic' requires a sweep executor, which this engine "
            "does not ship yet -- run one Simulation.spec.seed at a time "
            "(fidelity: deterministic)",
        )
        self.fidelity = "stochastic"
