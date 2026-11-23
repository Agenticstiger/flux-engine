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

The columns of a clique are categorical. The levels a clique lists for a column are the levels the
column draws: they must be the contract's levels when the contract declares them (an ``enum``).

Nothing here reads a record. It reads counts a profiler released, and what it writes is
post-processing of them.
"""

from __future__ import annotations

from itertools import product
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.author.basis import Label, assumed, from_profile
from flux_engine.author.columns import (
    DEFAULT_TOLERANCES,
    TOLERANCE_REASON,
    ColumnPlan,
    PropertyPlan,
    share_texts,
)
from flux_engine.calibrate.junction import JunctionStep, junction_tree
from flux_engine.core.errors import FluxError
from flux_engine.generator.statistics import FLX_3402
from flux_engine.seam.contract import LogicalKind
from flux_engine.seam.coverage import printable
from flux_stats.cliques import CLIQUE_DEFINITION, CliqueEntry
from flux_stats.noise import point_estimate

__all__ = ["draw_cliques"]

_CATEGORICAL = (LogicalKind.STRING, LogicalKind.BOOL)

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
) -> Set[str]:
    """Declare every column of *cliques* drawn given its separators; the columns it wrote.

    *columns* are the expose's column plans, *kinds* their logical kinds, *order* the declared
    column order and *determined* the columns a contract rule sets. Raises ``FLX-3402`` when the
    cliques cannot be drawn: a column the contract does not declare or one that cannot be drawn
    given others, levels that are not the contract's, or cliques with no junction tree.
    """
    if not cliques:
        return set()
    plans = {plan.name: plan for plan in columns}
    labels = [f"/{'/'.join(_tokens(expose_id, clique))}" for clique in cliques]
    for clique in cliques:
        for name in clique.columns:
            _check_column(plans, kinds, determined, name, clique)
    steps = junction_tree(
        [clique.columns for clique in cliques],
        order=[name for name in order if name in plans],
        names=labels,
    )
    drawn: Set[str] = set()
    for step in steps:
        clique = cliques[step.clique]
        for position, name in enumerate(step.new):
            _draw(plans[name], plans, clique, step, position, order, expose_id)
            drawn.add(name)
    _properties(plans, cliques, kinds, expose_id)
    return drawn


def _check_column(
    plans: Mapping[str, ColumnPlan],
    kinds: Mapping[str, LogicalKind],
    determined: Set[str],
    name: str,
    clique: CliqueEntry,
) -> None:
    where = f"{clique.label}: column {printable(name)!r}"
    plan = plans.get(name)
    if plan is None or name not in kinds:
        raise _refuse(f"{where} is not a column of the contract's expose")
    if kinds[name] not in _CATEGORICAL:
        raise _refuse(f"{where} is not a categorical column")
    if name in determined:
        raise _refuse(f"{where} is set by a rule of the contract, so it is not drawn")
    if plan.fixed is not None:
        raise _refuse(f"{where} is {plan.fixed}, which is never drawn given another")
    if "nullFraction" in plan.declaration:
        raise _refuse(f"{where} holds nulls, and a clique column holds none (FLX-3402)")
    levels = clique.levels_of(name)
    if plan.level_names is not None:
        if set(plan.level_names) != set(levels):
            raise _refuse(
                f"{where} lists levels that are not the levels the contract declares "
                f"({', '.join(printable(level) for level in plan.level_names)})"
            )
    elif kinds[name] is LogicalKind.STRING:
        _adopt_levels(plan, clique, levels)
    else:
        raise _refuse(f"{where} is a boolean whose levels are not false and true")


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
                CLIQUE_DEFINITION,
            ),
        )
    )


def _draw(
    plan: ColumnPlan,
    plans: Mapping[str, ColumnPlan],
    clique: CliqueEntry,
    step: JunctionStep,
    position: int,
    order: Sequence[str],
    expose_id: str,
) -> None:
    """Write column *plan* of *step*: its levels, or its weights given the columns before it."""
    name = plan.name
    given = step.parents_of(position, order)
    levels = clique.levels_of(name)
    marginal = clique.marginal([*given, name])
    source = _tokens(expose_id, clique) + ("noisyCounts",)
    basis = from_profile(source, CLIQUE_DEFINITION)
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
    if not given:
        (texts,) = rows.values()
        plan.declaration["levels"] = dict(zip(levels, texts, strict=True))
        for level in levels:
            plan.labels.append(Label(("levels", level), basis))
        plan.how = (
            f"its levels, from the noisy counts of clique {printable(', '.join(clique.columns))}"
        )
    else:
        table: Dict[str, Any] = {}
        for combination, texts in rows.items():
            node = table
            for level in combination[:-1]:
                node = node.setdefault(level, {})
            node[combination[-1]] = dict(zip(levels, texts, strict=True))
            for level in levels:
                plan.labels.append(
                    Label(
                        ("levelWeightsGiven", *combination, level),
                        assumed(_NO_MASS_REASON) if combination in empty else basis,
                    )
                )
        # One parent is a name, as it always was; several are a list, the order the tables nest in.
        plan.declaration["given"] = given[0] if len(given) == 1 else list(given)
        plan.declaration["levelWeightsGiven"] = table
        plan.how = (
            f"its levels, drawn given {printable(', '.join(given))} from the noisy counts of "
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


def _properties(
    plans: Mapping[str, ColumnPlan],
    cliques: Sequence[CliqueEntry],
    kinds: Mapping[str, LogicalKind],
    expose_id: str,
) -> None:
    """The level histograms each clique answers, as properties the run is scored on.

    A clique summed onto one of its columns, or onto two, is a level histogram the profile's
    reader forms (``flux_stats.profile``). Each column gets its ungrouped one unless its own
    statistics already gave it one, and each ordered pair of a clique's columns gets the
    histogram of one grouped by the other. A boolean column carries no property, as a boolean
    column never does.
    """
    have: Set[Tuple[str, Optional[str]]] = {
        (str(prop.document.get("column")), prop.document.get("groupBy"))
        for plan in plans.values()
        for prop in plan.properties
        if prop.document.get("property") == "levelHistogram"
    }
    for clique in cliques:
        scored = [
            name
            for name in clique.columns
            if kinds[name] is LogicalKind.STRING and "support" in plans[name].declaration
        ]
        for name in scored:
            for other in [None, *(c for c in scored if c != name)]:
                if (name, other) in have:
                    continue
                have.add((name, other))
                document: Dict[str, Any] = {"property": "levelHistogram", "column": name}
                if other is not None:
                    document["groupBy"] = other
                document["tolerance"] = {
                    "type": "distance",
                    "measure": "totalVariation",
                    "max": DEFAULT_TOLERANCES["distance"],
                    "sampling": True,
                }
                plans[name].properties.append(
                    PropertyPlan(
                        document,
                        [Label(("tolerance", "max"), assumed(TOLERANCE_REASON))],
                        scorable=True,
                        reads=[
                            (
                                _tokens(expose_id, clique),
                                "the property is scored against the level histogram it sums to",
                            )
                        ],
                    )
                )
