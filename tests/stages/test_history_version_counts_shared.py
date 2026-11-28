# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A run with workers works a history's version layout out once (ADR 0017, the amendment of 2026-11-28).

Each entity takes a count its room fits, which reads every entity, so the layout is worked out in the
process that starts the pool and each worker is handed it, as it is handed an association's pairing
(ADR 0039). What is pinned: the layout is built once in the parent and never in a worker, the bytes
at ``--jobs 4`` are the bytes at ``--jobs 1``, and the shares of ``--shard I/N`` joined are the whole run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest

from flux_engine.core.kernels import KERNEL_V2
from flux_engine.generator import history
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline

from ._fleet import HISTORY, run_bundle
from ._fleet import bundle as fleet_bundle
from ._same_run import assert_same_run_at_one_and_four_jobs, assert_shares_join_to_the_run
from .test_history_version_counts_run import _every_memo

pytestmark = pytest.mark.integration


def _big(tmp_path: Path) -> Path:
    versions = 3 * parallel.MIN_ROWS_PER_JOB + 11
    return fleet_bundle(tmp_path, versions=versions, entities=2500, depots=1200)


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_a_run_with_workers_works_the_layout_out_once_and_makes_the_bytes_one_job_makes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _big(tmp_path)
    _every_memo()
    asked: List[int] = []
    real = parallel.FramedPool.close

    def counting(self: Any, *, failed: bool = False) -> None:
        # Ask each worker, before it is shut down, how many layouts it worked out itself.
        asked.extend(
            future.result()
            for future in [self._pool.submit(history.chain_builds) for _ in range(16)]
        )
        real(self, failed=failed)

    monkeypatch.setattr(parallel.FramedPool, "close", counting)
    before = history.chain_builds()
    four = pipeline.execute(bundle, jobs=4, kernel=KERNEL_V2)
    built = history.chain_builds() - before
    assert asked and all(found == 0 for found in asked), asked
    assert built == 1, "the process that starts the pool works the one layout out once"
    monkeypatch.undo()
    _every_memo()
    one = pipeline.execute(bundle, jobs=1, kernel=KERNEL_V2)
    for mine, theirs in zip(four.emits, one.emits, strict=True):
        assert mine.writer.lines() == theirs.writer.lines()


@pytest.mark.slow
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_four_jobs_make_the_run_one_job_makes_under_the_fit(tmp_path: Path) -> None:
    assert_same_run_at_one_and_four_jobs(_big(tmp_path), plan=False, result=True, kernel=KERNEL_V2)


def test_the_shares_of_a_run_joined_are_the_whole_run_under_the_fit(tmp_path: Path) -> None:
    assert_shares_join_to_the_run(fleet_bundle(tmp_path), tmp_path, run_bundle, 3, (HISTORY,))
