# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""More than one job makes the run one job makes: the same rows, lines and digests.

The unit tests pin the pieces :mod:`flux_engine.stages._parallel` is made of:
the job count, when an emit is worth a worker, how the rows are cut, and which
refusal wins. The slow tests run the small telco bundle with its row count
raised past the point where workers start, once with one job and once with
three, and compare everything the run hands on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from decimal import ROUND_UP, Context, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.core.seedtree import derive_root, digest_bytes
from flux_engine.generator.rows import build_plan, generate_rows
from flux_engine.seam.conform import render_row
from flux_engine.seam.contract import compile_expose
from flux_engine.stages import _parallel as parallel
from flux_engine.stages import pipeline
from flux_engine.stages.apply import _emit_summary_line
from flux_engine.stages.plan import build_plan_document
from tests.bundles import bundle_with_rows

from .conftest import FIXTURE_BUNDLE, REPO_ROOT


def _bundle(tmp_path: Path, rows: int) -> Path:
    return bundle_with_rows(FIXTURE_BUNDLE, tmp_path / "bundle", rows)


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("jobs", [0, -1, True, False, 1.5, "2", None])
def test_a_job_count_is_a_positive_integer(jobs: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        parallel.require_jobs(jobs)


@pytest.mark.unit
def test_an_emit_gets_a_worker_only_when_it_has_rows_enough_for_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parallel, "cpu_jobs", lambda: 16)
    least = parallel.MIN_ROWS_PER_JOB
    assert parallel.workers_for(0, 8) == 1
    assert parallel.workers_for(2 * least - 1, 8) == 1
    assert parallel.workers_for(2 * least, 8) == 2
    assert parallel.workers_for(100 * least, 8) == 8
    assert parallel.workers_for(100 * least, 1) == 1


@pytest.mark.unit
def test_never_more_workers_than_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parallel, "cpu_jobs", lambda: 4)
    assert parallel.workers_for(10**9, 5000) == 4


@pytest.mark.unit
@pytest.mark.parametrize("rows,count", [(0, 4), (1, 4), (5, 10), (10, 3), (4096 * 9 + 7, 16)])
def test_the_slices_cover_every_row_once_in_order(rows: int, count: int) -> None:
    slices = parallel._slices(rows, count)
    assert [row for start, stop in slices for row in range(start, stop)] == list(range(rows))
    assert all(start < stop for start, stop in slices)
    assert len(slices) <= count


@pytest.mark.unit
def test_a_generation_refusal_anywhere_comes_before_a_framing_refusal() -> None:
    """One process generates every row before it renders any."""
    outcome = parallel._SliceOutcome
    refusals = [
        outcome(framing_refusal=("FLX-3202", "row 3")),
        outcome(),
        outcome(generation_refusal=("FLX-5021", "row 9000")),
        outcome(generation_refusal=("FLX-3402", "row 12000")),
    ]
    assert parallel._first_refusal(refusals) == ("FLX-5021", "row 9000")
    framing_only = [
        outcome(),
        outcome(framing_refusal=("FLX-3202", "row 9")),
        outcome(framing_refusal=("FLX-3203", "row 20")),
    ]
    assert parallel._first_refusal(framing_only) == ("FLX-3202", "row 9")
    assert parallel._first_refusal([outcome(), outcome()]) is None


@pytest.mark.unit
def test_execute_refuses_a_job_count_before_doing_any_work(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        pipeline.execute(tmp_path / "no-such-bundle", jobs=0)


# ---------------------------------------------------------------------------
# A whole run
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_three_jobs_make_the_run_one_job_makes(tmp_path: Path) -> None:
    rows = 3 * parallel.MIN_ROWS_PER_JOB + 11
    assert parallel.workers_for(rows, 3) == min(3, parallel.cpu_jobs())
    bundle = _bundle(tmp_path, rows)

    one = pipeline.execute(bundle, jobs=1)
    # The caller's own decimal context reaches neither this process's
    # arithmetic nor the workers'.
    with localcontext(Context(prec=5, rounding=ROUND_UP)):
        three = pipeline.execute(bundle, jobs=3)

    assert build_plan_document(three) == build_plan_document(one)
    assert len(three.emits) == len(one.emits)
    for mine, theirs in zip(three.emits, one.emits, strict=True):
        assert mine.result == theirs.result
        assert mine.rows == theirs.rows
        assert mine.writer.lines() == theirs.writer.lines()
    # The generated emit really went through the workers: a writer built from
    # framed lines has no sink, while one process writes through a NullSink.
    generated = [e for e in three.emits if e.plan is not None and e.plan.rows == rows]
    assert generated and all(e.writer.sink is None for e in generated)
    assert all(e.processes == min(3, parallel.cpu_jobs()) for e in generated)
    assert all(e.writer.sink is not None and e.processes == 1 for e in one.emits)


# ---------------------------------------------------------------------------
# The workers' surroundings
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("before", [None, "", "1"])
def test_children_start_with_a_safe_path_and_the_setting_is_put_back(
    before: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if before is None:
        monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    else:
        monkeypatch.setenv("PYTHONSAFEPATH", before)
    with parallel._children_without_the_working_directory():
        assert os.environ["PYTHONSAFEPATH"] == "1"
    assert os.environ.get("PYTHONSAFEPATH") == before


@pytest.mark.unit
def test_a_pool_never_asks_for_more_workers_than_the_platform_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parallel, "cpu_jobs", lambda: 128)
    monkeypatch.setattr(parallel, "_platform_max_workers", lambda: 61)
    assert parallel.workers_for(10**9, 100) == 61


@pytest.mark.unit
def test_before_python_3_13_the_cpu_count_is_the_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "process_cpu_count", raising=False)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 2, 5}, raising=False)
    assert parallel.cpu_jobs() == 3


_WORKERS_UNDER_FLAGS = (
    "from flux_engine.stages import _parallel; print(_parallel.workers_for(10**6, 4))"
)


@pytest.mark.integration
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
@pytest.mark.parametrize("flags,in_process", [(["-E"], True), (["-P"], False), ([], False)])
def test_a_parent_whose_children_would_ignore_pythonsafepath_makes_rows_itself(
    flags: List[str], in_process: bool
) -> None:
    """``-E`` children ignore ``PYTHONSAFEPATH``; ``-P`` keeps the path safe by itself."""
    # From the repository root, so that ``-c`` imports this tree even under ``-E``.
    done = subprocess.run(
        [sys.executable, *flags, "-c", _WORKERS_UNDER_FLAGS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    workers = int(done.stdout.strip())
    assert (workers == 1) is in_process


_PLANTED = """\
import os
with open(os.environ["FLUX_TEST_MARKER"], "a") as marker:
    marker.write("imported from the working directory\\n")
raise ImportError("a planted module")
"""

_RUN_TWO_JOBS = """\
import sys
from flux_engine.stages import pipeline

if __name__ == "__main__":
    result = pipeline.execute(sys.argv[1], jobs=2)
    print(result.emits[0].result.output_digest.value)
"""


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_a_package_in_the_working_directory_is_not_imported_by_a_worker(
    tmp_path: Path,
) -> None:
    """A checked-out bundle cannot put code in front of the standard library."""
    bundle = _bundle(tmp_path, 2 * parallel.MIN_ROWS_PER_JOB + 1)
    work = tmp_path / "work"
    (work / "multiprocessing").mkdir(parents=True)
    (work / "multiprocessing" / "__init__.py").write_text(_PLANTED)
    script = tmp_path / "scripts" / "run_two_jobs.py"
    script.parent.mkdir()
    script.write_text(_RUN_TWO_JOBS)
    marker = tmp_path / "marker.txt"

    done = subprocess.run(
        [sys.executable, str(script), str(bundle)],
        cwd=work,
        env={**os.environ, "FLUX_TEST_MARKER": str(marker)},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith("sha256:")
    assert not marker.exists(), marker.read_text()


_KILLED_PARENT = """\
import multiprocessing
import os
import time
from concurrent.futures import ProcessPoolExecutor

from flux_engine.stages import _parallel


def pid(_):
    time.sleep(0.2)
    return os.getpid()


if __name__ == "__main__":
    pool = ProcessPoolExecutor(
        2,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_parallel._exit_with_parent,
    )
    print(*sorted(set(pool.map(pid, range(8)))), flush=True)
    time.sleep(600)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="signals and pids are POSIX here")
def test_workers_end_when_the_process_that_started_them_is_killed(tmp_path: Path) -> None:
    script = tmp_path / "killed_parent.py"
    script.write_text(_KILLED_PARENT)
    parent = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True, cwd=tmp_path
    )
    try:
        assert parent.stdout is not None
        workers = [int(pid) for pid in parent.stdout.readline().split()]
        assert workers and all(_alive(pid) for pid in workers)
    finally:
        parent.kill()
        parent.wait(timeout=30)
    deadline = time.monotonic() + 30
    while any(_alive(pid) for pid in workers) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not [pid for pid in workers if _alive(pid)]


# ---------------------------------------------------------------------------
# One budget for every run in the process
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runs_in_one_process_share_one_worker_per_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parallel, "cpu_jobs", lambda: 8)
    budget = parallel._WorkerBudget()
    with budget.reserve(6) as first:
        assert first == 6
        with budget.reserve(6) as second:
            assert second == 2
            with budget.reserve(4) as third:
                assert third == 1  # nothing left: this run makes its rows itself
    with budget.reserve(8) as again:
        assert again == 8


@pytest.mark.unit
def test_reserve_workers_draws_on_the_one_budget_of_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parallel, "cpu_jobs", lambda: 8)
    with parallel.reserve_workers(6) as first:
        with parallel.reserve_workers(6) as second:
            assert (first, second) == (6, 2)
    with parallel.reserve_workers(8) as again:
        assert again == 8


@pytest.mark.integration
def test_a_run_makes_its_rows_itself_when_other_runs_hold_every_worker(
    tmp_path: Path,
) -> None:
    """The pipeline asks the process's budget, not a budget of its own."""
    rows = 3 * parallel.MIN_ROWS_PER_JOB + 11
    bundle = _bundle(tmp_path, rows)
    with parallel.reserve_workers(parallel.cpu_jobs()):
        held = pipeline.execute(bundle, jobs=3)
    generated = [e for e in held.emits if e.plan is not None and e.plan.rows == rows]
    assert generated and all(e.processes == 1 for e in generated)
    assert all(e.writer.sink is not None for e in generated)


@pytest.mark.unit
def test_a_run_that_fails_gives_its_workers_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parallel, "cpu_jobs", lambda: 4)
    budget = parallel._WorkerBudget()
    with pytest.raises(RuntimeError):
        with budget.reserve(4):
            raise RuntimeError("the emit failed")
    with budget.reserve(4) as granted:
        assert granted == 4


@pytest.mark.unit
def test_the_summary_says_how_many_processes_made_an_emit() -> None:
    def emitted(source_plan: Any, processes: int) -> Any:
        return SimpleNamespace(
            resolved=SimpleNamespace(expose_id="moments"),
            result=SimpleNamespace(rows=9000),
            source="generator" if source_plan else "scenario",
            processes=processes,
        )

    def line(*outcomes: Any) -> str:
        return _emit_summary_line(SimpleNamespace(emits=outcomes))  # type: ignore[arg-type]

    assert line(emitted(True, 1)) == "emitted moments: 9000 rows (generator)"
    assert line(emitted(True, 4)) == "emitted moments: 9000 rows (generator, 4 processes)"
    assert line(emitted(False, 1)) == "emitted moments: 9000 rows (scenario)"


# ---------------------------------------------------------------------------
# A refusal raised inside a worker
# ---------------------------------------------------------------------------

_SEAM = "sha256:" + "1" * 64


def _refused_on_every_row(rows: int) -> Any:
    """A plan whose every row is refused when rendered: four integer digits in DECIMAL(5,2)."""
    expose = compile_expose(
        {
            "exposeId": "amounts",
            "kind": "stream",
            "contract": {"schema": [{"name": "amount", "type": "DECIMAL(5,2)"}]},
            "binding": {"platform": "kafka", "format": "kafka_topic", "location": {"topic": "t"}},
        }
    )
    plan = build_plan(
        expose,
        sim_id="sim-refusal",
        seam_digest=_SEAM,
        rows=rows,
        fidelity={"columns": {"amount": {"range": {"min": "1000", "max": "9000"}}}},
    )
    return plan, expose, derive_root(digest_bytes(_SEAM), 42)


@pytest.mark.slow
@pytest.mark.integration
@pytest.mark.skipif(parallel.cpu_jobs() < 2, reason="one CPU: every emit stays in one process")
def test_a_refusal_in_a_worker_is_the_refusal_one_process_raises() -> None:
    """Every slice is refused; the run must report the one at row 0, as one process does."""
    plan, expose, root = _refused_on_every_row(3 * parallel.MIN_ROWS_PER_JOB + 7)
    with pytest.raises(FluxError) as alone:
        for index, row in enumerate(generate_rows(plan, root=root)):
            render_row(row, expose, row_index=index)
    with pytest.raises(FluxError) as pooled:
        parallel.generate_framed(plan, root=root, expose=expose, workers=3)
    assert pooled.value.code is alone.value.code
    assert str(pooled.value) == str(alone.value)
    assert "row 0," in str(pooled.value)
