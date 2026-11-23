# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Object store targets for the Parquet and Arrow IPC sinks, with no network.

The store constructor is replaced by one that returns obstore's in-memory store,
so what these tests read back is what an upload would have stored.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks import registry
from flux_engine.sinks._objects import (
    ObjectLocation,
    Upload,
    is_object_target,
    parse_object_target,
    require_key_in_prefix,
    upload_files,
)

from .conftest import TEST_CREDENTIALS, pyarrow_or_skip
from .test_files import Stream, _deliver

pyarrow_or_skip()
obstore = pytest.importorskip("obstore")
obstore_store = pytest.importorskip("obstore.store")
from flux_engine.sinks import arrow_ipc, parquet  # noqa: E402  (after the importorskip)

pytestmark = pytest.mark.unit

TARGETS = {"parquet": parquet.target, "arrow-ipc": arrow_ipc.target}


class FromUrl:
    """A stand-in for ``obstore.store.from_url`` that records its calls."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.calls: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.store


@pytest.fixture
def from_url(monkeypatch: pytest.MonkeyPatch) -> FromUrl:
    stand_in = FromUrl(obstore_store.MemoryStore())
    monkeypatch.setattr(obstore_store, "from_url", stand_in)
    return stand_in


@pytest.fixture(params=sorted(TARGETS))
def kind(request: Any) -> str:
    return str(request.param)


def _stored(store: Any) -> Dict[str, bytes]:
    found: Dict[str, bytes] = {}
    for batch in obstore.list(store):
        for item in batch:
            found[item["path"]] = bytes(obstore.get(store, item["path"]).bytes())
    return found


def _deliver_to(kind: str, target: str, stream: Stream) -> Any:
    sink = TARGETS[kind](target, {}).sink_for(stream.context)
    receipt = _deliver(sink, stream.header, stream.rows)
    assert receipt is not None
    return sink, receipt


def test_the_objects_are_the_files_a_local_target_writes(
    kind: str, from_url: FromUrl, tmp_path: Path
) -> None:
    stream = Stream(40)
    _, local = _deliver_to(kind, str(tmp_path / "local"), stream)
    sink, receipt = _deliver_to(kind, "s3://bucket/runs/a/", stream)

    stored = _stored(from_url.store)
    expose = stream.context.expose_id
    prefix = f"runs/a/{expose}/"
    manifest = f"{prefix}_manifest-{stream.context.slice_key}-{kind}.json"
    expected = [f"runs/a/{entry.path}" for entry in local.files] + [manifest]
    assert sorted(stored) == sorted(expected)
    for entry in local.files:
        assert stored[f"runs/a/{entry.path}"] == (tmp_path / "local" / entry.path).read_bytes()
    assert stored[manifest] == (tmp_path / "local" / manifest[len("runs/a/") :]).read_bytes()

    assert receipt.target == "s3://bucket/runs/a"
    assert [entry.path for entry in receipt.files] == [f"runs/a/{e.path}" for e in local.files]
    for entry, mine in zip(local.files, receipt.files, strict=True):
        body = stored[mine.path]
        assert (mine.bytes, mine.sha256, mine.rows) == (
            len(body),
            hashlib.sha256(body).hexdigest(),
            entry.rows,
        )
        assert (mine.bytes, mine.sha256) == (entry.bytes, entry.sha256)
    assert receipt.writer["upload"] == f"obstore {obstore.__version__}"
    assert receipt.writer["library"] == "pyarrow"
    assert not sink.directory.exists(), "the staging directory is removed"


def test_the_store_is_built_from_the_bucket_alone(kind: str, from_url: FromUrl) -> None:
    _deliver_to(kind, "gs://the-bucket/a/b", Stream(3))
    assert from_url.calls, "the store is built"
    assert all(call == (("gs://the-bucket",), {}) for call in from_url.calls)


@pytest.mark.parametrize("scheme", ["s3", "gs", "az", "abfs"])
def test_every_scheme_is_an_object_target(scheme: str) -> None:
    location = parse_object_target(f"{scheme}://bucket/p")
    assert (location.scheme, location.bucket, location.prefix) == (scheme, "bucket", "p")
    assert location.store_url == f"{scheme}://bucket"
    assert is_object_target(f"{scheme}://bucket/p")


@pytest.mark.parametrize("target", ["out/", "../x", "C:\\out", "http://host/p", "file:///tmp/x"])
def test_anything_else_is_a_local_target(target: str) -> None:
    assert not is_object_target(target)


@pytest.mark.parametrize("target", ["out", "relative/dir"])
def test_a_local_target_needs_no_obstore(
    kind: str, target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "obstore", None)
    monkeypatch.chdir(tmp_path)
    _, receipt = _deliver_to(kind, target, Stream(3))
    assert receipt.target == target
    assert "upload" not in receipt.writer


@pytest.mark.parametrize(
    "target, prefix",
    [
        ("s3://b/a", "a"),
        ("s3://b/a/", "a"),
        ("s3://b/a/b/c", "a/b/c"),
        ("s3://b/a.b/c-d_e", "a.b/c-d_e"),
        ("S3://b/a", "a"),
    ],
)
def test_the_prefix_is_the_segments_joined(target: str, prefix: str) -> None:
    assert parse_object_target(target).prefix == prefix


@pytest.mark.parametrize(
    "target",
    [
        "s3://b",
        "s3://b/",
        "s3://b//",
        "s3://b//a",
        "s3://b/a//b",
        "s3://b/a//",
        "s3://b/./a",
        "s3://b/a/./b",
        "s3://b/a/.",
        "s3://b/..",
        "s3://b/a/../b",
        "s3://b/a/..",
        "s3://b/a\\b",
        "s3://b/\\",
        "s3://b/a%2Fb",
        "s3://b/a%2fb",
        "s3://b/a%5Cb",
        "s3://b/a%5cb",
        "s3://b/%2E%2E/b",
        "s3://b/%2e/b",
        "s3://b/a/%2e%2e",
        "s3://b/a%2Eb",
        "s3:///a",
        "s3://bu cket/a",
        "s3://b:9000/a",
        "az://%2e/a",
    ],
)
def test_a_prefix_that_could_leave_its_place_is_refused(kind: str, target: str) -> None:
    with pytest.raises(FluxError) as caught:
        parse_object_target(target)
    assert caught.value.code.code == "FLX-3510"
    with pytest.raises(FluxError) as through_factory:
        TARGETS[kind](target, {})
    assert through_factory.value.code.code == "FLX-3510"


def test_a_refused_target_writes_nothing(kind: str, from_url: FromUrl) -> None:
    with pytest.raises(FluxError):
        TARGETS[kind]("s3://b/a/../b", {})
    assert from_url.calls == [] and _stored(from_url.store) == {}


def test_a_key_must_start_with_the_prefix_and_a_slash() -> None:
    require_key_in_prefix("runs/a/x.parquet", "runs/a")
    for key in ("other/x", "runs/ab/x", "runs/a", "/runs/a/x", "x/runs/a/x"):
        with pytest.raises(FluxError) as caught:
            require_key_in_prefix(key, "runs/a")
        assert caught.value.code.code == "FLX-3510"


@pytest.mark.parametrize("relative", ["", "/abs/x", "../x", "a/../../x", "a//b", "./x", "a\\b"])
def test_a_relative_name_is_checked_before_it_becomes_a_key(relative: str) -> None:
    location = ObjectLocation("s3", "b", "p")
    with pytest.raises(FluxError) as caught:
        location.key(relative)
    assert caught.value.code.code == "FLX-3510"
    assert location.key("expose/x.parquet") == "p/expose/x.parquet"


def test_nothing_is_uploaded_when_a_key_is_outside_the_prefix(
    from_url: FromUrl, tmp_path: Path
) -> None:
    (tmp_path / "f").write_bytes(b"x")
    location = ObjectLocation("s3", "b", "p")
    with pytest.raises(FluxError) as caught:
        upload_files(location, [Upload(tmp_path / "f", "p/ok"), Upload(tmp_path / "f", "q/bad")])
    assert caught.value.code.code == "FLX-3510"
    assert from_url.calls == [] and _stored(from_url.store) == {}


def test_a_failed_upload_is_the_engines_error_scrubbed_and_unchained(
    kind: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIsecretvalue")

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "PUT https://user:hunter2@host.example/b/x?X-Amz-Signature=abcd1234 failed "
            "with wJalrXUtnFEMIsecretvalue"
        )

    monkeypatch.setattr(obstore, "put", refuse)
    stream = Stream(3)
    sink = TARGETS[kind]("s3://b/p", {}).sink_for(stream.context)
    sink.open(stream.header)
    for row in stream.rows:
        sink.write(row)
    with pytest.raises(FluxError) as caught:
        sink.close()
    error = caught.value
    assert error.code.code == "FLX-3505"
    assert error.__cause__ is None and error.__suppress_context__
    text = str(error)
    for leak in ("hunter2", "abcd1234", "wJalrXUtnFEMIsecretvalue"):
        assert leak not in text
    assert "RuntimeError" in text and "s3://b/p" in text
    assert sink.receipt is None
    assert not sink.directory.exists()


@pytest.mark.parametrize("name", ["parquet", "arrow-ipc"])
def test_without_obstore_an_object_target_names_the_extra(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "obstore", None)
    with pytest.raises(FluxError) as caught:
        registry.open_target(f"{name}:s3://b/p")
    assert caught.value.code.code == "FLX-3501"
    assert "flux-sim-engine[publish]" in str(caught.value)


def test_the_options_stay_closed(kind: str) -> None:
    module = parquet if kind == "parquet" else arrow_ipc
    assert "endpoint" not in module.OPTIONS
    with pytest.raises(FluxError):
        TARGETS[kind]("s3://b/p", {"endpoint": "http://elsewhere"})
    tuning = ("connect-timeout", "max-retries", "request-timeout", "retry-timeout")
    assert module.OPTIONS == tuple(
        sorted(
            (
                "batch-size",
                "compression",
                "compression-level",
                "instance-identity",
                "row-group-size",
                "rows-per-file",
                "verify",
                *tuning,
            )
            if kind == "parquet"
            else (
                "batch-size",
                "compression",
                "instance-identity",
                "rows-per-file",
                "verify",
                *tuning,
            )
        )
    )


@pytest.mark.parametrize(
    "target", ["s3://user:pass@bucket/p", "az://acct/c?sig=abc", "gs://bucket/p#frag"]
)
@pytest.mark.parametrize("name", ["parquet", "arrow-ipc"])
def test_a_credential_bearing_object_target_is_refused_before_any_sink_runs(
    name: str, target: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: List[str] = []
    monkeypatch.setattr(registry, "load_factory", lambda sink: ran.append(sink))
    with pytest.raises(FluxError) as caught:
        registry.open_target(f"{name}:{target}")
    assert caught.value.code.code == "FLX-3509"
    assert ran == [] and from_url.calls == []


_HOSTILE = "x\x1b[2J\nFLX-0000: forged‮"


def _inert(text: str) -> None:
    assert "\x1b" not in text and "‮" not in text
    assert len(text.strip().splitlines()) == 1


def test_a_refusal_never_prints_a_prefix_or_a_remote_message_raw(
    from_url: FromUrl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location = ObjectLocation("s3", "b", _HOSTILE)
    with pytest.raises(FluxError) as caught:
        location.key("../x")
    assert caught.value.code.code == "FLX-3510"
    _inert(str(caught.value))

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("\x1b[2J\nFLX-0000: forged‮")

    monkeypatch.setattr(obstore, "put", refuse)
    (tmp_path / "f").write_bytes(b"x")
    with pytest.raises(FluxError) as failed:
        upload_files(location, [Upload(tmp_path / "f", f"{_HOSTILE}/ok")])
    assert failed.value.code.code == "FLX-3505"
    _inert(str(failed.value))


def test_the_delivered_line_names_the_key_once_for_an_object_target(
    kind: str, from_url: FromUrl, tmp_path: Path
) -> None:
    from flux_engine.stages.deliver import receipt_line

    stream = Stream(10)
    _, receipt = _deliver_to(kind, "s3://bucket/runs/a", stream)
    expose = stream.context.expose_id
    assert receipt_line(receipt).endswith(f" under s3://bucket/runs/a/{expose}/")
    _, local = _deliver_to(kind, str(tmp_path / "local"), stream)
    assert receipt_line(local).endswith(f" under {tmp_path / 'local'}/{expose}/")


def _clear_store_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in list(os.environ):
        if name.startswith(("AWS_", "AZURE_", "GOOGLE_")):
            monkeypatch.delenv(name)


def test_an_object_store_that_cannot_be_set_up_fails_before_any_row_is_generated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flux_engine.stages.run import run

    from .test_run_with_sink import FIXTURE

    _clear_store_settings(monkeypatch)
    state = tmp_path / "state"
    for scheme in ("az", "abfs"):
        result = run(FIXTURE, state_dir=state, sink=f"parquet:{scheme}://box/p")
        assert not result.success and result.error_code == "FLX-3505"
        assert "AZURE_" in result.stderr and f"{scheme}://box/p" in result.stderr
        assert not (state / "runs").exists(), "no run was generated"


@pytest.mark.parametrize(
    ("scheme", "message", "names"),
    [
        (
            "s3",
            "Error performing PUT http://169.254.169.254/latest/api/token in 1s after 10 retries",
            ("AWS_ACCESS_KEY_ID", "AWS_ALLOW_HTTP"),
        ),
        ("s3", "HTTP error: builder error", ("AWS_ENDPOINT_URL", "AWS_ALLOW_HTTP")),
        ("gs", "Generic GCS error: no credentials", ("GOOGLE_APPLICATION_CREDENTIALS",)),
        ("abfs", "Generic MicrosoftAzure error: Account must be specified", ("AZURE_STORAGE_",)),
    ],
)
def test_a_credential_or_endpoint_failure_names_the_settings_for_its_scheme(
    scheme: str,
    message: str,
    names: Tuple[str, ...],
    from_url: FromUrl,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMIsecretvalue")

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(message + " wJalrXUtnFEMIsecretvalue")

    monkeypatch.setattr(obstore, "put", refuse)
    stream = Stream(3)
    sink = TARGETS["parquet"](f"{scheme}://b/p", {}).sink_for(stream.context)
    sink.open(stream.header)
    for row in stream.rows:
        sink.write(row)
    with pytest.raises(FluxError) as caught:
        sink.close()
    text = str(caught.value)
    assert caught.value.code.code == "FLX-3505"
    for name in names:
        assert name in text
    assert "wJalrXUtnFEMIsecretvalue" not in text
    other = {"s3": "GOOGLE_", "gs": "AWS_", "abfs": "GOOGLE_"}[scheme]
    assert other not in text


def test_a_failed_publish_says_the_run_was_kept_and_how_to_publish_it_again(
    tmp_path: Path,
    from_url: FromUrl,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from flux_engine.cli import main

    from .test_run_with_sink import FIXTURE

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Error performing PUT http://169.254.169.254/latest/api/token")

    monkeypatch.setattr(obstore, "put", refuse)
    state = tmp_path / "st"
    code = main(["run", str(FIXTURE), "--state-dir", str(state), "--sink", "parquet:s3://b/p"])
    err = capsys.readouterr().err
    assert code == 1
    [run_id] = [entry.name for entry in (state / "runs").iterdir()]
    assert f"the run {run_id} was kept" in err
    command = (
        f"fluxsim apply {FIXTURE} --run-id {run_id} --sink parquet:s3://b/p --state-dir {state}"
    )
    assert command in err
    # The command it prints publishes the run for real.
    monkeypatch.undo()
    for name, value in TEST_CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(obstore_store, "from_url", from_url)
    assert main(command.split()[1:]) == 0
    assert any(key.startswith("p/payment_recovery_moment/") for key in _stored(from_url.store))


def test_a_failed_publish_of_an_apply_without_a_run_id_says_which_run_was_kept(
    tmp_path: Path,
    from_url: FromUrl,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from flux_engine.cli import main

    from .test_run_with_sink import FIXTURE

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Error performing PUT http://169.254.169.254/latest/api/token")

    monkeypatch.setattr(obstore, "put", refuse)
    state = tmp_path / "st"
    code = main(["apply", str(FIXTURE), "--state-dir", str(state), "--sink", "parquet:s3://b/p"])
    err = capsys.readouterr().err
    assert code == 1
    [run_id] = [entry.name for entry in (state / "runs").iterdir()]
    assert f"the run {run_id} was kept" in err
    command = (
        f"fluxsim apply {FIXTURE} --run-id {run_id} --sink parquet:s3://b/p --state-dir {state}"
    )
    assert command in err


# ---------------------------------------------------------------------------
# The ADLS Gen2 form: abfs://CONTAINER@ACCOUNT.dfs.core.windows.net/PREFIX
# ---------------------------------------------------------------------------

ADLS = "abfs://data@acct1.dfs.core.windows.net/sims/a"


def test_the_adls_gen2_form_maps_container_account_and_prefix() -> None:
    location = parse_object_target(ADLS)
    assert (location.scheme, location.bucket, location.account, location.prefix) == (
        "abfs",
        "data",
        "acct1",
        "sims/a",
    )
    assert location.store_url == "abfs://data@acct1.dfs.core.windows.net"
    assert location.url == ADLS
    assert location.key("e/f.parquet") == "sims/a/e/f.parquet"
    # obstore reads the same container and account from what the engine hands it.
    store = obstore_store.from_url(location.store_url)
    assert store.config["container_name"] == "data"
    assert store.config["account_name"] == "acct1"


@pytest.mark.parametrize(
    "target",
    [
        "abfs://@acct1.dfs.core.windows.net/p",
        "abfs://acct1.dfs.core.windows.net/p",
        "abfs://data@example.com/p",
        "abfs://data@acct1.dfs.core.windows.net:8080/p",
        "abfs://data@ac.dfs.core.windows.net/p",
        "abfs://da ta@acct1.dfs.core.windows.net/p",
        "abfs://data@acct1.dfs.core.windows.net",
        "abfs://data@acct1.dfs.core.windows.net/a/../b",
        "s3://data@acct1.dfs.core.windows.net/p",
    ],
)
def test_a_form_that_names_no_container_or_account_is_still_refused(target: str) -> None:
    with pytest.raises(FluxError) as caught:
        parse_object_target(target)
    assert caught.value.code.code == "FLX-3510"


def test_the_adls_form_passes_the_credential_check_and_is_delivered(
    kind: str, from_url: FromUrl
) -> None:
    stream = Stream(5)
    sink = TARGETS[kind](ADLS, {}).sink_for(stream.context)
    receipt = _deliver(sink, stream.header, stream.rows)
    assert receipt is not None and receipt.target == ADLS
    assert all(call == (("abfs://data@acct1.dfs.core.windows.net",), {}) for call in from_url.calls)
    assert all(key.startswith("sims/a/") for key in _stored(from_url.store))


@pytest.mark.parametrize(
    "target",
    [
        "abfs://data:pw@acct1.dfs.core.windows.net/p",
        "abfs://data@acct1.dfs.core.windows.net/p?sig=abc",
        "abfs://data@acct1.dfs.core.windows.net/p#frag",
        "abfs://data@example.com/p",
    ],
)
def test_user_information_is_still_refused_beside_the_adls_form(
    target: str, from_url: FromUrl
) -> None:
    with pytest.raises(FluxError) as caught:
        registry.open_target(f"parquet:{target}")
    assert caught.value.code.code in ("FLX-3509", "FLX-3510")
    assert from_url.calls == []


# ---------------------------------------------------------------------------
# Tuning the client: timeouts and retries
# ---------------------------------------------------------------------------


def test_the_client_options_reach_the_store(kind: str, from_url: FromUrl) -> None:
    from datetime import timedelta

    TARGETS[kind](
        "s3://bucket/p",
        {
            "request-timeout": "20",
            "connect-timeout": "5",
            "max-retries": "2",
            "retry-timeout": "30",
        },
    )
    assert from_url.calls[-1] == (
        ("s3://bucket",),
        {
            "client_options": {
                "timeout": timedelta(seconds=20),
                "connect_timeout": timedelta(seconds=5),
            },
            "retry_config": {"max_retries": 2, "retry_timeout": timedelta(seconds=30)},
        },
    )


def test_a_zero_retry_budget_turns_retries_off(from_url: FromUrl) -> None:
    parquet.target("s3://bucket/p", {"max-retries": "0"})
    assert from_url.calls[-1][1] == {"retry_config": {"max_retries": 0}}


def test_obstore_itself_accepts_the_options_the_engine_passes() -> None:
    from datetime import timedelta

    store = obstore_store.from_url(
        "s3://bucket",
        client_options={"timeout": timedelta(seconds=20), "connect_timeout": timedelta(seconds=5)},
        retry_config={"max_retries": 2, "retry_timeout": timedelta(seconds=30)},
        config={"region": "us-east-1"},
    )
    assert type(store).__name__ == "S3Store"


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("request-timeout", "0"),
        ("connect-timeout", "-3"),
        ("retry-timeout", "soon"),
        ("request-timeout", "99999"),
        ("max-retries", "-1"),
        ("max-retries", "many"),
        ("max-retries", "1000"),
    ],
)
def test_a_bad_tuning_option_is_refused_by_name(kind: str, option: str, value: str) -> None:
    with pytest.raises(FluxError) as caught:
        TARGETS[kind]("s3://bucket/p", {option: value})
    assert caught.value.code.code == "FLX-3504" and option in str(caught.value)


def test_a_tuning_option_on_a_local_directory_is_refused(kind: str, tmp_path: Path) -> None:
    with pytest.raises(FluxError) as caught:
        TARGETS[kind](str(tmp_path), {"retry-timeout": "10"})
    assert caught.value.code.code == "FLX-3504" and "object store" in str(caught.value)
    assert not list(tmp_path.iterdir())


def test_the_failure_message_says_how_to_shorten_the_wait(
    from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("error sending request for url (https://x.example/): timed out")

    monkeypatch.setattr(obstore, "put", refuse)
    with pytest.raises(FluxError) as caught:
        upload_files(ObjectLocation("s3", "b", "p"), [Upload(Path(__file__), "p/x")])
    text = str(caught.value)
    assert "retry-timeout" in text and "max-retries" in text and "3 minutes" in text


# ---------------------------------------------------------------------------
# A failed upload and the objects it already wrote
# ---------------------------------------------------------------------------


class Flaky:
    """``obstore.put`` and ``delete`` that fail on chosen calls, over a real in-memory store."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, put_fails_on: int, delete_fails: bool = False
    ) -> None:
        self.puts = 0
        self.put_fails_on = put_fails_on
        self.delete_fails = delete_fails
        self.deletes: List[str] = []
        real_put, real_delete = obstore.put, obstore.delete
        flaky = self

        def put(store: Any, key: str, data: Any, **kwargs: Any) -> Any:
            flaky.puts += 1
            if flaky.puts == flaky.put_fails_on:
                raise RuntimeError("Error performing PUT: connection closed")
            return real_put(store, key, data, **kwargs)

        def delete(store: Any, key: Any, **kwargs: Any) -> Any:
            flaky.deletes.append(key)
            if flaky.delete_fails:
                raise RuntimeError("Error performing DELETE: connection closed")
            return real_delete(store, key, **kwargs)

        monkeypatch.setattr(obstore, "put", put)
        monkeypatch.setattr(obstore, "delete", delete)


def _failing_delivery(kind: str, rows: int = 40) -> FluxError:
    stream = Stream(rows)
    sink = TARGETS[kind]("s3://bucket/p", {"rows-per-file": "10"}).sink_for(stream.context)
    with pytest.raises(FluxError) as caught:
        _deliver(sink, stream.header, stream.rows)
    assert sink.receipt is None
    return caught.value


def test_a_failed_upload_deletes_the_data_objects_it_wrote(
    kind: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = Flaky(monkeypatch, put_fails_on=3)
    error = _failing_delivery(kind)
    assert error.code.code == "FLX-3505"
    assert "2 data objects this delivery had written were deleted" in str(error)
    assert _stored(from_url.store) == {}
    assert len(flaky.deletes) == 2


def test_a_failed_manifest_upload_deletes_every_data_object(
    kind: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    Flaky(monkeypatch, put_fails_on=5)  # four data files, then the manifest
    error = _failing_delivery(kind)
    assert "4 data objects this delivery had written were deleted" in str(error)
    assert _stored(from_url.store) == {}


def test_a_cleanup_that_fails_names_the_keys_that_remain(
    kind: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = Flaky(monkeypatch, put_fails_on=3, delete_fails=True)
    error = _failing_delivery(kind)
    text = str(error)
    left = sorted(_stored(from_url.store))
    assert len(left) == 2
    assert "could not be deleted" in text
    for key in left:
        assert key in text
    assert len(flaky.deletes) == 1, "one failed delete stops the cleanup: the endpoint is down"


def test_a_complete_earlier_delivery_is_never_cleaned_up(
    kind: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = Stream(40)
    sink = TARGETS[kind]("s3://bucket/p", {"rows-per-file": "10"}).sink_for(stream.context)
    assert _deliver(sink, stream.header, stream.rows) is not None
    before = _stored(from_url.store)
    flaky = Flaky(monkeypatch, put_fails_on=3)
    error = _failing_delivery(kind)
    assert _stored(from_url.store) == before
    assert flaky.deletes == []
    assert "earlier delivery" in str(error) and "kept" in str(error)


def test_a_failure_before_anything_is_written_has_nothing_to_clean_up(
    kind: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = Flaky(monkeypatch, put_fails_on=1)
    error = _failing_delivery(kind)
    assert flaky.deletes == [] and "deleted" not in str(error)


def test_a_manifest_put_that_commits_and_then_raises_keeps_the_data_objects(
    kind: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_put = obstore.put
    calls = {"n": 0}

    def put(store: Any, key: str, data: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        result = real_put(store, key, data, **kwargs)
        if calls["n"] == 5:  # four data files, then the manifest
            raise RuntimeError("Error performing PUT: timed out after the server stored it")
        return result

    monkeypatch.setattr(obstore, "put", put)
    deletes: List[str] = []
    real_delete = obstore.delete
    monkeypatch.setattr(
        obstore, "delete", lambda store, key, **kw: (deletes.append(key), real_delete(store, key))
    )
    error = _failing_delivery(kind)
    assert deletes == []
    assert len(_stored(from_url.store)) == 5
    assert "manifest of the stream is in place" in str(error) and "kept" in str(error)


def _denied_head(monkeypatch: pytest.MonkeyPatch) -> None:
    def head(store: Any, key: str) -> Any:
        raise PermissionError("Error performing HEAD: 403 Forbidden")

    monkeypatch.setattr(obstore, "head", head)


def test_a_head_refused_for_permission_does_not_fail_the_delivery(
    kind: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    _denied_head(monkeypatch)
    stream = Stream(40)
    sink = TARGETS[kind]("s3://bucket/p", {"rows-per-file": "10"}).sink_for(stream.context)
    assert _deliver(sink, stream.header, stream.rows) is not None
    assert len(_stored(from_url.store)) == 5


def test_a_failure_with_an_unreadable_manifest_state_keeps_the_data_objects(
    kind: str, from_url: FromUrl, monkeypatch: pytest.MonkeyPatch
) -> None:
    _denied_head(monkeypatch)
    flaky = Flaky(monkeypatch, put_fails_on=5)
    error = _failing_delivery(kind)
    assert flaky.deletes == []
    assert len(_stored(from_url.store)) == 4
    assert "could not be checked" in str(error) and "kept" in str(error)
