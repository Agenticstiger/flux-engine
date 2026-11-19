# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The accuracy store's file operations, none of which follows a symbolic link.

The store keeps one regular file per prediction id in each of two
directories, ``predictions`` and ``verdicts``, inside its own directory
(``<state-dir>/accuracy``). Every operation here works through a
descriptor of one of those directories, each opened with ``O_NOFOLLOW``
relative to the one above it, and names its file relative to that
descriptor, so a link planted where a record or a directory belongs is
refused or replaced, and never followed:

* :func:`open_directory` refuses a link, or anything but a directory, in
  place of the store's own directory, ``predictions`` or ``verdicts``. The
  directory that holds the store (the state directory ``--state-dir``
  names) and everything above it may be reached through links: where the
  store lives is the operator's choice, what is inside it is the store's.
* :func:`read_entry` opens a record with ``O_NOFOLLOW | O_NONBLOCK`` and
  checks its type on the descriptor before reading, so a link, a
  directory or a pipe is refused and a pipe cannot hang the read.
* :func:`create_once` publishes a sealed prediction. It writes a staged
  file beside the record's name, then gives it that name with ``link``,
  which never replaces anything: a record appears whole or not at all, and
  a name already taken, by a record or by a link, is left as it was.
  This is the Maildir delivery (write in ``tmp``, ``link`` into ``new``)
  and python-atomicwrites' ``overwrite=False`` move (MIT, 1.4.1), done
  relative to a directory descriptor.
* :func:`replace_entry` writes a verdict: a staged file renamed over its
  name with ``os.replace``, which replaces a link at that name rather than
  writing through it (python-atomicwrites' ``overwrite=True`` path).

Staged files are created with ``O_CREAT | O_EXCL | O_NOFOLLOW`` under a
short name no record can have (a dot, 16 random hex digits and a ``.tmp``
suffix, whatever the record's name), and are fsynced before they are
published, so a crash leaves at worst a stray staged file, never a
truncated record. A filesystem without hard links (FAT, some network and
FUSE mounts) answers ``link`` with ``EPERM`` or ``ENOTSUP``; there the
record is created by name with ``O_EXCL``, which is exclusive but not
atomic.

``dir_fd`` support is required, which Linux and macOS have.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path
from typing import List, Optional

__all__ = [
    "EntryRefused",
    "create_once",
    "list_names",
    "open_directory",
    "read_entry",
    "replace_entry",
]

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

#: The flags every file this module creates is opened with: exclusive, never
#: through a link. The mode is ``0o666`` before the umask, as a plain
#: ``write_text`` creates it, so a record keeps the permissions it always had.
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC

#: What a filesystem without hard links answers ``link`` with.
_NO_HARD_LINKS = frozenset(
    code
    for code in (getattr(errno, name, None) for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP"))
    if code is not None
)

#: Staged names are 64 random bits; a clash is retried, and this many clashes
#: in a row means something other than chance is taking the names.
_STAGE_ATTEMPTS = 8


class EntryRefused(Exception):
    """An entry of the store is a link, or not the type of file it should be.

    ``str(error)`` is the reason in words, such as ``"is a symbolic link"``.
    :func:`open_directory` names the refused directory in ``entry``; for a
    record, the caller names it.
    """

    def __init__(self, reason: str, *, entry: Optional[Path] = None) -> None:
        super().__init__(reason)
        self.entry = entry


def _mode_of(dir_fd: int, name: str) -> Optional[int]:
    """The type and mode of *name* itself, not of what a link points at; ``None`` when unreadable."""
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
    except OSError:
        return None


def _describe(mode: int, wanted: str) -> str:
    """Why an entry of *mode* is not the *wanted* type, in words."""
    if stat.S_ISLNK(mode):
        return "is a symbolic link"
    if stat.S_ISDIR(mode):
        kind = "a directory"
    elif stat.S_ISREG(mode):
        kind = "a regular file"
    elif stat.S_ISFIFO(mode):
        kind = "a named pipe"
    elif stat.S_ISSOCK(mode):
        kind = "a socket"
    else:
        kind = "a device"
    return f"is {kind}, not {wanted}"


def _open_child(parent_fd: int, name: str, *, create: bool) -> Optional[int]:
    """A descriptor on the directory *name* in *parent_fd*, or ``None`` when there is none.

    Never through a link: a link in its place, or anything else that is not
    a directory, raises :class:`EntryRefused`.
    """
    if create:
        try:
            os.mkdir(name, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        return os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError:
        mode = _mode_of(parent_fd, name)
        if mode is not None and not stat.S_ISDIR(mode):
            raise EntryRefused(_describe(mode, "a directory")) from None
        raise


def open_directory(root: Path, kind: str, *, create: bool) -> Optional[int]:
    """A descriptor on the directory *kind* of the store at *root*, or ``None`` when there is none.

    With *create*, the directories are made first. The directory that holds
    *root* is opened as named, links and all; *root* and *kind* are each
    opened with ``O_NOFOLLOW`` relative to the one above, so a link in place
    of either, or anything else that is not a directory, raises
    :class:`EntryRefused` naming it. The caller closes the descriptor.
    """
    # Not os.path.abspath: it drops "link/.." as text, where the kernel, and
    # every other stage given the same --state-dir, goes up from the target.
    root = Path(root)
    if root.name in ("", ".", ".."):
        raise ValueError(f"the accuracy store's directory must be named, not {str(root)!r}")
    if create:
        root.parent.mkdir(parents=True, exist_ok=True)
    try:
        holder_fd = os.open(root.parent, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        root_fd = _open_child(holder_fd, root.name, create=create)
    except EntryRefused as refused:
        raise EntryRefused(str(refused), entry=root) from None
    finally:
        os.close(holder_fd)
    if root_fd is None:
        return None
    try:
        return _open_child(root_fd, kind, create=create)
    except EntryRefused as refused:
        raise EntryRefused(str(refused), entry=root / kind) from None
    finally:
        os.close(root_fd)


def list_names(dir_fd: int) -> List[str]:
    """The names in the directory *dir_fd*, sorted."""
    return sorted(os.listdir(dir_fd))


def read_entry(dir_fd: int, name: str) -> Optional[bytes]:
    """The bytes of the regular file *name*, or ``None`` when no entry has that name.

    A link, a directory, a pipe or a device raises :class:`EntryRefused`.
    The type is checked on the descriptor that is then read, after the open
    and before any read, and the open does not block, so a name swapped for
    a pipe between a check and a read cannot hang the reader.
    """
    try:
        descriptor = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        mode = _mode_of(dir_fd, name)
        if mode is not None and not stat.S_ISREG(mode):
            raise EntryRefused(_describe(mode, "a regular file")) from None
        raise
    try:
        # Before os.fdopen, which refuses a directory with its own error.
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise EntryRefused(_describe(mode, "a regular file"))
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _remove(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def _write_all(descriptor: int, data: bytes) -> None:
    """Write *data* to *descriptor*, flush it to the disk, and close it."""
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _stage(dir_fd: int, name: str, data: bytes) -> str:
    """Write *data* to a new staged file beside *name*; return the staged file's name.

    The staged name does not contain *name*, so it stays short however long
    the record's name is: a record name of up to 255 bytes, the most a
    filesystem takes, can always be staged.
    """
    for _attempt in range(_STAGE_ATTEMPTS):
        staged = f".{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(staged, _CREATE, 0o666, dir_fd=dir_fd)
        except FileExistsError:
            continue
        try:
            _write_all(descriptor, data)
        except BaseException:
            _remove(dir_fd, staged)
            raise
        return staged
    raise FileExistsError(errno.EEXIST, f"no free staged name beside {name}")


def _create_by_name(dir_fd: int, name: str, data: bytes) -> bool:
    """Create *name* itself, exclusively: the route where ``link`` is not supported."""
    try:
        descriptor = os.open(name, _CREATE, 0o666, dir_fd=dir_fd)
    except FileExistsError:
        return False
    try:
        _write_all(descriptor, data)
    except BaseException:
        _remove(dir_fd, name)
        raise
    return True


def create_once(dir_fd: int, name: str, data: bytes) -> bool:
    """Publish *data* as *name* unless an entry has that name.

    ``True`` when this call published it, ``False`` when the name was taken,
    by anything, a link included. Nothing that is there is changed.
    """
    staged = _stage(dir_fd, name, data)
    linked: Optional[bool]
    try:
        os.link(staged, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        linked = True
    except FileExistsError:
        linked = False
    except OSError as error:
        if error.errno not in _NO_HARD_LINKS:
            raise
        linked = None
    finally:
        _remove(dir_fd, staged)
    published = _create_by_name(dir_fd, name, data) if linked is None else linked
    if published:
        os.fsync(dir_fd)
    return published


def replace_entry(dir_fd: int, name: str, data: bytes) -> None:
    """Write *data* as *name*, replacing what is there.

    ``os.replace`` renames over the name itself, so a link at *name* is
    replaced and the file it pointed at is not touched. A directory there
    raises :class:`EntryRefused`.
    """
    staged = _stage(dir_fd, name, data)
    try:
        os.replace(staged, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError:
        _remove(dir_fd, staged)
        mode = _mode_of(dir_fd, name)
        if mode is not None and stat.S_ISDIR(mode):
            raise EntryRefused(_describe(mode, "a regular file")) from None
        raise
    except BaseException:
        _remove(dir_fd, staged)
        raise
    os.fsync(dir_fd)
