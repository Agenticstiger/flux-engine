# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""A junction tree over clique marginals, and the order its columns are drawn in.

ADR 0021 and PROFILE-SPEC.md section 5a. A profile released under differential privacy can carry
the joint noisy counts of cliques of columns. This module arranges the cliques of one table into
a junction tree, the structure Private-PGM samples a graphical model from
(https://github.com/ryan112358/private-pgm, Apache-2.0, ``mbi/junction_tree.py``: a maximum
spanning tree of the cliques weighted by the size of their intersections; no code is used), and
says, for each clique in turn, which columns it introduces and which of the columns already
drawn they are drawn given.

The construction is the Chow-Liu forest's, one level up. :func:`flux_engine.calibrate.structure.
spanning_forest` keeps a maximum-weight spanning forest by Kruskal's rule with deterministic ties
and orients it away from a root; the Chow-Liu forest weighs pairs of columns by their mutual
information, and this tree weighs cliques by the number of columns they share. A forest of
two-column cliques is the Chow-Liu forest: each clique introduces one column and the column is
drawn given the one it shares.

A set of cliques has a junction tree exactly when a maximum-weight spanning tree of the graph of
its cliques has the **running intersection property**: the cliques that hold a column are
connected in the tree. A set that does not is refused (``FLX-3402``), naming the cliques.

The root of each tree, and so the order, is chosen without a choice: the clique with the most
columns, and of those the one whose sorted column names come first. Kruskal's rule takes edges in
the order ``(-shared columns, names)``, and the tree is walked breadth first from the root, children
in name order. Nothing depends on the order the cliques were written in the profile, except to
name them in a refusal.

Everything here is on names. No count is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from flux_engine.calibrate.structure import spanning_forest
from flux_engine.core.errors import FluxError
from flux_engine.generator.statistics import FLX_3402

__all__ = ["JunctionStep", "junction_tree"]


@dataclass(frozen=True)
class JunctionStep:
    """One clique's turn: the columns it introduces, and what they are drawn given.

    ``clique`` is the clique's position in the input. ``separator`` is the columns it shares with
    its parent, empty for a root. ``new`` is the columns it introduces: its own columns the parent
    does not hold, in the order they are drawn. Column ``new[j]`` is drawn given ``separator`` and
    ``new[:j]``, in the table's declared column order.
    """

    clique: int
    parent: Optional[int]
    separator: Tuple[str, ...]
    new: Tuple[str, ...]

    def parents_of(self, position: int, order: Sequence[str]) -> Tuple[str, ...]:
        """The columns ``new[position]`` is drawn given, in the table's declared order."""
        given = set(self.separator) | set(self.new[:position])
        return tuple(name for name in order if name in given)


def junction_tree(
    cliques: Sequence[Sequence[str]],
    *,
    order: Sequence[str],
    names: Optional[Sequence[str]] = None,
) -> Tuple[JunctionStep, ...]:
    """The steps that draw every column of *cliques*, each clique after its parent.

    *cliques* are the columns of each clique, in the order the profile lists them. *order* is
    the table's declared column order, which the columns a clique introduces are drawn in.
    *names* label the cliques in a refusal (the profile's pointers); by default they are
    ``clique 0``, ``clique 1`` and so on. Raises ``FLX-3402`` when the cliques have no junction
    tree. The steps come in drawing order: a root, then its tree breadth first, then the next
    root.
    """
    labels = list(names) if names is not None else [f"clique {i}" for i in range(len(cliques))]
    sets = [frozenset(columns) for columns in cliques]
    ranking = sorted(range(len(sets)), key=lambda i: (-len(sets[i]), tuple(sorted(sets[i]))))
    # A node's name is its rank, zero-padded, so the smallest name of a component is its largest
    # clique and spanning_forest's default root is the one this module means.
    node = {index: f"{rank:09d}" for rank, index in enumerate(ranking)}
    index_of = {name: index for index, name in node.items()}
    scores = [
        (Decimal(len(sets[i] & sets[j])), node[i], node[j])
        for i in range(len(sets))
        for j in range(i + 1, len(sets))
        if sets[i] & sets[j]
    ]
    edges = spanning_forest(scores, categorical=set(node.values()))
    parent = {index_of[edge.child]: index_of[edge.parent] for edge in edges}
    _running_intersection(sets, parent, labels)
    steps: List[JunctionStep] = []
    # spanning_forest returns the edges by depth from each root, then by child name.
    children_order = [index_of[edge.child] for edge in edges]
    for root in sorted((i for i in range(len(sets)) if i not in parent), key=lambda i: node[i]):
        steps.append(_step(root, None, sets, order))
        for child in children_order:
            if _root_of(child, parent) == root:
                steps.append(_step(child, parent[child], sets, order))
    return tuple(steps)


def _root_of(index: int, parent: Dict[int, int]) -> int:
    while index in parent:
        index = parent[index]
    return index


def _step(
    index: int, parent: Optional[int], sets: Sequence["frozenset[str]"], order: Sequence[str]
) -> JunctionStep:
    shared = sets[index] if parent is None else sets[index] & sets[parent]
    own = sets[index] - (set() if parent is None else sets[parent])
    return JunctionStep(
        clique=index,
        parent=parent,
        separator=() if parent is None else tuple(name for name in order if name in shared),
        new=tuple(name for name in order if name in own),
    )


def _running_intersection(
    sets: Sequence["frozenset[str]"], parent: Dict[int, int], labels: Sequence[str]
) -> None:
    """Refuse (``FLX-3402``) cliques whose tree does not keep every column's cliques together."""
    for column in sorted(set().union(*sets) if sets else set()):
        holding = [index for index, columns in enumerate(sets) if column in columns]
        # Walk each holding clique up its tree while the parent holds the column too; the tops
        # reached are the connected pieces of the column's cliques.
        tops: Dict[int, int] = {}
        for index in holding:
            top = index
            while top in parent and column in sets[parent[top]]:
                top = parent[top]
            tops.setdefault(top, index)
        if len(tops) > 1:
            first, second = list(tops.values())[:2]
            raise FluxError(
                FLX_3402,
                f"the cliques {labels[first]} (columns {', '.join(sorted(sets[first]))}) and "
                f"{labels[second]} (columns {', '.join(sorted(sets[second]))}) both hold column "
                f"{column!r} and nothing joins them through other cliques that hold it, so the "
                "cliques have no junction tree (the running intersection property fails)",
            )
