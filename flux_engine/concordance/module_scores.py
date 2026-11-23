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

All of them are counts and exact shares. No key, value or row appears in them.
"""

from __future__ import annotations

from bisect import bisect_right
from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from flux_engine.concordance.integrity import LinkIntegrity
from flux_engine.concordance.rule_shares import RuleShare
from flux_engine.concordance.scorecard import PropertyScore
from flux_engine.concordance.shares import Holds
from flux_engine.concordance.similarity import total_variation
from flux_engine.concordance.verifier import CENSORED, OUTSIDE, WITHIN
from flux_engine.core.canonical import nfc
from flux_engine.seam.counts import CountsEntry
from flux_engine.seam.facts import Fact
from flux_stats.definitions import MARGINAL_HISTOGRAM
from flux_stats.profile import Profile, RelationshipEntry
from flux_stats.spelling import render_exact

__all__ = [
    "CHILDREN_PER_PARENT",
    "DEFAULT_CHILDREN_TOLERANCE",
    "FACT",
    "REFERENTIAL_INTEGRITY",
    "children_row",
    "fact_row",
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


def _reference_histogram(
    link: LinkIntegrity, profile: Optional[Profile]
) -> Optional[Tuple[Any, Optional[Tuple[int, ...]]]]:
    """The profile's children-per-parent histogram of *link*: ``(statistic, published counts or None)``.

    ``None`` when the profile describes the relationship with none (no entry for the two tables and
    the same key columns, or no ungrouped ``marginalHistogram/1``). The counts are ``None`` when the
    group was withheld or has a withheld cell, so that nothing stands in for them.
    """
    if profile is None:
        return None
    entry: Optional[RelationshipEntry] = next(
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
    link: LinkIntegrity, profile: Optional[Profile], tolerance: Optional[Fraction] = None
) -> Optional[PropertyScore]:
    """The children-per-parent row of *link* in the table of its child, or ``None`` when it has none.

    A row exists when the profile describes the relationship with a ``marginalHistogram/1`` of children
    per parent and the run's parents were counted (the parent is not versioned).
    """
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


def _counts_tolerance(counts: Sequence[CountsEntry], link: LinkIntegrity) -> Optional[Fraction]:
    for entry in counts:
        if (entry.emit, entry.relationship) == (link.child, link.name) and entry.tolerance:
            return Fraction(Decimal(entry.tolerance))
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
        children = children_row(link, profile, _counts_tolerance(counts, link))
        if children is not None:
            found[link.child].append(children)
    return found
