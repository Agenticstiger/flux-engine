# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Counts over a run's tables, in a locked DuckDB in a process of their own (ADR 0028 section 3).

The parent, this module, never imports DuckDB: it starts the child
(``python -I -m flux_engine.consumers._child``, see :mod:`flux_engine.consumers._child`), sends it the
run's tables as Arrow IPC bytes, asks it for counts and stops it. Everything crosses the pipe as the
frames of :mod:`flux_engine.consumers._frames`, a JSON header and, for a table, the IPC stream, and
nothing is pickled. A message is bounded in size on both sides.

**The child's world.** It is started with ``env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "TZ": "UTC"}``
and nothing else (DuckDB without its ICU extension reads a time zone through the process's own, so
the child's is UTC and a rate never depends on the machine's zone), no stderr, and no descriptor but its two pipes (``close_fds``), so no variable or
credential of the parent reaches it. Its first reply lists the *keys* of the environment it sees,
never the values, and the limits it set; a test pins both.

**Time.** A query that passes ``limits.seconds`` is interrupted inside the child and takes the status
``timedOut``. The parent keeps a clock of its own for the whole batch, the sum of the limits plus
``grace_seconds`` (30), and ends the child, ``terminate`` then ``kill``, when the clock runs out and
raises ``FLX-7133``. The clock runs in a thread, so a child that has stopped reading is ended too.

**Crashes.** When the child dies (end of its stdout, a signal, a resource limit), the request in flight
takes the status ``crashed`` and is not run again. A fresh child is started under the same locks, the
tables are sent again and the remaining requests run. A fresh child that cannot start leaves the
remaining requests ``crashed``.

**What a result holds.** Per request: ``rows``, ``unit_rows`` and, with a ``group_by``, the count of
each group on both sides, the status, and the duckdb version beside the sqlglot version of the text.
The rate arithmetic is :mod:`flux_engine.consumers.rates`. The limits come in already lowered or raised;
this module never reads a bundle.

**More than one batch.** A calibration counts the rows of many runs, one for each candidate, in one
process: :meth:`QueryRunner.load` replaces the tables (the child drops the old ones first) and
:meth:`QueryRunner.count` answers a batch of requests, as many times as it is called. The CPU the child
may spend is that of ``batches`` batches, so a fit that counts a hundred runs is not stopped by the limit
that fits one. ``run`` is one batch over the requests the runner was made with.

The statuses of a run are ``counted``, ``timedOut``, ``outOfMemory``, ``error`` and ``crashed``.
``error`` is the status of a text DuckDB refused at execution (or that the child refused before it ran):
its reason holds the class name of the engine's exception and a sentence of ours, or the named reason a
text was refused, and never the engine's own message, which can quote a value of the run and so never
crosses the pipe. ``crashed`` is the status of a query whose process died; ``error`` leaves the process
running, and the other queries run as before.
"""

from __future__ import annotations

import importlib
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from types import MappingProxyType, TracebackType
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
)

from flux_engine.consumers import _frames
from flux_engine.consumers._child import connect_settings
from flux_engine.consumers._frames import FrameError
from flux_engine.consumers.rates import ordered_counts
from flux_engine.consumers.runner_errors import FLX_7131, FLX_7132, FLX_7133, FLX_7134
from flux_engine.core.errors import FluxError
from flux_engine.seam.consumers import Limits
from flux_engine.sinks._optional import is_available

#: A ``pyarrow.Table``. The name is a stand-in: only the sinks may import pyarrow, even for an annotation.
ArrowTable = Any

__all__ = [
    "CPU_MARGIN_SECONDS",
    "GRACE_SECONDS",
    "START_SECONDS",
    "BatchResult",
    "CountRequest",
    "QueryCounts",
    "QueryRunner",
    "Versions",
    "run_counts",
]

#: The seconds the whole batch may pass the sum of its queries' limits before the child is ended.
GRACE_SECONDS = 30

#: The seconds the child has to start, answer its first message and take its tables.
START_SECONDS = 30

#: The CPU the child may spend beyond the sum of the queries' limits: interpreter start and imports.
CPU_MARGIN_SECONDS = 30

#: Seconds a child is given to leave after it is asked to, and after SIGTERM, before the next step.
_LEAVE_SECONDS = 5
_TERMINATE_SECONDS = 2

#: The environment of the child: these two keys and nothing the parent holds.
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "TZ": "UTC"}

_STATUSES = frozenset({"counted", "timedOut", "outOfMemory", "error"})
_REASON_LIMIT = 300

#: The most CPU seconds the child accepts in its first message (see ``_child.main``).
_MOST_CPU_SECONDS = 10_000_000


@dataclass(frozen=True)
class CountRequest:
    """One query to count: the canonical text, and the unit whose rows its rate counts.

    ``text`` and ``unit_text`` are canonical SQL texts as strings (the checker writes them), and
    ``group_by`` a column of both. ``sqlglot_version`` is the version that wrote ``text``; it is
    carried to the result, never sent to the child.
    """

    id: str
    text: str
    unit_text: Optional[str] = None
    group_by: Optional[str] = None
    sqlglot_version: str = ""


@dataclass(frozen=True)
class QueryCounts:
    """What one request counted, or why it did not.

    ``rows`` and ``unit_rows`` are whole numbers when ``status`` is ``counted`` (``unit_rows`` is
    ``None`` for a request without a unit text). ``row_groups`` and ``unit_groups`` are
    ``(level or None, count)`` pairs, the null group first, for a request with a ``group_by``.
    """

    id: str
    status: str
    reason: Optional[str]
    rows: Optional[int]
    unit_rows: Optional[int]
    row_groups: Optional[Tuple[Tuple[Optional[str], int], ...]]
    unit_groups: Optional[Tuple[Tuple[Optional[str], int], ...]]
    duckdb_version: str
    sqlglot_version: str


@dataclass(frozen=True)
class Versions:
    """The versions the child reported: what the scorecard records."""

    duckdb: str
    pyarrow: str
    python: str


@dataclass(frozen=True)
class BatchResult:
    """The answers of one batch, in the order of the requests, with the versions and the restarts."""

    queries: Tuple[QueryCounts, ...]
    versions: Versions
    restarts: int


class _Gone(Exception):
    """The child ended, closed its pipe or broke the protocol. The text says how."""


class _Watchdog:
    """Runs *on_expiry* in a thread when *seconds* pass, unless cancelled first."""

    def __init__(self, seconds: float, on_expiry: Callable[[], None]) -> None:
        self._timer = threading.Timer(seconds, on_expiry)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def cancel(self) -> None:
        self._timer.cancel()


def _describe(returncode: Optional[int]) -> str:
    if returncode is None:
        return "it was still running"
    if returncode < 0:
        try:
            return f"signal {signal.Signals(-returncode).name}"
        except ValueError:
            return f"signal {-returncode}"
    return f"exit status {returncode}"


class _Child:
    """One query process and its pipes."""

    def __init__(self, command: Sequence[str]) -> None:
        self.process = subprocess.Popen(
            list(command),
            env=dict(_ENVIRONMENT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def put(self, header: bytes, payload: Optional[bytes] = None) -> None:
        stream = self.process.stdin
        assert stream is not None
        try:
            _frames.write_frame(stream, header, _frames.HEADER_LIMIT)
            if payload is not None:
                _frames.write_frame(stream, payload, _frames.TABLE_LIMIT)
        except (OSError, ValueError) as error:  # a closed pipe is a child that is gone
            raise _Gone(self._how_it_ended()) from error

    def take(self) -> Dict[str, Any]:
        stream = self.process.stdout
        assert stream is not None
        try:
            return _frames.read_header(stream, _frames.REPLY_LIMIT)
        except EOFError as error:
            raise _Gone(self._how_it_ended()) from error
        except FrameError as error:
            self.end()
            raise _Gone(f"it broke the protocol: {error}") from error
        except (OSError, ValueError) as error:
            raise _Gone(self._how_it_ended()) from error

    def _how_it_ended(self) -> str:
        try:
            returncode: Optional[int] = self.process.wait(timeout=_TERMINATE_SECONDS)
        except subprocess.TimeoutExpired:
            returncode = None
        if returncode is None:
            self.end()
            return "it closed its pipe and was ended"
        return f"it ended with {_describe(returncode)}"

    def end(self) -> None:
        """Stop the process now: terminate, then kill. Safe to call from any thread, and twice."""
        process = self.process
        if process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=_TERMINATE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_LEAVE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):  # already gone
            pass

    def stop(self, farewell: bytes) -> None:
        """Ask the process to leave, then end it if it does not, and close both pipes."""
        process = self.process
        try:
            if process.poll() is None:
                try:
                    self.put(farewell)
                    assert process.stdin is not None
                    process.stdin.close()
                    process.wait(timeout=_LEAVE_SECONDS)
                except (_Gone, OSError, subprocess.TimeoutExpired):
                    pass
            self.end()
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


def _clean_reason(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _Gone("it sent a reason that is not text")
    return "".join(ch if ch.isprintable() else " " for ch in value)[:_REASON_LIMIT]


def _count_of(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise _Gone("it sent a count that is not a whole number")
    return value


def _groups_of(value: Any) -> Tuple[Tuple[Optional[str], int], ...]:
    if not isinstance(value, list):
        raise _Gone("it sent groups that are not a list")
    pairs: List[Tuple[Optional[str], int]] = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 2:
            raise _Gone("it sent a group that is not a pair")
        key, count = entry
        if key is not None and not isinstance(key, str):
            raise _Gone("it sent a group that is not text")
        pairs.append((key, _count_of(count)))
    try:
        return ordered_counts(pairs)
    except ValueError as error:
        raise _Gone("it counted a group twice") from error


def _hello_of(reply: Dict[str, Any], memory: int) -> Mapping[str, Any]:
    if reply.get("op") == "missing":
        raise FluxError(
            FLX_7131,
            f"consumer queries need {reply.get('module')}, which the query process could not "
            "import: pip install 'flux-sim-engine[sql]'",
        )
    if reply.get("op") != "hello":
        raise _Gone("its first message is not a hello")
    for key in ("duckdb", "pyarrow", "python"):
        if not isinstance(reply.get(key), str):
            raise _Gone(f"its hello has no {key} version")
    if not isinstance(reply.get("limits"), dict):
        raise _Gone("its hello does not report its limits")
    if reply.get("settings") != connect_settings(memory):
        raise _Gone("its hello does not report the settings it was asked to connect with")
    environment = reply.get("environment")
    if not isinstance(environment, list) or not all(isinstance(key, str) for key in environment):
        raise _Gone("its hello does not list its environment")
    return MappingProxyType(dict(reply))


class QueryRunner:
    """A context manager: starts one query process, counts *requests* in it, stops it.

    ``tables`` maps each ``exposeId`` to its Arrow table; ``limits`` are the effective limits. The
    process starts on ``__enter__`` and stops on ``__exit__``, always. ``run`` answers every request
    once, in order. *command* replaces the child's command line, for a test that wraps the child; no
    other setting changes what the child is.
    """

    def __init__(
        self,
        tables: Mapping[str, ArrowTable],
        requests: Sequence[CountRequest],
        limits: Limits,
        *,
        grace_seconds: int = GRACE_SECONDS,
        start_seconds: int = START_SECONDS,
        command: Optional[Sequence[str]] = None,
        batches: int = 1,
    ) -> None:
        self._requests = tuple(requests)
        self._limits = limits
        self._batches = max(1, batches)
        self._grace = grace_seconds
        self._start_seconds = start_seconds
        self._command = tuple(command or _child_command())
        self._tables = _serialize(tables)
        self._headers = tuple(_count_header(request) for request in self._requests)
        self._child: Optional[_Child] = None
        self._expired = threading.Event()
        self._hello: Optional[Mapping[str, Any]] = None
        self._ran = False
        self._clock = 0

    # -- the context manager

    def __enter__(self) -> "QueryRunner":
        watchdog = _Watchdog(self._start_seconds, self._expire)
        watchdog.start()
        try:
            self._start()
        except _Gone as gone:
            self._stop()
            if self._expired.is_set():
                raise FluxError(
                    FLX_7133, f"the query process did not start in {self._start_seconds} seconds"
                ) from gone
            raise FluxError(FLX_7134, f"the query process did not start: {gone}") from gone
        except BaseException:
            self._stop()
            raise
        finally:
            watchdog.cancel()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self._stop()

    @property
    def hello(self) -> Mapping[str, Any]:
        """The child's first message: versions, the limits it set, its settings, its environment keys."""
        if self._hello is None:
            raise RuntimeError("the query process has not started")
        return self._hello

    # -- the child

    def _expire(self) -> None:
        self._expired.set()
        child = self._child
        if child is not None:
            child.end()

    def _stop(self) -> None:
        child, self._child = self._child, None
        if child is not None:
            child.stop(_frames.encode_header({"op": "stop"}))

    def _start(self) -> None:
        """Start a child under the locks and send it the tables. ``_Gone`` when it will not take them."""
        if self._expired.is_set():
            raise _Gone("the clock ran out")
        try:
            child = _Child(self._command)
        except OSError as error:
            raise _Gone(f"it could not be started: {type(error).__name__}") from error
        self._child = child
        budget = min(
            self._batches * max(1, len(self._requests)) * self._limits.seconds + CPU_MARGIN_SECONDS,
            _MOST_CPU_SECONDS,
        )
        child.put(
            _frames.encode_header(
                {
                    "op": "init",
                    "seconds": self._limits.seconds,
                    "memoryBytes": self._limits.memory,
                    "cpuSeconds": budget,
                }
            )
        )
        self._hello = _hello_of(child.take(), self._limits.memory)
        self._send_tables(child)

    # -- the batch

    def run(self) -> BatchResult:
        """Count every request, in order. ``FLX-7133`` when the batch passes its clock."""
        if self._child is None or self._ran:
            raise RuntimeError("run once, inside the with block")
        self._ran = True
        return self._count_batch()

    def load(self, tables: Mapping[str, ArrowTable]) -> None:
        """Replace the tables the child counts over with *tables*; the process is kept.

        The child drops what it was sent, then takes each table. A child that has died is replaced by
        a fresh one under the same locks, which is sent the new tables only.
        """
        if self._child is None:
            raise RuntimeError("load inside the with block")
        self._tables = _serialize(tables)
        child = self._child
        try:
            child.put(_frames.encode_header({"op": "clear"}))
            self._expect_ok(child.take(), "clear the tables")
            self._send_tables(child)
        except _Gone:
            self._stop()
            watchdog = _Watchdog(self._start_seconds, self._expire)
            watchdog.start()
            try:
                self._start()
            except _Gone as gone:
                self._stop()
                raise FluxError(
                    FLX_7134, f"the query process did not start again: {gone}"
                ) from gone
            finally:
                watchdog.cancel()

    def count(self, requests: Sequence[CountRequest]) -> BatchResult:
        """Count *requests* over the tables last loaded, in order; callable again for the next batch."""
        if self._child is None:
            raise RuntimeError("count inside the with block")
        self._requests = tuple(requests)
        self._headers = tuple(_count_header(request) for request in self._requests)
        return self._count_batch()

    def _count_batch(self) -> BatchResult:
        self._clock = len(self._requests) * self._limits.seconds + self._grace
        watchdog = _Watchdog(self._clock, self._expire)
        watchdog.start()
        results: List[QueryCounts] = []
        restarts = 0
        try:
            for position, request in enumerate(self._requests):
                try:
                    results.append(self._ask(position, request))
                    continue
                except _Gone as gone:
                    self._raise_if_expired(len(results))
                    results.append(self._crashed(request, str(gone)))
                restarts += self._restart(results, position + 1)
                if len(results) == len(self._requests):
                    break
        finally:
            watchdog.cancel()
        return BatchResult(tuple(results), self._versions(), restarts)

    @staticmethod
    def _expect_ok(reply: Dict[str, Any], what: str) -> None:
        if reply.get("op") != "ok":
            raise _Gone(f"it would not {what}: {_clean_reason(reply.get('reason'))}")

    def _send_tables(self, child: "_Child") -> None:
        for expose_id, payload in self._tables:
            child.put(
                _frames.encode_header({"op": "table", "exposeId": expose_id}),
                payload,
            )
            reply = child.take()
            if reply.get("op") != "ok":
                raise _Gone(
                    f"it refused the table {expose_id!r}: {_clean_reason(reply.get('reason'))}"
                )

    def _raise_if_expired(self, answered: int) -> None:
        if self._expired.is_set():
            raise FluxError(
                FLX_7133,
                f"the batch passed {self._clock} seconds with {answered} of {len(self._requests)} "
                "queries answered; the query process was ended",
            )

    def _restart(self, results: List[QueryCounts], remaining_from: int) -> int:
        """A fresh child when requests remain. 1 for a restart, 0 when none was needed or possible."""
        if remaining_from >= len(self._requests):
            return 0
        self._stop()
        try:
            self._start()
        except _Gone as gone:
            self._raise_if_expired(len(results))
            self._stop()
            reason = f"the query process would not start again: {gone}"
            results.extend(
                self._crashed(request, reason) for request in self._requests[remaining_from:]
            )
            return 1
        return 1

    def _ask(self, position: int, request: CountRequest) -> QueryCounts:
        child = self._child
        if child is None:
            raise _Gone("there is no query process")
        child.put(self._headers[position])
        return self._answer(request, child.take())

    def _answer(self, request: CountRequest, reply: Dict[str, Any]) -> QueryCounts:
        if reply.get("op") == "refused":
            return self._counts(request, "error", _clean_reason(reply.get("reason")))
        if reply.get("op") != "result" or reply.get("id") != request.id:
            raise _Gone("it answered another request")
        status = reply.get("status")
        if status not in _STATUSES:
            raise _Gone("it sent a status that is not one it has")
        reason = _clean_reason(reply.get("reason"))
        if status != "counted":
            return self._counts(request, str(status), reason)
        grouped = request.group_by is not None
        has_unit = request.unit_text is not None
        rows = _count_of(reply.get("rows"))
        unit_rows = _count_of(reply.get("unitRows")) if has_unit else None
        if has_unit != (reply.get("unitRows") is not None):
            raise _Gone("it counted a unit it was not asked for, or none it was asked for")
        row_groups = _groups_of(reply.get("rowGroups")) if grouped else None
        unit_groups = _groups_of(reply.get("unitGroups")) if grouped and has_unit else None
        if (not grouped) and (
            reply.get("rowGroups") is not None or reply.get("unitGroups") is not None
        ):
            raise _Gone("it grouped a count it was not asked to group")
        return self._counts(request, "counted", None, rows, unit_rows, row_groups, unit_groups)

    def _counts(
        self,
        request: CountRequest,
        status: str,
        reason: Optional[str],
        rows: Optional[int] = None,
        unit_rows: Optional[int] = None,
        row_groups: Optional[Tuple[Tuple[Optional[str], int], ...]] = None,
        unit_groups: Optional[Tuple[Tuple[Optional[str], int], ...]] = None,
    ) -> QueryCounts:
        return QueryCounts(
            id=request.id,
            status=status,
            reason=reason,
            rows=rows,
            unit_rows=unit_rows,
            row_groups=row_groups,
            unit_groups=unit_groups,
            duckdb_version=self._versions().duckdb,
            sqlglot_version=request.sqlglot_version,
        )

    def _crashed(self, request: CountRequest, how: str) -> QueryCounts:
        child = self._child
        if child is not None:
            child.end()
        return self._counts(request, "crashed", f"the query process failed: {how}")

    def _versions(self) -> Versions:
        hello = self._hello
        if hello is None:
            return Versions("", "", "")
        return Versions(hello["duckdb"], hello["pyarrow"], hello["python"])


def _child_command() -> Tuple[str, ...]:
    return (sys.executable, "-I", "-m", "flux_engine.consumers._child")


def _count_header(request: CountRequest) -> bytes:
    try:
        data = _frames.encode_header(
            {
                "op": "count",
                "id": request.id,
                "text": request.text,
                "unitText": request.unit_text,
                "groupBy": request.group_by,
            }
        )
    except FrameError as error:
        raise FluxError(FLX_7132, f"the query {request.id!r} cannot be sent: {error}") from error
    if len(data) > _frames.HEADER_LIMIT:
        raise FluxError(
            FLX_7132,
            f"the query {request.id!r} is {len(data)} bytes of message; the bound is "
            f"{_frames.HEADER_LIMIT}",
        )
    return data


def _serialize(tables: Mapping[str, ArrowTable]) -> Tuple[Tuple[str, bytes], ...]:
    """Each table as Arrow IPC stream bytes, by exposeId."""
    if not tables:
        return ()
    if not is_available("pyarrow"):
        raise FluxError(
            FLX_7131,
            "consumer queries need pyarrow, which is not installed: pip install 'flux-sim-engine[sql]'",
        )
    pa = importlib.import_module("pyarrow")
    out: List[Tuple[str, bytes]] = []
    for expose_id in sorted(tables):
        table = tables[expose_id]
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        data: bytes = sink.getvalue().to_pybytes()
        if len(data) > _frames.TABLE_LIMIT:
            raise FluxError(
                FLX_7132,
                f"the table {expose_id!r} is {len(data)} bytes of Arrow; the bound is "
                f"{_frames.TABLE_LIMIT}",
            )
        out.append((expose_id, data))
    return tuple(out)


def run_counts(
    tables: Mapping[str, ArrowTable],
    requests: Sequence[CountRequest],
    limits: Limits,
    *,
    grace_seconds: int = GRACE_SECONDS,
    start_seconds: int = START_SECONDS,
    command: Optional[Sequence[str]] = None,
) -> BatchResult:
    """Start a query process, count *requests* in it and stop it. See :class:`QueryRunner`."""
    with QueryRunner(
        tables,
        requests,
        limits,
        grace_seconds=grace_seconds,
        start_seconds=start_seconds,
        command=command,
    ) as runner:
        return runner.run()
