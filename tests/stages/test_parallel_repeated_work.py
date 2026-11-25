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

from ._same_run import assert_same_emits

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


def test_a_whole_pass_that_fails_otherwise_leaves_the_one_row_path_to_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _copy(HISTORY, tmp_path)
    relations._finals_store().clear()
    expected = _digests(pipeline.execute(bundle, scale=Decimal(15), jobs=1))

    real = rows.generate_row_slice
    whole_tries: List[str] = []

    def failing(plan: Any, start: int, stop: int, **kwargs: Any) -> Any:
        filling = relations._finals_of(kwargs["root"], plan).filling
        if filling and plan.expose.expose_id == PARENT and stop - start > 1:
            whole_tries.append(plan.expose.expose_id)
            raise RecursionError("the whole pass ran out of stack")
        return real(plan, start, stop, **kwargs)

    monkeypatch.setattr(rows, "generate_row_slice", failing)
    relations._finals_store().clear()
    got = _digests(pipeline.execute(bundle, scale=Decimal(15), jobs=1))

    assert got == expected
    assert len(whole_tries) == 1, "the reader tried the whole pass again after it failed"


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
    assert_same_emits(four, one, plan=False)
