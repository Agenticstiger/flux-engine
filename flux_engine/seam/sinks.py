# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Where emitted rows land: the two sinks the engine ships.

A sink is the last thing in the emit path and the least important: by
KERNEL-SPEC §8 the ``outputDigest`` is "computed **above** the sink; the
sink cannot influence it", so a sink cannot make a stream wrong, only
absent. Both implementations here satisfy
:class:`flux_engine.api.emit.EmitSink` structurally -- ``open`` once,
``write`` zero or more times, ``close`` once, on the failure path too.

:class:`NullSink` exists for the same reason ``/dev/null`` does. A run
that only needs the digest -- ``fluxsim plan``, a golden comparison, a
re-materialisation whose rows are then thrown away -- opens one, and the
fact that its ``outputDigest`` equals the file-writing sink's is the
mechanical statement of §8's rule.

:class:`NdjsonSink` writes the same framed lines the digest covers, so
the file on disk is the digest's own pre-image minus the header framing.
It re-renders nothing: the values it receives have already been rendered
per §9, and it serialises them through the one canonicaliser.

**The choice this module is making, stated.** ``NdjsonSink`` is kept, and
it is kept in order to be wired, not as an ornament. Today the only
production consumer of the ``EmitSink`` port is ``NullSink``: the pipeline
hard-codes it and ``apply`` writes the emit file itself from a fully
buffered byte string, so the streaming extension point exists and nothing
reaches it -- and a declared ``records.max`` of 100 000 is held in the
writer's buffer and again in the bytes handed to ``write_bytes``. The
alternative was to delete the class and the port and document the
buffered write as the one mechanism; that was rejected because the memory
profile it locks in is the wrong one for the record counts
SEAM-FIDELITY-SPEC section 2 already allows. What has to follow is an
optional sink parameter on ``stages.pipeline.execute`` that ``apply``
supplies, so this class becomes the writer rather than a second way of
writing. Until it does, this module has one mechanism in use and one
waiting to be connected, and this paragraph is the place that says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, List, Optional, Sequence

from flux_engine.api.emit import EmitHeader
from flux_engine.core.errors import FluxError
from flux_engine.seam.emit import FLX_3301, row_line

__all__ = ["NdjsonSink", "NullSink"]


@dataclass
class NullSink:
    """Accepts and discards rows, counting them.

    Not a test double: it is the sink a stage uses when the artifact it
    needs is the digest rather than the data. Because §8 computes the
    digest above the sink, a run that writes to this sink and a run that
    writes a file are answerable for the same ``outputDigest``.
    """

    header: Optional[EmitHeader] = field(default=None, init=False)
    rows: int = field(default=0, init=False)
    closed: bool = field(default=False, init=False)

    def open(self, header: EmitHeader) -> None:
        """Record the header. Opening twice is a lifecycle error."""
        if self.header is not None:
            raise FluxError(FLX_3301, f"expose {header.expose_id!r}: sink already open")
        self.header = header

    def write(self, row: Sequence[object]) -> None:
        """Count one row and drop it."""
        if self.header is None:
            raise FluxError(FLX_3301, "sink written to before open")
        if self.closed:
            raise FluxError(FLX_3301, f"expose {self.header.expose_id!r}: sink is closed")
        self.rows += 1

    def close(self) -> None:
        """Mark the stream closed. Closing twice is harmless."""
        self.closed = True


@dataclass
class NdjsonSink:
    """Writes rows as newline-delimited canonical JSON arrays.

    One line per row, ``JCS([v1, v2, ...]) + b"\\n"`` -- byte-for-byte the
    ``line(row)`` of KERNEL-SPEC §8, so concatenating the file with the
    framed header reproduces the digest's pre-image exactly.

    Give it a *path* and it owns the handle: the parent directory is
    created, the file is opened on :meth:`open` and closed on
    :meth:`close`. Give it a *stream* instead and it writes to that and
    never closes it -- a caller that owns a handle keeps owning it.

    The header is **not** written into the file. §8's stream is the rows;
    the header travels in the run's attestation
    (:mod:`flux_engine.seam.attest`), where it can be checked against the
    contract rather than re-parsed out of a data file.
    """

    path: Optional[Path] = None
    stream: Optional[BinaryIO] = None

    header: Optional[EmitHeader] = field(default=None, init=False)
    rows: int = field(default=0, init=False)
    closed: bool = field(default=False, init=False)
    _own_handle: Optional[BinaryIO] = field(default=None, init=False, repr=False)
    _written: List[bytes] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if (self.path is None) == (self.stream is None):
            raise ValueError("NdjsonSink takes exactly one of path= or stream=")
        if self.path is not None:
            self.path = Path(self.path)

    def open(self, header: EmitHeader) -> None:
        """Open the destination and record the header."""
        if self.header is not None:
            raise FluxError(FLX_3301, f"expose {header.expose_id!r}: sink already open")
        self.header = header
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._own_handle = self.path.open("wb")

    def write(self, row: Sequence[object]) -> None:
        """Serialise one already-rendered row and write its line."""
        if self.header is None:
            raise FluxError(FLX_3301, "sink written to before open")
        if self.closed:
            raise FluxError(FLX_3301, f"expose {self.header.expose_id!r}: sink is closed")
        line = row_line(list(row))
        self._target().write(line)
        self._written.append(line)
        self.rows += 1

    def close(self) -> None:
        """Close the handle this sink opened; leave a caller's handle alone."""
        self.closed = True
        if self._own_handle is not None:
            self._own_handle.close()
            self._own_handle = None

    def written_bytes(self) -> bytes:
        """Everything this sink wrote, for a caller comparing against the file."""
        return b"".join(self._written)

    def _target(self) -> BinaryIO:
        handle = self._own_handle if self._own_handle is not None else self.stream
        if handle is None:  # pragma: no cover - open() always sets one
            raise FluxError(FLX_3301, "sink has no destination")
        return handle
