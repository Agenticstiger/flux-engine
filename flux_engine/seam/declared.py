# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""What a contract declares and the run does not generate, and the refusal strictness adds.

A bundle can declare things this version of the engine accepts and then does
nothing about: the rows are drawn as if the declaration were not there. That is
a different fact from a column that fell back to a type default
(:mod:`flux_engine.seam.coverage`), which is about a column nobody declared
anything for; here somebody did, and the declaration is not honoured. Reporting
it is the same job, so this module follows that one: the plan says so, the
evidence pack lists it under "What Was Not Proven", and under strictness
(``--strict-coverage``, or ``strictCoverage: true`` in the Simulation's
extensions) the run is refused before any row exists (``FLX-3305``).

**Four kinds, a closed set.**

* ``shared-columns`` -- a relationship's ``sharedColumns``: the columns it says
  are copied from the parent row. Nothing copies them, so a row's value can
  differ from its parent's.
* ``shared-attribute`` -- a schema column's ``sharedAttribute``, the same claim
  made through a named attribute.
* ``check`` -- a ``check`` rule, in the contract or in the bundle, that
  determines no column. A check generates only through a FLUX ``generates``
  setting on a difference of dates; any other check is not enforced on the rows
  drawn.
* ``check-over-link`` -- a ``check`` that reads another row through a
  relationship. Listed apart because the read is what a reader looks for.

What a declaration is called in the contract is its *locator*, a path a reader
can follow (``semantics.relationships.item.sharedColumns``) and never a value.
The sentence a report says of a kind is a function of the kind alone, so a record
read back from a file cannot put words of its own into a report.

This module reads no contract: it holds the vocabulary, the rows and the refusal.
:func:`flux_engine.generator.rules.declared_not_generated` finds the declarations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from flux_engine.core.errors import FluxError, register
from flux_engine.seam.coverage import STRICT_BY_FLAG, printable

__all__ = [
    "FLX_3305",
    "KIND_CHECK",
    "KIND_CHECK_OVER_LINK",
    "KIND_SHARED_ATTRIBUTE",
    "KIND_SHARED_COLUMNS",
    "NotGenerated",
    "render_not_generated",
    "require_all_generated",
]

FLX_3305 = register(
    "FLX-3305",
    "a declaration is accepted but not generated and coverage is strict",
    "A contract can declare what this version of the engine accepts and does not "
    "generate: a relationship's sharedColumns, a column's sharedAttribute, and a "
    "check that determines no column (one without a generates setting, or one of a "
    "form that setting does not reach), alone or reading another row through a "
    "relationship. The rows are drawn as if the declaration were not there, so some "
    "of them can break it. That is reported on every plan and listed under What Was "
    "Not Proven in the evidence report; under --strict-coverage, or when the "
    "Simulation declares strictCoverage: true under its com.agentics.flux/1 "
    "extensions, it is refused, before any row exists, with one table row per "
    "declaration. To have a check generated, give a derive or a dependency for the "
    "column it constrains, or set generates on a check that is a difference of "
    "dates over the row's own columns. This version does not copy a column through "
    "a relationship, so a sharedColumns or sharedAttribute declaration, and a rule "
    "that reads another row, can only be removed from the bundle or accepted. Or "
    "drop --strict-coverage, and strictCoverage from the Simulation if it declares "
    "it, to accept the declaration as it is.",
)

KIND_SHARED_COLUMNS = "shared-columns"
KIND_SHARED_ATTRIBUTE = "shared-attribute"
KIND_CHECK = "check"
KIND_CHECK_OVER_LINK = "check-over-link"

#: The one sentence a report says of each kind, and nothing else says it.
_DETAIL: Mapping[str, str] = {
    KIND_SHARED_COLUMNS: (
        "the columns are not copied from the row the relationship reaches, so a value "
        "can differ from its parent's"
    ),
    KIND_SHARED_ATTRIBUTE: (
        "the attribute is not generated once and copied, so members can disagree"
    ),
    KIND_CHECK: (
        "a check that determines no column is not enforced: rows are drawn without it "
        "and some can break it"
    ),
    KIND_CHECK_OVER_LINK: (
        "a check over a relationship is not enforced: rows are drawn without it and "
        "some can break it"
    ),
}

#: Kinds in the order a report lists them.
_ORDER: Tuple[str, ...] = (
    KIND_SHARED_COLUMNS,
    KIND_SHARED_ATTRIBUTE,
    KIND_CHECK,
    KIND_CHECK_OVER_LINK,
)


@dataclass(frozen=True)
class NotGenerated:
    """One declaration of one expose that the run accepts and does not generate.

    ``locator`` says where the contract (or the bundle) declares it;
    ``names`` is what it names: the columns a ``sharedColumns`` lists, the
    attribute a column carries, or the ``<relationship>.<column>`` reads of a
    check over a link. ``kind`` is one of the four above, or construction
    raises ``ValueError``.
    """

    expose_id: str
    kind: str
    locator: str
    names: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _DETAIL:
            raise ValueError(f"no declaration is not generated as {self.kind!r}")
        if not self.locator:
            raise ValueError("a declaration has a locator")

    @property
    def detail(self) -> str:
        """What a report says of this declaration: fixed text per kind."""
        return _DETAIL[self.kind]

    def as_document(self) -> Dict[str, Any]:
        """The row as canonical-ready JSON."""
        return {
            "exposeId": self.expose_id,
            "kind": self.kind,
            "locator": self.locator,
            "names": list(self.names),
            "detail": self.detail,
        }

    @classmethod
    def from_document(cls, document: Any) -> "NotGenerated":
        """Read a row back; ``ValueError`` when *document* is not a row this module wrote."""
        if not isinstance(document, Mapping):
            raise ValueError("a declaration row is a mapping")
        try:
            expose_id, kind = document["exposeId"], document["kind"]
            locator, names = document["locator"], document["names"]
        except KeyError as error:
            raise ValueError(f"a declaration row is malformed: {error!r}") from error
        if not (
            isinstance(expose_id, str)
            and isinstance(kind, str)
            and isinstance(locator, str)
            and isinstance(names, Sequence)
            and not isinstance(names, (str, bytes))
            and all(isinstance(name, str) for name in names)
        ):
            raise ValueError("a declaration row carries a value of the wrong class")
        return cls(expose_id=expose_id, kind=kind, locator=locator, names=tuple(names))

    def sort_key(self) -> Tuple[int, str]:
        """Kind in report order, then locator, so a list is the same on every run."""
        return (_ORDER.index(self.kind), self.locator)

    def line(self) -> str:
        """One plain line naming the declaration, for a plan's output."""
        listed = f" ({', '.join(printable(name) for name in self.names)})" if self.names else ""
        return f"{printable(self.locator)}{listed}: {self.detail}"


def _by_expose(rows: Iterable[NotGenerated]) -> Dict[str, List[NotGenerated]]:
    grouped: Dict[str, List[NotGenerated]] = {}
    for row in rows:
        grouped.setdefault(row.expose_id, []).append(row)
    return grouped


def render_not_generated(rows: Sequence[NotGenerated]) -> List[str]:
    """The plan's lines for *rows*: one line per declaration, grouped by expose; none when no rows."""
    lines: List[str] = []
    for expose_id, group in _by_expose(rows).items():
        lines.append(
            f"declared, not generated: {printable(expose_id)}: {len(group)} "
            f"declaration{'s' if len(group) != 1 else ''} accepted and not generated"
        )
        lines.extend(f"  {row.line()}" for row in group)
    return lines


def _table(headings: Tuple[str, ...], rows: Sequence[Tuple[str, ...]]) -> List[str]:
    widths = [
        max(len(headings[index]), *(len(row[index]) for row in rows))
        for index in range(len(headings))
    ]

    def line(cells: Tuple[str, ...]) -> str:
        return "  ".join(
            cell.ljust(width) for cell, width in zip(cells, widths, strict=True)
        ).rstrip()

    return [line(headings), *(line(row) for row in rows)]


def require_all_generated(
    rows: Sequence[NotGenerated], *, asked_by: Sequence[str] = (STRICT_BY_FLAG,)
) -> None:
    """Raise ``FLX-3305`` naming every declaration the run accepts and does not generate.

    One refusal for the whole run, with a table per expose, as
    :func:`flux_engine.seam.coverage.require_full_coverage` does for columns.
    Nothing is raised when no declaration is left out. *asked_by* names what
    made the run strict, so the last line says what to take away to accept them.
    """
    if not rows:
        return
    lines = [
        f"{len(rows)} declaration{'s' if len(rows) != 1 else ''} "
        "accepted and not generated by this version"
    ]
    for expose_id, group in _by_expose(rows).items():
        lines.append(f"expose {printable(expose_id)!r} ({len(group)}):")
        table = _table(
            ("declaration", "kind", "names", "what is not done"),
            [
                (
                    printable(row.locator),
                    row.kind,
                    ", ".join(printable(name) for name in row.names) or "-",
                    row.detail,
                )
                for row in group
            ],
        )
        lines.extend(f"  {line}" for line in table)
    kinds = {row.kind for row in rows}
    if kinds & {KIND_CHECK}:
        lines.append(
            "to have a check generated, give a derive or a dependency for the column it "
            "constrains, or set generates on a check that is a difference of dates over the "
            "row's own columns; or remove the check from the bundle"
        )
    if kinds & {KIND_CHECK_OVER_LINK}:
        lines.append(
            "this version does not generate from a rule that reads another row through a "
            "relationship; remove the rule from the bundle"
        )
    if kinds & {KIND_SHARED_COLUMNS, KIND_SHARED_ATTRIBUTE}:
        lines.append(
            "this version does not copy a column through a relationship; remove "
            "sharedColumns or sharedAttribute from the bundle"
        )
    lines.append(f"without {' and '.join(asked_by)} this is a report and the run goes on")
    raise FluxError(FLX_3305, "\n".join(lines))
