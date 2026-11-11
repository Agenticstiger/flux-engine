# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The committed state, read from a ``file://`` slice -- SEAM-FIDELITY-SPEC §4.

    **Committed state**: the owner fixes the record set (``file://`` slice
    in v0.1; partition/sequence boundary recorded); ``stateOrdinal``
    increments per state.

    What leaves the boundary in v0.1: ``S``, ``C1``, ``C2``,
    ``stateOrdinal``, record count (or declared range), commitment time.
    **Never a record.**

:meth:`CommittedState.to_document` is that list and nothing else, plus the
``G`` the state was committed under -- a digest of *declarations*, which
the verifier recomputes for itself from the contract set (§5) and which
travels only so that a mismatch can be named (``FLX-5011``) instead of
surfacing as an unexplained failed comparison.

The slice format in v0.1 is newline-delimited JSON objects: one record per
line, keyed by column name. It is refused (``FLX-5035``) rather than
guessed at when it carries a scheme other than ``file://``, a line that is
not one JSON object, or a binary floating-point number -- which
KERNEL-SPEC §2.1 keeps out of every hashed path, and every value read here
reaches one.

Two things this module deliberately does not do.

**It does not read the host clock.** The commitment time is the owner's
declaration about when they fixed the records; reading the clock here
would make one record set produce a different committed state on every
read.

**It does not decide which blinding value a state is committed under.**
``C2`` is "sha256 over canonical ``S`` plus a per-STATE blinding value
that never leaves the boundary" (§4). *Per-state* is the whole of it: the
value belongs to the fixed record set, not to the invocation that happens
to be checking it, so :func:`generate_blinding` is called **once**, when
the state is formed, and the drawn value is kept by
:class:`~flux_engine.concordance.boundary.store.BoundaryStore` under the
owner's state directory. Every later check of that same record set --
another ``ship``, a ``verify``, a re-ship -- reads the same value back and
recomputes the same ``C2``. :func:`commit_state` therefore takes the value
as an argument and :class:`CommittedState` never carries it; the store,
not this module, is where the draw happens.

The state a value belongs to is identified by
:func:`slice_content_digest` over the records, paired with the emit's
``exposeId``: the same records commit to the same state (and the same
``C2``), and a changed record set is a new state with the next ordinal.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from flux_engine.concordance.boundary.merkle import (
    CATEGORY_BODY_BYTES,
    ColumnCommitment,
    LeafFunction,
    category_body,
    commit_column,
    order_commitments,
    units_body,
    units_body_width,
)
from flux_engine.concordance.boundary.statistic import (
    DerivedStatistic,
    Row,
    compute_statistic,
    encode_value,
)
from flux_engine.concordance.compiler import CompiledFidelity
from flux_engine.concordance.errors import FLX_5035
from flux_engine.concordance.predicate import ColumnKind, NullPolicy
from flux_engine.core.canonical import canonical_bytes, nfc, sha256_hex
from flux_engine.core.clock import parse_rfc3339_ms
from flux_engine.core.errors import FluxError

__all__ = [
    "C2_PREFIX",
    "CommittedState",
    "MINIMUM_BLINDING_BYTES",
    "SLICE_DIGEST_PREFIX",
    "SliceReference",
    "commit_state",
    "commitment_c2",
    "generate_blinding",
    "read_slice",
    "refuse_weak_blinding",
    "slice_content_digest",
]

#: Domain tag on the ``C2`` preimage, so a commitment over the derived
#: statistic cannot be confused with a digest of the same bytes taken for
#: any other purpose.
C2_PREFIX = b"flux-fidelity-c2/1\x00"

#: Domain tag on the committed-slice content digest, so the name a state
#: is filed under cannot collide with any other digest this engine takes.
SLICE_DIGEST_PREFIX = b"flux-fidelity-slice/1\x00"

#: The shortest blinding value this module will commit under. Shorter than
#: the digest it feeds, a blinding value is guessable, and a guessable
#: blinding value is no blinding value.
MINIMUM_BLINDING_BYTES = 32


@dataclass(frozen=True)
class SliceReference:
    """The ``file://`` slice a committed state was fixed from.

    §4 asks for the "partition/sequence boundary recorded". For a file
    slice the sequence boundary is the line range that was read, so a
    later state over the same file can say which records it appended
    rather than only that it holds more of them.
    """

    uri: str
    first_line: int
    last_line: int

    def to_document(self) -> Dict[str, Any]:
        """The canonical form recorded with the committed state."""
        return {
            "uri": self.uri,
            "firstLine": self.first_line,
            "lastLine": self.last_line,
        }


def _refuse_float(text: str) -> Decimal:
    raise FluxError(
        FLX_5035,
        "the slice carries a binary floating-point number; numbers are exact decimal "
        "strings (KERNEL-SPEC §2.1)",
    )


def read_slice(
    uri: str, *, maximum: Optional[int] = None, expose_id: Optional[str] = None
) -> Tuple[List[Row], SliceReference]:
    """Read a ``file://`` slice of newline-delimited JSON records.

    Blank lines are skipped and do not consume a sequence position. Every
    other line must be one JSON object; a line that is an array, a scalar,
    or unparseable is ``FLX-5035``, as is a number spelled as a JSON float
    -- ``1.5`` in the slice is refused, ``"1.5"`` is read exactly.

    ``--committed-slice`` is repeatable, once per expose, so every refusal
    here names the slice it is about: the file it was reading and, when the
    caller passes one, the *exposeId* that slice was given for. A refusal
    that named neither would leave an operator with several slices unable
    to tell which one is wrong. A path is not a record, so naming it takes
    nothing out of the boundary.
    """
    where = f"the committed slice for {expose_id!r}" if expose_id else "the committed slice"
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise FluxError(
            FLX_5035,
            # Not `not '<scheme>'`: for a bare path the scheme is empty,
            # and the message then read "... not '': /tmp/slice.ndjson",
            # which names the mistake as a quoted empty string.
            f"{where} is a file:// URL in v0.1; got {uri}"
            + ("" if parsed.scheme else " (a bare path is not a URL)"),
        )
    if parsed.netloc not in ("", "localhost"):
        raise FluxError(FLX_5035, f"{where} names no host other than localhost: {uri}")
    path = Path(unquote(parsed.path))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FluxError(FLX_5035, f"{where} at {path} could not be read: {exc.strerror}") from exc

    rows: List[Row] = []
    first_line = 0
    last_line = 0
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line, parse_float=_refuse_float)
        except json.JSONDecodeError as exc:
            raise FluxError(
                FLX_5035, f"{where} at {path} line {number} is not JSON: {exc.msg}"
            ) from None
        if not isinstance(record, dict):
            raise FluxError(
                FLX_5035,
                f"{where} at {path} line {number} is a {type(record).__name__}, not an object",
            )
        rows.append({nfc(str(key)): value for key, value in record.items()})
        if first_line == 0:
            first_line = number
        last_line = number
        if maximum is not None and len(rows) > maximum:
            raise FluxError(
                FLX_5035,
                f"{where} at {path} holds more than the declared records.max of {maximum}",
            )
    if not rows:
        raise FluxError(FLX_5035, f"{where} at {path} holds no records")
    return rows, SliceReference(uri=uri, first_line=first_line, last_line=last_line)


def slice_content_digest(rows: Sequence[Row]) -> str:
    """The name a committed record set is filed under: a digest of its records.

    Taken over the *parsed* records in the order the slice holds them,
    canonicalised (KERNEL-SPEC §2.1), rather than over the file's raw
    bytes: re-serialising the same records with different key order or
    spacing is the same record set, and it must map to the same committed
    state -- and so to the same ordinal, the same blinding value and the
    same ``C2``. Appending, removing or editing a record changes the
    digest, which is §8's "appended source records -> new committed state
    (ordinal+1)".

    A path is not a record and neither is a digest of one, so this value
    is a name the boundary files a state under; it is not part of §4's
    list of what leaves the boundary and
    :meth:`CommittedState.to_document` does not carry it.
    """
    body = canonical_bytes({"records": [dict(row) for row in rows]})
    return f"sha256:{sha256_hex(SLICE_DIGEST_PREFIX + len(body).to_bytes(8, 'big') + body)}"


def generate_blinding() -> bytes:
    """A fresh :data:`MINIMUM_BLINDING_BYTES`-byte blinding value.

    One place every caller draws from, so no call site can quietly settle
    for a constant that satisfies the length check.

    Called **once per committed state**, from
    :meth:`~flux_engine.concordance.boundary.store.BoundaryStore.commit`,
    at the moment the state is first formed. A stage that called it on
    every invocation would give one unchanged record set a different
    ``C2`` -- and so a different fidelity record and a different evidence
    archive -- each time it was checked.
    """
    return secrets.token_bytes(MINIMUM_BLINDING_BYTES)


def refuse_weak_blinding(blinding: bytes) -> None:
    """Refuse a blinding value that is too short, or that is a constant.

    The length check alone is satisfiable by a public constant --
    ``bytes(32)`` is 32 bytes and guessable by anyone -- which would make
    ``C2`` recomputable by an observer who can guess ``S``, exactly what
    §4's "a per-state blinding value that never leaves the boundary" rules
    out. A single-byte-repeated value (all zeros, all ``0xff``) is the
    shape that mistake takes, so it is refused by name rather than left to
    a length check that cannot see it.
    """
    if len(blinding) < MINIMUM_BLINDING_BYTES:
        raise FluxError(
            FLX_5035,
            f"a blinding value is at least {MINIMUM_BLINDING_BYTES} bytes, got {len(blinding)}",
        )
    if len(set(blinding)) == 1:
        raise FluxError(
            FLX_5035,
            "a blinding value of one repeated byte is a public constant, not a blinding "
            "value; draw it from a cryptographic source (secrets.token_bytes)",
        )


def commitment_c2(statistic: DerivedStatistic, blinding: bytes) -> str:
    """``C2``: sha256 over canonical ``S`` plus the per-state blinding value.

    Refuses a blinding value shorter than
    :data:`MINIMUM_BLINDING_BYTES`. The blinding value is appended after a
    length prefix so no two ``(statistic, blinding)`` pairs can produce
    one preimage by moving the boundary between them.
    """
    refuse_weak_blinding(blinding)
    body = canonical_bytes(statistic.to_document())
    preimage = C2_PREFIX + len(body).to_bytes(8, "big") + body + blinding
    return f"sha256:{sha256_hex(preimage)}"


@dataclass(frozen=True)
class CommittedState:
    """One fixed record set, as it is published outside the boundary."""

    expose_id: str
    ordinal: int
    g: str
    c1: Tuple[ColumnCommitment, ...]
    c2: str
    statistic: DerivedStatistic
    record_count: int
    committed_at: str
    committed_at_ms: int
    slice_reference: SliceReference

    def to_document(self) -> Dict[str, Any]:
        """Exactly §4's list of what leaves the boundary.

        ``S``, ``C1``, ``C2``, ``stateOrdinal``, the record count and the
        commitment time -- plus ``G`` and the slice's sequence boundary,
        which are declarations about the state rather than anything read
        out of it. No record, no per-column null count, no blinding value.
        """
        return {
            "exposeId": self.expose_id,
            "stateOrdinal": self.ordinal,
            "G": self.g,
            "C1": [entry.to_document() for entry in self.c1],
            "C2": self.c2,
            "S": self.statistic.to_document(),
            "recordCount": self.record_count,
            "committedAt": self.committed_at,
            "slice": self.slice_reference.to_document(),
        }

    def column_root(self, column: str) -> Optional[str]:
        """The ``C1`` root committed for *column*, or ``None``."""
        for entry in self.c1:
            if entry.column == column:
                return entry.root
        return None


def _column_bodies(
    rows: Sequence[Row],
    compiled: CompiledFidelity,
) -> Dict[str, Tuple[List[Optional[bytes]], int]]:
    """Encode every declared column into fixed-width leaf bodies.

    A ``RANGE`` column commits its grid value; a ``SUPPORT`` column
    commits the digest of its NFC bytes. A null commits nothing -- the
    position carries the reserved null encoding -- and a null in a column
    declared ``null: fail`` refuses the commitment, because a state that
    cannot be checked is not a state worth committing.
    """
    grid = compiled.block.grid
    bodies: Dict[str, Tuple[List[Optional[bytes]], int]] = {}
    for name in sorted(compiled.block.columns):
        column = compiled.block.columns[name]
        if column.kind is ColumnKind.RANGE:
            width = units_body_width(grid, column.interval)
        else:
            width = CATEGORY_BODY_BYTES
        encoded: List[Optional[bytes]] = []
        for index, row in enumerate(rows):
            if name not in row:
                raise FluxError(
                    FLX_5035, f"column {name!r} is absent from the committed slice row {index}"
                )
            cell = row[name]
            if cell is None:
                if column.null_policy is NullPolicy.FAIL:
                    raise FluxError(FLX_5035, f"column {name!r} row {index}: null is refused")
                encoded.append(None)
                continue
            if column.kind is ColumnKind.RANGE:
                encoded.append(units_body(encode_value(cell, column, grid, row_index=index), width))
            else:
                if not isinstance(cell, str):
                    raise FluxError(
                        FLX_5035,
                        f"column {name!r} row {index}: value class {type(cell).__name__!r}",
                    )
                encoded.append(category_body(cell))
        bodies[name] = (encoded, width)
    return bodies


def commit_state(
    *,
    rows: Sequence[Row],
    compiled: CompiledFidelity,
    ordinal: int,
    committed_at: str,
    blinding: bytes,
    slice_reference: SliceReference,
    leaf_function: Optional[LeafFunction] = None,
) -> CommittedState:
    """Fix *rows* as a committed state: ``C1``, ``S``, ``C2``.

    The order is deliberate. ``C1`` first, so the commitment covers the
    records before any aggregate is formed from them; then ``S``, which
    refuses (``FLX-5005``) a record count outside ``records`` or a group
    below ``records.min``; then ``C2`` over the canonical ``S`` and the
    state's blinding value. A state that fails any of the three is not
    published half-formed.

    *blinding* is the value this record set was committed under, which
    :meth:`~flux_engine.concordance.boundary.store.BoundaryStore.commit`
    draws once and keeps; calling this function with a freshly drawn value
    on every check would move ``C2`` under a state that has not changed.
    """
    if ordinal < 0:
        raise FluxError(FLX_5035, "a stateOrdinal is a non-negative counter")
    committed_at_ms = parse_rfc3339_ms(committed_at)
    bodies = _column_bodies(rows, compiled)
    commitments = order_commitments(
        [
            commit_column(
                column=name,
                bodies=encoded,
                padded_to=compiled.block.records.maximum,
                width=width,
                leaf_function=leaf_function,
            )
            for name, (encoded, width) in sorted(bodies.items())
        ]
    )
    statistic = compute_statistic(rows, compiled, subject="committed state")
    return CommittedState(
        expose_id=compiled.block.expose_id,
        ordinal=ordinal,
        g=compiled.g.value,
        c1=commitments,
        c2=commitment_c2(statistic, blinding),
        statistic=statistic,
        record_count=len(rows),
        committed_at=committed_at,
        committed_at_ms=committed_at_ms,
        slice_reference=slice_reference,
    )
