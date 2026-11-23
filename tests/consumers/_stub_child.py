# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A test wrapper around the query process: ``python -I _stub_child.py MODE [ARG]``.

It loads the real ``flux_engine.consumers._child``, replaces one of its functions according to MODE,
and runs its ``main``. The production child has no hook: every fault a test needs is put in here, on
the test's side of the command line.

* ``raw``: a count runs its text as it is on the locked connection, with no wrapper, so a test can
  show that the locks, not the wrapper, refuse a statement.
* ``bare``: the count text is the text itself, so the one-SELECT rule is what refuses it.
* ``crash``: a request whose text holds ``CRASH`` ends the process with exit status 7, and one that
  holds ``KILL`` ends it with SIGKILL.
* ``hang``: a request whose text holds ``HANG`` sleeps for ten minutes.
* ``files``: a request opens files until the system refuses, and answers how many it opened.
* ``write``: a request writes a byte to the file named by the next argument and answers what happened.
* ``crash_no_restart``: ``crash``, and the first start creates the marker file named by the next argument;
  any later start leaves at once with exit status 9.
* ``missing``: duckdb cannot be imported.
"""

from __future__ import annotations

import os
import signal
import sys
import time

from flux_engine.consumers import _child

mode = sys.argv[1]
original = _child._run_request


def _raw(connection, duckdb, text, group_by):  # type: ignore[no-untyped-def]
    connection.execute(text).fetchall()
    return 1, None


def _crash(connection, duckdb, request, seconds):  # type: ignore[no-untyped-def]
    if "CRASH" in request["text"]:
        os._exit(7)
    if "KILL" in request["text"]:
        os.kill(os.getpid(), signal.SIGKILL)
    return original(connection, duckdb, request, seconds)


def _hang(connection, duckdb, request, seconds):  # type: ignore[no-untyped-def]
    if "HANG" in request["text"]:
        time.sleep(600)
    return original(connection, duckdb, request, seconds)


def _answer(request, status, reason):  # type: ignore[no-untyped-def]
    return _child._result(request["id"], status, reason)


def _files(connection, duckdb, request, seconds):  # type: ignore[no-untyped-def]
    held = []
    try:
        for _ in range(64):
            held.append(os.open("/etc/hosts", os.O_RDONLY))
    except OSError as error:
        return _answer(request, "error", f"opened {len(held)} files before: {error.strerror}")
    return _answer(request, "counted", f"opened {len(held)} files")


def _write(connection, duckdb, request, seconds):  # type: ignore[no-untyped-def]
    try:
        with open(sys.argv[2], "wb") as stream:
            stream.write(b"x")
    except OSError as error:
        return _answer(request, "error", f"write refused: {error.strerror}")
    return _answer(request, "counted", "wrote a byte")


if mode == "raw":
    _child._count = _raw
elif mode == "bare":
    _child._count_text = lambda text, group_by: text
elif mode == "crash":
    _child._run_request = _crash
elif mode == "hang":
    _child._run_request = _hang
elif mode == "files":
    _child._run_request = _files
elif mode == "write":
    _child._run_request = _write
elif mode == "crash_no_restart":
    marker = sys.argv[2]
    if os.path.exists(marker):
        os._exit(9)
    open(marker, "wb").close()
    _child._run_request = _crash
elif mode == "missing":
    sys.modules["duckdb"] = None  # type: ignore[assignment]
else:
    raise SystemExit(f"unknown mode {mode!r}")

raise SystemExit(_child.main())
