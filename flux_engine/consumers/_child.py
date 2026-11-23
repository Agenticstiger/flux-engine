# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The query process: counts over a locked DuckDB (ADR 0028 section 3).

Started by :mod:`flux_engine.consumers.runner` as ``python -I -m flux_engine.consumers._child``, with an
environment of its own and its two pipes as the only descriptors it holds. It speaks the framing of
:mod:`flux_engine.consumers._frames` on stdin and stdout and nothing else: no pickle, and no row of a
result, only whole numbers and, for a grouped count, the levels the counts are grouped by.

In this order:

1. Read the ``init`` message (the limits). This needs the standard library only.
2. Lower the resource limits the platform enforces, **before** DuckDB is imported.
3. Import DuckDB and pyarrow, and open the in-memory database with every lock passed at connect.
4. Lower the number of files it may hold open to what it holds now plus 8, so it can open no new one.
5. Answer ``hello`` (versions, the limits it set, the keys of its environment) and then the
   messages ``table``, ``count``, ``clear`` and ``stop``. ``clear`` drops every table it was sent, so
   one process can count the rows of a second run (a calibration counts one run for each candidate).

pyarrow is imported with :func:`importlib.import_module`: the package that may import it is
:mod:`flux_engine.sinks`, and this process reads Arrow IPC bytes and nothing else of it.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from typing import IO, Any, Dict, List, Optional, Tuple

from flux_engine.consumers import _frames
from flux_engine.consumers._frames import FrameError

__all__ = ["MAX_GROUPS", "connect_settings", "main"]

#: The most groups one grouped count may return. A count with more is refused, not truncated.
MAX_GROUPS = 100_000

#: The room above the highest open descriptor the child keeps when it lowers its file limit.
_NOFILE_SPARE = 8

#: The address space the child may add to ``limits.memory``, for the interpreter, the libraries and
#: the tables it is sent. Applied only where the platform enforces ``RLIMIT_AS``.
_ADDRESS_SPACE_SPARE = 4 << 30

#: The statuses of a count, in the vocabulary of ADR 0028 section 4 where it has the word.
COUNTED = "counted"
TIMED_OUT = "timedOut"
OUT_OF_MEMORY = "outOfMemory"
ERROR = "error"


class _Refused(Exception):
    """A request the child will not run, with the reason it reports.

    The reason is a sentence of this module's own, never a part of an engine message.
    """

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason


def _engine_error(error: BaseException) -> str:
    """What crosses the pipe about an engine error: its class name and a sentence of ours.

    DuckDB's message is never sent. It can quote a value of the table (a conversion error does),
    and no value of the run leaves this process (ADR 0028 section 3).
    """
    return f"{type(error).__name__}: the engine refused the query"


def _whole(value: Any, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise _Refused(f"{name} is a whole number from {low} to {high}")
    return value


# ----------------------------------------------------------------------------- limits


def _lower(resource: Any, which: int, value: int) -> int:
    """Lower both limits of *which* to *value*, or leave one that is already lower. Returns the new one."""
    soft, hard = resource.getrlimit(which)
    for current in (soft, hard):
        if current != resource.RLIM_INFINITY and current < value:
            value = current
    resource.setrlimit(which, (value, value))
    return int(value)


def _highest_descriptor(limit: int) -> int:
    """The highest file descriptor this process holds open."""
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        return max((int(name) for name in names if name.isdigit()), default=2)
    highest = 2
    for descriptor in range(min(limit, 65_536)):
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        highest = descriptor
    return highest


def _lower_before_imports(seconds: int, memory: int, cpu: int) -> Dict[str, Optional[int]]:
    """A file may not grow, CPU time is capped, and so is the address space where it is enforced."""
    try:
        import resource
    except ImportError:  # no ``resource`` on this platform
        return {"fsize": None, "cpu": None, "as": None, "nofile": None}
    done: Dict[str, Optional[int]] = {"fsize": None, "cpu": None, "as": None, "nofile": None}
    done["fsize"] = _lower(resource, resource.RLIMIT_FSIZE, 0)
    done["cpu"] = _lower(resource, resource.RLIMIT_CPU, cpu)
    # macOS accepts RLIMIT_AS and does not enforce it, so a number there would claim a limit that
    # is not in force. The headroom above limits.memory covers the interpreter, duckdb and pyarrow
    # mappings, and the tables the child is sent; DuckDB's memory_limit does not count them.
    if sys.platform != "darwin" and hasattr(resource, "RLIMIT_AS"):
        done["as"] = _lower(resource, resource.RLIMIT_AS, memory + _ADDRESS_SPACE_SPARE)
    return done


def _lower_nofile(done: Dict[str, Optional[int]]) -> None:
    try:
        import resource
    except ImportError:
        return
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    ceiling = soft if soft != resource.RLIM_INFINITY else 65_536
    done["nofile"] = _lower(
        resource, resource.RLIMIT_NOFILE, _highest_descriptor(ceiling) + _NOFILE_SPARE
    )


# ----------------------------------------------------------------------------- database


def _memory_text(memory: int) -> str:
    return f"{memory >> 20}MiB" if memory % (1 << 20) == 0 else f"{memory}B"


def connect_settings(memory: int) -> Dict[str, Any]:
    """Exactly the settings ADR 0028 names, all passed when the connection opens."""
    return {
        "enable_external_access": False,
        "autoload_known_extensions": False,
        "autoinstall_known_extensions": False,
        "allow_community_extensions": False,
        "allow_unsigned_extensions": False,
        "allow_persistent_secrets": False,
        "python_enable_replacements": False,
        "threads": 1,
        "memory_limit": _memory_text(memory),
        "max_temp_directory_size": "0B",
        "lock_configuration": True,
    }


class _Deadline:
    """Interrupts the connection when a request passes its seconds.

    The interrupt is sent under the lock and only while the deadline is armed, so once
    :meth:`disarm` returns no interrupt is in flight or still to come, and a late timer cannot
    stop the next request.
    """

    def __init__(self, connection: Any, seconds: int) -> None:
        self._connection = connection
        self._seconds = seconds
        self._lock = threading.Lock()
        self._armed = False
        self._timer: Optional[threading.Timer] = None

    def arm(self) -> None:
        with self._lock:
            self._armed = True
            self._timer = threading.Timer(self._seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._armed:
                self._connection.interrupt()

    def disarm(self) -> None:
        with self._lock:
            self._armed = False
            if self._timer is not None:
                self._timer.cancel()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _count_text(text: str, group_by: Optional[str]) -> str:
    """The only SQL this process runs: a count written around a canonical text."""
    if group_by is None:
        return f"SELECT count(*) FROM ({text}) AS q"
    return (
        f"SELECT CAST(q.{_quote(group_by)} AS VARCHAR) AS g, count(*) "
        f"FROM ({text}) AS q GROUP BY g"
    )


Counted = Tuple[int, Optional[List[Tuple[Optional[str], int]]]]


def _count(connection: Any, duckdb: Any, text: str, group_by: Optional[str]) -> Counted:
    """Run one count: the total and, with *group_by*, the count of each group.

    The whole count text is split with DuckDB's own parser and runs only when it is exactly one
    SELECT. The statement that was parsed is the one executed, not the string again.
    """
    statements = connection.extract_statements(_count_text(text, group_by))
    if len(statements) != 1:
        raise _Refused(f"the text holds {len(statements)} statements; exactly one SELECT runs")
    statement = statements[0]
    if statement.type != duckdb.StatementType.SELECT:
        raise _Refused(f"the text is a {statement.type.name} statement; only a SELECT runs")
    cursor = connection.execute(statement)
    if group_by is None:
        (row,) = cursor.fetchall()
        return _number(row[0]), None
    rows = cursor.fetchmany(MAX_GROUPS + 1)
    if len(rows) > MAX_GROUPS:
        raise _Refused(f"the query has more than {MAX_GROUPS} groups")
    groups = [(_key(key), _number(count)) for key, count in rows]
    return sum(count for _key_, count in groups), groups


def _number(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise _Refused("a count is not a whole number")
    return value


def _key(value: Any) -> Optional[str]:
    if value is not None and type(value) is not str:
        raise _Refused("a group is not text")
    return value


def _result(request_id: str, status: str, reason: Optional[str]) -> Dict[str, Any]:
    return {
        "op": "result",
        "id": request_id,
        "status": status,
        "reason": reason,
        "rows": None,
        "unitRows": None,
        "rowGroups": None,
        "unitGroups": None,
    }


def _run_request(
    connection: Any, duckdb: Any, request: Dict[str, Any], seconds: int
) -> Dict[str, Any]:
    """Answer one ``count`` request. Never raises for a query: a failure is the answer's status."""
    request_id = request["id"]
    group_by = request["groupBy"]
    deadline = _Deadline(connection, seconds)
    deadline.arm()
    try:
        rows, row_groups = _count(connection, duckdb, request["text"], group_by)
        counted = _result(request_id, COUNTED, None)
        counted["rows"] = rows
        counted["rowGroups"] = None if row_groups is None else [list(pair) for pair in row_groups]
        if request["unitText"] is not None:
            unit, unit_groups = _count(connection, duckdb, request["unitText"], group_by)
            counted["unitRows"] = unit
            counted["unitGroups"] = (
                None if unit_groups is None else [list(pair) for pair in unit_groups]
            )
        return counted
    except _Refused as refusal:
        return _result(request_id, ERROR, refusal.reason)
    except duckdb.InterruptException:
        return _result(request_id, TIMED_OUT, f"the query passed {seconds} seconds")
    except duckdb.OutOfMemoryException:
        return _result(request_id, OUT_OF_MEMORY, "the query passed the memory limit")
    except duckdb.Error as error:
        return _result(request_id, ERROR, _engine_error(error))
    # A fault here is an answer, not a dead process.
    except Exception as error:  # noqa: BLE001
        return _result(request_id, ERROR, f"internal error: {type(error).__name__}")
    finally:
        deadline.disarm()


# ----------------------------------------------------------------------------- messages


def _reply(out: IO[bytes], document: Dict[str, Any]) -> None:
    data = _frames.encode_header(document)
    if len(data) > _frames.REPLY_LIMIT:
        if document.get("op") != "result":
            raise FrameError("a reply passes its bound")
        document = _result(str(document["id"]), ERROR, "the result passes the size of a reply")
        data = _frames.encode_header(document)
    _frames.write_frame(out, data, _frames.REPLY_LIMIT)


def _register(connection: Any, pa: Any, ipc: Any, request: Dict[str, Any], payload: bytes) -> None:
    expose_id = request["exposeId"]
    if type(expose_id) is not str or not expose_id:
        raise _Refused("a table is named by its exposeId")
    reader = ipc.open_stream(pa.py_buffer(payload))
    connection.register(expose_id, reader.read_all())


def _check_count(request: Dict[str, Any]) -> None:
    if type(request.get("id")) is not str:
        raise _Refused("a count has an id")
    if type(request.get("text")) is not str:
        raise _Refused("a count has a text")
    if request.get("unitText") is not None and type(request["unitText"]) is not str:
        raise _Refused("a count's unitText is text or null")
    if request.get("groupBy") is not None and type(request["groupBy"]) is not str:
        raise _Refused("a count's groupBy is text or null")
    request.setdefault("unitText", None)
    request.setdefault("groupBy", None)


def _read_init(stdin: Any) -> Optional[Tuple[int, int, int]]:
    """The ``(seconds, memoryBytes, cpuSeconds)`` of the ``init`` frame, or ``None`` when it is refused."""
    try:
        init = _frames.read_header(stdin, _frames.HEADER_LIMIT)
        if init.get("op") != "init":
            return None
        seconds = _whole(init.get("seconds"), 1, 86_400, "seconds")
        memory = _whole(init.get("memoryBytes"), 1 << 20, 1 << 44, "memoryBytes")
        cpu = _whole(init.get("cpuSeconds"), 1, 10_000_000, "cpuSeconds")
    except (EOFError, FrameError, _Refused):
        return None
    return seconds, memory, cpu


def _serve_one(
    op: Any,
    request: Dict[str, Any],
    stdin: Any,
    stdout: Any,
    engine: Tuple[Any, Any, Any, Any],
    tables: set[str],
    seconds: int,
) -> Optional[int]:
    """Answer one ``table``, ``clear`` or ``count`` request; an exit status when the child must stop."""
    connection, duckdb, pa, ipc = engine
    try:
        if op == "table":
            payload = _frames.read_frame(stdin, _frames.TABLE_LIMIT)
            if request.get("exposeId") in tables:
                raise _Refused("a table is sent once")
            _register(connection, pa, ipc, request, payload)
            tables.add(request["exposeId"])
            _reply(stdout, {"op": "ok"})
        elif op == "clear":
            for name in sorted(tables):
                connection.unregister(name)
            tables.clear()
            _reply(stdout, {"op": "ok"})
        elif op == "count":
            _check_count(request)
            _reply(stdout, _run_request(connection, duckdb, request, seconds))
        else:
            return 2
    except _Refused as refusal:
        _reply(stdout, {"op": "refused", "reason": refusal.reason})
    except (EOFError, FrameError):
        return 2
    except Exception as error:  # noqa: BLE001
        # A table that does not read, or a count that cannot start: refused, with no value in it.
        _reply(stdout, {"op": "refused", "reason": _engine_error(error)})
    return None


def main() -> int:
    """Run the protocol until ``stop`` or the end of stdin. The process exit status."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    asked = _read_init(stdin)
    if asked is None:
        return 2
    seconds, memory, cpu = asked
    limits = _lower_before_imports(seconds, memory, cpu)
    try:
        duckdb = importlib.import_module("duckdb")
        pa = importlib.import_module("pyarrow")
        ipc = importlib.import_module("pyarrow.ipc")
    except ModuleNotFoundError as error:
        _reply(stdout, {"op": "missing", "module": error.name or ""})
        return 3
    settings = connect_settings(memory)
    connection = duckdb.connect(":memory:", config=settings)
    _lower_nofile(limits)
    _reply(
        stdout,
        {
            "op": "hello",
            "duckdb": str(duckdb.__version__),
            "pyarrow": str(pa.__version__),
            "python": sys.version.split()[0],
            "limits": limits,
            "settings": {key: settings[key] for key in sorted(settings)},
            "environment": sorted(os.environ),
        },
    )
    tables: set[str] = set()
    engine = (connection, duckdb, pa, ipc)
    while True:
        try:
            request = _frames.read_header(stdin, _frames.HEADER_LIMIT)
        except EOFError:
            return 0
        except FrameError:
            return 2
        op = request.get("op")
        if op == "stop":
            return 0
        status = _serve_one(op, request, stdin, stdout, engine, tables, seconds)
        if status is not None:
            return status


if __name__ == "__main__":
    raise SystemExit(main())
