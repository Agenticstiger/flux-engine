# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim run --sink`` and ``fluxsim apply --sink``: hand every stream of an applied run to one sink.

Delivery comes after ``apply`` (and after ``verify``, when that was asked
for) and before ``ship``, so a run that failed its checks lands nowhere and
the evidence pack ``ship`` builds contains what the sinks did.

**The rows a sink receives are the digest's own pre-image.** Each stream's
framed lines -- ``JCS([v1, v2, ...]) + b"\\n"``, the exact bytes its
``outputDigest`` covers (KERNEL-SPEC §8) -- are decoded back into the
rendered values and written to the sink one row at a time. Nothing is
rendered a second time, so there is no second rendering that could
disagree with the first, and the sink still cannot influence the digest:
it was computed, over these bytes, before the sink was opened. A float in a
line would mean the line was not the engine's, and is refused.

**What the sinks did is evidence.** Every receipt is recorded in
``<run>/sink-receipts.json``, a ``flux-sink-receipts/1`` document that
``ship`` packs with the rest of the run directory. Receipts are keyed by
sink, target and expose: delivering the same run to a second place adds to
the document, and delivering it again to the same place replaces that
entry. The run id is a function of the plan, so every receipt in the file
is about the same streams.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from flux_engine.api.emit import EmitHeader
from flux_engine.api.stages import StageResult
from flux_engine.core.errors import FluxError
from flux_engine.core.target_label import target_label
from flux_engine.safeio import is_regular_file, read_text_nofollow
from flux_engine.sinks._objects import is_object_target
from flux_engine.sinks._secrets import scrub
from flux_engine.sinks.errors import FLX_3505
from flux_engine.sinks.receipts import SinkReceipt, StreamContext
from flux_engine.stages.errors import FLX_2109
from flux_engine.stages.layout import (
    RUN_FILE_REMEDY,
    RunLayout,
    atomic_write_text,
    read_json_object,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from flux_engine.sinks.registry import SinkTarget
    from flux_engine.stages.pipeline import EmitOutcome, PipelineResult

__all__ = [
    "RECEIPTS_VERSION",
    "deliver",
    "deliver_run",
    "open_sink",
    "receipt_line",
    "stream_rows",
    "write_receipts",
]

#: The receipts document's version. A reader refuses one it does not know.
RECEIPTS_VERSION = "flux-sink-receipts/1"


def _refuse_float(text: str) -> Any:
    raise FluxError(FLX_3505, "an emitted line holds a float, so it is not a line the engine wrote")


def stream_rows(outcome: "EmitOutcome") -> Iterator[List[Any]]:
    """The stream's rendered rows, decoded from the very lines its digest covers."""
    for line in outcome.writer.lines():
        yield json.loads(line, parse_float=_refuse_float, parse_constant=_refuse_float)


def _header(outcome: "EmitOutcome") -> EmitHeader:
    return EmitHeader(
        expose_id=outcome.expose.expose_id,
        columns=outcome.expose.column_names,
        types=outcome.expose.type_names,
        contract_digest=outcome.resolved.contract_digest,
    )


def deliver(
    result: "PipelineResult",
    *,
    run_id: str,
    target: "SinkTarget",
    record: Optional[Callable[[SinkReceipt], object]] = None,
) -> Tuple[SinkReceipt, ...]:
    """Write every stream of *result* to a sink from *target*; return the receipts.

    A stream is complete or absent: each sink publishes only when it
    received every row, and any failure stops the delivery with the
    stream that failed named. Every sink is built before the first row is
    written, so a setting that only a stream can disprove (a table name too
    long for one expose, two exposes landing in one table) stops the
    delivery before anything lands. *record* is called with each receipt
    as soon as its stream is published, so a later stream's failure leaves
    the earlier ones on record.
    """
    contexts = [
        StreamContext(
            run_id=run_id,
            expose_id=outcome.resolved.expose_id,
            rows=outcome.result.rows,
            output_digest=outcome.result.output_digest,
            declared_types=tuple(column.declared_type for column in outcome.expose.columns),
        )
        for outcome in result.emits
    ]
    sinks = _sinks_for(target, contexts)
    receipts: List[SinkReceipt] = []
    for outcome, context, sink in zip(result.emits, contexts, sinks, strict=True):
        _write_stream(target, sink, _header(outcome), context, stream_rows(outcome))
        receipt = sink.receipt
        if receipt is None:
            raise FluxError(
                FLX_3505,
                f"the {target.name} sink did not publish expose {context.expose_id!r} "
                f"({context.rows} rows)",
            )
        receipts.append(receipt)
        if record is not None:
            record(receipt)
    return tuple(receipts)


def open_sink(
    sink: Optional[str], options: Optional[Mapping[str, str]], *, stage: str
) -> Tuple[Any, Optional[StageResult]]:
    """Resolve ``--sink`` before anything runs: the target, or the *stage*'s result that refuses it.

    A sink named wrong or set up wrong is a usage error (exit 2); a missing extra is the
    environment's (exit 1).
    """
    if sink is None:
        return None, None
    from flux_engine.sinks.registry import open_target

    try:
        return open_target(sink, options), None
    except FluxError as error:
        return None, StageResult(
            stage=stage,
            exit_code=1 if error.code.code == "FLX-3501" else 2,
            _artifacts={"code": error.code.code},
            stderr=str(error),
            error_code=error.code.code,
        )


def deliver_run(
    layout: RunLayout,
    bundle_directory: Path,
    *,
    target: "SinkTarget",
    record: Optional[Callable[[SinkReceipt], object]] = None,
) -> Tuple[SinkReceipt, ...]:
    """Write every stream of an applied run to a sink from the files of its run directory.

    ``apply --sink`` uses this, because ``apply --run-id`` commits staged files and holds no
    execution in memory (:mod:`flux_engine.stages.staging`). The streams are read without
    following a link, and each is digested as ``outputDigest`` is and compared with the
    receipt's before a row goes to the sink, so what a sink is given is what the run
    recorded. The header comes from the bundle's contract, as the run's own does; the
    column types a file sink writes (an integer's width, a decimal's precision) are the
    contract's own.
    """
    from flux_engine.core.digest import DigestRef
    from flux_engine.safeio import read_bytes_nofollow
    from flux_engine.seam.emit import output_digest
    from flux_engine.seam.resolve import load_bundle, resolve_all_emits

    run_receipt = read_json_object(layout.receipt_path, code=FLX_2109, remedy=RUN_FILE_REMEDY)
    simulation_id = str(run_receipt["simulationId"])
    resolved = {
        entry.expose_id: entry
        for entry in resolve_all_emits(load_bundle(bundle_directory), simulation_id=simulation_id)
    }
    reported = run_receipt["emits"]
    streams = []
    for expose_id in sorted(reported):
        entry = resolved.get(expose_id)
        if entry is None:
            raise FluxError(FLX_3505, f"the run holds expose {expose_id!r}, the bundle does not")
        expose = entry.compile()
        header = EmitHeader(
            expose_id=expose_id,
            columns=expose.column_names,
            types=expose.type_names,
            contract_digest=entry.contract_digest,
        )
        data = read_bytes_nofollow(layout.emit_path(expose_id))
        found = output_digest(
            {
                "exposeId": expose_id,
                "columns": list(header.columns),
                "types": list(header.types),
                "contractDigest": header.contract_digest.value,
            },
            [data],
            subject=f"outputDigest:{expose_id}",
        )
        claimed = reported[expose_id]["outputDigest"]
        if found.value != claimed:
            raise FluxError(
                FLX_3505,
                f"emits/{expose_id}.ndjson digests to {found.value}, but the receipt reports "
                f"{claimed}; nothing is delivered",
            )
        context = StreamContext(
            run_id=layout.run_id,
            expose_id=expose_id,
            rows=int(reported[expose_id]["rows"]),
            output_digest=DigestRef(
                value=claimed, domain=found.domain, subject=f"outputDigest:{expose_id}"
            ),
            declared_types=tuple(column.declared_type for column in expose.columns),
        )
        streams.append((header, data, context))
    sinks = _sinks_for(target, [context for _, _, context in streams])
    receipts: List[SinkReceipt] = []
    for (header, data, context), sink in zip(streams, sinks, strict=True):
        _write_stream(target, sink, header, context, _rows_of(data))
        receipt = sink.receipt
        if receipt is None:
            raise FluxError(
                FLX_3505,
                f"the {target.name} sink did not publish expose {context.expose_id!r} "
                f"({context.rows} rows)",
            )
        receipts.append(receipt)
        if record is not None:
            record(receipt)
    return tuple(receipts)


def _rows_of(data: bytes) -> Iterator[List[Any]]:
    for line in data.splitlines():
        yield json.loads(line, parse_float=_refuse_float, parse_constant=_refuse_float)


def _write_stream(
    target: "SinkTarget",
    sink: Any,
    header: EmitHeader,
    context: StreamContext,
    rows: Iterator[List[Any]],
) -> None:
    """One stream into one sink, closed on every path; a failure names the stream."""
    try:
        sink.open(header)
        try:
            for row in rows:
                sink.write(row)
        finally:
            # Close on every path: a sink publishes only a whole stream,
            # so closing after a failure discards what it staged.
            sink.close()
    except Exception as exc:  # noqa: BLE001 - a third-party sink may raise anything
        raise _surfaced(exc, target, context) from None


def _surfaced(exc: Exception, target: "SinkTarget", context: StreamContext) -> FluxError:
    """The engine's own error for a sink's exception, scrubbed of every credential.

    The writer's message may hold a signed URL or a key from the environment, so it passes
    through :func:`~flux_engine.sinks._secrets.scrub` with the strings the target was opened
    from, so the target and the values of its options print as labels; the caller raises the
    result ``from None``, so the writer's traceback is not chained to it. A target that does
    not carry those strings (a third-party object that takes no attribute) prints the
    exception's class alone, never its text.
    """
    from flux_engine.sinks.registry import known_of

    known = known_of(target)
    if isinstance(exc, FluxError):
        return FluxError(exc.code, scrub(exc.detail, known or (), sink=target.name))
    first = ""
    if known is not None:
        first = (scrub(str(exc), known, sink=target.name).strip().splitlines() or [""])[0][:200]
    return FluxError(
        FLX_3505,
        f"the {target.name} sink failed on expose {context.expose_id!r}: "
        f"{type(exc).__name__}{': ' + first if first else ''}",
    )


def _sinks_for(target: "SinkTarget", contexts: Sequence[StreamContext]) -> List[Any]:
    """One sink per stream, built before the first row; a failure names the stream."""
    sinks: List[Any] = []
    for context in contexts:
        try:
            sinks.append(target.sink_for(context))
        except Exception as exc:  # noqa: BLE001
            raise _surfaced(exc, target, context) from None
    return sinks


def _key(entry: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """Where a receipt's rows went: one entry per sink, target, table and expose."""
    return (
        str(entry.get("sink")),
        str(entry.get("target")),
        str(entry.get("table") or ""),
        str(entry.get("exposeId")),
    )


def write_receipts(layout: RunLayout, receipts: Sequence[SinkReceipt]) -> Path:
    """Merge *receipts* into ``<run>/sink-receipts.json``; returns its path."""
    path = layout.sink_receipts_path
    entries: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    try:
        existing = json.loads(read_text_nofollow(path)) if is_regular_file(path) else None
    except (OSError, ValueError):
        existing = None
    if (
        isinstance(existing, dict)
        and existing.get("version") == RECEIPTS_VERSION
        and existing.get("runId") == layout.run_id
    ):
        previous = existing.get("receipts")
        for entry in previous if isinstance(previous, list) else []:
            if isinstance(entry, dict):
                entries[_key(entry)] = entry
    for receipt in receipts:
        document = receipt.as_document()
        entries[_key(document)] = document
    document = {
        "version": RECEIPTS_VERSION,
        "runId": layout.run_id,
        "receipts": [entries[key] for key in sorted(entries)],
    }
    atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


def receipt_line(receipt: SinkReceipt) -> str:
    """One line a person reads: which stream went where, and how much of it."""
    rows = f"{receipt.rows} row{'' if receipt.rows == 1 else 's'}"
    receipt = dataclasses.replace(receipt, target=target_label(receipt.sink, receipt.target))
    if receipt.table is not None:
        table = "/".join(receipt.table) if isinstance(receipt.table, tuple) else receipt.table
        return f"delivered {receipt.expose_id}: {rows} into {table} at {receipt.target}"
    files = len(receipt.files)
    first = receipt.files[0].path.rsplit("/", 1)[0] if receipt.files else receipt.expose_id
    if is_object_target(receipt.target):
        # An object target's file paths are whole keys, prefix included.
        scheme, _, rest = receipt.target.partition("://")
        where = f"{scheme}://{rest.partition('/')[0]}/{first}/"
    else:
        where = f"{receipt.target.rstrip('/')}/{first}/"
    return (
        f"delivered {receipt.expose_id}: {rows} in {files} {receipt.sink} "
        f"file{'' if files == 1 else 's'} under {where}"
    )
