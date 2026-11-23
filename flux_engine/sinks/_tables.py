# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What the table sinks share: one stream held as Arrow, published as one table write.

The Iceberg and Delta sinks write a table, not files the engine names, so they have no
staging directory to discard. :class:`TableSink` takes the rows as the Arrow batching
layer cuts them and keeps the batches until :meth:`~TableSink.close`, which writes the
stream as one table when exactly the rows the stream holds arrived and writes nothing
otherwise. That is what makes the protocol's rule "close runs on the failure path too"
safe: a run that failed half-way leaves no table behind.

**A failed write is the engine's own error.** Anything the writer raises becomes
``FLX-3505`` with its class and the first line of its message, run through
:func:`~flux_engine.sinks._secrets.scrub`, and the writer's exception is not chained,
so no traceback carries a signed URL or a header.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.api.emit import EmitHeader
from flux_engine.core.errors import FluxError
from flux_engine.seam.emit import FLX_3301
from flux_engine.sinks._secrets import scrub
from flux_engine.sinks.arrow import BatchBuilder, pa
from flux_engine.sinks.errors import FLX_3504, FLX_3505
from flux_engine.sinks.receipts import SinkReceipt, StreamContext

if TYPE_CHECKING:  # pragma: no cover
    import pyarrow

__all__ = ["TableSink", "TableWrite", "failure", "require_mode"]


@dataclass(frozen=True)
class TableWrite:
    """What a table write reports: where it went, and what the writer made."""

    target: str
    table: Union[str, Tuple[str, ...]]
    writer: Mapping[str, Any] = field(default_factory=dict)


def require_mode(sink: str, mode: Any, modes: Sequence[str]) -> str:
    """Return *mode* when it is one of *modes*; ``FLX-3504`` otherwise."""
    if mode not in modes:
        raise FluxError(FLX_3504, f"the {sink} sink's mode is not one of {', '.join(modes)}")
    return str(mode)


def failure(
    target: str, exc: BaseException, known: Sequence[str] = (), *, sink: str = ""
) -> FluxError:
    """The engine's ``FLX-3505`` for a writer's failure: class and scrubbed first line.

    *known* is every string of the target the writer may echo, which the scrub writes as its label.
    """
    detail = (scrub(str(exc), known, sink=sink).strip().splitlines() or [""])[0][:300]
    return FluxError(
        FLX_3505,
        f"writing to {target} failed: {type(exc).__name__}{': ' + detail if detail else ''}",
    )


class TableSink:
    """Holds one stream's batches and publishes them as one table write on close."""

    #: The sink's registry name, used in receipts.
    name: ClassVar[str] = ""

    def __init__(self, context: StreamContext) -> None:
        self.context = context
        self._header: Optional[EmitHeader] = None
        self._builder: Optional[BatchBuilder] = None
        self._batches: List["pyarrow.RecordBatch"] = []
        self._closed = False
        self._failed = False
        self.receipt: Optional[SinkReceipt] = None

    # -- what a subclass provides ---------------------------------------------

    @property
    def target(self) -> str:
        """Where the stream goes, as an error and the receipt name it: a label (:func:`~flux_engine.core.target_label.target_label`)."""
        raise NotImplementedError

    def _known_strings(self) -> List[str]:
        """The strings of the target a writer may echo in an error; the scrub writes them as labels."""
        return []

    def _publish(self, table: "pyarrow.Table") -> TableWrite:
        """Write *table*; the whole stream, once. May raise anything the writer raises."""
        raise NotImplementedError

    # -- the EmitSink protocol --------------------------------------------------

    def open(self, header: EmitHeader) -> None:
        """Check the header belongs to the stream this sink was set up for."""
        if self._header is not None:
            raise FluxError(FLX_3301, f"expose {header.expose_id!r}: sink already open")
        if self._closed:
            raise FluxError(FLX_3301, f"expose {header.expose_id!r}: sink is closed")
        if header.expose_id != self.context.expose_id:
            raise FluxError(
                FLX_3504,
                f"this {self.name} sink was set up for expose {self.context.expose_id!r} "
                f"and opened on {header.expose_id!r}",
            )
        self._header = header
        self._builder = BatchBuilder(header, declared_types=self.context.declared_types)

    def write(self, row: Sequence[object]) -> None:
        """Add one rendered row."""
        if self._builder is None:
            raise FluxError(FLX_3301, "sink written to before open")
        if self._closed:
            raise FluxError(FLX_3301, f"expose {self.context.expose_id!r}: sink is closed")
        try:
            batch = self._builder.append(row)
            if batch is not None:
                self._batches.append(batch)
        except BaseException:
            # The rows of a batch that failed are gone, but they were counted:
            # remember the failure so close() cannot take the count as whole.
            self._failed = True
            raise

    def close(self) -> None:
        """Write the stream when the whole of it arrived; write nothing otherwise."""
        if self._closed:
            return
        self._closed = True
        builder = self._builder
        batches, self._batches = self._batches, []
        if builder is None or self._failed or builder.rows != self.context.rows:
            return
        try:
            tail = builder.flush()
            if tail is not None:
                batches.append(tail)
            table = pa.Table.from_batches(batches, schema=builder.schema)
            written = self._publish(table)
        except FluxError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise failure(self.target, exc, self._known_strings(), sink=self.name) from None
        self.receipt = SinkReceipt(
            sink=self.name,
            run_id=self.context.run_id,
            expose_id=self.context.expose_id,
            output_digest=self.context.output_digest.value,
            rows=builder.rows,
            target=written.target,
            table=written.table,
            writer=dict(written.writer),
        )
