# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for flux_engine.lineage.emitter."""

from __future__ import annotations

import json
import sys
import uuid

import pytest

from flux_engine.api.lineage import DatasetRef, LineageEmitter, LineageEvent, LineageEventType
from flux_engine.core.digest import DigestDomain, DigestRef
from flux_engine.lineage.emitter import (
    NdjsonLineageEmitter,
    OpenLineageHttpEmitter,
    build_run_id,
    dataset_ref_for_binding,
    determinism_facet,
    flux_seam_facet,
    governance_facet,
    job_namespace,
    render_run_event,
    schema_facet,
)

PLAN_DIGEST = DigestRef(
    value="sha256:" + "11" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="plan-digest"
)


@pytest.mark.unit
def test_build_run_id_is_deterministic() -> None:
    a = build_run_id(PLAN_DIGEST, seed_root=42, run_index=0)
    b = build_run_id(PLAN_DIGEST, seed_root=42, run_index=0)
    assert a == b
    uuid.UUID(a)  # renders as a real UUID


@pytest.mark.unit
def test_build_run_id_differs_for_a_different_seed_or_run_index() -> None:
    base = build_run_id(PLAN_DIGEST, seed_root=42, run_index=0)
    assert build_run_id(PLAN_DIGEST, seed_root=43, run_index=0) != base
    assert build_run_id(PLAN_DIGEST, seed_root=42, run_index=1) != base


@pytest.mark.unit
def test_build_run_id_never_calls_uuid4() -> None:
    import inspect

    from flux_engine.lineage import emitter

    source = inspect.getsource(emitter)
    assert "uuid.uuid4(" not in source


@pytest.mark.unit
def test_job_namespace_format() -> None:
    """The argument is the Simulation's own id, not a directory basename.

    ``apply`` used to build this from ``bundle_directory.name``, so two
    copies of one bundle under differently-named directories emitted
    different lineage identities -- a graph that fragments per clone and
    per CI workspace, and an event that publishes the operator's local
    filesystem layout.
    """
    assert job_namespace("sim-payment-recovery") == "flux://sim-payment-recovery"


@pytest.mark.unit
def test_dataset_ref_for_binding_splits_scheme_and_authority() -> None:
    ref = dataset_ref_for_binding("snowflake://acme-wh/gold.payment_recovery_moment")
    assert ref.namespace == "snowflake://acme-wh"
    assert ref.name == "gold.payment_recovery_moment"


@pytest.mark.unit
def test_dataset_ref_for_binding_falls_back_for_a_bare_name() -> None:
    ref = dataset_ref_for_binding("payment_recovery_moment")
    assert ref.namespace == "flux"
    assert ref.name == "payment_recovery_moment"


@pytest.mark.unit
def test_schema_facet_preserves_declared_column_order() -> None:
    facet = schema_facet([("amount", "DECIMAL"), ("market", "STRING")])
    assert [f["name"] for f in facet["fields"]] == ["amount", "market"]
    assert facet["fields"][0]["type"] == "DECIMAL"


@pytest.mark.unit
def test_flux_seam_facet_accepts_digest_refs_or_strings() -> None:
    digest = DigestRef(value="sha256:" + "22" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="c")
    facet = flux_seam_facet(
        product_ref="telco.gold.payment_recovery_moment",
        expose_id="payment_recovery_moment",
        fluid_version="0.7.3",
        contract_digest=digest,
        output_digest="sha256:" + "33" * 32,
        rows=1000,
    )
    assert facet["contractDigest"] == digest.value
    assert facet["outputDigest"] == "sha256:" + "33" * 32
    assert facet["rows"] == 1000


@pytest.mark.unit
def test_determinism_facet_renders_digest_refs_as_their_values() -> None:
    facet = determinism_facet(
        kernel_version="0.1.0",
        seed_tree_version="flux-seed/1",
        trace_version="flux-trace/1",
        input_digest=PLAN_DIGEST,
        plan_digest=PLAN_DIGEST,
    )
    assert facet["planDigest"] == PLAN_DIGEST.value
    assert facet["runDigest"] is None


@pytest.mark.unit
def test_governance_facet_defaults_to_none_fields() -> None:
    facet = governance_facet()
    assert facet["organizationId"] is None
    assert facet["policyDigest"] is None


def _event(**facets: object) -> LineageEvent:
    output = DatasetRef(namespace="snowflake://wh", name="gold.moment")
    return LineageEvent(
        event_type=LineageEventType.START,
        event_time="2026-01-01T00:00:00Z",
        run_id=build_run_id(PLAN_DIGEST, seed_root=42, run_index=0),
        job_namespace=job_namespace("telco"),
        job_name="sim-payment-recovery",
        inputs=(),
        outputs=(output,),
        facets=facets,
    )


@pytest.mark.unit
def test_render_run_event_shape() -> None:
    event = _event()
    rendered = render_run_event(event)
    assert rendered["eventType"] == "START"
    assert rendered["job"] == {"namespace": "flux://telco", "name": "sim-payment-recovery"}
    assert rendered["run"]["runId"] == event.run_id
    assert rendered["outputs"][0]["namespace"] == "snowflake://wh"


@pytest.mark.unit
def test_render_run_event_reads_output_facets_from_flux_outputs_key() -> None:
    facet = schema_facet([("amount", "DECIMAL")])
    event = _event(**{"flux.outputs": {"gold.moment": {"schema": facet}}})
    rendered = render_run_event(event)
    assert rendered["outputs"][0]["facets"] == {"schema": facet}
    # the routing key itself never leaks into the run-level facets
    assert "flux.outputs" not in rendered["run"]["facets"]


@pytest.mark.unit
def test_render_run_event_output_with_no_facets_carries_none() -> None:
    event = _event()
    rendered = render_run_event(event)
    assert "facets" not in rendered["outputs"][0]


@pytest.mark.unit
def test_ndjson_emitter_writes_one_line_per_event(tmp_path) -> None:
    path = tmp_path / "run" / "lineage.ndjson"
    emitter: LineageEmitter = NdjsonLineageEmitter(path=path)
    emitter.emit(_event())
    emitter.emit(
        LineageEvent(
            event_type=LineageEventType.COMPLETE,
            event_time="2026-01-01T01:00:00Z",
            run_id=build_run_id(PLAN_DIGEST, seed_root=42, run_index=0),
            job_namespace=job_namespace("telco"),
            job_name="sim-payment-recovery",
        )
    )
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["eventType"] == "START"
    second = json.loads(lines[1])
    assert second["eventType"] == "COMPLETE"
    assert emitter.flush() is True


@pytest.mark.unit
def test_ndjson_emitter_creates_parent_directories(tmp_path) -> None:
    path = tmp_path / "a" / "b" / "lineage.ndjson"
    emitter = NdjsonLineageEmitter(path=path)
    emitter.emit(_event())
    assert path.exists()


@pytest.mark.unit
def test_ndjson_emitter_satisfies_the_lineage_emitter_protocol(tmp_path) -> None:
    emitter: LineageEmitter = NdjsonLineageEmitter(path=tmp_path / "lineage.ndjson")
    assert isinstance(emitter, LineageEmitter)


@pytest.mark.unit
def test_openlineage_http_emitter_raises_clearly_without_the_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # openlineage-python is not a dev dependency of this project (it lives
    # behind the [lineage] extra) -- assert the guard fires rather than
    # asserting the ambient environment's install state.
    monkeypatch.setitem(sys.modules, "openlineage", None)
    monkeypatch.setitem(sys.modules, "openlineage.client", None)
    with pytest.raises(ModuleNotFoundError, match="flux-engine\\[lineage\\]"):
        OpenLineageHttpEmitter(url="http://localhost:5000")


@pytest.mark.unit
def test_governance_facet_renders_digest_refs() -> None:
    digest = DigestRef(value="sha256:" + "44" * 32, domain=DigestDomain.JCS_DOCUMENT, subject="p")
    facet = governance_facet(
        organization_id="org-1", policy_digest=digest, decisions_digest="sha256:" + "55" * 32
    )
    assert facet["organizationId"] == "org-1"
    assert facet["policyDigest"] == digest.value
    assert facet["decisionsDigest"] == "sha256:" + "55" * 32


@pytest.mark.unit
def test_ndjson_emitter_records_a_write_failure(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    emitter = NdjsonLineageEmitter(path=tmp_path / "run" / "lineage.ndjson")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(emitter.path.__class__, "open", _boom)
    emitter.emit(_event())
    assert emitter.flush() is False


@pytest.mark.unit
def test_openlineage_http_emitter_uses_the_real_client_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the success path of the guard with a fake ``openlineage`` module.

    ``openlineage-python`` is an optional extra not installed in this
    environment; a minimal fake with the two names
    ``OpenLineageHttpEmitter`` imports proves the *construction* and
    *emit* paths without needing the real package.
    """
    import types

    calls = []

    class _FakeClient:
        def __init__(self, transport: object) -> None:
            self.transport = transport

        def emit(self, event: object) -> None:
            calls.append(event)

    class _FakeHttpConfig:
        def __init__(self, url: str, timeout: float = 5.0, endpoint: str | None = None) -> None:
            self.url = url

    class _FakeHttpTransport:
        def __init__(self, config: object) -> None:
            self.config = config

    client_module = types.ModuleType("openlineage.client")
    client_module.OpenLineageClient = _FakeClient  # type: ignore[attr-defined]
    transport_module = types.ModuleType("openlineage.client.transport.http")
    transport_module.HttpConfig = _FakeHttpConfig  # type: ignore[attr-defined]
    transport_module.HttpTransport = _FakeHttpTransport  # type: ignore[attr-defined]
    transport_pkg = types.ModuleType("openlineage.client.transport")
    client_pkg = types.ModuleType("openlineage.client")
    client_pkg.OpenLineageClient = _FakeClient  # type: ignore[attr-defined]
    openlineage_pkg = types.ModuleType("openlineage")

    monkeypatch.setitem(sys.modules, "openlineage", openlineage_pkg)
    monkeypatch.setitem(sys.modules, "openlineage.client", client_pkg)
    monkeypatch.setitem(sys.modules, "openlineage.client.transport", transport_pkg)
    monkeypatch.setitem(sys.modules, "openlineage.client.transport.http", transport_module)

    emitter = OpenLineageHttpEmitter(url="http://localhost:5000")
    emitter.emit(_event())
    assert len(calls) == 1
    assert emitter.flush() is True


# --- the HTTP emitter's flush actually flushes, within its bound -------------
#
# ``flush(timeout_seconds)`` used to be ``del timeout_seconds; return
# self._failed == 0``: a caller asking for a bounded flush got neither a
# flush nor a bound, and ``emit`` swallowed every exception, so a receiver
# that was down lost every event with nothing recorded anywhere.


def _install_fake_openlineage(monkeypatch: pytest.MonkeyPatch, transport_cls: type) -> None:
    """Put a minimal fake ``openlineage`` in ``sys.modules`` (the optional extra)."""
    import types

    class _FakeClient:
        def __init__(self, transport: object) -> None:
            self.transport = transport

        def emit(self, event: object) -> None:
            self.transport.sent.append(event)  # type: ignore[attr-defined]

    class _FakeHttpConfig:
        def __init__(self, url: str, timeout: float = 5.0, endpoint: str | None = None) -> None:
            self.url = url

    client_pkg = types.ModuleType("openlineage.client")
    client_pkg.OpenLineageClient = _FakeClient  # type: ignore[attr-defined]
    transport_pkg = types.ModuleType("openlineage.client.transport")
    transport_module = types.ModuleType("openlineage.client.transport.http")
    transport_module.HttpConfig = _FakeHttpConfig  # type: ignore[attr-defined]
    transport_module.HttpTransport = transport_cls  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "openlineage", types.ModuleType("openlineage"))
    monkeypatch.setitem(sys.modules, "openlineage.client", client_pkg)
    monkeypatch.setitem(sys.modules, "openlineage.client.transport", transport_pkg)
    monkeypatch.setitem(sys.modules, "openlineage.client.transport.http", transport_module)


class _ClosingTransport:
    """A transport that records the bound it was asked to finish within."""

    def __init__(self, config: object) -> None:
        self.config = config
        self.sent: list = []
        self.closed_with: list = []
        self.result = True

    def close(self, timeout: int = -1) -> bool:
        self.closed_with.append(timeout)
        return self.result


@pytest.mark.unit
def test_flush_passes_its_timeout_to_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_openlineage(monkeypatch, _ClosingTransport)
    emitter = OpenLineageHttpEmitter(url="http://localhost:5000")
    emitter.emit(_event())

    assert emitter.flush(timeout_seconds=11) is True
    assert emitter._client.transport.closed_with == [11]  # type: ignore[attr-defined]


@pytest.mark.unit
def test_flush_reports_a_transport_that_could_not_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_openlineage(monkeypatch, _ClosingTransport)
    emitter = OpenLineageHttpEmitter(url="http://localhost:5000")
    emitter._client.transport.result = False  # type: ignore[attr-defined]

    assert emitter.flush(timeout_seconds=1) is False


@pytest.mark.unit
def test_flush_reports_a_transport_that_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenTransport(_ClosingTransport):
        def close(self, timeout: int = -1) -> bool:
            raise ConnectionError("receiver is down")

    _install_fake_openlineage(monkeypatch, _BrokenTransport)
    emitter = OpenLineageHttpEmitter(url="http://localhost:5000")

    assert emitter.flush() is False
    assert emitter.failed == 1
    assert emitter.last_error is not None and "receiver is down" in emitter.last_error


@pytest.mark.unit
def test_a_dropped_event_is_visible_to_a_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """``emit`` never raises, so the counter is the only place a drop shows up."""

    class _RefusingTransport(_ClosingTransport):
        pass

    _install_fake_openlineage(monkeypatch, _RefusingTransport)
    emitter = OpenLineageHttpEmitter(url="http://localhost:5000")

    def _boom(event: object) -> None:
        raise ConnectionError("no route to host")

    monkeypatch.setattr(emitter._client, "emit", _boom)
    emitter.emit(_event())  # must not raise: a down receiver does not end a run

    assert emitter.failed == 1
    assert emitter.last_error is not None and "no route to host" in emitter.last_error
    assert emitter.flush() is False


@pytest.mark.unit
def test_a_transport_with_nothing_to_flush_is_a_clean_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive control: no flush to call is not a failure to flush."""

    class _SynchronousTransport:
        def __init__(self, config: object) -> None:
            self.config = config
            self.sent: list = []

    _install_fake_openlineage(monkeypatch, _SynchronousTransport)
    emitter = OpenLineageHttpEmitter(url="http://localhost:5000")
    emitter.emit(_event())

    assert emitter.flush(timeout_seconds=3) is True
    assert emitter.failed == 0


@pytest.mark.unit
def test_the_ndjson_emitter_reports_its_failures_too(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = NdjsonLineageEmitter(path=tmp_path / "run" / "lineage.ndjson")
    assert emitter.failed == 0

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(emitter.path.__class__, "open", _boom)
    emitter.emit(_event())
    assert emitter.failed == 1
