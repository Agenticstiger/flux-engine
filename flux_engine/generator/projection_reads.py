# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a history that reads its own projection through a link has rows that settle (ADR 0027).

A history can link to a table projected from it, as a self-referencing table does: an employee
version to the current row of its manager. The projection copies the history's columns, so each
row of the history reads a row that copies a row of the history. The rows settle when the columns
the history reads through the link do not depend on the link: the projection is then derived
twice, once to read the right cells of the link and once to copy the cells that were set from
them, and no more.

A column read through the link that is set from the link, or from a column that is, would need a
derivation more for each step it is away from a column that is not, and a column that is set from
itself never settles. Such a history is refused (``FLX-3403``) before any projection is derived.

The dependencies are a graph over the columns ``(exposeId, column)`` of the plans: what a rule
reads (:meth:`~flux_engine.generator.rules.RulePlan.column_reads`), the columns a column is drawn
given, the column a linked given reads, and the copy of each column of a projected emit from the
history's. A column is *tainted* when it reaches a column of a projection of its own history
without passing through a copy from that history.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.generator.rows import GeneratorPlan

__all__ = ["refuse_unsettled_reads"]

Node = Tuple[str, str]
#: A node and the relationships followed to reach it from the node that reads it.
Hop = Tuple[Node, Tuple[str, ...]]


class _Columns:
    """The columns the plans read, as a graph a search can follow."""

    def __init__(self, plans: Iterable[GeneratorPlan]) -> None:
        self.plans = {nfc(plan.expose.expose_id): plan for plan in plans}
        self._reads: Dict[str, Mapping[str, Iterable[Tuple[str, str, Tuple[str, ...]]]]] = {}
        self._edges: Dict[Node, Tuple[Hop, ...]] = {}

    def histories(self) -> Dict[str, Set[str]]:
        """Each history that has a projection, and the exposes projected from it."""
        found: Dict[str, Set[str]] = {}
        for expose_id, plan in self.plans.items():
            if plan.projection is not None:
                found.setdefault(nfc(plan.projection.of), set()).add(expose_id)
        return found

    def columns_of(self, expose_id: str) -> List[str]:
        plan = self.plans.get(expose_id)
        return [] if plan is None else [held.column.name for held in plan.columns]

    def edges(self, node: Node) -> Tuple[Hop, ...]:
        """What the column *node* is set from, and (last) the history column it copies, if any."""
        if node not in self._edges:
            self._edges[node] = self._found(node)
        return self._edges[node]

    def _found(self, node: Node) -> Tuple[Hop, ...]:
        expose_id, column = node
        plan = self.plans.get(expose_id)
        if plan is None:
            return ()
        found: List[Hop] = []
        if plan.rules is not None:
            if expose_id not in self._reads:
                self._reads[expose_id] = plan.rules.column_reads()
            found.extend(
                ((nfc(read), name), path)
                for read, name, path in self._reads[expose_id].get(column, ())
            )
        held = next((item for item in plan.columns if item.column.name == column), None)
        if held is not None:
            found.extend(((expose_id, parent), ()) for parent in held.statistics.parents)
        if plan.relations is not None:
            found.extend(
                ((nfc(given.parent), given.parent_column), (given.relationship,))
                for given in plan.relations.givens
                if given.column == column
            )
        if plan.projection is not None:
            origin = nfc(plan.projection.of)
            if column in self.columns_of(origin):
                found.append(((origin, column), ()))
        return tuple(found)

    def reach(self, starts: Iterable[Node], history: str) -> Dict[Node, Optional[Tuple[Node, Hop]]]:
        """Every column the *starts* are set from, with the hop that reached it first.

        A copy of a column of *history* from *history* is not followed: what lies beyond it is the
        history's own columns, which are what the search is about.
        """
        reached: Dict[Node, Optional[Tuple[Node, Hop]]] = {start: None for start in starts}
        queue: Deque[Node] = deque(reached)
        while queue:
            node = queue.popleft()
            plan = self.plans.get(node[0])
            copies = (
                plan is not None
                and plan.projection is not None
                and (nfc(plan.projection.of) == history)
            )
            for hop in self.edges(node):
                target, path = hop
                if copies and target[0] == history and not path:
                    continue
                if target not in reached:
                    reached[target] = (node, hop)
                    queue.append(target)
        return reached


def refuse_unsettled_reads(plans: Iterable[GeneratorPlan]) -> None:
    """Refuse (``FLX-3403``) a history that reads through a link a column that depends on the link.

    *plans* are the plans of every emit, with their reads attached and each projected emit's
    projection. A history reads its own projection when a column it is set from is a column of a
    table projected from it; the column it reads is *tainted* when the history sets it from such a
    column too. The refusal names the history, the link and the column.
    """
    graph = _Columns(plans)
    for history, projected in sorted(graph.histories().items()):
        names = graph.columns_of(history)
        reads = graph.reach([(history, name) for name in names], history)
        for node in sorted(reads):
            if node[0] not in projected or node[1] not in names:
                continue
            own = graph.reach([(history, node[1])], history)
            inside = sorted(found for found in own if found[0] in projected)
            if inside:
                _refuse(history, node, inside[0], reads)


def _refuse(
    history: str, read: Node, set_from: Node, reads: Mapping[Node, Optional[Tuple[Node, Hop]]]
) -> None:
    """Raise the refusal for *read*, a column of a projection of *history* that is tainted.

    *set_from* is the column of a projection the history sets it from, and *reads* the search that
    found *read*, whose first hop through a link names the link.
    """
    hops: List[Tuple[Node, Hop]] = []
    cursor = reads[read]
    while cursor is not None:
        hops.append(cursor)
        cursor = reads[cursor[0]]
    link = next(
        (".".join(hop[1]) for _, hop in reversed(hops) if hop[1]),
        "",
    )
    expose_id, column = read
    raise FluxError(
        _code(),
        f"{history!r} reads {expose_id}.{column} through {link!r}, and that column depends on that "
        f"link: {expose_id!r} copies it from {history!r}, which sets {history}.{column} from "
        f"{set_from[0]}.{set_from[1]}. The rows of a history that reads its own projection settle "
        "only when the columns it reads through the link do not depend on the link; read a column "
        f"of {history!r} that is drawn, or does not read {expose_id!r}",
    )


def _code() -> Any:
    from flux_engine.generator.relations import FLX_3403

    return FLX_3403
