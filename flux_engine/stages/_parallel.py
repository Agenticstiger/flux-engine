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

**One pool per run.** A run starts its pool once, with every emit's compiled
plan, and submits the slices of every emit that is worth workers together, so
the workers move from one emit to the next without idling and without being
started again. Each worker receives the plans once, through the pool's
initializer, and a slice names the emit it belongs to. Joining is per emit in
declared order, so refusals keep the order one process gives them. The idea,
one reusable executor that outlives a single call, is loky's (BSD-3-Clause); it
is a pattern here and no dependency.

Workers start from a ``forkserver`` where the platform has one (every POSIX
system), with the engine imported once in the server so that a worker is a fork
of an interpreter that already holds it, and with ``spawn`` elsewhere. Either way
a worker is never a ``fork`` of this process, which may be running threads. A
worker still costs something to start, so an emit smaller than
:data:`MIN_ROWS_PER_JOB` rows per job is generated in this process instead.
A worker starts with the current directory off its import path, and it ends when
the process that started it ends, even one that was killed.

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
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core import kernel2
from flux_engine.core.errors import FluxError
from flux_engine.core.rng import kernel_context
from flux_engine.core.seedtree import SeedTreeRoot
from flux_engine.generator.associations import pairings_of, seed_pairings
from flux_engine.generator.dates import DatesTally
from flux_engine.generator.history import hand_chains, seed_chains
from flux_engine.generator.projections import chain_of
from flux_engine.generator.relations import history_pick_of
from flux_engine.generator.rows import GeneratorPlan, generate_row_slice, written_rows
from flux_engine.generator.rules import new_tally
from flux_engine.seam.contract import CompiledExpose
from flux_engine.seam.emit import frame_rows
from flux_engine.stages._prewarm import (
    children_keep_the_working_directory_off_their_path,
    children_without_the_working_directory,
    cpu_jobs,
    pool_context,
)
from flux_stats.integers import is_int_at_least

__all__ = [
    "MIN_ROWS_PER_JOB",
    "FramedEmit",
    "FramedPool",
    "cpu_jobs",
    "framed_pool",
    "generate_framed",
    "map_in_pool",
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
    if not is_int_at_least(jobs, 1):
        raise ValueError(f"jobs must be a positive integer, got {jobs!r}")
    return jobs


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


_children_keep_the_working_directory_off_their_path = (
    children_keep_the_working_directory_off_their_path
)
_children_without_the_working_directory = children_without_the_working_directory


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

    A run's pool (:func:`framed_pool`) takes its workers from this one count,
    so the pools of runs that share a process never add up to more than one
    worker per CPU.
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
    """What a worker hands back for one slice: its framed lines, or where it stopped.

    The rows themselves stay in the worker unless the caller asks for them.
    Nothing downstream of the lines reads them (the digest, the file, a sink and
    the verdicts are all over the lines), and carrying a few million Decimal and
    date objects across a pipe, to be unpickled by one process, was the serial
    part of a parallel run.

    A refusal travels as its code and detail and is raised again from the
    parent's own catalog entry, so the parent's error is the very code
    object one process would have raised.
    """

    rows: Rows = ()
    lines: Tuple[bytes, ...] = ()
    #: What the slice's rows came to for the facts of ``lifetimes`` and ``within`` that carry a
    #: target, when the emit has one (ADR 0024 section 7); the join adds the slices' counts.
    dates: Optional[DatesTally] = None
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


#: A worker's copy of what it serves, set once by :func:`_start_worker`.
_WORKER: Dict[str, Any] = {}

#: One emit as a worker holds it: its plan and its compiled expose.
_EmitJob = Tuple[GeneratorPlan, CompiledExpose]


def _start_worker(
    emits: Mapping[str, _EmitJob],
    root: SeedTreeRoot,
    pairings: Mapping[Any, Any],
    chains: Mapping[Any, Any],
) -> None:
    _exit_with_parent()
    # The pipeline evaluates a run in the kernel's decimal context; a new
    # process starts in Python's default one, so install the kernel's here.
    setcontext(kernel_context())
    # An association that holds each pair once is worked out once, by the process that starts
    # the pool, and every worker holds it as that process does (ADR 0039).
    seed_pairings(pairings)
    # A history's version layout reads every entity, so it too is worked out once, here, and a
    # worker holds it as this process does (ADR 0017, the amendment of 2026-11-28).
    seed_chains(chains)
    _WORKER.update(emits=emits, root=root)


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


def _run_slice(task: Tuple[str, int, int, bool]) -> _SliceOutcome:
    """Generate, render and frame rows ``start`` to ``stop - 1`` of one emit, in a worker."""
    expose_id, start, stop, keep_rows = task
    plan, expose = _WORKER["emits"][expose_id]
    try:
        if plan.counter_based:
            # This process computes kernel 0.2.0's binary64 draws too (FLX-5050).
            kernel2.require_subnormals()
        tally = new_tally(plan)
        rows = written_rows(
            plan, generate_row_slice(plan, start, stop, root=_WORKER["root"], tally=tally)
        )
    except FluxError as error:
        return _SliceOutcome(generation_refusal=(error.code.code, error.detail))
    try:
        # The slice a column at a time; a slice that cannot be is rendered row by row, so a
        # refusal is the row renderer's own, naming its row.
        lines = frame_rows(rows, expose, first_row=start)
    except FluxError as error:
        return _SliceOutcome(framing_refusal=(error.code.code, error.detail))
    return _SliceOutcome(rows=rows if keep_rows else (), lines=tuple(lines), dates=tally)


def _pairings(emits: Mapping[str, _EmitJob], root: SeedTreeRoot) -> Dict[Any, Any]:
    """The distinct association pairings the emits read, worked out in this process (ADR 0039)."""
    return pairings_of([plan for plan, _ in emits.values()], root)


def _chains(emits: Mapping[str, _EmitJob], root: SeedTreeRoot) -> Dict[Any, Any]:
    """The version layout of every history the emits read, worked out in this process, one build each.

    Each is the layout its own plan's history returned, so a layout this process holds for another
    run, or for another key, is never handed over.
    """
    layouts = []
    for plan, _ in emits.values():
        histories = [plan] if plan.projection is None else [plan, plan.projection.history]
        for history in histories:
            if history_pick_of(history) is not None:
                layouts.append(chain_of(history, root))
    return hand_chains(layouts)


def _context() -> Any:
    """The multiprocessing context workers start from (see :func:`~flux_engine.stages._prewarm.pool_context`)."""
    return pool_context()


@dataclass(frozen=True)
class FramedEmit:
    """One emit's slices, in flight in a :class:`FramedPool`; :meth:`join` waits for them."""

    expose_id: str
    _pending: Any
    #: The slices' counts added up, set by :meth:`join`; ``None`` for an emit with no date target.
    dates: Optional[DatesTally] = None

    def join(self) -> Tuple[Rows, List[bytes]]:
        """The emit's rows (when asked for) and framed lines, in row order; a refusal is raised again."""
        from concurrent.futures.process import BrokenProcessPool

        try:
            outcomes = list(self._pending)
        except BrokenProcessPool as error:
            raise RuntimeError(
                f"a worker process generating {self.expose_id!r} stopped before it "
                "finished: it could not start, ran out of memory or was killed. A script "
                'that starts the run needs an `if __name__ == "__main__":` guard, because '
                "each worker imports the script again. jobs=1 generates the rows in this "
                "process."
            ) from error
        refusal = _first_refusal(outcomes)
        if refusal is not None:
            code, detail = refusal
            raise FluxError(code, detail)
        rows = tuple(row for outcome in outcomes for row in outcome.rows)
        counted = [outcome.dates for outcome in outcomes if outcome.dates is not None]
        if counted:
            total = DatesTally()
            for tally in counted:
                total.merge(tally)
            object.__setattr__(self, "dates", total)
        return rows, [line for outcome in outcomes for line in outcome.lines]


class FramedPool:
    """The one pool a run generates its emits with.

    Built from every emit's plan, so each worker holds them all from the start
    (one transfer per worker, through the initializer); :meth:`submit` cuts an
    emit's rows into slices and hands them to the pool without waiting, so the
    slices of several emits are in flight together.
    """

    def __init__(self, emits: Mapping[str, _EmitJob], *, root: SeedTreeRoot, workers: int) -> None:
        # Imported here, so that a one-job run and `fluxsim --help` never load them.
        from concurrent.futures import ProcessPoolExecutor

        self._emits = dict(emits)
        self._workers = workers
        with children_without_the_working_directory():
            # The pool starts its resource tracker when it is built; its workers start
            # as the first slices are submitted, and so does the forkserver, so
            # `submit` runs under the same guard.
            self._pool = ProcessPoolExecutor(
                max_workers=workers,
                mp_context=_context(),
                initializer=_start_worker,
                initargs=(
                    self._emits,
                    root,
                    _pairings(self._emits, root),
                    _chains(self._emits, root),
                ),
            )

    def submit(
        self,
        expose_id: str,
        *,
        keep_rows: bool = False,
        rows: Optional[Tuple[int, int]] = None,
    ) -> FramedEmit:
        """Start generating *expose_id*'s rows; returns what joins them.

        The workers keep the rows themselves unless *keep_rows* asks for them back.
        *rows* is the ``[start, stop)`` range to make, all of the emit's when ``None``.
        """
        plan, _ = self._emits[expose_id]
        first, last = (0, plan.rows) if rows is None else rows
        slices = [
            (first + start, first + stop)
            for start, stop in _slices(last - first, self._workers * _SLICES_PER_WORKER)
        ]
        with children_without_the_working_directory():
            pending = self._pool.map(
                _run_slice, [(expose_id, start, stop, keep_rows) for start, stop in slices]
            )
        return FramedEmit(expose_id, pending)

    def close(self, *, failed: bool = False) -> None:
        """Shut the pool down, waiting for the slices a worker is running.

        After a failure the slices not yet started are cancelled and the workers
        still running one finish it (or die with it) before this returns: the
        caller then gives their share of the budget back
        (:func:`reserve_workers`), and a worker still busy must not be counted
        as free.
        """
        self._pool.shutdown(wait=True, cancel_futures=failed)


@contextmanager
def framed_pool(
    emits: Mapping[str, _EmitJob], *, root: SeedTreeRoot, workers: int
) -> Iterator[FramedPool]:
    """``with framed_pool(...) as pool:`` one pool for the run; closed however the block ends."""
    pool = FramedPool(emits, root=root, workers=workers)
    failed = True
    try:
        yield pool
        failed = False
    finally:
        pool.close(failed=failed)


def map_in_pool(
    function: Callable[[Any], Any],
    tasks: Sequence[Any],
    jobs: int,
    *,
    initializer: Optional[Callable[[], None]] = None,
) -> List[Any]:
    """Map *function* over *tasks* in the run's kind of pool; the results are in the order of *tasks*.

    ``verify`` reads its emits with this. The workers come from the process's budget
    (:func:`reserve_workers`) and start as a generation's do, from the forkserver with the
    engine loaded and the working directory off their path; with fewer than two workers
    to be had, or fewer than two tasks, the tasks run here in order. *function* and
    *initializer* are module-level callables, since a worker finds them by name.
    """
    from concurrent.futures import ProcessPoolExecutor

    wanted = min(require_jobs(jobs), cpu_jobs(), _platform_max_workers(), len(tasks))
    if wanted <= 1 or not _children_keep_the_working_directory_off_their_path():
        return [function(task) for task in tasks]
    with reserve_workers(wanted) as workers:
        if workers <= 1:
            return [function(task) for task in tasks]
        with children_without_the_working_directory():
            pool = ProcessPoolExecutor(
                max_workers=workers, mp_context=_context(), initializer=initializer
            )
        failed = True
        try:
            with children_without_the_working_directory():
                results = list(pool.map(function, tasks))
            failed = False
            return results
        finally:
            pool.shutdown(wait=True, cancel_futures=failed)


def generate_framed(
    plan: GeneratorPlan, *, root: SeedTreeRoot, expose: CompiledExpose, workers: int
) -> Tuple[Rows, List[bytes]]:
    """*plan*'s rows and their framed lines, made by *workers* worker processes.

    The same rows, in the same order, and the same lines
    (``row_line(render_row(row))``) as one process makes. A refusal from a
    worker is raised again here with its code and message; the exception it
    was chained from, if any, stays in the worker. One emit in a pool of its
    own: a run with several emits uses :func:`framed_pool`.
    """
    with framed_pool({expose.expose_id: (plan, expose)}, root=root, workers=workers) as pool:
        return pool.submit(expose.expose_id, keep_rows=True).join()
