# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A catalogue, and the rows that follow it, written from a profile of format 1-3-0.

ADR 0023 section 5. Three rules, each reading one form of the profile that a rule of
ADR 0014 passed over:

- an expose the profile publishes whole under ``references`` is a reference expose
  (:func:`reference_table`, :func:`published_levels`): the bundle declares ``reference: true``
  for it and nothing else;
- a column statistic grouped by a link (``groupByLink``) is a two-way table of the column and the
  parent's column. The column is drawn ``given: <relationship>.<column>`` under the table's
  weights (:func:`link_givens`), under the rules the generator applies to such a column
  (SEAM-FIDELITY-SPEC section 2): a parent row on every row, a parent column drawn from its
  own levels, one ``given`` per column, no cycle among the exposes;
- a foreign key into a reference expose that the profile counts by level (a ``levelHistogram/1``
  over the keys) declares those levels, the child rows per catalogue row. A foreign key whose
  statistic is not over the catalogue's keys draws no level, and so picks a row uniformly
  (:func:`catalogue_foreign_keys`).

Every grouping this leaves out is passed over in the ledger with the reason the generator would
refuse it for, or the reason it is no table. The tables are read from the profile alone.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from flux_engine.author.basis import Label, assumed, from_profile
from flux_engine.author.columns import (
    DEFAULT_TOLERANCES,
    TOLERANCE_REASON,
    ColumnPlan,
    PropertyPlan,
)
from flux_engine.author.given import (
    _child_cells,
    _declare,
    _parent_levels,
    _Table,
    _table,
    note_no_range,
)
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.coverage import printable
from flux_engine.seam.keys import ContractKeys, Relationship
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM

__all__ = [
    "catalogue_foreign_keys",
    "link_givens",
    "published_levels",
    "reference_table",
]

_LEVELS = LEVEL_HISTOGRAM.identifier
_HISTOGRAM = MARGINAL_HISTOGRAM.identifier
_CATEGORICAL = (LogicalKind.STRING, LogicalKind.BOOL)


class Written(Protocol):
    """What this module reads of an expose the bundle writes (``AuthoredExpose``)."""

    expose_id: str
    columns: List[ColumnPlan]
    kinds: Dict[str, LogicalKind]
    derives: Dict[str, Tuple[int, str]]
    dependents: Set[str]
    reference: bool
    linked: Dict[str, ColumnPlan]
    skipped: Optional[str]


# ---------------------------------------------------------------------------
# The published rows
# ---------------------------------------------------------------------------


def reference_table(
    references: Mapping[str, Any], expose_id: str, columns: Sequence[str]
) -> Tuple[Optional[Mapping[str, Any]], Optional[str]]:
    """The published table of *expose_id* and, when it cannot be the expose's rows, why not.

    A reference expose publishes every column the contract declares and no other
    (``FLX-3403`` otherwise), so a table whose columns differ is no reference expose.
    """
    table = references.get(expose_id)
    if not isinstance(table, Mapping):
        return None, None
    published = table.get("columns")
    names = [str(name) for name in published] if isinstance(published, list) else []
    if sorted(names) != sorted(columns) or len(set(names)) != len(names):
        return None, (
            "the profile publishes rows for it under references, but not exactly the columns "
            "the contract declares"
        )
    return table, None


def published_levels(table: Mapping[str, Any], column: str) -> Optional[Tuple[str, ...]]:
    """The distinct published values of *column*, as text, in published order.

    ``None`` when the column is not published, a row publishes NULL (which has no level), or no
    row is published.
    """
    names = table.get("columns")
    rows = table.get("rows")
    if not isinstance(names, list) or not isinstance(rows, list) or column not in names:
        return None
    position = names.index(column)
    found: Dict[str, None] = {}
    for row in rows:
        if not isinstance(row, list) or position >= len(row) or row[position] is None:
            return None
        found.setdefault(str(row[position]), None)
    return tuple(found) or None


# ---------------------------------------------------------------------------
# A column drawn given a column of the row its link names
# ---------------------------------------------------------------------------


def _passed(plan: ColumnPlan, tokens: Tuple[str, ...], reason: str) -> None:
    plan.passed.append((tokens, reason))


def _reaches(edges: Mapping[str, Set[str]], start: str, goal: str) -> bool:
    """Whether the exposes read in a chain from *start* come to *goal*."""
    seen: Set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(edges.get(node, ()))
    return False


def _relationship_of(
    keys: Optional[ContractKeys], expose_id: str, link: Mapping[str, Any]
) -> Tuple[Optional[Relationship], Optional[str]]:
    """The one relationship of *expose_id* a grouping names, or why there is none."""
    parent = link.get("parent")
    table = str(parent.get("table")) if isinstance(parent, Mapping) else ""
    parent_columns = (
        tuple(str(name) for name in (parent.get("columns") or ()))
        if isinstance(parent, Mapping)
        else ()
    )
    child_columns = tuple(str(name) for name in link.get("columns") or ())
    found = [
        relationship
        for relationship in (keys.expose(expose_id).relationships if keys is not None else ())
        if relationship.target == table
        and tuple(relationship.from_columns) == child_columns
        and tuple(relationship.to_columns) == parent_columns
    ]
    if not found:
        return None, (
            f"the contract declares no relationship of {printable(expose_id)} from "
            f"{', '.join(printable(name) for name in child_columns)} to "
            f"{', '.join(printable(name) for name in parent_columns)} of {printable(table)}"
        )
    if len(found) > 1:
        return None, (
            "the contract declares two relationships over these columns, so the grouping does "
            "not say which one a column is drawn given"
        )
    return found[0], None


def _linked_property(
    child: ColumnPlan,
    table: _Table,
    key: str,
    levels: Tuple[str, ...],
) -> Tuple[Optional[PropertyPlan], Optional[ColumnPlan]]:
    """The property the run is scored on, grouped by the link, and the declaration of its levels.

    Written, as for a column drawn given another of its own row, only when the profile's
    statistic lists the levels or bins the column draws and a group of every level the parent
    draws, so ``verify`` has a statistic to compare each group with.
    """
    statistic = table.statistic
    if set(table.counts) != set(levels):
        return None, None
    tolerance = {
        "type": "distance",
        "measure": "totalVariation",
        "max": DEFAULT_TOLERANCES["distance"],
        "sampling": True,
    }
    labels: List[Label] = []
    if table.numeric:
        if note_no_range(child):
            return None, None
        document: Dict[str, Any] = {
            "property": "marginalHistogram",
            "column": child.name,
            "groupBy": key,
            "bins": list(statistic.get("bins", ())),
            "tolerance": tolerance,
        }
        labels = [
            Label(
                ("bins", str(position)),
                from_profile(table.tokens + ("bins", str(position)), _HISTOGRAM),
            )
            for position in range(len(document["bins"]))
        ]
    else:
        listed = {str(level) for level in statistic.get("levels", ())}
        if listed != set(table.cells) or "support" not in child.declaration:
            return None, None
        document = {
            "property": "levelHistogram",
            "column": child.name,
            "groupBy": key,
            "tolerance": tolerance,
        }
    grouping = ColumnPlan(
        name=key,
        declaration={"support": {"values": list(levels)}, "maxCardinality": len(levels)},
        labels=[
            Label(
                ("maxCardinality",),
                from_profile(table.tokens, _HISTOGRAM if table.numeric else _LEVELS),
            )
        ],
    )
    prop = PropertyPlan(
        document,
        [*labels, Label(("tolerance", "max"), assumed(TOLERANCE_REASON))],
        scorable=True,
        reads=[(table.tokens, "the grouped property is scored against it")],
    )
    return prop, grouping


def link_givens(
    exposes: Sequence[Written],
    keys: Optional[ContractKeys],
    profile: Mapping[str, Any],
    references: Mapping[str, Any],
    *,
    minimum_cell: int,
) -> None:
    """Draw each column given a column of its linked row, where a grouped-by-link statistic says how.

    *exposes* are the ones the bundle writes, the reference exposes among them, in contract order.
    A column that the grouping can be no ``given`` of keeps its draw and the statistic is passed
    over with the reason.
    """
    tables = profile.get("tables")
    by_id = {expose.expose_id: expose for expose in exposes if expose.skipped is None}
    reads: Dict[str, Set[str]] = {}
    for expose in exposes:
        if expose.skipped is not None or expose.reference:
            continue
        entries = (
            tables.get(expose.expose_id, {}).get("columns")
            if isinstance(tables, Mapping) and isinstance(tables.get(expose.expose_id), Mapping)
            else None
        )
        if not isinstance(entries, Mapping):
            continue
        plans = {plan.name: plan for plan in expose.columns}
        for name, plan in plans.items():
            entry = entries.get(name)
            statistics = entry.get("statistics") if isinstance(entry, Mapping) else None
            for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
                if (
                    isinstance(statistic, Mapping)
                    and isinstance(statistic.get("groupByLink"), Mapping)
                    and statistic.get("definition") in (_LEVELS, _HISTOGRAM)
                ):
                    tokens = ("tables", expose.expose_id, "columns", name, "statistics", str(index))
                    _one(
                        expose,
                        plan,
                        statistic,
                        tokens,
                        plans,
                        by_id,
                        keys,
                        references,
                        reads,
                        minimum_cell,
                    )


def _one(
    expose: Written,
    plan: ColumnPlan,
    statistic: Mapping[str, Any],
    tokens: Tuple[str, ...],
    plans: Mapping[str, ColumnPlan],
    by_id: Mapping[str, Written],
    keys: Optional[ContractKeys],
    references: Mapping[str, Any],
    reads: Dict[str, Set[str]],
    minimum_cell: int,
) -> None:
    link = statistic["groupByLink"]
    column = str(link.get("column"))
    name = plan.name
    relationship, why = _relationship_of(keys, expose.expose_id, link)
    if relationship is None:
        _passed(plan, tokens, why or "")
        return
    where = f"{relationship.name}.{column}"
    if where in plans:
        # A name with a dot that is exactly a column is that column, so a given or a group key
        # written under it would not reach the link (SEAM-FIDELITY-SPEC section 2).
        return _passed(
            plan,
            tokens,
            f"the expose has a column named exactly {printable(where)}, which a given or a group "
            "key of that name would read as the column and not as the link",
        )
    kind = LogicalKind.STRING if plan.int_levels else expose.kinds[name]
    if name in expose.derives or name in expose.dependents:
        return _passed(plan, tokens, "a rule of the contract sets this column, so it is not drawn")
    if plan.fixed is not None:
        return _passed(
            plan, tokens, f"the column is {plan.fixed}, which is never drawn given another"
        )
    if "given" in plan.declaration:
        return _passed(
            plan,
            tokens,
            f"the column is drawn given {printable(str(plan.declaration['given']))}, and a "
            "column is drawn given one other at most",
        )
    foreign = plans.get(relationship.from_columns[0])
    if foreign is None or "nullFraction" in foreign.declaration:
        return _passed(
            plan,
            tokens,
            f"relationship {printable(relationship.name)} may reach no row (its foreign key "
            "holds NULL), and a NULL has no level to draw given (FLX-3402)",
        )
    if any(
        column_name in expose.derives or column_name in expose.dependents
        for column_name in relationship.from_columns
    ):
        return _passed(
            plan,
            tokens,
            f"a rule sets the columns of relationship {printable(relationship.name)} (FLX-3402)",
        )
    if "given" in foreign.declaration:
        return _passed(
            plan,
            tokens,
            f"the foreign key of relationship {printable(relationship.name)} is itself drawn "
            "given another column (FLX-3402)",
        )
    parent = by_id.get(relationship.target)
    if parent is None:
        return _passed(plan, tokens, "the bundle writes no emit of the parent expose")
    if parent.expose_id == expose.expose_id or _reaches(reads, parent.expose_id, expose.expose_id):
        return _passed(
            plan,
            tokens,
            f"{printable(parent.expose_id)} reads {printable(expose.expose_id)} through its own "
            "given columns, and the rows would read each other in a cycle (FLX-3402)",
        )
    levels = _parent_levels_of(parent, column, references, where)
    if isinstance(levels, str):
        return _passed(plan, tokens, levels)
    cells = _child_cells(plan, statistic, kind)
    if cells is None:
        return _passed(
            plan, tokens, "its cells are not the levels or bins the column is drawn over"
        )
    groups = statistic.get("groups")
    group_keys = {
        str(group.get("group"))
        for group in (groups if isinstance(groups, list) else ())
        if isinstance(group, Mapping)
    }
    if not group_keys <= set(levels):
        return _passed(
            plan, tokens, f"its groups are not levels {printable(where)} draws (FLX-3402)"
        )
    table = _table(where, name, tokens, statistic, cells, kind not in _CATEGORICAL, minimum_cell)
    if table is None:
        return _passed(plan, tokens, "no group of it is a table the draw can read")
    _declare(plan, ColumnPlan(name=where), table, levels)
    reads.setdefault(expose.expose_id, set()).add(parent.expose_id)
    plan.reads.append(
        (tokens, f"its group counts are the weights of {printable(name)} given {printable(where)}")
    )
    prop, grouping = _linked_property(plan, table, where, levels)
    if prop is not None and grouping is not None:
        plan.properties.append(prop)
        expose.linked[where] = grouping


def _parent_levels_of(
    parent: Written,
    column: str,
    references: Mapping[str, Any],
    where: str,
) -> Any:
    """The levels the parent's *column* draws, or the reason it can be no parent of a given."""
    if parent.reference:
        table = references.get(parent.expose_id)
        if parent.kinds.get(column) is not LogicalKind.STRING:
            return (
                f"{printable(where)} is a column of the catalogue that is not text, and its "
                "published values are not read as levels"
            )
        levels = published_levels(table, column) if isinstance(table, Mapping) else None
        if levels is None:
            return (
                f"{printable(where)} publishes no rows, or NULL in some row, and a NULL has no "
                "level to condition on (FLX-3402)"
            )
        return levels
    plans = {plan.name: plan for plan in parent.columns}
    found = plans.get(column)
    if found is None:
        return f"{printable(parent.expose_id)} has no column {printable(column)} (FLX-3402)"
    if column in parent.derives:
        return f"a rule of the contract sets {printable(where)}, so no column is drawn given it"
    if "given" in found.declaration:
        return (
            f"{printable(where)} is itself drawn given another column, and a parent is drawn "
            "from its own levels (FLX-3402)"
        )
    levels_held = _parent_levels(found)
    if levels_held is None:
        return (
            f"{printable(where)} is not a categorical drawn from its own levels with no "
            "nullFraction, so it has no level to condition on (FLX-3402)"
        )
    return levels_held


# ---------------------------------------------------------------------------
# A foreign key into a catalogue
# ---------------------------------------------------------------------------

_UNIFORM = (
    "The profile gives the column no count of the catalogue's keys, so it picks a catalogue row "
    "uniformly."
)


def catalogue_foreign_keys(
    exposes: Sequence[Written],
    keys: Optional[ContractKeys],
    references: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> None:
    """Declare a foreign key into a reference expose by the keys its profile counts, else by none.

    A foreign key that declares ``levels`` takes the catalogue row whose key equals the level it
    draws (ADR 0023 section 2). The levels are the profile's, and each must be a published key
    (``FLX-3403``); the ones the foreign key's statistic does not cover leave those rows no
    children. A foreign key whose declaration is not that, a span of numbers or levels that are
    no key, would be refused or would not match its parent, so it declares nothing.
    """
    by_id = {expose.expose_id: expose for expose in exposes if expose.skipped is None}
    for expose in exposes:
        if expose.skipped is not None or expose.reference:
            continue
        plans = {plan.name: plan for plan in expose.columns}
        for relationship in keys.expose(expose.expose_id).relationships if keys else ():
            target = by_id.get(relationship.target)
            if target is None or not target.reference:
                continue
            table = references.get(target.expose_id)
            published = (
                published_levels(table, relationship.to_columns[0])
                if isinstance(table, Mapping) and len(relationship.to_columns) == 1
                else None
            )
            for column_name in relationship.from_columns:
                plan = plans.get(column_name)
                if plan is None:
                    continue
                levels = plan.declaration.get("levels")
                held = (
                    len(relationship.from_columns) == 1
                    and published is not None
                    and expose.kinds[column_name] is LogicalKind.STRING
                    and isinstance(levels, Mapping)
                    and set(map(str, levels)) <= set(published)
                )
                if held:
                    plan.how = (
                        f"a foreign key to {printable(target.expose_id)}, drawn over the levels "
                        "the profile counts: the child rows of each catalogue row"
                    )
                    continue
                _uniform(plan, expose, target.expose_id, column_name, profile)


def _uniform(
    plan: ColumnPlan, expose: Written, target: str, column: str, profile: Mapping[str, Any]
) -> None:
    """Strip a foreign key into a catalogue to the draw it can keep: a uniform pick."""
    if plan.declaration:
        statistics = (
            profile.get("tables", {}).get(expose.expose_id, {}).get("columns", {}).get(column, {})
        )
        found = statistics.get("statistics") if isinstance(statistics, Mapping) else None
        for index, item in enumerate(found if isinstance(found, list) else ()):
            if isinstance(item, Mapping):
                place = ("tables", expose.expose_id, "columns", column, "statistics", str(index))
                plan.passed.append(
                    (
                        place,
                        "its values are not levels of the catalogue's keys, so the foreign key "
                        "declares none: it picks a catalogue row uniformly",
                    )
                )
    # A foreign key that may be NULL stays so: only what drew its values is dropped.
    kept = {name: value for name, value in plan.declaration.items() if name == "nullFraction"}
    plan.declaration.clear()
    plan.declaration.update(kept)
    plan.labels = [label for label in plan.labels if label.tokens[:1] == ("nullFraction",)]
    plan.properties.clear()
    plan.reads = [item for item in plan.reads if "statistics" not in item[0]]
    plan.assumptions = [_UNIFORM]
    plan.weight_notes = []
    plan.choices = [_UNIFORM]
    plan.placeholder = False
    plan.how = f"a foreign key to {printable(target)}, picking a catalogue row uniformly"
