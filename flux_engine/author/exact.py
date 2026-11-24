# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A small table takes exact allocation (ADR 0016 section 7, "Small tables").

A distance tolerance that follows the row count is widened by
:func:`flux_stats.noise.sampling_widening`, which is the larger the fewer rows a group has. A
table is *small* when that widening, at its rows, for a property ``init`` writes for it, exceeds
the default tolerance: the tolerance is then mostly the sampling's. Every column of a small
table that ``allocation`` can read takes ``allocation: exact``, and its widening becomes the
rounding term alone. A column ``allocation`` cannot read is left as it is.

``allocation`` reads (``flux_engine.generator.rows._require_allocatable``) a column drawn from
levels, or from bins with their weights; one that is given another column when that column is
itself exact, categorical and given nothing. It refuses a key, a foreign key, a column a rule sets, and
any other family.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, List, Mapping, Optional, Sequence, Set, Tuple

from flux_engine.author.columns import DEFAULT_TOLERANCES, ColumnPlan
from flux_stats.noise import sampling_widening

__all__ = ["EXACT_HOW", "allot_small_tables", "is_small"]

#: What the written summary of a column of a small table adds: its rows are allocated exactly.
EXACT_HOW = (
    "allocated exactly, since the table is small and a sampled share would carry an error wider "
    "than the tolerance (allocation: exact, ADR 0016 section 6)"
)

_GIVEN = ("given", "levelWeightsGiven", "binWeightsGiven")


def _shares(weights: Any, size: int) -> Optional[List[Fraction]]:
    """The shares of *weights* (even when none are declared), or ``None`` when they are unusable."""
    if weights is None:
        return [Fraction(1, size)] * size if size else None
    try:
        found = [Fraction(Decimal(str(weight))) for weight in weights]
    except (ArithmeticError, TypeError, ValueError):
        return None
    total = sum(found, Fraction(0))
    if not found or total <= 0 or any(weight < 0 for weight in found):
        return None
    return [weight / total for weight in found]


def _declared(declaration: Mapping[str, Any]) -> Tuple[List[str], Optional[List[Any]]]:
    """A column's classes: the names of its levels (or none, for bins) and the weights it declares."""
    levels = declaration.get("levels")
    if isinstance(levels, Mapping):
        return [str(name) for name in levels], list(levels.values())
    if isinstance(levels, list):
        weights = declaration.get("levelWeights")
        return [str(name) for name in levels], weights if isinstance(weights, list) else None
    bins = declaration.get("bins")
    size = len(bins) - 1 if isinstance(bins, list) else 0
    weights = declaration.get("binWeights")
    return [str(position) for position in range(size)], (
        weights if isinstance(weights, list) else None
    )


def _groups(
    plan: ColumnPlan, by_name: Mapping[str, ColumnPlan], rows: int
) -> List[Tuple[int, int, Fraction]]:
    """The cells compared, the rows and the largest share of each group a property reads."""
    declaration = plan.declaration
    given = declaration.get("levelWeightsGiven") or declaration.get("binWeightsGiven")
    if given is None:
        names, weights = _declared(declaration)
        shares = _shares(weights, len(names))
        return [(len(names), rows, max(shares))] if shares is not None and names else []
    return _given_groups(declaration, given, by_name, rows)


def _given_groups(
    declaration: Mapping[str, Any], given: Any, by_name: Mapping[str, ColumnPlan], rows: int
) -> List[Tuple[int, int, Fraction]]:
    """The groups of a column drawn given another: one for each class of the column it is given."""
    parent = by_name.get(str(declaration.get("given")))
    if not isinstance(given, Mapping) or parent is None:
        return []
    names, _ = _declared(declaration)
    parent_names, parent_weights = _declared(parent.declaration)
    parent_shares = _shares(parent_weights, len(parent_names))
    if parent_shares is None:
        return []
    found: List[Tuple[int, int, Fraction]] = []
    for name, share in zip(parent_names, parent_shares, strict=True):
        group = given.get(name)
        group = list(group.values()) if isinstance(group, Mapping) else group
        shares = _shares(group, len(names)) if group is not None else None
        if shares is not None and share > 0:
            found.append((len(shares), max(1, int(rows * share)), max(shares)))
    return found


def _follows_rows(plan: ColumnPlan) -> bool:
    return any(
        isinstance(prop.document.get("tolerance"), Mapping)
        and prop.document["tolerance"].get("type") == "distance"
        and prop.document["tolerance"].get("sampling") is True
        for prop in plan.properties
    )


def is_small(
    rows: int, columns: Sequence[ColumnPlan], by_name: Optional[Mapping[str, ColumnPlan]] = None
) -> bool:
    """Whether the widening of a property of the columns at *rows* exceeds the default tolerance."""
    named = by_name if by_name is not None else {plan.name: plan for plan in columns}
    default = Fraction(DEFAULT_TOLERANCES["distance"])
    for plan in columns:
        if not _follows_rows(plan):
            continue
        for cells, group_rows, largest in _groups(plan, named, rows):
            if cells >= 1 and sampling_widening(cells, group_rows, largest) > default:
                return True
    return False


def _readable(plan: ColumnPlan, kind: Any, set_by_rule: bool) -> bool:
    """Whether ``allocation`` reads the column, apart from what it is given."""
    from flux_engine.seam.contract import LogicalKind

    declaration = plan.declaration
    if plan.fixed is not None or set_by_rule or not declaration or "family" in declaration:
        return False
    given = declaration.get("given")
    if "given" in declaration and not (isinstance(given, str) and "." not in given):
        return False
    if "levels" in declaration or "levelWeightsGiven" in declaration or "support" in declaration:
        return True
    if "rate" in declaration or (
        kind is LogicalKind.INT and "mean" in declaration and "range" not in declaration
    ):
        return False
    return "bins" in declaration and (
        "binWeights" in declaration or "binWeightsGiven" in declaration
    )


def _exact_names(expose: Any, by_name: Mapping[str, ColumnPlan]) -> Set[str]:
    """The names of the columns of *expose* that ``allocation: exact`` reads, and may be given."""
    readable = {
        plan.name
        for plan in expose.columns
        if _readable(
            plan,
            expose.kinds.get(plan.name),
            plan.name in expose.derives
            or plan.name in expose.dependents
            or plan.name in expose.copies
            or plan.name in expose.aggregated
            or plan.name in expose.guarded,
        )
    }
    exact = {
        name for name in readable if not any(key in by_name[name].declaration for key in _GIVEN)
    }
    for name in sorted(readable - exact):
        parent = by_name[name].declaration.get("given")
        if (
            isinstance(parent, str)
            and parent in exact
            and not any(key in by_name[parent].declaration for key in _GIVEN)
            # A column given a column drawn from bins cannot be allocated exactly (ADR 0036
            # decision 3): the layout is given a categorical column only.
            and "bins" not in by_name[parent].declaration
        ):
            exact.add(name)
    return exact


def allot_small_tables(
    exposes: Sequence[Any], skipped: Optional[Mapping[str, str]] = None
) -> List[Tuple[str, str]]:
    """Give every readable column of each small table of *exposes* ``allocation: exact``.

    An expose named in *skipped* is given none, and is listed with its reason as a note when it
    would have been: the notes, as (what they are about, the sentence), in expose order.
    """
    notes: List[Tuple[str, str]] = []
    for expose in exposes:
        note = _allot_one(expose, skipped)
        if note is not None:
            notes.append(note)
    return notes


def _allot_one(expose: Any, skipped: Optional[Mapping[str, str]]) -> Optional[Tuple[str, str]]:
    """Give *expose* ``allocation: exact`` when it is small; the note when it is skipped instead."""
    by_name = {plan.name: plan for plan in expose.columns}
    if not is_small(expose.rows, expose.columns, by_name):
        return None
    exact = _exact_names(expose, by_name)
    if skipped and expose.expose_id in skipped:
        if exact:
            return (f"{expose.expose_id}: allocation", skipped[expose.expose_id])
        return None
    for plan in expose.columns:
        if plan.name in exact:
            plan.declaration["allocation"] = "exact"
            plan.how = f"{plan.how}; {EXACT_HOW}" if plan.how else EXACT_HOW
    return None
