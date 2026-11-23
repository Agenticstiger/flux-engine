# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The async facade is contract-equivalent to the stage functions it wraps.

Three properties, each of which was false before this file existed.

1. **Every stage callable is reachable, with every keyword.** ``engine.ship``
   and ``engine.run`` silently dropped ``signing_key``, ``trust_key`` and
   ``committed_at``; ``engine.run``/``engine.verify`` dropped
   ``allow_declared_baseline_drift``. A library host was therefore confined
   to a subset of what the command line can do -- structurally forced onto
   the ephemeral-signing path, with no way to ship a record attributable to
   an operator. The parity check here is by introspection, so a keyword
   added to a stage function and not to its facade twin fails immediately.
2. **A failure is a StageResult, not a traceback.** The CLI funnelled every
   escaped exception into an envelope; the facade had no such boundary, so
   one failure was a typed result for one consumer and a raw exception in
   the other's event loop.
3. **The timeout contract is the facade's own**, and is not swallowed by
   the failure boundary above.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Dict, Set

import pytest

from flux_engine import engine
from flux_engine.api.stages import StageName, StageResult
from flux_engine.stages.apply import apply as apply_stage
from flux_engine.stages.plan import plan as plan_stage
from flux_engine.stages.replay import replay as replay_stage
from flux_engine.stages.run import run as run_stage
from flux_engine.stages.ship import ship as ship_stage
from flux_engine.stages.validate import validate as validate_stage
from flux_engine.stages.verify import verify as verify_stage

#: The facade callable that wraps each stage function.
_PAIRS: Dict[str, Any] = {
    "validate": validate_stage,
    "plan": plan_stage,
    "apply": apply_stage,
    "run": run_stage,
    "verify": verify_stage,
    "ship": ship_stage,
    "replay": replay_stage,
}


def _keywords(fn: Any) -> Set[str]:
    return {
        name
        for name, parameter in inspect.signature(fn).parameters.items()
        if parameter.kind in (parameter.KEYWORD_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        and name != "self"
    }


@pytest.mark.unit
def test_every_stage_has_a_facade_callable() -> None:
    assert sorted(engine.__all__) == sorted(StageName)
    for name in StageName:
        assert asyncio.iscoroutinefunction(getattr(engine, name)), name


@pytest.mark.unit
@pytest.mark.parametrize("stage", sorted(_PAIRS))
def test_every_stage_keyword_has_a_facade_counterpart(stage: str) -> None:
    """No keyword is offered by the stage and withheld by the facade."""
    stage_keywords = _keywords(_PAIRS[stage]) - {"precomputed"}
    facade_keywords = _keywords(getattr(engine, stage))
    missing = sorted(stage_keywords - facade_keywords)
    assert missing == [], f"engine.{stage} does not forward: {missing}"


@pytest.mark.unit
@pytest.mark.parametrize("stage", sorted(_PAIRS))
def test_the_facade_invents_no_keyword_of_its_own(stage: str) -> None:
    """Apart from ``timeout``, which is the facade's own contract."""
    extra = sorted(_keywords(getattr(engine, stage)) - _keywords(_PAIRS[stage]) - {"timeout"})
    assert extra == [], f"engine.{stage} accepts keywords the stage does not: {extra}"


@pytest.mark.unit
def test_a_stage_that_raises_comes_back_as_a_failed_stage_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw exception in a host's event loop is the failure this closes."""

    def explode(**_kwargs: object) -> StageResult:
        raise ValueError("a truncated receipt is not JSON")

    monkeypatch.setattr(engine, "_ship_stage", explode)
    result = asyncio.run(engine.ship(tmp_path, run_id="r", out_dir=tmp_path / "out"))
    assert isinstance(result, StageResult)
    assert result.stage == "ship"
    assert result.success is False
    assert result.error_code is None
    assert "a truncated receipt is not JSON" in result.stderr


@pytest.mark.unit
def test_a_catalogued_failure_keeps_its_flx_code_through_the_facade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.core.errors import FluxError
    from flux_engine.seam.resolve import FLX_3107

    def explode(**_kwargs: object) -> StageResult:
        raise FluxError(FLX_3107, "not a directory")

    monkeypatch.setattr(engine, "_validate_stage", explode)
    result = asyncio.run(engine.validate(tmp_path))
    assert result.error_code == "FLX-3107"
    assert result.exit_code == 1


@pytest.mark.unit
def test_the_timeout_still_raises_rather_than_becoming_a_failed_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The soft timeout is the facade's own contract, not a stage outcome."""
    import time

    def slow(**_kwargs: object) -> StageResult:
        time.sleep(0.5)
        return StageResult(stage="validate", exit_code=0)

    monkeypatch.setattr(engine, "_validate_stage", slow)

    async def go() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await engine.validate(tmp_path, timeout=0.01)

    asyncio.run(go())
