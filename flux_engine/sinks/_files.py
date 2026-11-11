# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Naming, staging and publishing the files of one stream.

One stream lands as one *slice* of files under a directory per expose::

    <directory>/<exposeId>/<slice>-00000<suffix>
    <directory>/<exposeId>/<slice>-00001<suffix>
    <directory>/<exposeId>/_manifest-<slice>-<sink>.json

``<slice>`` is the first 16 hex characters of the stream's ``outputDigest``,
so the names are a function of what the run produced: the same bundle and
seed write the same names, and a re-run replaces its own files rather than
adding a second copy beside them. Another run of the same expose (another
seed) has another digest and lands beside the first, the way it would be
appended to a table.

**Nothing is visible until it is whole.** Every file is written under a
hidden ``.<name>.part`` name in the same directory, flushed to disk, and
renamed into place only after the last row is in. Files of this slice that
the new set does not contain (a re-run with fewer files per stream) are then
removed, and the manifest is written last, through its own temporary name:
it is the slice's commit marker. A failure removes what was staged. Dataset
readers skip names that start with ``.`` or ``_`` (pyarrow, Spark, DuckDB's
``*.parquet`` globs), so neither a part file nor the manifest is ever read
as data.

Standard library only; the Arrow-backed sinks build on it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Mapping, Tuple, Union

from flux_engine.api.accuracy import IDENTIFIER_PATTERN
from flux_engine.core.canonical import sha256_hex_chunks
from flux_engine.core.errors import FluxError
from flux_engine.sinks.errors import FLX_3503, FLX_3504, FLX_3505
from flux_engine.sinks.receipts import FileEntry, StreamContext

__all__ = [
    "MANIFEST_VERSION",
    "FileSlice",
    "load_manifest",
    "manifest_name",
    "require_segment",
]

#: The manifest's document version. A reader refuses one it does not know.
MANIFEST_VERSION = "flux-sink-manifest/1"

#: An expose id names a directory, so it must be one safe path segment. The
#: engine already has the grammar for that: FLUX's and FLUID's identifier
#: (``$defs.identifier``), which starts and ends with a letter, digit or
#: underscore and holds dots and hyphens only inside -- no separator, no
#: ``.`` or ``..``. The accuracy store uses the same pattern to name files.
#: The schema enforces it on the contract already; the sink checks again
#: because it is about to use the id as a directory.
_SEGMENT = IDENTIFIER_PATTERN

_READ_CHUNK = 1 << 20


def require_segment(expose_id: str) -> str:
    """Return *expose_id* when it is one safe path segment; ``FLX-3504`` otherwise."""
    if not isinstance(expose_id, str) or _SEGMENT.fullmatch(expose_id) is None:
        raise FluxError(
            FLX_3504,
            f"expose id {expose_id!r} is not a FLUID identifier, so it cannot name a directory",
        )
    return expose_id


def manifest_name(context: StreamContext, sink: str) -> str:
    """The manifest's file name for *context*'s slice as *sink* writes it.

    The sink is in the name because two file sinks may write one stream into
    one directory (``.parquet`` and ``.arrow`` files never collide, and their
    manifests must not either).
    """
    return f"_manifest-{context.slice_key}-{sink}.json"


def load_manifest(
    manifest: Union[str, Path], *, sink: str
) -> Tuple[Dict[str, Any], Tuple[Path, ...]]:
    """Read a slice manifest; return it and the paths of its files, in slice order.

    Refuses (``FLX-3503``) a manifest of another version or another sink, and
    a file entry that is not ``<exposeId>/<name>`` below the manifest's own
    directory tree, so a manifest cannot send a reader outside it.
    """
    path = Path(manifest)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FluxError(FLX_3503, f"{path} is not a readable manifest") from exc
    if (
        not isinstance(document, dict)
        or document.get("version") != MANIFEST_VERSION
        or document.get("sink") != sink
    ):
        raise FluxError(FLX_3503, f"{path} is not a {MANIFEST_VERSION} manifest of the {sink} sink")
    root = path.parent.parent
    expose_id = document.get("exposeId")
    files = []
    for entry in document.get("files", []):
        relative = PurePosixPath(str(entry.get("path", "")))
        if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != expose_id:
            raise FluxError(FLX_3503, f"{path} lists a file outside its stream: {relative}")
        require_segment(relative.parts[0])
        if relative.parts[1].startswith((".", "_")):
            raise FluxError(FLX_3503, f"{path} lists a hidden file: {relative}")
        files.append(root.joinpath(*relative.parts))
    return document, tuple(files)


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                return
            yield chunk


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    """Make the renames durable, where the platform can.

    Best effort, as in most atomic-write helpers: Windows cannot open a
    directory to sync it, and some filesystems refuse ``fsync`` on one
    (``EINVAL``). The files themselves were synced before they were renamed.
    """
    if os.name != "posix":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


@dataclass
class _Staged:
    staged: Path
    final: Path
    rows: int = 0


@dataclass
class FileSlice:
    """The files one sink writes for one stream, from staging to publication."""

    directory: Path
    context: StreamContext
    sink: str
    suffix: str

    _files: List[_Staged] = field(default_factory=list, init=False, repr=False)
    _stream_dir: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        if self.directory.exists() and not self.directory.is_dir():
            raise FluxError(FLX_3504, f"{self.directory} exists and is not a directory")
        self._stream_dir = self.directory / require_segment(self.context.expose_id)

    @property
    def stream_dir(self) -> Path:
        """``<directory>/<exposeId>``: where this slice's files go."""
        return self._stream_dir

    def prepare(self) -> None:
        """Create the stream directory and clear part files a crashed run left behind."""
        try:
            self._stream_dir.mkdir(parents=True, exist_ok=True)
            for leftover in self._stream_dir.glob(f".{self.context.slice_key}-*{self.suffix}.part"):
                leftover.unlink(missing_ok=True)
        except OSError as exc:
            raise FluxError(FLX_3505, f"{self._stream_dir}: {exc.strerror or exc}") from exc

    def new_file(self) -> Path:
        """Start the next file of the slice; returns the hidden path to write it at."""
        index = len(self._files)
        final = self._stream_dir / f"{self.context.slice_key}-{index:05d}{self.suffix}"
        staged = self._stream_dir / f".{final.name}.part"
        self._files.append(_Staged(staged=staged, final=final))
        return staged

    def count(self, rows: int) -> None:
        """Record *rows* more rows in the file being written."""
        self._files[-1].rows += rows

    @property
    def staged_paths(self) -> Tuple[Path, ...]:
        """The hidden paths written so far, in slice order (for a read-back check)."""
        return tuple(entry.staged for entry in self._files)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.directory).as_posix()

    def publish(
        self, *, header: Mapping[str, Any], writer: Mapping[str, Any]
    ) -> Tuple[FileEntry, ...]:
        """Rename every staged file into place, drop stale ones, then write the manifest."""
        replaced = 0
        try:
            entries = []
            for entry in self._files:
                _fsync_file(entry.staged)
                entries.append(
                    FileEntry(
                        path=self._relative(entry.final),
                        sha256=sha256_hex_chunks(_chunks(entry.staged)),
                        bytes=entry.staged.stat().st_size,
                        rows=entry.rows,
                    )
                )
            for entry in self._files:
                os.replace(entry.staged, entry.final)
                replaced += 1
            keep = {entry.final.name for entry in self._files}
            for stale in self._slice_files():
                if stale.name not in keep:
                    stale.unlink(missing_ok=True)
            self._write_manifest(entries, header=header, writer=writer)
        except OSError as exc:
            # Once a file is in place the slice on disk is neither the old one
            # nor the new one. A slice is whole or absent, so take all of it
            # away; running again writes it whole. Before the first rename
            # nothing published has changed, and an earlier slice stays.
            self.discard()
            if replaced:
                self._remove_slice()
            raise FluxError(FLX_3505, f"{self._stream_dir}: {exc.strerror or exc}") from exc
        _fsync_directory(self._stream_dir)
        return tuple(entries)

    def _slice_files(self) -> List[Path]:
        """Every published data file of this slice, whatever its index width."""
        pattern = re.compile(
            rf"^{re.escape(self.context.slice_key)}-\d{{5,}}{re.escape(self.suffix)}$"
        )
        return [
            path
            for path in self._stream_dir.glob(f"{self.context.slice_key}-*{self.suffix}")
            if pattern.match(path.name)
        ]

    def _remove_slice(self) -> None:
        for path in [
            *self._slice_files(),
            self._stream_dir / manifest_name(self.context, self.sink),
        ]:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort on the failure path
                pass

    def _write_manifest(
        self, entries: List[FileEntry], *, header: Mapping[str, Any], writer: Mapping[str, Any]
    ) -> None:
        document: Dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "synthetic": True,
            "sink": self.sink,
            "runId": self.context.run_id,
            "exposeId": self.context.expose_id,
            "outputDigest": self.context.output_digest.value,
            "rows": self.context.rows,
            "header": dict(header),
            "writer": dict(writer),
            "files": [entry.as_document() for entry in entries],
        }
        final = self._stream_dir / manifest_name(self.context, self.sink)
        staged = self._stream_dir / f".{final.name}.part"
        staged.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _fsync_file(staged)
        os.replace(staged, final)

    def discard(self) -> None:
        """Remove every staged file. Published files are never touched."""
        for entry in self._files:
            try:
                entry.staged.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort on the failure path
                pass
        manifest_part = self._stream_dir / f".{manifest_name(self.context, self.sink)}.part"
        try:
            manifest_part.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort on the failure path
            pass
