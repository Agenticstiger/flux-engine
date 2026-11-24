# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Rows that hold fixed values, drawn exactly (ADR 0028 section 10).

A Simulation's ``conditions`` (:mod:`flux_engine.seam.conditions`) split an emit's rows into groups
in order, and each group fixes the values of some categorical columns. The rest of each row is
drawn from the joint the plan's conditionals describe, given those values: no rejection, no
approximation. The arithmetic is rational (:class:`fractions.Fraction`).

**The model.** Every categorical column of the plan without a declared null fraction is a variable
whose conditional is the one it draws with: its own levels (a root), or the row of its
``levelWeightsGiven`` that the levels of its ``given`` columns pick (ADR 0021 rule 3: in a clique
with separator ``S`` and new columns ``n_1 .. n_m``, ``n_j`` is drawn given ``S`` and
``n_1 .. n_(j-1)``, and the Chow-Liu forest of ADR 0014 is the same with each pair a clique). The
product of the conditionals is the joint.

**Bins.** A column drawn from ``bins`` with no null fraction is a variable too, its states its bins
named by the text of each lower edge (ADR 0036 decision 5), so a group that fixes a column drawn
given its bin changes the weights of the bins. A group still fixes categorical columns only. The
cell's first word chooses the bin from the group's table, and the place inside it is the
histogram's own draw at the index it always has, with the bin's pick left unread.

**The weights.** The columns are drawn in the plan's order. The weight of a value ``v`` of column
``c`` is its conditional given its parents times the sum, over the columns drawn after ``c`` that
descend from it, of the product of their conditionals, each fixed column held at its fixed value.
For a clique that is ``P(v | S, n_<j)`` times the sum over the clique's later new columns
``n_(j+1) .. n_m`` of their conditionals times the messages of the child cliques, and a child
clique ``D`` sends ``mu_D(sep_D)``, the same sum over all of ``D``'s new columns times its own
children's messages. Eliminating the columns from the last drawn to the first computes exactly
these sums (a message is the factor a column leaves behind when it is summed out), and the weights
of ``c`` are a function of the columns drawn before it that its sums depend on: for cliques, its
separator and the new columns before it, which are the columns it is drawn given. Values that leave
no cell above zero are refused at ``plan`` (``FLX-3646``), naming the group. The method is the
propagation of evidence in a junction tree (Dawid, 1992), in the pattern mbi follows when
constraints enter as potentials before it samples.

**The table.** The exact weights of a column's levels are normalised to shares. Levels are taken in
NFC order, each cumulative share is an exact fraction, and the threshold of a level is the smallest
whole number not below that fraction times ``2**53`` (the ceiling kernel 0.2.0 applies to its
Poisson thresholds); the last threshold is ``2**53``. A cell's 53-bit uniform is searched with
``bisect_right``: the level of the first threshold above it. A level with no weight takes no
threshold of its own and is never drawn. The per-row path and the column path build the same
thresholds from the same tables.

**The draws.** A drawn cell of a conditioned row draws on its own path at the index every cell of
its column draws at: the cell's first word, as a categorical column with no null fraction draws it
(KERNEL-SPEC-2 section 12). The group only chooses the table the uniform is searched in. A fixed
cell draws nothing. No seed-tree path is added, and a bundle without ``conditions`` is untouched.

**Rows.** A conditioned emit's rows are its groups' rows in order, so a row's group is a function
of its ordinal alone. ``--scale`` scales each group's count by the largest remainder
(:func:`scaled_counts`), and a shard takes the rows of its ordinals with their groups.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import product
from math import ceil
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Sequence,
    Tuple,
    Union,
)

import numpy as np

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import ErrorCode, FluxError, register
from flux_engine.core.rng import UNIFORM53_DENOMINATOR, nfc_sorted_items
from flux_engine.generator.families import Family
from flux_engine.generator.statistics import joint_level
from flux_engine.seam.conditions import (
    FLX_3641,
    FLX_3642,
    FLX_3643,
    ConditionGroup,
)
from flux_engine.seam.resolve import FLX_3105

if TYPE_CHECKING:
    from flux_engine.generator.rows import ColumnPlan, GeneratorPlan

__all__ = [
    "FLX_3646",
    "FLX_3647",
    "MOST_CELLS",
    "ConditionPlan",
    "scaled_counts",
    "thresholds_of",
    "with_conditions",
]

FLX_3646: ErrorCode = register(
    "FLX-3646",
    "a conditions group leaves no row possible",
    "The values a group fixes are checked against the joint the emit's declared conditionals "
    "describe, with the exact arithmetic that draws the rest of each row. When the values are "
    "impossible together, because a weight table gives a combination no weight or two fixed "
    "values need a combination the tables rule out, there is no row to draw and `plan` refuses "
    "the group, naming it and its values. Change the values, or the declared weights.",
)

FLX_3647: ErrorCode = register(
    "FLX-3647",
    "a conditions group cannot be drawn exactly from this emit's conditionals",
    "Exact conditioning sums out the columns a fixed column depends on, and the sum is held as a "
    "table. A column drawn given a column of a linked row has no conditional of its own to sum, "
    "and a sum over more than 1,048,576 cells is more than a plan holds. Either is refused at "
    "`plan`, naming the group and the column. Fix a column that is nearer the roots, or split "
    "the table's `given` lists so that fewer columns are drawn together.",
)

#: The most cells one table of the elimination may hold.
MOST_CELLS = 1 << 20

#: A table of weights, keyed by the levels of its columns.
_Factor = Tuple[Tuple[str, ...], Dict[Tuple[str, ...], Fraction]]
#: What a cell is searched in: its levels in NFC order and a threshold for each.
Table = Tuple[Tuple[str, ...], Tuple[int, ...]]
#: What a group decides for a column: its fixed level, its table, or nothing.
Decision = Union[str, Table, None]

_MEMO_GROUPS = 4096


def thresholds_of(weights: Mapping[str, Fraction]) -> Table:
    """The table a cell is searched in, from the exact *weights* of its levels.

    The weights are normalised to shares. Levels with a share are taken in NFC order, and the
    threshold of each is ``ceil(cumulative share * 2**53)`` on the exact fraction; the last is
    ``2**53`` whatever the ceiling would give. Raises ``ValueError`` when no level has weight.
    """
    live = [(level, Fraction(weight)) for level, weight in nfc_sorted_items(weights) if weight > 0]
    total = sum((weight for _, weight in live), Fraction(0))
    if not live or total <= 0:
        raise ValueError("a table with no weight")
    thresholds: List[int] = []
    cumulative = Fraction(0)
    for _, weight in live[:-1]:
        cumulative += weight / total
        thresholds.append(ceil(cumulative * UNIFORM53_DENOMINATOR))
    thresholds.append(UNIFORM53_DENOMINATOR)
    return tuple(level for level, _ in live), tuple(thresholds)


def level_at(table: Table, uniform: int) -> str:
    """The level whose threshold is the first above *uniform* (``bisect_right``)."""
    levels, thresholds = table
    return levels[bisect.bisect_right(thresholds, uniform)]


def scaled_counts(counts: Sequence[int], total: int) -> Tuple[int, ...]:
    """*counts* scaled so that they add up to *total*, by the largest remainder.

    Each count's exact share of *total* is cut down to a whole number, and the rows that are left
    go one each to the counts with the largest remainders, the earlier group first on a tie. So
    the result adds up to *total* and no count is more than one away from its exact share.
    """
    if total < 0 or any(count < 0 for count in counts):
        raise ValueError("counts and total are not negative")
    base = sum(counts)
    if not counts:
        return ()
    if base == 0:
        raise ValueError("counts add up to nothing")
    quotas = [Fraction(count * total, base) for count in counts]
    floors = [quota.numerator // quota.denominator for quota in quotas]
    left = total - sum(floors)
    ranked = sorted(range(len(counts)), key=lambda i: (-(quotas[i] - floors[i]), i))
    for index in ranked[:left]:
        floors[index] += 1
    return tuple(floors)


# ---------------------------------------------------------------------------
# Factors
# ---------------------------------------------------------------------------


def _multiply(
    factors: Sequence[_Factor], domains: Mapping[str, Tuple[str, ...]], position: Mapping[str, int]
) -> _Factor:
    """The product of *factors*, over the union of their columns in draw order."""
    union = sorted({name for names, _ in factors for name in names}, key=position.__getitem__)
    size = 1
    for name in union:
        size *= len(domains[name])
        if size > MOST_CELLS:
            raise OverflowError(union)
    slots = [[union.index(name) for name in names] for names, _ in factors]
    result: Dict[Tuple[str, ...], Fraction] = {}
    for combo in product(*(domains[name] for name in union)):
        weight = Fraction(1)
        for (_, table), slot in zip(factors, slots, strict=True):
            part = table.get(tuple(combo[i] for i in slot))
            if part is None:
                weight = Fraction(0)
                break
            weight *= part
        if weight:
            result[combo] = weight
    return tuple(union), result


def _reduce(factor: _Factor, evidence: Mapping[str, str]) -> _Factor:
    """*factor* with each column of *evidence* held at its value and dropped."""
    names, table = factor
    keep = [i for i, name in enumerate(names) if name not in evidence]
    held = [(i, evidence[name]) for i, name in enumerate(names) if name in evidence]
    reduced = {
        tuple(key[i] for i in keep): weight
        for key, weight in table.items()
        if all(key[i] == level for i, level in held)
    }
    return tuple(names[i] for i in keep), reduced


@dataclass
class _Solution:
    """What one group's values leave of each column: a table for every scope the weights read.

    ``tables[name]`` is ``(scope, {levels of scope: table})``. A column with no entry is not
    changed by the group, and draws as it would without one. ``mass`` is the probability of the
    group's values under the plan's conditionals.
    """

    mass: Fraction
    tables: Dict[str, Tuple[Tuple[str, ...], Dict[Tuple[str, ...], Table]]]


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionPlan:
    """The groups of a conditioned emit, and what they need to draw its rows.

    ``groups`` carry their row counts, so a row's group is read from the cumulative ``ends``.
    ``variables`` are the plan's categorical columns without a null fraction, the columns a group
    may fix and the ones the weights are summed over; ``order`` is the plan's draw order of all its
    columns. ``touched`` holds every column some group fixes or changes the weights of: a column
    outside it is drawn as it would be with no conditions. What is derived from these is kept per
    process (:attr:`_cache`) and never pickled.
    """

    groups: Tuple[ConditionGroup, ...]
    ends: Tuple[int, ...]
    order: Tuple[str, ...] = field(compare=False)
    variables: Tuple["ColumnPlan", ...] = field(compare=False, repr=False)
    touched: FrozenSet[str] = field(compare=False)
    _cache: Dict[Any, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __getstate__(self) -> Dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if key != "_cache"}

    def __setstate__(self, state: Mapping[str, Any]) -> None:
        for key, value in state.items():
            object.__setattr__(self, key, value)
        object.__setattr__(self, "_cache", {})

    @property
    def rows(self) -> int:
        """The rows the groups cover."""
        return self.ends[-1] if self.ends else 0

    def as_document(self) -> Dict[str, Any]:
        """The groups as the plan document records them."""
        return {
            "groups": [
                {"values": dict(group.values), "count": group.count} for group in self.groups
            ]
        }

    def group_of(self, row_index: int) -> int:
        """The group the row at *row_index* belongs to: a function of the ordinal alone."""
        return bisect.bisect_right(self.ends, row_index)

    def fixed(self, group: int) -> Mapping[str, str]:
        """The levels group *group* fixes, by column."""
        return dict(self.groups[group].values)

    # -- the model --------------------------------------------------------

    def _model(self) -> Tuple[Dict[str, "ColumnPlan"], Dict[str, Tuple[str, ...]], Dict[str, int]]:
        found = self._cache.get("model")
        if found is None:
            from flux_engine.generator.rows import class_names

            by_name = {plan.column.name: plan for plan in self.variables}
            domains = {name: tuple(sorted(class_names(plan))) for name, plan in by_name.items()}
            position = {name: index for index, name in enumerate(self.order)}
            found = (by_name, domains, position)
            self._cache["model"] = found
        return found

    def _base(self, name: str) -> _Factor:
        """The conditional of column *name*: its parents then itself, weights above zero only."""
        cached = self._cache.get(("base", name))
        if cached is not None:
            return cached
        by_name, domains, _ = self._model()
        statistics = by_name[name].statistics
        parents = statistics.parents
        if is_binned(by_name[name]):
            table = _bin_table(by_name[name], domains)
        elif statistics.level_weights_given is not None:
            table = _given_table(statistics, domains)
        else:
            table = _own_table(statistics)
        factor: _Factor = ((*parents, name), table)
        self._cache[("base", name)] = factor
        return factor

    def _relevant(self, fixed: Mapping[str, str]) -> List[str]:
        """The fixed columns and their ancestors, in draw order."""
        by_name, _, position = self._model()
        found: set[str] = set()
        pending = list(fixed)
        while pending:
            name = pending.pop()
            if name not in found:
                found.add(name)
                pending.extend(by_name[name].statistics.parents)
        return sorted(found, key=position.__getitem__)

    def relevant_to(self, group: int) -> FrozenSet[str]:
        """The columns group *group* fixes or conditions: its fixed columns and their ancestors."""
        return frozenset(self._relevant(self.fixed(group)))

    def solution(self, group: int) -> _Solution:
        """The group's tables, from the exact elimination; kept for the values it fixes."""
        values = self.groups[group].values
        kept = self._cache.setdefault("solutions", {})
        found = kept.get(values)
        if found is None:
            if len(kept) >= _MEMO_GROUPS:
                kept.clear()
            found = self._solve(group, values)
            kept[values] = found
        return found

    def _solve(self, group: int, values: Tuple[Tuple[str, str], ...]) -> _Solution:
        by_name, domains, position = self._model()
        fixed = dict(values)
        relevant = self._relevant(fixed)
        for name in relevant:
            if by_name[name].statistics.given_link is not None:
                raise FluxError(
                    FLX_3647,
                    f"conditions group {group} ({_shown(values)}): the column {name!r} is drawn "
                    "given a column of a linked row, so it has no conditional of its own to sum",
                )
        factors = [_reduce(self._base(name), fixed) for name in relevant]
        tables: Dict[str, Tuple[Tuple[str, ...], Dict[Tuple[str, ...], Table]]] = {}
        for name in reversed(relevant):
            if name in fixed:
                continue
            bucket = [factor for factor in factors if name in factor[0]]
            factors = [factor for factor in factors if name not in factor[0]]
            try:
                joint = _multiply(bucket, domains, position)
            except OverflowError:
                raise FluxError(
                    FLX_3647,
                    f"conditions group {group} ({_shown(values)}): summing out {name!r} holds "
                    f"more than {MOST_CELLS} cells",
                ) from None
            scope = tuple(joint[0][:-1])
            by_scope: Dict[Tuple[str, ...], Dict[str, Fraction]] = {}
            for combo, weight in joint[1].items():
                by_scope.setdefault(combo[:-1], {})[combo[-1]] = weight
            tables[name] = (scope, {key: thresholds_of(row) for key, row in by_scope.items()})
            factors.append(
                (scope, {key: sum(row.values(), Fraction(0)) for key, row in by_scope.items()})
            )
        mass = Fraction(1)
        for _, table in factors:
            mass *= table.get((), Fraction(0))
        if mass <= 0:
            raise FluxError(
                FLX_3646,
                f"conditions group {group} ({_shown(values)}) leaves no row possible: the "
                "declared conditionals give these values together no weight",
            )
        return _Solution(mass=mass, tables=tables)

    # -- the draws --------------------------------------------------------

    def decide(self, row_index: int, name: str, levels: Mapping[str, str]) -> Decision:
        """What the row's group decides for column *name*, given the levels drawn before it.

        The fixed level (a string), the table its uniform is searched in, or ``None`` when the
        group leaves the column as it would be drawn with no conditions. *levels* holds the level
        each column drawn so far took in the row.
        """
        group = self.group_of(row_index)
        fixed = self.groups[group].values
        for column, level in fixed:
            if column == name:
                return level
        entry = self.solution(group).tables.get(name)
        if entry is None:
            return None
        scope, tables = entry
        return self._lookup(group, name, tables, tuple(levels[column] for column in scope))

    def _lookup(
        self, group: int, name: str, tables: Dict[Tuple[str, ...], Table], key: Tuple[str, ...]
    ) -> Table:
        found = tables.get(key)
        if found is None:
            raise FluxError(
                FLX_3646,
                f"conditions group {group}: column {name!r} was asked for a table at {key}, "
                "which the group's values leave no weight",
            )
        return found

    def column_levels(
        self,
        name: str,
        rows: "np.ndarray",
        uniforms: "np.ndarray",
        levels_of: Callable[[str], List[str]],
    ) -> Dict[int, str]:
        """The level each of *rows* takes for *name* where its group decides one, by position.

        *uniforms* are the rows' first words of the column's own stream, and *levels_of* gives the
        levels a column took over the same rows. Rows whose group leaves the column alone are not
        in the result. Each row's level is the one :meth:`decide` and :func:`level_at` give it.
        """
        found: Dict[int, str] = {}
        groups = np.searchsorted(np.array(self.ends, dtype=np.int64), rows, side="right")
        for group in np.unique(groups).tolist():
            positions = np.nonzero(groups == group)[0]
            fixed = dict(self.groups[group].values)
            if name in fixed:
                for position in positions.tolist():
                    found[position] = fixed[name]
                continue
            entry = self.solution(group).tables.get(name)
            if entry is None:
                continue
            scope, tables = entry
            scoped = [levels_of(column) for column in scope]
            by_key: Dict[Tuple[str, ...], List[int]] = {}
            for position in positions.tolist():
                by_key.setdefault(tuple(levels[position] for levels in scoped), []).append(position)
            for key, taken in by_key.items():
                names, thresholds = self._lookup(group, name, tables, key)
                bounds = np.array(thresholds[:-1], dtype=np.uint64)
                chosen = np.searchsorted(bounds, uniforms[taken], side="right").tolist()
                for position, index in zip(taken, chosen, strict=True):
                    found[position] = names[index]
        return found


def _own_table(statistics: Any) -> Dict[Tuple[str, ...], Fraction]:
    """A column's own weights as a table over its levels, the weights above zero only."""
    from flux_engine.generator.rows import _column_levels

    weights = _column_levels(statistics)
    total = sum((Fraction(weight) for weight in weights.values()), Fraction(0))
    return {(level,): Fraction(weight) / total for level, weight in weights.items() if weight > 0}


def is_binned(plan: "ColumnPlan") -> bool:
    """Whether the column is drawn from bins (:func:`flux_engine.generator.rows.is_binned`)."""
    from flux_engine.generator.rows import is_binned as _is_binned

    return _is_binned(plan)


def _bin_table(
    plan: "ColumnPlan", domains: Mapping[str, Tuple[str, ...]]
) -> Dict[Tuple[str, ...], Fraction]:
    """A column drawn from bins as a table over its parents' classes then its own bin's class.

    The weights are its ``binWeights``, or the list of ``binWeightsGiven`` its parents' classes
    pick, one weight per bin; a bin is named by the text of its lower edge (ADR 0036 decision 3).
    """
    from flux_engine.generator.rows import class_names

    statistics = plan.statistics
    names = class_names(plan)
    table: Dict[Tuple[str, ...], Fraction] = {}
    if statistics.bin_weights_given is None:
        rows: List[Tuple[Tuple[str, ...], Sequence[Any]]] = [((), statistics.bin_weights or ())]
    else:
        rows = []
        for combo in product(*(domains[parent] for parent in statistics.parents)):
            key = combo[0] if len(combo) == 1 else joint_level(combo)
            rows.append((combo, statistics.bin_weights_given[key]))
    for combo, weights in rows:
        total = sum((Fraction(weight) for weight in weights), Fraction(0))
        for name, weight in zip(names, weights, strict=True):
            if weight > 0:
                table[(*combo, name)] = Fraction(weight) / total
    return table


def _given_table(
    statistics: Any, domains: Mapping[str, Tuple[str, ...]]
) -> Dict[Tuple[str, ...], Fraction]:
    """A column's weights given its parents, as a table over the parents' levels then its own."""
    table: Dict[Tuple[str, ...], Fraction] = {}
    for combo in product(*(domains[parent] for parent in statistics.parents)):
        key = combo[0] if len(combo) == 1 else joint_level(combo)
        row = statistics.level_weights_given[key]
        total = sum((Fraction(weight) for weight in row.values()), Fraction(0))
        for level, weight in row.items():
            if weight > 0:
                table[(*combo, level)] = Fraction(weight) / total
    return table


def _shown(values: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(f"{column}={level}" for column, level in values)


def with_conditions(
    plan: "GeneratorPlan", groups: Sequence[ConditionGroup], *, where: str = ""
) -> "GeneratorPlan":
    """*plan* with its emit's *groups*, checked, and every group's values solved.

    Refused: a plan that is not kernel 0.2.0's (``FLX-3105``), counts that are not the plan's rows
    (``FLX-3641``), a column that is not a categorical column without nulls and a value that is
    not one of its levels (``FLX-3642``, ``FLX-3643``), a group whose values leave no row possible
    (``FLX-3646``) and one that cannot be summed (``FLX-3647``). A group without a count covers
    every row. Every distinct set of values is solved here, so what ``plan`` accepts is drawn.
    """
    from dataclasses import replace

    prefix = f"{where}: " if where else ""
    if not groups:
        return plan
    if not plan.counter_based:
        raise FluxError(
            FLX_3105, f"{prefix}conditions generate on kernel 0.2.0 only; this plan is kernel 0.1.0"
        )
    counted = _counted_groups(plan, groups, prefix)
    variables = tuple(
        column_plan
        for column_plan in plan.columns
        if (column_plan.family is Family.CATEGORICAL or is_binned(column_plan))
        and column_plan.statistics.null_fraction is None
    )
    _check_group_values(counted, variables, prefix)
    from flux_engine.generator.rows import _draw_order

    order = tuple(plan.columns[i].column.name for i in _draw_order(plan))
    ends: List[int] = []
    running = 0
    for group in counted:
        running += group.count or 0
        ends.append(running)
    conditions = ConditionPlan(
        groups=tuple(counted),
        ends=tuple(ends),
        order=order,
        variables=variables,
        touched=frozenset(),
    )
    object.__setattr__(conditions, "touched", _solve_groups(conditions, counted))
    conditions._cache.pop("solutions", None)
    return replace(plan, conditions=conditions)


def _counted_groups(
    plan: "GeneratorPlan", groups: Sequence[ConditionGroup], prefix: str
) -> List[ConditionGroup]:
    """*groups* with the count each covers, or ``FLX-3641`` when the counts are not the rows."""
    if len(groups) == 1 and groups[0].count is None:
        return [ConditionGroup(values=groups[0].values, count=plan.rows)]
    counted = list(groups)
    total = sum(group.count or 0 for group in counted)
    if any(group.count is None for group in counted) or total != plan.rows:
        raise FluxError(
            FLX_3641,
            f"{prefix}the conditions count {total} rows and the plan has {plan.rows}; the "
            "groups' counts are the emit's rows",
        )
    return counted


def _check_group_values(
    counted: Sequence[ConditionGroup], variables: Sequence["ColumnPlan"], prefix: str
) -> None:
    """Refuse a fixed column that is not one of *variables* and a value that is not its level."""
    from flux_engine.generator.rows import _level_names

    by_name = {
        column_plan.column.name: column_plan
        for column_plan in variables
        if column_plan.family is Family.CATEGORICAL
    }
    for index, group in enumerate(counted):
        for column, level in group.values:
            plan_of = by_name.get(nfc(column))
            if plan_of is None:
                raise FluxError(
                    FLX_3643,
                    f"{prefix}conditions group {index}: {column!r} is not a categorical column "
                    "of the emit that draws without nulls",
                )
            if level not in _level_names(plan_of.statistics):
                raise FluxError(
                    FLX_3642,
                    f"{prefix}conditions group {index}: {level!r} is not a level of {column!r}",
                )


def _solve_groups(conditions: ConditionPlan, counted: Sequence[ConditionGroup]) -> FrozenSet[str]:
    """Solve every distinct set of values that covers rows; the columns the groups touch."""
    touched: set[str] = set()
    seen: set[Tuple[Tuple[str, str], ...]] = set()
    for index, group in enumerate(counted):
        if group.values in seen or not group.count:
            continue
        seen.add(group.values)
        touched.update(conditions.relevant_to(index))
        conditions.solution(index)
    return frozenset(touched)
