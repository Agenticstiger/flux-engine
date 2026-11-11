# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The async facade dispatches to the same stage functions, off the event loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from flux_engine import engine


def test_validate_returns_the_same_shape_as_the_sync_stage(bundle_dir: Path) -> None:
    result = asyncio.run(engine.validate(bundle_dir))
    assert result.stage == "validate"
    assert result.success


def test_plan_then_apply_round_trip_through_the_async_facade(
    bundle_dir: Path, state_dir: Path
) -> None:
    async def go() -> None:
        planned = await engine.plan(bundle_dir, state_dir=state_dir)
        assert planned.success, planned.stderr
        applied = await engine.apply(
            bundle_dir, run_id=planned.artifacts["run_id"], state_dir=state_dir
        )
        assert applied.success, applied.stderr

    asyncio.run(go())


def test_a_soft_timeout_raises_timeout_error_without_crashing_the_loop(
    bundle_dir: Path, state_dir: Path
) -> None:
    async def go() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await engine.plan(bundle_dir, state_dir=state_dir, timeout=0.0001)

    asyncio.run(go())


def test_concurrent_validate_calls_do_not_interfere(bundle_dir: Path) -> None:
    async def go() -> None:
        results = await asyncio.gather(
            engine.validate(bundle_dir), engine.validate(bundle_dir), engine.validate(bundle_dir)
        )
        assert all(r.success for r in results)

    asyncio.run(go())
