# Copyright 2026 Agentics Transformation Limited
#
# SPDX-License-Identifier: Apache-2.0

"""Whether a history reads a projection of itself (ADR 0027).

A snapshot, an events emit or a periodic emit takes its rows from the history it is projected
from, so a history whose plan reads one of its own projections would be derived from rows that
are derived from it. The plans of such a history do not settle on one derivation, and a column
that depends on the link would need a dependency-aware derivation that this generator does not
have, so the shape is refused before any projection is derived (``FLX-3403``).

The decision is made over the emits, not their columns. An emit has an edge to every emit its
plan reads or sums, whatever drives the read (a rule, a ``within`` entry, a column drawn
``given`` a linked row, a weighted pick, a ``parentWhen``), and a projected emit has an edge to
its history. A history is refused when a projection of it can be reached from it: it then reads a
projection of itself, directly, through other emits, or through the projections of another
history that reads a projection of it in turn.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List, Mapping, Optional, Set

from flux_engine.core.canonical import nfc
from flux_engine.core.errors import FluxError
from flux_engine.generator.relations import FLX_3403
from flux_engine.generator.rows import GeneratorPlan

__all__ = ["refuse_self_reads"]


def refuse_self_reads(plans: Mapping[str, GeneratorPlan], projections: Mapping[str, str]) -> None:
    """Refuse (``FLX-3403``) a history that reads, by any path, a projection of itself.

    *plans* are the related plans by exposeId, with their reads and sums attached; *projections*
    maps each projected emit to the history it is projected from. The refusal names the history
    and the emits in order, from the history to the projection of it that it reads.
    """
    edges: Dict[str, Set[str]] = {
        nfc(expose_id): {nfc(target) for target, _ in plan.reads}
        | {nfc(child) for child, _, _ in plan.sums}
        for expose_id, plan in plans.items()
    }
    for projected, history in projections.items():
        edges.setdefault(nfc(projected), set()).add(nfc(history))
    for history in sorted({nfc(held) for held in projections.values()}):
        own = {nfc(emit) for emit, held in projections.items() if nfc(held) == history}
        path = _path_to(history, own, edges)
        if path is not None:
            raise FluxError(
                FLX_3403,
                f"history {history!r} reads a projection of itself: {' -> '.join(path)}. A history "
                "whose plan reads a projection of itself, directly or through other emits, is not "
                f"generated; read a drawn column of {history!r} instead",
            )


def _path_to(start: str, targets: Set[str], edges: Mapping[str, Set[str]]) -> Optional[List[str]]:
    """The emits from *start* to the nearest of *targets*, in order, or ``None`` when none is reached."""
    came: Dict[str, Optional[str]] = {start: None}
    queue: Deque[str] = deque([start])
    while queue:
        emit = queue.popleft()
        for next_emit in sorted(edges.get(emit, ())):
            if next_emit in came:
                continue
            came[next_emit] = emit
            if next_emit in targets:
                path = [next_emit]
                while (before := came[path[-1]]) is not None:
                    path.append(before)
                return path[::-1]
            queue.append(next_emit)
    return None
