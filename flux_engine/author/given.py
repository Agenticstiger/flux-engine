# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Grouped statistics become conditional draws: at most one ``given`` per column.

ADR 0014 section 4. A ``levelHistogram/1`` or ``marginalHistogram/1`` of a
column grouped by a categorical column of the same table is a two-way table of
counts. Each table is scored by its mutual information, and the forest
:func:`flux_engine.calibrate.structure.spanning_forest` keeps (Chow and Liu
1968, under the generator's rules: one ``given`` per column, no cycle, a
histogram column only as a leaf, a categorical parent with no
``nullFraction``) decides which column is drawn given which. The child then
declares ``levelWeightsGiven`` or ``binWeightsGiven`` from the group counts, a
withheld cell filled as the column's own counts are (the remainder of the
group's ``k``), and a parent level the table holds no group for drawing the
child's own shares, stated as an assumption. Every grouping the forest leaves
out is passed over in the ledger, with the reason.

The tables are read from the profile alone: nothing here sees a record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.author.basis import Basis, Label, assumed, from_profile
from flux_engine.author.columns import (
    DEFAULT_TOLERANCES,
    TOLERANCE_REASON,
    ColumnPlan,
    PropertyPlan,
    cell_weights,
    count_cells,
    share_texts,
)
from flux_engine.calibrate.structure import PairTable, mutual_information, spanning_forest
from flux_engine.core.errors import FluxError
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.coverage import printable
from flux_stats.definitions import LEVEL_HISTOGRAM, MARGINAL_HISTOGRAM

__all__ = ["choose_givens"]

_LEVELS = LEVEL_HISTOGRAM.identifier
_HISTOGRAM = MARGINAL_HISTOGRAM.identifier
_FREE = "F"
_CATEGORICAL = (LogicalKind.STRING, LogicalKind.BOOL)

#: Why a parent level with no published group draws the child's own shares.
_NO_GROUP_REASON = (
    "The profile publishes no group of this level of the column drawn given, so the column "
    "takes its own shares here, as if the two were independent."
)


@dataclass
class _Table:
    """One grouped statistic as a two-way table: counts and bases by (group level, cell)."""

    parent: str
    child: str
    tokens: Tuple[str, ...]
    statistic: Mapping[str, Any]
    numeric: bool
    #: The child's cells: its level names, or its bin positions as text.
    cells: Tuple[str, ...]
    counts: Dict[str, List[Fraction]] = field(default_factory=dict)
    bases: Dict[str, List[Basis]] = field(default_factory=dict)
    #: Each group's own withheld-cell sentences, for the person reading the output.
    notes: List[str] = field(default_factory=list)


def _parent_levels(plan: ColumnPlan) -> Optional[Tuple[str, ...]]:
    """The levels a column draws when it may be the column another is drawn given."""
    if plan.fixed is not None or plan.level_names is None or "nullFraction" in plan.declaration:
        return None
    return plan.level_names


def _child_cells(
    plan: ColumnPlan, statistic: Mapping[str, Any], kind: LogicalKind
) -> Optional[Tuple[str, ...]]:
    """The cells a grouped statistic fills for *plan*, or ``None`` when it cannot be its table."""
    if plan.fixed is not None or plan.placeholder or plan.key_span:
        return None
    definition = statistic.get("definition")
    if kind in _CATEGORICAL:
        if definition != _LEVELS or plan.level_names is None:
            return None
        listed = {str(level) for level in statistic.get("levels", ())}
        if not listed or not listed <= set(plan.level_names):
            return None
        return plan.level_names
    if definition != _HISTOGRAM or kind not in (LogicalKind.INT, LogicalKind.DECIMAL):
        return None
    bins = statistic.get("bins")
    declared = plan.declaration.get("bins")
    if not isinstance(bins, list) or len(bins) < 2 or not isinstance(declared, list):
        return None
    # The grouped bins replace the column's own when they differ, so they have to lie
    # inside what the check holds the column to: its range, else its own bins.
    checked = plan.declaration.get("range")
    low, high = (
        (checked.get("min"), checked.get("max"))
        if isinstance(checked, Mapping)
        else (declared[0], declared[-1])
    )
    try:
        inside = Decimal(str(low)) <= Decimal(str(bins[0])) and Decimal(str(bins[-1])) <= Decimal(
            str(high)
        )
    except ArithmeticError:
        return None
    if not inside:
        return None
    return tuple(str(position) for position in range(len(bins) - 1))


def _table(
    parent: str,
    child: str,
    tokens: Tuple[str, ...],
    statistic: Mapping[str, Any],
    cells: Tuple[str, ...],
    numeric: bool,
    minimum_cell: int,
) -> Optional[_Table]:
    """The grouped statistic's published groups as counts per child cell, withheld cells filled."""
    table = _Table(parent, child, tokens, statistic, numeric, cells)
    key = "binCounts" if numeric else "levelCounts"
    listed = list(cells) if numeric else [str(level) for level in statistic.get("levels", ())]
    absent = [] if numeric else [level for level in cells if level not in listed]
    groups = statistic.get("groups")
    for position, group in enumerate(groups if isinstance(groups, list) else ()):
        if not isinstance(group, Mapping) or group.get("status") != _FREE:
            continue
        level = str(group.get("group"))
        if level in table.counts:
            return None  # two groups of one level: not a table this can read
        found = count_cells(group, key)
        if len(found) != len(listed):
            return None
        total = group.get("k")
        weights, bases = cell_weights(
            minimum_cell,
            found,
            tokens,
            key,
            _HISTOGRAM if numeric else _LEVELS,
            total=total if isinstance(total, int) and not isinstance(total, bool) else None,
            absent=len(absent),
            group=str(position),
        )
        by_cell = dict(zip(listed + absent, zip(weights, bases, strict=True), strict=True))
        table.counts[level] = [by_cell[cell][0] for cell in cells]
        table.bases[level] = [by_cell[cell][1] for cell in cells]
        if sum(table.counts[level], Fraction(0)) <= 0:
            return None
        if any(cell.count is None for cell in found) or absent:
            table.notes.append(level)
    return table if table.counts else None


def _information(table: _Table) -> Decimal:
    """The table's mutual information, from its counts rounded to whole records."""
    counts = {
        (level, cell): max(0, round(count))
        for level, row in table.counts.items()
        for cell, count in zip(table.cells, row, strict=True)
    }
    if sum(counts.values()) <= 0:
        return Decimal(0)
    try:
        return mutual_information(PairTable((table.parent, table.child), counts))
    except FluxError:
        return Decimal(0)


def _own_shares(plan: ColumnPlan, cells: Tuple[str, ...], numeric: bool) -> List[Fraction]:
    """The child's own weights over *cells*: what a parent level with no group draws."""
    if numeric:
        weights = plan.declaration.get("binWeights")
        if isinstance(weights, list) and len(weights) == len(cells):
            return [Fraction(Decimal(str(weight))) for weight in weights]
        return [Fraction(1)] * len(cells)
    levels = plan.declaration.get("levels")
    if isinstance(levels, Mapping):
        return [Fraction(Decimal(str(levels.get(cell, "0")))) for cell in cells]
    return [Fraction(1)] * len(cells)


def _transposed(table: _Table, levels: Tuple[str, ...]) -> Optional[_Table]:
    """The table read the other way: its cells as the groups, its groups' *levels* as cells.

    Only a table that publishes a group for every one of *levels* can be read so:
    a missing group is an unknown row, never a row of zeros.
    """
    if table.numeric or set(table.counts) != set(levels):
        return None
    flipped = _Table(table.child, table.parent, table.tokens, table.statistic, False, levels)
    for position, cell in enumerate(table.cells):
        row = [table.counts[level][position] for level in levels]
        if sum(row, Fraction(0)) <= 0:
            continue
        flipped.counts[cell] = row
        flipped.bases[cell] = [table.bases[level][position] for level in levels]
    flipped.notes = list(table.notes)
    return flipped


def _declare(
    child: ColumnPlan,
    parent: ColumnPlan,
    table: _Table,
    parent_levels: Tuple[str, ...],
) -> None:
    """Write *child*'s conditional weights on *parent* from *table*, with every basis."""
    rows: Dict[str, List[str]] = {}
    labels: List[Label] = []
    own = _own_shares(child, table.cells, table.numeric)
    key = "binWeightsGiven" if table.numeric else "levelWeightsGiven"
    missing: List[str] = []
    for level in parent_levels:
        if level in table.counts:
            weights, bases = table.counts[level], table.bases[level]
        else:
            weights, bases = own, [assumed(_NO_GROUP_REASON)] * len(own)
            missing.append(level)
        texts = share_texts(weights)
        rows[level] = texts
        for cell, basis in zip(table.cells, bases, strict=True):
            labels.append(Label((key, level, cell), basis))
    dropped = ("binWeights",) if table.numeric else ("levels",)
    for name in dropped:
        child.declaration.pop(name, None)
    child.labels = [label for label in child.labels if label.tokens[:1] != dropped]
    bins = [str(edge) for edge in table.statistic.get("bins", ())] if table.numeric else []
    if table.numeric and child.declaration.get("bins") != bins:
        # The column's own bins were moved or cut to meet its own statistics; drawn given
        # a parent, it is drawn over the grouped statistic's bins, each cited there.
        child.declaration["bins"] = bins
        child.labels = [label for label in child.labels if label.tokens[:1] != ("bins",)]
        labels += [
            Label(
                ("bins", str(position)),
                from_profile(table.tokens + ("bins", str(position)), _HISTOGRAM),
            )
            for position in range(len(bins))
        ]
        child.assumptions = [
            sentence for sentence in child.assumptions if sentence not in child.choices
        ]
    child.labels += labels
    child.declaration["given"] = parent.name
    if table.numeric:
        child.declaration[key] = {level: list(row) for level, row in rows.items()}
    else:
        child.declaration[key] = {
            level: dict(zip(table.cells, row, strict=True)) for level, row in rows.items()
        }
    child.assumptions = [
        sentence for sentence in child.assumptions if sentence not in child.weight_notes
    ]
    if table.notes:
        sentence = (
            f"Drawn given {printable(parent.name)}: in the groups the profile withholds counts "
            "of, or lists no level of, those counts share what the group's published counts "
            "leave of its records."
        )
        child.assumptions.append(sentence)
    if missing:
        names = ", ".join(printable(level) for level in missing)
        child.assumptions.append(
            f"Drawn given {printable(parent.name)}, whose levels {names} the profile publishes "
            "no group for: there the column takes its own shares."
        )
    noun = "bins" if table.numeric else "levels"
    child.how = (
        f"its {noun}, drawn given {printable(parent.name)} from the profile's grouped counts"
    )


def _grouped_property(
    child: ColumnPlan, table: _Table, parent_levels: Tuple[str, ...]
) -> Optional[PropertyPlan]:
    """The grouped property the run is scored on, when the statistic matches the declaration.

    A ``levelHistogram`` grouped by the parent, over the child's declared levels,
    or a grouped ``marginalHistogram`` over its bins. Written only when the
    profile's statistic lists exactly those levels and a group of every level the
    parent draws, so ``verify`` has a statistic to compare it with.
    """
    statistic = table.statistic
    if set(table.counts) != set(parent_levels):
        return None
    tolerance = {
        "type": "distance",
        "measure": "totalVariation",
        "max": DEFAULT_TOLERANCES["distance"],
    }
    if table.numeric:
        document: Dict[str, Any] = {
            "property": "marginalHistogram",
            "column": child.name,
            "groupBy": table.parent,
            "bins": list(statistic.get("bins", ())),
            "tolerance": tolerance,
        }
        bins = [
            Label(
                ("bins", str(position)),
                from_profile(table.tokens + ("bins", str(position)), _HISTOGRAM),
            )
            for position in range(len(document["bins"]))
        ]
    else:
        listed = {str(level) for level in statistic.get("levels", ())}
        if listed != set(table.cells) or "support" not in child.declaration:
            return None
        document = {
            "property": "levelHistogram",
            "column": child.name,
            "groupBy": table.parent,
            "tolerance": tolerance,
        }
        bins = []
    return PropertyPlan(
        document,
        [*bins, Label(("tolerance", "max"), assumed(TOLERANCE_REASON))],
        scorable=True,
        reads=[(table.tokens, "the grouped property is scored against it")],
    )


def choose_givens(
    columns: Sequence[ColumnPlan],
    kinds: Mapping[str, LogicalKind],
    entries: Mapping[str, Any],
    table_tokens: Tuple[str, ...],
    *,
    minimum_cell: int,
    determined: Optional[Set[str]] = None,
) -> None:
    """Draw columns given others where the profile's grouped statistics say how.

    *entries* are the profile's columns of the table, *table_tokens* where the
    table is (``/tables/<exposeId>``), and *determined* the columns a rule sets,
    which are never drawn given another nor another given them. A chosen table
    that matches the child's declaration also gives the child a grouped
    property, which the run is scored on.
    """
    determined = set(determined or ())
    plans = {plan.name: plan for plan in columns}
    tables: List[_Table] = []
    for name, plan in plans.items():
        entry = entries.get(name) if isinstance(entries, Mapping) else None
        statistics = entry.get("statistics") if isinstance(entry, Mapping) else None
        for index, statistic in enumerate(statistics if isinstance(statistics, list) else ()):
            if not isinstance(statistic, Mapping) or "groupBy" not in statistic:
                continue
            tokens = table_tokens + ("columns", name, "statistics", str(index))
            reason = _refusal(name, statistic, plans, kinds, determined)
            if reason is not None:
                plan.passed.append((tokens, reason))
                continue
            parent = str(statistic["groupBy"])
            cells = _child_cells(plan, statistic, kinds[name])
            if cells is None:
                plan.passed.append(
                    (tokens, "its cells are not the levels or bins the column is drawn over")
                )
                continue
            table = _table(
                parent,
                name,
                tokens,
                statistic,
                cells,
                kinds[name] not in _CATEGORICAL,
                minimum_cell,
            )
            if table is None:
                plan.passed.append((tokens, "no group of it is a table the draw can read"))
                continue
            tables.append(table)
    if not tables:
        return
    parents = {name for name, plan in plans.items() if _parent_levels(plan) is not None}
    categorical = {name for name in parents if name not in determined}
    leaves = {table.child for table in tables if table.child not in categorical}
    scored = [(_information(table), table.parent, table.child) for table in tables]
    edges = spanning_forest(scored, categorical=categorical, numeric=leaves)
    chosen: Dict[str, Tuple[str, _Table]] = {}
    for edge in edges:
        found = _best(tables, edge.parent, edge.child)
        if found is None:
            continue
        parent_levels = _parent_levels(plans[edge.parent])
        assert parent_levels is not None  # the forest names only parents that can be one
        drawn: Optional[_Table]
        if found.parent == edge.parent:
            drawn = found
            prop = _grouped_property(plans[edge.child], found, parent_levels)
        else:
            drawn, prop = _transposed(found, plans[edge.child].level_names or ()), None
        if drawn is None:
            continue
        _declare(plans[edge.child], plans[edge.parent], drawn, parent_levels)
        chosen[edge.child] = (edge.parent, found)
        plans[found.child].reads.append(
            (found.tokens, f"its group counts are the weights of {edge.child} given {edge.parent}")
        )
        if prop is not None:
            plans[edge.child].properties.append(prop)
    for table in tables:
        if any(found.tokens == table.tokens for _, found in chosen.values()):
            continue
        winner = chosen.get(table.child)
        if winner is not None:
            why = (
                f"{printable(table.child)} is drawn given {printable(winner[0])}, and a column "
                "is drawn given one other at most"
            )
        else:
            other = chosen.get(table.parent)
            why = (
                f"{printable(table.parent)} is itself drawn given {printable(other[0])}, and the "
                "forest the columns are drawn in keeps the link with more mutual information"
                if other is not None
                else "the forest the columns are drawn in keeps a link with more mutual "
                "information"
            )
        plans[table.child].passed.append((table.tokens, why))


def _best(tables: Sequence[_Table], one: str, other: str) -> Optional[_Table]:
    """The table of the pair with the most mutual information, the parent's own first."""
    pair = [table for table in tables if {table.parent, table.child} == {one, other}]
    if not pair:
        return None
    pair.sort(key=lambda table: (table.parent != one, -_information(table)))
    return pair[0]


def _refusal(
    name: str,
    statistic: Mapping[str, Any],
    plans: Mapping[str, ColumnPlan],
    kinds: Mapping[str, LogicalKind],
    determined: Set[str],
) -> Optional[str]:
    """Why a grouped statistic can be no ``given`` of the column, or ``None`` when it can."""
    parent = str(statistic.get("groupBy"))
    if name in determined:
        return "a rule of the contract sets this column, so it is not drawn"
    if parent in determined:
        return f"a rule of the contract sets {printable(parent)}, so no column is drawn given it"
    plan = plans[name]
    if plan.fixed is not None:
        return f"the column is {plan.fixed}, which is never drawn given another"
    other = plans.get(parent)
    if other is None or kinds.get(parent) not in _CATEGORICAL:
        return f"{printable(parent)} is not a categorical column of this table"
    if other.fixed is not None:
        return f"{printable(parent)} is {other.fixed}, so no column is drawn given it"
    if "nullFraction" in other.declaration:
        return (
            f"{printable(parent)} holds nulls, and the column another is drawn given holds "
            "none (FLX-3402)"
        )
    if other.level_names is None:
        return f"the profile does not publish the levels {printable(parent)} draws"
    groups = statistic.get("groups")
    keys = {
        str(group.get("group"))
        for group in (groups if isinstance(groups, list) else ())
        if isinstance(group, Mapping)
    }
    if not keys <= set(other.level_names):
        return f"its groups are not levels {printable(parent)} draws"
    return None
