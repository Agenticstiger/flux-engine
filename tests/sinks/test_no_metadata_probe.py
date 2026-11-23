# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""An object store target with no credentials is refused, and nothing is contacted.

Without a credential in the environment an object store asks the cloud's instance metadata
service for one (``169.254.169.254`` on AWS and the others), a network call the person did not
ask for. The sinks refuse such a target before the run (``FLX-3505``) unless
``--sink-opt instance-identity=true`` says the instance's own credentials are wanted.

The metadata endpoint is moved to a loopback server that records what reaches it, so the tests
see the real store's behaviour and no packet leaves the machine.
"""

from __future__ import annotations

import http.server
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Tuple

import pytest

from flux_engine.core.errors import FluxError
from flux_engine.sinks import registry
from flux_engine.sinks._objects import (
    INSTANCE_IDENTITY_OPTION,
    explicit_credentials,
    parse_object_target,
    require_explicit_credentials,
)
from flux_engine.stages.run import run

from .conftest import TEST_CREDENTIALS, pyarrow_or_skip

pyarrow_or_skip()
obstore = pytest.importorskip("obstore")
obstore_store = pytest.importorskip("obstore.store")

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "telco-fidelity-small"

#: Every variable any of the rules below reads, so a test starts from an empty environment.
_CLEARED = (
    *TEST_CREDENTIALS,
    "AWS_PROFILE",
    "AWS_SKIP_SIGNATURE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "GOOGLE_SERVICE_ACCOUNT",
    "GOOGLE_SERVICE_ACCOUNT_PATH",
    "GOOGLE_SERVICE_ACCOUNT_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_SKIP_SIGNATURE",
    "SERVICE_ACCOUNT",
    "AZURE_CREDENTIAL_TYPE",
    "AZURE_STORAGE_ACCESS_KEY",
    "AZURE_STORAGE_MASTER_KEY",
    "AZURE_STORAGE_SAS_KEY",
    "AZURE_STORAGE_SAS_TOKEN",
    "AZURE_STORAGE_TOKEN",
    "AZURE_STORAGE_CLIENT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_STORAGE_CLIENT_SECRET",
    "AZURE_CLIENT_SECRET",
    "AZURE_STORAGE_TENANT_ID",
    "AZURE_STORAGE_AUTHORITY_ID",
    "AZURE_TENANT_ID",
    "AZURE_AUTHORITY_ID",
    "AZURE_FEDERATED_TOKEN_FILE",
    "AZURE_USE_AZURE_CLI",
    "AZURE_SKIP_SIGNATURE",
    "AZURE_STORAGE_USE_EMULATOR",
)

#: A target of each scheme, and the sinks that take it.
TARGETS = ("s3://bucket/prefix", "gs://bucket/prefix", "az://container/prefix", "abfs://c/prefix")


class _Recorder(http.server.BaseHTTPRequestHandler):
    requests: List[Tuple[str, str]]

    def _record(self) -> None:
        self.requests.append((self.command, self.path))
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_PUT = do_POST = do_HEAD = _record

    def log_message(self, *_args: Any) -> None:  # silence the server's own log line
        pass


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An environment with no credential of any cloud, and no gcloud credentials file."""
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("APPDATA", raising=False)


@pytest.fixture
def metadata_service(
    monkeypatch: pytest.MonkeyPatch, no_credentials: None
) -> Iterator[List[Tuple[str, str]]]:
    """A loopback stand-in for the instance metadata service; yields what it was asked."""
    requests: List[Tuple[str, str]] = []
    handler = type("Handler", (_Recorder,), {"requests": requests})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AWS_METADATA_ENDPOINT", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _Boom:
    """A store constructor that fails the test when it is called."""

    calls = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        type(self).calls += 1
        raise AssertionError("the store was built for a target with no credentials")


@pytest.fixture
def no_store(monkeypatch: pytest.MonkeyPatch) -> _Boom:
    boom = _Boom()
    _Boom.calls = 0
    monkeypatch.setattr(obstore_store, "from_url", boom)
    return boom


# ---------------------------------------------------------------- what counts as explicit


@pytest.mark.parametrize(
    ("scheme", "env", "expected"),
    [
        ("s3", {}, False),
        ("s3", {"AWS_ACCESS_KEY_ID": "a"}, False),
        ("s3", {"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b"}, True),
        ("s3", {"AWS_WEB_IDENTITY_TOKEN_FILE": "/t"}, False),
        (
            "s3",
            {"AWS_WEB_IDENTITY_TOKEN_FILE": "/t", "AWS_ROLE_ARN": "arn:aws:iam::1:role/r"},
            True,
        ),
        ("s3", {"AWS_SKIP_SIGNATURE": "true"}, True),
        ("s3", {"AWS_SKIP_SIGNATURE": "false"}, False),
        # The store reads an ECS or EKS credentials URI over the network: that is an opt-in.
        ("s3", {"AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/x"}, False),
        # obstore ignores a profile, so a profile alone is no credential for it.
        ("s3", {"AWS_PROFILE": "work"}, False),
        ("gs", {}, False),
        ("gs", {"GOOGLE_SERVICE_ACCOUNT": "/sa.json"}, True),
        ("gs", {"GOOGLE_SERVICE_ACCOUNT_KEY": "{}"}, True),
        ("gs", {"GOOGLE_APPLICATION_CREDENTIALS": "/adc.json"}, True),
        ("gs", {"GOOGLE_BEARER_TOKEN": "t"}, True),
        ("gs", {"GOOGLE_SKIP_SIGNATURE": "1"}, True),
        ("az", {}, False),
        ("az", {"AZURE_STORAGE_ACCOUNT_NAME": "acct"}, False),
        ("az", {"AZURE_STORAGE_ACCOUNT_KEY": "k"}, True),
        ("az", {"AZURE_STORAGE_SAS_KEY": "sig"}, True),
        ("az", {"AZURE_STORAGE_TOKEN": "t"}, True),
        ("az", {"AZURE_CLIENT_ID": "c"}, False),
        ("az", {"AZURE_CLIENT_ID": "c", "AZURE_CLIENT_SECRET": "s", "AZURE_TENANT_ID": "t"}, True),
        (
            "abfs",
            {"AZURE_CLIENT_ID": "c", "AZURE_TENANT_ID": "t", "AZURE_FEDERATED_TOKEN_FILE": "/f"},
            True,
        ),
        ("abfs", {"AZURE_USE_AZURE_CLI": "true"}, True),
        (
            "az",
            {"AZURE_STORAGE_ACCOUNT_KEY": "k", "AZURE_CREDENTIAL_TYPE": "managed_identity"},
            False,
        ),
    ],
)
def test_what_counts_as_an_explicit_credential(
    scheme: str, env: Mapping[str, str], expected: bool, tmp_path: Path
) -> None:
    assert explicit_credentials(scheme, {"HOME": str(tmp_path), **env}) is expected


def test_a_profile_counts_for_deltalake_only(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path), "AWS_PROFILE": "work"}
    assert explicit_credentials("s3", env, library="deltalake") is True
    assert explicit_credentials("s3", env, library="obstore") is False


def test_the_gcloud_application_default_credentials_file_counts(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path)}
    assert explicit_credentials("gs", env) is False
    file = tmp_path / ".config" / "gcloud" / "application_default_credentials.json"
    file.parent.mkdir(parents=True)
    file.write_text("{}")
    assert explicit_credentials("gs", env) is True


def test_the_opt_in_is_not_named_like_a_credential() -> None:
    from flux_engine.core.credentials import credential_option

    # The registry refuses a credential-shaped option name, so this one must not be one.
    assert credential_option(INSTANCE_IDENTITY_OPTION) is False


# ------------------------------------------------------------------- the refusal, per sink


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("sink", ["parquet", "arrow-ipc"])
def test_a_file_sink_target_with_no_credentials_is_refused_and_the_store_is_never_built(
    sink: str, target: str, no_credentials: None, no_store: _Boom
) -> None:
    with pytest.raises(FluxError) as caught:
        registry.open_target(f"{sink}:{target}")
    assert caught.value.code.code == "FLX-3505"
    assert "has no credentials set" in caught.value.detail
    assert "nothing was contacted" in caught.value.detail
    assert f"--sink-opt {INSTANCE_IDENTITY_OPTION}=true" in caught.value.detail
    assert _Boom.calls == 0


@pytest.mark.parametrize(
    ("target", "named"),
    [
        ("s3://bucket/p", "AWS_ACCESS_KEY_ID"),
        ("gs://bucket/p", "GOOGLE_SERVICE_ACCOUNT"),
        ("az://container/p", "AZURE_STORAGE_ACCOUNT_KEY"),
    ],
)
def test_the_remedy_names_the_clouds_own_settings(
    target: str, named: str, no_credentials: None, no_store: _Boom
) -> None:
    with pytest.raises(FluxError) as caught:
        registry.open_target(f"parquet:{target}")
    assert named in caught.value.detail


def test_with_the_opt_in_the_store_is_built_as_it_always_was(
    no_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: List[str] = []
    monkeypatch.setattr(
        obstore_store,
        "from_url",
        lambda url, **kw: built.append(url) or obstore_store.MemoryStore(),
    )
    registry.open_target("parquet:s3://bucket/p", {INSTANCE_IDENTITY_OPTION: "true"})
    registry.open_target("arrow-ipc:gs://bucket/p", {INSTANCE_IDENTITY_OPTION: "yes"})
    assert built == ["s3://bucket", "gs://bucket"]


def test_the_opt_in_must_be_true_or_false(no_credentials: None) -> None:
    with pytest.raises(FluxError) as caught:
        registry.open_target("parquet:s3://bucket/p", {INSTANCE_IDENTITY_OPTION: "instance"})
    assert caught.value.code.code == "FLX-3504"


def test_explicit_credentials_proceed_without_the_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: List[str] = []
    monkeypatch.setattr(
        obstore_store,
        "from_url",
        lambda url, **kw: built.append(url) or obstore_store.MemoryStore(),
    )
    registry.open_target("parquet:s3://bucket/p")  # the suite's autouse credentials
    assert built == ["s3://bucket"]


def test_a_local_directory_needs_no_credentials(no_credentials: None, tmp_path: Path) -> None:
    registry.open_target(f"parquet:{tmp_path / 'out'}")


def test_the_check_is_one_function_a_custom_caller_can_use(no_credentials: None) -> None:
    with pytest.raises(FluxError):
        require_explicit_credentials(parse_object_target("s3://bucket/p"))
    require_explicit_credentials(parse_object_target("s3://bucket/p", instance_identity=True))


def test_a_delta_object_target_with_no_credentials_is_refused(no_credentials: None) -> None:
    pytest.importorskip("deltalake")
    for target in ("s3://bucket/p", "gs://bucket/p", "az://container/p"):
        with pytest.raises(FluxError) as caught:
            registry.open_target(f"delta:{target}")
        assert caught.value.code.code == "FLX-3505"
        assert "nothing was contacted" in caught.value.detail
    registry.open_target("delta:s3://bucket/p", {INSTANCE_IDENTITY_OPTION: "true"})


# ------------------------------------------------- the real store, against a recording service


def test_a_refused_target_sends_nothing_to_the_metadata_service(
    metadata_service: List[Tuple[str, str]],
) -> None:
    with pytest.raises(FluxError) as caught:
        registry.open_target("parquet:s3://bucket/p")
    assert caught.value.code.code == "FLX-3505"
    assert metadata_service == []


def test_with_the_opt_in_the_real_store_does_ask_the_metadata_service(
    metadata_service: List[Tuple[str, str]], tmp_path: Path
) -> None:
    """The control for the test above: without it, silence would prove nothing."""
    target = registry.open_target("parquet:s3://bucket/p", {INSTANCE_IDENTITY_OPTION: "true"})
    assert metadata_service == [], "building the store makes no request"
    location = parse_object_target("s3://bucket/p", instance_identity=True)
    from flux_engine.sinks._objects import Upload, upload_files

    file = tmp_path / "part.bin"
    file.write_bytes(b"x")
    with pytest.raises(FluxError) as caught:
        upload_files(location, [Upload(file, "p/part.bin")])
    assert caught.value.code.code == "FLX-3505"
    assert "has no credentials set" not in caught.value.detail
    assert ("PUT", "/latest/api/token") in metadata_service
    assert target is not None


def test_a_run_to_an_object_store_with_no_credentials_fails_before_anything_runs(
    metadata_service: List[Tuple[str, str]], tmp_path: Path
) -> None:
    state = tmp_path / "state"
    result = run(FIXTURE, state_dir=state, sink="parquet:s3://bucket/prefix", sink_options={})
    assert result.exit_code != 0 and result.error_code == "FLX-3505"
    assert not state.exists(), "nothing was planned or applied"
    assert metadata_service == []


def test_a_run_with_the_opt_in_goes_ahead_and_reaches_the_store(
    metadata_service: List[Tuple[str, str]], tmp_path: Path
) -> None:
    state = tmp_path / "state"
    result = run(
        FIXTURE,
        state_dir=state,
        sink="parquet:s3://bucket/prefix",
        sink_options={INSTANCE_IDENTITY_OPTION: "true"},
    )
    assert state.exists(), "the simulation ran"
    # The recording service has no credentials to give, so the upload is what fails.
    assert result.error_code == "FLX-3505"
    assert ("PUT", "/latest/api/token") in metadata_service


def test_the_error_is_the_same_on_every_target_of_the_family(no_credentials: None) -> None:
    seen: Dict[str, str] = {}
    for target in TARGETS:
        with pytest.raises(FluxError) as caught:
            registry.open_target(f"parquet:{target}")
        seen[target] = caught.value.code.code
    assert set(seen.values()) == {"FLX-3505"}
