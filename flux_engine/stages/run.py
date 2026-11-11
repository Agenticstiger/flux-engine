# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim run``: ``plan`` then ``apply``, with ``--verify``/``--ship`` sugar.

Not a new stage -- three existing ones, called in the loop's own order.
Stops at the first non-zero exit code, the way `terraform apply` stops
before a downstream step it has no plan for; the artifacts of every stage
that did run are still returned, under that stage's own key, so a caller
that stopped at ``verify`` can still read what ``plan``/``apply`` produced.

**A stage that did not run says so.** ``fluxsim run --verify --ship`` whose
``apply`` failed used to return an artifacts map with no ``verify`` and no
``ship`` key at all, which reads identically to a verify that ran and
found nothing -- and a caller checking ``artifacts["verify"]["ok"]`` got a
``KeyError`` rather than an answer. Every requested stage that the stop
skipped is now recorded as ``{"ran": False, "reason": ...}`` and named on
stdout (:func:`_not_run`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from flux_engine.api.stages import StageResult
from flux_engine.stages.apply import apply as run_apply
from flux_engine.stages.plan import plan_with_result as run_plan_with_result
from flux_engine.stages.ship import ship as run_ship
from flux_engine.stages.verify import verify as run_verify

__all__ = ["run"]


def _not_run(
    artifacts: Dict[str, Any],
    stdout_lines: List[str],
    *,
    requested: Sequence[str],
    reason: str,
) -> None:
    """Record every *requested* stage this run stopped before reaching.

    Both surfaces, from one call: the artifacts map a machine reads and
    the stdout a person reads. A stage that was asked for and did not
    happen is a fact about the run, and the absence of a key is not a way
    to state it.
    """
    for stage in requested:
        artifacts[stage] = {"ran": False, "reason": reason}
        stdout_lines.append(f"{stage}: not run ({reason})")


def run(
    bundle_directory: Union[str, Path],
    *,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    verify: bool = False,
    ship: bool = False,
    out_dir: Union[str, Path, None] = None,
    committed_slice: Optional[Dict[str, str]] = None,
    update_golden: bool = False,
    reason: Optional[str] = None,
    allow_declared_baseline_drift: bool = False,
    signing_key: Optional[bytes] = None,
    trust_key: Optional[bytes] = None,
    committed_at: Optional[str] = None,
) -> StageResult:
    """``plan`` + ``apply``, then ``verify``/``ship`` when asked for.

    ``ship`` implies applying the run first but does not itself run
    ``verify`` -- SEAM-FIDELITY-SPEC's gate is its own check, independent
    of the golden/RFC-04/row-conformance legs ``verify`` runs. Pass both
    flags to get the full loop in one call, exactly what the CLI's
    ``run --verify --ship`` sugar does.

    The plan's own execution is handed to ``apply`` rather than thrown
    away: a plan *is* an execution of the whole pipeline, so re-running it
    a millisecond later over the same bytes doubled the wall clock of
    every ``fluxsim run`` for nothing.
    """
    if ship and out_dir is None:
        return StageResult(stage="run", exit_code=2, stderr="--ship requires --out-dir/-o")

    # The downstream stages this call was asked for, in loop order. Only
    # these are ever reported as "not run": a stage nobody requested did
    # not happen, but that is not a fact about this run.
    downstream = [name for name, asked in (("verify", verify), ("ship", ship)) if asked]

    planned, planned_result = run_plan_with_result(
        bundle_directory, simulation_id=simulation_id, state_dir=state_dir, zone=zone
    )
    artifacts: Dict[str, Any] = {"plan": dict(planned.artifacts)}
    stdout_lines = [line for line in (planned.stdout,) if line]
    if not planned.success:
        _not_run(
            artifacts,
            stdout_lines,
            requested=["apply", *downstream],
            reason=f"plan exited {planned.exit_code}",
        )
        return StageResult(
            stage="run",
            exit_code=planned.exit_code,
            _artifacts=artifacts,
            stdout="\n".join(stdout_lines),
            stderr=planned.stderr,
            error_code=planned.error_code,
        )

    run_id = planned.artifacts["run_id"]
    applied = run_apply(
        bundle_directory,
        run_id=run_id,
        simulation_id=simulation_id,
        state_dir=state_dir,
        zone=zone,
        auto_plan=False,
        precomputed=planned_result,
    )
    artifacts["apply"] = dict(applied.artifacts)
    if applied.stdout:
        stdout_lines.append(applied.stdout)
    if not applied.success:
        _not_run(
            artifacts,
            stdout_lines,
            requested=downstream,
            reason=f"apply exited {applied.exit_code}",
        )
        return StageResult(
            stage="run",
            exit_code=applied.exit_code,
            _artifacts=artifacts,
            stdout="\n".join(stdout_lines),
            stderr=applied.stderr,
            error_code=applied.error_code,
        )

    if verify:
        verified = run_verify(
            bundle_directory,
            run_id=run_id,
            state_dir=state_dir,
            committed_slice=committed_slice,
            update_golden=update_golden,
            reason=reason,
            allow_declared_baseline_drift=allow_declared_baseline_drift,
            # One pair of keys for the whole loop: a --trust-key that
            # reached `ship` and not `verify` would leave one invocation
            # checking one record's signature against two anchors.
            signing_key=signing_key,
            trust_key=trust_key,
        )
        artifacts["verify"] = dict(verified.artifacts)
        if verified.stdout:
            stdout_lines.append(verified.stdout)
        if not verified.success:
            _not_run(
                artifacts,
                stdout_lines,
                requested=["ship"] if ship else [],
                reason=f"verify exited {verified.exit_code}",
            )
            return StageResult(
                stage="run",
                exit_code=verified.exit_code,
                _artifacts=artifacts,
                stdout="\n".join(stdout_lines),
                stderr=verified.stderr,
                error_code=verified.error_code,
            )

    if ship:
        assert out_dir is not None
        shipped = run_ship(
            bundle_directory,
            run_id=run_id,
            out_dir=out_dir,
            state_dir=state_dir,
            committed_slice=committed_slice,
            signing_key=signing_key,
            trust_key=trust_key,
            committed_at=committed_at,
        )
        artifacts["ship"] = dict(shipped.artifacts)
        if shipped.stdout:
            stdout_lines.append(shipped.stdout)
        if not shipped.success:
            return StageResult(
                stage="run",
                exit_code=shipped.exit_code,
                _artifacts=artifacts,
                stdout="\n".join(stdout_lines),
                stderr=shipped.stderr,
                error_code=shipped.error_code,
            )

    stdout_lines.append(f"run {run_id} complete" + (f"; shipped to {out_dir}" if ship else ""))
    return StageResult(
        stage="run",
        exit_code=0,
        _artifacts=artifacts,
        stdout="\n".join(stdout_lines),
    )
