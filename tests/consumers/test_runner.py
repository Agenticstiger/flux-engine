# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Counts in a locked DuckDB in a process of their own (ADR 0028 section 3)."""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence

import pytest

pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")

from flux_engine.consumers import _frames  # noqa: E402
from flux_engine.consumers.runner import (  # noqa: E402
    BatchResult,
    CountRequest,
    QueryCounts,
    QueryRunner,
    run_counts,
)
from flux_engine.core.errors import FluxError  # noqa: E402
from flux_engine.seam.consumers import Limits  # noqa: E402

STUB = str(Path(__file__).with_name("_stub_child.py"))
LIMITS = Limits(seconds=20, memory=256 << 20)
SUBS = '"subscriptions"'


def subscriptions() -> Any:
    return pa.table(
        {
            "plan": ["basic", "basic", "pro", None, "pro", "pro"],
            "renewed": [True, False, True, True, True, False],
        }
    )


def stub(*arguments: str) -> Sequence[str]:
    return (sys.executable, "-I", STUB, *arguments)


def counted(text: str, *, unit: Optional[str] = None, group: Optional[str] = None) -> CountRequest:
    return CountRequest("q", text, unit, group)


def run(
    requests: Sequence[CountRequest],
    *,
    limits: Limits = LIMITS,
    command: Optional[Sequence[str]] = None,
    **settings: Any,
) -> BatchResult:
    return run_counts(
        {"subscriptions": subscriptions()}, requests, limits, command=command, **settings
    )


def one(result: BatchResult) -> QueryCounts:
    (only,) = result.queries
    return only


# ----------------------------------------------------------------------------- counts


def test_a_count_and_the_count_of_its_unit() -> None:
    result = run(
        [
            CountRequest(
                "renewed",
                f"SELECT * FROM {SUBS} WHERE renewed",
                f"SELECT * FROM {SUBS}",
                sqlglot_version="30.33.0",
            )
        ]
    )
    answer = one(result)
    assert (answer.status, answer.reason) == ("counted", None)
    assert (answer.rows, answer.unit_rows) == (4, 6)
    assert answer.row_groups is None and answer.unit_groups is None
    assert result.restarts == 0


def test_every_result_records_the_duckdb_version_beside_the_sqlglot_version() -> None:
    import duckdb

    result = run([CountRequest("a", "SELECT 1", sqlglot_version="30.33.0")])
    answer = one(result)
    assert (answer.duckdb_version, answer.sqlglot_version) == (duckdb.__version__, "30.33.0")
    assert result.versions.duckdb == duckdb.__version__
    assert result.versions.pyarrow == pa.__version__


def test_a_query_without_a_unit_is_counted_for_its_rows_only() -> None:
    answer = one(run([counted(f"SELECT * FROM {SUBS} WHERE plan = 'pro'")]))
    assert (answer.rows, answer.unit_rows) == (3, None)


def test_a_grouped_count_counts_each_group_and_keeps_the_null_group_apart() -> None:
    answer = one(
        run(
            [
                counted(
                    f"SELECT * FROM {SUBS} WHERE renewed",
                    unit=f"SELECT * FROM {SUBS}",
                    group="plan",
                )
            ]
        )
    )
    assert answer.status == "counted"
    assert answer.row_groups == ((None, 1), ("basic", 1), ("pro", 2))
    assert answer.unit_groups == ((None, 1), ("basic", 2), ("pro", 3))
    assert (answer.rows, answer.unit_rows) == (4, 6)


def test_a_group_named_none_is_a_level_and_not_the_null_group() -> None:
    table = pa.table({"g": ["None", None, "None"]})
    result = run_counts(
        {"t": table}, [counted('SELECT * FROM "t"', unit='SELECT * FROM "t"', group="g")], LIMITS
    )
    assert one(result).row_groups == ((None, 1), ("None", 2))


def test_the_group_column_name_is_quoted() -> None:
    table = pa.table({'we"ird': ["x", "y", "x"]})
    result = run_counts({"t": table}, [counted('SELECT * FROM "t"', group='we"ird')], LIMITS)
    assert one(result).row_groups == (("x", 2), ("y", 1))


def test_an_empty_table_counts_zero() -> None:
    table = pa.table({"a": pa.array([], type=pa.int64())})
    answer = one(
        run_counts({"t": table}, [counted('SELECT * FROM "t"', unit='SELECT * FROM "t"')], LIMITS)
    )
    assert (answer.rows, answer.unit_rows) == (0, 0)


def test_results_come_back_in_the_order_of_the_requests_and_one_bad_one_does_not_stop_the_rest() -> (
    None
):
    requests = [
        CountRequest("first", "SELECT 1"),
        CountRequest("second", "SELECT * FROM no_such_table"),
        CountRequest("third", f"SELECT * FROM {SUBS}"),
    ]
    first, second, third = run(requests).queries
    assert [first.id, second.id, third.id] == ["first", "second", "third"]
    assert (first.status, second.status, third.status) == ("counted", "error", "counted")
    assert second.reason is not None and "\n" not in second.reason
    assert third.rows == 6


def test_a_parent_variable_is_not_visible_to_a_query() -> None:
    # the name a replacement scan would find
    visible_to_python = pa.table({"x": [1]})  # noqa: F841
    answer = one(run([counted("SELECT * FROM visible_to_python")]))
    assert answer.status == "error"
    assert answer.reason is not None and answer.reason.startswith("CatalogException")


def test_an_engine_message_never_crosses_the_pipe_so_no_value_of_the_table_does() -> None:
    table = pa.table({"v": ["secret-value-xyz", "other"]})
    result = run_counts(
        {"t": table}, [counted('SELECT * FROM "t" WHERE CAST(v AS INTEGER) > 0')], LIMITS
    )
    answer = one(result)
    assert answer.status == "error"
    assert answer.reason is not None
    assert answer.reason.startswith("ConversionException")
    assert "secret-value-xyz" not in answer.reason and "other" not in answer.reason
    assert not set(answer.reason) & set("'\"`"), answer.reason  # no quote of the engine's message


# ----------------------------------------------------------------------------- the hello


def test_the_hello_reports_the_limits_the_child_set_and_the_settings_it_connected_with() -> None:
    import duckdb

    limits = Limits(seconds=7, memory=64 << 20)
    requests = [CountRequest("a", "SELECT 1"), CountRequest("b", "SELECT 2")]
    with QueryRunner({}, requests, limits) as runner:
        hello = dict(runner.hello)
        runner.run()
    assert hello["duckdb"] == duckdb.__version__ and hello["pyarrow"] == pa.__version__
    assert hello["settings"] == {
        "enable_external_access": False,
        "autoload_known_extensions": False,
        "autoinstall_known_extensions": False,
        "allow_community_extensions": False,
        "allow_unsigned_extensions": False,
        "allow_persistent_secrets": False,
        "python_enable_replacements": False,
        "threads": 1,
        "memory_limit": "64MiB",
        "max_temp_directory_size": "0B",
        "lock_configuration": True,
    }
    limits_set = hello["limits"]
    if sys.platform == "win32":  # pragma: no cover
        assert limits_set["fsize"] is None
        return
    assert limits_set["fsize"] == 0
    assert limits_set["cpu"] == 2 * 7 + 30  # the sum of the per-query seconds plus a margin
    if sys.platform == "darwin":
        assert limits_set["as"] is None  # macOS accepts the limit and does not enforce it
    else:
        assert limits_set["as"] is not None and limits_set["as"] >= 64 << 20
    # the highest descriptor open (its two pipes, and the few the interpreter holds) plus 8
    assert 8 < limits_set["nofile"] <= 40


def test_nothing_the_parent_holds_reaches_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "parent-secret")
    monkeypatch.setenv("DUCKDB_TEST", "1")
    monkeypatch.setenv("PYTHONPATH", "/nonexistent")
    with QueryRunner({}, [], LIMITS) as runner:
        keys = runner.hello["environment"]
    assert "AWS_SECRET_ACCESS_KEY" not in keys
    assert "DUCKDB_TEST" not in keys and "PYTHONPATH" not in keys
    assert {"PATH", "LC_ALL", "TZ"} <= set(keys)
    # macOS adds one key of its own to every process; no other key is expected
    assert set(keys) <= {"PATH", "LC_ALL", "TZ", "__CF_USER_TEXT_ENCODING"}


def test_the_child_starts_with_the_isolated_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[Sequence[str]] = []
    real = subprocess.Popen

    def spy(command: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(list(command))
        assert kwargs["stdin"] == subprocess.PIPE and kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.DEVNULL and kwargs["close_fds"] is True
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
        return real(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    run([CountRequest("a", "SELECT 1")])
    assert seen == [[sys.executable, "-I", "-m", "flux_engine.consumers._child"]]


# ----------------------------------------------------------------------------- the locks

READS = [
    "SELECT * FROM read_csv('/etc/hosts')",
    "SELECT * FROM read_text('/etc/hosts')",
    "SELECT * FROM glob('/*')",
    "SELECT * FROM 'https://example.com/data.csv'",
]

STATEMENTS = [
    *READS,
    "ATTACH 'attached.db' AS attached",
    "COPY (SELECT 1) TO 'copied.csv'",
    "EXPORT DATABASE 'exported'",
    "INSTALL httpfs",
    "LOAD httpfs",
    "SET threads = 4",
    "SET enable_external_access = true",
    "SET lock_configuration = false",
]


def _nothing_appeared(directory: Path) -> None:
    assert sorted(path.name for path in directory.iterdir()) == []


@pytest.mark.parametrize("text", READS)
def test_a_read_of_a_file_or_a_url_is_refused_by_the_real_child(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    answer = one(run([counted(text)]))
    assert answer.status == "error"
    assert answer.reason is not None and answer.reason.startswith("PermissionException")
    _nothing_appeared(tmp_path)


@pytest.mark.parametrize("text", STATEMENTS)
def test_the_connection_itself_refuses_each_statement_even_run_with_no_wrapper(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub runs the text as it is, so only the locks can refuse it."""
    monkeypatch.chdir(tmp_path)
    answer = one(run([counted(text)], command=stub("raw")))
    assert answer.status == "error", text
    assert answer.reason is not None
    assert not re.search(r"ParserException|SyntaxException", answer.reason), answer.reason
    _nothing_appeared(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "SELECT 1) AS q; SET threads = 4; SELECT count(*) FROM (SELECT 1",
        "SELECT 1) AS q; COPY (SELECT 1) TO 'x.csv'; SELECT count(*) FROM (SELECT 1",
        "SELECT 1) AS q; ATTACH 'x.db'; SELECT count(*) FROM (SELECT 1",
    ],
)
def test_a_text_that_holds_two_statements_runs_neither(
    text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    answer = one(run([counted(text)]))
    assert answer.status == "error"
    assert answer.reason is not None and "3 statements" in answer.reason
    _nothing_appeared(tmp_path)


def test_a_text_that_is_one_statement_but_not_a_select_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    answer = one(run([counted("COPY (SELECT 1) TO 'x.csv'")], command=stub("bare")))
    assert answer.status == "error"
    assert answer.reason is not None and "COPY statement; only a SELECT runs" in answer.reason
    _nothing_appeared(tmp_path)


def test_a_request_that_needs_a_new_file_still_fails() -> None:
    if sys.platform == "win32":  # pragma: no cover
        pytest.skip("the child has no resource module")
    answer = one(run([counted("SELECT 1")], command=stub("files")))
    assert answer.status == "error"
    assert answer.reason is not None
    match = re.match(r"opened (\d+) files before: (.+)", answer.reason)
    assert match is not None, answer.reason
    assert int(match.group(1)) < 16
    assert "Too many open files" in match.group(2)


def test_a_file_may_not_grow(tmp_path: Path) -> None:
    if sys.platform == "win32":  # pragma: no cover
        pytest.skip("the child has no resource module")
    target = tmp_path / "grown.bin"
    answer = one(run([counted("SELECT 1")], command=stub("write", str(target))))
    assert answer.status in {"error", "crashed"}
    assert target.stat().st_size == 0


# ----------------------------------------------------------------------------- time and memory


def test_a_runaway_query_times_out_and_the_next_request_still_answers() -> None:
    requests = [
        CountRequest("runaway", "SELECT * FROM range(100000) a(x), range(100000) b(y)"),
        CountRequest("after", f"SELECT * FROM {SUBS}"),
    ]
    started = time.monotonic()
    runaway, after = run(requests, limits=Limits(seconds=1, memory=256 << 20)).queries
    assert time.monotonic() - started < 15
    assert runaway.status == "timedOut" and runaway.rows is None
    assert (after.status, after.rows) == ("counted", 6)


def test_a_query_past_the_memory_limit_is_out_of_memory_and_the_next_request_answers() -> None:
    requests = [
        CountRequest(
            "big", "SELECT DISTINCT i::VARCHAR || 'abcdefghijklmnop' FROM range(30000000) t(i)"
        ),
        CountRequest("after", f"SELECT * FROM {SUBS}"),
    ]
    big, after = run(requests, limits=Limits(seconds=20, memory=16 << 20)).queries
    assert big.status == "outOfMemory"
    assert (after.status, after.rows) == ("counted", 6)


# ----------------------------------------------------------------------------- the parent's clock


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_the_parent_ends_a_child_that_stops_answering() -> None:
    requests = [CountRequest("stuck", "SELECT 'HANG'")]
    started = time.monotonic()
    with pytest.raises(FluxError) as caught:
        with QueryRunner(
            {}, requests, Limits(seconds=1, memory=64 << 20), grace_seconds=1, command=stub("hang")
        ) as runner:
            child = runner._child
            assert child is not None
            pid = child.process.pid
            runner.run()
    assert caught.value.code.code == "FLX-7133"
    assert time.monotonic() - started < 30
    assert _gone(pid)


def test_a_child_that_never_starts_answering_is_ended_after_the_start_clock() -> None:
    script = "import time; time.sleep(600)"
    started = time.monotonic()
    with pytest.raises(FluxError) as caught:
        with QueryRunner(
            {}, [], LIMITS, start_seconds=1, command=(sys.executable, "-I", "-c", script)
        ):
            pass  # pragma: no cover
    assert caught.value.code.code == "FLX-7133"
    assert time.monotonic() - started < 30


# ----------------------------------------------------------------------------- crashes


def test_a_crash_marks_the_request_in_flight_and_a_fresh_child_runs_the_rest() -> None:
    requests = [
        CountRequest("a", f"SELECT * FROM {SUBS}"),
        CountRequest("b", "SELECT 'CRASH'"),
        CountRequest("c", f"SELECT * FROM {SUBS} WHERE renewed"),
        CountRequest("d", "SELECT 'KILL'"),
        CountRequest("e", f"SELECT * FROM {SUBS}"),
    ]
    result = run(requests, command=stub("crash"))
    statuses = [answer.status for answer in result.queries]
    assert statuses == ["counted", "crashed", "counted", "crashed", "counted"]
    assert result.restarts == 2
    a, b, c, d, e = result.queries
    assert b.reason is not None and "exit status 7" in b.reason
    assert d.reason is not None and "SIGKILL" in d.reason
    # the tables were sent again to each fresh child
    assert (a.rows, c.rows, e.rows) == (6, 4, 6)
    assert b.rows is None and d.rows is None


def test_a_request_that_crashes_a_fresh_child_too_is_not_retried() -> None:
    requests = [
        CountRequest("a", "SELECT 'CRASH'"),
        CountRequest("b", "SELECT 'CRASH'"),
        CountRequest("c", f"SELECT * FROM {SUBS}"),
    ]
    result = run(requests, command=stub("crash"))
    assert [answer.status for answer in result.queries] == ["crashed", "crashed", "counted"]
    assert result.restarts == 2


def test_a_fresh_child_that_will_not_start_leaves_the_rest_crashed(tmp_path: Path) -> None:
    requests = [
        CountRequest("a", "SELECT 'CRASH'"),
        CountRequest("b", "SELECT 'y'"),
        CountRequest("c", "SELECT 'z'"),
    ]
    result = run(requests, command=stub("crash_no_restart", str(tmp_path / "started")))
    assert [answer.status for answer in result.queries] == ["crashed"] * 3
    assert result.restarts == 1
    assert result.queries[2].reason is not None
    assert "would not start again" in result.queries[2].reason


def test_one_process_counts_the_tables_of_many_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A calibration counts one run for each candidate in one process: load replaces, count answers."""
    started: List[Any] = []
    real = subprocess.Popen

    def counting(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counting)
    first = pa.table({"renewed": [True, False, True]})
    second = pa.table({"renewed": [True, True, True, True, False]})
    requests = [
        CountRequest("renewed", f"SELECT * FROM {SUBS} WHERE renewed", f"SELECT * FROM {SUBS}")
    ]
    with QueryRunner({}, requests, LIMITS, batches=3) as runner:
        runner.load({"subscriptions": first})
        a = runner.count(requests).queries[0]
        runner.load({"subscriptions": second})
        b = runner.count(requests).queries[0]
        runner.load({"subscriptions": first})
        c = runner.count(requests).queries[0]
    assert [(x.status, x.rows, x.unit_rows) for x in (a, b, c)] == [
        ("counted", 2, 3),
        ("counted", 4, 5),
        ("counted", 2, 3),
    ]
    assert len(started) == 1


def test_a_loaded_table_replaces_the_one_before_it_and_a_name_it_dropped_is_gone() -> None:
    requests = [CountRequest("a", f"SELECT * FROM {SUBS}")]
    with QueryRunner({}, requests, LIMITS) as runner:
        runner.load({"subscriptions": subscriptions()})
        assert runner.count(requests).queries[0].rows == 6
        runner.load({"other": subscriptions()})
        gone = runner.count(requests).queries[0]
    assert gone.status == "error"


def test_a_crash_in_a_later_batch_still_gets_a_fresh_child_with_the_latest_tables() -> None:
    requests = [CountRequest("a", "SELECT 'CRASH'"), CountRequest("b", f"SELECT * FROM {SUBS}")]
    small = pa.table({"renewed": [True, False]})
    with QueryRunner({}, requests, LIMITS, command=stub("crash"), batches=2) as runner:
        runner.load({"subscriptions": subscriptions()})
        first = runner.count(requests)
        runner.load({"subscriptions": small})
        second = runner.count(requests)
    assert [answer.status for answer in first.queries] == ["crashed", "counted"]
    assert [answer.status for answer in second.queries] == ["crashed", "counted"]
    assert (first.queries[1].rows, second.queries[1].rows) == (6, 2)


def test_the_cpu_a_child_may_spend_covers_every_batch_it_will_count() -> None:
    requests = [CountRequest("a", "SELECT 1")]
    with QueryRunner({}, requests, Limits(seconds=2, memory=256 << 20), batches=50) as runner:
        cpu = runner.hello["limits"]["cpu"]
    assert cpu is None or cpu >= 50 * 2


def test_a_missing_library_in_the_child_is_flx_7131() -> None:
    with pytest.raises(FluxError) as caught:
        run([CountRequest("a", "SELECT 1")], command=stub("missing"))
    assert caught.value.code.code == "FLX-7131"
    assert "flux-sim-engine[sql]" in str(caught.value)


def test_a_child_that_ends_before_its_hello_is_flx_7134() -> None:
    with pytest.raises(FluxError) as caught:
        run(
            [CountRequest("a", "SELECT 1")],
            command=(sys.executable, "-I", "-c", "raise SystemExit(5)"),
        )
    assert caught.value.code.code == "FLX-7134"
    assert "exit status 5" in str(caught.value)


# ----------------------------------------------------------------------------- bounds and framing


def test_a_query_text_past_the_message_bound_is_refused_before_anything_starts() -> None:
    huge = "SELECT '" + "x" * _frames.HEADER_LIMIT + "'"
    with pytest.raises(FluxError) as caught:
        run([CountRequest("big", huge)])
    assert caught.value.code.code == "FLX-7132"


def test_a_frame_past_its_bound_is_refused_before_its_bytes_are_read() -> None:
    stream = io.BytesIO((10).to_bytes(4, "big") + b"x" * 10)
    with pytest.raises(_frames.FrameError):
        _frames.read_frame(stream, 9)
    assert stream.tell() == 4  # only the length was taken


def test_a_frame_cut_short_is_an_end_of_file() -> None:
    with pytest.raises(EOFError):
        _frames.read_frame(io.BytesIO((10).to_bytes(4, "big") + b"xxx"), 100)
    with pytest.raises(EOFError):
        _frames.read_frame(io.BytesIO(b""), 100)
    with pytest.raises(EOFError):
        _frames.read_frame(io.BytesIO(b"\x00\x00"), 100)


@pytest.mark.parametrize(
    "document",
    [b"[1]", b'{"a": 1.5}', b'{"a": NaN}', b"\xff", b"{", b"[" * 100_000],
)
def test_a_header_is_a_json_object_without_floats(document: bytes) -> None:
    with pytest.raises(_frames.FrameError):
        _frames.decode_header(document)


def test_a_header_round_trips_with_no_pickle() -> None:
    buffer = io.BytesIO()
    _frames.write_header(buffer, {"op": "count", "id": "é", "n": 3, "none": None}, 1000)
    buffer.seek(0)
    assert _frames.read_header(buffer, 1000) == {"op": "count", "id": "é", "n": 3, "none": None}
    assert b"\x80" not in buffer.getvalue()[4:5]  # a pickle starts with 0x80, a header with "{"


# ----------------------------------------------------------------------------- the code


CONSUMERS = Path(__file__).resolve().parents[2] / "flux_engine" / "consumers"


@pytest.mark.parametrize("name", ["runner.py", "_child.py", "_frames.py", "tables.py"])
def test_nothing_pickles_over_the_pipe(name: str) -> None:
    """No call of ``.send(`` or ``.recv(`` (which pickle), and no module that serialises objects."""
    tree = ast.parse((CONSUMERS / name).read_text(encoding="utf-8"))
    banned = {"pickle", "marshal", "shelve", "dill", "multiprocessing", "cloudpickle"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"send", "recv"}, f"{name}:{node.lineno}"
        if isinstance(node, ast.Import):
            assert not banned & {alias.name.split(".")[0] for alias in node.names}
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.split(".")[0] not in banned  # an import_module("pickle")


def _assert_no_exception_text(node: ast.AST, name: str) -> None:
    """*node*, inside the handler of *name*, sends neither the exception's text nor its arguments."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        assert not (node.value.id == name and node.attr == "args"), node.lineno
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"str", "repr", "format", "print"}:
            names = {a.id for a in node.args if isinstance(a, ast.Name)}
            assert name not in names, node.lineno
    if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name):
        assert node.value.id != name, node.lineno


def test_the_child_never_sends_an_exception_s_text_or_arguments() -> None:
    """No ``str(exc)``, ``repr(exc)``, ``exc.args`` or ``f"{exc}"`` of a caught exception."""
    tree = ast.parse((CONSUMERS / "_child.py").read_text(encoding="utf-8"))
    handlers = [
        node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler) and node.name
    ]
    assert handlers
    for handler in handlers:
        assert handler.name is not None
        for node in (inner for statement in handler.body for inner in ast.walk(statement)):
            _assert_no_exception_text(node, handler.name)


def test_importing_the_runner_imports_no_duckdb_sqlglot_or_pyarrow() -> None:
    probe = (
        "import sys\n"
        "import flux_engine.consumers.runner, flux_engine.consumers.tables\n"
        "loaded = sorted(name for name in ('duckdb', 'sqlglot', 'pyarrow') if name in sys.modules)\n"
        "print(loaded)\n"
    )
    output = subprocess.run(
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "[]"


def test_running_a_batch_imports_no_duckdb_in_the_parent() -> None:
    probe = (
        "import sys\n"
        "import pyarrow as pa\n"
        "from flux_engine.consumers.runner import CountRequest, run_counts\n"
        "from flux_engine.seam.consumers import Limits\n"
        "result = run_counts({'t': pa.table({'a': [1, 2]})}, [CountRequest('q', 'SELECT * FROM \"t\"')],"
        " Limits(seconds=10, memory=1 << 28))\n"
        "print(result.queries[0].rows, 'duckdb' in sys.modules)\n"
    )
    output = subprocess.run(
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "2 False"


# ----------------------------------------------------------------------------- time zones


def test_a_timestamp_compares_in_utc_whatever_the_parent_zone(monkeypatch: Any) -> None:
    # DuckDB without its ICU extension reads a time zone through the process's own, so the child
    # runs with TZ=UTC and never sees the parent's: a 23:30 UTC row is after 23:00 on its UTC day.
    import datetime

    from flux_engine.consumers.tables import _naive_timestamps

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    moment = datetime.datetime(2026, 1, 1, 23, 30, tzinfo=datetime.timezone.utc)
    aware = pa.table({"happened_at": pa.array([moment], type=pa.timestamp("ms", tz="UTC"))})
    table = _naive_timestamps(aware, pa)
    assert table.schema.field("happened_at").type == pa.timestamp("ms")
    result = run_counts(
        {"events": table},
        [
            CountRequest(
                "after",
                "SELECT * FROM \"events\" WHERE happened_at >= TIMESTAMP '2026-01-01 23:00:00'",
            ),
            CountRequest(
                "day",
                "SELECT * FROM \"events\" WHERE CAST(happened_at AS DATE) = DATE '2026-01-01'",
            ),
        ],
        LIMITS,
    )
    assert [query.rows for query in result.queries] == [1, 1]
