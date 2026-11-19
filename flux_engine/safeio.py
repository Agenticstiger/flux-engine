# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Writes and reads under the state directory that never follow a link.

The state directory is where the engine records a run: ``plan.json``, the
ledgers, the emit streams, the receipt, ``EVIDENCE.md`` and the golden records.
Every one of those is a file this engine owns the name of. A link planted at
that name, by anything with write access to the directory, must not turn the
engine's write into a write somewhere else, and must not turn a read that feeds
a digest or a report into a read of some other file.

Three rules, held in one place so a call site cannot get them subtly wrong:

* **Replace, never write through.** :func:`write_text_atomic` and
  :func:`write_bytes_atomic` stage the bytes in the target's own directory and
  ``os.replace`` them over the name. A link at the name is replaced, not
  followed, and a reader never sees a half-written file.
* **Append only to a regular file.** :func:`open_append` and
  :func:`append_text` open with ``O_NOFOLLOW`` and check the descriptor, after
  the open, is a regular file (``FLX-2117``).
* **Read what the name holds, not what it points at.** :func:`read_bytes_nofollow`
  and :func:`read_text_nofollow` open with ``O_NOFOLLOW`` and ``O_NONBLOCK`` and
  check the descriptor is a regular file, so a name swapped for a pipe or a
  link to ``/dev/zero`` cannot hang or flood a stage.

The directories themselves are checked by :func:`require_real_directory` and
:func:`refuse_links_within` (``FLX-2118``).

This module imports ``os``, so it lives outside the kernel packages, which may
not. The kernel's writers take the writing function as an argument instead.

**What this does not close.**

* The directory checks (:func:`require_real_directory`,
  :func:`refuse_links_within`) are check, then use: a directory swapped for a link
  after the check and before the write is not caught. Closing that fully needs
  every open to be relative to a directory descriptor (``dir_fd``), which a path
  based API cannot do. The file-level checks are made on the open descriptor and
  have no such gap.
* Where ``os.open`` has no ``O_NOFOLLOW`` (Windows), the open is preceded by an
  ``lstat`` of the name, which a race can still beat.
* A Windows junction is not a symbolic link to ``lstat``. Where
  ``os.path.isjunction`` exists (Python 3.12 and later) it is checked as well;
  earlier interpreters do not see one.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import IO, Any, BinaryIO, Tuple, Union

from flux_engine.core.errors import ErrorCode, FluxError, register

__all__ = [
    "FLX_2117",
    "FLX_2118",
    "UnsafeFileError",
    "append_text",
    "is_regular_file",
    "open_append",
    "read_bytes_nofollow",
    "read_text_nofollow",
    "refuse_links_within",
    "require_real_directory",
    "write_bytes_atomic",
    "write_text_atomic",
]

FLX_2117: ErrorCode = register(
    "FLX-2117",
    "a run file is a link or is not a regular file",
    "The engine appends to some files of a run in place, and it reads others "
    "back to digest or to report on. It does neither through a link: a name "
    "that is a symbolic link, a directory, a pipe or a device is not the file "
    "the engine wrote, and following it would write to, or report on, some "
    "other path. The type is checked on the open descriptor, after the open, so "
    "a name swapped between a check and the open cannot slip through. Remove "
    "the entry named in the message, or delete the run directory and run "
    "`fluxsim plan` again. The files the engine writes whole are replaced "
    "rather than refused, so a link at one of those names is overwritten "
    "without its target being touched.",
)

FLX_2118: ErrorCode = register(
    "FLX-2118",
    "a state directory is a link, or holds one",
    "The directories the engine writes under (`runs/`, the run directory, "
    "`emits/` and `golden/` in the state directory, its `boundary/` directory, "
    "a file sink's stream directory and the output directory of an evidence "
    "pack) are created by the engine and hold only what it writes. One of them "
    "being a symbolic link would send every file written under it to wherever "
    "the link points, and a run directory that already holds a link would do "
    "the same for that one name. The engine refuses before it writes anything. "
    "Remove the link (the target is not touched), or choose another "
    "--state-dir or output directory.",
)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class UnsafeFileError(FluxError, OSError):
    """``FLX-2117``, also an ``OSError``.

    The refusal is a catalogued code for the CLI and a test, and an ``OSError``
    for a caller that already treats an unreadable record as one.
    """


PathLike = Union[str, "os.PathLike[str]"]


def _is_junction(path: PathLike) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction is not None and isjunction(path))


def _lstat_is_link(path: Path) -> bool:
    """Whether *path* is a link (or a junction), for platforms whose ``open`` has no ``O_NOFOLLOW``."""
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode) or _is_junction(path)
    except OSError:
        return False


def _fsync_directory(directory: Path) -> None:
    """Make a rename durable, where the platform can; best effort, as a staged write is."""
    if os.name != "posix":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _create_staged(target: Path) -> Tuple[int, str]:
    """A new, exclusive, hidden staged file beside *target*: its descriptor and name."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW
    for _ in range(100):
        staged = target.parent / f".{target.name}-{secrets.token_hex(8)}.tmp"
        try:
            return os.open(staged, flags, 0o666), str(staged)
        except FileExistsError:
            continue
    raise FileExistsError(f"no unused staged name beside {target}")  # pragma: no cover


def _write_atomic(path: PathLike, data: Union[bytes, str]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Created here, not by mkstemp, with the mode a plain open would ask for: the
    # kernel applies the process umask, which is read nowhere and set nowhere.
    handle, staged = _create_staged(target)
    try:
        if isinstance(data, bytes):
            stream: IO[Any] = os.fdopen(handle, "wb")
        else:
            stream = os.fdopen(handle, "w", encoding="utf-8", newline="")
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, target)
        _fsync_directory(target.parent)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise
    return target


def write_bytes_atomic(path: PathLike, data: bytes) -> Path:
    """Write *data* to *path* through a staged file and one ``os.replace``.

    The staged file sits in the target's own directory, is synced, and takes the
    target's name in one rename: the name either is not there or holds the whole
    document, and a link at the name is replaced, not written through. Parent
    directories are created. Returns *path*.
    """
    return _write_atomic(path, data)


def write_text_atomic(path: PathLike, text: str) -> Path:
    """:func:`write_bytes_atomic` of *text* as UTF-8, with no newline translation."""
    return _write_atomic(path, text)


def _refusal(path: Path, why: str) -> UnsafeFileError:
    return UnsafeFileError(FLX_2117, f"{path}: {why}")


def open_append(path: PathLike) -> BinaryIO:
    """Open *path* for appending, creating it, and never through a link.

    ``O_NOFOLLOW`` makes the open fail on a link; the descriptor is then checked
    to be a regular file. ``FLX-2117`` otherwise. The caller closes the stream.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _NOFOLLOW and _lstat_is_link(target):
        raise _refusal(target, "is a link")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _NONBLOCK
    try:
        descriptor = os.open(target, flags, 0o666)
    except OSError as error:
        # A link fails the open with ELOOP; a pipe with no reader fails it with
        # ENXIO; a directory with EISDIR. Name each by what it is.
        try:
            mode = os.lstat(target).st_mode
        except OSError:
            raise error from None
        if stat.S_ISLNK(mode) or _is_junction(target):
            raise _refusal(target, "is a link") from error
        if not stat.S_ISREG(mode):
            raise _refusal(target, "is not a regular file") from error
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _refusal(target, "is not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return os.fdopen(descriptor, "ab")


def append_text(path: PathLike, text: str) -> None:
    """Append *text* (UTF-8) to *path* through :func:`open_append`."""
    with open_append(path) as stream:
        stream.write(text.encode("utf-8"))


def read_bytes_nofollow(path: PathLike) -> bytes:
    """The bytes of the regular file at *path*, read from the file that was opened.

    ``FLX-2117`` (an :class:`UnsafeFileError`, which is also an ``OSError``) for a
    name that is a link, a directory, a pipe or a device; a plain ``OSError`` for
    a name that is missing or unreadable. The type is checked on the descriptor,
    after the open, and the open does not block, so a name that is swapped for a
    pipe between a check and a read cannot hang the stage, and a link to
    ``/dev/zero`` cannot be read without end.
    """
    target = Path(path)
    if not _NOFOLLOW and _lstat_is_link(target):
        raise _refusal(target, "is a link")
    try:
        descriptor = os.open(target, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except OSError as error:
        if _lstat_is_link(target):
            raise _refusal(target, "is a link") from error
        raise
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _refusal(target, "is not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def read_text_nofollow(path: PathLike) -> str:
    """:func:`read_bytes_nofollow` decoded as UTF-8."""
    return read_bytes_nofollow(path).decode("utf-8")


def require_real_directory(path: PathLike, *, create: bool = False) -> Path:
    """*path* as a directory that is itself a directory, not a link to one.

    ``FLX-2118`` for a link, or for a name that exists and is not a directory.
    With *create*, a missing directory is made first (with its parents).
    """
    target = Path(path)
    if create:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            pass  # a file or a dangling link is here; the check below names it
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        return target
    if stat.S_ISLNK(mode) or _is_junction(target):
        raise FluxError(FLX_2118, f"{target} is a link")
    if not stat.S_ISDIR(mode):
        raise FluxError(FLX_2118, f"{target} is not a directory")
    return target


def refuse_links_within(directory: PathLike) -> None:
    """``FLX-2118`` when anything under *directory* is a link. Nothing is followed."""
    pending = [Path(directory)]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except FileNotFoundError:
            continue
        for entry in entries:
            if entry.is_symlink() or _is_junction(entry.path):
                raise FluxError(FLX_2118, f"{entry.path} is a link")
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))


def is_regular_file(path: PathLike) -> bool:
    """Whether *path* names a regular file itself, not a link to one.

    ``Path.is_file`` follows a link, so it answers "yes" for a name that a
    read through :func:`read_bytes_nofollow` is about to refuse. A stage that
    asks whether a run file is there asks this instead.
    """
    try:
        return stat.S_ISREG(os.lstat(path).st_mode) and not _is_junction(path)
    except OSError:
        return False
