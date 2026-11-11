# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What the Parquet and Arrow IPC sinks share: the lifecycle and the cutting.

:class:`ColumnarFileSink` implements :class:`~flux_engine.api.emit.EmitSink`
on top of the Arrow batching layer and :class:`~flux_engine.sinks._files.FileSlice`.
Rows arrive one at a time, become RecordBatches, and are cut into *units*
-- a Parquet row group, an IPC record batch -- and files at fixed row
counts. The cut points depend only on the stream's length and the sink's
settings, never on how the rows happened to be batched on the way in, so
the bytes on disk are a function of the stream and the settings.

**Publish on a whole stream, discard on anything less.** The sink knows from
its :class:`~flux_engine.sinks.receipts.StreamContext` how many rows the
stream holds. :meth:`close` publishes the slice when exactly that many
arrived and discards it otherwise, which is what makes the protocol's rule
"close runs on the failure path too" safe: a run that failed half-way
leaves no files behind. After a publish, :attr:`receipt` holds the
:class:`~flux_engine.sinks.receipts.SinkReceipt`; after a discard it is
``None``.

**An optional read-back check.** With ``verify=True`` the sink reads its own
staged files back before publishing them and recomputes the stream's
``outputDigest`` from them (:func:`flux_engine.sinks.arrow.output_digest_of`).
A mismatch is ``FLX-3505`` and nothing is published. It costs about as much
as rendering the stream again, so it is off by default; the test suite runs
the same comparison on every change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Mapping, Optional, Sequence, Union

from flux_engine.api.emit import EmitHeader
from flux_engine.core.digest import digest_of_bytes
from flux_engine.core.errors import FluxError
from flux_engine.seam.emit import FLX_3301
from flux_engine.sinks._files import FileSlice
from flux_engine.sinks.arrow import (
    DEFAULT_BATCH_SIZE,
    BatchBuilder,
    header_document,
    output_digest_of,
    pa,
)
from flux_engine.sinks.errors import FLX_3504, FLX_3505
from flux_engine.sinks.receipts import SinkReceipt, StreamContext

if TYPE_CHECKING:  # pragma: no cover - annotations only
    import pyarrow

__all__ = ["ColumnarFileSink", "FileTarget", "optional_positive_int", "positive_int"]


def positive_int(name: str, value: Any) -> int:
    """Check one integer option; ``FLX-3504`` naming the option otherwise."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FluxError(FLX_3504, f"{name} must be a positive integer, got {value!r}")
    return value


def optional_positive_int(name: str, value: Any) -> Optional[int]:
    """:func:`positive_int`, with ``None`` meaning "no limit"."""
    return None if value is None else positive_int(name, value)


@dataclass
class _Writing:
    """The file being written: its writer and how many rows it holds."""

    writer: Any
    rows: int = 0


class ColumnarFileSink:
    """Base for the sinks that write Arrow-typed files. Subclasses pick the format."""

    #: The sink's registry name, used in receipts and manifests.
    name: ClassVar[str] = ""
    #: The file name suffix, dot included.
    suffix: ClassVar[str] = ""

    def __init__(
        self,
        directory: Union[str, Path],
        context: StreamContext,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        rows_per_file: Optional[int] = None,
        verify: bool = False,
    ) -> None:
        self.context = context
        self.batch_size = positive_int("batch-size", batch_size)
        self.rows_per_file = optional_positive_int("rows-per-file", rows_per_file)
        if not isinstance(verify, bool):
            raise FluxError(FLX_3504, f"verify must be true or false, got {verify!r}")
        self.verify = verify
        self.directory = Path(directory)
        self._slice = FileSlice(self.directory, context, sink=self.name, suffix=self.suffix)
        self._header: Optional[EmitHeader] = None
        self._builder: Optional[BatchBuilder] = None
        self._pending: List["pyarrow.RecordBatch"] = []
        self._pending_rows = 0
        self._current: Optional[_Writing] = None
        self._closed = False
        self._failed = False
        self.receipt: Optional[SinkReceipt] = None

    # -- what a subclass provides ---------------------------------------------

    def _unit(self) -> int:
        """Rows per unit: a Parquet row group, an IPC record batch."""
        raise NotImplementedError

    def _open_writer(self, path: Path, schema: "pyarrow.Schema") -> Any:
        raise NotImplementedError

    def _write_unit(self, writer: Any, table: "pyarrow.Table") -> None:
        raise NotImplementedError

    def _close_writer(self, writer: Any) -> None:
        raise NotImplementedError

    def _read_back(self, paths: Sequence[Path]) -> "pyarrow.Table":
        raise NotImplementedError

    def writer_settings(self) -> Dict[str, Any]:
        """The settings that shape the bytes, for the manifest and the receipt."""
        return {
            "library": "pyarrow",
            "version": pa.__version__,
            "batchSize": self.batch_size,
            "rowsPerFile": self.rows_per_file,
        }

    # -- the EmitSink protocol --------------------------------------------------

    def open(self, header: EmitHeader) -> None:
        """Check the header belongs to the stream this sink was set up for, then stage."""
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
        self._builder = BatchBuilder(
            header,
            batch_size=self.batch_size,
            metadata={
                "flux.synthetic": "true",
                "flux.runId": self.context.run_id,
                "flux.outputDigest": self.context.output_digest.value,
            },
        )
        self._slice.prepare()

    def write(self, row: Sequence[object]) -> None:
        """Add one rendered row."""
        if self._builder is None:
            raise FluxError(FLX_3301, "sink written to before open")
        if self._closed:
            raise FluxError(FLX_3301, f"expose {self.context.expose_id!r}: sink is closed")
        try:
            batch = self._builder.append(row)
            if batch is not None:
                self._take(batch, final=False)
        except BaseException:
            # The rows of a batch that failed are gone, but they were counted:
            # remember the failure so close() cannot take the count as whole.
            self._failed = True
            raise

    def close(self) -> None:
        """Publish the slice when the whole stream arrived; discard it otherwise."""
        if self._closed:
            return
        self._closed = True
        builder = self._builder
        if builder is None:
            return
        if self._failed or builder.rows != self.context.rows:
            self._abort()
            return
        try:
            tail = builder.flush()
            self._take(tail, final=True)
            self._finish_file()
            if not self._slice.staged_paths:
                # An empty stream still lands as one file holding the schema,
                # so a reader finds the table and its columns, with no rows.
                self._start_file()
                self._finish_file()
            if self.verify:
                self._check_read_back()
            files = self._slice.publish(
                header=header_document(builder.header), writer=self.writer_settings()
            )
        except OSError as exc:
            self._abort()
            raise FluxError(FLX_3505, f"{self._slice.stream_dir}: {exc.strerror or exc}") from exc
        except BaseException:
            self._abort()
            raise
        self.receipt = SinkReceipt(
            sink=self.name,
            run_id=self.context.run_id,
            expose_id=self.context.expose_id,
            output_digest=self.context.output_digest.value,
            rows=builder.rows,
            target=str(self.directory),
            files=files,
            writer=self.writer_settings(),
        )

    # -- cutting rows into units and files ----------------------------------------

    def _take(self, batch: Optional["pyarrow.RecordBatch"], *, final: bool) -> None:
        if batch is not None and batch.num_rows:
            self._pending.append(batch)
            self._pending_rows += batch.num_rows
        while self._pending_rows:
            room = None
            if self.rows_per_file is not None:
                used = self._current.rows if self._current is not None else 0
                room = self.rows_per_file - used
            unit = self._unit() if room is None else min(self._unit(), room)
            if self._pending_rows < unit and not final:
                return
            size = min(unit, self._pending_rows)
            table = pa.Table.from_batches(self._pending, schema=self._builder_schema())
            chunk, rest = table.slice(0, size), table.slice(size)
            self._pending = rest.to_batches()
            self._pending_rows -= size
            if self._current is None:
                self._start_file()
            assert self._current is not None
            self._write_unit(self._current.writer, chunk.combine_chunks())
            self._current.rows += size
            self._slice.count(size)
            if self.rows_per_file is not None and self._current.rows == self.rows_per_file:
                self._finish_file()

    def _builder_schema(self) -> "pyarrow.Schema":
        assert self._builder is not None
        return self._builder.schema

    def _start_file(self) -> None:
        path = self._slice.new_file()
        self._current = _Writing(writer=self._open_writer(path, self._builder_schema()))

    def _finish_file(self) -> None:
        if self._current is not None:
            self._close_writer(self._current.writer)
            self._current = None

    def _abort(self) -> None:
        if self._current is not None:
            try:
                self._close_writer(self._current.writer)
            except Exception:  # pragma: no cover - the slice is being thrown away
                pass
            self._current = None
        self._pending, self._pending_rows = [], 0
        self._slice.discard()

    def _check_read_back(self) -> None:
        table = self._read_back(self._slice.staged_paths)
        found = output_digest_of(table)
        if found.value != self.context.output_digest.value:
            raise FluxError(
                FLX_3505,
                f"the files written for expose {self.context.expose_id!r} digest to "
                f"{found.value}, not to the stream's {self.context.output_digest.value}",
            )


#: A stream that is never written: building a sink for it runs every check
#: on the settings, and touches nothing on disk.
_PROBE = StreamContext(
    run_id="probe", expose_id="probe", rows=0, output_digest=digest_of_bytes(b"", subject="probe")
)


@dataclass(frozen=True)
class FileTarget:
    """A file sink's checked settings, handing out one sink per stream."""

    name: str
    directory: str
    sink_class: Any
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The text, not a Path: Path("") is ".", and a sink must never fall
        # back to the current directory because a target was left out.
        if not self.directory.strip():
            raise FluxError(
                FLX_3504,
                f"the {self.name} sink needs a directory: --sink {self.name}:DIRECTORY",
            )
        # Every setting is checked now, before the simulation runs.
        self.sink_class(Path(self.directory), _PROBE, **self.settings)

    def sink_for(self, context: StreamContext) -> ColumnarFileSink:
        """A fresh sink for one stream."""
        sink: ColumnarFileSink = self.sink_class(Path(self.directory), context, **self.settings)
        return sink
