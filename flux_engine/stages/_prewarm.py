# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Start the forkserver early, so its preload overlaps the run's own serial work.

A worker pool starts its forkserver the first time a slice is submitted, and the
first slice then waits for the server to import everything a worker needs, about
0.3 s. The run has serial work of its own ahead of that point (loading the
bundle, compiling it, the simulation), so the server can be started before any of
it: :func:`start_forkserver_early` spawns the server and returns at once, the
server imports its preload beside the parent, and the pool, when it comes, finds
the server running and the import done or nearly done. CPython's
``multiprocessing.forkserver`` already works this way underneath: ``ensure_running``
returns when the server process has been spawned, and a connection made while the
server still imports its preload waits in the listening socket's queue. Prior art
for the shape: joblib and loky start their workers once and reuse them; this
borrows the "start the expensive part first" half of that, and nothing else.

This module imports the standard library only, and nothing from the engine, so that
the command line can call it before the engine's own imports and ``fluxsim`` pays
no more to import it than it costs to read the file.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator, List, Optional

#: The module a forkserver imports once, so that a worker is a fork of an interpreter that
#: holds what it needs. The smallest module with the worker's entry points: it brings the
#: kernel, the generator's row code and the seam's emit code, and no stage.
PRELOAD_MODULE = "flux_engine.stages._parallel"

#: Imported before and after the preload: the first ignores ``SIGINT`` while the server imports,
#: the last puts the interpreter's handler back (``multiprocessing`` ignores it only after the preload).
SIGINT_GUARD_MODULE = "flux_engine.stages._sigint_guard"
SIGINT_RESTORE_MODULE = "flux_engine.stages._sigint_restore"

_SPAWN_LOCK = threading.Lock()


def preload_modules(*between: str) -> List[str]:
    """The modules the forkserver imports, in order: the guard, any of *between*, the preload, the restore."""
    return [SIGINT_GUARD_MODULE, *between, PRELOAD_MODULE, SIGINT_RESTORE_MODULE]


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


def children_keep_the_working_directory_off_their_path() -> bool:
    """Whether a spawned worker honours ``PYTHONSAFEPATH``.

    A child gets this interpreter's flags. Under ``-E`` it ignores the
    environment, so the variable that keeps the current directory off its
    path would be ignored too, unless ``-P`` (or ``-I``, which implies both)
    keeps it off anyway. In that one case the rows are made in this process.
    """
    return bool(sys.flags.safe_path) or not sys.flags.ignore_environment


@contextmanager
def children_without_the_working_directory() -> Iterator[None]:
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


def pool_context() -> Any:
    """The multiprocessing context workers start from: a forkserver with the engine loaded.

    The server imports :data:`PRELOAD_MODULE`, and with it what a worker needs, once, with
    ``SIGINT`` ignored while it does (:func:`preload_modules`);
    each worker is then a fork of that interpreter, which starts in milliseconds where
    a ``spawn`` imports the engine again. The preload applies only to the server's first
    start in a process, and the server is started inside
    :func:`children_without_the_working_directory`, so it, and every worker it forks,
    keeps the current directory off its import path.
    """
    import multiprocessing

    if sys.platform != "win32" and "forkserver" in multiprocessing.get_all_start_methods():
        context = multiprocessing.get_context("forkserver")
        context.set_forkserver_preload(preload_modules())
        return context
    return multiprocessing.get_context("spawn")


def start_forkserver_early(jobs: Optional[int]) -> bool:
    """Spawn the forkserver now if a run at *jobs* may want a pool; ``True`` if one is running.

    Nothing starts for one job, for a machine with one CPU, where workers would not honour
    ``PYTHONSAFEPATH`` (the rows are then made in this process), or where workers are spawned
    rather than forked from a server. The call reserves no worker from the process's budget
    and changes which workers a run uses: the pool is built later, exactly as before, and
    finds the server already running. The server ends when this process ends, as it does
    when the pool starts it. A failure to start it is swallowed: the pool starts it again,
    on its own, and reports whatever is wrong then.
    """
    if not isinstance(jobs, int) or isinstance(jobs, bool) or jobs < 2:
        return False
    if cpu_jobs() < 2 or not children_keep_the_working_directory_off_their_path():
        return False
    try:
        context = pool_context()
        if context.get_start_method() != "forkserver":
            return False
        from multiprocessing import forkserver

        with children_without_the_working_directory():
            forkserver.ensure_running()
    except Exception:  # pragma: no cover - the pool reports it when it needs the server
        return False
    return True
