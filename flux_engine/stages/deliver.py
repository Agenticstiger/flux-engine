# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim run --sink``: hand every stream of an applied run to one sink.

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

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from flux_engine.api.emit import EmitHeader
from flux_engine.core.errors import FluxError
from flux_engine.sinks.errors import FLX_3505
from flux_engine.sinks.receipts import SinkReceipt, StreamContext
from flux_engine.stages.layout import RunLayout, atomic_write_text

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from flux_engine.sinks.registry import SinkTarget
    from flux_engine.stages.pipeline import EmitOutcome, PipelineResult

__all__ = ["RECEIPTS_VERSION", "deliver", "receipt_line", "stream_rows", "write_receipts"]

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
        )
        for outcome in result.emits
    ]
    sinks = [target.sink_for(context) for context in contexts]
    receipts: List[SinkReceipt] = []
    for outcome, context, sink in zip(result.emits, contexts, sinks, strict=True):
        try:
            sink.open(_header(outcome))
            try:
                for row in stream_rows(outcome):
                    sink.write(row)
            finally:
                # Close on every path: a sink publishes only a whole stream,
                # so closing after a failure discards what it staged.
                sink.close()
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001 - a third-party sink may raise anything
            first = (str(exc).strip().splitlines() or [""])[0][:200]
            raise FluxError(
                FLX_3505,
                f"the {target.name} sink failed on expose {context.expose_id!r}: "
                f"{type(exc).__name__}{': ' + first if first else ''}",
            ) from exc
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
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
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
    if receipt.table is not None:
        return f"delivered {receipt.expose_id}: {rows} into {receipt.table} at {receipt.target}"
    files = len(receipt.files)
    first = receipt.files[0].path.rsplit("/", 1)[0] if receipt.files else receipt.expose_id
    where = f"{receipt.target.rstrip('/')}/{first}/"
    return (
        f"delivered {receipt.expose_id}: {rows} in {files} {receipt.sink} "
        f"file{'' if files == 1 else 's'} under {where}"
    )
