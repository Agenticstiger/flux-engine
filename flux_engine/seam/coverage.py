# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How each column of an expose was produced, and the refusal strictness adds.

Every column of an exposed table has a producer. This module names the
producers once, so that a plan's report, a strict refusal and an evidence
pack all say the same thing about the same column.

**Five origins, a closed set.**

* ``declared`` -- the author's own declaration decides the draw: the
  column's entry under the emit's ``fidelity.columns`` (a range, a support,
  levels, a rate, bins). A statistic the author wrote down.
* ``profile`` -- a statistic profile decides it. Nothing in the engine
  produces this origin yet. It is in the set so that a pack written today
  and one written when profiles land read one vocabulary.
* ``copied`` -- the value is a runtime attribute of the simulated world,
  passed through as it is: ``subject.id``, ``event.data.amount``
  (:mod:`flux_engine.seam.mapping`). The author, the vertical or the
  column's own name picked the attribute; :attr:`ColumnCoverage.decided_by`
  says which.
* ``derived`` -- computed from other inputs and never drawn. An identifier
  column is a function of the seam digest and the row index
  (SEAM-FIDELITY-SPEC §1). So is a key the contract declares, laid out from
  the row index so that no value repeats, and a foreign key that holds its
  parent's key at the parent row its relationship picks
  (:mod:`flux_engine.generator.relations`). A foreign key whose own
  declaration picks the parent (it draws as declared, then holds the
  parent's nearest key) is ``declared``.
* ``default`` -- the column declares nothing the engine could produce it
  from, and the type's own default fills it: ``null`` for an optional
  column no mapping step resolved, the simulation's time bounds for a
  DATE or TIMESTAMP column with no declaration of its own.

Only ``default`` is a gap. The other four each trace to something an author
or the engine's own rules decided, and a ``default`` column is one nobody
decided: it is there because the contract lists it. Coverage is *reported*
always (the plan says so, the receipt-side artifact records it, the evidence
pack lists it) and *refused* under strictness (:func:`require_full_coverage`,
``FLX-3304``), so a team that wants the gap to stop a run can say so, and a
team that is still writing declarations is not stopped by it.

**Where the shape comes from.** The refusal is a per-column table, the
shape dbt gives a failed enforced model contract (dbt-core,
``core/dbt/exceptions.py``, ``ContractError``: one row per column, with the
column, what was found and why it does not match, sorted by column).
dbt-core is Apache-2.0; only the idea is taken. The per-column record in
the evidence pack follows OpenLineage's ``ColumnLineageDatasetFacet`` 1-2-0
(see :mod:`flux_engine.evidence.provenance`).

This module reads no run and no declaration: it holds the vocabulary, the
rows and the refusal. What decides a column's origin is the module that
knows how the column was produced
(:meth:`flux_engine.seam.mapping.ColumnMapping.coverage`,
:func:`flux_engine.generator.coverage.coverage_of_plan`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError, register

__all__ = [
    "FLX_3304",
    "SOURCE_IDENTIFIER",
    "SOURCE_KEY",
    "SOURCE_FLUX_RULE_PREFIX",
    "SOURCE_RELATIONSHIP_PREFIX",
    "SOURCE_RULE_PREFIX",
    "SOURCE_TIME_BOUNDS",
    "STRICT_BY_DECLARATION",
    "STRICT_BY_FLAG",
    "ColumnCoverage",
    "ExposeCoverage",
    "Origin",
    "printable",
    "require_full_coverage",
    "strictness_asked_by",
]

FLX_3304 = register(
    "FLX-3304",
    "a column fell back to a type default and coverage is strict",
    "Every column of an exposed table is produced by a declaration, a profile, a "
    "copy of a runtime attribute or a derivation. A column none of those reaches "
    "falls back to its type's default: null for an optional column of a "
    "scenario-driven emit that no mapping step resolved, the simulation's time "
    "bounds for a DATE or TIMESTAMP column of a generator-driven emit that "
    "declares no draw of its own. That is reported on every plan; under "
    "--strict-coverage, or when the Simulation declares strictCoverage: true under "
    "its com.agentics.flux/1 extensions, it is refused, before any row exists, "
    "with one table row per column. Cover a column of a scenario-driven emit by mapping it with the "
    "emit's mapping block, or by giving the run an attribute of its own name; "
    "declaring it under fidelity.columns would make the whole emit "
    "generator-driven. Cover a DATE or TIMESTAMP column of a generator-driven "
    "emit by declaring a range, a rate, or bins with binWeights for it under the "
    "emit's fidelity.columns. Or drop --strict-coverage, and strictCoverage from "
    "the Simulation if it declares it, to accept the default.",
)

#: What can ask for strictness. The refusal names each one that did, because the
#: way out differs: a flag is dropped from the command, a declaration is removed
#: from the bundle, and the flag cannot turn a declaration off.
STRICT_BY_FLAG = "--strict-coverage"
STRICT_BY_DECLARATION = "the strictCoverage declaration of the Simulation"


def strictness_asked_by(*, flag: bool, declared: bool) -> Tuple[str, ...]:
    """Who asked for strict coverage, in the order the refusal names them; empty when nobody did."""
    return tuple(
        source
        for source, asked in ((STRICT_BY_FLAG, flag), (STRICT_BY_DECLARATION, declared))
        if asked
    )


#: ``ColumnCoverage.source`` of an identifier column: it is a function of the
#: seam digest ``H`` and the row index, and of nothing declared per column.
SOURCE_IDENTIFIER = "H+rowIndex"

#: ``ColumnCoverage.source`` of a DATE or TIMESTAMP column that fell back to
#: the simulation's own ``spec.timeBounds``.
SOURCE_TIME_BOUNDS = "spec.timeBounds"

#: ``ColumnCoverage.source`` of a key column: the keys the contract declares
#: in its exposes' ``semantics``, laid out from the row index.
SOURCE_KEY = "semantics+rowIndex"

#: ``ColumnCoverage.source`` of a column a contract rule determines is this prefix
#: followed by the ids of the rules, joined by commas.
SOURCE_RULE_PREFIX = "contract.invariants."

#: ``ColumnCoverage.source`` of a column a FLUX-side rule determines (EXPR-SPEC-2 §1):
#: this prefix followed by the ids of the rules, joined by commas.
SOURCE_FLUX_RULE_PREFIX = "bundle.rules."

#: ``ColumnCoverage.source`` of a foreign key column is this prefix followed
#: by the name of the relationship that sets it.
SOURCE_RELATIONSHIP_PREFIX = "semantics.relationships."


class Origin(Enum):
    """How a column was produced. The set is closed; see the module docstring."""

    DECLARED = "declared"
    PROFILE = "profile"
    COPIED = "copied"
    DERIVED = "derived"
    DEFAULT = "default"


#: Every way a column can be produced, by origin and by who decided it, and the
#: one sentence a report says of it. The sentence is a function of the pair and
#: nothing else: a record read back from a file never supplies its own, so a
#: damaged or edited file cannot put its own words into a report
#: (:meth:`ColumnCoverage.from_document`). ``profile`` has no entry because
#: nothing produces it yet; a record that names it is refused.
_PRODUCERS: Mapping[Tuple[Origin, str], str] = {
    (Origin.COPIED, "override"): "named by the emit's mapping block",
    (Origin.COPIED, "alias"): "named by the vertical's alias table",
    (Origin.COPIED, "name"): "an attribute with the column's own name",
    (Origin.DECLARED, "fidelity"): "declared under fidelity.columns",
    (Origin.DECLARED, "relationship"): (
        "declared under fidelity.columns, then held at the nearest key of the parent "
        "its relationship names"
    ),
    (Origin.DERIVED, "identifier"): (
        "an identifier derived from the seam digest and the row index, never drawn"
    ),
    (Origin.DERIVED, "key"): (
        "a key the contract declares, laid out from the row index so that no value "
        "repeats, never drawn"
    ),
    (Origin.DERIVED, "relationship"): (
        "the parent's key at the parent row the relationship picks, so it always names "
        "a row the parent holds"
    ),
    (Origin.DERIVED, "rule"): (
        "set by the rule that determines it (a contract invariant or a FLUX-side rule), on "
        "the rows the rule covers; any other row keeps the value the column's own "
        "declaration gives it"
    ),
    (Origin.DERIVED, "dependency"): (
        "set to the value its key carries (a dependency's `from` values), drawn once for the "
        "key, on the rows the dependency covers, where a band with an upper bound puts the "
        "column's declared NULLs on rows of their own; any other row keeps the value the "
        "column's own declaration gives it"
    ),
    (Origin.DEFAULT, "time-bounds"): (
        "uniform within the simulation's time bounds: the column declares no draw of its own"
    ),
    (Origin.DEFAULT, "none"): "null: no override, alias or attribute of the column's name",
}


#: What to do about a column that fell back, by who decided the default: the
#: two kinds of emit are covered in different places, and only one of them is
#: covered under ``fidelity.columns``.
_HOW_TO_COVER: Mapping[str, str] = {
    "none": "map it with the emit's mapping block, or give the run an attribute of its own name",
    "time-bounds": (
        "declare a range, a rate, or bins with binWeights for it under the emit's fidelity.columns"
    ),
}


@dataclass(frozen=True)
class ColumnCoverage:
    """One column, how it was produced, and from what.

    ``source`` is a locator a reader can follow and never a value: the
    runtime attribute a copied column reads (``subject.id``), the
    declaration a declared column reads (``fidelity.columns.<column>``,
    relative to the emit's extension block), :data:`SOURCE_IDENTIFIER` or
    :data:`SOURCE_TIME_BOUNDS`. It is ``None`` for a default with nothing to
    point at (an optional column rendered ``null``).

    ``decided_by`` says who chose the producer: ``override`` (the emit's own
    ``mapping`` block), ``alias`` (the vertical's table), ``name`` (the
    column's own name matched an attribute), ``fidelity`` (the column's
    declaration), ``identifier`` (the type's rule), ``time-bounds`` and
    ``none`` (a default nothing decided), ``key`` (a key the contract
    declares), ``relationship`` (the contract's relationship that sets a
    foreign key) and ``rule`` (a contract rule that determines the column).

    The pair ``(origin, decided_by)`` must be one of the eleven the engine
    produces, and ``source`` must be what that pair implies, or construction
    raises ``ValueError``: a row is either something this module could have
    written or it does not exist. :attr:`detail` is the one plain sentence a
    report says of the pair. It carries no value from any row
    (KERNEL-SPEC §9) and is never stored on the row, so it cannot differ
    from the table above.
    """

    column: str
    type_name: str
    required: bool
    origin: Origin
    source: Optional[str]
    decided_by: str

    def __post_init__(self) -> None:
        if (self.origin, self.decided_by) not in _PRODUCERS:
            raise ValueError(
                f"no column is produced as {self.origin.value!r} decided by {self.decided_by!r}"
            )
        if not self._source_fits():
            raise ValueError(
                f"a {self.origin.value} column decided by {self.decided_by!r} "
                f"cannot have the source {self.source!r}"
            )

    def _source_fits(self) -> bool:
        """Whether ``source`` is what ``(origin, decided_by)`` implies."""
        if self.origin is Origin.DERIVED:
            if self.decided_by == "key":
                return self.source == SOURCE_KEY
            if self.decided_by in ("rule", "dependency"):
                return isinstance(self.source, str) and any(
                    self.source.startswith(prefix) and len(self.source) > len(prefix)
                    for prefix in (SOURCE_RULE_PREFIX, SOURCE_FLUX_RULE_PREFIX)
                )
            if self.decided_by == "relationship":
                return (
                    isinstance(self.source, str)
                    and self.source.startswith(SOURCE_RELATIONSHIP_PREFIX)
                    and len(self.source) > len(SOURCE_RELATIONSHIP_PREFIX)
                )
            return self.source == SOURCE_IDENTIFIER
        if self.origin is Origin.DECLARED:
            return self.source == f"fidelity.columns.{self.column}"
        if self.origin is Origin.DEFAULT:
            return self.source == (SOURCE_TIME_BOUNDS if self.decided_by == "time-bounds" else None)
        return isinstance(self.source, str) and bool(self.source)

    @property
    def detail(self) -> str:
        """What a report says of this column's producer: fixed text per pair."""
        return _PRODUCERS[(self.origin, self.decided_by)]

    def as_document(self) -> Dict[str, Any]:
        """The row as canonical-ready JSON."""
        return {
            "column": self.column,
            "type": self.type_name,
            "required": self.required,
            "origin": self.origin.value,
            "source": self.source,
            "decidedBy": self.decided_by,
            "detail": self.detail,
        }

    @classmethod
    def from_document(cls, document: Any) -> "ColumnCoverage":
        """Read a row back; ``ValueError`` when *document* is not a row this module wrote.

        Nothing is guessed and nothing is taken on trust. A row with a
        missing key, a wrong class, an origin outside :class:`Origin` or a
        pair and source this module never writes is refused, so a reader
        never reports a column as covered because the file it read was
        damaged. The row's own ``detail`` is not read: the sentence a report
        says comes from the pair.
        """
        if not isinstance(document, Mapping):
            raise ValueError("a column row is a mapping")
        try:
            origin = Origin(document["origin"])
            column, type_name = document["column"], document["type"]
            required, source = document["required"], document["source"]
            decided_by = document["decidedBy"]
        except (KeyError, ValueError) as error:
            raise ValueError(f"a column row is malformed: {error!r}") from error
        if not (
            isinstance(column, str)
            and isinstance(type_name, str)
            and isinstance(required, bool)
            and (source is None or isinstance(source, str))
            and isinstance(decided_by, str)
        ):
            raise ValueError("a column row carries a value of the wrong class")
        return cls(
            column=column,
            type_name=type_name,
            required=required,
            origin=origin,
            source=source,
            decided_by=decided_by,
        )


@dataclass(frozen=True)
class ExposeCoverage:
    """Every column of one expose, in the declared column order."""

    expose_id: str
    columns: Tuple[ColumnCoverage, ...]

    @property
    def defaulted(self) -> Tuple[ColumnCoverage, ...]:
        """The columns that fell back to a type default, in declared order."""
        return tuple(row for row in self.columns if row.origin is Origin.DEFAULT)

    def counts(self) -> Dict[str, int]:
        """How many columns each origin produced, every origin present, in set order."""
        return {
            origin.value: sum(1 for row in self.columns if row.origin is origin)
            for origin in Origin
        }

    def as_document(self) -> Dict[str, Any]:
        """The expose as canonical-ready JSON."""
        return {
            "exposeId": self.expose_id,
            "columns": [row.as_document() for row in self.columns],
        }

    @classmethod
    def from_document(cls, document: Any) -> "ExposeCoverage":
        """Read an expose back; ``ValueError`` when it is not one this module wrote."""
        if not isinstance(document, Mapping):
            raise ValueError("an expose entry is a mapping")
        expose_id, columns = document.get("exposeId"), document.get("columns")
        if not isinstance(expose_id, str) or not isinstance(columns, Sequence):
            raise ValueError("an expose entry names its exposeId and lists its columns")
        if isinstance(columns, (str, bytes)):
            raise ValueError("an expose entry lists its columns")
        return cls(
            expose_id=expose_id,
            columns=tuple(ColumnCoverage.from_document(row) for row in columns),
        )


def printable(name: str) -> str:
    """*name* as it is written into a diagnostic: quoted when it is not plain text.

    A FLUID column name is any non-empty string, so one can carry a newline or
    a terminal escape; a diagnostic that printed it as it is would let a
    contract rewrite the lines around it.
    """
    return name if name.isprintable() else repr(name)


def _table(headings: Tuple[str, ...], rows: Sequence[Tuple[str, ...]]) -> List[str]:
    """Left-aligned text table, two spaces between columns, no trailing blanks."""
    widths = [
        max(len(headings[index]), *(len(row[index]) for row in rows))
        for index in range(len(headings))
    ]

    def line(cells: Tuple[str, ...]) -> str:
        return "  ".join(
            cell.ljust(width) for cell, width in zip(cells, widths, strict=True)
        ).rstrip()

    return [line(headings), *(line(row) for row in rows)]


def require_full_coverage(
    coverages: Iterable[ExposeCoverage], *, asked_by: Sequence[str] = (STRICT_BY_FLAG,)
) -> None:
    """Raise ``FLX-3304`` naming every column that fell back to a type default.

    One refusal for the whole run, not one per expose: a bundle with three
    exposes and a gap in each is told so once, with a table per expose, the
    way dbt's contract error names every column it finds wrong rather than
    the first. Nothing is raised when no column defaulted. *asked_by* names
    what made the run strict (:func:`strictness_asked_by`), so the last line
    says what to take away to accept the default.
    """
    refused = [coverage for coverage in coverages if coverage.defaulted]
    if not refused:
        return
    total = sum(len(coverage.defaulted) for coverage in refused)
    lines = [f"{total} column{'s' if total != 1 else ''} fell back to a type default"]
    for coverage in refused:
        lines.append(
            f"expose {coverage.expose_id!r} "
            f"({len(coverage.defaulted)} of {len(coverage.columns)} columns):"
        )
        table = _table(
            ("column", "type", "required", "fell back to"),
            [
                (
                    printable(row.column),
                    row.type_name,
                    "yes" if row.required else "no",
                    row.detail,
                )
                for row in coverage.defaulted
            ],
        )
        lines.extend(f"  {line}" for line in table)
        for kind in dict.fromkeys(row.decided_by for row in coverage.defaulted):
            lines.append(f"  to cover a column of this emit, {_HOW_TO_COVER[kind]}")
    lines.append(f"without {' and '.join(asked_by)} this is a report and the run goes on")
    raise FluxError(FLX_3304, "\n".join(lines))
