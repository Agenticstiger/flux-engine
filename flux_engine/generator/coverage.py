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

Six outcomes, and nothing else:

* a column a contract rule determines is ``derived``, decided by ``rule``, and
  its source names the rules' ids (:mod:`flux_engine.generator.rules`); it
  comes first, because a rule sets the column whatever else could have;

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
* every other column is ``declared``: its family and parameters come from
  its entry under the emit's ``fidelity.columns``. A refused column never
  gets this far, because :func:`~flux_engine.generator.rows.build_plan`
  refuses a column it cannot parameterise (``FLX-3402``).

Reading a plan changes nothing about it: no digest in
:meth:`~flux_engine.generator.rows.GeneratorPlan.as_document` depends on
this module, and ``generatorDigest`` is the one it was before it existed.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from flux_engine.generator.families import ColumnType
from flux_engine.generator.rows import GeneratorPlan
from flux_engine.generator.rules import FORM_DEPENDENCY, ORIGIN_BUNDLE
from flux_engine.seam.coverage import (
    SOURCE_FLUX_RULE_PREFIX,
    SOURCE_IDENTIFIER,
    SOURCE_KEY,
    SOURCE_RELATIONSHIP_PREFIX,
    SOURCE_RULE_PREFIX,
    SOURCE_TIME_BOUNDS,
    ColumnCoverage,
    ExposeCoverage,
    Origin,
)

__all__ = ["coverage_of_plan"]


def coverage_of_plan(plan: GeneratorPlan) -> ExposeCoverage:
    """The coverage of every column of *plan*'s expose, in the declared column order."""
    rows: List[ColumnCoverage] = []
    for column_plan in plan.columns:
        column = column_plan.column
        related = _related(plan, column.name)
        if plan.rules is not None and column.name in plan.rules.determined:
            ruled = plan.rules.rules_of(column.name)
            ids = ",".join(rule.id for rule in ruled)
            prefix = (
                SOURCE_FLUX_RULE_PREFIX if ruled[0].origin == ORIGIN_BUNDLE else SOURCE_RULE_PREFIX
            )
            by_dependency = ruled[0].form == FORM_DEPENDENCY
            origin, source = Origin.DERIVED, prefix + ids
            decided_by = "dependency" if by_dependency else "rule"
        elif related is not None:
            origin, source, decided_by = related
        elif column_plan.column_type is ColumnType.IDENTIFIER:
            origin, source, decided_by = Origin.DERIVED, SOURCE_IDENTIFIER, "identifier"
        elif column_plan.defaulted:
            origin, source, decided_by = Origin.DEFAULT, SOURCE_TIME_BOUNDS, "time-bounds"
        else:
            origin, source, decided_by = (
                Origin.DECLARED,
                f"fidelity.columns.{column.name}",
                "fidelity",
            )
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


def _related(plan: GeneratorPlan, column: str) -> Optional[Tuple[Origin, str, str]]:
    """The coverage of a column the plan's relations decide, or ``None``."""
    # Imported here: relations imports the row generator, which imports nothing of this.
    from flux_engine.generator.relations import CompositeMember, ForeignKey, ParentPart

    driver = None if plan.relations is None else plan.relations.driver(column)
    if driver is None:
        return None
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
