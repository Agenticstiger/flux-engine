# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Checks that one run is the same run however it is cut: by jobs, or by shares joined in order."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Sequence, Tuple

from flux_engine.stages import pipeline
from flux_engine.stages.layout import RunLayout
from flux_engine.stages.plan import build_plan_document

apply_module = import_module("flux_engine.stages.apply")
plan_module = import_module("flux_engine.stages.plan")


def execute_at_one_and_four_jobs(bundle: Path, **options: Any) -> Tuple[Any, Any]:
    """The bundle's run in one job and in four, in memory; ``options`` go to both."""
    one = pipeline.execute(bundle, jobs=1, **options)
    four = pipeline.execute(bundle, jobs=4, **options)
    return one, four


def assert_same_emits(four: Any, one: Any, *, plan: bool = True, result: bool = False) -> None:
    """Four jobs wrote every emit's lines one job wrote, and the plan and results with the flags."""
    if plan:
        assert build_plan_document(four) == build_plan_document(one)
    for mine, theirs in zip(four.emits, one.emits, strict=True):
        if result:
            assert mine.result == theirs.result
        assert mine.writer.lines() == theirs.writer.lines()


def assert_same_run_at_one_and_four_jobs(
    bundle: Path, *, plan: bool = True, result: bool = False, **options: Any
) -> Tuple[Any, Any]:
    """Run the bundle in one job and four, check them alike, and return both runs."""
    one, four = execute_at_one_and_four_jobs(bundle, **options)
    assert_same_emits(four, one, plan=plan, result=result)
    return one, four


def assert_shares_join_to_the_run(
    bundle: Path,
    tmp_path: Path,
    whole: RunLayout,
    count: int,
    expose_ids: Sequence[str],
    *,
    kernel: str = "0.2.0",
) -> None:
    """Applied in ``count`` shares, the bundle's emits joined in order are ``whole``'s bytes."""
    state = tmp_path / "shards"
    planned = plan_module.plan(bundle, state_dir=state, kernel=kernel, stage=False)
    run_id = planned.artifacts["run_id"]
    assert run_id == whole.run_id
    for index in range(1, count + 1):
        part = apply_module.apply(bundle, run_id=run_id, state_dir=state, shard=(index, count))
        assert part.success, part.stderr
    shards = RunLayout.for_state_dir(state, run_id)
    for expose_id in expose_ids:
        joined = b"".join(
            shards.shard_emit_path(expose_id, index, count).read_bytes()
            for index in range(1, count + 1)
        )
        assert joined == whole.emit_path(expose_id).read_bytes(), expose_id
