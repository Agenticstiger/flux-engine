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
holds nothing secret -- a target is recorded as a label of its non-secret parts --
because receipts are written into the run directory and from there into the
evidence pack.

Standard library only: the registry, the CLI and the stages read these
types without loading any optional extra.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from flux_engine.core.digest import DigestRef
from flux_engine.core.target_label import target_label

__all__ = ["FileEntry", "RecordedGrade", "SinkReceipt", "StreamContext"]


@dataclass(frozen=True)
class RecordedGrade:
    """The privacy grade a run is held to, worked out again from its bound record and its bundle.

    ``grade`` is ``P0``, ``P1``, ``P3``, ``P4`` or ``P5`` (ADR 0020) when the run's record states
    one, else ``None``, and ``problem`` then names what is missing or does not check: a run that
    pins no profile and declares no generator plugin, a plan, ``run-digest.json`` or ``receipt.json``
    that does not bind to the run, or a bundle that is not the plan's.

    ``profile_grade`` is the grade worked out from the profile the command was given, when that
    profile is the one the run pins, and ``None`` otherwise; ``profile_problem`` then says why a
    profile does not pair, when one was given. An external audience is held to ``profile_grade``
    alone.
    """

    grade: Optional[str] = None
    problem: Optional[str] = None
    profile_grade: Optional[str] = None
    profile_problem: Optional[str] = None


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
    declared_types
        The contract's own spelling of each column's type, positionally aligned to the
        header (``smallint``, ``decimal(18, 4)``), or empty when the caller has none. The
        header carries the closed set's ``INT`` alone, so this is where a file sink learns
        an integer column's width. No digest covers it.
    privacy_grade
        The grade the run is held to, worked out from its bound record and its bundle, and from
        the pinned profile when the command was given it
        (:class:`RecordedGrade`, :func:`flux_engine.stages.deliver.privacy_grade_of`), or
        ``None`` when the caller did not read the run's record. A sink whose destination
        declares an audience compares it with that audience's minimum (ADR 0038). No digest
        covers it.
    bundle_directory
        The bundle the run was planned from, or ``None``. A sink that needs the declarations
        behind a stream (its keys, its history, the topic the contract binds it to) reads
        them there. It never says what grade the run has. No digest covers it.
    simulation_id
        The Simulation the run realises, or ``None`` when the caller does not say.
    delivered
        The receipts the run's ``sink-receipts.json`` already holds for this stream's expose, as
        their documents (any sink, any target), or empty. A sink whose destination keeps its own
        record of what it received may compare it with this one. Every other sink ignores it.
        No digest covers it.
    """

    run_id: str
    expose_id: str
    rows: int
    output_digest: DigestRef
    declared_types: Tuple[str, ...] = ()
    privacy_grade: Optional[RecordedGrade] = None
    bundle_directory: Optional[Path] = None
    simulation_id: Optional[str] = None
    delivered: Tuple[Mapping[str, Any], ...] = ()

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

    ``target`` is where the rows went: a directory, or a database driver and its
    location. The document and the printed line show it only as its label
    (:func:`~flux_engine.core.target_label.target_label`), never as typed.
    ``writer`` records the settings that shape the bytes or the table (the
    library version, the compression, the mode), so a reader can tell why
    two receipts for one ``outputDigest`` list different file hashes.
    ``table`` is a name, or the levels of an identifier as a tuple where a catalogue
    names tables by levels (an Iceberg table), which the document writes as a list.
    """

    sink: str
    run_id: str
    expose_id: str
    output_digest: str
    rows: int
    target: str
    files: Tuple[FileEntry, ...] = ()
    table: Optional[Union[str, Tuple[str, ...]]] = None
    writer: Mapping[str, Any] = field(default_factory=dict)
    #: Tells apart two targets that print alike, such as two databases named by the same
    #: environment variable: a short digest of the target after its credentials are removed.
    target_id: Optional[str] = None

    def as_document(self) -> Dict[str, Any]:
        """The receipt as a JSON-ready document, keys in a stable order."""
        document: Dict[str, Any] = {
            "sink": self.sink,
            "runId": self.run_id,
            "exposeId": self.expose_id,
            "outputDigest": self.output_digest,
            "rows": self.rows,
            "target": target_label(self.sink, self.target),
            "synthetic": True,
            "writer": dict(self.writer),
        }
        if self.files:
            document["files"] = [entry.as_document() for entry in self.files]
        if self.target_id is not None:
            document["targetId"] = self.target_id
        if self.table is not None:
            document["table"] = list(self.table) if isinstance(self.table, tuple) else self.table
        return document
