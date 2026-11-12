# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""OpenLineage ``RunEvent`` emission -- the wire format, and where it goes.

Two independent things live here.

**Building the event.** :func:`build_run_id` is the one piece of identity
this package invents, and it invents nothing: a run's id is
``uuid5(namespace, planDigest + seedRoot + runIndex)`` -- deterministic, so
the same run reported twice (a retry, a replay) reports under the same id.
``uuid4`` never appears in this module; the standing rule against
random/wall-clock identity in the *kernel* extends here by choice, not by
the import-linter contract (this package sits outside the kernel source
set), because a lineage graph that cannot be replayed against the run it
describes is not lineage, it is a log line.

**Sending it somewhere.** :class:`NdjsonLineageEmitter` writes
newline-delimited OpenLineage events to a run directory -- always
available, no optional dependency. :class:`OpenLineageHttpEmitter` posts
through the ``openlineage-python`` client and exists only when that
package (``flux-engine[lineage]``) is installed; constructing it without
the extra raises immediately, with a message naming the extra, rather
than failing confusingly deep inside a run. Both satisfy
:class:`flux_engine.api.lineage.LineageEmitter` -- neither's failure to
deliver ever raises out of :meth:`emit`, matching the Protocol's own rule
that a receiver being down loses the event, not the work.

:attr:`~flux_engine.api.lineage.DatasetRef` carries no facets (it is a
frozen, two-field identity in ``flux_engine.api``) even though OpenLineage
datasets carry facets on the wire. This module's convention closes that
gap without touching ``flux_engine.api``: output-level facets travel in
``LineageEvent.facets["flux.outputs"]``, keyed by the output dataset's
``name``, and :func:`render_run_event` reads them from there when it
renders each output's ``facets`` on the wire.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from flux_engine.api.lineage import DatasetRef, LineageEvent
from flux_engine.core.digest import DigestRef

if TYPE_CHECKING:
    from flux_engine.api.lineage import LineageEmitter

__all__ = [
    "OPENLINEAGE_PRODUCER",
    "OPENLINEAGE_SCHEMA_URL",
    "NdjsonLineageEmitter",
    "OpenLineageHttpEmitter",
    "build_run_id",
    "dataset_ref_for_binding",
    "determinism_facet",
    "flux_seam_facet",
    "governance_facet",
    "job_namespace",
    "render_run_event",
    "schema_facet",
]

#: This engine's own producer URI, carried on every event this module renders
#: (OpenLineage's own convention: a stable URI naming the thing that emitted it).
OPENLINEAGE_PRODUCER = "https://github.com/Agenticstiger/flux-engine"

#: The OpenLineage 2.0 RunEvent schema this module's wire shape targets.
OPENLINEAGE_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/definitions/RunEvent"

#: The fixed namespace :func:`build_run_id` derives every run id under.
_RUN_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://flux-engine.internal/run")

#: The key inside ``LineageEvent.facets`` this module reads per-output
#: dataset facets from -- see the module docstring for why they cannot
#: live on ``DatasetRef`` itself.
_OUTPUT_FACETS_KEY = "flux.outputs"


def build_run_id(plan_digest: DigestRef, seed_root: int, run_index: int = 0) -> str:
    """Return the deterministic OpenLineage run id for one run of one plan.

    ``uuid5`` over ``(planDigest, seedRoot, runIndex)`` -- the same triple
    always names the same run, and a different plan, seed, or sweep index
    always names a different one. ``uuid4`` is never used here: a lineage
    graph keyed by a random id cannot be checked against a replay of the
    run it claims to describe.
    """
    name = f"{plan_digest.value}:{seed_root}:{run_index}"
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, name))


def job_namespace(simulation_id: str) -> str:
    """Return the OpenLineage job namespace for *simulation_id*: ``flux://<id>``.

    The id is the Simulation's own (``Simulation.id``), never the name of
    the directory the bundle happens to sit in on the machine that ran it.
    A namespace derived from a local path fragments one job across every
    clone and CI workspace that runs it -- ``flux://bundle``,
    ``flux://src``, ``flux://workspace`` -- and puts the operator's
    filesystem layout into a published lineage event.
    """
    return f"flux://{simulation_id}"


def dataset_ref_for_binding(binding_location: str) -> DatasetRef:
    """Split a FLUID binding-location URI into an OpenLineage-shaped :class:`DatasetRef`.

    Follows OpenLineage's own dataset-naming convention: ``namespace`` is
    the physical location (``scheme://authority``), ``name`` is the path
    under it. A location with no recognisable scheme+authority (a bare
    table name, for a contract that declares no physical binding yet) is
    named under a ``flux`` namespace rather than rejected -- a run can
    still report *that* it produced something even when it cannot yet say
    exactly where.
    """
    parsed = urlsplit(binding_location)
    if parsed.scheme and parsed.netloc:
        namespace = f"{parsed.scheme}://{parsed.netloc}"
        name = parsed.path.lstrip("/") or parsed.netloc
        return DatasetRef(namespace=namespace, name=name)
    return DatasetRef(namespace="flux", name=binding_location)


def schema_facet(columns: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
    """Build an OpenLineage ``SchemaDatasetFacet`` from ``(name, logicalType)`` pairs.

    *columns* is the expose's declared column order (KERNEL-SPEC §8/§9);
    the facet preserves that order rather than sorting it, because a
    schema facet describes a specific contract's column list, not a set.
    """
    return {
        "_producer": OPENLINEAGE_PRODUCER,
        "_schemaURL": (
            "https://openlineage.io/spec/facets/1-1-0/SchemaDatasetFacet.json#/$defs/SchemaDatasetFacet"
        ),
        "fields": [{"name": name, "type": logical_type} for name, logical_type in columns],
    }


def flux_seam_facet(
    *,
    product_ref: str,
    expose_id: str,
    fluid_version: str,
    contract_digest: Union[str, DigestRef],
    output_digest: Union[str, DigestRef],
    rows: int,
) -> Dict[str, Any]:
    """Build the ``flux.seam`` output-dataset facet (KERNEL-SPEC §8's seam, as lineage).

    Everything a reader of the lineage graph needs to re-derive that this
    output is the FLUID seam crossing this bundle declared -- the
    referenced contract, the expose, the digests that bind them -- and
    nothing that requires re-reading the bundle to interpret.
    """
    return {
        "_producer": OPENLINEAGE_PRODUCER,
        "productRef": product_ref,
        "exposeId": expose_id,
        "fluidVersion": fluid_version,
        "contractDigest": (
            contract_digest.value if isinstance(contract_digest, DigestRef) else contract_digest
        ),
        "outputDigest": (
            output_digest.value if isinstance(output_digest, DigestRef) else output_digest
        ),
        "rows": rows,
    }


def determinism_facet(
    *,
    kernel_version: str,
    seed_tree_version: str,
    trace_version: str,
    input_digest: Union[str, DigestRef],
    plan_digest: Union[str, DigestRef],
    run_digest: Optional[Union[str, DigestRef]] = None,
) -> Dict[str, Any]:
    """Build the ``flux.determinism`` run facet: the versions and digests that bind a run to its plan."""

    def _value(ref: Optional[Union[str, DigestRef]]) -> Optional[str]:
        if ref is None:
            return None
        return ref.value if isinstance(ref, DigestRef) else ref

    return {
        "_producer": OPENLINEAGE_PRODUCER,
        "kernelVersion": kernel_version,
        "seedTreeVersion": seed_tree_version,
        "traceVersion": trace_version,
        "inputDigest": _value(input_digest),
        "planDigest": _value(plan_digest),
        "runDigest": _value(run_digest),
    }


def governance_facet(
    *,
    organization_id: Optional[str] = None,
    policy_digest: Optional[Union[str, DigestRef]] = None,
    decisions_digest: Optional[Union[str, DigestRef]] = None,
) -> Dict[str, Any]:
    """Build the ``flux.governance`` run facet: the tenancy and enforcement context of a run."""

    def _value(ref: Optional[Union[str, DigestRef]]) -> Optional[str]:
        if ref is None:
            return None
        return ref.value if isinstance(ref, DigestRef) else ref

    return {
        "_producer": OPENLINEAGE_PRODUCER,
        "organizationId": organization_id,
        "policyDigest": _value(policy_digest),
        "decisionsDigest": _value(decisions_digest),
    }


def _dataset_dict(ref: DatasetRef, facets: Mapping[str, Any]) -> Dict[str, Any]:
    rendered: Dict[str, Any] = {"namespace": ref.namespace, "name": ref.name}
    if facets:
        rendered["facets"] = dict(facets)
    return rendered


def render_run_event(event: LineageEvent) -> Dict[str, Any]:
    """Render *event* as one OpenLineage ``RunEvent`` JSON object.

    Per-output facets are read from ``event.facets["flux.outputs"]``
    (keyed by dataset name) rather than from the ``DatasetRef``s themselves
    -- see the module docstring.
    """
    output_facets = event.facets.get(_OUTPUT_FACETS_KEY, {}) if event.facets else {}
    run_facets = {key: value for key, value in event.facets.items() if key != _OUTPUT_FACETS_KEY}
    return {
        "eventType": event.event_type.value,
        "eventTime": event.event_time,
        "producer": OPENLINEAGE_PRODUCER,
        "schemaURL": OPENLINEAGE_SCHEMA_URL,
        "run": {"runId": event.run_id, "facets": run_facets},
        "job": {"namespace": event.job_namespace, "name": event.job_name},
        "inputs": [_dataset_dict(ref, {}) for ref in event.inputs],
        "outputs": [_dataset_dict(ref, output_facets.get(ref.name, {})) for ref in event.outputs],
    }


@dataclass
class NdjsonLineageEmitter:
    """Appends every event, OpenLineage-rendered, as one NDJSON line to *path*.

    Always available -- no optional dependency. Never raises out of
    :meth:`emit`: an ``OSError`` writing the line is recorded and
    :meth:`flush` reports it, matching
    :class:`~flux_engine.api.lineage.LineageEmitter`'s contract that a
    delivery failure is answerable, not a run-ending exception.
    """

    path: Path
    _failed: int = 0
    _sent: int = 0

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def emit(self, event: LineageEvent) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(render_run_event(event), sort_keys=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._sent += 1
        except OSError:
            self._failed += 1

    @property
    def failed(self) -> int:
        """How many events this emitter could not deliver. Zero is the clean case."""
        return self._failed

    def flush(self, timeout_seconds: int = 5) -> bool:
        """Return whether every :meth:`emit` call so far wrote its line successfully."""
        del timeout_seconds  # writes are synchronous; nothing to wait for
        return self._failed == 0


class OpenLineageHttpEmitter:
    """Sends events to an OpenLineage HTTP backend via ``openlineage-python``.

    Requires the ``flux-engine[lineage]`` extra. Constructing this class
    without ``openlineage-python`` installed raises ``ModuleNotFoundError``
    immediately and by name, rather than failing the first time a run
    tries to emit.
    """

    def __init__(self, url: str, *, endpoint: Optional[str] = None, timeout: float = 5.0) -> None:
        try:
            from openlineage.client import OpenLineageClient
            from openlineage.client.transport.http import HttpConfig, HttpTransport
        except ImportError as exc:  # pragma: no cover - exercised via the guard test below
            raise ModuleNotFoundError(
                "OpenLineageHttpEmitter requires the 'openlineage-python' package "
                "(install flux-engine[lineage])"
            ) from exc

        config = (
            HttpConfig(url=url, endpoint=endpoint, timeout=timeout)
            if endpoint
            else HttpConfig(url=url, timeout=timeout)
        )
        self._client = OpenLineageClient(transport=HttpTransport(config))
        self._failed = 0
        self._last_error: Optional[str] = None

    def emit(self, event: LineageEvent) -> None:
        try:
            # The client's own ``emit`` accepts only its RunEvent/DatasetEvent/
            # JobEvent classes and raises ValueError on anything else. FLUX
            # renders the OpenLineage JSON itself (``render_run_event``), so
            # hand the dict to the transport, which serializes it with the
            # client's Serde: null values are dropped (the HTTP body can lack
            # keys the NDJSON line keeps as null) and keys are sorted. Typed as
            # Any: the transport's ``emit`` is annotated for the client's event
            # classes, and CI type-checks without the extra.
            transport: Any = self._client.transport
            transport.emit(render_run_event(event))
        except Exception as error:  # noqa: BLE001 - a down receiver must not fail the run
            self._failed += 1
            self._last_error = f"{type(error).__name__}: {error}"

    @property
    def failed(self) -> int:
        """How many events this emitter could not deliver.

        Read it. ``emit`` cannot raise -- a lineage receiver that is down
        must not end a run -- so this counter, and :meth:`flush`'s return
        value, are the only places a dropped event is visible at all. A
        caller that ignores both has silently discarded its lineage.
        """
        return self._failed

    @property
    def last_error(self) -> Optional[str]:
        """The type and message of the most recent delivery failure, or ``None``."""
        return self._last_error

    def flush(self, timeout_seconds: int = 5) -> bool:
        """Flush the client's transport, waiting at most *timeout_seconds*.

        The argument is honoured, not discarded: the OpenLineage Python
        client owns delivery (its HTTP transport may buffer), so a caller
        asking for a bounded flush is asking the transport to finish
        inside that bound. Returns ``False`` when the transport reports it
        could not, or when any :meth:`emit` call was dropped -- so a
        caller that checks the return value learns about a down receiver
        even though ``emit`` itself never raised.
        """
        flushed = self._flush_transport(timeout_seconds)
        return flushed and self._failed == 0

    def _flush_transport(self, timeout_seconds: int) -> bool:
        """Ask the client's transport to finish, within *timeout_seconds*.

        The client's transports do not share one flush signature across
        versions -- some expose ``close(timeout=...)``, some ``flush()``,
        some neither -- so the bound is offered by keyword and the
        no-argument spelling is the fallback. A transport with no flush at
        all is nothing to wait for, which is a clean flush rather than a
        failure.
        """
        transport = getattr(self._client, "transport", None)
        for name in ("close", "flush"):
            call = getattr(transport, name, None)
            if call is None:
                continue
            try:
                try:
                    outcome = call(timeout=timeout_seconds)
                except TypeError:
                    outcome = call()
            except Exception as error:  # noqa: BLE001 - a flush failure is answerable
                self._failed += 1
                self._last_error = f"{type(error).__name__}: {error}"
                return False
            return outcome is not False
        return True


def openlineage_client_available() -> bool:
    """Return whether ``openlineage-python`` is importable in this environment."""
    import importlib.util

    return importlib.util.find_spec("openlineage") is not None


if TYPE_CHECKING:  # pragma: no cover - type-check-time only, never executed

    def _emitters_satisfy_the_lineage_emitter_contract(
        ndjson: NdjsonLineageEmitter, http: OpenLineageHttpEmitter
    ) -> Tuple["LineageEmitter", "LineageEmitter"]:
        """Static assertion: both concrete emitters satisfy the Protocol by shape."""
        return ndjson, http
