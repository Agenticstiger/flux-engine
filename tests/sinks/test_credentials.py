# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Every sink refuses credential-shaped options and targets, and its errors are scrubbed."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Iterator, List, Mapping
from urllib.parse import quote, quote_plus

import pytest

from flux_engine.core.digest import digest_of_bytes
from flux_engine.core.errors import FluxError
from flux_engine.core.target_label import target_label
from flux_engine.sinks import adbc, arrow_ipc, parquet, registry
from flux_engine.sinks._secrets import credential_key, credential_url, known_strings, scrub
from flux_engine.sinks.receipts import SinkReceipt, StreamContext
from flux_engine.stages import deliver

from .test_registry import _points, memory_factory  # noqa: F401

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The key rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "Password",
        "db-password",
        "passwd",
        "pwd",
        "secret",
        "client_secret",
        "token",
        "auth.token",
        "auth_token-env",
        "credential",
        "credentials",
        "sas",
        "sas-token",
        "signature",
        "sig",
        "apikey",
        "API_KEY",
        "key",
        "KEY",
        "secretaccesskey",
        "secret-access-key",
        "SecretAccessKey",
        "authkey",
        "auth-key",
        "AuthKey",
        "passphrase",
        "pass-phrase",
        "PassPhrase",
        "authorization",
        "Authorization",
        "bearer",
        "BEARER",
        "sslkey",
        "sslpassword",
        "passfile",
        "access-key",
        "access_key",
        "account_key",
        "api.key",
        "client-key",
        "private_key",
        "secret.key",
        "storage-key",
        "master-key",
        "sse-key",
        "encryption-key",
        "accessKey",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_STORAGE_ACCOUNT_KEY",
        "accesskey",
        "db.accesskey",
        "db.access.key",
        "db_access_key",
        "ACCESSKEY",
        "secretkey",
        "PrivateKey",
        "apikey",
        "db.accountkey",
        "authtoken",
        "dbpassword",
        "clientsecret",
    ],
)
def test_a_name_that_names_a_credential_is_flagged(name: str) -> None:
    assert credential_key(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "mode",
        "prefix",
        "schema",
        "uri",
        "uri-env",
        "region",
        "endpoint",
        "keyspace",
        "primary-key",
        "partition_key",
        "sort-key",
        "signal",
        "design",
        "tokenizers",
        "passport",
        "key-prefix",
        "keys",
        "the-key",
    ],
)
def test_an_ordinary_name_is_not_flagged(name: str) -> None:
    assert credential_key(name) is False


@pytest.mark.parametrize("key", ["db.accesskey", "db.access-key", "db.access_key", "db.AccessKey"])
def test_every_spelling_of_a_credential_key_gets_the_credential_refusal(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "load_factory", lambda name: _Spy())
    with pytest.raises(FluxError) as caught:
        registry.open_target("adbc:sqlite", {key: "hunter2-value"})
    assert caught.value.code.code == "FLX-3508"
    assert "hunter2-value" not in str(caught.value)


def test_no_built_in_option_name_is_flagged() -> None:
    names = [*parquet.OPTIONS, *arrow_ipc.OPTIONS, *adbc.OPTIONS]
    assert names, "the built-in sinks list their options"
    assert [name for name in names if credential_key(name)] == []


# ---------------------------------------------------------------------------
# The target rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target, shown",
    [
        ("s3://user:pass@bucket/prefix", "s3://…@bucket/prefix"),
        ("s3://user@bucket/prefix", "s3://…@bucket/prefix"),
        ("https://host/x?sig=abc", "https://host/x?…"),
        ("az://account/container#frag", "az://account/container#…"),
        ("gs://bucket/p?a=1#f", "gs://bucket/p?…#…"),
    ],
)
def test_a_url_that_carries_credentials_is_flagged_and_labelled(target: str, shown: str) -> None:
    assert credential_url(target) is True
    assert target_label("x", target) == shown
    assert "pass" not in shown and "sig=" not in shown


@pytest.mark.parametrize(
    "target",
    [
        "",
        "out/",
        "/data/out",
        "duckdb",
        "sim.duckdb",
        "C:\\data\\out",
        "s3://bucket/prefix",
        "https://host:8080/a/b",
        "postgresql",
        "sqlite",
    ],
)
def test_a_target_without_credentials_is_not_flagged(target: str) -> None:
    assert credential_url(target) is False


# ---------------------------------------------------------------------------
# scrub
# ---------------------------------------------------------------------------


def test_scrub_writes_the_known_target_as_its_label() -> None:
    raw = "https://user:pw@host/x?sig=abc#f"
    assert scrub(f"GET {raw} failed", [raw], sink="x") == "GET https://…@host/x?…#… failed"
    assert scrub("see (https://u:p@h/x?a=b), then https://h2/y?q=1.", ["https://u:p@h/x?a=b"]) == (
        "see (https://…@h/x?…), then https://h2/y?…."
    )


_FAKE = "Zq7FAKEVAL1xY"


def _pieces(secret: str, size: int = 4) -> List[str]:
    return [secret[i : i + size] for i in range(len(secret) - size + 1)]


@pytest.mark.parametrize(
    ("message", "shown"),
    [
        (
            f"PUT https://u:{_FAKE}@h/p?X-Amz-Signature={_FAKE}#f failed with 403",
            "PUT https://…@h/p?…#… failed with 403",
        ),
        (
            f"connect to 'postgresql://u:{_FAKE}@h:5432/db' refused",
            "connect to 'postgresql://…@h:5432/db' refused",
        ),
        (
            f"cannot reach (postgresql://u:{_FAKE}@h:5432/db), giving up",
            "cannot reach (postgresql://…@h:5432/db), giving up",
        ),
        (
            f"<a href=https://u:{_FAKE}@h/p?k={_FAKE}>x</a>",
            "<a href=https://…@h/p?…>x</a>",
        ),
    ],
)
def test_scrub_writes_every_url_in_a_message_as_its_label(message: str, shown: str) -> None:
    text = scrub(message)
    assert text == shown
    assert not any(piece in text for piece in _pieces(_FAKE))


def test_scrub_leaves_a_message_without_a_url_unchanged() -> None:
    message = "GET failed; password=hunter2 for user@host:5432/db and a:/b, see /tmp/a b/c? no"
    assert scrub(message) == message


def test_scrub_of_a_hostile_message_is_linear() -> None:
    import time

    def run(text: str) -> float:
        start = time.perf_counter()
        scrub(text)
        return time.perf_counter() - start

    unit = ["a:" * 500 + "@" * 500, "://" * 300, "x://" + "u:p@" * 250, "https://" + "?#@:/" * 200]
    small = "".join(unit)
    big = small * (1_000_000 // len(small) + 1)
    run(small)
    base = max(run(small * 20), 1e-4)
    elapsed = run(big)
    # 1 MB is about 50 times the baseline text: a quadratic scan would be about 2500 times.
    assert len(big) >= 1_000_000
    assert elapsed < max(base * (len(big) / len(small * 20)) * 20, 10.0)
    for hostile in ("://" * 340_000, "a" * 1_000_000 + "://", "x://" + "@" * 1_000_000):
        assert run(hostile) < max(base * 2000, 10.0)


def test_known_strings_hold_the_target_the_values_and_what_a_variable_resolves_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIM_URI", "postgresql://u:pw@h/db")
    options = {"uri-env": "SIM_URI", "mode": "append", "db.password-env": "UNSET_VARIABLE"}
    assert known_strings("sqlite", options) == [
        "sqlite",
        ("postgresql://u:pw@h/db", "$SIM_URI"),
    ]
    assert scrub("bad postgresql://u:pw@h/db", known_strings("sqlite", options)) == "bad $SIM_URI"


def test_scrub_replaces_the_value_of_a_credential_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI-k7mdeng")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "FwoGZXIvYXdz-session")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_KEY", "c3RvcmFnZS1rZXk=")
    monkeypatch.setenv("FLUX_TEST_REGION", "eu-west-1")
    monkeypatch.setenv("FLUX_TEST_TOKEN", "abc")  # shorter than four characters: left alone
    message = (
        "denied: wJalrXUtnFEMI-k7mdeng and FwoGZXIvYXdz-session and c3RvcmFnZS1rZXk= "
        "in eu-west-1 with abc"
    )
    assert scrub(message) == "denied: *** and *** and *** in eu-west-1 with abc"


def test_scrub_does_both_in_one_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLUX_TEST_API_KEY", "k-1234567890")
    raw = "https://user:pw@host/x?sig=abc#f"
    text = scrub(f"{raw} rejected k-1234567890", [raw])
    assert text == "https://…@host/x?…#… rejected ***"


_FAKE = "Zq7FAKEVAL1xY"


def _holds_a_piece(text: str, secret: str = _FAKE) -> bool:
    return any(
        secret[start:stop] in text
        for start in range(len(secret))
        for stop in range(start + 4, len(secret) + 1)
    )


def test_scrub_replaces_the_encoded_and_quoted_forms_of_an_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = f'p@ss {_FAKE}\'"x"'
    monkeypatch.setenv("FLUX_TEST_API_KEY", value)
    for echo in (
        value,
        quote(value, safe=""),
        quote(value),
        quote_plus(value),
        repr(value)[1:-1],
        json.dumps(value)[1:-1],
    ):
        assert scrub(f"denied {echo} now") == "denied *** now", echo


def test_scrub_replaces_the_encoded_and_quoted_forms_of_a_known_string() -> None:
    raw = f"postgresql://u:{_FAKE}@h/db"
    for echo in (quote(raw, safe=""), quote_plus(raw), repr(raw)[1:-1], f"{{'dsn': '{raw}'}}"):
        assert not _holds_a_piece(scrub(f"bad {echo}", [raw], sink="adbc")), echo
    option = f"Bearer {_FAKE}"
    text = scrub(f"bad {quote(option, safe='')} {option!r}", [(option, "x")], sink="adbc")
    assert not _holds_a_piece(text)


def test_an_option_value_that_is_no_url_is_written_as_a_digest_label_in_a_message() -> None:
    known = known_strings("sqlite", {"db.adbc.sample": f"password:{_FAKE}", "mode": "append"})
    text = scrub(f"bad password:{_FAKE} in append mode", known, sink="sqlite")
    assert not _holds_a_piece(text)
    assert "append mode" in text


# ---------------------------------------------------------------------------
# open_target
# ---------------------------------------------------------------------------


class _Spy:
    """A factory that records that it ran."""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def __call__(self, target: str, options: Mapping[str, str]) -> Any:
        self.calls.append((target, dict(options)))
        return memory_factory(target, {})


spy = _Spy()


@pytest.mark.parametrize("spec", ["parquet:out/", "adbc:duckdb", "arrow-ipc:out/", "memory:here"])
@pytest.mark.parametrize(
    "key", ["password", "access-key", "sas-token", "client_secret", "signature", "api.key"]
)
def test_a_credential_shaped_option_is_refused_before_any_factory_runs(
    spec: str, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy()
    monkeypatch.setattr(
        registry, "_entry_points", _points(("memory", f"{__name__}:memory_factory"))
    )
    monkeypatch.setattr(registry, "load_factory", lambda name: spy)
    with pytest.raises(FluxError) as caught:
        registry.open_target(spec, {key: "hunter2-value"})
    assert caught.value.code.code == "FLX-3508"
    assert key in str(caught.value)
    assert "hunter2-value" not in str(caught.value)
    assert spy.calls == []


def test_a_third_party_sink_is_held_to_the_same_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    spy.calls.clear()
    monkeypatch.setattr(registry, "_entry_points", _points(("memory", f"{__name__}:spy")))
    with pytest.raises(FluxError) as caught:
        registry.open_target("memory:here", {"label": "x", "account-key": "abcd1234"})
    assert caught.value.code.code == "FLX-3508" and "account-key" in str(caught.value)
    assert "abcd1234" not in str(caught.value) and spy.calls == []
    # The same stub opens with an ordinary option.
    assert registry.open_target("memory:here", {"label": "x"}) is not None
    assert spy.calls == [("here", {"label": "x"})]


@pytest.mark.parametrize(
    "target, shown",
    [
        ("s3://user:pass@bucket/p", "s3://…@bucket/p"),
        ("az://acct/c?sig=abc", "az://acct/c?…"),
        ("gs://bucket/p#frag", "gs://bucket/p#…"),
    ],
)
@pytest.mark.parametrize("name", ["parquet", "memory"])
def test_a_credential_bearing_target_is_refused_and_shown_as_a_label(
    name: str, target: str, shown: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy()
    monkeypatch.setattr(
        registry, "_entry_points", _points(("memory", f"{__name__}:memory_factory"))
    )
    monkeypatch.setattr(registry, "load_factory", lambda sink: spy)
    with pytest.raises(FluxError) as caught:
        registry.open_target(f"{name}:{target}")
    assert caught.value.code.code == "FLX-3509"
    assert repr(shown) in str(caught.value)
    for leak in ("user:pass", "sig=abc", "#frag"):
        assert leak not in str(caught.value)
    assert spy.calls == []


def test_a_variable_name_option_is_not_a_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _Spy()
    monkeypatch.setattr(registry, "load_factory", lambda name: spy)
    registry.open_target("adbc:bigquery", {"db.adbc.x.auth_token-env": "SOME_VARIABLE"})
    assert spy.calls == [("bigquery", {"db.adbc.x.auth_token-env": "SOME_VARIABLE"})]


def test_a_plain_path_and_driver_target_pass(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    assert registry.open_target(f"parquet:{tmp_path / 'out'}") is not None


def test_a_factory_error_is_scrubbed_and_not_chained(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLUX_TEST_PASSWORD", "hunter2-long")

    def factory(target: str, options: Mapping[str, str]) -> Any:
        try:
            raise OSError("cannot reach https://u:p@host/x?sig=abc with hunter2-long")
        except OSError as inner:
            raise RuntimeError(str(inner)) from inner

    monkeypatch.setattr(registry, "load_factory", lambda name: factory)
    with pytest.raises(FluxError) as caught:
        registry.open_target("memory:here", {"endpoint": "https://u:p@host/x?sig=abc"})
    message = str(caught.value)
    assert "hunter2-long" not in message and "sig=abc" not in message and "u:p" not in message
    assert "https://…@host/x?…" in message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# A surfaced sink error
# ---------------------------------------------------------------------------


def _context() -> StreamContext:
    return StreamContext(
        run_id="r",
        expose_id="pay",
        rows=1,
        output_digest=digest_of_bytes(b"", subject="probe"),
    )


class _Leaky:
    """A sink whose writer fails with a signed URL and a key from the environment."""

    def __init__(self, error: Exception) -> None:
        self.error, self.receipt = error, None

    def open(self, header: Any) -> None:
        pass

    def write(self, row: Any) -> None:
        try:
            raise ConnectionError("PUT https://u:pw@host/x?sig=abc#f 403 with key k-1234567890")
        except ConnectionError as inner:
            raise self.error from inner

    def close(self) -> None:
        pass


class _Target:
    name = "leaky"

    def __init__(self, error: Exception, *known: str) -> None:
        self.error = error
        if known:
            self._flux_known_strings = known

    def sink_for(self, context: StreamContext) -> _Leaky:
        return _Leaky(self.error)


def _rows() -> Iterator[List[Any]]:
    yield ["a"]


_RAW = "https://u:pw@host/x?sig=abc#f"


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"PUT {_RAW} 403 with key k-1234567890"),
        FluxError("FLX-3505", f"PUT {_RAW} 403 with key k-1234567890"),
    ],
)
def test_a_writers_error_names_the_target_by_its_label_and_is_not_chained(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUX_TEST_API_KEY", "k-1234567890")
    target = _Target(error, _RAW)
    sink = target.sink_for(_context())
    with pytest.raises(FluxError) as caught:
        deliver._write_stream(target, sink, object(), _context(), _rows())  # type: ignore[arg-type]
    message = str(caught.value)
    assert caught.value.code.code == "FLX-3505"
    for leak in ("u:pw", "sig=abc", "k-1234567890", "#f"):
        assert leak not in message
    assert "https://…@host/x?…#…" in message and "***" in message
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


def test_a_writers_error_text_is_not_printed_by_a_call_site_that_knows_no_target() -> None:
    target = _Target(RuntimeError(f"PUT {_RAW} 403"))  # no strings to replace: no text printed
    sink = target.sink_for(_context())
    with pytest.raises(FluxError) as caught:
        deliver._write_stream(target, sink, object(), _context(), _rows())  # type: ignore[arg-type]
    assert str(caught.value).endswith("failed on expose 'pay': RuntimeError")
    assert "host" not in str(caught.value)


def test_a_target_that_fails_building_a_sink_prints_no_free_text_and_is_not_chained() -> None:
    class Broken:
        name = "broken"

        def sink_for(self, context: StreamContext) -> Any:
            raise RuntimeError("no route to https://u:pw@host/x?sig=abc")

    with pytest.raises(FluxError) as caught:
        deliver._sinks_for(Broken(), [_context()])  # type: ignore[arg-type]
    assert (
        "pw" not in str(caught.value)
        and "sig=abc" not in str(caught.value)
        and "host" not in str(caught.value)
    )
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


# ---------------------------------------------------------------------------
# The command a failed delivery prints
# ---------------------------------------------------------------------------


def _hint(sink: str, options: List[str]) -> str:
    import argparse
    from types import SimpleNamespace

    from flux_engine.cli._output import _republish_hint

    args = argparse.Namespace(
        sink=sink, sink_opt=options, bundle="bundle", run_id="r1", state_dir=None
    )
    result = SimpleNamespace(
        success=False, artifacts={"sinks": {"ok": False}, "plan": {"run_id": "r1"}}
    )
    hint = _republish_hint(args, result)
    assert hint is not None
    return hint


@pytest.mark.parametrize(
    "option",
    [
        "endpoint=https://user:hunter2@host/p",
        "endpoint=https://host/p?sig=hunter2&x=1",
        "endpoint=host/p?access_token=hunter2",
        "account=user:hunter2@acct/db/schema",
        "uri=host=h password=hunter2",
        "uri=file:x.db?mode=ro",
        "db.adbc.snowflake.sql.account=hunter2",
    ],
)
def test_the_republish_command_prints_the_value_of_an_option_only_when_its_key_holds_no_secret(
    option: str,
) -> None:
    hint = _hint("memory:here", [option])
    key = option.partition("=")[0]
    assert "hunter2" not in hint
    assert hint.endswith(f"--sink-opt '{key}=…'")
    assert "replacing each …" in hint


def test_the_republish_command_runs_as_printed_when_nothing_was_withheld() -> None:
    hint = _hint(
        "adbc:sqlite", ["uri-env=SIM_URI", "db.token-env=SIM_TOKEN", "mode=append", "verify=true"]
    )
    assert hint.endswith(
        "--sink adbc:sqlite --sink-opt uri-env=SIM_URI --sink-opt db.token-env=SIM_TOKEN "
        "--sink-opt mode=append --sink-opt verify=true"
    )
    assert "…" not in hint and "withheld" not in hint
    assert _hint("parquet:out/run", []).endswith("--sink parquet:out/run")


@pytest.mark.parametrize(
    "sink, shown",
    [
        ("adbc:u:hunter2@h:5432/db", "adbc:adbc target sha256:6ab813b82612"),
        ("parquet:postgresql://u:hunter2@h/db?x=1", "parquet:postgresql://…@h/db?…"),
        ("adbc:host=h password=hunter2", "adbc:host=h (1 setting withheld)"),
    ],
)
def test_the_republish_command_marks_a_sink_target_that_was_withheld(sink: str, shown: str) -> None:
    hint = _hint(sink, [])
    assert "hunter2" not in hint
    assert hint.endswith(f"--sink {shlex.quote(shown)}")
    assert "replacing each …" in hint


def test_the_options_that_never_hold_a_secret_are_the_built_in_sinks_own() -> None:
    from flux_engine.sinks import adbc, arrow_ipc, parquet
    from flux_engine.sinks._options import PLAIN_OPTIONS

    pytest.importorskip("deltalake")
    from flux_engine.sinks import delta

    options = {*parquet.OPTIONS, *arrow_ipc.OPTIONS, *adbc.OPTIONS, *delta.OPTIONS}
    assert PLAIN_OPTIONS | {"uri", "uri-env"} == options
    assert not any(credential_key(name) for name in PLAIN_OPTIONS)


def test_the_republish_command_never_prints_what_an_environment_variable_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SIM_URI", "postgresql://db/hunter2-database")
    monkeypatch.setenv("SIM_TOKEN", "hunter2-token-value")
    hint = _hint("adbc:postgresql", ["uri-env=SIM_URI", "db.password-env=SIM_TOKEN"])
    assert "uri-env=SIM_URI" in hint and "db.password-env=SIM_TOKEN" in hint
    assert "hunter2" not in hint
    # A credential's value that reached the command by another route is hidden too.
    assert "hunter2" not in _hint("memory:hunter2-token-value", [])


def test_scrub_leaves_the_working_directory_in_a_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PWD", "/work/sim-project")
    monkeypatch.setenv("OLDPWD", "/work/previous-project")
    message = "wrote /work/sim-project/out and read /work/previous-project/in"
    assert scrub(message) == message


# ---------------------------------------------------------------------------
# Every DSN form of an inline secret
# ---------------------------------------------------------------------------

#: A secret in each connection-string form; ``FAKEVAL1`` is the fake value that must never be printed.
_DSN_FORMS = [
    "postgresql://user:FAKEVAL1@db:5432/sim",
    "//user:FAKEVAL1@db/sim",
    "user:FAKEVAL1@account/db/schema",
    "postgresql://db/sim?password=FAKEVAL1",
    "postgresql://db/sim?sslmode=require&sig=FAKEVAL1",
    "file:x.db?mode=rwc;sig=FAKEVAL1",
    "file:x.db?mode=rwc;X-Amz-Signature=FAKEVAL1&a=b",
    "file:x.db#access_token=FAKEVAL1",
    "file:x.db?a=b#access_token=FAKEVAL1",
    "host=127.0.0.1 dbname=x password=FAKEVAL1",
    "host=127.0.0.1 dbname=x password='FAKEVAL1 with spaces'",
    "host=127.0.0.1 dbname=x password = FAKEVAL1",
    "host=127.0.0.1 PASSWORD=FAKEVAL1",
    "host=127.0.0.1 passwd=FAKEVAL1",
    "host=127.0.0.1 sslpassword=FAKEVAL1",
    "host=127.0.0.1 sslkey=FAKEVAL1",
    "host=127.0.0.1 passfile=FAKEVAL1",
    "host=127.0.0.1 pass-phrase=FAKEVAL1",
    "Server=db;Database=x;PWD=FAKEVAL1;",
    "Server=db;Database=x;Password=FAKEVAL1",
    "Driver={ODBC};Server=db;Pwd={FAKEVAL1;with;semicolons}",
    "Server=db;Authorization=FAKEVAL1",
    "file:x.db?subscription-key=FAKEVAL1",
    "file:x.db?a=b&license_key=FAKEVAL1",
    "file:x.db#licenseKey=FAKEVAL1",
    "host=127.0.0.1 ssh-key=FAKEVAL1",
    "Server=db;Encryption_Key=FAKEVAL1",
]

_HARMLESS_DSNS = [
    "host=127.0.0.1 dbname=x",
    "host=127.0.0.1 dbname=x password=",
    "file:x.db?mode=ro",
    "file:x.db?mode=rwc;cache=shared",
    "?page=2",
    "sim.sqlite",
    "/data/sim.sqlite",
    "C:\\data\\sim@2.db",
    "postgresql://db/sim?sslmode=require&application_name=flux",
    "postgresql://user@db/sim",
    "Server=db;Database=x;Trusted_Connection=yes",
    "host=db password-env=SIM_PW",
    "host=db keyspace=sim keyfile=none monkey=1",
    "file:x.db?keyspace=a;monkey=b",
]


@pytest.mark.parametrize("dsn", _DSN_FORMS)
def test_every_dsn_form_of_an_inline_secret_is_flagged_and_labelled_without_it(dsn: str) -> None:
    from flux_engine.core.credentials import credential_dsn

    assert credential_dsn(dsn) is True
    label = target_label("adbc", dsn)
    assert "FAKEVAL1" not in label and "semicolons" not in label and "spaces" not in label
    echoed = scrub(f"driver said: {dsn} was refused", [dsn], sink="adbc")
    assert "FAKEVAL1" not in echoed and label in echoed


@pytest.mark.parametrize(
    "dsn, shown",
    [
        ("host=127.0.0.1 dbname=x", "host=127.0.0.1 dbname=x"),
        ("host=127.0.0.1 dbname=x password=", "host=127.0.0.1 dbname=x (1 setting withheld)"),
        ("file:x.db?mode=ro", "file:x.db?…"),
        ("file:x.db?mode=rwc;cache=shared", "file:x.db?…"),
        ("sim.sqlite", "adbc target sha256:15cbdb1dfd8b"),
        ("/data/sim.sqlite", "adbc target sha256:5d28bcb3971b"),
        ("postgresql://db/sim?sslmode=require&application_name=flux", "postgresql://db/sim?…"),
        ("postgresql://user@db/sim", "postgresql://…@db/sim"),
        (
            "Server=db;Database=x;Trusted_Connection=yes",
            "Server=db;Database=x (1 setting withheld)",
        ),
        ("Server=h;Database=d;Uid=u;Pwd=x", "Server=h;Database=d (2 settings withheld)"),
    ],
)
def test_a_harmless_dsn_is_labelled_by_what_it_shows(dsn: str, shown: str) -> None:
    assert target_label("adbc", dsn) == shown


@pytest.mark.parametrize(
    "dsn",
    [
        "host=h password\n=\nFAKEVAL1",
        "host=h password\x0b=\x0cFAKEVAL1",
        "host=h\npassword\r=FAKEVAL1",
        "postgresql://h/db?pass%77ord=FAKEVAL1",
        "postgresql://h/db?%70assword=FAKEVAL1",
        "host=h pass%77ord=FAKEVAL1",
        "Server=h;%50wd=FAKEVAL1",
    ],
)
def test_the_refusal_reads_white_space_around_an_equals_sign_and_percent_encoded_names(
    dsn: str,
) -> None:
    from flux_engine.core.credentials import credential_dsn

    assert credential_dsn(dsn) is True


@pytest.mark.parametrize(
    "message",
    [
        "could not connect: host=127.0.0.1 dbname=x password=FAKEVAL1 failed",
        "bad uri 'file:x.db?mode=rwc;sig=FAKEVAL1'",
        "bad target //user:FAKEVAL1@db/sim",
    ],
)
def test_scrub_replaces_the_known_target_and_never_searches(message: str) -> None:
    assert scrub(message) == message
    raw = message.split("'")[1] if "'" in message else message.split(": ")[-1].split(" failed")[0]
    raw = raw.replace("bad target ", "")
    assert "FAKEVAL1" not in scrub(message, [raw], sink="adbc")


@pytest.mark.parametrize("dsn", _DSN_FORMS)
def test_the_republish_command_never_echoes_a_secret_of_any_dsn_form(dsn: str) -> None:
    for hint in (
        _hint("adbc:sqlite", [f"uri={dsn}"]),
        _hint("adbc:sqlite", [f"endpoint={dsn}"]),
        _hint(f"adbc:{dsn}", []),
    ):
        assert "FAKEVAL1" not in hint
        assert "semicolons" not in hint and "spaces" not in hint


def test_the_republish_command_shows_no_option_value_of_a_key_that_may_hold_a_secret() -> None:
    hint = _hint("adbc:sqlite", ["uri=host=127.0.0.1 dbname=x", "mode=append"])
    assert "'uri=…'" in hint and "mode=append" in hint
    assert "FAKEVAL1" not in _hint("adbc:sqlite", ["password=FAKEVAL1"])


def test_a_receipt_prints_and_records_its_target_without_a_secret() -> None:
    for dsn in ("sqlite:host=h password=FAKEVAL1", "postgresql://u:FAKEVAL1@db/sim"):
        receipt = SinkReceipt(
            sink="adbc",
            run_id="r",
            expose_id="e",
            output_digest="sha256:" + "0" * 64,
            rows=1,
            target=dsn,
            table="t",
        )
        assert "FAKEVAL1" not in str(receipt.as_document()) + deliver.receipt_line(receipt)


def test_the_working_directory_variables_stay_unmasked_in_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PWD", "/work/FAKEVAL1-dir")
    monkeypatch.setenv("OLDPWD", "/work/FAKEVAL2-dir")
    message = "wrote /work/FAKEVAL1-dir/out and read /work/FAKEVAL2-dir/in"
    assert scrub(message) == message


@pytest.mark.parametrize("name", ["subscription-key", "license_key", "ssh-key", "licenseKey"])
def test_a_key_word_is_a_credential_in_a_connection_string_only(name: str) -> None:
    from flux_engine.core.credentials import credential_option, credential_query

    assert credential_query(f"a=1&{name}=FAKEVAL1") is True
    assert credential_option(name) is False  # a plugin option such as sort_key stays accepted


def _receipt(
    target: str, *, files: bool = False, sink: str = "parquet", key: str = "e/part-0.parquet"
) -> SinkReceipt:
    from flux_engine.sinks.receipts import FileEntry

    return SinkReceipt(
        sink=sink,
        run_id="r",
        expose_id="e",
        output_digest="sha256:" + "0" * 64,
        rows=2,
        target=target,
        files=((FileEntry(path=key, sha256="0" * 64, bytes=1, rows=2),) if files else ()),
        table=None if files else "t",
    )


def test_the_delivered_line_names_an_object_stores_whole_key_prefix() -> None:
    # An object target's file paths are whole keys, prefix included.
    line = deliver.receipt_line(
        _receipt("s3://bkt/pfx/run-a", files=True, key="pfx/run-a/e/part-0.parquet")
    )
    assert line.endswith("under s3://bkt/pfx/run-a/e/")


def test_the_delivered_line_names_a_local_directory_as_typed() -> None:
    assert deliver.receipt_line(_receipt("out/run", files=True)).endswith("under out/run/e/")
    assert deliver.receipt_line(_receipt("/abs/dir", files=True)).endswith("under /abs/dir/e/")


def test_a_receipt_shows_and_records_a_target_only_as_its_label() -> None:
    for raw, label in (
        ("file:x.db?mode=ro", "file:x.db?…"),
        ("Server=h;Database=d;Uid=u;Pwd=x", "Server=h;Database=d (2 settings withheld)"),
        ("postgresql://u:pw@h:5432/db", "postgresql://…@h:5432/db"),
        (f"//u:{'Zq7FAKEVAL1xY'}@h:5432/db", "//…@h:5432/db"),
    ):
        receipt = _receipt(raw)
        assert receipt.as_document()["target"] == label
        assert deliver.receipt_line(receipt).endswith(f"at {label}")


def test_a_target_a_sink_opened_carries_what_a_library_may_echo_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "https://u:Zq7FAKEVAL1xY@host/x?sig=abc"

    class Failing:
        name = "failing"

        def sink_for(self, context: StreamContext) -> Any:
            raise RuntimeError(f"no route to {raw} or to {raw.replace('://', ':/')}")

    monkeypatch.setattr(registry, "load_factory", lambda name: lambda target, options: Failing())
    opened = registry.open_target("failing:here", {"endpoint": raw})
    assert registry.known_of(opened) == ("here", (raw, target_label("option", raw)))
    with pytest.raises(FluxError) as caught:
        deliver._sinks_for(opened, [_context()])
    assert "Zq7F" not in str(caught.value) and "sig=abc" not in str(caught.value)
    assert "https://…@host/x?…" in str(caught.value)
