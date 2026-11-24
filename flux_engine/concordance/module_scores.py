# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""The scorecard rows the rules and the relationships of a run make (docs/spec/SCORECARD-SPEC.md §3, formats 1-3-0 and 1-4-0).

A **fact** is one rule of an emit, checked on the run's rows (:mod:`flux_engine.concordance.rule_shares`),
scored against the share the profile measured. The profile's ``invariants`` entry of the same name
holds the rows that did and did not satisfy the rule (PROFILE-SPEC.md section 5), so the pair is a
two-level histogram, and the score is the one the scorecard gives a ``levelHistogram/1``: one minus the
total variation distance, which for two levels is the gap between the two shares,
``1 - |profile share - run share|``. The Simulation's tolerance for the fact
(:mod:`flux_engine.seam.facts`, default ``0``) decides within or outside by the same gap. A fact the
profile gave no share for, and a fact whose rule covers no row, is ``censored``: shown, and left out of
every mean.

A **relationship** is scored by its referential integrity (:mod:`flux_engine.concordance.integrity`): the
mean of the shares of its checks, within only when every check holds on every row it covers.

A **children-per-parent** row (format 1-4-0) sits beside a relationship the profile describes with a
``marginalHistogram/1`` of children per parent: the run's parents counted into the profile's bins by their
children, compared as a histogram is, one minus the total variation distance, within when the distance is
at most the tolerance of the relationship's ``counts`` entry (default ``0.10``).

A children-per-parent row of a relationship whose ``counts`` entry has ``given`` (format 1-5-0) is scored per
class of the parent column: the run's parents of each class are counted into the profile's bins by their
children, and each class is compared with the profile's group (a statistic grouped by that column, or by
its bins) by total variation distance. Classes are named by ordinal, the tolerance is the entry's, and a
noisy reference widens each class's margin by SCORECARD-SPEC.md section 9's ``w``, class by class.

All of them are counts and exact shares. No key, value or row appears in them.
"""

from __future__ import annotations

from bisect import bisect_right
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from flux_engine.concordance.integrity import LinkIntegrity, ParentGroups
from flux_engine.concordance.rule_shares import RuleShare
from flux_engine.concordance.scorecard import PropertyScore
from flux_engine.concordance.shares import Holds
from flux_engine.concordance.similarity import total_variation
from flux_engine.concordance.verifier import CENSORED, OUTSIDE, WITHIN, group_order
from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.seam.counts import CountsEntry
from flux_engine.seam.facts import Fact
from flux_stats.definitions import MARGINAL_HISTOGRAM
from flux_stats.noise import widening
from flux_stats.profile import Profile, ProfileGroup, RelationshipEntry, StatisticEntry
from flux_stats.reading import MS_PER_DAY, parse_date_days, parse_rfc3339_ms
from flux_stats.spelling import render_exact

__all__ = [
    "CHILDREN_PER_PARENT",
    "DEFAULT_CHILDREN_TOLERANCE",
    "FACT",
    "REFERENTIAL_INTEGRITY",
    "children_row",
    "fact_row",
    "grouped_children_row",
    "link_row",
    "module_rows",
]

FACT = "fact"
REFERENTIAL_INTEGRITY = "referentialIntegrity"
CHILDREN_PER_PARENT = "childrenPerParent"

#: The largest total variation distance a children-per-parent row allows when the relationship's
#: ``counts`` entry declares none (or the relationship has no entry).
DEFAULT_CHILDREN_TOLERANCE = Fraction(1, 10)

_ZERO = Fraction(0)
_ONE = Fraction(1)


def _holds(holds: Optional[Holds]) -> Optional[Dict[str, Any]]:
    return None if holds is None else holds.to_document()


def fact_row(
    share: RuleShare, profile: Optional[Profile], tolerance: Optional[Fraction] = None
) -> PropertyScore:
    """One rule's fact, scored against the share *profile* measured for it."""
    measured: Optional[Holds] = None
    entry = profile.invariant(share.name) if profile is not None else None
    counts = entry.counts() if entry is not None else None
    if counts is not None:
        measured = Holds(counts[0], counts[1])
    allowed = tolerance if tolerance is not None else _ZERO
    run_share = share.holds.share
    profile_share = measured.share if measured is not None else None
    score: Optional[Fraction] = None
    gap: Optional[Fraction] = None
    outcome = CENSORED
    if run_share is not None and profile_share is not None:
        gap = abs(profile_share - run_share)
        score = _ONE - gap
        outcome = WITHIN if gap <= allowed else OUTSIDE
    document: Dict[str, Any] = {
        "propertyId": f"fact:{share.name}",
        "property": FACT,
        "fact": share.name,
        "dimension": "invariants",
        "outcome": outcome,
        "score": None if score is None else render_exact(score),
        "weight": "1",
        "run": share.holds.to_document(),
        "profile": _holds(measured),
        "tolerance": render_exact(allowed),
        "gap": None if gap is None else render_exact(gap),
        "enforced": share.enforced,
    }
    return PropertyScore(
        expose_id=share.expose_id,
        property_id=str(document["propertyId"]),
        kind=FACT,
        statistic="",
        columns=(),
        group_by=None,
        dimension="invariants",
        outcome=outcome,
        score=score,
        groups_scored=0,
        groups_censored=0,
        groups_outside=(),
        tolerance={"type": "absolute", "value": render_exact(allowed)},
        margin=None,
        document=document,
        label=f"fact {share.name}",
    )


def link_row(link: LinkIntegrity) -> PropertyScore:
    """One relationship's referential integrity."""
    score = link.score
    outcome = CENSORED if score is None else (WITHIN if link.whole else OUTSIDE)
    name = f"{link.child}.{link.name}"
    document: Dict[str, Any] = {
        "propertyId": f"relationship:{name}",
        "property": REFERENTIAL_INTEGRITY,
        "relationship": link.name,
        "child": link.child,
        "parent": link.parent,
        "fromColumns": list(link.from_columns),
        "toColumns": list(link.to_columns),
        "cardinality": link.cardinality,
        "joinType": link.join_type,
        "dimension": "crossTable",
        "outcome": outcome,
        "score": None if score is None else render_exact(score),
        "weight": "1",
        "checks": {check: holds.to_document() for check, holds in link.checks},
    }
    return PropertyScore(
        expose_id=link.child,
        property_id=str(document["propertyId"]),
        kind=REFERENTIAL_INTEGRITY,
        statistic="",
        columns=(),
        group_by=None,
        dimension="crossTable",
        outcome=outcome,
        score=score,
        groups_scored=0,
        groups_censored=0,
        groups_outside=(),
        tolerance={},
        margin=None,
        document=document,
        label=f"referential integrity of {name} -> {link.parent}",
    )


def _relationship_entry(
    link: LinkIntegrity, profile: Optional[Profile]
) -> Optional[RelationshipEntry]:
    """The profile's entry for the relationship of *link* (the two tables and the same key columns)."""
    if profile is None:
        return None
    return next(
        (
            item
            for item in profile.relationships
            if item.child_table == link.child
            and item.parent_table == link.parent
            and tuple(nfc(name) for name in item.child_columns) == link.from_columns
            and tuple(nfc(name) for name in item.parent_columns) == link.to_columns
        ),
        None,
    )


def _reference_histogram(
    link: LinkIntegrity, profile: Optional[Profile]
) -> Optional[Tuple[Any, Optional[Tuple[int, ...]]]]:
    """The profile's children-per-parent histogram of *link*: ``(statistic, published counts or None)``.

    ``None`` when the profile describes the relationship with none (no entry for the two tables and
    the same key columns, or no ungrouped ``marginalHistogram/1``). The counts are ``None`` when the
    group was withheld or has a withheld cell, so that nothing stands in for them.
    """
    entry = _relationship_entry(link, profile)
    if entry is None:
        return None
    for statistic in entry.statistics:
        if (
            statistic.definition.identifier != MARGINAL_HISTOGRAM.identifier
            or statistic.group_by is not None
            or statistic.group_by_link is not None
            or statistic.group_by_bins is not None
            or len(statistic.bins) < 2
        ):
            continue
        for group in statistic.groups:
            held = group.statistic
            if group.published and not group.suppressed_cells and held is not None and held.k > 0:
                return statistic, held.bin_counts
        return statistic, None
    return None


def _binned(spread: Mapping[int, int], edges: Sequence[Fraction]) -> Tuple[int, ...]:
    """Parents by number of children, counted into bins half open at the top but the last, which is closed.

    The work is one search per distinct number of children, not one scan of the bins per child.
    """
    counts = [0] * (len(edges) - 1)
    upper = list(edges[1:])
    for children, parents in spread.items():
        counts[min(bisect_right(upper, children), len(counts) - 1)] += parents
    return tuple(counts)


def children_row(
    link: LinkIntegrity,
    profile: Optional[Profile],
    tolerance: Optional[Fraction] = None,
    given: Optional[str] = None,
) -> Optional[PropertyScore]:
    """The children-per-parent row of *link* in the table of its child, or ``None`` when it has none.

    A row exists when the profile describes the relationship with a ``marginalHistogram/1`` of children
    per parent and the run's parents were counted (the parent is not versioned). When the relationship
    is laid out *given* a column of its parent and the profile's histogram is grouped by that column,
    the row is scored per class of it (:func:`grouped_children_row`).
    """
    if given is not None:
        grouped = grouped_children_row(link, profile, tolerance, given)
        if grouped is not None:
            return grouped
    found = _reference_histogram(link, profile)
    if found is None or link.parents_by_children is None:
        return None
    statistic, published = found
    edges = tuple(statistic.bins)
    run_counts = _binned(link.parents_by_children, edges)
    parents = sum(run_counts)
    allowed = tolerance if tolerance is not None else DEFAULT_CHILDREN_TOLERANCE
    reference_parents = sum(published) if published is not None else 0
    score: Optional[Fraction] = None
    distance: Optional[Fraction] = None
    outcome = CENSORED
    if published is not None and parents > 0 and reference_parents > 0:
        distance = total_variation(run_counts, parents, published, reference_parents)
        score = _ONE - distance
        outcome = WITHIN if distance <= allowed else OUTSIDE
    name = f"{link.child}.{link.name}"
    document: Dict[str, Any] = {
        "propertyId": f"children:{name}",
        "property": CHILDREN_PER_PARENT,
        "relationship": link.name,
        "child": link.child,
        "parent": link.parent,
        "dimension": "crossTable",
        "outcome": outcome,
        "score": None if score is None else render_exact(score),
        "weight": "1",
        "bins": [render_exact(edge) for edge in edges],
        "run": {"parents": parents, "binCounts": list(run_counts)},
        "profile": (
            None
            if published is None
            else {"parents": reference_parents, "binCounts": list(published)}
        ),
        "tolerance": render_exact(allowed),
        "distance": None if distance is None else render_exact(distance),
    }
    return PropertyScore(
        expose_id=link.child,
        property_id=str(document["propertyId"]),
        kind=CHILDREN_PER_PARENT,
        statistic="",
        columns=(),
        group_by=None,
        dimension="crossTable",
        outcome=outcome,
        score=score,
        groups_scored=0,
        groups_censored=0,
        groups_outside=(),
        tolerance={"type": "absolute", "value": render_exact(allowed)},
        margin=None,
        document=document,
        label=f"children per parent of {name}",
    )


def _grouped_statistic(
    link: LinkIntegrity, profile: Optional[Profile], given: str
) -> Optional[StatisticEntry]:
    """The profile's children-per-parent histogram of *link* grouped by the parent column *given*.

    A ``marginalHistogram/1`` of children per parent whose ``groupBy`` is the column, or whose
    ``groupByBins`` is over it; ``None`` when the profile holds none.
    """
    entry = _relationship_entry(link, profile)
    if entry is None:
        return None
    wanted = nfc(given)
    for statistic in entry.statistics:
        if (
            statistic.definition.identifier == MARGINAL_HISTOGRAM.identifier
            and statistic.group_by_link is None
            and len(statistic.bins) >= 2
            and (
                (statistic.group_by is not None and nfc(statistic.group_by) == wanted)
                or (
                    statistic.group_by_bins is not None
                    and nfc(statistic.group_by_bins.column) == wanted
                )
            )
        ):
            return statistic
    return None


def _edge_units(cell: Any, declared: str) -> Optional[Fraction]:
    """A cell of the parent column in the units a profile spells bin edges in, or ``None`` if it has none.

    A number as itself, a DATE in milliseconds (the profile reads it as a timestamp) and a timestamp
    in milliseconds since the epoch.
    """
    kind = declared.upper()
    try:
        if kind.startswith("DATE"):
            return Fraction(parse_date_days(str(cell)) * MS_PER_DAY)
        if kind.startswith("TIMESTAMP"):
            return Fraction(parse_rfc3339_ms(str(cell)))
        return Fraction(Decimal(str(cell)))
    except (ArithmeticError, ValueError, FluxError):
        return None


def _run_groups(
    parents: ParentGroups, statistic: StatisticEntry, edges: Sequence[Fraction]
) -> Dict[str, Tuple[int, ...]]:
    """The run's parents of each group of *statistic* counted into the bins *edges* by their children.

    Groups of a ``groupBy`` are keyed by the value as the emit spells it; groups of a ``groupByBins``
    by the lower edge of the profile's bin the value falls in (a bin is half open and the last is
    closed). A value below the first edge or above the last is in no group.
    """
    merged: Dict[str, Dict[int, int]] = {}
    grouping = statistic.group_by_bins
    for cell, spread in parents.spreads.items():
        if grouping is None:
            key = nfc(str(cell))
        else:
            units = _edge_units(cell, parents.declared)
            if units is None or units < grouping.bins[0] or units > grouping.bins[-1]:
                continue
            at = min(bisect_right(grouping.bins, units) - 1, len(grouping.bins) - 2)
            key = grouping.keys[at]
        into = merged.setdefault(key, {})
        for number, count in spread.items():
            into[number] = into.get(number, 0) + count
    return {key: _binned(spread, edges) for key, spread in merged.items()}


def _group_reach(statistic: StatisticEntry, group: ProfileGroup) -> Optional[Fraction]:
    """:func:`flux_stats.noise.widening` of one noisy group, or ``None`` when it has no records to widen over."""
    held = group.statistic
    if statistic.cell_variance is None or held is None:
        return None
    total = sum(group.noisy, Fraction(0)) if group.noisy else Fraction(held.k)
    if total <= 0 or held.k <= 0:
        return None
    return widening(statistic.cell_variance, len(held.bin_counts), total, held.k)


def grouped_children_row(
    link: LinkIntegrity,
    profile: Optional[Profile],
    tolerance: Optional[Fraction],
    given: str,
) -> Optional[PropertyScore]:
    """The children-per-parent row of *link* scored per class of the parent column *given*, or ``None``.

    ``None`` when the profile holds no children-per-parent histogram grouped by *given* (or by its
    bins), or the run's parents were not counted by it. The run's parents of each group of the
    profile's statistic are counted into the profile's bins by their number of children, as the
    ungrouped row counts them, and each group is compared with the profile's by total variation
    distance, within when it is at most the tolerance plus, for a noisy reference, the widening of
    SCORECARD-SPEC.md section 9 for that group. A group the profile withheld, or the run has no
    parent in, is censored. The row is within when every scored group is, outside when one is not,
    and censored when none was scored; its score is the mean of the scored groups' scores.
    """
    statistic = _grouped_statistic(link, profile, given)
    parents = link.parents_by_group
    if statistic is None or parents is None or nfc(parents.column) != nfc(given):
        return None
    edges = tuple(statistic.bins)
    run = _run_groups(parents, statistic, edges)
    allowed = tolerance if tolerance is not None else DEFAULT_CHILDREN_TOLERANCE
    # Named by ordinal in the order of the bins when grouped by bins, as a property's groups are
    # (SCORECARD-SPEC.md section 3), not in the text order of the profile's keys.
    held = {group.key: group for group in statistic.groups}
    ordered = [held[key] for key in group_order(held, by_bins=statistic.group_by_bins is not None)]
    scored = [
        _score_group(statistic, group, ordinal, run.get(group.key), allowed)
        for ordinal, group in enumerate(ordered)
    ]
    distances = [item.distance for item in scored if item.distance is not None]
    scores = [_ONE - distance for distance in distances]
    outside = tuple(item.ordinal for item in scored if item.outcome == OUTSIDE)
    score = sum(scores, _ZERO) / len(scores) if scores else None
    outcome = CENSORED if not scores else (OUTSIDE if outside else WITHIN)
    name = f"{link.child}.{link.name}"
    document = _grouped_document(link, statistic, scored, (outcome, score, allowed))
    return PropertyScore(
        expose_id=link.child,
        property_id=str(document["propertyId"]),
        kind=CHILDREN_PER_PARENT,
        statistic="",
        columns=(),
        group_by=None,
        dimension="crossTable",
        outcome=outcome,
        score=score,
        groups_scored=len(scores),
        groups_censored=len(scored) - len(scores),
        groups_outside=outside,
        tolerance={"type": "absolute", "value": render_exact(allowed)},
        margin=None,
        document=document,
        label=f"children per parent of {name} by {given}",
    )


class _ScoredGroup(NamedTuple):
    """One group of a grouped children-per-parent row: its entry, its outcome, and what it measured."""

    ordinal: int
    outcome: str
    distance: Optional[Fraction]
    reach: Optional[Fraction]
    entry: Dict[str, Any]


def _score_group(
    statistic: StatisticEntry,
    group: ProfileGroup,
    ordinal: int,
    counts: Optional[Tuple[int, ...]],
    allowed: Fraction,
) -> _ScoredGroup:
    """One group of the profile's statistic compared with the run's parents of it, by distance."""
    published = _published_counts(group)
    counts = counts if counts is not None else (0,) * (len(statistic.bins) - 1)
    run_parents = sum(counts)
    reference_parents = sum(published) if published is not None else 0
    distance, reach, outcome = _compare_group(statistic, group, (counts, published), allowed)
    profile = (
        None if published is None else {"parents": reference_parents, "binCounts": list(published)}
    )
    entry: Dict[str, Any] = {
        "ordinal": ordinal,
        "outcome": outcome,
        "run": {"parents": run_parents, "binCounts": list(counts)},
        "profile": profile,
        "distance": None if distance is None else render_exact(distance),
    }
    if reach is not None and distance is not None:
        entry["widening"] = render_exact(reach)
    return _ScoredGroup(ordinal, outcome, distance, reach, entry)


def _compare_group(
    statistic: StatisticEntry,
    group: ProfileGroup,
    sides: Tuple[Tuple[int, ...], Optional[Tuple[int, ...]]],
    allowed: Fraction,
) -> Tuple[Optional[Fraction], Optional[Fraction], str]:
    """The distance of the run's counts from the group's published ones, the widening, the outcome.

    Censored, with no distance, when the group was withheld, either side holds no parent, or a
    noisy reference has no records to widen over.
    """
    counts, published = sides
    run_parents = sum(counts)
    if published is None or run_parents <= 0 or sum(published) <= 0:
        return None, None, CENSORED
    reach = _group_reach(statistic, group) if statistic.cell_variance is not None else None
    if statistic.cell_variance is not None and reach is None:
        return None, None, CENSORED
    distance = total_variation(counts, run_parents, published, sum(published))
    return distance, reach, WITHIN if distance <= allowed + (reach or _ZERO) else OUTSIDE


def _published_counts(group: ProfileGroup) -> Optional[Tuple[int, ...]]:
    """The counts a group publishes, or ``None`` when it was withheld or has a withheld cell."""
    held = group.statistic
    if group.published and not group.suppressed_cells and held is not None and held.k > 0:
        return held.bin_counts
    return None


def _grouping_member(statistic: StatisticEntry) -> Dict[str, Any]:
    """``groupByBins`` or ``groupBy``: how the profile's statistic groups the parents."""
    if statistic.group_by_bins is None:
        return {"groupBy": statistic.group_by}
    return {
        "groupByBins": {
            "column": statistic.group_by_bins.column,
            "bins": [render_exact(edge) for edge in statistic.group_by_bins.bins],
        }
    }


def _grouped_document(
    link: LinkIntegrity,
    statistic: StatisticEntry,
    scored: Sequence[_ScoredGroup],
    row: Tuple[str, Optional[Fraction], Fraction],
) -> Dict[str, Any]:
    """The scorecard document of a grouped children-per-parent row: *row* is its outcome, score, tolerance."""
    outcome, score, allowed = row
    distances = [item.distance for item in scored if item.distance is not None]
    widenings = [
        item.reach for item in scored if item.reach is not None and item.distance is not None
    ]
    document: Dict[str, Any] = {
        "propertyId": f"children:{link.child}.{link.name}",
        "property": CHILDREN_PER_PARENT,
        "relationship": link.name,
        "child": link.child,
        "parent": link.parent,
        "dimension": "crossTable",
        "outcome": outcome,
        "score": None if score is None else render_exact(score),
        "weight": "1",
        "bins": [render_exact(edge) for edge in statistic.bins],
        "groups": [item.entry for item in scored],
        "tolerance": render_exact(allowed),
        "distance": None if not distances else render_exact(max(distances)),
    }
    document.update(_grouping_member(statistic))
    if statistic.cell_variance is not None:
        document["noise"] = {
            "mechanism": statistic.noise.mechanism if statistic.noise is not None else "",
            "cellVariance": render_exact(statistic.cell_variance),
            "widening": render_exact(max(widenings, default=_ZERO)),
        }
    return document


def _counts_tolerance(counts: Sequence[CountsEntry], link: LinkIntegrity) -> Optional[Fraction]:
    for entry in counts:
        if (entry.emit, entry.relationship) == (link.child, link.name) and entry.tolerance:
            return Fraction(Decimal(entry.tolerance))
    return None


def _counts_given(counts: Sequence[CountsEntry], link: LinkIntegrity) -> Optional[str]:
    """The parent column a relationship's ``counts`` entry lays it out given, or ``None``."""
    for entry in counts:
        if (entry.emit, entry.relationship) == (link.child, link.name):
            return entry.given
    return None


def module_rows(
    rules: Sequence[RuleShare],
    links: Sequence[LinkIntegrity],
    facts: Sequence[Fact],
    profile: Optional[Profile],
    counts: Sequence[CountsEntry] = (),
) -> Mapping[str, Sequence[PropertyScore]]:
    """Every fact and relationship row of a run, by the ``exposeId`` of the table it sits in.

    A fact is in the table of its rule's emit, a relationship in its child's. Within a table the
    facts come first in rule order, then the relationships in declared order, each followed by its
    children-per-parent row when the profile describes one (*counts* gives the entries' tolerances).
    """
    tolerances = {(fact.emit, fact.rule): fact.tolerance for fact in facts}
    found: Dict[str, List[PropertyScore]] = {}
    for share in rules:
        found.setdefault(share.expose_id, []).append(
            fact_row(share, profile, tolerances.get((share.expose_id, share.rule_id)))
        )
    for link in links:
        found.setdefault(link.child, []).append(link_row(link))
        children = children_row(
            link, profile, _counts_tolerance(counts, link), _counts_given(counts, link)
        )
        if children is not None:
            found[link.child].append(children)
    return found
