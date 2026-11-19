# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``column-provenance.json``, and the section of ``EVIDENCE.md`` that reads it.

An evidence pack says how each column of each exposed table was produced
(declared, profile, copied, derived or default) and from what. ``apply``
writes the record next to the run it describes; ``ship`` reads it back for
the report and packs it with the rest of the run directory, where the
archive's manifest covers it like every other file.

**The shape is OpenLineage's.** Each exposed table gets a
``ColumnLineageDatasetFacet`` (1-2-0): ``fields.<column>.inputFields[]``,
each with ``namespace``, ``name``, ``field`` and a ``transformations[]``
list. A copied column's input is the runtime attribute it reads, named the
way the job is: namespace ``flux://<simulationId>``, the attribute's scope
(``subject``, ``event``, ``moment``) as the name and the rest of the path as
the field. Every other column has an empty ``inputFields``: it has no
upstream field, which is the truthful answer for a column drawn from a
declaration or derived from an identifier rule. The facet is the part a
lineage consumer reads; ``columns`` beside it is the part FLUX adds, one
row per column with its origin, its source and who decided it, because a
column lineage graph has no word for "declared" or "fell back to a default".
(OpenLineage, Apache-2.0; the spec and its Python client are not imported
here, and ``tests/lineage`` checks the facet against the client's own
classes when the ``lineage`` extra is installed.)

The record is a function of the run's declarations and nothing else: no
clock, no host, no row. The same bundle and seed give the same bytes at any
``--jobs``.

**The plan holds its digest.** ``plan`` builds the record from its own
execution and writes the digest of it into ``plan.json`` as
``columnProvenanceDigest``, the way it writes ``seed-paths.ndjson``'s as
``seedTreeDigest`` (:func:`provenance_digest`). ``apply`` writes the file, and
``ship`` and ``verify`` read it back only through the plan: a record that is
missing, or does not hash to that digest, is ``FLX-2115``. The plan's own digest
names the run, so the record is covered by ``planDigest`` and so by the run
digest the in-toto statement names. The digest is of the file's bytes exactly as
they are on disk, KERNEL-SPEC §2.3's ``raw-bytes`` domain, as ``seedTreeDigest``
is of ``seed-paths.ndjson``: any change to the file moves it, reformatting and
a duplicated key included, and no parser is involved in deciding whether the file
is the one the plan vouches for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine._vocabulary import find_banned_words
from flux_engine.api.lineage import DatasetRef
from flux_engine.core.digest import DigestRef, digest_of_bytes
from flux_engine.lineage.emitter import column_input_field, column_lineage_facet, job_namespace
from flux_engine.seam.coverage import ColumnCoverage, ExposeCoverage, Origin, printable

__all__ = [
    "PROVENANCE_DIGEST_KEY",
    "PROVENANCE_FORMAT",
    "BoundProvenance",
    "build_provenance_document",
    "parse_provenance",
    "provenance_digest",
    "quoted_name",
    "render_provenance_section",
    "render_uncovered_columns",
]

#: The ``format`` member of ``column-provenance.json``. A change to the shape
#: of a row or of the facet is a new value here.
PROVENANCE_FORMAT = "flux-column-provenance/1"

#: The key under which ``plan.json`` records the digest of the record.
PROVENANCE_DIGEST_KEY = "columnProvenanceDigest"


@dataclass(frozen=True)
class BoundProvenance:
    """A record the plan vouches for: what it says, and the exact bytes that hashed to the plan's digest.

    ``data`` is carried so that what is packed is what was checked. A caller
    that read the file, checked it and then let a pack open it again by name
    would pack whatever the name pointed at by then.
    """

    coverages: Tuple[ExposeCoverage, ...]
    data: bytes


def _lineage_inputs(row: ColumnCoverage, namespace: str) -> List[Dict[str, Any]]:
    """The input fields of one column: its source attribute when it was copied, else none."""
    if row.origin is not Origin.COPIED or row.source is None:
        return []
    scope, _, rest = row.source.partition(".")
    if not rest:  # a source with no scope prefix: name the whole path as the field
        scope, rest = "world", row.source
    return [column_input_field(namespace=namespace, name=scope, field=rest, description=row.detail)]


def build_provenance_document(
    *, simulation_id: str, exposes: Sequence[Tuple[ExposeCoverage, DatasetRef]]
) -> Dict[str, Any]:
    """The record ``apply`` writes: one entry per expose, in the run's emit order.

    *exposes* pairs each expose's coverage with the dataset it is written
    to, so an entry can be set straight into an OpenLineage output
    dataset's ``facets``.
    """
    namespace = job_namespace(simulation_id)
    entries = []
    for coverage, dataset in exposes:
        document = coverage.as_document()
        entries.append(
            {
                **document,
                "dataset": {"namespace": dataset.namespace, "name": dataset.name},
                "counts": coverage.counts(),
                "facets": {
                    "columnLineage": column_lineage_facet(
                        [(row.column, _lineage_inputs(row, namespace)) for row in coverage.columns]
                    )
                },
            }
        )
    return {"format": PROVENANCE_FORMAT, "simulationId": simulation_id, "exposes": entries}


def provenance_digest(data: bytes) -> DigestRef:
    """The ``raw-bytes`` digest of a record, from its bytes exactly as on disk.

    No parsing, so a damaged, hostile or merely reformatted file cannot fail
    here: it digests to something else, and the plan's digest does not match.
    """
    return digest_of_bytes(data, subject=PROVENANCE_DIGEST_KEY)


def parse_provenance(text: str) -> Tuple[ExposeCoverage, ...]:
    """Read a record back as one :class:`ExposeCoverage` per expose.

    Raises ``ValueError`` when *text* is not a record this module wrote: not
    JSON, a different ``format``, or a row
    :meth:`~flux_engine.seam.coverage.ColumnCoverage.from_document` refuses.
    Nothing is repaired or guessed.
    """
    document = json.loads(text)
    if not isinstance(document, Mapping) or document.get("format") != PROVENANCE_FORMAT:
        raise ValueError(f"not a {PROVENANCE_FORMAT} record")
    exposes = document.get("exposes")
    if not isinstance(exposes, Sequence) or isinstance(exposes, (str, bytes)):
        raise ValueError("the record's exposes are a list")
    return tuple(ExposeCoverage.from_document(entry) for entry in exposes)


def quoted_name(name: str, fallback: str) -> str:
    """*name* in backticks, or *fallback* when printing it would harm the report.

    A column or an attribute is named by the bundle's author, not by this
    engine, and a FLUID column name is any non-empty string. Three things in
    one would damage the report, so each is withheld:

    * a reserved word. The whole rendered report is refused (``FLX-8030``)
      when it holds one, so a section that listed every column would stop a
      bundle from shipping because one of its columns has such a word in its
      name, which no earlier report did;
    * a character that is not plain text, a newline most of all, which would
      let a name start a heading of its own and forge a section;
    * a backtick, which would end the quoting early.

    The structured record (``column-provenance.json``) keeps the real name;
    only the prose withholds it, and says that it did.
    """
    if printable(name) != name or "`" in name or find_banned_words(name):
        return fallback
    return f"`{name}`"


def render_uncovered_columns(coverages: Optional[Sequence[ExposeCoverage]]) -> str:
    """The sentence about columns that fell back to a type default.

    Three answers, because they are three different facts: the run's plan binds
    no record of how its columns were produced (``None``), none fell back, or
    these did. Never left out: a reader who finds no column named has to be able to
    tell "none" from "nobody looked". Exposes are listed in the order given, which
    is the run's emit order and the order of the section above it, and columns in
    their declared order.
    """
    if coverages is None:
        return (
            "Column provenance is not bound to this run, so this report says nothing "
            "about whether a column fell back to a type default."
        )
    listed = "; ".join(
        f"`{coverage.expose_id}`: "
        + ", ".join(
            quoted_name(row.column, "a column whose name this report does not print")
            for row in coverage.defaulted
        )
        for coverage in coverages
        if coverage.defaulted
    )
    if not listed:
        return (
            "No column fell back to a type default: every column was produced by a "
            "declaration, a copy or a derivation."
        )
    return (
        "These columns fell back to a type default, so no declaration or source "
        f"describes their values: {listed}."
    )


_UNPRINTED_SOURCE = "an attribute whose name this report does not print"

#: Said once, above the per-expose lists. It says what the reader can rely on and
#: no more: the plan holds a digest of the record and `ship` checked the file
#: against it, which shows the file is the one the plan's execution produced, and
#: not that the engine classified each column rightly.
_PROVENANCE_NOTE = (
    "This is what `apply` recorded for the run. The run's plan holds a digest of it, and "
    "`ship` checked the record against that digest; it does not work out again how each "
    "column was produced."
)


def _describe(row: ColumnCoverage) -> str:
    """One column, in a sentence a reader of ``EVIDENCE.md`` can act on."""
    source = quoted_name(row.source or "", _UNPRINTED_SOURCE)
    if row.origin is Origin.COPIED:
        return f"copied from {source} ({row.detail})"
    if row.origin is Origin.DECLARED:
        return f"declared in {source}"
    return f"{row.origin.value}: {row.detail}"


def render_provenance_section(coverages: Optional[Sequence[ExposeCoverage]]) -> str:
    """The body of ``EVIDENCE.md``'s "Column Provenance" section.

    ``None`` means the run's plan binds no record, and the section says so
    rather than saying nothing: a reader who finds no column listed has to be
    able to tell "all covered" from "nothing to say".
    """
    if coverages is None:
        return (
            "No column provenance is bound to this run: its plan holds no digest of a "
            "`column-provenance.json` (it was written before the plan carried one), so this "
            "report does not say how any column was produced. Plan and apply the bundle again "
            "for a run that binds one."
        )
    if not coverages:
        return "This run declared no exposes."
    blocks = [_PROVENANCE_NOTE]
    for coverage in coverages:
        counts = coverage.counts()
        shown = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
        total = len(coverage.columns)
        header = (
            f"### `{coverage.expose_id}`\n\n"
            f"{total} column{'s' if total != 1 else ''}: {shown or 'none'}."
        )
        bullets = "\n".join(
            f"- {quoted_name(row.column, f'column {position}')} ({row.type_name}): {_describe(row)}"
            for position, row in enumerate(coverage.columns, start=1)
        )
        blocks.append(f"{header}\n\n{bullets}" if bullets else header)
    return "\n\n".join(blocks)
