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
* ``profile`` -- the pinned profile decides it. A column of a reference expose
  takes the value the profile publishes for its row in a table released whole
  (``references``, ADR 0023), decided by ``reference``.
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
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from flux_engine.core.errors import FluxError, register

__all__ = [
    "FLX_3304",
    "SOURCE_HISTORY",
    "SOURCE_HISTORY_TRACKED_PREFIX",
    "SOURCE_EVENT_LAG",
    "SOURCE_IDENTIFIER",
    "SOURCE_KEY",
    "SOURCE_PERIOD_KINDS",
    "SOURCE_PLUGIN",
    "SOURCE_PROJECTION_PREFIX",
    "SOURCE_REFERENCE_PREFIX",
    "SOURCE_FLUX_RULE_PREFIX",
    "SOURCE_LIFETIME_PREFIX",
    "SOURCE_RELATIONSHIP_PREFIX",
    "SOURCE_RULE_PREFIX",
    "SOURCE_SHARED_ATTRIBUTE_PREFIX",
    "SOURCE_WITHIN_PREFIX",
    "SOURCE_TIME_BOUNDS",
    "SOURCE_VERSION_PREFIX",
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

#: ``ColumnCoverage.source`` of a validity or activation column of a history's emit: the
#: ``history`` key of the Simulation's namespace, whose entry for the emit draws the chain.
SOURCE_HISTORY = "history"

#: ``ColumnCoverage.source`` of a tracked column of a history's emit is this prefix followed by
#: the column: the entry's ``tracked`` list holds its levels and transitions.
SOURCE_HISTORY_TRACKED_PREFIX = "history.tracked."

#: ``ColumnCoverage.source`` of a column a projected emit takes from the history it projects is
#: this prefix followed by the history's emit and the column it reads.
SOURCE_VERSION_PREFIX = "versions."

#: ``ColumnCoverage.source`` of a column a projection works out from its history is this prefix
#: followed by the projection's key (``events`` or ``periods``).
SOURCE_PROJECTION_PREFIX = "projections."

#: ``ColumnCoverage.source`` of an event's recorded date when its entry declares a ``lag``.
SOURCE_EVENT_LAG = "events.lag"

#: ``ColumnCoverage.source`` of a column a periods entry's kinds override.
SOURCE_PERIOD_KINDS = "periods.kinds"

#: ``ColumnCoverage.source`` of a column a generator plugin generates: the ``generators`` key of
#: the Simulation's extensions, which declares the plugin (ADR 0032).
SOURCE_PLUGIN = "generators"

#: ``ColumnCoverage.source`` of a column of a reference expose is this prefix followed by its
#: ``exposeId`` and the column, as the pinned profile publishes them (``references``, ADR 0023).
SOURCE_REFERENCE_PREFIX = "references."

#: ``ColumnCoverage.source`` of a column a contract rule determines is this prefix
#: followed by the ids of the rules, joined by commas.
SOURCE_RULE_PREFIX = "contract.invariants."

#: ``ColumnCoverage.source`` of a column a FLUX-side rule determines (EXPR-SPEC-2 §1):
#: this prefix followed by the ids of the rules, joined by commas.
SOURCE_FLUX_RULE_PREFIX = "bundle.rules."

#: ``ColumnCoverage.source`` of an end column that a lifetime's ``duration`` draws (ADR 0024
#: section 4): this prefix followed by the column.
SOURCE_LIFETIME_PREFIX = "bundle.lifetimes."

#: ``ColumnCoverage.source`` of a date that ``within`` entries draw (ADR 0024 section 5): this
#: prefix followed by the column.
SOURCE_WITHIN_PREFIX = "bundle.within."

#: ``ColumnCoverage.source`` of a foreign key column is this prefix followed
#: by the name of the relationship that sets it.
SOURCE_RELATIONSHIP_PREFIX = "semantics.relationships."

#: ``ColumnCoverage.source`` of a column copied from a ``sharedAttribute`` (EXPR-SPEC-2 section 9):
#: this prefix followed by the attribute's name. A copy read from a relationship's ``sharedColumns``
#: is named by the relationship and the column, ``semantics.relationships.<relationship>.sharedColumns.<column>``.
SOURCE_SHARED_ATTRIBUTE_PREFIX = "contract.sharedAttribute."


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
#: (:meth:`ColumnCoverage.from_document`).
_PRODUCERS: Mapping[Tuple[Origin, str], str] = {
    (Origin.COPIED, "override"): "named by the emit's mapping block",
    (Origin.COPIED, "alias"): "named by the vertical's alias table",
    (Origin.COPIED, "name"): "an attribute with the column's own name",
    (Origin.COPIED, "rule"): (
        "copied by the rule that determines it from a column of the row a link reaches, as that "
        "row's own pass writes it, on the rows the rule covers; a row whose link reaches no row "
        "takes NULL, and a column that declares a nullFraction keeps its own NULLs"
    ),
    (Origin.DECLARED, "fidelity"): "declared under fidelity.columns",
    (Origin.DECLARED, "plugin"): (
        "generated by the generator plugin the Simulation declares for the expose, then held by "
        "the engine to the column's type, nullability, range, length and levels and to the "
        "contract's checks"
    ),
    (Origin.PROFILE, "reference"): (
        "the value the pinned profile publishes for the row in its reference table, a table "
        "released whole as reference data"
    ),
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
    (Origin.DERIVED, "entity"): (
        "the key of an entity of a history, which the contract declares, laid out from the "
        "entity index so that no value repeats across entities, and repeated on every version "
        "of its entity; never drawn"
    ),
    (Origin.DERIVED, "projection"): (
        "worked out from the history the emit projects, never drawn: an event's op, instant "
        "and reason, a period's first day, and the recorded date of an event whose entry "
        "declares no lag, which is its instant"
    ),
    (Origin.DECLARED, "lag"): (
        "the change's instant plus a lag drawn from the lag the events entry declares"
    ),
    (Origin.DECLARED, "history"): (
        "drawn by the history chain from the emit's history entry: the start of each version, "
        "its length and so its end, which is NULL, or the entry's open end, on an open version"
    ),
    (Origin.DECLARED, "tracked"): (
        "a tracked column of the emit's history entry: its first level is drawn from the "
        "levels the entry declares, and it changes along the transitions the entry declares "
        "from one version of an entity to the next"
    ),
    (Origin.DECLARED, "period-kind"): (
        "declared as an override in the emit's periods kinds, on the rows of the kinds that "
        "name it; every other row takes the value its period's version holds, or the column's "
        "own declaration when the history has no such column"
    ),
    (Origin.COPIED, "version"): (
        "the value of the version the row is read from, in a column of the history the emit "
        "projects: a snapshot's last version of an entity, the version an event is about (the "
        "previous one for a before column, none for the after column of a delete), or the "
        "version a period holds"
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


def _names_after(source: Optional[str], prefixes: Sequence[str]) -> bool:
    """Whether *source* is text that starts with one of *prefixes* and goes on after it."""
    return isinstance(source, str) and any(
        source.startswith(prefix) and len(source) > len(prefix) for prefix in prefixes
    )


_RULE_PREFIXES = (
    SOURCE_RULE_PREFIX,
    SOURCE_FLUX_RULE_PREFIX,
    SOURCE_LIFETIME_PREFIX,
    SOURCE_WITHIN_PREFIX,
)

#: The pairs whose source is text that starts with one of these prefixes and goes on after it.
_PREFIXED_SOURCES: Mapping[Tuple[Origin, str], Tuple[str, ...]] = {
    (Origin.DERIVED, "projection"): (SOURCE_PROJECTION_PREFIX,),
    (Origin.DERIVED, "rule"): _RULE_PREFIXES,
    (Origin.DERIVED, "dependency"): _RULE_PREFIXES,
    (Origin.DERIVED, "relationship"): (SOURCE_RELATIONSHIP_PREFIX,),
    (Origin.COPIED, "version"): (SOURCE_VERSION_PREFIX,),
    (Origin.COPIED, "rule"): (
        SOURCE_RULE_PREFIX,
        SOURCE_FLUX_RULE_PREFIX,
        SOURCE_RELATIONSHIP_PREFIX,
        SOURCE_SHARED_ATTRIBUTE_PREFIX,
    ),
}

#: The pairs whose source is one text, worked out from the column's name. A pair in neither table
#: takes any non-empty source (a copy of a runtime attribute), and a reference column's is checked
#: by :meth:`ColumnCoverage._reference_source_fits`.
_EXACT_SOURCES: Mapping[Tuple[Origin, str], Callable[[str], Optional[str]]] = {
    (Origin.DERIVED, "key"): lambda column: SOURCE_KEY,
    (Origin.DERIVED, "entity"): lambda column: SOURCE_KEY,
    (Origin.DERIVED, "identifier"): lambda column: SOURCE_IDENTIFIER,
    (Origin.DECLARED, "plugin"): lambda column: SOURCE_PLUGIN,
    (Origin.DECLARED, "history"): lambda column: SOURCE_HISTORY,
    (Origin.DECLARED, "lag"): lambda column: SOURCE_EVENT_LAG,
    (Origin.DECLARED, "tracked"): lambda column: f"{SOURCE_HISTORY_TRACKED_PREFIX}{column}",
    (Origin.DECLARED, "period-kind"): lambda column: SOURCE_PERIOD_KINDS,
    (Origin.DECLARED, "fidelity"): lambda column: f"fidelity.columns.{column}",
    (Origin.DECLARED, "relationship"): lambda column: f"fidelity.columns.{column}",
    (Origin.DEFAULT, "time-bounds"): lambda column: SOURCE_TIME_BOUNDS,
    (Origin.DEFAULT, "none"): lambda column: None,
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
    foreign key) and ``rule`` (a contract rule that determines the column, or one that copies it from the
    row a link reaches). A history's emit adds ``entity`` (the natural key of an entity, repeated on
    its versions), ``history`` (a validity or activation column the chain draws) and ``tracked`` (a
    tracked column); a projected emit adds ``version`` (a column copied from the history it
    projects), ``projection`` (a value worked out from the history) and ``period-kind`` (an
    override of a periods kind).

    The pair ``(origin, decided_by)`` must be one of those the engine
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
        pair = (self.origin, self.decided_by)
        if pair in _PREFIXED_SOURCES:
            return _names_after(self.source, _PREFIXED_SOURCES[pair])
        if self.origin is Origin.PROFILE:
            return self._reference_source_fits()
        if pair in _EXACT_SOURCES:
            return self.source == _EXACT_SOURCES[pair](self.column)
        return isinstance(self.source, str) and bool(self.source)

    def _reference_source_fits(self) -> bool:
        """Whether ``source`` names this column of a reference table: ``<prefix><expose>.<column>``."""
        return (
            isinstance(self.source, str)
            and self.source.startswith(SOURCE_REFERENCE_PREFIX)
            and self.source.endswith(f".{self.column}")
            and len(self.source) > len(SOURCE_REFERENCE_PREFIX) + len(self.column) + 1
        )

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
