# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a sink is told before a stream, and what it hands back after one.

:class:`StreamContext` is everything a delivery knows about a stream before
its first row: the run, the expose, how many rows are coming and the
``outputDigest`` the engine already computed over them. A sink names its
files and its database slices after that digest, so the same run lands in
the same place every time, and it counts rows against the expected total,
so a stream that stopped short is discarded rather than published.

:class:`SinkReceipt` is what a sink returns once a stream is in place: the
rows, and either the files with their sha256 or the table with its mode. It
holds nothing secret -- a database target is recorded without credentials --
because receipts are written into the run directory and from there into the
evidence pack.

Standard library only: the registry, the CLI and the stages read these
types without loading any optional extra.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from flux_engine.core.digest import DigestRef

__all__ = ["FileEntry", "SinkReceipt", "StreamContext"]


@dataclass(frozen=True)
class StreamContext:
    """One stream as a delivery sees it before the first row.

    Attributes
    ----------
    run_id
        The run the stream belongs to.
    expose_id
        The expose the stream realises; the header a sink is opened with
        must name the same one.
    rows
        How many rows the stream holds. A sink publishes only when it has
        received exactly this many.
    output_digest
        The stream's ``outputDigest``, computed above the sink (KERNEL-SPEC
        §8). Sinks key file names and database slices on it.
    """

    run_id: str
    expose_id: str
    rows: int
    output_digest: DigestRef

    def __post_init__(self) -> None:
        if isinstance(self.rows, bool) or not isinstance(self.rows, int) or self.rows < 0:
            raise ValueError(f"rows must be a non-negative integer, got {self.rows!r}")

    @property
    def digest_hex(self) -> str:
        """The digest's 64 hex characters, without the ``sha256:`` prefix."""
        return self.output_digest.hex

    @property
    def slice_key(self) -> str:
        """The first 16 hex characters of the digest: the prefix of every file name."""
        return self.digest_hex[:16]


@dataclass(frozen=True)
class FileEntry:
    """One file a sink wrote: its path below the sink's directory, size and hash."""

    path: str
    sha256: str
    bytes: int
    rows: int

    def as_document(self) -> Dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes, "rows": self.rows}


@dataclass(frozen=True)
class SinkReceipt:
    """What one sink did with one stream.

    ``target`` is where the rows went, as the user named it: a directory, or
    a database driver and its location with any credentials removed.
    ``writer`` records the settings that shape the bytes or the table (the
    library version, the compression, the mode), so a reader can tell why
    two receipts for one ``outputDigest`` list different file hashes.
    """

    sink: str
    run_id: str
    expose_id: str
    output_digest: str
    rows: int
    target: str
    files: Tuple[FileEntry, ...] = ()
    table: Optional[str] = None
    writer: Mapping[str, Any] = field(default_factory=dict)

    def as_document(self) -> Dict[str, Any]:
        """The receipt as a JSON-ready document, keys in a stable order."""
        document: Dict[str, Any] = {
            "sink": self.sink,
            "runId": self.run_id,
            "exposeId": self.expose_id,
            "outputDigest": self.output_digest,
            "rows": self.rows,
            "target": self.target,
            "synthetic": True,
            "writer": dict(self.writer),
        }
        if self.files:
            document["files"] = [entry.as_document() for entry in self.files]
        if self.table is not None:
            document["table"] = self.table
        return document
