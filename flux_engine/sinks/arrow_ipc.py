# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Arrow IPC sink: one stream as Arrow IPC files (the Feather v2 format).

The same layout, naming, staging and manifest as the Parquet sink
(:mod:`flux_engine.sinks._files`), in Arrow's own file format: the batches
exactly as the batching layer typed them, readable with no conversion by
pyarrow, DuckDB, Polars and every Arrow implementation. Record batches hold
exactly ``batch_size`` rows (the last one fewer), and buffers are
compressed with zstd unless asked otherwise, so for one pyarrow version the
stream and the settings determine the bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Optional, Sequence, Union

from flux_engine.core.errors import FluxError
from flux_engine.sinks._columnar import ColumnarFileSink
from flux_engine.sinks._files import load_manifest
from flux_engine.sinks.arrow import DEFAULT_BATCH_SIZE, pa
from flux_engine.sinks.errors import FLX_3504
from flux_engine.sinks.receipts import StreamContext

if TYPE_CHECKING:  # pragma: no cover - annotations only
    import pyarrow

__all__ = ["ArrowIpcSink", "read_files", "read_stream"]

_CODECS = ("zstd", "lz4", "none")


class ArrowIpcSink(ColumnarFileSink):
    """Writes one stream as Arrow IPC files under *directory*.

    *compression* is ``zstd`` (the default), ``lz4`` or ``none``; the
    remaining parameters are :class:`~flux_engine.sinks.parquet.ParquetSink`'s,
    with ``batch_size`` also fixing the record batch size in the file.
    """

    name: ClassVar[str] = "arrow-ipc"
    suffix: ClassVar[str] = ".arrow"

    def __init__(
        self,
        directory: Union[str, Path],
        context: StreamContext,
        *,
        compression: str = "zstd",
        rows_per_file: Optional[int] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        verify: bool = False,
    ) -> None:
        super().__init__(
            directory, context, batch_size=batch_size, rows_per_file=rows_per_file, verify=verify
        )
        if compression not in _CODECS:
            raise FluxError(
                FLX_3504,
                f"compression {compression!r} is not one of {', '.join(_CODECS)}",
            )
        self.compression = compression

    def _unit(self) -> int:
        return self.batch_size

    def _open_writer(self, path: Path, schema: "pyarrow.Schema") -> Any:
        options = pa.ipc.IpcWriteOptions(
            compression=None if self.compression == "none" else self.compression
        )
        return pa.ipc.new_file(str(path), schema, options=options)

    def _write_unit(self, writer: Any, table: "pyarrow.Table") -> None:
        for batch in table.to_batches(max_chunksize=table.num_rows or None):
            writer.write_batch(batch)

    def _close_writer(self, writer: Any) -> None:
        writer.close()

    def _read_back(self, paths: Sequence[Path]) -> "pyarrow.Table":
        return read_files(paths)

    def writer_settings(self) -> Dict[str, Any]:
        settings = super().writer_settings()
        settings.update({"format": "arrow-ipc", "compression": self.compression})
        return settings


def read_files(paths: Sequence[Path]) -> "pyarrow.Table":
    """Read Arrow IPC files in order as one table."""
    tables = []
    for path in paths:
        with pa.ipc.open_file(str(path)) as reader:
            tables.append(reader.read_all())
    return pa.concat_tables(tables) if len(tables) > 1 else tables[0]


def read_stream(manifest: Union[str, Path]) -> "pyarrow.Table":
    """Read the slice an Arrow IPC manifest lists, in slice order, as one table."""
    _, paths = load_manifest(manifest, sink=ArrowIpcSink.name)
    return read_files(paths)
