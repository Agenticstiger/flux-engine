# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Async engine facade -- the library entry point a host process imports.

Mirrors forge-cli's ``fluid_build.engine`` (Pulumi Automation API pattern):
one ``async def`` per stage, dispatching to the synchronous
:mod:`flux_engine.stages` functions via ``asyncio.to_thread`` so an
in-process consumer (a host application's backend, a hosted runner) never
blocks its event loop on a run that can take real wall-clock time.

Frozen kwarg names, from this module's first release forward: adding an
optional kwarg to any of these functions is not a breaking change,
renaming or removing one is. Every callable accepts an optional
``timeout: float | None`` -- when it fires the awaiter gets
``TimeoutError``, but (Python threads cannot be cancelled) the stage
thread keeps running in the background; this is the same soft-timeout
contract forge-cli's engine documents, not a hard kill.

Every keyword the stage functions in :mod:`flux_engine.stages` accept is
accepted here too, and ``tests/engine`` asserts that pairing so the two
boundaries cannot drift apart again: a library host that could not sign a
fidelity record, or could not accept a declared-baseline drift, was
structurally confined to a subset of what the command line can do.

Kernel packages (``core``, ``world``, ``runtime``, ``expr``, ``seam``) may
not import this module -- see the import-linter contract in
``pyproject.toml`` -- so nothing here is on any digest path; it is purely
an orchestration layer over :mod:`flux_engine.stages`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Optional, Union

from flux_engine.api.stages import StageResult, failure_result
from flux_engine.stages.apply import apply as _apply_stage
from flux_engine.stages.plan import plan as _plan_stage
from flux_engine.stages.replay import replay as _replay_stage
from flux_engine.stages.run import run as _run_stage
from flux_engine.stages.ship import ship as _ship_stage
from flux_engine.stages.validate import validate as _validate_stage
from flux_engine.stages.verify import verify as _verify_stage

__all__ = ["apply", "plan", "replay", "run", "ship", "validate", "verify"]


async def _call(fn: Any, timeout: Optional[float], stage: str, /, **kwargs: Any) -> StageResult:
    # `timeout` is positional-only (S3 note from the API review): with only
    # `fn` marked `/`, a future stage kwarg literally named ``timeout``
    # arriving through **kwargs would silently bind to this function's own
    # `timeout` parameter instead of forwarding to the stage call -- moving
    # the `/` past `timeout` closes that off structurally rather than by
    # convention.
    # The same failure boundary the CLI has. Without it, a stage that
    # raised -- a truncated receipt is a real one: present, so the file
    # check passes, and then `json.loads` raises -- reached a host's event
    # loop as a raw exception while the identical failure reached a CLI
    # caller as a typed envelope. `TimeoutError` is deliberately NOT
    # converted: it is this function's own contract (see the module
    # docstring), not a stage's outcome.
    coro = asyncio.to_thread(fn, **kwargs)
    try:
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise
    except Exception as error:  # noqa: BLE001 - the facade's outermost boundary
        return failure_result(stage, error)


async def validate(
    bundle_directory: Union[str, Path],
    *,
    tolerate_additive: bool = True,
    timeout: Optional[float] = None,
) -> StageResult:
    """``fluxsim validate``, off the event loop."""
    return await _call(
        _validate_stage,
        timeout,
        "validate",
        bundle_directory=bundle_directory,
        tolerate_additive=tolerate_additive,
    )


async def plan(
    bundle_directory: Union[str, Path],
    *,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    timeout: Optional[float] = None,
) -> StageResult:
    """``fluxsim plan``, off the event loop."""
    return await _call(
        _plan_stage,
        timeout,
        "plan",
        bundle_directory=bundle_directory,
        simulation_id=simulation_id,
        state_dir=state_dir,
        zone=zone,
    )


async def apply(
    bundle_directory: Union[str, Path],
    *,
    run_id: Optional[str] = None,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    auto_plan: bool = True,
    timeout: Optional[float] = None,
) -> StageResult:
    """``fluxsim apply``, off the event loop."""
    return await _call(
        _apply_stage,
        timeout,
        "apply",
        bundle_directory=bundle_directory,
        run_id=run_id,
        simulation_id=simulation_id,
        state_dir=state_dir,
        zone=zone,
        auto_plan=auto_plan,
    )


async def run(
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
    timeout: Optional[float] = None,
) -> StageResult:
    """``fluxsim run`` (``plan`` + ``apply``, with ``--verify``/``--ship`` sugar), off the event loop."""
    return await _call(
        _run_stage,
        timeout,
        "run",
        bundle_directory=bundle_directory,
        simulation_id=simulation_id,
        state_dir=state_dir,
        zone=zone,
        verify=verify,
        ship=ship,
        out_dir=out_dir,
        committed_slice=committed_slice,
        update_golden=update_golden,
        reason=reason,
        allow_declared_baseline_drift=allow_declared_baseline_drift,
        signing_key=signing_key,
        trust_key=trust_key,
        committed_at=committed_at,
    )


async def verify(
    bundle_directory: Union[str, Path],
    *,
    run_id: str,
    state_dir: Union[str, Path, None] = None,
    update_golden: bool = False,
    reason: Optional[str] = None,
    committed_slice: Optional[Dict[str, str]] = None,
    vectors_path: Union[str, Path, None] = None,
    allow_declared_baseline_drift: bool = False,
    signing_key: Optional[bytes] = None,
    trust_key: Optional[bytes] = None,
    timeout: Optional[float] = None,
) -> StageResult:
    """``fluxsim verify``, off the event loop."""
    return await _call(
        _verify_stage,
        timeout,
        "verify",
        bundle_directory=bundle_directory,
        run_id=run_id,
        state_dir=state_dir,
        update_golden=update_golden,
        reason=reason,
        committed_slice=committed_slice,
        vectors_path=vectors_path,
        allow_declared_baseline_drift=allow_declared_baseline_drift,
        signing_key=signing_key,
        trust_key=trust_key,
    )


async def ship(
    bundle_directory: Union[str, Path],
    *,
    run_id: str,
    out_dir: Union[str, Path],
    state_dir: Union[str, Path, None] = None,
    committed_slice: Optional[Dict[str, str]] = None,
    signing_key: Optional[bytes] = None,
    trust_key: Optional[bytes] = None,
    committed_at: Optional[str] = None,
    timeout: Optional[float] = None,
) -> StageResult:
    """``fluxsim ship``, off the event loop."""
    return await _call(
        _ship_stage,
        timeout,
        "ship",
        bundle_directory=bundle_directory,
        run_id=run_id,
        out_dir=out_dir,
        state_dir=state_dir,
        committed_slice=committed_slice,
        signing_key=signing_key,
        trust_key=trust_key,
        committed_at=committed_at,
    )


async def replay(
    bundle_directory: Union[str, Path],
    *,
    run_id: str,
    assert_digest: str,
    simulation_id: Optional[str] = None,
    state_dir: Union[str, Path, None] = None,
    zone: Optional[str] = None,
    timeout: Optional[float] = None,
) -> StageResult:
    """``fluxsim replay --assert-digest``, off the event loop."""
    return await _call(
        _replay_stage,
        timeout,
        "replay",
        bundle_directory=bundle_directory,
        run_id=run_id,
        assert_digest=assert_digest,
        simulation_id=simulation_id,
        state_dir=state_dir,
        zone=zone,
    )
