# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Noisy clique marginals become columns drawn given their separators (PROFILE-SPEC.md 5a).

A profile released under differential privacy can carry the joint noisy counts of cliques of
categorical columns. ``fluxsim init`` arranges the cliques of a table into a junction tree
(:mod:`flux_engine.calibrate.junction`) and writes each column of it drawn given its separator
columns, so the chain of conditionals reproduces the joint the cliques describe. The Chow-Liu
forest of :mod:`flux_engine.author.given` is the degree-one case: a clique of two columns is an
edge, and its column is drawn given the one it shares.

**The rule that makes a conditional a distribution.** The column ``c`` of a clique, drawn given
the columns ``S`` (the clique's separator and the clique's columns drawn before ``c``, in the
table's declared order), takes for each combination of levels ``s`` of ``S`` the vector of noisy
counts of ``(s, c)``, summed over the clique's columns outside ``S`` and ``c``, and makes it a
distribution by :func:`flux_stats.noise.point_estimate`: projected onto the non-negative vectors
that add up to its noisy total, then normalised. A vector with no mass (a total not above zero)
has equal shares, said so in the bundle as an assumption. The first column of a root clique has no
``S`` and takes the whole marginal. Every number is exact.

A clique of the first definition holds categorical columns. The levels it lists for a column are the
levels the column draws: they must be the contract's levels when the contract declares them (an
``enum``).

**Binned columns** (``cliqueMarginal/2``, ADR 0036). A number or timestamp column of a clique lists
the edges of its bins, and a bin is one of its levels: the junction tree arranges it as it arranges
any column, and the column is drawn from ``bins`` and ``binWeights``, or ``binWeightsGiven`` given
its separator, over the clique's edges. A column drawn given a binned column reads that column's
bin, keyed by the text of the bin's lower edge as the column's own ``bins`` write it. The clique's
edges win over the column's own histogram, which the ledger passes over, and the column's range is
the span of the edges. Init refuses a clique whose edges do not cover the range the contract
declares for the column (``FLX-3402``). A binned column is drawn from a clique only, never as a
parent of the Chow-Liu forest. Estimated cliques are refused by the profile reader.

Nothing here reads a record. It reads counts a profiler released, and what it writes is
post-processing of them.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from itertools import product
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.author.basis import Label, assumed, from_profile
from flux_engine.author.columns import (
    DEFAULT_TOLERANCES,
    TOLERANCE_REASON,
    ColumnContext,
    ColumnPlan,
    PropertyPlan,
    _spell,
    share_texts,
)
from flux_engine.author.ranges import grid_bounds
from flux_engine.calibrate.junction import JunctionStep, junction_tree
from flux_engine.core.errors import FluxError
from flux_engine.generator.statistics import FLX_3402
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.coverage import printable
from flux_stats.cliques import CliqueEntry, edge_text
from flux_stats.noise import point_estimate

__all__ = ["draw_cliques"]

_CATEGORICAL = (LogicalKind.STRING, LogicalKind.BOOL)
_NUMERIC = (LogicalKind.INT, LogicalKind.DECIMAL, LogicalKind.DATE, LogicalKind.TIMESTAMP)

#: Why a combination of parent levels with no mass draws equal shares.
_NO_MASS_REASON = (
    "The noisy counts of this combination of levels add up to nothing above zero, so the column "
    "takes equal shares of its levels here."
)
#: The choice that reads noisy counts as a distribution, stated once for each drawn column.
_POINT_ESTIMATE = (
    "The clique's counts are noisy, so each row of weights is their point estimate: projected "
    "onto the non-negative vectors that add up to the row's noisy total, then normalised."
)
#: Why a column's own histogram is passed over when a clique draws the column.
_OWN_HISTOGRAM = (
    "a clique draws this column over its own bins, which win over the column's histogram"
)
#: Why a clique with a binned column is passed over where a plan has no column given a bin.
_NO_BINNED_PARENT = (
    "a new plan runs kernel 0.1.0, which draws no column given a bin, so a clique that holds a "
    "number or timestamp column is not drawn"
)


def _refuse(message: str) -> FluxError:
    return FluxError(FLX_3402, message)


def _tokens(expose_id: str, clique: CliqueEntry) -> Tuple[str, ...]:
    return ("tables", expose_id, "cliques", str(clique.index))


def draw_cliques(
    columns: Sequence[ColumnPlan],
    kinds: Mapping[str, LogicalKind],
    cliques: Sequence[CliqueEntry],
    expose_id: str,
    *,
    order: Sequence[str],
    determined: Set[str],
    contexts: Optional[Mapping[str, ColumnContext]] = None,
    binned_parents: bool = True,
) -> Set[str]:
    """Declare every column of *cliques* drawn given its separators; the columns it wrote.

    *columns* are the expose's column plans, *kinds* their logical kinds, *order* the declared
    column order and *determined* the columns a contract rule sets. *contexts* are what each
    column was declared from (its range and grid), and *binned_parents* is whether the plan a new
    bundle gets draws a column given a bin: without it a clique that holds a binned column is
    passed over. Raises ``FLX-3402`` when the cliques cannot be drawn: a column the contract does
    not declare or one that cannot be drawn given others, levels that are not the contract's, bins
    that do not cover the range the contract declares, or cliques with no junction tree.
    """
    plans = {plan.name: plan for plan in columns}
    if not binned_parents:
        cliques = _without_binned(plans, cliques, expose_id)
    if not cliques:
        return set()
    labels = [f"/{'/'.join(_tokens(expose_id, clique))}" for clique in cliques]
    for clique in cliques:
        for name in clique.columns:
            _check_column(plans, kinds, determined, name, clique, (contexts or {}).get(name))
    steps = junction_tree(
        [clique.columns for clique in cliques],
        order=[name for name in order if name in plans],
        names=labels,
    )
    drawn: Set[str] = set()
    for step in steps:
        clique = cliques[step.clique]
        for position, name in enumerate(step.new):
            _draw(plans[name], plans, kinds, clique, step, position, order, expose_id)
            drawn.add(name)
    _properties(plans, cliques, kinds, expose_id)
    return drawn


def _without_binned(
    plans: Mapping[str, ColumnPlan], cliques: Sequence[CliqueEntry], expose_id: str
) -> List[CliqueEntry]:
    """The cliques that hold no binned column; the others are passed over, each with why."""
    kept: List[CliqueEntry] = []
    for clique in cliques:
        if not any(clique.binned(name) for name in clique.columns):
            kept.append(clique)
            continue
        holder = next((plans[name] for name in clique.columns if name in plans), None)
        if holder is not None:
            holder.passed.append((_tokens(expose_id, clique), _NO_BINNED_PARENT))
    return kept


def _check_column(
    plans: Mapping[str, ColumnPlan],
    kinds: Mapping[str, LogicalKind],
    determined: Set[str],
    name: str,
    clique: CliqueEntry,
    context: Optional[ColumnContext],
) -> None:
    where = f"{clique.label}: column {printable(name)!r}"
    plan = plans.get(name)
    if plan is None or name not in kinds:
        raise _refuse(f"{where} is not a column of the contract's expose")
    numeric = _check_kind(where, kinds[name], clique.binned(name))
    _check_plan(where, plan, name in determined, numeric)
    if numeric:
        _check_edges(where, kinds[name], clique, name, context)
        return
    _check_levels(where, plan, clique, name, kinds[name])


def _check_kind(where: str, kind: LogicalKind, binned: bool) -> bool:
    """Whether the column is a number or timestamp one; refuse a kind a clique cannot hold or list so."""
    numeric = kind in _NUMERIC
    if not numeric and kind not in _CATEGORICAL:
        raise _refuse(f"{where} is not a categorical, number or timestamp column")
    if numeric and not binned:
        raise _refuse(f"{where} is a number or timestamp column, and a clique lists its bins")
    if not numeric and binned:
        raise _refuse(f"{where} lists bins, and is not a number or timestamp column")
    return numeric


def _check_plan(where: str, plan: ColumnPlan, determined: bool, numeric: bool) -> None:
    """Refuse a column that a rule sets, that is never drawn given another, or that holds nulls."""
    if determined:
        raise _refuse(f"{where} is set by a rule of the contract, so it is not drawn")
    if plan.fixed is not None:
        raise _refuse(f"{where} is {plan.fixed}, which is never drawn given another")
    if plan.int_levels:
        raise _refuse(f"{where} is drawn as levels, and a clique lists its bins")
    if "nullFraction" in plan.declaration and not (
        numeric and Decimal(str(plan.declaration["nullFraction"])) == 0
    ):
        raise _refuse(f"{where} holds nulls, and a clique column holds none (FLX-3402)")


def _check_levels(
    where: str, plan: ColumnPlan, clique: CliqueEntry, name: str, kind: LogicalKind
) -> None:
    """A categorical column lists the levels its contract declares, or takes them when it has none."""
    levels = clique.levels_of(name)
    if plan.level_names is not None:
        if set(plan.level_names) != set(levels):
            raise _refuse(
                f"{where} lists levels that are not the levels the contract declares "
                f"({', '.join(printable(level) for level in plan.level_names)})"
            )
    elif kind is LogicalKind.STRING:
        _adopt_levels(plan, clique, levels)
    else:
        raise _refuse(f"{where} is a boolean whose levels are not false and true")


def _spelled_edges(kind: LogicalKind, clique: CliqueEntry, name: str) -> List[str]:
    """The edges of *name* in *clique* as the fidelity block spells a bin of a column of *kind*."""
    return [str(_spell(kind, Decimal(edge_text(edge)))) for edge in clique.edges_of(name)]


def _check_edges(
    where: str,
    kind: LogicalKind,
    clique: CliqueEntry,
    name: str,
    context: Optional[ColumnContext],
) -> None:
    """Refuse (``FLX-3402``) edges that the column's contract and grid cannot draw over."""
    edges = clique.edges_of(name)
    spelled = _spelled_edges(kind, clique, name)
    if len(set(spelled)) != len(spelled):
        raise _refuse(
            f"{where} lists bins that the fidelity block spells as one edge, so they are "
            "not strictly increasing"
        )
    if context is None:
        return
    if any((edge * (1 << context.fractional_bits)).denominator != 1 for edge in edges):
        raise _refuse(f"{where} lists an edge that does not land on the profile's grid")
    rule = context.range_rule
    if rule is None or kind not in (LogicalKind.INT, LogicalKind.DECIMAL):
        return
    scale = context.column.logical_type.scale or 0
    held = grid_bounds(rule, Fraction(1, 10**scale))
    if held is None:
        return
    low, high = held
    if (low is not None and edges[0] > low) or (high is not None and edges[-1] < high):
        raise _refuse(
            f"{where} lists bins that do not cover the range the contract declares for the "
            "column, so a value it may hold would lie in no bin"
        )


def _adopt_levels(plan: ColumnPlan, clique: CliqueEntry, levels: Tuple[str, ...]) -> None:
    """A string column the contract does not enumerate takes its levels from the clique."""
    for key in ("levels", "support", "maxCardinality"):
        plan.declaration.pop(key, None)
    plan.labels = [label for label in plan.labels if label.tokens[:1] not in (("levels",),)]
    plan.assumptions = [text for text in plan.assumptions if text not in plan.weight_notes]
    plan.weight_notes = []
    plan.level_names = levels
    plan.declaration["support"] = {"values": list(levels)}
    plan.declaration["maxCardinality"] = len(levels)
    plan.labels.append(
        Label(
            ("maxCardinality",),
            from_profile(
                ("tables", clique.expose_id, "cliques", str(clique.index), "levels"),
                clique.definition,
            ),
        )
    )


def _class_keys(
    kinds: Mapping[str, LogicalKind], clique: CliqueEntry, name: str
) -> Tuple[str, ...]:
    """The classes a column draws, as a column given it keys its weights: levels, or lower edges.

    A binned column's class is its bin, keyed by the text of the bin's lower edge as the column's
    own ``bins`` write it (SEAM-FIDELITY-SPEC.md section 2).
    """
    if clique.binned(name):
        return tuple(_spelled_edges(kinds[name], clique, name)[:-1])
    return clique.levels_of(name)


def _drop_own_draw(plan: ColumnPlan, expose_id: str) -> List[Tuple[str, ...]]:
    """Take a column's own distribution off it; the statistics of the profile it came from.

    The declaration's bins, weights, range, mean and spread go, with their labels, the properties
    of its own histogram, and a zero ``nullFraction``, which a column drawn from a clique does
    not declare. Returns the pointers of the profile's statistics those numbers were read from.
    """
    gone = ("bins", "binWeights", "range", "mean", "stddev", "nullFraction")
    read: List[Tuple[str, ...]] = []
    for label in plan.labels:
        found = label.basis.statistic
        if label.tokens[:1] and label.tokens[0] in gone and found is not None:
            tokens = found[0]
            if (
                len(tokens) >= 6
                and tokens[:2] == ("tables", expose_id)
                and tokens[4] == "statistics"
            ):
                if tokens[:6] not in read:
                    read.append(tokens[:6])
    for key in gone:
        plan.declaration.pop(key, None)
    plan.labels = [label for label in plan.labels if label.tokens[:1] not in [(k,) for k in gone]]
    plan.properties = [
        prop for prop in plan.properties if prop.document["property"] != "marginalHistogram"
    ]
    plan.assumptions = [text for text in plan.assumptions if text not in plan.weight_notes]
    plan.weight_notes = []
    return read


def _draw(
    plan: ColumnPlan,
    plans: Mapping[str, ColumnPlan],
    kinds: Mapping[str, LogicalKind],
    clique: CliqueEntry,
    step: JunctionStep,
    position: int,
    order: Sequence[str],
    expose_id: str,
) -> None:
    """Write column *plan* of *step*: its levels or bins, or its weights given the columns before it."""
    name = plan.name
    given = step.parents_of(position, order)
    levels = clique.levels_of(name)
    binned = clique.binned(name)
    marginal = clique.marginal([*given, name])
    source = _tokens(expose_id, clique) + ("noisyCounts",)
    basis = from_profile(source, clique.definition)
    if binned:
        passed = _drop_own_draw(plan, expose_id)
        for tokens in passed:
            plan.passed.append((tokens, _OWN_HISTOGRAM))
    else:
        plan.declaration.pop("levels", None)
        plan.labels = [label for label in plan.labels if label.tokens[:1] != ("levels",)]
        plan.assumptions = [text for text in plan.assumptions if text not in plan.weight_notes]
        plan.weight_notes = []
    rows: Dict[Tuple[str, ...], List[str]] = {}
    empty: List[Tuple[str, ...]] = []
    for combination in product(*(clique.levels_of(parent) for parent in given)):
        values = [marginal[(*combination, level)] for level in levels]
        estimate = point_estimate(values)
        rows[combination] = share_texts(list(estimate.shares))
        if estimate.total <= 0:
            empty.append(combination)
    if binned:
        _declare_bins(plan, kinds, clique, expose_id)
    keys = [_class_keys(kinds, clique, parent) for parent in given]
    positions = {parent: clique.levels_of(parent) for parent in given}
    classes = {
        parent: dict(zip(positions[parent], keys[index], strict=True))
        for index, parent in enumerate(given)
    }
    if not given:
        (texts,) = rows.values()
        if binned:
            plan.declaration["binWeights"] = list(texts)
            for index in range(len(texts)):
                plan.labels.append(Label(("binWeights", str(index)), basis))
        else:
            plan.declaration["levels"] = dict(zip(levels, texts, strict=True))
            for level in levels:
                plan.labels.append(Label(("levels", level), basis))
        plan.how = (
            f"its {'bins' if binned else 'levels'}, from the noisy counts of clique "
            f"{printable(', '.join(clique.columns))}"
        )
    else:
        table: Dict[str, Any] = {}
        weights_key = "binWeightsGiven" if binned else "levelWeightsGiven"
        for combination, texts in rows.items():
            named = tuple(
                classes[parent][level] for parent, level in zip(given, combination, strict=True)
            )
            node = table
            for key in named[:-1]:
                node = node.setdefault(key, {})
            if binned:
                node[named[-1]] = list(texts)
            else:
                node[named[-1]] = dict(zip(levels, texts, strict=True))
            for index, level in enumerate(levels):
                tail = str(index) if binned else level
                plan.labels.append(
                    Label(
                        (weights_key, *named, tail),
                        assumed(_NO_MASS_REASON) if combination in empty else basis,
                    )
                )
        # One parent is a name, as it always was; several are a list, the order the tables nest in.
        plan.declaration["given"] = given[0] if len(given) == 1 else list(given)
        plan.declaration[weights_key] = table
        plan.how = (
            f"its {'bins' if binned else 'levels'}, drawn given "
            f"{printable(', '.join(given))} from the noisy counts of "
            f"clique {printable(', '.join(clique.columns))}"
        )
        if empty:
            plan.assumptions.append(
                "Drawn given "
                f"{printable(', '.join(given))}: for {len(empty)} combination"
                f"{'s' if len(empty) != 1 else ''} of its levels the noisy counts add up to "
                "nothing above zero, so the column takes equal shares there."
            )
    plan.assumptions.append(_POINT_ESTIMATE)
    plan.choices.append(_POINT_ESTIMATE)
    plan.reads.append(
        (
            _tokens(expose_id, clique),
            f"its counts are the weights of {printable(name)}"
            + (f" given {printable(', '.join(given))}" if given else ""),
        )
    )


def _declare_bins(
    plan: ColumnPlan, kinds: Mapping[str, LogicalKind], clique: CliqueEntry, expose_id: str
) -> None:
    """Declare a binned column's ``bins`` and ``range`` from the clique's edges, each cited."""
    name = plan.name
    spelled = _spelled_edges(kinds[name], clique, name)
    at = clique.columns.index(name)
    domain = _tokens(expose_id, clique) + ("domains", str(at), "bins")
    plan.declaration["bins"] = list(spelled)
    plan.declaration["range"] = {"min": spelled[0], "max": spelled[-1]}
    for index in range(len(spelled)):
        plan.labels.append(
            Label(("bins", str(index)), from_profile(domain + (str(index),), clique.definition))
        )
    plan.labels.append(Label(("range", "min"), from_profile(domain + ("0",), clique.definition)))
    plan.labels.append(
        Label(("range", "max"), from_profile(domain + (str(len(spelled) - 1),), clique.definition))
    )


def _scored(plans: Mapping[str, ColumnPlan], kinds: Mapping[str, LogicalKind], name: str) -> bool:
    """Whether a property can be written for column *name*: a text column with a support, or a bin."""
    plan = plans[name]
    if kinds[name] in _NUMERIC:
        return "bins" in plan.declaration
    return kinds[name] is LogicalKind.STRING and "support" in plan.declaration


def _tolerance() -> Dict[str, Any]:
    return {
        "type": "distance",
        "measure": "totalVariation",
        "max": DEFAULT_TOLERANCES["distance"],
        "sampling": True,
    }


def _properties(
    plans: Mapping[str, ColumnPlan],
    cliques: Sequence[CliqueEntry],
    kinds: Mapping[str, LogicalKind],
    expose_id: str,
) -> None:
    """The histograms each clique answers, as properties the run is scored on.

    A clique summed onto one of its columns, or onto two, is a histogram the profile's reader
    forms (``flux_stats.profile``). Each column gets its ungrouped one unless its own statistics
    already gave it one, and each ordered pair of a clique's columns gets the histogram of one
    grouped by the other: a level histogram of a category column by another, a marginal histogram
    of a number column over the clique's bins by a category column, and one by the bins of a
    number column that comes later in the clique. A boolean column carries no property, as a
    boolean column never does.
    """
    have: Set[Tuple[str, Optional[str], Optional[str]]] = {
        (
            str(prop.document.get("column")),
            prop.document.get("groupBy"),
            (prop.document.get("groupByBins") or {}).get("column"),
        )
        for plan in plans.values()
        for prop in plan.properties
        if prop.document.get("property") in ("levelHistogram", "marginalHistogram")
        and (prop.document.get("property") == "levelHistogram" or prop.document.get("bins"))
    }
    for clique in cliques:
        scored = [name for name in clique.columns if _scored(plans, kinds, name)]
        for name in scored:
            for other in [None, *(c for c in scored if c != name)]:
                made = _property(name, other, clique, kinds, plans, have, expose_id)
                if made is not None:
                    plans[name].properties.append(made)


def _property(
    name: str,
    other: Optional[str],
    clique: CliqueEntry,
    kinds: Mapping[str, LogicalKind],
    plans: Mapping[str, ColumnPlan],
    have: Set[Tuple[str, Optional[str], Optional[str]]],
    expose_id: str,
) -> Optional[PropertyPlan]:
    """The property of column *name* grouped by *other* that *clique* answers, or ``None``."""
    binned = clique.binned(name)
    by_bins = other is not None and clique.binned(other)
    if by_bins and (not binned or clique.columns.index(other or "") < clique.columns.index(name)):
        return (
            None  # a category column grouped by a number's bins is not formed, nor is one before it
        )
    key = (name, None if by_bins else other, other if by_bins else None)
    if key in have:
        return None
    have.add(key)
    document: Dict[str, Any] = {
        "property": "marginalHistogram" if binned else "levelHistogram",
        "column": name,
    }
    labels: List[Label] = []
    if binned:
        edges = _spelled_edges(kinds[name], clique, name)
        document["bins"] = edges
        domain = _tokens(expose_id, clique) + ("domains", str(clique.columns.index(name)), "bins")
        labels += [
            Label(("bins", str(index)), from_profile(domain + (str(index),), clique.definition))
            for index in range(len(edges))
        ]
    if other is not None:
        if by_bins:
            document["groupByBins"] = {
                "column": other,
                "bins": _spelled_edges(kinds[other], clique, other),
            }
        else:
            document["groupBy"] = other
    document["tolerance"] = _tolerance()
    labels.append(Label(("tolerance", "max"), assumed(TOLERANCE_REASON)))
    return PropertyPlan(
        document,
        labels,
        scorable=True,
        reads=[
            (
                _tokens(expose_id, clique),
                "the property is scored against the histogram it sums to",
            )
        ],
    )
