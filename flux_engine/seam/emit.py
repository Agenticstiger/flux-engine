# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The emit stream: the ordered header, ``outputDigest``, and the seam digest.

Implements KERNEL-SPEC §8 and SEAM-FIDELITY-SPEC §3.

§8 spells the stream exactly::

    header       = {"exposeId", "columns": [name...], "types": [logical type...],
                    "contractDigest"}
    line(row)    = JCS([v1, v2, ...]) + b"\\n"        # values rendered per §9
    outputDigest = "sha256:" + hex(sha256(b"flux-emit/1\\x00" + JCS(header) + b"\\x00"
                                          + concat(line(row) for row in rows)))

**Rows are arrays.** §8 gives the reason in one clause: "JCS sorts object
keys, so objects would erase the order". A row emitted as an object would
be digested in alphabetical column order, and two contracts that declare
the same columns in different orders would produce the same stream --
which is precisely the difference the seam exists to preserve.

**The digest is computed above the sink.** :class:`EmitWriter` renders,
frames and digests; the sink is handed bytes that have already been
accounted for. §8: "Computed **above** the sink; the sink cannot influence
it." :class:`~flux_engine.seam.sinks.NullSink` and
:class:`~flux_engine.seam.sinks.NdjsonSink` therefore produce the same
``outputDigest`` for the same rows, and a test pins that.

**The framing has no ambiguous concatenation.** The label, the header and
the row block are separated by NUL bytes, and every line ends in a
newline, so no two different (header, rows) pairs share a pre-image: JCS
output contains neither a NUL nor a bare newline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from flux_engine.api.emit import EmitHeader, EmitSink
from flux_engine.core.canonical import canonical_bytes
from flux_engine.core.digest import DigestRef, digest_of_document, digest_of_stream
from flux_engine.core.errors import FluxError, register
from flux_engine.core.seedtree import digest_bytes, seam_bytes
from flux_engine.seam.conform import RenderedRow, Row, render_row
from flux_engine.seam.contract import CompiledExpose

__all__ = [
    "EMIT_LABEL",
    "EXCLUDED_FROM_REUSABLE_DIGEST",
    "FLX_3301",
    "FLX_3302",
    "EmitWriter",
    "EmitResult",
    "SeamDigests",
    "emit_header_document",
    "output_digest",
    "row_line",
    "seam_digest",
    "seam_digests",
    "seam_entropy_bytes",
    "seam_reusable_digest",
    "strip_excluded_from_fidelity_block",
]

FLX_3301 = register(
    "FLX-3301",
    "emit writer used out of order",
    "A stream is opened once, written to zero or more times, and closed "
    "once. Writing before the header is opened, writing after the stream is "
    "closed, or opening twice would put rows outside the framing the "
    "outputDigest covers (KERNEL-SPEC §8), so the writer refuses rather "
    "than digesting a stream whose shape nobody declared.",
)

FLX_3302 = register(
    "FLX-3302",
    "a required expose column has no runtime attribute mapped onto it",
    "A scenario-driven emit renders each moment onto the expose's declared "
    "columns through the mapping :mod:`flux_engine.seam.mapping` resolves: "
    "an alias declared for the vertical, an override declared on the emit, "
    "or the column's own name matching a runtime attribute. A column the "
    "contract marks required that none of those three resolve leaves the "
    "row with no value to place, and a required column may not be null "
    "(FLX-3202) -- so the run is refused where the mapping is decided, "
    "before a row exists, rather than on every row it would produce. "
    "Declare the source under the emit's mapping block, or add the column "
    "to the vertical's alias table.",
)

#: §8's domain-separation label. Part of the digest pre-image, so a change
#: to it moves every ``outputDigest`` and is a ``kernelVersion`` bump.
EMIT_LABEL = b"flux-emit/1"

_NUL = b"\x00"
_NEWLINE = b"\n"

#: SEAM-FIDELITY-SPEC §3's excluded set: the declarations that may change
#: without invalidating a committed statistic. ``G`` is ``H`` computed with
#: these removed, so "a change that leaves ``G`` unchanged keeps the
#: committed statistic reusable".
#:
#: ``tolerance`` and ``minVariance`` are removed from the *properties* §2
#: declares them on, and ``maxStateAge`` from ``fidelity.budget`` -- the
#: three positions §2 gives them, not every position a key of that name
#: can appear in. A depth-blind filter erased the whole
#: ``fidelity.columns.tolerance`` entry of a contract with a column called
#: ``tolerance`` (a plausible name in a measurement contract): its range,
#: support, null policy and generator family all vanished from ``G``, so
#: editing that column's declared range -- "any other declaration in the
#: block", which §8 says must force a new committed state -- left ``G``
#: unmoved and kept reusing a state committed under the old range.
EXCLUDED_FROM_REUSABLE_DIGEST: Tuple[str, ...] = ("tolerance", "minVariance")

_FIDELITY_KEY = "fidelity"
_PROPERTIES_KEY = "properties"
_BUDGET_KEY = "budget"
_MAX_STATE_AGE_KEY = "maxStateAge"


# ---------------------------------------------------------------------------
# The header and the stream
# ---------------------------------------------------------------------------


def emit_header_document(expose: CompiledExpose, contract_digest: DigestRef) -> Dict[str, Any]:
    """Build §8's header for *expose*.

    The four fields §8 names, and no others: the expose id, the declared
    column names in declared order, the canonical logical type spelling of
    each column, and the referenced contract's digest. The types travel as
    the closed set's own spelling (``DECIMAL(18,4)``, not the contract's
    ``decimal(18, 4)``), so two contracts that spell one type differently
    but mean the same thing produce one stream digest.
    """
    return {
        "exposeId": expose.expose_id,
        "columns": list(expose.column_names),
        "types": list(expose.type_names),
        "contractDigest": contract_digest.value,
    }


def row_line(rendered: Sequence[Any]) -> bytes:
    """``JCS([v1, v2, ...]) + b"\\n"`` -- §8's ``line(row)``."""
    return canonical_bytes(list(rendered)) + _NEWLINE


def output_digest(header: Mapping[str, Any], lines: Iterable[bytes], *, subject: str) -> DigestRef:
    """Digest a framed emit stream in the ``jcs-ndjson-stream`` domain (§8)."""
    material = EMIT_LABEL + _NUL + canonical_bytes(header) + _NUL + b"".join(lines)
    return digest_of_stream(material, subject=subject)


# ---------------------------------------------------------------------------
# H and G (SEAM-FIDELITY-SPEC §3)
# ---------------------------------------------------------------------------


def _seam_document(
    contract_document: Mapping[str, Any],
    emit: Mapping[str, Any],
    columns: Sequence[str],
    extension: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The document §3 hashes: the contract, the emit, the extension, the columns.

    ``extension`` is the emit's entry in the document-level
    ``extensions["com.agentics.flux/1"].emits`` map (SEAM-FIDELITY-SPEC
    §2). The key is **present only when the emit has such an entry**, so
    ``H`` for a bundle that declares none is byte-identical to what it was
    before the block moved out of ``spec.emits[]`` -- the shipped example
    keeps its digest, its seed root and its whole stream.
    """
    document: Dict[str, Any] = {
        "contract": dict(contract_document),
        "emit": dict(emit),
        "columns": list(columns),
    }
    if extension is not None:
        document["extension"] = dict(extension)
    return document


def seam_digest(
    contract_document: Mapping[str, Any],
    emit: Mapping[str, Any],
    columns: Sequence[str],
    *,
    extension: Optional[Mapping[str, Any]] = None,
    subject: str = "H",
) -> DigestRef:
    """``H`` -- SEAM-FIDELITY-SPEC §3 and KERNEL-SPEC §3.

    The ``jcs-document`` digest of ``{"contract": <parsed .fluid.yml>,
    "emit": <emits[] entry>, "extension": <the emit's entry in the
    document-level extension block, when it has one>, "columns": [declared
    column names in order]}``. KERNEL-SPEC §3 derives the run's seed root
    from it, so "changing the contract changes every stream" is a
    mechanical consequence rather than a promise: the canonicaliser
    normalises NFC spellings and key order first, so a re-ordered YAML file
    is the same ``H`` and a re-worded contract is not.
    """
    return digest_of_document(
        _seam_document(contract_document, emit, columns, extension), subject=subject
    )


def strip_excluded_from_fidelity_block(block: Mapping[str, Any]) -> Dict[str, Any]:
    """Remove §3's excluded set from one fidelity block, by position.

    ``tolerance``/``minVariance`` come off the entries of
    ``properties[]``; ``maxStateAge`` comes off ``budget``. ``encoding``,
    ``records``, ``columns`` and ``failureAction`` are copied through
    untouched, so a column that happens to be *named* ``tolerance`` keeps
    every declaration ``G`` has to be sensitive to.
    """
    result: Dict[str, Any] = {}
    for key, item in block.items():
        if key == _PROPERTIES_KEY and _is_list(item):
            result[key] = [
                (
                    {k: v for k, v in entry.items() if k not in EXCLUDED_FROM_REUSABLE_DIGEST}
                    if isinstance(entry, Mapping)
                    else entry
                )
                for entry in item
            ]
        elif key == _BUDGET_KEY and isinstance(item, Mapping):
            result[key] = {k: v for k, v in item.items() if k != _MAX_STATE_AGE_KEY}
        else:
            result[key] = item
    return result


def _is_list(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _strip_excluded(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Remove §3's excluded set from an emit entry or an extension entry.

    The excluded set lives inside the ``fidelity`` block and nowhere else,
    so this walks the entry's own keys and rewrites exactly that one.
    """
    result: Dict[str, Any] = {}
    for key, item in value.items():
        if key == _FIDELITY_KEY and isinstance(item, Mapping):
            result[key] = strip_excluded_from_fidelity_block(item)
        else:
            result[key] = item
    return result


def seam_reusable_digest(
    contract_document: Mapping[str, Any],
    emit: Mapping[str, Any],
    columns: Sequence[str],
    *,
    extension: Optional[Mapping[str, Any]] = None,
    subject: str = "G",
) -> DigestRef:
    """``G`` -- ``H`` with SEAM-FIDELITY-SPEC §3's excluded set removed.

    §8's change table reads: "tolerance / ``minVariance`` / ``maxStateAge``
    only -> ``H`` moves, ``G`` unchanged -> re-run the check against the
    cached ``S``, new record"; "any other declaration in the block or the
    contract -> ``G`` moves -> new committed state required". So ``G`` is
    what decides whether a data owner has to commit a fresh state, and it
    is deliberately blind to exactly three declarations and nothing else.

    The excluded set is removed from the extension member too: since the
    block moved to ``extensions["com.agentics.flux/1"].emits[<exposeId>]``
    (SEAM-FIDELITY-SPEC §2) that is where every ``tolerance``, every
    ``minVariance`` and ``budget.maxStateAge`` now lives, and a ``G`` blind
    only to the emit entry would move on a tolerance edit §8 says leaves
    the committed statistic reusable.
    """
    return digest_of_document(
        _seam_document(
            contract_document,
            _strip_excluded(emit),
            columns,
            None if extension is None else _strip_excluded(extension),
        ),
        subject=subject,
    )


@dataclass(frozen=True)
class SeamDigests:
    """``H`` and ``G`` for one emit, and the entropy ``H`` contributes."""

    h: DigestRef
    g: DigestRef

    @property
    def entropy_bytes(self) -> bytes:
        """``H``'s 32 raw bytes -- KERNEL-SPEC §3's ``H_bytes`` for one emit."""
        return digest_bytes(self.h.value)


def seam_digests(
    contract_document: Mapping[str, Any],
    emit: Mapping[str, Any],
    columns: Sequence[str],
    extension: Optional[Mapping[str, Any]] = None,
) -> SeamDigests:
    """Compute ``H`` and ``G`` for one emit in one pass."""
    return SeamDigests(
        h=seam_digest(contract_document, emit, columns, extension=extension),
        g=seam_reusable_digest(contract_document, emit, columns, extension=extension),
    )


def seam_entropy_bytes(digests: Sequence[SeamDigests]) -> bytes:
    """KERNEL-SPEC §3's ``H_bytes`` for a Simulation's whole ``emits[]``.

    "When a Simulation has several ``emits[]``, ``H_bytes`` is the sha256
    of the concatenation of the per-emit digests in ``emits[]`` order."
    The single-emit case is the digest's own bytes; both cases are
    :func:`flux_engine.core.seedtree.seam_bytes`, so the seam and the seed
    tree cannot disagree about the rule.
    """
    return seam_bytes([entry.h.value for entry in digests])


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmitResult:
    """What one closed emit stream is answerable for."""

    expose_id: str
    header: Mapping[str, Any]
    rows: int
    output_digest: DigestRef


@dataclass
class EmitWriter:
    """Renders, frames and digests one emit stream, then hands bytes to a sink.

    The order of operations is the point. A row is rendered against the
    compiled expose (KERNEL-SPEC §9), framed as ``JCS(array) + b"\\n"``
    (§8), folded into the running digest, and only then offered to the
    sink. A sink that drops every row, buffers them, or writes them to a
    socket that never connects changes nothing about the digest the run
    reports -- which is what makes ``NullSink`` a legitimate way to compute
    an ``outputDigest`` without producing a file.

    The writer holds the framed lines so the stream can be re-digested and
    the rows re-read after close; a caller that only wants the digest can
    pass ``retain_lines=False``.
    """

    expose: CompiledExpose
    contract_digest: DigestRef
    sink: Optional[EmitSink] = None
    retain_lines: bool = True

    _header: Optional[Dict[str, Any]] = field(default=None, init=False, repr=False)
    _lines: List[bytes] = field(default_factory=list, init=False, repr=False)
    _digest_material: List[bytes] = field(default_factory=list, init=False, repr=False)
    _rows: int = field(default=0, init=False)
    _opened: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._header = emit_header_document(self.expose, self.contract_digest)

    # -- lifecycle ----------------------------------------------------------

    @property
    def header(self) -> Mapping[str, Any]:
        """§8's header document for this stream."""
        assert self._header is not None  # set in __post_init__
        return self._header

    @property
    def rows(self) -> int:
        """How many rows have been written."""
        return self._rows

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has run."""
        return self._closed

    def open(self) -> "EmitWriter":
        """Open the sink with this stream's header. Idempotent it is not."""
        if self._opened:
            raise FluxError(FLX_3301, f"expose {self.expose.expose_id!r}: already open")
        self._opened = True
        if self.sink is not None:
            self.sink.open(self._sink_header())
        return self

    def _sink_header(self) -> EmitHeader:
        return EmitHeader(
            expose_id=self.expose.expose_id,
            columns=self.expose.column_names,
            types=self.expose.type_names,
            contract_digest=self.contract_digest,
        )

    def write(self, row: Row) -> RenderedRow:
        """Render, frame, digest and forward one row. Returns what was written."""
        if self._closed:
            raise FluxError(FLX_3301, f"expose {self.expose.expose_id!r}: stream is closed")
        if not self._opened:
            raise FluxError(FLX_3301, f"expose {self.expose.expose_id!r}: write before open")
        rendered = render_row(row, self.expose, row_index=self._rows)
        line = row_line(rendered)
        self._digest_material.append(line)
        if self.retain_lines:
            self._lines.append(line)
        self._rows += 1
        if self.sink is not None:
            self.sink.write(rendered)
        return rendered

    def write_all(self, rows: Iterable[Row]) -> int:
        """Write every row of *rows* in order; returns how many were written."""
        for row in rows:
            self.write(row)
        return self._rows

    def close(self) -> EmitResult:
        """Close the sink and return the stream's :class:`EmitResult`.

        Safe to call twice: the second call returns the same result rather
        than closing a sink the run already released. The sink is closed
        even when the digest computation is the last thing that happens, so
        a caller using ``try/finally`` never leaks a handle.
        """
        if not self._opened:
            raise FluxError(FLX_3301, f"expose {self.expose.expose_id!r}: close before open")
        if not self._closed:
            self._closed = True
            if self.sink is not None:
                self.sink.close()
        return self.result()

    # -- what the stream is answerable for ----------------------------------

    def result(self) -> EmitResult:
        """The stream's :class:`EmitResult` as of now."""
        return EmitResult(
            expose_id=self.expose.expose_id,
            header=self.header,
            rows=self._rows,
            output_digest=self.output_digest(),
        )

    def output_digest(self) -> DigestRef:
        """§8's ``outputDigest`` over the rows written so far."""
        return output_digest(
            self.header,
            self._digest_material,
            subject=f"outputDigest:{self.expose.expose_id}",
        )

    def lines(self) -> Tuple[bytes, ...]:
        """The framed NDJSON lines, when the writer was asked to retain them."""
        if not self.retain_lines:
            raise FluxError(
                FLX_3301,
                f"expose {self.expose.expose_id!r}: this writer was built with "
                "retain_lines=False, so the framed lines were not kept",
            )
        return tuple(self._lines)

    def stream_bytes(self) -> bytes:
        """The concatenated row block -- the NDJSON payload without the header."""
        return b"".join(self.lines())

    def __enter__(self) -> "EmitWriter":
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        # §8's sink contract: close() runs on the failure path too.
        if self._opened and not self._closed:
            self.close()
