# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""``fluxsim init`` writes an association table from its two sides' children per parent (ADR 0039).

An association table is an expose of two foreign keys and no other column, each the one column of
a ``many_to_one`` relationship, with no key of its own: an enrolment of a student and a course. The
profile describes each relationship with its children per parent, which init writes as a ``counts``
entry as it would for any counted relationship; this module adds the pairing:

* :func:`association_pair` says whether an expose is such a table, and names its two relationships
  in the contract's order (the rows follow the first);
* :func:`edge_rows` gives the table's rows when the profile publishes no ``rowCount``: the
  inverse-variance mean of the two sides' totals of children, each a published total (a
  ``mean/1``'s ``Sx``, exact to the unit) or the sum a histogram's classes give (each class's
  parents at its middle, with the variance of an even spread over the class), rounded and kept
  within one row and the pairs the two parents make;
* :func:`association_entry` is the ``associations`` entry, written once both relationships are
  laid out by counts. It declares ``distinct: true``: a table of two foreign keys and no other
  column holds each pair once, which no statistic of the profile states, so a note says so.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.seam.keys import ExposeKeys, Relationship
from flux_stats.definitions import MARGINAL_HISTOGRAM, MEAN

__all__ = [
    "association_entry",
    "association_pair",
    "edge_rows",
]

#: What a person is told of an association's ``distinct``, when the rows fit in the pairs.
_DISTINCT = (
    "A table of two foreign keys and no other column is taken to hold each pair once "
    "(distinct: true): no statistic of the profile says whether a pair repeats. Set distinct: "
    "false if the source repeats pairs."
)
#: ... and when they do not.
_REPEATED = (
    "The table's {rows} rows are more than the {pairs} pairs its {first} and {second} parents "
    "make, so some pair repeats (distinct: false)."
)

#: The variance of a published total of children: a whole number, exact to its unit.
_EXACT = Fraction(1, 12)


def association_pair(
    keys: Optional[ExposeKeys], columns: Sequence[str]
) -> Optional[Tuple[Relationship, Relationship]]:
    """The two relationships of an association table, in the contract's order, or ``None``.

    An expose is one when it declares exactly two relationships, each ``many_to_one`` over one
    column, the two columns distinct and together every column of the expose, and no entity key.
    """
    if keys is None or keys.entities or keys.versioned or len(keys.relationships) != 2:
        return None
    first, second = keys.relationships
    if any(
        item.cardinality != "many_to_one" or len(item.from_columns) != 1 for item in (first, second)
    ):
        return None
    held = {first.from_columns[0], second.from_columns[0]}
    if len(held) != 2 or held != set(columns):
        return None
    return first, second


def _profile_entry(
    profile: Mapping[str, Any], relationship: Relationship
) -> Optional[Mapping[str, Any]]:
    """The profile's ``relationships`` entry of *relationship*, by its two tables and its columns."""
    found = profile.get("relationships")
    for entry in found if isinstance(found, list) else ():
        if not isinstance(entry, Mapping):
            continue
        parent, child = entry.get("parent"), entry.get("child")
        if (
            isinstance(parent, Mapping)
            and isinstance(child, Mapping)
            and parent.get("table") == relationship.target
            and child.get("table") == relationship.source
            and list(child.get("columns") or ()) == list(relationship.from_columns)
        ):
            return entry
    return None


def _group(statistic: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The one published, ungrouped group of *statistic*, or ``None``."""
    groups = statistic.get("groups")
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], Mapping):
        return None
    group = groups[0]
    return group if group.get("status") == "F" and group.get("group", "") == "" else None


def _published_total(statistic: Mapping[str, Any], bits: int) -> Optional[Fraction]:
    """The total of children a ``mean/1`` publishes (``Sx`` in units of ``2**-bits``), or ``None``."""
    group = _group(statistic)
    raw = None if group is None else group.get("Sx")
    try:
        return None if raw is None else Fraction(int(str(raw)), 2**bits)
    except ValueError:
        return None


def _histogram_total(statistic: Mapping[str, Any]) -> Optional[Tuple[Fraction, Fraction]]:
    """The total of children a histogram of children per parent gives, and its variance, or ``None``.

    Each class's parents sit at its middle; a class's variance is that of an even spread over its
    whole counts. Bin ``k`` holds ``[edge_k, edge_(k+1) - 1]``, the last bin its upper edge too.
    """
    group = _group(statistic)
    bins, counts = statistic.get("bins"), None if group is None else group.get("binCounts")
    if not isinstance(bins, list) or not isinstance(counts, list) or len(counts) != len(bins) - 1:
        return None
    try:
        edges = [int(str(edge)) for edge in bins]
    except ValueError:
        return None
    if any(not isinstance(count, int) or isinstance(count, bool) for count in counts):
        return None
    total, variance = Fraction(0), Fraction(0)
    for index, parents in enumerate(counts):
        low = edges[index]
        high = edges[index + 1] - (0 if index == len(counts) - 1 else 1)
        if high < low:
            return None
        width = high - low + 1
        total += Fraction(parents * (low + high), 2)
        variance += Fraction(parents * (width * width - 1), 12)
    return total, variance


def _side_total(
    entry: Optional[Mapping[str, Any]], bits: int
) -> Optional[Tuple[Fraction, Fraction]]:
    """One side's total of children and its variance: a published total first, else a histogram's."""
    statistics = entry.get("statistics") if entry is not None else None
    held = (
        [item for item in statistics if isinstance(item, Mapping)]
        if isinstance(statistics, list)
        else []
    )
    for statistic in held:
        if statistic.get("definition") == MEAN.identifier:
            total = _published_total(statistic, bits)
            if total is not None:
                return total, _EXACT
    for statistic in held:
        if statistic.get("definition") == MARGINAL_HISTOGRAM.identifier:
            found = _histogram_total(statistic)
            if found is not None:
                return found
    return None


def edge_rows(
    profile: Mapping[str, Any],
    pair: Tuple[Relationship, Relationship],
    parents: Tuple[int, int],
    bits: int,
) -> Optional[Tuple[int, str]]:
    """The rows of an association whose table publishes no ``rowCount``, and the sentence that says how.

    The inverse-variance mean of the two sides' totals (one side alone when the other gives none),
    rounded half up, and kept within one row and the ``parents[0] * parents[1]`` pairs. ``None``
    when neither side gives a total.
    """
    sides = [
        (relationship.name, found)
        for relationship in pair
        if (found := _side_total(_profile_entry(profile, relationship), bits)) is not None
    ]
    if not sides:
        return None
    weights = [1 / max(variance, _EXACT) for _, (_, variance) in sides]
    mean = sum(
        (weight * total for weight, (_, (total, _)) in zip(weights, sides, strict=True)),
        Fraction(0),
    ) / sum(weights, Fraction(0))
    rows = min(max(int(mean + Fraction(1, 2)), 1), parents[0] * parents[1])
    named = " and ".join(f"{name} ({float(total):.0f})" for name, (total, _) in sides)
    return rows, (
        f"The profile has no rowCount: {rows} rows, the inverse-variance mean of the children "
        f"per parent the relationships total: {named}."
    )


def association_entry(
    expose_id: str,
    pair: Tuple[Relationship, Relationship],
    counted: Sequence[Mapping[str, Any]],
    rows: Tuple[int, int, int],
) -> Optional[Tuple[Dict[str, Any], str]]:
    """The ``associations`` entry of *expose_id* and its note, when both its sides are laid out by counts.

    *rows* are the table's rows and its two parents'. Rows above the pairs the parents make prove
    that a pair repeats: the entry declares ``distinct: false``. At or below them, ``distinct:
    true`` is the assumption a table of two keys and no other column most often holds.
    """
    laid = {str(entry["relationship"]) for entry in counted if entry.get("emit") == expose_id}
    names: List[str] = [relationship.name for relationship in pair]
    if not all(name in laid for name in names):
        return None
    edges, first, second = rows
    if edges > first * second:
        note = _REPEATED.format(rows=edges, pairs=first * second, first=first, second=second)
        return {"emit": expose_id, "relationships": names, "distinct": False}, note
    return {"emit": expose_id, "relationships": names, "distinct": True}, _DISTINCT
