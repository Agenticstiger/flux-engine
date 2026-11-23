# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim run --sink``: a secret typed in a target or an option is never printed or stored.

Each case runs the real command line with a fake secret (:data:`SECRET`) in a place a shape list
misses, and fails when any four-character piece of it is in what the command printed (plain and
``--json``), in the state directory (``sink-receipts.json`` included) or in the files the sink wrote.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from .conftest import pyarrow_or_skip

pyarrow_or_skip()

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"
SECRET = "Zq7FAKEVAL1xY"


def _leaked(blob: str, secret: str = SECRET) -> List[str]:
    """Every piece of *secret*, four characters or more, that *blob* holds."""
    return [
        secret[start:stop]
        for start in range(len(secret))
        for stop in range(start + 4, len(secret) + 1)
        if secret[start:stop] in blob
    ]


def _written(root: Path) -> str:
    return "\n".join(
        path.read_text(errors="replace") for path in sorted(root.rglob("*")) if path.is_file()
    )


def _fluxsim(*args: str, cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-m", "flux_engine.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _run_and_find_no_secret(tmp_path: Path, *sink_words: str) -> "subprocess.CompletedProcess[str]":
    """Run the command plain and as ``--json`` with *sink_words* (``--sink X --sink-opt Y``) and check what it left."""
    work, state = tmp_path / "cwd", tmp_path / "state"
    work.mkdir()
    result = None
    for flags in ((), ("--json",)):
        result = _fluxsim(
            *flags, "run", str(FIXTURE), "--state-dir", str(state), *sink_words, cwd=work
        )
        printed = result.stdout + result.stderr
        assert _leaked(printed) == [], printed
    assert result is not None
    assert _leaked(_written(state)) == []
    for path in work.rglob("*"):
        if path.is_file() and path.stat().st_size < 1_000_000:
            assert _leaked(path.read_text(errors="replace")) == [], path
    return result


def _adbc(uri: str) -> List[str]:
    return ["--sink", "adbc:sqlite", "--sink-opt", f"uri={uri}"]


# -- 1. a uri that is no URL is never shown as a path ---------------------------


@pytest.mark.parametrize(
    "uri",
    [
        f"Bearer {SECRET}",
        f"Token {SECRET}",
        f"password:{SECRET}",
        f'"password": "{SECRET}"',
        f"sqlite:password:{SECRET}",
        SECRET,
    ],
)
def test_the_cli_never_shows_a_uri_value_that_is_no_url_as_if_it_were_a_path(
    tmp_path: Path, uri: str
) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    _run_and_find_no_secret(tmp_path, *_adbc(uri))


def test_the_cli_never_shows_a_db_option_value_that_is_no_url(tmp_path: Path) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    for index, value in enumerate((f"Bearer {SECRET}", f"password:{SECRET}", SECRET)):
        sub = tmp_path / f"value-{index}"
        sub.mkdir()
        _run_and_find_no_secret(
            sub, "--sink", "adbc:sqlite", "--sink-opt", f"db.adbc.sample={value}"
        )


# -- 2. a host that is not a host, a port that is not a number --------------------


@pytest.mark.parametrize("uri", [f"postgresql://h:{SECRET}/db", f"postgresql://u:{SECRET}%40h/db"])
def test_the_cli_withholds_a_host_or_port_that_is_not_one(tmp_path: Path, uri: str) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    _run_and_find_no_secret(tmp_path, *_adbc(uri))
    sub = tmp_path / "again"
    sub.mkdir()
    _run_and_find_no_secret(sub, "--sink", f"parquet:{uri}")


# -- 3. a path that hides a secret -------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        f"postgresql://h/db%3Fpassword%3D{SECRET}",
        f"postgresql://h/db;password={SECRET}",
        f"postgresql://h/{SECRET}%40x",
    ],
)
def test_the_cli_withholds_a_path_that_holds_an_encoded_or_delimited_secret(
    tmp_path: Path, uri: str
) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    _run_and_find_no_secret(tmp_path, *_adbc(uri))
    sub = tmp_path / "again"
    sub.mkdir()
    _run_and_find_no_secret(sub, "--sink", f"parquet:{uri}")


# -- 4. a slash before the last at sign ---------------------------------------------


@pytest.mark.parametrize(
    "target",
    [f"s3://bkt/pfx@{SECRET}/run", f"gs://bkt/p/q@{SECRET}", f"az://acct/c@{SECRET}/x"],
)
def test_the_cli_shows_nothing_after_the_scheme_when_a_slash_precedes_an_at_sign(
    tmp_path: Path, target: str
) -> None:
    """The target is refused before any store is built: no test opens a connection."""
    pytest.importorskip("adbc_driver_sqlite")
    pytest.importorskip("deltalake")
    for sink in ("parquet", "arrow-ipc", "delta"):
        sub = tmp_path / sink
        sub.mkdir()
        result = _run_and_find_no_secret(sub, "--sink", f"{sink}:{target}")
        assert result.returncode != 0 and "FLX-3510" in result.stdout + result.stderr
    sub = tmp_path / "adbc"
    sub.mkdir()
    _run_and_find_no_secret(sub, *_adbc(target))


# -- 5. an option's value is never echoed by its own validation ----------------------


@pytest.mark.parametrize(
    "sink, option",
    [
        ("parquet", "row-group-size"),
        ("parquet", "compression"),
        ("parquet", "compression-level"),
        ("parquet", "batch-size"),
        ("parquet", "rows-per-file"),
        ("parquet", "verify"),
        ("arrow-ipc", "compression"),
        ("arrow-ipc", "batch-size"),
        ("delta", "mode"),
        ("adbc:sqlite", "mode"),
        ("adbc:sqlite", "verify"),
        ("adbc:sqlite", "batch-size"),
    ],
)
def test_the_cli_refuses_a_bad_option_value_naming_the_option_only(
    tmp_path: Path, sink: str, option: str
) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    if sink == "delta":
        pytest.importorskip("deltalake")
    target = f"{sink}:{tmp_path / 'out'}" if not sink.startswith("adbc") else sink
    uri = ["--sink-opt", f"uri={tmp_path / 'x.db'}"] if sink.startswith("adbc") else []
    result = _run_and_find_no_secret(
        tmp_path, "--sink", target, "--sink-opt", f"{option}={SECRET}", *uri
    )
    assert result.returncode != 0
    assert option in result.stdout + result.stderr


# -- 6. a library echoes a secret percent-encoded or quoted --------------------------


def test_the_cli_never_prints_the_encoded_or_quoted_form_of_a_secret_a_library_echoes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    from urllib.parse import quote, quote_plus

    from flux_engine.cli import main
    from flux_engine.sinks import registry

    option = f"Bearer {SECRET} x"
    key = f"k'\"{SECRET}"
    monkeypatch.setenv("FLUX_TEST_API_KEY", key)
    echoes = [
        quote(option, safe=""),
        quote_plus(option),
        repr(option)[1:-1],
        quote(key, safe=""),
        repr(key)[1:-1],
        json.dumps(key)[1:-1],
    ]

    class Failing:
        name = "failing"

        def sink_for(self, context: object) -> object:
            raise RuntimeError("; ".join(echoes))

    monkeypatch.setattr(registry, "load_factory", lambda name: lambda target, options: Failing())
    state = tmp_path / "state"
    for flags in ((), ("--json",)):
        code = main(
            [
                *flags,
                "run",
                str(FIXTURE),
                "--state-dir",
                str(state),
                "--sink",
                "failing:here",
                "--sink-opt",
                f"endpoint={option}",
            ]
        )
        captured = capsys.readouterr()
        assert code != 0
        assert _leaked(captured.out + captured.err) == [], captured.out + captured.err
    assert _leaked(_written(state)) == []


# -- 7. a very large string is replaced in linear time -------------------------------


def test_the_cli_replaces_a_very_large_option_value_without_a_pattern_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from flux_engine.cli import main
    from flux_engine.sinks import registry

    big = " " * 100_000 + SECRET

    class Failing:
        name = "failing"

        def sink_for(self, context: object) -> object:
            raise RuntimeError(f"cannot use {big}")

    import time

    monkeypatch.setattr(registry, "load_factory", lambda name: lambda target, options: Failing())
    start = time.perf_counter()
    code = main(
        [
            "--json",
            "run",
            str(FIXTURE),
            "--state-dir",
            str(tmp_path / "state"),
            "--sink",
            "failing:here",
            "--sink-opt",
            f"endpoint={big}",
        ]
    )
    # The pattern search this replaced took eleven seconds here, and a runner is slower.
    assert time.perf_counter() - start < 10.0
    captured = capsys.readouterr()
    assert code != 0 and _leaked(captured.out + captured.err) == []


# -- 8. the error path: a value that ends in a line break, and a name that is a secret ----


@pytest.mark.parametrize("ending", ["\n", " ", "\r\n", "\x85", " \n"])
def test_the_cli_never_prints_a_uri_that_ends_in_a_line_break_when_the_driver_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    uri = f"file:{tmp_path}/none/x.db?mode=ro&cache={SECRET}{ending}"
    work = tmp_path / "by-option"
    work.mkdir()
    result = _run_and_find_no_secret(work, "--sink", "adbc:sqlite", "--sink-opt", f"uri={uri}")
    assert result.returncode != 0
    monkeypatch.setenv("FLUXJ_URI", uri)
    work = tmp_path / "by-variable"
    work.mkdir()
    result = _run_and_find_no_secret(
        work, "--sink", "adbc:sqlite", "--sink-opt", "uri-env=FLUXJ_URI"
    )
    assert result.returncode != 0


def test_the_cli_never_echoes_a_secret_typed_where_a_name_goes(tmp_path: Path) -> None:
    pytest.importorskip("adbc_driver_sqlite")
    for index, words in enumerate(
        (
            ["--sink", "adbc:sqlite", "--sink-opt", f"uri-env={SECRET}"],
            ["--sink", "adbc:sqlite", "--sink-opt", f"uri-env={SECRET}-x"],
            ["--sink", "adbc:sqlite", "--sink-opt", f"{SECRET}==", "--sink-opt", "uri=x.db"],
            ["--sink", f"parquet:{tmp_path / 'out'}", "--sink-opt", f"{SECRET}=1"],
            ["--sink", "adbc:sqlite", "--sink-opt", f"db.{SECRET}-env=x", "--sink-opt", "uri=x"],
            ["--sink", f"parquet:{tmp_path / 'out'}", "--sink-opt", f"{SECRET}-password=1"],
        )
    ):
        sub = tmp_path / f"case-{index}"
        sub.mkdir()
        result = _run_and_find_no_secret(sub, *words)
        assert result.returncode != 0, words
