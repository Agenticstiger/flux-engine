# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""One emit's rows generated in several processes, byte for byte as one process makes them.

SEAM-FIDELITY-SPEC §1 addresses every generator draw by its row and column,
never by the order the generator reaches it, so rows ``i`` to ``j`` are the
same rows in whichever process they are made. A run with more than one job
cuts its row range into slices, has a pool of worker processes generate,
render and frame each slice, and joins the framed lines in row order. The
lines are what an ``outputDigest`` covers (KERNEL-SPEC §8), so joining them
in order is all it takes for the digest, and every byte the run writes, to
be what one job gives.

The pattern is borrowed from tpchgen-rs (Apache-2.0), which generates TPC-H
data as independent, deterministic parts in parallel and stays byte-identical
with the reference generator. Here the parts are row slices and the pool is
the standard library's :class:`concurrent.futures.ProcessPoolExecutor`.

Workers are started with ``spawn`` on every platform: a fresh interpreter
that imports the engine, rather than a ``fork`` of a process that may be
running threads. Starting one costs about as much as generating
:data:`MIN_ROWS_PER_JOB` rows, so an emit smaller than that per job is
generated in this process instead. A worker starts with the current
directory off its import path, and it ends when the process that started it
ends, even one that was killed.

Refusals keep their order. One process generates every row of an emit
before it renders any, so a generation refusal anywhere comes before a
rendering refusal anywhere, and within each kind the lowest row comes
first. The join applies the same rule to the slices' outcomes, and raises
the refusal again in this process with its own code and message.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from decimal import setcontext
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError
from flux_engine.core.rng import kernel_context
from flux_engine.core.seedtree import SeedTreeRoot
from flux_engine.generator.rows import GeneratorPlan, generate_row_slice
from flux_engine.seam.conform import render_row
from flux_engine.seam.contract import CompiledExpose
from flux_engine.seam.emit import row_line

__all__ = [
    "MIN_ROWS_PER_JOB",
    "cpu_jobs",
    "generate_framed",
    "require_jobs",
    "reserve_workers",
    "workers_for",
]

#: The fewest rows a worker process is started for.
MIN_ROWS_PER_JOB = 4096

#: Slices per worker. More than one, so that one slow slice does not leave
#: the other workers idle at the end of the run.
_SLICES_PER_WORKER = 4

#: ``ProcessPoolExecutor`` refuses more workers than this on Windows.
_WINDOWS_MAX_WORKERS = 61

Rows = Tuple[Tuple[Any, ...], ...]


def require_jobs(jobs: int) -> int:
    """*jobs* if it is a usable job count; ``ValueError`` otherwise."""
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ValueError(f"jobs must be a positive integer, got {jobs!r}")
    return jobs


def cpu_jobs() -> int:
    """One job per CPU this process may run on.

    ``os.process_cpu_count`` (Python 3.13) and, before it,
    ``os.sched_getaffinity`` where the platform has it count the CPUs the
    process's affinity allows; ``os.cpu_count`` is the machine's count. None
    of them knows a container's CPU quota: under one, give ``--jobs`` a number.
    """
    if hasattr(os, "process_cpu_count"):
        count = os.process_cpu_count()
    elif hasattr(os, "sched_getaffinity"):
        count = len(os.sched_getaffinity(0))
    else:  # pragma: no cover - platforms with neither
        count = os.cpu_count()
    return max(1, count or 1)


def _platform_max_workers() -> int:
    """The most workers one pool may have here."""
    return _WINDOWS_MAX_WORKERS if sys.platform == "win32" else sys.maxsize


def workers_for(rows: int, jobs: int) -> int:
    """How many worker processes an emit of *rows* rows gets under *jobs*; 1 is this one.

    Never more than one per CPU: generating rows is CPU-bound, so a process
    beyond that only adds a start-up cost, and a job count typed with an
    extra digit cannot start thousands of processes.
    """
    cap = min(require_jobs(jobs), cpu_jobs(), _platform_max_workers())
    if not _children_keep_the_working_directory_off_their_path():
        return 1
    return max(1, min(cap, rows // MIN_ROWS_PER_JOB))


def _children_keep_the_working_directory_off_their_path() -> bool:
    """Whether a spawned worker honours ``PYTHONSAFEPATH``.

    A child gets this interpreter's flags. Under ``-E`` it ignores the
    environment, so the variable that keeps the current directory off its
    path would be ignored too, unless ``-P`` (or ``-I``, which implies both)
    keeps it off anyway. In that one case the rows are made in this process.
    """
    return bool(sys.flags.safe_path) or not sys.flags.ignore_environment


class _WorkerBudget:
    """The worker processes every run in this process may have at once: one per CPU.

    Only workers are counted. A run that makes its rows itself, in its own
    process, reserves nothing.

    Runs that share a process (a host running several through the async
    facade) would otherwise each start a full pool, and together run several
    workers per CPU. A run takes the workers it asks for or what is left,
    whichever is fewer, and gives them back when its emit is done. One left
    with fewer than two makes its rows itself rather than wait, so no run
    ever blocks on another.

    loky's reusable executor shares one pool per interpreter; this shares the
    count of workers instead, because each emit's pool carries that emit's
    plan in its initializer.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_use = 0

    @contextmanager
    def reserve(self, wanted: int) -> Iterator[int]:
        reserved = 0
        try:
            with self._lock:
                granted = min(wanted, cpu_jobs() - self._in_use)
                if granted >= 2:
                    self._in_use += granted
                    reserved = granted
            # With nothing reserved, this process makes the rows.
            yield reserved or 1
        finally:
            if reserved:
                with self._lock:
                    self._in_use -= reserved


_BUDGET = _WorkerBudget()


def reserve_workers(wanted: int) -> AbstractContextManager[int]:
    """Reserve up to *wanted* workers for one emit: ``with reserve_workers(n) as workers:``.

    *workers* is how many to start, and 1 means this process makes the rows
    (see :class:`_WorkerBudget`).
    """
    return _BUDGET.reserve(wanted)


def _slices(rows: int, count: int) -> List[Tuple[int, int]]:
    """``[start, stop)`` bounds cutting ``range(rows)`` into at most *count* slices, in order."""
    step = max(1, -(-rows // max(1, count)))
    return [(start, min(start + step, rows)) for start in range(0, rows, step)]


@dataclass(frozen=True)
class _SliceOutcome:
    """What a worker hands back for one slice: its rows and lines, or where it stopped.

    A refusal travels as its code and detail and is raised again from the
    parent's own catalog entry, so the parent's error is the very code
    object one process would have raised.
    """

    rows: Rows = ()
    lines: Tuple[bytes, ...] = ()
    generation_refusal: Optional[Tuple[str, str]] = None
    framing_refusal: Optional[Tuple[str, str]] = None


def _first_refusal(outcomes: Sequence[_SliceOutcome]) -> Optional[Tuple[str, str]]:
    """The refusal one process would have raised: any generation one first, by row."""
    for outcome in outcomes:
        if outcome.generation_refusal is not None:
            return outcome.generation_refusal
    for outcome in outcomes:
        if outcome.framing_refusal is not None:
            return outcome.framing_refusal
    return None


#: A worker's copy of the emit it serves, set once by :func:`_start_worker`.
_WORKER: Dict[str, Any] = {}


def _start_worker(plan: GeneratorPlan, root: SeedTreeRoot, expose: CompiledExpose) -> None:
    _exit_with_parent()
    # The pipeline evaluates a run in the kernel's decimal context; a new
    # process starts in Python's default one, so install the kernel's here.
    setcontext(kernel_context())
    _WORKER.update(plan=plan, root=root, expose=expose)


def _exit_with_parent() -> None:
    """End this worker when the process that started it ends, however it ends.

    A pool's shutdown stops its workers, but a parent that is killed (the
    out-of-memory killer, SIGKILL, an unhandled SIGTERM) never shuts its
    pool down, and its workers would wait on the call queue for ever: each
    of them holds the queue's write end as well, so no end of file comes.
    The sentinel multiprocessing gives every child it starts becomes ready
    when the parent exits.
    """
    import multiprocessing
    from multiprocessing.connection import wait

    parent = multiprocessing.parent_process()
    if parent is None:  # pragma: no cover - always set in a spawned worker
        return
    sentinel = parent.sentinel

    def watch() -> None:
        wait([sentinel])
        os._exit(1)

    threading.Thread(target=watch, name="flux-parent-watch", daemon=True).start()


_SPAWN_LOCK = threading.Lock()


@contextmanager
def _children_without_the_working_directory() -> Iterator[None]:
    """Start processes with the current directory off their import path.

    A spawned worker starts as ``python -c ...``, and ``-c`` puts the current
    directory first on ``sys.path``, where ``fluxsim`` itself never has it. A
    ``multiprocessing`` or ``selectors`` package in the directory a run was
    started from, a checked-out bundle for one, would then be imported in
    place of the standard library's, in every worker. ``PYTHONSAFEPATH``
    (Python 3.11) tells a new interpreter to leave that directory off. It is
    set while the pool starts its processes, under a lock, and put back
    afterwards, since ``os.environ`` belongs to the whole process.

    Nothing is changed when the children start safe already: this
    interpreter runs with ``-P`` (multiprocessing passes its flags on), or
    the variable is set, as ``fluxsim`` sets it for its own process. Then no
    other thread ever sees the environment change.
    """
    if sys.flags.safe_path or os.environ.get("PYTHONSAFEPATH"):
        yield
        return
    with _SPAWN_LOCK:
        previous = os.environ.get("PYTHONSAFEPATH")
        os.environ["PYTHONSAFEPATH"] = "1"
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("PYTHONSAFEPATH", None)
            else:
                os.environ["PYTHONSAFEPATH"] = previous


def _run_slice(bounds: Tuple[int, int]) -> _SliceOutcome:
    """Generate, render and frame rows ``bounds[0]`` to ``bounds[1] - 1``, in a worker."""
    plan: GeneratorPlan = _WORKER["plan"]
    expose: CompiledExpose = _WORKER["expose"]
    start, stop = bounds
    try:
        rows = generate_row_slice(plan, start, stop, root=_WORKER["root"])
    except FluxError as error:
        return _SliceOutcome(generation_refusal=(error.code.code, error.detail))
    lines: List[bytes] = []
    for row_index, row in enumerate(rows, start=start):
        try:
            lines.append(row_line(render_row(row, expose, row_index=row_index)))
        except FluxError as error:
            return _SliceOutcome(rows=rows, framing_refusal=(error.code.code, error.detail))
    return _SliceOutcome(rows=rows, lines=tuple(lines))


def generate_framed(
    plan: GeneratorPlan, *, root: SeedTreeRoot, expose: CompiledExpose, workers: int
) -> Tuple[Rows, List[bytes]]:
    """*plan*'s rows and their framed lines, made by *workers* worker processes.

    The same rows, in the same order, and the same lines
    (``row_line(render_row(row))``) as one process makes. A refusal from a
    worker is raised again here with its code and message; the exception it
    was chained from, if any, stays in the worker.
    """
    # Imported here, so that a one-job run and `fluxsim --help` never load them.
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    slices = _slices(plan.rows, workers * _SLICES_PER_WORKER)
    # The pool starts its resource tracker when it is built and its workers as
    # the slices are submitted, which map does before it returns.
    #
    # On the way out after a failure the pool is shut down with wait=True: the
    # workers still running a slice finish it (or die with it) before this
    # returns, because the caller then gives their share of the budget back
    # (reserve_workers), and a worker still busy must not be counted as free.
    failure: Optional[BaseException] = None
    with _children_without_the_working_directory():
        pool = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_start_worker,
            initargs=(plan, root, expose),
        )
        try:
            pending = pool.map(_run_slice, slices)
        except BaseException as error:
            failure = error
    if failure is not None:
        pool.shutdown(wait=True, cancel_futures=True)
        raise failure
    try:
        outcomes = list(pending)
    except BrokenProcessPool as error:
        pool.shutdown(wait=True, cancel_futures=True)
        raise RuntimeError(
            f"a worker process generating {expose.expose_id!r} stopped before it "
            "finished: it could not start, ran out of memory or was killed. A script "
            'that starts the run needs an `if __name__ == "__main__":` guard, because '
            "each worker imports the script again. jobs=1 generates the rows in this "
            "process."
        ) from error
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown()

    refusal = _first_refusal(outcomes)
    if refusal is not None:
        code, detail = refusal
        raise FluxError(code, detail)
    rows = tuple(row for outcome in outcomes for row in outcome.rows)
    lines = [line for outcome in outcomes for line in outcome.lines]
    return rows, lines
