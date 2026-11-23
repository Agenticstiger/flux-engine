# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A worker does not repeat work that grows with a parent emit, and the bytes stay what one job gives.

A row of a parent emit that a child reads is worked out by a slice of one row, and every such slice
pays the column pass's fixed cost. A history's child reads most of the parent's rows, so each
process used to pay it for most of them. The reader now makes a small parent whole, in one slice,
once it has paid for a share of it a row at a time, and the rows it keeps are the rows the
one-row slices would have made. The tests count the one-row slices (a count of work done, never a
time) and compare the digests of one job and several.
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from flux_engine.generator import relations, rows
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline

REPO_ROOT = Path(__file__).resolve().parents[2]
HISTORY = REPO_ROOT / "tests" / "fixtures" / "history-window"
TEMPORAL = REPO_ROOT / "tests" / "fixtures" / "temporal"
PARENT = "subscription_version"

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _copy(source: Path, tmp_path: Path) -> Path:
    target = tmp_path / source.name
    target.mkdir()
    for path in source.glob("*.yml"):
        shutil.copy2(path, target / path.name)
    return target


def _digests(result: Any) -> List[Any]:
    return [emit.result for emit in result.emits]


@pytest.fixture()
def single_row_slices(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, int]]:
    """Every slice of one row the generator is asked for, as ``(expose id, row)``."""
    seen: List[Tuple[str, int]] = []
    real = rows.generate_row_slice

    def counting(plan: Any, start: int, stop: int, **kwargs: Any) -> Any:
        if stop - start == 1:
            seen.append((plan.expose.expose_id, start))
        return real(plan, start, stop, **kwargs)

    monkeypatch.setattr(rows, "generate_row_slice", counting)
    return seen


def test_a_parent_read_row_by_row_is_made_whole_after_a_share_of_it(
    tmp_path: Path, single_row_slices: List[Tuple[str, int]]
) -> None:
    relations._finals_store().clear()
    result = pipeline.execute(_copy(HISTORY, tmp_path), scale=Decimal(15), jobs=1)
    parent_rows = next(
        e.plan.rows
        for e in result.emits
        if e.plan is not None and e.plan.expose.expose_id == PARENT
    )
    singles = sum(1 for expose_id, _ in single_row_slices if expose_id == PARENT)

    assert parent_rows > 1000
    # Before: nearly every row of the parent was worked out by a slice of its own.
    bound = max(relations._FILL_FLOOR, parent_rows // relations._FILL_SHARE)
    assert singles <= bound, (singles, parent_rows)


@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
@pytest.mark.parametrize(
    ("source", "scale"), [(HISTORY, 15), (TEMPORAL, 12)], ids=["history", "temporal"]
)
def test_four_jobs_give_the_digests_one_job_gives(tmp_path: Path, source: Path, scale: int) -> None:
    bundle = _copy(source, tmp_path)
    one = pipeline.execute(bundle, scale=Decimal(scale), jobs=1)
    four = pipeline.execute(bundle, scale=Decimal(scale), jobs=4)

    assert _digests(four) == _digests(one)
    assert any(emit.processes > 1 for emit in four.emits)
    for mine, theirs in zip(four.emits, one.emits, strict=True):
        assert mine.writer.lines() == theirs.writer.lines()
