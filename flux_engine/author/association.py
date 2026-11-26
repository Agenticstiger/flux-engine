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
  laid out by counts. Rows above the pairs the two parents make prove that a pair repeats, and
  it declares ``distinct: false``; at or below them it declares ``distinct: true``, the
  assumption a table of two foreign keys and no other column most often holds, which no
  statistic of the profile states, so a note says so.

* :func:`derive_parent_rows` settles a table of two sides when one side's parent has only an
  assumed row count: a total of children that rests on an assumed number of parents is no total,
  so the pairs come from the side whose parent rows the profile gives, and the assumed parent
  takes the rows that make its published children per parent add up to them. Both sides of an
  association count the same pairs, so the parents of each, times their mean children, are equal
  (the degree-sum identity of a bipartite graph; networkx's ``bipartite.configuration_model``
  asks the same of the two degree sequences, and PopulationSim keeps a household table and a
  person table consistent by controlling both to one total). Where both parents are assumed, or
  the rows derived cannot hold the pairs, no association is written.

A side's children follow no column of its parent: they pair with the other side's stubs one by
one, so init gives neither ``counts`` entry a given column and lists a grouping of them unused.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from flux_engine.seam.keys import ExposeKeys, Relationship
from flux_stats.definitions import MARGINAL_HISTOGRAM, MEAN

__all__ = [
    "Derived",
    "association_entry",
    "association_pair",
    "derive_parent_rows",
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


def _entry_index(profile: Mapping[str, Any], relationship: Relationship) -> Optional[int]:
    """The place in the profile's ``relationships`` of *relationship*, by its two tables and its columns."""
    found = profile.get("relationships")
    for index, entry in enumerate(found if isinstance(found, list) else ()):
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
            return index
    return None


def _profile_entry(
    profile: Mapping[str, Any], relationship: Relationship
) -> Optional[Mapping[str, Any]]:
    """The profile's ``relationships`` entry of *relationship*, by its two tables and its columns."""
    index = _entry_index(profile, relationship)
    found = profile.get("relationships")
    return found[index] if index is not None and isinstance(found, list) else None


def _group(statistic: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The one published, ungrouped group of *statistic*, or ``None``."""
    groups = statistic.get("groups")
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], Mapping):
        return None
    group = groups[0]
    return group if group.get("status") == "F" and group.get("group", "") == "" else None


def _published_total(
    statistic: Mapping[str, Any], bits: int
) -> Optional[Tuple[Fraction, Optional[int]]]:
    """The total of children a ``mean/1`` publishes (``Sx`` in units of ``2**-bits``), and its parents.

    The parents are the ``k`` the statistic counts, when it gives one.
    """
    group = _group(statistic)
    raw = None if group is None else group.get("Sx")
    try:
        total = None if raw is None else Fraction(int(str(raw)), 2**bits)
    except ValueError:
        return None
    if total is None or group is None:
        return None
    parents = group.get("k")
    return total, parents if isinstance(parents, int) and not isinstance(parents, bool) else None


def _histogram_total(
    statistic: Mapping[str, Any], clear: Optional[Tuple[bool, ...]] = None
) -> Optional[Tuple[Fraction, Fraction, int, int]]:
    """The total of children a histogram of children per parent gives, its variance, its parents and its top.

    Each class's parents sit at its middle; a class's variance is that of an even spread over its
    whole counts. Bin ``k`` holds ``[edge_k, edge_(k+1) - 1]``, the last bin its upper edge too.
    The parents are the histogram's counts added up, and the top is the most children a parent of
    a class that holds one has. With *clear*, a noisy histogram's per-cell flags for standing clear
    of the noise, a cell that does not carries no weight: the total, the variance, the parents and
    the top are over the cells that do.
    """
    read = _whole_counts(statistic, clear)
    if read is None:
        return None
    edges, counts = read
    total, variance, top = Fraction(0), Fraction(0), 0
    for index, parents in enumerate(counts):
        low = edges[index]
        high = edges[index + 1] - (0 if index == len(counts) - 1 else 1)
        if high < low:
            return None
        top = high if parents > 0 else top
        width = high - low + 1
        total += Fraction(parents * (low + high), 2)
        variance += Fraction(parents * (width * width - 1), 12)
    return total, variance, sum(counts), top


def _whole_counts(
    statistic: Mapping[str, Any], clear: Optional[Tuple[bool, ...]]
) -> Optional[Tuple[List[int], List[int]]]:
    """A histogram's whole edges and whole counts, a cell not in *clear* counting 0, or ``None``."""
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
    if clear is None:
        return edges, counts
    if len(clear) != len(counts):
        return None
    return edges, [count if standing else 0 for count, standing in zip(counts, clear, strict=True)]


@dataclass(frozen=True)
class _Degrees:
    """One side's published children per parent: their total, its variance and the parents it is over."""

    total: Fraction
    variance: Fraction
    #: The parents the total is over, when the statistic counts them.
    parents: Optional[int]
    #: The most children a parent can have by the statistic, when it is a histogram.
    top: Optional[int] = None
    #: The parents the whole histogram counts, whatever cells stood clear of the noise.
    counted: Optional[int] = None
    #: A noisy histogram none of whose cells stands clear of the noise.
    no_clear_cell: bool = False


def _mean_of(found: Optional[_Degrees]) -> Optional[Fraction]:
    """The children per parent of a side, or ``None`` when its parents are not counted."""
    return Fraction(found.total, found.parents) if found is not None and found.parents else None


def _side_degrees(
    entry: Optional[Mapping[str, Any]],
    bits: int,
    clear_of: Optional[Mapping[int, Tuple[bool, ...]]] = None,
) -> Optional[_Degrees]:
    """One side's children per parent: a published total first, else a histogram's.

    *clear_of* gives, by the position of a histogram among the entry's statistics, which of its
    cells stand clear of the noise; the histogram is then read over those cells alone.
    """
    statistics = entry.get("statistics") if entry is not None else None
    held = (
        [(position, item) for position, item in enumerate(statistics) if isinstance(item, Mapping)]
        if isinstance(statistics, list)
        else []
    )
    for _, statistic in held:
        if statistic.get("definition") == MEAN.identifier:
            published = _published_total(statistic, bits)
            if published is not None:
                return _Degrees(published[0], _EXACT, published[1], None, published[1])
    for position, statistic in held:
        if statistic.get("definition") == MARGINAL_HISTOGRAM.identifier:
            clear = None if clear_of is None else clear_of.get(position)
            found = _histogram_total(statistic, clear)
            if found is not None:
                everything = found if clear is None else _histogram_total(statistic)
                counted = None if everything is None else everything[2]
                return _Degrees(
                    found[0],
                    found[1],
                    found[2],
                    found[3],
                    counted,
                    clear is not None and found[2] == 0,
                )
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
        (relationship.name, (found.total, found.variance))
        for relationship in pair
        if (found := _side_degrees(_profile_entry(profile, relationship), bits)) is not None
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


@dataclass(frozen=True)
class Derived:
    """The rows of an association and of the parent whose own count was an assumption."""

    #: The association's rows, and the sentence that says how.
    edges: int
    edges_note: str
    #: Which of the pair's two sides the assumed parent is on, its rows and the sentence.
    side: int
    parent_rows: int
    parent_note: str


def _half_up(value: Fraction) -> int:
    return int(value + Fraction(1, 2))


def derive_parent_rows(
    profile: Mapping[str, Any],
    pair: Tuple[Relationship, Relationship],
    parents: Tuple[int, int],
    assumed: Tuple[bool, bool],
    bits: int,
    noisy: Optional[Mapping[Tuple[str, ...], Any]] = None,
) -> Union[Derived, str]:
    """The rows of an association and of its assumed parent, or why no association is written.

    Both sides count the same pairs, so a side whose parent rows are an assumption has no total
    of its own: the pairs are the other side's parents times its mean children per parent, and
    the assumed parent takes the rows that its own mean children per parent give them. The result
    is the reason as a string when both parents are assumed, when a side publishes no mean, when a
    count of parents the side publishes is far from the rows derived, or when the rows derived
    cannot hold the pairs: more children per parent than the other side has parents, which no
    table of distinct pairs holds.
    """
    try:
        return _derive(profile, pair, parents, assumed, bits, noisy)
    except (OverflowError, ValueError):
        return "a number the profile publishes is beyond what a bundle can declare"


def _clear_cells(
    profile: Mapping[str, Any],
    relationship: Relationship,
    noisy: Optional[Mapping[Tuple[str, ...], Any]],
) -> Dict[int, Tuple[bool, ...]]:
    """The cells of each noisy histogram of *relationship* that stand clear of the noise, by statistic."""
    index = _entry_index(profile, relationship)
    found: Dict[int, Tuple[bool, ...]] = {}
    for tokens, note in (noisy or {}).items():
        if index is not None and tokens[:2] == ("relationships", str(index)):
            if len(tokens) == 4 and getattr(note, "clear", None) is not None:
                found[int(tokens[3])] = note.clear
    return found


def _derive(
    profile: Mapping[str, Any],
    pair: Tuple[Relationship, Relationship],
    parents: Tuple[int, int],
    assumed: Tuple[bool, bool],
    bits: int,
    noisy: Optional[Mapping[Tuple[str, ...], Any]],
) -> Union[Derived, str]:
    """The rows of an association and of its assumed parent, or why no association is written.

    Both sides count the same pairs, so a side whose parent rows are an assumption has no total
    of its own: the pairs are the other side's parents times its mean children per parent, and
    the assumed parent takes the rows that its own mean children per parent give them. The result
    is the reason as a string when both parents are assumed, when a side publishes no mean, or
    when the rows derived cannot hold the pairs: more children per parent than the other side has
    parents, which no table of distinct pairs holds.
    """
    if all(assumed):
        return (
            f"the rows of both the {pair[0].target!r} and the {pair[1].target!r} parents are "
            "assumptions, so neither side gives a total of children"
        )
    other = 0 if assumed[1] else 1
    side = 1 - other
    held, lacking = pair[other], pair[side]
    from_held = _side_degrees(_profile_entry(profile, held), bits)
    # The mean of the side whose parent is assumed is read over the cells that stand clear of the
    # noise: a cell inside it says nothing of how many children a parent has.
    from_lacking = _side_degrees(
        _profile_entry(profile, lacking), bits, _clear_cells(profile, lacking, noisy)
    )
    mean_held = _mean_of(from_held)
    mean_lacking = _mean_of(from_lacking)
    if from_held is None:
        return f"the {held.name!r} relationship publishes no children per parent"
    if from_lacking is not None and from_lacking.no_clear_cell:
        return (
            f"no cell of the {lacking.name!r} histogram of children per {lacking.target!r} "
            "stands clear of the noise, so it gives no mean"
        )
    if mean_lacking is None or mean_lacking <= 0:
        return (
            f"the {lacking.name!r} relationship publishes no mean children per "
            f"{lacking.target!r}, so the rows of the assumed parent cannot follow from the pairs"
        )
    pairs = from_held.total if mean_held is None else mean_held * parents[other]
    edges = max(_half_up(pairs), 1)
    rows = _half_up(pairs / mean_lacking)
    if from_lacking is not None and from_lacking.counted:
        counted = from_lacking.counted
        if rows > 10 * counted or counted > 10 * rows:
            return (
                f"the {rows} {lacking.target!r} the pairs give are far from the {counted} that "
                f"{lacking.name!r} counts"
            )
    if rows < 1:
        return (
            f"the {edges} pairs over the {float(mean_lacking):.4g} children per "
            f"{lacking.target!r} that {lacking.name!r} publishes hold fewer than one "
            f"{lacking.target!r}"
        )
    if edges > rows * parents[other]:
        return (
            f"the {edges} pairs cannot be drawn between {rows} {lacking.target!r} and "
            f"{parents[other]} {held.target!r}, so the {float(mean_lacking):.4g} children per "
            f"{lacking.target!r} that {lacking.name!r} publishes do not fit the pairs"
        )
    for degrees, other_parents, name in (
        (from_lacking, parents[other], lacking.name),
        (from_held, rows, held.name),
    ):
        if degrees is not None and degrees.top is not None and degrees.top > other_parents:
            return (
                f"a parent of {name!r} holds up to {degrees.top} children, more than the "
                f"{other_parents} parents of the other side"
            )
    total = f"{float(pairs):.0f}"
    return Derived(
        edges,
        f"The profile has no rowCount: {edges} rows, the children per parent the {held.name} "
        f"relationship totals ({total}), the rows of its {lacking.target} parent being assumed.",
        side,
        rows,
        f"The profile has no row count for {lacking.target}: {rows} rows, the {edges} pairs of "
        f"{held.target} and {lacking.target} over the {float(mean_lacking):.4g} children per "
        f"{lacking.target} that {lacking.name} publishes (both sides of the association count the "
        "same pairs).",
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
