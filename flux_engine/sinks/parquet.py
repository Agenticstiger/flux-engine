# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The Parquet sink: one stream as zstd-compressed Parquet files.

Rows land at ``<directory>/<exposeId>/<slice>-NNNNN.parquet`` with a
``_manifest-<slice>-parquet.json`` beside them (see :mod:`flux_engine.sinks._files`),
typed by the Arrow batching layer: DECIMAL as Parquet DECIMAL, TIMESTAMP as
a UTC-adjusted millisecond timestamp, DATE as DATE, BYTES as binary.

**Settings that shape the bytes are pinned, not defaulted.** The codec and
its level, the format version, the data page version and the dictionary
setting are passed explicitly, so a pyarrow release that changes one of its
defaults does not quietly change the files. Row groups hold exactly
``row_group_size`` rows (the last one fewer), whatever the batch size, so
for one pyarrow version a stream and its settings determine the file bytes;
the manifest records the version and the settings beside each file's
sha256.

Each file's Arrow schema metadata carries the stream header, the run id,
the ``outputDigest`` and ``flux.synthetic = true``, which Parquet stores as
key-value metadata a reader sees without Arrow. :func:`read_stream` reads a
slice back from its manifest, and
:func:`flux_engine.sinks.arrow.output_digest_of` turns the result back into
the stream's digest.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Optional, Sequence, Union

from flux_engine.core.errors import FluxError
from flux_engine.sinks._columnar import ColumnarFileSink, positive_int
from flux_engine.sinks._files import load_manifest
from flux_engine.sinks._optional import require
from flux_engine.sinks.arrow import DEFAULT_BATCH_SIZE, pa
from flux_engine.sinks.errors import FLX_3504
from flux_engine.sinks.receipts import StreamContext

if TYPE_CHECKING:  # pragma: no cover - annotations only
    import pyarrow

pq = require("pyarrow.parquet", extra="arrow", feature="the Parquet sink")

__all__ = [
    "DEFAULT_COMPRESSION",
    "DEFAULT_COMPRESSION_LEVEL",
    "DEFAULT_ROW_GROUP_SIZE",
    "ParquetSink",
    "read_stream",
]

DEFAULT_COMPRESSION = "zstd"
#: zstd's own default level, pinned rather than left to the writer.
DEFAULT_COMPRESSION_LEVEL = 3
#: pyarrow's own default row group size, pinned for the same reason.
DEFAULT_ROW_GROUP_SIZE = 1_048_576

_CODECS = ("zstd", "snappy", "gzip", "lz4", "brotli", "none")
_FORMAT_VERSION = "2.6"
_DATA_PAGE_VERSION = "1.0"


class ParquetSink(ColumnarFileSink):
    """Writes one stream as Parquet files under *directory*.

    Parameters
    ----------
    directory
        Where the files go; ``<directory>/<exposeId>/`` is created.
    context
        The stream's run, expose, row count and ``outputDigest``.
    compression, compression_level
        The codec (``zstd`` by default; ``snappy``, ``gzip``, ``lz4``,
        ``brotli`` or ``none``) and, for codecs that take one, its level
        (zstd defaults to 3; the others to pyarrow's default for them).
    row_group_size
        Rows per row group (default 1,048,576).
    rows_per_file
        Start a new file after this many rows; ``None`` (the default) writes
        one file per stream.
    batch_size
        Rows per Arrow batch while converting; it changes memory use, not
        the bytes written.
    verify
        Read the files back and recompute the ``outputDigest`` before
        publishing them.
    """

    name: ClassVar[str] = "parquet"
    suffix: ClassVar[str] = ".parquet"

    def __init__(
        self,
        directory: Union[str, Path],
        context: StreamContext,
        *,
        compression: str = DEFAULT_COMPRESSION,
        compression_level: Optional[int] = None,
        row_group_size: int = DEFAULT_ROW_GROUP_SIZE,
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
        self.compression_level = _level_for(compression, compression_level)
        self.row_group_size = positive_int("row-group-size", row_group_size)

    def _unit(self) -> int:
        return self.row_group_size

    def _open_writer(self, path: Path, schema: "pyarrow.Schema") -> Any:
        return pq.ParquetWriter(
            str(path),
            schema,
            compression=self.compression,
            compression_level=self.compression_level,
            version=_FORMAT_VERSION,
            data_page_version=_DATA_PAGE_VERSION,
            use_dictionary=True,
            write_statistics=True,
        )

    def _write_unit(self, writer: Any, table: "pyarrow.Table") -> None:
        # One call, one row group: the unit is already cut to size.
        writer.write_table(table, row_group_size=table.num_rows)

    def _close_writer(self, writer: Any) -> None:
        writer.close()

    def _read_back(self, paths: Sequence[Path]) -> "pyarrow.Table":
        return _read_files(paths)

    def writer_settings(self) -> Dict[str, Any]:
        settings = super().writer_settings()
        settings.update(
            {
                "format": "parquet",
                "formatVersion": _FORMAT_VERSION,
                "dataPageVersion": _DATA_PAGE_VERSION,
                "compression": self.compression,
                "compressionLevel": self.compression_level,
                "rowGroupSize": self.row_group_size,
            }
        )
        return settings


def _level_for(compression: str, level: Optional[int]) -> Optional[int]:
    """The level the codec will use: pinned when not given, checked when given.

    zstd without a level gets 3, zstd's own default; another codec with
    levels gets pyarrow's default for it, recorded in the manifest. A level
    for a codec that has none, or outside the codec's range, is refused
    before the run rather than failing the first write after it.
    """
    takes_level = compression != "none" and pa.Codec.supports_compression_level(compression)
    if level is None:
        if compression == "zstd":
            return DEFAULT_COMPRESSION_LEVEL
        return int(pa.Codec.default_compression_level(compression)) if takes_level else None
    if not takes_level:
        raise FluxError(FLX_3504, f"compression {compression!r} takes no compression-level")
    positive_int("compression-level", level)
    lowest = max(1, int(pa.Codec.minimum_compression_level(compression)))
    highest = int(pa.Codec.maximum_compression_level(compression))
    if not lowest <= level <= highest:
        raise FluxError(
            FLX_3504,
            f"compression-level for {compression} must be between {lowest} and {highest}, "
            f"got {level}",
        )
    return level


def _read_files(paths: Sequence[Path]) -> "pyarrow.Table":
    tables = [pq.read_table(str(path)) for path in paths]
    return pa.concat_tables(tables) if len(tables) > 1 else tables[0]


def read_stream(manifest: Union[str, Path]) -> "pyarrow.Table":
    """Read the slice a Parquet manifest lists, in slice order, as one table.

    ``output_digest_of(read_stream(path))`` is the stream's ``outputDigest``
    for a slice this sink wrote.
    """
    _, paths = load_manifest(manifest, sink=ParquetSink.name)
    return _read_files(paths)
