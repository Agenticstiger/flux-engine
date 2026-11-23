# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""How each column of a generator-driven emit was produced.

A plan built from declared statistics (:func:`flux_engine.generator.rows.build_plan`)
already knows, per column, whether the value is the author's declaration,
a derivation or the type's own default. This reads that off and files it as
a coverage report (:mod:`flux_engine.seam.coverage`), so the same questions
get the same answers for a generator-driven emit and a scenario-driven one
(:meth:`flux_engine.seam.mapping.ColumnMapping.coverage`).

Seven outcomes, and nothing else:

* a column a contract rule determines is ``derived``, decided by ``rule``, and
  its source names the rules' ids (:mod:`flux_engine.generator.rules`); it
  comes first, because a rule sets the column whatever else could have; one whose
  whole value is a column read through a link is ``copied`` instead, decided by ``rule``;

* a key the contract declares is ``derived``, decided by ``key``: laid out
  from the row index so that no value repeats
  (:mod:`flux_engine.generator.relations`);
* a foreign key is ``derived``, decided by ``relationship``, when its parent
  row is picked for it, and ``declared``, decided by ``relationship``, when
  its own declaration draws the value it then moves to the parent's nearest
  key;
* an identifier column is ``derived``: a function of the seam digest and the
  row index, never drawn (SEAM-FIDELITY-SPEC §1's identifier row);
* a key or foreign key keeps its relational producer above even when its
  range came from the simulation's time bounds: the relations, not the
  bounds, decide its value;
* a column the plan marks ``defaulted`` is a ``default``: it declares no
  draw of its own and takes the type's default, which for a DATE or
  TIMESTAMP column is uniform within the simulation's time bounds;
* a column a generator plugin generates is ``declared``, decided by ``plugin``, and its source is the
  Simulation's ``generators`` key (ADR 0032), which comes before the fidelity block because the plugin,
  not a draw from the block, makes the column;
* every other column is ``declared``: its family and parameters come from
  its entry under the emit's ``fidelity.columns``. A refused column never
  gets this far, because :func:`~flux_engine.generator.rows.build_plan`
  refuses a column it cannot parameterise (``FLX-3402``).

A history's emit and the emits projected from it (ADR 0017 and ADR 0027) have origins of their own,
because their columns are laid out by the chain and not by the row index:

* the natural entity key is ``derived``, decided by ``entity``: laid out from the entity index and
  repeated on every version of its entity;
* a validity or activation column is ``declared``, decided by ``history``: the chain draws it from
  the emit's history entry, and a tracked column is ``declared``, decided by ``tracked``;
* a column the history does not set is drawn on an entity's first version row and carried on the
  others: its own declaration still draws it, so it stays ``declared``, decided by ``fidelity``;
* a column of a snapshot, an event or a periodic row that reads a column of the history is
  ``copied``, decided by ``version``; what an event or a period works out from the history alone is
  ``derived``, decided by ``projection``; an event's recorded date, when its entry declares a ``lag``,
  is ``declared``, decided by ``lag``, since the lag is drawn; and a column a periods kind overrides is ``declared``,
  decided by ``period-kind``.

Reading a plan changes nothing about it: no digest in
:meth:`~flux_engine.generator.rows.GeneratorPlan.as_document` depends on
this module, and ``generatorDigest`` is the one it was before it existed.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Set, Tuple

from flux_engine.generator.families import ColumnType
from flux_engine.generator.rows import GeneratorPlan
from flux_engine.generator.rules import FORM_DEPENDENCY, ORIGIN_BUNDLE, ORIGIN_SHARED
from flux_engine.seam.coverage import (
    SOURCE_EVENT_LAG,
    SOURCE_FLUX_RULE_PREFIX,
    SOURCE_HISTORY,
    SOURCE_HISTORY_TRACKED_PREFIX,
    SOURCE_IDENTIFIER,
    SOURCE_KEY,
    SOURCE_PERIOD_KINDS,
    SOURCE_PLUGIN,
    SOURCE_PROJECTION_PREFIX,
    SOURCE_REFERENCE_PREFIX,
    SOURCE_RELATIONSHIP_PREFIX,
    SOURCE_RULE_PREFIX,
    SOURCE_TIME_BOUNDS,
    SOURCE_VERSION_PREFIX,
    ColumnCoverage,
    ExposeCoverage,
    Origin,
)

__all__ = ["coverage_of_plan"]


def coverage_of_plan(plan: GeneratorPlan) -> ExposeCoverage:
    """The coverage of every written column of *plan*'s expose, in the declared column order.

    A hidden column (ADR 0024 section 2) is not written, and the report is about what is.
    """
    rows: List[ColumnCoverage] = []
    for column_plan in plan.columns:
        column = column_plan.column
        if column.hidden:
            continue
        origin, source, decided_by = _decided(plan, column_plan)
        rows.append(
            ColumnCoverage(
                column=column.name,
                type_name=column.type_name,
                required=column.required,
                origin=origin,
                source=source,
                decided_by=decided_by,
            )
        )
    return ExposeCoverage(expose_id=plan.expose.expose_id, columns=tuple(rows))


def _decided(plan: GeneratorPlan, column_plan: Any) -> Tuple[Origin, str, str]:
    """What produces one column: its origin, its source and what decided it.

    The first of these that applies: a rule that determines the column, a relation of
    the plan, an identifier, a default drawn from the time bounds, or the fidelity block.
    """
    column = column_plan.column
    projected = _projected(plan, column.name)
    if projected is not None:
        return projected
    related = _related(plan, column.name)
    if plan.rules is not None and column.name in plan.rules.determined:
        return _ruled(plan.rules.rules_of(column.name))
    if related is not None:
        return related
    if plan.plugin is not None and column.name in plan.plugin.names:
        return Origin.DECLARED, SOURCE_PLUGIN, "plugin"
    if column_plan.column_type is ColumnType.IDENTIFIER:
        return Origin.DERIVED, SOURCE_IDENTIFIER, "identifier"
    if column_plan.defaulted:
        return Origin.DEFAULT, SOURCE_TIME_BOUNDS, "time-bounds"
    return Origin.DECLARED, f"fidelity.columns.{column.name}", "fidelity"


def _projected(plan: GeneratorPlan, name: str) -> Optional[Tuple[Origin, str, str]]:
    """The coverage of a column a projected emit takes from its history, or ``None`` for another.

    A snapshot copies every column from an entity's last version. An event reads its op, its
    instants, its reason and its before and after images from the history, and copies each other
    column the history has; a periodic row copies the columns the history has and works out its
    period column, and a periods kind may override a column. Any other column of an event or a
    periodic row is the emit's own.
    """
    projection = plan.projection
    if projection is None:
        return None
    # A hidden column of the history is not written, so no label names it as a source.
    shown = {
        column.column.name for column in projection.history.columns if not column.column.hidden
    }
    hidden = {column.column.name for column in projection.history.columns if column.column.hidden}
    copied = Origin.COPIED, f"{SOURCE_VERSION_PREFIX}{projection.of}.{name}", "version"
    entry = projection.entry
    if entry is None:
        return None if name in hidden else copied
    if projection.kind == "periods":
        return _periodic(entry, name, shown, copied)
    return _event(projection, name, shown, hidden, copied)


def _periodic(
    entry: Any, name: str, shown: Set[str], copied: Tuple[Origin, str, str]
) -> Optional[Tuple[Origin, str, str]]:
    """A periodic row's column: overridden by a kind, the period's first day, or a version's."""
    if any(name in dict(kind.overrides) for kind in entry.kinds):
        return Origin.DECLARED, SOURCE_PERIOD_KINDS, "period-kind"
    if name == entry.column:
        return Origin.DERIVED, SOURCE_PROJECTION_PREFIX + "periods", "projection"
    return copied if name in shown else None


def _event(
    projection: Any, name: str, shown: Set[str], hidden: Set[str], copied: Tuple[Origin, str, str]
) -> Optional[Tuple[Origin, str, str]]:
    """An event's column: its lagged date, what the history works out, a before or after image."""
    entry = projection.entry
    if name == entry.date and entry.lag is not None:
        return Origin.DECLARED, SOURCE_EVENT_LAG, "lag"
    if name in (entry.op, entry.at, entry.date, entry.type_column):
        return Origin.DERIVED, SOURCE_PROJECTION_PREFIX + projection.kind, "projection"
    for column, read in (*entry.before, *entry.after):
        if column == name and read not in hidden:
            return Origin.COPIED, f"{SOURCE_VERSION_PREFIX}{projection.of}.{read}", "version"
    return copied if name in shown else None


def _ruled(ruled: Sequence[Any]) -> Tuple[Origin, str, str]:
    """The coverage of a column the rules *ruled* determine, the first of them naming it."""
    ids = ",".join(rule.id for rule in ruled)
    prefix = SOURCE_FLUX_RULE_PREFIX if ruled[0].origin == ORIGIN_BUNDLE else SOURCE_RULE_PREFIX
    origin, source = Origin.DERIVED, prefix + ids
    if (
        ruled[0].duration is not None or ruled[0].within is not None or ruled[0].period is not None
    ) and ruled[0].source is not None:
        # a date a lifetime's duration, a period's end or a within entry draws is named by its column
        source = ruled[0].source
    if ruled[0].origin == ORIGIN_SHARED and ruled[0].source is not None:
        # A copy is named by its relationship and column, or by its attribute.
        source = ruled[0].source
    decided_by = "dependency" if ruled[0].form == FORM_DEPENDENCY else "rule"
    if all(rule.whole_path for rule in ruled):
        # A column whose rule's whole value is a path takes the linked row's value.
        origin = Origin.COPIED
    return origin, source, decided_by


def _chained(driver: Any, column: str) -> Optional[Tuple[Origin, str, str]]:
    """The coverage of a column a history's chain lays out, or ``None`` for another driver."""
    # Imported here: relations imports the row generator, which imports nothing of this.
    from flux_engine.generator.relations import ChainColumn, EntityColumn, TrackedColumn

    if isinstance(driver, EntityColumn):
        return Origin.DERIVED, SOURCE_KEY, "entity"
    if isinstance(driver, ChainColumn):
        return Origin.DECLARED, SOURCE_HISTORY, "history"
    if isinstance(driver, TrackedColumn):
        return Origin.DECLARED, f"{SOURCE_HISTORY_TRACKED_PREFIX}{column}", "tracked"
    return None


def _related(plan: GeneratorPlan, column: str) -> Optional[Tuple[Origin, str, str]]:
    """The coverage of a column the plan's relations decide, or ``None``."""
    # Imported here: relations imports the row generator, which imports nothing of this.
    from flux_engine.generator.reference import ReferenceColumn
    from flux_engine.generator.relations import CompositeMember, ForeignKey, ParentPart

    driver = None if plan.relations is None else plan.relations.driver(column)
    if driver is None:
        return None
    chained = _chained(driver, column)
    if chained is not None:
        return chained
    if isinstance(driver, ReferenceColumn):
        # The pinned profile publishes the row's value in a table released whole (ADR 0023).
        source = f"{SOURCE_REFERENCE_PREFIX}{plan.expose.expose_id}.{column}"
        return Origin.PROFILE, source, "reference"
    if isinstance(driver, ForeignKey):
        if driver.pick == "declared":
            return Origin.DECLARED, f"fidelity.columns.{column}", "relationship"
        return Origin.DERIVED, SOURCE_RELATIONSHIP_PREFIX + driver.relationship, "relationship"
    if isinstance(driver, CompositeMember):
        for part in driver.composite.parts:
            if isinstance(part, ParentPart) and column in part.columns:
                return (
                    Origin.DERIVED,
                    SOURCE_RELATIONSHIP_PREFIX + part.relationship,
                    "relationship",
                )
    return Origin.DERIVED, SOURCE_KEY, "key"
