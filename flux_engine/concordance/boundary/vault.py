# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Where the boundary keeps a committed state's blinding value -- §4.

SEAM-FIDELITY-SPEC §4 asks ``C2`` to commit to the derived statistic under
"a per-state blinding value that never leaves the boundary". Two words in
that sentence decide this module.

**per-state.** The value belongs to the fixed record set, not to the
process that happens to be checking it. Drawn afresh on every ``fluxsim
ship`` / ``fluxsim verify``, it gives one unchanged record set a different
``C2`` on every call -- and therefore a different fidelity record, a
different digest for that record inside the evidence pack, and a different
Merkle root for the archive. Two ships of one applied run under one
signing key then produce two archives that disagree about a state nobody
touched, which is the opposite of what a commitment is for. So the value
is drawn **once**, when the state is first formed, and read back for every
later check of that same state.

**never leaves the boundary.** Keeping it therefore cannot mean writing it
into the run directory: everything under ``<state_dir>/runs/<runId>/`` is
staged into the evidence archive by
:func:`~flux_engine.evidence.pack.pack_evidence`, so a blinding value put
there would travel to every reader of the pack and blind nothing. It is
kept beside the runs instead, under ``<state_dir>/boundary/``, in a file
this module writes ``0o600`` inside a ``0o700`` directory -- outside every
tree the packer stages, and outside the bundle directory, which the packer
only ever reads.

**The state's name is its records.** An entry is filed under the emit's
``exposeId`` and
:func:`~flux_engine.concordance.boundary.slice.slice_content_digest` over
the committed records. The same records resolve to the same entry -- same
``stateOrdinal``, same blinding value, same commitment time -- and a
changed record set has no entry yet, so it is drawn a new one at the next
ordinal for that expose (§8: "appended source records | new committed
state (ordinal+1)").

**A store that cannot answer refuses.** An entry whose blinding value is
absent, or a file that cannot be read or written, is ``FLX-5013`` and the
check stops. Quietly drawing a replacement is the one thing this module
must never do: it would publish a second ``C2`` for a record set that has
not changed, and the fidelity record carrying it would look exactly like
the first.

Nothing here reads the host clock. The commitment time an entry records is
the instant its caller supplied when the state was formed.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

try:  # pragma: no cover - the POSIX branch is the one CI and every target run take
    import fcntl
except ImportError:  # pragma: no cover - Windows has no advisory file lock here
    fcntl = None  # type: ignore[assignment]

from flux_engine.concordance.boundary.slice import generate_blinding, refuse_weak_blinding
from flux_engine.concordance.errors import FLX_5013
from flux_engine.core.errors import FluxError

__all__ = [
    "BOUNDARY_DIRNAME",
    "BlindingVault",
    "LOCK_FILENAME",
    "VAULT_FILENAME",
    "VAULT_VERSION",
    "VaultedState",
]

#: The directory, under the owner's state directory, that holds everything
#: the boundary keeps to itself. A sibling of ``runs/``, never inside one:
#: the packer stages a run directory into the archive and this must not go
#: with it.
BOUNDARY_DIRNAME = "boundary"

#: The one file in that directory.
VAULT_FILENAME = "states.json"

#: The sentinel :meth:`BlindingVault.resolve` takes an exclusive advisory
#: lock on. Beside the store rather than on it: the store is replaced by
#: ``os.replace``, so a lock held on its inode would stop describing the
#: file the next caller opens.
LOCK_FILENAME = "states.lock"

#: The document's own version, so a later shape can be read back rather
#: than guessed at.
VAULT_VERSION = 1


@dataclass(frozen=True)
class VaultedState:
    """One committed state's boundary-private identity.

    ``ordinal`` and ``committed_at`` are published (they are two of §4's
    six items) but they are *decided* here, once, so that re-forming the
    state re-forms the same one. ``blinding`` is published nowhere.
    """

    expose_id: str
    slice_digest: str
    ordinal: int
    committed_at: str
    blinding: bytes

    def to_document(self) -> Dict[str, Any]:
        """The stored form. Only ever written inside the boundary directory."""
        return {
            "exposeId": self.expose_id,
            "sliceDigest": self.slice_digest,
            "stateOrdinal": self.ordinal,
            "committedAt": self.committed_at,
            "blinding": self.blinding.hex(),
        }


def _read_entry(entry: Any, *, path: Path, index: int) -> VaultedState:
    """Parse one stored entry, refusing a shape that cannot be trusted."""
    where = f"{path} entry {index}"
    if not isinstance(entry, dict):
        raise FluxError(FLX_5013, f"{where} is a {type(entry).__name__}, not an object")
    try:
        expose_id = str(entry["exposeId"])
        slice_digest = str(entry["sliceDigest"])
        ordinal = int(entry["stateOrdinal"])
        committed_at = str(entry["committedAt"])
        blinding_hex = entry["blinding"]
    except (KeyError, TypeError, ValueError) as error:
        raise FluxError(
            FLX_5013,
            f"{where} does not name a committed state and its blinding value ({error})",
        ) from None
    if not isinstance(blinding_hex, str) or not blinding_hex:
        raise FluxError(
            FLX_5013,
            f"{where} carries no blinding value, so the C2 of state {ordinal} of "
            f"{expose_id!r} cannot be recomputed",
        )
    try:
        blinding = bytes.fromhex(blinding_hex)
    except ValueError:
        raise FluxError(FLX_5013, f"{where}: the blinding value is not a hex string") from None
    # The same guard the drawing side passes: a store edited to hold a
    # public constant must be refused on the way out, not only on the way
    # in.
    refuse_weak_blinding(blinding)
    return VaultedState(
        expose_id=expose_id,
        slice_digest=slice_digest,
        ordinal=ordinal,
        committed_at=committed_at,
        blinding=blinding,
    )


@dataclass(frozen=True)
class BlindingVault:
    """The boundary's own directory under the owner's state directory."""

    directory: Path

    @classmethod
    def for_state_dir(cls, state_dir: Union[str, Path]) -> "BlindingVault":
        """The vault of the state directory a stage was given."""
        return cls(directory=Path(state_dir) / BOUNDARY_DIRNAME)

    @property
    def path(self) -> Path:
        """The file the entries live in."""
        return self.directory / VAULT_FILENAME

    def entries(self) -> Tuple[VaultedState, ...]:
        """Every stored state, ordered by expose then ordinal.

        An absent file is an empty store, not a refusal: the first check
        of the first run has nothing to read back yet. A file that exists
        and cannot be read, or that holds something other than this
        document, is ``FLX-5013`` -- an unreadable store is not an empty
        one.
        """
        if not self.path.exists():
            return ()
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as error:
            raise FluxError(
                FLX_5013, f"the boundary store at {self.path} could not be read: {error.strerror}"
            ) from error
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            raise FluxError(
                FLX_5013, f"the boundary store at {self.path} is not JSON: {error.msg}"
            ) from None
        if not isinstance(document, dict) or not isinstance(document.get("states"), list):
            raise FluxError(
                FLX_5013,
                f"the boundary store at {self.path} does not hold a list of committed states",
            )
        version = document.get("vaultVersion")
        if version != VAULT_VERSION:
            raise FluxError(
                FLX_5013,
                f"the boundary store at {self.path} is version {version!r}; this build "
                f"reads version {VAULT_VERSION}",
            )
        states = [
            _read_entry(entry, path=self.path, index=index)
            for index, entry in enumerate(document["states"])
        ]
        return tuple(sorted(states, key=lambda state: (state.expose_id, state.ordinal)))

    def find(self, *, expose_id: str, slice_digest: str) -> Optional[VaultedState]:
        """The state these records were committed as, or ``None`` for a new one."""
        for state in self.entries():
            if state.expose_id == expose_id and state.slice_digest == slice_digest:
                return state
        return None

    @property
    def lock_path(self) -> Path:
        """The advisory-lock sentinel :meth:`resolve` serialises on."""
        return self.directory / LOCK_FILENAME

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Hold an exclusive advisory lock on the vault directory.

        ``resolve`` reads the store, decides on an ordinal, draws a
        blinding value and writes the whole document back. Run twice at
        once -- and :mod:`flux_engine.engine` dispatches every stage
        through ``asyncio.to_thread``, so two ``ship`` calls against one
        ``--state-dir`` is the documented usage, not an edge case -- both
        callers read the same store and the second write drops the first
        caller's entry. The lost state is then re-drawn under a *different*
        blinding value at the next check, which is the one thing this
        module's docstring says it must never do: a second ``C2``, a
        second signed fidelity record and a second Merkle root for a
        record set nobody touched.
        """
        if fcntl is None:  # pragma: no cover - no POSIX advisory lock available
            yield
            return
        self._ensure_directory()
        descriptor = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _ensure_directory(self) -> None:
        """The ``0o700`` boundary directory, created if it is not there yet."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
        except OSError as error:
            raise FluxError(
                FLX_5013,
                f"the boundary directory at {self.directory} could not be opened "
                f"({error.strerror}); no blinding value can be kept here",
            ) from error

    def resolve(self, *, expose_id: str, slice_digest: str, committed_at: str) -> VaultedState:
        """Return this record set's state, drawing one if it has none yet.

        A drawn entry takes the next ordinal for *expose_id* (1 for the
        first) and *committed_at* as its commitment time, and is written
        back before it is handed over. An entry that already exists is
        handed back untouched, so its ordinal, its commitment time and its
        blinding value are the ones the state was formed under however
        long ago.

        The whole read-decide-draw-write sequence runs under an exclusive
        advisory lock (see :meth:`_exclusive`), and the store is re-read
        *inside* it -- a concurrent caller that added an entry between the
        first read and the lock is seen, so its entry survives and this
        one takes the ordinal after it.
        """
        existing = self.find(expose_id=expose_id, slice_digest=slice_digest)
        if existing is not None:
            return existing
        with self._exclusive():
            held = self.entries()
            for state in held:
                if state.expose_id == expose_id and state.slice_digest == slice_digest:
                    return state
            ordinals = [state.ordinal for state in held if state.expose_id == expose_id]
            drawn = VaultedState(
                expose_id=expose_id,
                slice_digest=slice_digest,
                ordinal=(max(ordinals) + 1) if ordinals else 1,
                committed_at=committed_at,
                blinding=generate_blinding(),
            )
            self._append(held, drawn)
            return drawn

    def _append(self, held: Tuple[VaultedState, ...], drawn: VaultedState) -> None:
        """Write *held* plus *drawn* back, atomically and readable only by the owner."""
        document = {
            "vaultVersion": VAULT_VERSION,
            "states": [state.to_document() for state in (*held, drawn)],
        }
        body = json.dumps(document, indent=2, sort_keys=True) + "\n"
        self._ensure_directory()
        try:
            # Written to a temporary file in the same directory and moved
            # into place, so a store half-written by an interrupted stage
            # never becomes the store the next stage reads back.
            handle, staged = tempfile.mkstemp(dir=self.directory, prefix=".states-", suffix=".json")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(staged, 0o600)
                os.replace(staged, self.path)
                # The rename itself is durable only once the directory
                # entry is: without this, a crash between the replace and
                # the next writeback can leave the store at neither the
                # old document nor the new one.
                directory_fd = os.open(str(self.directory), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except BaseException:
                Path(staged).unlink(missing_ok=True)
                raise
        except OSError as error:
            raise FluxError(
                FLX_5013,
                f"the boundary store at {self.path} could not be written "
                f"({error.strerror}); the blinding value for state "
                f"{drawn.ordinal} of {drawn.expose_id!r} has nowhere to live",
            ) from error

    def snapshot(self) -> Mapping[str, List[int]]:
        """Expose id -> the ordinals held, ascending. Names no blinding value."""
        by_expose: Dict[str, List[int]] = {}
        for state in self.entries():
            by_expose.setdefault(state.expose_id, []).append(state.ordinal)
        return by_expose

    def __repr__(self) -> str:
        """Name the directory, never the values in it."""
        return f"BlindingVault(directory={str(self.directory)!r})"
