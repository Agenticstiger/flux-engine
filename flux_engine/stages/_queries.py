# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What ``verify`` does with the consumers' queries (ADR 0028 sections 3, 4 and 7).

A bundle that declares ``consumers`` has every query counted on the run, and the result is the scorecard's
``queries`` dimension (:mod:`flux_engine.concordance.queries`). The work is in two steps, as the module
checks are, so that every refusal that needs no run is made before anything expensive:

* :func:`prepare_queries` reads the bundle's registry under the ceilings the flags set, reads the held-out
  profile with the profile reader, and refuses a pinned profile that names a held-out query and a held-out
  profile that names a calibration one. It needs no SQL library.
* :func:`count_queries` checks the registry against the bundle's schema, digests each emitted stream as
  ``deliver_run`` does before it builds the tables, counts every query, its unit and the requirements in
  one query process, and forms the rows. Without the ``[sql]`` extra, or for a run whose artifacts do not
  reproduce their digest, it forms a dimension that says why nothing was counted.

The SQL libraries are imported when :func:`count_queries` needs them and never when this module is.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version as _distribution_version
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple, Union

from flux_engine.api.emit import EmitHeader
from flux_engine.concordance.queries import (
    INSTALL_COMMAND,
    NOT_COUNTED,
    QueriesDimension,
    QueryVersions,
    RequirementRow,
)
from flux_engine.consumers.verify_errors import FLX_3700, FLX_3703
from flux_engine.core.errors import FluxError
from flux_engine.seam.consumers import Ceilings, ConsumerRegistry, Limits, raised_ceilings
from flux_engine.seam.profile import PinnedProfile, load_profile
from flux_engine.sinks._optional import is_available
from flux_engine.stages._consumer_check import checked_registry
from flux_engine.stages.layout import RunLayout

__all__ = [
    "QueriesLeg",
    "QueriesPlan",
    "count_queries",
    "effective_limits",
    "prepare_queries",
    "query_ceilings",
    "run_tables",
]

#: The libraries the queries need: the ``[sql]`` extra.
_LIBRARIES = ("sqlglot", "duckdb", "pyarrow")

_FLAGS = {"seconds": "--max-query-seconds", "memory": "--max-query-memory"}


@dataclass(frozen=True)
class QueriesPlan:
    """What ``verify`` settled before any leg: the registry, its schema, and the profiles' names."""

    registry: ConsumerRegistry
    #: The ceilings the command's flags set, for the limits the queries run with.
    ceilings: Ceilings
    #: ``(header, declared types)`` of every emit of the run, in ``exposeId`` order.
    streams: Tuple[Tuple[EmitHeader, Tuple[str, ...]], ...]
    schema: Any
    held_out: Optional[PinnedProfile] = None


@dataclass(frozen=True)
class QueriesLeg:
    """The dimension, and the refusal when counting was refused rather than not possible."""

    dimension: QueriesDimension
    #: One line naming why the queries were refused (a check of the registry, a stream that is not the
    #: run's, a query process that failed); empty when none was.
    failure: str = ""
    error_code: Optional[str] = None
    #: The refusal itself, for a caller that raises it rather than reporting a line.
    error: Optional[FluxError] = None


def effective_limits(plan: QueriesPlan) -> Limits:
    """What each query may spend: the bundle's own value where it declares one, else the ceiling.

    The ceiling is the engine's 30 seconds and 1GiB, or what ``--max-query-seconds`` and
    ``--max-query-memory`` raised it to. A bundle that declares a value below it keeps that value.
    """
    declared = plan.registry.limits
    return Limits(
        seconds=declared.seconds if "seconds" in declared.declared else plan.ceilings.seconds,
        memory=declared.memory if "memory" in declared.declared else plan.ceilings.memory,
    )


def query_ceilings(seconds: Optional[int], memory: Optional[int]) -> Ceilings:
    """The ceilings the flags ask for, or ``FLX-3700`` naming the flag that is outside its range."""
    for key, value in (("seconds", seconds), ("memory", memory)):
        if value is None:
            continue
        try:
            raised_ceilings(**{key: value})
        except ValueError as error:
            raise FluxError(FLX_3700, f"{_FLAGS[key]}: {error}") from error
    return raised_ceilings(seconds=seconds, memory=memory)


def prepare_queries(
    bundle_directory: Union[str, Path],
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    *,
    pinned: Optional[PinnedProfile],
    held_out: Union[str, Path, None],
    ceilings: Ceilings,
) -> Optional[QueriesPlan]:
    """The registry the bundle declares and the profiles its targets come from, or ``None``.

    ``None`` when the bundle declares no ``consumers`` and no held-out profile was given. The block is
    read against the engine's own ceilings, as every stage reads it: a bundle may only lower them, and
    *ceilings* (what the flags raised) only set the limits of a query the bundle gave none for.
    Refusals: an unreadable bundle or a block past the engine's ceilings (``FLX-3614``, ``FLX-3615``), a
    held-out profile that cannot be read (``FLX-3109``, ``FLX-5036``), and the separation of the two sets
    (``FLX-3701``, ``FLX-3702``).
    """
    from flux_engine.consumers.outcome import require_apart
    from flux_engine.consumers.schema import bundle_schema, expose_schema
    from flux_engine.seam.resolve import declared_consumers, load_bundle, resolve_all_emits

    simulation_id = str(receipt.get("simulationId") or plan_document.get("simulationId"))
    loaded = load_bundle(Path(bundle_directory))
    registry = declared_consumers(loaded.simulation(simulation_id), where=simulation_id)
    held = load_profile(Path(held_out)) if held_out is not None else None
    if registry is None:
        if held is None:
            return None
        registry = ConsumerRegistry()
    require_apart(
        registry, pinned.profile if pinned is not None else None, held.profile if held else None
    )
    resolved = resolve_all_emits(loaded, simulation_id=simulation_id)
    streams = []
    schemas = []
    for emit in sorted(resolved, key=lambda entry: entry.expose_id):
        compiled = emit.compile()
        header = EmitHeader(
            expose_id=emit.expose_id,
            columns=compiled.column_names,
            types=compiled.type_names,
            contract_digest=emit.contract_digest,
        )
        streams.append((header, tuple(column.declared_type for column in compiled.columns)))
        if not emit.is_hidden_table:
            schemas.append(expose_schema(compiled, emit.keys))
    return QueriesPlan(
        registry=registry,
        ceilings=ceilings,
        streams=tuple(streams),
        schema=bundle_schema(schemas),
        held_out=held,
    )


def _not_counted(detail: str, *, error: Optional[FluxError] = None) -> QueriesLeg:
    dimension = QueriesDimension(status=NOT_COUNTED, detail=detail)
    if error is None:
        return QueriesLeg(dimension)
    return QueriesLeg(dimension, f"queries: {error}", error.code.code, error)


def _grade_of(document: Mapping[str, Any]) -> str:
    from flux_engine.evidence.grades import grade_profile

    return grade_profile(document, pinned=True).pattern.value


def _pinned_grade(
    plan_document: Mapping[str, Any], pinned: Optional[PinnedProfile]
) -> Optional[str]:
    """The grade of the pinned profile: from its document when it was handed over, else the plan's."""
    from flux_engine.stages._grades import recorded_grade
    from flux_engine.stages._scorecard import plan_pin

    if pinned is not None:
        return _grade_of(pinned.document)
    if plan_pin(plan_document) is None:
        return None
    return recorded_grade(plan_document, pinned=True).value


def _digested_streams(
    plan: QueriesPlan, layout: RunLayout, receipt: Mapping[str, Any]
) -> List[Any]:
    """Each stream of the run, digested as ``outputDigest`` is and compared with the receipt's.

    Returns the :class:`~flux_engine.consumers.tables.RunStream` of every emit. A stream that is not
    the run's is ``FLX-3703``, so that no count is taken over rows the run did not produce.
    """
    from flux_engine.consumers.tables import RunStream
    from flux_engine.safeio import read_bytes_nofollow
    from flux_engine.seam.emit import output_digest

    reported = receipt.get("emits")
    reported = reported if isinstance(reported, Mapping) else {}
    streams = []
    for header, declared in plan.streams:
        entry = reported.get(header.expose_id)
        if not isinstance(entry, Mapping):
            continue
        data = read_bytes_nofollow(layout.emit_path(header.expose_id))
        found = output_digest(
            {
                "exposeId": header.expose_id,
                "columns": list(header.columns),
                "types": list(header.types),
                "contractDigest": header.contract_digest.value,
            },
            [data],
            subject=f"outputDigest:{header.expose_id}",
        )
        if found.value != entry.get("outputDigest"):
            raise FluxError(
                FLX_3703,
                f"emits/{header.expose_id}.ndjson digests to {found.value}, but the receipt reports "
                f"{entry.get('outputDigest')}; nothing was counted",
            )
        streams.append(RunStream(header, declared))
    return streams


def run_tables(plan: QueriesPlan, layout: RunLayout, receipt: Mapping[str, Any]) -> Any:
    """The tables of a run, each stream digested as ``outputDigest`` is and read as the queries read it.

    ``FLX-3703`` when a stream is not the one the receipt reports. A calibration reads the tables of
    each candidate it runs through this, so it counts over rows the run produced and no others.
    """
    from flux_engine.consumers.tables import tables_from_run

    return tables_from_run(layout.emits_dir, _digested_streams(plan, layout, receipt))


def count_queries(
    plan: QueriesPlan,
    layout: RunLayout,
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    *,
    pinned: Optional[PinnedProfile],
    artifacts_ok: bool,
) -> QueriesLeg:
    """Count every query and requirement on the run and form the ``queries`` dimension.

    A refusal (a check of the registry, a stream that is not the run's, a query process that failed)
    does not escape: the leg carries it, with its code, and the dimension is not measured.
    """
    if not artifacts_ok:
        return _not_counted(
            "the run's artifacts do not reproduce run-digest.json (FLX-2110), so its rows are "
            "not the run's; re-run apply"
        )
    if not all(is_available(name) for name in _LIBRARIES):
        return _not_counted(
            f"consumer queries need duckdb, sqlglot and pyarrow ({INSTALL_COMMAND})"
        )
    try:
        return _count(plan, layout, receipt, plan_document, pinned)
    except FluxError as error:
        return _not_counted(f"the queries were refused ({error.code.code})", error=error)


def _count(
    plan: QueriesPlan,
    layout: RunLayout,
    receipt: Mapping[str, Any],
    plan_document: Mapping[str, Any],
    pinned: Optional[PinnedProfile],
) -> QueriesLeg:
    from flux_engine.consumers.check import SQLGLOT_VERSION
    from flux_engine.consumers.outcome import profile_targets, query_rows
    from flux_engine.consumers.requirements import (
        check_requirements,
        derive_requirements,
        requirement_requests,
    )
    from flux_engine.consumers.runner import CountRequest, run_counts
    from flux_engine.consumers.tables import tables_from_run

    registry = plan.registry
    checked = checked_registry(registry, plan.schema)
    requirements = derive_requirements(checked.queries, plan.schema)
    recorded = registry.compiled_sqlglot
    differ = recorded is not None and recorded != SQLGLOT_VERSION
    counts: Optional[List[Any]] = None
    if differ:
        # Another sqlglot version may write the text another way: nothing is counted or compared.
        versions = QueryVersions(
            duckdb=_distribution_version("duckdb"),
            sqlglot=SQLGLOT_VERSION,
            pyarrow=_distribution_version("pyarrow"),
        )
        settled = check_requirements(
            requirements, None, recorded_sqlglot=recorded, current_sqlglot=SQLGLOT_VERSION
        )
    else:
        streams = _digested_streams(plan, layout, receipt)
        tables = tables_from_run(layout.emits_dir, streams)
        requests = [
            CountRequest(
                id=query.id,
                text=query.text,
                unit_text=query.unit_text,
                group_by=query.group_by,
                sqlglot_version=query.sqlglot_version,
            )
            for query in checked.queries
        ] + list(requirement_requests(requirements, plan.schema))
        batch = run_counts(tables, requests, effective_limits(plan))
        counts = list(batch.queries[: len(checked.queries)])
        settled = check_requirements(
            requirements,
            batch.queries[len(checked.queries) :],
            recorded_sqlglot=recorded,
            current_sqlglot=SQLGLOT_VERSION,
        )
        versions = QueryVersions(
            duckdb=batch.versions.duckdb, sqlglot=SQLGLOT_VERSION, pyarrow=batch.versions.pyarrow
        )
    rows = query_rows(
        registry,
        {query.id: query.digest for query in checked.queries},
        counts,
        profile_targets(pinned.profile) if pinned is not None else None,
        profile_targets(plan.held_out.profile) if plan.held_out is not None else None,
    )
    dimension = QueriesDimension(
        queries=rows,
        requirements=tuple(
            RequirementRow(
                query=item.requirement.query,
                expose_id=item.requirement.expose,
                column=item.requirement.column,
                kind=item.requirement.kind,
                description=item.requirement.describe(),
                status=item.status,
            )
            for item in settled
        ),
        versions=versions,
        pinned_grade=_pinned_grade(plan_document, pinned),
        held_out_grade=_grade_of(plan.held_out.document) if plan.held_out is not None else None,
    )
    return QueriesLeg(dimension)
